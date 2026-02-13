from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from .settings import settings

log = logging.getLogger(__name__)


dp = Dispatcher()


@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "TG Parser bot online.\n\n"
        "Planned menus: Accounts / Channels / Status.\n"
        "Commands: /status"
    )


@dp.message(Command("status"))
async def status(m: Message):
    # Placeholder: later show accounts/channels count + last worker tick
    await m.answer("Status: ok (scaffold).")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
