"""Cloudinary media provider

Revision ID: a91c5d7e4b20
Revises: e8f2a6c9134b
Create Date: 2026-07-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a91c5d7e4b20"
down_revision: Union[str, Sequence[str], None] = "e8f2a6c9134b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


new_storage_type = sa.Enum(
    "database",
    "volume",
    "kv",
    "queue",
    "object",
    "media",
    name="storage_type_new",
)
old_storage_type = sa.Enum(
    "database",
    "volume",
    "kv",
    "queue",
    "object",
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


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM storage WHERE type::text = 'media') THEN
            RAISE EXCEPTION 'disconnect media providers before downgrading';
          END IF;
        END
        $$
        """
    )
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
