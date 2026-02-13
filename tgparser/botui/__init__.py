from __future__ import annotations

from aiogram import Dispatcher

from .routers.accounts import router as accounts_router
from .routers.channels import router as channels_router
from .routers.menu import router as menu_router
from .routers.staff import router as staff_router


def setup_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # Track bot users on every message/callback (tgreact-style: update_or_create_user everywhere).
    # IMPORTANT: TrackUser must run before StaffGate, so users are upserted even when denied.
    from .middleware import StaffGateMiddleware, TrackUserMiddleware

    dp.message.middleware(TrackUserMiddleware())
    dp.callback_query.middleware(TrackUserMiddleware())

    dp.message.middleware(StaffGateMiddleware())
    dp.callback_query.middleware(StaffGateMiddleware())

    # Admin/staff commands first (so they are reachable even if other routers change).
    dp.include_router(staff_router)
    dp.include_router(accounts_router)
    dp.include_router(channels_router)
    dp.include_router(menu_router)
    return dp
