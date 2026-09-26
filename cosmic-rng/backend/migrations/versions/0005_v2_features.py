"""v2: friends, guilds, weekly boards, season pass, sets, shards, prestige, login bonus

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26 00:10:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_stats", sa.Column("shards", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("user_stats", sa.Column("prestige", sa.Integer(), server_default="0", nullable=False))
    op.add_column("user_stats", sa.Column("login_streak", sa.Integer(), server_default="0", nullable=False))
    op.add_column("user_stats", sa.Column("login_cycle", sa.Integer(), server_default="0", nullable=False))
    op.add_column("user_stats", sa.Column("last_bonus_day", sa.String(length=10), server_default="", nullable=False))

    op.create_table(
        "friendships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("friend_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("user_id", "friend_id", name="uq_friendships_pair"),
    )
    op.create_index("ix_friendships_friend", "friendships", ["friend_id", "status"])
    op.create_index("ix_friendships_user", "friendships", ["user_id", "status"])

    op.create_table(
        "guilds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=24), nullable=False, unique=True),
        sa.Column("tag", sa.String(length=8), nullable=False, unique=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("is_open", sa.Boolean(), server_default=sa.text("1") if op.get_bind().dialect.name == "sqlite" else sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "guild_members",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=12), server_default="member", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_guild_members_guild", "guild_members", ["guild_id"])

    op.create_table(
        "weekly_stats",
        sa.Column("week", sa.String(length=10), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rolls", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("best_odds", sa.Double(), server_default="0", nullable=False),
        sa.Column("points", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_index("ix_weekly_rolls", "weekly_stats", ["week", "rolls"])
    op.create_index("ix_weekly_best", "weekly_stats", ["week", "best_odds"])

    op.create_table(
        "season_pass_claims",
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tier_idx", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "set_claims",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("set_key", sa.String(length=48), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def downgrade() -> None:
    for t in ("set_claims", "season_pass_claims", "weekly_stats", "guild_members", "guilds", "friendships"):
        op.drop_table(t)
    for c in ("last_bonus_day", "login_cycle", "login_streak", "prestige", "shards"):
        op.drop_column("user_stats", c)
