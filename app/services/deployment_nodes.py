"""Enrollment, health, placement, and target discovery for deployment nodes."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models import (
    Deployment,
    DeploymentNode,
    Project,
    Storage,
    StorageProject,
    utc_now,
)

_NODE_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class DeploymentNodeConfigurationError(ValueError):
    """A deployment node configuration is invalid."""


class DeploymentNodeConnectionError(RuntimeError):
    """A deployment node could not be reached or authenticated."""


class DeploymentNodeCapacityError(RuntimeError):
    """No eligible deployment node has available capacity."""


class DeploymentNodeSafetyError(RuntimeError):
    """A node lifecycle action would orphan runtime resources."""


@dataclass(frozen=True)
class DeploymentNodeConfig:
    name: str
    endpoint_url: str
    runtime_host: str
    region: str
    max_deployments: int


class DeploymentNodeService:
    """Hide node policy, health, scheduling, and generated target credentials."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport

    @classmethod
    def build_config(
        cls,
        *,
        name: object,
        endpoint_url: object,
        runtime_host: object,
        region: object = "global",
        max_deployments: object = 20,
        allow_insecure: bool = False,
    ) -> DeploymentNodeConfig:
        name = str(name or "").strip()
        if not _NODE_NAME_PATTERN.fullmatch(name):
            raise DeploymentNodeConfigurationError(
                "Node name must contain 1–100 letters, numbers, dots, underscores, or hyphens."
            )
        endpoint_url = cls.normalize_endpoint_url(
            endpoint_url, allow_insecure=allow_insecure
        )
        runtime_host = cls.normalize_runtime_host(runtime_host)
        region = str(region or "global").strip().lower()
        if not _REGION_PATTERN.fullmatch(region):
            raise DeploymentNodeConfigurationError("Node region is invalid.")
        try:
            max_deployments = int(max_deployments)
        except (TypeError, ValueError) as exc:
            raise DeploymentNodeConfigurationError(
                "Node capacity must be a number."
            ) from exc
        if not 1 <= max_deployments <= 10_000:
            raise DeploymentNodeConfigurationError(
                "Node capacity must be between 1 and 10000 deployments."
            )
        return DeploymentNodeConfig(
            name=name,
            endpoint_url=endpoint_url,
            runtime_host=runtime_host,
            region=region,
            max_deployments=max_deployments,
        )

    @staticmethod
    def build_token(value: object) -> str:
        token = str(value or "").strip()
        if not 32 <= len(token) <= 512:
            raise DeploymentNodeConfigurationError(
                "Node token must contain between 32 and 512 characters."
            )
        if any(character.isspace() for character in token):
            raise DeploymentNodeConfigurationError(
                "Node token cannot contain whitespace."
            )
        return token

    async def verify(
        self,
        config: DeploymentNodeConfig,
        token: str,
    ) -> dict[str, object]:
        await self.validate_endpoint_network(config.endpoint_url)
        timeout = max(1, self.settings.deployment_node_request_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(timeout, 5)),
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.get(
                    f"{config.endpoint_url}/v1/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise DeploymentNodeConnectionError(
                f"Node verification failed with HTTP {exc.response.status_code}."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DeploymentNodeConnectionError(
                "Node verification could not complete."
            ) from exc
        if not isinstance(payload, dict) or payload.get("protocol_version") != 1:
            raise DeploymentNodeConnectionError(
                "Node agent protocol is not supported."
            )
        if payload.get("docker") != "OK":
            raise DeploymentNodeConnectionError("Node Docker runtime is unavailable.")
        if str(payload.get("runtime_host") or "") != config.runtime_host:
            raise DeploymentNodeConnectionError(
                "Node runtime host does not match the enrollment settings."
            )
        if payload.get("runtime_scheme") != "http":
            raise DeploymentNodeConnectionError(
                "Node runtime scheme must be HTTP behind central Traefik."
            )
        try:
            agent_capacity = int(payload.get("max_deployments") or 0)
        except (TypeError, ValueError) as exc:
            raise DeploymentNodeConnectionError(
                "Node agent returned invalid capacity metadata."
            ) from exc
        if config.max_deployments > agent_capacity:
            raise DeploymentNodeConnectionError(
                "Control-plane capacity exceeds the node-agent limit."
            )
        return {
            "protocol_version": 1,
            "agent_id": str(payload.get("agent_id") or "")[:128],
            "runtime_scheme": str(payload.get("runtime_scheme") or "http"),
            "port_min": int(payload.get("port_min") or 0),
            "port_max": int(payload.get("port_max") or 0),
            "cpus": int(payload.get("cpus") or 0),
            "memory_bytes": int(payload.get("memory_bytes") or 0),
            "agent_max_deployments": agent_capacity,
        }

    def configure(
        self,
        node: DeploymentNode,
        config: DeploymentNodeConfig,
        token: str,
        health: dict[str, object],
    ) -> None:
        node.name = config.name
        node.endpoint_url = config.endpoint_url
        node.runtime_host = config.runtime_host
        node.region = config.region
        node.max_deployments = config.max_deployments
        node.config = health
        node.token = token
        node.status = "active"
        node.healthy = True
        node.error = None
        node.last_checked_at = utc_now()
        node.updated_at = utc_now()

    async def refresh_health(
        self,
        db: AsyncSession,
        node: DeploymentNode,
    ) -> bool:
        config = self.build_config(
            name=node.name,
            endpoint_url=node.endpoint_url,
            runtime_host=node.runtime_host,
            region=node.region,
            max_deployments=node.max_deployments,
            allow_insecure=True,
        )
        try:
            health = await self.verify(config, node.token)
        except (DeploymentNodeConfigurationError, DeploymentNodeConnectionError) as exc:
            node.healthy = False
            node.error = {
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            node.last_checked_at = utc_now()
            node.updated_at = utc_now()
            await db.commit()
            return False
        node.healthy = True
        node.config = health
        node.error = None
        node.last_checked_at = utc_now()
        node.updated_at = utc_now()
        await db.commit()
        return True

    async def select_node(
        self,
        db: AsyncSession,
        project: Project,
        environment_id: str,
    ) -> DeploymentNode | None:
        preference = str((project.config or {}).get("deployment_node") or "automatic")
        if preference == "local":
            return None
        if await self.project_requires_local_runtime(db, project.id, environment_id):
            if preference not in {"", "automatic", "local"}:
                raise DeploymentNodeCapacityError(
                    "This environment uses local volumes or SQLite and must deploy on the primary node."
                )
            return None

        query = select(DeploymentNode).where(
            DeploymentNode.status == "active",
            DeploymentNode.healthy.is_(True),
        )
        if preference not in {"", "automatic"}:
            query = query.where(DeploymentNode.id == preference)
        nodes = list((await db.execute(query)).scalars().all())
        if not nodes:
            if preference not in {"", "automatic"}:
                raise DeploymentNodeCapacityError(
                    "The selected deployment node is unavailable."
                )
            return None

        node_ids = [node.id for node in nodes]
        counts_result = await db.execute(
            select(Deployment.node_id, func.count(Deployment.id))
            .where(
                Deployment.node_id.in_(node_ids),
                Deployment.container_id.isnot(None),
                or_(
                    Deployment.container_status.is_(None),
                    Deployment.container_status != "removed",
                ),
            )
            .group_by(Deployment.node_id)
        )
        counts = {node_id: int(count) for node_id, count in counts_result.all()}
        eligible = [
            node
            for node in nodes
            if counts.get(node.id, 0) < node.max_deployments
        ]
        if not eligible:
            if preference not in {"", "automatic"}:
                raise DeploymentNodeCapacityError(
                    "The selected deployment node is at capacity."
                )
            return None
        return min(
            eligible,
            key=lambda node: (
                counts.get(node.id, 0) / node.max_deployments,
                counts.get(node.id, 0),
                node.name.casefold(),
            ),
        )

    async def project_requires_local_runtime(
        self,
        db: AsyncSession,
        project_id: str,
        environment_id: str,
    ) -> bool:
        result = await db.execute(
            select(StorageProject, Storage)
            .join(Storage, StorageProject.storage_id == Storage.id)
            .where(
                StorageProject.project_id == project_id,
                Storage.type.in_(["database", "volume"]),
                Storage.status == "active",
            )
        )
        for association, _ in result.all():
            environment_ids = association.environment_ids or []
            if not environment_ids or environment_id in environment_ids:
                return True
        return False

    async def assert_deletable(
        self,
        db: AsyncSession,
        node: DeploymentNode,
    ) -> None:
        count = (
            await db.execute(
                select(func.count(Deployment.id)).where(
                    Deployment.node_id == node.id,
                    Deployment.container_id.isnot(None),
                    or_(
                        Deployment.container_status.is_(None),
                        Deployment.container_status != "removed",
                    ),
                )
            )
        ).scalar_one()
        if count:
            raise DeploymentNodeSafetyError(
                f"Node still owns {count} retained deployment container(s). Remove them before deleting the node."
            )
        try:
            payload = await self.request(node, "GET", "/v1/runtimes")
        except DeploymentNodeConnectionError as exc:
            raise DeploymentNodeSafetyError(
                "Node runtime inventory could not be verified. Restore the node before deleting it."
            ) from exc
        runtimes = payload.get("runtimes") if isinstance(payload, dict) else None
        if runtimes:
            raise DeploymentNodeSafetyError(
                "Node still reports managed runtime containers. Remove them before deleting the node."
            )

    async def request(
        self,
        node: DeploymentNode,
        method: str,
        path: str,
        **kwargs,
    ) -> dict[str, object]:
        timeout = max(1, self.settings.deployment_node_request_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=node.endpoint_url,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 5)),
                follow_redirects=False,
                headers={"Authorization": f"Bearer {node.token}"},
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise DeploymentNodeConnectionError(
                f"Node request failed with HTTP {exc.response.status_code}."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DeploymentNodeConnectionError("Node request failed.") from exc
        if not isinstance(payload, dict):
            raise DeploymentNodeConnectionError("Node returned an invalid response.")
        return payload

    async def write_targets(self, db: AsyncSession) -> None:
        nodes = list(
            (
                await db.execute(
                    select(DeploymentNode)
                    .where(
                        DeploymentNode.status.in_(["active", "draining"]),
                        DeploymentNode._token.isnot(None),
                    )
                    .order_by(DeploymentNode.id.asc())
                )
            ).scalars()
        )
        payload = {
            "version": 1,
            "targets": [
                {
                    "node_id": node.id,
                    "endpoint_url": node.endpoint_url,
                    "token": node.token,
                }
                for node in nodes
            ],
        }
        path = Path(self.settings.deployment_node_targets_file)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target_file:
                json.dump(payload, target_file, separators=(",", ":"))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    async def remote_container_snapshots(self) -> list[dict]:
        try:
            payload = json.loads(
                Path(self.settings.deployment_node_targets_file).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            return []
        targets = payload.get("targets") if isinstance(payload, dict) else []
        if not isinstance(targets, list):
            return []

        async def fetch(target: dict) -> list[dict]:
            try:
                endpoint = str(target["endpoint_url"]).rstrip("/")
                token = str(target["token"])
                timeout = max(
                    1, self.settings.deployment_node_request_timeout_seconds
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(
                        f"{endpoint}/v1/runtimes",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    data = response.json()
                runtimes = data.get("runtimes") if isinstance(data, dict) else []
                if not isinstance(runtimes, list):
                    raise ValueError("invalid runtime inventory")
                return runtimes
            except (KeyError, httpx.HTTPError, ValueError) as exc:
                raise DeploymentNodeConnectionError(
                    "Remote node runtime inventory could not be verified."
                ) from exc

        batches = await asyncio.gather(
            *(fetch(target) for target in targets if isinstance(target, dict))
        )
        return [runtime for batch in batches for runtime in batch]

    async def validate_endpoint_network(self, endpoint_url: str) -> None:
        parsed = urlparse(endpoint_url)
        if (
            self.settings.env != "development"
            and not self.settings.deployment_node_allow_insecure_endpoints
            and parsed.scheme != "https"
        ):
            raise DeploymentNodeConfigurationError(
                "Deployment node endpoints must use HTTPS in production."
            )
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(self._resolve, parsed.hostname or "", parsed.port),
                timeout=5,
            )
        except (OSError, TimeoutError) as exc:
            raise DeploymentNodeConnectionError(
                "Deployment node endpoint could not be resolved."
            ) from exc
        allow_private = (
            self.settings.env == "development"
            or self.settings.deployment_node_allow_private_endpoints
        )
        if not allow_private and any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise DeploymentNodeConfigurationError(
                "Deployment node endpoint resolves to a private or reserved address; the operator must explicitly enable private nodes."
            )

    @staticmethod
    def normalize_endpoint_url(value: object, *, allow_insecure: bool) -> str:
        raw = str(value or "").strip()
        parsed = urlparse(raw)
        schemes = {"http", "https"} if allow_insecure else {"https"}
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise DeploymentNodeConfigurationError(
                "Node endpoint must be an HTTP(S) origin without credentials or a path."
            )
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    @staticmethod
    def normalize_runtime_host(value: object) -> str:
        host = str(value or "").strip()
        unwrapped = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            ipaddress.ip_address(unwrapped)
        except ValueError:
            if not _HOST_PATTERN.fullmatch(host):
                raise DeploymentNodeConfigurationError(
                    "Runtime host must be a DNS name or IP address without a scheme or port."
                )
        return unwrapped

    @staticmethod
    def token_hint(node: DeploymentNode) -> str:
        value = node.token
        if len(value) <= 8:
            return "••••••••"
        return f"{value[:4]}••••{value[-4:]}"

    @classmethod
    def validate_runtime_url(
        cls, node: DeploymentNode, value: object
    ) -> str:
        raw = str(value or "").strip()
        parsed = urlparse(raw)
        expected_scheme = str((node.config or {}).get("runtime_scheme") or "http")
        try:
            port = int(parsed.port or 0)
            port_min = int((node.config or {}).get("port_min") or 0)
            port_max = int((node.config or {}).get("port_max") or 0)
        except (TypeError, ValueError) as exc:
            raise DeploymentNodeConfigurationError(
                "Remote runtime address is invalid."
            ) from exc
        if (
            parsed.scheme != expected_scheme
            or parsed.hostname != node.runtime_host
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not port_min <= port <= port_max
        ):
            raise DeploymentNodeConfigurationError(
                "Remote runtime address does not match its enrolled node."
            )
        host = node.runtime_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{expected_scheme}://{host}:{port}"

    @staticmethod
    def _resolve(hostname: str, port: int | None) -> set[str]:
        results = socket.getaddrinfo(
            hostname,
            port or 443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return {str(result[4][0]) for result in results}
