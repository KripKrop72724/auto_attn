"""The happy paths, end to end, through the real controller and the fake engine."""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QTimer

from sl_installer.core import fakes, texts
from sl_installer.core.api import Family, InstallMode, ReplugPhase, Severity, StepId, steps_for
from sl_installer.ui import pages


def assert_engine_never_ran_on_gui_thread(h):
    assert h.world.calls, "the engine was never called"
    assert h.world.calls_on_main_thread() == []


def test_happy_zkt_end_to_end(harness):
    h = harness("happy_zkt")
    h.hold("restart_unplug")  # to look at the screen between the two starts
    ticks = []
    clock = QTimer()
    clock.setInterval(10)
    clock.timeout.connect(lambda: ticks.append(1))
    clock.start()

    h.signed_in()
    assert h.window.top_bar.user_label.text() == "Signed in as admin"
    assert h.window.top_bar.internet_chip.text == "Internet OK"
    h.fill_details(Family.ZKT, "lf-zone-bld9-04")
    form = h.page(pages.DETAILS).form
    assert form.zone_id.text() == "LF-ZONE-BLD9-04"  # typed in small letters, shown in capitals
    assert form.zone_name.text() == "LF-ZONE-BLD9-04"  # filled in from the ZONE ID
    assert form.preview_name.text() == "Building 9 · Device 4"
    assert form.preview_group.text() == "Life group"
    assert form.device_id.text() == "1"
    assert form.bundle.currentText() == "ZKT 2.8.1 (recommended)"
    assert form.wifi_ssid.text() == "RE7" and form.wifi_password.text() == "test-wifi-pass"
    assert not form.advanced.expanded

    h.click_primary(pages.DETAILS)
    h.wait_page(pages.CONNECT)
    h.wait(lambda: h.page(pages.CONNECT).primary_button.isEnabled())
    h.click_primary(pages.CONNECT)
    h.wait_page(pages.CHECKING)
    h.wait(lambda: h.page(pages.CHECKING).result.isVisible())
    assert h.page(pages.CHECKING).result.advice.code == "NEW_BOARD"
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.CONFIRM)

    confirm = h.page(pages.CONFIRM)
    assert not confirm.primary_button.isEnabled()  # "I understand" first
    rows = dict(confirm.facts.values())
    assert rows["ZONE ID"] == "LF-ZONE-BLD9-04" and rows["Board ID"] == "AC:27:6E:A4:3D:30"
    confirm.understand.setChecked(True)
    h.click_primary(pages.CONFIRM)
    h.wait_page(pages.INSTALLING)
    assert h.page(pages.INSTALLING).steps.keys() == [
        step.value for step in steps_for(InstallMode.NEW_BOARD)
    ]
    assert not h.window.top_bar.sign_out_button.isEnabled()
    h.wait_page(pages.FIRST_START, timeout=8000)
    first_start = h.page(pages.FIRST_START)
    # The first start passed. One more, deliberate replug: nothing says "ready" yet.
    h.wait(
        lambda: first_start.again and first_start.phase is ReplugPhase.WAITING_UNPLUG,
        timeout=8000,
    )
    notice = first_start.result.advice
    assert notice.code == "RESTART_CHECK" and notice.severity is Severity.INFO
    assert notice.what_happened.startswith("The board started for LF-ZONE-BLD9-04, device")
    assert notice.what_to_do == (texts.RESTART_REPLUG, "Do not press any button.")
    assert "The setup is not finished." in notice.what_happened
    assert first_start.title_label.text() == "Start the board again"
    assert first_start.cards["unplug"].title.text() == "Unplug the cable again"
    assert not first_start.primary_button.isVisible()  # no Next before the second start
    assert not h.window.top_bar.sign_out_button.isEnabled()  # the board is being watched
    h.wait(lambda: [report[2] for report in h.world.reports] == ["BOOT_OK"])
    h.release("restart_unplug")
    h.first_start_done()
    assert first_start.result.advice.code == "START_CHECKS_OK"
    assert first_start.result.advice.what_happened.startswith(texts.SETTINGS_KEPT)
    assert "LF-ZONE-BLD9-04" in first_start.result.advice.what_happened
    assert any("started for LF-ZONE-BLD9-04" in line for line in first_start.console.shown_lines)
    # Head office hears how both starts went, under the install's request number, and
    # both starts' console lines go to this install's log.
    h.wait(lambda: [report[2] for report in h.world.reports] == ["BOOT_OK", "RESTART_OK"])
    request_id = h.controller.session.summary.request_id
    assert {report[0] for report in h.world.reports} == {request_id}
    assert h.world.reports[0][3]["zone_id_seen"] == "LF-ZONE-BLD9-04"
    assert h.world.reports[1][3]["settings_kept"] is True
    assert h.world.transcripts == [request_id, request_id]
    # The restart check knows what the first start showed.
    assert h.world.first_starts == [h.controller.session.boot]
    h.click_primary(pages.FIRST_START)
    h.wait_page(pages.DONE)

    done = h.page(pages.DONE)
    assert done.kept_settings
    assert done.subtitle_label.text().startswith(f"{texts.SETTINGS_KEPT} Take the board")
    assert done.credentials.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"
    assert done.credentials.values["password"].text() == "test-hotspot-pass"
    assert done.credentials.values["address"].text() == "http://192.168.254.1"
    assert not done.hik_notice.isVisible()
    assert h.window.top_bar.sign_out_button.isEnabled()
    done.secondary_button.click()  # Save a record
    h.wait(lambda: done.saved.isVisible())
    path = h.controller.session.record_path
    assert path and os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        record = handle.read()
    assert "LF-ZONE-BLD9-04" in record and "test-wifi-pass" not in record
    assert "First start: ok" in record and "After a restart: ok" in record

    h.click_primary(pages.DONE)  # Set up another board
    h.wait_page(pages.MACHINE)
    assert h.page(pages.MACHINE).zkt_card.isChecked()

    clock.stop()
    assert len(ticks) > 20, "the GUI clock stopped: something blocked the GUI thread"
    assert_engine_never_ran_on_gui_thread(h)
    assert h.world.called("installer.run") == 1


def test_happy_hik_end_to_end(harness):
    h = harness("happy_hik")
    h.to_details(Family.HIKVISION)
    form = h.page(pages.DETAILS).form
    assert form.machine_card.isVisible()
    assert form.bundle.currentText() == "Hikvision 3.1.4 (recommended)"
    form.zone_id.setText("LF-ZONE-JHELUM-01")
    h.click_primary(pages.DETAILS)  # machine address, password and serial are missing
    assert h.controller.current == pages.DETAILS
    assert form.hik_host_field.error.text() and form.hik_password_field.error.text()
    # The board and head office both need the serial, so it sits beside the model.
    assert form.hik_serial_field.error.text() and form.hik_serial_field.isVisible()
    assert not form.advanced.expanded
    form.hik_host.setText("192.168.1.20")
    form.hik_password.setText("test-machine-pass")
    form.hik_model.setCurrentIndex(form.hik_model.findData("ds-k1t321mfwx"))
    form.hik_serial.setText("TEST-SERIAL-0001")
    assert form.model_notice.isVisible()  # untested model: a gentle warning
    # No HTTPS: it needs the machine's own certificate, which the app cannot add.
    assert form.hik_transport.findData("https") == -1 and form.hik_port.text() == "80"
    h.click_primary(pages.DETAILS)
    h.wait_page(pages.CONNECT)
    h.wait(lambda: h.page(pages.CONNECT).primary_button.isEnabled())
    h.click_primary(pages.CONNECT)
    h.wait(lambda: h.page(pages.CHECKING).result.isVisible())
    h.click_primary(pages.CHECKING)
    h.wait_page(pages.CONFIRM)
    rows = dict(h.page(pages.CONFIRM).facts.values())
    assert rows["Machine type"] == "Hikvision" and rows["Machine address"] == "192.168.1.20"
    assert rows["Machine model"] == "DS-K1T321MFWX"
    config = h.controller.session.config
    assert config.hik is not None and config.zkt is None
    assert config.hik.profile == "ds-k1t321mfwx-v4.61.0-20260828-unverified-v1"
    assert config.hik.transport == "http_digest" and config.hik.port == 80
    assert config.hik.expected_serial == "TEST-SERIAL-0001"
    assert config.hik.source_epoch == ""  # the engine fills <zone>-<date>
    h.page(pages.CONFIRM).understand.setChecked(True)
    h.click_primary(pages.CONFIRM)
    h.first_start_done()
    # Hikvision boards get the restart check too.
    assert h.page(pages.FIRST_START).result.advice.code == "START_CHECKS_OK"
    assert h.world.called("boot.wait_restart") == 1
    h.click_primary(pages.FIRST_START)
    h.wait_page(pages.DONE)
    assert h.page(pages.DONE).hik_notice.isVisible()
    assert h.page(pages.DONE).kept_settings
    assert_engine_never_ran_on_gui_thread(h)


def test_board_that_loses_its_settings_on_restart_never_reaches_done(harness):
    # 2 Nov 2026: two new boards started well once, then had no settings at every later start.
    h = harness("settings_lost_on_restart")
    h.start_install()
    h.first_start_done()
    first_start = h.page(pages.FIRST_START)
    advice = first_start.result.advice
    # The engine's words, also without advice, with the board ID for its label.
    assert advice == texts.settings_lost_on_restart("ac:27:6e:a4:3d:30")
    assert any("AC:27:6E:A4:3D:30" in step for step in advice.what_to_do)
    assert any("no settings" in line for line in first_start.console.shown_lines)
    assert not first_start.primary_button.isVisible()  # no Next: never called ready
    assert first_start.secondary_button.text() == "Go back to the start"
    assert first_start.result.reference.text() == "Reference: SETTINGS_LOST_ON_RESTART"
    h.controller._first_start_next()  # even if asked, Done does not open
    assert h.controller.current == pages.FIRST_START

    # Head office hears it, and the setup record is kept for the help file without a click.
    h.wait(lambda: sorted(report[2] for report in h.world.reports) == ["BOOT_OK", "RESTART_FAILED"])
    (detail,) = [report[3] for report in h.world.reports if report[2] == "RESTART_FAILED"]
    assert detail["outcome"] == "settings_lost" and detail["settings_kept"] is False
    h.wait(lambda: h.controller.session.record_path is not None)
    with open(h.controller.session.record_path, encoding="utf-8") as handle:
        record = handle.read()
    assert "First start: ok" in record and "After a restart: settings_lost" in record
    assert h.world.called("records.save_install_record") == 1
    assert h.window.top_bar.sign_out_button.isEnabled()

    first_start.secondary_button.click()  # Go back to the start
    h.wait_page(pages.HOME)
    assert_engine_never_ran_on_gui_thread(h)


def test_back_keeps_what_was_typed_and_stops_watching_usb(harness):
    h = harness("happy_zkt")
    h.to_connect()
    assert h.controller.running("presence")
    h.page(pages.CONNECT).back_button.click()
    h.wait_page(pages.DETAILS)
    assert not h.controller.running("presence")
    assert h.page(pages.DETAILS).form.zone_id.text() == "LF-ZONE-BLD9-04"
    h.page(pages.DETAILS).back_button.click()
    h.wait_page(pages.MACHINE)
    h.page(pages.MACHINE).zkt_card.click()
    h.wait_page(pages.DETAILS)
    assert h.page(pages.DETAILS).form.zone_id.text() == "LF-ZONE-BLD9-04"


def test_zone_name_change_link(harness):
    h = harness("happy_zkt")
    h.to_details()
    form = h.page(pages.DETAILS).form
    form.zone_id.setText("ZONE-LAHORE-01")
    assert form.device_id.text() == "3"  # 1 and 2 are taken in this zone
    assert form.zone_name.isReadOnly()
    form.change_name.click()
    assert not form.zone_name.isReadOnly()
    form.zone_name.setText("ZONE-LAHORE-02")
    form.zone_name.textEdited.emit("ZONE-LAHORE-02")
    form.zone_id.setText("ZONE-MULTAN-01")
    assert form.zone_name.text() == "ZONE-LAHORE-02"  # changed by hand: kept
    form.change_name.click()  # "Use the ZONE ID"
    assert form.zone_name.text() == "ZONE-MULTAN-01" and form.zone_name.isReadOnly()


def test_mistakes_are_shown_next_to_the_fields(harness):
    h = harness("happy_zkt")
    h.to_details()
    form = h.page(pages.DETAILS).form
    form.zone_id.setText("LF ZONE 9")
    assert "spaces" in form.zone_id_field.error.text()  # checked while typing
    form.zone_id.setText("LF-ZONE-BLD9-04")
    form.wifi_password.setText("short")
    h.click_primary(pages.DETAILS)
    assert h.controller.current == pages.DETAILS
    assert form.advanced.expanded  # the problem is under Advanced
    assert form.wifi_password_field.error.text()
    assert form.wifi_password.line_edit.hasFocus() or not h.window.isActiveWindow()


def test_light_guide_from_home_and_back(harness):
    h = harness("happy_zkt")
    h.signed_in()
    h.act(pages.HOME, "lights")
    h.wait_page(pages.LIGHT_GUIDE)
    guide = h.page(pages.LIGHT_GUIDE)
    guide.rhythm_cards["slow"].click()
    assert guide.colour_card.isVisible()
    assert guide.colour_names() == ["Blue", "Purple", "Yellow", "Amber", "Orange", "Red-orange"]
    guide.colour_cards["ZKT_FAILURE"].click()
    assert guide.answer.isVisible() and "comm key" in guide.answer.meaning.text()
    assert guide.answer.look_alike.isVisible()
    guide.rhythm_cards["off"].click()  # one colour only: straight to the answer
    assert not guide.colour_card.isVisible() and guide.chosen.key == "OFF"
    guide.secondary_button.click()  # Start again
    assert guide.chosen is None and not guide.answer.isVisible()
    guide.back_button.click()
    h.wait_page(pages.HOME)


def test_help_opens_for_the_screen_and_closes(harness):
    h = harness("happy_zkt")
    h.to_connect()
    h.page(pages.CONNECT).help_button.click()
    assert h.window.help_open and h.window.help_panel.topic == "connect"
    h.window.help_panel.save_button.click()
    h.wait(lambda: h.window.help_panel.saved.isVisible())
    assert h.world.called("records.save_help_file") == 1
    h.window.scrim.clicked.emit()
    assert not h.window.help_open


def test_sign_out_returns_to_sign_in(harness):
    h = harness("happy_zkt")
    h.signed_in()
    h.window.top_bar.sign_out_button.click()
    h.wait_page(pages.SIGN_IN)
    assert not h.window.top_bar.user_label.isVisible()
    assert h.page(pages.SIGN_IN).password.text() == ""
    h.wait(lambda: h.world.called("server.logout") == 1)


@pytest.mark.parametrize("scenario", fakes.SCENARIOS)
def test_every_fake_scenario_builds(scenario):
    services = fakes.make_fake_services(scenario)
    assert services.defaults.is_test_build
    assert services.usb.snapshot() is not None


def test_unknown_scenario_is_refused():
    with pytest.raises(ValueError):
        fakes.make_fake_services("no-such-story")


def test_change_settings_steps():
    assert steps_for(InstallMode.CHANGE_SETTINGS) == (StepId.PREPARE_FILES, StepId.WRITE_SETTINGS)


def test_home_explains_when_head_office_says_no(harness):
    from sl_installer.core.api import Capabilities

    h = harness("happy_zkt")
    h.signed_in()
    h.idle("capabilities")
    assert h.controller.home_notice() is None
    for caps, code, blocked in (
        (Capabilities(False, True, "1.0.0"), "INSTALLER_OFF", True),
        (Capabilities(True, False, "1.0.0"), "SETTINGS_SERVICE_DOWN", False),
        (Capabilities(True, True, "9.0.0"), "APP_TOO_OLD", True),
    ):
        h.controller.capabilities = caps
        h.controller.show(pages.HOME)
        home = h.page(pages.HOME)
        assert home.notice.isVisible() and home.notice.advice.code == code
        assert home.setup_card.isEnabled() is not blocked


def test_chosen_software_version_survives_going_back(harness):
    h = harness("happy_zkt")
    h.fill_details()
    form = h.page(pages.DETAILS).form
    form.bundle.setCurrentIndex(form.bundle.findData("zkt-2.7.7-4c1e9a20"))
    h.click_primary(pages.DETAILS)
    h.wait_page(pages.CONNECT)
    h.page(pages.CONNECT).back_button.click()
    h.wait_page(pages.DETAILS)
    h.idle("bundles")
    assert form.selected_bundle_id() == "zkt-2.7.7-4c1e9a20"


def test_run_gui_starts_on_sign_in_and_returns(qapp):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from sl_installer.ui.app import run_gui
    from sl_installer.ui.window import MainWindow

    services = fakes.make_fake_services("happy_zkt")
    seen = []

    def look_and_leave():
        windows = [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.services is services
        ]
        seen.extend(window.controller.current for window in windows)
        for window in windows:
            window.hide()  # closing the last window would also post a Quit event
        QApplication.exit(0)

    QTimer.singleShot(300, look_and_leave)
    try:
        assert run_gui(services) == 0
    finally:
        # A Quit event left in the queue would end every later event loop at once.
        QApplication.removePostedEvents(qapp, QEvent.Type.Quit)
    assert seen == [pages.SIGN_IN]
    assert fakes.fake_world(services).calls_on_main_thread() == []
