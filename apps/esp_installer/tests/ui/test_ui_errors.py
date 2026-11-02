"""Each problem story shows the right advice and the right way forward."""

from __future__ import annotations

import sys

import pytest

from sl_installer.core import fakes, texts
from sl_installer.core.api import (
    BootOutcome,
    BootReport,
    Family,
    InstallerError,
    InstallMode,
    PresenceReport,
    PresenceState,
    ReplugPhase,
    StepId,
    StepProgress,
    steps_for,
)
from sl_installer.ui import pages


def sign_in_advice(h):
    h.wait(lambda: h.page(pages.SIGN_IN).advice.isVisible())
    return h.page(pages.SIGN_IN).advice.advice


def test_wrong_password_then_pause(harness):
    h = harness("login_wrong_password")
    h.sign_in("wrong-pass")
    assert sign_in_advice(h).code == "WRONG_PASSWORD"
    assert h.controller.current == pages.SIGN_IN
    assert h.page(pages.SIGN_IN).primary_button.isEnabled()
    for _ in range(2):
        h.page(pages.SIGN_IN).password.setText("wrong-pass")
        h.click_primary(pages.SIGN_IN)
        h.wait(lambda: not h.controller.running("login"))
    assert sign_in_advice(h).code == "LOGIN_PAUSED"


def test_sign_in_paused(harness):
    h = harness("login_paused")
    h.sign_in()
    assert sign_in_advice(h).code == "LOGIN_PAUSED"


def test_no_internet(harness):
    h = harness("no_internet")
    h.sign_in()
    assert sign_in_advice(h).illustration == "no_internet"
    h.wait(lambda: h.window.top_bar.internet_chip.text == "No internet")
    assert h.window.top_bar.server_chip.state == "bad"


def test_server_down(harness):
    h = harness("server_down")
    h.sign_in()
    assert sign_in_advice(h).code == "SERVER_UNREACHABLE"
    h.wait(lambda: h.window.top_bar.server_chip.text == "Head office not answering")
    assert h.window.top_bar.internet_chip.state == "ok"


def test_no_board_then_found(harness):
    h = harness("no_board_then_found")
    h.hold("board_found")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "USB_NO_BOARD"  # the engine's advice, after 15 s
    assert connect.problem.advice.illustration == "cable"
    assert not connect.primary_button.isEnabled()
    h.release("board_found")
    h.wait(lambda: connect.primary_button.isEnabled())
    assert not connect.problem.isVisible() and connect.found.isVisible()


def test_wrong_socket(harness):
    h = harness("wrong_socket")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "USB_WRONG_SOCKET"
    assert connect.problem.advice.illustration == "usb_socket"
    assert not connect.primary_button.isEnabled()


def test_board_running_other_software_then_found_in_setup_mode(harness):
    h = harness("other_usb_mode")
    h.hold("setup_mode")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    advice = connect.problem.advice
    assert advice.code == "USB_SETUP_MODE_NEEDED" and advice.illustration == "boot_button"
    # The story shows the engine's own steps, so its screenshot shows them too.
    assert (advice.title, advice.what_happened, advice.what_to_do) == (
        texts.SETUP_MODE_NEEDED.title,
        texts.SETUP_MODE_NEEDED.what_happened,
        texts.SETUP_MODE_NEEDED.what_to_do,
    )
    assert connect.waiting.isVisible()  # still looking: the board is found by itself
    assert not connect.primary_button.isEnabled() and not connect.found.isVisible()
    h.qtbot.wait(50)
    assert h.world.called("boards.inspect") == 0
    h.release("setup_mode")  # BOOT held through RST: the board restarts in setup mode
    h.wait(lambda: connect.primary_button.isEnabled())
    assert not connect.problem.isVisible() and connect.found.isVisible()
    assert h.controller.current == pages.CONNECT  # it waits for Next, as the last step says
    h.click_primary(pages.CONNECT)
    h.wait_page(pages.CHECKING)
    h.wait(lambda: h.page(pages.CHECKING).result.isVisible())
    assert h.page(pages.CHECKING).result.advice.code == "NEW_BOARD"


def test_setup_mode_steps_stay_up_while_the_board_restarts(harness):
    # RST without BOOT, or the board's own software, restarts it: gone for a moment, then
    # back in that software. Its steps stay up instead of flashing away while being read.
    h = harness("other_usb_mode")
    h.hold("setup_mode")  # the story sends nothing more: the test sends the reports
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    h.idle("presence_advice")
    explained = h.world.called("diagnosis.diagnose")
    gap = [PresenceReport(PresenceState.NO_BOARD, seconds_in_state=s) for s in (0.0, 1.0, 2.9)]
    back = PresenceReport(PresenceState.OTHER_USB_MODE, (fakes.OTHER_USB,), seconds_in_state=0.0)
    for report in gap + [back]:
        h.controller._on_presence(report)
        assert connect.problem.isVisible(), report
        assert connect.problem.advice.code == "USB_SETUP_MODE_NEEDED"
    h.qtbot.wait(50)
    assert h.world.called("diagnosis.diagnose") == explained  # the same advice stayed up
    # Gone for longer: unplugged, so the screen looks for the board again.
    for report in gap + [PresenceReport(PresenceState.NO_BOARD, seconds_in_state=3.0)]:
        h.controller._on_presence(report)
    assert not connect.problem.isVisible() and connect.waiting.isVisible()
    h.controller._on_presence(back)  # plugged in again, still in its own software
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "USB_SETUP_MODE_NEEDED"


def test_several_boards(harness):
    h = harness("several_boards")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "USB_SEVERAL_BOARDS"


def test_faulty_board_is_reported_once(harness):
    h = harness("faulty_restart_loop")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "BOARD_FAULTY"
    assert connect.problem.advice.illustration == "faulty_board"
    assert not connect.waiting.isVisible()  # no point waiting for this board
    assert not connect.primary_button.isEnabled()
    h.wait(lambda: len(h.world.reports) == 1)
    h.qtbot.wait(100)  # the watcher keeps reporting the loop
    assert [report[2] for report in h.world.reports] == ["BOARD_FAULTY"]
    assert h.world.reports[0][1] == "ac:27:6e:a4:3d:30"


def test_already_set_up_board_gets_new_settings(harness):
    h = harness("secured_board_fix_settings")
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "ALREADY_SET_UP"
    assert checking.primary_button.text() == "Change its settings"
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.FIX_SETTINGS)
    h.idle("bundles")
    fix = h.page(pages.FIX_SETTINGS)
    assert fix.warning_card.isVisible()  # it was used at a site
    assert not fix.primary_button.isEnabled()  # until the ZONE ID is typed
    # The typed details win over what head office has, in this setup.
    assert fix.form.zone_id.text() == "LF-ZONE-BLD9-04"
    fix.confirm_edit.setText("lf-zone-bld9-02")
    assert fix.primary_button.isEnabled()
    # The comm key is never carried over from the Details defaults: only the machine knows it.
    assert fix.form.zkt_comm_key.text() == ""
    h.click_primary(pages.FIX_SETTINGS)
    assert h.controller.current == pages.FIX_SETTINGS
    assert fix.form.zkt_comm_key_field.error.text()
    fix.form.zkt_comm_key.setText("12345")
    h.click_primary(pages.FIX_SETTINGS)
    h.wait_page(pages.CONFIRM)
    confirm = h.page(pages.CONFIRM)
    assert confirm.mode is InstallMode.CHANGE_SETTINGS and not confirm.understand.isVisible()
    assert dict(confirm.facts.values())["Board software"] == "Stays the same (2.7.8)"
    assert h.controller.session.config.zkt.comm_key == 12345
    h.click_primary(pages.CONFIRM)
    h.wait_page(pages.INSTALLING)
    assert h.page(pages.INSTALLING).steps.keys() == [
        StepId.PREPARE_FILES.value,
        StepId.WRITE_SETTINGS.value,
    ]
    # New settings must survive a restart too: Fix settings gets the restart check.
    h.first_start_done()
    assert h.page(pages.FIRST_START).result.advice.code == "START_CHECKS_OK"
    assert h.world.called("boot.wait_restart") == 1
    h.click_primary(pages.FIRST_START)
    h.wait_page(pages.DONE)
    assert h.page(pages.DONE).title_label.text() == "The new settings are on the board"
    assert h.page(pages.DONE).kept_settings
    assert h.page(pages.DONE).subtitle_label.text().startswith(texts.SETTINGS_KEPT)


def test_check_board_then_fix_settings_from_head_office_record(harness):
    h = harness("secured_board_fix_settings")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    h.wait(lambda: "HOTSPOT_OPEN" in board.shown_codes(), timeout=5000)
    assert "DEVICE_OFFLINE" in board.shown_codes()
    assert any("own Wi-Fi" in line for line in board.console.shown_lines)
    assert board.primary_button.isEnabled()
    board.secondary_button.click()  # What is the light doing?
    h.wait_page(pages.LIGHT_GUIDE)
    h.page(pages.LIGHT_GUIDE).back_button.click()
    h.wait_page(pages.CHECK_BOARD)
    h.idle("check")
    h.click_primary(pages.CHECK_BOARD)
    h.wait_page(pages.FIX_SETTINGS)
    fix = h.page(pages.FIX_SETTINGS)
    assert fix.form.zone_id.text() == "LF-ZONE-BLD9-02"  # from head office
    assert fix.form.device_id.text() == "1"
    assert not fix.family_field.isVisible()  # head office knows: ZKTeco


def test_resume_after_key(harness):
    h = harness("resume_after_key")
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "RESUME"
    assert checking.primary_button.text() == "Finish the setup"
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.CONFIRM)
    assert h.page(pages.CONFIRM).mode is InstallMode.RESUME
    h.click_primary(pages.CONFIRM)
    h.wait_page(pages.INSTALLING)
    assert StepId.LOCK_KEY.value not in h.page(pages.INSTALLING).steps.keys()
    h.first_start_done()
    assert h.page(pages.FIRST_START).result.advice.code == "START_CHECKS_OK"
    assert h.world.called("boot.wait_restart") == 1


def test_install_error_before_key_try_again(harness):
    h = harness("install_error_before_key")
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "COPY_FAILED"
    assert installing.steps.state_of(StepId.COPY_FIRMWARE.value) == "failed"
    assert installing.primary_button.text() == "Try again"
    assert installing.secondary_button.text() == "Go back to the start"
    assert h.controller.close_blocker() is None
    h.click_primary(pages.INSTALLING)
    h.wait_page(pages.FIRST_START, timeout=8000)
    assert h.world.called("installer.run") == 2


def test_install_error_after_key_finishes_the_setup(harness):
    h = harness("install_error_after_key")
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "STARTUP_COPY_FAILED"
    assert installing.primary_button.text() == "Finish the setup"
    assert not installing.secondary_button.isVisible()
    h.click_primary(pages.INSTALLING)
    h.wait_page(pages.CHECKING)
    checking = h.page(pages.CHECKING)
    h.wait(lambda: checking.result.isVisible())
    assert checking.result.advice.code == "RESUME"
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.CONFIRM)
    h.click_primary(pages.CONFIRM)
    h.wait_page(pages.FIRST_START, timeout=8000)
    assert h.controller.session.mode is InstallMode.RESUME


def test_first_boot_wrong_zone(harness):
    h = harness("first_boot_wrong_zone")
    h.start_install()
    first_start = h.page(pages.FIRST_START)
    h.wait(lambda: first_start.result.isVisible(), timeout=8000)
    assert first_start.result.advice.code == "BOOT_WRONG_ZONE"
    assert first_start.primary_button.text() == "Fix settings"  # the advice names this button
    assert first_start.secondary_button.text() == "Check the board"
    h.wait(lambda: [report[2] for report in h.world.reports] == ["BOOT_FAILED"])
    assert h.world.reports[0][3]["outcome"] == "wrong_settings"
    h.click_primary(pages.FIRST_START)
    h.wait_page(pages.CHECKING)


def test_manual_setup_mode_then_try_again(harness):
    h = harness("manual_boot_mode")
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "SETUP_MODE_FAILED"
    assert checking.result.advice.illustration == "boot_button"
    assert checking.primary_button.text() == "Try again"
    h.click_primary(pages.CHECKING)
    h.wait(lambda: checking.result.isVisible() and checking.result.advice.code == "NEW_BOARD")


def test_board_locked_by_someone_else_stops(harness):
    h = harness("secured_other")
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "BOARD_SECURED_OTHER"
    assert not checking.primary_button.isVisible()
    assert checking.secondary_button.text() == "Use another board"


def test_family_rule(harness):
    h = harness("family_mismatch")
    h.checked(Family.ZKT)
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "FAMILY_MISMATCH"
    assert "Hikvision" in checking.result.advice.title
    # The way on is head office's machine type, never a setup as the other one.
    assert checking.primary_button.text() == "Choose Hikvision"
    assert any("Click Choose Hikvision" in step for step in checking.result.advice.what_to_do)
    assert checking.secondary_button.text() == "Use another board"
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.MACHINE)


def test_stop_before_the_key(harness):
    h = harness("happy_zkt")
    h.hold("step_copy_firmware")
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.current is StepId.COPY_FIRMWARE)
    assert installing.secondary_button.isVisible() and installing.secondary_button.text() == "Stop"
    assert h.controller.close_blocker() is None
    installing.secondary_button.click()
    h.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "SETUP_STOPPED"
    assert installing.primary_button.text() == "Start again"
    assert h.world.called("installer.run") == 1


def test_no_stop_and_no_closing_after_the_key(harness):
    h = harness("happy_zkt")
    h.hold("step_copy_startup")
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.current is StepId.COPY_STARTUP)
    assert not installing.secondary_button.isVisible()
    assert not installing.back_button.isVisible()
    assert h.controller.close_blocker()
    h.controller._installing_cancel()  # even if asked, nothing is cancelled
    assert not h.controller.session.user_cancelled
    h.release("step_copy_startup")
    h.wait_page(pages.FIRST_START, timeout=8000)
    assert h.controller.close_blocker() is None


def test_steps_for_new_board_include_the_key():
    assert StepId.LOCK_KEY in steps_for(InstallMode.NEW_BOARD)


# ------------------------------------------- the engine's own advice on these screens


class FailingInstaller:
    """The real engine's failure, as Installer.run raises it."""

    def __init__(self, error: InstallerError) -> None:
        self.error = error

    def run(self, mode, board, config, on_step, cancel):
        on_step(StepProgress(self.error.step, 0.5))
        raise self.error


class FinishedBoot:
    """Watched starts that end at once: the first start gives ``report``, and each restart
    check the next of ``restarts``, as the real engine's BootWatcher returns them."""

    def __init__(self, report: BootReport, *restarts: BootReport) -> None:
        self.report = report
        self.restarts = list(restarts)
        self.calls = []
        self.firsts = []

    def wait_first_boot(
        self, mac, expect, on_phase, on_event, cancel, timeout_s=120.0, transcript=None
    ):
        self.calls.append("first start")
        return self.report

    def wait_restart(
        self,
        mac,
        expect,
        on_phase,
        on_event,
        cancel,
        timeout_s=120.0,
        transcript=None,
        first=None,
    ):
        self.calls.append("restart check")
        self.firsts.append(first)
        return self.restarts.pop(0)

    def watch_console(self, candidate, on_event, cancel):
        cancel.wait(60)


FIRST_OK = BootReport(
    BootOutcome.OK,
    "LF-ZONE-BLD9-04",
    "1",
    "2.7.8",
    advice=texts.boot_ok("LF-ZONE-BLD9-04", "1", "2.7.8"),
)
RESTART_OK = BootReport(
    BootOutcome.OK,
    "LF-ZONE-BLD9-04",
    "1",
    "2.7.8",
    advice=texts.settings_kept("LF-ZONE-BLD9-04", "1", "2.7.8"),
)
# The buttons a step could name; Help is on every screen and holds "Save a help file".
BUTTONS = ("Try again", "Fix settings", "Next", "Check the board", "Finish the setup")


@pytest.mark.parametrize(
    "error, button",
    [
        (
            InstallerError(
                texts.resume_needed(texts.CONNECTION_LOST),
                step=StepId.COPY_STARTUP,
                irreversible_done=True,
            ),
            "Finish the setup",
        ),
        (
            InstallerError(texts.invalid_field("zone_id"), step=StepId.PREPARE_FILES),
            "Change the details",
        ),
        (
            InstallerError(
                texts.device_id_in_use("1", "LF-ZONE-BLD9-04", "2"), step=StepId.PREPARE_FILES
            ),
            "Change the details",
        ),
    ],
    ids=["resume", "invalid-field", "device-id-taken"],
)
def test_engine_install_advice_names_the_button_on_screen(harness, error, button):
    h = harness("happy_zkt")
    h.services.installer = FailingInstaller(error)
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    assert installing.primary_button.text() == button
    assert any(f"Click {button}" in step for step in error.advice.what_to_do)


def test_locked_key_that_failed_its_check_is_not_offered_to_finish(harness):
    h = harness("happy_zkt")
    h.services.installer = FailingInstaller(
        InstallerError(texts.KEY_CHECK_FAILED, step=StepId.LOCK_KEY, irreversible_done=True)
    )
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "KEY_CHECK_FAILED"  # "Do not use this board."
    assert not installing.primary_button.isVisible()
    assert installing.secondary_button.text() == "Go back to the start"


@pytest.mark.parametrize(
    "report, button",
    [
        (
            BootReport(
                BootOutcome.WRONG_SETTINGS,
                advice=texts.wrong_settings("zone A, device 1", "zone B, device 1"),
            ),
            "Fix settings",
        ),
        (BootReport(BootOutcome.STORAGE_PROBLEM, advice=texts.STORAGE_PROBLEM), "Fix settings"),
        (
            BootReport(BootOutcome.SECURITY_KEY_PROBLEM, advice=texts.SETTINGS_UNREADABLE),
            "Fix settings",
        ),
        (
            BootReport(BootOutcome.FAMILY_MISMATCH, advice=texts.FAMILY_MISMATCH_LOOP),
            "Fix settings",
        ),
        (BootReport(BootOutcome.NO_OUTPUT, advice=texts.NO_OUTPUT), "Try again"),
        (BootReport(BootOutcome.NOT_REPLUGGED, advice=texts.NOT_REPLUGGED), "Try again"),
    ],
    ids=lambda item: item.outcome.value if isinstance(item, BootReport) else item,
)
def test_engine_first_start_advice_names_the_button_on_screen(harness, report, button):
    h = harness("happy_zkt")
    h.services.boot = FinishedBoot(report)
    h.start_install()
    first_start = h.page(pages.FIRST_START)
    h.wait(lambda: first_start.result.isVisible(), timeout=8000)
    assert first_start.primary_button.text() == button
    assert any(f"Click {button}" in step for step in report.advice.what_to_do)


def test_board_that_made_its_own_key_is_not_offered_a_fix(harness):
    h = harness("happy_zkt")
    h.services.boot = FinishedBoot(
        BootReport(BootOutcome.SECURITY_KEY_PROBLEM, advice=texts.SECURITY_KEY_LOST)
    )
    h.start_install()
    first_start = h.page(pages.FIRST_START)
    h.wait(lambda: first_start.result.isVisible(), timeout=8000)
    assert not first_start.primary_button.isVisible()
    assert first_start.secondary_button.text() == "Check the board"


# ----------------------------------------------------- the restart check (second start)


@pytest.mark.parametrize(
    "restart, primary, secondary, kept",
    [
        (
            BootReport(BootOutcome.SETTINGS_LOST, advice=texts.SETTINGS_LOST_ON_RESTART),
            None,
            "Go back to the start",
            False,
        ),
        (
            BootReport(BootOutcome.SETTINGS_NOT_SEEN, advice=texts.SETTINGS_NOT_SEEN),
            "Try again",
            "Check the board",
            None,
        ),
        (
            BootReport(BootOutcome.NO_OUTPUT, advice=texts.NO_OUTPUT),
            "Try again",
            "Check the board",
            None,
        ),
        (
            BootReport(BootOutcome.NOT_REPLUGGED, advice=texts.NOT_REPLUGGED),
            "Try again",
            "Check the board",
            None,
        ),
        (
            BootReport(BootOutcome.FAULTY_RESTART_LOOP, advice=texts.CRASH_LOOP),
            "Try again",
            "Check the board",
            None,
        ),
        (
            BootReport(
                BootOutcome.WRONG_SETTINGS,
                advice=texts.wrong_settings("zone A, device 1", "zone B, device 1"),
            ),
            "Fix settings",
            "Check the board",
            None,
        ),
    ],
    ids=lambda item: item.outcome.value if isinstance(item, BootReport) else None,
)
def test_engine_restart_advice_names_the_button_on_screen(
    harness, restart, primary, secondary, kept
):
    # A second start that does not pass gets the first start's ways on, except a board that
    # lost its settings: it must not go to a site, so it gets no button but Help.
    h = harness("happy_zkt")
    h.services.boot = FinishedBoot(FIRST_OK, restart)
    h.start_install()
    h.first_start_done()
    first_start = h.page(pages.FIRST_START)
    assert first_start.result.advice is restart.advice
    if primary is None:
        assert not first_start.primary_button.isVisible()
    else:
        assert first_start.primary_button.text() == primary
    assert first_start.secondary_button.text() == secondary
    on_screen = {primary, secondary}
    for step in restart.advice.what_to_do:
        for words in BUTTONS:
            if words not in on_screen:
                assert f"click {words}".lower() not in step.lower(), (words, step)
    h.controller._first_start_next()  # never Done after a failed restart check
    assert h.controller.current == pages.FIRST_START
    # Two reports from two workers: their order is not what this checks.
    h.wait(lambda: sorted(report[2] for report in h.world.reports) == ["BOOT_OK", "RESTART_FAILED"])
    (detail,) = [report[3] for report in h.world.reports if report[2] == "RESTART_FAILED"]
    assert detail["advice"] == restart.advice.code and detail["settings_kept"] is kept


def test_try_again_after_the_restart_check_watches_only_the_restart_again(harness):
    h = harness("happy_zkt")
    boot = FinishedBoot(
        FIRST_OK, BootReport(BootOutcome.NOT_REPLUGGED, advice=texts.NOT_REPLUGGED), RESTART_OK
    )
    h.services.boot = boot
    h.start_install()
    h.first_start_done()
    first_start = h.page(pages.FIRST_START)
    assert first_start.result.advice.code == "NOT_REPLUGGED"
    h.click_primary(pages.FIRST_START)  # Try again
    h.wait(
        lambda: first_start.result.advice.code == "START_CHECKS_OK"
        and not h.controller.running("boot")
    )
    assert boot.calls == ["first start", "restart check", "restart check"]
    h.wait(
        lambda: sorted(report[2] for report in h.world.reports)
        == ["BOOT_OK", "RESTART_FAILED", "RESTART_OK"]
    )
    h.click_primary(pages.FIRST_START)  # Next
    h.wait_page(pages.DONE)
    assert h.page(pages.DONE).kept_settings
    # Each restart check was given the first start's report.
    assert boot.firsts == [FIRST_OK, FIRST_OK]


def test_try_again_after_a_failed_first_start_watches_the_first_start_again(harness):
    h = harness("happy_zkt")
    boot = FinishedBoot(BootReport(BootOutcome.NO_OUTPUT, advice=texts.NO_OUTPUT))
    h.services.boot = boot
    h.start_install()
    h.first_start_done()
    h.click_primary(pages.FIRST_START)  # Try again
    h.wait(lambda: boot.calls == ["first start", "first start"])
    h.first_start_done()
    assert not h.page(pages.FIRST_START).again  # never the restart check after a failure


def test_closing_during_the_restart_check_asks_first(harness, monkeypatch):
    # Once the board has started, nothing else stops a person in a hurry from taking it
    # away: closing the app then would skip the check that it keeps its settings.
    from PySide6.QtGui import QCloseEvent

    h = harness("happy_zkt")
    h.hold("unplug")
    h.start_install()
    first_start = h.page(pages.FIRST_START)
    h.wait(lambda: first_start.phase is ReplugPhase.WAITING_UNPLUG, timeout=8000)
    assert h.controller.close_question() is None  # the first start: as before
    h.hold("restart_unplug")
    h.release("unplug")
    h.wait(
        lambda: first_start.again and first_start.phase is ReplugPhase.WAITING_UNPLUG,
        timeout=8000,
    )
    assert h.controller.close_blocker() is None  # it asks only: the check can take minutes
    question = h.controller.close_question()
    assert question == (
        "The app has not finished checking this board. If you close the app now, do not "
        "install the board at a site."
    )
    answers = [False, True]
    asked = []
    monkeypatch.setattr(
        h.window, "confirm_close", lambda words: asked.append(words) or answers.pop(0)
    )
    kept_open = QCloseEvent()
    h.window.closeEvent(kept_open)  # Keep the app open
    assert not kept_open.isAccepted() and asked == [question]
    assert h.controller.running("boot")  # the restart check goes on
    closed = QCloseEvent()
    h.window.closeEvent(closed)  # Close the app
    assert closed.isAccepted() and not h.controller.running("boot")


def test_nothing_to_ask_once_the_restart_check_has_finished(harness):
    h = harness("happy_zkt")
    h.start_install()
    h.first_start_done()
    assert h.page(pages.FIRST_START).result.advice.code == "START_CHECKS_OK"
    assert h.controller.close_question() is None


@pytest.mark.parametrize("words, closes", [("Keep the app open", False), ("Close the app", True)])
def test_close_question_buttons(harness, words, closes):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    h = harness("happy_zkt")
    shown = []
    tries = [0]

    def answer() -> None:
        boxes = [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        if not boxes:  # not open yet
            tries[0] += 1
            if tries[0] < 250:
                QTimer.singleShot(20, answer)
            return
        box = boxes[0]
        shown.append((box.windowTitle(), box.text(), [b.text() for b in box.buttons()]))
        for button in box.buttons():
            if button.text() == words:
                button.click()
                return
        box.reject()  # never left open: the asserts below say what was wrong

    QTimer.singleShot(0, answer)
    assert h.window.confirm_close("The app has not finished checking this board.") is closes
    (title, text, buttons) = shown[0]
    if sys.platform != "darwin":  # Qt drops a message box's title on macOS
        assert title == "The check is not finished"
    assert text == "The app has not finished checking this board."
    assert sorted(buttons) == ["Close the app", "Keep the app open"]
