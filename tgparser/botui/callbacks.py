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

# Channels actions
CH_ADD_PUBLIC = "channels:add:public"
CH_ADD_PRIVATE = "channels:add:private"
CH_LIST = "channels:list"
CH_DISABLE = "channels:disable"  # prefix: channels:disable:<id>
CH_ENABLE = "channels:enable"  # prefix: channels:enable:<id>

BACK = "nav:back"
REFRESH = "nav:refresh"


def is_menu_callback(data: str | None) -> bool:
    return data in {MAIN, ACCOUNTS, CHANNELS, STATUS}


def is_nav_callback(data: str | None) -> bool:
    return data in {BACK, REFRESH}
