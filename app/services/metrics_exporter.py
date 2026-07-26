"""Docker stats to Prometheus text-format exporter."""

from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class DeploymentMetrics:
    deployment_id: str
    project_id: str
    environment_id: str
    branch: str
    container_id: str
    container_name: str
    image: str
    cpu_seconds: float
    online_cpus: int
    memory_working_set_bytes: int
    memory_limit_bytes: int
    network_receive_bytes: int
    network_transmit_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    pids: int


class DockerMetricsExporter:
    """Collect stats only for labeled DevPush deployment containers."""

    def __init__(
        self,
        docker_host: str | None = None,
        *,
        timeout_seconds: float = 4.0,
        max_concurrency: int = 8,
    ):
        host = docker_host or os.getenv(
            "DOCKER_HOST", "tcp://docker-proxy:2375"
        )
        self.base_url = self._docker_http_url(host)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_concurrency = max(1, int(max_concurrency))
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=self.timeout_seconds,
                read=self.timeout_seconds,
                write=self.timeout_seconds,
                pool=self.timeout_seconds,
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> bool:
        response = await self.client.get("/_ping")
        response.raise_for_status()
        return response.text.strip() == "OK"

    async def collect(self) -> list[DeploymentMetrics]:
        response = await self.client.get("/containers/json", params={"all": 1})
        response.raise_for_status()
        containers = response.json()
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def collect_one(container: dict[str, Any]):
            labels = container.get("Labels") or {}
            deployment_id = labels.get("devpush.deployment_id")
            project_id = labels.get("devpush.project_id")
            if (
                not deployment_id
                or not project_id
                or container.get("State") != "running"
            ):
                return None
            async with semaphore:
                try:
                    stats_response = await self.client.get(
                        f"/containers/{container['Id']}/stats",
                        params={"stream": "false", "one-shot": "true"},
                    )
                    stats_response.raise_for_status()
                except (httpx.HTTPError, KeyError):
                    return None
            return self.parse(container, stats_response.json())

        results = await asyncio.gather(
            *(collect_one(container) for container in containers)
        )
        return [item for item in results if item is not None]

    @classmethod
    def parse(
        cls, container: dict[str, Any], stats: dict[str, Any]
    ) -> DeploymentMetrics:
        labels = container.get("Labels") or {}
        cpu_stats = stats.get("cpu_stats") or {}
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        total_usage = cls._nonnegative(cpu_usage.get("total_usage"))
        online_cpus = cls._nonnegative(cpu_stats.get("online_cpus"))
        if not online_cpus:
            online_cpus = len(cpu_usage.get("percpu_usage") or []) or 1

        memory = stats.get("memory_stats") or {}
        usage = cls._nonnegative(memory.get("usage"))
        memory_detail = memory.get("stats") or {}
        inactive_file = cls._nonnegative(
            memory_detail.get("inactive_file")
            or memory_detail.get("total_inactive_file")
        )
        working_set = max(0, usage - inactive_file)

        networks = stats.get("networks") or {}
        network_receive = sum(
            cls._nonnegative(values.get("rx_bytes"))
            for values in networks.values()
        )
        network_transmit = sum(
            cls._nonnegative(values.get("tx_bytes"))
            for values in networks.values()
        )

        block_read = 0
        block_write = 0
        block_entries = (
            (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive")
            or []
        )
        for entry in block_entries:
            operation = str(entry.get("op") or "").lower()
            value = cls._nonnegative(entry.get("value"))
            if operation == "read":
                block_read += value
            elif operation == "write":
                block_write += value

        names = container.get("Names") or []
        container_name = str(names[0] if names else "").lstrip("/")
        return DeploymentMetrics(
            deployment_id=str(labels.get("devpush.deployment_id") or ""),
            project_id=str(labels.get("devpush.project_id") or ""),
            environment_id=str(labels.get("devpush.environment_id") or ""),
            branch=str(labels.get("devpush.branch") or ""),
            container_id=str(container.get("Id") or "")[:12],
            container_name=container_name,
            image=str(container.get("Image") or ""),
            cpu_seconds=total_usage / 1_000_000_000,
            online_cpus=online_cpus,
            memory_working_set_bytes=working_set,
            memory_limit_bytes=cls._nonnegative(memory.get("limit")),
            network_receive_bytes=network_receive,
            network_transmit_bytes=network_transmit,
            block_read_bytes=block_read,
            block_write_bytes=block_write,
            pids=cls._nonnegative((stats.get("pids_stats") or {}).get("current")),
        )

    @classmethod
    def render(
        cls,
        values: list[DeploymentMetrics],
        *,
        scrape_duration_seconds: float = 0.0,
    ) -> str:
        metrics = (
            (
                "devpush_deployment_cpu_seconds_total",
                "counter",
                "Cumulative CPU time consumed by a deployment container.",
                "cpu_seconds",
            ),
            (
                "devpush_deployment_online_cpus",
                "gauge",
                "Online CPUs visible to a deployment container.",
                "online_cpus",
            ),
            (
                "devpush_deployment_memory_working_set_bytes",
                "gauge",
                "Container memory usage excluding inactive file cache.",
                "memory_working_set_bytes",
            ),
            (
                "devpush_deployment_memory_limit_bytes",
                "gauge",
                "Container memory limit reported by Docker.",
                "memory_limit_bytes",
            ),
            (
                "devpush_deployment_network_receive_bytes_total",
                "counter",
                "Cumulative bytes received across container interfaces.",
                "network_receive_bytes",
            ),
            (
                "devpush_deployment_network_transmit_bytes_total",
                "counter",
                "Cumulative bytes transmitted across container interfaces.",
                "network_transmit_bytes",
            ),
            (
                "devpush_deployment_block_read_bytes_total",
                "counter",
                "Cumulative block-device bytes read by a deployment.",
                "block_read_bytes",
            ),
            (
                "devpush_deployment_block_write_bytes_total",
                "counter",
                "Cumulative block-device bytes written by a deployment.",
                "block_write_bytes",
            ),
            (
                "devpush_deployment_pids",
                "gauge",
                "Current process count in a deployment container.",
                "pids",
            ),
        )
        lines: list[str] = []
        for name, metric_type, help_text, field in metrics:
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))
            for item in values:
                lines.append(
                    f"{name}{cls._labels(item)} {cls._number(getattr(item, field))}"
                )

        lines.extend(
            (
                "# HELP devpush_deployment_info Deployment container metadata.",
                "# TYPE devpush_deployment_info gauge",
            )
        )
        for item in values:
            info_labels = cls._labels(
                item,
                extra={
                    "container_id": item.container_id,
                    "container_name": item.container_name,
                    "image": item.image,
                },
            )
            lines.append(f"devpush_deployment_info{info_labels} 1")

        lines.extend(
            (
                "# HELP devpush_metrics_exporter_scrape_duration_seconds Time spent collecting Docker stats.",
                "# TYPE devpush_metrics_exporter_scrape_duration_seconds gauge",
                "devpush_metrics_exporter_scrape_duration_seconds "
                f"{cls._number(scrape_duration_seconds)}",
                "# HELP devpush_metrics_exporter_containers Number of deployment containers exported.",
                "# TYPE devpush_metrics_exporter_containers gauge",
                f"devpush_metrics_exporter_containers {len(values)}",
            )
        )
        return "\n".join(lines) + "\n"

    async def export(self) -> str:
        started = time.monotonic()
        values = await self.collect()
        return self.render(
            values, scrape_duration_seconds=time.monotonic() - started
        )

    @classmethod
    def _labels(
        cls,
        item: DeploymentMetrics,
        *,
        extra: dict[str, str] | None = None,
    ) -> str:
        labels = {
            "project_id": item.project_id,
            "deployment_id": item.deployment_id,
            "environment_id": item.environment_id,
            "branch": item.branch,
            **(extra or {}),
        }
        rendered = ",".join(
            f'{name}="{cls._escape_label(value)}"'
            for name, value in sorted(labels.items())
        )
        return "{" + rendered + "}"

    @staticmethod
    def _escape_label(value: object) -> str:
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace('"', '\\"')
        )

    @staticmethod
    def _nonnegative(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _number(value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "0"
        if not math.isfinite(number):
            return "0"
        return format(number, ".12g")

    @staticmethod
    def _docker_http_url(docker_host: str) -> str:
        if docker_host.startswith("tcp://"):
            return "http://" + docker_host.removeprefix("tcp://")
        if docker_host.startswith(("http://", "https://")):
            return docker_host
        raise ValueError("Metrics exporter requires a TCP Docker proxy endpoint.")
