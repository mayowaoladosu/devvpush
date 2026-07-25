import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arq.jobs import JobStatus

from services.deployment_diagnostics import DeploymentDiagnosticService
from services.deployment_heartbeat import DeploymentHeartbeat
from services.deployment_jobs import DeploymentJobs
from services.deployment_reconciler import DeploymentReconciler


class FakeSession:
    def __init__(self, deployment=None, rowcount=1):
        self.deployment = deployment
        self.rowcount = rowcount
        self.execute = AsyncMock(
            return_value=SimpleNamespace(rowcount=rowcount)
        )
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, _model, _deployment_id):
        return self.deployment


class FakeJob:
    def __init__(self, status, result_info=None):
        self._status = status
        self._result_info = result_info

    async def status(self):
        return self._status

    async def result_info(self):
        return self._result_info


class DeploymentObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return SimpleNamespace(
            deployment_worker_heartbeat_seconds=60,
            deployment_orphan_timeout_seconds=20,
            deployment_orphan_confirm_seconds=1,
            deployment_queue_grace_seconds=30,
            github_app_private_key="",
            github_app_webhook_secret="",
            github_app_client_secret="",
            google_client_secret="",
            resend_api_key="",
            smtp_password="",
            secret_key="",
            encryption_key="",
            postgres_password="",
        )

    def deployment(
        self,
        *,
        status="prepare",
        heartbeat=None,
        created_at=None,
    ):
        return SimpleNamespace(
            id="deployment-id",
            job_id="deployment-id",
            status=status,
            conclusion=None,
            worker_job_id="deployment-id",
            worker_phase=status,
            worker_attempt=2,
            worker_heartbeat_at=heartbeat,
            created_at=created_at
            or datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
        )

    def redis(self, healthy=True):
        return SimpleNamespace(exists=AsyncMock(return_value=1 if healthy else 0))

    def test_failure_payload_is_structured_and_redacted(self):
        settings = self.settings()
        settings.github_app_client_secret = "super-secret-value"
        fake_token = "github_" + "pat_" + "abcdefghijklmnopqrstuvwxyz"

        with patch(
            "services.deployment_diagnostics.get_settings",
            return_value=settings,
        ):
            payload = DeploymentDiagnosticService.failure_payload(
                stage="prepare",
                code="worker_crash",
                message=f"Bearer {fake_token} super-secret-value",
                source="watchdog",
                attempt=3,
                hint="Retry safely",
                details={"token": "super-secret-value"},
            )

        self.assertEqual("worker_crash", payload["code"])
        self.assertEqual("watchdog", payload["source"])
        self.assertEqual(3, payload["attempt"])
        self.assertNotIn("super-secret-value", str(payload))
        self.assertNotIn(fake_token, str(payload))
        self.assertIn("[REDACTED]", str(payload))

    def test_durable_and_loki_logs_merge_in_timestamp_order(self):
        loki = [{"timestamp": "30", "message": "runtime"}]
        diagnostics = [
            {"timestamp": "10", "message": "started"},
            {"timestamp": "20", "message": "watchdog"},
        ]

        merged = DeploymentDiagnosticService.merge_logs(
            loki, diagnostics, limit=2
        )

        self.assertEqual(["watchdog", "runtime"], [item["message"] for item in merged])

    async def test_heartbeat_records_and_clears_worker_lease(self):
        session = FakeSession()
        diagnostic = AsyncMock(return_value=True)
        ctx = {"job_id": "job-id", "job_try": 2}

        with (
            patch(
                "services.deployment_heartbeat.AsyncSessionLocal",
                side_effect=[session, session],
            ),
            patch(
                "services.deployment_heartbeat.DeploymentDiagnosticService.record_external",
                diagnostic,
            ),
            patch(
                "services.deployment_heartbeat.get_settings",
                return_value=self.settings(),
            ),
        ):
            heartbeat = DeploymentHeartbeat("deployment-id", ctx, "prepare")
            await heartbeat.start()
            await heartbeat.stop()

        self.assertTrue(heartbeat.active)
        self.assertEqual(2, session.execute.await_count)
        diagnostic.assert_awaited_once()

    async def test_concluded_deployment_skips_heartbeat(self):
        session = FakeSession(rowcount=0)
        diagnostic = AsyncMock(return_value=True)

        with (
            patch(
                "services.deployment_heartbeat.AsyncSessionLocal",
                return_value=session,
            ),
            patch(
                "services.deployment_heartbeat.DeploymentDiagnosticService.record_external",
                diagnostic,
            ),
            patch(
                "services.deployment_heartbeat.get_settings",
                return_value=self.settings(),
            ),
        ):
            heartbeat = DeploymentHeartbeat(
                "deployment-id", {"job_id": "job-id"}, "prepare"
            )
            await heartbeat.start()
            await heartbeat.stop()

        self.assertFalse(heartbeat.active)
        diagnostic.assert_not_awaited()

    async def test_fresh_heartbeat_protects_long_running_build(self):
        deployment = self.deployment(
            heartbeat=datetime.now(UTC).replace(tzinfo=None)
        )
        session = FakeSession(deployment=deployment)
        job_factory = AsyncMock()

        with (
            patch(
                "services.deployment_reconciler.AsyncSessionLocal",
                return_value=session,
            ),
            patch("services.deployment_reconciler.Job", job_factory),
        ):
            incident = await DeploymentReconciler(
                self.redis(), self.settings()
            ).inspect(deployment.id)

        self.assertIsNone(incident)
        job_factory.assert_not_called()

    async def test_stale_in_progress_job_reports_worker_heartbeat_loss(self):
        deployment = self.deployment(
            heartbeat=(datetime.now(UTC) - timedelta(minutes=1)).replace(
                tzinfo=None
            )
        )
        session = FakeSession(deployment=deployment)
        fake_job = FakeJob(JobStatus.in_progress)

        with (
            patch(
                "services.deployment_reconciler.AsyncSessionLocal",
                return_value=session,
            ),
            patch(
                "services.deployment_reconciler.Job", return_value=fake_job
            ),
        ):
            incident = await DeploymentReconciler(
                self.redis(), self.settings()
            ).inspect(deployment.id)

        self.assertIsNotNone(incident)
        self.assertEqual("worker_heartbeat_expired", incident.code)
        self.assertEqual("in_progress", incident.job_status)
        self.assertEqual(2, incident.attempt)

    async def test_queued_job_without_heartbeat_is_not_orphaned(self):
        deployment = self.deployment(heartbeat=None)
        session = FakeSession(deployment=deployment)
        fake_job = FakeJob(JobStatus.queued)

        with (
            patch(
                "services.deployment_reconciler.AsyncSessionLocal",
                return_value=session,
            ),
            patch(
                "services.deployment_reconciler.Job", return_value=fake_job
            ),
        ):
            incident = await DeploymentReconciler(
                self.redis(), self.settings()
            ).inspect(deployment.id)

        self.assertIsNone(incident)

    async def test_queued_job_without_worker_health_is_recoverable(self):
        deployment = self.deployment(heartbeat=None)
        session = FakeSession(deployment=deployment)
        fake_job = FakeJob(JobStatus.queued)

        with (
            patch(
                "services.deployment_reconciler.AsyncSessionLocal",
                return_value=session,
            ),
            patch(
                "services.deployment_reconciler.Job", return_value=fake_job
            ),
        ):
            incident = await DeploymentReconciler(
                self.redis(healthy=False), self.settings()
            ).inspect(deployment.id)

        self.assertIsNotNone(incident)
        self.assertEqual("jobs_worker_unavailable", incident.code)

    async def test_failed_arq_result_becomes_recoverable_incident(self):
        deployment = self.deployment(heartbeat=None)
        session = FakeSession(deployment=deployment)
        result = SimpleNamespace(
            success=False,
            result=RuntimeError("worker exploded"),
            finish_time=datetime.now(UTC),
        )
        fake_job = FakeJob(JobStatus.complete, result)

        with (
            patch(
                "services.deployment_reconciler.AsyncSessionLocal",
                return_value=session,
            ),
            patch(
                "services.deployment_reconciler.Job", return_value=fake_job
            ),
            patch(
                "services.deployment_diagnostics.get_settings",
                return_value=self.settings(),
            ),
        ):
            incident = await DeploymentReconciler(
                self.redis(), self.settings()
            ).inspect(deployment.id)

        self.assertIsNotNone(incident)
        self.assertEqual("job_failed_without_transition", incident.code)
        self.assertIn("RuntimeError", incident.message)

    async def test_lifecycle_jobs_use_deterministic_ids(self):
        queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=SimpleNamespace()))

        await DeploymentJobs.enqueue_finalize(queue, "deployment-id")
        await DeploymentJobs.enqueue_failure(
            queue,
            "deployment-id",
            "prepare",
            "failed",
        )

        self.assertEqual(
            "fdeployment-id",
            queue.enqueue_job.await_args_list[0].kwargs["_job_id"],
        )
        self.assertEqual(
            "xdeployment-id",
            queue.enqueue_job.await_args_list[1].kwargs["_job_id"],
        )


if __name__ == "__main__":
    unittest.main()
