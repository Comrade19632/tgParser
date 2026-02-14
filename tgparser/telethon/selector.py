from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, or_, select

from ..db import SessionLocal
from ..models import (
    Account,
    AccountChannelMembership,
    AccountChannelStatus,
    AccountStatus,
    Channel,
    ChannelType,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PickResult:
    account: Account | None
    reason: str


def _is_ready_account_clause(*, now: datetime):
    return and_(
        Account.is_active.is_(True),
        Account.status == AccountStatus.active,
        or_(Account.cooldown_until.is_(None), Account.cooldown_until <= now),
        Account.session_string != "",
    )


def pick_account_for_channel(*, ch: Channel, exclude_account_ids: set[int] | None = None) -> PickResult:
    """Pick best account candidate for channel.

    Policy (v1):
    - Skip inactive/banned/cooldown accounts.
    - Prefer already joined accounts for private channels.
    - LRU: order by last_used_at ASC (NULLs first), then account.id.

    Returns only ONE account. Caller can retry with different policy if needed.
    """

    now = datetime.now(timezone.utc)

    exclude_account_ids = exclude_account_ids or set()

    with SessionLocal() as db:
        base = select(Account).where(_is_ready_account_clause(now=now))
        if exclude_account_ids:
            base = base.where(Account.id.notin_(exclude_account_ids))

        # For private channels, prefer joined memberships, but allow unknown to attempt join.
        if ch.type == ChannelType.private:
            m = AccountChannelMembership

            # LEFT JOIN membership for this channel.
            base = base.outerjoin(m, and_(m.account_id == Account.id, m.channel_id == ch.id))

            # Exclude forbidden memberships.
            base = base.where(or_(m.id.is_(None), m.status != AccountChannelStatus.forbidden))

            # Sort key: joined first, then pending/join_requested/unknown/error.
            joined_first = case((m.status == AccountChannelStatus.joined, 0), else_=1)
            base = base.order_by(joined_first.asc())

        # LRU rotation.
        base = base.order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc())

        acc = db.execute(base.limit(1)).scalars().first()
        if not acc:
            return PickResult(account=None, reason="no_ready_accounts")

        return PickResult(account=acc, reason="picked")


def mark_account_used(*, account_id: int, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            return
        acc.last_used_at = now
        acc.updated_at = now
        db.commit()


def upsert_membership(
    *,
    account_id: int,
    channel_id: int,
    status: AccountChannelStatus,
    note: str = "",
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)

    with SessionLocal() as db:
        existing = db.execute(
            select(AccountChannelMembership).where(
                AccountChannelMembership.account_id == account_id,
                AccountChannelMembership.channel_id == channel_id,
            )
        ).scalars().first()

        if existing is None:
            existing = AccountChannelMembership(account_id=account_id, channel_id=channel_id)
            db.add(existing)

        existing.status = status
        existing.note = (note or "")[:5000]
        existing.last_checked_at = now
        existing.updated_at = now

        if status == AccountChannelStatus.join_requested:
            existing.join_requested_at = existing.join_requested_at or now
        if status == AccountChannelStatus.pending_approval:
            existing.join_requested_at = existing.join_requested_at or now
        if status == AccountChannelStatus.joined:
            existing.joined_at = existing.joined_at or now
        if status == AccountChannelStatus.forbidden:
            existing.forbidden_at = existing.forbidden_at or now

        db.commit()
