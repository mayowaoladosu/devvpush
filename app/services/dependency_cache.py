"""Generation-scoped dependency caches for zero-config runners."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import Settings

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_OFFICIAL_RUNNER_PREFIX = "ghcr.io/devpushhq/runner-"
_CACHE_ROOT = Path("cache") / "dependencies"
_CACHE_TARGET = "/cache"
_READY_MARKER = ".devpush-ready"


class DependencyCacheError(RuntimeError):
    """A dependency-cache configuration or filesystem failure."""


@dataclass(frozen=True)
class DependencyCacheMount:
    source: str
    target: str
    runtime_path: Path
    project_id: str
    environment_id: str
    runner_slug: str
    generation: int
    namespace: str
    warm: bool

    @property
    def bind(self) -> str:
        return f"{self.source}:{self.target}"

    @property
    def ready_marker(self) -> str:
        return f"{self.target}/{_READY_MARKER}"


class DependencyCacheService:
    """Resolve, rotate, and prune zero-config package-manager caches.

    Official runner images expose package-manager caches beneath ``/cache``.
    Each project, environment, runner image, and cache generation gets an
    isolated host-backed directory. Rotating the generation makes future
    deployments cold without mutating mounts used by running deployments.
    """

    def __init__(self, settings: Settings, runners: list[dict]):
        self.settings = settings
        self.runners = runners

    @staticmethod
    def is_enabled(config: dict | None) -> bool:
        values = config or {}
        return (
            values.get("build_strategy") != "dockerfile"
            and values.get("dependency_cache", True) is not False
        )

    @staticmethod
    def generation(config: dict | None) -> int:
        raw = (config or {}).get("dependency_cache_generation", 1)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if 1 <= value <= 2_147_483_647 else 1

    @classmethod
    def rotate_config(cls, config: dict | None) -> dict:
        values = dict(config or {})
        current = cls.generation(values)
        values["dependency_cache_generation"] = (
            current + 1 if current < 2_147_483_647 else 1
        )
        return values

    def prepare(self, deployment) -> DependencyCacheMount | None:
        config = deployment.config or {}
        if not self.is_enabled(config):
            return None

        runner = self._resolve_runner(config)
        if not runner:
            return None
        target = self._cache_target(runner)
        if not target:
            return None

        self._validate_id(deployment.project_id, "Project")
        self._validate_id(deployment.environment_id, "Environment")

        runner_slug = str(runner.get("slug") or "")
        image = str(runner.get("image") or "")
        namespace = hashlib.sha256(
            f"{runner_slug}\0{image}".encode("utf-8")
        ).hexdigest()[:20]
        generation = self.generation(config)
        relative = (
            _CACHE_ROOT
            / deployment.project_id
            / f"generation-{generation}"
            / deployment.environment_id
            / namespace
        )
        runtime_path = Path(self.settings.data_dir) / relative
        host_path = Path(self.settings.host_data_dir or self.settings.data_dir) / relative
        warm = (runtime_path / _READY_MARKER).is_file()

        try:
            runtime_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise DependencyCacheError(
                "Dependency cache directory could not be prepared."
            ) from exc

        return DependencyCacheMount(
            source=str(host_path).replace("\\", "/"),
            target=target,
            runtime_path=runtime_path,
            project_id=deployment.project_id,
            environment_id=deployment.environment_id,
            runner_slug=runner_slug,
            generation=generation,
            namespace=namespace,
            warm=warm,
        )

    def current_generation_exists(self, project_id: str, config: dict | None) -> bool:
        root = self._project_root(project_id)
        return (root / f"generation-{self.generation(config)}").is_dir()

    def prune(
        self, project_id: str, preserve_generations: set[int]
    ) -> list[Path]:
        root = self._project_root(project_id)
        if not root.is_dir():
            return []

        removed: list[Path] = []
        try:
            paths = list(root.iterdir())
        except FileNotFoundError:
            return []
        for path in paths:
            generation = self._parse_generation(path.name)
            if generation is None or generation in preserve_generations:
                continue
            self._remove_tree(path)
            removed.append(path)
        return removed

    def delete_project(self, project_id: str) -> bool:
        root = self._project_root(project_id)
        if not root.exists():
            return False
        self._remove_tree(root)
        return True

    def _project_root(self, project_id: str) -> Path:
        self._validate_id(project_id, "Project")
        return Path(self.settings.data_dir) / _CACHE_ROOT / project_id

    def _resolve_runner(self, config: dict) -> dict | None:
        slug = config.get("runner") or config.get("image")
        if not isinstance(slug, str) or not slug:
            return None
        return next(
            (
                runner
                for runner in self.runners
                if runner.get("slug") == slug and runner.get("enabled") is True
            ),
            None,
        )

    @staticmethod
    def _cache_target(runner: dict) -> str | None:
        configured = runner.get("cache_directory")
        image = str(runner.get("image") or "")
        target = configured or (
            _CACHE_TARGET if image.startswith(_OFFICIAL_RUNNER_PREFIX) else None
        )
        if target is None:
            return None
        if target != _CACHE_TARGET:
            logger.warning(
                "Ignoring unsupported dependency cache directory %r for runner %s.",
                target,
                runner.get("slug"),
            )
            return None
        return target

    @staticmethod
    def _parse_generation(name: str) -> int | None:
        if not name.startswith("generation-"):
            return None
        try:
            value = int(name.removeprefix("generation-"))
        except ValueError:
            return None
        return value if value >= 1 else None

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not _SAFE_ID_RE.fullmatch(str(value or "")):
            raise DependencyCacheError(f"{label} identifier is invalid.")

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DependencyCacheError(
                "Dependency cache files could not be removed."
            ) from exc
