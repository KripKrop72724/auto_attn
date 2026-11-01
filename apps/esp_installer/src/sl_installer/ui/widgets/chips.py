"""Status chips for the top bar: a coloured dot and a few words."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .. import theme

OK, BAD, UNKNOWN = "ok", "bad", "unknown"

_DOT = {OK: theme.SUCCESS_SOLID, BAD: theme.ERROR_SOLID, UNKNOWN: theme.TEXT_DISABLED}


class _Dot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(12, 12))
        self.colour = QColor(theme.TEXT_DISABLED)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.colour)
        painter.drawEllipse(0, 0, self.width(), self.height())
        painter.end()


class StatusChip(QFrame):
    """``set_state("ok", "Internet OK")``; the words always say the state too.

    The words are never shortened (on a narrow window the user name gives way
    instead); the tooltip adds the last check's own words, which need more room.
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chip")
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 14, 0)
        layout.setSpacing(8)
        self._dot = _Dot()
        self._label = QLabel(text)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        self.state = UNKNOWN
        self.detail = ""

    @property
    def text(self) -> str:
        return self._label.text()

    @property
    def words_fit(self) -> bool:
        """True while the whole of the words is drawn (the window gives them room)."""
        return self._label.width() >= self._label.sizeHint().width()

    def set_state(self, state: str, text: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self._dot.colour = QColor(_DOT.get(state, theme.TEXT_DISABLED))
        self._dot.update()
        self._label.setText(text)
        self.setAccessibleName(text)
        self.setAccessibleDescription(detail)
        self.setToolTip(f"{text}. {detail}" if detail else text)
