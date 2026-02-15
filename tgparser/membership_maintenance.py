from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telethon import errors

from .db import SessionLocal
from .models import Account, AccountStatus, AccountChannelMembership, Channel, ChannelType
from .telethon.dialogs import get_entity_from_dialogs
from .telethon.join_service import ensure_joined
from .telethon.pool import TelethonClientPool
from .telethon.selector import AccountChannelStatus, upsert_membership

log = logging.getLogger(__name__)


# Backoff policy (v1): keep it simple and safe.
# - After we sent a join request (InviteRequestSentError), do NOT re-send it.
#   We only re-check dialogs periodically.
# - After a pending approval status, same behavior.
# - After errors, retry but not too often.
JOIN_REQUEST_DIALOGS_RECHECK_EVERY = timedelta(hours=6)
ERROR_RETRY_EVERY = timedelta(minutes=30)
JOINED_REFRESH_EVERY = timedelta(hours=24)


@dataclass(frozen=True)
class MembershipSummary:
    channels_total: int = 0
    channels_touched: int = 0
    memberships_updated: int = 0
    accounts_cooldown_marked: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _account_ready(acc: Account, *, now: datetime) -> bool:
    if not acc.is_active:
        return False
    if acc.status != AccountStatus.active:
        return False
    if acc.cooldown_until and acc.cooldown_until > now:
        return False
    if not (acc.session_string or "").strip():
        return False
    return True


def _parse_floodwait_seconds(note: str) -> int | None:
    # join_service formats like: "FloodWait 123s"
    if not note:
        return None
    if "FloodWait" not in note:
        return None
    parts = note.replace("FloodWait", "").strip().split("s", 1)
    try:
        return int(parts[0].strip())
    except Exception:
        return None


def _mark_account_cooldown(*, account_id: int, seconds: int, note: str, now: datetime) -> None:
    seconds = max(0, int(seconds or 0))
    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            return
        acc.status = AccountStatus.cooldown
        acc.cooldown_until = now + timedelta(seconds=seconds)
        acc.last_error = (note or "")[:5000]
        acc.updated_at = now
        db.commit()


def _should_recheck(last_checked_at: datetime | None, *, every: timedelta, now: datetime) -> bool:
    if last_checked_at is None:
        return True
    return last_checked_at + every <= now


async def ensure_membership_once(*, max_channels: int = 50) -> MembershipSummary:
    """Best-effort membership maintenance.

    Goal: proactively keep account/channel membership in sync so parsing doesn't waste ticks
    on join attempts and "not in dialogs" edge-cases.

    Policy (v1):
    - Maintain membership for PRIVATE and PUBLIC channels (to reduce get_entity resolves by relying on dialogs cache).
    - For join_requested/pending_approval, never re-send invites; only re-check dialogs.
    - Respect account cooldown and mark cooldown on FloodWait.
    - Keep work bounded per tick.
    """

    now = _now()

    with SessionLocal() as db:
        channels = list(
            db.execute(
                select(Channel)
                .where(Channel.is_active.is_(True))
                .order_by(Channel.id.asc())
            ).scalars()
        )

        accounts = list(
            db.execute(
                select(Account)
                .where(Account.is_active.is_(True))
                .order_by(Account.id.asc())
            ).scalars()
        )

    # Include public channels too: by joining once we can often resolve entity via dialogs.
    channels = channels[: max(0, int(max_channels))]

    summary = MembershipSummary(channels_total=len(channels))
    if not channels or not accounts:
        return summary

    pool = TelethonClientPool()

    memberships_updated = 0
    cooldown_marked = 0
    touched = 0

    # Simple deterministic selection: for each channel pick the first ready account.
    # (Selector already does smarter rotation during parsing.)
    for ch in channels:
        acc = next((a for a in accounts if _account_ready(a, now=now)), None)
        if acc is None:
            continue

        touched += 1

        with SessionLocal() as db:
            mem = db.execute(
                select(AccountChannelMembership)
                .where(
                    AccountChannelMembership.account_id == acc.id,
                    AccountChannelMembership.channel_id == ch.id,
                )
            ).scalars().first()

            mem_status = mem.status if mem else AccountChannelStatus.unknown
            mem_last_checked = mem.last_checked_at if mem else None

            any_pending = db.execute(
                select(AccountChannelMembership.id)
                .where(
                    AccountChannelMembership.channel_id == ch.id,
                    AccountChannelMembership.status.in_(
                        [
                            AccountChannelStatus.join_requested,
                            AccountChannelStatus.pending_approval,
                        ]
                    ),
                )
                .limit(1)
            ).first()

        # 1) For join_requested/pending_approval: only re-check dialogs with backoff.
        if mem_status in {AccountChannelStatus.join_requested, AccountChannelStatus.pending_approval}:
            if not _should_recheck(mem_last_checked, every=JOIN_REQUEST_DIALOGS_RECHECK_EVERY, now=now):
                continue

            try:
                async with pool.connected(account=acc) as client:
                    entity = await get_entity_from_dialogs(client=client, ch=ch)
                if entity is not None:
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.joined,
                        note="entity found in dialogs (approved)",
                        now=now,
                    )
                    memberships_updated += 1
            except errors.FloodWaitError as e:
                _mark_account_cooldown(
                    account_id=acc.id,
                    seconds=int(getattr(e, "seconds", 0) or 0),
                    note=f"FloodWait {getattr(e, 'seconds', 0)}s during dialogs recheck",
                    now=now,
                )
                cooldown_marked += 1
            except Exception:
                log.exception("membership: dialogs recheck failed channel_id=%s account_id=%s", ch.id, acc.id)
            continue

        # 2) Joined: occasionally refresh dialogs visibility (cheap sanity check).
        if mem_status == AccountChannelStatus.joined:
            if not _should_recheck(mem_last_checked, every=JOINED_REFRESH_EVERY, now=now):
                continue
            try:
                async with pool.connected(account=acc) as client:
                    entity = await get_entity_from_dialogs(client=client, ch=ch)
                if entity is None:
                    # Membership drift; keep status but record note.
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.error,
                        note="joined previously but missing from dialogs",
                        now=now,
                    )
                    memberships_updated += 1
            except Exception:
                log.exception("membership: joined refresh failed channel_id=%s account_id=%s", ch.id, acc.id)
            continue

        # 3) Forbidden: do nothing.
        if mem_status == AccountChannelStatus.forbidden:
            continue

        # 4) Unknown/Error: try to ensure_joined, but do not spam if someone is already pending.
        if mem_status == AccountChannelStatus.error:
            if not _should_recheck(mem_last_checked, every=ERROR_RETRY_EVERY, now=now):
                continue

        if any_pending:
            # Guardrail: one pending request per channel.
            continue

        try:
            async with pool.connected(account=acc) as client:
                if not await client.is_user_authorized():
                    # Mark account as requiring re-auth so selector stops picking it.
                    with SessionLocal() as db:
                        db_acc = db.get(Account, acc.id)
                        if db_acc:
                            db_acc.status = AccountStatus.auth_required
                            db_acc.last_error = "Telethon session is not authorized"
                            db_acc.cooldown_until = None
                            db_acc.updated_at = now
                            db.commit()
                    continue

                res = await ensure_joined(client=client, ch=ch)

            if res.access_status is not None:
                if res.access_status.value == "joined":
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.joined,
                        note=res.note,
                        now=now,
                    )
                    memberships_updated += 1
                elif res.access_status.value == "join_requested":
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.join_requested,
                        note=res.note,
                        now=now,
                    )
                    memberships_updated += 1
                elif res.access_status.value == "pending_approval":
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.pending_approval,
                        note=res.note,
                        now=now,
                    )
                    memberships_updated += 1
                elif res.access_status.value == "forbidden":
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.forbidden,
                        note=res.note,
                        now=now,
                    )
                    memberships_updated += 1
                else:
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.error,
                        note=res.note,
                        now=now,
                    )
                    memberships_updated += 1

            fw = _parse_floodwait_seconds(res.note)
            if fw is not None:
                _mark_account_cooldown(account_id=acc.id, seconds=fw, note=res.note, now=now)
                cooldown_marked += 1

        except errors.FloodWaitError as e:
            _mark_account_cooldown(
                account_id=acc.id,
                seconds=int(getattr(e, "seconds", 0) or 0),
                note=f"FloodWait {getattr(e, 'seconds', 0)}s during ensure_joined",
                now=now,
            )
            cooldown_marked += 1
        except Exception:
            log.exception("membership: ensure_joined failed channel_id=%s account_id=%s", ch.id, acc.id)

    return MembershipSummary(
        channels_total=summary.channels_total,
        channels_touched=touched,
        memberships_updated=memberships_updated,
        accounts_cooldown_marked=cooldown_marked,
    )
