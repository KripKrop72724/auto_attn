"""The first start: unplug, wait 3 seconds, plug back in, then watch the board.

Once the board has started well, the same steps once more (``start_again``): the
restart check, which shows that the board keeps its settings when it starts again.
"""

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

# The words of the first start (False) and of the restart check (True).
_TITLES: Dict[bool, Tuple[str, str]] = {
    False: ("Start the board", "The board now starts for the first time. Follow the steps."),
    True: ("Start the board again", "The board must keep its settings when it starts again."),
}
_CARD_TITLES: Dict[str, Dict[bool, str]] = {
    UNPLUG: {False: "Unplug the cable", True: "Unplug the cable again"},
    WAIT: {False: "Wait 3 seconds", True: "Wait 3 seconds"},
    REPLUG: {False: "Plug it back in", True: "Plug it back in"},
}
_WATCHING: Dict[bool, str] = {
    False: "Watching the board start. This can take up to 2 minutes.",
    True: "Watching the board start again. This can take up to 2 minutes.",
}


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
        super().__init__(*_TITLES[False], parent)
        # The outcome card; during the restart check it says why the steps come again.
        self.result = AdviceCard()
        self.result.hide()
        self.content.addWidget(self.result)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.cards: Dict[str, _StepCard] = {
            UNPLUG: _StepCard(1, _CARD_TITLES[UNPLUG][False], ("cable_in", "unplug")),
            WAIT: _StepCard(2, _CARD_TITLES[WAIT][False], ("wait",)),
            REPLUG: _StepCard(3, _CARD_TITLES[REPLUG][False], ("cable_in", "replug")),
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
        self.again = False  # True during the restart check
        self.countdown = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.set_primary(None)
        self.set_back(False)

    def start(self) -> None:
        """The first start: the steps from the top, nothing watched yet."""
        self._reset(again=False)
        self.result.hide()

    def start_again(self, notice: Advice) -> None:
        """The restart check: the same steps once more, under ``notice``, which says why."""
        self._reset(again=True)
        self.result.set_advice(notice)
        self.result.show()
        self.scroll_to_top()

    def _reset(self, again: bool) -> None:
        self.again = again
        self.set_title(*_TITLES[again])
        self.phase = None
        self._timer.stop()
        for key, card in self.cards.items():
            card.title.setText(_CARD_TITLES[key][again])
            card.set_state("waiting")
            card.set_compact(False)
            card.note.setText("")
        self.watch.hide()
        self.console.clear()
        self.set_primary(None)
        self.set_secondary(None)

    def set_phase(self, phase: ReplugPhase) -> None:
        self.phase = phase
        if phase is ReplugPhase.WAITING_UNPLUG:
            self.cards[UNPLUG].set_state("active")
            self.cards[UNPLUG].note.setText("Pull the cable out now.")
            self._show_steps()
        elif phase is ReplugPhase.WAITING_REPLUG:
            self.cards[UNPLUG].set_state("done")
            self.cards[UNPLUG].note.setText("Done.")
            self.start_countdown()
            self._show_steps()
        elif phase is ReplugPhase.WATCHING:
            self._timer.stop()
            for key in (UNPLUG, WAIT, REPLUG):
                self.cards[key].set_state("done")
                self.cards[key].set_compact(True)
            self.watch.show()
            self.spinner.show()
            self.watch_words.setText(_WATCHING[self.again])

    def _show_steps(self) -> None:
        """During the restart check the card above pushes the steps down: on a small screen
        their live words ("Pull the cable out now.", the countdown, "Plug it back in now")
        would be out of view, so the page scrolls to them. The three cards share one row."""
        if self.again:
            self.reveal(self.cards[REPLUG])

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
            self._show_steps()  # the longer words can make the row taller

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
