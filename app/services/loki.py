import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from utils.log import epoch_nano_to_iso, parse_structured_log

logger = logging.getLogger(__name__)

DOCKER_TIMESTAMP_RE = re.compile(
    r"^(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z\s(?P<message>.*)$"
)


class LokiService:
    def __init__(self, loki_url: str = "http://loki:3100"):
        self.loki_url = loki_url
        self.client = httpx.AsyncClient()

    def _format_loki_log(
        self, stream: dict, ts: str, line: str, **extra_labels
    ) -> dict:
        """Format a Loki log entry consistently."""
        timestamp_iso = epoch_nano_to_iso(ts)
        message, level = parse_structured_log(line)
        return {
            "timestamp_iso": timestamp_iso,
            "timestamp": ts,
            "message": message,
            "level": level,
            "labels": {"stream": stream.get("stream", "stdout"), **extra_labels},
        }

    async def get_logs(
        self,
        project_id: str,
        limit: int = 100,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        deployment_id: str | None = None,
        environment_id: str | None = None,
        branch: str | None = None,
        keyword: str | None = None,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """Get logs from Loki."""

        query_parts = [f'project_id="{project_id}"']

        if deployment_id:
            query_parts.append(f'deployment_id="{deployment_id}"')
        if environment_id:
            query_parts.append(f'environment_id="{environment_id}"')
        if branch:
            query_parts.append(f'branch="{branch}"')

        query = "{" + ", ".join(query_parts) + "}"

        if keyword:
            query += f' |~ "(?i){re.escape(keyword)}"'

        params = {
            "query": query,
            "start": start_timestamp,
            "end": end_timestamp,
            "limit": limit,
        }

        response = await self.client.get(
            f"{self.loki_url}/loki/api/v1/query_range", params=params, timeout=timeout
        )
        response.raise_for_status()
        data = response.json()

        logs = []

        if "data" in data and "result" in data["data"]:
            for stream in data["data"]["result"]:
                for timestamp_ns, log_line in stream["values"]:
                    timestamp_iso = epoch_nano_to_iso(timestamp_ns)
                    message, level = parse_structured_log(log_line)
                    logs.append(
                        {
                            "timestamp_iso": timestamp_iso,
                            "timestamp": timestamp_ns,
                            "message": message,
                            "level": level,
                            "labels": {
                                "project_id": stream["stream"]["project_id"],
                                "deployment_id": stream["stream"]["deployment_id"],
                                "environment_id": stream["stream"]["environment_id"],
                                "branch": stream["stream"]["branch"],
                            },
                        }
                    )

        logs.sort(key=lambda x: int(x["timestamp"]))
        return logs

    async def push_log(
        self,
        labels: dict[str, str],
        line: str,
        timestamp_ns: int | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Push a single log line to Loki."""
        clean_labels = {k: str(v) for k, v in labels.items() if v is not None}
        ts = str(timestamp_ns if timestamp_ns is not None else time.time_ns())
        payload = {"streams": [{"stream": clean_labels, "values": [[ts, line]]}]}
        response = await self.client.post(
            f"{self.loki_url}/loki/api/v1/push", json=payload, timeout=timeout
        )
        response.raise_for_status()

    async def push_logs(
        self,
        labels: dict[str, str],
        lines: list[tuple[int, str]],
        timeout: float = 10.0,
    ) -> None:
        """Push multiple ordered log lines to one Loki stream."""
        if not lines:
            return
        clean_labels = {k: str(v) for k, v in labels.items() if v is not None}
        payload = {
            "streams": [
                {
                    "stream": clean_labels,
                    "values": [[str(timestamp), line] for timestamp, line in lines],
                }
            ]
        }
        response = await self.client.post(
            f"{self.loki_url}/loki/api/v1/push", json=payload, timeout=timeout
        )
        response.raise_for_status()

    async def preserve_container_logs(
        self,
        container,
        deployment,
        tail: int = 1000,
    ) -> int:
        """Preserve final Docker output that file discovery may have missed."""
        try:
            raw_lines = await container.log(
                stdout=True,
                stderr=True,
                tail=tail,
                timestamps=True,
            )
        except Exception:
            logger.warning(
                "Failed to read final Docker logs for deployment %s",
                deployment.id,
                exc_info=True,
            )
            return 0

        fallback_timestamp = time.time_ns()
        parsed_lines: list[tuple[int, str, str]] = []
        for index, raw_line in enumerate(raw_lines):
            timestamp, line = self._parse_docker_log_line(
                raw_line, fallback_timestamp + index
            )
            message, _ = parse_structured_log(line)
            key = message.strip()
            if key:
                parsed_lines.append((timestamp, line, key))

        if not parsed_lines:
            return 0

        start_timestamp = min(line[0] for line in parsed_lines)
        end_timestamp = max(line[0] for line in parsed_lines)
        try:
            existing = await self.get_logs(
                project_id=deployment.project_id,
                deployment_id=deployment.id,
                limit=tail,
                start_timestamp=str(max(0, start_timestamp - 1_000_000_000)),
                end_timestamp=str(end_timestamp + 1_000_000_000),
            )
        except Exception:
            logger.warning(
                "Failed to query existing Loki logs for deployment %s",
                deployment.id,
                exc_info=True,
            )
            existing = []

        known = {
            (int(entry.get("timestamp", 0)), str(entry.get("message", "")).strip())
            for entry in existing
            if entry.get("message") and entry.get("timestamp")
        }
        missing: list[tuple[int, str]] = []
        for timestamp, line, message in parsed_lines:
            key = (timestamp, message)
            if key in known:
                continue
            known.add(key)
            missing.append((timestamp, line))

        if not missing:
            return 0

        labels = {
            "project_id": deployment.project_id,
            "deployment_id": deployment.id,
            "environment_id": deployment.environment_id,
            "branch": deployment.branch,
            "stream": "combined",
            "source": "docker-fallback",
        }
        try:
            await self.push_logs(labels, missing)
        except Exception:
            logger.warning(
                "Failed to preserve final Docker logs for deployment %s",
                deployment.id,
                exc_info=True,
            )
            return 0
        return len(missing)

    @staticmethod
    def _parse_docker_log_line(
        raw_line: str, fallback_timestamp: int
    ) -> tuple[int, str]:
        line = str(raw_line).rstrip("\r\n")
        match = DOCKER_TIMESTAMP_RE.fullmatch(line)
        if not match:
            return fallback_timestamp, line
        second = datetime.strptime(match.group("second"), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=UTC
        )
        fraction = (match.group("fraction") or "").ljust(9, "0")
        timestamp = int(second.timestamp()) * 1_000_000_000 + int(fraction or 0)
        return timestamp, match.group("message")
