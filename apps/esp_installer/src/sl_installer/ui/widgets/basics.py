"""Small building blocks: text, cards, rows of facts, buttons."""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme


def text(
    value: str = "",
    role: Optional[str] = None,
    wrap: bool = True,
    selectable: bool = False,
    object_name: Optional[str] = None,
) -> QLabel:
    """A plain-text label. Engine and user text is never treated as HTML."""
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    if role:
        label.setProperty("role", role)
    if object_name:
        label.setObjectName(object_name)
    if selectable:
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
    return label


class ElidedLabel(QLabel):
    """One line of text that ends in "…" when there is no room for all of it.

    The layout may squeeze it down to a few letters, so a long user name never
    pushes the rest of the top bar off a narrow window. The full words are the
    accessible name, and the tooltip while they are cut short.
    """

    def __init__(
        self,
        value: str = "",
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        object_name: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        if object_name:
            self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._mode = mode
        self._full = ""
        self.set_full_text(value)

    def full_text(self) -> str:
        return self._full

    @property
    def elided(self) -> bool:
        return QLabel.text(self) != self._full

    def set_full_text(self, value: str) -> None:
        self._full = value or ""
        self.setAccessibleName(self._full)
        self.updateGeometry()
        self._fit()

    def sizeHint(self) -> QSize:  # the whole text, however much of it is shown now
        self.ensurePolished()
        margins = self.contentsMargins()
        width = self.fontMetrics().horizontalAdvance(self._full) + margins.left() + margins.right()
        return QSize(width + 2, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        self.ensurePolished()
        room = self.fontMetrics().horizontalAdvance("Mmmm…")
        return QSize(min(self.sizeHint().width(), room), super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        width = self.contentsRect().width()
        shown = self._full
        if width > 0 and self.fontMetrics().horizontalAdvance(self._full) > width:
            shown = self.fontMetrics().elidedText(self._full, self._mode, width)
        if QLabel.text(self) != shown:
            self.setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


def set_role(label: QLabel, role: str) -> None:
    if label.property("role") != role:
        label.setProperty("role", role)
        theme.repolish(label)


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.BORDER}; border: none;")
    return line


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class Card(QFrame):
    """A white panel with rounded corners; ``body`` is its vertical layout."""

    def __init__(
        self,
        title: Optional[str] = None,
        soft: bool = False,
        padding: int = 24,
        spacing: int = 14,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cardSoft" if soft else "card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding - 4, padding, padding)
        self.body.setSpacing(spacing)
        self.title_label: Optional[QLabel] = None
        if title:
            self.title_label = text(title, "h2")
            self.body.addWidget(self.title_label)


class FactList(QWidget):
    """Rows of "what" and "value", e.g. Board ID  AC:27:6E:A4:3D:30."""

    def __init__(
        self,
        rows: Sequence[Tuple[str, str]] = (),
        key_width: int = 210,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnMinimumWidth(0, key_width)
        self._grid.setColumnStretch(1, 1)
        self.set_rows(rows)

    def set_rows(self, rows: Iterable[Tuple[str, str]]) -> None:
        clear_layout(self._grid)
        for index, (key, value) in enumerate(rows):
            key_label = text(key, "key")
            key_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            value_label = text(value or "-", "value", selectable=True)
            value_label.setAccessibleName(f"{key}: {value}")
            self._grid.addWidget(key_label, index, 0)
            self._grid.addWidget(value_label, index, 1)

    def values(self) -> List[Tuple[str, str]]:
        rows = []
        for row in range(self._grid.rowCount()):
            key_item, value_item = (
                self._grid.itemAtPosition(row, 0),
                self._grid.itemAtPosition(row, 1),
            )
            if key_item and value_item:
                rows.append((key_item.widget().text(), value_item.widget().text()))
        return rows


def button(label: str, kind: str = "secondary", name: Optional[str] = None) -> QPushButton:
    """A big, keyboard-friendly button. ``kind`` is primary, secondary, danger, link or toggle."""
    widget = QPushButton(label)
    widget.setProperty("kind", kind)
    widget.setAutoDefault(False)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if name:
        widget.setObjectName(name)
    if kind in ("link", "toggle"):
        widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def set_kind(widget: QPushButton, kind: str) -> None:
    if widget.property("kind") != kind:
        widget.setProperty("kind", kind)
        theme.repolish(widget)


class ChoiceCard(QPushButton):
    """A big card that is one button: picture, title and one line of text."""

    def __init__(
        self,
        title: str,
        description: str,
        picture: Optional[str] = None,
        emphasis: bool = False,
        vertical: bool = False,
        picture_size: QSize = QSize(112, 84),
        leading: Optional[QWidget] = None,
        compact: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        from .picture import Picture  # local import: picture imports nothing from here

        self.setProperty("kind", "choice")
        self.setProperty("emphasis", "true" if emphasis else "false")
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        layout = (QVBoxLayout if vertical else QHBoxLayout)(self)
        margin = 12 if compact else 18
        layout.setContentsMargins(margin + 2, margin, margin + 2, margin)
        layout.setSpacing(14 if compact else 18)
        self.picture = leading
        if leading is None and picture:
            self.picture = Picture(picture, picture_size)
        if self.picture is not None:
            self.picture.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(
                self.picture,
                0,
                Qt.AlignmentFlag.AlignCenter if vertical else Qt.AlignmentFlag.AlignVCenter,
            )
        words = QVBoxLayout()
        words.setSpacing(4)
        self.title_label = text(title, "choiceTitleSmall" if compact else "choiceTitle")
        self.text_label = text(description, "choiceText")
        self.text_label.setVisible(bool(description))
        if not vertical:
            words.addStretch(1)
        for label in (self.title_label, self.text_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            if vertical:
                label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            words.addWidget(label)
        words.addStretch(1)
        layout.addLayout(words, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def sizeHint(self) -> QSize:  # the layout, not the (empty) button text, decides
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:
        return self.layout().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        return self.layout().totalHeightForWidth(width)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)


class Badge(QLabel):
    """A small rounded tag such as the Life / Health group."""

    def __init__(
        self, value: str = "", role: str = "groupLife", parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(value, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_badge(self, value: str, role: str) -> None:
        self.setText(value)
        set_role(self, role)


class Clickable(QFrame):
    """A frame that reports clicks (used for the help overlay's dark backdrop)."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        self.clicked.emit()
        event.accept()
