"""The board hardware test: a small State Life test program tests the board's parts.

Why: the hardware gate (board.py) trusts what the chip's eFuses claim ("Embedded PSRAM
8MB (AP_3v3)"), so PSRAM-faulty boards passed it, got their key locked and then died at
every start (ac:27:6e:a4:e5:e8 and a4:57:88, 4 Nov 2026; ESP-IDF's own boot memtest even
said "OK" on a4:57:88 before it crashed). Owner decisions of 16 Nov 2026: before every key
lock (NEW_BOARD and RESUME) and as the home option "Test a new board", the app

1. reads the eFuse summary (before),
2. writes the unsecured test program (bootloader 0x0, partition table 0x8000, app 0x10000;
   pinned in boardtest_images.py) with esptool's flasher stub,
3. starts it: clears the volatile RTC force-download flag (write-mem) and resets the chip
   with esptool's RTC watchdog reset, the way tests/firmware/hardware/wifi_portal/
   run_spare.py does; if nothing comes it asks for one physical replug; if the program's
   lines come without their BEGIN (the port came back late) it starts it once more
   (MAX_STARTS); a watch lasts WATCH_S, up to WATCH_MAX_S while the program shows progress,
4. reads the program's SLTEST lines (console.py) and judges them (TestJudge, below),
5. always cleans up: the program forces download mode and restarts after its END and CLEAN
   lines (else esptool's default reset gets there), 0x0-0x110000 is erased (the whole chip
   when the program did not report its own test sectors erased), and the eFuse summary is
   read again: every field must be as before (efuses_unchanged).

eFuses must never change. The plan here is its own, checked by its own safety checker
(safety_problems): no espefuse command but ``summary``, no burn, protect or flash-voltage
command ever, exact offsets, and the booting reset only in the start call. The install
plan's rules (flashplan.FORBIDDEN_ARGS) are not loosened for it. The board must be outside
Secure Download Mode, with Secure Boot and flash encryption off, blank or with only our
key locked (an earlier setup stopped after the lock), the hardware gate OK, and its
board ID is checked before every call.

A board the gate refused is not written at all: its report is FAILED at once, with the
gate's evidence, so it still gets a certificate for the seller.

``check_failed_board`` is for a locked board that never reached head office: it is never
written or reset; after one replug its own start is watched and classified with the
console helpers (hardware fault, fixable by Fix the settings or Finish the setup, or a
board that works). A board the test program can run on is tested instead (run_test).

The line protocol (board_test_firmware, SPEC A): ``SLTEST BEGIN|ALIVE|ERR|RESULT|END|
CLEAN key=value ...``; END carries the CRC-32 of the run's lines (console.ConsoleParser
checks it as ``crc_ok``).
"""
from __future__ import annotations

import enum
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import texts
from .api import (Advice, BoardInfo, BoardTestProgress, BoardTestReport, BoardTestVerdict,
                  CancelToken, ConsoleEvent, ConsoleKind, HardwareVerdict, InstallerError,
                  InstallMode, PhaseFn, ReplugPhase, SecurityState, TestFinding,
                  UsbCandidate)
from .board import (classify_efuses, efuse_mac, hardware_advice, parse_banner,
                    parse_security_info, read_summary_file)
from .boardtest_images import (IMAGES, WRITTEN_AREA, ImageError, TestImage, pinned_sha256,
                               verified_images)
from .clock import Clock
from .console import (REPLUG_TIMEOUT_S, UNPLUG_TIMEOUT_S, BootWatcher, ConsoleLog, ConsoleWatch,
                      SerialOpener, boot_hints, crash_advice, crash_loop, crashes,
                      restarts_seen, startup_refusal)
from .esptool_runner import (ESPEFUSE, ESPTOOL, PROGRESS_LINE, ToolCall, ToolResult, ToolRunner,
                             advice_for, failure_cause)
from .flashplan import CHIP, KEEP_FLASH_ARGS
from .keepawake import keep_awake
from .logging_setup import get_logger, redact
from .naming import normalize_mac

log = get_logger("boardtest")

KIND_VENDOR = "vendor_test"
KIND_LOCKED = "locked_board"
RECOVERY_RESUME = "RESUME"
RECOVERY_FIX_SETTINGS = "FIX_SETTINGS"

STAGES = ("psram", "iram", "flash", "cpu", "wifi")
NO_START_S = 12.0          # no line and no USB drop this long after the start: not started
SILENCE_S = 8.0            # no line this long after BEGIN, port open: the board hangs
WATCH_S = 150.0            # one start's watch, longer only while the program makes progress
WATCH_MAX_S = 300.0        # one start's watch, at most (slow but working flash chips)
END_TO_CLEAN_S = 30.0      # the program erases its test sectors after END
SETTLE_S = 2.0             # after a crash line: the rest of the dump
LINGER_S = 3.0             # after a decided fault: trailing lines for the proof
LOOP_DECIDE_S = 45.0       # USB drops but no line: decide by then
PORT_WAIT_S = 20.0         # the board back on USB (it re-enumerates after every reset)
AFTER_END_S = 3.0          # the program restarts into download mode right after CLEAN
TEST_VALID_S = 30 * 60     # a passed test lets the key be locked this long
MAX_STARTS = 2             # start calls per test: one, and one more when BEGIN was missed
PROOF_MAX = 20
RTC_CNTL_OPTION1_REG = 0x6000812C       # ESP32-S3; esptool targets/esp32s3.py
RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK = 0x1

_ALLOWED_STATES = (SecurityState.BLANK, SecurityState.KEY_BURNED_NOT_STARTED)
# RESULT codes that say the test program itself could not test, not that the board failed.
NOT_BOARD_FAULTS = frozenset({"FLASH_TEST_AREA_MISSING"})
_TECH = {"psram": "PSRAM (octal SPI RAM)", "iram": "internal SRAM", "flash": "SPI flash",
         "cpu": "CPU cores", "wifi": "Wi-Fi radio"}
_OK_TITLES = {"psram": "The extra memory works", "iram": "The chip's own memory works",
              "flash": "The memory chip works", "cpu": "Both processor cores work",
              "wifi": "The Wi-Fi works"}
_STAGE_WORDS = {"psram": "Testing the extra memory", "iram": "Testing the chip's own memory",
                "flash": "Testing the memory chip", "cpu": "Testing the processor",
                "wifi": "Testing the Wi-Fi"}
# Overall progress: where each stage starts and ends.
_SPAN = {"check": (0.0, 0.05), "write": (0.05, 0.2), "start": (0.2, 0.25),
         "psram": (0.25, 0.5), "iram": (0.5, 0.55), "flash": (0.55, 0.7),
         "cpu": (0.7, 0.74), "wifi": (0.74, 0.85), "cleanup": (0.85, 1.0)}


# ---------------------------------------------------------------- the plan

class Purpose(str, enum.Enum):
    IDENTITY = "identity"            # get-security-info: board ID and lock state
    EFUSE_BEFORE = "efuse_before"    # espefuse summary before anything is written
    WRITE_IMAGES = "write_images"    # the three test images
    START = "start"                  # clear the force-download flag, RTC watchdog reset
    ERASE_TEST_AREA = "erase_area"   # 0x0-0x110000 after the test
    ERASE_CHIP = "erase_chip"        # the whole chip, when the program did not clean up
    EFUSE_AFTER = "efuse_after"      # espefuse summary after the erase


@dataclass(frozen=True)
class TestCall:
    __test__ = False  # not a pytest test class

    purpose: Purpose
    call: ToolCall
    summary_path: Optional[str] = None
    total_bytes: int = 0


def boardtest_args(port: str, *command: str, after: str = "no-reset",
              stub: bool = True) -> Tuple[str, ...]:
    args = ["--chip", CHIP, "--port", port, "--before", "default-reset", "--after", after]
    if not stub:
        args.append("--no-stub")
    return tuple(args) + tuple(command)


def identity_call(port: str) -> TestCall:
    return TestCall(Purpose.IDENTITY, ToolCall(
        ESPTOOL, boardtest_args(port, "get-security-info", stub=False), "check board identity",
        timeout_s=60.0, idle_timeout_s=45.0))


def efuse_call(purpose: Purpose, port: str, path: Path) -> TestCall:
    assert purpose in (Purpose.EFUSE_BEFORE, Purpose.EFUSE_AFTER)
    return TestCall(purpose, ToolCall(
        ESPEFUSE, ("--chip", CHIP, "--port", port, "--before", "default-reset", "summary",
                   "--format", "json", "--file", str(path)),
        "read eFuse summary", timeout_s=90.0, idle_timeout_s=60.0), summary_path=str(path))


def write_call(port: str, images: Sequence[Tuple[TestImage, Path]]) -> TestCall:
    pairs: List[str] = []
    total = 0
    for image, path in images:
        pairs += [hex(image.offset), str(path)]
        total += image.size or path.stat().st_size
    return TestCall(Purpose.WRITE_IMAGES, ToolCall(
        ESPTOOL, boardtest_args(port, "write-flash", *KEEP_FLASH_ARGS, *pairs),
        "write test program",
        timeout_s=180.0 + total / 25_000, idle_timeout_s=90.0 + 40.0 * total / 0x100000),
        total_bytes=total)


def start_call(port: str) -> TestCall:
    """Clear RTC_CNTL_FORCE_DOWNLOAD_BOOT (volatile, not an eFuse), then the RTC watchdog
    reset that starts the program (esptool's write_reg + watchdog_reset, run_spare.py)."""
    return TestCall(Purpose.START, ToolCall(
        ESPTOOL, boardtest_args(port, "write-mem", hex(RTC_CNTL_OPTION1_REG), "0x0",
                           hex(RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK), after="watchdog-reset",
                           stub=False),
        "start test program", timeout_s=60.0, idle_timeout_s=45.0))


def erase_area_call(port: str) -> TestCall:
    start, end = WRITTEN_AREA
    return TestCall(Purpose.ERASE_TEST_AREA, ToolCall(
        ESPTOOL, boardtest_args(port, "erase-region", hex(start), hex(end - start)),
        "erase test program", timeout_s=120.0, idle_timeout_s=90.0))


def erase_chip_call(port: str) -> TestCall:
    return TestCall(Purpose.ERASE_CHIP, ToolCall(
        ESPTOOL, boardtest_args(port, "erase-flash"), "erase whole memory chip",
        timeout_s=400.0, idle_timeout_s=300.0))


def build_plan(port: str, images: Sequence[Tuple[TestImage, Path]], work: Path
               ) -> Tuple[TestCall, ...]:
    """Every call a board test may make, in order (ERASE_CHIP only when needed)."""
    return (identity_call(port), efuse_call(Purpose.EFUSE_BEFORE, port, work / "before.json"),
            write_call(port, images), start_call(port), identity_call(port),
            erase_area_call(port), erase_chip_call(port),
            efuse_call(Purpose.EFUSE_AFTER, port, work / "after.json"))


# Never in any board-test call: espefuse writes of any kind, the install plan's dangerous
# esptool commands, and flash-voltage changes.
FORBIDDEN_TOKENS = ("burn-efuse", "burn-key", "burn-key-digest", "burn-block-data", "burn-bit",
                    "burn-custom-mac", "write-protect-efuse", "read-protect-efuse",
                    "set-flash-voltage", "execute-scripts", "--do-not-confirm", "--force",
                    "load-ram", "run", "hard-reset", "soft-reset", "no-reset-stub",
                    "--erase-all", "read-flash", "merge-bin")
_COMMANDS = {
    Purpose.IDENTITY: (ESPTOOL, "get-security-info"),
    Purpose.EFUSE_BEFORE: (ESPEFUSE, "summary"),
    Purpose.EFUSE_AFTER: (ESPEFUSE, "summary"),
    Purpose.WRITE_IMAGES: (ESPTOOL, "write-flash"),
    Purpose.START: (ESPTOOL, "write-mem"),
    Purpose.ERASE_TEST_AREA: (ESPTOOL, "erase-region"),
    Purpose.ERASE_CHIP: (ESPTOOL, "erase-flash"),
}


def board_problems(board: BoardInfo) -> List[str]:
    """Why the test program may not go onto this board (empty: it may)."""
    problems: List[str] = []
    if board.secure_download_mode:
        problems.append("board is in Secure Download Mode")
    if board.security not in _ALLOWED_STATES:
        problems.append(f"board security is {board.security.value}")
    if board.hardware is not HardwareVerdict.OK:
        problems.append(f"hardware gate says {board.hardware.value}")
    if board.evidence.get("secure_boot"):
        problems.append("Secure Boot is on")
    if board.evidence.get("flash_encryption"):
        problems.append("flash encryption is on")
    if not normalize_mac(board.mac):
        problems.append("no board ID")
    return problems


def never_started(board: BoardInfo) -> bool:
    """A board outside Secure Download Mode that has never started (blank, or with only our
    key locked): it is never replugged to watch its start (check_failed_board)."""
    return not board.secure_download_mode and board.security in _ALLOWED_STATES


def call_problems(planned: TestCall, images: Sequence[Tuple[TestImage, Path]] = ()
                  ) -> List[str]:
    """The rules every single board-test call keeps (empty: it keeps them)."""
    call = planned.call
    args = list(call.args)
    problems = [f"{token} is never allowed" for token in FORBIDDEN_TOKENS if token in args]
    problems += [f"{arg} is never allowed" for arg in args if arg.startswith("burn")]
    expected = _COMMANDS.get(planned.purpose)
    if expected is None:
        return problems + [f"unknown purpose {planned.purpose}"]
    tool, command = expected
    if call.tool != tool:
        problems.append(f"{planned.purpose.value} must use {tool}, not {call.tool}")
    others = {cmd for _tool, cmd in _COMMANDS.values()} - {command}
    if command not in args:
        problems.append(f"{planned.purpose.value} must run {command}")
    problems += [f"{planned.purpose.value} may not run {other}" for other in others
                 if other in args]
    if "--chip" not in args or args[args.index("--chip") + 1] != CHIP:
        problems.append("every call names the chip")
    if call.tool == ESPEFUSE:
        tail = args[args.index(command) + 1:] if command in args else []
        if tail[:3] != ["--format", "json", "--file"] or len(tail) != 4:
            problems.append("an eFuse summary only writes its JSON file")
        if "--after" in args:
            problems.append("espefuse has no --after")
        return problems
    after = args[args.index("--after") + 1] if "--after" in args else None
    wanted_after = "watchdog-reset" if planned.purpose is Purpose.START else "no-reset"
    if after != wanted_after:
        problems.append(f"{planned.purpose.value} must use --after {wanted_after}")
    if "--before" not in args or args[args.index("--before") + 1] != "default-reset":
        problems.append("every esptool call uses --before default-reset")
    tail = args[args.index(command) + 1:] if command in args else []
    if planned.purpose is Purpose.START:
        if tail != [hex(RTC_CNTL_OPTION1_REG), "0x0", hex(RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK)]:
            problems.append("the start call only clears the force-download flag")
    elif planned.purpose is Purpose.ERASE_TEST_AREA:
        start, end = WRITTEN_AREA
        if tail != [hex(start), hex(end - start)]:
            problems.append("the test area erase covers exactly the test program")
    elif planned.purpose is Purpose.ERASE_CHIP:
        if tail:
            problems.append("erase-flash takes no arguments")
    elif planned.purpose is Purpose.WRITE_IMAGES:
        values = [item for item in tail if not item.startswith("--") and item != "keep"]
        if tail[:len(KEEP_FLASH_ARGS)] != list(KEEP_FLASH_ARGS):
            problems.append("the write keeps the images' flash settings")
        pairs = [(values[i], values[i + 1]) for i in range(0, len(values) - 1, 2)]
        wanted = [(hex(image.offset), str(path)) for image, path in images]
        if len(values) % 2 or pairs != wanted or len(pairs) != len(IMAGES):
            problems.append("the write puts exactly the three test images at 0x0, 0x8000 and "
                            "0x10000")
        for image, path in images:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is None or size > image.max_size:
                problems.append(f"{image.name} does not fit at {image.offset:#x}")
    elif planned.purpose is Purpose.IDENTITY:
        if tail or "--no-stub" not in args:
            problems.append("the identity check is a plain ROM get-security-info")
    return problems


def safety_problems(board: BoardInfo, calls: Sequence[TestCall],
                    images: Sequence[Tuple[TestImage, Path]]) -> List[str]:
    """board_problems plus every call's rules; the only booting reset is the start call."""
    problems = board_problems(board)
    for planned in calls:
        problems += [f"{planned.call.label}: {problem}"
                     for problem in call_problems(planned, images)]
    starts = [planned for planned in calls if planned.purpose is Purpose.START]
    if len(starts) > 1:
        problems.append("the program is started only once")
    return problems


# --------------------------------------------------------------- the gate

def board_test_problem(mode: InstallMode, board: BoardInfo, report: Optional[BoardTestReport],
                       now_utc: datetime) -> Optional[str]:
    """Why ``report`` does not let ``mode`` lock or finish this board (None: it does).

    NEW_BOARD and RESUME need a PASSED test of this board ID with its eFuses proven
    unchanged and the test program erased, finished within TEST_VALID_S (by the injected
    clock). CHANGE_SETTINGS needs none.
    """
    if mode is InstallMode.CHANGE_SETTINGS:
        return None
    if report is None:
        return "no board test"
    if not isinstance(report, BoardTestReport) or report.verdict is not BoardTestVerdict.PASSED:
        return "the board test did not pass"
    if normalize_mac(report.mac) != normalize_mac(board.mac) or (
            normalize_mac(report.board.mac) != normalize_mac(board.mac)):
        return f"the board test is for {report.mac}, not {board.mac}"
    if report.efuses_unchanged is not True:
        return "the board test did not prove the eFuses unchanged"
    if report.erased is not True:
        return "the test program was not erased"
    try:
        finished = datetime.fromisoformat(report.finished_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return f"the board test time {report.finished_at!r} is not readable"
    if finished.tzinfo is None:
        return "the board test time has no time zone"
    age = (now_utc - finished).total_seconds()
    if age > TEST_VALID_S:
        return f"the board test is {age / 60:.0f} minutes old"
    if age < -60:
        return "the board test is dated in the future"
    return None


def check_board_test(mode: InstallMode, board: BoardInfo, report: Optional[BoardTestReport],
                     now_utc: datetime) -> None:
    """InstallerError(texts.BOARD_TEST_REQUIRED) unless board_test_problem is None."""
    problem = board_test_problem(mode, board, report, now_utc)
    if problem is not None:
        raise InstallerError(texts.BOARD_TEST_REQUIRED,
                             cause=f"{mode.value} refused: {problem}")


def gate_finding(board: BoardInfo) -> Tuple[TestFinding, Advice]:
    """The hardware gate's refusal as a finding with its evidence, and its advice."""
    advice = hardware_advice(board, locked=board.security is not SecurityState.BLANK) \
        or texts.TEST_FAILED
    facts = {key: board.evidence.get(key) for key in (
        "chip_line", "features", "flash_id", "flash_size_mb", "psram_mb",
        "xmc_startup_needed", "xmc_startup_failed", "hardware_detail") if key in board.evidence}
    detail = (f"The hardware check refused the board before anything was written: "
              f"{board.hardware.value}"
              f"{' (' + board.hardware_detail + ')' if board.hardware_detail else ''}")
    if facts:
        detail += "; " + ", ".join(f"{key}={value}" for key, value in facts.items())
    proof = [f"{key}: {value}" for key, value in facts.items()]
    return _finding(advice.code, "check", "fail", detail + ".", proof,
                    title=advice.title), advice


# -------------------------------------------------------------- judgement

@dataclass(frozen=True)
class Judgement:
    """What the board's console showed (TestJudge)."""

    verdict: BoardTestVerdict
    code: str                         # the main finding's code
    findings: Tuple[TestFinding, ...]
    measurements: Dict[str, object]
    end_crc_ok: Optional[bool] = None
    clean_ok: bool = False            # the program erased its own test sectors
    program_done: bool = False        # END seen: the program forces download mode itself
    recovery: Optional[str] = None
    advice: Optional[Advice] = None


def _last_proof(lines: Optional[Sequence[Tuple[float, str]]], count: int = 10
                ) -> Tuple[str, ...]:
    return tuple(text for _at, text in (lines or ())[-count:])


def _proof(*groups: Sequence[str]) -> Tuple[str, ...]:
    out: List[str] = []
    for group in groups:
        for line in group:
            if line and line not in out:
                out.append(line)
    return tuple(out[:PROOF_MAX])


def _finding(code: str, stage: str, result: str, detail: str,
             proof: Sequence[str] = (), title: Optional[str] = None) -> TestFinding:
    words = title or texts.board_test_advice(code).title
    return TestFinding(code, stage, result, words, detail, tuple(proof)[:PROOF_MAX])


def _int(value: object) -> Optional[int]:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _number(value: object) -> object:
    text = str(value)
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            continue
    return text


@dataclass
class _Sltest:
    """The SLTEST lines of one watch, in order."""

    begin: Optional[ConsoleEvent] = None
    begin_pos: int = -1
    begins: int = 0
    alive: Optional[ConsoleEvent] = None
    results: Dict[str, ConsoleEvent] = field(default_factory=dict)
    errors: Dict[str, List[ConsoleEvent]] = field(default_factory=dict)
    end: Optional[ConsoleEvent] = None
    clean: Optional[ConsoleEvent] = None
    first_pos: int = -1
    last_stage_pos: Dict[int, str] = field(default_factory=dict)


def _sltest(events: Sequence[ConsoleEvent]) -> _Sltest:
    found = _Sltest()
    for position, event in enumerate(events):
        if event.kind is not ConsoleKind.SELF_TEST:
            continue
        what = event.data.get("sltest")
        if found.first_pos < 0:
            found.first_pos = position
        if what == "BEGIN":
            found.begins += 1
            if found.begin is None:
                found.begin, found.begin_pos = event, position
        elif what == "ALIVE":
            found.alive = event
        elif what == "ERR":
            found.errors.setdefault(event.data.get("stage", "?"), []).append(event)
        elif what == "RESULT":
            found.results.setdefault(event.data.get("stage", "?"), event)
        elif what == "END" and found.end is None:
            found.end = event
        elif what == "CLEAN" and found.clean is None:
            found.clean = event
    return found


def _stage_at(events: Sequence[ConsoleEvent], position: int) -> str:
    """The test stage running at ``position``: the newest ALIVE before it, or the stage
    after the newest RESULT, or the first stage."""
    stage = None
    for event in events[:position]:
        if event.kind is not ConsoleKind.SELF_TEST:
            continue
        what = event.data.get("sltest")
        if what == "ALIVE":
            stage = event.data.get("stage")
        elif what == "RESULT":
            done = event.data.get("stage")
            stage = STAGES[STAGES.index(done) + 1] if done in STAGES[:-1] else done
    return stage if stage in STAGES else STAGES[0]


def _psram_lines(events: Sequence[ConsoleEvent]) -> List[ConsoleEvent]:
    return [event for event in events
            if event.kind in (ConsoleKind.PSRAM_OK, ConsoleKind.PSRAM_FAILED)
            or event.data.get("signal") in ("psram_init", "psram_density")]


def _panics(events: Sequence[ConsoleEvent]) -> List[ConsoleEvent]:
    """Crash lines, a panicking task watchdog too (the test program sets it to panic)."""
    return [event for event in events if event.kind is ConsoleKind.PANIC
            and event.data.get("reason") not in ("rebooting", "brownout")]


# ROM reset reasons that say the supply dipped (the brownout text itself is often lost: the
# chip resets at once and USB-Serial-JTAG drops with it).
POWER_RESETS = frozenset({"BROWN_OUT_RST", "GLITCH_RTC_RST"})
# ROM reset reasons that say the computer reset the chip over USB-Serial/JTAG (its RTS/DTR
# lines), never the board itself: e.g. a port closed and reopened on macOS.
HOST_RESETS = frozenset({"USB_UART_CHIP_RESET", "USB_JTAG_CHIP_RESET"})


def _host_reset(reason: Optional[str]) -> bool:
    return bool(reason) and (reason in HOST_RESETS or str(reason).startswith("USB_"))


def _brownout(events: Sequence[ConsoleEvent]) -> Optional[ConsoleEvent]:
    """The brownout detector's line, or a ROM reset line whose reason is a supply dip."""
    return next((event for event in events if (
        event.kind is ConsoleKind.PANIC and event.data.get("reason") == "brownout") or (
        event.kind is ConsoleKind.BOOT_BANNER
        and event.data.get("reset_reason") in POWER_RESETS)), None)


def _download_banner(event: ConsoleEvent) -> bool:
    """A ROM line saying the chip started in download mode (the program forces that after
    its CLEAN line)."""
    return event.kind is ConsoleKind.BOOT_BANNER and (
        event.data.get("download_mode") == "1"
        or (bool(event.data.get("reset_reason")) and "DOWNLOAD" in event.raw))


# Lines that only appear once ESP-IDF's start-up (PSRAM included) is over.
_APP_KINDS = frozenset({
    ConsoleKind.APP_VERSION, ConsoleKind.APP_STARTED, ConsoleKind.STORAGE_READY,
    ConsoleKind.WIFI_CONNECTING, ConsoleKind.WIFI_CONNECTED, ConsoleKind.SETUP_AP_OPEN,
    ConsoleKind.ADD_CONNECTED, ConsoleKind.ADD_FAILED, ConsoleKind.TERMINAL_FOUND,
    ConsoleKind.PROVISIONING_LOADED, ConsoleKind.SETTINGS_MISSING, ConsoleKind.NVS_FAILED,
    ConsoleKind.NVS_KEY_DANGER, ConsoleKind.FAMILY_MISMATCH})


def _past_startup(events: Sequence[ConsoleEvent]) -> bool:
    return any(event.kind in _APP_KINDS or event.data.get("app_main") == "1"
               for event in events)


def _period(reset_times: Sequence[float]) -> Optional[float]:
    if len(reset_times) < 2:
        return None
    gaps = [b - a for a, b in zip(reset_times, reset_times[1:])]
    return sum(gaps) / len(gaps)


def measurements_from(events: Sequence[ConsoleEvent]) -> Dict[str, object]:
    """The BEGIN line's facts and each RESULT's numbers."""
    found = _sltest(events)
    values: Dict[str, object] = {}
    if found.begin is not None:
        data = found.begin.data
        for key, name in (("fw", "test_program_version"), ("mac", "mac_reported"),
                          ("chip_rev", "chip_rev"), ("cores", "cores"), ("idf", "idf"),
                          ("flash_id", "flash_id"), ("flash_size", "flash_size"),
                          ("psram_size", "psram_size"), ("proto", "protocol")):
            if key in data:
                values[name] = _number(data[key]) if key in ("cores", "flash_size",
                                                             "psram_size", "proto") else data[key]
    renamed = {("psram", "mb_s"): "psram_mb_s", ("wifi", "networks"): "networks",
               ("wifi", "best_rssi"): "best_rssi", ("cpu", "temp_c"): "temp_c"}
    # The flash RESULT repeats the chip's ID and size as the test read them: kept apart from
    # BEGIN's flash_id/flash_size, and the hex ID stays text (204018 is not a number).
    renamed.update({("flash", "id"): "flash_id_read", ("flash", "size"): "flash_size_read"})
    as_text = {("flash", "id"), ("cpu", "sum")}
    for stage, event in found.results.items():
        for key, value in event.data.items():
            if key in ("sltest", "stage"):
                continue
            name = renamed.get((stage, key), f"{stage}_{key}")
            values.setdefault(name, value if (stage, key) in as_text else _number(value))
    return values


def _result_detail(stage: str, event: ConsoleEvent,
                   errors: Sequence[ConsoleEvent]) -> str:
    data = event.data
    tech = _TECH.get(stage, stage)
    count = _int(data.get("errors")) or 0
    words = (f"{tech} test: result {data.get('result', '?')}, code {data.get('code', '?')}, "
             f"detail {data.get('detail', '-')}, {count:,} errors, {data.get('ms', '?')} ms")
    if errors:
        first = errors[0].data
        words += (f"; first error in {first.get('test', '?')} at {first.get('addr', '?')} "
                  f"(wrote {first.get('want', '?')}, read {first.get('got', '?')})")
    extra = [f"{key}={value}" for key, value in data.items()
             if key not in ("sltest", "stage", "result", "code", "detail", "errors", "ms")]
    if extra:
        words += "; " + ", ".join(extra)
    return words


def _result_finding(stage: str, event: ConsoleEvent,
                    errors: Sequence[ConsoleEvent]) -> TestFinding:
    result = event.data.get("result", "fail")
    if result not in ("ok", "warn", "fail"):
        result = "fail"
    code = event.data.get("code") or ("OK" if result == "ok" else f"{stage.upper()}_FAILED")
    title = _OK_TITLES.get(stage, "This part works") if result == "ok" else None
    proof = [event.raw] + [error.raw for error in errors[:8]]
    return _finding(code, stage, result, _result_detail(stage, event, errors), proof, title)


class TestJudge:
    """Decides a watched test start from its console (pure: watch + time in, verdict out).

    ``check`` is called for every event and in silence; it returns the Judgement once the
    watch can stop: a PASS waits for the program's CLEAN line, a fault keeps reading
    LINGER_S more for the proof. ``final`` (the deadline or a cancel) always decides.
    """

    __test__ = False  # not a pytest test class

    def __init__(self, mac: str, *, locked: bool = False,
                 placeholders: Sequence[str] = ("RE7",), board: Optional[BoardInfo] = None
                 ) -> None:
        self.mac = normalize_mac(mac) or mac
        self.locked = locked
        self.placeholders = tuple(placeholders)
        self.board = board
        self._decided: Optional[Judgement] = None
        self._decided_at: Optional[float] = None

    def check(self, watch: ConsoleWatch, now: float, *, final: bool = False
              ) -> Optional[Judgement]:
        t = now - watch.started
        if self._decided is not None:
            if final or self._decided_at is None or t - self._decided_at >= LINGER_S:
                return self._decided
            return None
        verdict = self.evaluate(watch, t, final=final)
        if verdict is None:
            return None
        if final or verdict.verdict is BoardTestVerdict.PASSED or verdict.program_done:
            return verdict
        self._decided, self._decided_at = verdict, t
        return None

    @property
    def undecided(self) -> bool:
        """No fault decided yet (none lingering for its proof)."""
        return self._decided is None

    def decisive(self, watch: ConsoleWatch, now: float, judgement: Judgement) -> bool:
        """A FAILED ``judgement`` that the console proves without the deadline (decided
        already, or reached by evaluate(final=False) too): it stands after a cancel. Any
        other judgement made at a cancel is the cancel's."""
        if judgement.verdict is not BoardTestVerdict.FAILED:
            return False
        if self._decided is not None:
            return True
        again = self.evaluate(watch, now - watch.started, final=False)
        return again is not None and again.verdict is BoardTestVerdict.FAILED

    def evaluate(self, watch: ConsoleWatch, t: float, *, final: bool) -> Optional[Judgement]:
        """The verdict at ``t`` seconds into the watch, or None while it is too early."""
        if self.locked:
            return self._locked(watch, t, final)
        found = _sltest(watch.events)
        if found.first_pos < 0:
            return self._before_begin(watch, t, final)
        return self._after_begin(watch, found, t, final)

    # ------------------------------------------------------ the test program

    def _judgement(self, verdict: BoardTestVerdict, code: str,
                   findings: Sequence[TestFinding], watch: ConsoleWatch, **extra: object
                   ) -> Judgement:
        advice = extra.pop("advice", None) or texts.board_test_advice(code)
        return Judgement(verdict, code, tuple(findings), measurements_from(watch.events),
                         advice=advice, **extra)  # type: ignore[arg-type]

    def _before_begin(self, watch: ConsoleWatch, t: float, final: bool
                      ) -> Optional[Judgement]:
        events = watch.events
        lines = watch.lines
        refused = startup_refusal(events, final=final)
        if refused is not None:
            proof = _proof([refused.raw], _last_proof(lines))
            if refused.data.get("reason") == "xmc_flash":
                return self._judgement(BoardTestVerdict.FAILED, "FLASH_CHIP_UNSUPPORTED", [
                    _finding("FLASH_CHIP_UNSUPPORTED", "startup", "fail",
                             "The ESP-IDF 5.5.3 second-stage bootloader stopped in its XMC "
                             "flash startup flow; this flash chip can never start ESP-IDF "
                             "5.5.3 software.", proof)], watch)
            return self._judgement(BoardTestVerdict.INCOMPLETE, "TEST_PROGRAM_REFUSED", [
                _finding("TEST_PROGRAM_REFUSED", "startup", "warn",
                         f"The bootloader refused to start the test program "
                         f"({refused.data.get('reason', 'other')}).", proof)], watch)
        brownout = _brownout(events)
        if brownout is not None and (final or restarts_seen(events, watch.resets) >= 1):
            return self._power(watch, brownout)
        psram = _psram_lines(events)
        panics = _panics(events)
        restarts = restarts_seen(events, watch.resets)
        last = watch.last_line_at
        silent = last is not None and t - last >= SILENCE_S
        settled = bool(panics) and (final or t - panics[0].at >= SETTLE_S)
        # Evidence only: a crash, two restarts or real silence. A normal start prints the
        # same PSRAM lines about 5 s before BEGIN, so a cancel or the deadline in that
        # window is never a fault by itself.
        if psram and (settled or restarts >= 2 or silent):
            how = ("crashed" if panics else "restarted" if restarts >= 2
                   else "went silent")
            proof = _proof([event.raw for event in psram][:6],
                           [event.raw for event in panics][:3], _last_proof(lines, 6))
            return self._judgement(BoardTestVerdict.FAILED, "PSRAM_STARTUP_FAULT", [
                _finding("PSRAM_STARTUP_FAULT", "startup", "fail",
                         f"The board {how} during ESP-IDF's PSRAM start-up, after the octal "
                         f"PSRAM driver identified the chip and before the test program "
                         f"started ({len(panics)} crash lines, {restarts} restarts).",
                         proof)], watch)
        if settled:
            crash = panics[0]
            proof = _proof([crash.raw], _last_proof(lines, 12))
            return self._judgement(BoardTestVerdict.FAILED, "STARTUP_CRASH", [
                _finding("STARTUP_CRASH", "startup", "fail",
                         f"The board crashed during ESP-IDF start-up, before the test program "
                         f"ran ({crash.data.get('reason', 'panic')}).", proof)], watch)
        meaningful = [event for event in events if event.kind is not ConsoleKind.BOOT_BANNER
                      and event.data.get("signal") != "port_busy"]
        period = _period(watch.reset_times)
        if watch.resets >= 2 and period is not None and not meaningful:
            if 6.0 <= period <= 16.0:
                return self._judgement(BoardTestVerdict.FAILED, "STARTUP_HANG", [
                    _finding("STARTUP_HANG", "startup", "fail",
                             f"Silent restart loop: the USB port drops and returns about every "
                             f"{period:.1f} s ({watch.resets} times) with no console output, "
                             f"the RTC watchdog signature of a board that hangs during start-up.",
                             _last_proof(lines))], watch)
            if period < 6.0 and watch.resets >= 3:
                advice = texts.restart_loop_fast(period, button="Test again")
                return self._judgement(BoardTestVerdict.INCOMPLETE, advice.code, [
                    _finding(advice.code, "startup", "warn",
                             f"Fast restart loop: the USB port drops about every {period:.1f} s "
                             f"with no console output (power or cable first).",
                             _last_proof(lines), title=advice.title)], watch, advice=advice)
        download = any(event.data.get("download_mode") == "1" for event in events)
        board_lines = watch.other_lines + len(meaningful) + sum(
            1 for event in events if event.kind is ConsoleKind.BOOT_BANNER)
        if download and not meaningful:
            return self._no_start(watch, "The chip came back in ROM download mode instead of "
                                         "starting the test program.")
        if not board_lines and watch.resets == 0 and t >= NO_START_S:
            return self._no_start(watch, f"No console output and no USB reset in {t:.0f} s "
                                         "after the start.")
        if board_lines and silent and t >= NO_START_S:
            return self._no_start(watch, "Start-up lines, then silence before the test program "
                                         "began.")
        if not board_lines and t >= LOOP_DECIDE_S:
            return self._no_start(watch, f"No console output in {t:.0f} s "
                                         f"({watch.resets} USB resets).")
        if final:
            return self._no_start(watch, "The test program did not begin.")
        return None

    def _end_lost(self, watch: ConsoleWatch, found: _Sltest, results: Sequence[TestFinding],
                  t: float, final: bool) -> Optional[Judgement]:
        """The program finished but its END line never arrived intact (lost or garbled on
        USB): never a pass and never a board fault, the run is not proven. Proof that it
        finished: its CLEAN line, its restart into download mode (it does that after CLEAN),
        or every RESULT and then silence."""
        events = watch.events
        done = all(stage in found.results for stage in STAGES)
        # The start call cleared the force-download flag: only the program sets it again,
        # after CLEAN, so a download-mode start after BEGIN means it finished.
        start = found.begin_pos if found.begin_pos >= 0 else found.first_pos
        download = any(_download_banner(event) for event in events[start + 1:])
        last = watch.last_line_at if watch.last_line_at is not None else 0.0
        quiet = done and (final or t - last >= SILENCE_S)
        if found.clean is None and not download and not quiet:
            return None
        clean_ok = found.clean is not None and found.clean.data.get("result") == "ok"
        seen = ("its CLEAN line" if found.clean is not None
                else "its restart into download mode" if download
                else "every result, then silence")
        proof = [event.raw for event in (found.clean,) if event is not None]
        return self._judgement(BoardTestVerdict.INCOMPLETE, "USB_DATA_GARBLED", list(results) + [
            _finding("USB_DATA_GARBLED", "check", "warn",
                     f"The test program finished ({seen}) but its END line was not received "
                     f"intact, so the run cannot be proven.",
                     _proof(proof, _last_proof(watch.lines, 6)))], watch,
            clean_ok=clean_ok, program_done=found.clean is not None or download)

    def _no_start(self, watch: ConsoleWatch, detail: str) -> Judgement:
        return self._judgement(BoardTestVerdict.INCOMPLETE, "NO_START", [
            _finding("NO_START", "start", "warn", detail, _last_proof(watch.lines))], watch)

    def _power(self, watch: ConsoleWatch, brownout: ConsoleEvent) -> Judgement:
        return self._judgement(BoardTestVerdict.INCOMPLETE, "POWER_PROBLEM", [
            _finding("POWER_PROBLEM", "startup", "warn",
                     "The brownout detector reset the chip: the USB supply voltage dropped.",
                     _proof([brownout.raw], _last_proof(watch.lines, 6)))], watch,
            advice=texts.POWER_PROBLEM)

    def _after_begin(self, watch: ConsoleWatch, found: _Sltest, t: float, final: bool
                     ) -> Optional[Judgement]:
        events = watch.events
        lines = watch.lines
        start = found.begin_pos if found.begin_pos >= 0 else found.first_pos
        after = events[start + 1:]
        results = [_result_finding(stage, found.results[stage], found.errors.get(stage, ()))
                   for stage in STAGES if stage in found.results]
        results += [_result_finding(stage, event, found.errors.get(stage, ()))
                    for stage, event in found.results.items() if stage not in STAGES]
        fails = [item for item in results if item.result == "fail"
                 and item.code not in NOT_BOARD_FAULTS]
        unable = [item for item in results if item.result == "fail"
                  and item.code in NOT_BOARD_FAULTS]
        reported = (found.begin.data.get("mac") if found.begin is not None else None)
        if reported and normalize_mac(reported) and normalize_mac(reported) != self.mac:
            return self._judgement(BoardTestVerdict.INCOMPLETE, "BOARD_SWAPPED", [
                _finding("BOARD_SWAPPED", "check", "warn",
                         f"The test program ran on {reported}, not on {self.mac}.",
                         [found.begin.raw])], watch, advice=texts.BOARD_SWAPPED)

        brownout = _brownout(after)
        if brownout is not None:
            return self._power(watch, brownout)
        if found.end is None:
            lost = self._end_lost(watch, found, results, t, final)
            if lost is not None:
                return lost
        panics = _panics(after)
        if panics and found.end is None:
            crash = panics[0]
            if not final and t - crash.at < SETTLE_S:
                return None
            stage = _stage_at(events, events.index(crash))
            code = "PSRAM_CRASH" if stage == "psram" else "TEST_CRASH"
            finding = _finding(code, stage, "fail",
                               f"The board crashed during the {_TECH[stage]} test "
                               f"({crash.data.get('reason', 'panic')}"
                               f"{', core ' + crash.data['core'] if crash.data.get('core') else ''}"
                               f").", _proof([crash.raw], _last_proof(lines, 12)))
            return self._judgement(BoardTestVerdict.FAILED, code, results + [finding], watch)

        restarted = [event for event in after
                     if (event.kind is ConsoleKind.SELF_TEST
                         and event.data.get("sltest") == "BEGIN")
                     or (event.kind is ConsoleKind.BOOT_BANNER
                         and event.data.get("reset_reason"))]
        if restarted and found.end is None:
            stage = _stage_at(events, events.index(restarted[0]))
            reason = next((event.data.get("reset_reason") for event in restarted
                           if event.data.get("reset_reason")), None)
            proof = _proof([event.raw for event in restarted][:3], _last_proof(lines, 8))
            if _host_reset(reason):
                return self._judgement(BoardTestVerdict.INCOMPLETE, "TEST_RESTARTED", results + [
                    _finding("TEST_RESTARTED", stage, "warn",
                             f"The computer reset the board over USB ({reason}) during the "
                             f"{_TECH[stage]} test; this is not a board fault.", proof)], watch)
            if reason and reason != "POWERON":
                code = "PSRAM_CRASH" if stage == "psram" else "TEST_CRASH"
                return self._judgement(BoardTestVerdict.FAILED, code, results + [_finding(
                    code, stage, "fail", f"The chip reset itself ({reason}) during the "
                    f"{_TECH[stage]} test.", proof)], watch)
            return self._judgement(BoardTestVerdict.INCOMPLETE, "TEST_RESTARTED", results + [
                _finding("TEST_RESTARTED", stage, "warn",
                         f"The board restarted during the {_TECH[stage]} test "
                         f"(reset reason {reason or 'not seen'}).", proof)], watch)

        if found.end is not None:
            end = found.end
            crc_ok = end.data.get("crc_ok") == "1"
            clean_ok = found.clean is not None and found.clean.data.get("result") == "ok"
            if found.clean is None and not final and t - end.at < END_TO_CLEAN_S \
                    and watch.port_open:
                return None
            extra = {"end_crc_ok": crc_ok, "clean_ok": clean_ok, "program_done": True}
            if not crc_ok:
                missed = "" if found.begin is not None else " (the BEGIN line was not received)"
                return self._judgement(BoardTestVerdict.INCOMPLETE, "USB_DATA_GARBLED",
                                       results + [_finding(
                                           "USB_DATA_GARBLED", "check", "warn",
                                           f"The END line's CRC-32 {end.data.get('crc', '?')} "
                                           f"does not match the lines received{missed}.",
                                           [end.raw])], watch, **extra)
            if fails:
                return self._judgement(BoardTestVerdict.FAILED, fails[0].code, results, watch,
                                       **extra)
            if unable:
                return self._judgement(BoardTestVerdict.INCOMPLETE, unable[0].code, results,
                                       watch, advice=texts.board_test_advice(unable[0].code),
                                       **extra)
            if end.data.get("result") != "ok":
                return self._judgement(BoardTestVerdict.FAILED, "TEST_FAILED", results + [
                    _finding("TEST_FAILED", "check", "fail",
                             f"The test program reported failed stages: "
                             f"{end.data.get('failed', '?')}.", [end.raw])], watch, **extra)
            missing = [stage for stage in STAGES if stage not in found.results]
            if missing:
                return self._judgement(BoardTestVerdict.INCOMPLETE, "TEST_INCOMPLETE",
                                       results + [_finding(
                                           "TEST_INCOMPLETE", "check", "warn",
                                           f"No result for: {', '.join(missing)}.",
                                           [end.raw])], watch, **extra)
            return self._judgement(BoardTestVerdict.PASSED, "BOARD_TEST_PASSED", results, watch,
                                   **extra)

        last = watch.last_line_at if watch.last_line_at is not None else 0.0
        stage = _stage_at(events, len(events))
        hang = _finding(f"{stage.upper()}_HANG", stage, "fail",
                        f"No console output for {t - last:.1f} s during the {_TECH[stage]} "
                        f"test while the USB port stayed open (heartbeat every 0.5 s expected).",
                        _last_proof(lines, 10))
        if watch.port_open and t - last >= SILENCE_S:
            return self._judgement(BoardTestVerdict.FAILED, hang.code, results + [hang], watch)
        if not watch.port_open and t - last >= SILENCE_S + PORT_WAIT_S:
            return self._judgement(BoardTestVerdict.INCOMPLETE, "BOARD_DISCONNECTED",
                                   results + [_finding(
                                       "BOARD_DISCONNECTED", stage, "warn",
                                       f"The USB port went away during the {_TECH[stage]} test "
                                       "and did not come back.", _last_proof(lines, 6))],
                                   watch, advice=texts.BOARD_DISCONNECTED)
        if not final:
            return None
        if fails:
            return self._judgement(BoardTestVerdict.FAILED, fails[0].code, results, watch)
        if watch.port_open and t - last >= SILENCE_S:
            return self._judgement(BoardTestVerdict.FAILED, hang.code, results + [hang], watch)
        return self._judgement(BoardTestVerdict.INCOMPLETE, "TEST_INCOMPLETE", results + [
            _finding("TEST_INCOMPLETE", stage, "warn",
                     f"The test program did not finish ({stage} test still running).",
                     _last_proof(lines, 6))], watch)

    # ------------------------------------------------------- a locked board

    def _locked(self, watch: ConsoleWatch, t: float, final: bool) -> Optional[Judgement]:
        """A locked board's own start (check_failed_board): never written, only watched."""
        events = watch.events
        lines = watch.lines
        board = self.board
        recovery_resume = board is not None and \
            board.security is SecurityState.KEY_BURNED_NOT_STARTED
        fixable = board is not None and board.security is SecurityState.SECURED_STATE_LIFE

        def first(*wanted: ConsoleKind) -> Optional[ConsoleEvent]:
            return next((event for event in events if event.kind in wanted), None)

        def done(verdict: BoardTestVerdict, code: str, stage: str, result: str, detail: str,
                 proof: Sequence[str], advice: Optional[Advice] = None,
                 recovery: Optional[str] = None, title: Optional[str] = None) -> Judgement:
            words = title or (advice.title if advice is not None else None)
            return self._judgement(verdict, code, [
                _finding(code, stage, result, detail, proof, title=words)], watch,
                advice=advice, recovery=recovery)

        refused = startup_refusal(events, final=final)
        if refused is not None:
            proof = _proof([refused.raw], _last_proof(lines, 8))
            if refused.data.get("reason") == "xmc_flash":
                return done(BoardTestVerdict.FAILED, "FLASH_CHIP_UNSUPPORTED", "startup", "fail",
                            "The ESP-IDF 5.5.3 second-stage bootloader stops in its XMC flash "
                            "startup flow on every start.", proof,
                            texts.flash_chip_unsupported(board.flash_id if board else None,
                                                         locked=True))
            return done(BoardTestVerdict.INCOMPLETE, "STARTUP_REFUSED", "startup", "warn",
                        f"The bootloader refused to start the software "
                        f"({refused.data.get('reason', 'other')}).", proof,
                        texts.startup_refused(refused.raw[:160]))
        key_danger = first(ConsoleKind.NVS_KEY_DANGER)
        if key_danger is not None:
            return done(BoardTestVerdict.INCOMPLETE, "SECURITY_KEY_LOST", "startup", "warn",
                        "The software generated its own NVS encryption keys: the HMAC key was "
                        "missing at the first start.", [key_danger.raw], texts.SECURITY_KEY_LOST)
        settings = first(ConsoleKind.SETTINGS_MISSING, ConsoleKind.NVS_FAILED,
                         ConsoleKind.FAMILY_MISMATCH)
        if settings is not None:
            advice = texts.FAILED_BOARD_FIX_SETTINGS if fixable else texts.SETTINGS_UNREADABLE
            return done(BoardTestVerdict.INCOMPLETE, "SETTINGS_MISSING", "startup", "warn",
                        f"The software starts but its encrypted settings are missing or "
                        f"unreadable ({settings.kind.value}).", [settings.raw], advice,
                        RECOVERY_FIX_SETTINGS if fixable else None)
        psram_failed = first(ConsoleKind.PSRAM_FAILED)
        if psram_failed is not None:
            return done(BoardTestVerdict.FAILED, "PSRAM_STARTUP_FAULT", "startup", "fail",
                        "ESP-IDF's PSRAM start-up failed (the PSRAM was not found or failed "
                        "its memory test).", _proof([psram_failed.raw], _last_proof(lines, 8)))
        brownout = _brownout(events)
        if brownout is not None and restarts_seen(events, watch.resets) >= 1:
            return self._power(watch, brownout)
        if crash_loop(events, watch.resets):
            crashed = crashes(events) or _panics(events)
            proof = _proof([event.raw for event in _psram_lines(events)][:6],
                           [event.raw for event in crashed][:3], _last_proof(lines, 6))
            if crash_advice(events) is texts.STARTUP_CRASH:
                code = "PSRAM_STARTUP_FAULT" if _psram_lines(events) else "STARTUP_CRASH"
                return done(BoardTestVerdict.FAILED, code, "startup", "fail",
                            f"The board crashes on every start during ESP-IDF start-up, before "
                            f"the application runs "
                            f"({crashed[0].data.get('reason', 'panic') if crashed else 'panic'}"
                            f", {restarts_seen(events, watch.resets)} restarts).", proof)
            return done(BoardTestVerdict.INCOMPLETE, "CRASH_LOOP", "startup", "warn",
                        "The application starts and then crashes, again and again.", proof,
                        texts.CRASH_LOOP)
        psram = _psram_lines(events)
        if psram and not _past_startup(events):
            restarts = restarts_seen(events, watch.resets)
            last = watch.last_line_at
            silent = last is not None and t - last >= SILENCE_S
            if restarts >= 2 or silent:
                # The count is only given when it decided the verdict: a silent board is
                # judged before its watchdog restarts it, and the report's measurements hold
                # the restarts seen by the end of the watch.
                how = (f"restarts ({restarts} restarts)" if restarts >= 2
                       else "goes silent")
                return done(BoardTestVerdict.FAILED, "PSRAM_STARTUP_FAULT", "startup", "fail",
                            f"The board {how} during ESP-IDF's PSRAM start-up, after the octal "
                            f"PSRAM driver identified the chip and before the application "
                            f"ran.",
                            _proof([event.raw for event in psram][:6], _last_proof(lines, 6)))
        running = next((event for event in events if event.kind is ConsoleKind.APP_STARTED
                        or event.kind in (ConsoleKind.STORAGE_READY, ConsoleKind.WIFI_CONNECTING,
                                          ConsoleKind.WIFI_CONNECTED, ConsoleKind.SETUP_AP_OPEN,
                                          ConsoleKind.ADD_CONNECTED, ConsoleKind.ADD_FAILED,
                                          ConsoleKind.TERMINAL_FOUND)), None)
        if running is not None:
            hints = boot_hints(events, self.placeholders)
            connected = first(ConsoleKind.ADD_CONNECTED)
            if connected is not None:
                return done(BoardTestVerdict.PASSED, "BOARD_STARTS_NORMALLY", "startup", "ok",
                            "The application started and connected to ADD.",
                            [running.raw, connected.raw], texts.BOARD_STARTS_NORMALLY)
            hotspot = first(ConsoleKind.SETUP_AP_OPEN)
            failed = first(ConsoleKind.ADD_FAILED)
            retries = sum(1 for event in events if event.kind is ConsoleKind.WIFI_RETRY)
            advice = None
            if hotspot is not None:
                advice = texts.hotspot_open(hints.get("hotspot"))
            elif failed is not None:
                advice = (texts.TIME_SERVICE_BLOCKED if failed.data.get("reason") == "time"
                          else texts.HEAD_OFFICE_UNREACHABLE)
            elif hints.get("wifi_placeholder") == "1" and (final or retries >= 3):
                advice = texts.wifi_placeholder(hints.get("wifi_ssid", ""), hints.get("hotspot"))
            elif retries >= 3:
                advice = texts.wifi_not_joining(hints.get("wifi_ssid", ""), hints.get("hotspot"))
            if advice is not None:
                said = hotspot or failed or running
                return done(BoardTestVerdict.INCOMPLETE, "FAILED_BOARD_NO_HEAD_OFFICE",
                            "startup", "info",
                            f"The application runs but did not reach ADD ({advice.code}).",
                            [running.raw, said.raw], advice,
                            title=texts.FAILED_BOARD_NO_HEAD_OFFICE.title)
            if final:
                return done(BoardTestVerdict.PASSED, "BOARD_STARTS_NORMALLY", "startup", "ok",
                            "The application started; it had not reached ADD when the watch "
                            "ended.", [running.raw], texts.BOARD_STARTS_NORMALLY)
            return None
        period = _period(watch.reset_times)
        meaningful = [event for event in events if event.kind is not ConsoleKind.BOOT_BANNER
                      and event.data.get("signal") != "port_busy"]
        # Start-up lines alone (ROM, bootloader, PSRAM) still fit a silent start-up loop.
        startup_only = [event for event in meaningful if event not in psram]
        if watch.resets >= 2 and period is not None and not startup_only:
            if 6.0 <= period <= 16.0:
                return done(BoardTestVerdict.FAILED, "STARTUP_HANG", "startup", "fail",
                            f"Silent restart loop: the USB port drops and returns about every "
                            f"{period:.1f} s with no console output (RTC watchdog during "
                            f"start-up).", _last_proof(lines))
            if period < 6.0 and watch.resets >= 3:
                advice = texts.restart_loop_fast(period, button="Check again")
                return done(BoardTestVerdict.INCOMPLETE, advice.code, "startup", "warn",
                            f"Fast restart loop about every {period:.1f} s (power or cable "
                            f"first).", _last_proof(lines), advice)
        if crashes(events) and (final or t - crashes(events)[0].at >= SETTLE_S + 8.0):
            crashed = crashes(events)
            code = "PSRAM_STARTUP_FAULT" if _psram_lines(events) else "STARTUP_CRASH"
            if crash_advice(events) is texts.STARTUP_CRASH:
                return done(BoardTestVerdict.FAILED, code, "startup", "fail",
                            f"The board crashed during ESP-IDF start-up "
                            f"({crashed[0].data.get('reason', 'panic')}).",
                            _proof([crashed[0].raw], _last_proof(lines, 8)))
        if not final:
            return None
        if board is not None and board.evidence.get("note") == "FIRST_START_UNFINISHED":
            return done(BoardTestVerdict.INCOMPLETE, "FIRST_START_UNFINISHED", "startup", "warn",
                        "The first start burned the Secure Boot digests but stopped before "
                        "Secure Boot was enabled.", _last_proof(lines),
                        texts.FIRST_START_UNFINISHED)
        if recovery_resume:
            return done(BoardTestVerdict.INCOMPLETE, "FAILED_BOARD_RESUME", "startup", "warn",
                        "The key is locked but the setup did not write the software's start.",
                        _last_proof(lines), texts.FAILED_BOARD_RESUME, RECOVERY_RESUME)
        if any(event.data.get("download_mode") == "1" for event in events):
            return done(BoardTestVerdict.INCOMPLETE, "DOWNLOAD_MODE_STUCK", "startup", "warn",
                        "The chip started in ROM download mode.", _last_proof(lines),
                        texts.DOWNLOAD_MODE_STUCK)
        if not watch.other_lines and not meaningful and not events:
            return done(BoardTestVerdict.INCOMPLETE, "FAILED_BOARD_SILENT", "startup", "warn",
                        f"No console output in {t:.0f} s after the replug "
                        f"({watch.resets} USB resets).", (), texts.FAILED_BOARD_SILENT)
        return done(BoardTestVerdict.INCOMPLETE, "START_NOT_SEEN", "startup", "warn",
                    "Console lines came, but none from the running application.",
                    _last_proof(lines), texts.START_NOT_SEEN)


# ----------------------------------------------------------------- tester

BEGIN_MISSED = "BEGIN_MISSED"   # internal: a start's lines came without BEGIN (restart once)


def _log_note(record: Optional[ConsoleLog], text: str) -> None:
    if record is not None:
        record.note(text)


def _begin_missed(events: Sequence[ConsoleEvent]) -> bool:
    """The first test program line of this watch is not its BEGIN."""
    first = next((event for event in events if event.kind is ConsoleKind.SELF_TEST), None)
    return first is not None and first.data.get("sltest") != "BEGIN"


def _progressing(watch: ConsoleWatch, now: float) -> bool:
    """The program printed a test line lately: within SILENCE_S, or within END_TO_CLEAN_S
    after its END (it erases its test blocks before CLEAN)."""
    newest = next((event for event in reversed(watch.events)
                   if event.kind is ConsoleKind.SELF_TEST), None)
    if newest is None:
        return False
    allowed = END_TO_CLEAN_S if newest.data.get("sltest") == "END" else SILENCE_S
    return now - watch.started - newest.at < allowed


class _Abort(Exception):
    """Stops a test run with a finding; cleanup still runs when something was written."""

    def __init__(self, verdict: BoardTestVerdict, finding: TestFinding,
                 advice: Optional[Advice] = None) -> None:
        super().__init__(finding.code)
        self.verdict = verdict
        self.finding = finding
        self.advice = advice


@dataclass
class _Run:
    board: BoardInfo
    on_progress: Callable[[BoardTestProgress], None]
    cancel: CancelToken
    work: Path
    tool_log: List[str] = field(default_factory=list)
    findings: List[TestFinding] = field(default_factory=list)
    efuse_before: Dict[str, object] = field(default_factory=dict)
    efuse_after: Dict[str, object] = field(default_factory=dict)
    written: bool = False
    started: bool = False             # the start call ran: the program may have run
    starts: int = 0                   # start calls made (at most MAX_STARTS)
    erase_all: bool = False           # a stopped run may have left test blocks: whole chip
    console_log: Optional[str] = None
    port: str = ""


class _UsbFinder:
    def find(self, mac: str) -> Tuple[Optional[UsbCandidate], int]:  # pragma: no cover
        raise NotImplementedError


class BoardTester:
    """BoardTestServiceProto (module notes)."""

    def __init__(self, *, runner: ToolRunner, usb: _UsbFinder, clock: Optional[Clock] = None,
                 image_dir: Optional[Path] = None, images: Tuple[TestImage, ...] = IMAGES,
                 temp_root: Optional[Path] = None, console_logs: Optional[Path] = None,
                 opener: Optional[SerialOpener] = None,
                 placeholder_ssids: Sequence[str] = ("RE7",)) -> None:
        self._runner = runner
        self._usb = usb
        self._clock = clock or Clock()
        self._image_dir = image_dir
        self._images = images
        self._temp_root = temp_root
        self._console_logs = console_logs
        self._watcher = BootWatcher(usb, self._clock, opener=opener,
                                    placeholder_ssids=placeholder_ssids,
                                    console_logs=console_logs)
        self._placeholders = tuple(placeholder_ssids)

    # ---------------------------------------------------------- public

    def run_test(self, board: BoardInfo, on_progress: Callable[[BoardTestProgress], None],
                 on_phase: PhaseFn, on_event: Callable[[ConsoleEvent], None],
                 cancel: CancelToken) -> BoardTestReport:
        with keep_awake():
            return self._run_test(board, on_progress, on_phase, on_event, cancel)

    def check_failed_board(self, board: BoardInfo, on_phase: PhaseFn,
                           on_event: Callable[[ConsoleEvent], None], cancel: CancelToken,
                           seconds: float = 90.0) -> BoardTestReport:
        if not board_problems(board) or never_started(board):
            # Unlocked enough for the test program (an earlier setup stopped after the key),
            # or a board that has never started: a replug would boot whatever is in its
            # flash, maybe the production Secure Boot bootloader, whose first start burns
            # eFuses. run_test never resets it when it may not run: a gate refusal is
            # reported at once, anything else is BOARD_TEST_NOT_POSSIBLE.
            return self.run_test(board, lambda progress: None, on_phase, on_event, cancel)
        with keep_awake():
            return self._check_locked(board, on_phase, on_event, cancel, seconds)

    # ---------------------------------------------------------- helpers

    def _now(self) -> str:
        return self._clock.now_utc().astimezone().replace(microsecond=0).isoformat()

    def _progress(self, run: _Run, stage: str, within: Optional[float], detail: str) -> None:
        low, high = _SPAN.get(stage, (1.0, 1.0))
        fraction = None if within is None else low + (high - low) * max(0.0, min(1.0, within))
        try:
            run.on_progress(BoardTestProgress(stage, fraction, detail))
        except Exception as error:  # a screen problem must not stop the test
            log.warning("board test progress listener failed: %s", error)

    def _kind(self, board: BoardInfo) -> str:
        return KIND_VENDOR if board.security is SecurityState.BLANK else KIND_LOCKED

    def _report(self, board: BoardInfo, verdict: BoardTestVerdict, started: str,
                findings: Sequence[TestFinding], *, run: Optional[_Run] = None,
                measurements: Optional[Dict[str, object]] = None,
                efuses_unchanged: Optional[bool] = None, erased: Optional[bool] = None,
                end_crc_ok: Optional[bool] = None, recovery: Optional[str] = None,
                advice: Optional[Advice] = None, kind: Optional[str] = None) -> BoardTestReport:
        values = dict(measurements or {})
        values.setdefault("flash_id", board.flash_id)
        values.setdefault("chip_revision", board.chip_revision)
        keys = board.security in (SecurityState.KEY_BURNED_NOT_STARTED,
                                  SecurityState.SECURED_STATE_LIFE)
        report = BoardTestReport(
            mac=board.mac, verdict=verdict, kind=kind or self._kind(board), started_at=started,
            finished_at=self._now(), board=board, findings=tuple(findings),
            measurements=values,
            efuse_before=dict(run.efuse_before) if run else {},
            efuse_after=dict(run.efuse_after) if run else {},
            efuses_unchanged=efuses_unchanged, erased=erased, keys_burned=keys,
            console_log=run.console_log if run else None,
            tool_log=tuple(run.tool_log) if run else (),
            image_sha256=pinned_sha256(self._images), end_crc_ok=end_crc_ok,
            recovery=recovery, advice=advice)
        level = log.error if verdict is BoardTestVerdict.FAILED else log.info
        level("board test of %s: %s (%s)", board.mac, verdict.value,
              ", ".join(f"{item.code}:{item.result}" for item in findings) or "no findings")
        return report

    def _gate_report(self, board: BoardInfo, started: str) -> BoardTestReport:
        finding, advice = gate_finding(board)
        return self._report(board, BoardTestVerdict.FAILED, started, [finding], advice=advice,
                            measurements={"flash_id": board.flash_id,
                                          "psram_mb": board.psram_mb,
                                          "flash_size_mb": board.flash_size_mb})

    def _call(self, run: _Run, planned: TestCall, stage: str, *, cancellable: bool = True,
              on_line: Optional[Callable[[str], None]] = None) -> ToolResult:
        problems = call_problems(planned, self._verified if planned.purpose is
                                 Purpose.WRITE_IMAGES else ())
        if problems:  # never reached by the plan here; the checker guards every call
            log.error("board test call refused: %s", problems)
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "UNEXPECTED", stage, "warn", "; ".join(problems)), texts.UNEXPECTED)
        if cancellable and run.cancel.cancelled:
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "CANCELLED", stage, "info", "Cancelled before the next step."), texts.CANCELLED)
        result = self._runner.run(planned.call, cancel=run.cancel if cancellable else None,
                                  on_line=on_line)
        command = " ".join(planned.call.args)
        run.tool_log.append(redact(f"$ {planned.call.tool} {command}\n"
                                   f"# {planned.call.label}: exit {result.returncode}"
                                   f"{' (timed out)' if result.timed_out else ''}\n"
                                   f"{result.output}"))
        if not result.ok:
            advice = advice_for(result)
            verdict = BoardTestVerdict.INCOMPLETE
            raise _Abort(verdict, _finding(advice.code, stage, "warn", failure_cause(result),
                                           result.output.splitlines()[-4:],
                                           title=advice.title), advice)
        if planned.call.tool == ESPTOOL:
            self._check_banner(run, result, stage)
        return result

    _verified: Sequence[Tuple[TestImage, Path]] = ()

    def _check_banner(self, run: _Run, result: ToolResult, stage: str) -> None:
        banner = parse_banner(result.output)
        if banner.secure_download_mode or banner.mac != normalize_mac(run.board.mac):
            seen = "in Secure Download Mode" if banner.secure_download_mode else banner.mac
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "BOARD_SWAPPED", stage, "warn", f"esptool reached board {seen} instead of "
                f"{run.board.mac}.", [line for line in result.output.splitlines()
                                       if "MAC" in line or "Chip type" in line][:4]),
                texts.BOARD_SWAPPED)

    def _resolve_port(self, run: _Run, stage: str, wait_s: float = PORT_WAIT_S,
                      cancellable: bool = True) -> str:
        """This board's port, by its USB board ID, before every call (never an ID-less one)."""
        deadline = self._clock.monotonic() + wait_s
        while True:
            candidate, natives = self._usb.find(run.board.mac)
            if candidate is not None and normalize_mac(candidate.mac) == normalize_mac(
                    run.board.mac):
                run.port = candidate.port
                return candidate.port
            if natives > 1:
                raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                    "SEVERAL_BOARDS", stage, "warn", "More than one board is connected."),
                    texts.SEVERAL_BOARDS)
            if self._clock.monotonic() >= deadline:
                if candidate is not None and candidate.mac is None:
                    raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                        "BOARD_ID_NOT_READABLE", stage, "warn",
                        f"No USB board ID on {candidate.port}."), texts.BOARD_ID_NOT_READABLE)
                raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                    "BOARD_DISCONNECTED", stage, "warn", "The board is not on USB."),
                    texts.BOARD_DISCONNECTED)
            if self._clock.sleep(0.25, run.cancel if cancellable else None) and cancellable:
                raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                    "CANCELLED", stage, "info", "Cancelled."), texts.CANCELLED)

    def _check_identity(self, run: _Run, result: ToolResult, stage: str) -> None:
        banner = parse_banner(result.output)
        info = parse_security_info(result.output)
        problems = []
        if info is None:
            problems.append("no security report")
        else:
            if info.secure_boot:
                problems.append("Secure Boot is on")
            if info.flash_encryption:
                problems.append("flash encryption is on")
            if info.secure_download_mode or banner.secure_download_mode:
                problems.append("Secure Download Mode is on")
        if problems:
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "BOARD_TEST_NOT_POSSIBLE", stage, "warn",
                "The identity check refused the test: " + ", ".join(problems) + ".",
                result.output.splitlines()[-6:]), texts.BOARD_TEST_NOT_POSSIBLE)

    def _efuses(self, run: _Run, purpose: Purpose, stage: str, cancellable: bool
                ) -> Dict[str, object]:
        path = run.work / ("efuse-before.json" if purpose is Purpose.EFUSE_BEFORE
                           else "efuse-after.json")
        if path.exists():
            path.unlink()
        port = self._resolve_port(run, stage, cancellable=cancellable)
        self._call(run, efuse_call(purpose, port, path), stage, cancellable=cancellable)
        summary = read_summary_file(path)
        if not summary:
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "SECURITY_UNKNOWN", stage, "warn", "The eFuse summary could not be read."),
                texts.SECURITY_UNKNOWN)
        reported = efuse_mac(summary)
        if reported != normalize_mac(run.board.mac):
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "BOARD_SWAPPED", stage, "warn", f"The eFuse summary is for {reported}."),
                texts.BOARD_SWAPPED)
        return summary

    # -------------------------------------------------------- the test run

    def _run_test(self, board: BoardInfo, on_progress: Callable[[BoardTestProgress], None],
                  on_phase: PhaseFn, on_event: Callable[[ConsoleEvent], None],
                  cancel: CancelToken) -> BoardTestReport:
        started = self._now()
        if board.hardware not in (HardwareVerdict.OK, HardwareVerdict.NOT_CHECKED):
            # The gate refused it: nothing is written, the report still proves why.
            return self._gate_report(board, started)
        problems = board_problems(board)
        if problems:
            return self._report(board, BoardTestVerdict.INCOMPLETE, started, [_finding(
                "BOARD_TEST_NOT_POSSIBLE", "check", "warn",
                "The test program may not run on this board: " + ", ".join(problems) + ".")],
                advice=texts.BOARD_TEST_NOT_POSSIBLE)
        try:
            verified = verified_images(self._image_dir or Path("."), self._images) \
                if self._image_dir is not None else None
            if verified is None:
                raise ImageError("no board test image folder")
        except ImageError as error:
            log.error("board test images refused: %s", error)
            return self._report(board, BoardTestVerdict.INCOMPLETE, started, [_finding(
                "BOARD_TEST_IMAGES_DAMAGED", "check", "warn", str(error))],
                advice=texts.BOARD_TEST_IMAGES_DAMAGED)
        if self._temp_root is not None:
            self._temp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="sl-boardtest-",
                                     dir=str(self._temp_root) if self._temp_root else None))
        run = _Run(board, on_progress, cancel, work)
        self._verified = verified
        plan = build_plan(board.port, verified, work)
        unsafe = safety_problems(board, plan, verified)
        if unsafe:  # a bug here, never a board fault: refuse loudly
            log.error("board test plan refused: %s", unsafe)
            shutil.rmtree(work, ignore_errors=True)
            return self._report(board, BoardTestVerdict.INCOMPLETE, started, [_finding(
                "UNEXPECTED", "check", "warn", "; ".join(unsafe))], advice=texts.UNEXPECTED)
        judgement: Optional[Judgement] = None
        stop: Optional[_Abort] = None
        record: Optional[ConsoleLog] = None
        try:
            try:
                self._progress(run, "check", 0.0, "Checking the board")
                port = self._resolve_port(run, "check")
                identity = self._call(run, identity_call(port), "check")
                self._check_identity(run, identity, "check")
                run.efuse_before = self._efuses(run, Purpose.EFUSE_BEFORE, "check", True)
                state, _note = classify_efuses(run.efuse_before)
                if state is not board.security:
                    raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                        "BOARD_TEST_NOT_POSSIBLE", "check", "warn",
                        f"The eFuse summary says {state.value}, the board check said "
                        f"{board.security.value}."), texts.BOARD_TEST_NOT_POSSIBLE)
                self._progress(run, "check", 1.0, "Checking the board")
                record = self._watcher.console_log(
                    board.mac, f"boardtest-{self._clock.now_utc():%Y%m%d-%H%M%S}")
                if record is not None:
                    record.begin("Board hardware test")
                    run.console_log = str(record.path)
                self._write(run)
                judgement = self._start_and_watch(run, on_phase, on_event, record)
            except _Abort as abort:
                stop = abort
            except Exception as error:  # every failure must end in a report, after cleanup
                log.exception("unexpected board test failure")
                stop = _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                    "UNEXPECTED", "check", "warn", repr(error)[:300]), texts.UNEXPECTED)
            # Cleanup always runs once something may be on the board, cancel or not.
            erased: Optional[bool] = None
            unchanged: Optional[bool] = None
            if run.written:
                erased, unchanged = self._cleanup(run, judgement)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return self._finish(run, started, judgement, stop, erased, unchanged, record)

    def _write(self, run: _Run) -> None:
        port = self._resolve_port(run, "write")
        planned = write_call(port, self._verified)
        total = planned.total_bytes or 1
        starts = sorted((image.offset, image.size or 0) for image, _path in self._verified)
        last = [-1.0]

        def line(text: str) -> None:
            found = PROGRESS_LINE.search(text)
            if not found:
                return
            address = int(found.group(1), 16)
            done = 0
            for offset, size in starts:
                if address >= offset + size:
                    done += size
                elif address >= offset:
                    done += address - offset
            fraction = done / total
            if fraction - last[0] >= 0.05:
                last[0] = fraction
                self._progress(run, "write", fraction, "Copying the test program")

        self._progress(run, "write", 0.0, "Copying the test program")
        run.written = True  # from here on, a cancel or failure leaves something to erase
        self._call(run, planned, "write", on_line=line)
        self._progress(run, "write", 1.0, "Copying the test program")

    def _start(self, run: _Run, record: Optional[ConsoleLog]) -> None:
        if run.starts >= MAX_STARTS:  # never reached by the flow below; a guard only
            raise _Abort(BoardTestVerdict.INCOMPLETE, _finding(
                "UNEXPECTED", "start", "warn", f"more than {MAX_STARTS} starts refused"),
                texts.UNEXPECTED)
        port = self._resolve_port(run, "start")
        run.started = True  # even a failed start call may have reset the chip
        run.starts += 1
        self._call(run, start_call(port), "start")
        # With the ConsoleLog's times, this note, "the port ... is open" and the BEGIN line
        # show how long this computer takes to give the port back after the reset.
        _log_note(record, f"start call {run.starts} finished")

    def _watch_started(self, run: _Run, on_event: Callable[[ConsoleEvent], None],
                       record: Optional[ConsoleLog]) -> Judgement:
        """Watch the program just started. When its lines arrive without their BEGIN (the
        port came back too late after the reset), the END crc can never be proven: stop at
        once and start it once more (esptool resets it into download mode first) instead
        of watching a run that can only end USB_DATA_GARBLED."""
        judgement = self._watch(run, on_event, record,
                                begin_missed_stops=run.starts < MAX_STARTS)
        if judgement.code != BEGIN_MISSED or run.cancel.cancelled:
            return judgement
        _log_note(record, "the BEGIN line was missed: starting the test program again")
        if judgement.measurements.get("first_stage") not in ("psram", "iram"):
            # The stopped run may have reached its flash stage: the next run's CLEAN only
            # covers the blocks that run touches, so the cleanup erases the whole chip.
            run.erase_all = True
        run.findings.append(_finding(
            BEGIN_MISSED, "start", "info",
            "The test program's first line (BEGIN) was missed because the USB port came back "
            "late after the start; the program was started once more.",
            judgement.findings[0].proof if judgement.findings else (),
            title="The test was started again"))
        self._progress(run, "start", 0.0, "Starting the test again")
        self._start(run, record)
        return self._watch(run, on_event, record, begin_missed_stops=False)

    def _start_and_watch(self, run: _Run, on_phase: PhaseFn,
                         on_event: Callable[[ConsoleEvent], None],
                         record: Optional[ConsoleLog]) -> Judgement:
        self._progress(run, "start", 0.0, "Starting the test")
        self._start(run, record)
        judgement = self._watch_started(run, on_event, record)
        if judgement.code == "NO_START" and not run.cancel.cancelled:
            # Some computers leave a board in download mode after a reset over USB; a power
            # cycle clears that (new-device-firmware-path, 29 Oct 2026). Once.
            on_phase(ReplugPhase.WAITING_UNPLUG)
            if not self._watcher.wait_presence(run.board.mac, present=False,
                                               timeout=UNPLUG_TIMEOUT_S, cancel=run.cancel):
                return self._early(run, judgement)
            _log_note(record, "the board was unplugged")
            on_phase(ReplugPhase.WAITING_REPLUG)
            if not self._watcher.wait_presence(run.board.mac, present=True,
                                               timeout=REPLUG_TIMEOUT_S, cancel=run.cancel):
                return self._early(run, judgement)
            _log_note(record, "the board was plugged in again")
            on_phase(ReplugPhase.WATCHING)
            second = self._watch_started(run, on_event, record)
            if second.code == "NO_START":
                return replace(second, findings=judgement.findings + second.findings)
            return second
        return judgement

    def _early(self, run: _Run, judgement: Judgement) -> Judgement:
        if run.cancel.cancelled:
            return replace(judgement, code="CANCELLED", findings=judgement.findings + (
                _finding("CANCELLED", "start", "info", "Cancelled while waiting for the "
                         "replug."),), advice=texts.CANCELLED)
        return replace(judgement, code="NOT_REPLUGGED", findings=judgement.findings + (
            _finding("NOT_REPLUGGED", "start", "warn", "The board was not unplugged and "
                     "plugged in again.", title=texts.NOT_REPLUGGED.title),),
            advice=texts.NOT_REPLUGGED)

    def _watch(self, run: _Run, on_event: Callable[[ConsoleEvent], None],
               record: Optional[ConsoleLog], *, begin_missed_stops: bool = False
               ) -> Judgement:
        watch = ConsoleWatch(started=self._clock.monotonic(), events=[], lines=[])
        judge = TestJudge(run.board.mac)
        decided: List[Judgement] = []
        missed: List[bool] = []
        stage_seen = [""]

        def listen(event: ConsoleEvent) -> None:
            if event.kind is ConsoleKind.SELF_TEST:
                self._test_progress(run, event, stage_seen)
            try:
                on_event(event)
            except Exception as error:  # a screen problem must not stop the test
                log.warning("board test console listener failed: %s", error)

        def check(_event: Optional[ConsoleEvent]) -> bool:
            now = self._clock.monotonic()
            if begin_missed_stops and judge.undecided and _begin_missed(watch.events):
                missed.append(True)
                return True
            verdict = judge.check(watch, now)
            if verdict is not None:
                decided.append(verdict)
                return True
            # Past WATCH_S only while the program still shows progress (up to WATCH_MAX_S):
            # a slow but good flash chip must not end INCOMPLETE; a hang is still caught by
            # the judge's silence rule.
            return now - watch.started >= WATCH_S and not _progressing(watch, now)

        self._watcher.read(run.board.mac, watch, listen, run.cancel,
                           deadline=watch.started + WATCH_MAX_S, stop=check, record=record)
        if decided:
            return decided[0]
        if missed:
            first = next(event for event in watch.events
                         if event.kind is ConsoleKind.SELF_TEST)
            stage = first.data.get("stage") if first.data.get("sltest") == "ALIVE" else None
            return Judgement(BoardTestVerdict.INCOMPLETE, BEGIN_MISSED, (_finding(
                BEGIN_MISSED, "start", "info", "The test program's lines arrived without "
                "their BEGIN line.", [first.raw], title="The test was started again"),),
                {"first_stage": stage}, advice=texts.USB_DATA_GARBLED)
        now = self._clock.monotonic()
        final = judge.check(watch, now, final=True)
        assert final is not None
        if run.cancel.cancelled and not final.program_done and not judge.decisive(
                watch, now, final):
            return replace(final, verdict=BoardTestVerdict.INCOMPLETE, code="CANCELLED",
                           findings=final.findings + (_finding(
                               "CANCELLED", "check", "info", "Cancelled during the test."),),
                           advice=texts.CANCELLED)
        return final

    def _test_progress(self, run: _Run, event: ConsoleEvent, stage_seen: List[str]) -> None:
        what = event.data.get("sltest")
        stage = event.data.get("stage", "")
        if what == "BEGIN":
            self._progress(run, "start", 1.0, "The test has started")
        elif what == "ALIVE" and stage in STAGES:
            step, of = _int(event.data.get("step")), _int(event.data.get("of"))
            within = (step / of) if step is not None and of else None
            detail = _STAGE_WORDS[stage] + (f" (part {step} of {of})" if within is not None
                                            else "")
            if stage != stage_seen[0] or within is not None:
                stage_seen[0] = stage
                self._progress(run, stage, within if within is not None else 0.0, detail)
        elif what == "RESULT" and stage in STAGES:
            self._progress(run, stage, 1.0, _STAGE_WORDS[stage])
        elif what == "END":
            self._progress(run, "cleanup", 0.0, "Removing the test program")

    # ----------------------------------------------------------- cleanup

    def _cleanup(self, run: _Run, judgement: Optional[Judgement]
                 ) -> Tuple[Optional[bool], Optional[bool]]:
        """Erase what the test wrote and read the eFuses again (never cancelled)."""
        self._progress(run, "cleanup", 0.1, "Removing the test program")
        # Without its CLEAN line the program may have left test blocks anywhere in its
        # data partition; before it started it wrote nothing there.
        clean_ok = (bool(judgement and judgement.clean_ok) or not run.started) \
            and not run.erase_all
        erased = False
        if judgement is not None and judgement.program_done:
            # After END and CLEAN the program forces download mode and restarts: let it go
            # off USB first, or esptool would catch it halfway.
            self._watcher.wait_presence(run.board.mac, present=False, timeout=AFTER_END_S,
                                        cancel=CancelToken())
        try:
            port = self._resolve_port(run, "cleanup", wait_s=PORT_WAIT_S, cancellable=False)
            identity = self._call(run, identity_call(port), "cleanup", cancellable=False)
            self._check_identity(run, identity, "cleanup")
            area = self._attempt(run, erase_area_call, "cleanup")
            self._progress(run, "cleanup", 0.5, "Removing the test program")
            erased = area
            if area and not clean_ok:
                # The program did not say it erased its own test blocks: the whole chip.
                erased = self._attempt(run, erase_chip_call, "cleanup")
            self._progress(run, "cleanup", 0.8, "Checking the board")
            run.efuse_after = self._efuses(run, Purpose.EFUSE_AFTER, "cleanup", False)
        except _Abort as abort:
            log.error("board test cleanup of %s stopped: %s (%s)", run.board.mac,
                      abort.finding.code, abort.finding.detail)
            run.findings.append(replace(abort.finding, stage="cleanup"))
        unchanged: Optional[bool] = None
        if run.efuse_before and run.efuse_after:
            changed = efuse_differences(run.efuse_before, run.efuse_after)
            unchanged = not changed
            if changed:
                log.critical("EFUSES CHANGED during the board test of %s: %s", run.board.mac,
                             ", ".join(changed))
                run.findings.append(_finding(
                    "EFUSE_CHANGED", "cleanup", "fail",
                    f"eFuse summary after the test differs from before in: "
                    f"{', '.join(changed[:20])}.", [f"changed: {name}" for name in changed]))
            else:
                run.findings.append(_finding(
                    "EFUSES_UNCHANGED", "cleanup", "ok",
                    f"All {len(run.efuse_before)} eFuse summary fields are the same before and "
                    f"after the test.", title="The board's permanent settings did not change"))
        if erased:
            run.findings.append(_finding(
                "ERASED", "cleanup", "ok",
                "Test program erased (0x0-0x110000" + ("" if clean_ok else
                                                       ", then the whole flash chip") + ").",
                title="The test program was removed"))
        else:
            run.findings.append(_finding(
                "ERASE_NOT_FINISHED", "cleanup", "warn",
                "The test program could not be erased: the board did not answer in download "
                "mode."))
        self._progress(run, "cleanup", 1.0, "Removing the test program")
        return erased, unchanged

    def _attempt(self, run: _Run, build: Callable[[str], TestCall], stage: str) -> bool:
        """One erase call, tried twice: the board can still be re-enumerating."""
        for attempt in (1, 2):
            port = self._resolve_port(run, stage, cancellable=False)
            try:
                self._call(run, build(port), stage, cancellable=False)
                return True
            except _Abort as abort:
                if attempt == 2 or abort.finding.code == "BOARD_SWAPPED":
                    raise
                self._clock.sleep(1.0)
        return False

    def _finish(self, run: _Run, started: str, judgement: Optional[Judgement],
                stop: Optional[_Abort], erased: Optional[bool], unchanged: Optional[bool],
                record: Optional[ConsoleLog]) -> BoardTestReport:
        board = run.board
        findings: List[TestFinding] = []
        measurements: Dict[str, object] = {}
        advice: Optional[Advice] = None
        crc: Optional[bool] = None
        if judgement is not None:
            verdict = judgement.verdict
            findings += judgement.findings
            measurements.update(judgement.measurements)
            advice = judgement.advice
            crc = judgement.end_crc_ok
            main = judgement.code
        else:
            assert stop is not None
            verdict = stop.verdict
            findings.append(stop.finding)
            advice = stop.advice
            main = stop.finding.code
        findings += run.findings
        if unchanged is False:
            # Should never happen: loud, and never a pass.
            if verdict is not BoardTestVerdict.FAILED:
                verdict = BoardTestVerdict.INCOMPLETE
            advice, main = texts.EFUSE_CHANGED, "EFUSE_CHANGED"
        if run.written and not erased:
            if verdict is BoardTestVerdict.PASSED:
                verdict = BoardTestVerdict.INCOMPLETE
            if main != "EFUSE_CHANGED":
                advice, main = texts.ERASE_NOT_FINISHED, "ERASE_NOT_FINISHED"
        if verdict is BoardTestVerdict.PASSED and unchanged is not True:
            verdict, advice = BoardTestVerdict.INCOMPLETE, texts.SECURITY_UNKNOWN
        recovery = None
        if verdict is BoardTestVerdict.PASSED:
            advice = texts.BOARD_TEST_PASSED
            if board.security is SecurityState.KEY_BURNED_NOT_STARTED:
                recovery = RECOVERY_RESUME
        if record is not None:
            record.end_text(f"{verdict.value} ({main})")
        return self._report(board, verdict, started, findings, run=run,
                            measurements=measurements, efuses_unchanged=unchanged,
                            erased=erased if run.written else None,
                            end_crc_ok=crc, recovery=recovery,
                            advice=advice or texts.board_test_advice(main))

    # ------------------------------------------------------- a locked board

    def _check_locked(self, board: BoardInfo, on_phase: PhaseFn,
                      on_event: Callable[[ConsoleEvent], None], cancel: CancelToken,
                      seconds: float) -> BoardTestReport:
        started = self._now()
        mac = normalize_mac(board.mac) or board.mac
        record = self._watcher.console_log(mac, f"failedboard-{self._clock.now_utc():%Y%m%d%H%M%S}")
        if record is not None:
            record.begin("Check a failed board")
        log_path = str(record.path) if record is not None else None
        judge = TestJudge(mac, locked=True, placeholders=self._placeholders, board=board)
        on_phase(ReplugPhase.WAITING_UNPLUG)
        replugged = self._watcher.wait_presence(mac, present=False, timeout=UNPLUG_TIMEOUT_S,
                                                cancel=cancel)
        if replugged:
            if record is not None:
                record.note("the board was unplugged")
            on_phase(ReplugPhase.WAITING_REPLUG)
            replugged = self._watcher.wait_presence(mac, present=True, timeout=REPLUG_TIMEOUT_S,
                                                    cancel=cancel)
        if not replugged:
            code, advice = (("CANCELLED", texts.CANCELLED) if cancel.cancelled
                            else ("NOT_REPLUGGED", texts.NOT_REPLUGGED))
            if record is not None:
                record.end_text(f"incomplete ({code})")
            return replace(self._report(board, BoardTestVerdict.INCOMPLETE, started, [_finding(
                code, "check", "info", "The board was not unplugged and plugged in again.",
                title=advice.title)], advice=advice, kind=KIND_LOCKED), console_log=log_path)
        if record is not None:
            record.note("the board was plugged in again")
        on_phase(ReplugPhase.WATCHING)
        watch = ConsoleWatch(started=self._clock.monotonic(), events=[], lines=[])
        decided: List[Judgement] = []

        def check(_event: Optional[ConsoleEvent]) -> bool:
            verdict = judge.check(watch, self._clock.monotonic())
            if verdict is not None:
                decided.append(verdict)
            return verdict is not None

        self._watcher.read(mac, watch, on_event, cancel, deadline=watch.started + seconds,
                           stop=check, record=record)
        if decided:
            judgement = decided[0]
        elif cancel.cancelled:
            judgement = Judgement(BoardTestVerdict.INCOMPLETE, "CANCELLED", (_finding(
                "CANCELLED", "check", "info", "Cancelled while the start was watched."),),
                measurements_from(watch.events), advice=texts.CANCELLED)
        else:
            final = judge.check(watch, self._clock.monotonic(), final=True)
            assert final is not None
            judgement = final
        measurements = dict(judgement.measurements)
        for event in watch.events:
            if event.kind is ConsoleKind.PSRAM_OK and event.data.get("size_mb"):
                measurements["psram_size_mb_reported"] = _number(event.data["size_mb"])
            if event.kind is ConsoleKind.APP_VERSION and event.data.get("version"):
                measurements["app_version"] = event.data["version"]
        period = _period(watch.reset_times)
        if period is not None:
            measurements["restart_period_s"] = round(period, 1)
        measurements["restarts"] = restarts_seen(watch.events, watch.resets)
        if record is not None:
            record.end_text(f"{judgement.verdict.value} ({judgement.code})")
        report = self._report(board, judgement.verdict, started, judgement.findings,
                              measurements=measurements, recovery=judgement.recovery,
                              advice=judgement.advice, kind=KIND_LOCKED)
        return replace(report, console_log=log_path)


def efuse_differences(before: Dict[str, object], after: Dict[str, object]) -> List[str]:
    """The eFuse summary fields whose content differs (all values; [] when identical)."""
    names = sorted(set(before) | set(after))
    return [name for name in names if before.get(name) != after.get(name)]


def report_lines(report: BoardTestReport) -> List[str]:
    """A board test report for a terminal (bench.py)."""
    lines = [f"Board test of {report.mac}: {report.verdict.value.upper()} ({report.kind})",
             f"  eFuses unchanged: {report.efuses_unchanged}   test program erased: "
             f"{report.erased}   END crc ok: {report.end_crc_ok}"]
    for item in report.findings:
        lines.append(f"  [{item.result:4}] {item.stage:8} {item.code}: {item.title}")
        lines.append(f"         {item.detail}")
    if report.measurements:
        lines.append("  Measured: " + ", ".join(f"{key}={value}" for key, value in
                                                 sorted(report.measurements.items())
                                                 if value is not None))
    if report.recovery:
        lines.append(f"  Recovery: {report.recovery}")
    return lines


__all__ = ["BoardTester", "TestJudge", "gate_finding", "Judgement", "Purpose", "TestCall",
           "build_plan",
           "board_problems", "never_started", "call_problems", "safety_problems", "board_test_problem",
           "check_board_test", "efuse_differences", "report_lines", "measurements_from",
           "KIND_VENDOR", "KIND_LOCKED", "RECOVERY_RESUME", "RECOVERY_FIX_SETTINGS", "STAGES"]
