"""Starts the window. ``run_gui`` is the GUI entry used by __main__."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from ..core import api
from . import pictures, theme
from .window import MIN_HEIGHT, MIN_WIDTH, TITLE, MainWindow

PREFERRED_SIZE = QSize(1280, 800)


def ensure_app(argv: Optional[Sequence[str]] = None) -> QApplication:
    """The one QApplication, created with high-DPI settings and the State Life theme."""
    app = QApplication.instance()
    if app is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(list(argv) if argv is not None else sys.argv[:1])
    app.setApplicationName(TITLE)
    app.setOrganizationName("State Life")
    app.setWindowIcon(pictures.app_icon())
    theme.apply(app)
    return app


def fitted_size(app: QApplication) -> QSize:
    """1280 x 800 where it fits; smaller laptop screens get most of the screen."""
    screen = app.primaryScreen()
    if screen is None:
        return QSize(PREFERRED_SIZE)
    room = screen.availableGeometry().size()
    width = max(MIN_WIDTH, min(PREFERRED_SIZE.width(), int(room.width() * 0.94)))
    height = max(MIN_HEIGHT, min(PREFERRED_SIZE.height(), int(room.height() * 0.94)))
    return QSize(width, height)


def run_gui(services: api.Services) -> int:
    app = ensure_app()
    window = MainWindow(services)
    window.resize(fitted_size(app))
    window.show()
    window.controller.start()
    code = app.exec()
    window.controller.shutdown()
    # Workers stop at their next cancel check; give them a moment to end cleanly.
    window.controller.runner.wait(3000)
    return int(code)
