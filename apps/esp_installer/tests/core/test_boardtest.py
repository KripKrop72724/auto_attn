"""The board hardware test (core/boardtest.py): its own plan and safety checker, the
judge on scripted consoles and on the real faulty boards' consoles, whole runs against a
simulated board (pass, faults, hang, no start, cancel, cleanup), the failed-board check and
the installer's gate."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

import pytest

from sl_installer.core import boardtest, boardtest_scripts as scripts, texts
from sl_installer.core.api import (BoardInfo, BoardTestVerdict, CancelToken, ConsoleKind,
                                   HardwareVerdict, InstallerError, InstallMode, ReplugPhase,
                                   SecurityState)
from sl_installer.core.boardtest import (BoardTester, Purpose, TestCall, TestJudge,
                                         board_problems, board_test_problem, build_plan,
                                         call_problems, check_board_test, efuse_differences,
                                         safety_problems)
from sl_installer.core.console import ConsoleParser, ConsoleWatch
from sl_installer.core.esptool_runner import ESPEFUSE, ESPTOOL, ToolCall

from . import support
from .support import MAC, OTHER_MAC, PORT, FakeBoard, FakeClock, FakeRunner, FakeUsb, SerialScript

NEVER = ("burn-efuse", "burn-key", "burn-key-digest", "burn-block-data", "burn-bit",
         "burn-custom-mac", "write-protect-efuse", "read-protect-efuse", "set-flash-voltage")


def blank_board(**changes) -> BoardInfo:
    info = BoardInfo(PORT, MAC, "ESP32-S3", 16, 8, HardwareVerdict.OK, SecurityState.BLANK,
                     False, {"secure_boot": False, "flash_encryption": False},
                     flash_id="c8 40 18", chip_revision=2)
    return replace(info, **changes)


def key_burned_board(**changes) -> BoardInfo:
    return blank_board(security=SecurityState.KEY_BURNED_NOT_STARTED, **changes)


def sdm_board(**changes) -> BoardInfo:
    info = BoardInfo(PORT, MAC, "ESP32-S3", None, None, HardwareVerdict.NOT_CHECKED,
                     SecurityState.SECURED_STATE_LIFE, True, {"flags": "0x000000c5"})
    return replace(info, **changes)


# --------------------------------------------------------------- the plan

@pytest.fixture
def images(tmp_path):
    folder, pinned = support.make_test_images(tmp_path / "images")
    return [(image, (folder / image.name).resolve()) for image in pinned]


def every_call(images, tmp_path):
    return build_plan(PORT, images, tmp_path)


def test_the_plan_never_burns_or_protects_an_efuse(images, tmp_path):
    plan = every_call(images, tmp_path)
    assert [planned.purpose for planned in plan] == [
        Purpose.IDENTITY, Purpose.EFUSE_BEFORE, Purpose.WRITE_IMAGES, Purpose.START,
        Purpose.IDENTITY, Purpose.ERASE_TEST_AREA, Purpose.ERASE_CHIP, Purpose.EFUSE_AFTER]
    for planned in plan:
        args = planned.call.args
        assert not [arg for arg in args if arg in NEVER or arg.startswith("burn")], args
        assert "--do-not-confirm" not in args and "--force" not in args
        if planned.call.tool == ESPEFUSE:
            assert args[args.index("--before") + 2:] [:1] == ("summary",)
            assert "summary" in args and args[-4:-1] == ("--format", "json", "--file")
    assert safety_problems(blank_board(), plan, images) == []


def test_the_only_booting_reset_is_the_start_call(images, tmp_path):
    for planned in every_call(images, tmp_path):
        args = planned.call.args
        if planned.call.tool != ESPTOOL:
            assert "--after" not in args
            continue
        after = args[args.index("--after") + 1]
        assert after == ("watchdog-reset" if planned.purpose is Purpose.START else "no-reset")
    start = boardtest.start_call(PORT).call.args
    # Only the volatile RTC force-download flag is touched, never an eFuse register.
    assert start[-4:] == ("write-mem", "0x6000812c", "0x0", "0x1")


def test_the_write_puts_exactly_the_three_images_at_their_offsets(images, tmp_path):
    planned = boardtest.write_call(PORT, images)
    args = planned.call.args
    tail = args[args.index("write-flash") + 1:]
    assert tail[:6] == ("--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep")
    assert tail[6:] == ("0x0", str(images[0][1]), "0x8000", str(images[1][1]),
                        "0x10000", str(images[2][1]))
    erase = boardtest.erase_area_call(PORT).call.args
    assert erase[-3:] == ("erase-region", "0x0", "0x110000")


@pytest.mark.parametrize("token", NEVER + ("--force", "load-ram", "run", "hard-reset",
                                           "--do-not-confirm", "burn-anything"))
def test_the_checker_refuses_any_forbidden_word(images, tmp_path, token):
    for planned in every_call(images, tmp_path):
        bad = replace(planned, call=replace(planned.call, args=planned.call.args + (token,)))
        assert call_problems(bad, images), (planned.purpose, token)


def test_the_checker_refuses_other_offsets_commands_and_resets(images, tmp_path):
    write = boardtest.write_call(PORT, images)
    moved = tuple("0x20000" if arg == "0x10000" else arg for arg in write.call.args)
    assert call_problems(replace(write, call=replace(write.call, args=moved)), images)
    two = boardtest.write_call(PORT, images[:2])
    assert call_problems(two, images)
    start = boardtest.start_call(PORT)
    other_register = tuple("0x60008000" if arg == "0x6000812c" else arg
                           for arg in start.call.args)
    assert call_problems(replace(start, call=replace(start.call, args=other_register)))
    erase = boardtest.erase_area_call(PORT)
    wider = erase.call.args[:-1] + ("0x1000000",)
    assert call_problems(replace(erase, call=replace(erase.call, args=wider)))
    booting = tuple("watchdog-reset" if arg == "no-reset" else arg for arg in erase.call.args)
    assert call_problems(replace(erase, call=replace(erase.call, args=booting)))
    burn = TestCall(Purpose.EFUSE_AFTER, ToolCall(ESPEFUSE, (
        "--chip", "esp32s3", "--port", PORT, "--before", "default-reset", "burn-efuse",
        "SECURE_BOOT_EN", "1"), "x"))
    assert call_problems(burn)
    write_mem_as_erase = replace(erase, call=replace(start.call))
    assert call_problems(write_mem_as_erase)
    started_twice = every_call(images, tmp_path) + (boardtest.start_call(PORT),)
    assert "the program is started only once" in safety_problems(blank_board(), started_twice,
                                                                 images)


@pytest.mark.parametrize("board, words", [
    (sdm_board(), "Secure Download Mode"),
    (blank_board(evidence={"secure_boot": True}), "Secure Boot is on"),
    (blank_board(evidence={"flash_encryption": True}), "flash encryption is on"),
    (blank_board(security=SecurityState.SECURED_OTHER), "security is secured_other"),
    (blank_board(security=SecurityState.UNSECURED_USED), "security is unsecured_used"),
    (blank_board(security=SecurityState.UNKNOWN), "security is unknown"),
    (blank_board(hardware=HardwareVerdict.FLASH_CHIP_UNSUPPORTED), "hardware gate"),
], ids=["sdm", "secure-boot", "encrypted", "other-keys", "used", "unknown", "gate"])
def test_boards_the_program_may_never_reach(board, words):
    assert any(words in problem for problem in board_problems(board))


def test_blank_and_key_burned_boards_may_be_tested():
    assert board_problems(blank_board()) == []
    assert board_problems(key_burned_board()) == []


# ---------------------------------------------------------- the console

def watch_of(lines, *, port_open=True, step=0.05, start=0.0) -> ConsoleWatch:
    """A finished watch of ``lines`` (strings or (at, line)), as BootWatcher.read fills it."""
    parser = ConsoleParser(MAC)
    watch = ConsoleWatch(started=0.0, events=[], lines=[], port_open=port_open)
    for index, item in enumerate(lines):
        at, line = item if isinstance(item, tuple) else (start + index * step, item)
        event = parser.parse(line, at)
        if event is None:
            continue
        watch.last_line_at = at
        watch.lines.append((at, event.raw))
        if event.kind is ConsoleKind.OTHER and not event.data:
            watch.other_lines += 1
        else:
            watch.events.append(event)
    return watch


def judged(lines, *, locked=False, board=None, after=1.0, final=True, **watch):
    w = watch_of(lines, **watch)
    end = (w.last_line_at or 0.0) + after
    return TestJudge(MAC, locked=locked, board=board).evaluate(w, end, final=final)


def fixture_lines(name):
    lines = []
    for row in support.board_sample(name).splitlines():
        if row[:9].strip().replace(".", "").isdigit():  # a ConsoleLog file: drop the time
            row = row[9:]
            if row.startswith("#"):
                continue
        if row.startswith("==="):
            continue
        lines.append(row)
    return lines


def test_sltest_lines_are_their_own_kind_with_their_fields():
    parser = ConsoleParser(MAC)
    event = parser.parse("SLTEST RESULT stage=psram result=fail code=PSRAM_DATA_ERRORS "
                         "detail=pattern_55 errors=1204 ms=21000")
    assert event.kind is ConsoleKind.SELF_TEST
    assert event.data == {"sltest": "RESULT", "stage": "psram", "result": "fail",
                          "code": "PSRAM_DATA_ERRORS", "detail": "pattern_55",
                          "errors": "1204", "ms": "21000"}
    assert event.raw.startswith("SLTEST RESULT stage=psram")  # nothing redacted


def test_the_end_crc_is_checked_on_the_exact_lines():
    run = [line for _at, line in scripts.passing_run(MAC)]
    parser = ConsoleParser(MAC)
    events = [parser.parse(line) for line in run]
    end = next(e for e in events if e and e.data.get("sltest") == "END")
    assert end.data["crc_ok"] == "1"
    parser = ConsoleParser(MAC)
    changed = [line.replace("errors=0 ms=310", "errors=0 ms=311") for line in run]
    events = [parser.parse(line) for line in changed]
    end = next(e for e in events if e and e.data.get("sltest") == "END")
    assert end.data["crc_ok"] == "0"


def test_sltest_lines_are_never_dropped_by_the_other_line_cap():
    from sl_installer.core.console import BootWatcher

    clock = FakeClock()
    usb = FakeUsb()
    serial = SerialScript(clock, usb)
    clock.hooks.append(serial.tick)
    noise = [f"I ({i}) something: line {i}" for i in range(400)]
    serial.at(clock.now + 0.1, "print", noise + ["SLTEST ALIVE stage=psram step=1 of=2 at=0"])
    watch = ConsoleWatch(started=clock.now, events=[])
    BootWatcher(usb, clock, opener=serial.opener).read(
        MAC, watch, lambda e: None, CancelToken(), deadline=clock.now + 3, stop=None)
    assert watch.other_lines == 400
    assert [e.kind for e in watch.events] == [ConsoleKind.SELF_TEST]


# ------------------------------------------------------------ the judge

def test_a_complete_good_run_passes():
    verdict = judged(scripts.passing_run(MAC))
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.end_crc_ok
    assert verdict.clean_ok and verdict.program_done
    assert [(f.stage, f.result) for f in verdict.findings] == [
        (stage, "ok") for stage in boardtest.STAGES]
    assert verdict.measurements["psram_size"] == 8 * 1024 * 1024
    assert verdict.measurements["psram_mb_s"] == 41
    assert verdict.measurements["networks"] == 6 and verdict.measurements["best_rssi"] == -48
    assert verdict.measurements["temp_c"] == 41


def test_a_pass_waits_for_the_program_to_clean_up():
    lines = [item for item in scripts.passing_run(MAC) if "CLEAN" not in item[1]]
    w = watch_of(lines)
    judge = TestJudge(MAC)
    assert judge.check(w, w.last_line_at + 2.0) is None   # CLEAN may still come
    late = judge.check(w, w.last_line_at + boardtest.END_TO_CLEAN_S + 0.1)
    assert late.verdict is BoardTestVerdict.PASSED and not late.clean_ok


def test_psram_data_errors_fail_with_the_first_error_as_proof():
    verdict = judged(scripts.psram_errors_run(MAC))
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_DATA_ERRORS"
    finding = verdict.findings[0]
    assert finding.stage == "psram" and finding.result == "fail"
    assert finding.title == texts.PSRAM_DATA_ERRORS.title
    assert "1,204 errors" in finding.detail and "0x3c812340" in finding.detail
    assert "wrote 0x55555555, read 0x55515555" in finding.detail
    assert len([p for p in finding.proof if p.startswith("SLTEST ERR")]) == 8
    assert verdict.advice is texts.PSRAM_DATA_ERRORS and not verdict.advice.can_retry


@pytest.mark.parametrize("stage, code", [
    ("psram", "PSRAM_NOT_FOUND"), ("psram", "PSRAM_WRONG_SIZE"), ("psram", "PSRAM_TOO_SMALL"),
    ("iram", "IRAM_DATA_ERRORS"), ("flash", "FLASH_WRONG_SIZE"),
    ("flash", "FLASH_READ_UNSTABLE"), ("flash", "FLASH_WRITE_ERRORS"), ("cpu", "CPU_MISMATCH"),
    ("wifi", "WIFI_RADIO_FAILED"),
])
def test_every_stage_failure_is_a_hardware_fault(stage, code):
    run = scripts.program_run(MAC, results={stage: {"result": "fail", "code": code,
                                                    "errors": 3}})
    verdict = judged(run)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == code
    assert verdict.advice.code == code and not verdict.advice.can_retry


def test_no_wifi_networks_is_a_warning_not_a_fault():
    run = scripts.program_run(MAC, results={"wifi": {"result": "warn",
                                                     "code": "WIFI_NO_NETWORKS",
                                                     "networks": 0}})
    verdict = judged(run)
    assert verdict.verdict is BoardTestVerdict.PASSED
    warn = [f for f in verdict.findings if f.result == "warn"]
    assert [f.code for f in warn] == ["WIFI_NO_NETWORKS"]
    assert warn[0].title == texts.WIFI_NO_NETWORKS.title


def test_a_wrong_crc_cannot_pass():
    verdict = judged(scripts.program_run(MAC, crc="deadbeef"))
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "USB_DATA_GARBLED"
    assert verdict.end_crc_ok is False and verdict.advice is texts.USB_DATA_GARBLED


def test_a_missed_begin_line_cannot_pass():
    run = [item for item in scripts.passing_run(MAC) if "BEGIN" not in item[1]]
    verdict = judged(run)
    assert verdict.code == "USB_DATA_GARBLED" and verdict.verdict is not BoardTestVerdict.PASSED


@pytest.mark.parametrize("stage", boardtest.STAGES)
def test_silence_after_begin_is_a_hang_in_that_stage(stage):
    run = scripts.program_run(MAC, stop_after=stage)
    w = watch_of(run)
    judge = TestJudge(MAC)
    assert judge.check(w, w.last_line_at + boardtest.SILENCE_S - 0.5) is None
    assert judge.check(w, w.last_line_at + boardtest.SILENCE_S + 0.1) is None  # lingers
    verdict = judge.check(w, w.last_line_at + boardtest.SILENCE_S + boardtest.LINGER_S + 0.2)
    assert verdict.verdict is BoardTestVerdict.FAILED
    assert verdict.code == f"{stage.upper()}_HANG"
    assert verdict.findings[-1].stage == stage and verdict.advice.code == f"{stage.upper()}_HANG"


def test_a_gone_port_is_not_a_hang():
    run = scripts.program_run(MAC, stop_after="flash")
    w = watch_of(run, port_open=False)
    verdict = TestJudge(MAC).evaluate(w, w.last_line_at + boardtest.SILENCE_S + 1, final=False)
    assert verdict is None
    verdict = TestJudge(MAC).evaluate(w, w.last_line_at + 40, final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "BOARD_DISCONNECTED"


def crash_lines(stage):
    run = scripts.program_run(MAC, stop_after=stage)
    last = run[-1][0]
    return run + [(last + 0.1, "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). "
                               "Exception was unhandled."),
                  (last + 0.12, "PC      : 0x40375a2b  PS      : 0x00060a30")]


def test_a_crash_in_the_memory_test_is_a_psram_crash():
    verdict = judged(crash_lines("psram"), after=boardtest.SETTLE_S + 0.1)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_CRASH"
    assert "LoadProhibited" in verdict.findings[-1].detail
    assert verdict.findings[-1].proof[0].startswith("Guru Meditation")


def test_a_crash_in_another_stage_names_it():
    verdict = judged(crash_lines("wifi"), after=boardtest.SETTLE_S + 0.1)
    assert verdict.code == "TEST_CRASH" and verdict.findings[-1].stage == "wifi"


def test_a_crash_waits_for_the_rest_of_the_dump():
    lines = crash_lines("cpu")
    assert judged(lines, after=0.5, final=False) is None


def test_a_watchdog_reset_during_the_test_is_a_fault_and_a_replug_is_not():
    run = scripts.program_run(MAC, stop_after="flash")
    last = run[-1][0]
    rtc = run + [(last + 1, "ESP-ROM:esp32s3-20210327"),
                 (last + 1, "rst:0x10 (RTCWDT_RTC_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)")]
    assert judged(rtc).code == "TEST_CRASH"
    power = run + [(last + 1, "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)")]
    verdict = judged(power)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "TEST_RESTARTED"
    again = run + [(last + 2, scripts._begin(MAC))]
    assert judged(again).code == "TEST_RESTARTED"


def test_brownout_is_a_power_problem_not_a_board_fault():
    run = scripts.program_run(MAC, stop_after="wifi")
    last = run[-1][0]
    verdict = judged(run + [(last + 0.1, "Brownout detector was triggered")])
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.advice is texts.POWER_PROBLEM


def test_the_test_on_another_board_is_refused():
    verdict = judged(scripts.passing_run(OTHER_MAC))
    assert verdict.code == "BOARD_SWAPPED" and verdict.verdict is BoardTestVerdict.INCOMPLETE


# ------------------------------------------ before BEGIN, and the real boards

def test_the_real_psram_hang_board_fails_at_start_up():
    # ac:27:6e:a4:e5:e8: every start stops after "Multicore app", then panics.
    lines = fixture_lines("psram-fault-hang-a4e5e8-console.txt")
    verdict = judged(lines, after=boardtest.SETTLE_S + 0.1, final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_STARTUP_FAULT"
    proof = verdict.findings[0].proof
    assert any("octal_psram: vendor id" in line for line in proof)
    assert any(line.startswith("Guru Meditation") for line in proof)
    assert verdict.advice is texts.PSRAM_STARTUP_FAULT and not verdict.advice.can_retry


def test_the_real_psram_crash_board_fails_at_start_up_although_its_memtest_said_ok():
    # ac:27:6e:a4:57:88: IDF's memtest said OK once, then every start crashed.
    lines = fixture_lines("psram-fault-crash-a45788-console.txt")
    assert any("SPI SRAM memory test OK" in line for line in lines)
    verdict = judged(lines, after=boardtest.SETTLE_S + 0.1, final=False)
    assert verdict.code == "PSRAM_STARTUP_FAULT" and verdict.verdict is BoardTestVerdict.FAILED


def test_the_real_xmc_board_is_a_flash_chip_fault():
    verdict = judged(fixture_lines("xmc-20-7018-first-start-console.txt"), final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED
    assert verdict.code == "FLASH_CHIP_UNSUPPORTED"
    assert any("XMC flash startup fail" in line for line in verdict.findings[0].proof)


def test_psram_lines_then_silence_is_a_psram_start_up_fault():
    lines = scripts.timed(scripts.PSRAM_HANG_A4E5E8[:13])  # up to "Multicore app"
    verdict = judged(lines, after=boardtest.SILENCE_S + 0.1, final=False)
    assert verdict.code == "PSRAM_STARTUP_FAULT"
    # A locked board judged on silence, before its watchdog restarts it: no "(0 restarts)"
    # that the report's measurements contradict later (board ac:27:6e:a4:57:88, 16 Nov).
    locked = judged(lines, locked=True, after=boardtest.SILENCE_S + 0.1, final=False)
    assert locked.code == "PSRAM_STARTUP_FAULT"
    assert "restarts" not in locked.findings[0].detail


def test_a_crash_before_any_psram_line_is_a_start_up_crash():
    lines = ["ESP-ROM:esp32s3-20210327", "rst:0x10 (RTCWDT_RTC_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)",
             "I (60) boot: Loaded app from partition at offset 0x10000",
             "Guru Meditation Error: Core  0 panic'ed (IllegalInstruction). Exception was "
             "unhandled."]
    verdict = judged(lines, after=boardtest.SETTLE_S + 0.1, final=False)
    assert verdict.code == "STARTUP_CRASH" and verdict.verdict is BoardTestVerdict.FAILED


def silent_loop(period, count=4) -> ConsoleWatch:
    w = ConsoleWatch(started=0.0, events=[], lines=[], port_open=True)
    w.resets = count
    w.reset_times = tuple(period * (i + 1) for i in range(count))
    return w


def test_a_silent_ten_second_restart_loop_is_a_start_up_hang():
    verdict = TestJudge(MAC).evaluate(silent_loop(10.2), 45.0, final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "STARTUP_HANG"
    assert "10.2 s" in verdict.findings[0].detail


def test_a_fast_restart_loop_asks_about_power_first():
    verdict = TestJudge(MAC).evaluate(silent_loop(2.0), 10.0, final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "RESTART_LOOP_FAST"
    assert "Test again" in " ".join(verdict.advice.what_to_do)


def test_nothing_at_all_is_no_start_after_twelve_seconds():
    w = ConsoleWatch(started=0.0, events=[], lines=[], port_open=True)
    assert TestJudge(MAC).evaluate(w, boardtest.NO_START_S - 0.5, final=False) is None
    verdict = TestJudge(MAC).evaluate(w, boardtest.NO_START_S + 0.1, final=False)
    assert verdict.code == "NO_START" and verdict.verdict is BoardTestVerdict.INCOMPLETE


def test_download_mode_is_no_start_at_once():
    verdict = judged(["rst:0x10 (RTCWDT_RTC_RST),boot:0x23 (DOWNLOAD(USB/UART0))",
                      "waiting for download"], final=False)
    assert verdict.code == "NO_START"


# ------------------------------------------------------- a locked board

@pytest.mark.parametrize("name", ["psram-fault-hang-a4e5e8-console.txt",
                                  "psram-fault-crash-a45788-console.txt"])
def test_locked_psram_boards_are_hardware_faults(name):
    verdict = judged(fixture_lines(name), locked=True, board=sdm_board(), final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_STARTUP_FAULT"
    assert verdict.recovery is None


def test_a_locked_xmc_board_is_a_flash_chip_fault():
    verdict = judged(fixture_lines("xmc-20-7018-first-start-console.txt"), locked=True,
                     board=key_burned_board(flash_id="20 70 18"), final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "FLASH_CHIP_UNSUPPORTED"
    assert "20 70 18" in verdict.advice.what_happened


def test_a_locked_board_without_settings_can_be_fixed():
    verdict = judged(scripts.timed(scripts.LOCKED_SETTINGS_MISSING), locked=True,
                     board=sdm_board(), final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE
    assert verdict.recovery == boardtest.RECOVERY_FIX_SETTINGS
    assert verdict.advice is texts.FAILED_BOARD_FIX_SETTINGS


def test_a_board_locked_by_someone_else_gets_no_fix_offer():
    verdict = judged(scripts.timed(scripts.LOCKED_SETTINGS_MISSING), locked=True,
                     board=sdm_board(security=SecurityState.SECURED_OTHER), final=False)
    assert verdict.recovery is None


def test_a_locked_board_that_reaches_head_office_works():
    verdict = judged(scripts.timed(scripts.LOCKED_STARTS_NORMALLY), locked=True,
                     board=sdm_board(), final=False)
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.code == "BOARD_STARTS_NORMALLY"


def test_a_locked_board_on_the_placeholder_wifi_works_but_cannot_reach_head_office():
    lines = list(scripts.LOCKED_STARTS_NORMALLY[:9]) + [
        "I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
        "I (6000) zone_lite: Retrying Wi-Fi connection (1)",
        "W (121400) wifi_setup: Wi-Fi setup access point enabled (recovery mode)"]
    verdict = judged(scripts.timed(lines), locked=True, board=sdm_board(), final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE
    assert verdict.findings[0].title == texts.FAILED_BOARD_NO_HEAD_OFFICE.title
    assert verdict.advice.code == "HOTSPOT_OPEN"


def test_a_silent_locked_board_loop_is_a_hardware_fault():
    verdict = TestJudge(MAC, locked=True, board=sdm_board()).evaluate(silent_loop(9.8), 40.0,
                                                                     final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "STARTUP_HANG"


def test_a_silent_locked_board_says_so():
    w = ConsoleWatch(started=0.0, events=[], lines=[], port_open=True)
    verdict = TestJudge(MAC, locked=True, board=sdm_board()).evaluate(w, 90.0, final=True)
    assert verdict.code == "FAILED_BOARD_SILENT" and verdict.verdict is BoardTestVerdict.INCOMPLETE


# -------------------------------------------------------- whole test runs

class Rig:
    """A BoardTester on a simulated board: esptool via FakeBoard, the console via a
    SerialScript. ``program`` is what the board prints after the start call (None: it
    does not start); after a CLEAN line it drops off USB and comes back in download mode."""

    def __init__(self, tmp_path: Path, program=None, *, fake: Optional[FakeBoard] = None,
                 back_after_clean: bool = True, replug_program=None) -> None:
        self.clock = FakeClock()
        self.usb = FakeUsb()
        self.serial = SerialScript(self.clock, self.usb)
        self.clock.hooks.append(self.serial.tick)
        self.fake = fake or FakeBoard()
        self.runner = FakeRunner(self.fake)
        self.folder, self.pinned = support.make_test_images(tmp_path / "images")
        self.logs = tmp_path / "logs"
        self.work = tmp_path / "work"
        self.tester = BoardTester(runner=self.runner, usb=self.usb, clock=self.clock,
                                  image_dir=self.folder, images=self.pinned,
                                  temp_root=self.work, console_logs=self.logs,
                                  opener=self.serial.opener)
        self.program = program
        self.replug_program = replug_program
        self.back_after_clean = back_after_clean
        self.fake.on_start = self._started
        self.progress: List = []
        self.phases: List[ReplugPhase] = []
        self.events: List = []

    def _play(self, program, delay: float) -> None:
        now = self.clock.now
        for at, line in program:
            self.serial.at(now + delay + at, "print", line)
        clean = [at for at, line in program if line.startswith("SLTEST CLEAN")]
        if clean:
            # The program forces download mode and restarts: USB drops and returns.
            self.serial.at(now + delay + clean[0] + 0.2, "unplug")
            if self.back_after_clean:
                self.serial.at(now + delay + clean[0] + 1.2, "plug")

    def _started(self, fake: FakeBoard) -> None:
        # The RTC watchdog reset drops USB; the port is back about 0.8 s later.
        self.usb.present = False
        self.serial.generation += 1
        self.serial.pending = b""
        self.serial.at(self.clock.now + 0.8, "plug")
        if self.program is not None:
            self._play(self.program, 0.9)

    def on_phase(self, phase: ReplugPhase) -> None:
        self.phases.append(phase)
        if phase is ReplugPhase.WAITING_UNPLUG:
            self.serial.at(self.clock.now + 1.0, "unplug")
        elif phase is ReplugPhase.WAITING_REPLUG:
            self.serial.at(self.clock.now + 2.0, "plug")
            if self.replug_program is not None:
                self._play(self.replug_program, 2.3)

    def run(self, board=None, cancel=None):
        return self.tester.run_test(board or blank_board(), self.progress.append,
                                    self.on_phase, self.events.append,
                                    cancel or CancelToken())

    def commands(self):
        return [next(a for a in c.args if a in (
            "get-security-info", "summary", "write-flash", "write-mem", "erase-region",
            "erase-flash")) for c in self.runner.calls]


def assert_never_burned(rig: Rig) -> None:
    for call in rig.runner.calls:
        assert not [arg for arg in call.args if arg in NEVER or arg.startswith("burn")]
    assert rig.fake.burned_key is None


def test_a_good_board_passes_and_is_erased_with_its_efuses_unchanged(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert report.passed and not report.hardware_fault
    assert report.kind == "vendor_test" and not report.keys_burned and report.recovery is None
    assert report.efuses_unchanged is True and report.erased is True and report.end_crc_ok
    assert report.advice is texts.BOARD_TEST_PASSED
    assert rig.commands() == ["get-security-info", "summary", "write-flash", "write-mem",
                              "get-security-info", "erase-region", "summary"]
    assert rig.fake.flash == {} and rig.fake.chip_erases == 0  # the program cleaned its blocks
    assert sorted(rig.fake.erased) == [(0x0, 0x110000)]
    assert rig.fake.mem_writes == [(0x6000812C, 0, 1)] and rig.fake.started_programs == 1
    assert report.efuse_before == report.efuse_after and report.efuse_before["MAC"]
    assert {f.code for f in report.findings} >= {"OK", "EFUSES_UNCHANGED", "ERASED"}
    assert len(report.tool_log) == 7 and report.tool_log[2].startswith("$ esptool")
    assert report.image_sha256 == {image.name: image.sha256 for image in rig.pinned}
    assert report.measurements["psram_mb_s"] == 41 and report.measurements["flash_id"] == "c84018"
    assert rig.phases == []  # it started by itself: no replug asked
    assert_never_burned(rig)
    # Progress went forward to the end, through every stage.
    stages = [p.stage for p in rig.progress]
    for stage in ("check", "write", "start", *boardtest.STAGES, "cleanup"):
        assert stage in stages
    fractions = [p.fraction for p in rig.progress if p.fraction is not None]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    # The console log is kept like a setup's, and named in the report.
    log = Path(report.console_log)
    assert log.parent == rig.logs and log.name.startswith("console-ac276ea43d30-boardtest-")
    text = log.read_text(encoding="utf-8")
    assert "=== Board hardware test" in text and "SLTEST END result=ok" in text
    assert "=== Result: passed (BOARD_TEST_PASSED) ===" in text
    assert not list(rig.work.glob("sl-boardtest-*"))  # the work folder is gone


def test_a_finished_passed_test_opens_the_installer_gate(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    report = rig.run()
    assert board_test_problem(InstallMode.NEW_BOARD, blank_board(), report,
                              rig.clock.now_utc()) is None


def test_a_psram_fault_fails_and_is_still_erased(tmp_path):
    rig = Rig(tmp_path, scripts.psram_errors_run(MAC))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.FAILED and report.hardware_fault
    assert report.advice is texts.PSRAM_DATA_ERRORS
    assert report.findings[0].code == "PSRAM_DATA_ERRORS"
    assert report.erased is True and report.efuses_unchanged is True
    assert_never_burned(rig)


def test_the_psram_hang_board_fails_and_the_whole_chip_is_erased(tmp_path):
    rig = Rig(tmp_path, scripts.timed(scripts.PSRAM_HANG_A4E5E8))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.FAILED and report.findings[0].code == \
        "PSRAM_STARTUP_FAULT"
    # The program never ran to its CLEAN line: its test blocks may be anywhere.
    assert rig.commands()[-3:] == ["erase-region", "erase-flash", "summary"]
    assert rig.fake.chip_erases == 1 and report.erased is True
    assert rig.clock.now - 1000 < boardtest.WATCH_S  # decided early, not at the deadline
    assert_never_burned(rig)


def test_a_hang_in_the_test_fails_and_is_erased(tmp_path):
    rig = Rig(tmp_path, scripts.program_run(MAC, stop_after="flash"))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.FAILED
    assert report.advice.code == "FLASH_HANG" and report.erased is True
    assert rig.fake.chip_erases == 1


def test_a_board_that_does_not_start_is_replugged_once(tmp_path):
    rig = Rig(tmp_path, None, replug_program=scripts.passing_run(MAC))
    report = rig.run()
    assert rig.phases == [ReplugPhase.WAITING_UNPLUG, ReplugPhase.WAITING_REPLUG,
                          ReplugPhase.WATCHING]
    assert report.verdict is BoardTestVerdict.PASSED, report.findings


def test_a_board_that_never_starts_is_incomplete_and_erased(tmp_path):
    rig = Rig(tmp_path, None)
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.advice is texts.NO_START
    assert [f.code for f in report.findings].count("NO_START") == 2
    assert report.erased is True and rig.fake.chip_erases == 1  # it may have run unseen


def test_cancel_during_the_test_still_cleans_up(tmp_path):
    cancel = CancelToken()
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    rig.events_seen = 0

    def cancel_on_psram(event):
        if event.data.get("sltest") == "ALIVE":
            cancel.cancel()

    report = rig.tester.run_test(blank_board(), rig.progress.append, rig.on_phase,
                                 cancel_on_psram, cancel)
    assert report.verdict is BoardTestVerdict.INCOMPLETE
    assert any(f.code == "CANCELLED" for f in report.findings)
    assert "erase-region" in rig.commands() and report.erased is True
    assert report.efuses_unchanged is True


def test_cancel_before_writing_writes_nothing(tmp_path):
    cancel = CancelToken()
    cancel.cancel()
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    report = rig.run(cancel=cancel)
    assert report.verdict is BoardTestVerdict.INCOMPLETE and report.advice is texts.CANCELLED
    assert "write-flash" not in rig.commands() and report.erased is None


def test_a_board_that_cannot_be_reached_for_cleanup_says_how_to_finish(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC), back_after_clean=False)
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE and report.erased is False
    assert report.advice is texts.ERASE_NOT_FINISHED
    assert "BOOT" in " ".join(report.advice.what_to_do)
    assert "Test again" in " ".join(report.advice.what_to_do)
    assert report.efuses_unchanged is None
    assert board_test_problem(InstallMode.NEW_BOARD, blank_board(), report,
                              rig.clock.now_utc()) is not None


def test_changed_efuses_are_a_red_alarm_and_never_a_pass(tmp_path, caplog):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    started = rig.fake.on_start

    def change(fake):
        started(fake)
        fake.summary["WR_DIS"]["value"] = 1  # cannot happen with our program: prove we see it

    rig.fake.on_start = change
    report = rig.run()
    assert report.efuses_unchanged is False and report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.advice is texts.EFUSE_CHANGED
    changed = [f for f in report.findings if f.code == "EFUSE_CHANGED"]
    assert changed and "WR_DIS" in changed[0].detail
    assert any("EFUSES CHANGED" in record.getMessage() for record in caplog.records)
    assert efuse_differences(report.efuse_before, report.efuse_after) == ["WR_DIS"]


def test_a_refused_board_gets_a_failed_report_without_any_write(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    board = blank_board(hardware=HardwareVerdict.FLASH_CHIP_UNSUPPORTED, flash_id="20 70 18",
                        hardware_detail="memory chip 20 70 18",
                        evidence={"flash_id": "20 70 18", "xmc_startup_failed": True,
                                  "chip_line": "ESP32-S3 (QFN56) (revision v0.2)"})
    report = rig.run(board)
    assert rig.runner.calls == []
    assert report.verdict is BoardTestVerdict.FAILED and report.erased is None
    finding = report.findings[0]
    assert finding.stage == "check" and finding.code == "FLASH_CHIP_UNSUPPORTED"
    assert "20 70 18" in finding.detail and "xmc_startup_failed: True" in finding.proof
    assert report.advice.code == "FLASH_CHIP_UNSUPPORTED"


@pytest.mark.parametrize("board", [
    sdm_board(), blank_board(evidence={"secure_boot": True}),
    blank_board(security=SecurityState.SECURED_OTHER),
], ids=["sdm", "secure-boot", "other-keys"])
def test_locked_boards_are_never_written(tmp_path, board):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    report = rig.run(board)
    assert rig.runner.calls == [] and report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.advice is texts.BOARD_TEST_NOT_POSSIBLE


def test_a_board_whose_identity_check_shows_secure_boot_is_not_written(tmp_path):
    fake = FakeBoard(efuse_state="secured-not-sdm")
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = rig.run()
    assert rig.commands() == ["get-security-info"]
    assert report.advice is texts.BOARD_TEST_NOT_POSSIBLE


def test_a_swapped_board_is_not_written(tmp_path):
    fake = FakeBoard()
    fake.reported_mac = OTHER_MAC  # the chip answering is another board
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = rig.run()
    assert rig.commands() == ["get-security-info"]
    assert report.advice is texts.BOARD_SWAPPED and report.verdict is BoardTestVerdict.INCOMPLETE


def test_a_board_without_its_usb_board_id_is_not_written(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    rig.usb.mac = None  # Windows gave no board ID for the port
    report = rig.run()
    assert rig.runner.calls == [] and report.advice is texts.BOARD_ID_NOT_READABLE


def test_damaged_images_are_never_written(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    (rig.folder / "board-test.bin").write_bytes(b"not the pinned program")
    report = rig.run()
    assert rig.runner.calls == [] and report.advice is texts.BOARD_TEST_IMAGES_DAMAGED


def test_unpinned_images_are_never_written(tmp_path):
    rig = Rig(tmp_path, scripts.passing_run(MAC))
    rig.tester._images = tuple(replace(image, sha256=None) for image in rig.pinned)
    report = rig.run()
    assert rig.runner.calls == [] and report.advice is texts.BOARD_TEST_IMAGES_DAMAGED


def test_a_failed_write_is_still_cleaned_up(tmp_path):
    fake = FakeBoard()
    fake.failures["write-flash"] = support.Failure(2, support.SERIAL_NOISE)
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.findings[0].code == texts.CONNECTION_LOST.code
    assert rig.commands()[-2:] == ["erase-region", "summary"] and report.erased is True
    assert rig.fake.chip_erases == 0  # the program never started


def test_a_key_burned_board_without_secure_boot_is_tested_and_offered_resume(tmp_path):
    fake = FakeBoard(efuse_state="key-burned")
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = rig.run(key_burned_board())
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert report.kind == "locked_board" and report.keys_burned
    assert report.recovery == boardtest.RECOVERY_RESUME
    assert_never_burned(rig)


def test_a_board_whose_efuses_disagree_with_the_check_is_not_written(tmp_path):
    fake = FakeBoard(efuse_state="key-burned")
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = rig.run(blank_board())  # the check said blank; the eFuses say key burned
    assert "write-flash" not in rig.commands()
    assert report.advice is texts.BOARD_TEST_NOT_POSSIBLE


# -------------------------------------------------- the failed-board check

def locked_rig(tmp_path, lines):
    rig = Rig(tmp_path, None, replug_program=scripts.timed(lines))
    return rig


def check(rig, board, seconds=90.0):
    return rig.tester.check_failed_board(board, rig.on_phase, rig.events.append, CancelToken(),
                                         seconds=seconds)


def test_a_locked_psram_board_is_watched_never_written_and_fails(tmp_path):
    rig = locked_rig(tmp_path, fixture_lines("psram-fault-crash-a45788-console.txt"))
    report = check(rig, sdm_board())
    assert rig.runner.calls == []
    assert rig.phases == [ReplugPhase.WAITING_UNPLUG, ReplugPhase.WAITING_REPLUG,
                          ReplugPhase.WATCHING]
    assert report.verdict is BoardTestVerdict.FAILED and report.kind == "locked_board"
    assert report.findings[0].code == "PSRAM_STARTUP_FAULT" and report.recovery is None
    assert report.keys_burned and report.erased is None and report.efuses_unchanged is None
    assert report.measurements["app_version"] == "2.8.1"
    assert Path(report.console_log).name.startswith("console-ac276ea43d30-failedboard-")


def test_a_locked_board_without_settings_is_offered_fix_settings(tmp_path):
    rig = locked_rig(tmp_path, scripts.LOCKED_SETTINGS_MISSING)
    report = check(rig, sdm_board())
    assert rig.runner.calls == []
    assert report.recovery == boardtest.RECOVERY_FIX_SETTINGS
    assert report.verdict is BoardTestVerdict.INCOMPLETE


def test_a_locked_board_that_is_not_replugged_says_so(tmp_path):
    rig = Rig(tmp_path, None)
    rig.on_phase = rig.phases.append  # the person never unplugs it
    report = rig.tester.check_failed_board(sdm_board(), rig.on_phase, rig.events.append,
                                           CancelToken(), seconds=30)
    assert report.advice is texts.NOT_REPLUGGED and rig.runner.calls == []


def test_a_key_burned_board_without_secure_boot_is_tested_instead(tmp_path):
    fake = FakeBoard(efuse_state="key-burned")
    rig = Rig(tmp_path, scripts.passing_run(MAC), fake=fake)
    report = check(rig, key_burned_board())
    assert "write-flash" in rig.commands() and report.verdict is BoardTestVerdict.PASSED
    assert report.recovery == boardtest.RECOVERY_RESUME


# ------------------------------------------------------------ the gate

def test_the_gate_needs_a_fresh_passed_test_of_this_board(tmp_path):
    clock = FakeClock()
    now = clock.now_utc()
    board = blank_board()
    good = support.board_test_report(board, now=now)
    assert board_test_problem(InstallMode.NEW_BOARD, board, good, now) is None
    assert board_test_problem(InstallMode.RESUME, board, good, now) is None
    assert board_test_problem(InstallMode.CHANGE_SETTINGS, board, None, now) is None
    for report, words in [
        (None, "no board test"),
        (support.board_test_report(board, verdict=BoardTestVerdict.FAILED, now=now),
         "did not pass"),
        (support.board_test_report(board, verdict=BoardTestVerdict.INCOMPLETE, now=now),
         "did not pass"),
        (support.board_test_report(board, now=now, mac=OTHER_MAC), "not " + MAC),
        (support.board_test_report(board, now=now, efuses_unchanged=None), "eFuses"),
        (support.board_test_report(board, now=now, efuses_unchanged=False), "eFuses"),
        (support.board_test_report(board, now=now, erased=False), "erased"),
        (support.board_test_report(board, now=now - timedelta(minutes=31)), "minutes old"),
        (support.board_test_report(board, now=now + timedelta(minutes=5)), "future"),
        (support.board_test_report(board, finished_at="yesterday"), "not readable"),
        (support.board_test_report(board, finished_at="2026-11-16T10:00:00"), "time zone"),
    ]:
        problem = board_test_problem(InstallMode.NEW_BOARD, board, report, now)
        assert problem and words in problem, (words, problem)
    assert board_test_problem(InstallMode.NEW_BOARD, board,
                              support.board_test_report(board, now=now - timedelta(minutes=29)),
                              now) is None
    with pytest.raises(InstallerError) as caught:
        check_board_test(InstallMode.NEW_BOARD, board, None, now)
    assert caught.value.advice is texts.BOARD_TEST_REQUIRED


@pytest.mark.parametrize("mode, security", [
    (InstallMode.NEW_BOARD, SecurityState.BLANK),
    (InstallMode.RESUME, SecurityState.KEY_BURNED_NOT_STARTED),
])
@pytest.mark.parametrize("why", ["none", "failed", "old", "other-board"])
def test_the_installer_refuses_before_head_office_or_any_write(tmp_path, store, zkt_bundle,
                                                               mode, security, why):
    from .test_installer import Rig as InstallRig

    fake = FakeBoard(efuse_state="key-burned" if security is SecurityState.KEY_BURNED_NOT_STARTED
                     else "blank")
    rig = InstallRig(tmp_path, store, zkt_bundle, fake)
    board = blank_board(security=security)
    now = rig.clock.now_utc()
    report = {"none": None,
              "failed": support.board_test_report(board, verdict=BoardTestVerdict.FAILED,
                                                  now=now),
              "old": support.board_test_report(board, now=now - timedelta(hours=1)),
              "other-board": support.board_test_report(board, now=now, mac=OTHER_MAC)}[why]
    with pytest.raises(InstallerError) as caught:
        rig.run(mode, board, board_test=report)
    assert caught.value.advice is texts.BOARD_TEST_REQUIRED
    assert not caught.value.irreversible_done
    assert rig.runner.calls == [] and rig.server.events == []  # nothing written, ADD not asked
    assert fake.burned_key is None


def test_a_settings_change_needs_no_board_test(tmp_path, store, zkt_bundle):
    from .test_installer import Rig as InstallRig

    rig = InstallRig(tmp_path, store, zkt_bundle, FakeBoard(efuse_state="sdm", sdm=True))
    board = sdm_board()
    rig.run(InstallMode.CHANGE_SETTINGS, board, board_test=None)
    assert rig.runner.calls  # it went on to write the settings


# ------------------------------------------------------------- the words

def test_every_finding_code_has_plain_advice():
    codes = ["PSRAM_NOT_FOUND", "PSRAM_WRONG_SIZE", "PSRAM_TOO_SMALL", "PSRAM_DATA_ERRORS",
             "PSRAM_CRASH", "PSRAM_STARTUP_FAULT", "IRAM_DATA_ERRORS", "FLASH_WRONG_SIZE",
             "FLASH_READ_UNSTABLE", "FLASH_WRITE_ERRORS", "CPU_MISMATCH", "WIFI_RADIO_FAILED",
             "WIFI_NO_NETWORKS", "TEST_CRASH", "STARTUP_HANG", "STARTUP_CRASH",
             "FLASH_CHIP_UNSUPPORTED", "EFUSE_CHANGED", "USB_DATA_GARBLED", "NO_START",
             "TEST_RESTARTED", "TEST_PROGRAM_REFUSED", "TEST_INCOMPLETE", "ERASE_NOT_FINISHED",
             "BOARD_TEST_REQUIRED", "BOARD_STARTS_NORMALLY", "POWER_PROBLEM",
             "FLASH_ID_CHANGED", "FLASH_TEST_AREA_MISSING",
             *[f"{stage.upper()}_HANG" for stage in boardtest.STAGES]]
    for code in codes:
        advice = texts.board_test_advice(code)
        assert advice.code == code, code
        if advice.illustration == "faulty_board":
            assert not advice.can_retry and "aside" in " ".join(advice.what_to_do)
    assert texts.board_test_advice("SOMETHING_NEW") is texts.TEST_FAILED
    assert texts.get("BOARD_TEST_REQUIRED") is texts.BOARD_TEST_REQUIRED


def test_a_missing_test_area_is_the_program_s_problem_not_the_board_s():
    run = scripts.program_run(MAC, results={"flash": {"result": "fail",
                                                      "code": "FLASH_TEST_AREA_MISSING",
                                                      "detail": "no_fltest_partition"}})
    verdict = judged(run)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE
    assert verdict.code == "FLASH_TEST_AREA_MISSING"
    assert verdict.advice is texts.FLASH_TEST_AREA_MISSING


# ------------------------------------------------------- review fixes (16 Nov)

@pytest.mark.parametrize("hardware", [HardwareVerdict.NOT_CHECKED,
                                      HardwareVerdict.FLASH_CHIP_UNSUPPORTED,
                                      HardwareVerdict.BOARD_TYPE_UNSUPPORTED])
@pytest.mark.parametrize("security", [SecurityState.KEY_BURNED_NOT_STARTED,
                                      SecurityState.BLANK])
def test_a_never_started_board_is_never_replugged_by_the_failed_board_check(
        tmp_path, hardware, security):
    # A replug would boot the production Secure Boot bootloader a stopped setup may have
    # written, and its first start burns eFuses.
    rig = Rig(tmp_path, None, replug_program=scripts.timed(scripts.LOCKED_STARTS_NORMALLY))
    board = blank_board(security=security, hardware=hardware)
    assert boardtest.never_started(board) and board_problems(board)
    report = check(rig, board)
    assert ReplugPhase.WAITING_UNPLUG not in rig.phases and rig.phases == []
    assert rig.runner.calls == [] and report.erased is None
    if hardware is HardwareVerdict.NOT_CHECKED:
        assert report.verdict is BoardTestVerdict.INCOMPLETE
        assert report.advice is texts.BOARD_TEST_NOT_POSSIBLE
    else:
        assert report.verdict is BoardTestVerdict.FAILED  # the gate's refusal, with proof
        assert report.findings[0].stage == "check"
        assert report.kind == ("locked_board" if security is not SecurityState.BLANK
                               else "vendor_test")


def test_a_cancel_just_before_begin_is_not_a_psram_fault():
    # Every normal start prints the PSRAM lines about 1.5 s before BEGIN.
    verdict = judged(scripts._boot(MAC), after=1.0, final=True)
    assert verdict.verdict is not BoardTestVerdict.FAILED and verdict.code == "NO_START"
    assert judged(scripts._boot(MAC), after=1.0, final=False) is None


def test_a_cancel_in_that_window_reports_cancelled(tmp_path):
    cancel = CancelToken()
    boot_only = scripts.timed([line for _at, line in scripts._boot(MAC)])
    rig = Rig(tmp_path, boot_only)

    def cancel_on_psram(event):
        if event.data.get("signal") == "psram_density":
            cancel.cancel()

    report = rig.tester.run_test(blank_board(), rig.progress.append, rig.on_phase,
                                 cancel_on_psram, cancel)
    assert report.verdict is BoardTestVerdict.INCOMPLETE and report.advice is texts.CANCELLED
    assert not any(f.code == "PSRAM_STARTUP_FAULT" for f in report.findings)


def test_a_decisive_fault_stands_after_a_cancel():
    lines = scripts.timed(scripts.PSRAM_HANG_A4E5E8)
    w = watch_of(lines)
    judge = TestJudge(MAC)
    now = w.last_line_at + boardtest.SETTLE_S + 0.1
    final = judge.check(w, now, final=True)
    assert final.code == "PSRAM_STARTUP_FAULT" and judge.decisive(w, now, final)


def without_end(run):
    return [(at, line) for at, line in run if not line.startswith("SLTEST END")]


def download_restart(after):
    return [(after, "ESP-ROM:esp32s3-20210327"),
            (after, "rst:0x3 (RTC_SW_SYS_RST),boot:0x23 (DOWNLOAD(USB/UART0))"),
            (after + 0.01, "waiting for download")]


@pytest.mark.parametrize("final", [True, False])
def test_a_lost_end_line_is_garbled_usb_not_a_crash(final):
    run = without_end(scripts.passing_run(MAC))
    last = run[-1][0]
    verdict = judged(run + download_restart(last + 0.3), after=0.5, final=final)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "USB_DATA_GARBLED"
    assert verdict.program_done and verdict.clean_ok


def test_a_lost_end_and_clean_then_download_mode_is_garbled_usb():
    run = [(at, line) for at, line in scripts.passing_run(MAC)
           if not line.startswith(("SLTEST END", "SLTEST CLEAN"))]
    last = run[-1][0]
    verdict = judged(run + download_restart(last + 1.5), after=0.5, final=False)
    assert verdict.code == "USB_DATA_GARBLED" and verdict.program_done
    assert not verdict.clean_ok  # the whole chip is erased


def test_a_lost_end_line_and_then_silence_is_garbled_usb_not_a_wifi_hang():
    run = [(at, line) for at, line in scripts.passing_run(MAC)
           if not line.startswith(("SLTEST END", "SLTEST CLEAN"))]
    verdict = judged(run, after=boardtest.SILENCE_S + 0.1, final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "USB_DATA_GARBLED"


def test_a_lost_end_line_never_passes_a_whole_run(tmp_path):
    rig = Rig(tmp_path, without_end(scripts.passing_run(MAC)))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE and not report.hardware_fault
    assert any(f.code == "USB_DATA_GARBLED" for f in report.findings)
    assert report.erased is True


def test_a_brownout_reset_without_its_text_is_a_power_problem():
    run = scripts.program_run(MAC, stop_after="wifi")
    last = run[-1][0]
    verdict = judged(run + [(last + 0.5, "ESP-ROM:esp32s3-20210327"),
                            (last + 0.5, "rst:0xf (BROWN_OUT_RST),boot:0x8 "
                                         "(SPI_FAST_FLASH_BOOT)")])
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "POWER_PROBLEM"


def test_brownout_restarts_before_begin_are_a_power_problem_not_psram():
    boot = [line for _at, line in scripts._boot(MAC)]
    brown = ["ESP-ROM:esp32s3-20210327", "rst:0xf (BROWN_OUT_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"]
    lines = scripts.timed(boot + brown + boot[2:] + brown + boot[2:])
    verdict = judged(lines, final=False)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "POWER_PROBLEM"
    locked = judged(lines, locked=True, board=sdm_board(), final=False)
    assert locked.code == "POWER_PROBLEM"


def test_a_locked_board_that_hangs_in_psram_start_up_is_a_hardware_fault():
    # Start-up lines up to "Multicore app", no panic, the RTC watchdog every ~10 s.
    start = list(scripts.PSRAM_HANG_A4E5E8[:13])
    lines = [(10.0 * n + 0.05 * i, line) for n in range(4) for i, line in enumerate(start)]
    w = watch_of(lines)
    w.resets = 3
    w.reset_times = (10.0, 20.0, 30.0)
    verdict = TestJudge(MAC, locked=True, board=sdm_board()).evaluate(w, 39.0, final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_STARTUP_FAULT"
    assert any("octal_psram" in line for line in verdict.findings[0].proof)


def test_a_locked_board_whose_start_stops_in_psram_then_goes_silent_is_a_fault():
    lines = scripts.timed(scripts.PSRAM_HANG_A4E5E8[:13])
    verdict = judged(lines, locked=True, board=sdm_board(), after=boardtest.SILENCE_S + 0.1,
                     final=False)
    assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_STARTUP_FAULT"
    # A board that got past start-up is not a PSRAM fault.
    verdict = judged(scripts.timed(scripts.LOCKED_STARTS_NORMALLY[:5]), locked=True,
                     board=sdm_board(), after=boardtest.SILENCE_S + 0.1, final=False)
    assert verdict is None or verdict.code != "PSRAM_STARTUP_FAULT"
