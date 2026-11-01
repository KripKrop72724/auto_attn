"""Form pieces: a labelled field with hint and error, a password box with an
eye, and a section that opens and closes."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .. import pictures, theme
from .basics import button, text


def set_invalid(editor: QWidget, invalid: bool) -> None:
    if bool(editor.property("invalid") == "true") != invalid:
        editor.setProperty("invalid", "true" if invalid else "false")
        theme.repolish(editor)


class PasswordInput(QWidget):
    """A password box and an eye button that shows or hides it."""

    textChanged = Signal(str)
    returnPressed = Signal()

    def __init__(self, revealed: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.line_edit = QLineEdit()
        self.line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.eye = QToolButton()
        self.eye.setObjectName("eye")
        self.eye.setCheckable(True)
        self.eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.eye.setIconSize(QSize(22, 22))
        self.eye.toggled.connect(self.set_revealed)
        layout.addWidget(self.line_edit, 1)
        layout.addWidget(self.eye)
        self.line_edit.textChanged.connect(self.textChanged)
        self.line_edit.returnPressed.connect(self.returnPressed)
        self.setFocusProxy(self.line_edit)
        self.set_revealed(revealed)

    def set_revealed(self, revealed: bool) -> None:
        self.line_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password
        )
        words = "Hide the password" if revealed else "Show the password"
        self.eye.blockSignals(True)
        self.eye.setChecked(revealed)
        self.eye.blockSignals(False)
        self.eye.setIcon(pictures.ui_icon("eye-off" if revealed else "eye", 22))
        self.eye.setToolTip(words)
        self.eye.setAccessibleName(words)

    @property
    def revealed(self) -> bool:
        return self.line_edit.echoMode() == QLineEdit.EchoMode.Normal

    def text(self) -> str:
        return self.line_edit.text()

    def setText(self, value: str) -> None:  # noqa: N802 (Qt-style name)
        self.line_edit.setText(value)

    def clear(self) -> None:
        self.line_edit.clear()

    def setAccessibleName(self, name: str) -> None:  # noqa: N802
        super().setAccessibleName(name)
        self.line_edit.setAccessibleName(name)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self.line_edit.setEnabled(enabled)
        self.eye.setEnabled(enabled)


class FieldBlock(QWidget):
    """Label, the editor, a hint under it, and an error line when needed."""

    def __init__(
        self,
        label: str,
        editor: QWidget,
        hint: str = "",
        target: Optional[QWidget] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.label = text(label, "fieldLabel")
        self.editor = editor
        if target is None:
            target = editor.line_edit if isinstance(editor, PasswordInput) else editor
        self._target = target
        self.label.setBuddy(target)
        self.hint = text(hint, "hint")
        self.hint.setVisible(bool(hint))
        self.error = text("", "error")
        self.error.hide()
        layout.addWidget(self.label)
        layout.addWidget(editor)
        layout.addWidget(self.hint)
        layout.addWidget(self.error)
        target.setAccessibleName(label)
        if hint:
            target.setAccessibleDescription(hint)

    @property
    def focus_target(self) -> QWidget:
        return self._target

    def set_error(self, message: Optional[str]) -> None:
        self.error.setText(message or "")
        self.error.setVisible(bool(message))
        set_invalid(self.focus_target, bool(message))
        if message:
            self.focus_target.setAccessibleDescription(message)
        else:
            self.focus_target.setAccessibleDescription(self.hint.text())

    @property
    def has_error(self) -> bool:
        return self.error.isVisible() or bool(self.error.text())

    def set_hint(self, hint: str) -> None:
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))


class Collapsible(QWidget):
    """A heading button that opens and closes the content under it."""

    toggled = Signal(bool)

    def __init__(
        self, title: str, content: QWidget, expanded: bool = False, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._title = title
        self.header = button(title, "toggle")
        self.header.setCheckable(True)
        self.header.setIconSize(QSize(20, 20))
        self.header.toggled.connect(self.set_expanded)
        self.content = content
        layout.addWidget(self.header, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(content)
        self.set_expanded(expanded)

    @property
    def expanded(self) -> bool:
        return self.content.isVisibleTo(self)

    def set_expanded(self, expanded: bool) -> None:
        self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(False)
        self.header.setIcon(pictures.ui_icon("chevron-down" if expanded else "chevron-right", 20))
        self.header.setAccessibleName(f"{self._title} ({'open' if expanded else 'closed'})")
        self.content.setVisible(expanded)
        self.toggled.emit(expanded)


class _UpperCase(QValidator):
    def validate(self, value: str, position: int):  # noqa: D102 (Qt override)
        return QValidator.State.Acceptable, value.upper(), position


class UpperCaseEdit(QLineEdit):
    """A line edit that turns what you type or paste into capitals (ZONE ID)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._validator = _UpperCase(self)
        self.setValidator(self._validator)
