"""The last look before anything is written to the board."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtWidgets import QCheckBox, QWidget

from ...core.api import InstallMode, Severity
from ..widgets import BasePage, Card, FactList, Notice
from . import CONFIRM

_WORDS = {
    InstallMode.NEW_BOARD: (
        "Start the setup",
        "This prepares the board for State Life for good",
        "After this, the board can only be used for State Life attendance. It cannot be undone.",
    ),
    InstallMode.RESUME: (
        "Finish the setup",
        "This finishes the setup that stopped earlier",
        "The board is already locked for State Life. The app copies everything again.",
    ),
    InstallMode.CHANGE_SETTINGS: (
        "Write the new settings",
        "This replaces the board's settings",
        "The software stays the same. The board forgets the site's Wi-Fi, and punches still "
        "on the board are deleted.",
    ),
}


class ConfirmPage(BasePage):
    page_id = CONFIRM
    help_topic = "confirm"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Check the details", "Make sure everything below is right before you start.", parent
        )
        # The warning and the tick box come first: Start stays grey until the box is ticked.
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self.understand = QCheckBox("I understand")
        self.understand.setAccessibleName("I understand that this cannot be undone")
        self.content.addWidget(self.understand)
        self.understand.toggled.connect(self._ticked)
        summary = Card("What will be set")
        self.facts = FactList()
        summary.body.addWidget(self.facts)
        self.content.addWidget(summary)
        self.mode = InstallMode.NEW_BOARD
        self.set_primary("Start the setup", "start", enabled=False)
        self.set_back(True)

    def show_summary(self, rows: Sequence[Tuple[str, str]], mode: InstallMode) -> None:
        self.mode = mode
        start, title, body = _WORDS[mode]
        self.facts.set_rows(rows)
        severity = Severity.WARNING if mode is InstallMode.NEW_BOARD else Severity.INFO
        self.notice.set_notice(title, body, severity)
        needs_tick = mode is InstallMode.NEW_BOARD
        self.understand.blockSignals(True)
        self.understand.setChecked(False)
        self.understand.blockSignals(False)
        self.understand.setVisible(needs_tick)
        self.set_primary(start, "start", enabled=not needs_tick)

    def _ticked(self, checked: bool) -> None:
        self.set_primary_enabled(checked or self.mode is not InstallMode.NEW_BOARD)
        if checked and self.understand.hasFocus():
            # Ticked with the keyboard: Enter now starts, it does not untick (E12).
            self.primary_button.setFocus()

    def first_focus(self) -> Optional[QWidget]:
        return self.understand if self.understand.isVisible() else self.primary_button
