"""Deterministic ARQ job identities for deployment lifecycle recovery."""

from __future__ import annotations

from typing import Any

from arq.connections import ArqRedis


class DeploymentJobs:
    @staticmethod
    def start_id(deployment_id: str) -> str:
        return deployment_id

    @staticmethod
    def finalize_id(deployment_id: str) -> str:
        return f"f{deployment_id}"

    @staticmethod
    def failure_id(deployment_id: str) -> str:
        return f"x{deployment_id}"

    @classmethod
    def expected_id(cls, deployment) -> str | None:
        if deployment.status == "prepare":
            return deployment.job_id or cls.start_id(deployment.id)
        if deployment.status == "finalize":
            return cls.finalize_id(deployment.id)
        if deployment.status == "fail":
            return cls.failure_id(deployment.id)
        return None

    @classmethod
    async def enqueue_finalize(cls, queue: ArqRedis, deployment_id: str):
        return await queue.enqueue_job(
            "finalize_deployment",
            deployment_id,
            _job_id=cls.finalize_id(deployment_id),
        )

    @classmethod
    async def enqueue_failure(
        cls,
        queue: ArqRedis,
        deployment_id: str,
        stage: str,
        reason: str,
        *,
        code: str = "deployment_failed",
        source: str = "worker",
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ):
        return await queue.enqueue_job(
            "fail_deployment",
            deployment_id,
            stage,
            reason,
            code,
            source,
            details,
            hint,
            _job_id=cls.failure_id(deployment_id),
        )
