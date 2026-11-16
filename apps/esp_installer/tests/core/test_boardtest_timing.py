"""The board test on real-world USB timing (hardware-risk review, 16 Nov 2026): a BEGIN line
lost because the port came back late, a cut ROM line glued to a test line, a reset the
computer caused, a slow but good flash chip, and the port close order on macOS. None of
these may block a good board, and none may let a faulty board pass."""
from __future__ import annotations

from typing import List, Optional, Sequence

import pytest

from sl_installer.core import boardtest, boardtest_scripts as scripts, console
from sl_installer.core.api import BoardTestVerdict, ReplugPhase

from .support import MAC
from .test_boardtest import Rig, assert_never_burned, judged


# ----------------------------------------------- a cut line glued to a test line

def test_a_cut_rom_line_glued_to_begin_still_passes():
    run = [(at, ("rst:0x10 (RTCWDT_RT" + line) if line.startswith("SLTEST BEGIN") else line)
           for at, line in scripts.passing_run(MAC)]
    assert any(line.startswith("rst:0x10 (RTCWDT_RTSLTEST BEGIN") for _at, line in run)
    verdict = judged(run)
    assert verdict.verdict is BoardTestVerdict.PASSED and verdict.end_crc_ok
    assert verdict.measurements["psram_size"] == 8 * 1024 * 1024


def test_noise_glued_to_any_test_line_is_read_from_sltest():
    parser = console.ConsoleParser(MAC)
    event = parser.parse("\x00\xffgarbageSLTEST ALIVE stage=psram step=1 of=9 at=0")
    assert event.kind.name == "SELF_TEST" and event.data["sltest"] == "ALIVE"
    assert event.raw.startswith("SLTEST ALIVE")


def test_two_test_lines_glued_together_can_never_pass():
    run = scripts.passing_run(MAC)
    index = next(i for i, (_at, line) in enumerate(run) if "RESULT stage=psram" in line)
    glued = run[:index - 1] + [(run[index][0], run[index - 1][1] + run[index][1])] + \
        run[index + 1:]
    verdict = judged(glued)
    assert verdict.verdict is not BoardTestVerdict.PASSED
    assert verdict.code == "USB_DATA_GARBLED"


# --------------------------------------------------- a reset the computer caused

@pytest.mark.parametrize("reason, code", [("USB_UART_CHIP_RESET", "0x15"),
                                          ("USB_JTAG_CHIP_RESET", "0x16")])
@pytest.mark.parametrize("stage", ["psram", "flash"])
def test_a_reset_by_the_computer_is_not_a_board_fault(reason, code, stage):
    run = scripts.program_run(MAC, stop_after=stage)
    last = run[-1][0]
    reset = run + [(last + 0.5, "ESP-ROM:esp32s3-20210327"),
                   (last + 0.5, f"rst:{code} ({reason}),boot:0x8 (SPI_FAST_FLASH_BOOT)")]
    verdict = judged(reset)
    assert verdict.verdict is BoardTestVerdict.INCOMPLETE and verdict.code == "TEST_RESTARTED"
    assert reason in verdict.findings[-1].detail and "not a board fault" in \
        verdict.findings[-1].detail


def test_a_watchdog_reset_is_still_a_fault():
    run = scripts.program_run(MAC, stop_after="psram")
    last = run[-1][0]
    for reason in ("RTCWDT_RTC_RST", "TG0WDT_SYS_RST", "RTC_SW_CPU_RST"):
        verdict = judged(run + [(last + 1, f"rst:0x10 ({reason}),boot:0x8 "
                                           "(SPI_FAST_FLASH_BOOT)")])
        assert verdict.verdict is BoardTestVerdict.FAILED and verdict.code == "PSRAM_CRASH"


# ----------------------------------------------------- the port came back late

class StartsRig(Rig):
    """Rig whose board is like USB-Serial/JTAG (lines printed while no port is open are
    lost) and whose port comes back ``plug_after[n]`` seconds after start call n, playing
    ``programs[n]`` (None: nothing). The reset of each start ends the run before it."""

    def __init__(self, tmp_path, programs: Sequence[Optional[list]],
                 plug_after: Sequence[float], replug_program=None) -> None:
        super().__init__(tmp_path, None, replug_program=replug_program)
        self.serial.drop_unread = True
        self.programs = list(programs)
        self.plug_after = list(plug_after)

    def _started(self, fake) -> None:
        number = fake.started_programs - 1
        self.usb.present = False
        self.serial.generation += 1
        self.serial.pending = b""
        self.serial.timeline = []  # the reset ends whatever the program was going to print
        self.serial.at(self.clock.now + self.plug_after[min(number, len(self.plug_after) - 1)],
                       "plug")
        program = self.programs[min(number, len(self.programs) - 1)]
        if program is not None:
            self._play(program, 0.9)


def starts(rig: Rig) -> int:
    return rig.commands().count("write-mem")


def test_a_begin_lost_to_a_slow_port_is_started_again_and_passes(tmp_path):
    # BEGIN comes 2.4 s after the start; the port is back only after 2.6 s the first time.
    rig = StartsRig(tmp_path, [scripts.passing_run(MAC)], plug_after=[2.6, 0.8])
    report = rig.run()
    assert "SLTEST BEGIN" in " ".join(rig.serial.dropped)  # really lost, not just skipped
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert starts(rig) == 2 and rig.fake.started_programs == 2
    assert rig.commands() == ["get-security-info", "summary", "write-flash", "write-mem",
                              "write-mem", "get-security-info", "erase-region", "summary"]
    missed = [f for f in report.findings if f.code == "BEGIN_MISSED"]
    assert len(missed) == 1 and missed[0].result == "info"
    assert rig.phases == []  # no replug asked
    text = open(report.console_log, encoding="utf-8").read()
    assert "start call 1 finished" in text and "start call 2 finished" in text
    assert "the BEGIN line was missed" in text
    assert report.erased is True and report.efuses_unchanged is True
    assert_never_burned(rig)


def test_the_restart_happens_at_once_not_after_the_whole_run(tmp_path):
    rig = StartsRig(tmp_path, [scripts.passing_run(MAC)], plug_after=[2.6, 0.8])
    started = rig.clock.now
    rig.run()
    # Two runs of about 20 s would take over 40 s; the first is stopped at its first line.
    assert rig.clock.now - started < 40


def test_a_run_stopped_in_its_flash_stage_gets_the_whole_chip_erased(tmp_path):
    # The port came back so late that the first line seen is the flash stage's: that run
    # may have left test blocks the next run's CLEAN does not cover.
    run = scripts.passing_run(MAC)
    flash = next(at for at, line in run if "ALIVE stage=flash" in line)
    rig = StartsRig(tmp_path, [run], plug_after=[flash + 0.9 - 0.1, 0.8])
    report = rig.run()
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert starts(rig) == 2 and rig.fake.chip_erases == 1 and report.erased is True


def test_a_run_stopped_in_its_memory_stage_needs_no_whole_chip_erase(tmp_path):
    rig = StartsRig(tmp_path, [scripts.passing_run(MAC)], plug_after=[2.6, 0.8])
    rig.run()
    assert rig.fake.chip_erases == 0


def test_a_begin_lost_every_time_is_never_a_pass_and_never_a_third_start(tmp_path):
    rig = StartsRig(tmp_path, [scripts.passing_run(MAC)], plug_after=[2.6, 2.6])
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.advice.code == "USB_DATA_GARBLED"
    assert starts(rig) == boardtest.MAX_STARTS == 2
    assert report.erased is True
    assert_never_burned(rig)


def test_a_faulty_board_whose_begin_was_lost_still_fails(tmp_path):
    rig = StartsRig(tmp_path, [scripts.psram_errors_run(MAC)], plug_after=[2.6, 0.8])
    report = rig.run()
    assert report.verdict is BoardTestVerdict.FAILED and report.hardware_fault
    assert report.findings[0].code == "PSRAM_DATA_ERRORS"
    assert starts(rig) == 2 and report.erased is True


def test_a_faulty_board_whose_begin_is_always_lost_never_passes(tmp_path):
    rig = StartsRig(tmp_path, [scripts.psram_errors_run(MAC)], plug_after=[2.6, 2.6])
    report = rig.run()
    assert report.verdict is not BoardTestVerdict.PASSED


def test_after_a_replug_a_lost_begin_is_started_again(tmp_path):
    # Start 1 leaves the chip in download mode (nothing printed); after the replug the
    # power-on run arrives without its BEGIN (the port was back too late); start 2 is
    # seen whole.
    lost = [item for item in scripts.passing_run(MAC) if "SLTEST BEGIN" not in item[1]]
    rig = StartsRig(tmp_path, [None, scripts.passing_run(MAC)], plug_after=[0.8, 0.8],
                    replug_program=lost)
    rig.serial.drop_unread = False
    report = rig.run()
    assert rig.phases == [ReplugPhase.WAITING_UNPLUG, ReplugPhase.WAITING_REPLUG,
                          ReplugPhase.WATCHING]
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert starts(rig) == 2
    assert [f.code for f in report.findings].count("BEGIN_MISSED") == 1


# ------------------------------------------------- a slow but good flash chip

def stretched(run, factor):
    """``run`` with every gap after BEGIN ``factor`` times longer (a slow board)."""
    begin = next(at for at, line in run if line.startswith("SLTEST BEGIN"))
    return [(at if at < begin else begin + (at - begin) * factor, line) for at, line in run]


def test_a_slow_board_that_keeps_reporting_progress_passes_after_150_s(tmp_path):
    run = stretched(scripts.passing_run(MAC), 10)
    assert run[-1][0] > boardtest.WATCH_S + 30
    tests = [at for at, line in run if line.startswith("SLTEST") and "CLEAN" not in line]
    assert max(b - a for a, b in zip(tests, tests[1:])) < boardtest.SILENCE_S
    rig = Rig(tmp_path, run)
    report = rig.run()
    assert report.verdict is BoardTestVerdict.PASSED, report.findings
    assert report.erased is True and rig.fake.chip_erases == 0


def test_progress_without_an_end_stops_at_the_hard_limit(tmp_path):
    begin = scripts._begin(MAC)
    beats = [(1.5, begin)] + [(1.5 + 2 * i, scripts.sltest(
        "ALIVE", stage="flash", step=i, of=999, at=f"0x{i:08x}")) for i in range(1, 400)]
    rig = Rig(tmp_path, beats)
    started = rig.clock.now
    report = rig.run()
    assert report.verdict is BoardTestVerdict.INCOMPLETE
    assert report.advice.code == "TEST_INCOMPLETE"
    took = rig.clock.now - started
    assert boardtest.WATCH_MAX_S < took < boardtest.WATCH_MAX_S + 120  # + cleanup
    assert rig.fake.chip_erases == 1  # no CLEAN: the whole chip


def test_a_hang_is_still_found_by_silence_long_before_the_limit(tmp_path):
    rig = Rig(tmp_path, stretched(scripts.program_run(MAC, stop_after="flash"), 5))
    report = rig.run()
    assert report.verdict is BoardTestVerdict.FAILED and report.advice.code == "FLASH_HANG"


# ------------------------------------------------------ the port close order

class Lines:
    def __init__(self) -> None:
        self.order: List[str] = []
        self._rts = self._dtr = True

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value
        self.order.append(f"rts={int(value)}")
        assert not (self._rts and not self._dtr), "passed through the USJ reset state"

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value
        self.order.append(f"dtr={int(value)}")
        assert not (self._rts and not self._dtr), "passed through the USJ reset state"

    def close(self) -> None:
        self.order.append("close")


def test_on_macos_the_port_is_closed_rts_first(monkeypatch):
    monkeypatch.setattr(console.sys, "platform", "darwin")
    handle = Lines()
    console._close(handle)
    assert handle.order == ["rts=0", "dtr=0", "close"]


def test_on_windows_the_lines_are_left_alone(monkeypatch):
    monkeypatch.setattr(console.sys, "platform", "win32")
    handle = Lines()
    console._close(handle)
    assert handle.order == ["close"]


def test_a_gone_port_still_closes(monkeypatch):
    monkeypatch.setattr(console.sys, "platform", "darwin")

    class Gone:
        closed = False

        def __setattr__(self, name, value):
            if name in ("rts", "dtr"):
                raise OSError("device disconnected")
            object.__setattr__(self, name, value)

        def close(self):
            object.__setattr__(self, "closed", True)

    handle = Gone()
    console._close(handle)
    assert handle.closed
