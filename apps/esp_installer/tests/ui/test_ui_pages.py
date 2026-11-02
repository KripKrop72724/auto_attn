"""Every screen and widget draws, at both supported window sizes."""

from __future__ import annotations

import dataclasses
import threading

import pytest
from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QApplication

from sl_installer.core import api
from sl_installer.core.api import Advice, Severity
from sl_installer.ui import leds, pages, pictures
from sl_installer.ui.runner import Operation, Runner
from sl_installer.ui.widgets import AdviceCard, LedSwatch, PasswordInput, StepIndicator


@pytest.mark.parametrize("size", [QSize(1280, 800), QSize(1024, 700)], ids=["1280x800", "1024x700"])
def test_every_page_renders(harness, size):
    h = harness("happy_zkt")
    h.window.resize(size)
    for page_id in pages.ALL:
        h.window.show_page(page_id)
        QApplication.processEvents()
        image = h.window.grab()
        assert not image.isNull() and image.width() == size.width(), page_id
        page = h.page(page_id)
        assert page.title_label.text(), page_id
        # Nothing may be wider than the window: no sideways scrolling.
        assert page.scroll.horizontalScrollBar().maximum() == 0, page_id


def test_board_found_reads_well_without_a_usb_board_id(harness):
    connect = harness("happy_zkt").page(pages.CONNECT)
    board = api.UsbCandidate("COM7", api.UsbKind.NATIVE, 0x303A, 0x1001, None)
    connect.show_found(board)
    assert connect.found.body.text() == ("This computer does not show the board ID. "
                                         "Click Next to read it from the board.")
    connect.show_found(dataclasses.replace(board, mac="ac:27:6e:a4:3d:30"))
    assert connect.found.body.text() == "Board ID AC:27:6E:A4:3D:30. Click Next."


def test_window_title_and_test_ribbon(harness):
    h = harness("happy_zkt")
    assert h.window.windowTitle() == "State Life Device Installer"
    assert h.window.ribbon is not None and "TEST BUILD" in h.window.ribbon.label.text()


def test_no_ribbon_on_a_release_build(qtbot):
    from sl_installer.core import fakes
    from sl_installer.ui.window import MainWindow

    services = fakes.make_fake_services("happy_zkt")
    services.defaults = api.FieldDefaults(
        wifi_password="test-wifi-pass", setup_ap_password="test-hotspot-pass", is_test_build=False
    )
    window = MainWindow(services)
    qtbot.addWidget(window)
    assert window.ribbon is None


@pytest.mark.parametrize("key", sorted(pictures.ILLUSTRATIONS))
def test_advice_card_shows_every_picture(qtbot, key):
    card = AdviceCard(
        Advice(
            "TEST", "A title", "What happened.", ("Do this.", "Then that."), Severity.WARNING, key
        )
    )
    qtbot.addWidget(card)
    card.resize(900, 300)
    card.show()
    assert card._picture.has_picture and card._picture.isVisible()
    assert not card.grab().isNull()


def test_advice_card_without_picture_shows_an_icon(qtbot):
    card = AdviceCard(
        Advice("TEST", "A title", "What happened.", (), Severity.ERROR, "no-such-picture")
    )
    qtbot.addWidget(card)
    card.show()
    assert not card._picture.isVisible() and card._icon.isVisible()
    assert card.reference.text() == "Reference: TEST"


def test_success_advice_has_no_reference_code(qtbot):
    card = AdviceCard(Advice("OK", "Done", "All good.", (), Severity.SUCCESS))
    qtbot.addWidget(card)
    card.show()
    assert not card.reference.isVisible()


def test_password_eye_shows_and_hides(qtbot):
    field = PasswordInput()
    qtbot.addWidget(field)
    field.setText("test-wifi-pass")
    assert not field.revealed
    field.eye.click()
    assert field.revealed and field.eye.accessibleName() == "Hide the password"
    field.eye.click()
    assert not field.revealed


def test_step_indicator_names_the_current_step(qtbot):
    steps = StepIndicator()
    qtbot.addWidget(steps)
    steps.set_steps(["Machine", "Details", "Connect"], 1)
    assert steps.accessibleName() == "Step 2 of 3: Details"


def test_led_swatch_animates_only_while_shown(qtbot):
    swatch = LedSwatch(leds.BY_KEY["WIFI_CONNECTING"])
    qtbot.addWidget(swatch)
    assert not swatch._timer.isActive()
    swatch.show()
    assert swatch._timer.isActive()
    swatch.set_state(leds.BY_KEY["HEALTHY"])  # steady: nothing to animate
    assert not swatch._timer.isActive()
    swatch.hide()
    assert not swatch._timer.isActive()


def test_runner_brings_results_and_callbacks_to_the_gui_thread(qtbot):
    runner = Runner()
    gui = threading.current_thread()
    seen = []
    operation = Operation("test")

    def work():
        relayed = runner.relay(
            lambda value: seen.append(("callback", value, threading.current_thread() is gui)),
            operation,
        )
        relayed(1)
        return threading.current_thread() is not gui

    runner.start(
        work,
        lambda result: seen.append(("done", result, threading.current_thread() is gui)),
        operation=operation,
    )
    qtbot.waitUntil(lambda: len(seen) == 2, timeout=3000)
    assert seen == [("callback", 1, True), ("done", True, True)]


def test_runner_drops_results_of_stopped_work(qtbot):
    runner = Runner()
    seen = []
    operation = Operation("test")
    started = threading.Event()

    def work():
        started.set()
        operation.token.wait(2)
        return "late"

    runner.start(work, seen.append, seen.append, operation)
    assert started.wait(2)
    operation.stop()
    qtbot.waitUntil(lambda: not runner.busy, timeout=3000)
    QTimer.singleShot(0, lambda: None)
    QApplication.processEvents()
    assert seen == []


def test_keyboard_goes_through_the_form_before_the_footer(harness):
    h = harness("happy_zkt")
    h.controller.start()
    page = h.page(pages.SIGN_IN)
    page.focus_first()
    order = []
    widget = page.username
    for _ in range(40):
        order.append(widget)
        widget = widget.nextInFocusChain()
    assert order.index(page.password.line_edit) < order.index(page.password.eye)
    assert (
        order.index(page.password.eye)
        < order.index(page.help_button)
        < order.index(page.primary_button)
    )


def test_fields_have_accessible_names(harness):
    h = harness("happy_hik")
    form = h.page(pages.DETAILS).form
    for field in form._all_fields():
        assert field.focus_target.accessibleName(), field.label.text()
    assert form.wifi_password.eye.accessibleName() in ("Show the password", "Hide the password")


@pytest.mark.parametrize("page_id", [pages.DETAILS, pages.FIX_SETTINGS])
def test_connection_offers_only_normal_http(harness, page_id):
    # HTTPS needs the machine's own certificate, which the app cannot add (review CR2).
    h = harness("happy_hik")
    form = h.page(page_id).form
    transport = form.hik_transport
    assert [transport.itemData(index) for index in range(transport.count())] == ["http_digest"]
    assert transport.currentText() == "Normal (HTTP)"
    # A default or a saved answer of "https" never comes out of the form.
    form.reset(api.Family.HIKVISION, api.FieldDefaults(hik_transport="https"))
    assert form.values().hik_transport == "http_digest"
    form.fill(dataclasses.replace(form.values(), hik_transport="https"))
    assert transport.currentData() == "http_digest"
    assert form.values().hik_transport == "http_digest" and form.values().hik_port == 80


def test_first_start_page_says_when_it_is_the_restart_check(harness):
    from sl_installer.core.api import BootOutcome, BootReport, ReplugPhase
    from sl_installer.ui import wording

    page = harness("happy_zkt").page(pages.FIRST_START)
    page.start_again(wording.restart_check(BootReport(BootOutcome.OK, "LF-ZONE-BLD9-04", "1")))
    assert page.again and page.title_label.text() == "Start the board again"
    assert page.subtitle_label.text() == "The board must keep its settings when it starts again."
    assert [card.title.text() for card in page.cards.values()] == [
        "Unplug the cable again",
        "Wait 3 seconds",
        "Plug it back in",
    ]
    assert page.result.isVisibleTo(page) and page.result.advice.code == "RESTART_CHECK"
    assert not page.primary_button.isVisibleTo(page)
    page.set_phase(ReplugPhase.WATCHING)
    assert page.watch_words.text() == (
        "Watching the board start again. This can take up to 2 minutes."
    )
    page.start()  # the first start's own words again
    assert not page.again and page.title_label.text() == "Start the board"
    assert page.cards["unplug"].title.text() == "Unplug the cable"
    assert not page.result.isVisibleTo(page)


def test_restart_check_keeps_the_replug_words_in_view_on_a_small_screen(harness):
    # The card over the restart check pushes the steps down; their live words must stay
    # in view, also at the smallest supported size.
    from PySide6.QtCore import QPoint

    from sl_installer.core.api import ReplugPhase

    h = harness("happy_zkt")
    h.window.resize(1024, 700)
    h.hold("restart_unplug")
    h.hold("restart_replug")
    h.start_install()
    page = h.page(pages.FIRST_START)
    h.wait(lambda: page.again and page.phase is ReplugPhase.WAITING_UNPLUG, 8000)

    def in_view(widget) -> bool:
        viewport = page.scroll.viewport()
        top = widget.mapTo(viewport, QPoint(0, 0)).y()
        return widget.isVisible() and top >= 0 and top + widget.height() <= viewport.height()

    unplug = page.cards["unplug"].note
    h.wait(lambda: in_view(unplug))
    assert unplug.text() == "Pull the cable out now."
    h.release("restart_unplug")
    h.wait(lambda: page.phase is ReplugPhase.WAITING_REPLUG)
    page.start_countdown(0)  # the 3 seconds are over
    replug = page.cards["replug"].note
    h.wait(lambda: in_view(replug))
    assert replug.text() == "Plug it back in now. Do not press any button."
