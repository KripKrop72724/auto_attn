"""One-shot ZKT storage recovery releases (2.6.24-2.6.26) for the two Peshawar ESPs.

The image hands every retained blocked-identity row to ADD queue-evidence
custody, retires the local copies only after every exact receipt, records the
retired event UIDs for the rollback image, and always returns to the exact
signed 2.5.2 application. 2.6.25 also skips flash regions SPIFFS cannot read,
reporting each one to ADD, within an owner-approved 16 KiB bound. 2.6.26 also
releases the pages SPIFFS kept when it hid Peshawar-02's receipted file. All
are HIL_ONLY and never promotable. Each exact target may start independently: no
run is ever accepted as an upgrade, so the ordinary ordered-acceptance gate
cannot apply.
"""
from __future__ import annotations

import struct
from typing import Any

from zk_add.hil_scope import HilTarget, parse_hil_targets

VERSIONS = ("2.6.24", "2.6.25", "2.6.26")
RELEASE_IDS = {version: f"zone-lite-{version}" for version in VERSIONS}
BASELINE_VERSION = "2.5.2"
BASELINE_IMAGE = "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b"
MARKER = "ZONE_STORAGE_CONTRACT_V2:RECOVERY:READ=2:LANES=3F:BASE=2.5.2"
TARGETS = (
    HilTarget(connector_id="bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", mac="e0:72:a1:d7:05:c4",
              terminal_serial="CJH9211060009"),
    HilTarget(connector_id="233dac02-eb1b-4598-a876-e3a7b1ecfd54", mac="e0:72:a1:d5:08:a0",
              terminal_serial="CJH9211060002"),
)
CONTRACT = {
    "allowed_bootstrap_images": {BASELINE_VERSION: BASELINE_IMAGE},
    "allowed_bootstrap_versions": [BASELINE_VERSION],
    "read_format": 2,
    "reader_mask": 63,
    "schema_version": 2,
    "write_format": 1,
}
# Once the device selected the image for boot, the one-shot run is never offered
# again for that deployment, even when its terminal progress report was lost.
OFFERABLE_STATES = frozenset({"PENDING", "OFFERED", "DOWNLOADING", "VERIFYING"})


def recovery_hil_targets(raw: Any) -> list[HilTarget]:
    targets = parse_hil_targets(raw)
    if tuple(targets) != TARGETS:
        raise ValueError("Storage recovery requires its exact reviewed Peshawar scope.")
    return targets


def validate_recovery_manifest(manifest: dict, version: str) -> dict:
    contract = manifest.get("queue_storage")
    qualified = (
        version in RELEASE_IDS
        and manifest.get("version") == version
        and manifest.get("release_id") == RELEASE_IDS[version]
        and manifest.get("firmware_family") == "zkt"
        and manifest.get("project_name") == "zone_lite"
        and manifest.get("minimum_bootstrap_version") == BASELINE_VERSION
        and manifest.get("release_channel") == "EXPERIMENTAL_HIL_ONLY"
        and "factory_trial" not in manifest and "runtime_profile" not in manifest
        and isinstance(contract, dict) and contract == CONTRACT
        and all(type(contract[key]) is int for key in ("schema_version", "read_format", "reader_mask", "write_format"))
    )
    if not qualified:
        raise ValueError("Storage recovery manifest is missing or unqualified.")
    recovery_hil_targets(manifest.get("hil_targets"))
    return contract


def validate_recovery_image(image: bytes, version: str) -> None:
    """Bind the signed manifest to the compiled recovery role and descriptor."""
    if (version not in RELEASE_IDS or len(image) < 112 or image[0] != 0xE9
            or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[48:80].split(b"\0", 1)[0] != version.encode()
            or image[80:112].split(b"\0", 1)[0] != b"zone_lite"
            or image.count(MARKER.encode() + b"\0") != 1
            or image.count(b"ZONE_STORAGE_CONTRACT_V") != 1):
        raise ValueError("Storage recovery image lacks its exact descriptor or compiled recovery marker.")
