from __future__ import annotations

import argparse
import asyncio
import logging

import redis.asyncio as redis

from .settings import settings
from .worker import acquire_lock, release_lock, tick


async def _run_once(*, force: bool) -> int:
    logging.basicConfig(level=logging.INFO)
    r = redis.from_url(settings.redis_url)

    token = None
    if not force:
        token = await acquire_lock(r)
        if not token:
            logging.getLogger(__name__).info("tick_once: skipped (lock held)")
            return 2

    try:
        tick_id = int(await r.incr("tgparser:tick:seq"))
        await tick(r, tick_id=tick_id)
        return 0
    finally:
        try:
            if token:
                await release_lock(r, token=token)
        finally:
            # Avoid "Event loop is closed" warnings on interpreter shutdown.
            try:
                await r.aclose()
            except Exception:
                pass


def main() -> None:
    p = argparse.ArgumentParser(description="Run a single TG Parser worker tick.")
    p.add_argument("--force", action="store_true", help="Run even if tick lock is held")
    args = p.parse_args()

    raise SystemExit(asyncio.run(_run_once(force=args.force)))


if __name__ == "__main__":
    main()
