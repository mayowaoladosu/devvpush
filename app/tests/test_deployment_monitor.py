import unittest
from datetime import UTC, datetime, timedelta

from workers.monitor import _parse_container_started_at, _readiness_timed_out


class DeploymentMonitorTimeoutTests(unittest.TestCase):
    def test_nanosecond_docker_timestamp_is_parsed(self):
        started_at = _parse_container_started_at("2026-07-24T15:00:01.123456789Z")

        self.assertEqual(
            datetime(2026, 7, 24, 15, 0, 1, 123456, tzinfo=UTC),
            started_at,
        )

    def test_build_duration_does_not_consume_readiness_timeout(self):
        now = datetime(2026, 7, 24, 15, 10, tzinfo=UTC)
        container_info = {"State": {"StartedAt": "2026-07-24T15:09:55.000000000Z"}}

        timed_out = _readiness_timed_out(
            container_info,
            now=now,
            fallback_started_at=now - timedelta(hours=1),
            timeout_seconds=300,
        )

        self.assertFalse(timed_out)

    def test_running_container_times_out_from_its_start(self):
        now = datetime(2026, 7, 24, 15, 10, tzinfo=UTC)
        container_info = {"State": {"StartedAt": "2026-07-24T15:04:59.000000000Z"}}

        timed_out = _readiness_timed_out(
            container_info,
            now=now,
            fallback_started_at=now,
            timeout_seconds=300,
        )

        self.assertTrue(timed_out)


if __name__ == "__main__":
    unittest.main()
