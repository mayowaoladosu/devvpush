"""Add deployment worker diagnostics

Revision ID: a71c2b9d4e10
Revises: 4fe4c96ad3dd
Create Date: 2026-07-25 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a71c2b9d4e10"
down_revision: Union[str, Sequence[str], None] = "4fe4c96ad3dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deployment", sa.Column("worker_job_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "deployment", sa.Column("worker_phase", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "deployment",
        sa.Column("worker_attempt", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "deployment", sa.Column("worker_heartbeat_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_deployment_worker_job_id", "deployment", ["worker_job_id"], unique=False
    )
    op.create_index(
        "ix_deployment_worker_heartbeat_at",
        "deployment",
        ["worker_heartbeat_at"],
        unique=False,
    )

    op.create_table(
        "deployment_diagnostic",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployment.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deployment_diagnostic_deployment_id",
        "deployment_diagnostic",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_deployment_diagnostic_code",
        "deployment_diagnostic",
        ["code"],
        unique=False,
    )
    op.create_index(
        "ix_deployment_diagnostic_created_at",
        "deployment_diagnostic",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_deployment_diagnostic_deployment_created",
        "deployment_diagnostic",
        ["deployment_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deployment_diagnostic_deployment_created",
        table_name="deployment_diagnostic",
    )
    op.drop_index(
        "ix_deployment_diagnostic_created_at", table_name="deployment_diagnostic"
    )
    op.drop_index("ix_deployment_diagnostic_code", table_name="deployment_diagnostic")
    op.drop_index(
        "ix_deployment_diagnostic_deployment_id", table_name="deployment_diagnostic"
    )
    op.drop_table("deployment_diagnostic")

    op.drop_index("ix_deployment_worker_heartbeat_at", table_name="deployment")
    op.drop_index("ix_deployment_worker_job_id", table_name="deployment")
    op.drop_column("deployment", "worker_heartbeat_at")
    op.drop_column("deployment", "worker_attempt")
    op.drop_column("deployment", "worker_phase")
    op.drop_column("deployment", "worker_job_id")
