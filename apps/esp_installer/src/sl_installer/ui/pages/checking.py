"""Reads the board, looks it up at head office, and says what can be done."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QWidget

from ...core import api
from ...core.api import Advice
from .. import naming, wording
from ..widgets import AdviceCard, BasePage, Card, FactList, ProgressStepList, StateIcon
from . import CHECKING

READ, LOOKUP = "read", "lookup"
_WORKING = "This takes up to a minute. Keep the cable plugged in."


class CheckingPage(BasePage):
    page_id = CHECKING
    help_topic = "checking"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Checking the board", _WORKING, parent)
        # The outcome goes first, so it is seen without scrolling.
        self.result = AdviceCard(picture_size=QSize(208, 156))
        self.result.hide()
        self.result.action.connect(self.action)
        self.content.addWidget(self.result)
        progress = Card()
        self.steps = ProgressStepList()
        self.steps.set_steps(
            ((READ, "Reading the board"), (LOOKUP, "Looking it up at head office"))
        )
        progress.body.addWidget(self.steps)
        self.content.addWidget(progress)
        self.facts_card = Card("About this board")
        self.facts = FactList()
        self.facts_card.body.addWidget(self.facts)
        self.facts_card.hide()
        self.content.addWidget(self.facts_card)
        self.set_primary("Next", "next", enabled=False)
        self.set_back(True)

    def start(self) -> None:
        self.set_subtitle(_WORKING)
        self.steps.set_state(READ, StateIcon.WORKING)
        self.steps.set_state(LOOKUP, StateIcon.WAITING)
        self.result.hide()
        self.facts_card.hide()
        self.set_primary("Next", "next", enabled=False)
        self.set_secondary(None)

    def board_read(self, board: api.BoardInfo) -> None:
        self.steps.set_state(READ, StateIcon.DONE)
        self.steps.set_state(LOOKUP, StateIcon.WORKING)
        self.show_facts(board, None)

    def show_facts(self, board: api.BoardInfo, record: Optional[api.DeviceRecord]) -> None:
        rows = [
            ("Board ID", naming.board_id(board.mac)),
            ("Board type", wording.HARDWARE_WORDS.get(board.hardware, "")),
            ("Setup state", wording.SECURITY_WORDS.get(board.security, "")),
        ]
        if record is not None and record.known:
            place = record.zone_id or "-"
            if record.device_id:
                place = f"{place}, device number {record.device_id}"
            rows.append(("Head office knows it as", place))
            if record.status_text:
                rows.append(("Status", record.status_text))
        self.facts.set_rows(rows)
        self.facts_card.show()

    def show_result(
        self,
        advice: Advice,
        primary: Optional[Tuple[str, str]] = None,
        secondary: Optional[Tuple[str, str]] = None,
        failed_step: Optional[str] = None,
        actions: Sequence[Tuple[str, str, str]] = (),
    ) -> None:
        """The outcome card. ``primary``/``secondary`` are (words, action) for the footer."""
        if failed_step:
            self.steps.set_state(failed_step, StateIcon.FAILED)
            for key in self.steps.keys():
                if self.steps.state_of(key) == StateIcon.WORKING:
                    self.steps.set_state(key, StateIcon.WAITING)
        else:
            for key in self.steps.keys():
                self.steps.set_state(key, StateIcon.DONE)
        self.result.set_advice(advice, actions)
        self.result.show()
        self.set_subtitle("The check is finished. Read the result below.")
        self.scroll_to_top()
        if primary:
            self.set_primary(primary[0], primary[1], enabled=True)
        else:
            self.set_primary(None)
        if secondary:
            self.set_secondary(secondary[0], secondary[1])
        else:
            self.set_secondary(None)
