"""The installer's screens. Each page only shows things and reports button
presses as ``action`` names; ui/controller.py decides what happens next."""

from __future__ import annotations

SIGN_IN = "sign_in"
HOME = "home"
MACHINE = "machine"
DETAILS = "details"
CONNECT = "connect"
CHECKING = "checking"
CONFIRM = "confirm"
INSTALLING = "installing"
FIRST_START = "first_start"
DONE = "done"
CHECK_BOARD = "check_board"
FIX_SETTINGS = "fix_settings"
LIGHT_GUIDE = "light_guide"

ALL = (
    SIGN_IN,
    HOME,
    MACHINE,
    DETAILS,
    CONNECT,
    CHECKING,
    CONFIRM,
    INSTALLING,
    FIRST_START,
    DONE,
    CHECK_BOARD,
    FIX_SETTINGS,
    LIGHT_GUIDE,
)
