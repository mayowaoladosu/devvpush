import unittest
from unittest.mock import AsyncMock

from services.metrics_exporter import DockerMetricsExporter


class DockerMetricsExporterTests(unittest.IsolatedAsyncioTestCase):
    def container(self):
        return {
            "Id": "abcdef1234567890",
            "Names": ["/runner-deployment-id"],
            "Image": "example/app:latest",
            "State": "running",
            "Labels": {
                "devpush.deployment_id": "deployment-id",
                "devpush.project_id": "project-id",
                "devpush.environment_id": "prod",
                "devpush.branch": 'feature/quote"test',
            },
        }

    def stats(self):
        return {
            "cpu_stats": {
                "cpu_usage": {
                    "total_usage": 2_500_000_000,
                    "percpu_usage": [1, 1],
                },
                "online_cpus": 2,
            },
            "memory_stats": {
                "usage": 300,
                "limit": 1024,
                "stats": {"inactive_file": 50},
            },
            "networks": {
                "eth0": {"rx_bytes": 100, "tx_bytes": 40},
                "eth1": {"rx_bytes": 20, "tx_bytes": 10},
            },
            "blkio_stats": {
                "io_service_bytes_recursive": [
                    {"op": "Read", "value": 1000},
                    {"op": "read", "value": 500},
                    {"op": "Write", "value": 750},
                    {"op": "Discard", "value": 99},
                ]
            },
            "pids_stats": {"current": 7},
        }

    def test_parse_uses_cumulative_counters_and_working_set(self):
        parsed = DockerMetricsExporter.parse(self.container(), self.stats())

        self.assertEqual("deployment-id", parsed.deployment_id)
        self.assertEqual("project-id", parsed.project_id)
        self.assertEqual("runner-deployment-id", parsed.container_name)
        self.assertEqual(2.5, parsed.cpu_seconds)
        self.assertEqual(2, parsed.online_cpus)
        self.assertEqual(250, parsed.memory_working_set_bytes)
        self.assertEqual(1024, parsed.memory_limit_bytes)
        self.assertEqual(120, parsed.network_receive_bytes)
        self.assertEqual(50, parsed.network_transmit_bytes)
        self.assertEqual(1500, parsed.block_read_bytes)
        self.assertEqual(750, parsed.block_write_bytes)
        self.assertEqual(7, parsed.pids)

    def test_render_is_valid_prometheus_text_and_escapes_labels(self):
        value = DockerMetricsExporter.parse(self.container(), self.stats())

        rendered = DockerMetricsExporter.render(
            [value], scrape_duration_seconds=0.25
        )

        self.assertIn("# TYPE devpush_deployment_cpu_seconds_total counter", rendered)
        self.assertIn("devpush_deployment_cpu_seconds_total{", rendered)
        self.assertIn('branch="feature/quote\\"test"', rendered)
        self.assertIn("} 2.5", rendered)
        self.assertIn("devpush_deployment_memory_working_set_bytes", rendered)
        self.assertIn("devpush_metrics_exporter_containers 1", rendered)
        self.assertNotIn("nan", rendered.lower())

    async def test_collect_ignores_platform_and_stopped_containers(self):
        exporter = DockerMetricsExporter("http://docker-proxy:2375")
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: [
            self.container(),
            {"Id": "platform", "State": "running", "Labels": {}},
            {
                **self.container(),
                "Id": "stopped",
                "State": "exited",
            },
        ]
        stats_response = AsyncMock()
        stats_response.raise_for_status = lambda: None
        stats_response.json = self.stats
        exporter.client.get = AsyncMock(side_effect=[response, stats_response])

        try:
            values = await exporter.collect()
        finally:
            await exporter.close()

        self.assertEqual(1, len(values))
        self.assertEqual("deployment-id", values[0].deployment_id)
        self.assertEqual(2, exporter.client.get.await_count)

    def test_rejects_unix_socket_configuration(self):
        with self.assertRaisesRegex(ValueError, "TCP Docker proxy"):
            DockerMetricsExporter("unix:///var/run/docker.sock")


if __name__ == "__main__":
    unittest.main()
