"""Add phone_number + is_active to accounts

Revision ID: 9a1f6f0c2d3e
Revises: 6c1e1b2a4d9f
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9a1f6f0c2d3e"
down_revision = "6c1e1b2a4d9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("phone_number", sa.String(length=32), nullable=False, server_default=""))
    op.add_column("accounts", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))

    # Keep defaults at ORM level; remove server_default to avoid surprises for future inserts.
    op.alter_column("accounts", "phone_number", server_default=None)
    op.alter_column("accounts", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("accounts", "is_active")
    op.drop_column("accounts", "phone_number")
