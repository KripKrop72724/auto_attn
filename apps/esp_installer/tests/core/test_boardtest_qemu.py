"""The board test program's REAL console output, judged by the app.

fixtures/boards/qemu-*.txt are console captures of the compiled board test program
(ESP-IDF v5.5.3) running in Espressif's QEMU for the ESP32-S3: the exact pinned image
(its lines read at sl_out.c emit() through gdb, bytes untouched) and a SIM-ONLY copy whose
console is on UART0 and which can inject one fault (PSRAM bit stuck, PSRAM hang, flash
page bit flip). Each file says how it was made; '# tap:' rows are gdb workarounds for what
QEMU lacks (no temperature sensor, no radio) and never reach the parser.

What QEMU cannot emulate is pinned here as such: its radio calibration never returns, so a
run left alone ends in WIFI_HANG; with a real board's Wi-Fi RESULT in its place the same run
passes. Blank QEMU eFuses report mac=00:00:00:00:00:00, which is the board MAC used here.

These tests feed the lines through ConsoleParser + TestJudge.check exactly as BoardTester
does (one judge, events in time order, checked in silence too), check every END line's crc
the firmware computed against the parser's and an independent CRC-32, and drive whole
BoardTester runs (write, start, judge, cleanup, eFuse compare) and the certificate on them.
"""
from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path
from typing import List, Tuple

import pytest

from sl_installer.core import boardtest, texts
from sl_installer.core.api import BoardTestVerdict, ConsoleKind
from sl_installer.core.boardtest import TestJudge
from sl_installer.core.console import ConsoleParser, ConsoleWatch
from sl_installer.core.records import Records

from . import support
from .support import MAC, FakeBoard
from .test_boardtest import Rig, assert_never_burned, blank_board
from .test_boardtest_protocol import (FIRMWARE, Firmware, c_format, end_format,
                                      esp_rom_crc32_le)

QEMU_MAC = "00:00:00:00:00:00"   # blank eFuses in QEMU
Rows = List[Tuple[float, str]]
needs_sources = pytest.mark.skipif(not FIRMWARE.is_dir(),
                                   reason="board_test_firmware sources not in this checkout")

ALL = ("exact-pass", "exact-full", "exact-wifi-natural", "sim-base-natural",
       "sim-base-wifi-natural", "sim-base-full", "sim-psram-bitflip", "sim-psram-hang",
       "sim-flash-write")
_ROW = re.compile(r"^\[\s*(\d+\.\d+)\] (.*)$")


def qemu_rows(name: str) -> Rows:
    """(host seconds, console line) of a capture; header and '# tap:' rows dropped."""
    rows: Rows = []
    for row in support.board_sample(f"qemu-{name}.txt").splitlines():
        found = _ROW.match(row)
        if found and not found.group(2).startswith("#"):
            rows.append((float(found.group(1)), found.group(2)))
    return rows


def until_clean(rows: Rows) -> Rows:
    """The rows up to the first CLEAN: a real board restarts into download mode there."""
    index = next(i for i, (_at, line) in enumerate(rows) if line.startswith("SLTEST CLEAN"))
    return rows[:index + 1]


class AppWatch:
    """BootWatcher.read + BoardTester._watch on recorded rows: one parser, one judge, the
    watch filled in time order and judged after every line and every 0.1 s of silence."""

    def __init__(self, mac: str = QEMU_MAC) -> None:
        self.parser = ConsoleParser(mac)
        self.judge = TestJudge(mac)
        self.watch = ConsoleWatch(started=0.0, events=[], lines=[], port_open=True)

    def _add(self, at: float, line: str) -> None:
        event = self.parser.parse(line, at)
        if event is None:
            return
        self.watch.last_line_at = at
        self.watch.lines.append((at, event.raw))
        if event.kind is ConsoleKind.OTHER and not event.data:
            self.watch.other_lines += 1
        else:
            self.watch.events.append(event)

    def run(self, rows: Rows, *, deadline: float = boardtest.WATCH_S):
        """(time decided, judgement); the judgement at the deadline when nothing decided."""
        pending = list(rows)
        t = 0.0
        while t < deadline:
            while pending and pending[0][0] <= t:
                self._add(*pending.pop(0))
                found = self.judge.check(self.watch, t)
                if found is not None:
                    return t, found
            found = self.judge.check(self.watch, t)
            if found is not None:
                return t, found
            t = round(t + 0.1, 3)
        return t, self.judge.check(self.watch, t, final=True)


def judged(name: str, mac: str = QEMU_MAC):
    return AppWatch(mac).run(qemu_rows(name))


def sltest_runs(rows: Rows) -> List[Tuple[List[str], str]]:
    """Each program run's CRC lines (BEGIN up to END, CLEAN excluded) and its END line."""
    runs, current = [], None
    for _at, line in rows:
        if line.startswith("SLTEST BEGIN"):
            current = []
        if current is None or not line.startswith("SLTEST "):
            continue
        if line.startswith("SLTEST END"):
            runs.append((current, line))
            current = None
        elif not line.startswith("SLTEST CLEAN"):
            current.append(line)
    return runs


def firmware_crc(lines: List[str]) -> int:
    """sl_out.c: esp_rom_crc32_le over every line, '\\n' between them."""
    crc = 0
    for index, line in enumerate(lines):
        if index:
            crc = esp_rom_crc32_le(crc, b"\n")
        crc = esp_rom_crc32_le(crc, line.encode("ascii"))
    return crc


# ------------------------------------------------------------------ the CRC contract

def test_every_qemu_end_crc_is_the_crc_the_app_computes():
    checked = 0
    for name in ALL:
        rows = qemu_rows(name)
        parser = ConsoleParser(QEMU_MAC)
        ends = [event for event in (parser.parse(line, at) for at, line in rows)
                if event is not None and event.data.get("sltest") == "END"]
        runs = sltest_runs(rows)
        assert len(ends) == len(runs)
        for (lines, end_line), event in zip(runs, ends):
            printed = int(end_line.rsplit("crc=", 1)[1], 16)
            assert printed == firmware_crc(lines) == zlib.crc32("\n".join(lines).encode())
            assert event.data["crc_ok"] == "1", (name, end_line)
            checked += 1
    # exact-pass, exact-full (two runs), sim-base-full, sim-psram-bitflip, sim-flash-write
    assert checked == 6


def _line_pattern(fmt: str) -> "re.Pattern[str]":
    pieces = re.split(r"%0?\d*l?[dusx]", fmt)
    return re.compile("SLTEST " + "[^ ]+?".join(re.escape(piece) for piece in pieces) + "$")


@needs_sources
def test_every_qemu_line_has_the_firmware_s_format():
    patterns = [_line_pattern(fmt) for fmt in Firmware().formats]
    end = _line_pattern(end_format()[len("SLTEST "):])
    seen = set()
    for name in ALL:
        for _at, line in qemu_rows(name):
            if not line.startswith("SLTEST "):
                continue
            assert end.match(line) or any(p.match(line) for p in patterns), (name, line)
            seen.add(line.split(" ")[1])
    assert seen == {"BEGIN", "ALIVE", "ERR", "RESULT", "END", "CLEAN"}


def test_one_changed_byte_of_a_real_run_spoils_its_crc():
    rows = [(at, line.replace("ms=387", "ms=388")) for at, line in qemu_rows("exact-pass")]
    _t, verdict = AppWatch().run(rows)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "USB_DATA_GARBLED"
    assert verdict.end_crc_ok is False


# ------------------------------------------------------------------ the verdicts

def test_the_good_qemu_run_passes_with_its_real_measurements():
    t, verdict = judged("exact-pass")
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.code == "BOARD_TEST_PASSED"
    assert verdict.end_crc_ok and verdict.clean_ok and verdict.program_done
    # Wi-Fi was forced past QEMU's missing radio: no networks, a warning only.
    assert [(f.stage, f.code, f.result) for f in verdict.findings] == [
        ("psram", "OK", "ok"), ("iram", "OK", "ok"), ("flash", "OK", "ok"), ("cpu", "OK", "ok"),
        ("wifi", "WIFI_NO_NETWORKS", "warn")]
    clean_at = next(at for at, line in qemu_rows("exact-pass") if "SLTEST CLEAN" in line)
    assert clean_at <= t < clean_at + 0.2     # decided on the CLEAN line
    m = verdict.measurements
    assert m["test_program_version"] == "1.0.0" and m["protocol"] == 1 and m["idf"] == "v5.5.3"
    assert m["mac_reported"] == QEMU_MAC and m["chip_rev"] == "v0.0" and m["cores"] == 2
    assert m["flash_id"] == "9d6018" and m["flash_id_read"] == "9d6018"
    assert m["flash_size"] == m["flash_size_read"] == 16 * 1024 * 1024
    assert m["psram_size"] == 8 * 1024 * 1024 and m["psram_tested"] == 8_257_472
    assert m["temp_c"] == "na" and m["cpu_sum"] == "cdcb3e9f" and m["networks"] == 0


@needs_sources
def test_the_good_qemu_run_with_a_real_board_wifi_result_passes():
    """QEMU has no radio. Put the firmware's own Wi-Fi ok line (as a real board prints it)
    in place of the forced one, END's crc recomputed the firmware's way: it passes."""
    rows = until_clean(qemu_rows("exact-pass"))
    fw = Firmware()
    wifi_ok = "SLTEST " + c_format(fw.fmt("RESULT stage=wifi result=ok"), 2900, 6, -48)
    replaced = [(at, wifi_ok if line.startswith("SLTEST RESULT stage=wifi") else line)
                for at, line in rows]
    lines, end_line = sltest_runs(replaced)[0]
    assert wifi_ok in lines
    crc = firmware_crc(lines)
    new_end = "SLTEST " + c_format(end_format()[7:], "ok", "none", crc)
    with_end = [(at, new_end if line == end_line else line) for at, line in replaced]
    _t, verdict = AppWatch().run(with_end)
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.end_crc_ok
    assert [(f.stage, f.result) for f in verdict.findings] == [
        (stage, "ok") for stage in boardtest.STAGES]
    assert verdict.measurements["networks"] == 6 and verdict.measurements["best_rssi"] == -48
    # Kept with the old END, the swapped line is caught by the crc.
    _t, kept = AppWatch().run(replaced)
    assert kept.code == "USB_DATA_GARBLED" and kept.verdict is not BoardTestVerdict.PASSED


@pytest.mark.parametrize("name, code, stage", [
    # QEMU's radio calibration never returns (no radio): a real hang to the judge.
    ("exact-wifi-natural", "WIFI_HANG", "wifi"),
    ("sim-base-wifi-natural", "WIFI_HANG", "wifi"),
    # QEMU has no temperature sensor: the read waits forever with interrupts off.
    ("sim-base-natural", "CPU_HANG", "cpu"),
    # SIM_FAULT (b): the PSRAM test spins forever 3 MiB into its x55 write.
    ("sim-psram-hang", "PSRAM_HANG", "psram"),
])
def test_silence_in_a_real_run_is_a_hang_of_that_stage(name, code, stage):
    rows = qemu_rows(name)
    last_sltest = max(at for at, line in rows if line.startswith("SLTEST "))
    t, verdict = judged(name)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == code
    assert verdict.advice.code == code and verdict.end_crc_ok is None
    hang = verdict.findings[-1]
    assert hang.stage == stage and hang.code == code and hang.result == "fail"
    assert f"SLTEST ALIVE stage={stage}" in "\n".join(hang.proof)
    # Decided by the silence (8 s), plus the linger for trailing proof, never the deadline.
    silence = boardtest.SILENCE_S + boardtest.LINGER_S
    assert last_sltest + silence - 0.1 <= t <= last_sltest + silence + 0.3
    done = [f.stage for f in verdict.findings if f.result == "ok"]
    assert done == list(boardtest.STAGES[:boardtest.STAGES.index(stage)])


def test_the_real_psram_bitflip_fails_with_its_err_lines_as_proof():
    t, verdict = judged("sim-psram-bitflip")
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_DATA_ERRORS"
    assert verdict.advice is texts.PSRAM_DATA_ERRORS and verdict.end_crc_ok
    finding = verdict.findings[0]
    assert finding.stage == "psram" and finding.result == "fail"
    assert "detail zeros, 10 errors" in finding.detail
    assert "first error in zeros at 0x3c1b1bf4 (wrote 0x00000000, read 0x00000020)" \
        in finding.detail
    errs = [line for line in finding.proof if line.startswith("SLTEST ERR stage=psram")]
    assert len(errs) == 8       # the firmware's cap; errors=10 counts them all
    # Wi-Fi was forced to fail in this run as well; the first failed stage leads.
    assert [f.code for f in verdict.findings if f.result == "fail"] == [
        "PSRAM_DATA_ERRORS", "WIFI_RADIO_FAILED"]


def test_the_real_flash_write_fault_fails_with_its_err_lines_as_proof():
    _t, verdict = judged("sim-flash-write")
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "FLASH_WRITE_ERRORS"
    assert verdict.advice is texts.FLASH_WRITE_ERRORS and verdict.end_crc_ok
    finding = next(f for f in verdict.findings if f.stage == "flash")
    assert "detail write, 1,024 errors" in finding.detail
    assert "first error in write at 0x00702000 (wrote 0xfccbc981, read 0xfccbc980)" \
        in finding.detail
    assert len([p for p in finding.proof if p.startswith("SLTEST ERR stage=flash")]) == 8


@pytest.mark.parametrize("name", ["exact-full", "sim-base-full"])
def test_a_real_radio_failure_names_the_step_and_error(name):
    rows = qemu_rows(name)
    t, verdict = judged(name)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "WIFI_RADIO_FAILED"
    assert verdict.end_crc_ok and verdict.clean_ok
    wifi = next(f for f in verdict.findings if f.stage == "wifi")
    assert "detail init_esp_err_no_mem" in wifi.detail
    # exact-full: QEMU ran the program again after CLEAN; the app had decided before that.
    begins = [at for at, line in rows if line.startswith("SLTEST BEGIN")]
    assert len(begins) == (3 if name == "exact-full" else 1)
    if len(begins) > 1:
        assert t < begins[1]


def test_a_real_run_on_another_board_is_refused():
    _t, verdict = judged("exact-pass", mac=MAC)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "BOARD_SWAPPED"


# ------------------------------------------------------------------ whole runs

def qemu_program(name: str) -> Rows:
    """What the board prints after the start call, timed from its first line."""
    rows = qemu_rows(name)
    if any(line.startswith("SLTEST CLEAN") for _at, line in rows):
        rows = until_clean(rows)
    first = rows[0][0]
    return [(round(at - first, 3), line) for at, line in rows]


def qemu_rig(tmp_path: Path, name: str) -> Rig:
    rig = Rig(tmp_path, qemu_program(name), fake=FakeBoard(mac=QEMU_MAC))
    rig.usb.mac = QEMU_MAC
    return rig


@pytest.mark.parametrize("name, verdict, code, chip_erased", [
    ("exact-pass", BoardTestVerdict.PASSED, "BOARD_TEST_PASSED", False),
    ("sim-psram-bitflip", BoardTestVerdict.FAILED, "PSRAM_DATA_ERRORS", False),
    ("sim-flash-write", BoardTestVerdict.FAILED, "FLASH_WRITE_ERRORS", False),
    ("exact-full", BoardTestVerdict.FAILED, "WIFI_RADIO_FAILED", False),
    ("sim-psram-hang", BoardTestVerdict.FAILED, "PSRAM_HANG", True),
    ("exact-wifi-natural", BoardTestVerdict.FAILED, "WIFI_HANG", True),
])
def test_whole_board_tests_on_real_program_output(tmp_path, name, verdict, code, chip_erased):
    rig = qemu_rig(tmp_path, name)
    report = rig.run(blank_board(mac=QEMU_MAC))
    assert report.verdict is verdict, report.findings
    assert report.advice.code == code
    assert report.efuses_unchanged is True and report.erased is True
    assert rig.fake.started_programs == 1 and rig.phases == []
    commands = rig.commands()
    assert commands[:5] == ["get-security-info", "summary", "write-flash", "write-mem",
                            "get-security-info"]
    # A run that ended with CLEAN erased its own test blocks; a hang gets the whole chip.
    assert commands[5:] == (["erase-region", "erase-flash", "summary"] if chip_erased
                            else ["erase-region", "summary"])
    assert rig.fake.chip_erases == (1 if chip_erased else 0)
    assert report.end_crc_ok is (None if chip_erased else True)
    assert report.measurements["flash_id"] == "9d6018"
    log = Path(report.console_log).read_text(encoding="utf-8")
    assert "SLTEST BEGIN proto=1 fw=1.0.0" in log
    assert_never_burned(rig)


@pytest.fixture
def offscreen(qapp):
    """The session's QApplication: the PDF needs one (see test_certificate.offscreen)."""
    return qapp


@pytest.mark.parametrize("name, word", [("exact-pass", "passed"),
                                        ("sim-psram-bitflip", "fault")])
def test_certificates_of_real_program_output(tmp_path, offscreen, name, word):
    rig = qemu_rig(tmp_path, name)
    report = rig.run(blank_board(mac=QEMU_MAC))
    records = Records(tmp_path / "records", tmp_path / "logs", "1.2.0")
    pdf, evidence = records.save_board_test(report, "Bench Tester")
    assert Path(pdf).name.startswith("board-test-000000000000-")
    assert Path(pdf).name.endswith(f"-{word}.pdf")
    assert Path(pdf).read_bytes().startswith(b"%PDF")
    with zipfile.ZipFile(evidence) as archive:
        console = archive.read("console.log").decode()
        certificate = archive.read("certificate.txt").decode()
    end = next(line for _at, line in qemu_program(name) if line.startswith("SLTEST END"))
    assert end in console
    if word == "passed":
        assert "PASSED" in certificate
        # QEMU has no temperature sensor: the program printed temp_c=na.
        assert "Chip temperature:" in certificate and "na °C" not in certificate
        assert "not measured (information only)" in certificate
    else:
        assert "PSRAM_DATA_ERRORS" in certificate or texts.PSRAM_DATA_ERRORS.title in certificate
        assert "0x3c1b1bf4" in certificate
