"""merge alembic heads (memberships + prior)

Revision ID: 5086e87bc00f
Revises: d4f5e6a7b8c9, e2a6c9b1a7f0
Create Date: 2026-02-14

"""

from __future__ import annotations


# revision identifiers, used by Alembic.
revision = "5086e87bc00f"
down_revision = ("d4f5e6a7b8c9", "e2a6c9b1a7f0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge revision: no schema changes.
    pass


def downgrade() -> None:
    # Merge revision: no schema changes.
    pass
