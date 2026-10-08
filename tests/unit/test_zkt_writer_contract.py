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

from reader_matrix_fixtures import pinned, writer_manifest  # noqa: F401
from test_zkt_bridge_contract import session as session
from zk_add.models import Connector, ZKTDevice
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    FirmwareEvent, FirmwareHilRun,
    _storage_predecessor_exclusion,
    sync_release_store,
)
from zk_add.settings import settings
from zk_add.storage_contract import validate_storage_contract
from zk_add.time_utils import utc_now
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt_writer_contract import (
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
        minimum_bootstrap_version="2.6.17",
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
        {"minimum_bootstrap_version": "2.6.16"},
        {"hil_targets": signed_hil_targets()[:-1]},
        {"queue_storage": {**writer_contract(), "journal_capture": 1}},
        {"queue_storage": {**writer_contract(), "allowed_bootstrap_images": {"2.6.17": "c" * 64}}},
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
        image().replace(b"BRIDGE=2.6.17", b"BRIDGE=2.6.16"),
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
def installed(session, pinned):  # noqa: F811
    reader = pinned["readers"][0]
    now = utc_now()
    bridge = FirmwareRelease(
        release_id="zone-lite-2.6.23",
        version="2.6.23",
        git_sha=reader["source_sha"],
        image_sha256=reader["artifact_sha256"],
        image_size=1380352,
        signing_key_id=reader["signing_key_id"],
        partition_layout="zone-lite-ota-v1",
        storage_name="synthetic",
        manifest={"application_sha256": reader["application_sha256"]},
        manifest_signature="test",
        state="HIL_ONLY",
    )
    target = signed_hil_targets()[0]
    connector = Connector(
        connector_id=target["connector_id"],
        hardware_id=target["mac"],
        zone_id="test",
        zone_name="test",
        device_id="test",
        display_name="test",
        firmware_version="zone-lite-2.6.23",
        zkt_custody_enabled=True,
        ota_image_sha256=reader["application_sha256"],
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
    connector.zkt_device = ZKTDevice(serial=target["terminal_serial"], expected_serial=target["terminal_serial"],
        confirmed_serial=target["terminal_serial"], terminal_binding_state="CONFIRMED")
    session.add_all([bridge, connector])
    session.flush()
    campaign = FirmwareCampaign(
        campaign_id="test",
        release_id=bridge.id,
        zone_id="test",
        actor="test",
        idempotency_key="test",
        typed_confirmation="2.6.23",
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
        target_version="2.6.23",
    )
    session.add(deployment)
    session.flush()
    from zk_add.hil_runs import _release_identity
    identity = _release_identity(bridge).model_dump(mode="json")
    run = FirmwareHilRun(run_id="ready", deployment_id=deployment.id, connector_id=connector.id,
        release_id=bridge.id, actor="test", idempotency_key="ready", status="BRIDGE_READY",
        target=target, release_identity=identity, baseline={"profile": "BRIDGE_READINESS_V1", "boot_id": "test"},
        started_at=now - timedelta(minutes=16), ends_at=now - timedelta(minutes=1), completed_at=now,
        result={"outcome": "READY", "reasons": []})
    session.add_all([run, FirmwareEvent(deployment_id=deployment.id, state="BRIDGE_READY", details={
        **identity, "target": target, "run_id": run.run_id, "profile": "BRIDGE_READINESS_V1", "outcome": "READY"})])
    session.flush()
    release = FirmwareRelease(version="2.7.0", state="HIL_ONLY", manifest={**manifest(), **writer_manifest(pinned)})
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
        "failed-original-bridge",
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
    elif fault == "failed-original-bridge":
        connector.firmware_version = "2.6.16"
        connector.ota_image_sha256 = "7a6d7d69e8c033723d9075edd87b96747920260114576da5c1f6359872737599"
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


@pytest.mark.parametrize("fault", ["missing", "forged", "target", "image", "key", "git", "stale", "future", "cancelled", "later-incomplete"])
def test_writer_requires_its_exact_stored_bridge_ready(installed, fault):
    session, connector, bridge, deployment, release = installed
    run = session.scalar(select(FirmwareHilRun))
    event = session.scalar(select(FirmwareEvent))
    if fault == "missing":
        session.delete(event)
    elif fault == "forged":
        session.delete(run)
    elif fault in {"target", "image", "key", "git"}:
        key = {"target": "target", "image": "application_sha256", "key": "signing_key_id", "git": "git_sha"}[fault]
        value = signed_hil_targets()[1] if fault == "target" else "wrong"
        event.details = {**event.details, key: value}
        if fault == "target":
            run.target = value
        else:
            run.release_identity = {**run.release_identity, key: value}
    elif fault == "stale":
        session.add(FirmwareDeployment(deployment_id="newer-bridge", campaign_id=deployment.campaign_id,
            release_id=bridge.id, connector_id=connector.id, status="SUCCEEDED", target_version=bridge.version))
    elif fault == "future":
        run.completed_at = utc_now() + timedelta(minutes=1)
    elif fault == "cancelled":
        session.get(FirmwareCampaign, deployment.campaign_id).status = "CANCELLED"
    else:
        session.add(FirmwareEvent(deployment_id=deployment.id, state="BRIDGE_INCOMPLETE", details=event.details))
    session.flush()
    assert _storage_predecessor_exclusion(session, release, connector)


@pytest.mark.parametrize("preferred_order", ["ascending", "descending"])
def test_actual_writer_campaign_assignment_and_download_require_stored_bridge_ready(installed, pinned, monkeypatch, tmp_path, preferred_order):  # noqa: F811
    from zk_add import ota
    session, connector, bridge, deployment, release = installed
    if preferred_order == "descending":
        from zk_add import zkt_reader_matrix as matrix
        reversed_policy = deepcopy(pinned)
        reversed_policy["readers"].reverse()
        monkeypatch.setattr(matrix, "VERSIONS", tuple(reversed(matrix.VERSIONS)))
        matrix.MATRIX_PATH.write_text(json.dumps(reversed_policy))
        release.manifest = {**release.manifest, **writer_manifest(reversed_policy)}
        assert release.manifest["queue_storage"]["allowed_bootstrap_versions"] == ["2.6.22", "2.6.23"]
        assert release.manifest["minimum_bootstrap_version"] == "2.6.22"
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "fleet_root_secret", "isolated-test-secret")
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    connector.connected = connector.ota_capable = True
    connector.ota_partition_layout = "zone-lite-ota-v1"
    bridge_campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    bridge_campaign.status = "COMPLETED"
    release.release_id, release.git_sha, release.image_sha256 = "zone-lite-2.7.0", "b" * 40, "f" * 64
    release.image_size, release.signing_key_id, release.partition_layout = 4, "test", "zone-lite-ota-v1"
    release.storage_name, release.manifest_signature = "test.bin", "test"
    release.minimum_bootstrap_version = "2.6.22"
    release.manifest = {**release.manifest, "_hil_targets": signed_hil_targets()[:1]}
    (tmp_path / release.storage_name).write_bytes(b"test")
    session.add(release)
    session.flush()
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "BRIDGE_READY"))
    correct = event.details
    event.details = {**correct, "run_id": "forged"}
    with pytest.raises(ValueError, match="READY_NOT_VERIFIED"):
        ota.preview_campaign_scope(session, release_public_id=release.release_id, zone_id=connector.zone_id)
    event.details = correct
    preview = ota.preview_campaign_scope(session, release_public_id=release.release_id, zone_id=connector.zone_id)
    ota.create_campaign(session, release_public_id=release.release_id, zone_id=connector.zone_id,
        reason="test", typed_confirmation=release.version, actor="test", scope_token=preview["scope_token"], idempotency_key="writer")
    assigned = ota.assignment_for_connector(session, connector=connector, public_base="https://add.test")
    assert assigned is not None
    token = assigned["download_url"].rsplit("/", 1)[1]
    assert ota.resolve_download(session, token)[0].id == release.id
    offered = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "OFFERED"))
    committed = deepcopy(offered.details)
    assert committed["reader_admission"]["reader"]["application_sha256"] == connector.ota_image_sha256
    from sqlalchemy.orm import Session
    for field, invalid in (("ota_secure_boot", False), ("ota_rollback_enabled", False),
            ("ota_running_partition", "factory"), ("ota_image_sha256", "e" * 64),
            ("firmware_version", "2.6.15")):
        session.commit()
        previous = getattr(connector, field)
        with Session(session.get_bind()) as peer:
            setattr(peer.get(Connector, connector.id), field, invalid)
            peer.commit()
        assert getattr(connector, field) == previous  # Real stale identity map.
        assert ota.assignment_for_connector(session, connector=connector, public_base="https://add.test") is None
        with pytest.raises(ValueError, match="storage predecessor"):
            ota.resolve_download(session, token)
        with Session(session.get_bind()) as peer:
            setattr(peer.get(Connector, connector.id), field, previous)
            peer.commit()
        session.refresh(connector)
    with Session(session.get_bind()) as peer:
        peer.get(FirmwareRelease, release.id).state = "REVOKED"
        peer.commit()
    assert release.state == "HIL_ONLY"
    assert ota.assignment_for_connector(session, connector=connector, public_base="https://add.test") is None
    with pytest.raises(ValueError):
        ota.resolve_download(session, token)
    with Session(session.get_bind()) as peer:
        peer.get(FirmwareRelease, release.id).state = "HIL_ONLY"
        peer.commit()
    session.refresh(release)
    changed = deepcopy(committed)
    changed["reader_admission"]["reader"]["signing_key_id"] = "substituted"
    offered.details = changed
    session.flush()
    assert ota.assignment_for_connector(session, connector=connector, public_base="https://add.test") is None
    with pytest.raises(ValueError, match="admission reader identity"):
        ota.resolve_download(session, token)
    offered.details = committed
    bridge_campaign.status = "CANCELLED"
    session.flush()
    assert ota.assignment_for_connector(session, connector=connector, public_base="https://add.test") is None
    with pytest.raises(ValueError, match="storage predecessor"):
        ota.resolve_download(session, token)


def test_qualified_bridge_new_boot_requires_fresh_current_reader_evidence(installed):
    session, connector, bridge, deployment, release = installed
    connector.boot_id = "verified-return-boot"
    diagnostics = deepcopy(connector.firmware_diagnostics)
    diagnostics.update(boot_id=connector.boot_id, sampled_at=utc_now().isoformat())
    connector.firmware_diagnostics, connector.firmware_diagnostics_at = diagnostics, utc_now()
    assert _storage_predecessor_exclusion(session, release, connector) is None
    diagnostics = deepcopy(diagnostics)
    diagnostics["journal_runtime"]["reader_ready"] = False
    connector.firmware_diagnostics = diagnostics
    assert _storage_predecessor_exclusion(session, release, connector) == "JOURNAL_BRIDGE_READER_NOT_VERIFIED"


@pytest.mark.parametrize("fault", ["bridge", "campaign", "terminal", "admission", "secure_boot", "rollback",
                                   "partition", "current_image", "current_version", "readiness_run"])
def test_cached_identity_map_cannot_restore_revoked_reader_scope(installed, fault):
    from sqlalchemy.orm import Session
    session, connector, bridge, deployment, release = installed
    session.commit()
    assert _storage_predecessor_exclusion(session, release, connector) is None
    terminal = connector.zkt_device
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "BRIDGE_READY"))
    readiness = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == event.details["run_id"]))
    with Session(session.get_bind()) as peer:
        if fault == "bridge":
            peer.get(FirmwareRelease, bridge.id).state = "REVOKED"
        elif fault == "campaign":
            peer.get(FirmwareCampaign, campaign.id).status = "CANCELLED"
        elif fault == "terminal":
            peer.get(ZKTDevice, terminal.id).confirmed_serial = "changed-terminal"
        elif fault == "admission":
            row = peer.get(FirmwareEvent, event.id)
            row.details = {**row.details, "run_id": "changed"}
        elif fault == "readiness_run":
            peer.get(FirmwareHilRun, readiness.id).status = "BRIDGE_INCOMPLETE"
        else:
            field, value = {"secure_boot": ("ota_secure_boot", False), "rollback": ("ota_rollback_enabled", False),
                "partition": ("ota_running_partition", "factory"), "current_image": ("ota_image_sha256", "e" * 64),
                "current_version": ("firmware_version", "2.6.15")}[fault]
            setattr(peer.get(Connector, connector.id), field, value)
        peer.commit()
    # Objects genuinely remain cached before the gate's fresh evidence query.
    assert bridge.state == "HIL_ONLY" and campaign.status == "ACTIVE"
    assert terminal.confirmed_serial == signed_hil_targets()[0]["terminal_serial"]
    assert connector.ota_secure_boot and connector.ota_rollback_enabled
    assert connector.ota_running_partition == "ota_1" and connector.firmware_version == "zone-lite-2.6.23"
    assert readiness.status == "BRIDGE_READY"
    assert _storage_predecessor_exclusion(session, release, connector) is not None
