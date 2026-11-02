"""A picture of the board's light that blinks exactly like the real one."""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import leds, theme
from . import motion

# What an unlit light looks like: the small white plastic dot on the board.
_UNLIT = QColor(theme.TEXT_SECONDARY)


class LedSwatch(QWidget):
    """Draws one ``leds.LightState`` on a patch of board, animated in real time."""

    def __init__(
        self,
        state: Optional[leds.LightState] = None,
        diameter: int = 64,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._diameter = diameter
        self._started = time.monotonic()
        self._fixed_ms: Optional[int] = None
        self.setFixedSize(QSize(diameter + 24, diameter + 24))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self.update)
        self._describe()

    @property
    def state(self) -> Optional[leds.LightState]:
        return self._state

    def set_state(self, state: Optional[leds.LightState]) -> None:
        self._state = state
        self._started = time.monotonic()
        self._describe()
        self._sync_timer()
        self.update()

    def show_moment(self, tick_ms: Optional[int]) -> None:
        """Hold the animation at one moment (None lets it run again)."""
        self._fixed_ms = tick_ms
        self._sync_timer()
        self.update()

    def _describe(self) -> None:
        if self._state is None:
            self.setAccessibleName("Light")
            return
        rhythm = next((item.title for item in leds.RHYTHMS if item.key == self._state.rhythm), "")
        self.setAccessibleName(f"Light: {self._state.colour}, {rhythm.lower()}")

    def _tick(self) -> int:
        if self._fixed_ms is not None:
            return self._fixed_ms
        if motion.frozen():
            # A moment where the light is clearly on: bright for breathing, lit for blinks.
            breathing = self._state is not None and self._state.rhythm == leds.BREATHING
            return 900 if breathing else 100
        return int((time.monotonic() - self._started) * 1000)

    def _animated(self) -> bool:
        return (
            self._state is not None
            and self._state.rhythm not in (leds.STEADY, leds.OFF)
            and self._fixed_ms is None
            and not motion.frozen()
        )

    def _sync_timer(self) -> None:
        if self.isVisible() and self._animated():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        board = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        painter.setPen(QPen(QColor(theme.NAVY_DARK), 1))
        painter.setBrush(QColor(theme.NAVY))
        painter.drawRoundedRect(board, 14, 14)
        centre = QPointF(self.width() / 2, self.height() / 2)
        radius = self._diameter / 2 * 0.62
        red, green, blue = (0, 0, 0)
        if self._state is not None:
            red, green, blue = leds.colour_at(self._state, self._tick())
        lit = max(red, green, blue) > 0
        if lit:
            colour = QColor(red, green, blue)
            glow = QRadialGradient(centre, self._diameter / 2)
            halo = QColor(colour)
            halo.setAlpha(150)
            glow.setColorAt(0.0, halo)
            halo.setAlpha(60)
            glow.setColorAt(0.55, halo)
            halo.setAlpha(0)
            glow.setColorAt(1.0, halo)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(centre, self._diameter / 2, self._diameter / 2)
            core = QRadialGradient(centre, radius)
            core.setColorAt(0.0, QColor(theme.WHITE))
            core.setColorAt(0.45, colour.lighter(130))
            core.setColorAt(1.0, colour)
            painter.setBrush(core)
            painter.drawEllipse(centre, radius, radius)
        else:
            painter.setPen(QPen(QColor(theme.TEXT_TERTIARY), 2))
            painter.setBrush(_UNLIT)
            painter.drawEllipse(centre, radius, radius)
        painter.end()
