from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str

    # Operator notifications (Telegram user/chat id)
    admin_chat_id: int | None = None

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    tick_interval_seconds: int = 3600
    default_backfill_days: int = 0

    # Telethon API credentials are stored per-account in DB (not in env).


settings = Settings()
