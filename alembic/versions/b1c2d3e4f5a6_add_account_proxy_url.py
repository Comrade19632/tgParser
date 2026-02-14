"""add account proxy_url

Revision ID: b1c2d3e4f5a6
Revises: aa12bb34cc56
Create Date: 2026-02-14 14:37:00Z

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "aa12bb34cc56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("proxy_url", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("accounts", "proxy_url")
