"""Authenticated project monitoring backed by internal Prometheus queries."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


@dataclass(frozen=True)
class MonitoringWindow:
    slug: str
    label: str
    duration: timedelta
    step_seconds: int


WINDOWS = {
    "1h": MonitoringWindow("1h", "Last hour", timedelta(hours=1), 15),
    "6h": MonitoringWindow("6h", "Last 6 hours", timedelta(hours=6), 30),
    "24h": MonitoringWindow("24h", "Last 24 hours", timedelta(hours=24), 120),
    "7d": MonitoringWindow("7d", "Last 7 days", timedelta(days=7), 600),
}

_COLORS = {
    "cpu": "#8b5cf6",
    "memory": "#3b82f6",
    "network_receive": "#10b981",
    "network_transmit": "#f59e0b",
    "block_read": "#06b6d4",
    "block_write": "#f43f5e",
    "pids": "#a855f7",
}


class PrometheusMonitoringService:
    """Hide PromQL, response parsing, and chart geometry behind one interface."""

    def __init__(self, base_url: str, timeout_seconds: float = 8.0):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def dashboard(
        self,
        *,
        project_id: str,
        deployment_id: str,
        window_slug: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        window = WINDOWS.get(window_slug, WINDOWS["1h"])
        end = now or datetime.now(UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        start = end - window.duration
        selector = (
            f'project_id="{self._escape(project_id)}",'
            f'deployment_id="{self._escape(deployment_id)}"'
        )
        rate_window = "1m" if window.duration <= timedelta(hours=6) else "5m"
        queries = {
            "cpu": f"sum(rate(devpush_deployment_cpu_seconds_total{{{selector}}}[{rate_window}]))",
            "memory": f"max(devpush_deployment_memory_working_set_bytes{{{selector}}})",
            "memory_limit": f"max(devpush_deployment_memory_limit_bytes{{{selector}}})",
            "network_receive": f"sum(rate(devpush_deployment_network_receive_bytes_total{{{selector}}}[{rate_window}]))",
            "network_transmit": f"sum(rate(devpush_deployment_network_transmit_bytes_total{{{selector}}}[{rate_window}]))",
            "block_read": f"sum(rate(devpush_deployment_block_read_bytes_total{{{selector}}}[{rate_window}]))",
            "block_write": f"sum(rate(devpush_deployment_block_write_bytes_total{{{selector}}}[{rate_window}]))",
            "pids": f"max(devpush_deployment_pids{{{selector}}})",
        }
        try:
            results = await asyncio.gather(
                *(
                    self.query_range(
                        query,
                        start=start,
                        end=end,
                        step_seconds=window.step_seconds,
                    )
                    for query in queries.values()
                )
            )
            available = True
            error = None
        except (httpx.HTTPError, ValueError) as exc:
            results = [[] for _ in queries]
            available = False
            error = exc.__class__.__name__

        series = dict(zip(queries, results, strict=True))
        cpu = [(timestamp, value * 100) for timestamp, value in series["cpu"]]
        memory = series["memory"]
        memory_limit = self._latest(series["memory_limit"])
        network_receive = series["network_receive"]
        network_transmit = series["network_transmit"]
        block_read = series["block_read"]
        block_write = series["block_write"]
        pids = series["pids"]
        current_memory = self._latest(memory)

        current = {
            "cpu_percent": self._latest(cpu),
            "memory_bytes": current_memory,
            "memory_limit_bytes": memory_limit,
            "memory_percent": (
                current_memory / memory_limit * 100 if memory_limit > 0 else 0.0
            ),
            "network_bytes_per_second": self._latest(network_receive)
            + self._latest(network_transmit),
            "block_bytes_per_second": self._latest(block_read)
            + self._latest(block_write),
            "pids": self._latest(pids),
        }

        return {
            "available": available,
            "error": error,
            "window": window.slug,
            "windows": list(WINDOWS.values()),
            "has_data": any(series.values()),
            "current": current,
            "current_display": {
                "cpu": self.format_value(current["cpu_percent"], "percent"),
                "memory": self.format_value(current["memory_bytes"], "bytes"),
                "memory_limit": self.format_value(
                    current["memory_limit_bytes"], "bytes"
                ),
                "memory_percent": self.format_value(
                    current["memory_percent"], "percent"
                ),
                "network": self.format_value(
                    current["network_bytes_per_second"], "bytes_per_second"
                ),
                "block": self.format_value(
                    current["block_bytes_per_second"], "bytes_per_second"
                ),
                "pids": self.format_value(current["pids"], "number"),
            },
            "charts": {
                "cpu": self.build_chart(
                    [("CPU", cpu, _COLORS["cpu"])], unit="percent"
                ),
                "memory": self.build_chart(
                    [("Working set", memory, _COLORS["memory"])], unit="bytes"
                ),
                "network": self.build_chart(
                    [
                        ("Received", network_receive, _COLORS["network_receive"]),
                        ("Transmitted", network_transmit, _COLORS["network_transmit"]),
                    ],
                    unit="bytes_per_second",
                ),
                "block": self.build_chart(
                    [
                        ("Read", block_read, _COLORS["block_read"]),
                        ("Written", block_write, _COLORS["block_write"]),
                    ],
                    unit="bytes_per_second",
                ),
                "pids": self.build_chart(
                    [("Processes", pids, _COLORS["pids"])], unit="number"
                ),
            },
        }

    async def query_range(
        self,
        query: str,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> list[tuple[float, float]]:
        response = await self.client.get(
            "/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ValueError("Prometheus query failed.")
        result = (payload.get("data") or {}).get("result") or []
        if not result:
            return []
        values = result[0].get("values") or []
        parsed: list[tuple[float, float]] = []
        for timestamp, raw_value in values:
            try:
                value = float(raw_value)
                timestamp_value = float(timestamp)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and math.isfinite(timestamp_value):
                parsed.append((timestamp_value, max(0.0, value)))
        return parsed

    @classmethod
    def build_chart(
        cls,
        datasets: list[tuple[str, list[tuple[float, float]], str]],
        *,
        unit: str,
        width: int = 640,
        height: int = 180,
    ) -> dict[str, Any]:
        all_points = [point for _, values, _ in datasets for point in values]
        if not all_points:
            return {
                "has_data": False,
                "lines": [],
                "max_label": cls.format_value(0, unit),
                "mid_label": cls.format_value(0, unit),
                "min_label": cls.format_value(0, unit),
            }
        min_time = min(timestamp for timestamp, _ in all_points)
        max_time = max(timestamp for timestamp, _ in all_points)
        max_value = max(value for _, value in all_points)
        y_max = max(max_value * 1.1, 1.0)
        time_span = max(max_time - min_time, 1.0)
        lines = []
        for name, values, color in datasets:
            points = " ".join(
                f"{(timestamp - min_time) / time_span * width:.2f},"
                f"{height - value / y_max * height:.2f}"
                for timestamp, value in values
            )
            lines.append(
                {
                    "name": name,
                    "color": color,
                    "points": points,
                    "latest": cls._latest(values),
                }
            )
        return {
            "has_data": True,
            "lines": lines,
            "max_label": cls.format_value(y_max, unit),
            "mid_label": cls.format_value(y_max / 2, unit),
            "min_label": cls.format_value(0, unit),
        }

    @staticmethod
    def format_value(value: float, unit: str) -> str:
        if unit == "percent":
            return f"{value:.1f}%"
        if unit in {"bytes", "bytes_per_second"}:
            suffix = "/s" if unit == "bytes_per_second" else ""
            amount = max(0.0, value)
            for label in ("B", "KB", "MB", "GB", "TB"):
                if amount < 1024 or label == "TB":
                    return f"{amount:.1f} {label}{suffix}"
                amount /= 1024
        if unit == "number":
            return str(int(round(value)))
        return f"{value:.2f}"

    @staticmethod
    def _latest(values: list[tuple[float, float]]) -> float:
        return values[-1][1] if values else 0.0

    @staticmethod
    def _escape(value: str) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace('"', '\\"')
        )
