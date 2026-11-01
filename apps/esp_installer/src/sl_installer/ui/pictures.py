"""Finds the SVG pictures and the State Life logo shipped in resources/pictures.

Advice cards name their picture with a key (Advice.illustration). The keys
the engine may use are listed in ILLUSTRATIONS; any other key, or a missing
file, simply shows no picture.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

PICTURES_DIR = Path(__file__).resolve().parent.parent / "resources" / "pictures"
LOGO_FILE = PICTURES_DIR / "state-life-logo.png"

# Keys an Advice may name, and what each picture shows.
ILLUSTRATIONS: Dict[str, str] = {
    "unplug": "Pull the cable out of the board.",
    "replug": "Push the cable back into the socket marked USB.",
    "usb_socket": "The board's two sockets. Use the one marked USB, not COM.",
    "boot_button": "Hold the BOOT button, tap the RST button, then let go of BOOT.",
    "cable": "A computer connected by a USB cable to the socket marked USB on the board.",
    "faulty_board": "A faulty board with a FAULTY label on it.",
    "hotspot": "A phone joining the board's own Wi-Fi.",
    "wait": "Wait 3 seconds.",
    # Extra pictures the screens use; the engine may name them too.
    "cable_in": "The cable plugged into the socket marked USB.",
    "light": "The small light on the board.",
    "success": "The board is ready.",
    "machine_zkt": "A ZKTeco attendance machine.",
    "machine_hik": "A Hikvision attendance machine.",
    "no_internet": "No internet.",
    "head_office": "The head office server.",
}

_renderers: Dict[str, Optional[QSvgRenderer]] = {}


def picture_path(key: Optional[str]) -> Optional[Path]:
    if not key or "/" in key or "\\" in key or key.startswith("."):
        return None
    path = PICTURES_DIR / f"{key}.svg"
    return path if path.is_file() else None


def renderer(key: Optional[str]) -> Optional[QSvgRenderer]:
    """A cached renderer for a picture key, or None when there is no such picture."""
    if not key:
        return None
    if key not in _renderers:
        path = picture_path(key)
        loaded = QSvgRenderer(str(path)) if path else None
        if loaded is not None and not loaded.isValid():
            log.warning("picture %s could not be read", key)
            loaded = None
        _renderers[key] = loaded
    return _renderers[key]


def describe(key: Optional[str]) -> str:
    return ILLUSTRATIONS.get(key or "", "")


def ui_icon_path(name: str) -> str:
    """Absolute path of a small UI icon, written with forward slashes for style sheets."""
    return (PICTURES_DIR / "ui" / f"{name}.svg").as_posix()


def ui_icon(name: str, size: int = 24) -> QIcon:
    """A crisp icon at 1x and 2x drawn from resources/pictures/ui/<name>.svg."""
    icon = QIcon()
    path = PICTURES_DIR / "ui" / f"{name}.svg"
    svg = QSvgRenderer(str(path))
    if not svg.isValid():
        return icon
    for scale in (1, 2, 3):
        pixmap = QPixmap(QSize(size * scale, size * scale))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        svg.render(painter, QRectF(0, 0, size * scale, size * scale))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap)
    return icon


def logo_pixmap(size: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    source = QPixmap(str(LOGO_FILE))
    if source.isNull():
        return source
    target = int(round(size * max(device_pixel_ratio, 1.0)))
    scaled = source.scaled(
        target,
        target,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(max(device_pixel_ratio, 1.0))
    return scaled


def app_icon() -> QIcon:
    icon = QIcon()
    source = QPixmap(str(LOGO_FILE))
    if source.isNull():
        return icon
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(
            source.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
    return icon
