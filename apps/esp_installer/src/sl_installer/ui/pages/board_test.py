"""The board test running: the steps with ticks, a bar, and what the board says.

Two uses. Test a new board (and the test before every setup): the app copies a
small test program to the board, which tests its memory, memory chip,
processor and Wi-Fi radio, then the app removes it again (core/boardtest.py).
Check a failed board, on a locked board: the board is unplugged and plugged back
in, and the app watches what it says while it starts. When the engine asks for
a replug (``set_phase``), the three pictures of the first start show here too.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QWidget

from ...core import api
from ...core.api import Advice, ConsoleEvent, ReplugPhase, Severity
from .. import naming, wording
from ..widgets import (
    AdviceCard,
    BasePage,
    Card,
    FactList,
    Notice,
    ProgressStepList,
    Spinner,
    StateIcon,
    text,
)
from ..widgets import motion
from ..widgets.console import ConsoleView
from . import BOARD_TEST
from .first_start import REPLUG, UNPLUG, WAIT, _StepCard

READ = "read"
# The rows of a test, and which row each engine stage (BoardTestProgress.stage) moves.
TEST_ROWS: Tuple[Tuple[str, str], ...] = (
    (READ, "Reading the board"),
    ("write", "Copying the test program to the board"),
    ("memory", "Testing the extra memory"),
    ("flash", "Testing the memory chip"),
    ("cpu", "Testing the processor"),
    ("wifi", "Testing the Wi-Fi radio"),
    ("cleanup", "Removing the test program"),
)
CHECK_ROWS: Tuple[Tuple[str, str], ...] = (
    (READ, "Reading the board"),
    ("watch", "Watching the board start"),
)
STAGE_ROW: Dict[str, str] = {
    "check": "write",
    "write": "write",
    "start": "write",
    "psram": "memory",
    "iram": "memory",
    "flash": "flash",
    "cpu": "cpu",
    "wifi": "wifi",
    "cleanup": "cleanup",
    "startup": "watch",
}
# Findings name these stages; a failed one marks its row.
FINDING_ROW: Dict[str, str] = {**STAGE_ROW, "check": READ}

_TITLES = {
    "test": ("Testing the board", "This takes about 2 minutes. Keep the cable plugged in."),
    "check": ("Checking the failed board", "Follow the steps. Keep this window open."),
}
_TEST_NOTICE = (
    "The board is not locked",
    "The app copies a small test program to the board, tests the board, then removes the "
    "program again. Nothing on the board is changed for good.",
)
_REPLUG_WORDS = {
    "test": (
        "Unplug the board and plug it back in",
        "The test program did not start by itself. Follow the 3 steps below.",
    ),
    "check": (
        "Unplug the board and plug it back in",
        "The app watches what the board says while it starts. Follow the 3 steps below.",
    ),
}


class BoardTestPage(BasePage):
    page_id = BOARD_TEST
    help_topic = "board_test"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(*_TITLES["test"], parent)
        # A problem, or a board that cannot be tested this way; first, so it is seen.
        self.problem = AdviceCard(picture_size=QSize(208, 156))
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)
        self.notice = Notice(*_TEST_NOTICE, Severity.INFO)
        self.content.addWidget(self.notice)

        # The replug steps, only while the engine asks for them.
        self.replug_card = Card(padding=20, spacing=12)
        self.replug_title = text("", "h2")
        self.replug_words = text("", "muted")
        self.replug_card.body.addWidget(self.replug_title)
        self.replug_card.body.addWidget(self.replug_words)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.cards: Dict[str, _StepCard] = {
            UNPLUG: _StepCard(1, "Unplug the cable", ("cable_in", "unplug")),
            WAIT: _StepCard(2, "Wait 3 seconds", ("wait",)),
            REPLUG: _StepCard(3, "Plug it back in", ("cable_in", "replug")),
        }
        for card in self.cards.values():
            row.addWidget(card, 1)
        self.replug_card.body.addLayout(row)
        self.replug_card.hide()
        self.content.addWidget(self.replug_card)

        self.progress = progress = Card()
        overall = QHBoxLayout()
        overall.setSpacing(12)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("How far the test is")
        self.percent = text("", "muted", wrap=False)
        overall.addWidget(self.bar, 1)
        overall.addWidget(self.percent)
        progress.body.addLayout(overall)
        self.steps = ProgressStepList()
        progress.body.addWidget(self.steps)
        self.content.addWidget(progress)

        self.facts_card = Card("About this board")
        self.facts = FactList(key_width=170)
        self.facts_card.body.addWidget(self.facts)
        self.facts_card.hide()
        self.content.addWidget(self.facts_card)

        self.watch = Card("What the board is saying")
        status = QHBoxLayout()
        status.setSpacing(12)
        self.spinner = Spinner(24)
        self.watch_words = text("", "muted")
        status.addWidget(self.spinner)
        status.addWidget(self.watch_words, 1)
        self.watch.body.addLayout(status)
        self.console = ConsoleView("Nothing yet.")
        self.watch.body.addWidget(self.console)
        self.content.addWidget(self.watch)

        self.labels: Dict[str, str] = {}
        self.mode = "test"
        self.phase: Optional[ReplugPhase] = None
        self.stage = ""
        self.countdown = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.set_primary(None)
        self.set_back(False)
        self.start()

    # ---------------------------------------------------------------- states

    def _set_rows(self, mode: str) -> None:
        rows = TEST_ROWS if mode == "test" else CHECK_ROWS
        self.labels = dict(rows)
        self.steps.set_steps(rows)
        # A watched start has no length to show; a test has.
        self.bar.setVisible(mode == "test")
        self.percent.setVisible(mode == "test")
        self.progress.show()

    def start(self, mode: str = "test", board_read: bool = False) -> None:
        """A new run: ``mode`` "test" or "check"; ``board_read`` when Checking read it."""
        self.mode = mode
        self.set_title(*_TITLES[mode])
        self.phase = None
        self.stage = ""
        self._timer.stop()
        self.problem.hide()
        self.notice.setVisible(mode == "test")
        self.notice.set_notice(*_TEST_NOTICE, Severity.INFO)
        self.replug_card.hide()
        for card in self.cards.values():
            card.set_state("waiting")
            card.set_compact(False)
            card.note.setText("")
        self._set_rows(mode)
        self.steps.set_state(READ, StateIcon.DONE if board_read else StateIcon.WORKING)
        self._set_overall(0.0)
        self.facts_card.setVisible(board_read and bool(self.facts.values()))
        self.console.clear()
        self.watch.hide()
        self.spinner.show()
        self.watch_words.setText("")
        self.set_primary(None)
        self.set_secondary(None)
        self.set_back(False)

    def board_read(self, board: api.BoardInfo) -> None:
        self.steps.set_state(READ, StateIcon.DONE)
        rows = [
            ("Board ID", naming.board_id(board.mac)),
            ("Board type", wording.HARDWARE_WORDS.get(board.hardware, "")),
        ]
        rows += wording.refused_hardware_rows(board)
        rows.append(("Setup state", wording.SECURITY_WORDS.get(board.security, "")))
        self.facts.set_rows(rows)
        self.facts_card.show()

    def set_mode(self, mode: str) -> None:
        """Switch the rows after the board was read (a failed board that can be tested)."""
        if mode == self.mode:
            return
        self.mode = mode
        self.set_title(*_TITLES[mode])
        self.notice.setVisible(mode == "test")
        self._set_rows(mode)
        self.steps.set_state(READ, StateIcon.DONE)

    def running(self) -> None:
        """The board was read; the test (or the watch) starts now. No Back while it runs:
        leaving would drop the close guards while the engine still cleans up."""
        self.problem.hide()
        self.set_back(False)
        self.set_primary(None)
        self.progress.show()
        self.steps.set_state(READ, StateIcon.DONE)
        first = "write" if self.mode == "test" else "watch"
        self.steps.set_state(first, StateIcon.WORKING)
        self.watch.show()
        self.spinner.show()
        self.watch_words.setText(
            "The test is running." if self.mode == "test" else "Watching the board."
        )

    def update_progress(self, progress: object) -> None:
        stage = str(getattr(progress, "stage", "") or "")
        fraction = getattr(progress, "fraction", None)
        detail = str(getattr(progress, "detail", "") or "")
        self.stage = stage
        if fraction is not None:
            self._set_overall(fraction)
        if stage == "done":
            for key in self.steps.keys():
                if self.steps.state_of(key) == StateIcon.WORKING:
                    self.steps.set_state(key, StateIcon.DONE)
            self._set_overall(1.0)
            return
        row = STAGE_ROW.get(stage)
        keys = self.steps.keys()
        if row not in keys:
            return
        index = keys.index(row)
        for earlier in keys[:index]:
            if self.steps.state_of(earlier) in (StateIcon.WAITING, StateIcon.WORKING):
                self.steps.set_state(earlier, StateIcon.DONE)
        # The row names the stage already; its detail says more, or nothing.
        shown = "" if detail.strip().lower() == self.labels.get(row, "").lower() else detail
        self.steps.set_state(row, StateIcon.WORKING, None, shown)
        if detail:
            self.watch_words.setText(detail)

    def _set_overall(self, fraction: float) -> None:
        value = max(0.0, min(1.0, float(fraction)))
        self.bar.setValue(int(value * 1000))
        self.percent.setText(f"{int(round(value * 100))}%")

    def set_cancel_allowed(self, allowed: bool) -> None:
        if allowed:
            words = "Stop the test" if self.mode == "test" else "Stop"
            self.set_secondary(words, "cancel", kind="danger")
        else:
            self.set_secondary(None)

    def stopping(self) -> None:
        self.set_secondary(None)
        words = (
            "The app is stopping the test and removing the test program. Please wait."
            if self.mode == "test"
            else "The app is stopping. Please wait."
        )
        self.notice.set_notice("Stopping", words, Severity.INFO)
        self.notice.show()

    # ---------------------------------------------------------------- replug

    def set_phase(self, phase: ReplugPhase) -> None:
        self.phase = phase
        if phase is ReplugPhase.WATCHING:
            self._timer.stop()
            for key in (UNPLUG, WAIT, REPLUG):
                self.cards[key].set_state("done")
            self.replug_card.hide()
            self.watch.show()
            self.spinner.show()
            return
        title, words = _REPLUG_WORDS[self.mode]
        self.replug_title.setText(title)
        self.replug_words.setText(words)
        if self.replug_card.isHidden():
            self.replug_card.show()
            self.reveal(self.replug_card)
        if phase is ReplugPhase.WAITING_UNPLUG:
            for card in self.cards.values():
                card.set_state("waiting")
                card.note.setText("")
            self.cards[UNPLUG].set_state("active")
            self.cards[UNPLUG].note.setText("Pull the cable out now.")
        elif phase is ReplugPhase.WAITING_REPLUG:
            self.cards[UNPLUG].set_state("done")
            self.cards[UNPLUG].note.setText("Done.")
            self.start_countdown()

    def start_countdown(self, seconds: int = 3) -> None:
        self.countdown = seconds
        self.cards[WAIT].set_state("active")
        self._show_count()
        if seconds > 0 and not motion.frozen():
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

    # ---------------------------------------------------------------- console

    def add_event(self, event: ConsoleEvent) -> None:
        if self.watch.isHidden():
            self.watch.show()
        self.console.add(event)

    # ---------------------------------------------------------------- the end

    def finish(self, report: object) -> None:
        """Ticks (and crosses) from the report, for the moment before the result page."""
        self._timer.stop()
        self.spinner.hide()
        self.watch_words.setText("Finished.")
        self.set_secondary(None)
        failed = {
            FINDING_ROW.get(str(getattr(item, "stage", "")), "")
            for item in getattr(report, "findings", ()) or ()
            if getattr(item, "result", "") == "fail"
        }
        for key in self.steps.keys():
            if key in failed:
                self.steps.set_state(key, StateIcon.FAILED)
            elif self.steps.state_of(key) == StateIcon.WORKING:
                self.steps.set_state(key, StateIcon.DONE)
        self._set_overall(1.0)

    def show_problem(
        self, advice: Advice, actions: Sequence[Tuple[str, str, str]] = ()
    ) -> None:
        """A problem card at the top, its buttons on the card; nothing runs any more."""
        self._timer.stop()
        self.spinner.hide()
        self.replug_card.hide()
        started = any(
            self.steps.state_of(key) != StateIcon.WAITING
            for key in self.steps.keys()
            if key != READ
        )
        # Before anything ran (a board that cannot be tested) the steps would only distract.
        self.progress.setVisible(started)
        for key in self.steps.keys():
            if self.steps.state_of(key) == StateIcon.WORKING:
                self.steps.set_state(key, StateIcon.WAITING)
        if self.watch_words.text():
            self.watch_words.setText("Stopped.")
        self.notice.hide()
        self.problem.set_advice(advice, actions)
        self.problem.show()
        self.set_secondary(None)
        self.set_back(True)
        self.scroll_to_top()

    def first_focus(self) -> Optional[QWidget]:
        if self.problem.isVisible():
            return self.problem.first_button() or super().first_focus()
        return None
