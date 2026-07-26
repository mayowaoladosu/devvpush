"""Deterministic ARQ jobs for persistent storage lifecycle transitions."""

from __future__ import annotations

from datetime import datetime

from arq.connections import ArqRedis


class StorageJobs:
    @staticmethod
    def job_id(storage_id: str, action: str, updated_at: datetime) -> str:
        transition = updated_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return f"storage:{storage_id}:{action}:{transition}"

    @classmethod
    async def enqueue(
        cls,
        queue: ArqRedis,
        *,
        storage_id: str,
        status: str,
        updated_at: datetime,
    ):
        jobs = {
            "pending": ("provision_storage", "provision"),
            "resetting": ("reset_storage", "reset"),
            "deleted": ("deprovision_storage", "deprovision"),
        }
        job = jobs.get(status)
        if not job:
            return None
        function, action = job
        return await queue.enqueue_job(
            function,
            storage_id,
            _job_id=cls.job_id(storage_id, action, updated_at),
        )
