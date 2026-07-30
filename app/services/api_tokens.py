"""Scoped, revocable LayerRail API tokens."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from models import ApiToken, Team, User, utc_now

TOKEN_PATTERN = re.compile(
    r"^lr_(?P<mode>live|test)_(?P<prefix>[A-Za-z0-9_-]{8})_"
    r"(?P<secret>[A-Za-z0-9_-]{32,128})$"
)
API_SCOPES = frozenset(
    {
        "projects:read",
        "projects:write",
        "deployments:read",
        "deployments:write",
        "logs:read",
        "audit:read",
        "webhooks:read",
        "webhooks:write",
    }
)
DEFAULT_API_SCOPES = tuple(sorted(API_SCOPES))


class ApiTokenError(ValueError):
    """An API token request is invalid or unauthorized."""


@dataclass(frozen=True)
class ApiPrincipal:
    token: ApiToken
    team: Team
    user: User | None

    def require(self, scope: str) -> None:
        if scope not in API_SCOPES:
            raise ApiTokenError("Unknown API scope.")
        if "*" not in self.token.scopes and scope not in self.token.scopes:
            raise ApiTokenError(f"The API token requires the {scope} scope.")


class ApiTokenService:
    """Generate once, persist only a digest, and authenticate team-scoped tokens."""

    @staticmethod
    def digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        team: Team,
        user: User,
        name: object,
        scopes: list[str] | tuple[str, ...] | None = None,
        expires_in_days: int | None = 90,
        mode: str = "live",
    ) -> tuple[ApiToken, str]:
        token_name = str(name or "").strip()
        if not 1 <= len(token_name) <= 100:
            raise ApiTokenError("Token name must contain between 1 and 100 characters.")
        if mode not in {"live", "test"}:
            raise ApiTokenError("Token mode is invalid.")
        selected_scopes = sorted(
            set(DEFAULT_API_SCOPES if scopes is None else scopes)
        )
        if not selected_scopes or any(scope not in API_SCOPES for scope in selected_scopes):
            raise ApiTokenError("Select at least one valid API scope.")
        if expires_in_days is not None and not 1 <= int(expires_in_days) <= 3650:
            raise ApiTokenError("Token expiry must be between 1 and 3650 days.")

        prefix = secrets.token_urlsafe(6)[:8]
        secret = secrets.token_urlsafe(32)
        raw_token = f"lr_{mode}_{prefix}_{secret}"
        token = ApiToken(
            team_id=team.id,
            name=token_name,
            prefix=prefix,
            token_hash=cls.digest(raw_token),
            scopes=selected_scopes,
            expires_at=(
                utc_now() + timedelta(days=int(expires_in_days))
                if expires_in_days is not None
                else None
            ),
            created_by_user_id=user.id,
        )
        db.add(token)
        await db.commit()
        await db.refresh(token)
        return token, raw_token

    @classmethod
    async def authenticate(
        cls,
        db: AsyncSession,
        raw_token: object,
        *,
        update_last_used: bool = True,
    ) -> ApiPrincipal:
        value = str(raw_token or "").strip()
        match = TOKEN_PATTERN.fullmatch(value)
        if not match:
            raise ApiTokenError("API token is invalid.")
        result = await db.execute(
            select(ApiToken)
            .options(
                joinedload(ApiToken.team),
                joinedload(ApiToken.created_by_user),
            )
            .where(ApiToken.token_hash == cls.digest(value))
            .limit(1)
        )
        token = result.scalar_one_or_none()
        now = utc_now()
        if (
            not token
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
            or not token.team
            or token.team.status != "active"
        ):
            raise ApiTokenError("API token is invalid or expired.")
        if update_last_used and (
            token.last_used_at is None
            or token.last_used_at <= now - timedelta(minutes=5)
        ):
            token.last_used_at = now
            await db.commit()
        return ApiPrincipal(
            token=token,
            team=token.team,
            user=token.created_by_user,
        )

    @staticmethod
    async def revoke(
        db: AsyncSession,
        token: ApiToken,
        *,
        team_id: str,
    ) -> None:
        if token.team_id != team_id:
            raise ApiTokenError("API token does not belong to this team.")
        if token.revoked_at is None:
            token.revoked_at = utc_now()
            await db.commit()

    @staticmethod
    def display(token: ApiToken) -> str:
        return f"lr_••••_{token.prefix}_••••••••"
