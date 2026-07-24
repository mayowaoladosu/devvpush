import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from services.loki import LokiService


class FakeContainer:
    def __init__(self, lines=None, error=None):
        self.lines = lines or []
        self.error = error
        self.calls = []

    async def log(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.lines


class FakeLokiService(LokiService):
    def __init__(self, existing=None):
        self.existing = existing or []
        self.pushed = []

    async def get_logs(self, **kwargs):
        self.query = kwargs
        return self.existing

    async def push_logs(self, labels, lines, timeout=10.0):
        self.pushed.append((labels, lines, timeout))


class FailedContainerLogRetentionTests(unittest.IsolatedAsyncioTestCase):
    def deployment(self):
        return SimpleNamespace(
            id="deployment-id",
            project_id="project-id",
            environment_id="prod",
            branch="main",
        )

    async def test_preserves_only_logs_alloy_has_not_ingested(self):
        second = int(datetime(2026, 7, 24, 13, 47, 5, tzinfo=UTC).timestamp())
        existing_timestamp = second * 1_000_000_000 + 100_000_000
        missing_timestamp = second * 1_000_000_000 + 200_000_000
        container = FakeContainer(
            [
                "2026-07-24T13:47:05.100000000Z existing line\n",
                "2026-07-24T13:47:05.200000000Z intentional e2e failure\n",
            ]
        )
        loki = FakeLokiService(
            existing=[
                {"timestamp": str(existing_timestamp), "message": "existing line"}
            ]
        )

        preserved = await loki.preserve_container_logs(container, self.deployment())

        self.assertEqual(1, preserved)
        self.assertEqual(
            {
                "project_id": "project-id",
                "deployment_id": "deployment-id",
                "environment_id": "prod",
                "branch": "main",
                "stream": "combined",
                "source": "docker-fallback",
            },
            loki.pushed[0][0],
        )
        self.assertEqual(missing_timestamp, loki.pushed[0][1][0][0])
        self.assertEqual("intentional e2e failure", loki.pushed[0][1][0][1])
        self.assertEqual(
            str(existing_timestamp - 1_000_000_000), loki.query["start_timestamp"]
        )
        self.assertEqual(
            str(missing_timestamp + 1_000_000_000), loki.query["end_timestamp"]
        )
        self.assertEqual(
            {
                "stdout": True,
                "stderr": True,
                "tail": 1000,
                "timestamps": True,
            },
            container.calls[0],
        )

    async def test_log_api_failure_does_not_break_failure_handling(self):
        container = FakeContainer(error=RuntimeError("container disappeared"))
        loki = FakeLokiService()

        preserved = await loki.preserve_container_logs(container, self.deployment())

        self.assertEqual(0, preserved)
        self.assertEqual([], loki.pushed)

    async def test_repeated_messages_with_different_timestamps_are_preserved(self):
        container = FakeContainer(
            [
                "2026-07-24T13:47:05.100000000Z retrying\n",
                "2026-07-24T13:47:05.200000000Z retrying\n",
            ]
        )
        loki = FakeLokiService()

        preserved = await loki.preserve_container_logs(container, self.deployment())

        self.assertEqual(2, preserved)
        timestamps = [timestamp for timestamp, _ in loki.pushed[0][1]]
        self.assertEqual(sorted(timestamps), timestamps)
        self.assertNotEqual(timestamps[0], timestamps[1])


if __name__ == "__main__":
    unittest.main()
