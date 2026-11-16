"""Board test certificates: what a board test proved, on paper.

Three kinds, all built from one ``BoardTestReport`` (api.py):

* PASSED - "Board test certificate - PASSED (fit for State Life use)": every test
  stage passed, the eFuses read the same after the test as before it and the test
  program was erased again.
* VENDOR_RETURN - "Hardware fault certificate - NOT FIT FOR USE": the proof a vendor
  needs to take a faulty board back. A locked board (one our setup already gave its
  security keys) says so honestly, and that the keys do not cause the fault.
* FAULT_REPORT - the internal "Board fault report" for any report, also one that
  could not finish or a board that can still be finished or fixed.

``build_certificate`` turns a report into a plain ``CertificateData`` (no Qt, easy to
test), ``certificate_text`` gives the plain-text version (it goes into the evidence
ZIP) and ``render_pdf`` draws the A4 PDF with Qt's QTextDocument + QPdfWriter (QtGui
only: PySide6-Essentials ships neither QtPdf nor QtPrintSupport in the app). Qt is
imported inside ``render_pdf`` so the engine stays Qt-free. Rendering needs no event
loop, uses QImage (never QPixmap) and works on a worker thread and under
QT_QPA_PLATFORM=offscreen.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from .api import SecurityState
from .board import classify_efuses, flash_encryption_enabled, key_block_unused
from .logging_setup import redact
from .naming import normalize_mac

LOGO_FILE = (Path(__file__).resolve().parent.parent / "resources" / "pictures"
             / "state-life-logo.png")
PROOF_LINES_MAX = 40
PROOF_LINE_CHARS = 180
STAGE_ORDER = ("check", "startup", "psram", "iram", "flash", "cpu", "wifi", "cleanup")
TEST_STAGES = ("psram", "iram", "flash", "cpu", "wifi")


class CertificateKind(str, Enum):
    PASSED = "passed"
    VENDOR_RETURN = "vendor_return"
    FAULT_REPORT = "fault_report"


TITLES = {
    CertificateKind.PASSED: "Board test certificate — PASSED (fit for State Life use)",
    CertificateKind.VENDOR_RETURN: "Hardware fault certificate — NOT FIT FOR USE",
    CertificateKind.FAULT_REPORT: "Board fault report",
}
# The last word of a record's file name (records.save_board_test).
FILE_WORDS = {
    CertificateKind.PASSED: "passed",
    CertificateKind.VENDOR_RETURN: "fault",
    CertificateKind.FAULT_REPORT: "report",
}

# Finding codes that prove a fault in the board's own parts (memory, storage chip,
# processor, radio, start-up), as opposed to cables, settings, the network or a run
# that could not finish.
HARDWARE_CODES = frozenset({
    "PSRAM_NOT_FOUND", "PSRAM_WRONG_SIZE", "PSRAM_TOO_SMALL", "PSRAM_DATA_ERRORS",
    "PSRAM_CRASH", "PSRAM_HANG", "PSRAM_STARTUP_FAULT", "PSRAM_STARTUP_HANG", "PSRAM_FAILED",
    "IRAM_DATA_ERRORS", "IRAM_HANG", "IRAM_CRASH",
    "FLASH_WRONG_SIZE", "FLASH_READ_UNSTABLE", "FLASH_WRITE_ERRORS", "FLASH_HANG",
    "FLASH_CRASH", "FLASH_CHIP_UNSUPPORTED", "FLASH_ID_CHANGED",
    "CPU_MISMATCH", "CPU_HANG", "CPU_CRASH", "WIFI_RADIO_FAILED", "WIFI_HANG", "WIFI_CRASH",
    "TEST_CRASH", "STARTUP_CRASH", "STARTUP_HANG", "BOOTLOADER_FAILED",
    "WRONG_CHIP", "WRONG_FLASH_SIZE", "WRONG_PSRAM", "BOARD_TYPE_UNSUPPORTED",
    "FAULTY_RESTART_LOOP", "CRASH_LOOP",
})
_HARDWARE_SUFFIXES = ("_DATA_ERRORS", "_HANG", "_CRASH")

STAGE_WORDS = {
    "check": "Hardware check",
    "startup": "Start-up",
    "psram": "Extra memory (PSRAM)",
    "iram": "Internal memory",
    "flash": "Storage chip (flash)",
    "cpu": "Processor cores",
    "wifi": "Wi-Fi radio",
    "cleanup": "Clean-up",
}
RESULT_WORDS = {"ok": "PASS", "fail": "FAIL", "warn": "NOTE", "info": "INFO"}
_SECURITY_WORDS = {
    SecurityState.BLANK: "Blank (no keys, Secure Boot off)",
    SecurityState.KEY_BURNED_NOT_STARTED: "State Life key burned, setup not finished",
    SecurityState.SECURED_STATE_LIFE: "Secured by State Life",
    SecurityState.SECURED_OTHER: "Secured by someone else",
    SecurityState.UNSECURED_USED: "Not blank (keys in use)",
    SecurityState.UNKNOWN: "Not known",
}
_FLASH_MAKERS = {
    "c8": "GigaDevice", "ef": "Winbond", "0b": "XTX", "c2": "Macronix",
    "68": "Boya", "5e": "Zbit", "a1": "Fudan", "85": "Puya", "9d": "ISSI",
}
_IMAGE_ORDER = {"bootloader.bin": 0, "partition-table.bin": 1, "board-test.bin": 2}
_LOG_ENTRY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s{2}(.*)$")

# Colours: State Life blue and the three verdicts (text, background, border).
BRAND = "#0B8FD6"
INK = "#1F2933"
MUTED = "#5F6B7A"
VERDICT_COLOURS = {
    "passed": ("#14532D", "#E7F6EC", "#1E8E3E"),
    "failed": ("#8C1D18", "#FDECEA", "#C5221F"),
    "incomplete": ("#7A4B00", "#FFF4DE", "#E0A100"),
}


@dataclass(frozen=True)
class TestRow:
    stage: str      # person words ("Extra memory (PSRAM)")
    result: str     # PASS / FAIL / NOTE / INFO / "-"
    code: str
    detail: str


@dataclass(frozen=True)
class ProofLine:
    at: str         # "+12.3 s" from the start of the watched run, or "" when not known
    text: str


@dataclass(frozen=True)
class CertificateData:
    kind: CertificateKind
    title: str
    number: str
    issued: str                       # "2026-11-16 14:03:22 PKT (UTC+05:00)"
    tested_by: str
    app_version: str
    test_program: str                 # "1.0.0" or "not used (...)"
    image_sha256: Tuple[Tuple[str, str], ...]
    test_run: str                     # "started ... - finished ..."
    verdict: str                      # passed / failed / incomplete
    verdict_label: str                # the big words in the result box
    reason: str                       # plain words
    technical: str                    # one technical sentence for the vendor
    board: Tuple[Tuple[str, str], ...]
    security: Tuple[Tuple[str, str], ...]
    keys_statement: str               # "" unless a locked board's vendor claim
    tests: Tuple[TestRow, ...]
    measurements: Tuple[Tuple[str, str], ...]
    next_steps: Tuple[str, ...]       # fault report only: what to do now
    proof: Tuple[ProofLine, ...]
    evidence_name: str
    evidence_sha256: str
    footer: str

    @property
    def colours(self) -> Tuple[str, str, str]:
        return VERDICT_COLOURS.get(self.verdict, VERDICT_COLOURS["incomplete"])


# ------------------------------------------------------------------ building

def is_hardware_code(code: str) -> bool:
    code = (code or "").upper()
    return code in HARDWARE_CODES or code.endswith(_HARDWARE_SUFFIXES)


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


def default_kind(report: object) -> CertificateKind:
    verdict = _value(getattr(report, "verdict", ""))
    if verdict == "passed":
        return CertificateKind.PASSED
    if verdict == "failed":
        return CertificateKind.VENDOR_RETURN
    return CertificateKind.FAULT_REPORT


def certificate_number(moment: datetime, mac: str) -> str:
    digits = (normalize_mac(mac) or mac or "").replace(":", "").lower()
    tail = (digits[-6:] or "000000").rjust(6, "0")
    return f"SL-BT-{moment.strftime('%Y%m%d')}-{moment.strftime('%H%M%S')}-{tail}"


def _local(moment: datetime) -> datetime:
    return moment.astimezone() if moment.tzinfo is None else moment


def format_moment(moment: datetime) -> str:
    moment = _local(moment)
    offset = moment.strftime("%z")
    zone = moment.tzname() or ""
    utc = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    if zone and zone != utc and not zone.startswith(("UTC", "GMT+", "GMT-", "+", "-")):
        return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} {zone} ({utc})"
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} ({utc})"


def _iso_words(text: str) -> str:
    if not text:
        return "not known"
    try:
        return format_moment(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return text


def main_finding(report: object) -> Optional[object]:
    """The finding the verdict rests on: a hardware fail first, then any fail, then a note."""
    findings = list(getattr(report, "findings", ()) or ())
    fails = [f for f in findings if _value(getattr(f, "result", "")) == "fail"]
    for finding in fails:
        if is_hardware_code(getattr(finding, "code", "")):
            return finding
    if fails:
        return fails[0]
    for finding in findings:
        if _value(getattr(finding, "result", "")) == "warn":
            return finding
    return None


_SDM_UNREADABLE = "not readable (secure download mode)"


def _unknown(board: object, words: str) -> str:
    return _SDM_UNREADABLE if getattr(board, "secure_download_mode", False) else words


def _revision_words(board: object, measurements: Mapping[str, object]) -> str:
    revision = getattr(board, "chip_revision", None)
    if isinstance(revision, int):
        return f"v{revision // 100}.{revision % 100}"
    return str(measurements.get("chip_rev") or _unknown(board, "not known"))


def _jedec(text: str) -> str:
    """"204018" or "20 40 18" as "20 40 18"."""
    digits = text.replace(" ", "").upper()
    if len(digits) == 6:
        return " ".join(digits[i:i + 2] for i in range(0, 6, 2))
    return text.strip().upper()


def _flash_words(board: object, measurements: Mapping[str, object]) -> str:
    jedec = getattr(board, "flash_id", None) or measurements.get("flash_id")
    size = getattr(board, "flash_size_mb", None)
    if not size and isinstance(measurements.get("flash_size"), int):
        size = int(measurements["flash_size"]) // (1024 * 1024)
    parts = []
    if jedec:
        text = str(jedec).strip()
        digits = text.replace(" ", "").lower()
        if " " not in text and len(digits) == 6:
            text = " ".join(digits[i:i + 2] for i in range(0, 6, 2)).upper()
        maker = _FLASH_MAKERS.get(digits[:2])
        parts.append(f"JEDEC ID {text.upper()}" + (f" ({maker})" if maker else ""))
    elif getattr(board, "secure_download_mode", False) and not size:
        return _SDM_UNREADABLE[:1].upper() + _SDM_UNREADABLE[1:]
    else:
        parts.append("JEDEC ID not read")
    parts.append(f"{size} MB" if size else "size not read")
    return ", ".join(parts)


def _psram_words(board: object, measurements: Mapping[str, object]) -> str:
    evidence = getattr(board, "evidence", {}) or {}
    features = str(evidence.get("features") or "")
    reported = ""
    match = re.search(r"Embedded PSRAM[^,]*", features)
    if match:
        reported = match.group(0).strip()
    elif getattr(board, "psram_mb", None):
        reported = f"{board.psram_mb} MB"  # type: ignore[attr-defined]
    measured = measurements.get("psram_size")
    words = (f"reported: {reported}" if reported else
             f"reported: {_unknown(board, 'not given')}")
    if isinstance(measured, int):
        words += (f"; found by the test program: {measured / (1024 * 1024):g} MB" if measured
                  else "; found by the test program: none")
    return words


def _board_rows(report: object) -> Tuple[Tuple[str, str], ...]:
    board = getattr(report, "board", None)
    measurements = dict(getattr(report, "measurements", {}) or {})
    evidence = dict(getattr(board, "evidence", {}) or {})
    chip = getattr(board, "chip", None) or "ESP32-S3"
    rows = [
        ("Board ID (MAC)", getattr(report, "mac", "") or getattr(board, "mac", "")),
        ("Chip", f"{chip}, revision {_revision_words(board, measurements)}"),
        ("Flash chip", _flash_words(board, measurements)),
        ("PSRAM", _psram_words(board, measurements)),
        ("USB serial", str(evidence.get("usb_mac") or "not given")),
    ]
    if evidence.get("chip_line"):
        rows.append(("Chip as read", str(evidence["chip_line"])))
    if getattr(board, "hardware_detail", ""):
        rows.append(("Hardware check", str(board.hardware_detail)))  # type: ignore[attr-defined]
    return tuple((label, str(value)) for label, value in rows)


_EFUSE_STATE_WORDS = {
    SecurityState.BLANK: "Blank",
    SecurityState.KEY_BURNED_NOT_STARTED: "State Life key burned",
    SecurityState.SECURED_STATE_LIFE: "Secured by State Life",
    SecurityState.SECURED_OTHER: "Secured by someone else",
    SecurityState.UNSECURED_USED: "Keys in use",
    SecurityState.UNKNOWN: "State not known",
}


def _efuse_words(summary: Mapping[str, object], sdm: bool) -> str:
    if not summary:
        return "Not readable (secure download mode)" if sdm else "Not read"
    state, _note = classify_efuses(dict(summary))
    secure_boot = (summary.get("SECURE_BOOT_EN") or {}).get("value") is True \
        if isinstance(summary.get("SECURE_BOOT_EN"), dict) else False
    encryption = flash_encryption_enabled(dict(summary))
    used = sum(1 for index in range(6) if not key_block_unused(dict(summary), index))
    return (f"{_EFUSE_STATE_WORDS.get(state, state.value)}; Secure Boot "
            f"{'on' if secure_boot else 'off'}; flash encryption "
            f"{'on' if encryption else 'off'}; key blocks in use: {used} of 6 "
            f"({len(summary)} fields read)")


def efuse_differences(before: Mapping[str, object], after: Mapping[str, object]) -> List[str]:
    """Names of the eFuse fields whose value differs between the two summaries."""
    names = sorted(set(before) | set(after))
    changed = []
    for name in names:
        one, two = before.get(name), after.get(name)
        if isinstance(one, dict) and isinstance(two, dict):
            if one.get("value") != two.get("value"):
                changed.append(name)
        elif one != two:
            changed.append(name)
    return changed


def _security_rows(report: object) -> Tuple[Tuple[str, str], ...]:
    board = getattr(report, "board", None)
    sdm = bool(getattr(board, "secure_download_mode", False))
    before = dict(getattr(report, "efuse_before", {}) or {})
    after = dict(getattr(report, "efuse_after", {}) or {})
    unchanged = getattr(report, "efuses_unchanged", None)
    if unchanged is True:
        same = "Yes — every eFuse value read after the test equals the value read before"
    elif unchanged is False:
        diff = efuse_differences(before, after)
        same = "NO — eFuses CHANGED during the test" + (
            f" ({', '.join(diff[:8])})" if diff else "")
    elif sdm:
        same = "Not readable (secure download mode)"
    else:
        same = "Not checked"
    erased = getattr(report, "erased", None)
    if erased is True:
        removed = "Yes — the test program was erased from the board"
    elif erased is False:
        removed = "NO — the test program could not be erased (press Test again to finish)"
    else:
        removed = "Not applicable (no test program was written)"
    keys = getattr(report, "keys_burned", False)
    if keys:
        key_words = "Burned before this test, during an earlier State Life setup"
    elif unchanged is True:
        key_words = "Not burned — the test burned no key and changed no eFuse"
    else:
        key_words = "Not burned"
    rows = [
        ("Security at the check", _SECURITY_WORDS.get(getattr(board, "security", None),
                                                      "Not known")),
        ("Secure download mode", "Yes" if sdm else "No"),
    ]
    if before or after or not sdm:
        rows += [("eFuses before", _efuse_words(before, sdm)),
                 ("eFuses after", _efuse_words(after, sdm)),
                 ("eFuses unchanged", same)]
    else:
        rows.append(("eFuses", "Not readable (secure download mode); nothing was written to "
                               "the board"))
    rows += [("Test program removed", removed), ("State Life security keys", key_words)]
    return tuple(rows)


def _test_rows(report: object) -> Tuple[TestRow, ...]:
    findings = list(getattr(report, "findings", ()) or ())
    order = {stage: index for index, stage in enumerate(STAGE_ORDER)}
    findings.sort(key=lambda f: order.get(_value(getattr(f, "stage", "")), len(order)))
    rows = []
    seen = set()
    for finding in findings:
        stage = _value(getattr(finding, "stage", ""))
        seen.add(stage)
        result = _value(getattr(finding, "result", ""))
        detail = getattr(finding, "detail", "") or getattr(finding, "title", "")
        rows.append(TestRow(STAGE_WORDS.get(stage, stage or "-"), RESULT_WORDS.get(result,
                                                                                   result.upper()),
                            getattr(finding, "code", "") or "-", str(detail)))
    if getattr(report, "kind", "") == "vendor_test" and _value(
            getattr(report, "verdict", "")) != "passed":
        for stage in TEST_STAGES:
            if stage not in seen:
                rows.append(TestRow(STAGE_WORDS[stage], "-", "NOT_RUN",
                                    "Not run: the test stopped before this stage."))
    return tuple(rows)


_MEASUREMENT_LABELS = (
    ("psram_size", "PSRAM size found", lambda v: f"{int(v) / (1024 * 1024):g} MB ({v} bytes)"),
    ("psram_mb_s", "PSRAM speed", lambda v: f"{v} MB/s"),
    ("flash_id", "Flash ID read by the test program", lambda v: _jedec(str(v))),
    ("flash_size", "Flash size read by the test program",
     lambda v: f"{int(v) / (1024 * 1024):g} MB ({v} bytes)"),
    ("chip_rev", "Chip revision read by the test program", str),
    ("cores", "Processor cores", str),
    ("temp_c", "Chip temperature", lambda v: f"{v} °C (information only)"),
    ("networks", "Wi-Fi networks seen", str),
    ("best_rssi", "Strongest Wi-Fi signal", lambda v: f"{v} dBm"),
    ("loop_period_s", "Restart loop period", lambda v: f"{v} s"),
)


def _measurement_rows(measurements: Mapping[str, object]) -> Tuple[Tuple[str, str], ...]:
    rows = []
    used = set()
    for name, label, words in _MEASUREMENT_LABELS:
        if name in measurements and measurements[name] not in (None, ""):
            used.add(name)
            try:
                rows.append((label, words(measurements[name])))
            except (TypeError, ValueError):
                rows.append((label, str(measurements[name])))
    for name in sorted(measurements):
        if name not in used and name not in ("fw", "test_program_version", "proto", "idf",
                                             "mac") and measurements[name] not in (None, ""):
            rows.append((name.replace("_", " "), str(measurements[name])))
    return tuple(rows)


def read_console_entries(path: Optional[str]) -> List[Tuple[Optional[float], str]]:
    """(seconds, text) for each board line in a ConsoleLog file; notes and frames skipped."""
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: List[Tuple[Optional[float], str]] = []
    for raw in text.splitlines():
        match = _LOG_ENTRY.match(raw)
        if match:
            body = match.group(2)
            if not body.startswith("# "):
                entries.append((float(match.group(1)), body))
        elif raw.strip() and not raw.startswith(("===", "#")):
            entries.append((None, raw.strip()))
    return entries


def _at(seconds: Optional[float]) -> str:
    return "" if seconds is None else f"+{seconds:.1f} s"


def proof_lines(report: object, limit: int = PROOF_LINES_MAX) -> Tuple[ProofLine, ...]:
    """Up to ``limit`` exact lines: each finding's proof (fails first), else the SLTEST results.

    A proof line found in the run's console log gets its time from the start of the run.
    """
    entries = read_console_entries(getattr(report, "console_log", None))
    used_entries = set()
    wanted: List[str] = []
    findings = list(getattr(report, "findings", ()) or ())
    findings.sort(key=lambda f: {"fail": 0, "warn": 1}.get(_value(getattr(f, "result", "")), 2))
    for finding in findings:
        for line in getattr(finding, "proof", ()) or ():
            if line and line not in wanted:
                wanted.append(str(line))
    if not wanted:
        wanted = [text for _at_s, text in entries
                  if text.startswith("SLTEST ") and not text.startswith("SLTEST ALIVE")]
    result: List[ProofLine] = []
    cursor = 0
    for line in wanted[:limit]:
        when: Optional[float] = None
        # Proof lines come in the board's order: look after the last match first.
        order = list(range(cursor, len(entries))) + list(range(0, cursor))
        for index in order:
            seconds, text = entries[index]
            if index not in used_entries and text.strip() == line.strip():
                used_entries.add(index)
                when = seconds
                cursor = index + 1
                break
        text = redact(line.rstrip())
        if len(text) > PROOF_LINE_CHARS:
            text = text[:PROOF_LINE_CHARS - 1] + "…"
        result.append(ProofLine(_at(when), text))
    return tuple(result)


def only_watched(report: object) -> bool:
    """A locked board's report whose board was only watched while it started (Check a
    failed board): nothing was written, no stage of the test program ran. Its parts were
    not tested, whatever its measurements hold (the watch always records restarts)."""
    if getattr(report, "kind", "") != "locked_board":
        return False
    if getattr(report, "erased", None) is not None:
        return False  # the test program was written: it ran
    return not any(_value(getattr(finding, "stage", "")) in TEST_STAGES
                   for finding in getattr(report, "findings", ()) or ())


def _verdict_words(kind: CertificateKind, report: object) -> str:
    verdict = _value(getattr(report, "verdict", ""))
    if verdict == "passed":
        if only_watched(report):
            return "NO HARDWARE FAULT FOUND"
        return "PASSED — FIT FOR STATE LIFE USE"
    if verdict == "failed":
        return "NOT FIT FOR USE — HARDWARE FAULT"
    if getattr(report, "recovery", None):
        return "BOARD CAN BE RECOVERED"
    return "TEST NOT FINISHED — NO VERDICT ON THE HARDWARE"


def _reason(report: object, finding: Optional[object]) -> Tuple[str, str]:
    verdict = _value(getattr(report, "verdict", ""))
    advice = getattr(report, "advice", None)
    if verdict == "passed":
        if finding is not None and _value(getattr(finding, "result", "")) != "warn" \
                and not only_watched(report):
            return (str(getattr(finding, "title", "")), str(getattr(finding, "detail", "")))
        plain = ("Every test passed: the extra memory, the storage chip, both processor cores "
                 "and the Wi-Fi radio work, and the board was left as it came.")
        if only_watched(report):
            plain = "The board starts normally. No hardware fault was found."
        technical = ""
        if finding is not None:
            technical = (f"Note: {getattr(finding, 'title', '')}. "
                         f"{getattr(finding, 'detail', '')}").strip()
        return plain, technical
    if finding is not None:
        return (str(getattr(finding, "title", "")), str(getattr(finding, "detail", "")))
    if advice is not None:
        return (str(getattr(advice, "title", "")), str(getattr(advice, "what_happened", "")))
    return ("The test did not finish.", "")


_RECOVERY_WORDS = {
    "RESUME": "This board can still be finished: use Finish the setup.",
    "FIX_SETTINGS": "This board's settings are missing or damaged: use Fix the settings.",
}


def _next_steps(report: object) -> Tuple[str, ...]:
    steps: List[str] = []
    recovery = getattr(report, "recovery", None)
    if recovery in _RECOVERY_WORDS:
        steps.append(_RECOVERY_WORDS[recovery])
    advice = getattr(report, "advice", None)
    if advice is not None:
        steps.extend(str(step) for step in getattr(advice, "what_to_do", ()) or ())
    return tuple(steps)


def _keys_statement(kind: CertificateKind, report: object, finding: Optional[object]) -> str:
    if kind is not CertificateKind.VENDOR_RETURN or finding is None:
        return ""
    # Only a board that State Life keyed: a board secured by someone else, or used without
    # keys, was never given our keys, whatever its report kind.
    if not getattr(report, "keys_burned", False):
        return ""
    if not is_hardware_code(getattr(finding, "code", "")):
        return ""
    reason = str(getattr(finding, "title", "")).rstrip(".") or str(getattr(finding, "code", ""))
    if reason[:1].isupper() and reason[1:2].islower():
        reason = reason[:1].lower() + reason[1:]
    return ("This board was given State Life security keys during setup before the fault "
            f"showed. The fault is in the board's hardware: {reason}. "
            "The keys do not cause it.")


def build_certificate(report: object, *, tester: str, app_version: str, now: datetime,
                      kind: Optional[CertificateKind] = None, evidence_name: str = "",
                      evidence_sha256: str = "",
                      test_program_version: Optional[str] = None) -> CertificateData:
    """The certificate for ``report``; ``kind`` defaults to the one its verdict allows.

    A PASSED certificate needs a PASSED report and a VENDOR_RETURN one a FAILED report
    (ValueError otherwise); a FAULT_REPORT can be made for any report.
    """
    kind = CertificateKind(kind) if kind is not None else default_kind(report)
    verdict = _value(getattr(report, "verdict", ""))
    if kind is CertificateKind.PASSED and verdict != "passed":
        raise ValueError(f"a PASSED certificate needs a passed test, not {verdict!r}")
    if kind is CertificateKind.VENDOR_RETURN and verdict != "failed":
        raise ValueError(f"a vendor return certificate needs a failed test, not {verdict!r}")
    mac = str(getattr(report, "mac", "") or "")
    measurements = dict(getattr(report, "measurements", {}) or {})
    # A board that was only watched never had the test program written: its pinned image
    # hashes say nothing about it, so they are not printed.
    images = ({} if only_watched(report)
              else dict(getattr(report, "image_sha256", {}) or {}))
    finding = main_finding(report)
    plain, technical = _reason(report, finding)
    program = test_program_version or str(measurements.get("fw") or
                                          measurements.get("test_program_version") or "")
    if images:
        program = program or "State Life board test program"
    else:
        program = "Not used (the board was only watched while it started)"
    tested_by = (tester or "").strip() or "Not given"
    return CertificateData(
        kind=kind,
        title=TITLES[kind],
        number=certificate_number(_local(now), mac),
        issued=format_moment(now),
        tested_by=redact(tested_by),
        app_version=app_version,
        test_program=program,
        image_sha256=tuple(sorted(((str(k), str(v)) for k, v in images.items()),
                                  key=lambda item: (_IMAGE_ORDER.get(item[0], 9), item[0]))),
        test_run=(f"{_iso_words(getattr(report, 'started_at', ''))} to "
                  f"{_iso_words(getattr(report, 'finished_at', ''))}"),
        verdict=verdict if verdict in VERDICT_COLOURS else "incomplete",
        verdict_label=_verdict_words(kind, report),
        reason=plain,
        technical=technical,
        board=_board_rows(report),
        security=_security_rows(report),
        keys_statement=_keys_statement(kind, report, finding),
        tests=_test_rows(report),
        measurements=_measurement_rows(measurements),
        next_steps=_next_steps(report) if kind is CertificateKind.FAULT_REPORT else (),
        proof=proof_lines(report),
        evidence_name=evidence_name,
        evidence_sha256=evidence_sha256,
        footer=(f"Produced by the State Life Device Installer {app_version}. Verify: the "
                "SHA-256 of the evidence ZIP must match the value above."),
    )


# ------------------------------------------------------------------ plain text

def _pairs(rows: Iterable[Tuple[str, str]], width: int = 26) -> List[str]:
    return [f"  {label + ':':<{width}}{value}" for label, value in rows]


def certificate_text(data: CertificateData) -> str:
    rule = "=" * 72
    lines = [
        "State Life Device Installer",
        data.title,
        rule,
        *_pairs((
            ("Certificate number", data.number),
            ("Issued", data.issued),
            ("Tested by", data.tested_by),
            ("App version", data.app_version),
            ("Test program", data.test_program),
            ("Test run", data.test_run),
        )),
    ]
    for name, digest in data.image_sha256:
        lines.append(f"  {'SHA-256 ' + name + ':':<26}{digest}")
    lines += ["", "RESULT", f"  {data.verdict_label}", f"  {data.reason}"]
    if data.technical:
        lines.append(f"  Technical: {data.technical}")
    if data.keys_statement:
        lines += ["", f"  {data.keys_statement}"]
    lines += ["", "BOARD", *_pairs(data.board), "", "SECURITY", *_pairs(data.security)]
    lines += ["", "TESTS"]
    if data.tests:
        for row in data.tests:
            lines.append(f"  {row.stage:<22}{row.result:<6}{row.code:<24}{row.detail}")
    else:
        lines.append("  No test results.")
    if data.measurements:
        lines += ["", "MEASUREMENTS", *_pairs(data.measurements, 38)]
    if data.next_steps:
        lines += ["", "WHAT TO DO NOW", *(f"  - {step}" for step in data.next_steps)]
    lines += ["", "PROOF (exact lines from the board and the tools)"]
    if data.proof:
        lines += [f"  {line.at:>9}  {line.text}" for line in data.proof]
    else:
        lines.append("  No board output was kept.")
    lines += ["", "EVIDENCE"]
    if data.evidence_name:
        lines += _pairs((("Evidence file", data.evidence_name),
                         ("SHA-256", data.evidence_sha256 or "not given")))
        lines.append("  The full board output is in the evidence file.")
    else:
        lines.append("  This text is part of the evidence file; the full board output is in it.")
    lines += ["", data.footer, ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ PDF

def _e(text: object) -> str:
    return html.escape(str(text), quote=False)


def _mono(family: str, size: float) -> str:
    return f"font-family: '{family}'; font-size: {size}pt;"


def _kv_table(rows: Sequence[Tuple[str, str]], mono_labels: Sequence[str] = (),
              mono: str = "") -> str:
    out = ['<table width="100%" cellspacing="0" cellpadding="5" class="kv">']
    for index, (label, value) in enumerate(rows):
        shade = ' bgcolor="#F5F8FB"' if index % 2 == 0 else ""
        style = f' style="{mono}"' if label in mono_labels and mono else ""
        out.append(f'<tr{shade}><td width="30%" class="label">{_e(label)}</td>'
                   f'<td{style}>{_e(value)}</td></tr>')
    out.append("</table>")
    return "".join(out)


def _result_colour(result: str) -> str:
    return {"PASS": "#1E8E3E", "FAIL": "#C5221F", "NOTE": "#B26B00"}.get(result, MUTED)


HEADINGS = ("TEST DETAILS", "BOARD", "SECURITY", "TESTS", "MEASUREMENTS", "WHAT TO DO NOW",
            "PROOF", "EVIDENCE")


def _h2(title: str, breaks: Iterable[str]) -> str:
    style = ' style="page-break-before: always;"' if title in breaks else ""
    return f"<h2{style}>{_e(title)}</h2>"


def certificate_html(data: CertificateData, mono_family: str = "Courier New",
                     breaks: Iterable[str] = ()) -> str:
    """The body of the certificate as Qt rich text (QTextDocument's HTML subset).

    ``mono_family`` must be a font this computer has: Qt's CSS takes one family name
    reliably, so render_pdf passes the fixed-pitch font it found. ``breaks`` names the
    section headings that start a new page (render_pdf moves a heading that would sit
    at the foot of a page).
    """
    breaks = frozenset(breaks)
    text, back, border = data.colours
    mono = _mono(mono_family, 7.5)
    proof_style = _mono(mono_family, 7) + " white-space: pre-wrap; margin: 0;"
    parts = [
        "<html><head><style>",
        f"body {{ color: {INK}; }}",
        f"h1 {{ font-size: 17pt; font-weight: 700; color: {INK}; margin: 0; }}",
        f"h2 {{ font-size: 10.5pt; font-weight: 700; color: {BRAND}; margin-top: 14px;"
        " margin-bottom: 4px; }}",
        f".sub {{ color: {MUTED}; font-size: 8.5pt; }}",
        f".label {{ color: {MUTED}; }}",
        "table.kv td { font-size: 8.5pt; }",
        f"table.grid th {{ font-size: 8pt; color: #FFFFFF; background-color: {BRAND};"
        " text-align: left; }}",
        "table.grid td { font-size: 8pt; }",
        "</style></head><body>",
        f"<h1>{_e(data.title)}</h1>",
        f'<p class="sub">Certificate number <b>{_e(data.number)}</b> &nbsp;·&nbsp; '
        f"issued {_e(data.issued)}</p>",
        # The verdict box: one coloured cell.
        f'<table width="100%" cellspacing="0" cellpadding="12" border="2" '
        f'style="border-color: {border}; border-style: solid; border-collapse: collapse; '
        f'margin-top: 8px;">'
        f'<tr><td bgcolor="{back}">'
        f'<p style="font-size: 15pt; font-weight: 700; color: {text}; margin: 0;">'
        f"{_e(data.verdict_label)}</p>"
        f'<p style="font-size: 10pt; color: {INK}; margin-top: 6px;">{_e(data.reason)}</p>',
    ]
    if data.technical:
        parts.append(f'<p style="font-size: 8.5pt; color: {MUTED}; margin-top: 4px;">'
                     f"Technical: {_e(data.technical)}</p>")
    if data.keys_statement:
        parts.append(f'<p style="font-size: 9pt; color: {INK}; margin-top: 6px;">'
                     f"<b>Security keys:</b> {_e(data.keys_statement)}</p>")
    parts.append("</td></tr></table>")

    parts.append(_h2("TEST DETAILS", breaks))
    about = [("Tested by", data.tested_by), ("Test run", data.test_run),
             ("App version", f"State Life Device Installer {data.app_version}"),
             ("Test program", data.test_program)]
    about += [(f"SHA-256 {name}", digest) for name, digest in data.image_sha256]
    parts.append(_kv_table(about, mono_labels=[f"SHA-256 {n}" for n, _d in data.image_sha256],
                           mono=mono))

    parts.append(_h2("BOARD", breaks))
    parts.append(_kv_table(data.board))
    parts.append(_h2("SECURITY", breaks))
    parts.append(_kv_table(data.security))

    parts.append(_h2("TESTS", breaks))
    if data.tests:
        parts.append('<table width="100%" cellspacing="0" cellpadding="5" class="grid" '
                     'border="1" style="border-color: #D5DCE4; border-style: solid; '
                     'border-collapse: collapse;"><tr>')
        for title, width in (("Stage", "22%"), ("Result", "9%"), ("Code", "25%"),
                             ("Detail", "44%")):
            parts.append(f'<th width="{width}" bgcolor="{BRAND}" align="left">'
                         f'<span style="color: #FFFFFF;">{title}</span></th>')
        parts.append("</tr>")
        for index, row in enumerate(data.tests):
            shade = ' bgcolor="#F5F8FB"' if index % 2 else ""
            parts.append(
                f"<tr{shade}><td>{_e(row.stage)}</td>"
                f'<td><b><span style="color: {_result_colour(row.result)};">'
                f"{_e(row.result)}</span></b></td>"
                f'<td style="{_mono(mono_family, 8)}">{_e(row.code)}</td>'
                f"<td>{_e(row.detail)}</td></tr>")
        parts.append("</table>")
    else:
        parts.append('<p class="sub">No test results.</p>')

    if data.measurements:
        parts.append(_h2("MEASUREMENTS", breaks))
        parts.append(_kv_table(data.measurements))
    if data.next_steps:
        parts.append(_h2("WHAT TO DO NOW", breaks) + "<ul>")
        parts += [f'<li style="font-size: 9pt;">{_e(step)}</li>' for step in data.next_steps]
        parts.append("</ul>")

    parts.append(_h2("PROOF", breaks))
    parts.append('<p class="sub">Exact lines from the board and the tools; times are from the '
                 "start of the run.</p>")
    if data.proof:
        parts.append('<table width="100%" cellspacing="0" cellpadding="6" bgcolor="#F4F6F8" '
                     'border="1" style="border-color: #D5DCE4; border-style: solid; '
                     'border-collapse: collapse;"><tr><td>')
        for line in data.proof:
            stamp = _e(line.at.rjust(9)).replace(" ", "&nbsp;")
            parts.append(f'<p style="{proof_style}">'
                         f'<span style="color: {MUTED};">{stamp}</span>&nbsp;&nbsp;'
                         f"{_e(line.text)}</p>")
        parts.append("</td></tr></table>")
    else:
        parts.append('<p class="sub">No board output was kept.</p>')

    parts.append(_h2("EVIDENCE", breaks))
    if data.evidence_name:
        parts.append(_kv_table((("Evidence file", data.evidence_name),
                                ("SHA-256", data.evidence_sha256 or "not given")),
                               mono_labels=("SHA-256",), mono=mono))
        parts.append('<p style="font-size: 8.5pt;">The full board output is in the evidence '
                     "file.</p>")
    else:
        parts.append('<p style="font-size: 8.5pt;">The full board output is in the evidence '
                     "file.</p>")
    parts.append("</body></html>")
    return "".join(parts)


_MONO = ("Consolas", "Menlo", "DejaVu Sans Mono", "Liberation Mono", "Courier New")
_SANS = ("Segoe UI", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans", "Liberation Sans")


def _families(installed: Sequence[str], wanted: Sequence[str]) -> List[str]:
    """The wanted families this computer has (asking for missing ones is slow in Qt)."""
    have = {name.lower() for name in installed}
    found = [name for name in wanted if name.lower() in have]
    return found or list(installed[:1]) or list(wanted[:1])


def _orphan_heading(document: object, page_height: float) -> Optional[str]:
    """The first section heading left alone at the foot of a page, if any.

    A heading is alone when the two blocks after it (a caption, a table's first row)
    end on a later page than the heading itself.
    """
    layout = document.documentLayout()  # type: ignore[attr-defined]
    block = document.begin()  # type: ignore[attr-defined]
    while block.isValid():
        if block.blockFormat().headingLevel() == 2 and block.text() in HEADINGS:
            page = int(layout.blockBoundingRect(block).top() // page_height)
            follower = block.next()
            for _ in range(2):
                if not follower.isValid():
                    break
                bottom = layout.blockBoundingRect(follower).bottom()
                if int((bottom - 1) // page_height) != page:
                    return block.text()
                follower = follower.next()
        block = block.next()
    return None


_APP = None  # a QGuiApplication made here when the caller had none (bench, tests)


def _ensure_gui_app() -> None:
    import os
    import sys

    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication

    global _APP
    if QCoreApplication.instance() is not None:
        return
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QGuiApplication(sys.argv[:1] or ["sl-installer"])


def render_pdf(data: CertificateData, path: Path, logo: Optional[Path] = None) -> Path:
    """Draw ``data`` as an A4 PDF at 300 dpi into ``path`` (written in place)."""
    from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
    from PySide6.QtGui import (QColor, QFont, QFontDatabase, QImage, QPageLayout, QPageSize,
                               QPainter,
                               QPdfWriter, QPen, QTextDocument)

    _ensure_gui_app()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(path))
    writer.setResolution(300)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                                     QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
    writer.setTitle(f"{data.title} {data.number}")
    writer.setCreator(f"State Life Device Installer {data.app_version}")

    dpi = 300
    mm = dpi / 25.4
    page_w = int(round(210 * mm))
    page_h = int(round(297 * mm))
    margin_x = 16 * mm
    header_h = 24 * mm
    footer_h = 16 * mm
    body_top = header_h + 6 * mm
    body_w = page_w - 2 * margin_x
    body_h = page_h - body_top - footer_h - 4 * mm

    painter = QPainter()
    if not painter.begin(writer):
        raise OSError(f"cannot write {path}")
    try:
        base = QFont()
        base.setFamilies(_families(QFontDatabase.families(), _SANS))
        base.setPointSizeF(9)
        document = QTextDocument()
        document.documentLayout().setPaintDevice(writer)
        document.setDefaultFont(base)
        document.setDocumentMargin(0)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        mono_family = _families(QFontDatabase.families(), _MONO + (fixed,))[0]
        document.setPageSize(QSizeF(body_w, body_h))
        breaks: List[str] = []
        for _attempt in range(len(HEADINGS)):
            document.setHtml(certificate_html(data, mono_family, breaks))
            orphan = _orphan_heading(document, body_h)
            if orphan is None or orphan in breaks:
                break
            breaks.append(orphan)
        pages = max(1, document.pageCount())

        image = QImage(str(logo or LOGO_FILE))
        _text, _back, verdict_border = data.colours
        for page in range(pages):
            if page:
                writer.newPage()
            # Header: logo, name, document kind and number; a brand rule beneath.
            logo_size = 15 * mm
            top = 7 * mm
            if not image.isNull():
                painter.drawImage(QRectF(margin_x, top, logo_size, logo_size), image)
            head = QFont(base)
            head.setPointSizeF(13)
            head.setBold(True)
            painter.setFont(head)
            painter.setPen(QColor(INK))
            text_x = margin_x + logo_size + 4 * mm
            painter.drawText(QRectF(text_x, top, body_w * 0.55, logo_size * 0.55),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                             "State Life")
            small = QFont(base)
            small.setPointSizeF(8)
            painter.setFont(small)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(text_x, top + logo_size * 0.58, body_w * 0.6,
                                    logo_size * 0.42),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                             "Device Installer — board hardware test")
            right = QRectF(margin_x + body_w * 0.45, top, body_w * 0.55, logo_size)
            kind_font = QFont(base)
            kind_font.setPointSizeF(9)
            kind_font.setBold(True)
            painter.setFont(kind_font)
            painter.setPen(QColor(verdict_border))
            short = {CertificateKind.PASSED: "BOARD TEST CERTIFICATE",
                     CertificateKind.VENDOR_RETURN: "HARDWARE FAULT CERTIFICATE",
                     CertificateKind.FAULT_REPORT: "BOARD FAULT REPORT"}[data.kind]
            painter.drawText(QRectF(right.x(), right.y(), right.width(), right.height() * 0.55),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, short)
            painter.setFont(small)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(right.x(), right.y() + right.height() * 0.58, right.width(),
                                    right.height() * 0.42),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, data.number)
            painter.setPen(QPen(QColor(BRAND), 0.8 * mm))
            painter.drawLine(QPointF(margin_x, header_h), QPointF(page_w - margin_x, header_h))

            # Body: this page's slice of the document.
            painter.save()
            painter.translate(margin_x, body_top)
            clip = QRectF(0, 0, body_w, body_h)
            painter.setClipRect(clip)
            painter.translate(0, -page * body_h)
            document.drawContents(painter, QRectF(0, page * body_h, body_w, body_h))
            painter.restore()

            # Footer: the verification sentence and the page number.
            foot_top = page_h - footer_h
            painter.setPen(QPen(QColor("#D5DCE4"), 0.3 * mm))
            painter.drawLine(QPointF(margin_x, foot_top), QPointF(page_w - margin_x, foot_top))
            foot = QFont(base)
            foot.setPointSizeF(7)
            painter.setFont(foot)
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(margin_x, foot_top + 2 * mm, body_w * 0.68, footer_h - 4 * mm),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                             | Qt.TextFlag.TextWordWrap, data.footer)
            painter.drawText(QRectF(margin_x + body_w * 0.68, foot_top + 2 * mm, body_w * 0.32,
                                    footer_h - 4 * mm),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                             f"{data.number}\nPage {page + 1} of {pages}")
    finally:
        painter.end()
    return path


def write_pdf(data: CertificateData, path: Path) -> Path:
    """Write the PDF next to ``path`` first, then move it in place (no half files)."""
    import os

    path = Path(path)
    part = path.with_name(path.name + ".part")
    try:
        render_pdf(data, part)
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()
    return path


__all__ = [
    "CertificateData", "CertificateKind", "ProofLine", "TestRow", "build_certificate",
    "certificate_html", "certificate_number", "certificate_text", "default_kind",
    "efuse_differences", "is_hardware_code", "main_finding", "only_watched", "proof_lines",
    "render_pdf",
    "write_pdf",
]
