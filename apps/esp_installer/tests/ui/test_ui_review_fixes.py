"""The screens' side of the review findings (E1-E16, F1-F3, B1), one story per finding.

Each test fails on the code before the fix: the situation the reviewer found is
set up through the real controller and the fake services, and the screen is
checked for what the person now sees and can press.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from sl_installer.core import api, fakes, texts
from sl_installer.core.api import (
    Advice,
    Capabilities,
    ConnectivityStatus,
    ConsoleEvent,
    ConsoleKind,
    DeviceRecord,
    Family,
    InstallerError,
    PresenceReport,
    PresenceState,
    SecurityState,
    StepId,
    StepProgress,
)
from sl_installer.ui import pages, wording
from sl_installer.ui.controller import CAPABILITIES_REFRESH_S
from sl_installer.ui.pages.fix_settings import data_loss_warning
from sl_installer.ui.widgets.topbar import USER_MAX_WIDTH


class FailingInstaller:
    """The engine's failure, as Installer.run raises it."""

    def __init__(self, error: InstallerError) -> None:
        self.error = error

    def run(self, mode, board, config, on_step, cancel):
        on_step(StepProgress(self.error.step or StepId.PREPARE_FILES, 0.5))
        raise self.error


def to_fix_settings(h) -> None:
    """Check a board, then Fix settings, with head office's record loaded."""
    h.check_board()
    h.click_primary(pages.CHECK_BOARD)
    h.wait_page(pages.FIX_SETTINGS)
    h.idle("bundles")


def install_failure(h, error: InstallerError):
    h.services.installer = FailingInstaller(error)
    h.start_install()
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.problem.isVisible())
    return installing


def footer(page):
    """(main button words or None, second button words or None) as the person sees them."""
    primary = page.primary_button.text() if page.primary_button.isVisible() else None
    secondary = page.secondary_button.text() if page.secondary_button.isVisible() else None
    return primary, secondary


# ------------------------------------------------- E1: the board's own Wi-Fi


def test_check_board_shows_the_boards_own_wifi(harness):
    h = harness("secured_board_fix_settings")
    h.hold("console_line")  # the board has not said anything yet
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    assert board.hotspot_card.isVisible()  # a set-up board always has its own Wi-Fi
    assert board.hotspot.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"
    assert board.hotspot.values["password"].text() == "test-hotspot-pass"
    assert board.hotspot.values["address"].text() == "http://192.168.254.1"
    assert board.hotspot.labels["password"].text() == "Board's Wi-Fi password"
    assert not board.hotspot_steps.expanded
    h.release("console_line")
    # The advice says to use the password "shown on this screen": the steps open with it.
    h.wait(lambda: "HOTSPOT_OPEN" in board.shown_codes())
    assert board.hotspot_steps.expanded


def test_check_board_has_no_wifi_card_for_a_new_board(harness):
    h = harness("happy_zkt")
    h.check_board()
    assert not h.page(pages.CHECK_BOARD).hotspot_card.isVisible()


def test_light_guide_blue_blink_gives_the_boards_wifi_name_and_password(harness):
    h = harness("secured_board_fix_settings")
    h.check_board()
    h.page(pages.CHECK_BOARD).secondary_button.click()  # What is the light doing?
    h.wait_page(pages.LIGHT_GUIDE)
    guide = h.page(pages.LIGHT_GUIDE)
    guide.rhythm_cards["slow"].click()
    guide.colour_cards["WIFI_CONNECTING"].click()
    details = guide.answer.hotspot
    assert details.isVisible()
    assert details.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"  # this board's own
    assert details.values["password"].text() == "test-hotspot-pass"
    assert not any("last screen" in step for step in guide.chosen.what_to_do)
    guide.colour_cards["ZKT_FAILURE"].click()
    assert not details.isVisible()


def test_light_guide_from_home_shows_the_wifi_name_pattern(harness):
    h = harness("happy_zkt")
    h.signed_in()
    h.act(pages.HOME, "lights")
    h.wait_page(pages.LIGHT_GUIDE)
    guide = h.page(pages.LIGHT_GUIDE)
    guide.choose_rhythm("slow")
    guide.choose_state("WIFI_CONNECTING")
    details = guide.answer.hotspot
    assert details.values["hotspot"].text() == "SLIC ATTENDANCE-XXXX"
    assert not details.copies["hotspot"].isVisible()  # a pattern, nothing to copy
    assert details.values["password"].text() == "test-hotspot-pass"


def test_done_calls_it_the_boards_wifi_password(harness):
    h = harness("happy_zkt")
    h.to_done()
    done = h.page(pages.DONE).credentials
    assert done.labels["password"].text() == "Board's Wi-Fi password"
    assert done.labels["address"].text() == "Wi-Fi setup page"


# ------------------------------------- E2, B1: Fix settings needs head office


def test_check_board_offers_no_fix_until_head_office_answers(harness):
    h = harness("lookup_fails")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    assert not board.primary_button.isVisible()  # no Fix settings button at all
    assert board.fix_blocked.isVisible()
    assert board.fix_blocked.advice.what_happened == (
        "Head office could not be reached, so settings cannot be changed now."
    )
    h.controller._check_board_fix()  # even if asked, nothing opens
    assert h.controller.current == pages.CHECK_BOARD
    inspections = h.world.inspections
    board.fix_blocked.button_for("Try again").click()
    h.wait(lambda: board.primary_button.isVisible() and board.primary_button.isEnabled())
    assert h.world.inspections == inspections  # only head office was asked again
    assert not board.fix_blocked.isVisible()
    assert dict(board.record_facts.values())["Zone"] == "LF-ZONE-BLD9-02, device number 1"
    h.click_primary(pages.CHECK_BOARD)
    h.wait_page(pages.FIX_SETTINGS)
    fix = h.page(pages.FIX_SETTINGS)
    # Used at a site: the warning and the typed ZONE ID are back, as head office says.
    assert fix.warning_card.isVisible() and fix.needs_typed_confirmation


def test_check_board_says_why_head_office_does_not_allow_new_settings(harness):
    h = harness("settings_change_not_allowed")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    assert not board.primary_button.isVisible()
    assert board.fix_blocked.advice.code == "SETTINGS_CHANGE_NOT_ALLOWED"
    assert board.fix_blocked.advice.what_happened == (
        "This board was set up before this app. Only head office can change its settings."
    )
    assert board.fix_blocked.button_for("Try again") is None
    h.controller._check_board_fix()
    assert h.controller.current == pages.CHECK_BOARD


def test_setup_flow_does_not_offer_to_change_a_board_head_office_refuses(harness):
    h = harness("settings_change_not_allowed")
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "SETTINGS_CHANGE_NOT_ALLOWED"
    assert footer(checking) == (None, "Use another board")
    h.controller._checking_fix()
    assert h.controller.current == pages.CHECKING


def test_setup_flow_does_not_finish_a_locked_board_head_office_has_seen_online(harness):
    # Key locked, never started, yet head office has seen it at a site: ADD refuses to
    # finish it, so "Finish the setup" is not offered (review F1).
    h = harness("settings_change_not_allowed")
    h.world.board_state = SecurityState.KEY_BURNED_NOT_STARTED
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "SETTINGS_CHANGE_NOT_ALLOWED"
    assert not checking.result.advice.can_retry
    assert footer(checking) == (None, "Use another board")
    assert h.controller.session.mode is None


def test_fix_settings_needs_no_board_software(harness, monkeypatch):
    # Head office has published nothing: a settings change writes no board software (F3).
    monkeypatch.setattr(fakes, "BUNDLES", ())
    h = harness("secured_board_fix_settings")
    to_fix_settings(h)
    fix = h.page(pages.FIX_SETTINGS)
    fix.confirm_edit.setText("LF-ZONE-BLD9-02")
    fix.form.zkt_comm_key.setText("12345")
    h.click_primary(pages.FIX_SETTINGS)
    h.wait_page(pages.CONFIRM)
    assert not fix.problem.isVisible()
    assert h.controller.session.config.bundle_id == ""
    assert dict(h.page(pages.CONFIRM).facts.values())["Board software"] == (
        "Stays the same (2.7.8)"
    )
    assert h.world.called("server.bundles") == 0


def test_fix_settings_never_guesses_an_unknown_board(harness):
    h = harness("lookup_fails")
    h.check_board()
    h.hold("lookup")
    h.controller.start_fixing()  # reached some other way than the (hidden) button
    h.wait_page(pages.FIX_SETTINGS)
    fix = h.page(pages.FIX_SETTINGS)
    assert fix.blocked and fix.blocker.isVisible()
    assert not fix.form.isVisible() and not fix.warning_card.isVisible()
    assert not fix.primary_button.isEnabled()
    h.release("lookup")
    h.wait(lambda: h.controller.session.fix_loaded)
    assert not fix.blocked and fix.form.isVisible()
    assert fix.subtitle_label.text().startswith("Head office knows only the zone")
    assert fix.needs_typed_confirmation  # head office says it was used at a site
    assert fix.form.zone_id.text() == "LF-ZONE-BLD9-02"


def test_data_loss_warning_says_what_head_office_knows():
    record = DeviceRecord(
        known=True,
        mac=fakes.BOARD_MAC,
        zone_id="LF-ZONE-BLD9-02",
        online=False,
        ever_online=True,
        status_text="Offline since 10:42 am",
    )
    advice = data_loss_warning("LF-ZONE-BLD9-02", record)
    assert "Offline since 10:42 am" in advice.what_happened
    assert not any("last hour" in step for step in advice.what_to_do)
    assert any("call head office" in step for step in advice.what_to_do)
    assert advice.what_to_do[-1] == "To go on anyway, type LF-ZONE-BLD9-02 in the box below."
    online = data_loss_warning("LF-ZONE-BLD9-02", DeviceRecord(True, fakes.BOARD_MAC, online=True))
    assert online.what_to_do[-1] == "To go on, type LF-ZONE-BLD9-02 in the box below."


# ------------------------------------ E3, F2: what the settings page writes


def test_fix_settings_asks_for_the_comm_key_instead_of_the_default(harness):
    h = harness("secured_board_fix_settings")
    to_fix_settings(h)
    fix = h.page(pages.FIX_SETTINGS)
    form = fix.form
    assert fix.subtitle_label.text().startswith("Head office knows only the zone")
    words = fix.what_changes.body.text()
    assert "From head office: the ZONE ID, the ZONE NAME and the device number." in words
    assert "comm key" in words and "forgets the site's Wi-Fi" in words
    # In view, not under Advanced, and empty: the usual key would lock a board out of a
    # machine with another one (for example a key head office changed).
    assert form.zkt_comm_key_field.isVisible()
    assert form.machine_card.isAncestorOf(form.zkt_comm_key_field)
    assert not form.advanced.isAncestorOf(form.zkt_comm_key_field)
    assert form.zkt_comm_key.text() == ""
    assert "under Comm" in form.zkt_comm_key_field.hint.text()
    assert form.advanced.expanded  # everything else that is written can be seen
    assert form.zkt_port.text() == "4370" and form.zkt_preferred_ip.text() == "0.0.0.0"
    assert not form.hik_host_field.isVisible() and not form.hik_model_field.isVisible()
    fix.confirm_edit.setText("LF-ZONE-BLD9-02")
    h.click_primary(pages.FIX_SETTINGS)
    assert h.controller.current == pages.FIX_SETTINGS
    assert form.zkt_comm_key_field.error.text() == "Type the comm key from the attendance machine."
    form.zkt_comm_key.setText("4321")
    h.click_primary(pages.FIX_SETTINGS)
    h.wait_page(pages.CONFIRM)
    confirm = h.page(pages.CONFIRM)
    assert "forgets the site's Wi-Fi" in confirm.notice.body.text()
    assert "punches" in confirm.notice.body.text()
    assert h.controller.session.config.zkt.comm_key == 4321


def test_details_keep_the_usual_comm_key_under_advanced(harness):
    h = harness("happy_zkt")
    h.to_details()
    form = h.page(pages.DETAILS).form
    assert not form.fix_mode and not form.machine_card.isVisible()
    assert form.advanced.isAncestorOf(form.zkt_comm_key_field)
    assert form.zkt_comm_key.text() == str(fakes.TEST_DEFAULTS.zkt_comm_key)
    assert form.hik_model.findText("Choose the model") == -1


def test_fix_settings_for_hikvision_asks_for_the_machine(harness):
    h = harness("secured_hik_fix_settings")
    to_fix_settings(h)
    fix = h.page(pages.FIX_SETTINGS)
    form = fix.form
    assert form.family is Family.HIKVISION and form.machine_card.isVisible()
    # No model is guessed: the wrong one would give the machine the wrong profile.
    assert form.hik_model.currentText() == "Choose the model"
    assert form.hik_model.currentData() is None and not form.model_notice.isVisible()
    assert (
        form.hik_password.text() == "" and "cannot be read" in form.hik_password_field.hint.text()
    )
    assert "address, password, model and serial number" in fix.what_changes.body.text()
    fix.confirm_edit.setText("LF-ZONE-JHELUM-01")
    form.hik_host.setText("192.168.1.20")
    form.hik_password.setText("test-machine-pass")
    form.hik_serial.setText("TEST-SERIAL-0001")
    h.click_primary(pages.FIX_SETTINGS)
    assert h.controller.current == pages.FIX_SETTINGS
    assert "Choose the machine model" in form.hik_model_field.error.text()
    form.hik_model.setCurrentIndex(form.hik_model.findData("ds-k1t342efwx"))
    h.click_primary(pages.FIX_SETTINGS)
    h.wait_page(pages.CONFIRM)
    config = h.controller.session.config
    assert config.hik.profile == api.HIK_MODELS[0].profile
    assert config.hik.source_epoch == "lf-zone-jhelum-01-20261030"  # head office's own


# ------------------------------------------- E4: the other machine type


def test_set_up_board_known_as_the_other_type_offers_that_type(harness):
    h = harness("family_mismatch")
    h.world.board_state = SecurityState.SECURED_STATE_LIFE  # locked, and head office: Hikvision
    h.checked(Family.ZKT)
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "FAMILY_MISMATCH"  # not "Change its settings"
    assert footer(checking) == ("Choose Hikvision", "Use another board")
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.MACHINE)
    assert not h.controller.session.fixing


def test_fix_settings_does_not_fill_details_typed_for_the_other_type(harness):
    h = harness("family_mismatch")
    h.world.board_state = SecurityState.SECURED_STATE_LIFE
    h.checked(Family.ZKT)  # ZKTeco details typed for LF-ZONE-BLD9-04
    h.controller.start_fixing()  # even if the settings page were opened anyway
    h.wait_page(pages.FIX_SETTINGS)
    h.idle("bundles")
    form = h.page(pages.FIX_SETTINGS).form
    assert form.family is Family.HIKVISION and form.machine_card.isVisible()
    assert form.zone_id.text() == "LF-ZONE-BLD9-03"  # head office's, not the ZKTeco details


def test_family_mismatch_from_head_office_leads_to_the_machine_choice(harness):
    h = harness("happy_zkt")
    error = InstallerError(texts.family_mismatch(Family.HIKVISION), step=StepId.PREPARE_FILES)
    installing = install_failure(h, error)
    assert footer(installing) == ("Choose the machine type", "Go back to the start")
    # The engine's advice names that button (E11).
    assert installing.problem.advice.what_to_do[0] == (
        "Click Choose the machine type, then choose Hikvision."
    )
    h.click_primary(pages.INSTALLING)
    h.wait_page(pages.MACHINE)


# ------------------------------------------------ E5: fast restart loops


def test_fast_restart_loop_is_a_cable_problem_with_a_way_on(harness):
    h = harness("fast_restart_loop")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "RESTART_LOOP_FAST"
    assert connect.problem.advice.illustration == "cable"
    assert not connect.primary_button.isEnabled()  # Next stays grey
    assert connect.waiting.isVisible()  # a board that stops looping is found by itself
    again = connect.problem.button_for("Check again")
    assert again is not None
    h.qtbot.wait(100)  # the watcher keeps reporting the loop
    assert h.world.reports == []  # not reported as a faulty board
    again.click()
    h.wait_page(pages.CHECKING)
    h.wait(lambda: h.page(pages.CHECKING).result.isVisible())
    assert h.page(pages.CHECKING).result.advice.code == "NEW_BOARD"


def test_check_again_from_check_or_fix_does_not_keep_the_old_loop_advice(harness):
    h = harness("fast_restart_loop")
    h.signed_in()
    h.act(pages.HOME, "check")
    h.wait_page(pages.CONNECT)
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.button_for("Check again") is not None)
    connect.problem.button_for("Check again").click()
    h.wait_page(pages.CHECK_BOARD)
    h.idle("check")
    board = h.page(pages.CHECK_BOARD)
    assert "RESTART_LOOP_FAST" not in board.shown_codes()  # reading it ended the loop
    assert h.world.reports == []


def test_fast_restart_loop_with_the_engines_own_advice(harness):
    from sl_installer.core.diagnosis import Diagnoser

    h = harness("fast_restart_loop")
    h.services.diagnosis = Diagnoser(("RE7",))
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.advice.code == "RESTART_LOOP_FAST"
    assert connect.problem.button_for("Check again") is not None
    # The engine's words name that button and never ask for a FAULTY label.
    assert connect.problem.advice.what_to_do[-1] == "Then click Check again."
    assert "FAULTY" not in " ".join(connect.problem.advice.what_to_do)
    h.qtbot.wait(100)
    assert h.world.reports == []


def test_slow_silent_loop_is_still_the_faulty_board(harness):
    h = harness("faulty_restart_loop")
    h.to_connect()
    connect = h.page(pages.CONNECT)
    h.wait(lambda: connect.problem.isVisible())
    assert connect.problem.button_for("Check again") is None
    h.wait(lambda: [report[2] for report in h.world.reports] == ["BOARD_FAULTY"])


@pytest.mark.parametrize("period, code", [(3.4, "RESTART_LOOP_FAST"), (10.3, "BOARD_FAULTY")])
def test_loop_fallback_words_follow_the_period(period, code):
    report = PresenceReport(PresenceState.RESTART_LOOP, loop_period_s=period)
    advice = wording.presence_advice(report)
    assert advice.code == code
    assert wording.fast_restart_loop(report) is (code == "RESTART_LOOP_FAST")
    if code == "RESTART_LOOP_FAST":
        assert advice.what_to_do[-1] == "Then click Check again."
        assert "FAULTY" not in " ".join(advice.what_to_do)


# --------------------------------- E6: what failed while getting the files


@pytest.mark.parametrize(
    "advice, buttons",
    [
        (texts.NO_INTERNET, ("Try again", "Go back to the start")),
        (texts.PROVISIONER_UNAVAILABLE, ("Try again", "Go back to the start")),
        (texts.SERVER_UNREACHABLE, ("Try again", "Go back to the start")),
        (texts.SERVER_DISABLED, (None, "Go back to the start")),
        (texts.BUNDLE_NOT_TRUSTED, (None, "Go back to the start")),
    ],
    ids=lambda item: item.code if isinstance(item, Advice) else None,
)
def test_head_office_problems_are_not_sent_to_the_details(harness, advice, buttons):
    h = harness("happy_zkt")
    installing = install_failure(h, InstallerError(advice, step=StepId.PREPARE_FILES))
    assert footer(installing) == buttons


def test_details_problems_still_go_to_the_details(harness):
    h = harness("happy_zkt")
    error = InstallerError(texts.invalid_field("zone_id"), step=StepId.PREPARE_FILES)
    installing = install_failure(h, error)
    assert footer(installing) == ("Change the details", "Try again")


# ----------------------------------------------- E7: head office's switch


def test_home_check_again_after_head_office_switches_setup_on(harness):
    h = harness("installer_off")
    h.signed_in()
    h.idle("capabilities")
    home = h.page(pages.HOME)
    assert home.notice.isVisible() and home.notice.advice.code == "INSTALLER_OFF"
    assert not home.setup_card.isEnabled()
    again = home.notice.button_for("Check again")
    assert again is not None
    assert "Then click Check again." in home.notice.advice.what_to_do
    h.world.capabilities = Capabilities(True, True, "1.0.0")
    again.click()
    h.wait(lambda: not home.notice.isVisible())
    assert home.setup_card.isEnabled()


def test_home_looks_again_when_shown_again(harness):
    h = harness("installer_off")
    h.signed_in()
    h.idle("capabilities")
    h.world.capabilities = Capabilities(True, True, "1.0.0")
    h.act(pages.HOME, "lights")
    h.wait_page(pages.LIGHT_GUIDE)
    h.page(pages.LIGHT_GUIDE).back_button.click()
    h.wait_page(pages.HOME)
    h.wait(lambda: h.page(pages.HOME).setup_card.isEnabled())
    assert not h.page(pages.HOME).notice.isVisible()


def test_connectivity_check_looks_again_while_home_shows_the_notice(harness):
    h = harness("installer_off")
    h.signed_in()
    h.idle("capabilities")
    h.idle("connectivity")
    h.world.capabilities = Capabilities(True, True, "1.0.0")
    h.controller.check_connectivity()
    h.wait(lambda: h.page(pages.HOME).setup_card.isEnabled())


def test_capabilities_are_looked_at_again_every_few_minutes(harness):
    h = harness("happy_zkt")
    h.signed_in()
    h.idle("capabilities")
    h.act(pages.HOME, "lights")  # away from Home
    h.wait_page(pages.LIGHT_GUIDE)
    h.idle("connectivity")
    calls = h.world.called("server.capabilities")
    h.controller.check_connectivity()
    h.idle("connectivity")
    h.idle("capabilities")
    assert h.world.called("server.capabilities") == calls  # the last answer is fresh
    h.controller._capabilities_at -= CAPABILITIES_REFRESH_S + 1
    h.controller.check_connectivity()
    h.wait(lambda: h.world.called("server.capabilities") == calls + 1)


# ---------------------------------- E8: the light guide keeps the board going


def test_light_guide_from_check_board_keeps_the_board_talking(harness):
    h = harness("secured_board_fix_settings")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    h.wait(lambda: len(board.console.shown_lines) >= 3)
    seen = len(h.controller.session.events)
    assert h.world.inspections == 1
    board.secondary_button.click()  # What is the light doing?
    h.wait_page(pages.LIGHT_GUIDE)
    assert h.controller.running("console")  # still listening to the board
    h.page(pages.LIGHT_GUIDE).back_button.click()
    h.wait_page(pages.CHECK_BOARD)
    h.qtbot.wait(50)
    assert h.world.inspections == 1  # not read again: that would reset the board
    assert len(h.controller.session.events) >= seen and len(board.console.shown_lines) >= 3
    assert h.controller.running("console")
    assert board.primary_button.isVisible() and board.primary_button.isEnabled()
    board.again_link.click()  # Check again still reads the board afresh
    h.wait(lambda: h.world.inspections == 2)


def test_leaving_the_light_guide_elsewhere_frees_the_board(harness):
    h = harness("secured_board_fix_settings")
    h.check_board()
    h.page(pages.CHECK_BOARD).secondary_button.click()
    h.wait_page(pages.LIGHT_GUIDE)
    h.controller.go_home()
    h.wait_page(pages.HOME)
    assert not h.controller.running("console")


# ---------------------------------------------- E9: the Hikvision serial


def test_hikvision_summary_shows_the_machine_serial(harness):
    h = harness("happy_hik")
    h.to_confirm(Family.HIKVISION)
    rows = dict(h.page(pages.CONFIRM).facts.values())
    assert rows["Machine serial number"] == "TEST-SERIAL-0001"
    assert "letter by letter" in h.page(pages.DETAILS).form.hik_serial_field.hint.text()


# ------------------------------------------ E10: stopping a settings change


def test_stopping_a_settings_change_says_nothing_was_changed(harness):
    h = harness("secured_board_fix_settings")
    to_fix_settings(h)
    fix = h.page(pages.FIX_SETTINGS)
    fix.confirm_edit.setText("LF-ZONE-BLD9-02")
    fix.form.zkt_comm_key.setText("4321")
    h.click_primary(pages.FIX_SETTINGS)
    h.wait_page(pages.CONFIRM)
    h.hold("step_prepare_files")
    h.click_primary(pages.CONFIRM)
    h.wait_page(pages.INSTALLING)
    installing = h.page(pages.INSTALLING)
    h.wait(lambda: installing.secondary_button.isVisible())
    installing.secondary_button.click()  # Stop
    h.wait(lambda: installing.problem.isVisible())
    advice = installing.problem.advice
    assert advice.title == "Nothing was changed"
    assert "locked" not in advice.what_happened and "old settings" in advice.what_happened
    assert footer(installing) == ("Try again", "Go back to the start")


# ------------------------------------ E11: buttons the advice names are there


def test_resume_refused_details_offer_change_the_details(harness):
    h = harness("resume_after_key")
    cause = texts.device_id_in_use("1", "LF-ZONE-BLD9-04", "2")
    error = InstallerError(
        texts.resume_needed(cause), step=StepId.PREPARE_FILES, irreversible_done=True
    )
    installing = install_failure(h, error)
    assert footer(installing) == ("Finish the setup", "Change the details")
    installing.secondary_button.click()
    h.wait_page(pages.DETAILS)


def test_unclear_key_state_button_checks_the_board_again(harness):
    h = harness("happy_zkt")
    error = InstallerError(texts.KEY_STATE_UNCLEAR, step=StepId.LOCK_KEY, irreversible_done=True)
    installing = install_failure(h, error)
    assert footer(installing)[0] == "Check the board again"
    h.click_primary(pages.INSTALLING)
    h.wait_page(pages.CHECKING)


def test_sign_in_connection_problem_has_try_again(harness):
    h = harness("server_down")
    h.sign_in()
    page = h.page(pages.SIGN_IN)
    h.wait(lambda: page.advice.isVisible())
    again = page.advice.button_for("Try again")
    assert again is not None
    attempts = h.world.login_attempts
    again.click()
    h.wait(lambda: h.world.login_attempts == attempts + 1)


def test_wrong_password_has_no_try_again_button(harness):
    h = harness("login_wrong_password")
    h.sign_in("wrong-pass")
    page = h.page(pages.SIGN_IN)
    h.wait(lambda: page.advice.isVisible())
    assert page.advice.button_for("Try again") is None  # each try counts towards the lock


# --------------------------------------------- E12: Enter on "I understand"


def test_enter_on_i_understand_does_not_tick_it(harness):
    h = harness("happy_zkt")
    h.to_confirm()
    confirm = h.page(pages.CONFIRM)
    tick = confirm.understand
    tick.setFocus()
    QTest.keyClick(tick, Qt.Key.Key_Return)
    assert not tick.isChecked() and h.controller.current == pages.CONFIRM
    QTest.keyClick(tick, Qt.Key.Key_Space)  # Space ticks it
    assert tick.isChecked() and confirm.primary_button.isEnabled()
    QTest.keyClick(tick, Qt.Key.Key_Return)  # now Enter starts, it does not untick
    h.wait_page(pages.INSTALLING)


# ------------------------------------------------ E13: a narrow top bar


def test_top_bar_fits_a_narrow_window_with_a_long_name(harness):
    h = harness("server_down")
    h.window.resize(1024, 700)
    bar = h.window.top_bar
    bar.set_connectivity(
        ConnectivityStatus(True, False, "The internet works, but head office is not answering.")
    )
    bar.set_user("muhammad.usman.peshawar")
    # The chips say their state in full: the user name gives way, not "Internet OK".
    h.wait(
        lambda: (
            bar.internet_chip.words_fit
            and bar.server_chip.words_fit
            and bar.user_label.isVisible()
            and bar.user_label.width() <= USER_MAX_WIDTH
        )
    )
    # Fits even the smallest window, so nothing is clipped by the window edge.
    assert bar.minimumSizeHint().width() <= h.window.minimumWidth()
    assert bar.user_label.elided and bar.user_label.text().endswith("…")
    assert bar.user_label.text().startswith("Signed in as m")
    assert bar.user_label.full_text() == "Signed in as muhammad.usman.peshawar"
    assert bar.user_label.toolTip() == bar.user_label.full_text()
    assert bar.server_chip.text == "Head office not answering"


# ------------------------------------------- E14: what the last check said


def test_chips_and_sign_in_show_the_last_checks_words(harness):
    h = harness("server_down")
    h.controller.start()
    bar = h.window.top_bar
    h.wait(lambda: bar.server_chip.state == "bad")
    assert "head office is not answering" in bar.server_chip.toolTip()
    page = h.page(pages.SIGN_IN)
    assert page.network.isVisible()
    assert page.network.body.text() == "The internet works, but head office is not answering."


class _PortalConnectivity:
    def check(self) -> ConnectivityStatus:
        return ConnectivityStatus(
            False,
            False,
            "This network needs you to sign in to it first. Open a web browser and follow its "
            "sign-in page.",
        )


class _PortalServer(fakes.FakeServer):
    def login(self, username: str, password: str) -> str:
        raise InstallerError(texts.SECURE_CONNECTION_FAILED)


def test_network_sign_in_page_is_named_instead_of_the_clock(harness):
    h = harness("happy_zkt")
    h.services.connectivity = _PortalConnectivity()
    h.services.server = _PortalServer(h.world)
    h.controller.start()
    page = h.page(pages.SIGN_IN)
    h.wait(lambda: page.network.isVisible())
    assert "sign in to it first" in page.network.body.text()
    page.username.setText("admin")
    page.password.setText("test-admin-pass")
    h.click_primary(pages.SIGN_IN)
    h.wait(lambda: page.advice.isVisible())
    assert page.advice.advice.code == "NO_CONNECTION"
    assert "sign in to it first" in page.advice.advice.what_happened
    assert not page.network.isVisible()  # said once, on the card
    assert page.advice.button_for("Try again") is not None


def test_home_shows_why_head_office_is_not_answering(harness):
    h = harness("happy_zkt")
    h.signed_in()
    home = h.page(pages.HOME)
    h.controller._connectivity_checked(
        ConnectivityStatus(True, False, "The internet works, but head office is not answering.")
    )
    assert home.network.isVisible() and home.network.title.text() == "Head office is not answering"
    h.controller._connectivity_checked(ConnectivityStatus(True, True, "Connected to head office."))
    assert not home.network.isVisible()


# ------------------------------------------------ E15: one name per thing


def test_screens_say_device_number():
    event = ConsoleEvent(
        ConsoleKind.APP_STARTED, "raw", {"zone_id": "LF-ZONE-BLD9-02", "device_id": "1"}
    )
    assert (
        wording.console_words(event)[0] == "The board started for LF-ZONE-BLD9-02, device number 1."
    )


# ---------------------------------------- E16: a ZONE ID that cannot be written


def test_zone_id_board_settings_cannot_hold_blocks_the_change(harness):
    h = harness("zone_not_writable")
    to_fix_settings(h)
    fix = h.page(pages.FIX_SETTINGS)
    assert fix.blocked and fix.blocker.advice.code == "ZONE_ID_NOT_WRITABLE"
    assert fix.subtitle_label.text().startswith("New settings cannot be written")
    assert "LF-ZONE-G&PBLD8-01" in fix.blocker.advice.what_happened
    assert not fix.form.zone_id_field.error.text()  # head office's value is not called wrong
    assert not fix.warning_card.isVisible() and not fix.form.isVisible()
    assert fix.about.isVisible()  # which board this is still shows
    assert not fix.primary_button.isEnabled()
    fix.confirm_edit.setText("LF-ZONE-G&PBLD8-01")
    fix.form.zkt_comm_key.setText("4321")
    assert not fix.primary_button.isEnabled()
    h.controller._fix_settings_next()  # even if asked: nothing is written
    assert h.controller.current == pages.FIX_SETTINGS
