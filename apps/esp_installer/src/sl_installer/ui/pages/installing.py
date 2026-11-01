"""The install steps with progress. Back is not offered here."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtWidgets import QWidget

from ...core.api import Advice, InstallMode, Severity, StepId, StepProgress, steps_for
from ..widgets import AdviceCard, BasePage, Card, Notice, ProgressStepList, StateIcon
from ..wording import STEP_WORDS
from . import INSTALLING

_TITLES = {
    InstallMode.NEW_BOARD: ("Setting up the board", "This takes about 3 minutes. Please wait."),
    InstallMode.RESUME: ("Finishing the setup", "This takes about 3 minutes. Please wait."),
    InstallMode.CHANGE_SETTINGS: (
        "Writing the new settings",
        "This takes about a minute. Please wait.",
    ),
}


class InstallingPage(BasePage):
    page_id = INSTALLING
    help_topic = "installing"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(*_TITLES[InstallMode.NEW_BOARD], parent=parent)
        self.problem = AdviceCard()
        self.problem.hide()
        self.content.addWidget(self.problem)
        self.banner = Notice(
            "Keep the cable plugged in",
            "Do not unplug the board or close this window until the app tells you to.",
            Severity.WARNING,
        )
        self.content.addWidget(self.banner)
        card = Card()
        self.steps = ProgressStepList()
        card.body.addWidget(self.steps)
        self.content.addWidget(card)
        self.mode = InstallMode.NEW_BOARD
        self.current: Optional[StepId] = None
        self.set_primary(None)
        self.set_back(False)

    def start(self, mode: InstallMode) -> None:
        self.mode = mode
        self.set_title(*_TITLES[mode])
        self.steps.set_steps([(step.value, STEP_WORDS[step]) for step in steps_for(mode)])
        self.current = None
        self.problem.hide()
        self.banner.set_notice(
            "Keep the cable plugged in",
            "Do not unplug the board or close this window until the app tells you to.",
            Severity.WARNING,
        )
        self.banner.show()
        self.set_primary(None)
        self.set_back(False)

    def update_progress(self, progress: StepProgress) -> None:
        order = [StepId(key) for key in self.steps.keys()]
        if progress.step not in order:
            return
        index = order.index(progress.step)
        for earlier in order[:index]:
            self.steps.set_state(earlier.value, StateIcon.DONE)
        self.current = progress.step
        if progress.fraction is not None and progress.fraction >= 1.0:
            self.steps.set_state(progress.step.value, StateIcon.DONE)
        else:
            self.steps.set_state(
                progress.step.value, StateIcon.WORKING, progress.fraction, progress.detail
            )

    def set_cancel_allowed(self, allowed: bool) -> None:
        if allowed:
            self.set_secondary("Stop", "cancel", kind="danger")
        else:
            self.set_secondary(None)

    def finish_all(self) -> None:
        for key in self.steps.keys():
            self.steps.set_state(key, StateIcon.DONE)
        self.set_secondary(None)

    def show_problem(
        self,
        advice: Advice,
        actions: Sequence[Tuple[str, str, str]],
        failed_step: Optional[StepId] = None,
    ) -> None:
        step = failed_step or self.current
        if step is not None and step.value in self.steps.keys():
            self.steps.set_state(step.value, StateIcon.FAILED)
        for key in self.steps.keys():
            if self.steps.state_of(key) == StateIcon.WORKING:
                self.steps.set_state(key, StateIcon.WAITING)
        self.banner.hide()
        self.set_subtitle("Read what happened, then choose what to do.")
        self.problem.set_advice(advice)
        self.problem.show()
        self.show_actions(actions)
        self.scroll_to_top()

    def first_focus(self) -> Optional[QWidget]:
        if self.problem.isVisible():
            return super().first_focus()
        return None
