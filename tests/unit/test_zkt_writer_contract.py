"""Experimental package verification never substitutes for an installed bridge."""

import base64
from copy import deepcopy
import hashlib
import json
import struct
from datetime import timedelta

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest
from sqlalchemy import select

from test_zkt_bridge_contract import session as session
from zk_add.models import Connector
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    _storage_predecessor_exclusion,
    sync_release_store,
)
from zk_add.settings import settings
from zk_add.storage_contract import validate_storage_contract
from zk_add.time_utils import utc_now
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt_writer_contract import (
    BRIDGE_APPLICATION,
    BRIDGE_ARTIFACT,
    WRITER_MARKER,
    validate_writer_image,
    writer_contract,
)


def manifest():
    return dict(
        schema_version=2,
        version="2.7.0",
        release_id="zone-lite-2.7.0",
        firmware_family="zkt",
        project_name="zone_lite",
        release_channel="EXPERIMENTAL_HIL_ONLY",
        minimum_bootstrap_version="2.6.16",
        runtime_profile="ZKT_JOURNAL_V1",
        hil_targets=signed_hil_targets(),
        queue_storage=writer_contract(),
        application_sha256="a" * 64,
        git_sha="b" * 40,
        signing_key_id="isolated-test",
        partition_layout="zone-lite-ota-v1",
        image_name="firmware.bin",
    )


def image():
    raw = bytearray(112)
    raw[0] = 0xE9
    struct.pack_into("<I", raw, 32, 0xABCD5432)
    raw[48:53] = b"2.7.0"
    raw[80:89] = b"zone_lite"
    return bytes(raw) + WRITER_MARKER.encode() + b"\0"


def test_writer_metadata_and_compiled_role_agree():
    assert validate_storage_contract(manifest(), "2.7.0") == writer_contract()
    validate_writer_image(image())


@pytest.mark.parametrize("field", list(writer_contract()))
def test_no_missing_writer_capability(field):
    value = manifest()
    del value["queue_storage"][field]
    with pytest.raises(ValueError):
        validate_storage_contract(value, "2.7.0")


@pytest.mark.parametrize(
    "change",
    [
        {"release_channel": "AVAILABLE"},
        {"runtime_profile": "ZKT_LEGACY"},
        {"firmware_family": "hikvision"},
        {"minimum_bootstrap_version": "2.6.15"},
        {"hil_targets": signed_hil_targets()[:-1]},
        {"queue_storage": {**writer_contract(), "journal_capture": 1}},
        {"queue_storage": {**writer_contract(), "allowed_bootstrap_images": {"2.6.16": "c" * 64}}},
    ],
)
def test_no_implicit_authority_through_modified_manifest(change):
    with pytest.raises(ValueError):
        validate_storage_contract({**manifest(), **change}, "2.7.0")


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        image()[:112],
        image() + WRITER_MARKER.encode() + b"\0",
        image().replace(b"2.7.0", b"2.6.9"),
        image().replace(b"zone_lite", b"wrong_app"),
        image() + b"ZONE_STORAGE_CONTRACT_V3:BRIDGE\0",
        image().replace(b"AUTHORITY=ADD", b"AUTHORITY=ESP"),
    ],
)
def test_mislabeled_binary_rejected(bad):
    with pytest.raises(ValueError):
        validate_writer_image(bad)


def test_real_signed_store_requires_quarantine_and_exact_writer_marker(
    session, tmp_path, monkeypatch
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    monkeypatch.setattr(
        settings, "firmware_signing_public_key_pem_b64", base64.b64encode(public).decode()
    )
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    folder = tmp_path / "2.7.0"
    folder.mkdir()
    raw = image()
    value = {**manifest(), "image_sha256": hashlib.sha256(raw).hexdigest(), "image_size": len(raw)}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    (folder / "manifest.json").write_bytes(canonical)
    (folder / "manifest.sig").write_text(
        base64.b64encode(
            key.sign(
                canonical,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        ).decode()
    )
    (folder / "firmware.bin").write_bytes(raw)
    with pytest.raises(ValueError, match="quarantine"):
        sync_release_store(session)
    marker = dict(
        schema_version=2,
        git_sha=value["git_sha"],
        image_sha256=value["image_sha256"],
        application_sha256=value["application_sha256"],
        targets=signed_hil_targets()[:1],
    )
    (folder / ".hil-only.json").write_text(json.dumps(marker))
    sync_release_store(session)
    row = session.scalar(select(FirmwareRelease))
    assert row.state == "HIL_ONLY" and row.manifest["runtime_profile"] == "ZKT_JOURNAL_V1"


@pytest.fixture
def installed(session):
    now = utc_now()
    bridge = FirmwareRelease(
        release_id="zone-lite-2.6.16",
        version="2.6.16",
        git_sha="b" * 40,
        image_sha256=BRIDGE_ARTIFACT,
        image_size=1380352,
        signing_key_id="test",
        partition_layout="zone-lite-ota-v1",
        storage_name="synthetic",
        manifest={"application_sha256": BRIDGE_APPLICATION},
        manifest_signature="test",
        state="HIL_ONLY",
    )
    connector = Connector(
        connector_id="synthetic",
        hardware_id="aa:bb:cc:dd:ee:01",
        zone_id="test",
        zone_name="test",
        device_id="test",
        display_name="test",
        firmware_version="zone-lite-2.6.16",
        zkt_custody_enabled=True,
        ota_image_sha256=BRIDGE_APPLICATION,
        ota_running_partition="ota_1",
        ota_secure_boot=True,
        ota_rollback_enabled=True,
        boot_id="test",
        firmware_diagnostics_at=now,
        firmware_diagnostics={
            "boot_id": "test",
            "sampled_at": now.isoformat(),
            "journal_runtime": {
                "observed": True,
                "reader_ready": True,
                "phase": "READY",
                "compatibility": "",
            },
            "storage": {
                "persistence_verified": True,
                "recovery_complete": True,
                "durability": "HEALTHY",
                "upgrade_ready": True,
            },
        },
    )
    session.add_all([bridge, connector])
    session.flush()
    campaign = FirmwareCampaign(
        campaign_id="test",
        release_id=bridge.id,
        zone_id="test",
        actor="test",
        idempotency_key="test",
        typed_confirmation="2.6.16",
        reason="test",
    )
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id="test",
        campaign_id=campaign.id,
        release_id=bridge.id,
        connector_id=connector.id,
        status="SUCCEEDED",
        target_version="2.6.16",
    )
    session.add(deployment)
    session.flush()
    release = FirmwareRelease(version="2.7.0", state="HIL_ONLY", manifest=manifest())
    return session, connector, bridge, deployment, release


def test_real_assignment_gate_requires_reader_and_add_custody(installed):
    session, connector, bridge, deployment, release = installed
    assert _storage_predecessor_exclusion(session, release, connector) is None
    connector.zkt_custody_enabled = False
    assert (
        _storage_predecessor_exclusion(session, release, connector)
        == "JOURNAL_ADD_CUSTODY_DISABLED"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "rolled-back",
        "reconciling",
        "wrong-image",
        "factory",
        "old-version",
        "revoked",
        "unknown-reader",
        "wrong-boot",
        "old-sample",
        "error",
        "production",
    ],
)
def test_failed_bridge_never_authorizes_writer(installed, fault):
    session, connector, bridge, deployment, release = installed
    diagnostics = deepcopy(connector.firmware_diagnostics)
    if fault == "rolled-back":
        deployment.status = "ROLLED_BACK"
    elif fault == "reconciling":
        deployment.status = "RECONCILING"
    elif fault == "wrong-image":
        connector.ota_image_sha256 = "e" * 64
    elif fault == "factory":
        connector.ota_running_partition = "factory"
    elif fault == "old-version":
        connector.firmware_version = "2.6.15"
    elif fault == "revoked":
        bridge.state = "REVOKED"
    elif fault == "unknown-reader":
        diagnostics["journal_runtime"]["reader_ready"] = False
    elif fault == "wrong-boot":
        connector.boot_id = "different"
    elif fault == "old-sample":
        diagnostics["sampled_at"] = (utc_now() - timedelta(seconds=46)).isoformat()
    elif fault == "error":
        diagnostics["storage"]["persistence_probe_error"] = 5
    elif fault == "production":
        release.state = "AVAILABLE"
    connector.firmware_diagnostics = diagnostics
    session.flush()
    assert _storage_predecessor_exclusion(session, release, connector)
