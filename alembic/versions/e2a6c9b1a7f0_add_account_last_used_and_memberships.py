"""add account last_used_at + account_channel_memberships

Revision ID: e2a6c9b1a7f0
Revises: 6c1e1b2a4d9f
Create Date: 2026-02-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "e2a6c9b1a7f0"
down_revision = "6c1e1b2a4d9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "account_channel_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "unknown",
                "join_requested",
                "pending_approval",
                "joined",
                "forbidden",
                "error",
                name="accountchannelstatus",
            ),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("join_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forbidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "channel_id", name="uq_account_channel"),
    )

    op.create_index("ix_account_channel_memberships_account_id", "account_channel_memberships", ["account_id"])
    op.create_index("ix_account_channel_memberships_channel_id", "account_channel_memberships", ["channel_id"])


def downgrade() -> None:
    op.drop_index("ix_account_channel_memberships_channel_id", table_name="account_channel_memberships")
    op.drop_index("ix_account_channel_memberships_account_id", table_name="account_channel_memberships")
    op.drop_table("account_channel_memberships")

    # Enum cleanup (best-effort; some DBs require CASCADE).
    op.execute("DROP TYPE IF EXISTS accountchannelstatus")

    op.drop_column("accounts", "last_used_at")
