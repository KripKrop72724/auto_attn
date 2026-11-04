"""1.1.0: boards the software cannot start are refused, and the board's own Wi-Fi shows
whenever the board opens it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sl_installer.core import fakes
from sl_installer.core.api import ConsoleEvent, ConsoleKind, InstallerError
from sl_installer.ui import pages
from sl_installer.ui.widgets.credentials import NO_TEST_PASSWORD


def footer(page):
    primary = page.primary_button.text() if page.primary_button.isVisible() else None
    secondary = page.secondary_button.text() if page.secondary_button.isVisible() else None
    return primary, secondary


def refused_reports(h):
    return [
        (mac, detail) for _request, mac, kind, detail in h.world.reports if kind == "BOARD_REFUSED"
    ]


# ---------------------------------------------------------------- the board check


def test_new_board_with_an_xmc_memory_chip_is_refused(harness):
    h = harness("flash_chip_unsupported")
    h.checked()
    checking = h.page(pages.CHECKING)
    advice = checking.result.advice
    assert advice.code == "FLASH_CHIP_UNSUPPORTED"
    assert advice.title == "This board's memory chip cannot run our software"
    assert "20 70 18" in advice.what_happened
    assert advice.what_to_do[0] == "Put this board aside. It is not broken."
    # No way on with this board: no Next, no Try again.
    assert footer(checking) == (None, "Use another board")
    assert ("Memory chip ID", "20 70 18") in checking.facts.values()
    h.wait(lambda: bool(refused_reports(h)))
    assert refused_reports(h) == [
        (
            fakes.BOARD_MAC,
            {
                "reason": "FLASH_CHIP_UNSUPPORTED",
                "flash_id": "20 70 18",
                "chip": fakes.CHIP_LINE,
                "features": None,
            },
        )
    ]
    checking.secondary_button.click()
    h.wait_page(pages.CONNECT)
    assert h.world.called("installer.run") == 0


def test_locked_xmc_board_is_never_finished(harness):
    # The two boards of 3 Nov 2026: key burned, looping in the start-up program.
    h = harness("flash_chip_key_burned")
    h.checked()
    checking = h.page(pages.CHECKING)
    advice = checking.result.advice
    assert advice.code == "FLASH_CHIP_UNSUPPORTED"
    assert "earlier setup locked" in advice.what_happened
    assert "Do not try to set it up again." in advice.what_to_do
    assert footer(checking) == (None, "Use another board")  # never "Finish the setup"
    assert h.controller.session.mode is None


def test_board_type_not_supported_yet(harness):
    h = harness("board_type_unsupported")
    h.checked()
    checking = h.page(pages.CHECKING)
    advice = checking.result.advice
    assert advice.code == "BOARD_TYPE_UNSUPPORTED"
    assert advice.title == "This board type is not supported yet"
    assert "The app found: extra memory 8 MB, type AP_1v8." in advice.what_happened
    assert ("Found", "extra memory 8 MB, type AP_1v8") in checking.facts.values()
    assert footer(checking) == (None, "Use another board")
    h.wait(lambda: bool(refused_reports(h)))
    assert refused_reports(h)[0][1]["reason"] == "BOARD_TYPE_UNSUPPORTED"


def test_the_refusal_comes_before_a_head_office_problem(harness):
    # 3 Nov 2026: head office answered 502 while the XMC board was checked.
    h = harness("flash_chip_unsupported")
    server = h.services.server

    def down(mac):
        raise h.world.server_error()

    server.device = down
    h.checked()
    checking = h.page(pages.CHECKING)
    assert checking.result.advice.code == "FLASH_CHIP_UNSUPPORTED"
    assert footer(checking) == (None, "Use another board")


def test_check_a_board_says_what_is_wrong_and_reports_it_once(harness):
    h = harness("flash_chip_key_burned")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    h.wait(lambda: bool(board.shown_codes()))
    assert board.shown_codes()[0] == "FLASH_CHIP_UNSUPPORTED"
    assert ("Memory chip ID", "20 70 18") in board.board_facts.values()
    assert not board.primary_button.isEnabled()  # no Fix settings
    h.wait(lambda: bool(refused_reports(h)))
    board.again_link.click()  # Check again: the same board, reported once
    h.idle("check")
    h.qtbot.wait(50)
    assert len(refused_reports(h)) == 1


# --------------------------------------------------------------- the first start


def test_start_up_program_that_refuses_is_said_with_its_own_words(harness):
    h = harness("startup_refused")
    h.start_install()
    h.first_start_done()
    first_start = h.page(pages.FIRST_START)
    advice = first_start.result.advice
    assert advice.code == "STARTUP_REFUSED" and "Image hash failed" in advice.what_happened
    assert any("Save a help file" in step for step in advice.what_to_do)
    assert footer(first_start) == (None, "Check the board")
    assert h.controller.session.restart is None  # no restart check after a failed start
    assert any(
        event.kind is ConsoleKind.BOOTLOADER_FAILED for event in h.controller.session.boot.events
    )
    first_start.console.raw.set_expanded(True)
    assert (
        "E (48) esp_image: Image hash failed - image is corrupt"
        in first_start.console.raw_label.text()
    )
    h.wait(lambda: any(kind == "BOOT_FAILED" for _r, _m, kind, _d in h.world.reports))
    detail = next(d for _r, _m, kind, d in h.world.reports if kind == "BOOT_FAILED")
    assert detail["board_said"] == "E (48) esp_image: Image hash failed - image is corrupt"


@pytest.mark.parametrize(
    "password, shown",
    [
        ("test-hotspot-pass", "test-hotspot-pass"),
        ("", NO_TEST_PASSWORD),  # a test build has no hotspot password
    ],
)
def test_first_start_shows_the_boards_own_wifi_when_it_opens(harness, password, shown):
    h = harness("hotspot_at_start")
    h.services.defaults = replace(h.services.defaults, setup_ap_password=password)
    h.start_install()
    h.first_start_done()
    first_start = h.page(pages.FIRST_START)
    assert first_start.result.advice.code == "START_CHECKS_OK"
    assert first_start.hotspot_card.isVisible()
    assert first_start.hotspot.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"
    assert first_start.hotspot.values["password"].text() == shown
    assert first_start.hotspot.copies["password"].isVisibleTo(first_start) is bool(password)


def test_first_start_has_no_wifi_card_until_the_board_opens_it(harness):
    h = harness("happy_zkt")
    h.start_install()
    h.first_start_done()
    assert h.page(pages.FIRST_START).hotspot_card.isHidden()


def test_restart_check_hides_the_wifi_card_of_the_first_start(harness):
    h = harness("happy_zkt")
    h.hold("restart_unplug")
    h.start_install()
    h.wait_page(pages.FIRST_START, 8000)
    page = h.page(pages.FIRST_START)
    h.wait(lambda: page.again)
    page.show_hotspot("SLIC ATTENDANCE-3D31", "test-hotspot-pass")
    assert not page.hotspot_card.isHidden()
    page.start_again(page.result.advice)  # the replug closes the board's Wi-Fi
    assert page.hotspot_card.isHidden()


# ----------------------------------------------------------------- check a board


def test_check_a_board_shows_the_wifi_as_soon_as_the_board_opens_it(harness):
    # A new board: no Wi-Fi card, until the board says its own Wi-Fi is open.
    h = harness("happy_zkt")
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    assert board.hotspot_card.isHidden()
    opened = ConsoleEvent(
        ConsoleKind.SETUP_AP_OPEN,
        "W (121400) wifi_setup: Wi-Fi setup access point enabled (recovery mode)",
        {"mode": "recovery", "ssid": "SLIC ATTENDANCE-3D31"},
        121.4,
    )
    h.controller._on_check_event(opened)
    assert board.hotspot_card.isVisible() and board.hotspot_steps.expanded
    assert board.hotspot.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"
    assert board.hotspot.values["password"].text() == "test-hotspot-pass"
    h.controller._show_check_advice(())  # the advice is worked out again: the card stays
    assert board.hotspot_card.isVisible()


def test_a_lookup_failure_never_hides_a_refusal_on_check_a_board(harness):
    h = harness("flash_chip_unsupported")

    def down(mac):
        raise InstallerError(h.world.server_error().advice)

    h.services.server.device = down
    h.check_board()
    board = h.page(pages.CHECK_BOARD)
    h.wait(lambda: bool(board.shown_codes()))
    assert board.shown_codes()[0] == "FLASH_CHIP_UNSUPPORTED"
