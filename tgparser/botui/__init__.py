from __future__ import annotations

from aiogram import Dispatcher

from .routers.accounts import router as accounts_router
from .routers.channels import router as channels_router
from .routers.menu import router as menu_router


def setup_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(accounts_router)
    dp.include_router(channels_router)
    dp.include_router(menu_router)
    return dp
