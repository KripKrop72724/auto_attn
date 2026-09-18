"""The signing boundary must bind binary identity, not just a filename."""
import importlib.util
from pathlib import Path
import struct

import pytest

SPEC = importlib.util.spec_from_file_location(
    "firmware_identity", Path(__file__).parents[2] / "scripts/check_firmware_identity.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_signer_rejects_renamed_cross_family_and_wrong_version(tmp_path):
    descriptor = bytearray(112)
    descriptor[0] = 0xE9
    struct.pack_into("<I", descriptor, 32, 0xABCD5432)
    descriptor[48:53] = b"3.0.0"
    descriptor[80:99] = b"zone_lite_hikvision\0"
    image = tmp_path / "zone_lite.bin"
    image.write_bytes(descriptor)
    MODULE.verify(image, "hikvision", "3.0.0")
    with pytest.raises(ValueError, match="family"):
        MODULE.verify(image, "zkt", "3.0.0")
    with pytest.raises(ValueError, match="version"):
        MODULE.verify(image, "hikvision", "3.0.1")
    image.write_bytes(descriptor[:111])
    with pytest.raises(ValueError, match="descriptor"):
        MODULE.verify(image, "hikvision")
