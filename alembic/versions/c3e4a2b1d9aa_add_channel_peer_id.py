"""add channel peer_id

Revision ID: c3e4a2b1d9aa
Revises: 1f4c9d2a7e11
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3e4a2b1d9aa"
down_revision = "1f4c9d2a7e11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("peer_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("channels", "peer_id")
