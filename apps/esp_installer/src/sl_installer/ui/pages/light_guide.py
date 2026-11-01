"""What is the light doing? Pick the rhythm first, then the colour."""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ...core.api import Severity
from .. import leds, theme
from ..widgets import Card, ChoiceCard, HotspotDetails, LedSwatch, Notice, clear_layout, text
from ..widgets import BasePage
from . import LIGHT_GUIDE

_WHITE = (255, 255, 255)


def _demo(rhythm: leds.Rhythm) -> leds.LightState:
    """A neutral light that shows only the rhythm (white, unless the rhythm has fixed colours)."""
    states = leds.states_for(rhythm.key)
    if len(states) == 1 or rhythm.key in (leds.SWAP, leds.FLASH):
        return states[0]
    return leds.LightState(
        f"DEMO_{rhythm.key}", rhythm.key, "", _WHITE, None, 0, "", (), Severity.INFO
    )


class _AnswerCard(Card):
    def __init__(self) -> None:
        super().__init__(padding=24)
        row = QHBoxLayout()
        row.setSpacing(24)
        self.swatch = LedSwatch(None, 96)
        row.addWidget(self.swatch, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(10)
        self.title = text("", "h2")
        self.meaning = text("")
        self.heading = text("What to do", "h3")
        self._steps = QVBoxLayout()
        self._steps.setSpacing(8)
        self.look_alike = Notice()
        # The board's own Wi-Fi, for the lights that mean it may be open (review E1).
        self.hotspot = HotspotDetails()
        self.hotspot.hide()
        words.addWidget(self.title)
        words.addWidget(self.meaning)
        words.addWidget(self.heading)
        words.addLayout(self._steps)
        words.addWidget(self.hotspot)
        words.addWidget(self.look_alike)
        row.addLayout(words, 1)
        self.body.addLayout(row)

    def show_state(
        self, state: leds.LightState, hotspot: Optional[str] = None, password: str = ""
    ) -> None:
        """``hotspot`` is the board's own Wi-Fi name when a board is connected."""
        rhythm = next(item for item in leds.RHYTHMS if item.key == state.rhythm)
        self.swatch.set_state(state)
        if state.rhythm == leds.OFF:
            self.title.setText("No light")
        else:
            self.title.setText(f"{state.colour}, {rhythm.title.lower()}")
        self.meaning.setText(state.meaning)
        clear_layout(self._steps)
        for index, step in enumerate(state.what_to_do, start=1):
            line = QHBoxLayout()
            line.setSpacing(12)
            if len(state.what_to_do) > 1:
                number = text(str(index), "stepNumber", wrap=False)
                number.setAlignment(Qt.AlignmentFlag.AlignCenter)
                line.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
            line.addWidget(text(step), 1)
            self._steps.addLayout(line)
        if state.key in leds.HOTSPOT_STATES:
            self.hotspot.set_hotspot(hotspot, password)
            self.hotspot.show()
        else:
            self.hotspot.hide()
        if state.key in leds.LOOK_ALIKE:
            self.look_alike.set_notice(
                "Not sure about the colour?",
                "Yellow, amber, orange and red-orange can look alike. Connect the board "
                "to this computer and choose Check or fix a board.",
                Severity.INFO,
            )
            self.look_alike.show()
        else:
            self.look_alike.hide()
        severity = state.severity
        self.setProperty("state", "done" if severity is Severity.SUCCESS else "active")
        theme.repolish(self)


class LightGuidePage(BasePage):
    page_id = LIGHT_GUIDE
    help_topic = "light_guide"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "What is the light doing?",
            "First choose how the light behaves. Then choose its colour.",
            parent,
        )
        rhythm_card = Card("1. How does the light behave?")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        self.rhythm_cards: Dict[str, ChoiceCard] = {}
        for index, rhythm in enumerate(leds.RHYTHMS):
            swatch = LedSwatch(_demo(rhythm), 40)
            card = ChoiceCard(rhythm.title, rhythm.description, leading=swatch, compact=True)
            card.setCheckable(True)
            card.clicked.connect(lambda _checked=False, key=rhythm.key: self.choose_rhythm(key))
            grid.addWidget(card, index // 2, index % 2)
            self.rhythm_cards[rhythm.key] = card
        for column in range(2):
            grid.setColumnStretch(column, 1)
        rhythm_card.body.addLayout(grid)
        self.content.addWidget(rhythm_card)

        self.colour_card = Card("2. What colour is it?")
        self._colour_grid = QGridLayout()
        self._colour_grid.setHorizontalSpacing(14)
        self._colour_grid.setVerticalSpacing(14)
        self.colour_card.body.addLayout(self._colour_grid)
        self.colour_card.hide()
        self.content.addWidget(self.colour_card)
        self.colour_cards: Dict[str, ChoiceCard] = {}

        self.answer = _AnswerCard()
        self.answer.hide()
        self.content.addWidget(self.answer)

        self.rhythm: Optional[str] = None
        self.chosen: Optional[leds.LightState] = None
        self._hotspot: Optional[str] = None
        self._password = ""
        self.set_primary(None)
        self.set_secondary(None)
        self.set_back(True)

    def set_hotspot(self, name: Optional[str], password: str) -> None:
        """The board's own Wi-Fi name (None when no board is connected) and its password."""
        self._hotspot = name
        self._password = password

    def reset(self) -> None:
        self.rhythm = None
        self.chosen = None
        for card in self.rhythm_cards.values():
            card.setChecked(False)
        self.colour_card.hide()
        self.answer.hide()
        self.set_secondary(None)

    def choose_rhythm(self, key: str) -> None:
        self.rhythm = key
        self.chosen = None
        for name, card in self.rhythm_cards.items():
            card.setChecked(name == key)
        states = leds.states_for(key)
        self.answer.hide()
        self.set_secondary("Start again", "restart")
        if len(states) == 1:
            self.colour_card.hide()
            self.choose_state(states[0].key)
            return
        clear_layout(self._colour_grid)
        self.colour_cards = {}
        columns = 3
        for index, state in enumerate(states):
            swatch = LedSwatch(state, 40)
            card = ChoiceCard(state.colour, "", leading=swatch, compact=True)
            card.setCheckable(True)
            card.clicked.connect(lambda _checked=False, value=state.key: self.choose_state(value))
            self._colour_grid.addWidget(card, index // columns, index % columns)
            self.colour_cards[state.key] = card
        for column in range(columns):
            self._colour_grid.setColumnStretch(column, 1)
        self.colour_card.show()
        self.reveal(self.colour_card)

    def choose_state(self, key: str) -> None:
        state = leds.BY_KEY[key]
        self.chosen = state
        for name, card in self.colour_cards.items():
            card.setChecked(name == key)
        self.answer.show_state(state, self._hotspot, self._password)
        self.answer.show()
        self.reveal(self.answer)

    def colour_names(self) -> List[str]:
        return [card.title_label.text() for card in self.colour_cards.values()]
