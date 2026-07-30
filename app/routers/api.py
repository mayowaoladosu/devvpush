"""Versioned LayerRail REST API."""

from __future__ import annotations

import json
import re
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from config import Settings, get_settings
from db import get_db
from dependencies import (
    get_github_installation_service,
    get_github_service,
    get_queue,
    get_redis_client,
)
from models import (
    AuditEvent,
    Deployment,
    GithubInstallation,
    Project,
    WebhookDelivery,
    WebhookEndpoint,
    utc_now,
)
from services.api_tokens import ApiPrincipal, ApiTokenError, ApiTokenService
from services.audit import AuditService
from services.deployment import DeploymentService
from services.deployment_policy import DeploymentPolicyError
from services.github import GitHubService
from services.github_installation import GitHubInstallationService
from services.notifications import NotificationService
from services.project_config import ProjectConfigError, ProjectConfigService
from services.rate_limit import RateLimiter
from utils.user import get_user_github_token

router = APIRouter(prefix="/api/v1", tags=["LayerRail API"])
bearer = HTTPBearer(auto_error=False)
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}[A-Za-z0-9]$")


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    repo_id: int = Field(gt=0)
    repo_full_name: str = Field(min_length=3, max_length=255)
    github_installation_id: int = Field(gt=0)
    production_branch: str = Field(default="main", min_length=1, max_length=255)
    config: dict[str, object] = Field(default_factory=dict)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    status: str | None = None


class DeploymentCreateRequest(BaseModel):
    branch: str | None = Field(default=None, max_length=255)
    commit_sha: str | None = Field(default=None, max_length=64)


class ProjectConfigRequest(BaseModel):
    config: dict[str, object]


class WebhookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=20)


def _scope(principal: ApiPrincipal, required: str) -> None:
    try:
        principal.require(required)
    except ApiTokenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


async def _authorize_repository_installation(
    *,
    payload: ProjectCreateRequest,
    principal: ApiPrincipal,
    db: AsyncSession,
    github_service: GitHubService,
    installation_service: GitHubInstallationService,
) -> tuple[GithubInstallation, dict]:
    if not principal.user or principal.user.status != "active":
        raise HTTPException(
            status_code=403,
            detail="The API token creator must have an active GitHub identity.",
        )
    github_oauth_token = await get_user_github_token(db, principal.user)
    if not github_oauth_token:
        raise HTTPException(
            status_code=403,
            detail="Connect GitHub before creating projects through the API.",
        )
    try:
        repository = await github_service.get_repository(
            github_oauth_token,
            payload.repo_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail="The API token creator cannot access this repository.",
        ) from exc
    repository_name = str(repository.get("full_name") or "")
    if repository_name.casefold() != payload.repo_full_name.casefold():
        raise HTTPException(status_code=409, detail="Repository identity does not match.")
    try:
        installation_info = await github_service.get_repository_installation(
            repository_name
        )
        if int(installation_info.get("id") or 0) != payload.github_installation_id:
            raise HTTPException(
                status_code=403,
                detail="The GitHub App installation does not match this repository.",
            )
        installation = await installation_service.get_or_refresh_installation(
            payload.github_installation_id,
            db,
        )
        if not installation.token:
            raise ValueError("missing installation token")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=403,
            detail="The GitHub App installation cannot access this repository.",
        ) from exc
    return installation, repository


async def get_api_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
) -> ApiPrincipal:
    if not credentials or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer API token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = await ApiTokenService.authenticate(db, credentials.credentials)
    except ApiTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    limit = await RateLimiter.check(
        redis,
        bucket="api",
        identity=principal.token.id,
        limit=settings.api_rate_limit_requests,
        window_seconds=settings.api_rate_limit_window_seconds,
        enabled=settings.rate_limiting_enabled,
    )
    if not limit.allowed:
        raise HTTPException(
            status_code=429,
            detail="API rate limit exceeded.",
            headers={"Retry-After": str(limit.retry_after)},
        )
    return principal


async def _project(
    db: AsyncSession,
    principal: ApiPrincipal,
    project_id: str,
    *,
    eager: bool = False,
) -> Project:
    query = select(Project).where(
        Project.id == project_id,
        Project.team_id == principal.team.id,
        Project.status != "deleted",
    )
    if eager:
        query = query.options(
            joinedload(Project.github_installation),
            joinedload(Project.team),
        )
    project = (await db.execute(query.limit(1))).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def project_payload(project: Project) -> dict[str, object]:
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "team_id": project.team_id,
        "repo_id": project.repo_id,
        "repo_full_name": project.repo_full_name,
        "repo_status": project.repo_status,
        "status": project.status,
        "environments": project.active_environments,
        "config": ProjectConfigService.export(project),
        "environment_variables": [
            {"key": value.get("key"), "environment": value.get("environment") or None}
            for value in project.env_vars
        ],
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def deployment_payload(deployment: Deployment) -> dict[str, object]:
    return {
        "id": deployment.id,
        "project_id": deployment.project_id,
        "environment_id": deployment.environment_id,
        "branch": deployment.branch,
        "commit_sha": deployment.commit_sha,
        "commit": deployment.commit_meta,
        "trigger": deployment.trigger,
        "status": deployment.status,
        "conclusion": deployment.conclusion,
        "container_status": deployment.container_status,
        "node": deployment.node.name if deployment.node else "primary",
        "url": deployment.url,
        "error": deployment.error,
        "created_at": deployment.created_at,
        "concluded_at": deployment.concluded_at,
    }


@router.get("/health")
async def api_health() -> dict[str, str]:
    return {"status": "ok", "service": "layerrail-api", "version": "v1"}


@router.get("/whoami")
async def whoami(
    principal: ApiPrincipal = Depends(get_api_principal),
) -> dict[str, object]:
    return {
        "team": {
            "id": principal.team.id,
            "name": principal.team.name,
            "slug": principal.team.slug,
        },
        "token": {
            "id": principal.token.id,
            "name": principal.token.name,
            "prefix": principal.token.prefix,
            "scopes": principal.token.scopes,
            "expires_at": principal.token.expires_at,
        },
    }


@router.get("/projects")
async def list_projects(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:read")
    total = int(
        (
            await db.execute(
                select(func.count(Project.id)).where(
                    Project.team_id == principal.team.id,
                    Project.status != "deleted",
                )
            )
        ).scalar_one()
    )
    projects = list(
        (
            await db.execute(
                select(Project)
                .where(
                    Project.team_id == principal.team.id,
                    Project.status != "deleted",
                )
                .order_by(Project.updated_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).scalars()
    )
    return {
        "items": [project_payload(project) for project in projects],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.post("/projects", status_code=201)
async def create_project(
    payload: ProjectCreateRequest,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    github_service: GitHubService = Depends(get_github_service),
    installation_service: GitHubInstallationService = Depends(
        get_github_installation_service
    ),
) -> dict[str, object]:
    _scope(principal, "projects:write")
    if not _PROJECT_NAME.fullmatch(payload.name):
        raise HTTPException(status_code=422, detail="Project name is invalid.")
    if "/" not in payload.repo_full_name:
        raise HTTPException(status_code=422, detail="Repository name is invalid.")
    duplicate = (
        await db.execute(
            select(Project.id).where(
                Project.team_id == principal.team.id,
                func.lower(Project.name) == payload.name.lower(),
                Project.status != "deleted",
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status_code=409, detail="Project name is already in use.")
    installation, repository = await _authorize_repository_installation(
        payload=payload,
        principal=principal,
        db=db,
        github_service=github_service,
        installation_service=installation_service,
    )
    config: dict[str, object] = {
        "build_strategy": "zero-config",
        "dependency_cache": True,
        "dependency_cache_generation": 1,
    }
    if payload.config:
        try:
            public_config = {"version": "1", **payload.config}
            parsed_config = ProjectConfigService.parse(json.dumps(public_config))
            config = ProjectConfigService.merge(config, parsed_config)
        except (ProjectConfigError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    project = Project(
        name=payload.name,
        repo_id=payload.repo_id,
        repo_full_name=payload.repo_full_name,
        github_installation=installation,
        config=config,
        env_vars=[],
        environments=[
            {
                "id": "prod",
                "color": "purple",
                "name": "Production",
                "slug": "production",
                "branch": payload.production_branch,
                "status": "active",
            }
        ],
        team=principal.team,
        created_by_user_id=principal.user.id if principal.user else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="project.created",
        resource_type="project",
        resource_id=project.id,
        metadata={"repo_full_name": project.repo_full_name},
    )
    return project_payload(project)


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:read")
    return project_payload(await _project(db, principal, project_id))


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:write")
    project = await _project(db, principal, project_id)
    if payload.name is not None:
        if not _PROJECT_NAME.fullmatch(payload.name):
            raise HTTPException(status_code=422, detail="Project name is invalid.")
        duplicate = (
            await db.execute(
                select(Project.id).where(
                    Project.team_id == principal.team.id,
                    Project.id != project.id,
                    func.lower(Project.name) == payload.name.lower(),
                    Project.status != "deleted",
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            raise HTTPException(status_code=409, detail="Project name is already in use.")
        project.name = payload.name
    if payload.status is not None:
        if payload.status not in {"active", "paused"}:
            raise HTTPException(status_code=422, detail="Project status is invalid.")
        project.status = payload.status
    await db.commit()
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="project.updated",
        resource_type="project",
        resource_id=project.id,
        metadata={"name": project.name, "status": project.status},
    )
    return project_payload(project)


@router.get("/projects/{project_id}/config")
async def get_project_config(
    project_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:read")
    project = await _project(db, principal, project_id)
    return ProjectConfigService.export(project)


@router.put("/projects/{project_id}/config")
async def put_project_config(
    project_id: str,
    payload: ProjectConfigRequest,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:write")
    project = await _project(db, principal, project_id)
    try:
        result = ProjectConfigService.apply_to_project(
            project,
            json.dumps(payload.config),
        )
    except (ProjectConfigError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="project.config_updated",
        resource_type="project",
        resource_id=project.id,
        metadata={"source": result.source, "keys": sorted(result.values)},
    )
    return ProjectConfigService.export(project)


@router.get("/projects/{project_id}/export")
async def export_project(
    project_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "projects:read")
    project = await _project(db, principal, project_id)
    return {
        "format": "layerrail-project",
        "version": "1",
        "project": project_payload(project),
        "secrets_included": False,
    }


@router.get("/projects/{project_id}/deployments")
async def list_deployments(
    project_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    conclusion: str | None = None,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "deployments:read")
    project = await _project(db, principal, project_id)
    query = select(Deployment).options(joinedload(Deployment.node)).where(
        Deployment.project_id == project.id
    )
    if conclusion:
        query = query.where(Deployment.conclusion == conclusion)
    deployments = list(
        (
            await db.execute(
                query.order_by(Deployment.created_at.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).scalars()
    )
    return {
        "items": [deployment_payload(item) for item in deployments],
        "page": page,
        "limit": limit,
    }


@router.post("/projects/{project_id}/deployments", status_code=202)
async def create_deployment(
    project_id: str,
    payload: DeploymentCreateRequest,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
    queue: ArqRedis = Depends(get_queue),
    settings: Settings = Depends(get_settings),
    github_service: GitHubService = Depends(get_github_service),
    installation_service: GitHubInstallationService = Depends(
        get_github_installation_service
    ),
) -> dict[str, object]:
    _scope(principal, "deployments:write")
    deploy_limit = await RateLimiter.check(
        redis,
        bucket="api-deployments",
        identity=principal.token.id,
        limit=settings.api_deployment_rate_limit_requests,
        window_seconds=settings.api_deployment_rate_limit_window_seconds,
        enabled=settings.rate_limiting_enabled,
    )
    if not deploy_limit.allowed:
        raise HTTPException(
            status_code=429,
            detail="Deployment rate limit exceeded.",
            headers={"Retry-After": str(deploy_limit.retry_after)},
        )
    project = await _project(db, principal, project_id, eager=True)
    production = project.get_environment_by_id("prod") or {}
    branch = payload.branch or str(production.get("branch") or "main")
    commit_sha = payload.commit_sha or branch
    try:
        installation = await installation_service.get_or_refresh_installation(
            project.github_installation_id,
            db,
        )
        if not installation.token:
            raise ValueError("missing installation token")
        commit = await github_service.get_repository_commit(
            installation.token,
            project.repo_id,
            commit_sha,
            branch=branch,
        )
        deployment = await DeploymentService().schedule(
            project=project,
            branch=branch,
            commit=commit,
            db=db,
            redis_client=redis,
            queue=queue,
            trigger="api",
            current_user=principal.user,
        )
    except DeploymentPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Deployment could not be scheduled.") from exc
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="api.deployment_scheduled",
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={"project_id": project.id, "branch": branch},
    )
    return deployment_payload(deployment)


async def _deployment(
    db: AsyncSession,
    principal: ApiPrincipal,
    deployment_id: str,
) -> Deployment:
    deployment = (
        await db.execute(
            select(Deployment)
            .options(
                joinedload(Deployment.project).joinedload(Project.team),
                joinedload(Deployment.node),
            )
            .join(Project, Deployment.project_id == Project.id)
            .where(
                Deployment.id == deployment_id,
                Project.team_id == principal.team.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    return deployment


@router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "deployments:read")
    return deployment_payload(await _deployment(db, principal, deployment_id))


@router.post("/deployments/{deployment_id}/cancel")
async def cancel_deployment(
    deployment_id: str,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
    queue: ArqRedis = Depends(get_queue),
) -> dict[str, object]:
    _scope(principal, "deployments:write")
    deployment = await _deployment(db, principal, deployment_id)
    try:
        await DeploymentService().cancel(
            project=deployment.project,
            deployment=deployment,
            queue=queue,
            redis_client=redis,
            db=db,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="api.deployment_canceled",
        resource_type="deployment",
        resource_id=deployment.id,
    )
    return deployment_payload(deployment)


@router.post("/projects/{project_id}/environments/{environment_id}/rollback")
async def rollback_environment(
    project_id: str,
    environment_id: str,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    _scope(principal, "deployments:write")
    project = await _project(db, principal, project_id)
    environment = project.get_environment_by_id(environment_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found.")
    try:
        alias = await DeploymentService().rollback(
            environment=environment,
            project=project,
            db=db,
            redis_client=redis,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="environment.rolled_back",
        resource_type="project",
        resource_id=project.id,
        metadata={"environment_id": environment_id, "deployment_id": alias.deployment_id},
    )
    return {
        "project_id": project.id,
        "environment_id": environment_id,
        "deployment_id": alias.deployment_id,
        "previous_deployment_id": alias.previous_deployment_id,
    }


@router.get("/deployments/{deployment_id}/logs")
async def get_deployment_logs(
    deployment_id: str,
    request: Request,
    limit: int = Query(1000, ge=1, le=5000),
    keyword: str | None = Query(default=None, max_length=200),
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "logs:read")
    deployment = await _deployment(db, principal, deployment_id)
    try:
        logs = await request.app.state.loki_service.get_logs(
            project_id=deployment.project_id,
            deployment_id=deployment.id,
            keyword=keyword,
            limit=limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Deployment logs are unavailable.") from exc
    return {"deployment_id": deployment.id, "items": logs}


@router.get("/audit-events")
async def list_audit_events(
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "audit:read")
    query = select(AuditEvent).where(AuditEvent.team_id == principal.team.id)
    if before_id:
        query = query.where(AuditEvent.id < before_id)
    events = list(
        (
            await db.execute(query.order_by(AuditEvent.id.desc()).limit(limit))
        ).scalars()
    )
    return {
        "items": [
            {
                "id": event.id,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "actor_user_id": event.actor_user_id,
                "api_token_id": event.api_token_id,
                "metadata": event.metadata_json,
                "ip_address": event.ip_address,
                "created_at": event.created_at,
            }
            for event in events
        ]
    }


@router.get("/webhooks")
async def list_webhooks(
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _scope(principal, "webhooks:read")
    endpoints = list(
        (
            await db.execute(
                select(WebhookEndpoint)
                .where(WebhookEndpoint.team_id == principal.team.id)
                .order_by(WebhookEndpoint.created_at.desc())
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "id": endpoint.id,
                "name": endpoint.name,
                "url": endpoint.url,
                "events": endpoint.events,
                "status": endpoint.status,
                "failure_count": endpoint.failure_count,
                "last_delivered_at": endpoint.last_delivered_at,
                "last_error": endpoint.last_error,
                "created_at": endpoint.created_at,
            }
            for endpoint in endpoints
        ]
    }


@router.post("/webhooks", status_code=201)
async def create_webhook(
    payload: WebhookCreateRequest,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    _scope(principal, "webhooks:write")
    try:
        endpoint, secret = await NotificationService.configure_endpoint(
            db,
            team_id=principal.team.id,
            user_id=principal.user.id if principal.user else None,
            name=payload.name,
            url=payload.url,
            events=payload.events,
            settings=settings,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="webhook.created",
        resource_type="webhook",
        resource_id=endpoint.id,
        metadata={"events": endpoint.events, "url_host": endpoint.url.split("/", 3)[2]},
    )
    return {
        "id": endpoint.id,
        "name": endpoint.name,
        "url": endpoint.url,
        "events": endpoint.events,
        "secret": secret,
        "secret_notice": "This secret is shown only once.",
    }


@router.delete("/webhooks/{endpoint_id}", status_code=204)
async def delete_webhook(
    endpoint_id: str,
    request: Request,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _scope(principal, "webhooks:write")
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if not endpoint or endpoint.team_id != principal.team.id:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    await db.delete(endpoint)
    await db.commit()
    await AuditService.record(
        db,
        team_id=principal.team.id,
        user=principal.user,
        api_token=principal.token,
        request=request,
        action="webhook.deleted",
        resource_type="webhook",
        resource_id=endpoint_id,
    )
    return Response(status_code=204)


@router.post("/webhooks/{endpoint_id}/test", status_code=202)
async def test_webhook(
    endpoint_id: str,
    principal: ApiPrincipal = Depends(get_api_principal),
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_queue),
) -> dict[str, object]:
    _scope(principal, "webhooks:write")
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if not endpoint or endpoint.team_id != principal.team.id:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        event="webhook.test",
        payload={
            "message": "LayerRail webhook test",
            "team_id": principal.team.id,
            "sent_at": utc_now().isoformat() + "Z",
        },
    )
    db.add(delivery)
    await db.commit()
    await NotificationService.enqueue_webhook(queue, delivery.id)
    return {"delivery_id": delivery.id, "status": delivery.status}
