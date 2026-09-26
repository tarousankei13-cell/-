"""Japanese names for procedural item parts

Generated items compose their name from parts, so a Japanese name for each part
is what gives every generated item a Japanese name.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25 15:40:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("item_parts", sa.Column("name_ja", sa.String(length=64), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("item_parts", "name_ja")
