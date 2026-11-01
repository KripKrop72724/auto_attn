"""The main window: top bar, step indicator, the screens, and the help overlay."""

from __future__ import annotations

from typing import Dict, Optional, Sequence

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import QFrame, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from ..core import api
from . import pictures
from .pages.check_board import CheckBoardPage
from .pages.checking import CheckingPage
from .pages.confirm import ConfirmPage
from .pages.connect import ConnectPage
from .pages.details import DetailsPage
from .pages.done import DonePage
from .pages.first_start import FirstStartPage
from .pages.fix_settings import FixSettingsPage
from .pages.help_panel import PANEL_WIDTH, HelpPanel
from .pages.home import HomePage
from .pages.installing import InstallingPage
from .pages.light_guide import LightGuidePage
from .pages.machine_type import MachineTypePage
from .pages.sign_in import SignInPage
from .widgets import BasePage, Clickable, StepIndicator, TestRibbon, TopBar

TITLE = "State Life Device Installer"
MIN_WIDTH, MIN_HEIGHT = 900, 600


class MainWindow(QMainWindow):
    def __init__(self, services: api.Services, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from .controller import Controller  # the controller drives this window's pages

        self.services = services
        self.setWindowTitle(TITLE)
        self.setWindowIcon(pictures.app_icon())
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        central = QWidget()
        central.setObjectName("central")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.top_bar = TopBar()
        layout.addWidget(self.top_bar)
        self.ribbon: Optional[TestRibbon] = None
        if services.defaults.is_test_build:
            self.ribbon = TestRibbon()
            layout.addWidget(self.ribbon)
        self.step_frame = QFrame()
        self.step_frame.setObjectName("stepBar")
        step_layout = QVBoxLayout(self.step_frame)
        step_layout.setContentsMargins(16, 10, 16, 6)
        self.steps = StepIndicator()
        step_layout.addWidget(self.steps)
        self.step_frame.hide()
        layout.addWidget(self.step_frame)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.pages: Dict[str, BasePage] = {}
        for page in (
            SignInPage(),
            HomePage(),
            MachineTypePage(),
            DetailsPage(),
            ConnectPage(),
            CheckingPage(),
            ConfirmPage(),
            InstallingPage(),
            FirstStartPage(),
            DonePage(),
            CheckBoardPage(),
            FixSettingsPage(),
            LightGuidePage(),
        ):
            self.pages[page.page_id] = page
            self.stack.addWidget(page)

        self.scrim = Clickable(central)
        self.scrim.setStyleSheet("background: rgba(16, 24, 40, 100);")
        self.scrim.hide()
        self.help_panel = HelpPanel(services.app_version, central)
        self.help_panel.hide()
        self.scrim.clicked.connect(self.close_help)
        self.help_panel.closed.connect(self.close_help)

        self.controller = Controller(services, self)

    # --------------------------------------------------------------- pages

    def page(self, page_id: str) -> BasePage:
        return self.pages[page_id]

    @property
    def current_page(self) -> BasePage:
        return self.stack.currentWidget()

    def show_page(self, page_id: str) -> None:
        self.close_help()
        page = self.pages[page_id]
        self.stack.setCurrentWidget(page)
        page.scroll_to_top()
        QTimer.singleShot(0, page.focus_first)

    def set_steps(self, labels: Optional[Sequence[str]], index: int = -1) -> None:
        if not labels:
            self.step_frame.hide()
            return
        self.steps.set_steps(labels, index)
        self.step_frame.show()

    # ---------------------------------------------------------------- help

    def open_help(self, topic: str) -> None:
        self.help_panel.show_topic(topic)
        self._place_overlay()
        self.scrim.show()
        self.scrim.raise_()
        self.help_panel.show()
        self.help_panel.raise_()
        self.help_panel.close_button.setFocus()

    def close_help(self) -> None:
        if not self.help_panel.isVisible():
            return
        self.help_panel.hide()
        self.scrim.hide()
        page = self.current_page
        if page is not None and page.help_button.isVisible():
            page.help_button.setFocus()

    @property
    def help_open(self) -> bool:
        return self.help_panel.isVisible()

    def _place_overlay(self) -> None:
        central = self.centralWidget()
        top = self.top_bar.height() + (self.ribbon.height() if self.ribbon is not None else 0)
        height = max(0, central.height() - top)
        width = min(PANEL_WIDTH, central.width())
        self.scrim.setGeometry(0, top, central.width(), height)
        self.help_panel.setGeometry(central.width() - width, top, width, height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.help_panel.isVisible():
            self._place_overlay()

    # --------------------------------------------------------------- close

    def closeEvent(self, event: QCloseEvent) -> None:
        reason = self.controller.close_blocker()
        if reason:
            event.ignore()
            QMessageBox.warning(self, "Please wait", reason)
            return
        self.controller.shutdown()
        event.accept()
