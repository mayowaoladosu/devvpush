import asyncio
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import select, delete

from config import get_settings
from db import AsyncSessionLocal
from models import Storage, StorageProject, utc_now
from services.storage import StorageSafetyError, StorageService

logger = logging.getLogger(__name__)


async def provision_storage(ctx, resource_id: str):
    log_prefix = f"[ProvisionStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage provisioning")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(
                select(Storage)
                .where(Storage.id == resource_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return
        if storage.status != "pending":
            logger.info(
                "%s Ignoring provision request in status %s",
                log_prefix,
                storage.status,
            )
            return

        try:
            if storage.type == "database":
                await asyncio.to_thread(_ensure_database_path, settings, storage)
            elif storage.type == "volume":
                await asyncio.to_thread(_ensure_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            storage.status = "active"
            storage.error = None
            storage.updated_at = utc_now()
            await db.commit()
            logger.info(f"{log_prefix} Storage provisioned")
        except Exception as exc:
            storage.error = {
                "stage": f"provision_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Provisioning failed: {exc}", exc_info=True)
            raise


async def deprovision_storage(ctx, resource_id: str):
    log_prefix = f"[DeprovisionStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage deprovisioning")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(
                select(Storage)
                .where(Storage.id == resource_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return
        if storage.status != "deleted":
            logger.info(
                "%s Ignoring deprovision request in status %s",
                log_prefix,
                storage.status,
            )
            return

        try:
            storage_service = StorageService(settings)
            usages = await storage_service.mounted_containers(storage)
            if usages:
                storage.status = "active"
                storage.error = {
                    "stage": "deprovision_in_use",
                    "message": storage_service.usage_message(usages),
                    "last_attempt_at": utc_now().isoformat(),
                }
                storage.updated_at = utc_now()
                await db.commit()
                logger.warning("%s Storage is still mounted", log_prefix)
                return
            if storage.type == "database":
                await asyncio.to_thread(_remove_database_path, settings, storage)
            elif storage.type == "volume":
                await asyncio.to_thread(_remove_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            await db.execute(
                delete(StorageProject).where(StorageProject.storage_id == storage.id)
            )
            await db.execute(delete(Storage).where(Storage.id == storage.id))
            await db.commit()
            logger.info(f"{log_prefix} Storage deprovisioned")
        except StorageSafetyError as exc:
            storage.status = "active"
            storage.error = {
                "stage": "deprovision_safety_check",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error("%s Deprovision safety check failed: %s", log_prefix, exc)
        except Exception as exc:
            storage.error = {
                "stage": f"deprovision_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Deprovisioning failed: {exc}", exc_info=True)
            raise


async def reset_storage(ctx, resource_id: str):
    log_prefix = f"[ResetStorage:{resource_id}]"
    logger.info(f"{log_prefix} Starting storage reset")
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        storage = (
            await db.execute(
                select(Storage)
                .where(Storage.id == resource_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not storage:
            logger.error(f"{log_prefix} Storage not found")
            return
        if storage.status != "resetting":
            logger.info(
                "%s Ignoring reset request in status %s",
                log_prefix,
                storage.status,
            )
            return

        try:
            storage_service = StorageService(settings)
            usages = await storage_service.mounted_containers(storage)
            if usages:
                storage.status = "active"
                storage.error = {
                    "stage": "reset_in_use",
                    "message": storage_service.usage_message(usages),
                    "last_attempt_at": utc_now().isoformat(),
                }
                storage.updated_at = utc_now()
                await db.commit()
                logger.warning("%s Storage is still mounted", log_prefix)
                return
            if storage.type == "database":
                await asyncio.to_thread(_reset_database_path, settings, storage)
            elif storage.type == "volume":
                await asyncio.to_thread(_reset_volume_path, settings, storage)
            else:
                logger.error(f"{log_prefix} Unsupported storage type: {storage.type}")
                return

            storage.status = "active"
            storage.error = None
            storage.updated_at = utc_now()
            await db.commit()
            logger.info(f"{log_prefix} Storage reset")
        except Exception as exc:
            storage.status = "active"
            storage.error = {
                "stage": f"reset_{storage.type}",
                "message": str(exc),
                "last_attempt_at": utc_now().isoformat(),
            }
            storage.updated_at = utc_now()
            await db.commit()
            logger.error(f"{log_prefix} Reset failed: {exc}", exc_info=True)
            raise


def _ensure_database_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    db_path = base_dir / "db.sqlite"

    base_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    finally:
        conn.close()
    _apply_storage_permissions(settings, base_dir, db_path)


def _ensure_volume_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    base_dir.mkdir(parents=True, exist_ok=True)
    _apply_storage_permissions(settings, base_dir)


def _remove_database_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    if base_dir.exists():
        shutil.rmtree(base_dir)


def _remove_volume_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    if base_dir.exists():
        shutil.rmtree(base_dir)


def _reset_database_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    if base_dir.exists():
        shutil.rmtree(base_dir)
    _ensure_database_path(settings, storage)


def _reset_volume_path(settings, storage: Storage) -> None:
    base_dir = StorageService(settings).local_path(storage)
    base_dir.mkdir(parents=True, exist_ok=True)
    for entry in base_dir.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    _apply_storage_permissions(settings, base_dir)


def _apply_storage_permissions(
    settings, base_dir: Path, db_path: Path | None = None
) -> None:
    uid = int(settings.service_uid)
    gid = int(settings.service_gid)
    try:
        os.chown(base_dir, uid, gid)
        os.chmod(base_dir, 0o2770)
        if db_path and db_path.exists():
            os.chown(db_path, uid, gid)
            os.chmod(db_path, 0o660)
    except Exception as exc:
        if settings.env == "development":
            logger.warning("Failed to set development storage permissions: %s", exc)
            return
        raise RuntimeError("Failed to secure persistent storage permissions.") from exc
