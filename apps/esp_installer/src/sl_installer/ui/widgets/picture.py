"""Pictures drawn straight from SVG, so they stay sharp at any screen scale."""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from .. import pictures, theme
from . import motion


class Picture(QWidget):
    """Shows one picture key, keeping its shape, centred in the space it gets."""

    def __init__(
        self,
        key: Optional[str] = None,
        size: QSize = QSize(240, 180),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._key: Optional[str] = None
        self._size = QSize(size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(self._size)
        self.set_key(key)

    @property
    def key(self) -> Optional[str]:
        return self._key

    def set_key(self, key: Optional[str]) -> None:
        self._key = key
        self.setAccessibleName(pictures.describe(key) or "Picture")
        self.update()

    @property
    def has_picture(self) -> bool:
        return pictures.renderer(self._key) is not None

    def set_picture_size(self, size: QSize) -> None:
        self._size = QSize(size)
        self.setFixedSize(self._size)

    def sizeHint(self) -> QSize:
        return QSize(self._size)

    def paintEvent(self, event: QPaintEvent) -> None:
        svg = pictures.renderer(self._key)
        if svg is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        view = svg.viewBoxF()
        if view.isEmpty():
            view = QRectF(0, 0, svg.defaultSize().width(), svg.defaultSize().height())
        scale = min(self.width() / view.width(), self.height() / view.height())
        width, height = view.width() * scale, view.height() * scale
        svg.render(
            painter, QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
        )
        painter.end()


class AnimatedPicture(Picture):
    """Flips between pictures, e.g. cable in / cable out, to show an action."""

    def __init__(
        self,
        frames: Sequence[str],
        size: QSize = QSize(240, 180),
        interval_ms: int = 900,
        parent: Optional[QWidget] = None,
    ) -> None:
        self._frames = list(frames)
        self._index = 0
        super().__init__(self._frames[-1] if self._frames else None, size, parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._next)

    def set_frames(self, frames: Sequence[str]) -> None:
        self._frames = list(frames)
        self._index = 0
        self.set_key(self._frames[-1] if self._frames else None)
        self._sync_timer()

    def _next(self) -> None:
        if not self._frames:
            return
        self._index = (self._index + 1) % len(self._frames)
        self.set_key(self._frames[self._index])

    def _sync_timer(self) -> None:
        running = self.isVisible() and len(self._frames) > 1 and not motion.frozen()
        if running and not self._timer.isActive():
            self._timer.start()
        elif not running:
            self._timer.stop()
            if self._frames:
                # Frozen or hidden: show the frame that explains the action.
                self.set_key(self._frames[-1])

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()


class Spinner(QWidget):
    """A small turning ring that says "working on it"."""

    def __init__(self, diameter: int = 28, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._turn)
        self.setAccessibleName("Working")

    def _turn(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not motion.frozen():
            self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(3.0, self.width() / 9)
        rect = QRectF(width, width, self.width() - 2 * width, self.height() - 2 * width)
        painter.setPen(QPen(QColor(theme.BORDER), width))
        painter.drawEllipse(rect)
        pen = QPen(QColor(theme.PRIMARY), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        angle = 45 if motion.frozen() else self._angle
        painter.drawArc(rect, int(-angle * 16), int(-100 * 16))
        painter.end()


class StateIcon(QWidget):
    """A round icon for a step: waiting, working, done, failed or skipped."""

    WAITING, WORKING, DONE, FAILED, SKIPPED = "waiting", "working", "done", "failed", "skipped"

    def __init__(self, diameter: int = 28, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._state = self.WAITING
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._turn)

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state
        if state == self.WORKING and self.isVisible() and not motion.frozen():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _turn(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_state(self._state)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = float(self.width())
        rect = QRectF(1.5, 1.5, size - 3, size - 3)
        if self._state == self.DONE:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SUCCESS))
            painter.drawEllipse(rect)
            _draw_tick(painter, size, QColor(theme.WHITE))
        elif self._state == self.FAILED:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ERROR))
            painter.drawEllipse(rect)
            pen = QPen(QColor(theme.WHITE), size / 11)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            inset = size * 0.33
            painter.drawLine(QPointF(inset, inset), QPointF(size - inset, size - inset))
            painter.drawLine(QPointF(size - inset, inset), QPointF(inset, size - inset))
        elif self._state == self.WORKING:
            width = size / 9
            ring = QRectF(width, width, size - 2 * width, size - 2 * width)
            painter.setPen(QPen(QColor(theme.ACCENT_MUTED), width))
            painter.drawEllipse(ring)
            pen = QPen(QColor(theme.PRIMARY), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            angle = 45 if motion.frozen() else self._angle
            painter.drawArc(ring, int(-angle * 16), int(-110 * 16))
        else:
            colour = QColor(theme.BORDER_STRONG if self._state == self.WAITING else theme.BORDER)
            painter.setPen(QPen(colour, size / 14))
            painter.setBrush(QColor(theme.WHITE))
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        painter.end()


def _draw_tick(painter: QPainter, size: float, colour: QColor) -> None:
    pen = QPen(colour, size / 10)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    points = [
        QPointF(0.29 * size, 0.52 * size),
        QPointF(0.44 * size, 0.67 * size),
        QPointF(0.72 * size, 0.36 * size),
    ]
    painter.drawPolyline(QPolygonF(points))


def tick_icon_paint(painter: QPainter, rect: QRectF, done: bool) -> None:
    """Shared by the step indicator: a filled circle with a tick."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.SUCCESS if done else theme.PRIMARY))
    painter.drawEllipse(rect)
    size = rect.width()
    painter.save()
    painter.translate(rect.topLeft())
    _draw_tick(painter, size, QColor(theme.WHITE))
    painter.restore()
