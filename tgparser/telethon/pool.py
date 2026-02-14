from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from telethon import TelegramClient

from ..models import Account
from ..telethon_client import build_client

log = logging.getLogger(__name__)


@dataclass
class _ClientEntry:
    client: TelegramClient
    lock: asyncio.Lock
    refcount: int = 0
    connected: bool = False


class TelethonClientPool:
    """Best-effort Telethon client pool.

    Purpose:
    - Reuse client objects per Account during a single worker tick.
    - Serialize usage per account (Telethon client isn't safe for concurrent connects).

    Notes:
    - This pool is in-process only (no cross-worker sharing).
    - We still disconnect when refcount drops to 0 to avoid leaking connections.
    """

    def __init__(self) -> None:
        self._entries: dict[int, _ClientEntry] = {}
        self._global_lock = asyncio.Lock()

    async def _get_entry(self, *, account: Account) -> _ClientEntry:
        async with self._global_lock:
            ent = self._entries.get(account.id)
            if ent is None:
                ent = _ClientEntry(client=build_client(account=account), lock=asyncio.Lock())
                self._entries[account.id] = ent
            return ent

    @asynccontextmanager
    async def connected(self, *, account: Account):
        ent = await self._get_entry(account=account)

        async with ent.lock:
            ent.refcount += 1
            try:
                if not ent.connected:
                    await ent.client.connect()
                    ent.connected = True
                yield ent.client
            finally:
                ent.refcount = max(0, ent.refcount - 1)
                if ent.refcount == 0 and ent.connected:
                    try:
                        await ent.client.disconnect()
                    except Exception:
                        log.exception("telethon_pool: disconnect failed (account_id=%s)", account.id)
                    finally:
                        ent.connected = False
