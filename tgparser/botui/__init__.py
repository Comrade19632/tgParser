from __future__ import annotations

from aiogram import Dispatcher

from .routers.accounts import router as accounts_router
from .routers.channels import router as channels_router
from .routers.menu import router as menu_router


def setup_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # Track bot users on every message/callback (tgreact-style: update_or_create_user everywhere).
    from .middleware import TrackUserMiddleware

    dp.message.middleware(TrackUserMiddleware())
    dp.callback_query.middleware(TrackUserMiddleware())

    dp.include_router(accounts_router)
    dp.include_router(channels_router)
    dp.include_router(menu_router)
    return dp
