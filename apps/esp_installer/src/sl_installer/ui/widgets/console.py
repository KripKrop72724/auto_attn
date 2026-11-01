"""What the board says while it starts, in plain words (and its own words on request)."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...core.api import ConsoleEvent, Severity
from .. import theme, wording
from .basics import clear_layout, text
from .fields import Collapsible

_DOTS = {
    Severity.INFO: theme.PRIMARY,
    Severity.SUCCESS: theme.SUCCESS_SOLID,
    Severity.WARNING: theme.WARNING_SOLID,
    Severity.ERROR: theme.ERROR_SOLID,
}


class _Dot(QWidget):
    def __init__(self, severity: Severity, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(12, 12))
        self._colour = QColor(_DOTS.get(severity, theme.PRIMARY))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colour)
        painter.drawEllipse(1, 1, 10, 10)
        painter.end()


class ConsoleView(QFrame):
    """Plain-language lines, newest at the bottom; the raw lines sit behind a toggle."""

    MAX_LINES = 12
    MAX_RAW = 60

    def __init__(self, empty_words: str = "Nothing yet.", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("console")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        self._lines = QVBoxLayout()
        self._lines.setSpacing(8)
        layout.addLayout(self._lines)
        self._empty_words = empty_words
        self._raw: List[str] = []
        self._shown: List[str] = []
        self.raw_label = QLabel("")
        self.raw_label.setTextFormat(Qt.TextFormat.PlainText)
        self.raw_label.setWordWrap(True)
        self.raw_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "monospace"])
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSizeF(10)
        self.raw_label.setFont(mono)
        self.raw = Collapsible("Show the board's own words", self.raw_label)
        layout.addWidget(self.raw)
        self.clear()

    def clear(self) -> None:
        clear_layout(self._lines)
        self._raw = []
        self._shown = []
        self._lines.addWidget(text(self._empty_words, "muted"))
        self.raw_label.setText("")
        self.raw.setVisible(False)

    def add(self, event: ConsoleEvent) -> None:
        if event.raw:
            self._raw.append(event.raw)
            self._raw = self._raw[-self.MAX_RAW :]
            self.raw_label.setText("\n".join(self._raw))
            self.raw.setVisible(True)
        meaning = wording.console_words(event)
        if meaning is None:
            return
        words, severity = meaning
        if self._shown and self._shown[-1] == words:
            return  # the board repeats itself (e.g. Wi-Fi retries); one line is enough
        if not self._shown:
            clear_layout(self._lines)
        self._shown.append(words)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(_Dot(severity), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(text(words), 1)
        self._lines.addLayout(row)
        while self._lines.count() > self.MAX_LINES:
            item = self._lines.takeAt(0)
            if item.layout() is not None:
                clear_layout(item.layout())
            elif item.widget() is not None:
                item.widget().deleteLater()
        self.setAccessibleDescription(" ".join(self._shown[-3:]))

    @property
    def shown_lines(self) -> List[str]:
        return list(self._shown)
