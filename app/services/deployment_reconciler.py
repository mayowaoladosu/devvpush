"""Reconcile deployment lifecycle state after worker loss or queue failure."""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from arq.connections import ArqRedis
from arq.constants import default_queue_name, health_check_key_suffix
from arq.jobs import Job, JobStatus
from redis.exceptions import LockError
from sqlalchemy import select

from config import Settings
from db import AsyncSessionLocal
from models import Deployment
from services.deployment_diagnostics import DeploymentDiagnosticService
from services.deployment_jobs import DeploymentJobs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeploymentRecoveryIncident:
    deployment_id: str
    deployment_status: str
    stage: str
    code: str
    message: str
    job_id: str | None
    job_status: str
    attempt: int
    details: dict[str, object]


RecoveryCallback = Callable[[DeploymentRecoveryIncident], Awaitable[None]]


class DeploymentReconciler:
    """Confirm and recover lifecycle jobs whose durable heartbeat disappeared."""

    _ACTIVE_STATUSES = ("prepare", "finalize", "fail")

    def __init__(self, redis: ArqRedis, settings: Settings):
        self.redis = redis
        self.settings = settings
        self._suspected_at: dict[str, float] = {}

    async def run_once(self, recover: RecoveryCallback) -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Deployment.id).where(
                    Deployment.conclusion.is_(None),
                    Deployment.status.in_(self._ACTIVE_STATUSES),
                )
            )
            deployment_ids = list(result.scalars().all())

        active = set(deployment_ids)
        for deployment_id in set(self._suspected_at) - active:
            self._suspected_at.pop(deployment_id, None)

        recovered = 0
        now = time.monotonic()
        for deployment_id in deployment_ids:
            incident = await self.inspect(deployment_id)
            if incident is None:
                self._suspected_at.pop(deployment_id, None)
                continue

            first_seen = self._suspected_at.setdefault(deployment_id, now)
            if now - first_seen < self.settings.deployment_orphan_confirm_seconds:
                continue

            lock = self.redis.lock(
                f"lock:deployment-recovery:{deployment_id}",
                timeout=max(60, self.settings.deployment_orphan_timeout_seconds * 3),
                blocking_timeout=0,
            )
            if not await lock.acquire(blocking=False):
                continue
            try:
                confirmed = await self.inspect(deployment_id)
                if confirmed is None:
                    self._suspected_at.pop(deployment_id, None)
                    continue
                await DeploymentDiagnosticService.record_external(
                    deployment_id,
                    level="ERROR" if confirmed.deployment_status != "finalize" else "WARNING",
                    source="watchdog",
                    stage=confirmed.stage,
                    code="watchdog_recovery_started",
                    message=(
                        "The watchdog confirmed an abandoned lifecycle job and "
                        "started automatic recovery."
                    ),
                    details={
                        **confirmed.details,
                        "reason_code": confirmed.code,
                        "reason": confirmed.message,
                    },
                    attempt=confirmed.attempt,
                )
                await recover(confirmed)
                self._suspected_at.pop(deployment_id, None)
                recovered += 1
            finally:
                with suppress(LockError):
                    await lock.release()
        return recovered

    async def inspect(self, deployment_id: str) -> DeploymentRecoveryIncident | None:
        async with AsyncSessionLocal() as db:
            deployment = await db.get(Deployment, deployment_id)
            if (
                not deployment
                or deployment.conclusion
                or deployment.status not in self._ACTIVE_STATUSES
            ):
                return None

            now = datetime.now(timezone.utc)
            heartbeat = deployment.worker_heartbeat_at
            if heartbeat and heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            if heartbeat and now - heartbeat <= timedelta(
                seconds=self.settings.deployment_orphan_timeout_seconds
            ):
                return None

            created_at = deployment.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (now - created_at).total_seconds())
            job_id = DeploymentJobs.expected_id(deployment)
            job_status = JobStatus.not_found
            result_info = None
            if job_id:
                job = Job(job_id=job_id, redis=self.redis)
                job_status = await job.status()
                if job_status == JobStatus.complete:
                    result_info = await job.result_info()

            worker_health_key = default_queue_name + health_check_key_suffix
            worker_healthy = bool(await self.redis.exists(worker_health_key))
            if job_status in {JobStatus.queued, JobStatus.deferred} and (
                worker_healthy
                or age_seconds < self.settings.deployment_queue_grace_seconds
            ):
                return None
            if (
                job_status == JobStatus.in_progress
                and not heartbeat
                and age_seconds < self.settings.deployment_queue_grace_seconds
            ):
                return None
            if (
                job_status == JobStatus.not_found
                and not heartbeat
                and age_seconds < self.settings.deployment_queue_grace_seconds
            ):
                return None

            stage = deployment.worker_phase or deployment.status
            attempt = max(1, int(deployment.worker_attempt or 0))
            details: dict[str, object] = {
                "job_id": job_id or "",
                "job_status": job_status.value,
                "deployment_status": deployment.status,
                "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
                "age_seconds": round(age_seconds, 3),
                "jobs_worker_healthy": worker_healthy,
            }

            if job_status in {JobStatus.queued, JobStatus.deferred}:
                code = "jobs_worker_unavailable"
                message = (
                    "The lifecycle job remained queued after the jobs worker "
                    "health lease expired."
                )
            elif job_status == JobStatus.in_progress:
                code = "worker_heartbeat_expired" if heartbeat else "worker_heartbeat_missing"
                message = (
                    "The deployment worker stopped reporting progress while its "
                    "queue job was still marked in progress."
                )
            elif job_status == JobStatus.complete:
                success = bool(result_info and result_info.success)
                details["job_success"] = success
                if result_info:
                    details["job_finished_at"] = result_info.finish_time.isoformat()
                if success:
                    code = "job_completed_without_transition"
                    message = (
                        "The lifecycle job completed without advancing the "
                        "deployment to a terminal or recoverable state."
                    )
                else:
                    code = "job_failed_without_transition"
                    result = result_info.result if result_info else "unknown failure"
                    details["result_type"] = result.__class__.__name__
                    message = (
                        "The lifecycle job failed before it could persist the "
                        "deployment failure: "
                        f"{DeploymentDiagnosticService.exception_message(result if isinstance(result, BaseException) else RuntimeError(str(result)))}"
                    )
            else:
                code = "lifecycle_job_missing"
                message = (
                    "The deployment lifecycle job is missing from the queue and "
                    "no worker heartbeat is active."
                )

            return DeploymentRecoveryIncident(
                deployment_id=deployment.id,
                deployment_status=deployment.status,
                stage=stage,
                code=code,
                message=message,
                job_id=job_id,
                job_status=job_status.value,
                attempt=attempt,
                details=details,
            )
