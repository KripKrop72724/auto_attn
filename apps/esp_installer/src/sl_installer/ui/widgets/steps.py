"""Where you are in the setup, and the list of install steps with progress."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QGridLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from .. import theme
from .basics import clear_layout, text
from .picture import StateIcon, tick_icon_paint


class StepIndicator(QWidget):
    """A row of numbered circles: done ones are ticked, the current one is blue."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._labels: List[str] = []
        self._current = -1
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_steps(self, labels: Sequence[str], current: int) -> None:
        self._labels = list(labels)
        self._current = current
        if 0 <= current < len(self._labels):
            self.setAccessibleName(
                f"Step {current + 1} of {len(self._labels)}: {self._labels[current]}"
            )
        elif current >= len(self._labels):
            self.setAccessibleName("Every step is done")
        self.update()

    @property
    def current_label(self) -> Optional[str]:
        if 0 <= self._current < len(self._labels):
            return self._labels[self._current]
        return None

    def sizeHint(self) -> QSize:
        return QSize(720, 64)

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._labels:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self._labels)
        margin = 24.0
        slot = (self.width() - 2 * margin) / count
        radius = 12.0
        cy = 22.0
        number_font = theme.font(10, QFont.Weight.Bold)
        label_font = theme.font(theme.BODY_PT)
        current_font = theme.font(theme.BODY_PT, QFont.Weight.Bold)
        for index in range(count - 1):
            x1 = margin + slot * index + slot / 2 + radius + 4
            x2 = margin + slot * (index + 1) + slot / 2 - radius - 4
            colour = theme.SUCCESS if index < self._current else theme.BORDER_STRONG
            painter.setPen(QPen(QColor(colour), 2))
            painter.drawLine(QPointF(x1, cy), QPointF(x2, cy))
        for index, label in enumerate(self._labels):
            cx = margin + slot * index + slot / 2
            circle = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
            if index < self._current:
                tick_icon_paint(painter, circle, done=True)
            else:
                current = index == self._current
                painter.setPen(QPen(QColor(theme.PRIMARY if current else theme.BORDER_STRONG), 2))
                painter.setBrush(QColor(theme.PRIMARY if current else theme.WHITE))
                painter.drawEllipse(circle)
                painter.setFont(number_font)
                painter.setPen(QColor(theme.WHITE if current else theme.TEXT_TERTIARY))
                painter.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(index + 1))
            painter.setFont(current_font if index == self._current else label_font)
            painter.setPen(QColor(theme.TEXT if index == self._current else theme.TEXT_SECONDARY))
            metrics = QFontMetrics(painter.font())
            shown = metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(slot - 6))
            painter.drawText(
                QRectF(cx - slot / 2, cy + radius + 4, slot, 24),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                shown,
            )
        painter.end()


class ProgressStepList(QWidget):
    """The install steps, each with a state icon, words and (when active) a bar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        self._rows: Dict[str, Tuple[StateIcon, QLabel, QLabel, QProgressBar]] = {}
        self._order: List[str] = []

    def set_steps(self, steps: Sequence[Tuple[str, str]]) -> None:
        clear_layout(self._grid)
        self._rows.clear()
        self._order = [key for key, _label in steps]
        for index, (key, label) in enumerate(steps):
            icon = StateIcon(28)
            name = text(label, "muted")
            detail = text("", "hint")
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setAccessibleName(label)
            column = QVBoxLayout()
            column.setSpacing(6)
            column.setContentsMargins(0, 3, 0, 10)
            column.addWidget(name)
            column.addWidget(bar)
            column.addWidget(detail)
            holder = QWidget()
            holder.setLayout(column)
            self._grid.addWidget(icon, index, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(holder, index, 1)
            bar.hide()
            detail.hide()
            self._rows[key] = (icon, name, detail, bar)

    def set_state(
        self, key: str, state: str, fraction: Optional[float] = None, detail: str = ""
    ) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        icon, name, detail_label, bar = row
        icon.set_state(state)
        working = state == StateIcon.WORKING
        bar.setVisible(working)
        if working:
            if fraction is None:
                bar.setRange(0, 0)  # unknown length: a moving bar
            else:
                bar.setRange(0, 1000)
                bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        detail_label.setText(detail)
        detail_label.setVisible(bool(detail))
        role = "muted" if state in (StateIcon.WAITING, StateIcon.SKIPPED) else "h3"
        if name.property("role") != role:
            name.setProperty("role", role)
            theme.repolish(name)

    def state_of(self, key: str) -> Optional[str]:
        row = self._rows.get(key)
        return row[0].state if row else None

    def keys(self) -> List[str]:
        return list(self._order)
