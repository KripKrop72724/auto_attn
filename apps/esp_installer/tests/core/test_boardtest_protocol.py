"""The test program's console lines, made from the firmware's own format strings, are what
the app parses and judges (core/console.py ConsoleParser, core/boardtest.py TestJudge).

The format strings are read from board_test_firmware/main/*.c at test time and filled
with C printf rules; the END crc is computed the way the firmware does it (sl_out.c: a
chained esp_rom_crc32_le, reimplemented here from ESP-IDF's reference code, not zlib).
So a change of a field, a code or the crc on either side fails here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from sl_installer.core import boardtest, texts
from sl_installer.core.api import BoardTestVerdict, ConsoleKind
from sl_installer.core.console import ConsoleParser

from .support import MAC
from .test_boardtest import judged

FIRMWARE = Path(__file__).resolve().parents[2] / "board_test_firmware" / "main"

pytestmark = pytest.mark.skipif(not FIRMWARE.is_dir(),
                                reason="board_test_firmware sources not in this checkout")

_LITERALS = re.compile(r'((?:"(?:[^"\\]|\\.)*"\s*)+)')
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_C_CONVERSION = re.compile(r"%(0?\d*)l?([dusx])")
LINE_MAX_LEN = 240          # sl_out.c


def _sources() -> Dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(FIRMWARE.glob("*.c"))}


def _first_literal(text: str, start: int) -> str:
    found = _LITERALS.match(text, start)
    assert found, text[start:start + 80]
    return "".join(_STRING.findall(found.group(1)))


def firmware_formats() -> List[str]:
    """Every sl_line() format string of the firmware (without the "SLTEST " sl_line adds)."""
    formats: List[str] = []
    for text in _sources().values():
        for call in re.finditer(r"\bsl_line\(\s*", text):
            if text[call.start() - 5:call.start()] == "void ":
                continue    # the definition in sl_out.c
            formats.append(_first_literal(text, call.end()))
    return formats


def end_format() -> str:
    text = _sources()["sl_out.c"]
    found = re.search(r'snprintf\(buf, sizeof\(buf\),\s*("SLTEST END [^"]*")', text)
    assert found, "sl_end's format string moved"
    return _first_literal(found.group(1), 0)


def c_format(fmt: str, *args: object) -> str:
    """printf with the conversions the firmware uses (%d %u %lu %ld %s %08lx %06lx %02x)."""
    python = _C_CONVERSION.sub(lambda m: "%" + m.group(1) + ("d" if m.group(2) == "u"
                                                              else m.group(2)), fmt)
    return python % args


# ESP-IDF components/esp_rom/patches/esp_rom_crc.c: crc32_le inverts on entry and exit
# around the reflected 0xEDB88320 table.
_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _TABLE.append(_c)


def esp_rom_crc32_le(crc: int, data: bytes) -> int:
    crc = ~crc & 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return ~crc & 0xFFFFFFFF


class Firmware:
    """sl_out.c's emit()/sl_end(): the lines as printed, with the firmware's own crc."""

    def __init__(self) -> None:
        self.formats = firmware_formats()
        self.lines: List[str] = []
        self.crc = 0
        self.count = 0
        self.closed = False

    def fmt(self, prefix: str) -> str:
        matches = [f for f in self.formats if f.startswith(prefix)]
        exact = [f for f in matches if f == prefix]
        assert len(exact or matches) == 1, (prefix, matches)
        return (exact or matches)[0]

    def _emit(self, text: str, in_crc: bool) -> None:
        if in_crc and not self.closed:
            if self.count:
                self.crc = esp_rom_crc32_le(self.crc, b"\n")
            self.crc = esp_rom_crc32_le(self.crc, text.encode("ascii"))
            self.count += 1
        self.lines.append(text + "\r\n")     # the console turns "\n" into "\r\n"

    def line(self, prefix: str, *args: object) -> str:
        text = ("SLTEST " + c_format(self.fmt(prefix), *args))[:LINE_MAX_LEN - 1]
        self._emit(text, True)
        return text

    def end(self, failed: str) -> None:
        crc = self.crc if self.count else 0
        self.closed = True
        self._emit(c_format(end_format(), "ok" if failed == "none" else "fail", failed, crc),
                   False)

    # --- what app_main prints, stage by stage

    def begin(self, mac: str = MAC) -> None:
        octets = [int(part, 16) for part in mac.split(":")]
        self.line("BEGIN", 1, "1.0.0", *octets, 0, 2, 2, "v5.5.3", 0x204018,
                  16 * 1024 * 1024, 8 * 1024 * 1024)

    def alive(self, stage: str, step: int, of: int, at: int = 0) -> None:
        if at:
            self.line("ALIVE stage=%s step=%d of=%d at=0x", stage, step, of, at)
        else:
            self.line("ALIVE stage=%s step=%d of=%d at=0", stage, step, of)

    def err(self, stage: str, test: str, addr: int, want: int, got: int) -> None:
        self.line("ERR stage=", stage, test, addr, want, got)

    def psram_ok(self) -> None:
        self.line("RESULT stage=psram result=ok", 21400, 8 * 1024 * 1024, 7_864_320, 2, 41)

    def psram_errors(self, errors: int) -> None:
        self.line("RESULT stage=psram result=fail code=PSRAM_DATA_ERRORS", "pattern_55",
                  errors, 21000, 8 * 1024 * 1024, 7_864_320, 2)

    def iram_ok(self) -> None:
        self.line("RESULT stage=iram result=ok", 310, 319_488, 120)

    def flash(self, result: str = "ok", code: str = "OK", detail: str = "all_blocks_ok",
              errors: int = 0, flash_id: int = 0x204018) -> None:
        self.line("RESULT stage=flash", result, code, detail, errors, 18200, flash_id,
                  16 * 1024 * 1024, 14)

    def cpu_ok(self, temp: str = "41.5") -> None:
        self.line("RESULT stage=cpu result=ok", 120, temp, 0x5A17C3E9)

    def wifi_ok(self) -> None:
        self.line("RESULT stage=wifi result=ok", 2900, 6, -48)

    def wifi_none(self) -> None:
        self.line("RESULT stage=wifi result=warn", 6100)

    def wifi_failed(self, step: str, error: str) -> None:
        detail = re.sub(r"[^a-z0-9]", "_", f"{step}_{error}".lower())   # err_detail()
        self.line("RESULT stage=wifi result=fail", detail, 40)

    def clean(self, ok: bool = True) -> None:
        self.line("CLEAN result=", "ok" if ok else "fail")


def full_run(fw: Firmware, *, psram_errors: int = 0, flash: Optional[Tuple] = None,
             wifi: str = "ok", temp: str = "41.5") -> Firmware:
    fw.begin()
    failed: List[str] = []
    fw.alive("psram", 0, 20)
    fw.alive("psram", 3, 20, 0x3C812340)
    if psram_errors:
        for index in range(min(psram_errors, 8)):
            fw.err("psram", "pattern_55", 0x3C812340 + 4 * index, 0x55555555, 0x55515555)
        fw.psram_errors(psram_errors)
        failed.append("psram")
    else:
        fw.psram_ok()
    fw.alive("iram", 0, 10)
    fw.iram_ok()
    fw.alive("flash", 0, 3)
    fw.alive("flash", 2, 3, 0x00200000)
    if flash:
        fw.flash(*flash)
        failed.append("flash")
    else:
        fw.flash()
    fw.alive("cpu", 0, 1)
    fw.cpu_ok(temp)
    fw.alive("wifi", 0, 3)
    if wifi == "ok":
        fw.wifi_ok()
    elif wifi == "none":
        fw.wifi_none()
    else:
        fw.wifi_failed("init", "ESP_ERR_NO_MEM")
        failed.append("wifi")
    fw.end(",".join(failed) or "none")
    fw.clean()
    return fw


def parsed(lines: List[str]):
    parser = ConsoleParser(MAC)
    return [parser.parse(line) for line in lines]


# ------------------------------------------------------------------ the tests

def test_the_crc_reimplementation_is_the_standard_crc32():
    import zlib

    assert f"{esp_rom_crc32_le(0, b'123456789'):08x}" == "cbf43926"
    sample = b"SLTEST BEGIN a\nSLTEST ALIVE b"
    chained = esp_rom_crc32_le(esp_rom_crc32_le(esp_rom_crc32_le(0, b"SLTEST BEGIN a"),
                                                b"\n"), b"SLTEST ALIVE b")
    assert chained == zlib.crc32(sample)


def test_every_firmware_line_kind_is_known():
    kinds = {fmt.split(" ", 1)[0] for fmt in firmware_formats()}
    assert kinds == {"BEGIN", "ALIVE", "ERR", "RESULT", "CLEAN"}
    assert end_format().startswith("SLTEST END result=%s failed=%s crc=%08")


def test_no_firmware_field_name_would_be_hidden_by_the_log_redactor():
    names = set()
    for fmt in firmware_formats() + [end_format()]:
        names.update(re.findall(r"(?:^| )([a-z_][a-z0-9_]*)=", fmt))
    assert names
    for name in names:
        for word in ("pass", "key", "token", "secret"):
            assert word not in name, name


def test_every_firmware_result_code_has_its_own_advice():
    codes = set()
    for fmt in firmware_formats():
        codes.update(re.findall(r"code=([A-Z_]+[A-Z])", fmt))
    codes.update(re.findall(r'result\("\w+", "([A-Z_]+)"', _sources()["flash_test.c"]))
    codes.discard("OK")
    assert {"PSRAM_NOT_FOUND", "PSRAM_WRONG_SIZE", "PSRAM_TOO_SMALL", "PSRAM_DATA_ERRORS",
            "IRAM_DATA_ERRORS", "FLASH_ID_CHANGED", "FLASH_WRONG_SIZE",
            "FLASH_TEST_AREA_MISSING", "FLASH_READ_UNSTABLE", "FLASH_WRITE_ERRORS",
            "CPU_MISMATCH", "WIFI_RADIO_FAILED", "WIFI_NO_NETWORKS"} == codes
    for code in codes:
        assert texts.board_test_advice(code).code == code, code


def test_a_good_firmware_run_parses_with_a_matching_crc_and_passes():
    fw = full_run(Firmware())
    events = parsed(fw.lines)
    assert all(event.kind is ConsoleKind.SELF_TEST for event in events)
    begin = events[0].data
    assert begin["mac"] == MAC and begin["flash_id"] == "204018"
    assert begin["chip_rev"] == "v0.2" and begin["psram_size"] == str(8 * 1024 * 1024)
    end = next(event for event in events if event.data["sltest"] == "END")
    assert end.data["crc_ok"] == "1"
    verdict = judged(fw.lines)
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.end_crc_ok
    assert verdict.clean_ok and verdict.program_done
    assert [(f.stage, f.code, f.result) for f in verdict.findings] == [
        (stage, "OK", "ok") for stage in boardtest.STAGES]
    m = verdict.measurements
    assert m["test_program_version"] == "1.0.0" and m["flash_id"] == "204018"
    assert m["flash_id_read"] == "204018" and m["flash_size"] == 16 * 1024 * 1024
    assert m["psram_size"] == 8 * 1024 * 1024 and m["psram_mb_s"] == 41
    assert m["networks"] == 6 and m["best_rssi"] == -48 and m["temp_c"] == 41.5


def test_no_temperature_reading_still_passes():
    verdict = judged(full_run(Firmware(), temp="na").lines)
    assert verdict.verdict is BoardTestVerdict.PASSED
    assert verdict.measurements["temp_c"] == "na"


def test_no_wifi_networks_is_a_warning_and_still_passes():
    verdict = judged(full_run(Firmware(), wifi="none").lines)
    assert verdict.verdict is BoardTestVerdict.PASSED
    assert [f.code for f in verdict.findings if f.result == "warn"] == ["WIFI_NO_NETWORKS"]


def test_firmware_psram_errors_fail_with_the_err_lines_as_proof():
    verdict = judged(full_run(Firmware(), psram_errors=1204).lines)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_DATA_ERRORS"
    assert verdict.end_crc_ok
    finding = verdict.findings[0]
    assert "1,204 errors" in finding.detail and "0x3c812340" in finding.detail
    assert "wrote 0x55555555, read 0x55515555" in finding.detail
    assert len([line for line in finding.proof if line.startswith("SLTEST ERR")]) == 8


@pytest.mark.parametrize("flash, verdict_, code", [
    (("fail", "FLASH_ID_CHANGED", "id_read_differs", 1, 0xC84018), BoardTestVerdict.FAILED,
     "FLASH_ID_CHANGED"),
    (("fail", "FLASH_WRITE_ERRORS", "write", 3), BoardTestVerdict.FAILED, "FLASH_WRITE_ERRORS"),
    (("fail", "FLASH_TEST_AREA_MISSING", "no_fltest_partition", 0),
     BoardTestVerdict.INCOMPLETE, "FLASH_TEST_AREA_MISSING"),
])
def test_firmware_flash_results_are_judged(flash, verdict_, code):
    verdict = judged(full_run(Firmware(), flash=flash).lines)
    assert verdict.verdict is verdict_ and verdict.code == code and verdict.end_crc_ok
    assert verdict.advice.code == code


def test_a_firmware_radio_failure_names_the_step_and_error():
    verdict = judged(full_run(Firmware(), wifi="fail").lines)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "WIFI_RADIO_FAILED"
    finding = next(f for f in verdict.findings if f.stage == "wifi")
    assert "init_esp_err_no_mem" in finding.detail


def test_a_lost_firmware_line_spoils_the_crc():
    fw = full_run(Firmware())
    lost = [line for line in fw.lines if "at=0x00200000" not in line]
    assert len(lost) == len(fw.lines) - 1
    verdict = judged(lost)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "USB_DATA_GARBLED"
    assert verdict.end_crc_ok is False


def test_the_clean_line_is_not_part_of_the_crc():
    fw = full_run(Firmware())
    changed = [line.replace("CLEAN result=ok", "CLEAN result=fail") for line in fw.lines]
    events = parsed(changed)
    end = next(event for event in events if event.data["sltest"] == "END")
    assert end.data["crc_ok"] == "1"
