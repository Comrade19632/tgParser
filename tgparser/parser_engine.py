from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from telethon import errors

from .db import SessionLocal
from .models import Account, AccountStatus, Channel, ChannelAccessStatus, Post
from .notify import notify_admin, notify_team
from .telethon.account_service import TelethonConfigError
from .telethon.dialogs import get_entity_from_dialogs
from .telethon.join_service import ensure_joined
from .telethon.pool import TelethonClientPool
from .telethon.selector import (
    AccountChannelStatus,
    mark_account_used,
    pick_account_for_channel,
    upsert_membership,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseSummary:
    channels_total: int = 0
    channels_checked: int = 0
    channels_skipped_no_account: int = 0
    posts_inserted: int = 0


def _is_account_ready(acc: Account, *, now: datetime) -> bool:
    if not acc.is_active:
        return False
    if acc.status != AccountStatus.active:
        return False
    if acc.cooldown_until and acc.cooldown_until > now:
        return False
    if not acc.session_string:
        return False
    return True


def _channel_is_actionable(ch: Channel) -> bool:
    if not ch.is_active:
        return False
    # v1 rule: we still *try* to ensure_joined before parsing.
    # Hard skip only if we already know it's forbidden.
    if ch.access_status == ChannelAccessStatus.forbidden:
        return False
    return True


def _normalize_text(text: str | None) -> str:
    return (text or "").strip()


def _build_message_url(*, ch: Channel, entity, message_id: int) -> str:
    # Public channels: stable canonical URL.
    username = (getattr(entity, "username", None) or ch.identifier or "").lstrip("@").strip()
    if username:
        return f"https://t.me/{username}/{message_id}"

    # Private channels: best-effort.
    ent_id = getattr(entity, "id", None)
    if isinstance(ent_id, int) and ent_id > 0:
        return f"https://t.me/c/{ent_id}/{message_id}"

    return ""


async def parse_new_posts_once() -> ParseSummary:
    """Parse new posts for all active channels, incrementally."""

    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        channels = list(db.execute(select(Channel).order_by(Channel.id.asc())).scalars())
        actionable_channels = [c for c in channels if _channel_is_actionable(c)]

    summary = ParseSummary(channels_total=len(actionable_channels))

    if not actionable_channels:
        log.info("parser: no actionable channels")
        return summary

    # Accounts are selected per-channel (rotation + membership-aware).

    inserted_total = 0
    checked = 0

    pool = TelethonClientPool()

    for ch in actionable_channels:
        checked += 1

        last_exc: Exception | None = None
        parsed = False

        exclude: set[int] = set()
        attempts = 0

        while attempts < 8:
            attempts += 1
            pick = pick_account_for_channel(ch=ch, exclude_account_ids=exclude)
            acc = pick.account
            if acc is None:
                break

            try:
                async with pool.connected(account=acc) as client:
                    if not await client.is_user_authorized():
                        exclude.add(acc.id)
                        continue

                    # 1) Prefer dialogs entity (membership-aware) to avoid resolve username.
                    with SessionLocal() as db:
                        db_ch = db.get(Channel, ch.id)
                        if not db_ch:
                            parsed = True
                            break

                    entity = await get_entity_from_dialogs(client=client, ch=db_ch)

                    # 2) If not found in dialogs, try to join (public: JoinChannel, private: ImportChatInvite).
                    if entity is None and db_ch.access_status not in {
                        ChannelAccessStatus.joined,
                        ChannelAccessStatus.active,
                    }:
                        join_res = await ensure_joined(client=client, ch=db_ch)

                        # Track per-account membership state for selector.
                        if join_res.access_status == ChannelAccessStatus.joined:
                            upsert_membership(
                                account_id=acc.id,
                                channel_id=ch.id,
                                status=AccountChannelStatus.joined,
                                note=join_res.note,
                                now=now,
                            )
                        elif join_res.access_status == ChannelAccessStatus.pending_approval:
                            upsert_membership(
                                account_id=acc.id,
                                channel_id=ch.id,
                                status=AccountChannelStatus.pending_approval,
                                note=join_res.note,
                                now=now,
                            )
                        elif join_res.access_status == ChannelAccessStatus.forbidden:
                            upsert_membership(
                                account_id=acc.id,
                                channel_id=ch.id,
                                status=AccountChannelStatus.forbidden,
                                note=join_res.note,
                                now=now,
                            )
                        elif join_res.access_status == ChannelAccessStatus.error:
                            upsert_membership(
                                account_id=acc.id,
                                channel_id=ch.id,
                                status=AccountChannelStatus.error,
                                note=join_res.note,
                                now=now,
                            )

                        with SessionLocal() as db:
                            ch2 = db.get(Channel, ch.id)
                            if ch2:
                                ch2.last_checked_at = now
                                if join_res.access_status is not None:
                                    ch2.access_status = join_res.access_status
                                ch2.last_error = join_res.note if not join_res.ok else ""

                                ent = join_res.entity
                                ent_id = getattr(ent, "id", None)
                                if isinstance(ent_id, int) and ent_id:
                                    ch2.peer_id = int(ent_id)
                                ent_title = getattr(ent, "title", None)
                                if isinstance(ent_title, str) and ent_title.strip():
                                    ch2.title = ent_title.strip()

                                db.commit()

                        if join_res.ok:
                            # Try dialogs again after joining.
                            with SessionLocal() as db:
                                db_ch = db.get(Channel, ch.id)
                            if db_ch:
                                entity = join_res.entity or await get_entity_from_dialogs(client=client, ch=db_ch)

                    if entity is None:
                        # Not parsable for this account; continue to next account.
                        continue

                    # 3) Parse posts.
                    with SessionLocal() as db:
                        db_ch = db.get(Channel, ch.id)
                        if not db_ch:
                            parsed = True
                            break

                        # If entity exists, channel is accessible.
                        if db_ch.access_status not in {ChannelAccessStatus.active, ChannelAccessStatus.joined}:
                            db_ch.access_status = ChannelAccessStatus.joined

                        cursor = int(db_ch.cursor_message_id or 0)
                        max_seen_id = cursor
                        rows: list[dict] = []

                        if cursor <= 0:
                            msg_iter = client.iter_messages(entity, limit=20)
                        else:
                            msg_iter = client.iter_messages(entity, min_id=cursor, reverse=True)

                        async for msg in msg_iter:
                            text = _normalize_text(getattr(msg, "message", None))
                            if not text:
                                continue

                            mid = int(getattr(msg, "id", 0) or 0)
                            if mid <= cursor:
                                continue

                            max_seen_id = max(max_seen_id, mid)

                            published_at = getattr(msg, "date", None)
                            if not isinstance(published_at, datetime):
                                published_at = now
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=timezone.utc)

                            rows.append(
                                {
                                    "channel_id": db_ch.id,
                                    "message_id": mid,
                                    "original_url": _build_message_url(ch=db_ch, entity=entity, message_id=mid),
                                    "published_at": published_at,
                                    "text": text,
                                    "created_at": now,
                                }
                            )

                        inserted = 0
                        if rows:
                            # NOTE: psycopg3 may report rowcount=-1 for INSERT .. ON CONFLICT.
                            # Use RETURNING to get an accurate inserted count (length of returned ids).
                            stmt = insert(Post).values(rows)
                            stmt = stmt.on_conflict_do_nothing(index_elements=[Post.channel_id, Post.message_id])
                            stmt = stmt.returning(Post.id)
                            res = db.execute(stmt)
                            inserted = len(res.fetchall())

                        db_ch.cursor_message_id = max_seen_id if max_seen_id > cursor else cursor
                        db_ch.last_checked_at = now
                        db_ch.last_error = ""
                        db.commit()

                        inserted_total += inserted

                        log.info(
                            "parser: channel=%s ident=%s cursor=%s->%s fetched=%s inserted~=%s account_id=%s",
                            db_ch.id,
                            db_ch.identifier,
                            cursor,
                            db_ch.cursor_message_id,
                            len(rows),
                            inserted,
                            acc.id,
                        )

                    # Evidence for routing: this account successfully accessed the channel.
                    upsert_membership(
                        account_id=acc.id,
                        channel_id=ch.id,
                        status=AccountChannelStatus.joined,
                        note="parsed_ok",
                        now=now,
                    )
                    mark_account_used(account_id=acc.id, now=now)

                    parsed = True
                    break

            except TelethonConfigError as e:
                log.warning("parser: telethon config error: %s", e)
                return ParseSummary(
                    channels_total=len(actionable_channels),
                    channels_checked=checked,
                    channels_skipped_no_account=0,
                    posts_inserted=inserted_total,
                )
            except errors.FloodError as e:
                last_exc = e
                exclude.add(acc.id)
                if "FROZEN_METHOD_INVALID" in str(e):
                    with SessionLocal() as db:
                        db_acc = db.get(Account, acc.id)
                        if db_acc:
                            db_acc.status = AccountStatus.banned
                            db_acc.is_active = False
                            db_acc.last_error = f"Frozen: {e}"
                            db_acc.updated_at = now
                            db.commit()
                    log.warning("parser: quarantined frozen account id=%s", acc.id)
                    msg = (
                        f"⚠️ TG Parser: аккаунт заморожен (FROZEN_METHOD_INVALID). id={acc.id} phone={getattr(acc, 'phone_number', '') or ''}"
                    )
                    await notify_admin(msg)
                    await notify_team(msg)
                continue
            except Exception as e:
                last_exc = e
                exclude.add(acc.id)
                continue

        if not parsed:
            with SessionLocal() as db:
                db_ch = db.get(Channel, ch.id)
                if db_ch:
                    db_ch.last_error = (
                        f"Resolve/access failed: {type(last_exc).__name__}: {last_exc}" if last_exc else "Resolve/access failed"
                    )
                    db_ch.last_checked_at = now
                    db.commit()

            log.warning(
                "parser: no eligible account for channel (id=%s last_err=%s)",
                ch.id,
                f"{type(last_exc).__name__}: {last_exc}" if last_exc else "<none>",
            )
            continue

    return ParseSummary(
        channels_total=len(actionable_channels),
        channels_checked=checked,
        channels_skipped_no_account=0,
        posts_inserted=inserted_total,
    )
