"""add bot_users is_staff + notify_enabled

Revision ID: d4f5e6a7b8c9
Revises: c3e4a2b1d9aa
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d4f5e6a7b8c9"
down_revision = "c3e4a2b1d9aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_users", sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column(
        "bot_users", sa.Column("notify_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"))
    )
    # Drop server defaults to keep model-level defaults as source of truth.
    op.alter_column("bot_users", "is_staff", server_default=None)
    op.alter_column("bot_users", "notify_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("bot_users", "notify_enabled")
    op.drop_column("bot_users", "is_staff")
