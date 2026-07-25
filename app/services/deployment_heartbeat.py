"""Database-backed liveness leases for deployment lifecycle jobs."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from functools import wraps

from sqlalchemy import update

from config import get_settings
from db import AsyncSessionLocal
from models import Deployment, utc_now
from services.deployment_diagnostics import DeploymentDiagnosticService

logger = logging.getLogger(__name__)


def deployment_heartbeat(phase: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(ctx, deployment_id: str, *args, **kwargs):
            heartbeat = DeploymentHeartbeat(deployment_id, ctx, phase)
            await heartbeat.start()
            try:
                return await function(ctx, deployment_id, *args, **kwargs)
            finally:
                await heartbeat.stop()

        return wrapped

    return decorate


class DeploymentHeartbeat:
    """Refresh a deployment worker lease until the lifecycle task exits."""

    def __init__(self, deployment_id: str, ctx: dict, phase: str):
        self.deployment_id = deployment_id
        self.job_id = str(ctx.get("job_id") or f"direct-{phase}-{deployment_id}")
        self.attempt = max(0, int(ctx.get("job_try") or 0))
        self.phase = phase
        self.interval = get_settings().deployment_worker_heartbeat_seconds
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self.active = False

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, exc_type, exc, traceback):
        await self.stop()

    async def start(self):
        self.active = await self._touch()
        if not self.active:
            return self
        await DeploymentDiagnosticService.record_external(
            self.deployment_id,
            level="INFO",
            source="worker",
            stage=self.phase,
            code="job_started",
            message=(
                f"Deployment {self.phase} job started "
                f"(attempt {self.attempt or 1})."
            ),
            details={"job_id": self.job_id},
            attempt=self.attempt or 1,
        )
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self) -> None:
        if not self.active:
            return
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._clear()

    async def set_phase(self, phase: str) -> None:
        self.phase = phase
        await self._touch()

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
                return
            except TimeoutError:
                try:
                    if not await self._touch():
                        return
                except Exception:
                    logger.warning(
                        "Could not refresh heartbeat for deployment %s.",
                        self.deployment_id,
                        exc_info=True,
                    )

    async def _touch(self) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Deployment)
                .where(
                    Deployment.id == self.deployment_id,
                    Deployment.conclusion.is_(None),
                )
                .values(
                    worker_job_id=self.job_id,
                    worker_phase=self.phase,
                    worker_attempt=self.attempt,
                    worker_heartbeat_at=utc_now(),
                )
            )
            await db.commit()
            return result.rowcount > 0

    async def _clear(self) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Deployment)
                    .where(
                        Deployment.id == self.deployment_id,
                        Deployment.worker_job_id == self.job_id,
                    )
                    .values(
                        worker_job_id=None,
                        worker_phase=None,
                        worker_heartbeat_at=None,
                    )
                )
                await db.commit()
        except Exception:
            logger.warning(
                "Could not clear heartbeat for deployment %s.",
                self.deployment_id,
                exc_info=True,
            )
