from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from ..models import AccountStatus
from ..settings import settings
from .session_storage import SessionStorage

log = logging.getLogger(__name__)


class TelethonConfigError(RuntimeError):
    pass


def _require_telethon_config() -> tuple[int, str]:
    if not settings.telethon_api_id or not settings.telethon_api_hash:
        raise TelethonConfigError(
            "Telethon credentials are not configured. "
            "Set TELETHON_API_ID and TELETHON_API_HASH in .env"
        )
    return int(settings.telethon_api_id), str(settings.telethon_api_hash)


@dataclass(frozen=True)
class AccountHealth:
    status: AccountStatus
    last_error: str = ""
    cooldown_until: datetime | None = None


class TelethonAccountService:
    """Small service around Telethon client creation + account health checks."""

    def __init__(self, *, session_storage: SessionStorage):
        self._storage = session_storage

    async def check(self, *, account_id: int) -> AccountHealth:
        """Check if the session is present and authorized.

        v1 rules:
        - missing session => auth_required
        - unauthorized session => auth_required
        - FloodWait => cooldown
        - unexpected error => error
        """

        sess_str = self._storage.get_session_string(account_id=account_id)
        if not sess_str:
            return AccountHealth(status=AccountStatus.auth_required, last_error="Missing session_string")

        api_id, api_hash = _require_telethon_config()

        # Local import to keep worker start resilient if Telethon isn't installed.
        from telethon import TelegramClient

        client = TelegramClient(
            StringSession(sess_str),
            api_id,
            api_hash,
            connection_retries=1,
            retry_delay=1,
        )

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return AccountHealth(
                    status=AccountStatus.auth_required,
                    last_error="Session is not authorized",
                )

            me = await client.get_me()
            ident = getattr(me, "username", None) or getattr(me, "id", None) or "me"
            return AccountHealth(status=AccountStatus.active, last_error=f"OK: {ident}")
        except FloodWaitError as e:
            seconds = int(getattr(e, "seconds", 0) or 0)
            return AccountHealth(
                status=AccountStatus.cooldown,
                cooldown_until=datetime.now(timezone.utc) + timedelta(seconds=seconds),
                last_error=f"FloodWait: {seconds}s",
            )
        except TelethonConfigError:
            raise
        except Exception as e:
            return AccountHealth(status=AccountStatus.error, last_error=f"{type(e).__name__}: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.exception("telethon: disconnect failed (account_id=%s)", account_id)
