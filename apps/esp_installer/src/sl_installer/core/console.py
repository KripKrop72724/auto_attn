"""Reads the board's console and decides how its first start, and the next one, went.

The app console is a copy on the native USB-Serial/JTAG port (UART0 on the other
socket is the main one). The USB copy drops text nobody reads and the port
disappears on every chip reset, so the port is reopened as fast as it comes back.
Even so, a start's first lines can be gone before the PC lists the port again: the
start line comes under 2 s after power-on. The verdict (judge) therefore also takes
any later line of the running program as the start.

A good first start is not enough: on 2 Nov 2026 two new boards started well once,
then had no settings at every later start. So every install watches one more start,
after a deliberate replug (wait_restart), with the same rules and one more: it passes
only when the board names its settings again for the expected zone (the settings
line, or the start line with zone and device). Missing or unreadable settings then
mean the board lost them when it restarted, and so do other settings than its first
start showed.

Each watched start's lines are kept, redacted like installer.log, in a small file per
install in the log folder (ConsoleLog), and the help file carries them to head office.

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

The second-stage bootloader can refuse to start the program at all, with an E line of
its own (BOOTLOADER_FAILED): on 3 Nov 2026 two boards with an XMC flash chip printed
"XMC flash startup fail" and "failed when running XMC startup flow, reboot!" every few
milliseconds for good. The judge puts such a start first, before any power or
restart-loop advice, unless the program ran after the line (the bootloader can fall
back to another copy). Those lines always reach the screen's "board's own words".
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Dict, List, Optional, Pattern, Sequence, Tuple

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
# Per-install console logs (ConsoleLog): a 2-minute start prints well under 50 KB.
CONSOLE_LOG_PREFIX = "console-"
CONSOLE_LOG_MAX_BYTES = 256 * 1024
CONSOLE_LOGS_KEPT = 20
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

# Second-stage bootloader tags (ESP-IDF 5.5.3 bootloader_support, bootloader_start.c).
# The first set logs only in the bootloader; the shared ones also in the running program
# (an OTA check of another copy), so their E lines count only after a ROM start line.
_BOOTLOADER_TAGS = re.compile(r"^(?:boot|boot\.esp32s3|bootloader_flash|flash_parts|qio_mode|"
                              r"flash_encrypt|bootloader_random)$")
_SHARED_BOOT_TAGS = re.compile(r"^(?:boot_comm|esp_image|secure_boot\w*)$")
_ROM_LINE = re.compile(r"^(?:ESP-ROM:|rst:0x)")
# (what the line says, reason, fatal): a fatal line always ends in bootloader_reset(); after
# the others the bootloader may still start another copy of the program.
_BOOT_FAILURES: Sequence[Tuple[Pattern, str, bool]] = (
    (re.compile(r"XMC flash startup fail|failed when running XMC startup flow|"
                r"XMC chip detected .* while support disabled"), "xmc_flash", True),
    (re.compile(r"load partition table error|Failed to verify partition table|"
                r"partition \d+ invalid magic number"), "partition_table", True),
    (re.compile(r"No bootable app partitions"), "no_app", True),
    (re.compile(r"failed to load bootloader image header"), "bootloader_image", True),
    (re.compile(r"is not bootable"), "no_app", False),
    (re.compile(r"chip revision check failed|mismatch chip ID"), "chip_revision", False),
    (re.compile(r"Image hash failed|invalid magic|invalid segment|Checksum failed"), "image",
     False),
    (re.compile(r"Secure Boot V2 verification failed|secure boot verification failed", re.I),
     "secure_boot", False),
)

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
        # Who prints now: "bootloader" after a ROM start line, "program" once a line of the
        # running program came, None before either (the port opened halfway).
        self._stage: Optional[str] = None

    def parse(self, line: str, at: float = 0.0) -> Optional[ConsoleEvent]:
        text = _ANSI.sub("", line).strip()
        if not text:
            return None
        raw = redact(text)
        tag = ""
        level = ""
        message = text
        prefix = _IDF.match(text)
        if prefix:
            level, tag, message = prefix.group(1), prefix.group(3).strip(), prefix.group(4).strip()
        event = self._bootloader(level, tag, message, raw, at)
        if event is not None:
            return event
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

    def _bootloader(self, level: str, tag: str, message: str, raw: str,
                    at: float) -> Optional[ConsoleEvent]:
        """A second-stage bootloader error (BOOTLOADER_FAILED), following who prints now."""
        if _ROM_LINE.match(message) or (tag == "boot" and "2nd stage bootloader" in message):
            self._stage = "bootloader"
            return None
        if not tag:
            return None
        if _BOOTLOADER_TAGS.match(tag):
            self._stage = "bootloader"
        elif not _SHARED_BOOT_TAGS.match(tag):
            self._stage = "program"
            return None
        if tag == "bootloader_flash" and "XM25QHxxC startup flow" in message:
            # The bootloader runs the XMC startup flow; whether it worked comes next.
            return ConsoleEvent(ConsoleKind.OTHER, raw, {"signal": "xmc_flow"}, at)
        if level != "E" or self._stage != "bootloader":
            return None
        reason, fatal = "other", False
        for pattern, name, always in _BOOT_FAILURES:
            if pattern.search(message):
                reason, fatal = name, always
                break
        return ConsoleEvent(ConsoleKind.BOOTLOADER_FAILED, raw,
                            {"reason": reason, "fatal": "1" if fatal else "0"}, at)

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


# -------------------------------------------------------------- console log

_LINE_MAX = 1000           # characters kept of one console line
_ERROR_LINE = re.compile(r"^E \(\d+\) ")
_FRAME_SLACK = 16 * 1024   # room past the cap for the heading and result of later starts
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    return _SAFE_NAME.sub("-", value).strip("-.")[:64] or "board"


def console_log_files(folder: Path) -> List[Path]:
    """The per-install console logs in ``folder``, newest first."""
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob(f"{CONSOLE_LOG_PREFIX}*.log"):
        try:
            found.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    return [path for _when, _name, path in sorted(found, reverse=True)]


def prune_console_logs(folder: Path, keep: int) -> None:
    """Delete all but the ``keep`` newest console logs in ``folder``."""
    for path in console_log_files(folder)[max(keep, 0):]:
        try:
            path.unlink()
        except OSError as error:
            log.warning("could not remove %s: %s", path.name, error)


class ConsoleLog:
    """What the board printed in one install's watched starts, for head office.

    One file per install in the log folder; each watched start (the first start, the
    restart check, every Try again) is one section of it. Lines are redacted as in
    installer.log (logging_setup.redact: registered secrets, secret-looking names and
    long blobs). The file stops growing at ``max_bytes``. A problem with the file only
    ends the log, never the watch.
    """

    def __init__(self, path: Path, clock: Clock, max_bytes: int = CONSOLE_LOG_MAX_BYTES) -> None:
        self.path = path
        self._clock = clock
        self._max = max_bytes
        self._handle: Optional[IO[str]] = None
        self._size = 0
        self._began = 0.0
        self._full = False

    def begin(self, title: str) -> None:
        """A new watched start: its heading, with this computer's date and time."""
        self.close()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")
            self._size = self.path.stat().st_size
        except OSError as error:
            log.warning("console log %s not kept: %s", self.path.name, error)
            self.close()
            return
        self._began = self._clock.monotonic()
        self._full = False
        moment = self._clock.now_utc().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        self._write(f"=== {title}, {moment} ===", frame=True)

    def note(self, text: str) -> None:
        """Something the app saw itself, such as the unplug or the port opening."""
        self._entry(f"# {text}")

    def line(self, text: str) -> None:
        """One line as the board printed it (without colours), redacted."""
        cleaned = _ANSI.sub("", text).strip()
        if cleaned:
            self._entry(redact(cleaned)[:_LINE_MAX])

    def end(self, report: BootReport) -> None:
        advice = report.advice.code if report.advice else "-"
        self._write(f"=== Result: {report.outcome.value} ({advice}) ===", frame=True)
        self._write("", frame=True)
        self.close()

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _entry(self, text: str) -> None:
        if self._handle is None or self._full:
            return
        entry = f"{self._clock.monotonic() - self._began:7.1f}  {text}"
        if self._size + len(entry.encode("utf-8")) + len(os.linesep) > self._max:
            self._full = True
            self._write("# The rest of this start is not kept: this file is full.", frame=True)
            return
        self._write(entry)

    def _write(self, text: str, frame: bool = False) -> None:
        if self._handle is None or (frame and self._size > self._max + _FRAME_SLACK):
            return
        try:
            self._handle.write(text + "\n")
            self._handle.flush()
        except OSError as error:
            log.warning("console log %s stopped: %s", self.path.name, error)
            self.close()
            return
        self._size += len(text.encode("utf-8")) + len(os.linesep)


def _note(record: Optional[ConsoleLog], text: str) -> None:
    if record is not None:
        record.note(text)


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
    last_error: Optional[str] = None  # the newest E line the board printed, redacted


class BootWatcher:
    """BootService: replug guidance, then the verdict of each watched start from the console.

    With ``console_logs`` (the app's log folder) the lines of every watched start of an
    install that names itself (``transcript``) are kept there (ConsoleLog).
    """

    MAX_OTHER_EVENTS = 200
    MAX_KEPT_EVENTS = 2000

    def __init__(self, usb: _Usb, clock: Optional[Clock] = None,
                 opener: Optional[SerialOpener] = None,
                 placeholder_ssids: Sequence[str] = ("RE7",),
                 console_logs: Optional[Path] = None) -> None:
        self._usb = usb
        self._clock = clock or Clock()
        self._open = opener or open_serial
        self._placeholders = tuple(placeholder_ssids)
        self._console_logs = console_logs

    # ------------------------------------------------------ watched starts

    def wait_first_boot(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                        on_event: EventFn, cancel: CancelToken,
                        timeout_s: float = 120.0,
                        transcript: Optional[str] = None) -> BootReport:
        # The replug can take minutes; a PC that sleeps meanwhile never sees the start.
        with keep_awake():
            return self._wait_start(mac, expect, on_phase, on_event, cancel, timeout_s,
                                    restart=False, transcript=transcript)

    def wait_restart(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                     on_event: EventFn, cancel: CancelToken,
                     timeout_s: float = 120.0,
                     transcript: Optional[str] = None,
                     first: Optional[BootReport] = None) -> BootReport:
        """The restart check: after a good first start, one more replug and its start.

        Watched like the first start, with the same phases, timeouts and board-ID rules,
        and judged with ``restart``: the board must name its settings again (judge).
        ``first`` is the first start's report: settings other than it showed were lost.
        """
        with keep_awake():
            return self._wait_start(mac, expect, on_phase, on_event, cancel, timeout_s,
                                    restart=True, transcript=transcript, first=first)

    def _wait_start(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                    on_event: EventFn, cancel: CancelToken, timeout_s: float, *,
                    restart: bool, transcript: Optional[str],
                    first: Optional[BootReport] = None) -> BootReport:
        mac = normalize_mac(mac) or mac
        record = self._console_log(mac, transcript)
        if record is not None:
            record.begin("Second start, after a restart" if restart else "First start")
        try:
            report = self._watch_start(mac, expect, on_phase, on_event, cancel, timeout_s,
                                       restart=restart, record=record, first=first)
        except BaseException:
            if record is not None:
                record.close()
            raise
        if record is not None:
            record.end(report)
        return report

    def _watch_start(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                     on_event: EventFn, cancel: CancelToken, timeout_s: float, *,
                     restart: bool, record: Optional[ConsoleLog],
                     first: Optional[BootReport] = None) -> BootReport:
        on_phase(ReplugPhase.WAITING_UNPLUG)
        if not self._wait_presence(mac, present=False, timeout=UNPLUG_TIMEOUT_S, cancel=cancel):
            return self._report_early(cancel)
        _note(record, "the board was unplugged")
        on_phase(ReplugPhase.WAITING_REPLUG)
        if not self._wait_presence(mac, present=True, timeout=REPLUG_TIMEOUT_S, cancel=cancel):
            return self._report_early(cancel)
        _note(record, "the board was plugged in again")
        on_phase(ReplugPhase.WATCHING)
        watch = _Watch(started=self._clock.monotonic(), events=[])
        decided: List[BootReport] = []

        def check(_event: Optional[ConsoleEvent]) -> bool:
            verdict = judge(watch, expect, self._clock.monotonic(), timeout_s, final=False,
                            restart=restart, first=first, board_id=mac)
            if verdict is not None:
                decided.append(verdict)
            return verdict is not None

        self._read(mac, watch, on_event, cancel, deadline=watch.started + timeout_s,
                   stop=check, record=record)
        if cancel.cancelled and not decided:
            return BootReport(BootOutcome.CANCELLED, events=tuple(watch.events),
                              advice=texts.CANCELLED)
        report = decided[0] if decided else judge(watch, expect, self._clock.monotonic(),
                                                  timeout_s, final=True, restart=restart,
                                                  first=first, board_id=mac)
        assert report is not None
        log.info("%s of %s: %s (%d events, %d resets)",
                 "second start" if restart else "first start", mac, report.outcome.value,
                 len(watch.events), watch.resets)
        return report

    def _console_log(self, mac: str, transcript: Optional[str]) -> Optional[ConsoleLog]:
        """The install's console log, or None when nothing is to be kept."""
        if self._console_logs is None or not transcript:
            return None
        board = _safe_name((normalize_mac(mac) or mac).replace(":", ""))
        path = self._console_logs / f"{CONSOLE_LOG_PREFIX}{board}-{_safe_name(transcript)}.log"
        if not path.exists():
            # A new install's file: only the newest others stay, so the folder stays small.
            prune_console_logs(self._console_logs, CONSOLE_LOGS_KEPT - 1)
        return ConsoleLog(path, self._clock)

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
              fixed_port: Optional[str] = None, record: Optional[ConsoleLog] = None) -> None:
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
                            _note(record, "another program is using the port")
                            busy = ConsoleEvent(ConsoleKind.OTHER, "port busy",
                                                {"signal": "port_busy"},
                                                self._clock.monotonic() - watch.started)
                            watch.events.append(busy)
                            on_event(busy)
                        self._clock.sleep(0.1, cancel)
                        continue
                    _note(record, f"the port {port} is open")
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
                    _note(record, "the port closed")
                    for line in reader.flush():
                        self._emit(parser, line, watch, on_event, record)
                    if stop and stop(None):
                        return
                    continue
                for line in reader.feed(chunk or b""):
                    event = self._emit(parser, line, watch, on_event, record)
                    if stop and event is not None and stop(event):
                        return
                if not chunk:
                    if stop and stop(None):
                        return
                    self._clock.sleep(0.02, cancel)
        finally:
            _close(handle)

    def _emit(self, parser: ConsoleParser, line: str, watch: _Watch,
              on_event: EventFn, record: Optional[ConsoleLog] = None) -> Optional[ConsoleEvent]:
        if record is not None:
            record.line(line)  # every line, also those past the screen's limits below
        event = parser.parse(line, self._clock.monotonic() - watch.started)
        if event is None:
            return None
        if _ERROR_LINE.match(event.raw) or event.kind is ConsoleKind.BOOTLOADER_FAILED:
            watch.last_error = event.raw
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


def _shows_progress(event: ConsoleEvent) -> bool:
    """A line that shows the start went on past the bootloader's checks."""
    if event.kind is ConsoleKind.BOOT_BANNER:
        # "Loaded app from partition" (the bootloader found a copy) or "Calling app_main()".
        return "offset" in event.data or event.data.get("app_main") == "1"
    return event.kind not in (ConsoleKind.BOOTLOADER_FAILED, ConsoleKind.PANIC,
                              ConsoleKind.OTHER) or _after_start(event)


def _is_rom_start(event: ConsoleEvent) -> bool:
    return event.kind is ConsoleKind.BOOT_BANNER and bool(
        event.data.get("rom") or event.data.get("reset_reason"))


def startup_refusal(events: Sequence[ConsoleEvent], final: bool = True
                    ) -> Optional[ConsoleEvent]:
    """The bootloader line that stopped the board's start, or None (module notes).

    Each start begins at a ROM start line. A start counts as refused when it has a
    bootloader error and nothing after that error shows the start going on, and either
    the error always ends the start (fatal), the board started again after it, or the
    watch is over (``final``). A later start that went on clears it. The XMC line is
    preferred; otherwise the first error of that start, the cause the others follow.
    """
    starts: List[List[ConsoleEvent]] = [[]]
    for event in events:
        if _is_rom_start(event) and starts[-1]:
            starts.append([])
        starts[-1].append(event)
    found: Optional[ConsoleEvent] = None
    for index, start in enumerate(starts):
        refusals = [position for position, event in enumerate(start)
                    if event.kind is ConsoleKind.BOOTLOADER_FAILED]
        if not refusals:
            if any(_shows_progress(event) for event in start):
                found = None
            continue
        if any(_shows_progress(event) for event in start[refusals[-1] + 1:]):
            found = None
            continue
        errors = [start[position] for position in refusals]
        fatal = any(event.data.get("fatal") == "1" for event in errors)
        if fatal or final or index < len(starts) - 1:
            found = next((event for event in errors if event.data.get("reason") == "xmc_flash"),
                         errors[0])
    return found


REFUSAL_QUOTE_MAX = 160  # characters of the board's line quoted in the advice


def refusal_advice(event: ConsoleEvent) -> Advice:
    """What to say about a start the bootloader refused (startup_refusal)."""
    if event.data.get("reason") == "xmc_flash":
        return texts.flash_chip_unsupported(at_start=True)
    return texts.startup_refused(event.raw[:REFUSAL_QUOTE_MAX])


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
          final: bool, restart: bool = False, first: Optional[BootReport] = None,
          board_id: Optional[str] = None) -> Optional[BootReport]:
    """A watched start's outcome, or None while it is still too early to say.

    ``restart`` judges the restart check, the start after the good first one, with one
    more rule: it passes only when the board names its settings again, in the settings
    line for the expected zone or the start line with zone and device. Missing or
    unreadable settings then mean it lost them when it restarted (SETTINGS_LOST), and so
    do a zone or device other than the first start (``first``) showed. A board that runs
    without either line (both came before the port opened) cannot show that it kept them
    (SETTINGS_NOT_SEEN). Everything else is judged as at the first start. ``board_id``
    goes into the lost-settings advice, for the board's label.
    """
    events = watch.events
    started = _first(events, ConsoleKind.APP_STARTED)
    loaded = _first(events, ConsoleKind.PROVISIONING_LOADED)
    version = _first(events, ConsoleKind.APP_VERSION)
    # Only the start line names the device, and only it and the settings line the zone.
    zone_seen = (started.data.get("zone_id") if started else
                 loaded.data.get("zone_id") if loaded else None)
    device_seen = started.data.get("device_id") if started else None
    version_seen = version.data.get("version") if version else None

    def report(outcome: BootOutcome, advice: Optional[Advice],
               said: Optional[ConsoleEvent] = None) -> BootReport:
        # board_said: the line that decided a failed start, else the newest E line.
        line = said.raw if said is not None else (
            None if outcome is BootOutcome.OK else watch.last_error)
        return BootReport(outcome, zone_id_seen=zone_seen, device_id_seen=device_seen,
                          app_version_seen=version_seen, events=tuple(events), advice=advice,
                          board_said=line)

    def changed_since_first(zone: Optional[str], device: Optional[str]) -> bool:
        """At the restart check: other settings than the first start showed for this setup.

        Writing them again would last only until the next restart, as with lost ones. A
        value the first start did not see (its lines came before the port opened) is
        judged as at the first start.
        """
        if not restart or first is None or first.outcome is not BootOutcome.OK:
            return False
        return ((first.zone_id_seen is not None and not _same(zone, first.zone_id_seen))
                or (device is not None and first.device_id_seen is not None
                    and not _same(device, first.device_id_seen)))

    kinds = {event.kind for event in events}
    # The bootloader refused to start the program: first, before any power or restart-loop
    # advice, which would send the person after cables (3 Nov 2026, the XMC boards).
    refused = startup_refusal(events, final=final or now - watch.started >= timeout_s)
    if refused is not None:
        return report(BootOutcome.STARTUP_REFUSED, refusal_advice(refused), refused)
    if ConsoleKind.NVS_KEY_DANGER in kinds:
        return report(BootOutcome.SECURITY_KEY_PROBLEM, texts.SECURITY_KEY_LOST,
                      _first(events, ConsoleKind.NVS_KEY_DANGER))
    if ConsoleKind.FAMILY_MISMATCH in kinds:
        return report(BootOutcome.FAMILY_MISMATCH, texts.FAMILY_MISMATCH_LOOP,
                      _first(events, ConsoleKind.FAMILY_MISMATCH))
    if kinds & {ConsoleKind.NVS_FAILED, ConsoleKind.SETTINGS_MISSING}:
        line = next(event for event in events
                    if event.kind in (ConsoleKind.NVS_FAILED, ConsoleKind.SETTINGS_MISSING))
        if restart:
            # It read them at the first start (the 2 Nov 2026 boards): writing them again
            # would last only until the next restart.
            return report(BootOutcome.SETTINGS_LOST, texts.settings_lost_on_restart(board_id),
                          line)
        return report(BootOutcome.SECURITY_KEY_PROBLEM, texts.SETTINGS_UNREADABLE, line)
    if ConsoleKind.PSRAM_FAILED in kinds:
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.PSRAM_FAILED,
                      _first(events, ConsoleKind.PSRAM_FAILED))

    expected = f"zone {expect.zone_id}, device {expect.device_id}"
    if loaded and not _same(loaded.data.get("zone_id"), expect.zone_id):
        if changed_since_first(loaded.data.get("zone_id"), None):
            return report(BootOutcome.SETTINGS_LOST,
                          texts.settings_lost_on_restart(board_id, changed=True), loaded)
        return report(BootOutcome.WRONG_SETTINGS, texts.wrong_settings(
            f"zone {loaded.data.get('zone_id')}", expected), loaded)

    # Restarts come from the console as well as from port reopens: a crash keeps the
    # port open (see the module notes).
    brownout = next((event for event in events if _panic(event) == "brownout"), None)
    if brownout and restarts_seen(events, watch.resets) >= 1:
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.POWER_PROBLEM, brownout)
    if crash_loop(events, watch.resets):
        crashed = crashes(events)
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.CRASH_LOOP,
                      crashed[0] if crashed else None)

    # The start line can be printed before the app has the port open after the replug,
    # and then it is lost (module notes). A later line of the program shows the start as
    # well; only the zone and device then stay unconfirmed. The rules below count from
    # the first line that shows the start, so a crash after it is never missed.
    running = next((event for event in events
                    if event.kind is ConsoleKind.APP_STARTED or _after_start(event)), None)
    if running:
        if started and not (_same(started.data.get("zone_id"), expect.zone_id)
                            and _same(started.data.get("device_id"), expect.device_id)):
            if changed_since_first(started.data.get("zone_id"), started.data.get("device_id")):
                return report(BootOutcome.SETTINGS_LOST,
                              texts.settings_lost_on_restart(board_id, changed=True), started)
            return report(BootOutcome.WRONG_SETTINGS, texts.wrong_settings(
                f"zone {started.data.get('zone_id')}, device {started.data.get('device_id')}",
                expected), started)
        if ConsoleKind.STORAGE_FAILED in kinds:
            return report(BootOutcome.STORAGE_PROBLEM, texts.STORAGE_PROBLEM,
                          _first(events, ConsoleKind.STORAGE_FAILED))
        # A board that went down after it started is never a good start: watch on, and
        # say what happened when the time is up. A watchdog warning alone is no crash.
        # Without the start line its time is unknown, so then any crash seen counts.
        since = running.at if started else float("-inf")
        went_down = any(event.at >= since for event in events
                        if _panic(event) not in (None, "task_watchdog"))
        if not went_down:
            if not restart:
                outcome, advice = BootOutcome.OK, texts.boot_ok(zone_seen, device_seen,
                                                               version_seen)
            elif started or loaded:
                # Either line matched what was set (WRONG_SETTINGS above otherwise), and the
                # program runs: the settings survived the restart. Not before a line of the
                # running program: "provisioning is required" can follow the settings line.
                outcome, advice = BootOutcome.OK, texts.settings_kept(zone_seen, device_seen,
                                                                     version_seen)
            else:
                # Both lines are printed once, before this one: they came before the port
                # opened and cannot come again in this start.
                outcome, advice = BootOutcome.SETTINGS_NOT_SEEN, texts.SETTINGS_NOT_SEEN
            if ConsoleKind.STORAGE_READY in kinds:
                return report(outcome, advice)
            if final or now - watch.started - running.at >= STORAGE_WAIT_S:
                return report(outcome, advice)
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
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.POWER_PROBLEM, brownout)
    if crashes(events) or any(_panic(event) == "rebooting" for event in events):
        crashed = crashes(events)
        return report(BootOutcome.FAULTY_RESTART_LOOP, texts.CRASH_LOOP,
                      crashed[0] if crashed else None)
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
