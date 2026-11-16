"""Scripted board consoles for the board test: the fake services (fakes.py), the
screenshots and the engine tests use them.

Each script is a list of (seconds after the start, line) as the board prints it: SLTEST
lines in the protocol of board_test_firmware (with a correct END crc), ESP-IDF 5.5.3 boot
lines, and short excerpts of the real faulty boards' consoles in
tests/core/fixtures/boards (ac:27:6e:a4:e5:e8 hangs after "Multicore app" in its PSRAM
start-up; ac:27:6e:a4:57:88 passes IDF's memtest once and then crashes before its program).
They are kept here, outside the screens' word checks, because they are the board's words.
"""
from __future__ import annotations

import zlib
from typing import Dict, List, Optional, Sequence, Tuple

Script = List[Tuple[float, str]]

PSRAM_SIZE = 8 * 1024 * 1024
FLASH_SIZE = 16 * 1024 * 1024


def sltest(what: str, **fields: object) -> str:
    """One test program line: "SLTEST <WHAT> key=value ..."."""
    return " ".join(["SLTEST", what] + [f"{key}={value}" for key, value in fields.items()])


def end_crc(lines: Sequence[str]) -> str:
    """The END line's crc: CRC-32 (esp_rom_crc32_le with 0) of the lines joined by "\\n"."""
    return f"{zlib.crc32(chr(10).join(lines).encode('utf-8')) & 0xFFFFFFFF:08x}"


def _boot(mac: str) -> Script:
    """ROM and second-stage bootloader lines after the RTC watchdog reset that starts it."""
    return [
        (0.30, "ESP-ROM:esp32s3-20210327"),
        (0.30, "rst:0x10 (RTCWDT_RTC_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"),
        (0.32, "I (27) boot: ESP-IDF v5.5.3 2nd stage bootloader"),
        (0.33, "I (31) boot: chip revision: v0.2"),
        (0.36, "I (60) boot: Loaded app from partition at offset 0x10000"),
        (0.40, "I (467) octal_psram: vendor id    : 0x0d (AP)"),
        (0.40, "I (468) octal_psram: density      : 0x03 (64 Mbit)"),
        (0.41, "I (511) esp_psram: Found 8MB PSRAM device"),
        (0.42, "I (517) cpu_start: Multicore app"),
        (0.60, "I (700) app_init: Project name:     sl_board_test"),
        (0.60, "I (701) app_init: App version:      1.0.0"),
    ]


def _begin(mac: str, *, psram_size: int = PSRAM_SIZE) -> str:
    return sltest("BEGIN", proto=1, fw="1.0.0", mac=mac, chip_rev="v0.2", cores=2,
                  idf="v5.5.3", flash_id="c84018", flash_size=FLASH_SIZE, psram_size=psram_size)


_STAGE_RESULTS: Dict[str, Dict[str, object]] = {
    "psram": {"result": "ok", "code": "OK", "detail": "8388608", "errors": 0, "ms": 21400,
              "mb_s": 41},
    "iram": {"result": "ok", "code": "OK", "detail": "319488", "errors": 0, "ms": 310},
    "flash": {"result": "ok", "code": "OK", "detail": "16777216", "errors": 0, "ms": 18200},
    "cpu": {"result": "ok", "code": "OK", "detail": "5a17c3e9", "errors": 0, "ms": 120,
            "temp_c": 41},
    "wifi": {"result": "ok", "code": "OK", "detail": "scan", "errors": 0, "ms": 2600,
             "networks": 6, "best_rssi": -48},
}
_STAGE_SECONDS = {"psram": 6.0, "iram": 0.5, "flash": 5.0, "cpu": 0.5, "wifi": 3.0}
STAGES = ("psram", "iram", "flash", "cpu", "wifi")


def program_run(mac: str, *, results: Optional[Dict[str, Dict[str, object]]] = None,
             errors: Optional[Dict[str, Sequence[Dict[str, object]]]] = None,
             crc: Optional[str] = None, clean: str = "ok", stop_after: Optional[str] = None,
             boot: bool = True) -> Script:
    """A whole test program run: boot lines, BEGIN, ALIVE beats, a RESULT per stage, END
    (with the right crc unless ``crc`` is given) and CLEAN. ``results`` replaces stages'
    RESULT fields, ``errors`` adds their ERR lines; ``stop_after``: the last stage printed
    (an ALIVE of it, then nothing: a hang)."""
    script: Script = list(_boot(mac)) if boot else []
    moment = 1.5
    lines: List[str] = []

    def say(text: str, gap: float = 0.4) -> None:
        nonlocal moment
        moment += gap
        script.append((moment, text))
        lines.append(text)

    say(_begin(mac), 0.0)
    failed = []
    for stage in STAGES:
        beats = max(2, int(_STAGE_SECONDS[stage] / 0.4))
        for step in range(1, beats + 1):
            say(sltest("ALIVE", stage=stage, step=step, of=beats,
                       at=f"0x{0x3c000000 + step * 0x10000:08x}"))
            if stop_after == stage and step == beats // 2:
                return script
        for item in (errors or {}).get(stage, ())[:8]:
            say(sltest("ERR", stage=stage, **item), 0.01)
        fields = dict(_STAGE_RESULTS[stage])
        fields.update((results or {}).get(stage, {}))
        if fields.get("result") == "fail":
            failed.append(stage)
        say(sltest("RESULT", stage=stage, **fields))
    say(sltest("END", result="fail" if failed else "ok", failed=",".join(failed) or "none",
               crc=crc or end_crc(lines)))
    script.append((moment + 1.2, sltest("CLEAN", result=clean)))
    return script


def passing_run(mac: str) -> Script:
    return program_run(mac)


def psram_errors_run(mac: str) -> Script:
    """The extra memory gives wrong data: 1,204 errors, the first eight as ERR lines."""
    errors = [{"test": "pattern_55", "addr": f"0x{0x3c812340 + i * 0x40:08x}",
               "want": "0x55555555", "got": "0x55515555"} for i in range(8)]
    return program_run(mac, results={"psram": {"result": "fail", "code": "PSRAM_DATA_ERRORS",
                                            "detail": "pattern_55", "errors": 1204}},
                    errors={"psram": errors})


# The a4:e5:e8 board (fixtures/boards/psram-fault-hang-a4e5e8-console.txt): every start
# stops after "Multicore app" in IDF's PSRAM start-up and then panics.
PSRAM_HANG_A4E5E8: Tuple[str, ...] = (
    "ESP-ROM:esp32s3-20210327",
    "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (63) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (63) boot: chip revision: v0.2",
    "I (444) boot: Loaded app from partition at offset 0x10000",
    "I (467) octal_psram: vendor id    : 0x0d (AP)",
    "I (467) octal_psram: dev id       : 0x02 (generation 3)",
    "I (468) octal_psram: density      : 0x03 (64 Mbit)",
    "I (470) octal_psram: good-die     : 0x01 (Pass)",
    "I (506) MSPI Timing: Enter psram timing tuning",
    "I (511) esp_psram: Found 8MB PSRAM device",
    "I (514) esp_psram: Speed: 80MHz",
    "I (517) cpu_start: Multicore app",
    "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
    "Core  0 register dump:",
    "PC      : 0x403828a1  PS      : 0x00060034  A0      : 0x80376ab5  A1      : 0x3fceb0a0",
    "EXCVADDR: 0x00000004  LBEG    : 0x400570e8  LEND    : 0x400570f3  LCOUNT  : 0xffffffff",
)

# The a4:57:88 board (fixtures/boards/psram-fault-crash-a45788-console.txt), locked by its
# setup: IDF's memtest passes once, then the starts crash before the program, again and
# again (a crash keeps USB up, so the restart shows as "Rebooting..." and a ROM line).
LOCKED_PSRAM_A45788: Tuple[str, ...] = (
    "ESP-ROM:esp32s3-20210327",
    "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (63) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (444) boot: Loaded app from partition at offset 0x2a0000",
    "I (467) octal_psram: vendor id    : 0x0d (AP)",
    "I (468) octal_psram: density      : 0x03 (64 Mbit)",
    "I (511) esp_psram: Found 8MB PSRAM device",
    "I (517) cpu_start: Multicore app",
    "I (943) esp_psram: SPI SRAM memory test OK",
    "I (957) app_init: Project name:     zone_lite",
    "I (961) app_init: App version:      2.8.1",
    "Guru Meditation Error: Core  0 panic'ed (IllegalInstruction). Exception was unhandled.",
    "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
    "Rebooting...",
    "ESP-ROM:esp32s3-20210327",
    "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (63) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (444) boot: Loaded app from partition at offset 0x2a0000",
    "I (467) octal_psram: vendor id    : 0x0d (AP)",
    "I (511) esp_psram: Found 8MB PSRAM device",
    "I (517) cpu_start: Multicore app",
    "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
    "Rebooting...",
)

# A finished board whose encrypted settings are missing: it starts and stops (solid red).
LOCKED_SETTINGS_MISSING: Tuple[str, ...] = (
    "ESP-ROM:esp32s3-20210327",
    "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (63) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (444) boot: Loaded app from partition at offset 0x2a0000",
    "I (511) esp_psram: Found 8MB PSRAM device",
    "I (943) esp_psram: SPI SRAM memory test OK",
    "I (961) app_init: App version:      2.8.1",
    "I (1200) main_task: Calling app_main()",
    "W (1600) zone_config: No encrypted per-device provisioning namespace",
    "E (1610) zone_lite: Encrypted per-device provisioning is required",
)

# A finished board that starts and reaches head office: not faulty.
LOCKED_STARTS_NORMALLY: Tuple[str, ...] = (
    "ESP-ROM:esp32s3-20210327",
    "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (444) boot: Loaded app from partition at offset 0x2a0000",
    "I (511) esp_psram: Found 8MB PSRAM device",
    "I (961) app_init: App version:      2.8.1",
    "I (1200) main_task: Calling app_main()",
    "I (1600) zone_config: Loaded per-device provisioning for zone=LF-ZONE-GUJRAT-01",
    "I (1700) zone_lite: Zone Lite starting zone=LF-ZONE-GUJRAT-01 device_id=1",
    "I (2900) zone_lite: Storage ready; loaded 0 known event UIDs",
    "I (3000) zone_lite: Connecting to Wi-Fi SSID SiteNet",
    "I (5200) zone_lite: Wi-Fi connected with IP 192.168.1.50",
    "I (9100) add_connector: ADD live control channel connected",
)


def timed(lines: Sequence[str], start: float = 0.3, gap: float = 0.05) -> Script:
    """``lines`` printed one after another from ``start``."""
    return [(start + index * gap, line) for index, line in enumerate(lines)]
