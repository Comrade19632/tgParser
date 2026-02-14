from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ...db import SessionLocal
from ...models import Channel, ChannelAccessStatus, ChannelType
from ...telethon.dialogs import get_entity_from_dialogs
from ...telethon.join_service import ensure_joined
from ...telethon.pool import TelethonClientPool
from ...telethon.selector import (
    AccountChannelStatus,
    pick_account_for_channel,
    upsert_membership,
)
from .. import callbacks as cb

log = logging.getLogger(__name__)

router = Router()


def channels_actions_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Добавить public", callback_data=cb.CH_ADD_PUBLIC)
    kb.button(text="Добавить private", callback_data=cb.CH_ADD_PRIVATE)
    kb.button(text="Список", callback_data=cb.CH_LIST)
    kb.adjust(2, 1)
    kb.button(text="← Назад", callback_data=cb.MAIN)
    kb.button(text="Обновить", callback_data=cb.CHANNELS)
    kb.adjust(2, 2)
    return kb


def channel_row_kb(*, channel_id: int, is_active: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if is_active:
        kb.button(text="Disable", callback_data=f"{cb.CH_DISABLE}:{channel_id}")
    else:
        kb.button(text="Enable", callback_data=f"{cb.CH_ENABLE}:{channel_id}")
    kb.adjust(1)
    return kb


class AddChannelFlow(StatesGroup):
    identifier = State()
    backfill_days = State()


@dataclass(frozen=True)
class PendingChannel:
    type: ChannelType
    identifier: str


_PUBLIC_RE = re.compile(r"(?:https?://)?t\.me/(?P<username>[A-Za-z0-9_]{4,64})/?$", re.IGNORECASE)
_INVITE_RE = re.compile(
    r"(?:https?://)?t\.me/(?:\+|joinchat/)(?P<hash>[A-Za-z0-9_-]{8,})/?$", re.IGNORECASE
)


def normalize_public(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None

    if t.startswith("@"):  # @username
        t = t[1:]

    m = _PUBLIC_RE.match(t)
    if m:
        t = m.group("username")

    # final check
    if not re.fullmatch(r"[A-Za-z0-9_]{4,64}", t):
        return None

    return t.lower()


def normalize_invite(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None

    m = _INVITE_RE.match(t)
    if m:
        return m.group("hash")

    # allow passing raw hash part
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", t):
        return t

    return None


async def _attempt_join_on_add(*, channel_id: int) -> str:
    """Best-effort: try to join channel right after adding it.

    This is especially important for private invite links (join request / pending approval).
    """

    with SessionLocal() as db:
        ch = db.get(Channel, channel_id)
        if not ch:
            return "(join: channel not found)"

    pick = pick_account_for_channel(ch=ch)
    acc = pick.account
    if acc is None:
        return "(join: no ready accounts; add/authorize a userbot account first)"

    pool = TelethonClientPool()
    try:
        async with pool.connected(account=acc) as client:
            if not await client.is_user_authorized():
                return f"(join: account #{acc.id} is not authorized)"

            # First try dialogs (cheap). If present => already joined.
            entity = await get_entity_from_dialogs(client=client, ch=ch)
            if entity is not None:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.joined,
                    note="entity found in dialogs",
                )
                with SessionLocal() as db:
                    ch2 = db.get(Channel, ch.id)
                    if ch2 and ch2.access_status not in {ChannelAccessStatus.active, ChannelAccessStatus.joined}:
                        ch2.access_status = ChannelAccessStatus.joined
                        db.commit()
                return f"(join: OK via dialogs; account #{acc.id})"

            join_res = await ensure_joined(client=client, ch=ch)

            # Persist membership + channel status.
            if join_res.access_status == ChannelAccessStatus.joined:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.joined,
                    note=join_res.note,
                )
            elif join_res.access_status == ChannelAccessStatus.join_requested:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.join_requested,
                    note=join_res.note,
                )
            elif join_res.access_status == ChannelAccessStatus.pending_approval:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.pending_approval,
                    note=join_res.note,
                )
            elif join_res.access_status == ChannelAccessStatus.forbidden:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.forbidden,
                    note=join_res.note,
                )
            elif join_res.access_status == ChannelAccessStatus.error:
                upsert_membership(
                    account_id=acc.id,
                    channel_id=ch.id,
                    status=AccountChannelStatus.error,
                    note=join_res.note,
                )

            with SessionLocal() as db:
                ch2 = db.get(Channel, ch.id)
                if ch2:
                    if join_res.access_status is not None:
                        ch2.access_status = join_res.access_status
                    ch2.last_error = join_res.note if not join_res.ok else ""
                    db.commit()

            return f"(join: {join_res.access_status.value if join_res.access_status else 'unknown'}; account #{acc.id}; note={join_res.note})"
    except Exception as e:
        log.exception("join-on-add failed")
        return f"(join: error {type(e).__name__})"


async def _render_channels_menu(q: CallbackQuery) -> None:
    if q.message:
        await q.message.edit_text(
            "Каналы\n\nВыберите действие:",
            reply_markup=channels_actions_kb().as_markup(),
        )


@router.callback_query(lambda q: q.data == cb.CHANNELS)
async def channels_menu(q: CallbackQuery) -> None:
    await q.answer()
    await _render_channels_menu(q)


@router.callback_query(lambda q: q.data in {cb.CH_ADD_PUBLIC, cb.CH_ADD_PRIVATE})
async def ch_add_start(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.clear()

    is_public = (q.data == cb.CH_ADD_PUBLIC)
    ch_type = ChannelType.public if is_public else ChannelType.private
    await state.update_data(ch_type=ch_type.value)

    if q.message:
        if is_public:
            await q.message.answer(
                "Пришлите public канал (примеры):\n"
                "- @username\n"
                "- https://t.me/username\n\n"
                "/cancel — отмена"
            )
        else:
            await q.message.answer(
                "Пришлите invite-link (примеры):\n"
                "- https://t.me/+AbCdEf...\n"
                "- https://t.me/joinchat/AbCdEf...\n\n"
                "(можно вставить только hash)\n\n"
                "/cancel — отмена"
            )

    await state.set_state(AddChannelFlow.identifier)


@router.message(AddChannelFlow.identifier, Command("cancel"))
async def ch_add_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Ок, отменено.")


@router.message(AddChannelFlow.identifier, F.text)
async def ch_add_identifier(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ch_type = ChannelType(data["ch_type"])

    raw = (m.text or "").strip()
    if ch_type == ChannelType.public:
        ident = normalize_public(raw)
        if not ident:
            await m.answer("Неверный public идентификатор. Пример: @durov или https://t.me/durov")
            return
    else:
        ident = normalize_invite(raw)
        if not ident:
            await m.answer("Неверный invite-link/hash. Пример: https://t.me/+AbCdEf...")
            return

    await state.update_data(identifier=ident)
    await m.answer("Пришлите backfill_days (0..365). Пример: 0 или 30.\n/cancel — отмена.")
    await state.set_state(AddChannelFlow.backfill_days)


@router.message(AddChannelFlow.backfill_days, Command("cancel"))
async def ch_add_cancel2(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Ок, отменено.")


@router.message(AddChannelFlow.backfill_days, F.text)
async def ch_add_backfill(m: Message, state: FSMContext) -> None:
    raw = (m.text or "").strip()
    try:
        days = int(raw)
    except Exception:
        await m.answer("backfill_days must be an integer (0..365)")
        return

    if days < 0 or days > 365:
        await m.answer("backfill_days must be in range 0..365")
        return

    data = await state.get_data()
    ch_type = ChannelType(data["ch_type"])
    ident = data["identifier"]

    with SessionLocal() as db:
        existing = db.execute(
            select(Channel).where(Channel.type == ch_type, Channel.identifier == ident).order_by(Channel.id.asc())
        ).scalar_one_or_none()

        if existing and existing.is_active:
            await m.answer(f"Channel already exists and is active (id=#{existing.id})")
            await state.clear()
            return

        if existing and not existing.is_active:
            existing.is_active = True
            existing.backfill_days = days
            db.commit()

            note = await _attempt_join_on_add(channel_id=existing.id)
            await m.answer(f"Channel re-enabled: #{existing.id} {note}")
            await state.clear()
            return

        ch = Channel(type=ch_type, identifier=ident, backfill_days=days, is_active=True)
        db.add(ch)
        db.commit()

        note = await _attempt_join_on_add(channel_id=ch.id)
        await m.answer(f"Channel added: #{ch.id} ({ch_type.value}) {ident} backfill_days={days} {note}")

    await state.clear()


@router.callback_query(lambda q: q.data == cb.CH_LIST)
async def ch_list(q: CallbackQuery) -> None:
    await q.answer()

    with SessionLocal() as db:
        channels = list(db.execute(select(Channel).order_by(Channel.id.asc())).scalars())

    if not channels:
        if q.message:
            await q.message.answer("No channels yet.")
        return

    lines: list[str] = []
    for ch in channels:
        active_flag = "active" if ch.is_active else "disabled"
        last_checked = ch.last_checked_at.isoformat() if ch.last_checked_at else "—"
        last_error = (ch.last_error or "").strip()
        tail = f" — {last_error}" if last_error else ""
        lines.append(
            f"#{ch.id} [{active_flag}] {ch.type.value}:{ch.identifier} backfill={ch.backfill_days} "
            f"status={ch.access_status.value} checked={last_checked}{tail}"
        )

    if q.message:
        await q.message.answer("Channels:\n" + "\n".join(lines))

        # Send per-row controls (avoid huge inline keyboards)
        for ch in channels[:10]:
            await q.message.answer(
                f"Channel #{ch.id}",
                reply_markup=channel_row_kb(channel_id=ch.id, is_active=ch.is_active).as_markup(),
            )


@router.callback_query(F.data.startswith(f"{cb.CH_DISABLE}:"))
async def ch_disable(q: CallbackQuery) -> None:
    await q.answer()
    try:
        channel_id = int((q.data or "").split(":", 2)[2])
    except Exception:
        return

    with SessionLocal() as db:
        ch = db.get(Channel, channel_id)
        if not ch:
            if q.message:
                await q.message.answer("Channel not found")
            return
        ch.is_active = False
        db.commit()

    if q.message:
        await q.message.answer(f"Channel #{channel_id} disabled")


@router.callback_query(F.data.startswith(f"{cb.CH_ENABLE}:"))
async def ch_enable(q: CallbackQuery) -> None:
    await q.answer()
    try:
        channel_id = int((q.data or "").split(":", 2)[2])
    except Exception:
        return

    with SessionLocal() as db:
        ch = db.get(Channel, channel_id)
        if not ch:
            if q.message:
                await q.message.answer("Channel not found")
            return
        ch.is_active = True
        db.commit()

    if q.message:
        await q.message.answer(f"Channel #{channel_id} enabled")
