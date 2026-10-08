"""Exact, quarantined 2.7.0 writer package and retained signed reader."""
import struct
from datetime import datetime

from sqlalchemy import select

from zk_add.zkt_bridge_contract import REPLACEMENT_BRIDGE_VERSION, signed_hil_targets
from zk_add.zkt_reader_matrix import canonical, load_matrix, matrix_marker, minimum_reader_version, selected_reader, writer_matrix_contract

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
    contract = manifest.get("queue_storage")
    matrix = isinstance(contract, dict) and contract.get("schema_version") == 5
    expected = writer_matrix_contract() if matrix else writer_contract()
    minimum = minimum_reader_version(expected["reader_matrix"]) if matrix else REQUIRED_BRIDGE_VERSION
    if (manifest.get("version") != WRITER_VERSION or manifest.get("release_id") != "zone-lite-2.7.0"
            or manifest.get("firmware_family") != "zkt" or manifest.get("project_name") != "zone_lite"
            or manifest.get("release_channel") != "EXPERIMENTAL_HIL_ONLY"
            or manifest.get("minimum_bootstrap_version") != minimum
            or manifest.get("runtime_profile") != "ZKT_JOURNAL_V1"
            or manifest.get("hil_targets") != signed_hil_targets()
            or not isinstance(contract, dict) or canonical(contract) != canonical(expected)
            or any(type(contract[key]) is not type(value) for key, value in expected.items())):
        raise ValueError("Journal writer requires its exact experimental HIL storage contract.")
    return contract


def validate_writer_image(image, manifest=None):
    matrix = b"ZONE_STORAGE_CONTRACT_V5:" in image
    marker = matrix_marker(load_matrix(require_pinned=True)) if matrix else WRITER_MARKER
    if manifest is not None:
        contract = validate_writer_manifest(manifest)
        if matrix != (contract["schema_version"] == 5):
            raise ValueError("Writer image and manifest use different reader contracts.")
    if (len(image) < 112 or image[0] != 0xE9 or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[48:80].split(b"\0", 1)[0] != WRITER_VERSION.encode()
            or image[80:112].split(b"\0", 1)[0] != b"zone_lite"
            or image.count(marker.encode() + b"\0") != 1
            or image.count(b"ZONE_STORAGE_CONTRACT_V5:" if matrix else b"ZONE_STORAGE_CONTRACT_V4:") != 1
            or any(b"ZONE_STORAGE_CONTRACT_V" + str(version).encode() + b":" in image
                   for version in ((1, 2, 3, 4) if matrix else (1, 2, 3, 5)))):
        raise ValueError("Journal writer image lacks its exact descriptor or compiled writer marker.")


def reader_for_writer(release, connector):
    contract = validate_writer_manifest(release.manifest or {})
    if contract["schema_version"] != 5:
        return None
    version = (connector.firmware_version or "").removeprefix("zone-lite-")
    return selected_reader(contract, version, connector.ota_image_sha256)


def reader_release_matches(release, reader):
    from zk_add.ota import _application_sha256
    return (release is not None and release.release_id == reader["release_id"]
            and release.version == reader["version"] and release.git_sha == reader["source_sha"]
            and release.signing_key_id == reader["signing_key_id"]
            and release.image_sha256 == reader["artifact_sha256"]
            and _application_sha256(release) == reader["application_sha256"]
            and release.state == "HIL_ONLY" and release.revoked_at is None)


def qualified_bridge_hold(session, connector, reader=None):
    """An installed version claim cannot substitute for this target's stored verdict."""
    from zk_add.bridge_observation import EVENTS, ready_event_matches
    from zk_add.hil_runs import _release_identity
    from zk_add.hil_scope import target_matches
    from zk_add.ota import FirmwareCampaign, FirmwareRelease, FirmwareDeployment, FirmwareEvent, FirmwareHilRun
    from zk_add.time_utils import ensure_utc, utc_now
    from zk_add.zkt270_scope import BY_ID
    from zk_add.models import Connector, ZKTDevice
    connector = session.get(Connector, connector.id, populate_existing=True)
    if connector is None:
        return "JOURNAL_BRIDGE_EXACT_TARGET_REQUIRED"
    terminal = session.scalar(select(ZKTDevice).where(ZKTDevice.connector_id == connector.id)
        .execution_options(populate_existing=True))
    if terminal is None or connector.zkt_device is not terminal:
        return "JOURNAL_BRIDGE_EXACT_TARGET_REQUIRED"
    target = BY_ID.get(connector.connector_id)
    if target is None or not target_matches(target.identity, connector):
        return "JOURNAL_BRIDGE_EXACT_TARGET_REQUIRED"
    if reader is None:
        try:
            contract = writer_matrix_contract()
        except ValueError:
            return "JOURNAL_READER_MATRIX_BLOCKED"
        reader = selected_reader(contract, (connector.firmware_version or "").removeprefix("zone-lite-"),
                                 connector.ota_image_sha256)
        if reader is None:
            return "JOURNAL_EXACT_BRIDGE_REQUIRED"
    bridge = session.scalar(select(FirmwareRelease).where(FirmwareRelease.version == reader["version"])
        .execution_options(populate_existing=True))
    if bridge is None:
        return "JOURNAL_BRIDGE_ARTIFACT_MISSING"
    if not reader_release_matches(bridge, reader):
        return "JOURNAL_BRIDGE_ARTIFACT_UNAVAILABLE"
    installed = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == connector.id,
        FirmwareDeployment.release_id == bridge.id).order_by(FirmwareDeployment.id.desc()).limit(1).execution_options(populate_existing=True))
    if installed is None or installed.status != "SUCCEEDED":
        return "JOURNAL_BRIDGE_INSTALL_NOT_VERIFIED"
    campaign = session.get(FirmwareCampaign, installed.campaign_id, populate_existing=True)
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == installed.id,
        FirmwareEvent.state.in_(EVENTS)).order_by(FirmwareEvent.id.desc()).limit(1).execution_options(populate_existing=True))
    if campaign is None or campaign.status not in {"ACTIVE", "COMPLETED"}:
        return "JOURNAL_BRIDGE_READY_NOT_VERIFIED"
    if event is None:
        return "JOURNAL_BRIDGE_READY_MISSING"
    # ready_event_matches can otherwise reuse an older run from this session's
    # identity map even though the event and release above are current.
    run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == (event.details or {}).get("run_id"))
        .execution_options(populate_existing=True))
    identity = _release_identity(bridge).model_dump(mode="json")
    if (run is None or (event.details or {}).get("target") != target.identity.model_dump()
            or any((event.details or {}).get(key) != value for key, value in identity.items())
            or not ready_event_matches(session, event, installed, bridge)):
        return "JOURNAL_BRIDGE_READY_NOT_VERIFIED"
    # The signed reader's historical qualification survives a verified return
    # to that image. Current boot identity and recovery must independently pass
    # writer_predecessor_hold below; an old healthy runtime sample cannot do so.
    if reader["version"] == "2.6.22":
        try:
            from zk_add.zkt_factory_trial import revoked_evidence
            revoked_evidence(session, connector, installed, bridge)
        except (ImportError, ValueError):
            return "JOURNAL_FACTORY_REVOKED_PROOF_REQUIRED"
    if ensure_utc(run.completed_at) > utc_now():
        return "JOURNAL_BRIDGE_READY_STALE"
    return None


def writer_predecessor_hold(session, connector, release):
    from zk_add.models import Connector
    from zk_add.ota import FirmwareRelease, _versions_match
    from zk_add.time_utils import ensure_utc, utc_now
    if getattr(release, "id", None) is not None:
        release = session.get(FirmwareRelease, release.id, populate_existing=True)
    if release is None or release.state != "HIL_ONLY" or getattr(release, "revoked_at", None) is not None:
        return "JOURNAL_WRITER_HIL_ONLY"
    try:
        contract = validate_writer_manifest(release.manifest or {})
        if contract["schema_version"] != 5:
            return "JOURNAL_HISTORICAL_WRITER_AUDIT_ONLY"
        # Read current identity/security before selecting a matrix entry. The
        # later stored-readiness lookup also refreshes the connector, but that
        # cannot retroactively validate checks made against a cached old boot.
        connector = session.get(Connector, connector.id, populate_existing=True)
        if connector is None:
            return "JOURNAL_EXACT_BRIDGE_REQUIRED"
        reader = reader_for_writer(release, connector)
    except ValueError:
        return "JOURNAL_READER_MATRIX_BLOCKED"
    if reader is None:
        return "JOURNAL_EXACT_BRIDGE_REQUIRED"
    qualification = qualified_bridge_hold(session, connector, reader)
    if qualification:
        return qualification
    if (reader_for_writer(release, connector) != reader
            or not _versions_match(connector.firmware_version, reader["version"])
            or connector.ota_running_partition not in {"ota_0", "ota_1"}
            or not connector.ota_secure_boot or not connector.ota_rollback_enabled):
        return "JOURNAL_EXACT_BRIDGE_REQUIRED"
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
