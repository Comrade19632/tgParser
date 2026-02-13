from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon import errors
from telethon.sessions import StringSession

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Account, AccountStatus
from .session_storage import SessionStorage

log = logging.getLogger(__name__)


class TelethonConfigError(RuntimeError):
    pass


def _require_account_telethon_config(acc: Account) -> tuple[int, str]:
    if not acc.api_id or not acc.api_hash:
        raise TelethonConfigError(
            "Telethon credentials are not configured for this account. "
            "Expected accounts.api_id and accounts.api_hash to be set (stored in DB)."
        )
    return int(acc.api_id), str(acc.api_hash)


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

        with SessionLocal() as db:
            acc = db.execute(select(Account).where(Account.id == account_id)).scalar_one_or_none()
            if not acc:
                return AccountHealth(status=AccountStatus.error, last_error=f"Account not found: {account_id}")

        api_id, api_hash = _require_account_telethon_config(acc)

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
        except errors.FloodError as e:
            # Telegram may freeze accounts; this often manifests as FROZEN_METHOD_INVALID.
            msg = str(e)
            if "FROZEN_METHOD_INVALID" in msg:
                return AccountHealth(status=AccountStatus.banned, last_error=f"Frozen: {msg}")
            return AccountHealth(status=AccountStatus.error, last_error=f"FloodError: {msg}")
        except TelethonConfigError:
            raise
        except Exception as e:
            return AccountHealth(status=AccountStatus.error, last_error=f"{type(e).__name__}: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                log.exception("telethon: disconnect failed (account_id=%s)", account_id)
