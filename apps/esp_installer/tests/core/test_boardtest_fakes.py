"""The fake board test (core/fakes.py) that the screens, the screenshots and the self-test
use: each story gives the report the engine's own judge gives for its board."""
from __future__ import annotations

import pytest

from sl_installer.core import boardtest, fakes, texts
from sl_installer.core.api import (BoardTestVerdict, CancelToken, InstallerError, InstallMode,
                                   ReplugPhase)

from . import support


def services_for(scenario):
    services = fakes.make_fake_services(scenario, pace=0.0)
    board = services.boards.inspect(fakes.NATIVE, CancelToken())
    return services, board


def run_test(scenario):
    services, board = services_for(scenario)
    progress, events = [], []
    report = services.board_test.run_test(board, progress.append, lambda phase: None,
                                          events.append, CancelToken())
    return report, progress, events


@pytest.mark.parametrize("scenario, verdict, code", [
    ("board_test_pass", BoardTestVerdict.PASSED, "BOARD_TEST_PASSED"),
    ("happy_zkt", BoardTestVerdict.PASSED, "BOARD_TEST_PASSED"),
    ("board_test_psram_errors", BoardTestVerdict.FAILED, "PSRAM_DATA_ERRORS"),
    ("board_test_psram_hang", BoardTestVerdict.FAILED, "PSRAM_STARTUP_FAULT"),
    ("board_test_xmc_refused", BoardTestVerdict.FAILED, "FLASH_CHIP_UNSUPPORTED"),
    ("failed_board_resume", BoardTestVerdict.PASSED, "BOARD_TEST_PASSED"),
])
def test_the_fake_test_stories(scenario, verdict, code):
    report, progress, events = run_test(scenario)
    assert report.verdict is verdict and report.advice.code == code
    if scenario == "board_test_xmc_refused":
        assert progress == [] and events == [] and report.erased is None
    else:
        assert progress[-1].fraction == 1.0 and events
        assert report.erased is True and report.efuses_unchanged is True
    if scenario == "failed_board_resume":
        assert report.recovery == boardtest.RECOVERY_RESUME and report.keys_burned


def test_the_fake_psram_errors_story_carries_the_engine_s_proof():
    report, _progress, events = run_test("board_test_psram_errors")
    finding = report.findings[0]
    assert finding.code == "PSRAM_DATA_ERRORS" and "1,204 errors" in finding.detail
    end = [e for e in events if e.data.get("sltest") == "END"]
    assert end and end[0].data["crc_ok"] == "1"


@pytest.mark.parametrize("scenario, verdict, recovery", [
    ("failed_board_psram", BoardTestVerdict.FAILED, None),
    ("failed_board_settings", BoardTestVerdict.INCOMPLETE, boardtest.RECOVERY_FIX_SETTINGS),
    ("secured_board_fix_settings", BoardTestVerdict.PASSED, None),
])
def test_the_fake_failed_board_stories(scenario, verdict, recovery):
    services, board = services_for(scenario)
    phases = []
    report = services.board_test.check_failed_board(board, phases.append, lambda e: None,
                                                    CancelToken())
    assert report.verdict is verdict and report.recovery == recovery
    assert report.kind == "locked_board" and phases == list(ReplugPhase)
    assert report.efuses_unchanged is None and report.erased is None


def test_the_fake_installer_refuses_a_key_lock_without_a_passed_test():
    services, board = services_for("happy_zkt")
    config = support.zkt_config()
    with pytest.raises(InstallerError) as caught:
        services.installer.run(InstallMode.NEW_BOARD, board, config, lambda step: None,
                               CancelToken())
    assert caught.value.advice is texts.BOARD_TEST_REQUIRED
    world = fakes.fake_world(services)
    assert world.install_runs == 0
    failed, _p, _e = run_test("board_test_psram_errors")
    with pytest.raises(InstallerError):
        services.installer.run(InstallMode.NEW_BOARD, board, config, lambda step: None,
                               CancelToken(), board_test=failed)
    report = services.board_test.run_test(board, lambda p: None, lambda p: None,
                                          lambda e: None, CancelToken())
    services.installer.run(InstallMode.NEW_BOARD, board, config, lambda step: None,
                           CancelToken(), board_test=report)
    assert world.install_runs == 1


def test_the_fake_records_save_a_certificate_pair(tmp_path):
    services, board = services_for("board_test_pass")
    report, _p, _e = run_test("board_test_pass")
    pdf, evidence = services.records.save_board_test(report, "Field Tester")
    assert pdf.endswith(".pdf") and evidence.endswith(".zip")
    with open(pdf, "rb") as handle:
        assert handle.read(4) == b"%PDF"
    fakes.fake_world(services).cleanup()


def test_a_cancelled_fake_test_is_incomplete():
    services, board = services_for("board_test_pass")
    cancel = CancelToken()
    cancel.cancel()
    report = services.board_test.run_test(board, lambda p: None, lambda p: None,
                                          lambda e: None, cancel)
    assert report.verdict is BoardTestVerdict.INCOMPLETE and report.advice.code == "CANCELLED"


# ------------------------------------------------------------------ bench

def bench_run(monkeypatch, scenario, command, typed=fakes.BOARD_MAC):
    from sl_installer.core import bench

    services, _board = services_for(scenario)
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    args = bench.parser().parse_args([command, "--tester", "Bench Tester"])
    handler = bench.cmd_boardtest if command == "boardtest" else bench.cmd_failedboard
    try:
        return handler(services, args), fakes.fake_world(services)
    finally:
        fakes.fake_world(services).cleanup()


def test_bench_boardtest_tests_after_the_typed_board_id(monkeypatch, capsys):
    code, world = bench_run(monkeypatch, "board_test_pass", "boardtest")
    out = capsys.readouterr().out
    assert code == 0 and world.called("board_test.run_test") == 1
    assert world.called("records.save_board_test") == 1
    assert "PASSED" in out and "Certificate:" in out


def test_bench_boardtest_stops_without_the_typed_board_id(monkeypatch):
    with pytest.raises(SystemExit):
        bench_run(monkeypatch, "board_test_pass", "boardtest", typed="no")


def test_bench_boardtest_reports_a_faulty_board(monkeypatch, capsys):
    code, _world = bench_run(monkeypatch, "board_test_psram_errors", "boardtest")
    assert code == 4 and "PSRAM_DATA_ERRORS" in capsys.readouterr().out


def test_bench_failedboard_only_watches_a_locked_board(monkeypatch, capsys):
    code, world = bench_run(monkeypatch, "failed_board_psram", "failedboard", typed="")
    out = capsys.readouterr().out
    assert code == 4 and "Nothing is written to this board" in out
    assert world.called("board_test.check_failed_board") == 1
    assert world.called("board_test.run_test") == 0
    assert "PSRAM_STARTUP_FAULT" in out


def test_bench_failedboard_never_replugs_a_board_that_never_started(monkeypatch, capsys):
    # A key-burned board outside SDM whose hardware was not checked: a replug could boot the
    # production Secure Boot bootloader. It goes to run_test, which refuses it untouched.
    from dataclasses import replace as _replace

    from sl_installer.core import bench
    from sl_installer.core.api import HardwareVerdict, SecurityState

    services, _board = services_for("board_test_pass")
    inspect = services.boards.inspect
    monkeypatch.setattr(services.boards, "inspect", lambda candidate, cancel: _replace(
        inspect(candidate, cancel), security=SecurityState.KEY_BURNED_NOT_STARTED,
        hardware=HardwareVerdict.NOT_CHECKED))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    args = bench.parser().parse_args(["failedboard", "--tester", "Bench Tester"])
    world = fakes.fake_world(services)
    try:
        bench.cmd_failedboard(services, args)
    finally:
        world.cleanup()
    out = capsys.readouterr().out
    assert world.called("board_test.check_failed_board") == 0
    assert world.called("board_test.run_test") == 1
    assert "Unplug the board now" not in out and "not replugged" in out
