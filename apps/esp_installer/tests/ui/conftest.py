"""Fixtures for the screen tests: a real window driven by fake services.

Run in esp-installer-dev:local (QT_QPA_PLATFORM=offscreen), from apps/esp_installer:
    python -m pytest tests/ui -q
"""

from __future__ import annotations

import os
import sys
from typing import Callable, List

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32" and os.environ["QT_QPA_PLATFORM"] == "offscreen":
    # On Windows the offscreen platform reads fonts only from QT_QPA_FONTDIR, by
    # default a "fonts" folder PySide6 does not ship. Without it every letter is
    # an empty box and text is far wider than in the real app, which uses the
    # Windows fonts directly.
    os.environ.setdefault(
        "QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    )

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sl_installer.core import api, fakes  # noqa: E402
from sl_installer.ui import pages  # noqa: E402
from sl_installer.ui.app import ensure_app  # noqa: E402
from sl_installer.ui.window import MainWindow  # noqa: E402

PACE = 0.002


@pytest.fixture(scope="session", autouse=True)
def themed_app(qapp):
    """The QApplication with the installer's theme, like the real app."""
    return ensure_app()


class Harness:
    """One main window on one fake story, with helpers that click like a person."""

    def __init__(self, qtbot, scenario: str) -> None:
        self.qtbot = qtbot
        self.services = fakes.make_fake_services(scenario, pace=PACE)
        self.world = fakes.fake_world(self.services)
        self.window = MainWindow(self.services)
        self.window.resize(1280, 800)
        self.window.show()
        self.controller = self.window.controller
        self._held: List[str] = []

    # ------------------------------------------------------------ basics

    def page(self, page_id: str):
        return self.window.pages[page_id]

    def act(self, page_id: str, name: str) -> None:
        assert self.controller.current == page_id, f"on {self.controller.current}, not {page_id}"
        self.page(page_id).action.emit(name)

    def click_primary(self, page_id: str) -> None:
        button = self.page(page_id).primary_button
        assert button.isVisible() and button.isEnabled(), f"{page_id}: main button not available"
        button.click()

    def wait(self, condition: Callable[[], bool], timeout: int = 5000) -> None:
        self.qtbot.waitUntil(condition, timeout=timeout)

    def wait_page(self, page_id: str, timeout: int = 5000) -> None:
        self.wait(lambda: self.controller.current == page_id, timeout)

    def idle(self, kind: str, timeout: int = 5000) -> None:
        self.wait(lambda: not self.controller.running(kind), timeout)

    def hold(self, moment: str) -> None:
        self.world.hold(moment)
        self._held.append(moment)

    def release(self, moment: str) -> None:
        self.world.release(moment)

    def close(self) -> None:
        for moment in self._held:
            self.world.release(moment)
        self.controller.session.installing = False
        self.controller.shutdown()
        self.window.hide()
        assert self.controller.runner.wait(10000), "background work did not stop"
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.world.cleanup()

    # ------------------------------------------------------------- steps

    def sign_in(self, password: str = "test-admin-pass") -> None:
        self.controller.start()
        page = self.page(pages.SIGN_IN)
        page.username.setText("admin")
        page.password.setText(password)
        self.click_primary(pages.SIGN_IN)

    def signed_in(self) -> None:
        self.sign_in()
        self.wait_page(pages.HOME)

    def to_details(self, family: api.Family = api.Family.ZKT) -> None:
        self.signed_in()
        self.act(pages.HOME, "setup")
        self.wait_page(pages.MACHINE)
        card = (
            self.page(pages.MACHINE).zkt_card
            if family is api.Family.ZKT
            else self.page(pages.MACHINE).hik_card
        )
        card.click()
        self.wait_page(pages.DETAILS)
        self.idle("bundles")
        self.idle("zones")

    def fill_details(
        self, family: api.Family = api.Family.ZKT, zone: str = "LF-ZONE-BLD9-04"
    ) -> None:
        self.to_details(family)
        form = self.page(pages.DETAILS).form
        form.zone_id.setText(zone)
        if family is api.Family.HIKVISION:
            form.hik_host.setText("192.168.1.20")
            form.hik_password.setText("test-machine-pass")
            form.hik_serial.setText("TEST-SERIAL-0001")

    def to_connect(self, family: api.Family = api.Family.ZKT) -> None:
        self.fill_details(family)
        self.click_primary(pages.DETAILS)
        self.wait_page(pages.CONNECT)

    def board_found(self, family: api.Family = api.Family.ZKT) -> None:
        self.to_connect(family)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled())

    def checked(self, family: api.Family = api.Family.ZKT) -> None:
        self.board_found(family)
        self.click_primary(pages.CONNECT)
        self.wait_page(pages.CHECKING)
        self.wait(lambda: self.page(pages.CHECKING).result.isVisible())

    def to_confirm(self, family: api.Family = api.Family.ZKT) -> None:
        self.checked(family)
        self.click_primary(pages.CHECKING)
        self.wait_page(pages.CONFIRM)

    def start_install(self, family: api.Family = api.Family.ZKT) -> None:
        self.to_confirm(family)
        page = self.page(pages.CONFIRM)
        if page.understand.isVisible():
            page.understand.setChecked(True)
        self.click_primary(pages.CONFIRM)
        self.wait_page(pages.INSTALLING)

    def first_start_done(self, timeout: int = 8000) -> None:
        """Wait for the first-start screen's last card: both starts passed, or one failed.

        After a good first start the screen shows a card too, over the restart check's
        steps, while the second start is still being watched.
        """
        self.wait_page(pages.FIRST_START, timeout)
        page = self.page(pages.FIRST_START)
        self.wait(lambda: page.result.isVisible() and not self.controller.running("boot"), timeout)

    def to_done(self, family: api.Family = api.Family.ZKT) -> None:
        self.start_install(family)
        self.wait_page(pages.FIRST_START, timeout=8000)
        self.wait(lambda: self.page(pages.FIRST_START).primary_button.isVisible(), timeout=8000)
        self.click_primary(pages.FIRST_START)
        self.wait_page(pages.DONE)

    def check_board(self) -> None:
        self.signed_in()
        self.act(pages.HOME, "check")
        self.wait_page(pages.CONNECT)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled())
        self.click_primary(pages.CONNECT)
        self.wait_page(pages.CHECK_BOARD)
        self.idle("check")


@pytest.fixture
def harness(qtbot):
    made: List[Harness] = []

    def make(scenario: str) -> Harness:
        item = Harness(qtbot, scenario)
        made.append(item)
        return item

    yield make
    for item in made:
        item.close()
