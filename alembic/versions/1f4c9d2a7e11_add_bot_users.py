"""Add bot users table

Revision ID: 1f4c9d2a7e11
Revises: 6c1e1b2a4d9f
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "1f4c9d2a7e11"
down_revision = "6c1e1b2a4d9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bot_users_telegram_user_id", "bot_users", ["telegram_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_bot_users_telegram_user_id", table_name="bot_users")
    op.drop_table("bot_users")
