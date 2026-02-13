from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str

    database_url: str
    redis_url: str = "redis://redis:6379/0"

    tick_interval_seconds: int = 3600
    default_backfill_days: int = 0

    telethon_api_id: int | None = None
    telethon_api_hash: str | None = None


settings = Settings()
