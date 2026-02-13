"""Add is_active to channels

Revision ID: b7b0d1c3a4ef
Revises: 9a1f6f0c2d3e
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7b0d1c3a4ef"
down_revision = "9a1f6f0c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.alter_column("channels", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("channels", "is_active")
