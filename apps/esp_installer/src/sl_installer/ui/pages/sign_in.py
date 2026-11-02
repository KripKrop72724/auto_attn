"""Sign in with the ADD dashboard name and password."""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ...core.api import Advice, Severity
from ..widgets import (
    AdviceCard,
    BasePage,
    Card,
    FieldBlock,
    Notice,
    PasswordInput,
    Picture,
    Spinner,
    text,
)
from . import SIGN_IN

# Problems with the connection, not with the name or password: trying the same again
# can work, so the card offers Try again (the steps say "Click Try again"). Never for a
# wrong password: each try counts towards the pause and the account lock.
CONNECTION_CODES = frozenset(
    {
        "NO_INTERNET",
        "NO_CONNECTION",
        "SERVER_UNREACHABLE",
        "SECURE_CONNECTION_FAILED",
        "SERVER_ERROR",
        "RATE_LIMITED",
    }
)


class SignInPage(BasePage):
    page_id = SIGN_IN
    help_topic = "sign_in"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Sign in", "Use the same name and password as the attendance dashboard.", parent
        )
        # Problems go first, so they are seen without scrolling.
        self.network = Notice()
        self.network.hide()
        self.content.addWidget(self.network)
        self._network: Optional[Tuple[str, str]] = None
        self.advice = AdviceCard()
        self.advice.hide()
        self.advice.action.connect(self.action)
        self.content.addWidget(self.advice)
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(36)
        form = QVBoxLayout()
        form.setSpacing(18)
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.username.setPlaceholderText("Your dashboard user name")
        self.password = PasswordInput()
        self.password.line_edit.setMaxLength(256)
        self.username_field = FieldBlock("User name", self.username)
        self.password_field = FieldBlock("Password", self.password)
        form.addWidget(self.username_field)
        form.addWidget(self.password_field)
        busy = QHBoxLayout()
        busy.setSpacing(10)
        self.spinner = Spinner(24)
        self.busy_label = text("Signing in. Please wait.", "muted", wrap=False)
        busy.addWidget(self.spinner)
        busy.addWidget(self.busy_label)
        busy.addStretch(1)
        self.busy_row = QWidget()
        self.busy_row.setLayout(busy)
        self.busy_row.hide()
        form.addWidget(self.busy_row)
        form.addStretch(1)
        row.addLayout(form, 3)

        welcome = QVBoxLayout()
        welcome.setSpacing(10)
        welcome.addWidget(Picture("cable", QSize(240, 180)), 0, Qt.AlignmentFlag.AlignHCenter)
        welcome.addWidget(
            text("This app gets new boards ready for State Life attendance machines.", "muted")
        )
        welcome.addStretch(1)
        row.addLayout(welcome, 2)
        card.body.addLayout(row)
        self.content.addWidget(card)

        self.set_primary("Sign in", "sign_in")
        self.set_back(False)
        self.username.returnPressed.connect(self._submit_from_keyboard)
        self.password.returnPressed.connect(self._submit_from_keyboard)
        self.username.textChanged.connect(lambda _value: self.username_field.set_error(None))
        self.password.textChanged.connect(lambda _value: self.password_field.set_error(None))

    def _submit_from_keyboard(self) -> None:
        if self.primary_button.isEnabled():
            self.action.emit("sign_in")

    def credentials(self) -> Tuple[str, str]:
        return self.username.text().strip(), self.password.text()

    def check_filled(self) -> bool:
        username, password = self.credentials()
        self.username_field.set_error(None if username else "Type your user name.")
        self.password_field.set_error(None if password else "Type your password.")
        if not username:
            self.username.setFocus()
        elif not password:
            self.password.setFocus()
        return bool(username and password)

    def set_busy(self, busy: bool) -> None:
        self.busy_row.setVisible(busy)
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.set_primary("Signing in" if busy else "Sign in", "sign_in", enabled=not busy)

    def show_advice(self, advice: Optional[Advice]) -> None:
        if advice is None:
            self.advice.hide()
            self._show_network()
            return
        retry = advice.code in CONNECTION_CODES and advice.can_retry
        self.advice.set_advice(advice, [("Try again", "sign_in", "secondary")] if retry else [])
        self.advice.show()
        self._show_network()
        self.scroll_to_top()

    def set_network(self, words: Optional[Tuple[str, str]]) -> None:
        """(title, words) from the internet check, or None while both are fine."""
        self._network = words
        self._show_network()

    def _show_network(self) -> None:
        # A connection problem on the card already says it; do not say it twice.
        connection = (
            self.advice.isVisibleTo(self)
            and self.advice.advice is not None
            and self.advice.advice.code in CONNECTION_CODES
        )
        if self._network is None or connection:
            self.network.hide()
            return
        self.network.set_notice(self._network[0], self._network[1], Severity.WARNING)
        self.network.show()

    def clear_password(self) -> None:
        self.password.clear()
        self.password.set_revealed(False)

    def reset(self) -> None:
        self.set_busy(False)
        self.show_advice(None)
        self.clear_password()
        self.username_field.set_error(None)
        self.password_field.set_error(None)

    def first_focus(self) -> Optional[QWidget]:
        return self.password.line_edit if self.username.text() else self.username
