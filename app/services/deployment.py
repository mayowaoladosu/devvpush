import os
import re
import tempfile
import yaml
import aiodocker
import logging
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from redis.asyncio import Redis
from arq.connections import ArqRedis
from arq.jobs import Job, JobStatus

from models import Deployment, Alias, Project, User, Domain
from utils.environment import get_environment_for_branch
from config import Settings, get_settings
from services.audit import AuditService
from services.deployment_policy import (
    DeploymentPolicyError,
    DeploymentPolicyService,
)
from services.registry import RegistryService
from services.deployment_diagnostics import DeploymentDiagnosticService
from services.deployment_nodes import DeploymentNodeService
from services.notifications import NotificationService
from services.node_runtime import deployment_runtime_client
from services.storage import RuntimeStorage, StorageService

logger = logging.getLogger(__name__)


class DeploymentService:
    def __init__(self):
        pass

    @staticmethod
    def uses_dockerfile(config: dict | None) -> bool:
        return (config or {}).get("build_strategy") == "dockerfile"

    @classmethod
    def requires_runner(cls, config: dict | None) -> bool:
        return not cls.uses_dockerfile(config)

    @staticmethod
    def schedule_lock_key(project_id: str, environment_id: str) -> str:
        return f"lock:deployment-schedule:{project_id}:{environment_id}"

    @classmethod
    def environment_lock(
        cls, redis_client: Redis, project_id: str, environment_id: str
    ):
        settings = get_settings()
        return redis_client.lock(
            cls.schedule_lock_key(project_id, environment_id),
            timeout=settings.deployment_schedule_lock_seconds,
            blocking_timeout=settings.deployment_schedule_wait_seconds,
        )

    @staticmethod
    async def update_status(
        db: AsyncSession,
        deployment: Deployment,
        *,
        status: str | None = None,
        conclusion: str | None = None,
        error: dict | None = None,
        container_status: str | None = None,
        redis_client: Redis | None = None,
        emit: bool = True,
    ) -> bool:
        await db.refresh(
            deployment,
            attribute_names=["status", "conclusion", "error", "container_status"],
            with_for_update=True,
        )
        now = datetime.now(timezone.utc)
        existing_conclusion = deployment.conclusion
        applied_status = status
        applied_conclusion = conclusion
        status_for_event = status

        if existing_conclusion:
            if conclusion and conclusion != existing_conclusion:
                logger.info(
                    "Preserving terminal conclusion %s for deployment %s; "
                    "ignoring transition to %s.",
                    existing_conclusion,
                    deployment.id,
                    conclusion,
                )
            applied_conclusion = None
            status_for_event = None
            if status not in {None, "completed"}:
                applied_status = None
            if container_status == "running":
                container_status = None
            if error is not None:
                error = None

        if applied_status is not None:
            deployment.status = applied_status
        if applied_conclusion is not None:
            deployment.conclusion = applied_conclusion
            deployment.concluded_at = now.replace(tzinfo=None)
            deployment.worker_job_id = None
            deployment.worker_phase = None
            deployment.worker_heartbeat_at = None
            if deployment.project:
                deployment.project.updated_at = now.replace(tzinfo=None)
        if error is not None:
            deployment.error = error
        if container_status is not None:
            deployment.container_status = container_status

        await db.commit()

        if emit and redis_client and (status_for_event or applied_conclusion):
            status_value = (
                applied_conclusion if applied_conclusion else status_for_event
            )
            fields = {
                "event_type": "deployment_status_update",
                "project_id": deployment.project_id,
                "deployment_id": deployment.id,
                "deployment_status": status_value,
                "timestamp": now.isoformat(),
            }
            try:
                await redis_client.xadd(
                    f"stream:project:{deployment.project_id}:deployment:{deployment.id}:status",
                    fields,
                )
                await redis_client.xadd(
                    f"stream:project:{deployment.project_id}:updates", fields
                )
            except Exception:
                logger.warning(
                    "Could not emit status update for deployment %s.",
                    deployment.id,
                    exc_info=True,
                )

        return bool(applied_status or applied_conclusion or container_status)

    def get_alias_domains(
        self, deployment: Deployment, settings: Settings
    ) -> dict[str, str]:
        project = deployment.project
        values: dict[str, str] = {}

        if deployment.branch:
            sanitized_branch = re.sub(r"[^a-zA-Z0-9-]", "-", deployment.branch).lower()
            if sanitized_branch:
                branch_subdomain = f"{project.slug}-branch-{sanitized_branch}"
                values["branch_subdomain"] = branch_subdomain
                values["branch_domain"] = f"{branch_subdomain}.{settings.deploy_domain}"
                values["branch_url"] = (
                    f"{settings.url_scheme}://{values['branch_domain']}"
                )

        env_subdomain = None
        if deployment.environment_id == "prod":
            env_subdomain = project.slug
        else:
            environment = project.get_environment_by_id(deployment.environment_id)
            if environment:
                env_subdomain = f"{project.slug}-env-{environment.get('slug')}"
            else:
                logger.warning(
                    "Environment %s not found for deployment %s",
                    deployment.environment_id,
                    deployment.id,
                )

        env_id_subdomain = f"{project.slug}-env-id-{deployment.environment_id}"

        values["environment_id_subdomain"] = env_id_subdomain
        values["environment_id_domain"] = f"{env_id_subdomain}.{settings.deploy_domain}"
        values["environment_id_url"] = (
            f"{settings.url_scheme}://{values['environment_id_domain']}"
        )

        if env_subdomain:
            values["environment_subdomain"] = env_subdomain
            values["environment_domain"] = f"{env_subdomain}.{settings.deploy_domain}"
            values["environment_url"] = (
                f"{settings.url_scheme}://{values['environment_domain']}"
            )

        return values

    def get_runtime_env_vars(
        self, deployment: Deployment, settings: Settings
    ) -> dict[str, str]:
        """Build runner environment variables for a deployment."""
        env_vars = {var["key"]: var["value"] for var in (deployment.env_vars or [])}
        project = deployment.project
        environment = deployment.environment or {}

        layerrail_vars: dict[str, str] = {
            "LAYERRAIL": "true",
            "PORT": "8000",
            "HOST": "0.0.0.0",
            "HOSTNAME": "0.0.0.0",
            "LAYERRAIL_URL": deployment.url,
            "LAYERRAIL_DOMAIN": deployment.hostname,
            "LAYERRAIL_TEAM_ID": project.team_id,
            "LAYERRAIL_PROJECT_ID": project.id,
            "LAYERRAIL_ENVIRONMENT": environment.get("slug")
            or deployment.environment_id,
            "LAYERRAIL_DEPLOYMENT_ID": deployment.id,
            "LAYERRAIL_DEPLOYMENT_CREATED_AT": deployment.created_at.isoformat()
            + "Z",
            "LAYERRAIL_GIT_PROVIDER": "github",
            "LAYERRAIL_GIT_REPO": deployment.repo_full_name,
            "LAYERRAIL_GIT_REF": deployment.branch,
            "LAYERRAIL_GIT_COMMIT_SHA": deployment.commit_sha,
            "PUID": str(settings.service_uid),
            "PGID": str(settings.service_gid),
        }

        if settings.server_ip:
            layerrail_vars["LAYERRAIL_IP"] = settings.server_ip

        alias_domains = self.get_alias_domains(deployment, settings)

        if alias_domains.get("environment_domain"):
            layerrail_vars["LAYERRAIL_DOMAIN_ENVIRONMENT"] = alias_domains[
                "environment_domain"
            ]
        if alias_domains.get("environment_url"):
            layerrail_vars["LAYERRAIL_URL_ENVIRONMENT"] = alias_domains[
                "environment_url"
            ]
        if alias_domains.get("branch_domain"):
            layerrail_vars["LAYERRAIL_DOMAIN_BRANCH"] = alias_domains[
                "branch_domain"
            ]
        if alias_domains.get("branch_url"):
            layerrail_vars["LAYERRAIL_URL_BRANCH"] = alias_domains["branch_url"]

        if deployment.commit_meta:
            author = deployment.commit_meta.get("author")
            message = deployment.commit_meta.get("message")
            if author:
                layerrail_vars["LAYERRAIL_GIT_COMMIT_AUTHOR"] = author
            if message:
                layerrail_vars["LAYERRAIL_GIT_COMMIT_MESSAGE"] = message

        if deployment.repo_full_name and "/" in deployment.repo_full_name:
            owner, repo = deployment.repo_full_name.split("/", 1)
            layerrail_vars["LAYERRAIL_GIT_REPO_OWNER"] = owner
            layerrail_vars["LAYERRAIL_GIT_REPO_NAME"] = repo

        runtime_vars = dict(layerrail_vars)
        for key, value in layerrail_vars.items():
            if key == "LAYERRAIL":
                runtime_vars["DEVPUSH"] = value
            elif key.startswith("LAYERRAIL_"):
                runtime_vars["DEVPUSH_" + key.removeprefix("LAYERRAIL_")] = value

        for key, value in runtime_vars.items():
            if value is not None and value != "":
                env_vars.setdefault(key, str(value))

        return env_vars

    async def get_runtime_mounts(
        self, deployment: Deployment, db: AsyncSession, settings: Settings
    ) -> list[str]:
        """Build container bind mounts for storage resources."""
        runtime = await self.get_runtime_storage(deployment, db, settings)
        return runtime.binds

    async def get_runtime_storage(
        self,
        deployment: Deployment,
        db: AsyncSession,
        settings: Settings,
        *,
        lock: bool = False,
    ) -> RuntimeStorage:
        return await StorageService(settings).runtime(
            deployment,
            db,
            lock=lock,
        )

    async def setup_aliases(
        self, deployment: Deployment, db: AsyncSession, settings: Settings
    ) -> None:
        alias_domains = self.get_alias_domains(deployment, settings)
        branch_subdomain = alias_domains.get("branch_subdomain")
        env_subdomain = alias_domains.get("environment_subdomain")
        env_id_subdomain = alias_domains.get("environment_id_subdomain")

        if branch_subdomain:
            try:
                await Alias.update_or_create(
                    db,
                    subdomain=branch_subdomain,
                    deployment_id=deployment.id,
                    type="branch",
                    value=deployment.branch,
                )
            except Exception as exc:
                logger.warning("Failed to setup branch alias: %s", exc)

        if env_subdomain:
            try:
                await Alias.update_or_create(
                    db,
                    subdomain=env_subdomain,
                    deployment_id=deployment.id,
                    type="environment",
                    value=deployment.environment_id,
                    environment_id=deployment.environment_id,
                )
            except Exception as exc:
                logger.error("Failed to setup environment alias: %s", exc)

        if env_id_subdomain:
            try:
                await Alias.update_or_create(
                    db,
                    subdomain=env_id_subdomain,
                    deployment_id=deployment.id,
                    type="environment_id",
                    value=deployment.environment_id,
                    environment_id=deployment.environment_id,
                )
            except Exception as exc:
                logger.error("Failed to setup environment id alias: %s", exc)

    async def update_traefik_config(
        self,
        project: Project,
        db: AsyncSession,
        settings: Settings,
        *,
        include_deployment_ids: set[str] | None = None,
    ) -> None:
        """Update Traefik config for a project including domains."""
        path = os.path.join(settings.traefik_dir, f"project_{project.id}.yml")

        # Get aliases
        include_ids = include_deployment_ids or set()
        if include_ids:
            where_clause = or_(
                Deployment.conclusion == "succeeded",
                Deployment.id.in_(list(include_ids)),
            )
        else:
            where_clause = Deployment.conclusion == "succeeded"

        result = await db.execute(
            select(Alias)
            .options(joinedload(Alias.deployment).joinedload(Deployment.node))
            .join(Deployment, Alias.deployment_id == Deployment.id)
            .filter(
                Deployment.project_id == project.id,
                where_clause,
            )
        )
        aliases = result.scalars().all()

        # Get active domains
        domains_result = await db.execute(
            select(Domain).where(
                Domain.project_id == project.id, Domain.status == "active"
            )
        )
        domains = domains_result.scalars().all()

        remote_where = [
            Deployment.project_id == project.id,
            Deployment.node_id.isnot(None),
            Deployment.runtime_url.isnot(None),
            Deployment.container_status == "running",
        ]
        if include_ids:
            remote_where.append(
                or_(
                    Deployment.conclusion == "succeeded",
                    Deployment.id.in_(list(include_ids)),
                )
            )
        else:
            remote_where.append(Deployment.conclusion == "succeeded")
        remote_result = await db.execute(
            select(Deployment)
            .options(joinedload(Deployment.node))
            .where(*remote_where)
        )
        remote_deployments = list(remote_result.scalars().all())

        # Remove config if no aliases or domains
        if not aliases and not domains and not remote_deployments and os.path.exists(path):
            os.remove(path)
            return

        routers = {}
        services = {}
        middlewares = {}

        def service_for(deployment: Deployment) -> str:
            if not deployment.node_id:
                return f"deployment-{deployment.id}@docker"
            if not deployment.node:
                raise ValueError("Remote deployment node is unavailable.")
            service_name = f"deployment-{deployment.id}"
            runtime_url = DeploymentNodeService.validate_runtime_url(
                deployment.node, deployment.runtime_url
            )
            services[service_name] = {
                "loadBalancer": {"servers": [{"url": runtime_url}]}
            }
            return service_name

        for remote_deployment in remote_deployments:
            service_name = service_for(remote_deployment)
            router_config = {
                "rule": f"Host(`{remote_deployment.hostname}`)",
                "service": service_name,
                "priority": 10,
                "entryPoints": ["web", "websecure"]
                if settings.url_scheme == "https"
                else ["web"],
            }
            if settings.url_scheme == "https":
                router_config["tls"] = {"certResolver": "le"}
            routers[f"router-deployment-{remote_deployment.id}"] = router_config

        # Aliases
        for a in aliases:
            router_config = {
                "rule": f"Host(`{a.subdomain}.{settings.deploy_domain}`)",
                "service": service_for(a.deployment),
                "entryPoints": ["web", "websecure"]
                if settings.url_scheme == "https"
                else ["web"],
            }
            if settings.url_scheme == "https":
                router_config["tls"] = {"certResolver": "le"}
            routers[f"router-alias-{a.id}"] = router_config

        # Domains
        for domain in domains:
            env_alias = next(
                (
                    a
                    for a in aliases
                    if a.type == "environment_id" and a.value == domain.environment_id
                ),
                None,
            )

            if not env_alias:
                continue

            if domain.type == "route":
                router_config = {
                    "rule": f"Host(`{domain.hostname}`)",
                    "service": service_for(env_alias.deployment),
                    "entryPoints": ["web", "websecure"]
                    if settings.url_scheme == "https"
                    else ["web"],
                }
                if settings.url_scheme == "https":
                    # Force HTTP-0.1 ACME challenge
                    router_config["tls"] = {"certResolver": "lehttp"}
                routers[f"router-domain-{domain.id}"] = router_config

            elif domain.type in ["301", "302", "307", "308"]:
                middleware_name = f"redirect-{domain.id}"

                router_cfg = {
                    "rule": f"Host(`{domain.hostname}`)",
                    "service": "noop@internal",
                    "middlewares": [middleware_name],
                    "entryPoints": ["web", "websecure"]
                    if settings.url_scheme == "https"
                    else ["web"],
                }
                if settings.url_scheme == "https":
                    # Force HTTP-0.1 ACME challenge
                    router_cfg["tls"] = {"certResolver": "lehttp"}
                routers[f"router-redirect-{domain.id}"] = router_cfg

                middlewares[middleware_name] = {
                    "redirectRegex": {
                        "regex": f"^https?://{domain.hostname}/(.*)",
                        "replacement": f"https://{env_alias.subdomain}.{settings.deploy_domain}/$1",
                        "permanent": domain.type in ["301", "308"],
                    }
                }

        # If there is nothing to configure, remove any stale config file.
        if not routers and not services and not middlewares:
            if os.path.exists(path):
                os.remove(path)
            return

        # Write config
        os.makedirs(settings.traefik_dir, exist_ok=True)
        config = {"http": {"routers": routers}}
        if services:
            config["http"]["services"] = services
        if middlewares:
            config["http"]["middlewares"] = middlewares

        # Write atomically so Traefik's file watcher never reads a partially-written YAML.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.", dir=settings.traefik_dir
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(config, f, sort_keys=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    async def create(
        self,
        project: Project,
        branch: str,
        commit: dict,
        db: AsyncSession,
        redis_client: Redis,
        trigger: str = "user",
        current_user: User | None = None,
    ) -> Deployment:
        """Create a new deployment."""

        environment = get_environment_for_branch(branch, project.active_environments)
        if not environment:
            raise ValueError("No environment found for this branch.")

        config = project.config or {}
        runner_image = None
        if self.requires_runner(config):
            runner_slug = config.get("runner") or config.get("image")
            if not runner_slug:
                raise ValueError("Runner not set in project config.")
            registry_state = RegistryService(
                Path(get_settings().data_dir) / "registry"
            ).state
            runner_entry = next(
                (
                    runner
                    for runner in registry_state.runners
                    if runner.get("slug") == runner_slug
                ),
                None,
            )
            if not runner_entry:
                raise ValueError(f"Runner '{runner_slug}' not found in registry.")
            if runner_entry.get("enabled") is not True:
                raise ValueError(f"Runner '{runner_slug}' is disabled.")
            runner_image = runner_entry.get("image")
            if not runner_image:
                raise ValueError(f"Runner '{runner_slug}' has no image configured.")

        commit_user_author = commit.get("author") or {}
        commit_user_committer = commit.get("committer") or {}
        commit_payload = commit.get("commit") or {}
        commit_payload_author = commit_payload.get("author") or {}
        commit_payload_committer = commit_payload.get("committer") or {}

        author = (
            commit_user_author.get("login")
            or commit_user_committer.get("login")
            or commit_payload_author.get("name")
            or commit_payload_committer.get("name")
            or ""
        )
        message = commit_payload.get("message") or ""
        date_raw = (
            commit_payload_author.get("date")
            or commit_payload_committer.get("date")
            or datetime.now(timezone.utc).isoformat()
        )
        date = datetime.fromisoformat(date_raw.replace("Z", "+00:00")).isoformat()

        commit_meta = {
            "author": author,
            "message": message,
            "date": date,
        }
        provider_event_id = str(commit.get("provider_event_id") or "").strip()
        if provider_event_id:
            commit_meta["provider_event_id"] = provider_event_id[:255]

        node = await DeploymentNodeService(get_settings()).select_node(
            db,
            project,
            environment.get("id", ""),
        )

        deployment = Deployment(
            project=project,
            environment_id=environment.get("id", ""),
            branch=branch,
            commit_sha=commit["sha"],
            commit_meta=commit_meta,
            image=runner_image,
            trigger=trigger,
            node=node,
            created_by_user_id=current_user.id
            if trigger in {"user", "api"} and current_user
            else None,
        )
        db.add(deployment)
        await db.commit()

        try:
            await redis_client.xadd(
                f"stream:project:{project.id}:updates",
                fields={
                    "event_type": "deployment_creation",
                    "project_id": project.id,
                    "deployment_id": deployment.id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning(
                "Could not emit creation event for deployment %s.",
                deployment.id,
                exc_info=True,
            )

        logger.info(
            f"Deployment {deployment.id} created for "
            f"project {project.name} ({project.id}) to environment {environment.get('name')} ({environment.get('id')}) "
            f"on {'node ' + node.name if node else 'the primary node'}"
        )

        return deployment

    async def schedule(
        self,
        project: Project,
        branch: str,
        commit: dict,
        db: AsyncSession,
        redis_client: Redis,
        queue: ArqRedis,
        trigger: str = "user",
        current_user: User | None = None,
    ) -> Deployment:
        """Create and queue one deployment under an environment-scoped lock."""
        environment = get_environment_for_branch(branch, project.active_environments)
        if not environment:
            raise ValueError("No environment found for this branch.")

        lock = self.environment_lock(redis_client, project.id, environment["id"])
        superseded: list[Deployment] = []
        policy = DeploymentPolicyService.from_project(project)
        has_explicit_policy = DeploymentPolicyService.KEY in (
            getattr(project, "config", None) or {}
        )

        async with lock:
            provider_event_id = str(commit.get("provider_event_id") or "").strip()
            if trigger == "webhook" and provider_event_id:
                existing = await self._find_webhook_deployment(
                    project_id=project.id,
                    environment_id=environment["id"],
                    branch=branch,
                    provider_event_id=provider_event_id,
                    db=db,
                )
                if existing:
                    await self._ensure_start_job(existing, db, queue)
                    logger.info(
                        "Ignoring duplicate webhook deployment for %s at %s (%s)",
                        project.id,
                        provider_event_id,
                        existing.id,
                    )
                    return existing

            if has_explicit_policy:
                active_result = await db.execute(
                    select(Deployment).where(
                        Deployment.project_id == project.id,
                        Deployment.environment_id == environment["id"],
                        Deployment.conclusion.is_(None),
                        Deployment.status.in_(["prepare", "deploy", "finalize"]),
                    )
                )
                active = list(active_result.scalars())
                if trigger == "webhook" and policy.supersede_older:
                    active = [
                        item
                        for item in active
                        if not (
                            item.trigger == "webhook"
                            and item.branch == branch
                            and item.status in {"prepare", "deploy"}
                        )
                    ]
                if len(active) >= policy.max_concurrent:
                    raise DeploymentPolicyError(
                        "This environment has reached its concurrent deployment limit."
                    )

            deployment = await self.create(
                project=project,
                branch=branch,
                commit=commit,
                db=db,
                redis_client=redis_client,
                trigger=trigger,
                current_user=current_user,
            )

            try:
                await self._ensure_start_job(deployment, db, queue)
            except Exception:
                message = "Deployment could not be added to the build queue."
                try:
                    await DeploymentDiagnosticService.record(
                        db,
                        deployment.id,
                        level="ERROR",
                        source="queue",
                        stage="prepare",
                        code="queue_admission_failed",
                        message=message,
                    )
                except Exception:
                    logger.warning(
                        "Could not persist queue failure diagnostic for %s.",
                        deployment.id,
                        exc_info=True,
                    )
                await self.update_status(
                    db,
                    deployment,
                    status="completed",
                    conclusion="failed",
                    error=DeploymentDiagnosticService.failure_payload(
                        stage="prepare",
                        code="queue_admission_failed",
                        message=message,
                        source="queue",
                        hint="Retry after confirming the jobs worker and Redis are healthy.",
                    ),
                    redis_client=redis_client,
                )
                raise

            team_id = getattr(project, "team_id", None)
            if team_id:
                await AuditService.record(
                    db,
                    team_id=team_id,
                    user=current_user,
                    action="deployment.created",
                    resource_type="deployment",
                    resource_id=deployment.id,
                    metadata={
                        "project_id": project.id,
                        "branch": branch,
                        "commit_sha": deployment.commit_sha,
                        "trigger": trigger,
                    },
                )

            if trigger == "webhook" and policy.supersede_older:
                superseded = await self._mark_superseded_webhook_deployments(
                    replacement=deployment,
                    db=db,
                    redis_client=redis_client,
                    queue=queue,
                )

        for outdated in superseded:
            await self._abort_job(outdated, queue)
            await self._stop_container(outdated, db)
            try:
                team_id = getattr(project, "team_id", None)
                if not team_id:
                    continue
                await NotificationService.emit(
                    db,
                    queue,
                    team_id=team_id,
                    event="deployment.skipped",
                    payload=NotificationService.deployment_payload(
                        outdated,
                        project=project,
                    ),
                )
            except Exception:
                logger.warning(
                    "Could not queue skipped notification for deployment %s.",
                    outdated.id,
                    exc_info=True,
                )

        try:
            team_id = getattr(project, "team_id", None)
            if not team_id:
                return deployment
            await NotificationService.emit(
                db,
                queue,
                team_id=team_id,
                event="deployment.created",
                payload=NotificationService.deployment_payload(
                    deployment,
                    project=project,
                ),
            )
        except Exception:
            logger.warning(
                "Could not queue creation notification for deployment %s.",
                deployment.id,
                exc_info=True,
            )

        return deployment

    @staticmethod
    async def _ensure_start_job(
        deployment: Deployment, db: AsyncSession, queue: ArqRedis
    ) -> None:
        if deployment.conclusion:
            return
        if not deployment.job_id:
            deployment.job_id = deployment.id
            await db.commit()

        job = await queue.enqueue_job(
            "start_deployment",
            deployment.id,
            _job_id=deployment.job_id,
        )
        if job is not None:
            return

        status = await Job(job_id=deployment.job_id, redis=queue).status()
        if status == JobStatus.not_found:
            raise RuntimeError("Deployment queue rejected the job.")

    @staticmethod
    async def has_newer_successful_deployment(
        deployment: Deployment, db: AsyncSession
    ) -> bool:
        if deployment.trigger != "webhook":
            return False
        result = await db.execute(
            select(Deployment.id)
            .where(
                Deployment.project_id == deployment.project_id,
                Deployment.environment_id == deployment.environment_id,
                Deployment.branch == deployment.branch,
                Deployment.created_at > deployment.created_at,
                Deployment.conclusion == "succeeded",
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def _find_webhook_deployment(
        *,
        project_id: str,
        environment_id: str,
        branch: str,
        provider_event_id: str,
        db: AsyncSession,
    ) -> Deployment | None:
        result = await db.execute(
            select(Deployment)
            .where(
                Deployment.project_id == project_id,
                Deployment.environment_id == environment_id,
                Deployment.branch == branch,
                Deployment.trigger == "webhook",
                Deployment.commit_meta["provider_event_id"].as_string()
                == provider_event_id,
            )
            .order_by(Deployment.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _mark_superseded_webhook_deployments(
        self,
        *,
        replacement: Deployment,
        db: AsyncSession,
        redis_client: Redis,
        queue: ArqRedis,
    ) -> list[Deployment]:
        result = await db.execute(
            select(Deployment)
            .where(
                Deployment.project_id == replacement.project_id,
                Deployment.environment_id == replacement.environment_id,
                Deployment.branch == replacement.branch,
                Deployment.trigger == "webhook",
                Deployment.id != replacement.id,
                Deployment.conclusion.is_(None),
                Deployment.status.in_(["prepare", "deploy"]),
            )
            .order_by(Deployment.created_at.asc())
        )
        outdated = list(result.scalars().all())
        settings = get_settings()

        for deployment in outdated:
            await self.update_status(
                db,
                deployment,
                status="completed",
                conclusion="skipped",
                error={
                    "status": "superseded",
                    "message": (
                        f"Skipped because deployment {replacement.id[:7]} contains "
                        "a newer push for this branch."
                    ),
                    "deployment_id": replacement.id,
                    "commit_sha": replacement.commit_sha,
                },
                redis_client=redis_client,
            )
            try:
                await queue.enqueue_job(
                    "delete_container",
                    deployment.id,
                    _defer_by=settings.container_delete_grace_seconds,
                )
            except Exception:
                logger.warning(
                    "Could not queue cleanup for superseded deployment %s.",
                    deployment.id,
                    exc_info=True,
                )

        if outdated:
            logger.info(
                "Deployment %s superseded %s older webhook deployment(s).",
                replacement.id,
                len(outdated),
            )
        return outdated

    @staticmethod
    async def _abort_job(deployment: Deployment, queue: ArqRedis) -> bool:
        if not deployment.job_id:
            return False
        try:
            job = Job(job_id=deployment.job_id, redis=queue)
            job_info = await job.info()
            if not job_info or job_info.success is not None:
                return False
            aborted = await job.abort(
                timeout=get_settings().deployment_abort_timeout_seconds,
                poll_delay=0.1,
            )
            if not aborted:
                logger.warning(
                    "Abort was not acknowledged for deployment %s.", deployment.id
                )
            return aborted
        except TimeoutError:
            logger.warning(
                "Timed out waiting for deployment %s to abort.", deployment.id
            )
        except Exception:
            logger.warning(
                "Could not abort deployment job %s.", deployment.id, exc_info=True
            )
        return False

    @staticmethod
    async def _stop_container(deployment: Deployment, db: AsyncSession) -> None:
        if not deployment.container_id or deployment.container_status in {
            "removed",
            "stopped",
        }:
            return
        try:
            runtime_deployment = (
                await db.execute(
                    select(Deployment)
                    .options(joinedload(Deployment.node))
                    .where(Deployment.id == deployment.id)
                )
            ).scalar_one()
            async with deployment_runtime_client(
                runtime_deployment, get_settings()
            ) as docker_client:
                container = await docker_client.containers.get(
                    runtime_deployment.container_id
                )
                try:
                    await container.stop()
                except Exception:
                    pass
                await DeploymentService.update_status(
                    db,
                    deployment,
                    container_status="stopped",
                    emit=False,
                )
        except aiodocker.DockerError as error:
            if error.status == 404:
                await DeploymentService.update_status(
                    db,
                    deployment,
                    container_status="removed",
                    emit=False,
                )
            else:
                logger.warning(
                    "Could not stop deployment container %s: %s",
                    deployment.id,
                    error,
                )
        except Exception:
            logger.warning(
                "Could not stop deployment container %s.",
                deployment.id,
                exc_info=True,
            )

    @staticmethod
    async def _queue_cleanup(deployment: Deployment, queue: ArqRedis) -> None:
        try:
            await queue.enqueue_job(
                "delete_container",
                deployment.id,
                _defer_by=get_settings().container_delete_grace_seconds,
            )
        except Exception:
            logger.warning(
                "Could not queue cleanup for deployment %s.",
                deployment.id,
                exc_info=True,
            )

    async def cancel(
        self,
        project: Project,
        deployment: Deployment,
        queue: ArqRedis,
        redis_client: Redis,
        db: AsyncSession,
    ) -> Deployment:
        """Cancel a deployment."""
        logger.info("Cancel requested for deployment %s", deployment.id)

        lock = self.environment_lock(
            redis_client, deployment.project_id, deployment.environment_id
        )
        async with lock:
            await db.refresh(
                deployment,
                attribute_names=["status", "conclusion", "container_status"],
            )
            if (
                deployment.status in {"finalize", "fail", "completed"}
                or deployment.conclusion
            ):
                raise Exception(
                    "Deployment is already finalizing, failing, or completed"
                )

            await DeploymentService.update_status(
                db,
                deployment,
                status="completed",
                conclusion="canceled",
                redis_client=redis_client,
            )

        await self._queue_cleanup(deployment, queue)
        await self._abort_job(deployment, queue)
        await self._stop_container(deployment, db)

        team_id = getattr(project, "team_id", None)
        if team_id:
            await AuditService.record(
                db,
                team_id=team_id,
                action="deployment.canceled",
                resource_type="deployment",
                resource_id=deployment.id,
                metadata={"project_id": project.id},
            )
        try:
            if not team_id:
                return deployment
            await NotificationService.emit(
                db,
                queue,
                team_id=team_id,
                event="deployment.canceled",
                payload=NotificationService.deployment_payload(
                    deployment,
                    project=project,
                ),
            )
        except Exception:
            logger.warning(
                "Could not queue cancellation notification for deployment %s.",
                deployment.id,
                exc_info=True,
            )

        return deployment

    async def rollback(
        self,
        environment: dict,
        project: Project,
        db: AsyncSession,
        redis_client: Redis,
        settings: Settings,
    ) -> Alias:
        """Rollback an environment to its previous deployment."""
        subdomain = (
            project.slug
            if environment["id"] == "prod"
            else f"{project.slug}-env-{environment['slug']}"
        )

        lock = self.environment_lock(redis_client, project.id, environment["id"])
        async with lock:
            alias = (
                await db.execute(select(Alias).where(Alias.subdomain == subdomain))
            ).scalar_one_or_none()

            if not alias or not alias.previous_deployment_id:
                raise ValueError("No previous deployment to roll back to.")

            alias.deployment_id, alias.previous_deployment_id = (
                alias.previous_deployment_id,
                alias.deployment_id,
            )
            await db.commit()

            await self.update_traefik_config(project, db, settings)

        await redis_client.xadd(
            f"stream:project:{project.id}:updates",
            fields={
                "event_type": "deployment_rollback",
                "environment_id": environment["id"],
                "deployment_id": alias.deployment_id,
                "previous_deployment_id": alias.previous_deployment_id or "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return alias

    # async def promote(
    #     self,
    #     environment: dict,
    #     deployment: Deployment,
    #     project: Project,
    #     db: AsyncSession,
    #     redis_client: Redis,
    #     settings: Settings,
    # ) -> Alias:
    #     """Promote a deployment as current for an environment."""
    #     subdomain = (
    #         project.slug
    #         if environment["id"] == "prod"
    #         else f"{project.slug}-env-{environment['slug']}"
    #     )

    #     alias = (
    #         await db.execute(select(Alias).where(Alias.subdomain == subdomain))
    #     ).scalar_one_or_none()

    #     if not alias:
    #         raise ValueError("No alias found for this environment.")

    #     alias.deployment_id, alias.previous_deployment_id = (
    #         deployment.id,
    #         alias.deployment_id,
    #     )
    #     await db.commit()

    #     await self.update_traefik_config(project, db, settings)

    #     await redis_client.xadd(
    #         f"stream:project:{project.id}:updates",
    #         fields={
    #             "event_type": "deployment_promotion",
    #             "environment_id": environment["id"],
    #             "deployment_id": alias.deployment_id,
    #             "previous_deployment_id": alias.previous_deployment_id or "",
    #             "timestamp": datetime.now(timezone.utc).isoformat(),
    #         },
    #     )

    #     return alias
