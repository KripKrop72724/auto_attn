"""--screenshots: every screen and state, saved as PNGs at both sizes."""

from __future__ import annotations

from PySide6.QtGui import QImage

from sl_installer.ui import screenshots
from sl_installer.ui.widgets import motion


def test_capture_all_makes_every_picture(tmp_path):
    paths = screenshots.capture_all(str(tmp_path))
    assert len(paths) == len(screenshots.SCENES) * len(screenshots.SIZES)
    assert len(set(paths)) == len(paths)
    sizes = {(size.width(), size.height()) for size in screenshots.SIZES}
    for path in paths:
        image = QImage(path)
        assert not image.isNull(), path
        assert (image.width(), image.height()) in sizes, path
        assert path.endswith(f"{image.width()}x{image.height()}.png")
    assert not motion.frozen()  # animations are back on afterwards


def test_scene_names_are_unique():
    names = [name for name, _scenario, _scene in screenshots.SCENES]
    assert len(names) == len(set(names))
