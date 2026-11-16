"""Board test certificates (core/certificate.py): data, plain text and the PDF.

The reports are built from the real fixtures: the a45788 PSRAM start-up crash console,
the blank and key-burned eFuse summaries. ``make_report`` is shared with
test_records_board_test.py and the sample generator.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

# As in tests/ui/conftest.py: render without a screen, and on Windows read the fonts
# from the system (the offscreen platform has none of its own).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32" and os.environ["QT_QPA_PLATFORM"] == "offscreen":
    os.environ.setdefault(
        "QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from sl_installer.core import api, certificate  # noqa: E402
from sl_installer.core.api import (  # noqa: E402
    Advice, BoardInfo, HardwareVerdict, SecurityState)
from sl_installer.core.certificate import (  # noqa: E402
    CertificateKind, build_certificate, certificate_text)

FIXTURES = Path(__file__).parent / "fixtures"
A45788 = FIXTURES / "boards" / "psram-fault-crash-a45788-console.txt"
PKT = timezone(timedelta(hours=5), "PKT")
NOW = datetime(2026, 11, 16, 14, 3, 22, tzinfo=PKT)
IMAGES = {
    "bootloader.bin": "1" * 64,
    "partition-table.bin": "2" * 64,
    "board-test.bin": "3" * 64,
}

# ------------------------------------------------------------- report builders

Verdict = api.BoardTestVerdict
Finding = api.TestFinding
Report = api.BoardTestReport


def efuses(name: str) -> Dict[str, object]:
    return json.loads((FIXTURES / "efuse" / f"{name}.json").read_text(encoding="utf-8"))


def board(mac: str = "a4:e5:e8:12:34:56", *, sdm: bool = False,
          security: SecurityState = SecurityState.BLANK) -> BoardInfo:
    if sdm:
        return BoardInfo("/dev/cu.usbmodem1101", mac, "ESP32-S3", None, None,
                         HardwareVerdict.NOT_CHECKED, security, True,
                         {"usb_mac": mac, "secure_download_mode": True})
    return BoardInfo(
        "/dev/cu.usbmodem1101", mac, "ESP32-S3", 16, 8, HardwareVerdict.OK, security, False,
        {"usb_mac": mac, "chip_line": "ESP32-S3 (QFN56) (revision v0.2)",
         "features": "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)",
         "flash_id": "20 40 18"},
        flash_id="20 40 18", chip_revision=2)


SLTEST_PASS = (
    "SLTEST BEGIN proto=1 fw=1.0.0 mac=a4:e5:e8:12:34:56 chip_rev=v0.2 cores=2 idf=v5.5.3 "
    "flash_id=204018 flash_size=16777216 psram_size=8388608",
    "SLTEST RESULT stage=psram result=ok code=OK detail=2_rounds errors=0 ms=9120 mb_s=41",
    "SLTEST RESULT stage=iram result=ok code=OK detail=1_round errors=0 ms=310",
    "SLTEST RESULT stage=flash result=ok code=OK detail=14_blocks errors=0 ms=7020",
    "SLTEST RESULT stage=cpu result=ok code=OK detail=match errors=0 ms=420 temp_c=38",
    "SLTEST RESULT stage=wifi result=ok code=OK detail=scan errors=0 ms=2410 networks=7 "
    "best_rssi=-48",
    "SLTEST END result=ok failed=none crc=1a2b3c4d",
    "SLTEST CLEAN result=ok",
)


def console_log(tmp_path: Path, lines, name: str = "console-test.log") -> str:
    """A ConsoleLog-style file: a frame, then one "seconds  line" entry per line."""
    path = tmp_path / name
    out = ["=== Board test, 2026-11-16 14:01:02 +0500 ===", "    0.0  # port opened"]
    for index, line in enumerate(lines):
        out.append(f"{0.4 + index * 0.1:7.1f}  {line}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(path)


def make_report(tmp_path: Path, scenario: str = "passed", **changes) -> Report:
    if scenario == "passed":
        findings = tuple(
            Finding(code, stage, "ok", title, detail, (proof,))
            for (code, stage, title, detail), proof in zip((
                ("OK", "psram", "The extra memory works",
                 "PSRAM 8 MiB: 10 patterns x 2 rounds, 0 errors, 41 MB/s"),
                ("OK", "iram", "The internal memory works", "Internal RAM: 0 errors"),
                ("OK", "flash", "The storage chip works",
                 "Flash 20 40 18, 16 MiB: read stable, 14 x 64 KiB blocks written and verified"),
                ("OK", "cpu", "Both processor cores work", "Core checksums match"),
                ("OK", "wifi", "The Wi-Fi radio works", "7 networks, best -48 dBm"),
            ), SLTEST_PASS[1:6]))
        return Report(
            mac="a4:e5:e8:12:34:56", verdict=Verdict.PASSED, kind="vendor_test",
            started_at="2026-11-16T14:01:02+05:00", finished_at="2026-11-16T14:03:10+05:00",
            board=board(), findings=findings,
            measurements={"psram_size": 8388608, "psram_mb_s": 41, "flash_id": "204018",
                          "flash_size": 16777216, "networks": 7, "best_rssi": -48,
                          "temp_c": 38, "chip_rev": "v0.2", "fw": "1.0.0"},
            efuse_before=efuses("blank"), efuse_after=efuses("blank"), efuses_unchanged=True,
            erased=True, keys_burned=False,
            console_log=console_log(tmp_path, SLTEST_PASS),
            tool_log=("esptool write-flash 0x0 bootloader.bin ...\nHash of data verified.",),
            image_sha256=dict(IMAGES), end_crc_ok=True, **changes)
    if scenario == "psram_errors":
        errors = (
            "SLTEST ERR stage=psram test=pattern_55 addr=3c812340 want=55555555 got=55515555",
            "SLTEST ERR stage=psram test=pattern_55 addr=3c812344 want=55555555 got=55515555",
            "SLTEST RESULT stage=psram result=fail code=PSRAM_DATA_ERRORS detail=pattern "
            "errors=1204 ms=9890",
        )
        lines = SLTEST_PASS[:1] + errors + (
            "SLTEST END result=fail failed=psram crc=5e6f7a8b", "SLTEST CLEAN result=ok")
        return Report(
            mac="a4:e5:e8:12:34:56", verdict=Verdict.FAILED, kind="vendor_test",
            started_at="2026-11-16T14:01:02+05:00", finished_at="2026-11-16T14:02:20+05:00",
            board=board(), findings=(Finding(
                "PSRAM_DATA_ERRORS", "psram", "fail",
                "The board's extra memory gives wrong data",
                "PSRAM pattern test: 1,204 errors, first at 0x3c812340 (wrote 0x55555555, "
                "read 0x55515555)", errors),),
            measurements={"psram_size": 8388608, "flash_id": "204018", "chip_rev": "v0.2"},
            efuse_before=efuses("blank"), efuse_after=efuses("blank"), efuses_unchanged=True,
            erased=True, keys_burned=False, console_log=console_log(tmp_path, lines),
            tool_log=(), image_sha256=dict(IMAGES), end_crc_ok=True,
            advice=Advice("PSRAM_DATA_ERRORS", "This board is faulty",
                          "Its extra memory gives wrong data.",
                          ("Do not use this board.", "Return it to the vendor with the "
                           "certificate.")), **changes)
    if scenario == "locked_psram":
        raw = A45788.read_text(encoding="utf-8").splitlines()
        proof = tuple(line for line in raw[107:] if "octal_psram" in line or "esp_psram" in line
                      or "cpu_start" in line or "Guru Meditation" in line)[-16:]
        return Report(
            mac="a4:57:88:aa:bb:cc", verdict=Verdict.FAILED, kind="locked_board",
            started_at="2026-11-16T14:01:02+05:00", finished_at="2026-11-16T14:02:32+05:00",
            board=board("a4:57:88:aa:bb:cc", sdm=True,
                        security=SecurityState.SECURED_STATE_LIFE),
            findings=(Finding(
                "PSRAM_STARTUP_FAULT", "startup", "fail",
                "The board's extra memory fails when it starts",
                "Crash right after PSRAM init on every start (octal PSRAM AP 64 Mbit found, "
                "LoadProhibited panic before the app), 2 restarts seen", proof),),
            measurements={"restarts": 2},
            efuse_before={}, efuse_after={}, efuses_unchanged=None, erased=None,
            keys_burned=True, console_log=console_log(tmp_path, raw, "console-a45788.log"),
            tool_log=(), image_sha256={}, end_crc_ok=None, **changes)
    if scenario == "settings_missing":
        lines = ("I (1203) sl_boot: settings missing in NVS, starting setup Wi-Fi",
                 "I (1210) sl_portal: hotspot open")
        return Report(
            mac="10:20:ba:75:70:48", verdict=Verdict.INCOMPLETE, kind="locked_board",
            started_at="2026-11-16T14:01:02+05:00", finished_at="2026-11-16T14:02:32+05:00",
            board=board("10:20:ba:75:70:48", sdm=True,
                        security=SecurityState.SECURED_STATE_LIFE),
            findings=(Finding("SETTINGS_MISSING", "startup", "fail",
                              "The board's settings are missing",
                              "The app started but found no settings; it opened its own Wi-Fi",
                              lines),),
            measurements={}, efuse_before={}, efuse_after={}, efuses_unchanged=None,
            erased=None, keys_burned=True, console_log=console_log(tmp_path, lines),
            tool_log=(), image_sha256={}, end_crc_ok=None, recovery="FIX_SETTINGS",
            advice=Advice("SETTINGS_MISSING", "The board's settings are missing",
                          "The board works but has no settings.",
                          ("Click Fix the settings and type them again.",)), **changes)
    raise ValueError(scenario)


def build(report, kind=None, **extra):
    return build_certificate(report, tester="Asad Ullah", app_version="1.2.0", now=NOW,
                             kind=kind, **extra)


# ------------------------------------------------------------------ data + text

def test_passed_certificate_says_fit_for_use(tmp_path):
    data = build(make_report(tmp_path), evidence_name="e.zip", evidence_sha256="ab" * 32)
    assert data.kind is CertificateKind.PASSED
    assert data.title == "Board test certificate — PASSED (fit for State Life use)"
    assert data.number == "SL-BT-20261116-140322-123456"
    assert data.issued == "2026-11-16 14:03:22 PKT (UTC+05:00)"
    assert data.verdict == "passed"
    text = certificate_text(data)
    for words in ("Tested by:", "Asad Ullah", "1.2.0", "1.0.0", "3" * 64, "a4:e5:e8:12:34:56",
                  "JEDEC ID 20 40 18, 16 MB", "Embedded PSRAM 8MB (AP_3v3)",
                  "revision v0.2", "eFuses unchanged:", "Yes — every eFuse",
                  "Not burned", "Test program removed:", "PSRAM speed", "41 MB/s",
                  "SHA-256:", "ab" * 32, "The full board output is in the evidence file.",
                  "Produced by the State Life Device Installer 1.2.0. Verify: the SHA-256 of "
                  "the evidence ZIP must match the value above."):
        assert words in text, words
    assert [row.result for row in data.tests] == ["PASS"] * 5
    # Proof lines carry their time from the run's console log.
    assert data.proof[0].text.startswith("SLTEST RESULT stage=psram")
    assert data.proof[0].at == "+0.5 s"
    assert data.keys_statement == ""


def test_vendor_return_for_psram_data_errors(tmp_path):
    data = build(make_report(tmp_path, "psram_errors"))
    assert data.kind is CertificateKind.VENDOR_RETURN
    assert data.title == "Hardware fault certificate — NOT FIT FOR USE"
    assert data.verdict_label.startswith("NOT FIT FOR USE")
    assert data.reason == "The board's extra memory gives wrong data"
    assert "1,204 errors, first at 0x3c812340" in data.technical
    codes = [row.code for row in data.tests]
    assert codes[0] == "PSRAM_DATA_ERRORS"
    assert codes.count("NOT_RUN") == 4  # iram, flash, cpu, wifi never ran
    assert any("got=55515555" in line.text for line in data.proof)
    assert data.keys_statement == ""  # a blank board: no keys to speak of


def test_locked_board_vendor_claim_states_keys_honestly(tmp_path):
    report = make_report(tmp_path, "locked_psram")
    data = build(report)
    assert data.kind is CertificateKind.VENDOR_RETURN
    assert data.keys_statement == (
        "This board was given State Life security keys during setup before the fault showed. "
        "The fault is in the board's hardware: the board's extra memory fails when it starts. "
        "The keys do not cause it.")
    text = certificate_text(data)
    assert "Not readable (secure download mode)" in text
    assert "Burned before this test" in text
    assert "Not used (the board was only watched while it started)" in text
    assert any("Guru Meditation" in line.text for line in data.proof)
    assert any("octal_psram: density" in line.text for line in data.proof)
    assert all(line.at for line in data.proof)  # every proof line found in the log


def test_keys_statement_only_for_hardware_codes(tmp_path):
    report = make_report(tmp_path, "locked_psram")
    other = replace(report, findings=(Finding("USB_DATA_GARBLED", "check", "fail", "Bad cable",
                                              "crc mismatch"),))
    assert build(other).keys_statement == ""
    assert certificate.is_hardware_code("PSRAM_HANG")
    assert certificate.is_hardware_code("FLASH_CHIP_UNSUPPORTED")
    assert not certificate.is_hardware_code("SETTINGS_MISSING")
    assert not certificate.is_hardware_code("WIFI_NO_NETWORKS")


def test_fault_report_for_recoverable_board(tmp_path):
    report = make_report(tmp_path, "settings_missing")
    data = build(report)
    assert data.kind is CertificateKind.FAULT_REPORT
    assert data.title == "Board fault report"
    assert data.verdict == "incomplete"
    assert data.verdict_label == "BOARD CAN BE RECOVERED"
    assert data.next_steps[0].endswith("use Fix the settings.")
    assert "Click Fix the settings and type them again." in data.next_steps
    assert "WHAT TO DO NOW" in certificate_text(data)
    # Any report may get a fault report, also a passed or failed one.
    assert build(make_report(tmp_path, "passed"), CertificateKind.FAULT_REPORT).title == \
        "Board fault report"


def test_kind_must_fit_the_verdict(tmp_path):
    with pytest.raises(ValueError):
        build(make_report(tmp_path, "psram_errors"), CertificateKind.PASSED)
    with pytest.raises(ValueError):
        build(make_report(tmp_path, "passed"), CertificateKind.VENDOR_RETURN)
    with pytest.raises(ValueError):
        build(make_report(tmp_path, "settings_missing"), "vendor_return")


def test_changed_efuses_are_shouted(tmp_path):
    after = efuses("blank")
    after["SECURE_BOOT_EN"] = dict(after["SECURE_BOOT_EN"], value=True)
    report = make_report(tmp_path, "psram_errors", )
    report = replace(report, efuse_after=after, efuses_unchanged=False)
    text = certificate_text(build(report))
    assert "NO — eFuses CHANGED during the test (SECURE_BOOT_EN)" in text
    assert certificate.efuse_differences(efuses("blank"), after) == ["SECURE_BOOT_EN"]


def test_proof_is_capped_and_redacted(tmp_path):
    lines = tuple(f"SLTEST ERR stage=psram test=t{i} addr=0 want=0 got=1" for i in range(60))
    report = replace(make_report(tmp_path, "psram_errors"), findings=(
        Finding("PSRAM_DATA_ERRORS", "psram", "fail", "t", "d",
                lines + ("wifi_password=hunter22",)),))
    data = build(report)
    assert len(data.proof) == certificate.PROOF_LINES_MAX
    report = replace(report, findings=(Finding("X", "psram", "fail", "t", "d",
                                               ("wifi_password=hunter22",)),))
    assert "hunter22" not in certificate_text(build(report, CertificateKind.FAULT_REPORT))


def test_no_console_log_still_builds(tmp_path):
    report = replace(make_report(tmp_path, "passed"), console_log=None,
                     findings=tuple(replace(f, proof=()) for f in
                                    make_report(tmp_path).findings))
    data = build(report)
    assert data.proof == ()
    assert "No board output was kept." in certificate_text(data)


def test_number_uses_last_six_hex_of_mac():
    assert certificate.certificate_number(datetime(2026, 1, 2, 3, 4, 5), "A4:57:88:AA:BB:CC") \
        == "SL-BT-20260102-030405-aabbcc"


def test_engine_stays_qt_free():
    source = Path(certificate.__file__).read_text(encoding="utf-8")
    top = source.split("def render_pdf", 1)[0]
    assert not re.search(r"^from PySide6|^import PySide6", top, re.MULTILINE)


# ------------------------------------------------------------------ PDF

@pytest.fixture
def offscreen(qapp):
    """The session's QApplication (pytest-qt), as in the app: render_pdf makes only a
    QGuiApplication when there is none, and the screen tests later need widgets."""
    return qapp


@pytest.mark.parametrize("scenario", ["passed", "psram_errors", "locked_psram",
                                      "settings_missing"])
def test_pdf_renders(tmp_path, offscreen, scenario):
    data = build(make_report(tmp_path, scenario), evidence_name="board-test.zip",
                 evidence_sha256="cd" * 32)
    path = certificate.write_pdf(data, tmp_path / f"{scenario}.pdf")
    body = path.read_bytes()
    assert body.startswith(b"%PDF")
    assert 10_000 < len(body) < 3_000_000
    assert not list(tmp_path.glob("*.part"))


def test_html_escapes_board_text(tmp_path):
    report = replace(make_report(tmp_path, "psram_errors"), findings=(
        Finding("PSRAM_DATA_ERRORS", "psram", "fail", "<b>x</b>", "a & b", ("<script>",)),))
    html_text = certificate.certificate_html(build(report))
    assert "<script>" not in html_text
    assert "&lt;b&gt;x&lt;/b&gt;" in html_text


def test_pdf_renders_on_a_worker_thread(tmp_path, offscreen):
    # The app saves certificates from a worker thread (ui/runner.py).
    data = build(make_report(tmp_path, "locked_psram"), evidence_name="z.zip",
                 evidence_sha256="ef" * 32)
    errors = []

    def work():
        try:
            certificate.write_pdf(data, tmp_path / "thread.pdf")
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(60)
    assert not thread.is_alive() and not errors
    assert (tmp_path / "thread.pdf").read_bytes().startswith(b"%PDF")


def test_long_proof_runs_onto_more_pages(tmp_path, offscreen):
    lines = tuple(f"SLTEST ERR stage=psram test=t{i} addr=3c8{i:05x} want=55555555 "
                  f"got=55515555 " + "x" * 120 for i in range(40))
    report = replace(make_report(tmp_path, "psram_errors"), findings=(
        Finding("PSRAM_DATA_ERRORS", "psram", "fail", "t", "d", lines),))
    path = certificate.write_pdf(build(report), tmp_path / "long.pdf")
    assert len(re.findall(rb"/Type\s*/Page(?![s\w])", path.read_bytes())) >= 2


# ------------------------------------------------------- review fixes (16 Nov)

def watched_ok(tmp_path) -> Report:
    """Check a failed board on an SDM board that starts normally: watched, never tested
    (the watch always records restarts, so the measurements are never empty)."""
    return replace(make_report(tmp_path, "locked_psram"), verdict=Verdict.PASSED,
                   findings=(Finding("BOARD_STARTS_NORMALLY", "startup", "ok",
                                     "The board starts normally",
                                     "The application started and connected to ADD.", ()),),
                   measurements={"restarts": 0, "flash_id": None, "chip_revision": None})


def test_a_watched_locked_board_is_never_said_to_have_passed_tests(tmp_path):
    report = watched_ok(tmp_path)
    assert certificate.only_watched(report)
    data = build(report, CertificateKind.FAULT_REPORT)
    assert data.verdict_label == "NO HARDWARE FAULT FOUND"
    assert data.reason == "The board starts normally. No hardware fault was found."
    text = certificate_text(data)
    assert "Every test passed" not in text and "FIT FOR STATE LIFE USE" not in text


def test_a_tested_key_burned_board_still_says_its_tests_passed(tmp_path):
    report = replace(make_report(tmp_path), kind="locked_board", keys_burned=True,
                     board=board(security=SecurityState.KEY_BURNED_NOT_STARTED))
    assert not certificate.only_watched(report)
    assert build(report).verdict_label == "PASSED — FIT FOR STATE LIFE USE"


@pytest.mark.parametrize("security", [SecurityState.SECURED_OTHER,
                                      SecurityState.UNSECURED_USED, SecurityState.UNKNOWN])
def test_no_state_life_keys_claim_for_boards_we_never_keyed(tmp_path, security):
    report = replace(make_report(tmp_path, "locked_psram"), keys_burned=False,
                     board=board("a4:57:88:aa:bb:cc", sdm=True, security=security))
    data = build(report, CertificateKind.VENDOR_RETURN)
    assert data.keys_statement == ""
    assert "State Life security keys during setup" not in certificate_text(data)


def test_the_keys_row_claims_no_efuse_change_only_when_proven(tmp_path):
    def key_row(report):
        rows = dict(build(report, CertificateKind.FAULT_REPORT).security)
        return rows["State Life security keys"]

    proven = make_report(tmp_path)
    assert "changed no eFuse" in key_row(proven)
    unread = replace(make_report(tmp_path, "psram_errors"), efuses_unchanged=None,
                     efuse_after={}, erased=False)
    assert key_row(unread) == "Not burned"
    changed = replace(make_report(tmp_path, "psram_errors"), efuses_unchanged=False)
    assert key_row(changed) == "Not burned"


def test_a_watched_locked_board_prints_no_test_program_hashes(tmp_path):
    # Found on board ac:27:6e:a4:57:88 (16 Nov): the engine fills the pinned image hashes on
    # every report, but a watched board never had the test program written.
    from dataclasses import replace
    report = replace(make_report(tmp_path, "locked_psram"),
                     image_sha256={"board-test.bin": "ab" * 32})
    data = build(report)
    assert data.test_program == "Not used (the board was only watched while it started)"
    assert data.image_sha256 == ()
    assert "ab" * 32 not in certificate_text(data)
