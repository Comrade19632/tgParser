from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import callbacks as cb


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Аккаунты", callback_data=cb.ACCOUNTS),
                InlineKeyboardButton(text="Каналы", callback_data=cb.CHANNELS),
            ],
            [
                InlineKeyboardButton(text="Статус", callback_data=cb.STATUS),
                InlineKeyboardButton(text="Ошибки", callback_data=cb.ERRORS),
            ],
            [InlineKeyboardButton(text="Обновить", callback_data=cb.MAIN)],
        ]
    )


def submenu_kb(*, back_to_main: bool = True) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if back_to_main:
        row.append(InlineKeyboardButton(text="← Назад", callback_data=cb.MAIN))
    row.append(InlineKeyboardButton(text="Обновить", callback_data=cb.REFRESH))
    return InlineKeyboardMarkup(inline_keyboard=[row])
