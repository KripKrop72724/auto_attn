"""Each problem story shows the right advice and the right way forward."""

from __future__ import annotations

import pytest

from sl_installer.core import texts
from sl_installer.core.api import (
    BootOutcome,
    BootReport,
    Family,
    InstallerError,
    InstallMode,
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
    h.wait(lambda: h.page(pages.FIRST_START).result.isVisible(), timeout=8000)
    h.click_primary(pages.FIRST_START)
    h.wait_page(pages.DONE)
    assert h.page(pages.DONE).title_label.text() == "The new settings are on the board"


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
    h.wait(lambda: h.page(pages.FIRST_START).result.isVisible(), timeout=8000)


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
    def __init__(self, report: BootReport) -> None:
        self.report = report

    def wait_first_boot(self, mac, expect, on_phase, on_event, cancel, timeout_s=120.0):
        return self.report

    def watch_console(self, candidate, on_event, cancel):
        cancel.wait(60)


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
