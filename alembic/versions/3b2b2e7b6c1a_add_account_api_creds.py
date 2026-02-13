"""Add api_id/api_hash to accounts

Revision ID: 3b2b2e7b6c1a
Revises: f85057a66807
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3b2b2e7b6c1a"
down_revision = "f85057a66807"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("api_id", sa.Integer(), nullable=True))
    op.add_column("accounts", sa.Column("api_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "api_hash")
    op.drop_column("accounts", "api_id")
