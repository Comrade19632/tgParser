"""add AccountStatus.forbidden

Revision ID: aa12bb34cc56
Revises: 5086e87bc00f
Create Date: 2026-02-14 03:18:00Z

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "aa12bb34cc56"
down_revision = "5086e87bc00f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres enum alteration is non-transactional in older versions.
    # Use a DO block to keep it idempotent.
    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'accountstatus' AND e.enumlabel = 'forbidden'
    ) THEN
        ALTER TYPE accountstatus ADD VALUE 'forbidden';
    END IF;
END$$;
"""
    )


def downgrade() -> None:
    # Downgrading a Postgres enum value is not supported safely.
    # If needed, recreate the enum type manually.
    pass
