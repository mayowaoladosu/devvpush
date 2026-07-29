import asyncio
import logging
import shlex
from pathlib import Path

import aiodocker
from arq.connections import ArqRedis
from sqlalchemy import select, true
from sqlalchemy.orm import joinedload

from config import get_settings
from db import AsyncSessionLocal
from dependencies import (
    get_github_installation_service,
    get_redis_client,
)
from models import Alias, Deployment, Project
from services.dependency_cache import DependencyCacheService
from services.deployment import DeploymentService
from services.deployment_diagnostics import DeploymentDiagnosticService
from services.deployment_heartbeat import deployment_heartbeat
from services.deployment_jobs import DeploymentJobs
from services.dockerfile_builder import (
    DockerfileBuilder,
    DockerfileBuildSpec,
    is_managed_deployment_image,
    remove_managed_deployment_image,
    validate_dockerfile_runtime_image,
)
from services.loki import LokiService
from services.node_runtime import deployment_runtime_client
from services.registry import RegistryService

logger = logging.getLogger(__name__)

_build_semaphore: asyncio.Semaphore | None = None
_build_semaphore_limit: int | None = None


def _get_build_semaphore(limit: int) -> asyncio.Semaphore:
    global _build_semaphore, _build_semaphore_limit
    if _build_semaphore is None or _build_semaphore_limit != limit:
        _build_semaphore = asyncio.Semaphore(limit)
        _build_semaphore_limit = limit
    return _build_semaphore


def _get_dockerfile_builder(settings) -> DockerfileBuilder:
    proxy_url = settings.buildkit_proxy_url.strip()
    if not proxy_url:
        raise RuntimeError("BuildKit egress proxy address is unavailable.")
    return DockerfileBuilder(
        buildkit_host=settings.buildkit_host,
        docker_host=settings.docker_host,
        proxy_url=proxy_url,
        timeout_seconds=settings.dockerfile_build_timeout_seconds,
        image_load_timeout_seconds=settings.dockerfile_image_load_timeout_seconds,
        max_archive_bytes=settings.dockerfile_max_archive_bytes,
        max_context_bytes=settings.dockerfile_max_context_bytes,
        max_context_files=settings.dockerfile_max_context_files,
        max_image_bytes=settings.dockerfile_max_image_bytes,
    )


async def _push_loki_log(
    loki: LokiService,
    deployment: Deployment,
    message: str,
    level: str | None = None,
) -> None:
    labels = {
        "project_id": deployment.project_id,
        "deployment_id": deployment.id,
        "environment_id": deployment.environment_id,
        "branch": deployment.branch,
        "stream": "stdout",
    }
    line = f"{level}: {message}" if level else message
    try:
        await loki.push_log(labels, line)
    except Exception as exc:
        logger.warning("Failed to push log to Loki: %s", exc)
        await DeploymentDiagnosticService.record_once(
            deployment.id,
            level="WARNING",
            source="loki",
            stage=deployment.status or "prepare",
            code="loki_write_failed",
            message=(
                "Live log delivery is temporarily unavailable. "
                "Control-plane diagnostics are still being stored."
            ),
            details={"error": exc.__class__.__name__},
        )


async def _cleanup_startup_resources(
    *,
    deployment: Deployment,
    container,
    image_reference: str | None,
    settings,
    loki: LokiService | None,
) -> None:
    """Remove resources that may exist before normal lifecycle tracking begins."""
    async with deployment_runtime_client(deployment, settings) as docker_client:
        container_identifier = getattr(container, "id", None) or (
            f"runner-{deployment.id}"
        )
        try:
            tracked_container = await docker_client.containers.get(
                container_identifier
            )
        except aiodocker.DockerError as error:
            if error.status != 404:
                raise
        else:
            if loki:
                await loki.preserve_container_logs(tracked_container, deployment)
            try:
                await tracked_container.stop()
            except Exception:
                pass
            await tracked_container.delete(force=True)

        await remove_managed_deployment_image(
            docker_client,
            image_reference or deployment.image,
        )


async def _cleanup_if_concluded(
    *,
    db,
    deployment: Deployment,
    container,
    image_reference: str | None,
    settings,
    loki: LokiService | None,
) -> bool:
    """Stop startup if another request already concluded the deployment."""
    await db.refresh(
        deployment,
        attribute_names=["status", "conclusion", "error", "container_status"],
    )
    if not deployment.conclusion:
        return False

    await _cleanup_startup_resources(
        deployment=deployment,
        container=container,
        image_reference=image_reference,
        settings=settings,
        loki=loki,
    )
    await DeploymentService.update_status(
        db,
        deployment,
        status="completed",
        container_status=(
            "removed"
            if container or deployment.container_id
            else deployment.container_status
        ),
        emit=False,
    )
    logger.info(
        "[DeployStart:%s] Startup stopped after terminal conclusion %s.",
        deployment.id,
        deployment.conclusion,
    )
    return True


@deployment_heartbeat("prepare")
async def start_deployment(ctx, deployment_id: str):
    """Starts a deployment."""
    settings = get_settings()
    log_prefix = f"[DeployStart:{deployment_id}]"
    container = None
    deployment = None
    managed_image_reference = None
    loki: LokiService | None = None
    failure_stage = "prepare"
    try:
        redis_client = get_redis_client()
        logger.info(f"{log_prefix} Starting deployment")

        github_installation_service = get_github_installation_service()

        async with AsyncSessionLocal() as db:
            deployment = (
                await db.execute(
                    select(Deployment)
                    .options(joinedload(Deployment.project).joinedload(Project.team))
                    .options(joinedload(Deployment.node))
                    .where(Deployment.id == deployment_id)
                )
            ).scalar_one()
            loki = LokiService()

            if deployment.conclusion:
                logger.info(
                    "%s Deployment already concluded (%s); skipping startup.",
                    log_prefix,
                    deployment.conclusion,
                )
                return

            container = None
            async with deployment_runtime_client(
                deployment, settings
            ) as docker_client:
                # Mark deployment as in-progress
                await DeploymentService.update_status(
                    db,
                    deployment,
                    status="prepare",
                    redis_client=redis_client,
                )

                if await _cleanup_if_concluded(
                    db=db,
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                ):
                    return

                # Prepare environment variables
                env_vars_dict = DeploymentService().get_runtime_env_vars(
                    deployment, settings
                )
                mounts: list[str] = []
                storage_ids: list[str] = []
                config = deployment.config or {}
                uses_dockerfile = DeploymentService.uses_dockerfile(config)
                registry_state = RegistryService(
                    Path(settings.data_dir) / "registry"
                ).state
                cache_mount = DependencyCacheService(
                    settings, registry_state.runners
                ).prepare(deployment, create_local=not bool(deployment.node_id))
                if cache_mount:
                    mounts.append(cache_mount.bind)
                    env_vars_dict["DEVPUSH_DEPENDENCY_CACHE_GENERATION"] = str(
                        cache_mount.generation
                    )
                    if not deployment.node_id:
                        env_vars_dict["DEVPUSH_DEPENDENCY_CACHE"] = (
                            "hit" if cache_mount.warm else "miss"
                        )
                        await _push_loki_log(
                            loki,
                            deployment,
                            "Dependency cache %s (generation %s, runner %s)"
                            % (
                                "hit" if cache_mount.warm else "miss",
                                cache_mount.generation,
                                cache_mount.runner_slug,
                            ),
                        )

                commands = []
                github_installation = (
                    await github_installation_service.get_or_refresh_installation(
                        deployment.project.github_installation_id, db
                    )
                )
                if not github_installation.token:
                    raise ValueError("GitHub installation token missing.")

                runner_image = deployment.image
                if uses_dockerfile:
                    image_available = False
                    if is_managed_deployment_image(runner_image):
                        try:
                            await docker_client.images.get(runner_image)
                            image_available = True
                            await _push_loki_log(
                                loki,
                                deployment,
                                f"Reusing built image ({runner_image})",
                            )
                        except aiodocker.DockerError as error:
                            if error.status != 404:
                                raise

                    if not image_available:
                        spec = DockerfileBuildSpec(
                            deployment_id=deployment.id,
                            project_id=deployment.project_id,
                            repo_full_name=deployment.repo_full_name,
                            commit_sha=deployment.commit_sha,
                            source_token=github_installation.token,
                            root_directory=str(config.get("root_directory") or ""),
                            dockerfile_path=str(
                                config.get("dockerfile_path") or "Dockerfile"
                            ),
                        )
                        managed_image_reference = spec.image_reference

                        async def build_log(message: str) -> None:
                            await _push_loki_log(loki, deployment, message)

                        semaphore = _get_build_semaphore(
                            settings.dockerfile_build_max_concurrency
                        )
                        async with semaphore:
                            builder = _get_dockerfile_builder(settings)
                            image_loader = None
                            if deployment.node_id:

                                async def image_loader(
                                    archive_path,
                                    image_reference,
                                    on_log,
                                ):
                                    await docker_client.images.load_archive(
                                        archive_path,
                                        image_reference,
                                        deployment.id,
                                    )

                            result = await builder.build(
                                spec,
                                build_log,
                                image_loader=image_loader,
                            )
                        runner_image = result.image_reference
                        deployment.image = runner_image
                        await db.commit()
                else:
                    # Clone the selected revision inside the language runner.
                    commands.append(
                        f"echo 'Cloning {deployment.repo_full_name} (Branch: {deployment.branch}, Commit: {deployment.commit_sha[:7]})'"
                    )
                    env_vars_dict["DEVPUSH_GITHUB_TOKEN"] = github_installation.token
                    commands.append(
                        "git init -q && "
                        "printf '%s\n' "
                        "'#!/bin/sh' "
                        '\'case "$1" in *Username*) echo "x-access-token";; *) echo "$DEVPUSH_GITHUB_TOKEN";; esac\' '
                        "> /tmp/devpush-git-askpass && "
                        "chmod 700 /tmp/devpush-git-askpass && "
                        "export GIT_ASKPASS=/tmp/devpush-git-askpass GIT_TERMINAL_PROMPT=0 && "
                        f"git fetch -q --depth 1 https://github.com/{deployment.repo_full_name}.git {deployment.commit_sha} && "
                        "git checkout -q FETCH_HEAD && "
                        "unset GIT_ASKPASS GIT_TERMINAL_PROMPT DEVPUSH_GITHUB_TOKEN && "
                        "rm -f /tmp/devpush-git-askpass"
                    )

                    normalized_root_directory = (
                        str(config.get("root_directory") or "")
                        .strip()
                        .lstrip("./")
                        .strip("/")
                    )
                    if normalized_root_directory not in ("", ".", "./"):
                        quoted_root_directory = shlex.quote(
                            normalized_root_directory
                        )
                        commands.append(
                            f"echo 'Changing root directory to {normalized_root_directory}'"
                        )
                        commands.append(
                            f"test -d {quoted_root_directory} || {{ printf '\\033[31mError: root directory %s not found\\033[0m\\n' {quoted_root_directory} 1>&2; exit 1; }}"
                        )
                        commands.append(f"cd {quoted_root_directory}")

                    if config.get("build_command"):
                        commands.append("echo 'Installing dependencies...'")
                        commands.append(f"( {config.get('build_command')} )")
                        if cache_mount:
                            commands.append(
                                f"touch {shlex.quote(cache_mount.ready_marker)}"
                            )

                    if config.get("pre_deploy_command"):
                        commands.append("echo 'Running pre-deploy command...'")
                        commands.append(f"( {config.get('pre_deploy_command')} )")

                    commands.append("echo 'Starting application...'")
                    commands.append(f"( {config.get('start_command')} )")

                # Setup container configuration
                container_name = f"runner-{deployment.id}"
                router = f"deployment-{deployment.id}"

                labels = {
                    "traefik.enable": "true",
                    f"traefik.http.routers.{router}.rule": f"Host(`{deployment.slug}.{settings.deploy_domain}`)",
                    f"traefik.http.routers.{router}.service": f"{router}@docker",
                    f"traefik.http.routers.{router}.priority": "10",
                    f"traefik.http.services.{router}.loadbalancer.server.port": "8000",
                    "traefik.docker.network": "devpush_runner",
                    "devpush.deployment_id": deployment.id,
                    "devpush.project_id": deployment.project_id,
                    "devpush.environment_id": deployment.environment_id,
                    "devpush.branch": deployment.branch,
                }
                if cache_mount:
                    labels.update(
                        {
                            "devpush.cache_generation": str(cache_mount.generation),
                            "devpush.cache_namespace": cache_mount.namespace,
                        }
                    )

                if settings.url_scheme == "https":
                    labels.update(
                        {
                            f"traefik.http.routers.{router}.entrypoints": "websecure",
                            f"traefik.http.routers.{router}.tls": "true",
                            f"traefik.http.routers.{router}.tls.certresolver": "le",
                        }
                    )
                else:
                    labels[f"traefik.http.routers.{router}.entrypoints"] = "web"

                cpus: float | None = settings.default_cpus
                memory_mb: int | None = settings.default_memory_mb

                if settings.allow_custom_cpu and config.get("cpus") is not None:
                    max_cpus = settings.max_cpus
                    assert max_cpus is not None
                    try:
                        override_cpus = float(config.get("cpus"))
                    except (ValueError, TypeError):
                        logger.warning(
                            f"{log_prefix} Invalid CPU override in config; using default."
                        )
                    else:
                        if override_cpus > 0:
                            cpus = min(override_cpus, max_cpus)

                if settings.allow_custom_memory and config.get("memory") is not None:
                    max_memory_mb = settings.max_memory_mb
                    assert max_memory_mb is not None
                    try:
                        override_memory_mb = int(config.get("memory"))
                    except (ValueError, TypeError):
                        logger.warning(
                            f"{log_prefix} Invalid memory override in config; using default."
                        )
                    else:
                        if override_memory_mb > 0:
                            memory_mb = min(override_memory_mb, max_memory_mb)

                if not runner_image:
                    runner_slug = config.get("runner") or config.get("image")
                    if not runner_slug:
                        raise ValueError("Runner not set in deployment config.")
                    registry_state = RegistryService(
                        Path(settings.data_dir) / "registry"
                    ).state
                    runner_image = next(
                        (
                            runner.get("image")
                            for runner in registry_state.runners
                            if runner.get("slug") == runner_slug
                        ),
                        None,
                    )
                if not runner_image:
                    raise ValueError("Runner image not found for deployment.")

                if await _cleanup_if_concluded(
                    db=db,
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                ):
                    return

                await _push_loki_log(
                    loki,
                    deployment,
                    "Checking runner image availability...",
                )
                try:
                    image_info = await docker_client.images.inspect(runner_image)
                    if uses_dockerfile:
                        validate_dockerfile_runtime_image(image_info)
                    await _push_loki_log(
                        loki,
                        deployment,
                        f"Runner image already present ({runner_image})",
                    )
                except aiodocker.DockerError as error:
                    if error.status == 404:
                        if uses_dockerfile:
                            raise ValueError(
                                "Built Dockerfile image is unavailable after loading."
                            ) from error
                        await _push_loki_log(
                            loki,
                            deployment,
                            f"Pulling runner image ({runner_image})...",
                        )
                        try:
                            await docker_client.images.pull(runner_image)
                            await _push_loki_log(
                                loki,
                                deployment,
                                "Runner image pulled",
                            )
                        except Exception:
                            await _push_loki_log(
                                loki,
                                deployment,
                                "Failed to pull runner image",
                                level="Error",
                            )
                            raise
                    else:
                        raise

                runtime_storage = await DeploymentService().get_runtime_storage(
                    deployment,
                    db,
                    settings,
                    lock=True,
                )
                mounts.extend(runtime_storage.binds)
                storage_ids = runtime_storage.storage_ids
                for key, value in runtime_storage.environment.items():
                    if key.startswith(("DEVPUSH_OBJECT_", "DEVPUSH_MEDIA_")):
                        env_vars_dict[key] = value
                    else:
                        env_vars_dict.setdefault(key, value)
                if storage_ids:
                    labels["devpush.storage_ids"] = ",".join(storage_ids)
                    await _push_loki_log(
                        loki,
                        deployment,
                        "Attaching %s persistent storage resource(s)"
                        % len(storage_ids),
                    )

                await _push_loki_log(
                    loki,
                    deployment,
                    "Preparing and starting container...",
                )

                # Create and start container
                try:
                    container_config = {
                        "Image": runner_image,
                        "Env": [f"{k}={v}" for k, v in env_vars_dict.items()],
                        "Labels": labels,
                        "NetworkingConfig": {
                            "EndpointsConfig": {"devpush_runner": {}}
                        },
                        "HostConfig": {
                            **(
                                {
                                    "CpuQuota": int(cpus * 100000),
                                    "CpuPeriod": 100000,
                                }
                                if cpus is not None and cpus > 0
                                else {}
                            ),
                            **(
                                {"Memory": memory_mb * 1024 * 1024}
                                if memory_mb is not None and memory_mb > 0
                                else {}
                            ),
                            **({"Binds": mounts} if mounts else {}),
                            **(
                                {"GroupAdd": [str(settings.service_gid)]}
                                if runtime_storage.mounts
                                else {}
                            ),
                            "CapDrop": ["ALL"],
                            **(
                                {
                                    "CapAdd": [
                                        "CHOWN",
                                        "DAC_OVERRIDE",
                                        "FOWNER",
                                        "SETGID",
                                        "SETUID",
                                    ]
                                }
                                if not uses_dockerfile
                                else {}
                            ),
                            "PidsLimit": settings.runtime_pids_limit,
                            "SecurityOpt": ["no-new-privileges:true"],
                            "LogConfig": {
                                "Type": "json-file",
                                "Config": {"max-size": "10m", "max-file": "5"},
                            },
                        },
                    }
                    if not uses_dockerfile:
                        container_config.update(
                            {
                                "Cmd": ["/bin/sh", "-c", " && ".join(commands)],
                                "WorkingDir": "/app",
                            }
                        )
                    container = await docker_client.containers.create_or_replace(
                        name=container_name,
                        config=container_config,
                    )
                except aiodocker.DockerError as error:
                    queue: ArqRedis = ctx["redis"]
                    err_str = str(error)
                    if "No such image" in err_str or "not found" in err_str.lower():
                        reason = "Runner image not found. Contact your administrator."
                    elif "port is already allocated" in err_str.lower():
                        reason = "Port conflict. Another deployment may be using the same port."
                    else:
                        reason = f"Failed to create container: {error}"
                    await DeploymentJobs.enqueue_failure(
                        queue,
                        deployment_id,
                        "prepare",
                        reason,
                        code="container_create_failed",
                        source="docker",
                        details={"docker_status": error.status},
                    )
                    logger.error(
                        f"{log_prefix} Failed to start container for {deployment.id}: {error}"
                    )
                    return

                deployment.container_id = container.id
                deployment.runtime_url = getattr(container, "runtime_url", None)
                if deployment.node_id and not deployment.runtime_url:
                    raise RuntimeError(
                        "Remote node did not provide a routable runtime address."
                    )
                await db.commit()

                if await _cleanup_if_concluded(
                    db=db,
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                ):
                    return

                await container.start()
                if deployment.node_id:
                    deployment.runtime_url = getattr(container, "runtime_url", None)
                    await db.commit()
                    if cache_mount:
                        await _push_loki_log(
                            loki,
                            deployment,
                            "Node-local dependency cache %s (generation %s, runner %s)"
                            % (
                                "hit" if getattr(container, "cache_warm", False) else "miss",
                                cache_mount.generation,
                                cache_mount.runner_slug,
                            ),
                        )

                if await _cleanup_if_concluded(
                    db=db,
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                ):
                    return

                # Save container info
                failure_stage = "deploy"
                await DeploymentService.update_status(
                    db,
                    deployment,
                    status="deploy",
                    container_status="running",
                    redis_client=redis_client,
                )
                logger.info(
                    f"{log_prefix} Container {container.id} started. Monitoring..."
                )

    except asyncio.CancelledError:
        logger.info(f"{log_prefix} Deployment canceled.")

        if container and deployment:
            try:
                try:
                    await container.stop()
                except Exception:
                    pass
                if loki:
                    preserved = await loki.preserve_container_logs(
                        container, deployment
                    )
                    if preserved:
                        logger.info(
                            "%s Preserved %s final container log lines.",
                            log_prefix,
                            preserved,
                        )
                queue: ArqRedis = ctx["redis"]
                await queue.enqueue_job(
                    "delete_container",
                    deployment_id,
                    _defer_by=settings.container_delete_grace_seconds,
                )
            except Exception as e:
                logger.error(f"{log_prefix} Error cleaning up container: {e}")

            try:
                async with AsyncSessionLocal() as db:
                    current_deployment = (
                        await db.execute(
                            select(Deployment)
                            .options(joinedload(Deployment.project))
                            .options(joinedload(Deployment.node))
                            .where(Deployment.id == deployment_id)
                        )
                    ).scalar_one_or_none()
                    if current_deployment:
                        values = {
                            "status": "completed",
                            "container_status": (
                                "removed"
                                if current_deployment.container_status == "removed"
                                else "stopped"
                            ),
                            "redis_client": get_redis_client(),
                        }
                        if not current_deployment.conclusion:
                            message = (
                                "The deployment worker was interrupted before "
                                "startup completed."
                            )
                            payload = DeploymentDiagnosticService.failure_payload(
                                stage=failure_stage,
                                code="worker_interrupted",
                                message=message,
                                source="worker",
                                attempt=int(ctx.get("job_try") or 0),
                                hint=(
                                    "Retry the deployment. If this repeats, inspect "
                                    "the jobs worker health and resource limits."
                                ),
                            )
                            await DeploymentDiagnosticService.record(
                                db,
                                deployment_id,
                                level="ERROR",
                                source="worker",
                                stage=failure_stage,
                                code="worker_interrupted",
                                message=message,
                                details={"job_id": ctx.get("job_id")},
                                attempt=int(ctx.get("job_try") or 0),
                            )
                            values["conclusion"] = "failed"
                            values["error"] = payload
                        await DeploymentService.update_status(
                            db,
                            current_deployment,
                            **values,
                        )
            except Exception as e:
                logger.error(f"{log_prefix} Error updating deployment status: {e}")

        elif deployment:
            try:
                await _cleanup_startup_resources(
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                )
            except Exception:
                logger.warning(
                    "%s Could not remove canceled startup resources.",
                    log_prefix,
                    exc_info=True,
                )

    except Exception as e:
        if deployment:
            try:
                await _cleanup_startup_resources(
                    deployment=deployment,
                    container=container,
                    image_reference=managed_image_reference,
                    settings=settings,
                    loki=loki,
                )
            except Exception:
                logger.warning(
                    "%s Could not remove failed startup resources.",
                    log_prefix,
                    exc_info=True,
                )
        queue: ArqRedis = ctx["redis"]
        await DeploymentJobs.enqueue_failure(
            queue,
            deployment_id,
            failure_stage,
            "Deployment failed unexpectedly: "
            f"{DeploymentDiagnosticService.exception_message(e)}",
            code="startup_exception",
            source="worker",
            details={"exception_type": e.__class__.__name__},
            hint="Review the control-plane diagnostics and retry the deployment.",
        )
        logger.info(f"{log_prefix} Deployment startup failed.", exc_info=True)
    finally:
        if loki:
            await loki.client.aclose()


@deployment_heartbeat("finalize")
async def finalize_deployment(ctx, deployment_id: str):
    """Finalizes a deployment, setting up aliases and updating Traefik config."""
    settings = get_settings()
    redis_client = get_redis_client()
    service = DeploymentService()
    log_prefix = f"[DeployFinalize:{deployment_id}]"
    logger.info(f"{log_prefix} Finalizing deployment")

    queue: ArqRedis | None = ctx.get("redis") if isinstance(ctx, dict) else None

    async with AsyncSessionLocal() as db:
        deployment = None
        try:
            deployment = (
                await db.execute(
                    select(Deployment)
                    .options(joinedload(Deployment.project))
                    .where(Deployment.id == deployment_id)
                )
            ).scalar_one()

            lock = service.environment_lock(
                redis_client,
                deployment.project_id,
                deployment.environment_id,
            )
            async with lock:
                await db.refresh(
                    deployment,
                    attribute_names=["status", "conclusion", "error"],
                )
                if deployment.conclusion:
                    logger.info(
                        "%s Deployment already concluded (%s); skipping finalize.",
                        log_prefix,
                        deployment.conclusion,
                    )
                    return

                newer_is_current = (
                    await service.has_newer_successful_deployment(
                        deployment, db
                    )
                )
                if not newer_is_current:
                    await service.setup_aliases(deployment, db, settings)
                    await db.commit()

                    # Update Traefik dynamic config
                    try:
                        await service.update_traefik_config(
                            deployment.project,
                            db,
                            settings,
                            include_deployment_ids={deployment.id},
                        )
                    except Exception as e:
                        logger.error(
                            f"{log_prefix} Failed to update Traefik config: {e}"
                        )
                        await DeploymentDiagnosticService.record_external(
                            deployment.id,
                            level="ERROR",
                            source="routing",
                            stage="finalize",
                            code="routing_config_failed",
                            message=(
                                "The deployment started, but its environment routing "
                                "configuration could not be regenerated."
                            ),
                            details={"error": e.__class__.__name__},
                            attempt=int(ctx.get("job_try") or 0),
                        )
                else:
                    logger.info(
                        "%s Newer successful deployment already owns routing; "
                        "leaving aliases unchanged.",
                        log_prefix,
                    )

                await service.update_status(
                    db,
                    deployment,
                    status="completed",
                    conclusion="succeeded",
                    error=None,
                    redis_client=redis_client,
                )

            # Cleanup inactive deployments
            queue: ArqRedis = ctx["redis"]
            await queue.enqueue_job(
                "cleanup_inactive_containers", deployment.project_id
            )
            logger.info(
                f"{log_prefix} Inactive deployments cleanup job queued for project {deployment.project_id}."
            )

        except Exception:
            logger.error(f"{log_prefix} Error finalizing deployment.", exc_info=True)
            if queue:
                await DeploymentJobs.enqueue_failure(
                    queue,
                    deployment_id,
                    "finalize",
                    "Failed to finalize deployment (aliases/routing). The app may still be running.",
                    code="finalize_failed",
                    source="worker",
                    hint="Retry the deployment if the environment route does not recover.",
                )


@deployment_heartbeat("fail")
async def fail_deployment(
    ctx,
    deployment_id: str,
    status: str,
    reason: str | None = None,
    code: str = "deployment_failed",
    source: str = "worker",
    details: dict | None = None,
    hint: str | None = None,
):
    """Handles a failed deployment, cleaning up resources."""
    log_prefix = f"[DeployFail:{deployment_id}]"
    logger.info(f"{log_prefix} Handling failed deployment. Reason: {reason}")
    settings = get_settings()
    redis_client = get_redis_client()
    service = DeploymentService()

    async with AsyncSessionLocal() as db:
        deployment = (
            await db.execute(
                select(Deployment)
                .options(joinedload(Deployment.project))
                .options(joinedload(Deployment.node))
                .where(Deployment.id == deployment_id)
            )
        ).scalar_one()

        if deployment.conclusion == "canceled":
            logger.info(
                "%s Deployment already canceled; skipping fail handler.", log_prefix
            )
            return
        if deployment.conclusion:
            logger.info(
                "%s Deployment already concluded (%s); skipping fail handler.",
                log_prefix,
                deployment.conclusion,
            )
            return

        safe_reason = DeploymentDiagnosticService.sanitize(
            reason or "Deployment failed", 2_000
        )
        attempt = int(ctx.get("job_try") or deployment.worker_attempt or 0)
        try:
            await DeploymentDiagnosticService.record(
                db,
                deployment.id,
                level="ERROR",
                source=source,
                stage=status,
                code=code,
                message=safe_reason,
                details=details,
                attempt=attempt,
            )
        except Exception:
            logger.warning(
                "%s Could not persist structured diagnostics.",
                log_prefix,
                exc_info=True,
            )

        await service.update_status(
            db,
            deployment,
            status="fail",
            redis_client=redis_client,
        )

        if deployment.container_id and deployment.container_status not in (
            "removed",
            "stopped",
        ):
            try:
                async with deployment_runtime_client(
                    deployment, settings
                ) as docker_client:
                    container = await docker_client.containers.get(
                        deployment.container_id
                    )
                    loki = LokiService()
                    try:
                        preserved = await loki.preserve_container_logs(
                            container, deployment
                        )
                        if preserved:
                            logger.info(
                                "%s Preserved %s final container log lines.",
                                log_prefix,
                                preserved,
                            )
                    finally:
                        await loki.client.aclose()
                    try:
                        await container.stop()
                    except Exception:
                        pass
                    queue: ArqRedis = ctx["redis"]
                    await queue.enqueue_job(
                        "delete_container",
                        deployment.id,
                        _defer_by=settings.container_delete_grace_seconds,
                    )
                    logger.info(
                        f"{log_prefix} Cleaned up failed container {deployment.container_id}"
                    )
                    await service.update_status(
                        db,
                        deployment,
                        container_status="stopped",
                        emit=False,
                    )

            except aiodocker.DockerError as error:
                if error.status == 404:
                    logger.warning(
                        f"{log_prefix} Container {deployment.container_id} not found, already removed"
                    )
                    await service.update_status(
                        db,
                        deployment,
                        container_status="removed",
                        emit=False,
                    )
                else:
                    logger.error(
                        f"{log_prefix} Docker error cleaning up container {deployment.container_id}: {error}",
                        exc_info=True,
                    )
            except Exception:
                logger.warning(
                    f"{log_prefix} Could not cleanup container {deployment.container_id}.",
                    exc_info=True,
                )

        if not deployment.container_id or deployment.container_status == "removed":
            try:
                async with deployment_runtime_client(
                    deployment, settings
                ) as docker_client:
                    removed = await remove_managed_deployment_image(
                        docker_client, deployment.image
                    )
                    if removed:
                        logger.info("%s Removed failed build image.", log_prefix)
            except Exception:
                logger.warning(
                    "%s Could not remove failed build image.",
                    log_prefix,
                    exc_info=True,
                )

        await service.update_status(
            db,
            deployment,
            status="completed",
            conclusion="failed",
            error=DeploymentDiagnosticService.failure_payload(
                stage=status,
                code=code,
                message=safe_reason,
                source=source,
                attempt=attempt,
                hint=hint,
                details=details,
            ),
            redis_client=redis_client,
        )
        logger.error(f"{log_prefix} Deployment failed and cleaned up.")


async def delete_container(ctx, deployment_id: str):
    """Delete a deployment container after a grace period."""
    log_prefix = f"[DeleteContainer:{deployment_id}]"
    logger.info(f"{log_prefix} Deleting container")
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        deployment = (
            await db.execute(
                select(Deployment)
                .options(joinedload(Deployment.node))
                .where(Deployment.id == deployment_id)
            )
        ).scalar_one_or_none()
        if not deployment:
            logger.warning(f"{log_prefix} Deployment not found")
            return

        try:
            async with deployment_runtime_client(
                deployment, settings
            ) as docker_client:
                if deployment.container_id:
                    try:
                        container = await docker_client.containers.get(
                            deployment.container_id
                        )
                        try:
                            await container.stop()
                        except Exception:
                            pass
                        await container.delete(force=True)
                        deployment.container_status = "removed"
                        deployment.runtime_url = None
                    except aiodocker.DockerError as error:
                        if error.status == 404:
                            deployment.container_status = "removed"
                            deployment.runtime_url = None
                        else:
                            raise

                removed_image = await remove_managed_deployment_image(
                    docker_client, deployment.image
                )
                if removed_image:
                    logger.info(f"{log_prefix} Removed deployment image")
                await db.commit()
                queue: ArqRedis = ctx["redis"]
                try:
                    await queue.enqueue_job(
                        "prune_dependency_cache", deployment.project_id
                    )
                except Exception:
                    logger.warning(
                        "%s Could not enqueue dependency-cache pruning.",
                        log_prefix,
                        exc_info=True,
                    )
        except Exception:
            logger.error(
                f"[DeleteContainer:{deployment_id}] Error deleting container.",
                exc_info=True,
            )


async def cleanup_inactive_containers(
    ctx, project_id: str, remove_containers: bool = True
):
    """Stop/remove containers for deployments no longer referenced by aliases."""
    settings = get_settings()

    async with AsyncSessionLocal() as db:
        try:
            project = await db.get(Project, project_id)
            if not project:
                logger.warning(
                    "[CleanupInactiveContainers:%s] Project not found", project_id
                )
                return
            if project.status == "deleted":
                logger.info(
                    "[CleanupInactiveContainers:%s] Project deleted, skipping",
                    project_id,
                )
                return

            active_result = await db.execute(
                select(Alias.deployment_id)
                .join(Deployment, Alias.deployment_id == Deployment.id)
                .where(
                    Deployment.project_id == project_id,
                    Alias.deployment_id.isnot(None),
                )
                .union(
                    select(Alias.previous_deployment_id)
                    .join(
                        Deployment,
                        Alias.previous_deployment_id == Deployment.id,
                    )
                    .where(
                        Deployment.project_id == project_id,
                        Alias.previous_deployment_id.isnot(None),
                    )
                )
            )
            active_deployment_ids = set(active_result.scalars().all())
            inactive_result = await db.execute(
                select(Deployment)
                .options(joinedload(Deployment.node))
                .where(
                    Deployment.project_id == project_id,
                    Deployment.container_id.isnot(None),
                    Deployment.container_status == "running",
                    Deployment.status == "completed",
                    Deployment.id.notin_(active_deployment_ids)
                    if active_deployment_ids
                    else true(),
                )
            )
            inactive_deployments = list(inactive_result.scalars().all())
            stopped_count = 0
            removed_count = 0

            for deployment in inactive_deployments:
                try:
                    async with deployment_runtime_client(
                        deployment, settings
                    ) as docker_client:
                        container = await docker_client.containers.get(
                            deployment.container_id
                        )
                        await container.stop()
                        deployment.container_status = "stopped"
                        stopped_count += 1
                        if remove_containers:
                            await container.delete()
                            deployment.container_status = "removed"
                            deployment.runtime_url = None
                            removed_count += 1
                            await remove_managed_deployment_image(
                                docker_client, deployment.image
                            )
                except aiodocker.DockerError as error:
                    if error.status == 404:
                        deployment.container_status = "removed"
                        deployment.runtime_url = None
                    else:
                        logger.error(
                            "[CleanupInactiveContainers:%s] Docker error for %s: %s",
                            project_id,
                            deployment.id,
                            error,
                        )
                except Exception as error:
                    logger.error(
                        "[CleanupInactiveContainers:%s] Node cleanup failed for %s: %s",
                        project_id,
                        deployment.id,
                        error,
                    )

            if inactive_deployments:
                await db.commit()
            if removed_count:
                await DeploymentService().update_traefik_config(
                    project,
                    db,
                    settings,
                )
            logger.info(
                "[CleanupInactiveContainers:%s] Stopped: %s, Removed: %s",
                project_id,
                stopped_count,
                removed_count,
            )

            queue: ArqRedis = ctx["redis"]
            try:
                await queue.enqueue_job("prune_dependency_cache", project_id)
            except Exception:
                logger.warning(
                    "[CleanupInactiveContainers:%s] Could not enqueue dependency-cache pruning.",
                    project_id,
                    exc_info=True,
                )
        except Exception as error:
            logger.error(
                "[CleanupInactiveContainers:%s] Task failed: %s",
                project_id,
                error,
            )
            await db.rollback()
            raise
