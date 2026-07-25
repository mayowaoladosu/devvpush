import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arq.jobs import JobStatus

from services.deployment import DeploymentService
from workers.tasks.deployment import _cleanup_if_concluded


class FakeLock:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("lock-enter")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append("lock-exit")


class FakeRedis:
    def __init__(self, events):
        self.events = events
        self.lock_kwargs = None

    def lock(self, name, **kwargs):
        self.lock_kwargs = {"name": name, **kwargs}
        return FakeLock(self.events)


class FakeQueue:
    def __init__(self, events, error=None, result=True):
        self.events = events
        self.error = error
        self.result = result
        self.calls = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.events.append(f"enqueue:{name}")
        self.calls.append((name, args, kwargs))
        if self.error:
            raise self.error
        if not self.result:
            return None
        return SimpleNamespace(job_id=kwargs.get("_job_id") or "job-id")


class FakeDb:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _instance, **_kwargs):
        return None

    async def execute(self, _query):
        rows = self.rows

        class Scalars:
            def all(self):
                return rows

        return SimpleNamespace(scalars=lambda: Scalars())


class DeploymentSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def project(self):
        return SimpleNamespace(
            id="project-id",
            active_environments=[
                {
                    "id": "prod",
                    "name": "Production",
                    "slug": "production",
                    "branch": "main",
                }
            ],
        )

    def deployment(self, deployment_id="new-deployment"):
        return SimpleNamespace(
            id=deployment_id,
            project_id="project-id",
            environment_id="prod",
            branch="main",
            commit_sha="b" * 40,
            job_id=None,
            status="prepare",
            conclusion=None,
            container_id=None,
            container_status=None,
            trigger="webhook",
            created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
            concluded_at=None,
            error=None,
            project=SimpleNamespace(updated_at=None),
        )

    async def test_webhook_schedule_enqueues_before_marking_older_deployments(self):
        events = []
        service = DeploymentService()
        new_deployment = self.deployment()
        old_deployment = self.deployment("old-deployment")
        service.create = AsyncMock(
            side_effect=lambda **_kwargs: (
                events.append("create") or new_deployment
            )
        )
        service._find_webhook_deployment = AsyncMock(return_value=None)
        service._mark_superseded_webhook_deployments = AsyncMock(
            side_effect=lambda **_kwargs: (
                events.append("mark-superseded") or [old_deployment]
            )
        )
        service._abort_job = AsyncMock(
            side_effect=lambda *_args: events.append("abort-old")
        )
        service._stop_container = AsyncMock(
            side_effect=lambda *_args: events.append("stop-old")
        )
        redis = FakeRedis(events)
        queue = FakeQueue(events)
        db = FakeDb()

        result = await service.schedule(
            project=self.project(),
            branch="main",
            commit={"sha": "b" * 40, "provider_event_id": "delivery-1"},
            db=db,
            redis_client=redis,
            queue=queue,
            trigger="webhook",
        )

        self.assertIs(new_deployment, result)
        self.assertEqual(new_deployment.id, new_deployment.job_id)
        self.assertEqual(
            new_deployment.id,
            queue.calls[0][2]["_job_id"],
        )
        self.assertEqual(
            [
                "lock-enter",
                "create",
                "enqueue:start_deployment",
                "mark-superseded",
                "lock-exit",
                "abort-old",
                "stop-old",
            ],
            events,
        )
        self.assertEqual(
            "lock:deployment-schedule:project-id:prod",
            redis.lock_kwargs["name"],
        )

    async def test_duplicate_webhook_delivery_is_idempotent(self):
        events = []
        service = DeploymentService()
        existing = self.deployment("existing-deployment")
        existing.job_id = "existing-job"
        service.create = AsyncMock()
        service._find_webhook_deployment = AsyncMock(return_value=existing)
        service._mark_superseded_webhook_deployments = AsyncMock()
        queue = FakeQueue(events, result=False)
        queued_job = SimpleNamespace(
            status=AsyncMock(return_value=JobStatus.queued)
        )

        with patch("services.deployment.Job", return_value=queued_job):
            result = await service.schedule(
                project=self.project(),
                branch="main",
                commit={"sha": "b" * 40, "provider_event_id": "delivery-1"},
                db=FakeDb(),
                redis_client=FakeRedis(events),
                queue=queue,
                trigger="webhook",
            )

        self.assertIs(existing, result)
        self.assertEqual(
            "delivery-1",
            service._find_webhook_deployment.await_args.kwargs[
                "provider_event_id"
            ],
        )
        service.create.assert_not_awaited()
        service._mark_superseded_webhook_deployments.assert_not_awaited()
        self.assertEqual(1, len(queue.calls))
        queued_job.status.assert_awaited_once()

    async def test_same_commit_without_delivery_id_can_be_deployed_again(self):
        events = []
        service = DeploymentService()
        new_deployment = self.deployment()
        service.create = AsyncMock(return_value=new_deployment)
        service._find_webhook_deployment = AsyncMock()
        service._mark_superseded_webhook_deployments = AsyncMock(return_value=[])

        result = await service.schedule(
            project=self.project(),
            branch="main",
            commit={"sha": "b" * 40},
            db=FakeDb(),
            redis_client=FakeRedis(events),
            queue=FakeQueue(events),
            trigger="webhook",
        )

        self.assertIs(new_deployment, result)
        service._find_webhook_deployment.assert_not_awaited()
        service.create.assert_awaited_once()

    async def test_manual_deployment_never_supersedes_other_work(self):
        events = []
        service = DeploymentService()
        new_deployment = self.deployment()
        service.create = AsyncMock(return_value=new_deployment)
        service._find_webhook_deployment = AsyncMock()
        service._mark_superseded_webhook_deployments = AsyncMock()

        await service.schedule(
            project=self.project(),
            branch="main",
            commit={"sha": "b" * 40},
            db=FakeDb(),
            redis_client=FakeRedis(events),
            queue=FakeQueue(events),
            trigger="user",
        )

        service._find_webhook_deployment.assert_not_awaited()
        service._mark_superseded_webhook_deployments.assert_not_awaited()

    async def test_queue_failure_concludes_created_deployment(self):
        events = []
        service = DeploymentService()
        new_deployment = self.deployment()
        service.create = AsyncMock(return_value=new_deployment)
        update_status = AsyncMock()

        with patch.object(DeploymentService, "update_status", update_status):
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                await service.schedule(
                    project=self.project(),
                    branch="main",
                    commit={"sha": "b" * 40},
                    db=FakeDb(),
                    redis_client=FakeRedis(events),
                    queue=FakeQueue(events, RuntimeError("queue unavailable")),
                )

        kwargs = update_status.await_args.kwargs
        self.assertEqual("completed", kwargs["status"])
        self.assertEqual("failed", kwargs["conclusion"])
        self.assertEqual("prepare", kwargs["error"]["status"])
        self.assertEqual("queue_admission_failed", kwargs["error"]["code"])
        self.assertEqual("queue", kwargs["error"]["source"])

    async def test_superseded_webhooks_are_marked_skipped_and_cleaned(self):
        replacement = self.deployment("replacement-deployment")
        first = self.deployment("first-deployment")
        second = self.deployment("second-deployment")
        queue = FakeQueue([])
        update_status = AsyncMock()

        with patch.object(DeploymentService, "update_status", update_status):
            outdated = await DeploymentService()._mark_superseded_webhook_deployments(
                replacement=replacement,
                db=FakeDb([first, second]),
                redis_client=SimpleNamespace(),
                queue=queue,
            )

        self.assertEqual([first, second], outdated)
        self.assertEqual(2, update_status.await_count)
        for call in update_status.await_args_list:
            self.assertEqual("completed", call.kwargs["status"])
            self.assertEqual("skipped", call.kwargs["conclusion"])
            self.assertEqual(
                replacement.id, call.kwargs["error"]["deployment_id"]
            )
        self.assertEqual(
            ["delete_container", "delete_container"],
            [call[0] for call in queue.calls],
        )

    async def test_cancel_uses_bounded_abort_and_idempotent_cleanup(self):
        events = []
        service = DeploymentService()
        deployment = self.deployment()
        update_status = AsyncMock()
        service._queue_cleanup = AsyncMock()
        service._abort_job = AsyncMock()
        service._stop_container = AsyncMock()

        with patch.object(DeploymentService, "update_status", update_status):
            result = await service.cancel(
                project=self.project(),
                deployment=deployment,
                queue=SimpleNamespace(),
                redis_client=FakeRedis(events),
                db=FakeDb(),
            )

        self.assertIs(deployment, result)
        self.assertEqual("canceled", update_status.await_args.kwargs["conclusion"])
        service._queue_cleanup.assert_awaited_once()
        service._abort_job.assert_awaited_once()
        service._stop_container.assert_awaited_once()
        self.assertEqual(["lock-enter", "lock-exit"], events)

    async def test_terminal_conclusion_cannot_be_overwritten_or_revived(self):
        deployment = self.deployment()
        deployment.status = "completed"
        deployment.conclusion = "skipped"
        deployment.container_status = "removed"
        deployment.error = {"status": "superseded"}
        redis = SimpleNamespace(xadd=AsyncMock())

        changed = await DeploymentService.update_status(
            FakeDb(),
            deployment,
            status="deploy",
            conclusion="succeeded",
            container_status="running",
            error={"status": "deploy"},
            redis_client=redis,
        )

        self.assertFalse(changed)
        self.assertEqual("completed", deployment.status)
        self.assertEqual("skipped", deployment.conclusion)
        self.assertEqual("removed", deployment.container_status)
        self.assertEqual({"status": "superseded"}, deployment.error)
        redis.xadd.assert_not_awaited()

    async def test_abort_timeout_does_not_block_replacement(self):
        deployment = self.deployment()
        deployment.job_id = "job-id"
        fake_job = SimpleNamespace(
            info=AsyncMock(return_value=SimpleNamespace(success=None)),
            abort=AsyncMock(side_effect=TimeoutError),
        )

        with patch("services.deployment.Job", return_value=fake_job):
            aborted = await DeploymentService._abort_job(
                deployment, SimpleNamespace()
            )

        self.assertFalse(aborted)
        fake_job.abort.assert_awaited_once()

    async def test_newer_successful_deployment_blocks_stale_alias_promotion(self):
        deployment = self.deployment()
        result = SimpleNamespace(scalar_one_or_none=lambda: "newer-deployment")
        db = SimpleNamespace(execute=AsyncMock(return_value=result))

        is_stale = (
            await DeploymentService.has_newer_successful_deployment(
                deployment, db
            )
        )

        self.assertTrue(is_stale)
        db.execute.assert_awaited_once()

    async def test_manual_finalization_is_never_suppressed(self):
        deployment = self.deployment()
        deployment.trigger = "user"
        db = SimpleNamespace(execute=AsyncMock())

        is_stale = (
            await DeploymentService.has_newer_successful_deployment(
                deployment, db
            )
        )

        self.assertFalse(is_stale)
        db.execute.assert_not_awaited()

    async def test_worker_checkpoint_cleans_up_skipped_startup(self):
        deployment = self.deployment()
        deployment.conclusion = "skipped"
        deployment.container_id = "container-id"
        db = SimpleNamespace(refresh=AsyncMock())
        cleanup = AsyncMock()
        update_status = AsyncMock()

        with (
            patch(
                "workers.tasks.deployment._cleanup_startup_resources", cleanup
            ),
            patch.object(DeploymentService, "update_status", update_status),
        ):
            concluded = await _cleanup_if_concluded(
                db=db,
                deployment=deployment,
                container=SimpleNamespace(id="container-id"),
                image_reference="devpush/deployment-test:abcdef0",
                settings=SimpleNamespace(),
                loki=None,
            )

        self.assertTrue(concluded)
        cleanup.assert_awaited_once()
        self.assertEqual("completed", update_status.await_args.kwargs["status"])
        self.assertEqual("removed", update_status.await_args.kwargs["container_status"])


if __name__ == "__main__":
    unittest.main()
