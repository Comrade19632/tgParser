from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .db import SessionLocal
from .models import Account, AccountStatus, Channel, ChannelAccessStatus, ChannelType, Post
from .telethon.account_service import TelethonConfigError
from telethon import errors

from .telethon_client import connected_client

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
    # Keep scope tight for v1 parser engine: only parse channels that are already accessible.
    return ch.access_status in {ChannelAccessStatus.active, ChannelAccessStatus.joined}


def _normalize_entity_ref(ch: Channel) -> str:
    """Normalize stored channel identifier into a Telethon-friendly entity reference.

    tgreact practice: prefer passing full link/username into get_entity (no manual ResolveUsername).

    Stored forms we may have in DB:
    - public: "fridaymark" / "@fridaymark" / "https://t.me/fridaymark"
    - private: invite hash "k_Z9..." or full invite link "https://t.me/+k_Z9..."
    """

    raw = (ch.identifier or "").strip()
    if not raw:
        return raw

    if ch.type == ChannelType.public:
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if raw.startswith("t.me/"):
            return "https://" + raw
        if raw.startswith("@"):  # ok
            return raw
        # bare username
        return f"https://t.me/{raw.lstrip('@')}"

    # private
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("t.me/"):
        return "https://" + raw
    if raw.startswith("+"):
        return f"https://t.me/{raw}"
    # assume it's an invite hash
    return f"https://t.me/+{raw}"


def _normalize_text(text: str | None) -> str:
    return (text or "").strip()


def _build_message_url(*, ch: Channel, entity, message_id: int) -> str:
    # Public channels: stable canonical URL.
    if ch.type == ChannelType.public:
        username = (getattr(entity, "username", None) or ch.identifier or "").lstrip("@").strip()
        if username:
            return f"https://t.me/{username}/{message_id}"

    # Private channels: best-effort.
    # t.me/c/<internal_id>/<message_id> works for many private channels/groups.
    ent_id = getattr(entity, "id", None)
    if isinstance(ent_id, int) and ent_id > 0:
        return f"https://t.me/c/{ent_id}/{message_id}"

    return ""


async def parse_new_posts_once() -> ParseSummary:
    """Parse new posts for all active channels, incrementally.

    v1 scope:
    - Select one ready account (no rotation yet; see task #146).
    - For each actionable channel:
      - fetch messages with id > cursor_message_id
      - persist (channel_id, message_id, original_url, published_at, text)
      - dedupe via unique constraint (ON CONFLICT DO NOTHING)
      - advance cursor_message_id to max fetched id

    NOTE: Join-request / pending approval is handled in a later task (#148).
    """

    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        accounts = list(
            db.execute(select(Account).order_by(Account.id.asc())).scalars()
        )
        ready_accounts = [a for a in accounts if _is_account_ready(a, now=now)]

        channels = list(
            db.execute(select(Channel).order_by(Channel.id.asc())).scalars()
        )
        actionable_channels = [c for c in channels if _channel_is_actionable(c)]

    summary = ParseSummary(channels_total=len(actionable_channels))

    if not actionable_channels:
        log.info("parser: no actionable channels")
        return summary

    if not ready_accounts:
        log.warning("parser: no ready accounts (active+authorized). channels=%s", len(actionable_channels))
        return ParseSummary(
            channels_total=len(actionable_channels),
            channels_checked=0,
            channels_skipped_no_account=len(actionable_channels),
            posts_inserted=0,
        )

    inserted_total = 0
    checked = 0

    # v1+: per-channel account selection (simple): try ready accounts until one can access/resolve.
    for ch in actionable_channels:
        checked += 1

        with SessionLocal() as db:
            db_ch = db.get(Channel, ch.id)
            if not db_ch:
                continue
            cursor = int(db_ch.cursor_message_id or 0)

        entity_ref = _normalize_entity_ref(db_ch)

        # Try accounts one by one.
        picked = None
        last_exc: Exception | None = None
        for acc in ready_accounts:
            try:
                async with connected_client(account=acc) as client:
                    if not await client.is_user_authorized():
                        continue
                    entity = await client.get_entity(entity_ref)
                    picked = (acc, client, entity)
                    break
            except errors.FloodError as e:
                # Freeze/ban-like situations can manifest as FROZEN_METHOD_INVALID.
                last_exc = e
                continue
            except Exception as e:
                last_exc = e
                continue

        if not picked:
            with SessionLocal() as db:
                db_ch = db.get(Channel, ch.id)
                if db_ch:
                    db_ch.last_error = f"Resolve/access failed: {type(last_exc).__name__}: {last_exc}" if last_exc else "Resolve/access failed"
                    db_ch.last_checked_at = now
                    db.commit()
            log.warning("parser: no eligible account for channel (id=%s ref=%s)", ch.id, entity_ref)
            continue

        acc, client, entity = picked

        with SessionLocal() as db:
            db_ch = db.get(Channel, ch.id)
            if not db_ch:
                continue
            cursor = int(db_ch.cursor_message_id or 0)

            try:

                        max_seen_id = cursor
                        rows = []

                        # If cursor is empty (new channel), avoid crawling full history.
                        # Fetch a small tail of latest messages and set cursor accordingly.
                        if cursor <= 0:
                            msg_iter = client.iter_messages(entity, limit=20)
                        else:
                            msg_iter = client.iter_messages(entity, min_id=cursor, reverse=True)

                        async for msg in msg_iter:
                            # iter_messages can return service messages; keep only real content.
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

                        # Persist and advance cursor.
                        inserted = 0
                        if rows:
                            stmt = insert(Post).values(rows)
                            stmt = stmt.on_conflict_do_nothing(
                                index_elements=[Post.channel_id, Post.message_id]
                            )
                            res = db.execute(stmt)
                            # SQLAlchemy doesn't reliably report rowcount for DO NOTHING; best-effort.
                            inserted = int(getattr(res, "rowcount", 0) or 0)

                        db_ch.cursor_message_id = max_seen_id if max_seen_id > cursor else cursor
                        db_ch.last_checked_at = now
                        db_ch.last_error = ""
                        db.commit()

                        inserted_total += inserted

                        log.info(
                            "parser: channel=%s type=%s ident=%s ref=%s cursor=%s->%s fetched=%s inserted~=%s",
                            db_ch.id,
                            db_ch.type,
                            db_ch.identifier,
                            entity_ref,
                            cursor,
                            db_ch.cursor_message_id,
                            len(rows),
                            inserted,
                        )

            except TelethonConfigError as e:
                log.warning("parser: telethon config error: %s", e)
                # If config is missing for the picked account, we can't proceed meaningfully.
                return ParseSummary(
                    channels_total=len(actionable_channels),
                    channels_checked=checked,
                    channels_skipped_no_account=0,
                    posts_inserted=inserted_total,
                )
            except Exception as e:
                with SessionLocal() as db:
                    db_ch = db.get(Channel, ch.id)
                    if db_ch:
                        db_ch.last_error = f"{type(e).__name__}: {e}"
                        db_ch.last_checked_at = now
                        db.commit()
                log.exception(
                    "parser: channel failed (id=%s ident=%s)",
                    ch.id,
                    ch.identifier,
                )

    return ParseSummary(
        channels_total=len(actionable_channels),
        channels_checked=checked,
        channels_skipped_no_account=0,
        posts_inserted=inserted_total,
    )
