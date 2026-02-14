from __future__ import annotations

from dataclasses import dataclass

from . import callbacks as cb


@dataclass(frozen=True)
class View:
    key: str
    title: str
    body: str


def get_view(key: str) -> View:
    if key == cb.MAIN:
        return View(
            key=cb.MAIN,
            title="TG Parser",
            body=(
                "Выберите раздел:\n\n"
                "• Аккаунты — управление userbot-аккаунтами (Telethon)\n"
                "• Каналы — список каналов для парсинга\n"
                "• Статус — здоровье парсера и последний тик\n"
                "• Ошибки — последние ошибки по аккаунтам/каналам\n"
            ),
        )
    if key == cb.ACCOUNTS:
        return View(
            key=cb.ACCOUNTS,
            title="Аккаунты",
            body=(
                "Управление аккаунтами:\n\n"
                "- Добавить (код)\n"
                "- Добавить (tdata)\n"
                "- Список / выключить / удалить\n"
            ),
        )
    if key == cb.CHANNELS:
        return View(
            key=cb.CHANNELS,
            title="Каналы",
            body=(
                "Управление каналами:\n\n"
                "- Добавить public (@username или https://t.me/username)\n"
                "- Добавить private (invite-link)\n"
                "- Список / включить / выключить\n"
            ),
        )
    if key == cb.STATUS:
        return View(
            key=cb.STATUS,
            title="Статус",
            body="Используйте /status или кнопку «Статус» в главном меню.",
        )

    if key == cb.ERRORS:
        return View(
            key=cb.ERRORS,
            title="Ошибки",
            body="Используйте /errors или кнопку «Ошибки» в главном меню.",
        )
    return View(key=key, title="Неизвестно", body="Неизвестный экран.")
