"""Docker-compatible adapter for constrained remote deployment nodes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiodocker
import httpx

from config import Settings
from models import Deployment, DeploymentNode


class NodeRuntimeError(RuntimeError):
    """A safe remote runtime operation failed."""


class RemoteContainer:
    def __init__(
        self,
        client: "NodeDockerClient",
        deployment_id: str,
        *,
        container_id: str | None = None,
        runtime_url: str | None = None,
        cache_warm: bool = False,
    ):
        self.client = client
        self.deployment_id = deployment_id
        self.id = container_id or f"runner-{deployment_id}"
        self.runtime_url = runtime_url
        self.cache_warm = cache_warm

    async def start(self):
        payload = await self.client._request(
            "POST", f"/v1/runtimes/{self.deployment_id}/start"
        )
        self.id = str(payload.get("container_id") or self.id)
        self.runtime_url = self.client.runtime_url(payload)
        return payload

    async def show(self) -> dict:
        payload = await self.client._request(
            "GET", f"/v1/runtimes/{self.deployment_id}"
        )
        self.id = str(payload.get("container_id") or self.id)
        self.runtime_url = self.client.runtime_url(payload)
        return {
            "Id": self.id,
            "State": {
                "Status": payload.get("status") or "unknown",
                "ExitCode": int(payload.get("exit_code") or 0),
                "StartedAt": payload.get("started_at") or "",
            },
            "NetworkSettings": {
                "Ports": {
                    "8000/tcp": [
                        {"HostPort": str(payload.get("host_port") or "")}
                    ]
                }
            },
        }

    async def log(
        self,
        *,
        stdout: bool = True,
        stderr: bool = True,
        tail: int = 1000,
        timestamps: bool = True,
    ) -> list[str]:
        payload = await self.client._request(
            "GET",
            f"/v1/runtimes/{self.deployment_id}/logs",
            params={"tail": max(1, min(int(tail), 5000))},
        )
        lines = payload.get("lines")
        return [str(line) for line in lines] if isinstance(lines, list) else []

    async def stop(self):
        await self.client._request(
            "POST",
            f"/v1/runtimes/{self.deployment_id}/stop",
            allow_not_found=True,
        )

    async def delete(self, force: bool = False):
        await self.client._request(
            "DELETE",
            f"/v1/runtimes/{self.deployment_id}",
            allow_not_found=True,
        )


class RemoteContainers:
    def __init__(self, client: "NodeDockerClient"):
        self.client = client

    async def create_or_replace(self, *, name: str, config: dict) -> RemoteContainer:
        deployment = self.client.deployment
        expected_name = f"runner-{deployment.id}"
        if name != expected_name:
            raise NodeRuntimeError("Remote runtime container name is invalid.")
        environment = {}
        for entry in config.get("Env") or []:
            key, separator, value = str(entry).partition("=")
            if separator:
                environment[key] = value
        labels = config.get("Labels") or {}
        host_config = config.get("HostConfig") or {}
        binds = host_config.get("Binds") or []
        cache = None
        for bind in binds:
            _, separator, target = str(bind).rpartition(":")
            if not separator or target != "/cache":
                raise NodeRuntimeError(
                    "Local persistent storage cannot be mounted on a remote node."
                )
            cache = {
                "project_id": deployment.project_id,
                "environment_id": deployment.environment_id,
                "namespace": labels.get("devpush.cache_namespace"),
                "generation": int(labels.get("devpush.cache_generation") or 1),
            }
        storage_ids = [
            value.strip()
            for value in str(labels.get("devpush.storage_ids") or "").split(",")
            if value.strip()
        ]
        cpu_quota = host_config.get("CpuQuota")
        cpu_period = host_config.get("CpuPeriod") or 100_000
        memory = host_config.get("Memory")
        payload = {
            "deployment_id": deployment.id,
            "project_id": deployment.project_id,
            "environment_id": deployment.environment_id,
            "branch": deployment.branch,
            "node_id": self.client.node.id,
            "node_capacity": self.client.node.max_deployments,
            "image": config.get("Image"),
            "environment": environment,
            "command": config.get("Cmd"),
            "working_dir": config.get("WorkingDir"),
            "uses_dockerfile": not bool(config.get("Cmd")),
            "cpus": (
                float(cpu_quota) / float(cpu_period) if cpu_quota else None
            ),
            "memory_mb": int(memory) // (1024 * 1024) if memory else None,
            "pids_limit": int(host_config.get("PidsLimit") or 512),
            "storage_ids": storage_ids,
            "cache": cache,
        }
        result = await self.client._request("POST", "/v1/runtimes", json=payload)
        return RemoteContainer(
            self.client,
            deployment.id,
            container_id=str(result.get("container_id") or ""),
            runtime_url=self.client.runtime_url(result),
            cache_warm=bool(result.get("cache_warm")),
        )

    async def get(self, identifier: str) -> RemoteContainer:
        container = RemoteContainer(self.client, self.client.deployment.id)
        try:
            await container.show()
        except aiodocker.DockerError:
            raise
        return container


class RemoteImages:
    def __init__(self, client: "NodeDockerClient"):
        self.client = client

    async def inspect(self, image: str) -> dict:
        return await self.client._request(
            "POST", "/v1/images/inspect", json={"image": image}
        )

    async def get(self, image: str) -> dict:
        return await self.inspect(image)

    async def pull(self, image: str):
        return await self.client._request(
            "POST", "/v1/images/pull", json={"image": image}
        )

    async def delete(self, image: str, force: bool = False):
        return await self.client._request(
            "DELETE",
            "/v1/images",
            params={"image": image},
            allow_not_found=True,
        )

    async def load_archive(
        self,
        archive: Path,
        image_reference: str,
        deployment_id: str,
    ) -> None:
        size = archive.stat().st_size

        async def content():
            with archive.open("rb") as image_file:
                while chunk := await asyncio.to_thread(
                    image_file.read, 1024 * 1024
                ):
                    yield chunk

        await self.client._request(
            "POST",
            "/v1/images/load",
            params={
                "image_reference": image_reference,
                "deployment_id": deployment_id,
            },
            headers={
                "Content-Type": "application/x-tar",
                "Content-Length": str(size),
            },
            content=content(),
        )


class NodeDockerClient:
    """Expose the Docker subset used by deployment workers through a node agent."""

    def __init__(
        self,
        deployment: Deployment,
        node: DeploymentNode,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.deployment = deployment
        self.node = node
        self.settings = settings
        timeout = max(1, settings.deployment_node_request_timeout_seconds)
        self.client = httpx.AsyncClient(
            base_url=node.endpoint_url,
            headers={"Authorization": f"Bearer {node.token}"},
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(
                connect=min(timeout, 10),
                read=max(timeout, settings.dockerfile_image_load_timeout_seconds),
                write=max(timeout, settings.dockerfile_image_load_timeout_seconds),
                pool=min(timeout, 10),
            ),
        )
        self.containers = RemoteContainers(self)
        self.images = RemoteImages(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def close(self):
        await self.client.aclose()

    def runtime_url(self, payload: dict) -> str | None:
        try:
            port = int(payload.get("host_port") or 0)
            port_min = int((self.node.config or {}).get("port_min") or 0)
            port_max = int((self.node.config or {}).get("port_max") or 0)
        except (TypeError, ValueError):
            return None
        if not port or not port_min <= port <= port_max:
            return None
        scheme = str((self.node.config or {}).get("runtime_scheme") or "http")
        if scheme not in {"http", "https"}:
            return None
        host = self.node.runtime_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{scheme}://{host}:{port}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        **kwargs,
    ) -> dict:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NodeRuntimeError("Remote node request failed.") from exc
        if response.status_code == 404:
            if allow_not_found:
                return {}
            raise aiodocker.DockerError(404, {"message": "remote resource not found"})
        if not response.is_success:
            try:
                detail = str(response.json().get("detail") or "")
            except ValueError:
                detail = ""
            safe_detail = detail[:500] or "remote operation failed"
            raise NodeRuntimeError(
                f"Remote node request failed with HTTP {response.status_code}: {safe_detail}"
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise NodeRuntimeError("Remote node returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise NodeRuntimeError("Remote node returned an invalid response.")
        return payload


@asynccontextmanager
async def deployment_runtime_client(
    deployment: Deployment,
    settings: Settings,
):
    if deployment.node_id:
        node = deployment.node
        if not node or node.status == "deleted":
            raise NodeRuntimeError("Deployment node is unavailable.")
        client = NodeDockerClient(deployment, node, settings)
    else:
        client = aiodocker.Docker(url=settings.docker_host)
    try:
        yield client
    finally:
        await client.close()
