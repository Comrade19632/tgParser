from __future__ import annotations

import logging

from aiogram import Bot

from .settings import settings

log = logging.getLogger(__name__)


async def notify_admin(text: str) -> None:
    """Best-effort operator notifications.

    Requires ADMIN_CHAT_ID in env. Never raises.
    """

    chat_id = getattr(settings, "admin_chat_id", None)
    if not chat_id:
        return

    try:
        bot = Bot(token=settings.bot_token)
        await bot.send_message(chat_id=int(chat_id), text=text)
    except Exception:
        log.exception("notify_admin failed")
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
