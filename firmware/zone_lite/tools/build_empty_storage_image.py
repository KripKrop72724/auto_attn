#!/usr/bin/env python3
"""Build the empty SPIFFS image a first-use Zone Lite storage partition needs.

Zone Lite never formats its storage partition: format_if_mount_failed is false
so that a mount failure can never erase queued punches. A never-used ESP32-S3
has erased flash there, which SPIFFS refuses to mount (-10025), so the device
starts in fail-safe mode with a LOCAL_FAILURE LED. The production bootstrap
therefore writes an empty, formatted SPIFFS image to the storage partition.

The image is made by ESP-IDF's own spiffsgen.py, sized from the build's own
partition table, with the SPIFFS parameters a 29 Oct 2026 bench run proved on
real boards. The build's generated sdkconfig must use exactly those
parameters; a build that differs is refused instead of being given an image
its SPIFFS cannot mount.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

STORAGE_LABEL = "storage"
STORAGE_TYPE = 0x01  # data
STORAGE_SUBTYPE = 0x82  # spiffs
# ESP-IDF v5.5.3's Kconfig defaults, which sdkconfig.defaults does not
# override; storage_repair_core.c static-asserts the magic and magic length.
SPIFFS_PARAMETERS = {
    "SPIFFS_PAGE_SIZE": 256,
    "SPIFFS_OBJ_NAME_LEN": 32,
    "SPIFFS_META_LENGTH": 4,
    "SPIFFS_USE_MAGIC": True,
    "SPIFFS_USE_MAGIC_LENGTH": True,
}
# esp_spiffs uses the flash sector as SPIFFS's logical block; no Kconfig
# option changes it.
BLOCK_SIZE = 4096


def storage_partition(table: bytes) -> tuple[int, int]:
    """Offset and size of the single SPIFFS partition labelled storage."""
    found = []
    for start in range(0, len(table) - 31, 32):
        row = table[start:start + 32]
        if row[:2] != b"\xaa\x50":
            break
        kind, subtype, offset, size = struct.unpack_from("<BBII", row, 2)
        label = row[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        if label == STORAGE_LABEL:
            found.append((kind, subtype, offset, size))
    if len(found) != 1 or found[0][:2] != (STORAGE_TYPE, STORAGE_SUBTYPE):
        raise RuntimeError("Partition table has no single data/spiffs partition named storage")
    _kind, _subtype, offset, size = found[0]
    if size <= 0 or size % BLOCK_SIZE:
        raise RuntimeError("Storage partition size is not a whole number of SPIFFS blocks")
    return offset, size


def spiffs_mismatches(sdkconfig: dict) -> list[str]:
    """SPIFFS options the build compiled with that the image would not use."""
    mismatches = []
    for key, expected in SPIFFS_PARAMETERS.items():
        actual = sdkconfig.get(key)
        if type(actual) is not type(expected) or actual != expected:
            mismatches.append(f"CONFIG_{key}={actual!r} (image is built with {expected!r})")
    return mismatches


def spiffsgen_command(spiffsgen: Path, size: int, source: Path, output: Path) -> list[str]:
    return [
        sys.executable,
        str(spiffsgen),
        hex(size),
        str(source),
        str(output),
        "--page-size",
        str(SPIFFS_PARAMETERS["SPIFFS_PAGE_SIZE"]),
        "--block-size",
        str(BLOCK_SIZE),
        "--obj-name-len",
        str(SPIFFS_PARAMETERS["SPIFFS_OBJ_NAME_LEN"]),
        "--meta-len",
        str(SPIFFS_PARAMETERS["SPIFFS_META_LENGTH"]),
        "--use-magic",
        "--use-magic-len",
    ]


def build_empty_storage_image(*, build_dir: Path, output: Path, spiffsgen: Path) -> str:
    sdkconfig = json.loads((build_dir / "config" / "sdkconfig.json").read_text(encoding="utf-8"))
    mismatches = spiffs_mismatches(sdkconfig)
    if mismatches:
        raise RuntimeError(
            "The firmware's SPIFFS configuration differs from the empty storage image: "
            + "; ".join(mismatches)
        )
    _offset, size = storage_partition(
        (build_dir / "partition_table" / "partition-table.bin").read_bytes()
    )
    if not spiffsgen.is_file():
        raise RuntimeError(f"ESP-IDF spiffsgen.py is missing at {spiffsgen}")
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="zone-lite-empty-storage-") as empty:
        subprocess.run(
            spiffsgen_command(spiffsgen, size, Path(empty), output),
            check=True,
            stdin=subprocess.DEVNULL,
        )
    image = output.read_bytes()
    if len(image) != size:
        raise RuntimeError("Empty storage image size does not match the storage partition")
    if image.count(0xFF) == len(image):
        raise RuntimeError("Empty storage image is erased flash, not a formatted SPIFFS")
    return hashlib.sha256(image).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--spiffsgen",
        type=Path,
        default=Path(os.environ.get("IDF_PATH", "")) / "components" / "spiffs" / "spiffsgen.py",
    )
    args = parser.parse_args()
    try:
        digest = build_empty_storage_image(
            build_dir=args.build_dir, output=args.output, spiffsgen=args.spiffsgen
        )
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    print(f"Empty storage image {args.output} sha256 {digest}")


if __name__ == "__main__":
    main()
