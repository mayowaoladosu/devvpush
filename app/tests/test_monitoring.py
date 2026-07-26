import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from services.monitoring import PrometheusMonitoringService


class PrometheusMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def test_chart_geometry_shares_scale_across_lines(self):
        chart = PrometheusMonitoringService.build_chart(
            [
                ("Received", [(10, 0), (20, 100)], "#0f0"),
                ("Sent", [(10, 50), (20, 200)], "#f00"),
            ],
            unit="bytes_per_second",
        )

        self.assertTrue(chart["has_data"])
        self.assertEqual(2, len(chart["lines"]))
        self.assertEqual("0.0 B/s", chart["min_label"])
        self.assertIn("640.00,", chart["lines"][0]["points"])
        self.assertNotEqual(
            chart["lines"][0]["points"], chart["lines"][1]["points"]
        )

    def test_formats_resource_units(self):
        self.assertEqual("50.0%", PrometheusMonitoringService.format_value(50, "percent"))
        self.assertEqual("1.0 MB", PrometheusMonitoringService.format_value(1024**2, "bytes"))
        self.assertEqual(
            "2.0 KB/s",
            PrometheusMonitoringService.format_value(2048, "bytes_per_second"),
        )
        self.assertEqual("7", PrometheusMonitoringService.format_value(7.2, "number"))

    def test_promql_label_values_escape_control_characters(self):
        escaped = PrometheusMonitoringService._escape('project\\name\nline\r"quote')

        self.assertEqual('project\\\\name\\nline\\r\\"quote', escaped)

    async def test_query_range_parses_finite_samples(self):
        service = PrometheusMonitoringService("http://prometheus:9090")
        response = AsyncMock()
        response.raise_for_status = lambda: None
        response.json = lambda: {
            "status": "success",
            "data": {
                "result": [
                    {
                        "values": [
                            [10, "1.5"],
                            [20, "NaN"],
                            [30, "-2"],
                        ]
                    }
                ]
            },
        }
        service.client.get = AsyncMock(return_value=response)

        try:
            values = await service.query_range(
                "metric",
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 2, tzinfo=UTC),
                step_seconds=15,
            )
        finally:
            await service.close()

        self.assertEqual([(10.0, 1.5), (30.0, 0.0)], values)

    async def test_dashboard_builds_summary_and_all_charts(self):
        service = PrometheusMonitoringService("http://prometheus:9090")
        values = [
            [(10, 0.5), (20, 0.75)],
            [(10, 100), (20, 200)],
            [(10, 1000), (20, 1000)],
            [(10, 20), (20, 30)],
            [(10, 10), (20, 15)],
            [(10, 4), (20, 5)],
            [(10, 2), (20, 3)],
            [(10, 6), (20, 7)],
        ]
        service.query_range = AsyncMock(side_effect=values)

        try:
            dashboard = await service.dashboard(
                project_id="project-id",
                deployment_id="deployment-id",
                window_slug="1h",
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
        finally:
            await service.close()

        self.assertTrue(dashboard["available"])
        self.assertTrue(dashboard["has_data"])
        self.assertEqual(75.0, dashboard["current"]["cpu_percent"])
        self.assertEqual(20.0, dashboard["current"]["memory_percent"])
        self.assertEqual(45.0, dashboard["current"]["network_bytes_per_second"])
        self.assertEqual(8.0, dashboard["current"]["block_bytes_per_second"])
        self.assertEqual(7.0, dashboard["current"]["pids"])
        self.assertEqual(
            {"cpu", "memory", "network", "block", "pids"},
            set(dashboard["charts"]),
        )


if __name__ == "__main__":
    unittest.main()
