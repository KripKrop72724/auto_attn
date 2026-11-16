"""The board test program's images: the pins in core/boardtest_images.py, the committed
files in resources/boardtest and the build's manifest.json must all agree."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from sl_installer.config import load_config
from sl_installer.core import boardtest_images
from sl_installer.core.boardtest_images import (IMAGES, ImageError, image_problems,
                                                manifest_problems, pins_ready, read_manifest,
                                                verified_images)

from . import support

COMMITTED = load_config().board_test_dir


def test_the_layout_is_the_test_programs_own():
    assert [(image.name, image.offset, image.max_size) for image in IMAGES] == [
        ("bootloader.bin", 0x0, 0x8000), ("partition-table.bin", 0x8000, 0x1000),
        ("board-test.bin", 0x10000, 0x100000)]
    assert boardtest_images.WRITTEN_AREA == (0x0, 0x110000)


@pytest.mark.skipif(not (COMMITTED / boardtest_images.MANIFEST_NAME).is_file(),
                    reason="resources/boardtest is not committed yet (owner A builds it)")
def test_the_pins_match_the_committed_images_and_their_manifest():
    """The pins in boardtest_images are the committed images and their manifest."""
    manifest = read_manifest(COMMITTED)
    assert manifest is not None
    assert pins_ready(), "fill boardtest_images pins from resources/boardtest/manifest.json"
    assert manifest_problems(manifest) == []
    assert image_problems(COMMITTED) == []
    for image in IMAGES:
        data = (COMMITTED / image.name).read_bytes()
        assert len(data) == image.size <= image.max_size
        assert hashlib.sha256(data).hexdigest() == image.sha256


def test_images_that_match_their_pins_are_used(tmp_path):
    folder, pinned = support.make_test_images(tmp_path)
    assert image_problems(folder, pinned) == []
    found = verified_images(folder, pinned)
    assert [path.name for _image, path in found] == [image.name for image in pinned]
    assert all(path.is_absolute() for _image, path in found)


def test_a_changed_missing_or_unpinned_image_is_refused(tmp_path):
    folder, pinned = support.make_test_images(tmp_path)
    (folder / "partition-table.bin").write_bytes(b"\xff" * 0xC00)
    assert image_problems(folder, pinned) == [
        "board test image partition-table.bin does not match its pinned hash"]
    (folder / "board-test.bin").unlink()
    assert any("board-test.bin unreadable" in p for p in image_problems(folder, pinned))
    with pytest.raises(ImageError):
        verified_images(folder, pinned)
    unpinned = (replace(pinned[0], sha256=None),) + pinned[1:]
    assert not pins_ready(unpinned)
    assert "bootloader.bin is not pinned yet" in image_problems(folder, unpinned)[0]


def test_an_image_too_big_for_its_place_is_refused(tmp_path):
    folder, pinned = support.make_test_images(tmp_path, sizes=(0x9000, 0xC00, 0x1000))
    assert image_problems(folder, pinned) == [
        "board test image bootloader.bin does not match its pinned hash"]


def test_the_manifest_must_agree_with_the_pins(tmp_path):
    folder, pinned = support.make_test_images(tmp_path)
    files = [{"name": image.name, "offset": hex(image.offset), "size": image.size,
              "sha256": image.sha256} for image in pinned]
    manifest = {"version": "1.0.0", "idf": "v5.5.3", "files": files}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert read_manifest(folder) == manifest
    assert manifest_problems(manifest, pinned) == []
    wrong = dict(manifest, files=[dict(files[0], sha256="0" * 64)] + files[1:])
    assert manifest_problems(wrong, pinned) == [
        "bootloader.bin: manifest sha256 is not the pinned one"]
    assert manifest_problems(dict(manifest, idf="v5.4"), pinned)
    assert manifest_problems(dict(manifest, files=files[:2]), pinned)
    moved = dict(manifest, files=files[:2] + [dict(files[2], offset="0x20000")])
    assert any("offset" in p for p in manifest_problems(moved, pinned))
    (folder / "manifest.json").write_text("not json", encoding="utf-8")
    assert read_manifest(folder) is None
