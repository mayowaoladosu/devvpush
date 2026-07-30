"""Durable LayerRail email and signed-webhook notifications."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import secrets
import socket
from urllib.parse import ParseResult, urlparse

import httpx
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from config import Settings, get_settings
from db import AsyncSessionLocal
from models import (
    Deployment,
    NotificationSettings,
    TeamMember,
    User,
    WebhookDelivery,
    WebhookEndpoint,
    utc_now,
)
from utils.email import send_email

logger = logging.getLogger(__name__)
DEPLOYMENT_EVENTS = frozenset(
    {
        "deployment.created",
        "deployment.succeeded",
        "deployment.failed",
        "deployment.canceled",
        "deployment.skipped",
    }
)
WEBHOOK_EVENTS = frozenset({*DEPLOYMENT_EVENTS, "webhook.test"})


class NotificationConfigurationError(ValueError):
    """A notification endpoint is invalid."""


class NotificationService:
    @classmethod
    async def resolve_url(
        cls,
        value: object,
        settings: Settings,
    ) -> tuple[str, ParseResult, tuple[str, ...]]:
        raw = str(value or "").strip()
        parsed = urlparse(raw)
        allowed_schemes = {"http", "https"} if settings.env == "development" else {"https"}
        if (
            parsed.scheme not in allowed_schemes
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(raw) > 2048
        ):
            raise NotificationConfigurationError(
                "Webhook URL must be an HTTPS URL without embedded credentials."
            )
        try:
            port = parsed.port
            addresses = await asyncio.wait_for(
                asyncio.to_thread(cls._resolve, parsed.hostname, port),
                timeout=5,
            )
        except (OSError, TimeoutError, ValueError) as exc:
            raise NotificationConfigurationError(
                "Webhook hostname could not be resolved."
            ) from exc
        if not addresses:
            raise NotificationConfigurationError(
                "Webhook hostname could not be resolved."
            )
        if settings.env != "development" and any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise NotificationConfigurationError(
                "Webhook hostname must resolve only to public addresses."
            )
        return raw, parsed, tuple(
            sorted(addresses, key=lambda address: ipaddress.ip_address(address).version)
        )

    @classmethod
    async def validate_url(cls, value: object, settings: Settings) -> str:
        raw, _, _ = await cls.resolve_url(value, settings)
        return raw

    @staticmethod
    def pinned_url(parsed: ParseResult, address: str) -> str:
        host = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl()

    @staticmethod
    def host_header(parsed: ParseResult) -> str:
        hostname = parsed.hostname or ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port is not None and parsed.port != default_port:
            host = f"{host}:{parsed.port}"
        return host

    @staticmethod
    async def enqueue_webhook(queue: ArqRedis, delivery_id: str) -> bool:
        try:
            await queue.enqueue_job(
                "deliver_webhook",
                delivery_id,
                _job_id=f"webhook:{delivery_id}",
            )
        except Exception:
            logger.warning(
                "Webhook delivery %s remains pending after queue admission failed.",
                delivery_id,
                exc_info=True,
            )
            return False
        return True

    @classmethod
    async def reconcile_pending_webhooks(
        cls,
        queue: ArqRedis,
        *,
        limit: int = 100,
    ) -> int:
        async with AsyncSessionLocal() as db:
            delivery_ids = list(
                (
                    await db.execute(
                        select(WebhookDelivery.id)
                        .where(WebhookDelivery.status == "pending")
                        .order_by(WebhookDelivery.created_at.asc())
                        .limit(max(1, min(int(limit), 1_000)))
                    )
                ).scalars()
            )
        for delivery_id in delivery_ids:
            await cls.enqueue_webhook(queue, delivery_id)
        return len(delivery_ids)

    @classmethod
    async def configure_endpoint(
        cls,
        db: AsyncSession,
        *,
        team_id: str,
        user_id: int | None,
        name: object,
        url: object,
        events: object,
        settings: Settings,
    ) -> tuple[WebhookEndpoint, str]:
        endpoint_name = str(name or "").strip()
        if not 1 <= len(endpoint_name) <= 100:
            raise NotificationConfigurationError(
                "Webhook name must contain between 1 and 100 characters."
            )
        endpoint_url = await cls.validate_url(url, settings)
        selected = cls.normalize_events(events)
        secret = "whsec_" + secrets.token_urlsafe(32)
        endpoint = WebhookEndpoint(
            team_id=team_id,
            name=endpoint_name,
            url=endpoint_url,
            events=selected,
            status="active",
            created_by_user_id=user_id,
        )
        endpoint.secret = secret
        db.add(endpoint)
        await db.commit()
        await db.refresh(endpoint)
        return endpoint, secret

    @staticmethod
    def normalize_events(value: object) -> list[str]:
        if isinstance(value, str):
            candidates = value.replace(",", "\n").splitlines()
        elif isinstance(value, (list, tuple, set)):
            candidates = [str(item) for item in value]
        else:
            candidates = []
        selected = sorted(
            {
                event.strip()
                for event in candidates
                if event.strip() in WEBHOOK_EVENTS and event.strip() != "webhook.test"
            }
        )
        if not selected:
            raise NotificationConfigurationError(
                "Select at least one webhook event."
            )
        return selected

    @classmethod
    async def emit(
        cls,
        db: AsyncSession,
        queue: ArqRedis,
        *,
        team_id: str,
        event: str,
        payload: dict[str, object],
    ) -> list[WebhookDelivery]:
        if event not in DEPLOYMENT_EVENTS:
            raise NotificationConfigurationError("Notification event is invalid.")
        endpoints = list(
            (
                await db.execute(
                    select(WebhookEndpoint).where(
                        WebhookEndpoint.team_id == team_id,
                        WebhookEndpoint.status == "active",
                    )
                )
            ).scalars()
        )
        deliveries = [
            WebhookDelivery(endpoint_id=endpoint.id, event=event, payload=payload)
            for endpoint in endpoints
            if event in (endpoint.events or [])
        ]
        db.add_all(deliveries)
        await db.commit()
        for delivery in deliveries:
            await cls.enqueue_webhook(queue, delivery.id)
        try:
            await queue.enqueue_job(
                "deliver_deployment_email",
                event,
                payload,
                _job_id=f"email:{event}:{payload.get('deployment_id')}",
            )
        except Exception:
            logger.warning(
                "Could not queue deployment email for %s.",
                payload.get("deployment_id"),
                exc_info=True,
            )
        return deliveries

    @staticmethod
    def deployment_payload(
        deployment: Deployment,
        *,
        project=None,
    ) -> dict[str, object]:
        project = project or deployment.project
        return {
            "deployment_id": deployment.id,
            "project_id": deployment.project_id,
            "project_name": project.name if project else "",
            "environment_id": deployment.environment_id,
            "branch": deployment.branch,
            "commit_sha": deployment.commit_sha,
            "status": deployment.status,
            "conclusion": deployment.conclusion,
            "url": deployment.url,
            "created_at": deployment.created_at.isoformat() + "Z",
            "concluded_at": (
                deployment.concluded_at.isoformat() + "Z"
                if deployment.concluded_at
                else None
            ),
        }

    @staticmethod
    def _resolve(hostname: str, port: int | None) -> set[str]:
        return {
            str(result[4][0])
            for result in socket.getaddrinfo(
                hostname,
                port or 443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        }


async def deliver_webhook(ctx, delivery_id: str) -> None:
    attempt = max(1, int(ctx.get("job_try") or 1))
    async with AsyncSessionLocal() as db:
        delivery = (
            await db.execute(
                select(WebhookDelivery)
                .options(joinedload(WebhookDelivery.endpoint))
                .where(WebhookDelivery.id == delivery_id)
            )
        ).scalar_one_or_none()
        if not delivery or delivery.status == "delivered":
            return
        endpoint = delivery.endpoint
        if not endpoint or endpoint.status != "active":
            delivery.status = "failed"
            delivery.last_error = "Webhook endpoint is disabled."
            delivery.attempts = attempt
            await db.commit()
            return
        body = json.dumps(
            delivery.payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            endpoint.secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        try:
            _, parsed, addresses = await NotificationService.resolve_url(
                endpoint.url,
                get_settings(),
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
            ) as client:
                connect_error: httpx.HTTPError | None = None
                for address in addresses:
                    request = client.build_request(
                        "POST",
                        NotificationService.pinned_url(parsed, address),
                        content=body,
                        headers={
                            "Host": NotificationService.host_header(parsed),
                            "Content-Type": "application/json",
                            "User-Agent": "LayerRail-Webhooks/1.0",
                            "X-LayerRail-Delivery": delivery.id,
                            "X-LayerRail-Event": delivery.event,
                            "X-LayerRail-Signature": f"sha256={signature}",
                        },
                    )
                    if parsed.scheme == "https":
                        request.extensions["sni_hostname"] = parsed.hostname
                    try:
                        response = await client.send(request, stream=True)
                        try:
                            response.raise_for_status()
                            response_status = response.status_code
                        finally:
                            await response.aclose()
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        connect_error = exc
                else:
                    if connect_error:
                        raise connect_error
                    raise httpx.ConnectError("Webhook endpoint could not be reached.")
        except (httpx.HTTPError, NotificationConfigurationError) as exc:
            delivery.attempts = attempt
            delivery.response_status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            delivery.last_error = exc.__class__.__name__
            endpoint.failure_count += 1
            endpoint.last_error = delivery.last_error
            delivery.status = "failed" if attempt >= 3 else "pending"
            await db.commit()
            if attempt < 3:
                raise
            return
        delivery.status = "delivered"
        delivery.attempts = attempt
        delivery.response_status = response_status
        delivery.last_error = None
        delivery.delivered_at = utc_now()
        endpoint.failure_count = 0
        endpoint.last_error = None
        endpoint.last_delivered_at = utc_now()
        await db.commit()


async def deliver_deployment_email(
    ctx,
    event: str,
    payload: dict[str, object],
) -> None:
    settings = get_settings()
    if not settings.email_sender_address:
        return
    project_id = str(payload.get("project_id") or "")
    if not project_id:
        return
    async with AsyncSessionLocal() as db:
        deployment = (
            await db.execute(
                select(Deployment)
                .options(joinedload(Deployment.project))
                .where(Deployment.id == str(payload.get("deployment_id") or ""))
            )
        ).scalar_one_or_none()
        if not deployment or not deployment.project:
            return
        team_id = deployment.project.team_id
        preferences = await db.get(NotificationSettings, team_id)
        if not preferences:
            preferences = NotificationSettings(
                team_id=team_id,
                deployment_succeeded=False,
                deployment_failed=True,
                deployment_canceled=False,
                recipients=[],
            )
            db.add(preferences)
            await db.commit()
        enabled = {
            "deployment.succeeded": preferences.deployment_succeeded,
            "deployment.failed": preferences.deployment_failed,
            "deployment.canceled": preferences.deployment_canceled,
        }.get(event, False)
        if not enabled:
            return
        recipients = [str(value).strip() for value in preferences.recipients if str(value).strip()]
        if not recipients:
            recipients = list(
                (
                    await db.execute(
                        select(User.email)
                        .join(TeamMember, TeamMember.user_id == User.id)
                        .where(
                            TeamMember.team_id == team_id,
                            TeamMember.role.in_(["owner", "admin"]),
                            User.status == "active",
                        )
                    )
                ).scalars()
            )
        if not recipients:
            return
    conclusion = str(payload.get("conclusion") or event.rsplit(".", 1)[-1])
    project_name = html.escape(str(payload.get("project_name") or "project"))
    deployment_id = html.escape(str(payload.get("deployment_id") or ""))
    deployment_url = html.escape(str(payload.get("url") or ""))
    body = (
        '<div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:auto">'
        '<div style="color:#8B67F2;font-weight:700;font-size:18px">LayerRail</div>'
        f'<h1 style="font-size:24px">Deployment {html.escape(conclusion)}</h1>'
        f'<p><strong>{project_name}</strong> deployment <code>{deployment_id[:7]}</code> '
        f'is {html.escape(conclusion)}.</p>'
        f'<p><a href="{deployment_url}" style="color:#6842d9">Open deployment</a></p>'
        '</div>'
    )
    await asyncio.to_thread(
        send_email,
        recipients=recipients,
        subject=f"{project_name}: deployment {conclusion}",
        data=body,
        settings=settings,
    )
