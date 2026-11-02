"""The first choice after signing in."""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...core.api import Advice, Severity
from ..widgets import AdviceCard, BasePage, ChoiceCard, Notice, button
from . import HOME


class HomePage(BasePage):
    page_id = HOME
    help_topic = "home"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("What do you want to do?", "Choose one of these.", parent)
        # The last internet check's own words, while the internet or head office is down.
        self.network = Notice()
        self.network.hide()
        self.content.addWidget(self.network)
        self.notice = AdviceCard()
        self.notice.hide()
        self.notice.action.connect(self.action)
        self.content.addWidget(self.notice)
        size = QSize(112, 84)
        self.setup_card = ChoiceCard(
            "Set up a new board",
            "Get a new board ready for an attendance machine.",
            "cable",
            emphasis=True,
            picture_size=size,
        )
        self.check_card = ChoiceCard(
            "Check or fix a board",
            "See what a board is doing, or change its settings.",
            "light",
            picture_size=size,
        )
        self.connect_card = ChoiceCard(
            "How to connect a board",
            "See which cable and which socket to use.",
            "usb_socket",
            picture_size=size,
        )
        for card, name in (
            (self.setup_card, "setup"),
            (self.check_card, "check"),
            (self.connect_card, "connect_help"),
        ):
            card.clicked.connect(lambda _checked=False, value=name: self.action.emit(value))
            self.content.addWidget(card)
        links = QHBoxLayout()
        self.lights_link = button("What is the light on the board doing?", "link")
        self.lights_link.clicked.connect(lambda: self.action.emit("lights"))
        links.addWidget(self.lights_link)
        links.addStretch(1)
        self.content.addLayout(links)
        self.set_primary(None)
        self.set_back(False)

    def set_notice(self, advice: Optional[Advice], recheck: bool = True) -> None:
        """What head office says about setting up boards; ``recheck`` offers Check again."""
        if advice is None:
            self.notice.hide()
            return
        self.notice.set_advice(advice, [("Check again", "recheck", "secondary")] if recheck else [])
        self.notice.show()

    def set_network(self, words: Optional[Tuple[str, str]]) -> None:
        """(title, words) from the internet check, or None while both are fine."""
        if words is None:
            self.network.hide()
            return
        self.network.set_notice(words[0], words[1], Severity.WARNING)
        self.network.show()

    def first_focus(self) -> Optional[QWidget]:
        return self.setup_card
