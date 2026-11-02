"""Advice cards: what happened, then what to do, with a picture.

Every problem the app shows goes through ``AdviceCard`` so it always looks
and reads the same way. The code is shown small, for head office only.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.api import Advice, Severity
from .. import pictures, theme
from .basics import button, clear_layout, text
from .picture import Picture

ActionSpec = Tuple[str, str, str]  # (button words, action name, button kind)

_ICONS = {
    Severity.INFO: "sev-info",
    Severity.SUCCESS: "sev-success",
    Severity.WARNING: "sev-warning",
    Severity.ERROR: "sev-error",
}

_WHAT_TO_DO = {Severity.SUCCESS: "Next", Severity.INFO: "What to do"}


def severity_of(advice: Advice) -> Severity:
    """The advice's severity, even when the engine passed a plain string."""
    try:
        return Severity(advice.severity)
    except ValueError:
        return Severity.ERROR


class _SeverityIcon(QLabel):
    def __init__(
        self, severity: Severity, size: int = 36, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.set_severity(severity)

    def set_severity(self, severity: Severity) -> None:
        icon = pictures.ui_icon(_ICONS.get(severity, "sev-info"), self.width())
        self.setPixmap(icon.pixmap(QSize(self.width(), self.height()), self.devicePixelRatioF()))
        self.setAccessibleName(
            {
                Severity.INFO: "Information",
                Severity.SUCCESS: "Done",
                Severity.WARNING: "Warning",
                Severity.ERROR: "Problem",
            }[severity]
        )


class AdviceCard(QFrame):
    """Shows one Advice; buttons send ``action(name)``."""

    action = Signal(str)

    def __init__(
        self,
        advice: Optional[Advice] = None,
        actions: Sequence[ActionSpec] = (),
        picture_size: QSize = QSize(176, 132),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("advice")
        self.advice: Optional[Advice] = None
        self._buttons: List[QPushButton] = []
        outer = QHBoxLayout(self)
        outer.setContentsMargins(22, 20, 24, 20)
        outer.setSpacing(22)
        self._picture = Picture(None, picture_size)
        self._icon = _SeverityIcon(Severity.INFO)
        side = QVBoxLayout()
        side.setContentsMargins(0, 2, 0, 0)
        side.addWidget(self._picture, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        side.addStretch(1)
        outer.addLayout(side)

        words = QVBoxLayout()
        words.setSpacing(10)
        self.title = text("", object_name="adviceTitle")
        self.what_happened = text("")
        self.steps_heading = text("What to do", "h3")
        self._steps = QVBoxLayout()
        self._steps.setSpacing(8)
        self._buttons_row = QHBoxLayout()
        self._buttons_row.setSpacing(12)
        self.reference = text("", "caption", wrap=False, selectable=True)
        words.addWidget(self.title)
        words.addWidget(self.what_happened)
        words.addSpacing(2)
        words.addWidget(self.steps_heading)
        words.addLayout(self._steps)
        words.addStretch(1)
        footer = QHBoxLayout()
        footer.addLayout(self._buttons_row)
        footer.addStretch(1)
        footer.addWidget(self.reference, 0, Qt.AlignmentFlag.AlignBottom)
        words.addLayout(footer)
        outer.addLayout(words, 1)
        if advice is not None:
            self.set_advice(advice, actions)

    # ------------------------------------------------------------------ api

    def set_advice(self, advice: Advice, actions: Sequence[ActionSpec] = ()) -> None:
        self.advice = advice
        severity = severity_of(advice)
        self.setProperty("severity", severity.value)
        theme.repolish(self)
        for child in self.findChildren(QLabel):
            theme.repolish(child)
        self.title.setText(advice.title)
        self.what_happened.setText(advice.what_happened)
        self.what_happened.setVisible(bool(advice.what_happened))
        has_picture = pictures.renderer(advice.illustration) is not None
        self._picture.set_key(advice.illustration if has_picture else None)
        self._picture.setVisible(has_picture)
        self._icon.set_severity(severity)
        self._icon.setVisible(not has_picture)
        clear_layout(self._steps)
        steps = tuple(advice.what_to_do or ())
        self.steps_heading.setText(_WHAT_TO_DO.get(severity, "What to do"))
        self.steps_heading.setVisible(bool(steps))
        for index, step in enumerate(steps, start=1):
            row = QHBoxLayout()
            row.setSpacing(12)
            if len(steps) > 1:
                number = text(str(index), "bullet", wrap=False)
                number.setAlignment(Qt.AlignmentFlag.AlignCenter)
                row.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text(step), 1)
            self._steps.addLayout(row)
        # The code is for head office; only problems need one.
        show_code = bool(advice.code) and severity in (Severity.WARNING, Severity.ERROR)
        self.reference.setText(f"Reference: {advice.code}" if show_code else "")
        self.reference.setVisible(show_code)
        self.set_actions(actions)
        self.setAccessibleName(advice.title)
        self.setAccessibleDescription(" ".join((advice.what_happened,) + steps))

    def set_actions(self, actions: Sequence[ActionSpec]) -> None:
        clear_layout(self._buttons_row)
        self._buttons = []
        for words, name, kind in actions:
            widget = button(words, kind)
            widget.clicked.connect(lambda _checked=False, value=name: self.action.emit(value))
            self._buttons_row.addWidget(widget)
            self._buttons.append(widget)

    def button_for(self, words: str) -> Optional[QPushButton]:
        for widget in self._buttons:
            if widget.text() == words:
                return widget
        return None

    @property
    def buttons(self) -> List[QPushButton]:
        return list(self._buttons)

    def first_button(self) -> Optional[QPushButton]:
        return self._buttons[0] if self._buttons else None


class Notice(QFrame):
    """A one-line message with an icon, e.g. "Keep the cable plugged in"."""

    def __init__(
        self,
        title: str = "",
        body: str = "",
        severity: Severity = Severity.INFO,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("advice")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(14)
        self._icon = _SeverityIcon(severity, 28)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.title = text(title, object_name="adviceTitle")
        self.body = text(body)
        words.addWidget(self.title)
        words.addWidget(self.body)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(words, 1)
        self.set_notice(title, body, severity)

    def set_notice(self, title: str, body: str = "", severity: Severity = Severity.INFO) -> None:
        self.setProperty("severity", severity.value)
        theme.repolish(self)
        theme.repolish(self.title)
        self._icon.set_severity(severity)
        self.title.setText(title)
        self.title.setVisible(bool(title))
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.setAccessibleName(title or body)
