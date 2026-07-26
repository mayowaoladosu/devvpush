"""Storage mount paths

Revision ID: d4c8a19f2e63
Revises: a71c2b9d4e10
Create Date: 2026-07-26 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4c8a19f2e63"
down_revision: Union[str, Sequence[str], None] = "a71c2b9d4e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "storage_project",
        sa.Column("mount_path", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE storage_project AS association
        SET mount_path = '/data/' || storage.type::text || '/' || storage.name
        FROM storage
        WHERE storage.id = association.storage_id
        """
    )
    op.alter_column("storage_project", "mount_path", nullable=False)


def downgrade() -> None:
    op.drop_column("storage_project", "mount_path")