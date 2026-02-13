from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import secrets

import redis.asyncio as redis
from sqlalchemy import select

from .db import SessionLocal
from .models import Account, AccountStatus
from .settings import settings
from .telethon.account_service import TelethonAccountService, TelethonConfigError
from .telethon.session_storage import DbSessionStorage
from .parser_engine import parse_new_posts_once

log = logging.getLogger(__name__)


LOCK_KEY = "tgparser:tick:lock"
# TTL should cover the whole tick even if it runs long, otherwise another worker could
# acquire the lock after expiry and overlap.
# Keep a sane minimum (55m) but also tie it to the configured interval.
LOCK_TTL_SECONDS = max(60 * 55, settings.tick_interval_seconds + 300)

TICK_SEQ_KEY = "tgparser:tick:seq"
LAST_TICK_KEY = "tgparser:tick:last"  # Redis hash


_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""

_REFRESH_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
  return 0
end
"""


async def acquire_lock(r: redis.Redis) -> str | None:
    """Acquire tick lock.

    Returns a lock token string when acquired, otherwise None.

    We store a random token as the lock value and only release if it matches,
    to avoid deleting somebody else's lock in edge cases (TTL expiry, slow tick,
    or redis failover).
    """

    token = secrets.token_hex(16)
    # SET key value NX EX
    ok = await r.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS)
    return token if ok else None


async def release_lock(r: redis.Redis, *, token: str) -> None:
    try:
        await r.eval(_RELEASE_LOCK_LUA, 1, LOCK_KEY, token)
    except Exception:
        log.exception("Failed to release lock")


async def _lock_refresher(r: redis.Redis, *, token: str, interval_s: int = 30) -> None:
    """Keep the tick lock alive while the tick is running.

    Without this, a long-running tick could outlive the TTL and allow another worker
    to acquire the lock, causing overlapping work.
    """

    interval_s = max(5, int(interval_s))
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await r.eval(_REFRESH_LOCK_LUA, 1, LOCK_KEY, token, str(LOCK_TTL_SECONDS))
            except Exception:
                log.exception("Failed to refresh lock")
    except asyncio.CancelledError:
        return


@dataclass(frozen=True)
class TickSummary:
    accounts_checked: int = 0
    accounts_active_total: int = 0
    accounts_auth_required: int = 0
    accounts_cooldown: int = 0
    accounts_banned: int = 0
    accounts_error: int = 0

    channels_checked: int = 0
    channels_total: int = 0
    posts_inserted: int = 0


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

                # If Telegram freezes/bans the account, quarantine it automatically.
                if health.status == AccountStatus.banned:
                    acc.is_active = False
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
            "channels_checked": str(summary.channels_checked),
            "channels_total": str(summary.channels_total),
            "posts_inserted": str(summary.posts_inserted),
        },
    )


async def tick(r: redis.Redis, *, tick_id: int) -> None:
    started = datetime.now(timezone.utc)

    summary = await _update_accounts_status()

    # Parser engine (v1): incremental fetch + persist + dedupe.
    parse_summary = await parse_new_posts_once()
    summary = replace(
        summary,
        channels_checked=parse_summary.channels_checked,
        channels_total=parse_summary.channels_total,
        posts_inserted=parse_summary.posts_inserted,
    )

    finished = datetime.now(timezone.utc)
    await _persist_tick_meta(
        r,
        tick_id=tick_id,
        started_at=started,
        finished_at=finished,
        summary=summary,
    )

    log.info(
        "tick: ok id=%s duration_s=%.3f accounts_checked=%s errors=%s channels=%s/%s posts_inserted=%s",
        tick_id,
        (finished - started).total_seconds(),
        summary.accounts_checked,
        summary.accounts_error,
        summary.channels_checked,
        summary.channels_total,
        summary.posts_inserted,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    r = redis.from_url(settings.redis_url)

    while True:
        token = await acquire_lock(r)
        if not token:
            log.info("tick: skipped (lock held)")
        else:
            tick_id = int(await r.incr(TICK_SEQ_KEY))
            refresher = asyncio.create_task(_lock_refresher(r, token=token))
            try:
                await tick(r, tick_id=tick_id)
            finally:
                refresher.cancel()
                with contextlib.suppress(Exception):
                    await refresher
                await release_lock(r, token=token)

        await asyncio.sleep(settings.tick_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
