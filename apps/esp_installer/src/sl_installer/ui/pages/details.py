"""Details for a new board: the place (ZONE ID) and, for Hikvision, the machine."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...core.api import Advice, Family
from ..widgets import AdviceCard, BasePage
from . import DETAILS
from .settings_form import SettingsForm

_SUBTITLES = {
    Family.ZKT: "Type the ZONE ID that head office gave you. The rest is filled in for you.",
    Family.HIKVISION: (
        "Type the ZONE ID that head office gave you, and the attendance machine's details."
    ),
}


class DetailsPage(BasePage):
    page_id = DETAILS
    help_topic = "details"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Where will the board be used?", _SUBTITLES[Family.ZKT], parent)
        self.problem = AdviceCard()
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)
        self.form = SettingsForm(show_bundles=True)
        self.content.addWidget(self.form)
        self.set_primary("Next", "next")
        self.set_back(True)

    def set_family(self, family: Family) -> None:
        self.set_subtitle(_SUBTITLES[family])

    def show_problem(self, advice: Optional[Advice], retry: bool = True) -> None:
        if advice is None:
            self.problem.hide()
            return
        self.problem.set_advice(advice, [("Try again", "reload", "secondary")] if retry else [])
        self.problem.show()
        self.reveal(self.problem)

    def first_focus(self) -> Optional[QWidget]:
        return self.form.zone_id
