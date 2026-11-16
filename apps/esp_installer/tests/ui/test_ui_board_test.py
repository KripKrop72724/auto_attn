"""The board test on the screens (1.2.0): Test a new board, Check a failed board, and the
test that comes before every key lock. The board test itself is scripted
(screenshots.ScriptedBoardTest); its engine has its own tests in tests/core."""

from __future__ import annotations

import os

import pytest

from sl_installer.core import api, texts
from sl_installer.core.api import ConsoleEvent, ConsoleKind, InstallMode, ReplugPhase, Severity
from sl_installer.ui import controller as controller_module
from sl_installer.ui import pages, wording
from sl_installer.ui.widgets import StateIcon


def _verdict_title(h) -> str:
    return h.page(pages.BOARD_TEST_RESULT).verdict.title.text()


def _files(h):
    return dict(h.controller.session.test_files)


# ------------------------------------------------------------------- home


def test_home_offers_both_board_test_options(harness):
    h = harness("happy_zkt")
    h.signed_in()
    home = h.page(pages.HOME)
    assert home.test_card.isVisible() and home.test_card.isEnabled()
    assert home.failed_card.isVisible() and home.failed_card.isEnabled()
    assert home.test_card.accessibleName() == "Test a new board"
    assert home.failed_card.accessibleName() == "Check a failed board"


def test_board_test_options_stay_on_when_setup_is_switched_off(harness):
    # The test needs no head office: switching setup off must not switch it off.
    h = harness("installer_off")
    h.signed_in()
    h.idle("capabilities")
    home = h.page(pages.HOME)
    h.wait(lambda: not home.setup_card.isEnabled())
    assert home.test_card.isEnabled() and home.failed_card.isEnabled()
    home.test_card.click()
    h.wait_page(pages.CONNECT)


# ------------------------------------------------------------ test a new board


def test_new_board_passes_and_gets_a_certificate(harness, tmp_path):
    h = harness("happy_zkt")
    tester = h.scripted("pass")
    h.board_test_result()
    assert tester.calls == [("run_test", h.controller.session.board.mac)]
    assert _verdict_title(h).startswith("PASSED")
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.tester_name() == "admin"  # the signed-in person tested it
    files = _files(h)
    assert list(files) == [controller_module.PASSED_CERT]
    pdf, archive = files[controller_module.PASSED_CERT]
    assert os.path.isfile(pdf) and os.path.isfile(archive)
    assert page.primary_button.text() == "Save certificate (PDF)"
    assert page.secondary_button.text() == "Save evidence (ZIP)"
    assert all(result == "ok" for _name, result, _words in page.rows)
    assert h.window.steps._labels == list(controller_module.TEST_STEPS)

    # Save a copy: the dialog's answer comes from ask_save_path (patched here).
    target = tmp_path / "copy"
    h.controller.ask_save_path = lambda *_args: str(target)
    h.act(pages.BOARD_TEST_RESULT, "save_certificate")
    h.wait(lambda: (tmp_path / "copy.pdf").is_file())
    assert (tmp_path / "copy.pdf").read_bytes() == open(pdf, "rb").read()
    h.wait(lambda: "copy.pdf" in page.saved.what_happened.text())


def test_a_new_tested_by_name_saves_the_files_again(harness, tmp_path):
    h = harness("happy_zkt")
    h.scripted("pass")
    h.board_test_result()
    names = []
    save = h.services.records.save_board_test

    def counted(report, tester, kind=None):
        names.append(tester)
        return save(report, tester, kind)

    h.services.records.save_board_test = counted
    page = h.page(pages.BOARD_TEST_RESULT)
    page.tester.setText("Asad Ullah")
    h.controller.ask_save_path = lambda *_args: str(tmp_path / "evidence.zip")
    h.act(pages.BOARD_TEST_RESULT, "save_evidence")
    h.wait(lambda: (tmp_path / "evidence.zip").is_file())
    assert names == ["Asad Ullah"]
    assert h.controller.session.test_saved_by == "Asad Ullah"


def test_a_copy_needs_a_tester_name(harness):
    h = harness("happy_zkt")
    h.scripted("pass")
    h.board_test_result()
    asked = []
    h.controller.ask_save_path = lambda *args: asked.append(args)
    page = h.page(pages.BOARD_TEST_RESULT)
    page.tester.setText("  ")
    h.act(pages.BOARD_TEST_RESULT, "save_certificate")
    assert not asked
    assert page.saved.advice.code == "TESTED_BY_MISSING"


def test_cancelled_save_dialog_saves_no_copy(harness):
    h = harness("happy_zkt")
    h.scripted("pass")
    h.board_test_result()
    h.controller.ask_save_path = lambda *_args: None
    h.act(pages.BOARD_TEST_RESULT, "save_certificate")
    assert not h.controller.running("certificate_copy")
    assert h.page(pages.BOARD_TEST_RESULT).saved.advice.code == "BOARD_TEST_SAVED"


def test_memory_fault_is_not_fit_for_use(harness):
    h = harness("happy_zkt")
    h.scripted("memory_errors")
    h.board_test_result()
    advice = h.page(pages.BOARD_TEST_RESULT).verdict.advice
    assert advice.severity is Severity.ERROR and advice.title.startswith("NOT FIT FOR USE")
    assert "extra memory gives wrong data" in advice.what_happened
    assert list(_files(h)) == [controller_module.VENDOR_CERT]
    rows = {name: result for name, result, _words in h.page(pages.BOARD_TEST_RESULT).rows}
    assert rows["Extra memory"] == "fail" and rows["Wi-Fi radio"] == "ok"
    assert h.page(pages.BOARD_TEST_RESULT).retest_link.isVisible()


def test_memory_chip_refusal_gives_a_certificate(harness):
    # The board check refuses an XMC memory chip before anything is copied to the board.
    h = harness("flash_chip_unsupported")
    h.scripted("pass")
    h.board_test_result()
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.code == "BOARD_TEST_FAILED"
    assert "memory chip" in page.verdict.advice.what_happened
    assert [row[0] for row in page.rows] == ["Board check"]
    assert list(_files(h)) == [controller_module.VENDOR_CERT]


def test_test_that_could_not_finish_offers_test_again(harness):
    h = harness("happy_zkt")
    tester = h.scripted("no_start")
    h.board_test_result()
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.severity is Severity.WARNING
    assert page.verdict.advice.title == "The test could not finish"
    assert list(_files(h)) == [controller_module.REPORT_CERT]
    assert page.verdict.button_for("Test again") is not None
    page.verdict.button_for("Test again").click()
    h.wait_page(pages.BOARD_TEST_RESULT)
    h.wait(lambda: len(tester.calls) == 2)


def test_replug_steps_show_while_the_engine_asks(harness):
    h = harness("happy_zkt")
    tester = h.scripted("no_start", "unplug")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.phase is ReplugPhase.WAITING_UNPLUG)
    assert page.replug_card.isVisible()
    assert page.cards["unplug"].property("state") == "active"
    tester.release("unplug")
    h.wait_page(pages.BOARD_TEST_RESULT)


def test_locked_board_cannot_take_a_new_board_test(harness):
    h = harness("secured_board_fix_settings")
    tester = h.scripted("locked_memory")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.problem.isVisible())
    assert page.problem.advice.code == "BOARD_LOCKED_NO_TEST"
    assert tester.calls == []
    # The way on: check it as a failed board, without reading it again.
    page.problem.button_for("Check a failed board").click()
    h.wait_page(pages.BOARD_TEST_RESULT)
    assert tester.calls[0][0] == "check_failed_board"


def test_stopping_the_test_and_the_window_rules(harness):
    h = harness("happy_zkt")
    tester = h.scripted("pass", "psram")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.stage == "psram")
    assert not h.window.top_bar.sign_out_button.isEnabled()
    assert h.controller.close_question() is not None
    assert h.controller.close_blocker() is None  # testing, not writing
    h.controller.session.test_stage = "write"
    assert h.controller.close_blocker() is not None
    h.controller.session.test_stage = "psram"
    assert page.secondary_button.text() == "Stop the test"
    h.act(pages.BOARD_TEST, "cancel")
    h.wait_page(pages.BOARD_TEST_RESULT)
    assert h.controller.session.test_report.verdict is api.BoardTestVerdict.INCOMPLETE
    assert tester.calls and h.window.top_bar.sign_out_button.isEnabled()


def test_no_stop_while_the_test_program_is_removed(harness):
    h = harness("happy_zkt")
    h.scripted("pass", "cleanup")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.stage == "cleanup")
    assert not page.secondary_button.isVisible()
    assert h.controller.close_blocker() is not None


# --------------------------------------------------------- check a failed board


def test_failed_board_with_a_memory_fault_gets_a_vendor_claim(harness):
    h = harness("secured_board_fix_settings")
    tester = h.scripted("locked_memory")
    h.board_test_result("failed_board")
    assert tester.calls[0][0] == "check_failed_board"  # a locked board is only watched
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.code == "BOARD_TEST_FAILED"
    assert set(_files(h)) == {controller_module.VENDOR_CERT, controller_module.REPORT_CERT}
    assert page.primary_button.text() == "Save vendor claim (PDF)"
    assert page.secondary_button.text() == "Save fault report (PDF)"
    assert page.verdict.buttons == []  # nothing to fix: the hardware is faulty
    assert h.window.steps._labels == list(controller_module.FAILED_STEPS)


def test_failed_board_watch_shows_the_replug_steps(harness):
    h = harness("secured_board_fix_settings")
    tester = h.scripted("locked_memory", "unplug")
    h.board_test("failed_board")
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.phase is ReplugPhase.WAITING_UNPLUG)
    assert page.title_label.text() == "Checking the failed board"
    assert not page.bar.isVisible()
    tester.release("unplug")
    h.wait_page(pages.BOARD_TEST_RESULT)


def test_failed_board_with_missing_settings_leads_to_fix_settings(harness):
    h = harness("secured_board_fix_settings")
    h.scripted("locked_settings")
    h.board_test_result("failed_board")
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.code == "BOARD_SETTINGS_FIXABLE"
    assert list(_files(h)) == [controller_module.REPORT_CERT]
    page.verdict.button_for("Fix the settings").click()
    h.wait_page(pages.FIX_SETTINGS)
    assert h.controller.session.fixing
    h.act(pages.FIX_SETTINGS, "back")
    h.wait_page(pages.BOARD_TEST_RESULT)
    assert h.controller.session.test_files  # not saved twice


def test_key_burned_board_is_tested_and_its_setup_can_be_finished(harness):
    h = harness("resume_after_key")
    tester = h.scripted("resume_pass")
    h.board_test_result("failed_board")
    assert tester.calls[0][0] == "run_test"  # not locked yet: the test program can go on
    report = h.controller.session.test_report
    assert report.kind == "locked_board" and report.keys_burned and report.passed
    page = h.page(pages.BOARD_TEST_RESULT)
    page.verdict.button_for("Finish the setup").click()
    h.wait(lambda: h.controller.current in (pages.MACHINE, pages.DETAILS))
    session = h.controller.session
    assert session.flow is controller_module.Flow.SETUP and session.test_report is report


def test_never_set_up_board_can_be_tested_from_the_failed_board_check(harness):
    h = harness("happy_zkt")
    tester = h.scripted("pass")
    h.board_test("failed_board")
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.problem.isVisible())
    assert page.problem.advice.code == "BOARD_NEVER_SET_UP"
    page.problem.button_for("Test this board").click()
    h.wait_page(pages.BOARD_TEST_RESULT)
    assert tester.calls[0][0] == "run_test"
    assert _verdict_title(h).startswith("PASSED")


# ---------------------------------------------------------------- the setup


def test_setup_tests_the_board_before_confirm_and_hands_the_report_on(harness):
    h = harness("happy_zkt")
    tester = h.scripted("pass")
    seen = []
    run = h.services.installer.run

    def installer_run(mode, board, config, on_step, cancel, board_test=None):
        seen.append(board_test)
        return run(mode, board, config, on_step, cancel, board_test=board_test)

    h.services.installer.run = installer_run
    h.checked()
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.BOARD_TEST)
    assert h.window.steps._labels == list(controller_module.SETUP_STEPS)
    h.wait_page(pages.CONFIRM)
    assert len(tester.calls) == 1
    confirm = h.page(pages.CONFIRM)
    assert "passed its hardware test" in confirm.notice.body.text()
    confirm.understand.setChecked(True)
    h.click_primary(pages.CONFIRM)
    h.wait(lambda: bool(seen))
    assert seen[0] is h.controller.session.test_report and seen[0].passed


def test_failed_test_blocks_the_setup(harness):
    h = harness("happy_zkt")
    h.scripted("memory_errors")
    h.checked()
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.BOARD_TEST_RESULT)
    page = h.page(pages.BOARD_TEST_RESULT)
    assert "The board was not locked" in page.verdict.advice.what_happened
    assert page.another_link.text() == "Use another board"
    assert h.world.install_runs == 0
    # Back goes to the check, never on to Confirm.
    assert h.controller._back_target() == pages.CHECKING


def test_setup_without_a_passed_test_is_refused_and_tests_again(harness):
    h = harness("happy_zkt")
    tester = h.scripted("pass")

    def installer_run(mode, board, config, on_step, cancel, board_test=None):
        raise api.InstallerError(texts.BOARD_TEST_REQUIRED, cause="no board test")

    h.services.installer.run = installer_run
    h.start_install()
    page = h.page(pages.INSTALLING)
    h.wait(lambda: page.problem.isVisible())
    assert page.problem.advice.code == "BOARD_TEST_REQUIRED"
    assert page.primary_button.text() == "Test the board"
    h.click_primary(pages.INSTALLING)
    h.wait_page(pages.CONFIRM)
    assert len(tester.calls) == 2


def test_a_fresh_pass_is_only_for_the_same_board():
    board = api.BoardInfo("COM7", "ac:27:6e:a4:3d:30", "ESP32-S3", 16, 8,
                          api.HardwareVerdict.OK, api.SecurityState.BLANK, False)
    from sl_installer.ui.screenshots import ScriptedBoardTest

    report = ScriptedBoardTest("pass")._report(
        board, "passed", (), "2026-11-16T10:00:00+05:00", erased=True
    )
    fresh = controller_module.Controller.fresh_pass
    assert fresh(report, board)
    other = api.BoardInfo("COM7", "ac:27:6e:a4:3d:31", "ESP32-S3", 16, 8,
                          api.HardwareVerdict.OK, api.SecurityState.BLANK, False)
    assert not fresh(report, other)
    import dataclasses

    assert not fresh(dataclasses.replace(report, erased=False), board)
    assert not fresh(dataclasses.replace(report, efuses_unchanged=None), board)
    assert not fresh(dataclasses.replace(report, finished_at="2020-01-01T00:00:00+05:00"), board)
    failed = dataclasses.replace(report, verdict=api.BoardTestVerdict.FAILED)
    assert not fresh(failed, board)


# ----------------------------------------------------------------- wording


@pytest.mark.parametrize(
    "raw, words",
    [
        ("SLTEST BEGIN proto=1 fw=1.0.0", "The test program started."),
        ("SLTEST RESULT stage=psram result=ok code=OK errors=0 ms=1", "Extra memory: passed."),
        ("SLTEST RESULT stage=flash result=fail code=FLASH_WRITE_ERRORS", "Memory chip: failed."),
        ("SLTEST RESULT stage=wifi result=warn code=WIFI_NO_NETWORKS",
         "Wi-Fi radio: works, but found no Wi-Fi nearby."),
        ("SLTEST END result=ok failed=none crc=1c291ca3",
         "The test program finished. Every test passed."),
    ],
)
def test_test_program_lines_in_plain_words(raw, words):
    said = wording.console_words(ConsoleEvent(ConsoleKind.SELF_TEST, raw, {}))
    assert said is not None and said[0] == words


def test_heartbeats_are_not_shown():
    event = ConsoleEvent(ConsoleKind.SELF_TEST, "SLTEST ALIVE stage=psram step=1 of=9 at=0", {})
    assert wording.console_words(event) is None


def test_result_rows_mark_the_failed_stage(harness):
    h = harness("happy_zkt")
    h.scripted("memory_errors")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait_page(pages.BOARD_TEST_RESULT)
    assert page.steps.state_of("memory") == StateIcon.FAILED
    assert page.steps.state_of("wifi") == StateIcon.DONE


def test_resume_mode_is_tested_too(harness):
    h = harness("resume_after_key")
    tester = h.scripted("resume_pass")
    h.checked()
    assert h.controller.session.mode is InstallMode.RESUME
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.CONFIRM)
    assert [call[0] for call in tester.calls] == ["run_test"]


# ------------------------------------------- on the fake stories (the engine's judge)
#
# core/fakes.py's board test runs the engine's own judge on scripted consoles, so these
# show the screens with the findings and words the engine really gives.


def test_fake_story_pass(harness):
    h = harness("board_test_pass")
    h.board_test_result()
    assert h.world.called("board_test.run_test") == 1
    assert _verdict_title(h).startswith("PASSED")
    assert list(_files(h)) == [controller_module.PASSED_CERT]


@pytest.mark.parametrize("story", ["board_test_psram_errors", "board_test_psram_hang"])
def test_fake_story_memory_faults(harness, story):
    h = harness(story)
    h.board_test_result()
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.code == "BOARD_TEST_FAILED"
    assert "extra memory" in page.verdict.advice.what_happened
    assert list(_files(h)) == [controller_module.VENDOR_CERT]
    assert any(result == "fail" for _name, result, _words in page.rows)


def test_fake_story_xmc_refusal_writes_nothing(harness):
    h = harness("board_test_xmc_refused")
    h.board_test_result()
    report = h.controller.session.test_report
    assert report.hardware_fault and report.erased is None  # nothing was written
    assert [finding.stage for finding in report.findings] == ["check"]
    assert h.page(pages.BOARD_TEST_RESULT).verdict.advice.code == "BOARD_TEST_FAILED"


def test_fake_story_locked_memory_fault(harness):
    h = harness("failed_board_psram")
    h.board_test_result("failed_board")
    assert h.world.called("board_test.check_failed_board") == 1
    assert h.world.called("board_test.run_test") == 0
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.advice.code == "BOARD_TEST_FAILED"
    assert set(_files(h)) == {controller_module.VENDOR_CERT, controller_module.REPORT_CERT}
    assert page.primary_button.text() == "Save vendor claim (PDF)"


def test_fake_story_locked_settings_can_be_fixed(harness):
    h = harness("failed_board_settings")
    h.board_test_result("failed_board")
    page = h.page(pages.BOARD_TEST_RESULT)
    assert h.controller.session.test_report.recovery == "FIX_SETTINGS"
    page.verdict.button_for("Fix the settings").click()
    h.wait_page(pages.FIX_SETTINGS)


def test_fake_story_resume_after_the_key(harness):
    h = harness("failed_board_resume")
    h.board_test_result("failed_board")
    assert h.world.called("board_test.run_test") == 1
    page = h.page(pages.BOARD_TEST_RESULT)
    assert page.verdict.button_for("Finish the setup") is not None


def test_fake_installer_refuses_a_setup_without_a_test(harness):
    # The fake installer applies the engine's own rule (boardtest.check_board_test).
    h = harness("happy_zkt")
    h.to_confirm()
    h.controller.session.test_report = None
    page = h.page(pages.CONFIRM)
    page.understand.setChecked(True)
    h.click_primary(pages.CONFIRM)
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "BOARD_TEST_REQUIRED"
    assert h.world.install_runs == 0  # refused before anything ran
    assert installing.primary_button.text() == "Test the board"


# ---------------------------------------------------- review fixes (16 Nov)


def test_no_back_while_a_test_runs_after_a_problem_card(harness):
    # Check a failed board on a blank board, then Test this board: Back must go away.
    h = harness("happy_zkt")
    h.scripted("pass", "psram")
    h.board_test("failed_board")
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.problem.isVisible())
    assert page.back_button.isVisible()
    page.problem.button_for("Test this board").click()
    h.wait(lambda: page.stage == "psram")
    assert h.controller.running("board_test")
    assert not page.back_button.isVisible()
    h.controller.back()  # the keyboard or a stale click: refused while it runs
    assert h.controller.current == pages.BOARD_TEST and h.controller.running("board_test")
    assert h.controller.close_question() is not None


def test_no_back_while_a_failed_board_is_watched_after_a_problem_card(harness):
    # Test a new board on a locked board, then Check a failed board.
    h = harness("secured_board_fix_settings")
    tester = h.scripted("locked_memory", "unplug")
    h.board_test()
    page = h.page(pages.BOARD_TEST)
    h.wait(lambda: page.problem.isVisible())
    page.problem.button_for("Check a failed board").click()
    h.wait(lambda: page.phase is ReplugPhase.WAITING_UNPLUG)
    assert not page.back_button.isVisible()
    h.controller.back()
    assert h.controller.current == pages.BOARD_TEST and h.controller.running("board_test")
    tester.release("unplug")
    h.wait_page(pages.BOARD_TEST_RESULT)


def test_vendor_claim_and_fault_report_share_the_evidence_zip(harness, tmp_path):
    h = harness("secured_board_fix_settings")
    h.scripted("locked_memory")
    h.board_test_result("failed_board")
    files = _files(h)
    archive = files[controller_module.VENDOR_CERT][1]
    assert files[controller_module.REPORT_CERT][1] == archive and os.path.isfile(archive)
    target = tmp_path / "evidence"
    h.controller.ask_save_path = lambda *_args: str(target)
    h.act(pages.BOARD_TEST_RESULT, "save_evidence")
    h.wait(lambda: (tmp_path / "evidence.zip").is_file())
    assert (tmp_path / "evidence.zip").read_bytes() == open(archive, "rb").read()


def _stand_in(security=api.SecurityState.BLANK, sdm=False, **fields):
    from types import SimpleNamespace

    board = SimpleNamespace(security=security, secure_download_mode=sdm)
    values = dict(mac="ac:27:6e:a4:3d:30", finished_at="", kind="vendor_test",
                  keys_burned=False, efuses_unchanged=None, erased=None, board=board,
                  findings=(), recovery=None, advice=None,
                  verdict=api.BoardTestVerdict.FAILED)
    values.update(fields)
    return SimpleNamespace(**values)


def _security_row(report) -> str:
    from sl_installer.ui.pages.board_test_result import BoardTestResultPage

    return dict(BoardTestResultPage._facts(report))["Board's security"]


def test_a_new_board_refused_by_the_gate_is_never_called_locked():
    words = _security_row(_stand_in())
    assert "locked" not in words.lower() and words.startswith("Not checked")
    assert "locked" not in _security_row(_stand_in(erased=False)).lower()
    sdm = _stand_in(api.SecurityState.SECURED_STATE_LIFE, sdm=True, kind="locked_board")
    assert _security_row(sdm) == "Could not be read. The board is locked."


def test_an_efuse_change_on_a_key_burned_board_is_the_red_alarm():
    report = _stand_in(api.SecurityState.KEY_BURNED_NOT_STARTED, kind="locked_board",
                       keys_burned=True, efuses_unchanged=False, erased=True)
    assert _security_row(report) == "Changed during the test. Tell head office at once."
    same = _stand_in(api.SecurityState.KEY_BURNED_NOT_STARTED, kind="locked_board",
                     keys_burned=True, efuses_unchanged=True, erased=True)
    assert _security_row(same).startswith("Locked for State Life")


@pytest.mark.parametrize("verdict", [api.BoardTestVerdict.FAILED,
                                     api.BoardTestVerdict.INCOMPLETE])
def test_a_stopped_resume_setup_never_says_the_board_was_not_locked(verdict):
    finding = api.TestFinding("PSRAM_DATA_ERRORS", "psram", "fail", "The memory fails", "x", ())
    keyed = _stand_in(api.SecurityState.KEY_BURNED_NOT_STARTED, kind="locked_board",
                      keys_burned=True, verdict=verdict, findings=(finding,))
    words = wording.board_test_verdict(keyed, during_setup=True).what_happened
    assert "was not locked" not in words and "not finished" in words
    blank = _stand_in(verdict=verdict, findings=(finding,))
    assert "The board was not locked" in wording.board_test_verdict(
        blank, during_setup=True).what_happened
