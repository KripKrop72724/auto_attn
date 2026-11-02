"""Help: pictures and tips for the screen you are on, sliding in from the right."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ...core.api import Advice, Severity
from .. import wording
from ..widgets import AdviceCard, Picture, button, clear_layout, hline, text

PANEL_WIDTH = 440


class HelpPanel(QFrame):
    closed = Signal()
    save_help_file = Signal()
    open_folder = Signal(str)

    def __init__(self, app_version: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpPanel")
        self.setAccessibleName("Help")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("helpHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(24, 16, 16, 16)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(text("Help", "muted"))
        self.title = text("", "h2")
        titles.addWidget(self.title)
        top.addLayout(titles, 1)
        self.close_button = button("Close", "secondary")
        self.close_button.clicked.connect(self.closed)
        top.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self._items = QVBoxLayout(body)
        self._items.setContentsMargins(24, 20, 24, 20)
        self._items.setSpacing(18)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        bottom = QVBoxLayout(footer)
        bottom.setContentsMargins(24, 14, 24, 14)
        bottom.setSpacing(10)
        bottom.addWidget(text("Still stuck? Save a help file and send it to head office.", "muted"))
        self.save_button = button("Save a help file", "secondary")
        self.save_button.clicked.connect(self.save_help_file)
        bottom.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.saved = AdviceCard(picture_size=QSize(0, 0))
        self.saved.hide()
        self.saved.action.connect(self._saved_action)
        bottom.addWidget(self.saved)
        self.version = text(f"App version {app_version}" if app_version else "", "caption")
        bottom.addWidget(self.version)
        layout.addWidget(footer)
        self._saved_path = ""
        self.topic = ""
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.closed)

    def show_topic(self, key: str) -> None:
        topic = wording.HELP_TOPICS.get(key) or wording.HELP_TOPICS["home"]
        self.topic = key
        self.title.setText(topic.title)
        clear_layout(self._items)
        for index, item in enumerate(topic.items):
            if index:
                self._items.addWidget(hline())
            block = QVBoxLayout()
            block.setSpacing(6)
            if item.picture:
                block.addWidget(
                    Picture(item.picture, QSize(220, 165)), 0, Qt.AlignmentFlag.AlignHCenter
                )
                block.addSpacing(6)
            block.addWidget(text(item.heading, "h3"))
            block.addWidget(text(item.text))
            self._items.addLayout(block)
        self._items.addStretch(1)
        self.saved.hide()
        self.save_button.setEnabled(True)

    def set_saving(self) -> None:
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving")

    def show_saved(self, path: str) -> None:
        self._saved_path = path
        self.save_button.setEnabled(True)
        self.save_button.setText("Save a help file")
        self.saved.set_advice(
            Advice("HELP_FILE_SAVED", "Help file saved", f"It is in {path}", (), Severity.SUCCESS),
            [("Open the folder", "open_folder", "secondary")],
        )
        self.saved.show()

    def show_save_problem(self, advice: Advice) -> None:
        self.save_button.setEnabled(True)
        self.save_button.setText("Save a help file")
        self.saved.set_advice(advice)
        self.saved.show()

    def _saved_action(self, name: str) -> None:
        if name == "open_folder" and self._saved_path:
            self.open_folder.emit(self._saved_path)
