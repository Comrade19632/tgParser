from __future__ import annotations

import logging

import redis.asyncio as redis
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ...settings import settings
from ...user_tracking import track_user
from ...worker import LAST_TICK_KEY
from .. import callbacks as cb
from ..keyboards import main_menu_kb, submenu_kb
from ..views import get_view

log = logging.getLogger(__name__)

router = Router()


async def _status_body() -> str:
    r = redis.from_url(settings.redis_url)
    data = await r.hgetall(LAST_TICK_KEY)

    if not data:
        return (
            "No tick metadata yet.\n\n"
            "Worker has not completed a tick, or Redis was reset.\n"
            "Try again after the next worker run."
        )

    # redis-py returns dict[bytes, bytes]
    def _get(key: str, default: str = "?") -> str:
        v = data.get(key.encode())
        if v is None:
            return default
        try:
            return v.decode()
        except Exception:
            return default

    return (
        "Last tick:\n"
        f"- id: {_get('tick_id')}\n"
        f"- started_at: {_get('started_at')}\n"
        f"- finished_at: {_get('finished_at')}\n"
        f"- duration_s: {_get('duration_s')}\n\n"
        "Accounts summary:\n"
        f"- active_total: {_get('accounts_active_total', '0')}\n"
        f"- checked: {_get('accounts_checked', '0')}\n"
        f"- auth_required: {_get('accounts_auth_required', '0')}\n"
        f"- cooldown: {_get('accounts_cooldown', '0')}\n"
        f"- banned: {_get('accounts_banned', '0')}\n"
        f"- error: {_get('accounts_error', '0')}\n"
    )


async def _render_message(*, m: Message, view_key: str) -> None:
    if view_key == cb.STATUS:
        body = await _status_body()
        await m.answer(f"Status\n\n{body}", reply_markup=submenu_kb())
        return

    view = get_view(view_key)
    if view.key == cb.MAIN:
        await m.answer(f"{view.title}\n\n{view.body}", reply_markup=main_menu_kb())
    else:
        await m.answer(f"{view.title}\n\n{view.body}", reply_markup=submenu_kb())


async def _render_callback(*, q: CallbackQuery, view_key: str) -> None:
    markup = main_menu_kb() if view_key == cb.MAIN else submenu_kb()

    if view_key == cb.STATUS:
        text = f"Status\n\n{await _status_body()}"
    else:
        view = get_view(view_key)
        text = f"{view.title}\n\n{view.body}"

    # Prefer edit to keep UI clean; fall back to sending a new message.
    try:
        if not q.message:
            return
        await q.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        # Happens when text is the same (message is not modified) or message is too old.
        log.info("edit_text failed: %s", e)
        if q.message:
            await q.message.answer(text, reply_markup=markup)


@router.message(Command("start"))
async def cmd_start(m: Message) -> None:
    if m.from_user:
        track_user(m.from_user.id)
    await _render_message(m=m, view_key=cb.MAIN)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if m.from_user:
        track_user(m.from_user.id)
    await _render_message(m=m, view_key=cb.STATUS)


@router.callback_query(lambda q: cb.is_menu_callback(q.data))
async def on_menu(q: CallbackQuery) -> None:
    await q.answer()
    if q.from_user:
        track_user(q.from_user.id)
    await _render_callback(q=q, view_key=q.data or cb.MAIN)


@router.callback_query(lambda q: q.data == cb.REFRESH)
async def on_refresh(q: CallbackQuery) -> None:
    await q.answer("Refreshing…")
    # Keep the same current view if possible; fallback to main.
    current = cb.MAIN
    if q.message and q.message.text:
        if q.message.text.startswith("Accounts"):
            current = cb.ACCOUNTS
        elif q.message.text.startswith("Channels"):
            current = cb.CHANNELS
        elif q.message.text.startswith("Status"):
            current = cb.STATUS
    await _render_callback(q=q, view_key=current)
