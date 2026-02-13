from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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

TICK_SEQ_KEY = "tgparser:tick:seq"
LAST_TICK_KEY = "tgparser:tick:last"  # Redis hash


async def acquire_lock(r: redis.Redis) -> bool:
    # SET key value NX EX
    return bool(await r.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_SECONDS))


async def release_lock(r: redis.Redis) -> None:
    try:
        await r.delete(LOCK_KEY)
    except Exception:
        log.exception("Failed to release lock")


@dataclass(frozen=True)
class TickSummary:
    accounts_checked: int = 0
    accounts_active_total: int = 0
    accounts_auth_required: int = 0
    accounts_cooldown: int = 0
    accounts_banned: int = 0
    accounts_error: int = 0


async def _update_accounts_status() -> TickSummary:
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
        return TickSummary()

    with SessionLocal() as db:
        account_ids = list(
            db.execute(select(Account.id).where(Account.is_active.is_(True)).order_by(Account.id.asc())).scalars()
        )

        if not account_ids:
            log.info("accounts: none")
            return TickSummary()

        checked = 0
        for account_id in account_ids:
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

        # Summary counts (used for /status and for log line).
        statuses = list(
            db.execute(
                select(Account.status).where(Account.is_active.is_(True))
            ).scalars()
        )

        summary = TickSummary(
            accounts_checked=checked,
            accounts_active_total=len(statuses),
            accounts_auth_required=sum(1 for s in statuses if s == AccountStatus.auth_required),
            accounts_cooldown=sum(1 for s in statuses if s == AccountStatus.cooldown),
            accounts_banned=sum(1 for s in statuses if s == AccountStatus.banned),
            accounts_error=sum(1 for s in statuses if s == AccountStatus.error),
        )

        log.info(
            "accounts: checked=%s active=%s auth_required=%s cooldown=%s banned=%s error=%s",
            summary.accounts_checked,
            summary.accounts_active_total,
            summary.accounts_auth_required,
            summary.accounts_cooldown,
            summary.accounts_banned,
            summary.accounts_error,
        )

        return summary


async def _persist_tick_meta(
    r: redis.Redis,
    *,
    tick_id: int,
    started_at: datetime,
    finished_at: datetime,
    summary: TickSummary,
) -> None:
    duration_s = max(0.0, (finished_at - started_at).total_seconds())

    # Use a hash for readability/debugging.
    await r.hset(
        LAST_TICK_KEY,
        mapping={
            "tick_id": str(tick_id),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_s": f"{duration_s:.3f}",
            "accounts_checked": str(summary.accounts_checked),
            "accounts_active_total": str(summary.accounts_active_total),
            "accounts_auth_required": str(summary.accounts_auth_required),
            "accounts_cooldown": str(summary.accounts_cooldown),
            "accounts_banned": str(summary.accounts_banned),
            "accounts_error": str(summary.accounts_error),
        },
    )


async def tick(r: redis.Redis, *, tick_id: int) -> None:
    started = datetime.now(timezone.utc)

    summary = await _update_accounts_status()

    # TODO (next tasks):
    # - select active channels
    # - rotate across active accounts
    # - join request / pending approval handling
    # - fetch new messages
    # - persist (url/datetime/text) with dedupe

    finished = datetime.now(timezone.utc)
    await _persist_tick_meta(
        r,
        tick_id=tick_id,
        started_at=started,
        finished_at=finished,
        summary=summary,
    )

    log.info(
        "tick: ok id=%s duration_s=%.3f accounts_checked=%s errors=%s",
        tick_id,
        (finished - started).total_seconds(),
        summary.accounts_checked,
        summary.accounts_error,
    )


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
            tick_id = int(await r.incr(TICK_SEQ_KEY))
            try:
                await tick(r, tick_id=tick_id)
            finally:
                await release_lock(r)

        await asyncio.sleep(settings.tick_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
