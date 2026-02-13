"""Add indexes for posts lookup

Revision ID: 6c1e1b2a4d9f
Revises: 3b2b2e7b6c1a
Create Date: 2026-02-13

"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "6c1e1b2a4d9f"
down_revision = "3b2b2e7b6c1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # For dedupe / performance we keep:
    # - unique (channel_id, message_id) already exists
    # Additional indexes:
    op.create_index("ix_posts_original_url", "posts", ["original_url"], unique=False)
    op.create_index(
        "ix_posts_channel_published_at",
        "posts",
        ["channel_id", "published_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_posts_channel_published_at", table_name="posts")
    op.drop_index("ix_posts_original_url", table_name="posts")
