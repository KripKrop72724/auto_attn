"""The blue bar at the top of every screen, and the TEST BUILD ribbon."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...core.api import ConnectivityStatus
from .. import pictures
from .basics import ElidedLabel, button, text
from .chips import BAD, OK, UNKNOWN, StatusChip

# A long user name is cut short ("…", the whole name in its tooltip) rather than pushing
# the chips off a narrow window; the chips' words are never shortened (review E13).
USER_MAX_WIDTH = 260


class TopBar(QFrame):
    """Logo and name on the left; internet, server and sign-in on the right."""

    sign_out_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        self.setFixedHeight(68)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(12)

        self.logo = QLabel()
        self.logo.setFixedSize(40, 40)
        self.logo.setAccessibleName("State Life")
        layout.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.setContentsMargins(4, 0, 0, 0)
        self.app_name = text("State Life", object_name="appName", wrap=False)
        self.tagline = text("Device Installer", object_name="appTagline", wrap=False)
        names.addStretch(1)
        names.addWidget(self.app_name)
        names.addWidget(self.tagline)
        names.addStretch(1)
        layout.addLayout(names)
        layout.addStretch(1)

        self.internet_chip = StatusChip("Checking internet")
        self.server_chip = StatusChip("Checking head office")
        layout.addWidget(self.internet_chip)
        layout.addWidget(self.server_chip)
        layout.addSpacing(10)
        self.user_label = ElidedLabel("", object_name="userName")
        self.user_label.setMaximumWidth(USER_MAX_WIDTH)
        self.sign_out_button = button("Sign out", name="signOut")
        self.sign_out_button.clicked.connect(self.sign_out_clicked)
        layout.addWidget(self.user_label)
        layout.addWidget(self.sign_out_button)
        self.set_user(None)
        self.set_connectivity(None)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.logo.setPixmap(pictures.logo_pixmap(40, self.devicePixelRatioF()))

    def set_connectivity(self, status: Optional[ConnectivityStatus]) -> None:
        if status is None:
            self.internet_chip.set_state(UNKNOWN, "Checking internet")
            self.server_chip.set_state(UNKNOWN, "Checking head office")
            return
        # The check's own words (for example "This network needs you to sign in to it
        # first") go in both tooltips: the chips have room for a few words only.
        detail = status.detail or ""
        if status.internet:
            self.internet_chip.set_state(OK, "Internet OK", detail)
        else:
            self.internet_chip.set_state(BAD, "No internet", detail)
        if status.server:
            self.server_chip.set_state(OK, "Head office OK", detail)
        else:
            self.server_chip.set_state(BAD, "Head office not answering", detail)

    def set_user(self, user: Optional[str]) -> None:
        self.user_label.set_full_text(f"Signed in as {user}" if user else "")
        self.user_label.setVisible(bool(user))
        self.sign_out_button.setVisible(bool(user))

    def set_sign_out_enabled(self, enabled: bool) -> None:
        self.sign_out_button.setEnabled(enabled)
        self.sign_out_button.setToolTip("" if enabled else "Wait until the board is ready.")


class TestRibbon(QFrame):
    """Shown on builds without the real field settings."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("testRibbon")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 6, 24, 6)
        self.label = text(
            "TEST BUILD - for trying the app only. Do not use it to set up boards for sites.",
            wrap=False,
        )
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label, 1)
        self.setAccessibleName(self.label.text())
