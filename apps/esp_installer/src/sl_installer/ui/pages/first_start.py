"""The first start: unplug, wait 3 seconds, plug back in, then watch the board."""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...core.api import Advice, ConsoleEvent, ReplugPhase
from .. import theme
from ..widgets import AdviceCard, AnimatedPicture, BasePage, Card, Spinner, StateIcon, text
from ..widgets import motion
from ..widgets.console import ConsoleView
from . import FIRST_START

UNPLUG, WAIT, REPLUG = "unplug", "wait", "replug"


class _StepCard(Card):
    def __init__(self, number: int, title: str, frames: Sequence[str]) -> None:
        super().__init__(padding=18, spacing=10)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.icon = StateIcon(28)
        self.number = text(str(number), "stepNumber", wrap=False)
        self.number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = text(title, "h3")
        head.addWidget(self.number)
        head.addWidget(self.icon)
        head.addWidget(self.title, 1)
        self.body.addLayout(head)
        self.picture = AnimatedPicture(frames, QSize(220, 170))
        self.body.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignHCenter)
        self.note = text("", "muted")
        self.note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body.addWidget(self.note)
        self.set_state("waiting")

    def set_state(self, state: str) -> None:
        self.setProperty("state", state)
        theme.repolish(self)
        self.icon.setVisible(state == "done")
        self.number.setVisible(state != "done")
        if state == "done":
            self.icon.set_state(StateIcon.DONE)

    def set_compact(self, compact: bool) -> None:
        """Once the board is plugged back in, the pictures have done their job."""
        self.picture.setVisible(not compact)
        self.note.setVisible(not compact)


class FirstStartPage(BasePage):
    page_id = FIRST_START
    help_topic = "first_start"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Start the board", "The board now starts for the first time. Follow the steps.", parent
        )
        self.result = AdviceCard()
        self.result.hide()
        self.content.addWidget(self.result)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.cards: Dict[str, _StepCard] = {
            UNPLUG: _StepCard(1, "Unplug the cable", ("cable_in", "unplug")),
            WAIT: _StepCard(2, "Wait 3 seconds", ("wait",)),
            REPLUG: _StepCard(3, "Plug it back in", ("cable_in", "replug")),
        }
        for card in self.cards.values():
            row.addWidget(card, 1)
        self.content.addLayout(row)

        self.watch = Card("What the board is saying")
        status = QHBoxLayout()
        status.setSpacing(12)
        self.spinner = Spinner(24)
        self.watch_words = text("", "muted")
        status.addWidget(self.spinner)
        status.addWidget(self.watch_words, 1)
        self.watch.body.addLayout(status)
        self.console = ConsoleView("Waiting for the board to start.")
        self.watch.body.addWidget(self.console)
        self.content.addWidget(self.watch)

        self.phase: Optional[ReplugPhase] = None
        self.countdown = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.set_primary(None)
        self.set_back(False)

    def start(self) -> None:
        self.phase = None
        self._timer.stop()
        for card in self.cards.values():
            card.set_state("waiting")
            card.set_compact(False)
            card.note.setText("")
        self.watch.hide()
        self.result.hide()
        self.console.clear()
        self.set_primary(None)
        self.set_secondary(None)

    def set_phase(self, phase: ReplugPhase) -> None:
        self.phase = phase
        if phase is ReplugPhase.WAITING_UNPLUG:
            self.cards[UNPLUG].set_state("active")
            self.cards[UNPLUG].note.setText("Pull the cable out now.")
        elif phase is ReplugPhase.WAITING_REPLUG:
            self.cards[UNPLUG].set_state("done")
            self.cards[UNPLUG].note.setText("Done.")
            self.start_countdown()
        elif phase is ReplugPhase.WATCHING:
            self._timer.stop()
            for key in (UNPLUG, WAIT, REPLUG):
                self.cards[key].set_state("done")
                self.cards[key].set_compact(True)
            self.watch.show()
            self.spinner.show()
            self.watch_words.setText("Watching the board start. This can take up to 2 minutes.")

    def start_countdown(self, seconds: int = 3) -> None:
        self.countdown = seconds
        self.cards[WAIT].set_state("active")
        self._show_count()
        if not motion.frozen():
            self._timer.start()

    def _tick(self) -> None:
        self.countdown -= 1
        self._show_count()
        if self.countdown <= 0:
            self._timer.stop()

    def _show_count(self) -> None:
        if self.countdown > 0:
            unit = "second" if self.countdown == 1 else "seconds"
            self.cards[WAIT].note.setText(f"{self.countdown} {unit} left")
            self.cards[REPLUG].set_state("waiting")
            self.cards[REPLUG].note.setText("")
        else:
            self.cards[WAIT].set_state("done")
            self.cards[WAIT].note.setText("Done.")
            self.cards[REPLUG].set_state("active")
            self.cards[REPLUG].note.setText("Plug it back in now. Do not press any button.")

    def add_event(self, event: ConsoleEvent) -> None:
        self.console.add(event)

    def show_result(self, advice: Advice, actions: Sequence[Tuple[str, str, str]] = ()) -> None:
        """The outcome card at the top; its buttons go in the footer."""
        self._timer.stop()
        self.spinner.hide()
        self.watch_words.setText("Finished watching.")
        for card in self.cards.values():
            card.set_compact(True)
        self.result.set_advice(advice)
        self.result.show()
        self.show_actions(actions)
        self.scroll_to_top()

    def first_focus(self) -> Optional[QWidget]:
        if self.result.isVisible():
            return super().first_focus()
        return None
