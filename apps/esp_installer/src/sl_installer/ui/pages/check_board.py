"""Check a board: what it is, what head office knows, what it says, what to do."""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core import api
from ...core.api import Advice, ConsoleEvent
from .. import naming, wording
from ..widgets import (
    AdviceCard,
    BasePage,
    Card,
    Collapsible,
    FactList,
    HotspotDetails,
    Spinner,
    button,
    clear_layout,
    text,
)
from ..widgets.console import ConsoleView
from . import CHECK_BOARD
from .connect import numbered_steps


class _Waiting(QWidget):
    def __init__(self, words: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(Spinner(22))
        row.addWidget(text(words, "muted"), 1)


class CheckBoardPage(BasePage):
    page_id = CHECK_BOARD
    help_topic = "check_board"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Check a board", "Here is what the app found, and what to do.", parent)
        self.problem = AdviceCard()
        self.problem.hide()
        self.problem.action.connect(self.action)
        self.content.addWidget(self.problem)

        row = QHBoxLayout()
        row.setSpacing(20)
        self.board_card = Card("The board")
        self.board_wait = _Waiting("Reading the board.")
        self.board_facts = FactList(key_width=120)
        self.board_card.body.addWidget(self.board_wait)
        self.board_card.body.addWidget(self.board_facts)
        self.board_card.body.addStretch(1)
        self.record_card = Card("At head office")
        self.record_wait = _Waiting("Looking it up at head office.")
        self.record_facts = FactList(key_width=120)
        self.record_words = text("", "muted")
        self.record_card.body.addWidget(self.record_wait)
        self.record_card.body.addWidget(self.record_facts)
        self.record_card.body.addWidget(self.record_words)
        self.record_card.body.addStretch(1)
        row.addWidget(self.board_card, 1)
        row.addWidget(self.record_card, 1)
        self.content.addLayout(row)

        # Why Fix settings is not offered for a set-up board (head office not reached, or
        # head office does not allow it for this board). Fix settings is then hidden.
        self.fix_blocked = AdviceCard()
        self.fix_blocked.hide()
        self.fix_blocked.action.connect(self.action)
        self.content.addWidget(self.fix_blocked)

        self._advice_box = QVBoxLayout()
        self._advice_box.setSpacing(14)
        self.content.addLayout(self._advice_box)
        self.advice_cards: List[AdviceCard] = []

        # The board's own Wi-Fi, so it can be joined from a phone without finishing a
        # setup first (review E1). The advice above says "shown below".
        self.hotspot_card = Card("The board's own Wi-Fi")
        self.hotspot_card.body.addWidget(
            text(
                "If the board cannot join a Wi-Fi, it opens its own Wi-Fi after 2 minutes. "
                "Join it from a phone with these details, then choose the site's Wi-Fi.",
                "muted",
            )
        )
        self.hotspot = HotspotDetails()
        self.hotspot_card.body.addWidget(self.hotspot)
        self._hotspot_steps_widget = QWidget()
        self._hotspot_steps = QVBoxLayout(self._hotspot_steps_widget)
        self._hotspot_steps.setContentsMargins(0, 4, 0, 0)
        self.hotspot_steps = Collapsible(
            "How to choose the site's Wi-Fi", self._hotspot_steps_widget
        )
        self.hotspot_card.body.addWidget(self.hotspot_steps)
        self._hotspot_name: Optional[str] = None
        self.hotspot_card.hide()
        self.content.addWidget(self.hotspot_card)

        self.console_card = Card("What the board is saying")
        self.console_hint = text(
            "To watch the board start, unplug it, wait 3 seconds and plug it back in.", "muted"
        )
        self.console_card.body.addWidget(self.console_hint)
        self.console = ConsoleView("Nothing yet.")
        self.console_card.body.addWidget(self.console)
        self.content.addWidget(self.console_card)

        links = QHBoxLayout()
        self.again_link = button("Check again", "link")
        self.again_link.clicked.connect(lambda: self.action.emit("recheck"))
        links.addWidget(self.again_link)
        links.addStretch(1)
        self.content.addLayout(links)

        self.set_primary("Fix settings", "fix", enabled=False)
        self.set_secondary("What is the light doing?", "lights")
        self.set_back(True)

    def start(self) -> None:
        self.problem.hide()
        self.fix_blocked.hide()
        self.hide_hotspot()
        self.show_advice_list(())
        self.board_wait.show()
        self.board_facts.set_rows(())
        self.record_wait.show()
        self.record_facts.set_rows(())
        self.record_words.setText("")
        self.console.clear()
        self.set_fix_available(False)

    def show_board(self, board: api.BoardInfo) -> None:
        self.board_wait.hide()
        self.board_facts.set_rows(
            (
                ("Board ID", naming.board_id(board.mac)),
                ("Type", wording.HARDWARE_WORDS.get(board.hardware, "")),
                ("Setup", wording.SECURITY_WORDS.get(board.security, "")),
            )
        )

    def show_record(self, record: api.DeviceRecord) -> None:
        self.record_wait.hide()
        if not record.known:
            self.record_facts.set_rows(())
            self.record_words.setText("Head office does not know this board yet.")
            return
        rows = []
        name = record.public_name or (
            naming.public_name(record.zone_name) if record.zone_name else ""
        )
        if name:
            rows.append(("Name", name))
        place = record.zone_id or "-"
        if record.device_id:
            place = f"{place}, device number {record.device_id}"
        rows.append(("Zone", place))
        if record.group:
            rows.append(("Group", record.group))
        if record.family:
            rows.append(("Machine", wording.family_name(record.family)))
        if record.firmware_version:
            rows.append(("Software", record.firmware_version))
        status = record.status_text or (
            "Online" if record.online else "Offline" if record.online is False else ""
        )
        if status:
            rows.append(("Status", status))
        self.record_facts.set_rows(rows)
        self.record_words.setText("")

    def show_record_lookup(self) -> None:
        """Asking head office again (the board itself is not read again)."""
        self.record_wait.show()
        self.record_facts.set_rows(())
        self.record_words.setText("")

    def show_record_problem(self, advice: Advice) -> None:
        self.record_wait.hide()
        self.record_facts.set_rows(())
        self.record_words.setText(advice.title + ". " + advice.what_happened)

    def show_advice_list(self, advices: Sequence[Advice]) -> None:
        clear_layout(self._advice_box)
        self.advice_cards = []
        for advice in list(advices)[:3]:
            card = AdviceCard(advice)
            card.action.connect(self.action)
            self._advice_box.addWidget(card)
            self.advice_cards.append(card)

    def shown_codes(self) -> List[str]:
        return [card.advice.code for card in self.advice_cards if card.advice is not None]

    def add_event(self, event: ConsoleEvent) -> None:
        self.console.add(event)

    def show_problem(self, advice: Optional[Advice], retry: bool = True) -> None:
        if advice is None:
            self.problem.hide()
            return
        self.board_wait.hide()
        self.record_wait.hide()
        self.problem.set_advice(advice, [("Try again", "recheck", "primary")] if retry else [])
        self.problem.show()
        self.reveal(self.problem)

    def set_fix_available(self, available: bool) -> None:
        self.set_primary("Fix settings", "fix", enabled=available)
        self.primary_button.setToolTip(
            "" if available else "Only a board that is set up for State Life can get new settings."
        )
        self.primary_button.setVisible(True)
        self.fix_blocked.hide()

    def show_fix_blocked(self, advice: Advice, retry: bool = False) -> None:
        """Why this set-up board cannot get new settings now; Fix settings is not offered."""
        self.fix_blocked.set_advice(advice, [("Try again", "lookup", "secondary")] if retry else [])
        self.fix_blocked.show()
        self.set_primary("Fix settings", "fix", enabled=False)
        self.primary_button.hide()

    # ------------------------------------------------------------- hotspot

    def show_hotspot(self, name: Optional[str], password: str, open_steps: bool = False) -> None:
        """The board's own Wi-Fi; ``open_steps`` when the advice is about the Wi-Fi."""
        self.hotspot.set_hotspot(name, password)
        if name != self._hotspot_name or not self._hotspot_steps.count():
            self._hotspot_name = name
            clear_layout(self._hotspot_steps)
            steps = wording.portal_steps(name or f"{naming.HOTSPOT_PREFIX}XXXX")
            self._hotspot_steps.addLayout(numbered_steps(steps, role=None))
        if open_steps:
            self.hotspot_steps.set_expanded(True)
        self.hotspot_card.show()

    def hide_hotspot(self) -> None:
        self.hotspot_card.hide()
        self.hotspot_steps.set_expanded(False)
