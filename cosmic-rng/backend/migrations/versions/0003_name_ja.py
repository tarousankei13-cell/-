"""Japanese display names on content

The English name stays primary and this is shown beside it, so players who do
not read English still know what an item is. Editable from the admin panel like
any other content field.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24 15:10:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("rarities", "items", "biomes", "equipment", "boosts", "recipes", "shops",
          "shop_items", "cosmetics", "quests", "achievements", "seasons")


def upgrade() -> None:
    for t in TABLES:
        op.add_column(t, sa.Column("name_ja", sa.String(length=128), server_default="", nullable=False))


def downgrade() -> None:
    for t in TABLES:
        op.drop_column(t, "name_ja")
