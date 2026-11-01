"""ZKTeco or Hikvision: two big cards."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...core.api import Family
from ..widgets import BasePage, ChoiceCard
from . import MACHINE


class MachineTypePage(BasePage):
    page_id = MACHINE
    help_topic = "machine"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Which attendance machine is it for?",
            "Look for the brand name on the front of the attendance machine.",
            parent,
        )
        row = QHBoxLayout()
        row.setSpacing(24)
        size = QSize(240, 180)
        self.zkt_card = ChoiceCard(
            "ZKTeco",
            "The word ZKTeco is printed on the machine.",
            "machine_zkt",
            vertical=True,
            picture_size=size,
        )
        self.hik_card = ChoiceCard(
            "Hikvision",
            "The word Hikvision is printed on the machine.",
            "machine_hik",
            vertical=True,
            picture_size=size,
        )
        for card, family in ((self.zkt_card, Family.ZKT), (self.hik_card, Family.HIKVISION)):
            card.setCheckable(True)
            card.clicked.connect(lambda _checked=False, value=family: self.action.emit(value.value))
            row.addWidget(card)
        self.content.addLayout(row)
        self.set_primary(None)
        self.set_back(True)

    def set_selected(self, family: Optional[Family]) -> None:
        self.zkt_card.setChecked(family is Family.ZKT)
        self.hik_card.setChecked(family is Family.HIKVISION)

    def first_focus(self) -> Optional[QWidget]:
        return self.hik_card if self.hik_card.isChecked() else self.zkt_card
