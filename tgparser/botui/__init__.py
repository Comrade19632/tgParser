from __future__ import annotations

from aiogram import Dispatcher

from .routers.menu import router as menu_router


def setup_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(menu_router)
    return dp
