import asyncio
import logging
import shutil
from pathlib import Path

from arq.connections import RedisSettings

from config import get_settings
from workers.tasks.dependency_cache import prune_dependency_cache
from workers.tasks.deployment import (
    cleanup_inactive_containers,
    delete_container,
    fail_deployment,
    finalize_deployment,
    start_deployment,
)
from workers.tasks.project import delete_project
from workers.tasks.registry import (
    clear_all_runner_images,
    clear_runner_image,
    pull_all_runner_images,
    pull_runner_image,
)
from workers.tasks.storage import deprovision_storage, provision_storage, reset_storage
from workers.tasks.team import delete_team
from workers.tasks.user import delete_user
from services.notifications import deliver_deployment_email, deliver_webhook

logger = logging.getLogger(__name__)

settings = get_settings()


async def worker_startup(ctx):
    work_root = Path("/tmp/layerrail-builds")
    if work_root.exists():
        await asyncio.to_thread(shutil.rmtree, work_root, True)
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)


class WorkerSettings:
    functions = [
        start_deployment,
        finalize_deployment,
        fail_deployment,
        delete_user,
        delete_team,
        delete_project,
        cleanup_inactive_containers,
        delete_container,
        prune_dependency_cache,
        provision_storage,
        deprovision_storage,
        reset_storage,
        pull_runner_image,
        pull_all_runner_images,
        clear_runner_image,
        clear_all_runner_images,
        deliver_webhook,
        deliver_deployment_email,
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 8
    job_timeout = max(
        settings.job_timeout_seconds,
        settings.dockerfile_build_timeout_seconds
        + settings.dockerfile_image_load_timeout_seconds
        + 60,
    )
    job_timeout_seconds = job_timeout
    job_completion_wait_seconds = settings.job_completion_wait_seconds
    max_tries = settings.job_max_tries
    health_check_interval = 65  # Greater than 60s to avoid health check timeout
    allow_abort_jobs = True
    on_startup = worker_startup
