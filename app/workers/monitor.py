import asyncio
import logging
import re
from datetime import datetime, timezone

import aiodocker
import httpx
from arq.connections import ArqRedis, RedisSettings, create_pool
from sqlalchemy import exc, inspect, select

from config import get_settings
from db import AsyncSessionLocal
from models import Deployment
from services.deployment import DeploymentService

logger = logging.getLogger(__name__)

deployment_probe_state = {}  # deployment_id -> {"container": container_obj, "probe_active": bool}


def _parse_container_started_at(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(
        r"(\.\d{6})\d+(?=Z$|[+-]\d{2}:\d{2}$)",
        r"\1",
        str(value),
    ).replace("Z", "+00:00")
    try:
        started_at = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at.astimezone(timezone.utc)


def _readiness_timed_out(
    container_info: dict,
    *,
    now: datetime,
    fallback_started_at: datetime,
    timeout_seconds: int,
) -> bool:
    started_at = _parse_container_started_at(
        container_info.get("State", {}).get("StartedAt")
    )
    if started_at is None:
        started_at = fallback_started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds() > timeout_seconds


async def _http_probe(ip: str, port: int, timeout: float = 5) -> bool:
    """Check if the app responds to HTTP requests."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(f"http://{ip}:{port}/")
            return True
    except Exception:
        return False


async def _check_status(
    deployment: Deployment,
    docker_client: aiodocker.Docker,
    redis_pool: ArqRedis,
    db,
):
    """Checks the status of a single deployment's container."""
    if deployment.status == "completed" or deployment.conclusion:
        return

    if (
        deployment.id in deployment_probe_state
        and deployment_probe_state[deployment.id]["probe_active"]
    ):
        return

    log_prefix = f"[DeployMonitor:{deployment.id}]"

    if deployment.id not in deployment_probe_state:
        try:
            container = await docker_client.containers.get(deployment.container_id)
            deployment_probe_state[deployment.id] = {
                "container": container,
                "probe_active": True,
            }
        except Exception:
            await redis_pool.enqueue_job(
                "fail_deployment",
                deployment.id,
                "deploy",
                "Container stopped unexpectedly. Check the deployment logs for errors.",
            )
            return
    else:
        deployment_probe_state[deployment.id]["probe_active"] = True
        container = deployment_probe_state[deployment.id]["container"]

    # Probe check
    try:
        logger.info(f"{log_prefix} Probing container {deployment.container_id}")
        container_info = await container.show()
        status = container_info["State"]["Status"]

        created_at = (
            deployment.created_at.replace(tzinfo=timezone.utc)
            if deployment.created_at.tzinfo is None
            else deployment.created_at
        )
        if status == "running" and _readiness_timed_out(
            container_info,
            now=datetime.now(timezone.utc),
            fallback_started_at=created_at,
            timeout_seconds=get_settings().deployment_timeout_seconds,
        ):
            await redis_pool.enqueue_job(
                "fail_deployment",
                deployment.id,
                "deploy",
                "Timed out waiting for app to respond on port 8000. Ensure your app starts an HTTP server on this port.",
            )
            logger.warning(
                f"{log_prefix} Deployment timed out; failure job enqueued."
            )
            await _cleanup_deployment(deployment.id)
            return

        if status == "exited":
            exit_code = container_info["State"].get("ExitCode", -1)
            if exit_code == 0:
                reason = "App exited unexpectedly. Ensure your app keeps running and doesn't exit on its own."
            elif exit_code == 137:
                reason = "App was killed (out of memory or manually stopped). Try increasing memory limits."
            elif exit_code == 1:
                reason = (
                    "App crashed on startup. Check deployment logs for error details."
                )
            else:
                reason = f"App exited with code {exit_code}. Check deployment logs for error details."
            await redis_pool.enqueue_job(
                "fail_deployment",
                deployment.id,
                "deploy",
                reason,
            )
            logger.warning(
                f"{log_prefix} Deployment failed (failure job enqueued): {reason}"
            )
            await _cleanup_deployment(deployment.id)

        elif status == "running":
            networks = container_info.get("NetworkSettings", {}).get("Networks", {})
            container_ip = networks.get("devpush_runner", {}).get("IPAddress")
            if container_ip and await _http_probe(container_ip, 8000):
                await DeploymentService.update_status(
                    db,
                    deployment,
                    status="finalize",
                    redis_client=redis_pool,
                )
                await redis_pool.enqueue_job("finalize_deployment", deployment.id)
                logger.info(
                    f"{log_prefix} Deployment ready (finalization job enqueued)."
                )
                await _cleanup_deployment(deployment.id)

    except Exception as e:
        logger.error(
            f"{log_prefix} Unexpected error while checking status.", exc_info=True
        )
        await redis_pool.enqueue_job(
            "fail_deployment",
            deployment.id,
            "deploy",
            f"Unexpected error while monitoring deployment: {e}",
        )
        await _cleanup_deployment(deployment.id)
    finally:
        if deployment.id in deployment_probe_state:
            deployment_probe_state[deployment.id]["probe_active"] = False


async def _check_status_by_id(
    deployment_id: str,
    docker_client: aiodocker.Docker,
    redis_pool: ArqRedis,
):
    async with AsyncSessionLocal() as deployment_db:
        deployment = await deployment_db.get(Deployment, deployment_id)
        if deployment:
            await _check_status(
                deployment,
                docker_client,
                redis_pool,
                deployment_db,
            )


# Cleanup function
async def _cleanup_deployment(deployment_id: str):
    """Cleans up a deployment from the status dictionary."""
    if deployment_id in deployment_probe_state:
        del deployment_probe_state[deployment_id]


async def monitor():
    """Monitors the status of deployments."""
    logger.info("Deployment monitor started")
    settings = get_settings()
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    redis_pool = await create_pool(redis_settings)

    async with AsyncSessionLocal() as db:
        async with aiodocker.Docker(url=settings.docker_host) as docker_client:
            schema_ready = False
            while True:
                try:
                    # Ensure schema exists to avoid logging spam before migrations
                    if not schema_ready:
                        schema_ready = await db.run_sync(
                            lambda sync_session: inspect(
                                sync_session.connection()
                            ).has_table("alembic_version")
                        )
                        if not schema_ready:
                            logger.warning(
                                "Database schema not ready (no alembic_version); waiting for migrations..."
                            )
                            await asyncio.sleep(5)
                            continue

                    result = await db.execute(
                        select(Deployment.id).where(
                            Deployment.status == "deploy",
                            Deployment.conclusion.is_(None),
                            Deployment.container_status == "running",
                        )
                    )
                    deployment_ids = result.scalars().all()

                    if deployment_ids:
                        tasks = [
                            _check_status_by_id(
                                deployment_id,
                                docker_client,
                                redis_pool,
                            )
                            for deployment_id in deployment_ids
                        ]
                        await asyncio.gather(*tasks)

                except exc.SQLAlchemyError as e:
                    logger.error(f"Database error in monitor loop: {e}. Reconnecting.")
                    await db.close()
                    db = AsyncSessionLocal()
                except Exception:
                    logger.error("Critical error in monitor main loop", exc_info=True)

                await asyncio.sleep(2)


if __name__ == "__main__":
    import asyncio

    asyncio.run(monitor())
