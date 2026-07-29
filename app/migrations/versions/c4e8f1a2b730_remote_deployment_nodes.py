"""Remote deployment nodes

Revision ID: c4e8f1a2b730
Revises: a91c5d7e4b20
Create Date: 2026-07-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4e8f1a2b730"
down_revision: Union[str, Sequence[str], None] = "a91c5d7e4b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


node_status = postgresql.ENUM(
    "active",
    "draining",
    "disabled",
    "deleted",
    name="deployment_node_status",
    create_type=False,
)


def upgrade() -> None:
    node_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "deployment_node",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=False),
        sa.Column("runtime_host", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=63), nullable=False),
        sa.Column("max_deployments", sa.Integer(), nullable=False),
        sa.Column("status", node_status, nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "error",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_deployment_node_created_by_user_id_user"),
            ondelete="SET NULL",
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_node")),
    )
    op.create_index(
        op.f("ix_deployment_node_name"),
        "deployment_node",
        ["name"],
        unique=False,
    )
    op.create_index(
        "ix_deployment_node_name_lower",
        "deployment_node",
        [sa.literal_column("lower(name)")],
        unique=True,
    )
    op.create_index(
        op.f("ix_deployment_node_status"),
        "deployment_node",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployment_node_healthy"),
        "deployment_node",
        ["healthy"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployment_node_last_checked_at"),
        "deployment_node",
        ["last_checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployment_node_created_at"),
        "deployment_node",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployment_node_updated_at"),
        "deployment_node",
        ["updated_at"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_deployment_node_created_by_user_id_user"),
        "deployment_node",
        "user",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )
    op.add_column(
        "deployment",
        sa.Column("node_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "deployment",
        sa.Column("runtime_url", sa.String(length=2048), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_deployment_node_id_deployment_node"),
        "deployment",
        "deployment_node",
        ["node_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_deployment_node_id"),
        "deployment",
        ["node_id"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM deployment_node WHERE status::text != 'deleted')
             OR EXISTS (SELECT 1 FROM deployment WHERE node_id IS NOT NULL) THEN
            RAISE EXCEPTION 'remove remote deployment nodes before downgrading';
          END IF;
        END
        $$
        """
    )
    op.drop_index(op.f("ix_deployment_node_id"), table_name="deployment")
    op.drop_constraint(
        op.f("fk_deployment_node_id_deployment_node"),
        "deployment",
        type_="foreignkey",
    )
    op.drop_column("deployment", "runtime_url")
    op.drop_column("deployment", "node_id")
    op.drop_index(op.f("ix_deployment_node_updated_at"), table_name="deployment_node")
    op.drop_index(op.f("ix_deployment_node_created_at"), table_name="deployment_node")
    op.drop_index(
        op.f("ix_deployment_node_last_checked_at"), table_name="deployment_node"
    )
    op.drop_index(op.f("ix_deployment_node_healthy"), table_name="deployment_node")
    op.drop_index(op.f("ix_deployment_node_status"), table_name="deployment_node")
    op.drop_index("ix_deployment_node_name_lower", table_name="deployment_node")
    op.drop_index(op.f("ix_deployment_node_name"), table_name="deployment_node")
    op.drop_table("deployment_node")
    node_status.drop(op.get_bind(), checkfirst=True)
