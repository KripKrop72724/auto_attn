"""Words the screens need that are not engine advice.

The engine's Advice texts live in core/texts.py. Here are only screen words
(board states in plain language, what console lines mean, help topics) and a
few fallbacks for when the engine gives no advice, so the person is never
left looking at an empty card. Same writing rules everywhere: short
sentences, second person, no technical words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..core import api, texts
from ..core.api import (
    Advice,
    BootOutcome,
    ConsoleEvent,
    ConsoleKind,
    Family,
    HardwareVerdict,
    InstallMode,
    PresenceState,
    SecurityState,
    Severity,
    StepId,
)
from ..core.usb import FAST_LOOP_PERIOD
from . import naming

FAMILY_NAMES: Dict[Family, str] = {Family.ZKT: "ZKTeco", Family.HIKVISION: "Hikvision"}

# The install steps as the screen names them: api.STEP_LABELS, with "board software"
# where the engine contract says "firmware" (the word every other screen avoids).
STEP_WORDS: Dict[StepId, str] = {
    **api.STEP_LABELS,
    StepId.COPY_FIRMWARE: "Copying the board software",
}

SECURITY_WORDS: Dict[SecurityState, str] = {
    SecurityState.BLANK: "New. It has never been set up.",
    SecurityState.KEY_BURNED_NOT_STARTED: "Setup started but did not finish.",
    SecurityState.SECURED_STATE_LIFE: "Set up and locked for State Life.",
    SecurityState.SECURED_OTHER: "Locked by someone else.",
    SecurityState.UNSECURED_USED: "Used before, not by State Life.",
    SecurityState.UNKNOWN: "Could not tell.",
}

HARDWARE_WORDS: Dict[HardwareVerdict, str] = {
    HardwareVerdict.OK: "Correct. It is the kind State Life uses.",
    HardwareVerdict.WRONG_CHIP: "Wrong. State Life cannot use this kind.",
    HardwareVerdict.WRONG_FLASH_SIZE: "Wrong. State Life cannot use this kind.",
    HardwareVerdict.WRONG_PSRAM: "Wrong. State Life cannot use this kind.",
    HardwareVerdict.NOT_CHECKED: "Not checked. A locked board cannot show it.",
}

HARDWARE_OK = (HardwareVerdict.OK, HardwareVerdict.NOT_CHECKED)


def family_name(family: Optional[Family]) -> str:
    return FAMILY_NAMES.get(family, "Not known") if family else "Not known"


# ------------------------------------------------------------ console lines

_CONSOLE: Dict[ConsoleKind, Tuple[str, str, Severity]] = {
    # kind: (words with the line's data, words without it, how it looks)
    ConsoleKind.BOOT_BANNER: ("The board is starting.", "", Severity.INFO),
    ConsoleKind.PSRAM_OK: ("The board's memory is working.", "", Severity.SUCCESS),
    ConsoleKind.PSRAM_FAILED: ("The board's memory failed.", "", Severity.ERROR),
    ConsoleKind.SECURE_BOOT_FIRST_BOOT: (
        "The board is locking itself for State Life.",
        "",
        Severity.INFO,
    ),
    ConsoleKind.APP_STARTED: (
        "The board started for {zone_id}, device number {device_id}.",
        "The board started.",
        Severity.SUCCESS,
    ),
    ConsoleKind.PROVISIONING_LOADED: (
        "The board read its settings for {zone_id}.",
        "The board read its settings.",
        Severity.SUCCESS,
    ),
    ConsoleKind.APP_VERSION: ("Board software version {version}.", "", Severity.INFO),
    ConsoleKind.STORAGE_READY: ("The board's storage is ready.", "", Severity.SUCCESS),
    ConsoleKind.STORAGE_FAILED: ("The board's storage has a problem.", "", Severity.ERROR),
    ConsoleKind.NVS_KEY_DANGER: ("The board's security key is missing.", "", Severity.ERROR),
    ConsoleKind.NVS_FAILED: ("The board cannot read its settings.", "", Severity.ERROR),
    ConsoleKind.SETTINGS_MISSING: ("The board has no settings.", "", Severity.ERROR),
    ConsoleKind.FAMILY_MISMATCH: (
        "The settings are for the other machine type.",
        "",
        Severity.ERROR,
    ),
    ConsoleKind.WIFI_CONNECTING: (
        "Joining the Wi-Fi called {ssid}.",
        "Joining the Wi-Fi.",
        Severity.INFO,
    ),
    ConsoleKind.WIFI_CONNECTED: ("Joined the Wi-Fi.", "", Severity.SUCCESS),
    ConsoleKind.WIFI_RETRY: ("Still trying to join the Wi-Fi.", "", Severity.WARNING),
    ConsoleKind.SETUP_AP_OPEN: (
        "The board opened its own Wi-Fi called {ssid}.",
        "The board opened its own Wi-Fi.",
        Severity.INFO,
    ),
    ConsoleKind.ADD_CONNECTED: ("Connected to head office.", "", Severity.SUCCESS),
    ConsoleKind.ADD_FAILED: ("Could not reach head office yet.", "", Severity.WARNING),
    ConsoleKind.TERMINAL_FOUND: ("Found the attendance machine.", "", Severity.SUCCESS),
    ConsoleKind.TERMINAL_AUTH_FAILED: (
        "The attendance machine did not accept the password or comm key.",
        "",
        Severity.ERROR,
    ),
    ConsoleKind.TERMINAL_SECURE_PULL: (
        "The attendance machine asked for a secure connection.",
        "",
        Severity.INFO,
    ),
    ConsoleKind.TERMINAL_UNREACHABLE: (
        "Cannot reach the attendance machine yet.",
        "",
        Severity.WARNING,
    ),
    ConsoleKind.PANIC: ("The board hit a problem and restarted.", "", Severity.ERROR),
}


def console_words(event: ConsoleEvent) -> Optional[Tuple[str, Severity]]:
    """What a console line means in plain words; None for lines people need not see."""
    entry = _CONSOLE.get(event.kind)
    if entry is None:
        return None
    full, plain, severity = entry
    data = {key: str(value) for key, value in (event.data or {}).items() if value not in (None, "")}
    try:
        return full.format(**data), severity
    except (KeyError, IndexError, ValueError):
        # The line came without the detail the words need: say the short form, or nothing.
        return (plain, severity) if plain else None


# --------------------------------------------------------------- help topics


@dataclass(frozen=True)
class HelpItem:
    picture: Optional[str]
    heading: str
    text: str


@dataclass(frozen=True)
class HelpTopic:
    title: str
    items: Tuple[HelpItem, ...]


HELP_TOPICS: Dict[str, HelpTopic] = {
    "sign_in": HelpTopic(
        "Signing in",
        (
            HelpItem(
                None,
                "Which name and password?",
                "Use the same name and password as the attendance dashboard. "
                "Ask head office if you do not have them.",
            ),
            HelpItem(
                "no_internet",
                "This app needs the internet",
                "Look at the signs at the top of the window. Internet OK means this computer is "
                "online.",
            ),
            HelpItem(
                None,
                "Wrong password 3 times?",
                "The app waits 10 minutes before you can try again. This keeps the account safe.",
            ),
        ),
    ),
    "home": HelpTopic(
        "Getting started",
        (
            HelpItem(
                "cable", "Set up a new board", "Choose this for a board that has never been used."
            ),
            HelpItem(
                "light",
                "Check or fix a board",
                "Choose this when a board is not working, or to change its settings.",
            ),
            HelpItem(
                "usb_socket", "Connecting a board", "Always use the socket marked USB on the board."
            ),
        ),
    ),
    "machine": HelpTopic(
        "Which machine is it?",
        (
            HelpItem(
                "machine_zkt", "ZKTeco", "The word ZKTeco is printed on the front of the machine."
            ),
            HelpItem(
                "machine_hik",
                "Hikvision",
                "The word Hikvision is printed on the front of the machine.",
            ),
        ),
    ),
    "details": HelpTopic(
        "Filling in the details",
        (
            HelpItem(
                None,
                "ZONE ID",
                "Head office gives you the ZONE ID. Type it exactly, for example LF-ZONE-BLD9-03.",
            ),
            HelpItem(
                None,
                "ZONE NAME",
                "It is filled in from the ZONE ID. Change it only if head office asks you to. "
                "A name that starts with LF is in the Life group. Other names are in the Health "
                "group.",
            ),
            HelpItem(
                "machine_hik",
                "Hikvision machine address",
                "On the Hikvision machine, open the menu and look for Network. "
                "The address looks like 192.168.1.20.",
            ),
            HelpItem(
                None,
                "Advanced settings",
                "Leave them as they are unless head office tells you to change them.",
            ),
        ),
    ),
    "connect": HelpTopic(
        "Connecting the board",
        (
            HelpItem(
                "cable",
                "Use the right cable",
                "Use the cable that came with the board. Some phone cables only charge and will "
                "not work.",
            ),
            HelpItem(
                "usb_socket",
                "Use the socket marked USB",
                "The board has two sockets that look the same. Read the small word next to them. "
                "Use the one marked USB, not COM.",
            ),
            HelpItem(
                None,
                "Plug straight into the computer",
                "Do not use a USB hub or an extension cable. If nothing happens, try another "
                "socket "
                "on the computer.",
            ),
        ),
    ),
    "checking": HelpTopic(
        "If the board does not answer",
        (
            HelpItem(
                "boot_button",
                "Put the board in setup mode by hand",
                "Hold down the BOOT button. While you hold it, press and let go of the RST button. "
                "Then let go of BOOT and click Try again.",
            ),
            HelpItem(
                "replug",
                "Still nothing?",
                "Unplug the board, wait 3 seconds and plug it back in. Then click Try again.",
            ),
        ),
    ),
    "confirm": HelpTopic(
        "What happens next",
        (
            HelpItem(
                "cable_in",
                "The app sets up the board",
                "The app copies the software and the settings to the board. Then it locks the "
                "board, "
                "so that only State Life can use it. This cannot be undone.",
            ),
        ),
    ),
    "installing": HelpTopic(
        "While the board is set up",
        (
            HelpItem(
                "cable_in",
                "Keep the cable plugged in",
                "Do not unplug the board or close this window until the app tells you to.",
            ),
        ),
    ),
    "first_start": HelpTopic(
        "Starting the board",
        (
            HelpItem("unplug", "1. Unplug", "Pull the cable out of the board."),
            HelpItem("wait", "2. Wait", "Wait 3 seconds."),
            HelpItem(
                "replug",
                "3. Plug it back in",
                "Push the cable back into the socket marked USB. Do not press any button.",
            ),
            HelpItem(
                "light",
                "4. Watch the light",
                "The light on the board comes on after a few seconds.",
            ),
            # No picture: the steps are the same as 1 to 3, and item 3 shows plugging in.
            HelpItem(
                None,
                "5. Once more",
                "When the board has started, the app asks you to unplug it and plug it back in "
                "one more time. This checks that the board keeps its settings. If the board "
                "loses its settings, the app tells you what to do.",
            ),
        ),
    ),
    "done": HelpTopic(
        "At the site",
        (
            HelpItem(
                "hotspot",
                "Choosing the site's Wi-Fi",
                "If the board cannot join a Wi-Fi, it opens its own Wi-Fi after 2 minutes. "
                "Join it from a phone and choose the site's Wi-Fi.",
            ),
            HelpItem(
                "boot_button",
                "Open the board's Wi-Fi yourself",
                "Hold the BOOT button for 5 seconds while the board is on.",
            ),
        ),
    ),
    "check_board": HelpTopic(
        "Checking a board",
        (
            HelpItem(
                "light",
                "What is the light doing?",
                "The light guide tells you what each light means.",
            ),
            HelpItem(
                "replug",
                "Watch it start",
                "Unplug the board and plug it back in to see what it does while it starts.",
            ),
            HelpItem(
                "hotspot",
                "The board's own Wi-Fi",
                "Its name and password are on this screen. Use them on a phone to choose the "
                "site's Wi-Fi.",
            ),
        ),
    ),
    "fix_settings": HelpTopic(
        "Changing settings",
        (
            HelpItem(
                None,
                "What changes",
                "The app writes new settings to the board. The software on the board stays the "
                "same. Head office knows only the ZONE ID, the ZONE NAME and the device number. "
                "Type the comm key or the Hikvision machine's details from the attendance "
                "machine.",
            ),
            HelpItem(
                "hotspot",
                "The site's Wi-Fi",
                "The board forgets the site's Wi-Fi. At the site, choose it again with a phone "
                "and the board's own Wi-Fi.",
            ),
            HelpItem(
                None,
                "Punches on the board",
                "If the board was used at a site, punches that were not sent yet will be lost. "
                "Check that the board was online recently.",
            ),
        ),
    ),
    "light_guide": HelpTopic(
        "The light on the board",
        (
            HelpItem(
                "light",
                "Where is the light?",
                "It is the small light between the BOOT and RST buttons.",
            ),
            HelpItem(
                None,
                "Colours that look alike",
                "Yellow, amber, orange and red-orange can look the same. If you are not sure, "
                "connect the board and choose Check or fix a board.",
            ),
        ),
    ),
}


# ---------------------------------------------------------- site wifi steps


def portal_steps(hotspot: str) -> Tuple[str, ...]:
    """How to choose the site's Wi-Fi through the board's own Wi-Fi.

    Follows firmware/zone_lite/main/setup_portal.c: the hotspot opens after
    2 minutes without Wi-Fi (or after holding BOOT for 5 seconds), answers at
    192.168.254.1, lists 2.4 GHz networks, tests the chosen one for up to 30
    seconds, saves it and restarts.
    """
    return (
        "Plug the board into power near the attendance machine. Wait 2 minutes.",
        f"On a phone, open the Wi-Fi list and join {hotspot} with the board's Wi-Fi password "
        "shown here.",
        f"The Wi-Fi setup page opens by itself. If it does not, open "
        f"{naming.SETUP_PAGE_ADDRESS} in the phone's browser.",
        "Choose the site's Wi-Fi from the list and type its password. It must be a 2.4 GHz "
        "network.",
        "Tap Test connection and save. Wait up to 30 seconds.",
        "When it says the network is saved, the board restarts and joins the Wi-Fi.",
    )


# ------------------------------------------------------------------ fallbacks


def fast_restart_loop(report: api.PresenceReport) -> bool:
    """A restart loop of a few seconds: power, cable or a start-up problem, not the faulty board.

    The known faulty board restarts about every 10 seconds (its watchdog), silently. A
    faster loop is first a cable or USB-socket problem, and may be a board whose software
    stops at the start, which checking it (setup mode) or new settings can still fix. The
    limit is the engine's own (core/usb.py, core/diagnosis.py), so the words agree.
    """
    period = report.loop_period_s
    return (
        report.state is PresenceState.RESTART_LOOP
        and period is not None
        and period < FAST_LOOP_PERIOD[1]
    )


def presence_advice(report: api.PresenceReport) -> Optional[Advice]:
    state = report.state
    if state is PresenceState.NO_BOARD:
        return Advice(
            "NO_BOARD",
            "The board is not showing up",
            "This computer cannot see a board yet.",
            (
                "Use the cable that came with the board.",
                "Plug it into the socket marked USB on the board.",
                "Plug the other end straight into this computer, not into a hub.",
                "Try another socket on the computer.",
                "If nothing changes after 3 tries, the board may be faulty. "
                "Put it aside and use another.",
            ),
            Severity.WARNING,
            "cable",
        )
    if state is PresenceState.WRONG_SOCKET:
        return Advice(
            "WRONG_SOCKET",
            "The cable is in the wrong socket",
            "The board has two sockets. The cable is in the one marked COM.",
            (
                "Unplug the cable from the board.",
                "Plug it into the other socket, the one marked USB.",
            ),
            Severity.WARNING,
            "usb_socket",
        )
    if state is PresenceState.SEVERAL_BOARDS:
        return Advice(
            "SEVERAL_BOARDS",
            "More than one board is connected",
            "This computer can see more than one board.",
            ("Unplug every board except the one you want to use.",),
            Severity.WARNING,
            "unplug",
        )
    if state is PresenceState.RESTART_LOOP and fast_restart_loop(report):
        return Advice(
            "RESTART_LOOP_FAST",
            "The board keeps restarting",
            "It switches off and on every few seconds. Usually the cable or the socket on this "
            "computer does not give it enough power.",
            (
                "Use a short cable, straight into this computer, not a hub.",
                "Try another USB socket on this computer.",
                "Then click Check again.",
            ),
            Severity.WARNING,
            "cable",
        )
    if state is PresenceState.RESTART_LOOP:
        return Advice(
            "BOARD_FAULTY",
            "This board is faulty",
            "It keeps switching off and on by itself. It cannot be used.",
            (
                "Unplug the board.",
                "Put a FAULTY label on it and give it back to head office.",
                "Connect a new board.",
            ),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    return None


def board_refusal(board: api.BoardInfo) -> Optional[Advice]:
    """Why a board cannot be used at all, or None if it can."""
    if board.hardware not in HARDWARE_OK:
        return Advice(
            "WRONG_BOARD_TYPE",
            "This is the wrong kind of board",
            "It is not the model State Life uses.",
            ("Unplug it.", "Use one of the boards head office gave you."),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    if board.security is SecurityState.SECURED_OTHER:
        return Advice(
            "BOARD_LOCKED_BY_OTHERS",
            "This board belongs to someone else",
            "It was locked by another company. It cannot be set up for State Life.",
            ("Unplug it and put it aside.", "Use another board."),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    if board.security is SecurityState.UNSECURED_USED:
        return Advice(
            "BOARD_USED_ELSEWHERE",
            "This board was used for something else",
            "It is not new, and State Life did not set it up. It is not safe to use.",
            ("Unplug it and put it aside.", "Use a new board, or call head office."),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    if board.security is SecurityState.UNKNOWN:
        return Advice(
            "BOARD_UNCLEAR",
            "The app could not read this board",
            "The board answered, but not clearly.",
            ("Unplug the board, wait 3 seconds and plug it back in.", "Click Try again."),
            Severity.WARNING,
            "replug",
        )
    return None


def family_mismatch(known: Family, chosen: Family) -> Advice:
    """The board is ADD's other machine type; the screen offers "Choose <known>"."""
    other, wanted = family_name(known), family_name(chosen)
    return Advice(
        "FAMILY_MISMATCH",
        f"This board is for a {other} machine",
        f"Head office has this board set up for a {other} machine. "
        f"It cannot be set up for a {wanted} machine.",
        (f"Click Choose {other} to go on with this board.", "Or use another board."),
        Severity.ERROR,
        None,
        can_retry=False,
    )


def settings_change_blocked(
    record: Optional[api.DeviceRecord], lookup_failed: bool = False
) -> Optional[Advice]:
    """Why a set-up board cannot get new settings now, or None when it can.

    Only head office's answer can say whether punches would be lost and whether new
    settings would suit the board (a board locked before this app may use another
    security root and would then never start again: review B1), so no answer means no.
    """
    if lookup_failed or record is None:
        return Advice(
            "SETTINGS_NEED_HEAD_OFFICE",
            "Settings cannot be changed now",
            "Head office could not be reached, so settings cannot be changed now.",
            ("Check that this computer is on the internet.", "Then click Try again."),
            Severity.WARNING,
            "head_office",
        )
    if not record.settings_change_allowed:
        return _settings_not_allowed(record.settings_change_reason)
    return None


def resume_blocked(record: Optional[api.DeviceRecord]) -> Optional[Advice]:
    """Why a locked board that never started cannot be finished here, or None when it can.

    ADD finishes only a setup that stopped halfway. A board it has seen online was set
    up some other way, may use another security root (review F1), and ADD refuses it.
    """
    if record is not None and record.known and record.ever_online:
        return _settings_not_allowed(record.settings_change_reason)
    return None


def install_blocked(candidate: Optional[api.UsbCandidate]) -> Optional[Advice]:
    """Why no setup, finish or settings change may start on this board, or None when it can.

    The engine writes only to the port that has the board's USB board ID (core/installer.py),
    so a board this computer gives no ID for would stop at its first step. The screens say
    so before anything starts, in the engine's own words.
    """
    if candidate is not None and candidate.mac is None:
        return texts.BOARD_ID_NOT_READABLE
    return None


def _settings_not_allowed(reason: Optional[str]) -> Advice:
    return Advice(
        "SETTINGS_CHANGE_NOT_ALLOWED",
        "This app cannot change this board's settings",
        reason or "Head office does not allow new settings for this board from this app.",
        ("Leave the board as it is.", "Call head office and tell them the board ID."),
        Severity.WARNING,
        "head_office",
        can_retry=False,
    )


def zone_not_writable(zone_id: str) -> Advice:
    """Head office's ZONE ID has a character board settings cannot hold (review E16)."""
    return Advice(
        "ZONE_ID_NOT_WRITABLE",
        "This app cannot write settings for this board",
        f"Head office uses the ZONE ID {zone_id}. It has a character that board settings "
        "cannot hold, so this app cannot write new settings for this board.",
        ("Do not change the ZONE ID.", "Call head office."),
        Severity.ERROR,
        None,
        can_retry=False,
    )


def no_bundles(family: Optional[Family]) -> Advice:
    return Advice(
        "NO_BOARD_SOFTWARE",
        "No board software is ready",
        f"Head office has no software ready for {family_name(family)} machines yet.",
        ("Call head office.", "Then click Try again."),
        Severity.WARNING,
    )


def unexpected() -> Advice:
    return Advice(
        "UNEXPECTED",
        "Something went wrong",
        "The app hit a problem it did not expect.",
        ("Try again.", "If it happens again, open Help and save a help file for head office."),
        Severity.ERROR,
    )


def setup_stopped(mode: Optional[InstallMode] = None) -> Advice:
    if mode is InstallMode.CHANGE_SETTINGS:
        # The board was locked long ago; what matters is that nothing was written (E10).
        return Advice(
            "SETUP_STOPPED",
            "Nothing was changed",
            "You stopped before the new settings were written. The board keeps its old settings.",
            ("Click Try again to write them, or go back to the start.",),
            Severity.INFO,
        )
    return Advice(
        "SETUP_STOPPED",
        "Setup stopped",
        "You stopped the setup before the board was locked. The board is safe.",
        ("You can start again when you are ready.",),
        Severity.INFO,
    )


def installer_off() -> Advice:
    return Advice(
        "INSTALLER_OFF",
        "Board setup is switched off",
        "Head office has switched off board setup for now.",
        (
            "You can still check boards.",
            "Call head office to switch it on.",
            "Then click Check again.",
        ),
        Severity.WARNING,
        can_retry=False,
    )


def settings_service_down() -> Advice:
    return Advice(
        "SETTINGS_SERVICE_DOWN",
        "Head office cannot make board settings right now",
        "Setting up a board will not work until this is fixed.",
        (
            "Wait a few minutes, then click Check again.",
            "Call head office if it lasts more than an hour.",
        ),
        Severity.WARNING,
    )


def app_too_old(min_version: str) -> Advice:
    return Advice(
        "APP_TOO_OLD",
        "This app is too old",
        f"Head office needs version {min_version} or newer.",
        ("Ask head office for the new version of this app.",),
        Severity.ERROR,
        can_retry=False,
    )


def untested_model(which: str) -> Advice:
    """``which`` is "the DS-K1T341AMF" or "this model"."""
    return Advice(
        "MODEL_NOT_TESTED",
        "This model is not fully tested yet",
        f"State Life boards have not been fully tested with {which}. It may still work.",
        ("Tell head office after the setup, so they can check it.",),
        Severity.WARNING,
    )


def network_words(status: Optional[api.ConnectivityStatus]) -> Optional[Tuple[str, str]]:
    """(title, words) for a notice while the internet or head office is down (E14).

    The words are the check's own (for example a hotel or office network that wants
    its own sign-in page first), which the red chips have no room for.
    """
    if status is None or (status.internet and status.server):
        return None
    if not status.internet:
        return "No internet", status.detail or "This computer is not connected to the internet."
    return "Head office is not answering", (
        status.detail or "The internet works, but head office is not answering."
    )


def network_problem(status: api.ConnectivityStatus) -> Advice:
    """Sign-in failed while the internet check failed too: the network is the problem.

    A network with its own sign-in page breaks the secure connection, which would
    otherwise be blamed on the computer's clock.
    """
    title, words = network_words(status) or ("No internet", status.detail)
    return Advice(
        "NO_CONNECTION",
        title,
        words,
        ("When the connection works, click Try again.",),
        Severity.WARNING,
        "no_internet",
    )


def restart_check(first: api.BootReport) -> Advice:
    """The card over the second replug, once the first start has passed.

    The restart check is deliberate: the board must keep its settings when it starts
    again (core/console.py judge). The card must not read as the end of the setup: only
    the second start may say the board started correctly. It says the setup is not
    finished, for whoever would take the board away now. The page's subtitle says why
    the check is made, so the card does not repeat it.
    """
    if first.zone_id_seen:
        device = f", device number {first.device_id_seen}" if first.device_id_seen else ""
        happened = f"The board started for {first.zone_id_seen}{device}."
    else:
        # The start line, with the zone, came before the app could read the board.
        happened = "The board started."
    return Advice(
        "RESTART_CHECK",
        "One more check",
        f"{happened} The setup is not finished. Do not take the board to a site yet.",
        (texts.RESTART_REPLUG, "Do not press any button."),
        Severity.INFO,
        None,
    )


def start_checks_ok(first: api.BootReport, second: api.BootReport) -> Advice:
    """The last card of the first-start screen: both starts passed.

    One sentence a line, so the ZONE ID, which the person checks against head office's
    list, never breaks at one of its hyphens.
    """
    zone = second.zone_id_seen or first.zone_id_seen
    device = second.device_id_seen or first.device_id_seen
    version = second.app_version_seen or first.app_version_seen
    seen = [texts.SETTINGS_KEPT]
    if zone:
        number = f", device number {device}" if device else ""
        seen.append(f"Its settings are for {zone}{number}.")
    if version:
        seen.append(f"Its software is version {version}.")
    return Advice(
        "START_CHECKS_OK",
        "The board kept its settings",
        "\n".join(seen),
        (),
        Severity.SUCCESS,
        "success",
    )


DONE_SUBTITLE = "Take the board to the site. Then follow the steps below."


def done_subtitle(kept_settings: bool) -> str:
    """The Done screen's lead. It says the board kept its settings (texts.SETTINGS_KEPT)
    only once the restart check passed, and in the subtitle, so what the person needs at
    the site stays in view on a small screen."""
    return f"{texts.SETTINGS_KEPT} {DONE_SUBTITLE}" if kept_settings else DONE_SUBTITLE


def boot_fallback(
    report: api.BootReport,
    expect: Optional[api.InstallConfig],
    board_id: Optional[str] = None,
) -> Advice:
    """Advice for a watched start that went wrong, when the engine gave none.

    ``board_id`` goes on the label of a board that lost its settings.
    """
    outcome = report.outcome
    if outcome is BootOutcome.SETTINGS_LOST:
        return texts.settings_lost_on_restart(board_id)
    if outcome is BootOutcome.SETTINGS_NOT_SEEN:
        return texts.SETTINGS_NOT_SEEN
    if outcome is BootOutcome.WRONG_SETTINGS:
        seen = report.zone_id_seen or "another zone"
        wanted = expect.zone_id if expect else "the zone you chose"
        return Advice(
            "BOOT_WRONG_SETTINGS",
            "The board started with other settings",
            f"It started for {seen}, not {wanted}.",
            (
                "Click Fix settings to write the right settings.",
                "If it happens again, call head office.",
            ),
            Severity.ERROR,
        )
    if outcome is BootOutcome.NO_OUTPUT:
        return Advice(
            "BOOT_SILENT",
            "The board did not start",
            "It is plugged in, but it did not say anything.",
            (
                "Unplug the board, wait 3 seconds and plug it back in.",
                "Do not press any button.",
                "Click Try again.",
            ),
            Severity.WARNING,
            "replug",
        )
    if outcome is BootOutcome.FAULTY_RESTART_LOOP:
        return Advice(
            "BOARD_FAULTY",
            "This board is faulty",
            "It keeps switching off and on by itself.",
            ("Unplug the board.", "Put a FAULTY label on it and give it back to head office."),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    if outcome is BootOutcome.SECURITY_KEY_PROBLEM:
        return Advice(
            "BOOT_KEY_PROBLEM",
            "The board's security key does not match",
            "The board cannot read its own settings.",
            ("Do not set it up again.", "Put it aside and call head office."),
            Severity.ERROR,
            "faulty_board",
            can_retry=False,
        )
    if outcome is BootOutcome.STORAGE_PROBLEM:
        return Advice(
            "BOOT_STORAGE_PROBLEM",
            "The board's storage did not start",
            "The setup did not finish properly.",
            ("Click Fix settings to prepare the storage again.",),
            Severity.ERROR,
        )
    if outcome is BootOutcome.FAMILY_MISMATCH:
        return Advice(
            "BOOT_FAMILY_MISMATCH",
            "The software and settings do not match",
            "The board has software for one machine type and settings for the other.",
            ("Click Fix settings and choose the right machine type.",),
            Severity.ERROR,
        )
    if outcome is BootOutcome.NOT_REPLUGGED:
        return Advice(
            "BOOT_NOT_REPLUGGED",
            "The board was not plugged back in",
            "The app waited, but the board did not come back.",
            ("Click Try again and follow the steps.",),
            Severity.WARNING,
            "replug",
        )
    return Advice(
        "BOOT_STOPPED",
        "Stopped",
        "The app stopped watching the board.",
        ("Click Try again.",),
        Severity.INFO,
    )
