from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .db import SessionLocal
from .models import BotUser


def track_user(telegram_user_id: int) -> None:
    """Upsert user into bot_users (sync; safe to call from handlers)."""

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        existing = db.execute(
            select(BotUser).where(BotUser.telegram_user_id == int(telegram_user_id))
        ).scalar_one_or_none()

        if existing:
            existing.last_seen_at = now
        else:
            db.add(BotUser(telegram_user_id=int(telegram_user_id), first_seen_at=now, last_seen_at=now))

        db.commit()
