from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import callbacks as cb
from ..keyboards import main_menu_kb, submenu_kb
from ..views import get_view

log = logging.getLogger(__name__)

router = Router()


async def _render_message(*, m: Message, view_key: str) -> None:
    view = get_view(view_key)
    if view.key == cb.MAIN:
        await m.answer(f"{view.title}\n\n{view.body}", reply_markup=main_menu_kb())
    else:
        await m.answer(f"{view.title}\n\n{view.body}", reply_markup=submenu_kb())


async def _render_callback(*, q: CallbackQuery, view_key: str) -> None:
    view = get_view(view_key)
    markup = main_menu_kb() if view.key == cb.MAIN else submenu_kb()

    # Prefer edit to keep UI clean; fall back to sending a new message.
    try:
        if not q.message:
            return
        await q.message.edit_text(f"{view.title}\n\n{view.body}", reply_markup=markup)
    except TelegramBadRequest as e:
        # Happens when text is the same (message is not modified) or message is too old.
        log.info("edit_text failed: %s", e)
        if q.message:
            await q.message.answer(f"{view.title}\n\n{view.body}", reply_markup=markup)


@router.message(Command("start"))
async def cmd_start(m: Message) -> None:
    await _render_message(m=m, view_key=cb.MAIN)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    await _render_message(m=m, view_key=cb.STATUS)


@router.callback_query(lambda q: cb.is_menu_callback(q.data))
async def on_menu(q: CallbackQuery) -> None:
    await q.answer()
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
