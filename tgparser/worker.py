from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import redis.asyncio as redis
from sqlalchemy import select

from .db import Base, SessionLocal, engine
from .models import Account, AccountStatus
from .settings import settings
from .telethon.account_service import TelethonAccountService, TelethonConfigError
from .telethon.session_storage import DbSessionStorage

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


async def _update_accounts_status() -> None:
    """Minimal account health/status check.

    This is the foundation for later onboarding flows and pool rotation.

    Rules (v1):
    - empty session_string => auth_required
    - unauthorized session => auth_required
    - FloodWait => cooldown until now + seconds
    - other errors => error
    """

    # Lazy init: keep worker booting even if Telethon deps/config missing.
    try:
        service = TelethonAccountService(session_storage=DbSessionStorage())
    except Exception:  # pragma: no cover
        log.exception("telethon service init failed")
        return

    with SessionLocal() as db:
        accounts = list(db.execute(select(Account.id).order_by(Account.id.asc())).scalars())

        if not accounts:
            log.info("accounts: none")
            return

        checked = 0
        for account_id in accounts:
            checked += 1

            try:
                health = await service.check(account_id=account_id)

                acc = db.get(Account, account_id)
                if not acc:
                    continue

                acc.status = health.status
                acc.last_error = health.last_error
                acc.cooldown_until = health.cooldown_until
            except TelethonConfigError as e:
                # Config issue is global; no point iterating further.
                log.warning("telethon: config error: %s", e)
                break
            except Exception as e:
                acc = db.get(Account, account_id)
                if acc:
                    acc.status = AccountStatus.error
                    acc.last_error = f"{type(e).__name__}: {e}"
            finally:
                acc = db.get(Account, account_id)
                if acc:
                    acc.updated_at = datetime.now(timezone.utc)

        db.commit()
        log.info("accounts: checked=%s", checked)


async def tick() -> None:
    started = datetime.now(timezone.utc)

    await _update_accounts_status()

    # TODO (next tasks):
    # - select active channels
    # - rotate across active accounts
    # - join request / pending approval handling
    # - fetch new messages
    # - persist (url/datetime/text) with dedupe

    log.info("tick: ok (%s)", (datetime.now(timezone.utc) - started))


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
