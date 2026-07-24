"""Isolated Dockerfile builds through rootless BuildKit."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import signal
import tarfile
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import aiodocker
import httpx

logger = logging.getLogger(__name__)

LogCallback = Callable[[str], Awaitable[None] | None]

_MANAGED_IMAGE_RE = re.compile(
    r"^devpush/deployment-[a-z0-9][a-z0-9_-]{0,63}:[a-f0-9]{7,64}$"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_COMMIT_RE = re.compile(r"^[a-fA-F0-9]{7,64}$")


class DockerfileBuildError(RuntimeError):
    """A safe, user-facing Dockerfile build failure."""


@dataclass(frozen=True)
class DockerfileBuildSpec:
    deployment_id: str
    project_id: str
    repo_full_name: str
    commit_sha: str
    source_token: str = field(repr=False)
    root_directory: str = ""
    dockerfile_path: str = "Dockerfile"

    @property
    def image_reference(self) -> str:
        return (
            f"devpush/deployment-{self.deployment_id.lower()}:"
            f"{self.commit_sha[:12].lower()}"
        )


@dataclass(frozen=True)
class DockerfileBuildResult:
    image_reference: str
    digest: str | None = None


def is_managed_deployment_image(image_reference: str | None) -> bool:
    return bool(
        image_reference and _MANAGED_IMAGE_RE.fullmatch(str(image_reference).lower())
    )


def validate_dockerfile_runtime_image(image_info: dict[str, Any]) -> None:
    """Reject images that cannot run safely under the deployment contract."""
    config = image_info.get("Config") or {}
    if not config.get("Cmd") and not config.get("Entrypoint"):
        raise DockerfileBuildError("Dockerfile image must define CMD or ENTRYPOINT.")
    user = str(config.get("User") or "").strip()
    primary_user = user.split(":", 1)[0].strip().lower()
    if not user or primary_user in {"0", "root"}:
        raise DockerfileBuildError("Dockerfile image must declare a non-root USER.")


async def remove_managed_deployment_image(
    docker_client: aiodocker.Docker, image_reference: str | None
) -> bool:
    """Remove a per-deployment image without touching shared runner images."""
    if not is_managed_deployment_image(image_reference):
        return False
    try:
        await docker_client.images.delete(str(image_reference), force=False)
        return True
    except aiodocker.DockerError as error:
        if error.status == 404:
            return False
        raise


class DockerfileBuilder:
    """Build and load one repository image behind a single interface.

    Source credentials are used only by the worker-side archive download. They are
    never passed to BuildKit, embedded in the build context, or included in logs.
    """

    def __init__(
        self,
        *,
        buildkit_host: str,
        docker_host: str,
        proxy_url: str = "http://buildkit-egress:3128",
        work_root: Path = Path("/tmp/devpush-builds"),
        timeout_seconds: int = 900,
        image_load_timeout_seconds: int = 300,
        max_archive_bytes: int = 256 * 1024 * 1024,
        max_context_bytes: int = 1024 * 1024 * 1024,
        max_context_files: int = 100_000,
        max_image_bytes: int = 2 * 1024 * 1024 * 1024,
    ):
        self.buildkit_host = buildkit_host
        self.docker_host = docker_host
        self.proxy_url = proxy_url
        self.work_root = Path(work_root)
        self.timeout_seconds = timeout_seconds
        self.image_load_timeout_seconds = image_load_timeout_seconds
        self.max_archive_bytes = max_archive_bytes
        self.max_context_bytes = max_context_bytes
        self.max_context_files = max_context_files
        self.max_image_bytes = max_image_bytes

    async def build(
        self, spec: DockerfileBuildSpec, on_log: LogCallback
    ) -> DockerfileBuildResult:
        """Download, build, load, and clean up one immutable repository revision."""
        self._validate_spec(spec)
        root_directory = self._normalize_relative_path(
            spec.root_directory, "Root directory", allow_empty=True
        )
        dockerfile_path = self._normalize_relative_path(
            spec.dockerfile_path, "Dockerfile path", allow_empty=False
        )

        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace = Path(
            tempfile.mkdtemp(
                prefix=f"{spec.deployment_id.lower()}-", dir=self.work_root
            )
        )
        try:
            archive_path = workspace / "source.tar.gz"
            source_directory = workspace / "source"
            image_archive = workspace / "image.tar"
            metadata_path = workspace / "metadata.json"

            await self._emit(on_log, "Downloading immutable source archive...")
            await self._download_source(spec, archive_path)
            repository_directory = await self._extract_source_safely(
                archive_path, source_directory
            )
            context_directory = self._resolve_context(
                repository_directory, root_directory
            )
            self._validate_dockerfile(context_directory, dockerfile_path)

            await self._emit(
                on_log,
                f"Building {dockerfile_path} with isolated rootless BuildKit...",
            )
            await self._run_buildctl(
                spec,
                context_directory,
                dockerfile_path,
                image_archive,
                metadata_path,
                on_log,
            )
            if not image_archive.is_file() or image_archive.stat().st_size == 0:
                raise DockerfileBuildError("BuildKit did not produce an image archive.")
            if image_archive.stat().st_size > self.max_image_bytes:
                raise DockerfileBuildError(
                    "Built image exceeds the configured image-size limit."
                )

            await self._emit(on_log, "Loading the built image into the runtime...")
            await self._load_image(image_archive, spec.image_reference, on_log)
            digest = self._read_digest(metadata_path)
            await self._emit(
                on_log,
                f"Dockerfile image ready ({spec.image_reference})",
            )
            return DockerfileBuildResult(
                image_reference=spec.image_reference,
                digest=digest,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def _download_source(
        self, spec: DockerfileBuildSpec, archive_path: Path
    ) -> None:
        url = (
            f"https://api.github.com/repos/{spec.repo_full_name}/tarball/"
            f"{spec.commit_sha}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {spec.source_token}",
            "User-Agent": "devpush-dockerfile-builder",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.max_archive_bytes:
                        raise DockerfileBuildError(
                            "Repository archive exceeds the configured size limit."
                        )
                    total = 0
                    with archive_path.open("wb") as archive_file:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > self.max_archive_bytes:
                                raise DockerfileBuildError(
                                    "Repository archive exceeds the configured "
                                    "size limit."
                                )
                            archive_file.write(chunk)
        except DockerfileBuildError:
            raise
        except httpx.HTTPStatusError as error:
            raise DockerfileBuildError(
                f"GitHub source download failed with HTTP {error.response.status_code}."
            ) from error
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise DockerfileBuildError("GitHub source download failed.") from error

    def _extract_source(self, archive_path: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with tarfile.open(archive_path, mode="r:*") as archive:
                members = []
                total_size = 0
                roots: set[str] = set()
                for member in archive:
                    if len(members) >= self.max_context_files:
                        raise DockerfileBuildError(
                            "Repository contains too many files for a Dockerfile build."
                        )
                    members.append(member)
                    normalized = member.name.replace("\\", "/")
                    path = PurePosixPath(normalized)
                    if path.is_absolute() or ".." in path.parts:
                        raise DockerfileBuildError(
                            "Repository archive contains an unsafe path."
                        )
                    if path.parts:
                        roots.add(path.parts[0])
                    if member.isfile():
                        total_size += member.size
                        if total_size > self.max_context_bytes:
                            raise DockerfileBuildError(
                                "Repository build context exceeds the configured "
                                "size limit."
                            )

                if len(roots) != 1:
                    raise DockerfileBuildError(
                        "Repository archive has an unexpected directory layout."
                    )
                archive.extractall(destination, members=members, filter="data")
        except DockerfileBuildError:
            raise
        except (
            OSError,
            tarfile.TarError,
            tarfile.FilterError,
        ) as error:
            raise DockerfileBuildError(
                "Repository archive could not be extracted safely."
            ) from error

        repository_directory = destination / next(iter(roots))
        if not repository_directory.is_dir():
            raise DockerfileBuildError(
                "Repository archive did not contain a source directory."
            )
        return repository_directory

    async def _extract_source_safely(
        self, archive_path: Path, destination: Path
    ) -> Path:
        extraction = asyncio.create_task(
            asyncio.to_thread(self._extract_source, archive_path, destination)
        )
        try:
            return await asyncio.shield(extraction)
        except asyncio.CancelledError:
            try:
                await extraction
            except Exception:
                logger.debug("Source extraction failed during cancellation.")
            raise

    async def _run_buildctl(
        self,
        spec: DockerfileBuildSpec,
        context_directory: Path,
        dockerfile_path: str,
        image_archive: Path,
        metadata_path: Path,
        on_log: LogCallback,
    ) -> None:
        command = self._buildctl_command(
            spec,
            context_directory,
            dockerfile_path,
            image_archive,
            metadata_path,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,
            start_new_session=os.name != "nt",
        )
        recent_lines: deque[str] = deque(maxlen=12)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                assert process.stdout is not None
                while line_bytes := await process.stdout.readline():
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    recent_lines.append(line)
                    await self._emit(on_log, line)
                return_code = await process.wait()
        except TimeoutError as error:
            await self._terminate_process(process)
            raise DockerfileBuildError(
                f"Dockerfile build exceeded {self.timeout_seconds} seconds."
            ) from error
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        if return_code != 0:
            fallback_detail = "BuildKit returned no output."
            detail = recent_lines[-1] if recent_lines else fallback_detail
            raise DockerfileBuildError(
                f"Dockerfile build failed (exit {return_code}): {detail}"
            )

    def _buildctl_command(
        self,
        spec: DockerfileBuildSpec,
        context_directory: Path,
        dockerfile_path: str,
        image_archive: Path,
        metadata_path: Path,
    ) -> list[str]:
        cache_namespace = f"devpush-{spec.project_id.lower()}"
        output = f"type=docker,name={spec.image_reference},dest={image_archive}"
        command = [
            "prlimit",
            f"--fsize={self.max_image_bytes}",
            "--",
            "buildctl",
            "--addr",
            self.buildkit_host,
            "build",
            "--progress=plain",
            "--frontend=dockerfile.v0",
            "--local",
            f"context={context_directory}",
            "--local",
            f"dockerfile={context_directory}",
            "--opt",
            f"filename={dockerfile_path}",
            "--opt",
            f"build-arg:BUILDKIT_CACHE_MOUNT_NS={cache_namespace}",
        ]
        if self.proxy_url:
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                command.extend(["--opt", f"build-arg:{name}={self.proxy_url}"])
            for name in ("NO_PROXY", "no_proxy"):
                command.extend(["--opt", f"build-arg:{name}=localhost,127.0.0.1,::1"])
        command.extend(
            [
                "--opt",
                "label:com.devpush.managed=true",
                "--opt",
                f"label:com.devpush.deployment_id={spec.deployment_id}",
                "--opt",
                f"label:com.devpush.project_id={spec.project_id}",
                "--metadata-file",
                str(metadata_path),
                "--output",
                output,
            ]
        )
        return command

    async def _load_image(
        self,
        image_archive: Path,
        image_reference: str,
        on_log: LogCallback,
    ) -> None:
        base_url = self._docker_http_url(self.docker_host)
        size = image_archive.stat().st_size

        async def content():
            with image_archive.open("rb") as image_file:
                while True:
                    chunk = await asyncio.to_thread(image_file.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        headers = {
            "Content-Length": str(size),
            "Content-Type": "application/x-tar",
        }
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=httpx.Timeout(
                    connect=15.0,
                    read=float(self.image_load_timeout_seconds),
                    write=float(self.image_load_timeout_seconds),
                    pool=15.0,
                ),
            ) as client:
                async with client.stream(
                    "POST",
                    "/images/load?quiet=0",
                    content=content(),
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            payload: dict[str, Any] = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        detail = payload.get("error") or (
                            payload.get("errorDetail") or {}
                        ).get("message")
                        if detail:
                            raise DockerfileBuildError(
                                f"Runtime image load failed: {detail}"
                            )
                        message = payload.get("stream") or payload.get("status")
                        if message:
                            await self._emit(on_log, str(message).strip())
        except DockerfileBuildError:
            raise
        except httpx.HTTPStatusError as error:
            raise DockerfileBuildError(
                f"Runtime image load failed with HTTP {error.response.status_code}."
            ) from error
        except (httpx.HTTPError, OSError) as error:
            raise DockerfileBuildError("Runtime image load failed.") from error

        if not is_managed_deployment_image(image_reference):
            raise DockerfileBuildError(
                "Built image reference is not managed by devpush."
            )

    def _resolve_context(self, repository_directory: Path, root_directory: str) -> Path:
        repository = repository_directory.resolve()
        context = (
            repository.joinpath(*PurePosixPath(root_directory).parts).resolve()
            if root_directory
            else repository
        )
        if not context.is_relative_to(repository):
            raise DockerfileBuildError(
                "Root directory must stay inside the repository."
            )
        if not context.is_dir():
            raise DockerfileBuildError(
                f"Root directory '{root_directory}' was not found."
            )
        return context

    @staticmethod
    def _validate_dockerfile(context_directory: Path, dockerfile_path: str) -> None:
        context = context_directory.resolve()
        dockerfile = context.joinpath(*PurePosixPath(dockerfile_path).parts).resolve()
        if not dockerfile.is_relative_to(context):
            raise DockerfileBuildError(
                "Dockerfile path must stay inside the root directory."
            )
        if not dockerfile.is_file():
            raise DockerfileBuildError(
                f"Dockerfile '{dockerfile_path}' was not found in the root directory."
            )
        if dockerfile.stat().st_size > 1024 * 1024:
            raise DockerfileBuildError("Dockerfile exceeds the 1 MB size limit.")

    @staticmethod
    def _normalize_relative_path(
        value: str | None, label: str, *, allow_empty: bool
    ) -> str:
        raw = str(value or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        raw = raw.rstrip("/")
        if raw in {"", "."}:
            if allow_empty:
                return ""
            raise DockerfileBuildError(f"{label} is required.")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or "\x00" in raw:
            if label == "Root directory":
                raise DockerfileBuildError(
                    "Root directory must stay inside the repository."
                )
            raise DockerfileBuildError(
                "Dockerfile path must stay inside the root directory."
            )
        return path.as_posix()

    @staticmethod
    def _validate_spec(spec: DockerfileBuildSpec) -> None:
        if not _IDENTIFIER_RE.fullmatch(spec.deployment_id):
            raise DockerfileBuildError("Deployment identifier is invalid.")
        if not _IDENTIFIER_RE.fullmatch(spec.project_id):
            raise DockerfileBuildError("Project identifier is invalid.")
        if not _REPOSITORY_RE.fullmatch(spec.repo_full_name):
            raise DockerfileBuildError("GitHub repository name is invalid.")
        if not _COMMIT_RE.fullmatch(spec.commit_sha):
            raise DockerfileBuildError("Git commit identifier is invalid.")
        if not spec.source_token:
            raise DockerfileBuildError("GitHub source credentials are unavailable.")

    @staticmethod
    async def _emit(on_log: LogCallback, message: str) -> None:
        result = on_log(message)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            pass
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    @staticmethod
    def _docker_http_url(docker_host: str) -> str:
        if docker_host.startswith("tcp://"):
            return "http://" + docker_host.removeprefix("tcp://")
        if docker_host.startswith(("http://", "https://")):
            return docker_host
        raise DockerfileBuildError(
            "Docker image loading requires a TCP Docker proxy endpoint."
        )

    @staticmethod
    def _read_digest(metadata_path: Path) -> str | None:
        if not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        digest = metadata.get("containerimage.digest")
        return str(digest) if digest else None
