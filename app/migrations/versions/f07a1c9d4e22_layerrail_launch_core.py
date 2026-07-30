"""LayerRail launch core

Revision ID: f07a1c9d4e22
Revises: c4e8f1a2b730
Create Date: 2026-07-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f07a1c9d4e22"
down_revision: Union[str, Sequence[str], None] = "c4e8f1a2b730"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


webhook_endpoint_status = postgresql.ENUM(
    "active",
    "disabled",
    name="webhook_endpoint_status",
    create_type=False,
)
webhook_delivery_status = postgresql.ENUM(
    "pending",
    "delivered",
    "failed",
    name="webhook_delivery_status",
    create_type=False,
)


def upgrade() -> None:
    webhook_endpoint_status.create(op.get_bind(), checkfirst=True)
    webhook_delivery_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "api_token",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_api_token_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_api_token_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_token")),
    )
    op.create_index(op.f("ix_api_token_team_id"), "api_token", ["team_id"])
    op.create_index(op.f("ix_api_token_prefix"), "api_token", ["prefix"])
    op.create_index(
        op.f("ix_api_token_token_hash"),
        "api_token",
        ["token_hash"],
        unique=True,
    )
    op.create_index(op.f("ix_api_token_expires_at"), "api_token", ["expires_at"])
    op.create_index(op.f("ix_api_token_revoked_at"), "api_token", ["revoked_at"])
    op.create_index(op.f("ix_api_token_created_at"), "api_token", ["created_at"])

    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("api_token_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name=op.f("fk_audit_event_actor_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["api_token_id"],
            ["api_token.id"],
            name=op.f("fk_audit_event_api_token_id_api_token"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_audit_event_team_id_team"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    for column in (
        "team_id",
        "actor_user_id",
        "api_token_id",
        "action",
        "resource_type",
        "created_at",
    ):
        op.create_index(op.f(f"ix_audit_event_{column}"), "audit_event", [column])

    op.create_table(
        "notification_settings",
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("deployment_succeeded", sa.Boolean(), nullable=False),
        sa.Column("deployment_failed", sa.Boolean(), nullable=False),
        sa.Column("deployment_canceled", sa.Boolean(), nullable=False),
        sa.Column(
            "recipients",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_notification_settings_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", name=op.f("pk_notification_settings")),
    )

    op.create_table(
        "webhook_endpoint",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("status", webhook_endpoint_status, nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_webhook_endpoint_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_webhook_endpoint_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_endpoint")),
    )
    op.create_index(
        op.f("ix_webhook_endpoint_team_id"), "webhook_endpoint", ["team_id"]
    )
    op.create_index(
        op.f("ix_webhook_endpoint_status"), "webhook_endpoint", ["status"]
    )
    op.create_index(
        op.f("ix_webhook_endpoint_created_at"), "webhook_endpoint", ["created_at"]
    )

    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("endpoint_id", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", webhook_delivery_status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoint.id"],
            name=op.f("fk_webhook_delivery_endpoint_id_webhook_endpoint"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_delivery")),
    )
    op.create_index(
        op.f("ix_webhook_delivery_endpoint_id"),
        "webhook_delivery",
        ["endpoint_id"],
    )
    op.create_index(
        op.f("ix_webhook_delivery_event"), "webhook_delivery", ["event"]
    )
    op.create_index(
        op.f("ix_webhook_delivery_status"), "webhook_delivery", ["status"]
    )
    op.create_index(
        op.f("ix_webhook_delivery_created_at"),
        "webhook_delivery",
        ["created_at"],
    )
    op.create_index(
        "ix_webhook_delivery_endpoint_created",
        "webhook_delivery",
        ["endpoint_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_delivery_endpoint_created", table_name="webhook_delivery"
    )
    for column in ("created_at", "status", "event", "endpoint_id"):
        op.drop_index(
            op.f(f"ix_webhook_delivery_{column}"), table_name="webhook_delivery"
        )
    op.drop_table("webhook_delivery")

    for column in ("created_at", "status", "team_id"):
        op.drop_index(
            op.f(f"ix_webhook_endpoint_{column}"), table_name="webhook_endpoint"
        )
    op.drop_table("webhook_endpoint")
    op.drop_table("notification_settings")

    for column in (
        "created_at",
        "resource_type",
        "action",
        "api_token_id",
        "actor_user_id",
        "team_id",
    ):
        op.drop_index(op.f(f"ix_audit_event_{column}"), table_name="audit_event")
    op.drop_table("audit_event")

    for column in (
        "created_at",
        "revoked_at",
        "expires_at",
        "token_hash",
        "prefix",
        "team_id",
    ):
        op.drop_index(op.f(f"ix_api_token_{column}"), table_name="api_token")
    op.drop_table("api_token")

    webhook_delivery_status.drop(op.get_bind(), checkfirst=True)
    webhook_endpoint_status.drop(op.get_bind(), checkfirst=True)
