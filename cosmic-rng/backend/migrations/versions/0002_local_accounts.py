"""local email + password accounts

Adds the columns local accounts need and makes ``discord_id`` optional, since an
account now authenticates with either a password or Discord, not necessarily both.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 10:05:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(length=190), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "discord_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    # Usernames are a login identifier: unique regardless of case.
    op.create_index("uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.alter_column("users", "discord_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_logins")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "email")
