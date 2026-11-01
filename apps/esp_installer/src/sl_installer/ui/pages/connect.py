"""Plug in the board; the screen watches the USB sockets by itself."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core.api import Advice, Severity, UsbCandidate
from .. import naming
from ..widgets import AdviceCard, BasePage, Card, Notice, Picture, Spinner, text
from . import CONNECT

STEPS = (
    "Use the cable that came with the board.",
    "Plug it into the socket marked USB on the board.",
    "Plug the other end straight into this computer.",
)


def numbered_steps(steps: Sequence[str], role: Optional[str] = "h3") -> QVBoxLayout:
    """1, 2, 3 ... in blue circles, each with one short instruction."""
    column = QVBoxLayout()
    column.setSpacing(12)
    for index, words in enumerate(steps, start=1):
        row = QHBoxLayout()
        row.setSpacing(14)
        number = text(str(index), "stepNumber", wrap=False)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text(words, role), 1)
        column.addLayout(row)
    return column


class ConnectPage(BasePage):
    page_id = CONNECT
    help_topic = "connect"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Connect the board", "Plug the board into this computer with its USB cable.", parent
        )
        # What is happening goes first, so it is seen without scrolling: a problem, or
        # the board found, then "still looking".
        self.problem = AdviceCard(picture_size=QSize(208, 156))
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)
        self._actions: Tuple[Tuple[str, str, str], ...] = ()
        self.found = Notice()
        self.found.hide()
        self.content.addWidget(self.found)
        self.waiting = Card(soft=True, padding=18)
        waiting_row = QHBoxLayout()
        waiting_row.setSpacing(14)
        self.spinner = Spinner(28)
        waiting_row.addWidget(self.spinner)
        words = QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(text("Looking for the board", "h3"))
        words.addWidget(text("This screen changes by itself when the board is found.", "muted"))
        waiting_row.addLayout(words, 1)
        self.waiting.body.addLayout(waiting_row)
        self.content.addWidget(self.waiting)

        card = Card()
        row = QHBoxLayout()
        row.setSpacing(32)
        row.addWidget(Picture("cable", QSize(300, 225)), 0, Qt.AlignmentFlag.AlignTop)
        steps = numbered_steps(STEPS)
        steps.addSpacing(4)
        steps.addWidget(Picture("usb_socket", QSize(160, 140)), 0, Qt.AlignmentFlag.AlignLeft)
        steps.addStretch(1)
        row.addLayout(steps, 1)
        card.body.addLayout(row)
        self.content.addWidget(card)
        self.set_primary("Next", "next", enabled=False)
        self.set_back(True)

    def show_waiting(self) -> None:
        self.waiting.show()
        self.found.hide()
        self.problem.hide()
        self.set_primary_enabled(False)

    def show_found(self, board: UsbCandidate) -> None:
        self.waiting.hide()
        self.problem.hide()
        self.found.set_notice(
            "Board found", f"Board ID {naming.board_id(board.mac)}. Click Next.", Severity.SUCCESS
        )
        self.found.show()
        self.set_primary_enabled(True)
        self.primary_button.setFocus()

    def show_problem(
        self,
        advice: Advice,
        still_looking: bool = True,
        actions: Sequence[Tuple[str, str, str]] = (),
    ) -> None:
        """Next stays grey; ``actions`` are buttons on the card (e.g. Check again)."""
        self.found.hide()
        self.waiting.setVisible(still_looking)
        actions = tuple(actions)
        if self.problem.advice != advice or self._actions != actions:
            self.problem.set_advice(advice, actions)
            self._actions = actions
        self.problem.show()
        self.set_primary_enabled(False)

    def reset(self) -> None:
        self.show_waiting()
