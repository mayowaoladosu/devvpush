import asyncio
import logging
from pathlib import Path

import aiodocker
from sqlalchemy import or_, select

from config import get_settings
from db import AsyncSessionLocal
from models import Deployment, Project
from services.dependency_cache import DependencyCacheService
from services.registry import RegistryService

logger = logging.getLogger(__name__)


async def prune_dependency_cache(ctx, project_id: str):
    """Remove cache generations no longer mounted by project containers."""
    settings = get_settings()
    runners = RegistryService(Path(settings.data_dir) / "registry").state.runners
    cache = DependencyCacheService(settings, runners)

    async with AsyncSessionLocal() as db:
        project = await db.get(Project, project_id)
        if not project:
            removed = await asyncio.to_thread(cache.delete_project, project_id)
            if removed:
                logger.info(
                    "[DependencyCache:%s] Removed deleted project cache.", project_id
                )
            return

        preserve = {cache.generation(project.config)}
        result = await db.execute(
            select(Deployment.container_id).where(
                Deployment.project_id == project_id,
                Deployment.container_id.isnot(None),
                or_(
                    Deployment.container_status.is_(None),
                    Deployment.container_status != "removed",
                ),
            )
        )
        container_ids = [value for value in result.scalars().all() if value]

    async with aiodocker.Docker(url=settings.docker_host) as docker_client:
        for container_id in container_ids:
            try:
                container = await docker_client.containers.get(container_id)
                info = await container.show()
            except aiodocker.DockerError as error:
                if error.status == 404:
                    continue
                raise
            labels = (info.get("Config") or {}).get("Labels") or {}
            if labels.get("devpush.project_id") != project_id:
                continue
            raw_generation = labels.get("devpush.cache_generation")
            try:
                generation = int(raw_generation)
            except (TypeError, ValueError):
                continue
            if generation >= 1:
                preserve.add(generation)

    removed_paths = await asyncio.to_thread(cache.prune, project_id, preserve)
    if removed_paths:
        logger.info(
            "[DependencyCache:%s] Pruned %s unused generation(s).",
            project_id,
            len(removed_paths),
        )
