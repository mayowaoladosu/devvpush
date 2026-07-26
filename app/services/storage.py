"""Persistent storage attachment and lifecycle safety rules."""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiodocker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models import Deployment, Project, Storage, StorageProject, utc_now


MAX_STORAGE_ATTACHMENTS = 16
_STORAGE_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_MOUNT_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
_RESERVED_MOUNT_ROOTS = (
    "/bin",
    "/boot",
    "/cache",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
)


class StorageConfigurationError(ValueError):
    """A storage attachment is invalid or conflicts with another attachment."""


class StorageSafetyError(RuntimeError):
    """A destructive operation cannot be proven safe."""


@dataclass(frozen=True)
class RuntimeStorageMount:
    storage_id: str
    storage_name: str
    storage_type: str
    host_path: str
    container_path: str

    @property
    def bind(self) -> str:
        return f"{self.host_path}:{self.container_path}"

    @property
    def application_path(self) -> str:
        if self.storage_type == "database":
            return posixpath.join(self.container_path, "db.sqlite")
        return self.container_path


@dataclass(frozen=True)
class RuntimeStorage:
    mounts: tuple[RuntimeStorageMount, ...]

    @property
    def binds(self) -> list[str]:
        return [mount.bind for mount in self.mounts]

    @property
    def storage_ids(self) -> list[str]:
        return [mount.storage_id for mount in self.mounts]


@dataclass(frozen=True)
class StorageMountUsage:
    container_id: str
    container_name: str
    deployment_id: str
    project_id: str
    state: str


class StorageService:
    """Resolve safe mounts and protect persistent data from live containers."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def validate_storage_identity(storage: Storage) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", str(storage.team_id)):
            raise StorageConfigurationError("Storage team identifier is invalid.")
        if not _STORAGE_NAME_PATTERN.fullmatch(str(storage.name)):
            raise StorageConfigurationError("Storage name is invalid.")
        if storage.type not in {"database", "volume"}:
            raise StorageConfigurationError("Storage type is not mountable.")

    @staticmethod
    def default_mount_path(storage_type: str, storage_name: str) -> str:
        if storage_type not in {"database", "volume"}:
            raise StorageConfigurationError("Storage type is not mountable.")
        if not _STORAGE_NAME_PATTERN.fullmatch(str(storage_name)):
            raise StorageConfigurationError("Storage name is invalid.")
        return f"/data/{storage_type}/{storage_name}"

    @staticmethod
    def normalize_mount_path(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw != "/":
            raw = raw.rstrip("/")
        if len(raw) > 255:
            raise StorageConfigurationError(
                "Mount path must be 255 characters or fewer."
            )
        if not _MOUNT_PATH_PATTERN.fullmatch(raw) or ":" in raw:
            raise StorageConfigurationError(
                "Mount path must be an absolute POSIX path using letters, numbers, dots, underscores, hyphens, and slashes."
            )
        if any(segment in {"", ".", ".."} for segment in raw.split("/")[1:]):
            raise StorageConfigurationError(
                "Mount path cannot contain empty, current-directory, or parent-directory segments."
            )
        normalized = posixpath.normpath(raw)
        if normalized == "/" or any(
            normalized == root or normalized.startswith(f"{root}/")
            for root in _RESERVED_MOUNT_ROOTS
        ):
            raise StorageConfigurationError(
                "Mount path cannot replace a system or platform-managed directory."
            )
        if normalized in {"/app", "/data"}:
            raise StorageConfigurationError(
                "Mount path must target a directory inside /app or /data, not the directory itself."
            )
        return normalized

    @classmethod
    def effective_mount_path(
        cls, storage: Storage, association: StorageProject
    ) -> str:
        return cls.normalize_mount_path(association.mount_path) or cls.default_mount_path(
            storage.type, storage.name
        )

    @staticmethod
    def paths_overlap(first: str, second: str) -> bool:
        first = posixpath.normpath(first)
        second = posixpath.normpath(second)
        return (
            first == second
            or first.startswith(f"{second}/")
            or second.startswith(f"{first}/")
        )

    @staticmethod
    def environments_overlap(first: Iterable[str], second: Iterable[str]) -> bool:
        first_set = set(first)
        second_set = set(second)
        return not first_set or not second_set or bool(first_set & second_set)

    def local_path(self, storage: Storage) -> Path:
        self.validate_storage_identity(storage)
        root = (Path(self.settings.data_dir) / "storage").resolve()
        candidate = (
            root
            / storage.team_id
            / storage.type
            / storage.name
        )
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise StorageConfigurationError(
                "Storage path escapes the managed data directory."
            ) from exc
        return candidate

    def host_path(self, storage: Storage) -> str:
        self.validate_storage_identity(storage)
        self.local_path(storage)
        host_base = self.settings.host_data_dir or self.settings.data_dir
        return os.path.join(
            host_base,
            "storage",
            storage.team_id,
            storage.type,
            storage.name,
        )

    async def validate_attachment(
        self,
        db: AsyncSession,
        *,
        project_id: str,
        storage: Storage,
        mount_path: str | None,
        environment_ids: list[str] | None,
        association_id: str | None = None,
    ) -> str:
        normalized = self.normalize_mount_path(mount_path) or self.default_mount_path(
            storage.type, storage.name
        )
        await db.execute(
            select(Project.id)
            .where(Project.id == project_id)
            .with_for_update()
        )
        result = await db.execute(
            select(StorageProject, Storage)
            .join(Storage, StorageProject.storage_id == Storage.id)
            .where(
                StorageProject.project_id == project_id,
                Storage.status != "deleted",
            )
            .with_for_update()
        )
        attachments = result.all()
        if association_id is None and len(attachments) >= MAX_STORAGE_ATTACHMENTS:
            raise StorageConfigurationError(
                f"A project can connect at most {MAX_STORAGE_ATTACHMENTS} storage resources."
            )

        requested_environments = environment_ids or []
        for association, attached_storage in attachments:
            if association_id and str(association.id) == str(association_id):
                continue
            existing_path = self.effective_mount_path(
                attached_storage, association
            )
            if self.environments_overlap(
                requested_environments, association.environment_ids or []
            ) and self.paths_overlap(normalized, existing_path):
                raise StorageConfigurationError(
                    f'Mount path conflicts with storage "{attached_storage.name}" in one or more selected environments.'
                )
        return normalized

    async def disconnect_attachment(
        self,
        db: AsyncSession,
        *,
        project_id: str,
        association_id: str,
    ) -> bool:
        await db.execute(
            select(Project.id)
            .where(Project.id == project_id)
            .with_for_update()
        )
        association = (
            await db.execute(
                select(StorageProject)
                .where(
                    StorageProject.id == association_id,
                    StorageProject.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not association:
            return False
        await db.delete(association)
        await db.commit()
        return True

    async def runtime(
        self,
        deployment: Deployment,
        db: AsyncSession,
        *,
        lock: bool = False,
    ) -> RuntimeStorage:
        if lock:
            await db.execute(
                select(Project.id)
                .where(Project.id == deployment.project_id)
                .with_for_update(read=True)
            )
        query = (
            select(StorageProject, Storage)
            .join(Storage, StorageProject.storage_id == Storage.id)
            .where(
                StorageProject.project_id == deployment.project_id,
                Storage.status != "deleted",
                Storage.type.in_(["database", "volume"]),
            )
            .order_by(Storage.id.asc())
        )
        if lock:
            query = query.with_for_update(read=True)
        result = await db.execute(query)

        mounts: list[RuntimeStorageMount] = []
        for association, storage in result.all():
            environment_ids = association.environment_ids or []
            if environment_ids and deployment.environment_id not in environment_ids:
                continue
            if storage.status != "active":
                raise StorageConfigurationError(
                    f'Storage "{storage.name}" is not ready (status: {storage.status}).'
                )
            container_path = self.effective_mount_path(storage, association)
            for existing in mounts:
                if self.paths_overlap(container_path, existing.container_path):
                    raise StorageConfigurationError(
                        f'Storage mount path conflicts with "{existing.storage_name}".'
                    )
            mounts.append(
                RuntimeStorageMount(
                    storage_id=storage.id,
                    storage_name=storage.name,
                    storage_type=storage.type,
                    host_path=self.host_path(storage),
                    container_path=container_path,
                )
            )

        if len(mounts) > MAX_STORAGE_ATTACHMENTS:
            raise StorageConfigurationError(
                f"A deployment can mount at most {MAX_STORAGE_ATTACHMENTS} storage resources."
            )
        return RuntimeStorage(tuple(mounts))

    async def mounted_containers(self, storage: Storage) -> list[StorageMountUsage]:
        self.validate_storage_identity(storage)
        try:
            async with aiodocker.Docker(url=self.settings.docker_host) as docker:
                containers = await asyncio.wait_for(
                    docker.containers.list(all=True), timeout=5
                )
        except Exception as exc:
            raise StorageSafetyError(
                "Could not verify whether storage is mounted. Try again after Docker recovers."
            ) from exc
        return self.mount_usage_from_containers(storage, containers)

    async def begin_reset(self, db: AsyncSession, storage_id: str) -> Storage:
        storage = await self._lock_storage(db, storage_id)
        if storage.status != "active":
            raise StorageSafetyError(
                "Storage must be active before it can be reset."
            )
        await self._require_unmounted(storage)
        storage.status = "resetting"
        storage.error = None
        storage.updated_at = utc_now()
        await db.commit()
        return storage

    async def begin_delete(
        self, db: AsyncSession, storage_id: str
    ) -> tuple[Storage, str]:
        storage = await self._lock_storage(db, storage_id)
        if storage.status not in {"active", "pending"}:
            raise StorageSafetyError(
                "Storage is already being changed. Wait for it to finish."
            )
        await self._require_unmounted(storage)
        previous_status = storage.status
        storage.status = "deleted"
        storage.error = None
        storage.updated_at = utc_now()
        await db.commit()
        return storage, previous_status

    async def restore_after_queue_failure(
        self,
        db: AsyncSession,
        storage_id: str,
        *,
        status: str,
        stage: str,
        message: str,
    ) -> None:
        storage = await self._lock_storage(db, storage_id)
        storage.status = status
        storage.error = {
            "stage": stage,
            "message": message,
            "last_attempt_at": utc_now().isoformat(),
        }
        storage.updated_at = utc_now()
        await db.commit()

    async def _lock_storage(self, db: AsyncSession, storage_id: str) -> Storage:
        storage = (
            await db.execute(
                select(Storage)
                .where(Storage.id == storage_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not storage:
            raise StorageSafetyError("Storage no longer exists.")
        return storage

    async def _require_unmounted(self, storage: Storage) -> None:
        usages = await self.mounted_containers(storage)
        if usages:
            raise StorageSafetyError(self.usage_message(usages))

    def mount_usage_from_containers(
        self, storage: Storage, containers: Iterable[Any]
    ) -> list[StorageMountUsage]:
        expected_suffix = self._storage_suffix(storage)
        usages: list[StorageMountUsage] = []
        for container in containers:
            data = getattr(container, "_container", container) or {}
            labels = data.get("Labels") or data.get("labels") or {}
            storage_ids = {
                value.strip()
                for value in str(labels.get("devpush.storage_ids") or "").split(",")
                if value.strip()
            }
            source_match = any(
                self._normalize_source(mount.get("Source") or "").endswith(
                    expected_suffix
                )
                for mount in (data.get("Mounts") or [])
            )
            if storage.id not in storage_ids and not source_match:
                continue
            names = data.get("Names") or []
            usages.append(
                StorageMountUsage(
                    container_id=str(
                        data.get("Id") or data.get("ID") or data.get("id") or ""
                    )[:12],
                    container_name=str(names[0] if names else "").lstrip("/"),
                    deployment_id=str(labels.get("devpush.deployment_id") or ""),
                    project_id=str(labels.get("devpush.project_id") or ""),
                    state=str(data.get("State") or data.get("state") or "unknown"),
                )
            )
        return usages

    @staticmethod
    def usage_message(usages: list[StorageMountUsage]) -> str:
        deployment_ids = [
            usage.deployment_id[:7]
            for usage in usages
            if usage.deployment_id
        ]
        suffix = f" ({', '.join(deployment_ids[:3])})" if deployment_ids else ""
        return (
            f"Storage is still mounted by {len(usages)} deployment container(s){suffix}. "
            "Remove those deployments before resetting or deleting it."
        )

    @staticmethod
    def _normalize_source(value: str) -> str:
        return str(value).replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _storage_suffix(storage: Storage) -> str:
        return (
            f"/storage/{storage.team_id}/{storage.type}/{storage.name}"
        ).casefold()
