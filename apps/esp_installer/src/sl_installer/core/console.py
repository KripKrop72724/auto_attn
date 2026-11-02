"""Reads the board's console and decides how its first start went.

The app console is a copy on the native USB-Serial/JTAG port (UART0 on the other
socket is the main one). The USB copy drops text nobody reads and the port
disappears on every chip reset, so the port is reopened as fast as it comes back.
Even so, a start's first lines can be gone before the PC lists the port again: the
start line comes under 2 s after power-on. The verdict (judge) therefore also takes
any later line of the running program as the start.

Opening must not move DTR/RTS: on USB-Serial/JTAG, RTS high with DTR low resets
the chip. Windows (usbser.sys) applies both lines in one request, so they are
held low as the repo's PowerShell tools do. macOS raises both lines when a tty
opens and pyserial would then drop DTR before RTS, passing through "reset", so
there they are kept high (the 29 Oct 2026 bench finding).

Patterns match the exact firmware format strings (ESP_LOGx in
firmware/zone_lite/main) and ESP-IDF 5.5.3's boot, PSRAM, NVS and panic lines.

A crash does not drop the port: ESP-IDF 5.5.3 restarts the ESP32-S3 after a panic
with a CPU reset that keeps USB-Serial/JTAG connected (esp_restart_noos), so the
console's own "Rebooting..." and ROM reset lines are what show a crash loop, not a
port reopen.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple

from . import texts
from .api import (Advice, BootOutcome, BootReport, CancelToken, ConsoleEvent, ConsoleKind,
                  EventFn, InstallConfig, PhaseFn, ReplugPhase, UsbCandidate)
from .clock import Clock
from .keepawake import keep_awake
from .logging_setup import get_logger, redact
from .naming import hotspot_name, normalize_mac

log = get_logger("console")

BAUD = 115200
UNPLUG_TIMEOUT_S = 300.0
REPLUG_TIMEOUT_S = 300.0
STORAGE_WAIT_S = 25.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_IDF = re.compile(r"^([EWIDV]) \((\d+)\) ([^:]+?): (.*)$")
_SLOTS = {0x20000: "factory", 0x2A0000: "ota_0", 0x520000: "ota_1"}


@dataclass(frozen=True)
class _Rule:
    tag: Optional[Pattern]
    pattern: Pattern
    kind: ConsoleKind
    keys: Tuple[str, ...] = ()
    extra: Tuple[Tuple[str, str], ...] = ()


def _rule(tag: Optional[str], pattern: str, kind: ConsoleKind, *keys: str,
          **extra: str) -> _Rule:
    return _Rule(re.compile(tag) if tag else None, re.compile(pattern), kind, keys,
                 tuple(extra.items()))


_HIK_REASON = re.compile(r"^Poll retrying; reason=(\w+); stage=(\w+); http=(\d+)")

_RULES: Sequence[_Rule] = (
    # ROM and second-stage bootloader
    _rule(None, r"^ESP-ROM:(\S+)", ConsoleKind.BOOT_BANNER, "rom"),
    _rule(None, r"^rst:0x([0-9a-fA-F]+) \(([A-Z0-9_]+)\),boot:0x([0-9a-fA-F]+)",
          ConsoleKind.BOOT_BANNER, "reset_code", "reset_reason", "boot_mode"),
    _rule(None, r"waiting for download", ConsoleKind.BOOT_BANNER, download_mode="1"),
    _rule(r"^boot$", r"^ESP-IDF (\S+) 2nd stage bootloader", ConsoleKind.BOOT_BANNER, "idf"),
    _rule(r"^boot$", r"^Loaded app from partition at offset 0x([0-9a-fA-F]+)",
          ConsoleKind.BOOT_BANNER, "offset"),
    _rule(r"^main_task$", r"^Calling app_main\(\)", ConsoleKind.BOOT_BANNER, app_main="1"),
    # Secure Boot being switched on by the first boot ("permanently enabled" is logged
    # under the bootloader's "boot" tag, the rest under secure_boot*).
    _rule(r"^(secure_boot\w*|boot)$", r"enabling secure boot v2|Secure boot permanently enabled|"
          r"blowing secure boot efuse|Enabling Security download mode",
          ConsoleKind.SECURE_BOOT_FIRST_BOOT),
    # PSRAM
    _rule(r"^esp_psram$", r"^Found (\d+)MB PSRAM device", ConsoleKind.PSRAM_OK, "size_mb"),
    _rule(r"^esp_psram$", r"^SPI SRAM memory test OK", ConsoleKind.PSRAM_OK, test="ok"),
    _rule(None, r"PSRAM chip is not connected|Failed to init external RAM|"
          r"SPI SRAM memory test fail|External RAM failed memory test",
          ConsoleKind.PSRAM_FAILED),
    # Encrypted settings
    _rule(r"^nvs$", r"Failed to read NVS security cfg|Generating NVS encr-keys",
          ConsoleKind.NVS_KEY_DANGER),
    _rule(r"^zone_lite$", r"^Encrypted NVS initialization failed", ConsoleKind.NVS_FAILED),
    _rule(r"^zone_config$", r"^Could not open encrypted provisioning namespace",
          ConsoleKind.NVS_FAILED),
    _rule(r"^zone_config$", r"^No encrypted per-device provisioning namespace",
          ConsoleKind.SETTINGS_MISSING),
    _rule(r"^zone_lite$", r"^Encrypted per-device provisioning is required",
          ConsoleKind.SETTINGS_MISSING),
    _rule(r"^zone_config$", r"^Loaded per-device provisioning for zone=(.*)$",
          ConsoleKind.PROVISIONING_LOADED, "zone_id"),
    _rule(r"^zone_lite$", r"^Zone Lite starting zone=(.*?) device_id=(.*)$",
          ConsoleKind.APP_STARTED, "zone_id", "device_id"),
    # Storage
    _rule(r"^zone_lite$", r"^Storage ready; loaded (\d+) known event UIDs",
          ConsoleKind.STORAGE_READY, "known_events"),
    _rule(r"^zone_lite$", r"^Could not mount SPIFFS storage: (\S+)", ConsoleKind.STORAGE_FAILED,
          "error"),
    _rule(r"^SPIFFS$", r"mount failed, (-?\d+)", ConsoleKind.STORAGE_FAILED, "error"),
    _rule(r"^storage_repair$", r"^private mount failed (-?\d+)", ConsoleKind.STORAGE_FAILED,
          "error"),
    # Wi-Fi and the setup hotspot
    _rule(r"^zone_lite$", r"^Connecting to Wi-Fi SSID ?(.*)$", ConsoleKind.WIFI_CONNECTING,
          "ssid"),
    _rule(r"^zone_lite$", r"^Retrying Wi-Fi connection \((\d+)\)", ConsoleKind.WIFI_RETRY,
          "attempt"),
    _rule(r"^zone_lite$", r"^Wi-Fi connected with IP (\d+\.\d+\.\d+\.\d+)",
          ConsoleKind.WIFI_CONNECTED, "ip"),
    _rule(r"^wifi_setup$", r"^Wi-Fi setup access point enabled \((\w+) mode\)",
          ConsoleKind.SETUP_AP_OPEN, "mode"),
    _rule(r"^wifi_setup$", r"^Wi-Fi setup portal is disabled", ConsoleKind.OTHER,
          signal="portal_disabled"),
    _rule(r"^wifi_setup$", r"^Validated and atomically saved a new Wi-Fi network",
          ConsoleKind.OTHER, signal="wifi_saved"),
    # Head office
    _rule(r"^add_connector$", r"^ADD automatic onboarding completed", ConsoleKind.ADD_CONNECTED,
          stage="onboarded"),
    _rule(r"^add_connector$", r"^ADD live control channel connected", ConsoleKind.ADD_CONNECTED,
          stage="live"),
    _rule(r"^add_connector$", r"^ADD onboarding failed transport=(\S+) status=(-?\d+)",
          ConsoleKind.ADD_FAILED, "transport", "status"),
    _rule(r"^add_connector$", r"^Trusted time unavailable", ConsoleKind.ADD_FAILED,
          reason="time"),
    # ZKTeco terminal
    _rule(r"^zone_lite$", r"^Selected ZKT device (\d+\.\d+\.\d+\.\d+):(\d+)",
          ConsoleKind.TERMINAL_FOUND, "ip", "port"),
    _rule(r"^zone_lite$", r"^ZKT live attendance events registered", ConsoleKind.TERMINAL_FOUND,
          live="1"),
    _rule(r"^zone_lite$", r"^ZKT CMD_CONNECT response code=6001\b",
          ConsoleKind.TERMINAL_SECURE_PULL),
    _rule(r"^zone_lite$", r"^Secure-pull handshake failed at (\S+)",
          ConsoleKind.TERMINAL_SECURE_PULL, "handshake"),
    _rule(r"^zone_lite$", r"^ZKT auth failed with response code (\d+)",
          ConsoleKind.TERMINAL_AUTH_FAILED, "code"),
    _rule(r"^zone_lite$", r"^(\d+\.\d+\.\d+\.\d+):(\d+) answered TCP but failed ZKT auth",
          ConsoleKind.TERMINAL_AUTH_FAILED, "ip", "port"),
    _rule(r"^zone_lite$", r"^Rejecting authenticated ZKT (\S+) because serial",
          ConsoleKind.TERMINAL_AUTH_FAILED, "ip", reason="serial"),
    _rule(r"^zone_lite$", r"^No authenticated ZKT device found on port (\d+)",
          ConsoleKind.TERMINAL_UNREACHABLE, "port"),
    # Hikvision terminal (hikvision_runtime.c "Poll %s; reason=%s; ...")
    _rule(r"^hikvision$", r"^Poll OK; .*?terminal=([^;]+);", ConsoleKind.TERMINAL_FOUND, "ip"),
    # Crashes and power
    _rule(None, r"^Guru Meditation Error: Core\s+(\d+) panic'ed \(([^)]+)\)",
          ConsoleKind.PANIC, "core", "reason"),
    _rule(None, r"abort\(\) was called at PC", ConsoleKind.PANIC, reason="abort"),
    _rule(None, r"Task watchdog got triggered", ConsoleKind.PANIC, reason="task_watchdog"),
    _rule(None, r"Brownout detector was triggered", ConsoleKind.PANIC, reason="brownout"),
    _rule(None, r"^Rebooting\.\.\.", ConsoleKind.PANIC, reason="rebooting"),
)


class ConsoleParser:
    """Turns console lines into ConsoleEvents. Keeps the little state some need."""

    def __init__(self, mac: Optional[str] = None) -> None:
        self._project: Optional[str] = None
        self._error_check: Optional[str] = None
        self._hotspot = hotspot_name(mac)

    def parse(self, line: str, at: float = 0.0) -> Optional[ConsoleEvent]:
        text = _ANSI.sub("", line).strip()
        if not text:
            return None
        raw = redact(text)
        tag = ""
        message = text
        prefix = _IDF.match(text)
        if prefix:
            tag, message = prefix.group(3).strip(), prefix.group(4).strip()
        event = self._special(tag, message, raw, at)
        if event is not None:
            return event
        for rule in _RULES:
            if rule.tag is not None and not rule.tag.search(tag):
                continue
            found = rule.pattern.search(message)
            if not found:
                continue
            data = {key: (found.group(index + 1) or "").strip()
                    for index, key in enumerate(rule.keys)}
            data.update(dict(rule.extra))
            if rule.kind is ConsoleKind.BOOT_BANNER and "offset" in data:
                data["slot"] = _SLOTS.get(int(data["offset"], 16), "unknown")
            if rule.kind is ConsoleKind.SETUP_AP_OPEN and self._hotspot:
                data["ssid"] = self._hotspot
            return ConsoleEvent(rule.kind, raw, data, at)
        return ConsoleEvent(ConsoleKind.OTHER, raw, {}, at)

    def _special(self, tag: str, message: str, raw: str, at: float) -> Optional[ConsoleEvent]:
        if tag == "app_init":
            project = re.match(r"^Project name:\s+(\S+)", message)
            if project:
                self._project = project.group(1)
                return ConsoleEvent(ConsoleKind.OTHER, raw, {"project": self._project}, at)
            version = re.match(r"^App version:\s+(\S+)", message)
            if version:
                data = {"version": version.group(1)}
                if self._project:
                    data["project"] = self._project
                return ConsoleEvent(ConsoleKind.APP_VERSION, raw, data, at)
        check = re.match(r"^ESP_ERROR_CHECK failed: esp_err_t (0x[0-9a-fA-F]+)", message)
        if check:
            self._error_check = check.group(1).lower()
            return ConsoleEvent(ConsoleKind.PANIC, raw,
                                {"reason": "error_check", "error": self._error_check}, at)
        if re.match(r"^expression: zone_config_init\(\)", message):
            # zone_config_init returns ESP_ERR_INVALID_STATE (0x103) only when the
            # settings belong to the other firmware family.
            kind = (ConsoleKind.FAMILY_MISMATCH if self._error_check == "0x103"
                    else ConsoleKind.NVS_FAILED)
            return ConsoleEvent(kind, raw, {"error": self._error_check or ""}, at)
        hik = _HIK_REASON.match(message) if tag == "hikvision" else None
        if hik:
            reason, stage, http = hik.groups()
            data = {"reason": reason, "stage": stage, "http": http}
            if reason == "authentication_rejected":
                return ConsoleEvent(ConsoleKind.TERMINAL_AUTH_FAILED, raw, data, at)
            if reason in ("network_timeout_or_unreachable", "invalid_configuration"):
                return ConsoleEvent(ConsoleKind.TERMINAL_UNREACHABLE, raw, data, at)
            return ConsoleEvent(ConsoleKind.OTHER, raw, data, at)
        return None


# ------------------------------------------------------------------ serial

def default_line_state() -> Tuple[bool, bool]:
    """(dtr, rts) to hold while the port is open; see the module notes."""
    if sys.platform == "win32":
        return False, False
    return True, True


def open_serial(port: str, line_state: Optional[Tuple[bool, bool]] = None):
    import serial
    dtr, rts = line_state or default_line_state()
    handle = serial.Serial()
    handle.port = port
    handle.baudrate = BAUD
    handle.timeout = 0.05
    handle.write_timeout = 0
    # Set before open(): pyserial then applies them while opening instead of
    # opening with its defaults and toggling afterwards.
    handle.dtr = dtr
    handle.rts = rts
    handle.open()
    return handle


SerialOpener = Callable[[str], object]


class LineReader:
    """Bytes in, complete text lines out."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> List[str]:
        self._buffer += chunk
        parts = self._buffer.split(b"\n")
        self._buffer = parts.pop()
        if len(self._buffer) > 8192:  # a console with no newlines is not a log
            parts.append(self._buffer)
            self._buffer = b""
        return [part.decode("utf-8", "replace").rstrip("\r") for part in parts]

    def flush(self) -> List[str]:
        rest, self._buffer = self._buffer, b""
        return [rest.decode("utf-8", "replace")] if rest.strip() else []


def _is_busy(error: Exception) -> bool:
    text = str(error)
    return bool(re.search(r"Access is denied|Permission denied|Resource busy|PermissionError",
                          text, re.I))


# ------------------------------------------------------------------- watcher

class _Usb:
    """What the boot watcher needs from usb.UsbWatcher."""

    def find(self, mac: str) -> Tuple[Optional[UsbCandidate], int]:
        raise NotImplementedError


@dataclass
class _Watch:
    started: float
    events: List[ConsoleEvent]
    other_lines: int = 0
    resets: int = 0
    reset_times: Tuple[float, ...] = ()
    port_busy: bool = False


class BootWatcher:
    """BootService: replug guidance, then the first start's verdict from the console."""

    MAX_OTHER_EVENTS = 200
    MAX_KEPT_EVENTS = 2000

    def __init__(self, usb: _Usb, clock: Optional[Clock] = None,
                 opener: Optional[SerialOpener] = None,
                 placeholder_ssids: Sequence[str] = ("RE7",)) -> None:
        self._usb = usb
        self._clock = clock or Clock()
        self._open = opener or open_serial
        self._placeholders = tuple(placeholder_ssids)

    # ---------------------------------------------------------- first boot

    def wait_first_boot(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                        on_event: EventFn, cancel: CancelToken,
                        timeout_s: float = 120.0) -> BootReport:
        # The replug can take minutes; a PC that sleeps meanwhile never sees the start.
        with keep_awake():
            return self._wait_first_boot(mac, expect, on_phase, on_event, cancel, timeout_s)

    def _wait_first_boot(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                         on_event: EventFn, cancel: CancelToken,
                         timeout_s: float) -> BootReport:
        mac = normalize_mac(mac) or mac
        on_phase(ReplugPhase.WAITING_UNPLUG)
        if not self._wait_presence(mac, present=False, timeout=UNPLUG_TIMEOUT_S, cancel=cancel):
            return self._report_early(cancel)
        on_phase(ReplugPhase.WAITING_REPLUG)
        if not self._wait_presence(mac, present=True, timeout=REPLUG_TIMEOUT_S, cancel=cancel):
            return self._report_early(cancel)
        on_phase(ReplugPhase.WATCHING)
        watch = _Watch(started=self._clock.monotonic(), events=[])
        decided: List[BootReport] = []

        def check(_event: Optional[ConsoleEvent]) -> bool:
            verdict = judge(watch, expect, self._clock.monotonic(), timeout_s, final=False)
            if verdict is not None:
                decided.append(verdict)
            return verdict is not None

        self._read(mac, watch, on_event, cancel, deadline=watch.started + timeout_s,
                   stop=check)
        if cancel.cancelled and not decided:
            return BootReport(BootOutcome.CANCELLED, events=tuple(watch.events),
                              advice=texts.CANCELLED)
        report = decided[0] if decided else judge(watch, expect, self._clock.monotonic(),
                                                  timeout_s, final=True)
        assert report is not None
        log.info("first start of %s: %s (%d events, %d resets)", mac, report.outcome.value,
                 len(watch.events), watch.resets)
        return report

    def _report_early(self, cancel: CancelToken) -> BootReport:
        if cancel.cancelled:
            return BootReport(BootOutcome.CANCELLED, advice=texts.CANCELLED)
        return BootReport(BootOutcome.NOT_REPLUGGED, advice=texts.NOT_REPLUGGED)

    def _wait_presence(self, mac: str, *, present: bool, timeout: float,
                       cancel: CancelToken) -> bool:
        deadline = self._clock.monotonic() + timeout
        while not cancel.cancelled:
            candidate, _count = self._usb.find(mac)
            if (candidate is not None) == present:
                return True
            if self._clock.monotonic() >= deadline:
                return False
            if self._clock.sleep(0.1, cancel):
                return False
        return False

    # -------------------------------------------------------- live console

    def watch_console(self, candidate: UsbCandidate, on_event: EventFn,
                      cancel: CancelToken) -> None:
        mac = normalize_mac(candidate.mac) or ""
        watch = _Watch(started=self._clock.monotonic(), events=[])
        self._read(mac, watch, on_event, cancel, deadline=None, stop=None,
                   fixed_port=None if mac else candidate.port)

    def _read(self, mac: str, watch: _Watch, on_event: EventFn, cancel: CancelToken, *,
              deadline: Optional[float], stop: Optional[Callable[[Optional[ConsoleEvent]], bool]],
              fixed_port: Optional[str] = None) -> None:
        parser = ConsoleParser(mac)
        handle = None
        reader = LineReader()
        was_open = False
        try:
            while not cancel.cancelled:
                now = self._clock.monotonic()
                if deadline is not None and now >= deadline:
                    return
                if handle is None:
                    port = fixed_port
                    if port is None:
                        candidate, _count = self._usb.find(mac)
                        port = candidate.port if candidate else None
                    if port is None:
                        if stop and stop(None):
                            return
                        self._clock.sleep(0.05, cancel)
                        continue
                    try:
                        handle = self._open(port)
                    except Exception as error:  # the port is not ready or another app has it
                        if _is_busy(error) and not watch.port_busy:
                            watch.port_busy = True
                            busy = ConsoleEvent(ConsoleKind.OTHER, "port busy",
                                                {"signal": "port_busy"},
                                                self._clock.monotonic() - watch.started)
                            watch.events.append(busy)
                            on_event(busy)
                        self._clock.sleep(0.1, cancel)
                        continue
                    if was_open:
                        watch.resets += 1
                        watch.reset_times += (now - watch.started,)
                    was_open = True
                    reader = LineReader()
                try:
                    chunk = handle.read(4096)  # type: ignore[attr-defined]
                except Exception:  # the chip reset and USB went away
                    _close(handle)
                    handle = None
                    for line in reader.flush():
                        self._emit(parser, line, watch, on_event)
                    if stop and stop(None):
                        return
                    continue
                for line in reader.feed(chunk or b""):
                    event = self._emit(parser, line, watch, on_event)
                    if stop and event is not None and stop(event):
                        return
                if not chunk:
                    if stop and stop(None):
                        return
                    self._clock.sleep(0.02, cancel)
        finally:
            _close(handle)

    def _emit(self, parser: ConsoleParser, line: str, watch: _Watch,
              on_event: EventFn) -> Optional[ConsoleEvent]:
        event = parser.parse(line, self._clock.monotonic() - watch.started)
        if event is None:
            return None
        if event.kind is ConsoleKind.OTHER and not event.data:
            watch.other_lines += 1
            if watch.other_lines > self.MAX_OTHER_EVENTS:
                return None
        else:
            watch.events.append(event)
            if len(watch.events) > self.MAX_KEPT_EVENTS:
                del watch.events[:len(watch.events) - self.MAX_KEPT_EVENTS // 2]
        try:
            on_event(event)
        except Exception as error:  # a screen problem must not stop the watch
            log.warning("console listener failed: %s", error)
        return event


def _close(handle: object) -> None:
    if handle is not None:
        try:
            handle.close()  # type: ignore[attr-defined]
        except Exception:
            pass


# ------------------------------------------------------------------ verdict

def _first(events: Sequence[ConsoleEvent], kind: ConsoleKind) -> Optional[ConsoleEvent]:
    return next((event for event in events if event.kind is kind), None)


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _restart_period(reset_times: Sequence[float]) -> Optional[float]:
    if len(reset_times) < 2:
        return None
    gaps = [b - a for a, b in zip(reset_times, reset_times[1:])]
    return sum(gaps) / len(gaps)


# PANIC lines that are not a crash by themselves: "Rebooting..." is the panic
# handler's last line (the crash before it is counted already), the task watchdog only
# warns (the firmware leaves CONFIG_ESP_TASK_WDT_PANIC off) and a brownout is a power
# fault with its own advice.
_NOT_A_CRASH = ("rebooting", "task_watchdog", "brownout")


def _panic(event: ConsoleEvent) -> Optional[str]:
    """The reason of a PANIC line, None for every other line."""
    return event.data.get("reason", "") if event.kind is ConsoleKind.PANIC else None


# Lines the firmware prints only after its start line, so only once the main program has
# loaded this board's encrypted settings and found them complete. In app_main of ZKT 2.7.8,
# HIK 3.1.1 and 2.8.x the settings come first, then the start line, then storage, Wi-Fi
# (with the setup hotspot's controller), head office and the attendance machine. With
# missing or unreadable settings, or settings for the other machine type, app_main stops
# before the start line (or aborts) and prints none of these.
_AFTER_START = frozenset({
    ConsoleKind.STORAGE_READY, ConsoleKind.STORAGE_FAILED, ConsoleKind.WIFI_CONNECTING,
    ConsoleKind.WIFI_RETRY, ConsoleKind.WIFI_CONNECTED, ConsoleKind.SETUP_AP_OPEN,
    ConsoleKind.ADD_CONNECTED, ConsoleKind.ADD_FAILED, ConsoleKind.TERMINAL_FOUND,
    ConsoleKind.TERMINAL_AUTH_FAILED, ConsoleKind.TERMINAL_SECURE_PULL,
    ConsoleKind.TERMINAL_UNREACHABLE,
})


def _after_start(event: ConsoleEvent) -> bool:
    """True for a line that shows the main program running with its settings."""
    if event.kind is ConsoleKind.OTHER:
        # The setup hotspot's other lines, and Hikvision poll lines with no kind of their own.
        return (event.data.get("signal") in ("portal_disabled", "wifi_saved")
                or ("stage" in event.data and "http" in event.data))
    return event.kind in _AFTER_START


def crashes(events: Sequence[ConsoleEvent]) -> List[ConsoleEvent]:
    """Lines that show the board crashed: Guru Meditation, abort, a failed error check."""
    return [event for event in events
            if _panic(event) is not None and _panic(event) not in _NOT_A_CRASH]


def restarts_seen(events: Sequence[ConsoleEvent], port_reopens: int = 0) -> int:
    """How often the board restarted while it was watched.

    A port reopen is one restart; so is each "Rebooting..." line, and each ROM reset
    line other than a power-on (a power-on is someone plugging the board in).
    """
    rebooting = sum(1 for event in events if _panic(event) == "rebooting")
    resets = sum(1 for event in events if event.kind is ConsoleKind.BOOT_BANNER
                 and event.data.get("reset_reason") not in (None, "", "POWERON"))
    return max(port_reopens, rebooting, resets)


def crash_loop(events: Sequence[ConsoleEvent], port_reopens: int = 0) -> bool:
    """True when the board keeps crashing; the first start and diagnosis share this rule.

    Two crash lines are enough (a failed error check prints two: the check and its
    abort), and so are two restarts with a crash or a panic restart among them.
    """
    crashed = crashes(events)
    if len(crashed) >= 2:
        return True
    rebooted = any(_panic(event) == "rebooting" for event in events)
    return (bool(crashed) or rebooted) and restarts_seen(events, port_reopens) >= 2


def judge(watch: _Watch, expect: InstallConfig, now: float, timeout_s: float, *,
          final: bool) -> Optional[BootReport]:
    """The first start's outcome, or None while it is still too early to say."""
    events = watch.events
    started = _first(events, ConsoleKind.APP_STARTED)
    loaded = _first(events, ConsoleKind.PROVISIONING_LOADED)
    version = _first(events, ConsoleKind.APP_VERSION)
    # Only the start line names the device, and only it and the settings line the zone.
    zone_seen = (started.data.get("zone_id") if started else
                 loaded.data.get("zone_id") if loaded else None)
    device_seen = started.data.get("device_id") if started else None
    version_seen = version.data.get("version") if version else None

    def report(outcome: BootOutcome, advice: Optional[Advice]) -> BootReport:
        return BootReport(outcome, zone_id_seen=zone_seen, device_id_seen=device_seen,
                          app_version_seen=version_seen, events=tuple(events), advice=advice)

    kinds = {event.kind for event in events}
    if ConsoleKind.NVS_KEY_DANGER in kinds:
        return report(BootOutcome.SECURITY_KEY_PROBLEM, texts.SECURITY_KEY_LOST)
    if ConsoleKind.FAMILY_MISMATCH in kinds:
        return report(BootOutcome.FAMILY_MISMATCH, texts.FAMILY_MISMATCH_LOOP)
    if kinds & {ConsoleKind.NVS_FAILED, ConsoleKind.SETTINGS_MISSING}:
        return report(BootOutcome.SECURITY_KEY_PROBLEM, texts.SETTINGS_UNREADABLE)
    if ConsoleKind.PSRAM_FAILED in kinds:
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.PSRAM_FAILED)

    expected = f"zone {expect.zone_id}, device {expect.device_id}"
    if loaded and not _same(loaded.data.get("zone_id"), expect.zone_id):
        return report(BootOutcome.WRONG_SETTINGS, texts.wrong_settings(
            f"zone {loaded.data.get('zone_id')}", expected))

    # Restarts come from the console as well as from port reopens: a crash keeps the
    # port open (see the module notes).
    brownout = any(_panic(event) == "brownout" for event in events)
    if brownout and restarts_seen(events, watch.resets) >= 1:
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.POWER_PROBLEM)
    if crash_loop(events, watch.resets):
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.CRASH_LOOP)

    # The start line can be printed before the app has the port open after the replug,
    # and then it is lost (module notes). A later line of the program shows the start as
    # well; only the zone and device then stay unconfirmed. The rules below count from
    # the first line that shows the start, so a crash after it is never missed.
    running = next((event for event in events
                    if event.kind is ConsoleKind.APP_STARTED or _after_start(event)), None)
    if running:
        if started and not (_same(started.data.get("zone_id"), expect.zone_id)
                            and _same(started.data.get("device_id"), expect.device_id)):
            return report(BootOutcome.WRONG_SETTINGS, texts.wrong_settings(
                f"zone {started.data.get('zone_id')}, device {started.data.get('device_id')}",
                expected))
        if ConsoleKind.STORAGE_FAILED in kinds:
            return report(BootOutcome.STORAGE_PROBLEM, texts.STORAGE_PROBLEM)
        # A board that went down after it started is never a good start: watch on, and
        # say what happened when the time is up. A watchdog warning alone is no crash.
        # Without the start line its time is unknown, so then any crash seen counts.
        since = running.at if started else float("-inf")
        went_down = any(event.at >= since for event in events
                        if _panic(event) not in (None, "task_watchdog"))
        if not went_down:
            ok = texts.boot_ok(zone_seen, device_seen, version_seen)
            if ConsoleKind.STORAGE_READY in kinds:
                return report(BootOutcome.OK, ok)
            if final or now - watch.started - running.at >= STORAGE_WAIT_S:
                return report(BootOutcome.OK, ok)
            return None
    else:
        period = _restart_period(watch.reset_times)
        if watch.resets >= 2 and period is not None:
            if any(event.kind is ConsoleKind.PANIC for event in events):
                return report(BootOutcome.FAULTY_RESTART_LOOP, texts.CRASH_LOOP)
            meaningful = [event for event in events if event.kind is not ConsoleKind.BOOT_BANNER]
            if not meaningful and 6.0 <= period <= 16.0:
                return report(BootOutcome.FAULTY_RESTART_LOOP, texts.restart_loop_faulty(period))
            if not meaningful and period < 6.0:
                # The first-start screen's way on is Try again (watch again).
                return report(BootOutcome.FAULTY_RESTART_LOOP,
                              texts.restart_loop_fast(period, button="Try again"))
    if not final and now - watch.started < timeout_s:
        return None
    # Time is up. A board that crashed is not "silent", even if it crashed only once.
    if brownout:
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.POWER_PROBLEM)
    if crashes(events) or any(_panic(event) == "rebooting" for event in events):
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.CRASH_LOOP)
    if any(event.data.get("download_mode") == "1" for event in events):
        return report(BootOutcome.NO_OUTPUT, texts.DOWNLOAD_MODE_STUCK)
    if watch.port_busy and all(event.data.get("signal") == "port_busy" for event in events):
        return report(BootOutcome.NO_OUTPUT, texts.PORT_BUSY)
    board_lines = watch.other_lines + sum(1 for event in events
                                          if event.data.get("signal") != "port_busy")
    if not board_lines:
        return report(BootOutcome.NO_OUTPUT, texts.NO_OUTPUT)
    # Lines came, but none from the running program: it stopped early or never got there.
    return report(BootOutcome.NO_OUTPUT, texts.START_NOT_SEEN)


def boot_hints(events: Sequence[ConsoleEvent], placeholders: Sequence[str] = ("RE7",)
               ) -> Dict[str, str]:
    """Facts later screens use: Wi-Fi name tried, hotspot, terminal state."""
    hints: Dict[str, str] = {}
    for event in events:
        if event.kind is ConsoleKind.WIFI_CONNECTING:
            hints["wifi_ssid"] = event.data.get("ssid", "")
            hints["wifi_placeholder"] = "1" if hints["wifi_ssid"] in placeholders else "0"
        elif event.kind is ConsoleKind.SETUP_AP_OPEN and event.data.get("ssid"):
            hints["hotspot"] = event.data["ssid"]
        elif event.kind is ConsoleKind.WIFI_CONNECTED:
            hints["wifi_ip"] = event.data.get("ip", "")
    return hints
