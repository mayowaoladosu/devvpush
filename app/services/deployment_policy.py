"""Project deployment admission and webhook trigger policy."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Deployment, Project


class DeploymentPolicyError(ValueError):
    """A deployment is blocked by project policy."""


@dataclass(frozen=True)
class DeploymentPolicyDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class DeploymentPolicy:
    webhook_enabled: bool = True
    allowed_branches: tuple[str, ...] = ()
    ignored_branches: tuple[str, ...] = ()
    ignored_authors: tuple[str, ...] = ("dependabot[bot]",)
    skip_message_tokens: tuple[str, ...] = (
        "[skip layerrail]",
        "[skip deploy]",
        "[ci skip]",
    )
    max_concurrent: int = 1
    supersede_older: bool = True

    def decide(self, *, branch: str, author: str, message: str) -> DeploymentPolicyDecision:
        if not self.webhook_enabled:
            return DeploymentPolicyDecision(False, "Automatic deployments are disabled.")
        if self.allowed_branches and not any(
            fnmatchcase(branch, pattern) for pattern in self.allowed_branches
        ):
            return DeploymentPolicyDecision(False, "The branch is not allowed by deployment policy.")
        if any(fnmatchcase(branch, pattern) for pattern in self.ignored_branches):
            return DeploymentPolicyDecision(False, "The branch is ignored by deployment policy.")
        if author.casefold() in {value.casefold() for value in self.ignored_authors}:
            return DeploymentPolicyDecision(False, "The commit author is ignored by deployment policy.")
        message_folded = message.casefold()
        if any(token.casefold() in message_folded for token in self.skip_message_tokens):
            return DeploymentPolicyDecision(False, "The commit message requested that deployment be skipped.")
        return DeploymentPolicyDecision(True)

    def as_dict(self) -> dict[str, object]:
        return {
            "webhook_enabled": self.webhook_enabled,
            "allowed_branches": list(self.allowed_branches),
            "ignored_branches": list(self.ignored_branches),
            "ignored_authors": list(self.ignored_authors),
            "skip_message_tokens": list(self.skip_message_tokens),
            "max_concurrent": self.max_concurrent,
            "supersede_older": self.supersede_older,
        }


class DeploymentPolicyService:
    KEY = "deployment_policy"

    @classmethod
    def from_project(cls, project: Project) -> DeploymentPolicy:
        raw = (getattr(project, "config", None) or {}).get(cls.KEY)
        values = raw if isinstance(raw, dict) else {}
        try:
            max_concurrent = int(values.get("max_concurrent") or 1)
        except (TypeError, ValueError):
            max_concurrent = 1
        return DeploymentPolicy(
            webhook_enabled=bool(values.get("webhook_enabled", True)),
            allowed_branches=cls.patterns(values.get("allowed_branches")),
            ignored_branches=cls.patterns(values.get("ignored_branches")),
            ignored_authors=cls.patterns(
                values.get("ignored_authors"), default=("dependabot[bot]",)
            ),
            skip_message_tokens=cls.patterns(
                values.get("skip_message_tokens"),
                default=("[skip layerrail]", "[skip deploy]", "[ci skip]"),
            ),
            max_concurrent=max(1, min(max_concurrent, 50)),
            supersede_older=bool(values.get("supersede_older", True)),
        )

    @staticmethod
    def patterns(value: object, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        if value is None:
            return default
        if isinstance(value, str):
            parts = value.replace(",", "\n").splitlines()
        elif isinstance(value, (list, tuple, set)):
            parts = [str(item) for item in value]
        else:
            return default
        cleaned = tuple(dict.fromkeys(part.strip() for part in parts if part.strip()))
        return cleaned[:50]

    @classmethod
    def update_project(
        cls,
        project: Project,
        *,
        webhook_enabled: bool,
        allowed_branches: object,
        ignored_branches: object,
        ignored_authors: object,
        skip_message_tokens: object,
        max_concurrent: object,
        supersede_older: bool,
    ) -> DeploymentPolicy:
        try:
            concurrent = int(max_concurrent)
        except (TypeError, ValueError) as exc:
            raise DeploymentPolicyError("Maximum concurrent deployments must be a number.") from exc
        if not 1 <= concurrent <= 50:
            raise DeploymentPolicyError("Maximum concurrent deployments must be between 1 and 50.")
        policy = DeploymentPolicy(
            webhook_enabled=bool(webhook_enabled),
            allowed_branches=cls.patterns(allowed_branches),
            ignored_branches=cls.patterns(ignored_branches),
            ignored_authors=cls.patterns(ignored_authors),
            skip_message_tokens=cls.patterns(skip_message_tokens),
            max_concurrent=concurrent,
            supersede_older=bool(supersede_older),
        )
        config = dict(project.config or {})
        config[cls.KEY] = policy.as_dict()
        project.config = config
        return policy

    @staticmethod
    async def active_count(
        db: AsyncSession,
        *,
        project_id: str,
        environment_id: str,
        exclude_ids: tuple[str, ...] = (),
    ) -> int:
        query = select(func.count(Deployment.id)).where(
            Deployment.project_id == project_id,
            Deployment.environment_id == environment_id,
            Deployment.conclusion.is_(None),
            Deployment.status.in_(["prepare", "deploy", "finalize"]),
        )
        if exclude_ids:
            query = query.where(Deployment.id.notin_(exclude_ids))
        return int((await db.execute(query)).scalar_one())
