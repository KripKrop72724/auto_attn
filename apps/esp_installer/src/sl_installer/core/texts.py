"""Every message a person can see, in one place.

Rules for anything in here: short sentences, second person, no jargon. Say
"board" for the ESP32-S3 and "attendance machine" for the terminal. Never
write eFuse, NVS, flash, partition, COM port or MAC address. Codes are for
logs and support only.

One name for each thing, the one the screens use: "board ID", "head office",
"attendance machine", "device number" (the form's field), "machine address", and
"the board's own Wi-Fi" for the setup hotspot, whose name and "board's Wi-Fi
password" the screens show next to this advice. A step may only name a button that
is on the screen where the advice appears; "Save a help file" is inside Help.

Pictures (``illustration``) must be one of ILLUSTRATIONS; the screens ship
exactly those.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .api import Advice, Family, Severity
from .naming import normalize_mac

ILLUSTRATIONS = (
    "unplug", "replug", "usb_socket", "boot_button", "cable", "faulty_board", "hotspot", "wait",
)

PORTAL_ADDRESS = "http://192.168.254.1"

_SAVE_HELP = "If it happens again, open Help, click Save a help file and send it to head office."
_REPLUG = "Unplug the board, wait 5 seconds, and plug it back in."
# The board's own Wi-Fi: on the screen with this advice, the app shows its name and
# password (Check a board) as the last screen of a setup does.
_BOARD_WIFI_PASSWORD = "Use the board's Wi-Fi password shown on this screen."


def _a(code: str, title: str, what_happened: str, *what_to_do: str,
       severity: Severity = Severity.ERROR, illustration: Optional[str] = None,
       can_retry: bool = True) -> Advice:
    assert illustration is None or illustration in ILLUSTRATIONS, illustration
    return Advice(code=code, title=title, what_happened=what_happened,
                  what_to_do=tuple(what_to_do), severity=severity,
                  illustration=illustration, can_retry=can_retry)


def family_name(family: Optional[Family]) -> str:
    if family is Family.HIKVISION:
        return "Hikvision"
    return "ZKTeco"


# ------------------------------------------------------------------- USB link

NO_BOARD = _a(
    "NO_BOARD", "Connect the board",
    "The computer cannot see a board yet.",
    "Plug the cable into the socket on the board marked USB.",
    "Plug the other end straight into this computer, not into a hub.",
    "Wait a few seconds.",
    severity=Severity.INFO, illustration="usb_socket")

# Some new boards run a maker's demo that gives the computer no port to list, such as a
# USB keyboard or USB drive (ESP-IDF tusb_hid, tusb_msc at 303A:4002, serial "123456"):
# they show up only in setup mode. So "no board" advice says how before "faulty" (also
# ui/wording.presence_advice).
SETUP_MODE_TIP = ("Some new boards only show up in setup mode. Hold down the BOOT button. "
                  "While you hold it, press and release the RST button. Then let go of BOOT.")

NO_BOARD_TIPS = _a(
    "NO_BOARD_TIPS", "Still no board",
    "The computer still cannot see the board.",
    "Use the cable that came with the board. Some cables only charge and cannot carry data.",
    "Check that the cable is in the socket on the board marked USB.",
    "Try another USB socket on this computer.",
    SETUP_MODE_TIP,
    "If nothing works after 3 tries, the board may be faulty. Put it aside and use another one.",
    severity=Severity.WARNING, illustration="cable")

WRONG_SOCKET = _a(
    "WRONG_SOCKET", "Use the other socket on the board",
    "The cable is in the board's other socket. The app can only set up a board "
    "through the socket marked USB.",
    "Unplug the cable from the board.",
    "Plug it into the socket marked USB.",
    severity=Severity.WARNING, illustration="usb_socket")

# Connect, when the cable is in the socket marked USB but other software runs the board,
# such as a maker's demo on a new board (core/usb.py, OTHER_USB_MODE). The button steps
# are AUTO_DOWNLOAD_FAILED's. In setup mode the board shows up as usual: Connect says
# Board found and waits for Next, it does not move on by itself. Connect keeps this
# advice up through the short gap while the board restarts (ui/controller), so "still
# see this message" means the buttons did not work: holding BOOT while the board is
# plugged back in starts setup mode too. After that, a help file for head office; a
# board that runs other software is never called faulty here.
SETUP_MODE_NEEDED = _a(
    "SETUP_MODE_NEEDED", "Put the board into setup mode",
    "The board is running other software, so the app cannot set it up yet. "
    "This is normal for a new board.",
    "Hold down the BOOT button on the board.",
    "While you hold BOOT, press and release the RST button.",
    "Let go of BOOT.",
    "The app then finds the board by itself. When this screen says Board found, click Next.",
    "If you still see this message after 5 seconds, unplug the board.",
    "Hold down BOOT while you plug the board back into the socket marked USB. "
    "Then let go of BOOT.",
    "If you still see this message after 3 tries, open Help. "
    "Click Save a help file and send it to head office.",
    severity=Severity.WARNING, illustration="boot_button")

SEVERAL_BOARDS = _a(
    "SEVERAL_BOARDS", "Connect only one board",
    "The computer can see more than one board.",
    "Unplug every board except the one you want to set up.",
    "Wait a few seconds.",
    severity=Severity.WARNING, illustration="unplug")

BOARD_DISCONNECTED = _a(
    "BOARD_DISCONNECTED", "The board was unplugged",
    "The computer lost the board.",
    "Plug the board back into the socket marked USB.",
    "Keep the cable plugged in until the screen tells you to unplug it.",
    "Click Try again.",
    illustration="replug")

PORT_BUSY = _a(
    "PORT_BUSY", "Another program is using the board",
    "Another program on this computer is connected to the board, so this app cannot use it.",
    "Close other programs that talk to boards, for example Arduino or a serial monitor.",
    "Close any second copy of this app.",
    "Unplug the board, plug it back in, and click Try again.",
    illustration="replug")

BOARD_ID_UNKNOWN = _a(
    "BOARD_ID_UNKNOWN", "The app cannot read the board ID",
    "Windows did not tell the app which board this is.",
    _REPLUG,
    "Make sure the cable is in the socket marked USB.",
    "Click Try again.",
    illustration="replug")

# Some computers and hubs give no board ID over USB. The app writes only to the port
# that has the board's ID (installer._resolve_port), so it stops instead. No step names
# a button: the screens offer Try again, Check again or Finish the setup.
BOARD_ID_NOT_READABLE = _a(
    "BOARD_ID_NOT_READABLE", "This computer cannot read the board ID",
    "This computer cannot read the board ID from USB, so the app cannot check that it is "
    "still the same board.",
    "Plug the cable straight into another USB socket on this computer, not into a hub.",
    "If that does not help, use another computer.",
    severity=Severity.WARNING, illustration="cable")


def restart_loop_faulty(period_s: Optional[float]) -> Advice:
    every = f"about every {period_s:.0f} seconds" if period_s else "again and again"
    return _a(
        "RESTART_LOOP_FAULTY", "This board is faulty",
        f"The board keeps restarting {every} and never starts properly. "
        "This is a known fault that cannot be fixed here.",
        "Unplug the board.",
        "Put a FAULTY label on it and keep it aside.",
        "Connect a new board.",
        illustration="faulty_board", can_retry=False)


def restart_loop_fast(period_s: Optional[float], button: str = "Check again") -> Advice:
    """A loop too fast for the faulty-board signature: power first, never FAULTY (E5).

    ``button`` is the screen's way on: Check again on Connect and Check a board,
    Try again after the first start.
    """
    every = f"every {period_s:.0f} seconds" if period_s else "every few seconds"
    return _a(
        "RESTART_LOOP_FAST", "The board keeps restarting",
        f"The board restarts {every}. Usually the cable or the USB socket does not give "
        "enough power. Sometimes the board is faulty.",
        "Use a short cable straight into this computer, not a hub.",
        "Try another USB socket on this computer.",
        f"Then click {button}.",
        severity=Severity.WARNING, illustration="cable")


# ---------------------------------------------------------- talking to a board

AUTO_DOWNLOAD_FAILED = _a(
    "AUTO_DOWNLOAD_FAILED", "Put the board into setup mode",
    "The app could not put the board into setup mode by itself.",
    "Hold down the BOOT button on the board.",
    "While you hold BOOT, press and release the RST button.",
    "Let go of BOOT.",
    "Click Try again.",
    illustration="boot_button")

CABLE_PROBLEM = _a(
    "CABLE_PROBLEM", "The connection is not stable",
    "The board answered, but the data over the cable was not clean.",
    "Use a short, good cable straight into this computer. Do not use a hub.",
    "Try another USB socket on this computer.",
    "Click Try again.",
    illustration="cable")

CONNECTION_LOST = _a(
    "CONNECTION_LOST", "The connection to the board dropped",
    "The cable connection dropped while the app was working.",
    "Keep the cable plugged in and do not touch it while the app works.",
    "Use a short cable straight into this computer.",
    "Click Try again.",
    illustration="cable")

TOOL_TIMEOUT = _a(
    "TOOL_TIMEOUT", "The board stopped answering",
    "The board did not answer for too long.",
    _REPLUG,
    "Click Try again.",
    illustration="replug")

TOOL_FAILED = _a(
    "TOOL_FAILED", "The board gave an unexpected answer",
    "Something went wrong while the app talked to the board.",
    _REPLUG,
    "Click Try again.",
    _SAVE_HELP,
    illustration="replug")

TOOL_MISSING = _a(
    "TOOL_MISSING", "This app is damaged",
    "A part of this app is missing or damaged.",
    "Close the app.",
    "Install it again from the Setup file you were given.",
    can_retry=False)

WRONG_CHIP = _a(
    "WRONG_CHIP", "This is not the right kind of board",
    "This board uses a different chip. It cannot run the State Life attendance software.",
    "Unplug it.",
    "Use one of the approved boards.",
    illustration="faulty_board", can_retry=False)

WRONG_FLASH_SIZE = _a(
    "WRONG_FLASH_SIZE", "This board is the wrong model",
    "This board does not have enough memory for the attendance software.",
    "Unplug it.",
    "Use one of the approved boards.",
    illustration="faulty_board", can_retry=False)

WRONG_PSRAM = _a(
    "WRONG_PSRAM", "This board is the wrong model",
    "This board does not have the extra memory the attendance software needs.",
    "Unplug it.",
    "Use one of the approved boards.",
    illustration="faulty_board", can_retry=False)

# Boards the State Life software cannot start (board.py hardware gate), refused before
# anything is written. The board itself works, so it is "not broken": with another
# memory chip or board type the same model works. A board an earlier setup locked
# (``locked``) stays with head office; a new one can go back to the seller.
def flash_chip_unsupported(flash_id: Optional[str] = None, *, locked: bool = False,
                           at_start: bool = False) -> Advice:
    """The memory chip our start-up program cannot start (XMC 20 70 18, 3 Nov 2026).

    ``at_start``: seen in the board's own words while it started (console.judge), after
    a setup wrote it; ``flash_id`` is the chip ID the check measured, when known.
    """
    shown = f" Its memory chip ID is {flash_id}." if flash_id else ""
    if at_start:
        happened = ("The board's start-up program stopped because it cannot use this board's "
                    "memory chip. The board restarts again and again and can never start.")
    elif locked:
        happened = ("This board has a memory chip that the State Life software cannot start "
                    "with. An earlier setup locked the board, but it can never start. The app "
                    "did not write to it again.")
    else:
        happened = ("This board has a memory chip that the State Life software cannot start "
                    "with. The app did not change anything on the board.")
    if at_start or locked:
        tell = (f"Tell head office the board ID and the memory chip ID, {flash_id}."
                if flash_id else "Tell head office the board ID.")
        steps: Tuple[str, ...] = (
            "Put this board aside. It is not broken, but it cannot be used.",
            "Do not try to set it up again.", "Use another board.", tell)
    else:
        tell = (f"Tell the seller the memory chip ID, {flash_id}, and ask for boards with "
                "another memory chip." if flash_id
                else "Tell the seller, and ask for boards with another memory chip.")
        steps = ("Put this board aside. It is not broken.", "Use another board.", tell)
    return _a("FLASH_CHIP_UNSUPPORTED", "This board's memory chip cannot run our software",
              happened + shown, *steps, illustration="unplug", can_retry=False)


FLASH_CHIP_UNSUPPORTED = flash_chip_unsupported()


def board_type_unsupported(found: str = "") -> Advice:
    """A board type the software is not built for: ``found`` is what the board check
    measured that did not fit (board.hardware_gate, e.g. "extra memory 8 MB, type AP_1v8"),
    for head office."""
    shown = f" The app found: {found}." if found else ""
    return _a(
        "BOARD_TYPE_UNSUPPORTED", "This board type is not supported yet",
        "The State Life software cannot run on this type of board yet. The app did not change "
        "anything on the board." + shown,
        "Put this board aside. It is not broken.",
        "Use another board.",
        "Tell head office what the app found." if found else "Tell head office.",
        illustration="unplug", can_retry=False)


BOARD_TYPE_UNSUPPORTED = board_type_unsupported()


SECURED_OTHER = _a(
    "SECURED_OTHER", "This board is locked by someone else",
    "This board has been locked with another organisation's keys. It can never be used "
    "for State Life.",
    "Unplug it.",
    "Give it back to head office.",
    illustration="faulty_board", can_retry=False)

UNSECURED_USED = _a(
    "UNSECURED_USED", "This board was used before",
    "Another tool has already changed this board, so it is not safe to set it up.",
    "Unplug it and give it to head office.",
    "Use a new board.",
    can_retry=False)

SECURITY_UNKNOWN = _a(
    "SECURITY_UNKNOWN", "The app could not check the board",
    "The app could not read how the board is locked.",
    _REPLUG,
    "Click Try again.",
    "If it happens again, give the board to head office.",
    illustration="replug")

FIRST_START_UNFINISHED = _a(
    "FIRST_START_UNFINISHED", "The board did not finish its first start",
    "This board started once but stopped before it finished.",
    _REPLUG,
    "Wait 2 minutes, then check the board again.",
    severity=Severity.WARNING, illustration="replug")

ALREADY_SET_UP = _a(
    "ALREADY_SET_UP", "This board is already set up",
    "This board was set up for State Life before.",
    "To change its zone or settings, go back and choose Check or fix a board.",
    severity=Severity.WARNING, can_retry=False)

UNFINISHED_SETUP = _a(
    "UNFINISHED_SETUP", "This board's setup is not finished",
    "An earlier setup locked the board's security key but did not finish.",
    "Click Back, then click Set up a new board.",
    "Type the same zone and device details as before, if you know them.",
    "When the app has checked the board, click Finish the setup.",
    severity=Severity.WARNING)

BOARD_SWAPPED = _a(
    "BOARD_SWAPPED", "A different board is connected",
    "The board connected now is not the board the app started with.",
    "Plug in the board you started with.",
    "Click Try again.",
    illustration="replug")

MODE_NOT_ALLOWED = _a(
    "MODE_NOT_ALLOWED", "This step does not fit this board",
    "The app stopped because this board is not in the state this step needs. "
    "Nothing was changed.",
    "Go back and check the board again.",
    can_retry=False)

# ----------------------------------------------------------- install outcomes

CANCELLED = _a(
    "CANCELLED", "Setup stopped",
    "You stopped the setup. Nothing permanent was done to the board.",
    "You can start again at any time.",
    severity=Severity.INFO)

COPY_CHECK_FAILED = _a(
    "COPY_CHECK_FAILED", "The copy did not check out",
    "The software on the board does not match what the app copied. Nothing permanent "
    "was done to the board.",
    _REPLUG,
    "Click Try again.",
    "If it fails again, the board may be faulty. Put it aside and use another one.",
    illustration="replug")

KEY_NOT_LOCKED = _a(
    "KEY_NOT_LOCKED", "The security key was not locked",
    "The board did not accept its security key. Nothing permanent happened.",
    _REPLUG,
    "Click Try again.",
    illustration="replug")

KEY_STATE_UNCLEAR = _a(
    "KEY_STATE_UNCLEAR", "Check this board again",
    "The board stopped answering while its security key was being locked. The app cannot "
    "tell whether that finished.",
    _REPLUG,
    "Click Check the board again. The check shows whether to finish the setup.",
    "If the check says the board is not safe, keep it aside and tell head office.",
    severity=Severity.WARNING, illustration="replug")

KEY_CHECK_FAILED = _a(
    "KEY_CHECK_FAILED", "The security key check failed",
    "The security key was written, but the board does not show it as safely locked.",
    "Do not use this board.",
    "Open Help, click Save a help file and send it to head office with the board ID.",
    illustration="faulty_board", can_retry=False)

RESUME_NEEDED = _a(
    "RESUME_NEEDED", "Setup is not finished yet",
    "The board's security key is locked, so this board now belongs to State Life. "
    "The rest of the setup did not finish.",
    "Do not throw the board away.",
    "If the board was unplugged, plug it back into the socket marked USB.",
    "Click Finish the setup.",
    illustration="replug")

# Once the key is locked the screen offers "Finish the setup" (and "Change the details"
# when the details were the problem), never Try again or Next; the last step says which
# to click. "If it ..." steps say to put the board aside, which a locked board must
# never be.
_NOT_FOR_RESUME = re.compile(r"\bclick (?:Try again|Next|Change the details|Finish the setup)\b|"
                             r"^If it ", re.I)
_AND_RETRY = re.compile(r",? and click Try again\.$")


def resume_needed(cause: Optional[Advice] = None) -> Advice:
    """RESUME_NEEDED, keeping the steps that fix what stopped the setup."""
    if cause is None or cause.code in (RESUME_NEEDED.code, CANCELLED.code):
        return RESUME_NEEDED
    steps = [_AND_RETRY.sub(".", step) for step in cause.what_to_do]
    steps = [step for step in steps if not _NOT_FOR_RESUME.search(step)]
    if cause.code.startswith("INVALID_FIELD") or cause.code == "DEVICE_ID_IN_USE":
        last = ("Click Change the details and correct it. The app then checks the board again "
                "and offers Finish the setup.")
    else:
        last = "Click Finish the setup."
    if cause.code == BOARD_SWAPPED.code:
        # The locked board is the one the app started with, not the one plugged in now.
        happened = ("A different board is connected now. The security key of the board you "
                    "started with is locked, so that board now belongs to State Life. The "
                    "rest of its setup did not finish.")
    else:
        happened = f"{cause.title}. {RESUME_NEEDED.what_happened}"
    return _a(
        "RESUME_NEEDED", RESUME_NEEDED.title, happened,
        "Do not throw the board away.", *steps, last,
        illustration=cause.illustration or RESUME_NEEDED.illustration)


SETTINGS_WRITE_FAILED = _a(
    "SETTINGS_WRITE_FAILED", "The new settings were not written",
    "Writing the new settings did not finish.",
    "Keep the cable plugged in.",
    "Click Try again.",
    illustration="replug")

# ------------------------------------------------------- firmware from ADD

BUNDLE_DOWNLOAD_FAILED = _a(
    "BUNDLE_DOWNLOAD_FAILED", "The board software did not download",
    "The app could not download the board software from head office.",
    "Check the internet connection.",
    "Click Try again.")

BUNDLE_NOT_TRUSTED = _a(
    "BUNDLE_NOT_TRUSTED", "The board software failed its safety check",
    "The board software from head office did not pass the State Life safety check, so the app "
    "did not use it. Nothing was written to the board.",
    "Do not set up boards with this software.",
    "Tell head office.",
    can_retry=False)

BUNDLE_FILE_DAMAGED = _a(
    "BUNDLE_FILE_DAMAGED", "A board software file was damaged",
    "A downloaded board software file was damaged, so the app removed it.",
    "Click Try again to download it again.")

BUNDLE_WRONG_FAMILY = _a(
    "BUNDLE_WRONG_FAMILY", "Wrong board software for this machine type",
    "The board software you chose is for the other type of attendance machine.",
    "Go back and choose board software for the machine type you picked.",
    can_retry=False)

BUNDLE_NOT_INSTALLABLE = _a(
    "BUNDLE_NOT_INSTALLABLE", "This board software is not for new boards",
    "Head office has not approved this software for setting up boards.",
    "Go back and choose the recommended version.",
    can_retry=False)

DISK_FULL = _a(
    "DISK_FULL", "This computer is out of space",
    "There is not enough free space on this computer to keep the board software.",
    "Free up at least 200 MB, for example by emptying the Recycle Bin.",
    "Click Try again.")

# --------------------------------------------------------- head office (ADD)

PACKAGE_BAD = _a(
    "PACKAGE_BAD", "The secure settings did not check out",
    "The settings from head office failed a safety check, so the app did not use them. "
    "Nothing was written to the board.",
    "Click Try again.",
    "If it happens again, tell head office.")

PROVISIONER_UNAVAILABLE = _a(
    "PROVISIONER_UNAVAILABLE", "Head office cannot prepare settings right now",
    "The head office server is up, but it cannot make board settings at the moment.",
    "Wait a few minutes.",
    "Click Try again.",
    illustration="wait")

# The same words as the Check a board screen's own refusal (ui/wording.py).
SETTINGS_CHANGE_NOT_SUPPORTED = _a(
    "SETTINGS_CHANGE_NOT_SUPPORTED", "This app cannot change this board's settings",
    "This board was locked for State Life in an older way. Settings made by this app "
    "could stop it from working, so head office did not make them. Nothing was written "
    "to the board.",
    "Leave the board as it is.",
    "Call head office and tell them the board ID.",
    severity=Severity.WARNING, can_retry=False)


def rate_limited(minutes: int = 1) -> Advice:
    """Head office's request limit, with the wait it asked for (its Retry-After)."""
    minutes = max(1, int(minutes))
    wait = "one minute" if minutes == 1 else f"{minutes} minutes"
    return _a(
        "RATE_LIMITED", "Please wait a moment",
        "Head office asked the app to slow down.",
        f"Wait {wait}.",
        "Click Try again.",
        severity=Severity.WARNING, illustration="wait")


RATE_LIMITED = rate_limited(1)

SERVER_DISABLED = _a(
    "SERVER_DISABLED", "Board setup is switched off",
    "Head office has switched off board setup for now.",
    "Ask head office when you can set up boards again.",
    can_retry=False)

APP_TOO_OLD = _a(
    "APP_TOO_OLD", "This app is too old",
    "Head office needs a newer version of this app.",
    "Ask head office for the new Setup file and install it.",
    can_retry=False)

SERVER_ERROR = _a(
    "SERVER_ERROR", "Head office had a problem",
    "The head office server could not finish the request.",
    "Wait a few minutes.",
    "Click Try again.",
    _SAVE_HELP)

NOT_FOUND = _a(
    "NOT_FOUND", "Head office does not have that item",
    "Head office could not find what the app asked for. It may have been removed.",
    "Go back and choose again.")

# ------------------------------------------------------------------ sign-in

LOGIN_FAILED = _a(
    "LOGIN_FAILED", "Wrong username or password",
    "Head office did not accept the username or password.",
    "Check the username and password and try again.",
    "After 3 wrong tries the app waits 10 minutes before you can try again.",
    severity=Severity.WARNING)

ACCOUNT_LOCKED = _a(
    "ACCOUNT_LOCKED", "Sign-in is locked for now",
    "Head office has paused sign-in after too many wrong passwords. This also locks the "
    "dashboard.",
    "Wait 15 minutes, then try again with the correct password.",
    illustration="wait")

SESSION_EXPIRED = _a(
    "SESSION_EXPIRED", "Please sign in again",
    "You were signed out of head office.",
    "Click Sign out at the top of the window, then sign in again.",
    severity=Severity.WARNING)

STEP_UP_FAILED = _a(
    "STEP_UP_FAILED", "Your password was not accepted",
    "Head office did not accept the password you signed in with. It may have been changed.",
    "Sign out, then sign in again with the current password.")


def login_paused(minutes_left: int) -> Advice:
    minutes = max(1, int(minutes_left))
    unit = "minute" if minutes == 1 else "minutes"
    return _a(
        "LOGIN_PAUSED", "Sign-in is paused",
        f"There were 3 wrong passwords in a short time, so the app has paused sign-in "
        f"for {minutes} {unit}.",
        "Wait, then try again with the correct password.",
        "This protects the dashboard account: after 5 wrong tries head office locks it "
        "for 15 minutes for everyone.",
        severity=Severity.WARNING, illustration="wait")


# ------------------------------------------------------------------ network

NO_INTERNET = _a(
    "NO_INTERNET", "No internet",
    "This computer is not connected to the internet. The app needs the internet to get "
    "each board's secure settings from head office.",
    "Connect to Wi-Fi or plug in a network cable.",
    "Click Try again.")

SERVER_UNREACHABLE = _a(
    "SERVER_UNREACHABLE", "Head office cannot be reached",
    "The head office server is not answering.",
    "Check the internet connection.",
    "Wait a few minutes and click Try again.",
    "If it keeps happening, call head office.")

SECURE_CONNECTION_FAILED = _a(
    "SECURE_CONNECTION_FAILED", "The secure connection failed",
    "The app could not make a safe connection to head office. This often happens when "
    "the computer's date or time is wrong.",
    "Check that the date and time on this computer are correct.",
    "Click Try again.")


# ------------------------------------------------------ settings form checks

# The names the form's fields carry (ui/pages/settings_form.py), so the person finds them.
_FIELD_LABELS = {
    "zone_id": "the ZONE ID",
    "zone_name": "the ZONE NAME",
    "device_id": "the device number",
    "wifi_ssid": "the Wi-Fi name",
    "wifi_password": "the Wi-Fi password",
    "zkt_port": "the machine port",
    "zkt_comm_key": "the machine comm key",
    "zkt_preferred_ip": "the machine address",
    "hik_host": "the machine address",
    "hik_port": "the machine port",
    "hik_transport": "the connection",
    "hik_username": "the machine user name",
    "hik_password": "the machine password",
    "hik_expected_serial": "the machine serial number",
    "hik_profile": "the machine model",
    "hik_source_epoch": "the record start label",
    "hik_ca_pem": "the attendance machine certificate",
    "bundle_id": "the board software",
    "firmware_family": "the machine type",
}

_FIELD_RULES = {
    "zone_id": "Use letters, numbers, dots, dashes or underscores, at most 64 characters. "
               "Start with a letter or number.",
    "zone_name": "Use at most 120 characters, with no space at the start or end.",
    "device_id": "Use letters, numbers, dots, dashes or underscores, at most 31 characters.",
    "wifi_ssid": "Use 1 to 32 characters.",
    "wifi_password": "Use 8 to 63 characters.",
    "zkt_port": "Use a number from 1 to 65535. The usual port is 4370.",
    "zkt_comm_key": "Use a number from 0 to 4294967295. Read it on the attendance machine.",
    "zkt_preferred_ip": "Use 0.0.0.0 to let the board find the machine, or the machine's "
                        "local address such as 192.168.1.20.",
    "hik_host": "Type the machine's local IP address, such as 192.168.1.20.",
    "hik_port": "Use a number from 1 to 65535. The usual port is 80.",
    "hik_transport": "Under Advanced settings, set Connection to Normal (HTTP).",
    "hik_username": "Type the machine's web username, usually admin.",
    "hik_password": "Type the machine's web password.",
    "hik_expected_serial": "Type the serial number exactly as the machine shows it.",
    "hik_profile": "Choose the machine model from the list.",
    "hik_source_epoch": "Leave this empty so the app fills it in.",
    "hik_ca_pem": "Leave this empty unless head office gave you a certificate.",
}


def invalid_field(field: Optional[str], message: Optional[str] = None) -> Advice:
    key = field or ""
    label = _FIELD_LABELS.get(key, "the details")
    rule = _FIELD_RULES.get(key)
    # Shown on the install screen, whose button back to the form is "Change the details".
    steps = tuple(step for step in ("Click Change the details.", rule,
                                    "Correct it, then click Next.") if step)
    return _a(
        f"INVALID_FIELD:{key}" if key else "INVALID_FIELD", f"Please check {label}",
        message or f"Head office cannot use {label} as typed.",
        *steps, severity=Severity.WARNING)


def family_mismatch(known: Optional[Family]) -> Advice:
    name = family_name(known)
    return _a(
        "FAMILY_MISMATCH", "This board belongs to the other machine type",
        f"Head office knows this board as a {name} board. Setting it up for the other "
        "type would make it restart again and again.",
        # The install screen's button for this (E11).
        f"Click Choose the machine type, then choose {name}.",
        "If you really need to change it, ask head office first.",
        can_retry=False)


def device_id_in_use(device_id: str, zone_id: str, suggested: Optional[str]) -> Advice:
    steps = ([f"Use device number {suggested} instead, under Advanced settings."] if suggested
             else ["Choose another device number under Advanced settings."])
    return _a(
        "DEVICE_ID_IN_USE", "That device number is already taken",
        f"Device number {device_id} is already used by another board in {zone_id}.",
        "Click Change the details.", *steps, "Then click Next.", severity=Severity.WARNING)


# -------------------------------------------------------- first start (boot)

NOT_REPLUGGED = _a(
    "NOT_REPLUGGED", "The board was not plugged back in",
    "The app waited, but the board was not unplugged and plugged back in.",
    "Unplug the cable from the board.",
    "Wait 3 seconds.",
    "Plug it back in.",
    "Click Try again.",
    illustration="unplug")

NO_OUTPUT = _a(
    "NO_OUTPUT", "The board is silent",
    "The board is plugged in but did not send any messages.",
    "Unplug the board, wait 3 seconds, and plug it back in. Do not press any button.",
    "Click Try again.",
    "If it stays silent, the board may be faulty. Put it aside and use another one.",
    illustration="replug")

# Lines came, but none that the board's program prints once it runs (console.judge).
START_NOT_SEEN = _a(
    "START_NOT_SEEN", "The app did not see the board start",
    "The board sent some messages, but none of them showed that it started working.",
    "Unplug the board, wait 3 seconds, and plug it back in. Do not press any button.",
    "Click Try again.",
    "If it happens again, click Check the board.",
    severity=Severity.WARNING, illustration="replug")

DOWNLOAD_MODE_STUCK = _a(
    "DOWNLOAD_MODE_STUCK", "The board is waiting in setup mode",
    "The board started in setup mode instead of starting normally.",
    "Unplug the board.",
    "Wait 5 seconds.",
    "Plug it back in without pressing any button.",
    illustration="unplug")

SECURITY_KEY_LOST = _a(
    "SECURITY_KEY_LOST", "This board cannot be used",
    "The board started before its security key was in place and made its own key. It can "
    "never read State Life settings now.",
    "Put a FAULTY label on it and keep it aside.",
    "Tell head office the board ID.",
    illustration="faulty_board", can_retry=False)

SETTINGS_UNREADABLE = _a(
    "SETTINGS_UNREADABLE", "The board cannot read its settings",
    "The board started, but it cannot read its settings, so it does nothing. Its light "
    "is solid red.",
    "Click Fix settings to write the settings again.",
    "If it happens again, keep the board aside and tell head office.")

FAMILY_MISMATCH_LOOP = _a(
    "FAMILY_MISMATCH_LOOP", "The wrong machine type was chosen",
    "The board has software for one type of attendance machine and settings for the "
    "other, so it keeps restarting.",
    "Click Fix settings and choose the correct machine type: ZKTeco or Hikvision.")

STORAGE_PROBLEM = _a(
    "STORAGE_PROBLEM", "The board's storage is not ready",
    "The board started, but it could not open its storage space. Its light blinks "
    "red-orange.",
    "Click Fix settings to prepare the storage again.",
    "If it happens again, tell head office.")

STORAGE_CHECKING = _a(
    "STORAGE_CHECKING", "The board is checking its saved records",
    "The board is checking and repairing its saved records. This can take up to 15 minutes.",
    "Do not unplug the board.",
    "Wait until the light stops flashing white and blue.",
    severity=Severity.INFO, illustration="wait")

PSRAM_FAILED = _a(
    "PSRAM_FAILED", "This board is faulty or the wrong model",
    "The board's extra memory failed its start-up test.",
    "Unplug it, put a FAULTY label on it and keep it aside.",
    "Use one of the approved boards.",
    illustration="faulty_board", can_retry=False)

CRASH_LOOP = _a(
    "CRASH_LOOP", "The board keeps crashing",
    "The board starts and then crashes, again and again.",
    _REPLUG,
    "If it keeps crashing, open Help, click Save a help file and send it to head office.",
    illustration="replug")

def startup_refused(line: Optional[str] = None) -> Advice:
    """The board's start-up program (ESP-IDF's second-stage bootloader) stopped with an
    error of its own before the board's software ran (console.judge). ``line`` is that
    error as the board printed it, redacted; it is quoted so head office can read it."""
    said = f' It said: "{line}".' if line else ""
    return _a(
        "STARTUP_REFUSED", "The board's start-up program stopped",
        "The board's start-up program refused to start the board's software." + said,
        "Open Help, click Save a help file and send it to head office.",
        "Put the board aside until head office tells you what to do.",
        illustration="unplug", can_retry=False)


STARTUP_REFUSED = startup_refused()


POWER_PROBLEM = _a(
    "POWER_PROBLEM", "The board is not getting enough power",
    "The board switched itself off because the power dropped.",
    "Use a short cable straight into this computer, not a hub.",
    "Try another USB socket on this computer.",
    severity=Severity.WARNING, illustration="cable")

TEST_FIRMWARE = _a(
    "TEST_FIRMWARE", "This board has test software",
    "The board's software cannot open the board's own Wi-Fi. It is not the State Life "
    "release.",
    "Set the board up again with the recommended version.",
    "If you did, tell head office.",
    severity=Severity.WARNING)

WRONG_START_COPY = _a(
    "WRONG_START_COPY", "The board started its spare copy",
    "The board did not start the main copy of its software.",
    "Set the board up again, or tell head office.",
    severity=Severity.WARNING)


def boot_ok(zone_id: Optional[str], device_id: Optional[str], version: Optional[str]) -> Advice:
    parts = []
    if zone_id:
        parts.append(f"zone {zone_id}")
    if device_id:
        parts.append(f"device {device_id}")
    if parts:
        if version:
            parts.append(f"version {version}")
        happened = f"The board started with {', '.join(parts)}."
    else:
        # The start line, which names them, was missed; a later line showed it working.
        happened = "The board started and is working." + (
            f" Its software is version {version}." if version else "")
    return _a(
        "BOOT_OK", "The board started correctly", happened,
        severity=Severity.SUCCESS)


def wrong_settings(seen: str, expected: str) -> Advice:
    return _a(
        "WRONG_SETTINGS", "The board started with other settings",
        f"The board started as {seen}, not {expected}.",
        "Click Fix settings to write the right settings.")


# ------------------------------------------------ the restart check (second start)

# After a good first start the board is started once more, by hand: a board must keep its
# settings through a restart (2 Nov 2026: two new boards started well once, then lost them
# at every later start). The screens and the bench ask for it in these words.
RESTART_REPLUG = ("Unplug the board again, wait 3 seconds, and plug it back in. "
                  "The app checks that the board keeps its settings.")

# The only words that call a setup a success. The first-start screen, the Done screen and
# the setup record use them once the restart check passed, and never before.
SETTINGS_KEPT = "The board started correctly and kept its settings after a restart."


def settings_kept(zone_id: Optional[str], device_id: Optional[str],
                  version: Optional[str]) -> Advice:
    """The restart check passed: the second start named the settings again (console.judge)."""
    parts = [f"zone {zone_id}"] if zone_id else []
    if device_id:
        parts.append(f"device {device_id}")
    if version:
        parts.append(f"version {version}")
    happened = (f"The board started again with {', '.join(parts)}." if parts
                else "The board started again with its settings.")
    return _a("RESTART_OK", "The board kept its settings", happened, severity=Severity.SUCCESS)


def settings_lost_on_restart(board_id: Optional[str] = None, changed: bool = False) -> Advice:
    """The second start had no readable settings, or other ones (console.judge).

    Writing them again does not help (Fix settings lasts only until the next restart), so
    the board must not go to a site. It looks like a good board, so it gets a label with
    its ID (``board_id``, which the start screen does not show elsewhere). ``changed``: it
    named other settings than at its first start. With missing settings it stays still
    (solid red light); with unreadable ones it can keep restarting. The steps name no
    button: the screen offers only "Go back to the start".
    """
    if changed:
        happened = ("Its first start went well, but when the board restarted it had other "
                    "settings. It does not keep the settings it is given.")
    else:
        happened = ("Its first start went well, but when the board restarted it had lost its "
                    "settings. It cannot work like this.")
    shown = (normalize_mac(board_id) or board_id).upper() if board_id else None
    label = ("Put a label on it that says DO NOT USE and its board ID, " + shown + "."
             if shown else "Put a label on it that says DO NOT USE and its board ID.")
    return _a(
        "SETTINGS_LOST_ON_RESTART", "The board lost its settings", happened,
        "Do not use this board. If it came from a site, do not put it back.",
        f"Unplug it. {label} Keep it aside. Do not throw it away.",
        "Open Help and click Save a help file now, before you set up another board. Send the "
        "file to head office.",
        illustration="unplug", can_retry=False)


# Without the board ID: the catalogue's copy, and the screens' fallback when none is known.
SETTINGS_LOST_ON_RESTART = settings_lost_on_restart()

# The second start ran, but its settings line and start line came before the app had the
# port open (console module notes), so nothing showed which settings it has. Try again
# watches a new replug, so the replug comes after the click. A computer that always opens
# the port too late would repeat this, so the board stays away from the sites then.
SETTINGS_NOT_SEEN = _a(
    "SETTINGS_NOT_SEEN", "The app did not see the board read its settings",
    "The board started again, but the app missed the message that shows its settings. The "
    "app cannot tell yet whether the board kept them.",
    "Click Try again.",
    "When the screen asks, unplug the board, wait 3 seconds, and plug it back in. Do not "
    "press any button.",
    "If it happens again, do not install the board. Put it aside. Open Help, click Save a "
    "help file and send it to head office.",
    severity=Severity.WARNING, illustration="replug")


def wifi_placeholder(ssid: str, hotspot: Optional[str]) -> Advice:
    name = hotspot or "SLIC ATTENDANCE"
    return _a(
        "WIFI_PLACEHOLDER", "Choose the site Wi-Fi with a phone",
        f"The board is looking for the Wi-Fi called {ssid}. That name is only a "
        "placeholder, so this is expected.",
        "About 2 minutes after it starts, the board opens its own Wi-Fi.",
        f"On a phone, join the board's own Wi-Fi, called {name}. {_BOARD_WIFI_PASSWORD}",
        f"Open {PORTAL_ADDRESS} in the phone's browser.",
        "Choose the site's Wi-Fi and type its password. The board then restarts and joins it.",
        severity=Severity.INFO, illustration="hotspot")


def wifi_not_joining(ssid: str, hotspot: Optional[str]) -> Advice:
    name = hotspot or "SLIC ATTENDANCE"
    return _a(
        "WIFI_NOT_JOINING", "The board cannot join the Wi-Fi",
        f"The board keeps trying to join the Wi-Fi called {ssid}, but it cannot.",
        "Check that the Wi-Fi name and password are right.",
        "The board only works with 2.4 GHz Wi-Fi. Ask the local IT person if unsure.",
        "Bring the board closer to the router.",
        f"Or, on a phone, join the board's own Wi-Fi, called {name}, and choose another "
        f"Wi-Fi at {PORTAL_ADDRESS}. {_BOARD_WIFI_PASSWORD}",
        severity=Severity.WARNING, illustration="hotspot")


def hotspot_open(hotspot: Optional[str]) -> Advice:
    name = hotspot or "SLIC ATTENDANCE"
    return _a(
        "HOTSPOT_OPEN", "The board's own Wi-Fi is open",
        f"The board has opened its own Wi-Fi, called {name}, so you can choose the site's "
        "Wi-Fi.",
        f"On a phone, join {name}. {_BOARD_WIFI_PASSWORD}",
        f"Open {PORTAL_ADDRESS} in the phone's browser.",
        "Choose the site's Wi-Fi and type its password.",
        "The board restarts and joins that Wi-Fi.",
        severity=Severity.INFO, illustration="hotspot")


HEAD_OFFICE_UNREACHABLE = _a(
    "HEAD_OFFICE_UNREACHABLE", "The board cannot reach head office",
    "The board is on Wi-Fi, but it cannot reach the head office server.",
    "Check that the site's internet works.",
    "Ask the local IT person to allow the address autoattn.slichealth.com.",
    severity=Severity.WARNING)

TIME_SERVICE_BLOCKED = _a(
    "TIME_SERVICE_BLOCKED", "The board cannot get the time",
    "The board needs the correct time from the internet before it can talk to head office.",
    "Check that the site's internet works.",
    "Ask the local IT person to allow internet time (pool.ntp.org).",
    severity=Severity.WARNING)

HEAD_OFFICE_REJECTED = _a(
    "HEAD_OFFICE_REJECTED", "Head office did not accept the board",
    "The board reached head office, but its identity was not accepted.",
    "Click Fix settings to write fresh settings.",
    "If it happens again, tell head office the board ID.",
    severity=Severity.WARNING)

HEAD_OFFICE_CONNECTED = _a(
    "HEAD_OFFICE_CONNECTED", "The board is talking to head office",
    "The board has reached the head office server.",
    severity=Severity.SUCCESS)

COMM_KEY_WRONG = _a(
    "COMM_KEY_WRONG", "The attendance machine refused the board",
    "The board found the attendance machine, but the machine did not accept the comm key "
    "(the machine's connection password).",
    "On the attendance machine, look up its comm key in the communication settings.",
    # Fix settings asks for the comm key at the top, not under Advanced settings.
    "Click Fix settings and type it under The attendance machine, Machine comm key.",
    severity=Severity.WARNING)

NEEDS_ZKT_280 = _a(
    "NEEDS_ZKT_280", "This attendance machine needs newer board software",
    "This attendance machine uses a newer secure connection. The board needs ZKT "
    "2.8.0 or later to talk to it.",
    "If ZKT 2.8.0 or later is in the software list, set the board up with it.",
    "Otherwise tell head office. The board can be updated later.",
    severity=Severity.WARNING)

TERMINAL_NOT_FOUND = _a(
    "TERMINAL_NOT_FOUND", "The attendance machine was not found",
    "The board is on Wi-Fi but cannot find the attendance machine.",
    "Check that the attendance machine is switched on.",
    "Check that it is on the same network (the same router) as the board.",
    "If you know the machine's address, click Fix settings and type it under Advanced "
    "settings, Machine address.",
    severity=Severity.WARNING)

TERMINAL_SERIAL_MISMATCH = _a(
    "TERMINAL_SERIAL_MISMATCH", "This is a different attendance machine",
    "The board found an attendance machine, but not the one it was set up for.",
    "Check that the board is at the right site.",
    "Tell head office the board ID.",
    severity=Severity.WARNING)

HIK_PASSWORD_WRONG = _a(
    "HIK_PASSWORD_WRONG", "The attendance machine refused the password",
    "The Hikvision attendance machine did not accept the username or password.",
    "Check the machine's web username and password.",
    "Click Fix settings and type them again.",
    severity=Severity.WARNING)

HIK_NOT_REACHABLE = _a(
    "HIK_NOT_REACHABLE", "The attendance machine is not answering",
    "The board cannot reach the attendance machine at the machine address you typed.",
    "Check that the machine is on and its network cable is plugged in.",
    "Check the machine's address in its menu. If it changed, click Fix settings.",
    severity=Severity.WARNING)

HIK_SETTINGS_INCOMPLETE = _a(
    "HIK_SETTINGS_INCOMPLETE", "The attendance machine details are incomplete",
    "The board does not have all the details it needs to talk to the attendance machine.",
    "Click Fix settings and fill in every Hikvision detail.",
    severity=Severity.WARNING)

TERMINAL_CONNECTED = _a(
    "TERMINAL_CONNECTED", "The attendance machine is connected",
    "The board is talking to the attendance machine.",
    severity=Severity.SUCCESS)

BOARD_ONLINE = _a(
    "BOARD_ONLINE", "Head office sees this board online",
    "The board is connected and reporting to head office.",
    severity=Severity.SUCCESS)

BOARD_NOT_SEEN = _a(
    "BOARD_NOT_SEEN", "Head office has not seen this board yet",
    "This board has not reached head office from a site yet.",
    "That is normal before the board is installed at the site and has Wi-Fi.",
    severity=Severity.INFO)


def board_offline(status_text: Optional[str]) -> Advice:
    happened = status_text or "Head office does not see this board right now."
    return _a(
        "BOARD_OFFLINE", "Head office does not see this board right now",
        happened,
        "Check that the board has power and its light is on.",
        "Check the site's Wi-Fi and internet.",
        severity=Severity.WARNING)


UNEXPECTED = _a(
    "UNEXPECTED", "Something went wrong",
    "The app hit an unexpected problem.",
    "Click Try again.",
    _SAVE_HELP)


# --------------------------------------------------------------- light guide

@dataclass(frozen=True)
class LedState:
    """One pattern of the board's single light, from firmware led_status.c."""

    key: str                      # firmware state name, for logs
    colour: str                   # plain colour name
    rgb: Tuple[int, int, int]     # full-brightness colour the firmware draws
    rhythm: str                   # one of LED_RHYTHMS
    meaning: str
    what_to_do: Tuple[str, ...]
    severity: Severity


# The rhythm and colour words of the Light guide screen (ui/leds.py), so a hint on
# Check a board names a pattern the guide shows under the same words. The hints appear
# on Check a board, so where the guide says "choose Check or fix a board" they say
# "click Fix settings".
LED_RHYTHMS = ("always on", "slow blink", "fast blink", "slowly brighter and dimmer",
               "two quick flashes", "two colours swapping", "no light")

LED_GUIDE: Tuple[LedState, ...] = (
    LedState("HEALTHY", "green", (0, 255, 0), "always on",
             "Everything is working. Attendance is being sent.", ("Nothing to do.",),
             Severity.SUCCESS),
    LedState("BOOTING", "white", (212, 212, 212), "slowly brighter and dimmer",
             "The board is starting up. It may also be checking its saved records.",
             ("Wait. Do not unplug the board.",
              "If it lasts more than 15 minutes, call head office."), Severity.INFO),
    LedState("STORAGE_REPAIR", "white and blue", (255, 255, 255), "two colours swapping",
             "The board is repairing its saved records.",
             ("Do not unplug the board.", "Wait up to 15 minutes."), Severity.INFO),
    LedState("WIFI_CONNECTING", "blue", (0, 0, 255), "slow blink",
             "The board is trying to join the Wi-Fi. Its own Wi-Fi may also be open.",
             ("Wait 2 minutes.",
              "If it keeps blinking, use a phone to join the board's own Wi-Fi and choose "
              "the site's Wi-Fi.", _BOARD_WIFI_PASSWORD), Severity.INFO),
    LedState("ZKT_DISCOVERING", "light blue", (0, 212, 212), "slowly brighter and dimmer",
             "The board is on the Wi-Fi and is looking for the attendance machine.",
             ("Check that the attendance machine is switched on and on the same network.",
              "If it lasts more than 5 minutes, call head office."), Severity.INFO),
    LedState("ZKT_AUTHENTICATED", "light blue", (0, 255, 255), "always on",
             "The board is connected to the attendance machine and is getting ready.",
             ("Wait a few minutes. The light turns green when everything works.",),
             Severity.INFO),
    LedState("SYNCING", "purple", (212, 0, 212), "slowly brighter and dimmer",
             "The board is copying records between the attendance machine and head office.",
             ("Wait.", "Do not unplug the board."), Severity.INFO),
    LedState("BACKLOG", "amber", (255, 160, 0), "two quick flashes",
             "Some punches are waiting to be sent. On a Hikvision machine it can also mean "
             "the machine is not answering properly.",
             ("Check the internet at this site.",
              "For Hikvision, check that the machine is on and its password has not changed.",
              "Call head office if it lasts more than an hour."), Severity.WARNING),
    LedState("BLOCKED_IDENTITY", "purple", (255, 0, 255), "slow blink",
             "Some punches belong to people head office cannot match yet. Head office is "
             "handling it.",
             ("Nothing to do here.", "Tell head office if it lasts more than a day."),
             Severity.WARNING),
    LedState("TRUTH_REPAIR", "amber", (255, 170, 0), "slow blink",
             "The board is comparing its records with the attendance machine.",
             ("Wait.", "Do not unplug the board."), Severity.WARNING),
    LedState("ZKT_FLAPPING", "orange and dark blue", (255, 120, 0), "two colours swapping",
             "The connection to the attendance machine keeps dropping.",
             ("Check the attendance machine's power and network cable.",
              "Move the board closer to the Wi-Fi router."), Severity.WARNING),
    LedState("ZKT_FAILURE", "yellow", (255, 220, 0), "slow blink",
             "The board found the attendance machine, but the machine did not accept its "
             "comm key. Or a planned restart of the machine did not work.",
             ("Check the comm key in the attendance machine's menu.",
              "Click Fix settings and type it under The attendance machine, Machine comm key."),
             Severity.ERROR),
    LedState("ORDS_FAILURE", "orange", (255, 80, 0), "slow blink",
             "The board cannot send attendance to the HR system. The punches are kept safe "
             "on the board.",
             ("Check the internet at this site.",
              "Call head office if it lasts more than 30 minutes."), Severity.WARNING),
    LedState("LOCAL_FAILURE", "red-orange", (255, 24, 0), "slow blink",
             "The board has a problem with its own storage.",
             ("Unplug the board, wait 10 seconds and plug it back in.",
              "If the light comes back, call head office."), Severity.ERROR),
    LedState("RECOVERY_REBOOT", "red", (255, 0, 0), "fast blink",
             "The board is restarting the attendance machine, as planned. This happens "
             "around 2 am, 12 noon and 10 pm.",
             ("Do nothing. Wait about 2 minutes.",), Severity.INFO),
    LedState("FATAL", "red", (255, 0, 0), "always on",
             "The board has stopped. It has no working settings.",
             ("Click Fix settings to write the settings again.",
              "If it stays red, call head office."), Severity.ERROR),
    LedState("OFF", "no light", (0, 0, 0), "no light",
             "The board has no power, or it did not start.",
             ("Check that the cable is pushed in at both ends.",
              "Unplug the board, wait 3 seconds and plug it back in.",
              "If this app shows the board working, ignore the light."), Severity.WARNING),
)


def light_hint(state: LedState) -> Advice:
    """What the board's light should be doing now, judged from its console."""
    title = ("The light should be off" if state.rhythm == "no light"
             else f"The light should be {state.colour} ({state.rhythm})")
    return _a(f"LED_HINT:{state.key}", title, state.meaning, *state.what_to_do,
              severity=Severity.INFO)


def led_states(rhythm: Optional[str] = None, colour: Optional[str] = None
               ) -> Tuple[LedState, ...]:
    """Light patterns filtered the way the guide asks: rhythm first, then colour."""
    return tuple(state for state in LED_GUIDE
                 if (rhythm is None or state.rhythm == rhythm)
                 and (colour is None or state.colour == colour))


STATIC: Dict[str, Advice] = {
    advice.code: advice for advice in (
        NO_BOARD, NO_BOARD_TIPS, WRONG_SOCKET, SETUP_MODE_NEEDED, SEVERAL_BOARDS,
        BOARD_DISCONNECTED, PORT_BUSY,
        BOARD_ID_UNKNOWN, BOARD_ID_NOT_READABLE, AUTO_DOWNLOAD_FAILED, CABLE_PROBLEM,
        CONNECTION_LOST, TOOL_TIMEOUT,
        TOOL_FAILED, TOOL_MISSING, WRONG_CHIP, WRONG_FLASH_SIZE, WRONG_PSRAM,
        FLASH_CHIP_UNSUPPORTED, BOARD_TYPE_UNSUPPORTED, STARTUP_REFUSED, SECURED_OTHER,
        UNSECURED_USED, SECURITY_UNKNOWN, FIRST_START_UNFINISHED, ALREADY_SET_UP,
        UNFINISHED_SETUP, BOARD_SWAPPED, MODE_NOT_ALLOWED, CANCELLED, COPY_CHECK_FAILED,
        KEY_NOT_LOCKED, KEY_STATE_UNCLEAR, KEY_CHECK_FAILED, RESUME_NEEDED,
        SETTINGS_WRITE_FAILED, BUNDLE_DOWNLOAD_FAILED, BUNDLE_NOT_TRUSTED, BUNDLE_FILE_DAMAGED,
        BUNDLE_WRONG_FAMILY, BUNDLE_NOT_INSTALLABLE, DISK_FULL, PACKAGE_BAD,
        PROVISIONER_UNAVAILABLE, SETTINGS_CHANGE_NOT_SUPPORTED, RATE_LIMITED, SERVER_DISABLED,
        APP_TOO_OLD, SERVER_ERROR,
        NOT_FOUND, LOGIN_FAILED, ACCOUNT_LOCKED, SESSION_EXPIRED, STEP_UP_FAILED, NO_INTERNET,
        SERVER_UNREACHABLE, SECURE_CONNECTION_FAILED, NOT_REPLUGGED, NO_OUTPUT, START_NOT_SEEN,
        SETTINGS_LOST_ON_RESTART, SETTINGS_NOT_SEEN,
        DOWNLOAD_MODE_STUCK, SECURITY_KEY_LOST, SETTINGS_UNREADABLE, FAMILY_MISMATCH_LOOP,
        STORAGE_PROBLEM, STORAGE_CHECKING, PSRAM_FAILED, CRASH_LOOP, POWER_PROBLEM,
        TEST_FIRMWARE, WRONG_START_COPY, HEAD_OFFICE_UNREACHABLE, TIME_SERVICE_BLOCKED,
        HEAD_OFFICE_REJECTED, HEAD_OFFICE_CONNECTED, COMM_KEY_WRONG, NEEDS_ZKT_280,
        TERMINAL_NOT_FOUND, TERMINAL_SERIAL_MISMATCH, HIK_PASSWORD_WRONG, HIK_NOT_REACHABLE,
        HIK_SETTINGS_INCOMPLETE, TERMINAL_CONNECTED, BOARD_ONLINE, BOARD_NOT_SEEN, UNEXPECTED,
    )
}


def get(code: str) -> Advice:
    """The static advice for ``code``; UNEXPECTED for anything unknown."""
    return STATIC.get(code, UNEXPECTED)
