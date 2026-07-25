"""Durable, redacted control-plane diagnostics for deployments."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from db import AsyncSessionLocal
from models import Deployment, DeploymentDiagnostic

_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?<![A-Za-z0-9_])(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+@"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
)
_MAX_MESSAGE = 2_000
_MAX_DETAIL_STRING = 4_000


class DeploymentDiagnosticService:
    """Persist small control-plane events independently from Loki."""

    @classmethod
    async def record(
        cls,
        db: AsyncSession,
        deployment_id: str,
        *,
        level: str,
        source: str,
        stage: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        attempt: int | None = None,
        commit: bool = True,
    ) -> DeploymentDiagnostic:
        diagnostic = DeploymentDiagnostic(
            deployment_id=deployment_id,
            level=cls._normalize_level(level),
            source=cls._bounded_identifier(source, "worker"),
            stage=cls._bounded_identifier(stage, "prepare"),
            code=cls._bounded_identifier(code, "unknown_error", max_length=64),
            message=cls.sanitize(message, _MAX_MESSAGE),
            details=cls._sanitize_value(details or {}),
            attempt=max(0, int(attempt)) if attempt is not None else None,
        )
        db.add(diagnostic)
        if commit:
            await db.commit()
        else:
            await db.flush()
        return diagnostic

    @classmethod
    async def record_external(cls, deployment_id: str, **kwargs) -> bool:
        try:
            async with AsyncSessionLocal() as db:
                await cls.record(db, deployment_id, **kwargs)
            return True
        except Exception:
            return False

    @classmethod
    async def record_once(
        cls,
        deployment_id: str,
        *,
        code: str,
        **kwargs,
    ) -> bool:
        try:
            async with AsyncSessionLocal() as db:
                existing = await db.scalar(
                    select(DeploymentDiagnostic.id)
                    .where(
                        DeploymentDiagnostic.deployment_id == deployment_id,
                        DeploymentDiagnostic.code == code,
                    )
                    .limit(1)
                )
                if existing is not None:
                    return False
                await cls.record(db, deployment_id, code=code, **kwargs)
            return True
        except Exception:
            return False

    @classmethod
    def failure_payload(
        cls,
        *,
        stage: str,
        code: str,
        message: str,
        source: str,
        attempt: int | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": cls._bounded_identifier(stage, "prepare"),
            "code": cls._bounded_identifier(code, "deployment_failed", max_length=64),
            "message": cls.sanitize(message, _MAX_MESSAGE),
            "source": cls._bounded_identifier(source, "worker"),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if attempt is not None:
            payload["attempt"] = max(0, int(attempt))
        if hint:
            payload["hint"] = cls.sanitize(hint, _MAX_MESSAGE)
        if details:
            payload["details"] = cls._sanitize_value(details)
        return payload

    @classmethod
    async def get_logs(
        cls,
        db: AsyncSession,
        *,
        project_id: str,
        limit: int,
        deployment_id: str | None = None,
        environment_id: str | None = None,
        branch: str | None = None,
        keyword: str | None = None,
        start_timestamp: int | str | None = None,
        end_timestamp: int | str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            select(DeploymentDiagnostic, Deployment)
            .join(Deployment, DeploymentDiagnostic.deployment_id == Deployment.id)
            .where(Deployment.project_id == project_id)
        )
        if deployment_id:
            query = query.where(Deployment.id == deployment_id)
        if environment_id:
            query = query.where(Deployment.environment_id == environment_id)
        if branch:
            query = query.where(Deployment.branch == branch)
        if keyword:
            query = query.where(DeploymentDiagnostic.message.ilike(f"%{keyword}%"))
        start = cls._timestamp_to_datetime(start_timestamp)
        end = cls._timestamp_to_datetime(end_timestamp)
        if start:
            query = query.where(DeploymentDiagnostic.created_at >= start)
        if end:
            query = query.where(DeploymentDiagnostic.created_at <= end)

        result = await db.execute(
            query.order_by(DeploymentDiagnostic.created_at.desc()).limit(limit)
        )
        values = []
        for diagnostic, deployment in reversed(result.all()):
            created_at = diagnostic.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            timestamp = int(created_at.timestamp() * 1_000_000_000)
            values.append(
                {
                    "timestamp_iso": created_at.isoformat().replace("+00:00", "Z"),
                    "timestamp": str(timestamp),
                    "message": diagnostic.message,
                    "level": diagnostic.level,
                    "labels": {
                        "project_id": deployment.project_id,
                        "deployment_id": deployment.id,
                        "environment_id": deployment.environment_id,
                        "branch": deployment.branch,
                        "source": diagnostic.source,
                        "stage": diagnostic.stage,
                        "code": diagnostic.code,
                    },
                }
            )
        return values

    @staticmethod
    def merge_logs(*groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        merged = [item for group in groups for item in group]
        merged.sort(key=lambda item: int(item.get("timestamp") or 0))
        return merged[-limit:]

    @classmethod
    def exception_message(cls, error: BaseException) -> str:
        message = str(error).strip() or error.__class__.__name__
        return cls.sanitize(f"{error.__class__.__name__}: {message}", _MAX_MESSAGE)

    @classmethod
    def sanitize(cls, value: object, limit: int) -> str:
        text = str(value or "").replace("\x00", "").strip()
        settings = get_settings()
        secrets = (
            settings.github_app_private_key,
            settings.github_app_webhook_secret,
            settings.github_app_client_secret,
            settings.google_client_secret,
            settings.resend_api_key,
            settings.smtp_password,
            settings.secret_key,
            settings.encryption_key,
            settings.postgres_password,
        )
        for secret in secrets:
            if secret and len(secret) >= 8:
                text = text.replace(secret, "[REDACTED]")
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(
                lambda match: (match.group(1) if match.lastindex else "")
                + "[REDACTED]",
                text,
            )
        text = " ".join(text.split())
        return text[:limit] or "No diagnostic message was provided."

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls.sanitize(value, _MAX_DETAIL_STRING)
        if isinstance(value, dict):
            return {
                cls.sanitize(key, 128): cls._sanitize_value(item)
                for key, item in list(value.items())[:50]
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._sanitize_value(item) for item in list(value)[:50]]
        return cls.sanitize(value, _MAX_DETAIL_STRING)

    @staticmethod
    def _normalize_level(value: str) -> str:
        level = str(value or "ERROR").upper()
        return level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "SUCCESS"} else "ERROR"

    @staticmethod
    def _bounded_identifier(
        value: object, fallback: str, *, max_length: int = 32
    ) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value or ""))
        return (normalized[:max_length] or fallback)[:max_length]

    @staticmethod
    def _timestamp_to_datetime(value: int | str | None) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=UTC).replace(
                tzinfo=None
            )
        except (TypeError, ValueError, OSError):
            return None
