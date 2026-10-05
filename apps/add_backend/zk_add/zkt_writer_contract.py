"""Exact, quarantined 2.7.0 writer package and retained signed reader."""
import struct
from datetime import datetime

from sqlalchemy import select

from zk_add.zkt_bridge_contract import REPLACEMENT_BRIDGE_VERSION, signed_hil_targets

WRITER_VERSION = "2.7.0"
REQUIRED_BRIDGE_VERSION = REPLACEMENT_BRIDGE_VERSION
WRITER_MARKER = "ZONE_STORAGE_CONTRACT_V4:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:BRIDGE=2.6.17"
BRIDGE_APPLICATION = "f803361f8c3e1ed03f57814793942ebda45bbbd10cd0c1e77239602f5e6e32a1"
BRIDGE_ARTIFACT = "be23f88bfc8e2a6f7233ab2f8903c53ac9deaf1433ad39608e7f00f7e4f2bc71"


def writer_contract():
    return {"schema_version": 4, "read_format": 2, "reader_mask": 63, "write_format": 1,
            "journal_read_format": 1, "journal_write_format": 1, "journal_reader_mask": 63,
            "journal_capture": True, "delivery_authority": "ADD", "compatibility_version": REQUIRED_BRIDGE_VERSION,
            "allowed_bootstrap_versions": [REQUIRED_BRIDGE_VERSION],
            "allowed_bootstrap_images": {REQUIRED_BRIDGE_VERSION: BRIDGE_APPLICATION}}


def validate_writer_manifest(manifest):
    expected = writer_contract()
    contract = manifest.get("queue_storage")
    if (manifest.get("version") != WRITER_VERSION or manifest.get("release_id") != "zone-lite-2.7.0"
            or manifest.get("firmware_family") != "zkt" or manifest.get("project_name") != "zone_lite"
            or manifest.get("release_channel") != "EXPERIMENTAL_HIL_ONLY"
            or manifest.get("minimum_bootstrap_version") != REQUIRED_BRIDGE_VERSION
            or manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or manifest.get("hil_targets") != signed_hil_targets()
            or not isinstance(contract, dict) or contract != expected
            or any(type(contract[key]) is not type(value) for key, value in expected.items())):
        raise ValueError("Journal writer requires its exact experimental HIL storage contract.")
    return contract


def validate_writer_image(image):
    if (len(image) < 112 or image[0] != 0xE9 or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[48:80].split(b"\0", 1)[0] != WRITER_VERSION.encode()
            or image[80:112].split(b"\0", 1)[0] != b"zone_lite"
            or image.count(WRITER_MARKER.encode() + b"\0") != 1
            or image.count(b"ZONE_STORAGE_CONTRACT_V4:") != 1
            or any(b"ZONE_STORAGE_CONTRACT_V" + str(version).encode() + b":" in image for version in (1, 2, 3))):
        raise ValueError("Journal writer image lacks its exact descriptor or compiled writer marker.")


def writer_predecessor_hold(session, connector, release):
    from zk_add.ota import FirmwareRelease, FirmwareDeployment, _application_sha256, _versions_match
    from zk_add.time_utils import ensure_utc, utc_now
    if release.state != "HIL_ONLY":
        return "JOURNAL_WRITER_HIL_ONLY"
    if (not _versions_match(connector.firmware_version, REQUIRED_BRIDGE_VERSION)
            or connector.ota_image_sha256 != BRIDGE_APPLICATION
            or connector.ota_running_partition not in {"ota_0", "ota_1"}
            or not connector.ota_secure_boot or not connector.ota_rollback_enabled):
        return "JOURNAL_EXACT_BRIDGE_REQUIRED"
    bridge = session.scalar(select(FirmwareRelease).where(FirmwareRelease.version == REQUIRED_BRIDGE_VERSION))
    if (bridge is None or bridge.state != "HIL_ONLY" or bridge.revoked_at is not None
            or _application_sha256(bridge) != BRIDGE_APPLICATION or bridge.image_sha256 != BRIDGE_ARTIFACT):
        return "JOURNAL_BRIDGE_ARTIFACT_UNAVAILABLE"
    installed = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == connector.id,
        FirmwareDeployment.release_id == bridge.id).order_by(FirmwareDeployment.id.desc()).limit(1))
    if installed is None or installed.status != "SUCCEEDED":
        return "JOURNAL_BRIDGE_INSTALL_NOT_VERIFIED"
    diagnostics = connector.firmware_diagnostics or {}
    runtime = diagnostics.get("journal_runtime") or {}
    storage = diagnostics.get("storage") or {}
    now = utc_now()
    try:
        sampled = datetime.fromisoformat(diagnostics["sampled_at"].replace("Z", "+00:00"))
        fresh = (sampled.tzinfo is not None and connector.firmware_diagnostics_at is not None
            and 0 <= (now - ensure_utc(sampled)).total_seconds() <= 45
            and 0 <= (now - ensure_utc(connector.firmware_diagnostics_at)).total_seconds() <= 45)
    except (KeyError, TypeError, ValueError):
        fresh = False
    if (not fresh or not connector.boot_id or diagnostics.get("boot_id") != connector.boot_id
            or runtime.get("observed") is not True or runtime.get("reader_ready") is not True
            or runtime.get("phase") != "READY" or runtime.get("compatibility") != ""
            or storage.get("persistence_verified") is not True or storage.get("recovery_complete") is not True
            or storage.get("upgrade_ready") is not True or storage.get("durability") != "HEALTHY"
            or any(storage.get(key) for key in ("error_code", "upgrade_error", "persistence_probe_error",
                    "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))):
        return "JOURNAL_BRIDGE_READER_NOT_VERIFIED"
    return None
