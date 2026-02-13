from __future__ import annotations

MAIN = "menu:main"
ACCOUNTS = "menu:accounts"
CHANNELS = "menu:channels"
STATUS = "menu:status"

BACK = "nav:back"
REFRESH = "nav:refresh"


def is_menu_callback(data: str | None) -> bool:
    return data in {MAIN, ACCOUNTS, CHANNELS, STATUS}


def is_nav_callback(data: str | None) -> bool:
    return data in {BACK, REFRESH}
