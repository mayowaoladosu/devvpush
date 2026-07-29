"""Constrained Docker runtime used by the DevPush node agent."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

import httpx
from services.dockerfile_builder import validate_dockerfile_runtime_image
from services.metrics_exporter import DockerMetricsExporter

_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_CACHE_NAMESPACE_PATTERN = re.compile(r"^[a-f0-9]{20}$")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}$")
_MANAGED_IMAGE_PATTERN = re.compile(
    r"^devpush/deployment-[a-f0-9]{32}:[a-f0-9]{7,64}$"
)


class NodeDockerError(RuntimeError):
    """A safe node-agent Docker operation failure."""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class DockerRuntime:
    def __init__(
        self,
        *,
        socket_path: str,
        runtime_host: str,
        runtime_scheme: str,
        port_min: int,
        port_max: int,
        data_dir: Path,
        host_data_dir: Path,
        max_image_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.runtime_host = runtime_host
        self.runtime_scheme = runtime_scheme
        self.port_min = port_min
        self.port_max = port_max
        self.data_dir = data_dir
        self.host_data_dir = host_data_dir
        self.max_image_bytes = max_image_bytes
        self._port_lock = asyncio.Lock()
        self._next_port = port_min
        runtime_transport = transport or httpx.AsyncHTTPTransport(uds=socket_path)
        timeout = httpx.Timeout(connect=10, read=300, write=300, pool=10)
        self.client = httpx.AsyncClient(
            transport=runtime_transport,
            base_url="http://docker",
            timeout=timeout,
        )
        self.metrics = DockerMetricsExporter(
            docker_host="http://docker",
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
        )

    async def close(self) -> None:
        await self.metrics.close()
        await self.client.aclose()

    async def health(self) -> dict[str, int | str]:
        self.verify_data_directory()
        ping = await self.client.get("/_ping")
        ping.raise_for_status()
        info_response = await self.client.get("/info")
        info_response.raise_for_status()
        info = info_response.json()
        containers = await self.list_runtimes()
        return {
            "docker": ping.text.strip(),
            "cpus": int(info.get("NCPU") or 0),
            "memory_bytes": int(info.get("MemTotal") or 0),
            "runtimes": len(containers),
        }

    async def inspect_image(self, image: str) -> dict:
        self._validate_image(image)
        response = await self.client.get(
            f"/images/{quote(image, safe='')}/json"
        )
        if response.status_code == 404:
            raise NodeDockerError("Image not found.", status_code=404)
        self._raise_for_docker(response, "Image inspection failed")
        return response.json()

    async def pull_image(self, image: str, allowed_prefixes: tuple[str, ...]) -> None:
        self._validate_image(image)
        if not any(image.startswith(prefix) for prefix in allowed_prefixes):
            raise NodeDockerError("Image is not allowed by this node.")
        response = await self.client.post(
            "/images/create",
            params={"fromImage": image},
        )
        self._raise_for_docker(response, "Image pull failed")

    async def load_image(
        self,
        chunks,
        *,
        content_length: int,
        image_reference: str,
        deployment_id: str,
    ) -> dict:
        self._validate_id(deployment_id, "Deployment")
        if not _MANAGED_IMAGE_PATTERN.fullmatch(image_reference):
            raise NodeDockerError("Managed image reference is invalid.")
        if content_length <= 0 or content_length > self.max_image_bytes:
            raise NodeDockerError("Image archive size is invalid.")
        response = await self.client.post(
            "/images/load",
            params={"quiet": "0"},
            headers={
                "Content-Type": "application/x-tar",
                "Content-Length": str(content_length),
            },
            content=chunks,
        )
        self._raise_for_docker(response, "Image load failed")
        info = await self.inspect_image(image_reference)
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get("com.devpush.deployment_id") != deployment_id:
            try:
                await self.delete_image(image_reference)
            except Exception:
                pass
            raise NodeDockerError("Loaded image ownership label is invalid.")
        validate_dockerfile_runtime_image(info)
        return info

    async def delete_image(self, image: str) -> bool:
        if not _MANAGED_IMAGE_PATTERN.fullmatch(str(image or "")):
            return False
        response = await self.client.delete(
            f"/images/{quote(image, safe='')}"
        )
        if response.status_code == 404:
            return False
        self._raise_for_docker(response, "Image deletion failed")
        return True

    async def create_runtime(
        self,
        payload: dict,
        *,
        max_deployments: int,
    ) -> dict:
        deployment_id = str(payload.get("deployment_id") or "")
        project_id = str(payload.get("project_id") or "")
        environment_id = str(payload.get("environment_id") or "")
        self._validate_id(deployment_id, "Deployment")
        self._validate_id(project_id, "Project")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", environment_id):
            raise NodeDockerError("Environment identifier is invalid.")
        image = str(payload.get("image") or "")
        self._validate_image(image)

        env = payload.get("environment") or {}
        if not isinstance(env, dict) or len(env) > 256:
            raise NodeDockerError("Runtime environment is invalid.")
        rendered_env = []
        for key, value in env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", str(key)):
                raise NodeDockerError("Runtime environment key is invalid.")
            text = str(value)
            if len(text) > 65_536:
                raise NodeDockerError("Runtime environment value is too large.")
            rendered_env.append(f"{key}={text}")

        command = payload.get("command")
        if command is not None:
            if (
                not isinstance(command, list)
                or not command
                or len(command) > 8
                or any(not isinstance(item, str) for item in command)
                or sum(len(item) for item in command) > 131_072
            ):
                raise NodeDockerError("Runtime command is invalid.")
        working_dir = str(payload.get("working_dir") or "") or None
        if working_dir and (
            not working_dir.startswith("/")
            or ".." in Path(working_dir).parts
            or len(working_dir) > 255
        ):
            raise NodeDockerError("Runtime working directory is invalid.")

        labels = {
            "devpush.managed": "true",
            "devpush.deployment_id": deployment_id,
            "devpush.project_id": project_id,
            "devpush.environment_id": environment_id,
            "devpush.branch": str(payload.get("branch") or "")[:255],
            "devpush.node_id": str(payload.get("node_id") or "")[:32],
        }
        storage_ids = payload.get("storage_ids") or []
        if storage_ids:
            if (
                not isinstance(storage_ids, list)
                or len(storage_ids) > 16
                or any(not _ID_PATTERN.fullmatch(str(item)) for item in storage_ids)
            ):
                raise NodeDockerError("Storage identifiers are invalid.")
            labels["devpush.storage_ids"] = ",".join(storage_ids)

        binds = []
        cache = payload.get("cache")
        cache_warm = False
        if cache:
            bind, cache_warm = self._prepare_cache(cache)
            binds.append(bind)
            rendered_env = [
                entry
                for entry in rendered_env
                if not entry.startswith(
                    (
                        "DEVPUSH_DEPENDENCY_CACHE=",
                        "DEVPUSH_DEPENDENCY_CACHE_GENERATION=",
                    )
                )
            ]
            rendered_env.extend(
                [
                    "DEVPUSH_DEPENDENCY_CACHE="
                    + ("hit" if cache_warm else "miss"),
                    "DEVPUSH_DEPENDENCY_CACHE_GENERATION="
                    + str(cache["generation"]),
                ]
            )
            labels.update(
                {
                    "devpush.cache_generation": str(cache["generation"]),
                    "devpush.cache_namespace": str(cache["namespace"]),
                }
            )

        uses_dockerfile = bool(payload.get("uses_dockerfile"))
        pids_limit = max(16, min(int(payload.get("pids_limit") or 512), 4096))
        cpus = payload.get("cpus")
        memory_mb = payload.get("memory_mb")
        host_config = {
            "CapDrop": ["ALL"],
            "PidsLimit": pids_limit,
            "SecurityOpt": ["no-new-privileges:true"],
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "10m", "max-file": "5"},
            },
            "PortBindings": {
                "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": ""}]
            },
        }
        if not uses_dockerfile:
            host_config["CapAdd"] = [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "SETGID",
                "SETUID",
            ]
        if binds:
            host_config["Binds"] = binds
        if cpus is not None and float(cpus) > 0:
            host_config.update(
                {"CpuQuota": int(float(cpus) * 100_000), "CpuPeriod": 100_000}
            )
        if memory_mb is not None and int(memory_mb) > 0:
            host_config["Memory"] = int(memory_mb) * 1024 * 1024

        config = {
            "Image": image,
            "Env": rendered_env,
            "Labels": labels,
            "ExposedPorts": {"8000/tcp": {}},
            "HostConfig": host_config,
        }
        if command:
            config["Cmd"] = command
        if working_dir:
            config["WorkingDir"] = working_dir

        name = f"runner-{deployment_id}"
        async with self._port_lock:
            existing = await self.list_runtimes()
            active_ids = {
                str(
                    (item.get("Labels") or {}).get(
                        "devpush.deployment_id"
                    )
                    or ""
                )
                for item in existing
            }
            if (
                deployment_id not in active_ids
                and len(existing) >= max(1, max_deployments)
            ):
                raise NodeDockerError("Node deployment capacity reached.")
            await self._remove_owned_container(name, deployment_id)
            last_error = None
            for _ in range(self.port_max - self.port_min + 1):
                port = self._allocate_port()
                config["HostConfig"]["PortBindings"]["8000/tcp"][0][
                    "HostPort"
                ] = str(port)
                response = await self.client.post(
                    "/containers/create", params={"name": name}, json=config
                )
                if response.status_code in {201, 202}:
                    body = response.json()
                    return {
                        "container_id": body["Id"],
                        "host_port": port,
                        "runtime_url": self.runtime_url(port),
                        "cache_warm": cache_warm,
                    }
                detail = self._docker_detail(response)
                last_error = detail
                if "port is already allocated" not in detail.lower():
                    self._raise_for_docker(response, "Container creation failed")
            raise NodeDockerError(
                f"No runtime port is available: {last_error or 'port range exhausted'}."
            )

    async def start_runtime(self, deployment_id: str) -> dict:
        container_id = await self._owned_container_id(deployment_id)
        response = await self.client.post(f"/containers/{container_id}/start")
        if response.status_code not in {204, 304}:
            self._raise_for_docker(response, "Container start failed")
        return await self.inspect_runtime(deployment_id)

    async def inspect_runtime(self, deployment_id: str) -> dict:
        container_id = await self._owned_container_id(deployment_id)
        response = await self.client.get(f"/containers/{container_id}/json")
        self._raise_for_docker(response, "Container inspection failed")
        info = response.json()
        bindings = (
            (info.get("NetworkSettings") or {}).get("Ports") or {}
        ).get("8000/tcp") or []
        host_port = int(bindings[0]["HostPort"]) if bindings else 0
        state = info.get("State") or {}
        return {
            "container_id": str(info.get("Id") or ""),
            "status": str(state.get("Status") or "unknown"),
            "exit_code": int(state.get("ExitCode") or 0),
            "started_at": str(state.get("StartedAt") or ""),
            "host_port": host_port,
            "runtime_url": self.runtime_url(host_port) if host_port else None,
        }

    async def runtime_logs(
        self, deployment_id: str, *, tail: int = 1000
    ) -> list[str]:
        container_id = await self._owned_container_id(deployment_id)
        response = await self.client.get(
            f"/containers/{container_id}/logs",
            params={
                "stdout": "1",
                "stderr": "1",
                "timestamps": "1",
                "tail": str(max(1, min(tail, 5000))),
            },
        )
        self._raise_for_docker(response, "Container logs failed")
        return self._decode_logs(response.content)

    async def stop_runtime(self, deployment_id: str) -> None:
        try:
            container_id = await self._owned_container_id(deployment_id)
        except NodeDockerError:
            return
        response = await self.client.post(
            f"/containers/{container_id}/stop", params={"t": "5"}
        )
        if response.status_code not in {204, 304, 404}:
            self._raise_for_docker(response, "Container stop failed")

    async def delete_runtime(
        self,
        deployment_id: str,
        *,
        image: str | None = None,
    ) -> None:
        await self.stop_runtime(deployment_id)
        try:
            container_id = await self._owned_container_id(deployment_id)
        except NodeDockerError:
            container_id = None
        if container_id:
            response = await self.client.delete(
                f"/containers/{container_id}", params={"force": "1", "v": "1"}
            )
            if response.status_code not in {204, 404}:
                self._raise_for_docker(response, "Container deletion failed")
        if image:
            await self.delete_image(image)

    async def list_runtimes(self) -> list[dict]:
        filters = json.dumps({"label": ["devpush.managed=true"]})
        response = await self.client.get(
            "/containers/json", params={"all": "1", "filters": filters}
        )
        self._raise_for_docker(response, "Container listing failed")
        return response.json()

    async def metric_values(self) -> list[dict]:
        return [asdict(value) for value in await self.metrics.collect()]

    def runtime_url(self, port: int) -> str:
        host = self.runtime_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.runtime_scheme}://{host}:{port}"

    def verify_data_directory(self) -> Path:
        cache_root = self.data_dir / "cache" / "dependencies"
        try:
            cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(
                dir=cache_root,
                prefix=".devpush-write-check-",
            ):
                pass
        except OSError as exc:
            raise NodeDockerError(
                "Node data directory is not writable.",
                status_code=503,
            ) from exc
        return cache_root

    def _prepare_cache(self, cache: dict) -> tuple[str, bool]:
        project_id = str(cache.get("project_id") or "")
        environment_id = str(cache.get("environment_id") or "")
        namespace = str(cache.get("namespace") or "")
        generation = int(cache.get("generation") or 0)
        self._validate_id(project_id, "Cache project")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", environment_id):
            raise NodeDockerError("Cache environment identifier is invalid.")
        if not _CACHE_NAMESPACE_PATTERN.fullmatch(namespace):
            raise NodeDockerError("Cache namespace is invalid.")
        if not 1 <= generation <= 2_147_483_647:
            raise NodeDockerError("Cache generation is invalid.")
        cache_relative = (
            Path(project_id)
            / f"generation-{generation}"
            / environment_id
            / namespace
        )
        relative = Path("cache") / "dependencies" / cache_relative
        runtime_path = self.verify_data_directory() / cache_relative
        host_path = self.host_data_dir / relative
        try:
            runtime_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise NodeDockerError(
                "Node data directory is not writable.",
                status_code=503,
            ) from exc
        warm = (runtime_path / ".devpush-ready").is_file()
        return f"{host_path.as_posix()}:/cache", warm

    async def _remove_owned_container(
        self, name: str, deployment_id: str
    ) -> None:
        response = await self.client.get(f"/containers/{name}/json")
        if response.status_code == 404:
            return
        self._raise_for_docker(response, "Existing container inspection failed")
        labels = (response.json().get("Config") or {}).get("Labels") or {}
        if labels.get("devpush.deployment_id") != deployment_id:
            raise NodeDockerError("Container name is already reserved.")
        await self.delete_runtime(deployment_id)

    async def _owned_container_id(self, deployment_id: str) -> str:
        self._validate_id(deployment_id, "Deployment")
        name = f"runner-{deployment_id}"
        response = await self.client.get(f"/containers/{name}/json")
        if response.status_code == 404:
            raise NodeDockerError(
                "Runtime container was not found.", status_code=404
            )
        self._raise_for_docker(response, "Container inspection failed")
        info = response.json()
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get("devpush.deployment_id") != deployment_id:
            raise NodeDockerError("Runtime container ownership is invalid.")
        return str(info.get("Id") or name)

    def _allocate_port(self) -> int:
        port = self._next_port
        self._next_port = self.port_min if port >= self.port_max else port + 1
        return port

    @staticmethod
    def _decode_logs(payload: bytes) -> list[str]:
        lines: list[str] = []
        offset = 0
        framed = len(payload) >= 8 and payload[0] in {0, 1, 2}
        if framed:
            while offset + 8 <= len(payload):
                size = int.from_bytes(payload[offset + 4 : offset + 8], "big")
                start = offset + 8
                end = start + size
                if end > len(payload):
                    break
                text = payload[start:end].decode("utf-8", errors="replace")
                lines.extend(text.splitlines())
                offset = end
        else:
            lines = payload.decode("utf-8", errors="replace").splitlines()
        return lines

    @staticmethod
    def _docker_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:500]
        return str(payload.get("message") or response.text)[:500]

    @classmethod
    def _raise_for_docker(cls, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        raise NodeDockerError(
            f"{message} (Docker HTTP {response.status_code}): "
            f"{cls._docker_detail(response)}"
        )

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not _ID_PATTERN.fullmatch(value):
            raise NodeDockerError(f"{label} identifier is invalid.")

    @staticmethod
    def _validate_image(image: str) -> None:
        if not _IMAGE_PATTERN.fullmatch(image):
            raise NodeDockerError("Image reference is invalid.")
