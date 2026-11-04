"""Turns what the app knows about a board into an ordered list of Advice.

Inputs: the USB presence report, the board inspection (if any), ADD's record for
the board (if any) and console events (oldest first). The most important advice
comes first: errors, then warnings, then information and good news; within a
level, the order of the rules below.

Console rules look at the latest state, not the history: a board that failed to
reach the attendance machine and then found it is shown as connected.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from . import texts
from .api import (Advice, BoardInfo, ConsoleEvent, ConsoleKind, DeviceRecord, HardwareVerdict,
                  PresenceReport, PresenceState, SecurityState, Severity)
from .board import hardware_advice
from .console import crash_loop, refusal_advice, startup_refusal
from .naming import hotspot_name
from .validation import version_tuple

NO_BOARD_TIPS_AFTER_S = 15.0
WIFI_RETRIES_BEFORE_WORRY = 3
_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2,
                  Severity.SUCCESS: 3}
_TERMINAL_KINDS = (ConsoleKind.TERMINAL_FOUND, ConsoleKind.TERMINAL_AUTH_FAILED,
                   ConsoleKind.TERMINAL_UNREACHABLE, ConsoleKind.TERMINAL_SECURE_PULL)


class Diagnoser:
    """DiagnosisServiceProto."""

    def __init__(self, placeholder_ssids: Sequence[str] = ("RE7",)) -> None:
        self._placeholders = tuple(placeholder_ssids)

    def diagnose(self, board: Optional[BoardInfo], presence: PresenceReport,
                 device: Optional[DeviceRecord],
                 events: Sequence[ConsoleEvent]) -> List[Advice]:
        mac = board.mac if board else (presence.board.mac if presence.board else None)
        found: List[Tuple[int, Advice]] = []
        rules: Sequence[Callable[[], Optional[Advice]]] = (
            lambda: _presence(presence),
            lambda: _hardware(board),
            lambda: _security(board),
            *[(lambda rule=rule: rule(events)) for rule in _CONSOLE_FAULTS],
            lambda: _wifi(events, self._placeholders, mac),
            lambda: _head_office(events),
            lambda: _terminal(events),
            lambda: _device(device, board),
            lambda: _light(events),
        )
        for order, rule in enumerate(rules):
            advice = rule()
            if advice is not None:
                found.append((order, advice))
        found.sort(key=lambda item: (_SEVERITY_RANK[item[1].severity], item[0]))
        seen = set()
        ordered = []
        for _order, advice in found:
            if advice.code not in seen:
                seen.add(advice.code)
                ordered.append(advice)
        return ordered


# ------------------------------------------------------------------- rules

def _presence(presence: PresenceReport) -> Optional[Advice]:
    state = presence.state
    if state is PresenceState.RESTART_LOOP:
        period = presence.loop_period_s
        if period is not None and period < 6.0:
            return texts.restart_loop_fast(period)
        return texts.restart_loop_faulty(period)
    if state is PresenceState.SEVERAL_BOARDS:
        return texts.SEVERAL_BOARDS
    if state is PresenceState.OTHER_USB_MODE:
        return texts.SETUP_MODE_NEEDED
    if state is PresenceState.WRONG_SOCKET:
        return texts.WRONG_SOCKET
    if state is PresenceState.NO_BOARD:
        if presence.seconds_in_state >= NO_BOARD_TIPS_AFTER_S:
            return texts.NO_BOARD_TIPS
        return texts.NO_BOARD
    return None


def _hardware(board: Optional[BoardInfo]) -> Optional[Advice]:
    """The hardware gate's refusal (board.py), with what it measured."""
    if board is None or board.hardware in (HardwareVerdict.OK, HardwareVerdict.NOT_CHECKED):
        return None
    return hardware_advice(board)


def _security(board: Optional[BoardInfo]) -> Optional[Advice]:
    if board is None:
        return None
    if board.security is SecurityState.SECURED_OTHER:
        return texts.SECURED_OTHER
    if board.security is SecurityState.UNSECURED_USED:
        return texts.UNSECURED_USED
    if board.security is SecurityState.UNKNOWN:
        if board.evidence.get("note") == "FIRST_START_UNFINISHED":
            return texts.FIRST_START_UNFINISHED
        return texts.SECURITY_UNKNOWN
    if board.security is SecurityState.KEY_BURNED_NOT_STARTED:
        return texts.UNFINISHED_SETUP
    return None


def _kinds(events: Sequence[ConsoleEvent]) -> set:
    return {event.kind for event in events}


def _last(events: Sequence[ConsoleEvent], kinds: Sequence[ConsoleKind]
          ) -> Optional[ConsoleEvent]:
    for event in reversed(events):
        if event.kind in kinds:
            return event
    return None


def _signal(events: Sequence[ConsoleEvent], name: str) -> bool:
    return any(event.data.get("signal") == name for event in events)


def _startup_refused(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    # The same rule as the first start's verdict (console.judge): before power advice.
    refused = startup_refusal(events)
    return refusal_advice(refused) if refused is not None else None


def _key_lost(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    return texts.SECURITY_KEY_LOST if ConsoleKind.NVS_KEY_DANGER in _kinds(events) else None


def _family(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    return texts.FAMILY_MISMATCH_LOOP if ConsoleKind.FAMILY_MISMATCH in _kinds(events) else None


def _settings(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    if _kinds(events) & {ConsoleKind.NVS_FAILED, ConsoleKind.SETTINGS_MISSING}:
        return texts.SETTINGS_UNREADABLE
    return None


def _psram(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    return texts.PSRAM_FAILED if ConsoleKind.PSRAM_FAILED in _kinds(events) else None


def _power(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    if any(event.kind is ConsoleKind.PANIC and event.data.get("reason") == "brownout"
           for event in events):
        return texts.POWER_PROBLEM
    return None


def _crashes(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    # The same rule as the first start's verdict (console.judge), so the two agree.
    return texts.CRASH_LOOP if crash_loop(events) else None


def _download_mode(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    stuck = any(event.data.get("download_mode") == "1" for event in events)
    started = ConsoleKind.APP_STARTED in _kinds(events)
    return texts.DOWNLOAD_MODE_STUCK if stuck and not started else None


def _storage(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    last = _last(events, (ConsoleKind.STORAGE_READY, ConsoleKind.STORAGE_FAILED))
    if last is not None and last.kind is ConsoleKind.STORAGE_FAILED:
        return texts.STORAGE_PROBLEM
    return None


def _port_busy(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    return texts.PORT_BUSY if _signal(events, "port_busy") else None


def _test_firmware(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    return texts.TEST_FIRMWARE if _signal(events, "portal_disabled") else None


def _start_copy(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    banners = [event for event in events if event.kind is ConsoleKind.BOOT_BANNER
               and event.data.get("slot")]
    version = _last(events, (ConsoleKind.APP_VERSION,))
    if not banners:
        return None
    project = version.data.get("project") if version else None
    # ZKT boards run the release from ota_0; booting factory means otadata is wrong.
    if project == "zone_lite" and banners[-1].data.get("slot") == "factory":
        return texts.WRONG_START_COPY
    return None


_CONSOLE_FAULTS = (_startup_refused, _key_lost, _family, _settings, _psram, _power, _crashes,
                   _download_mode, _storage, _port_busy, _test_firmware, _start_copy)


def _wifi(events: Sequence[ConsoleEvent], placeholders: Sequence[str],
          mac: Optional[str]) -> Optional[Advice]:
    hotspot = hotspot_name(mac)
    last = _last(events, (ConsoleKind.WIFI_CONNECTING, ConsoleKind.WIFI_RETRY,
                          ConsoleKind.WIFI_CONNECTED, ConsoleKind.SETUP_AP_OPEN))
    if last is None or last.kind is ConsoleKind.WIFI_CONNECTED:
        return None
    if last.kind is ConsoleKind.SETUP_AP_OPEN:
        return texts.hotspot_open(last.data.get("ssid") or hotspot)
    connecting = _last(events, (ConsoleKind.WIFI_CONNECTING,))
    ssid = connecting.data.get("ssid", "") if connecting else ""
    if ssid in placeholders or not ssid:
        return texts.wifi_placeholder(ssid or "(no name)", hotspot)
    retries = [event for event in events if event.kind is ConsoleKind.WIFI_RETRY]
    if len(retries) >= WIFI_RETRIES_BEFORE_WORRY:
        return texts.wifi_not_joining(ssid, hotspot)
    return None


def _head_office(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    last = _last(events, (ConsoleKind.ADD_CONNECTED, ConsoleKind.ADD_FAILED))
    if last is None:
        return None
    if last.kind is ConsoleKind.ADD_CONNECTED:
        return texts.HEAD_OFFICE_CONNECTED
    if last.data.get("reason") == "time":
        return texts.TIME_SERVICE_BLOCKED
    if last.data.get("status") in ("401", "403"):
        return texts.HEAD_OFFICE_REJECTED
    return texts.HEAD_OFFICE_UNREACHABLE


def _app_version(events: Sequence[ConsoleEvent]) -> Optional[Tuple[str, Optional[str]]]:
    version = _last(events, (ConsoleKind.APP_VERSION,))
    if version is None:
        return None
    return version.data.get("version", ""), version.data.get("project")


def _terminal(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    last = _last(events, _TERMINAL_KINDS)
    if last is None:
        return None
    app = _app_version(events)
    # Hikvision poll lines carry a stage; ZKT lines never do.
    hikvision = (app is not None and app[1] == "zone_lite_hikvision") or "stage" in last.data
    if last.kind is ConsoleKind.TERMINAL_FOUND:
        return texts.TERMINAL_CONNECTED
    secure_pull = ConsoleKind.TERMINAL_SECURE_PULL in _kinds(events)
    if last.kind is ConsoleKind.TERMINAL_SECURE_PULL or (
            secure_pull and last.kind is ConsoleKind.TERMINAL_AUTH_FAILED
            and last.data.get("code") in ("6001", None)):
        if app is None or _older_than(app[0], "2.8.0"):
            return texts.NEEDS_ZKT_280
        return texts.COMM_KEY_WRONG
    if last.kind is ConsoleKind.TERMINAL_AUTH_FAILED:
        if last.data.get("reason") == "serial":
            return texts.TERMINAL_SERIAL_MISMATCH
        if hikvision or last.data.get("reason") == "authentication_rejected":
            return texts.HIK_PASSWORD_WRONG
        return texts.COMM_KEY_WRONG
    if last.data.get("reason") == "invalid_configuration":
        return texts.HIK_SETTINGS_INCOMPLETE
    if hikvision or last.data.get("reason") == "network_timeout_or_unreachable":
        return texts.HIK_NOT_REACHABLE
    return texts.TERMINAL_NOT_FOUND


def _older_than(version: str, other: str) -> bool:
    try:
        return version_tuple(version) < version_tuple(other)
    except ValueError:
        return True


def _device(device: Optional[DeviceRecord], board: Optional[BoardInfo]) -> Optional[Advice]:
    if device is None:
        return None
    if device.known and device.online:
        return texts.BOARD_ONLINE
    if device.known and device.ever_online:
        return texts.board_offline(device.status_text)
    if board is not None and board.security is SecurityState.SECURED_STATE_LIFE:
        return texts.BOARD_NOT_SEEN
    return None


def likely_light(events: Sequence[ConsoleEvent]) -> Optional[texts.LedState]:
    """The light pattern the board most likely shows, judged from its console."""
    kinds = _kinds(events)
    by_key = {state.key: state for state in texts.LED_GUIDE}
    if kinds & {ConsoleKind.NVS_FAILED, ConsoleKind.SETTINGS_MISSING}:
        return by_key["FATAL"]
    if _storage(events) is not None:
        return by_key["LOCAL_FAILURE"]
    terminal = _last(events, _TERMINAL_KINDS)
    if terminal is not None and terminal.kind is ConsoleKind.TERMINAL_AUTH_FAILED:
        return by_key["ZKT_FAILURE"]
    wifi = _last(events, (ConsoleKind.WIFI_CONNECTING, ConsoleKind.WIFI_RETRY,
                          ConsoleKind.WIFI_CONNECTED, ConsoleKind.SETUP_AP_OPEN))
    if wifi is not None and wifi.kind is not ConsoleKind.WIFI_CONNECTED:
        return by_key["WIFI_CONNECTING"]
    if terminal is not None and terminal.kind is ConsoleKind.TERMINAL_FOUND:
        return by_key["HEALTHY"] if terminal.data.get("live") == "1" else \
            by_key["ZKT_AUTHENTICATED"]
    if wifi is not None:
        return by_key["ZKT_DISCOVERING"]
    if ConsoleKind.APP_STARTED in kinds:
        return by_key["BOOTING"]
    return None


def _light(events: Sequence[ConsoleEvent]) -> Optional[Advice]:
    state = likely_light(events)
    return texts.light_hint(state) if state is not None else None
