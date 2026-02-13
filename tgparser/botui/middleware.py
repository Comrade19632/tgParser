from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..db import SessionLocal
from ..models import BotUser
from ..settings import settings
from ..user_tracking import track_user


class TrackUserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = None

        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is not None:
            # Never block bot flow on DB issues.
            try:
                track_user(int(user_id))
            except Exception:
                pass

        return await handler(event, data)


class StaffGateMiddleware(BaseMiddleware):
    """Deny all bot UI/handlers for non-staff users.

    Rule (owner): any incoming /start (and any message/callback) from non-staff must
    get a short "Доступ запрещен" response without menus.

    Admin exception:
    - if ADMIN_CHAT_ID matches chat/user id, allow (so admin can bootstrap staff).

    Note: TrackUserMiddleware should run before this one, so the user is always
    upserted even when denied.
    """

    DENY_TEXT = "Доступ запрещен"

    def _is_admin_chat(self, chat_id: int | None, user_id: int | None) -> bool:
        admin_chat_id = getattr(settings, "admin_chat_id", None)
        if not admin_chat_id:
            return False
        try:
            admin_chat_id = int(admin_chat_id)
        except Exception:
            return False
        return (chat_id is not None and int(chat_id) == admin_chat_id) or (user_id is not None and int(user_id) == admin_chat_id)

    def _is_allowed_admin_command(self, text: str | None) -> bool:
        if not text:
            return False
        # Keep a small allowlist so admin can manage staff even if not staff.
        return text.strip().startswith(("/staff", "/whoami"))

    def _is_staff(self, telegram_user_id: int) -> bool:
        with SessionLocal() as db:
            u = db.query(BotUser).filter(BotUser.telegram_user_id == int(telegram_user_id)).one_or_none()
            return bool(u and u.is_staff)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = None
        chat_id = None
        text = None

        if isinstance(event, Message) and event.from_user:
            user_id = int(event.from_user.id)
            chat_id = int(event.chat.id) if event.chat else None
            text = event.text
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = int(event.from_user.id)
            chat_id = int(event.message.chat.id) if event.message and event.message.chat else None

        if user_id is None:
            return await handler(event, data)

        if self._is_admin_chat(chat_id, user_id):
            return await handler(event, data)

        # Allow admin-only commands even if not staff (useful if admin_chat_id is unset).
        if isinstance(event, Message) and self._is_allowed_admin_command(text):
            return await handler(event, data)

        try:
            if self._is_staff(user_id):
                return await handler(event, data)
        except Exception:
            # On DB errors, fail closed (security).
            pass

        # Deny
        if isinstance(event, Message):
            try:
                await event.answer(self.DENY_TEXT)
            except Exception:
                pass
            return None

        if isinstance(event, CallbackQuery):
            try:
                await event.answer(self.DENY_TEXT, show_alert=False)
            except Exception:
                pass
            return None

        return None
