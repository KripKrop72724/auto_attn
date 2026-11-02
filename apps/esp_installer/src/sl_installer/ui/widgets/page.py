"""The frame every screen shares: title and one sentence at the top, the
content in a centred column, and Help, Back and the main button at the bottom."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import pictures
from .basics import button, set_kind, text

COLUMN_WIDTH = 960


class BasePage(QWidget):
    """Buttons send ``action(name)``; the controller decides what happens."""

    action = Signal(str)

    page_id = ""
    help_topic = ""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageBody")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("pageBody")
        centre = QHBoxLayout(holder)
        centre.setContentsMargins(0, 0, 0, 0)
        self.column_widget = QWidget()
        self.column_widget.setObjectName("pageColumn")
        self.column_widget.setMaximumWidth(COLUMN_WIDTH)
        self.column_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        centre.addStretch(1)
        centre.addWidget(self.column_widget, 100)
        centre.addStretch(1)
        self.column = QVBoxLayout(self.column_widget)
        self.column.setContentsMargins(40, 28, 40, 32)
        self.column.setSpacing(20)
        self.title_label = text(title, object_name="pageTitle")
        self.title_label.setAccessibleName(title)
        self.subtitle_label = text(subtitle, object_name="pageSubtitle")
        heading = QVBoxLayout()
        heading.setSpacing(6)
        heading.addWidget(self.title_label)
        heading.addWidget(self.subtitle_label)
        self.column.addLayout(heading)
        self._content = QVBoxLayout()
        self._content.setSpacing(20)
        self.column.addLayout(self._content)
        self.column.addStretch(1)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        bar = QHBoxLayout(footer)
        bar.setContentsMargins(32, 14, 32, 14)
        bar.setSpacing(12)
        # A leading space keeps the words clear of the icon; the spoken name stays "Help".
        self.help_button = button(" Help", "secondary")
        self.help_button.setAccessibleName("Help")
        self.help_button.setIcon(pictures.ui_icon("help", 20))
        self.help_button.setIconSize(QSize(20, 20))
        self.help_button.setToolTip("Pictures and tips for this screen")
        self.back_button = button("Back", "secondary")
        self.secondary_button = button("", "secondary")
        self.primary_button = button("Next", "primary")
        self.primary_button.setMinimumWidth(150)
        bar.addWidget(self.help_button)
        bar.addStretch(1)
        bar.addWidget(self.secondary_button)
        bar.addWidget(self.back_button)
        bar.addWidget(self.primary_button)
        outer.addWidget(footer)
        self.footer = footer

        self._primary_action = "next"
        self._secondary_action = ""
        self.help_button.clicked.connect(lambda: self.action.emit("help"))
        self.back_button.clicked.connect(lambda: self.action.emit("back"))
        self.primary_button.clicked.connect(lambda: self.action.emit(self._primary_action))
        self.secondary_button.clicked.connect(lambda: self.action.emit(self._secondary_action))
        self.secondary_button.hide()
        self.set_subtitle(subtitle)

    # ------------------------------------------------------------- content

    @property
    def content(self) -> QVBoxLayout:
        return self._content

    def set_title(self, title: str, subtitle: Optional[str] = None) -> None:
        self.title_label.setText(title)
        self.title_label.setAccessibleName(title)
        if subtitle is not None:
            self.set_subtitle(subtitle)

    def set_subtitle(self, subtitle: str) -> None:
        self.subtitle_label.setText(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))

    # ------------------------------------------------------------- buttons

    def set_primary(self, words: Optional[str], action: str = "next", enabled: bool = True) -> None:
        """Show the main button with ``words``; None hides it."""
        if words is None:
            self.primary_button.hide()
            return
        self.primary_button.setText(words)
        self._primary_action = action
        self.primary_button.setEnabled(enabled)
        self.primary_button.show()

    def set_primary_enabled(self, enabled: bool) -> None:
        self.primary_button.setEnabled(enabled)

    def set_secondary(
        self, words: Optional[str], action: str = "", kind: str = "secondary"
    ) -> None:
        if words is None:
            self.secondary_button.hide()
            return
        self.secondary_button.setText(words)
        self._secondary_action = action
        set_kind(self.secondary_button, kind)
        self.secondary_button.show()

    def set_back(self, visible: bool, enabled: bool = True, words: str = "Back") -> None:
        self.back_button.setText(words)
        self.back_button.setVisible(visible)
        self.back_button.setEnabled(enabled)

    @property
    def primary_action(self) -> str:
        return self._primary_action

    def show_actions(self, actions: Sequence[Tuple[str, str, str]]) -> None:
        """An outcome's buttons go in the footer, the main one at the bottom right.

        ``actions`` are (words, action name, kind); the first "primary" one becomes
        the main button and the first other one the second button.
        """
        primary = next(((words, name) for words, name, kind in actions if kind == "primary"), None)
        secondary = next(
            ((words, name, kind) for words, name, kind in actions if kind != "primary"), None
        )
        if primary:
            self.set_primary(primary[0], primary[1])
        else:
            self.set_primary(None)
        if secondary:
            self.set_secondary(secondary[0], secondary[1], secondary[2])
        else:
            self.set_secondary(None)

    # ------------------------------------------------------------- helpers

    def scroll_to_top(self) -> None:
        self.scroll.verticalScrollBar().setValue(0)

    def reveal(self, widget: QWidget) -> None:
        """Scroll so ``widget`` can be seen, once the new layout has settled."""

        def scroll() -> None:
            holder = self.scroll.widget()
            if holder is not None and holder.layout() is not None:
                holder.layout().activate()
            self.scroll.ensureWidgetVisible(widget, 40, 40)

        QTimer.singleShot(0, lambda: QTimer.singleShot(0, scroll))

    def focus_first(self) -> None:
        """Put the keyboard on the most useful control when the page opens."""
        self.order_tabs()
        target = self.first_focus()
        if target is not None and target.isVisible() and target.isEnabled():
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def order_tabs(self) -> None:
        """Tab goes through the page top to bottom, then Help, Back and the main button.

        The footer is built before each page's content, so without this the
        keyboard would visit the footer first.
        """
        tab = Qt.FocusPolicy.TabFocus.value
        focusable = [
            widget
            for widget in self.column_widget.findChildren(QWidget)
            if widget.focusPolicy().value & tab == tab
        ]
        previous = focusable[-1] if focusable else None
        for widget in (
            self.help_button,
            self.secondary_button,
            self.back_button,
            self.primary_button,
        ):
            if previous is not None:
                QWidget.setTabOrder(previous, widget)
            previous = widget

    def first_focus(self) -> Optional[QWidget]:
        if self.primary_button.isVisible() and self.primary_button.isEnabled():
            return self.primary_button
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = self.focusWidget()
            # Enter presses a focused button, but never toggles a tick box (Space does that):
            # on Confirm, Enter on "I understand" would otherwise tick and untick it (E12).
            if (
                isinstance(focused, QAbstractButton)
                and not isinstance(focused, (QCheckBox, QRadioButton))
                and focused.isVisible()
            ):
                focused.click()
                return
            if self.primary_button.isVisible() and self.primary_button.isEnabled():
                self.primary_button.click()
                return
        super().keyPressEvent(event)
