from __future__ import annotations

MAIN = "menu:main"
ACCOUNTS = "menu:accounts"
CHANNELS = "menu:channels"
STATUS = "menu:status"
ERRORS = "menu:errors"

# Accounts actions
ACC_ADD_PHONE = "accounts:add:phone"
ACC_ADD_TDATA = "accounts:add:tdata"
ACC_REAUTH_PHONE = "accounts:reauth:phone"  # prefix: accounts:reauth:phone:<id>:<page>
ACC_REAUTH_TDATA = "accounts:reauth:tdata"  # prefix: accounts:reauth:tdata:<id>:<page>
ACC_LIST = "accounts:list"  # exact or prefix: accounts:list:<page>
ACC_VIEW = "accounts:view"  # prefix: accounts:view:<id>:<page>
ACC_TOGGLE = "accounts:toggle"  # prefix: accounts:toggle:<id>:<page>
ACC_REMOVE = "accounts:remove"  # prefix: accounts:remove:<id>:<page>

# Channels actions
CH_ADD_PUBLIC = "channels:add:public"
CH_ADD_PRIVATE = "channels:add:private"
CH_LIST = "channels:list"  # exact or prefix: channels:list:<page>
CH_TOGGLE = "channels:toggle"  # prefix: channels:toggle:<id>:<page>
CH_DISABLE = "channels:disable"  # prefix: channels:disable:<id>
CH_ENABLE = "channels:enable"  # prefix: channels:enable:<id>

BACK = "nav:back"
REFRESH = "nav:refresh"


def is_menu_callback(data: str | None) -> bool:
    return data in {MAIN, ACCOUNTS, CHANNELS, STATUS, ERRORS}


def is_nav_callback(data: str | None) -> bool:
    return data in {BACK, REFRESH}
