from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from telethon import TelegramClient
from telethon.sessions import StringSession

from .models import Account
from .telethon.account_service import TelethonConfigError, _require_telethon_config

log = logging.getLogger(__name__)


def build_client(*, account: Account) -> TelegramClient:
    """Build a Telethon client using DB-backed StringSession.

    IMPORTANT: The caller is responsible for connecting + disconnecting.
    """

    api_id, api_hash = _require_telethon_config()

    # StringSession('') is valid but will be unauthorized; keep explicit message elsewhere.
    sess = StringSession(account.session_string or "")

    # We deliberately don't set `device_model`/etc yet; can be added later if needed.
    return TelegramClient(
        sess,
        api_id,
        api_hash,
        # Reduce noise; we'll manage logs ourselves.
        # (Telethon can be quite chatty on INFO.)
        # Note: `connection_retries` isn't a hard guarantee, but helps stability.
        connection_retries=1,
        retry_delay=1,
    )


@asynccontextmanager
async def connected_client(*, account: Account):
    client = build_client(account=account)
    try:
        await client.connect()
        yield client
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.exception("telethon: disconnect failed (account_id=%s)", account.id)
