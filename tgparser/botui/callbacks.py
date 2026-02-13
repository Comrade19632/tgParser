from __future__ import annotations

MAIN = "menu:main"
ACCOUNTS = "menu:accounts"
CHANNELS = "menu:channels"
STATUS = "menu:status"

# Accounts actions
ACC_ADD_PHONE = "accounts:add:phone"
ACC_ADD_TDATA = "accounts:add:tdata"
ACC_LIST = "accounts:list"
ACC_DISABLE = "accounts:disable"  # prefix: accounts:disable:<id>
ACC_REMOVE = "accounts:remove"  # prefix: accounts:remove:<id>

BACK = "nav:back"
REFRESH = "nav:refresh"


def is_menu_callback(data: str | None) -> bool:
    return data in {MAIN, ACCOUNTS, CHANNELS, STATUS}


def is_nav_callback(data: str | None) -> bool:
    return data in {BACK, REFRESH}
