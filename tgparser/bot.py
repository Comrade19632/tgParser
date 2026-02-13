from __future__ import annotations

import logging

from aiogram import Bot

from .botui import setup_dispatcher
from .settings import settings

log = logging.getLogger(__name__)

dp = setup_dispatcher()


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
