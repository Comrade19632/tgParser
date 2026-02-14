from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from telethon import errors

from .db import SessionLocal
from .models import (
    Account,
    AccountChannelMembership,
    AccountStatus,
    Channel,
    ChannelAccessStatus,
    ChannelType,
    Post,
)
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


def _mark_account_cooldown(*, account_id: int, seconds: int, note: str, now: datetime) -> None:
    """Persist FloodWait cooldown for the account.

    Selector respects cooldown_until, so this prevents the account from being
    picked again within the same tick.
    """

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


def _quarantine_account(*, account_id: int, status: AccountStatus, note: str, now: datetime) -> None:
    """Quarantine account so selector stops picking it.

    Policy: set status to banned/forbidden and soft-disable is_active.
    """

    with SessionLocal() as db:
        acc = db.get(Account, account_id)
        if not acc:
            return

        acc.status = status
        acc.is_active = False
        acc.cooldown_until = None
        acc.last_error = (note or "")[:5000]
        acc.updated_at = now
        db.commit()


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

            log.info(
                "selector: channel_id=%s channel_type=%s picked_account_id=%s reason=%s",
                ch.id,
                ch.type,
                acc.id,
                pick.reason,
            )

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

                    # NOTE: get_dialogs is rate-limited aggressively. For public channels we
                    # prefer direct username resolve first; dialogs lookup is mainly useful for
                    # private channels where membership already exists.
                    entity = None
                    if db_ch.type != ChannelType.public:
                        entity = await get_entity_from_dialogs(client=client, ch=db_ch)

                    # If the entity is already visible in dialogs, treat this (account,channel)
                    # as joined for selector purposes (e.g. after a private join request was approved).
                    if entity is not None:
                        upsert_membership(
                            account_id=acc.id,
                            channel_id=ch.id,
                            status=AccountChannelStatus.joined,
                            note="entity found in dialogs",
                            now=now,
                        )

                    # 1.1) If not in dialogs, try to resolve entity directly.
                    # For public channels this should work even without membership.
                    if entity is None:
                        try:
                            if db_ch.type == ChannelType.public:
                                ident = (db_ch.identifier or "").strip()
                                if ident:
                                    entity = await client.get_entity(ident)
                            else:
                                # Private: best-effort by numeric peer id if we have it.
                                if isinstance(db_ch.peer_id, int) and db_ch.peer_id:
                                    entity = await client.get_entity(int(db_ch.peer_id))
                        except Exception:
                            entity = None

                    # 2) If still not found, try to join (public: JoinChannel, private: ImportChatInvite).
                    # Avoid spamming join requests: if we already requested/pending for this account,
                    # do not call ImportChatInvite again.
                    # NOTE: For v1 we only attempt joining for private channels.
                    # Private channels are identified by invite hash; dialogs lookup requires peer_id.
                    # If entity is missing we must attempt ensure_joined even if access_status was already
                    # marked active/joined (e.g. channel added earlier but peer_id wasn't captured yet).
                    if entity is None and db_ch.type == ChannelType.private:
                        with SessionLocal() as db:
                            m = db.execute(
                                select(AccountChannelMembership.status).where(
                                    AccountChannelMembership.account_id == acc.id,
                                    AccountChannelMembership.channel_id == ch.id,
                                )
                            ).first()
                            mem_status = m[0] if m else None

                        if mem_status in {AccountChannelStatus.join_requested, AccountChannelStatus.pending_approval}:
                            exclude.add(acc.id)
                            continue

                        # Guardrail: never send a second join request if ANY account already has
                        # a pending/join_requested membership for this channel.
                        with SessionLocal() as db:
                            any_pending = db.execute(
                                select(AccountChannelMembership.id).where(
                                    AccountChannelMembership.channel_id == ch.id,
                                    AccountChannelMembership.status.in_(
                                        [
                                            AccountChannelStatus.join_requested,
                                            AccountChannelStatus.pending_approval,
                                        ]
                                    ),
                                )
                            ).first()

                        if any_pending:
                            log.info(
                                "join_guardrail: channel_id=%s skip_join account_id=%s reason=other_account_pending",
                                ch.id,
                                acc.id,
                            )
                            exclude.add(acc.id)
                            continue

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
                        elif join_res.access_status == ChannelAccessStatus.join_requested:
                            upsert_membership(
                                account_id=acc.id,
                                channel_id=ch.id,
                                status=AccountChannelStatus.join_requested,
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
                        # Not parsable for this account in this tick; exclude it so selector doesn't
                        # re-pick the same account in a tight loop (common when there is only one).
                        exclude.add(acc.id)
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

                        # Safety: if cursor is set but DB has no posts yet (e.g. previous failed run
                        # advanced cursor without inserts), treat as first-parse to avoid permanent
                        # "0 inserted" loops.
                        if cursor > 0:
                            any_post = db.execute(
                                select(Post.id).where(Post.channel_id == db_ch.id).limit(1)
                            ).first()
                            if not any_post:
                                cursor = 0

                        max_seen_id = cursor
                        rows: list[dict] = []

                        backfill_days = max(0, int(db_ch.backfill_days or 0))
                        backfill_since: datetime | None = None

                        if cursor <= 0 and backfill_days > 0:
                            # First parse for a channel: backfill up to N days of history.
                            # We bound the total amount to avoid infinite history walks.
                            backfill_since = now - timedelta(days=backfill_days)
                            msg_iter = client.iter_messages(entity, limit=2000)
                        elif cursor <= 0:
                            # Default first parse when backfill is disabled.
                            msg_iter = client.iter_messages(entity, limit=20)
                        else:
                            # Incremental: fetch messages after the cursor.
                            msg_iter = client.iter_messages(entity, min_id=cursor, reverse=True)

                        async for msg in msg_iter:
                            published_at = getattr(msg, "date", None)
                            if not isinstance(published_at, datetime):
                                published_at = now
                            if published_at.tzinfo is None:
                                published_at = published_at.replace(tzinfo=timezone.utc)

                            if backfill_since is not None and published_at < backfill_since:
                                # Backfill mode: stop once we reached older than the threshold.
                                break

                            text = _normalize_text(getattr(msg, "message", None))
                            if not text:
                                continue

                            mid = int(getattr(msg, "id", 0) or 0)
                            if mid <= cursor:
                                continue

                            max_seen_id = max(max_seen_id, mid)

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

                        mode = "backfill" if cursor <= 0 and backfill_since is not None else "incremental"
                        log.info(
                            "parser: mode=%s channel=%s ident=%s cursor=%s->%s fetched=%s inserted=%s account_id=%s",
                            mode,
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
            except errors.FloodWaitError as e:
                # Rate limit on this account. Persist cooldown and continue with other accounts.
                last_exc = e
                exclude.add(acc.id)

                seconds = int(getattr(e, "seconds", 0) or 0)
                _mark_account_cooldown(
                    account_id=acc.id,
                    seconds=seconds,
                    note=f"FloodWait {seconds}s",
                    now=now,
                )

                log.warning("parser: floodwait account_id=%s seconds=%s", acc.id, seconds)
                continue
            except (
                errors.PhoneNumberBannedError,
                errors.UserDeactivatedBanError,
            ) as e:
                # Account-level ban. Quarantine account and continue with others.
                last_exc = e
                exclude.add(acc.id)
                _quarantine_account(account_id=acc.id, status=AccountStatus.banned, note=f"Banned: {e}", now=now)
                log.warning("parser: quarantined banned account id=%s", acc.id)
                msg = (
                    f"⚠️ TG Parser: аккаунт забанен/деактивирован. id={acc.id} phone={getattr(acc, 'phone_number', '') or ''} err={type(e).__name__}"
                )
                await notify_admin(msg)
                await notify_team(msg)
                continue
            except (
                errors.UserDeactivatedError,
            ) as e:
                # Restricted/forbidden account state (soft quarantine).
                last_exc = e
                exclude.add(acc.id)
                _quarantine_account(account_id=acc.id, status=AccountStatus.forbidden, note=f"Forbidden: {e}", now=now)
                log.warning("parser: quarantined forbidden account id=%s", acc.id)
                msg = (
                    f"⚠️ TG Parser: аккаунт ограничен (forbidden). id={acc.id} phone={getattr(acc, 'phone_number', '') or ''}"
                )
                await notify_admin(msg)
                await notify_team(msg)
                continue
            except (
                errors.ChannelPrivateError,
                errors.ChatAdminRequiredError,
                errors.UserBannedInChannelError,
                errors.UserNotParticipantError,
                errors.ChatWriteForbiddenError,
            ) as e:
                # Channel-level forbidden for this account; mark membership forbidden and try other accounts.
                last_exc = e
                exclude.add(acc.id)
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.forbidden,
                    note=f"forbidden: {type(e).__name__}: {e}",
                    now=now,
                )
                continue
            except errors.FloodError as e:
                last_exc = e
                exclude.add(acc.id)
                if "FROZEN_METHOD_INVALID" in str(e):
                    _quarantine_account(
                        account_id=acc.id,
                        status=AccountStatus.banned,
                        note=f"Frozen: {e}",
                        now=now,
                    )
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
            forbidden_exc_types = (
                errors.ChannelPrivateError,
                errors.ChatAdminRequiredError,
                errors.UserBannedInChannelError,
                errors.UserNotParticipantError,
                errors.ChatWriteForbiddenError,
            )

            with SessionLocal() as db:
                db_ch = db.get(Channel, ch.id)
                if db_ch:
                    if isinstance(last_exc, forbidden_exc_types):
                        db_ch.access_status = ChannelAccessStatus.forbidden

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
