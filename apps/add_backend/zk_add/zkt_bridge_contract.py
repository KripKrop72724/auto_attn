"""Exact bridge packaging for experimental HIL; no writer or production grant."""
import struct

from zk_add.zkt270_scope import TARGETS

# Preserve revoked bridge package validation for audit and recovery.
# Each field repair has a new immutable identity, with no implied boot success.
BRIDGE_VERSION = "2.6.16"
REPLACEMENT_BRIDGE_VERSION = "2.6.17"
RECOVERY_BRIDGE_VERSION = "2.6.18"
STARTUP_BRIDGE_VERSION = "2.6.19"
DIAGNOSTIC_BRIDGE_VERSION = "2.6.20"
BRIDGE_VERSIONS = (BRIDGE_VERSION, REPLACEMENT_BRIDGE_VERSION, RECOVERY_BRIDGE_VERSION,
                   STARTUP_BRIDGE_VERSION, DIAGNOSTIC_BRIDGE_VERSION)
READINESS_BRIDGE_VERSIONS = (REPLACEMENT_BRIDGE_VERSION, RECOVERY_BRIDGE_VERSION,
                             STARTUP_BRIDGE_VERSION, DIAGNOSTIC_BRIDGE_VERSION)
BRIDGE_MARKER = "ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1"
PREDECESSOR_IMAGES = {
    "2.4.12": "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589",
    "2.5.2": "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
    "2.6.15": "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6",
}


def signed_hil_targets() -> list[dict]:
    return [target.identity.model_dump() for target in TARGETS]


def bridge_hil_targets(raw: object) -> list:
    """Quarantine can expose only an ordered prefix of the signed nationwide scope."""
    if (not isinstance(raw, list) or not 1 <= len(raw) <= len(TARGETS)
            or raw != signed_hil_targets()[:len(raw)]):
        raise ValueError("Journal bridge HIL scope must retain the exact nationwide prefix.")
    return [target.identity for target in TARGETS[:len(raw)]]


def bridge_marker(version: str) -> str:
    if version not in BRIDGE_VERSIONS:
        raise ValueError("Journal bridge version is unqualified.")
    return BRIDGE_MARKER + (f":VERSION={version}" if version != BRIDGE_VERSION else "")


def bridge_contract(version: str = BRIDGE_VERSION) -> dict:
    if not isinstance(version, str) or version not in BRIDGE_VERSIONS:
        raise ValueError("Journal bridge version is unqualified.")
    return {
        "allowed_bootstrap_images": dict(PREDECESSOR_IMAGES),
        "allowed_bootstrap_versions": list(PREDECESSOR_IMAGES),
        "compatibility_version": version,
        "delivery_authority": "LEGACY_UNTIL_PERSISTED_ADD_CUTOVER",
        "journal_capture": True,
        "journal_read_format": 1,
        "journal_reader_mask": 63,
        "journal_write_format": 1,
        "read_format": 2,
        "reader_mask": 63,
        "schema_version": 3,
        "write_format": 1,
    }


def validate_bridge_manifest(manifest: dict) -> dict:
    contract = manifest.get("queue_storage")
    expected = bridge_contract(manifest.get("version"))
    if (manifest.get("release_id") != f"zone-lite-{manifest['version']}"
            or manifest.get("firmware_family") != "zkt"
            or manifest.get("project_name") != "zone_lite"
            or manifest.get("release_channel") != "EXPERIMENTAL_HIL_ONLY"
            or manifest.get("minimum_bootstrap_version") != "2.4.12"
            or manifest.get("hil_targets") != signed_hil_targets()
            or not isinstance(contract, dict) or contract != expected
            or any(type(contract[key]) is not type(value) for key, value in expected.items())):
        raise ValueError("Journal bridge requires its exact experimental HIL storage contract.")
    return contract


def validate_bridge_image(image: bytes, version: str = BRIDGE_VERSION) -> None:
    """Bind manifest capabilities to the compiled role and ESP descriptor.

    The normal release loader independently verifies the signed manifest and
    entire image digest. A marker is packaging evidence, never runtime proof.
    """
    marker = bridge_marker(version)
    if (len(image) < 112 or image[0] != 0xE9
            or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[48:80].split(b"\0", 1)[0] != version.encode()
            or image[80:112].split(b"\0", 1)[0] != b"zone_lite"
            or image.count(marker.encode() + b"\0") != 1
            or image.count(b"ZONE_STORAGE_CONTRACT_V3:") != 1
            or b"ZONE_STORAGE_CONTRACT_V1:" in image or b"ZONE_STORAGE_CONTRACT_V2:" in image):
        raise ValueError("Journal bridge image lacks its exact descriptor or compiled reader/capture marker.")
