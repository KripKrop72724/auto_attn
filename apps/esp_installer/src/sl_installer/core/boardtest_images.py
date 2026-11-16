"""The board test program's three images, pinned by size and SHA-256 (like
bundles.STORAGE_EMPTY_SHA256).

The images are built from apps/esp_installer/board_test_firmware (ESP-IDF 5.5.3,
unsecured: no Secure Boot, no flash encryption, no eFuse writes) by its
build_images.py, which also writes resources/boardtest/manifest.json. CI rebuilds them
and fails when the bytes differ. The app checks every file against the pins below
before it writes anything to a board, and the self-test checks them in the installed
app, so a changed file in resources/boardtest can never reach a board.

The pins are the manifest.json values of the committed images (16 Nov 2026 build). A
rebuild that changes any byte must change them too: tests/core/test_boardtest_images.py
compares pins, files and manifest, and CI rebuilds the images from source.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MANIFEST_NAME = "manifest.json"
TEST_PROGRAM_VERSION = "1.0.0"
TEST_PROGRAM_IDF = "v5.5.3"


@dataclass(frozen=True)
class TestImage:
    """One image of the test program and where it goes on the board."""

    __test__ = False  # not a pytest test class

    name: str
    offset: int
    max_size: int              # the space the layout has for it
    size: Optional[int]        # pinned size in bytes (None: PLACEHOLDER, not pinned yet)
    sha256: Optional[str]      # pinned SHA-256 (None: PLACEHOLDER, not pinned yet)


# Flash layout of the test program (board_test_firmware/partitions.csv): bootloader at
# 0x0 up to the partition table at 0x8000, the factory app at 0x10000 (1 MiB).
BOOTLOADER = TestImage(
    "bootloader.bin", 0x0, 0x8000, size=20784,
    sha256="430a7e13bc99db069beda5a77367a439b4db848336696dda6dec4e31ca28b1e1")
PARTITION_TABLE = TestImage(
    "partition-table.bin", 0x8000, 0x1000, size=3072,
    sha256="9b046afd65142c07b04513517068a46e2308323c109e4a5a10de70fe3e91fdd7")
APP = TestImage(
    "board-test.bin", 0x10000, 0x100000, size=735072,
    sha256="4e75a8a00c6072fbe63590ae83bb16c8f5b67e4ad22bcd8d4fa7c7e16181149b")

IMAGES: Tuple[TestImage, ...] = (BOOTLOADER, PARTITION_TABLE, APP)
# Everything the images can occupy: erased again after every test (0x0 to the app's end).
WRITTEN_AREA = (0x0, APP.offset + APP.max_size)


class ImageError(ValueError):
    """A test image is missing, unpinned or not the pinned one: nothing was written."""


def pins_ready(images: Tuple[TestImage, ...] = IMAGES) -> bool:
    """True once every image has a pinned size and SHA-256 (no PLACEHOLDER left)."""
    return all(image.size is not None and image.sha256 for image in images)


def _sha256(path: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def image_problems(directory: Path, images: Tuple[TestImage, ...] = IMAGES) -> List[str]:
    """Why the test images in ``directory`` may not be used: empty when all match."""
    problems: List[str] = []
    for image in images:
        path = directory / image.name
        if image.size is None or not image.sha256:
            problems.append(f"board test image {image.name} is not pinned yet")
            continue
        try:
            size, digest = _sha256(path)
        except OSError as error:
            problems.append(f"board test image {image.name} unreadable: {error}")
            continue
        if size != image.size or digest != image.sha256 or size > image.max_size:
            problems.append(f"board test image {image.name} does not match its pinned hash")
    return problems


def verified_images(directory: Path, images: Tuple[TestImage, ...] = IMAGES
                    ) -> List[Tuple[TestImage, Path]]:
    """The images with their paths, every one checked; ImageError otherwise."""
    problems = image_problems(directory, images)
    if problems:
        raise ImageError("; ".join(problems))
    return [(image, (directory / image.name).resolve()) for image in images]


def pinned_sha256(images: Tuple[TestImage, ...] = IMAGES) -> Dict[str, str]:
    return {image.name: image.sha256 or "not pinned" for image in images}


def read_manifest(directory: Path) -> Optional[Dict[str, object]]:
    """resources/boardtest/manifest.json, or None when it is missing or not valid."""
    try:
        value = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def manifest_problems(manifest: Dict[str, object],
                      images: Tuple[TestImage, ...] = IMAGES) -> List[str]:
    """Where the build's manifest and the pins in this module disagree."""
    problems: List[str] = []
    if manifest.get("version") != TEST_PROGRAM_VERSION:
        problems.append(f"manifest version {manifest.get('version')!r} is not "
                        f"{TEST_PROGRAM_VERSION}")
    if manifest.get("idf") != TEST_PROGRAM_IDF:
        problems.append(f"manifest idf {manifest.get('idf')!r} is not {TEST_PROGRAM_IDF}")
    files = manifest.get("files")
    entries = {str(item.get("name")): item for item in files
               if isinstance(item, dict)} if isinstance(files, list) else {}
    if set(entries) != {image.name for image in images}:
        problems.append(f"manifest files {sorted(entries)} are not "
                        f"{sorted(image.name for image in images)}")
    for image in images:
        entry = entries.get(image.name)
        if entry is None:
            continue
        offset = entry.get("offset")
        offset_value = int(offset, 16) if isinstance(offset, str) else offset
        if offset_value != image.offset:
            problems.append(f"{image.name}: manifest offset {offset!r} is not {image.offset:#x}")
        if entry.get("size") != image.size:
            problems.append(f"{image.name}: manifest size {entry.get('size')!r} is not the "
                            f"pinned {image.size!r}")
        if entry.get("sha256") != image.sha256:
            problems.append(f"{image.name}: manifest sha256 is not the pinned one")
    return problems
