"""Redacted, append-only LayerRail audit history."""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from models import ApiToken, AuditEvent, User

_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|token|credential|api[_-]?key|private[_-]?key|authorization|cookie|value)",
    re.IGNORECASE,
)


class AuditService:
    """Persist bounded actor/resource events without secret values."""

    @classmethod
    async def record(
        cls,
        db: AsyncSession,
        *,
        team_id: str | None,
        action: str,
        resource_type: str,
        resource_id: object | None = None,
        user: User | None = None,
        api_token: ApiToken | None = None,
        request: Request | None = None,
        metadata: dict[str, object] | None = None,
        commit: bool = True,
    ) -> AuditEvent:
        action = str(action or "").strip()[:80]
        resource_type = str(resource_type or "").strip()[:40]
        if not action or not resource_type:
            raise ValueError("Audit action and resource type are required.")
        event = AuditEvent(
            team_id=team_id,
            actor_user_id=user.id if user else None,
            api_token_id=api_token.id if api_token else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id)[:64] if resource_id is not None else None,
            metadata_json=cls.redact(metadata or {}),
            ip_address=cls.client_ip(request),
            user_agent=(
                str(request.headers.get("user-agent") or "")[:255]
                if request
                else None
            ),
        )
        db.add(event)
        if commit:
            await db.commit()
            await db.refresh(event)
        return event

    @classmethod
    def redact(cls, value: Any, *, depth: int = 0) -> Any:
        if depth >= 4:
            return "[truncated]"
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 40:
                    result["_truncated"] = True
                    break
                name = str(key)[:80]
                result[name] = (
                    "[redacted]"
                    if _SENSITIVE_KEY.search(name)
                    else cls.redact(item, depth=depth + 1)
                )
            return result
        if isinstance(value, (list, tuple, set)):
            return [cls.redact(item, depth=depth + 1) for item in list(value)[:40]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return str(value)[:500] if isinstance(value, str) else value
        return str(value)[:500]

    @staticmethod
    def client_ip(request: Request | None) -> str | None:
        if not request or not request.client:
            return None
        return str(request.client.host or "")[:45] or None
