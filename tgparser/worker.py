from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import redis.asyncio as redis

from .db import Base, engine
from .settings import settings

log = logging.getLogger(__name__)


LOCK_KEY = "tgparser:tick:lock"
LOCK_TTL_SECONDS = 60 * 55  # avoid overlapping hour ticks


async def acquire_lock(r: redis.Redis) -> bool:
    # SET key value NX EX
    return bool(await r.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_SECONDS))


async def release_lock(r: redis.Redis) -> None:
    try:
        await r.delete(LOCK_KEY)
    except Exception:
        log.exception("Failed to release lock")


async def tick() -> None:
    # TODO: implement:
    # - select active channels
    # - rotate across active accounts
    # - join request / pending approval handling
    # - fetch new messages
    # - persist (url/datetime/text) with dedupe
    log.info("tick: stub (%s)", datetime.now(timezone.utc).isoformat())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Dev convenience: ensure tables exist.
    Base.metadata.create_all(bind=engine)

    r = redis.from_url(settings.redis_url)

    while True:
        got = await acquire_lock(r)
        if not got:
            log.info("tick: skipped (lock held)")
        else:
            try:
                await tick()
            finally:
                await release_lock(r)

        await asyncio.sleep(settings.tick_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
