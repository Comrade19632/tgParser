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
from ...models import Channel, ChannelType
from .. import callbacks as cb

log = logging.getLogger(__name__)

router = Router()


def channels_actions_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Add public", callback_data=cb.CH_ADD_PUBLIC)
    kb.button(text="Add private", callback_data=cb.CH_ADD_PRIVATE)
    kb.button(text="List", callback_data=cb.CH_LIST)
    kb.adjust(2, 1)
    kb.button(text="← Back", callback_data=cb.MAIN)
    kb.button(text="Refresh", callback_data=cb.CHANNELS)
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


async def _render_channels_menu(q: CallbackQuery) -> None:
    if q.message:
        await q.message.edit_text(
            "Channels\n\nChoose an action:",
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
                "Send public channel identifier (examples):\n"
                "- @username\n"
                "- https://t.me/username\n\n"
                "/cancel to abort"
            )
        else:
            await q.message.answer(
                "Send private invite link (examples):\n"
                "- https://t.me/+AbCdEf...\n"
                "- https://t.me/joinchat/AbCdEf...\n\n"
                "(You may also paste just the invite hash)\n\n"
                "/cancel to abort"
            )

    await state.set_state(AddChannelFlow.identifier)


@router.message(AddChannelFlow.identifier, Command("cancel"))
async def ch_add_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Cancelled.")


@router.message(AddChannelFlow.identifier, F.text)
async def ch_add_identifier(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ch_type = ChannelType(data["ch_type"])

    raw = (m.text or "").strip()
    if ch_type == ChannelType.public:
        ident = normalize_public(raw)
        if not ident:
            await m.answer("Invalid public channel identifier. Example: @durov or https://t.me/durov")
            return
    else:
        ident = normalize_invite(raw)
        if not ident:
            await m.answer("Invalid invite link/hash. Example: https://t.me/+AbCdEf...")
            return

    await state.update_data(identifier=ident)
    await m.answer("Send backfill_days (0..365). Example: 0 (no backfill) or 30. /cancel to abort")
    await state.set_state(AddChannelFlow.backfill_days)


@router.message(AddChannelFlow.backfill_days, Command("cancel"))
async def ch_add_cancel2(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Cancelled.")


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
            await m.answer(f"Channel re-enabled: #{existing.id}")
            await state.clear()
            return

        ch = Channel(type=ch_type, identifier=ident, backfill_days=days, is_active=True)
        db.add(ch)
        db.commit()

        await m.answer(f"Channel added: #{ch.id} ({ch_type.value}) {ident} backfill_days={days}")

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
