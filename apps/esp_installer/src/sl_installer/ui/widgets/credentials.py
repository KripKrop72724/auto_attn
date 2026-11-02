"""Things to type on a phone, large, each with a Copy button: the board's own Wi-Fi."""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QToolButton, QWidget

from .. import naming
from .basics import text

# Shown when a test build has no hotspot password (release builds always have one).
NO_TEST_PASSWORD = "Not in this test build"


class CredentialList(QFrame):
    """The things to type on the phone, large, each with a Copy button."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("credential")
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(18, 12, 14, 12)
        self._grid.setHorizontalSpacing(18)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(1, 1)
        self.labels: Dict[str, QLabel] = {}
        self.values: Dict[str, QLabel] = {}
        self.copies: Dict[str, QToolButton] = {}
        self._copyable: Dict[str, str] = {}

    def add_row(self, key: str, label: str) -> None:
        row = len(self.values)
        name = text(label, "muted", wrap=False)
        value = text("", "mono", selectable=True)
        copy = QToolButton()
        copy.setObjectName("copy")
        copy.setText("Copy")
        copy.setCursor(Qt.CursorShape.PointingHandCursor)
        copy.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        copy.setAccessibleName(f"Copy the {label.lower()}")
        copy.clicked.connect(lambda _checked=False, which=key: self._copy(which))
        self._grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignVCenter)
        self._grid.addWidget(value, row, 1, Qt.AlignmentFlag.AlignVCenter)
        self._grid.addWidget(copy, row, 2, Qt.AlignmentFlag.AlignVCenter)
        self.labels[key] = name
        self.values[key] = value
        self.copies[key] = copy

    def set_value(self, key: str, value: str, copyable: bool = True) -> None:
        self.values[key].setText(value)
        self.values[key].setAccessibleName(value)
        self._copyable[key] = value if copyable else ""
        self.copies[key].setVisible(copyable)
        self.copies[key].setText("Copy")

    def _copy(self, key: str) -> None:
        value = self._copyable.get(key)
        if not value:
            return
        QGuiApplication.clipboard().setText(value)
        button = self.copies[key]
        button.setText("Copied")
        QTimer.singleShot(2000, lambda: button.setText("Copy"))


class HotspotDetails(CredentialList):
    """The board's own Wi-Fi (setup hotspot): its name, its password and the setup page.

    The same three rows, with the same names, wherever the app shows them: the
    Done screen, Check a board and the light guide (review E1).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.add_row("hotspot", "Board's Wi-Fi name")
        self.add_row("password", "Board's Wi-Fi password")
        self.add_row("address", "Wi-Fi setup page")
        self.set_hotspot(None, "")

    def set_hotspot(self, name: Optional[str], password: str) -> None:
        """``name`` is None when no board is known: then only its pattern is shown."""
        if name:
            self.set_value("hotspot", name)
        else:
            # The last four letters come from the board ID, so they differ on every board.
            self.set_value("hotspot", f"{naming.HOTSPOT_PREFIX}XXXX", copyable=False)
        if password:
            self.set_value("password", password)
        else:
            self.set_value("password", NO_TEST_PASSWORD, copyable=False)
        self.set_value("address", naming.SETUP_PAGE_ADDRESS)
