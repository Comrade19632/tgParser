from __future__ import annotations

import logging

from aiogram import Bot

from .db import SessionLocal
from .models import BotUser
from .settings import settings

log = logging.getLogger(__name__)


async def _send(bot: Bot, *, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text)


async def notify_admin(text: str) -> None:
    """Best-effort operator notifications.

    Requires ADMIN_CHAT_ID in env. Never raises.
    """

    chat_id = getattr(settings, "admin_chat_id", None)
    if not chat_id:
        return

    bot = Bot(token=settings.bot_token)
    try:
        await _send(bot, chat_id=int(chat_id), text=text)
    except Exception:
        log.exception("notify_admin failed")
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


async def notify_team(text: str) -> None:
    """Notify staff bot users (best-effort).

    Rule (owner): notifications should go only to users with is_staff=true
    (optionally also require notify_enabled=true).
    """

    bot = Bot(token=settings.bot_token)
    try:
        with SessionLocal() as db:
            user_ids = (
                db.query(BotUser.telegram_user_id)
                .filter(BotUser.is_staff.is_(True), BotUser.notify_enabled.is_(True))
                .distinct()
                .all()
            )
            user_ids = [int(x[0]) for x in user_ids if x and x[0]]

        for uid in user_ids:
            try:
                await _send(bot, chat_id=uid, text=text)
            except Exception:
                # Do not fail broadcast on single bad chat.
                log.info("notify_team: failed for user_id=%s", uid)
    except Exception:
        log.exception("notify_team failed")
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
