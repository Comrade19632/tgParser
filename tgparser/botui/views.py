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
                "Choose a section:\n\n"
                "• Accounts — manage Telethon userbot accounts (stub)\n"
                "• Channels — manage channels list (stub)\n"
                "• Status — parser health and last tick\n"
                "• Errors — recent errors across accounts/channels\n"
            ),
        )
    if key == cb.ACCOUNTS:
        return View(
            key=cb.ACCOUNTS,
            title="Accounts",
            body=(
                "Accounts menu (stub).\n\n"
                "Planned actions:\n"
                "- Add account (phone-code)\n"
                "- Add account (tdata)\n"
                "- List / disable / remove\n"
            ),
        )
    if key == cb.CHANNELS:
        return View(
            key=cb.CHANNELS,
            title="Channels",
            body=(
                "Manage channels list:\n\n"
                "- Add public channel (@username or https://t.me/username)\n"
                "- Add private channel (invite link)\n"
                "- List / enable / disable\n"
            ),
        )
    if key == cb.STATUS:
        return View(
            key=cb.STATUS,
            title="Status",
            body="Use /status or the Status button in the main menu.",
        )

    if key == cb.ERRORS:
        return View(
            key=cb.ERRORS,
            title="Errors",
            body="Use /errors or the Errors button in the main menu.",
        )
    return View(key=key, title="Unknown", body="Unknown view.")
