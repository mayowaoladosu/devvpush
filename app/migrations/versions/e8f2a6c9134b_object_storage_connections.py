"""Object storage connections

Revision ID: e8f2a6c9134b
Revises: d4c8a19f2e63
Create Date: 2026-07-26 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f2a6c9134b"
down_revision: Union[str, Sequence[str], None] = "d4c8a19f2e63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


new_storage_type = sa.Enum(
    "database",
    "volume",
    "kv",
    "queue",
    "object",
    name="storage_type_new",
)
old_storage_type = sa.Enum(
    "database",
    "volume",
    "kv",
    "queue",
    name="storage_type_old",
)


def upgrade() -> None:
    new_storage_type.create(op.get_bind(), checkfirst=True)
    op.execute(
        """
        ALTER TABLE storage
        ALTER COLUMN type TYPE storage_type_new
        USING type::text::storage_type_new
        """
    )
    op.execute("DROP TYPE storage_type")
    op.execute("ALTER TYPE storage_type_new RENAME TO storage_type")
    op.add_column(
        "storage",
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
    )
    op.alter_column("storage_project", "mount_path", nullable=True)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM storage WHERE type::text = 'object') THEN
            RAISE EXCEPTION 'disconnect object storage before downgrading';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        UPDATE storage_project AS association
        SET mount_path = '/data/' || storage.type::text || '/' || storage.name
        FROM storage
        WHERE storage.id = association.storage_id
          AND association.mount_path IS NULL
        """
    )
    op.alter_column("storage_project", "mount_path", nullable=False)
    op.drop_column("storage", "credentials_encrypted")
    old_storage_type.create(op.get_bind(), checkfirst=True)
    op.execute(
        """
        ALTER TABLE storage
        ALTER COLUMN type TYPE storage_type_old
        USING type::text::storage_type_old
        """
    )
    op.execute("DROP TYPE storage_type")
    op.execute("ALTER TYPE storage_type_old RENAME TO storage_type")