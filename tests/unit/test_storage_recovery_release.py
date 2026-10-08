"""One-shot 2.6.24 storage recovery: exact Peshawar scope, never re-offered or promoted."""
import json
import re
import secrets
import struct
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zk_add.storage_contract import validate_storage_contract
from zk_add.storage_recovery import (
    BASELINE_IMAGE, CONTRACT, MARKER, TARGETS, validate_recovery_image, validate_recovery_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = "f" * 64
ZONES = ("ZONE-PESHAWAR-02", "ZONE-PESHAWAR-06")


def signed_manifest(**changes):
    manifest = {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "hil_targets": [target.model_dump() for target in TARGETS], "image_name": "zone-lite-2.6.24.bin",
        "minimum_bootstrap_version": "2.5.2", "partition_layout": "zone-lite-ota-v1",
        "project_name": "zone_lite", "queue_storage": json.loads(json.dumps(CONTRACT)),
        "release_channel": "EXPERIMENTAL_HIL_ONLY", "release_id": "zone-lite-2.6.24",
        "version": "2.6.24",
    }
    for key, value in changes.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    return manifest


def image(version=b"2.6.24", marker=MARKER.encode() + b"\0", project=b"zone_lite"):
    header = bytearray(112)
    header[0] = 0xE9
    struct.pack_into("<I", header, 32, 0xABCD5432)
    header[48:48 + len(version)] = version
    header[80:80 + len(project)] = project
    return bytes(header) + b"code" + marker + b"tail"


def test_exact_recovery_contract_and_manifest():
    assert validate_recovery_manifest(signed_manifest()) == CONTRACT
    assert validate_storage_contract(signed_manifest(), "2.6.24") == CONTRACT
    assert validate_storage_contract({}, "2.5.3") is None
    reordered = [TARGETS[1].model_dump(), TARGETS[0].model_dump()]
    extra_baseline = {**CONTRACT, "allowed_bootstrap_versions": ["2.4.12", "2.5.2"]}
    boolean_format = {**CONTRACT, "write_format": True}
    for bad in (
        signed_manifest(queue_storage=None), signed_manifest(queue_storage=extra_baseline),
        signed_manifest(queue_storage=boolean_format), signed_manifest(minimum_bootstrap_version="2.4.12"),
        signed_manifest(minimum_bootstrap_version=None), signed_manifest(release_channel=None),
        signed_manifest(hil_targets=reordered), signed_manifest(hil_targets=reordered[:1]),
        signed_manifest(release_id="zone-lite-2.6.25"), signed_manifest(firmware_family="hikvision"),
        signed_manifest(factory_trial={}), signed_manifest(runtime_profile="ZKT_JOURNAL_V1"),
    ):
        with pytest.raises(ValueError):
            validate_storage_contract(bad, "2.6.24")


def test_compiled_role_and_descriptor_bind_the_image():
    validate_recovery_image(image())
    legacy = b"ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2\0"
    for bad in (image(version=b"2.6.15"), image(project=b"zone_lite_hikvision"), image(marker=b""),
                image(marker=MARKER.encode() + b"\0" + MARKER.encode() + b"\0"),
                image(marker=MARKER.encode() + b"\0" + legacy), image(marker=legacy), b"\xe9" * 64):
        with pytest.raises(ValueError):
            validate_recovery_image(bad)


def test_reviewed_scope_matches_release_tooling_and_firmware():
    tooling = json.loads((ROOT / "deploy/add/hil-targets-2.6.24.json").read_text())
    assert tooling == [target.model_dump() for target in TARGETS]
    firmware = (ROOT / "firmware/zone_lite/main/storage_recovery.c").read_text()
    pairs = re.findall(r'\{"([0-9a-f-]{36})", \{((?:0x[0-9a-f]{2}, ){5}0x[0-9a-f]{2})\}\}', firmware)
    assert [(connector, ":".join(byte[2:] for byte in mac.split(", "))) for connector, mac in pairs] == [
        (target.connector_id, target.mac) for target in TARGETS]
    marker = (ROOT / "firmware/zone_lite/main/storage_upgrade.c").read_text()
    assert f'return "{MARKER}";' in marker
    assert BASELINE_IMAGE in (ROOT / "firmware/zone_lite/main/upgrade_guard.c").read_text()


@pytest.fixture
def recovery_session(monkeypatch, tmp_path):
    from zk_add import ota
    from zk_add.hil_2615_cities import CITY_TARGETS
    from zk_add.models import Base, Connector, ZKTDevice
    from zk_add.ota import HIL_269_EXACT_TARGETS
    from zk_add.settings import settings

    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    # The shared ordered configuration stays the 2.6.15 scope and is never used here.
    monkeypatch.setattr(settings, "firmware_hil_targets_json",
                        json.dumps([row.model_dump() for row in HIL_269_EXACT_TARGETS]))
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    assert not set(ZONES) & set(CITY_TARGETS)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    (tmp_path / "hil").mkdir()
    with Session(engine) as session:
        session.add(ota.FirmwareRelease(
            release_id="zone-lite-2.5.2", version="2.5.2", git_sha="c" * 40, image_sha256="d" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.2.0", storage_name="hil/baseline.bin",
            manifest_signature="test-signature", state="AVAILABLE",
            manifest={"application_sha256": BASELINE_IMAGE}))
        manifest = signed_manifest()
        release = ota.FirmwareRelease(
            release_id="zone-lite-2.6.24", version="2.6.24", git_sha="a" * 40, image_sha256="b" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.5.2", storage_name="hil/recovery.bin",
            manifest_signature="test-signature", state="HIL_ONLY",
            manifest={**manifest, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                      "_hil_targets": manifest["hil_targets"]})
        session.add(release)
        (tmp_path / release.storage_name).write_bytes(b"test recovery artifact")
        devices = []
        for index, (target, zone) in enumerate(zip(TARGETS, ZONES)):
            device = Connector(
                connector_id=target.connector_id, hardware_id=target.mac, zone_id=zone, zone_name=zone,
                device_id="1", display_name=zone, firmware_version="zone-lite-2.5.2", connected=True,
                active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
                ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT,
                ota_running_partition=("ota_1", "ota_0")[index], ota_image_sha256=BASELINE_IMAGE)
            device.zkt_device = ZKTDevice(
                serial=target.terminal_serial, expected_serial=target.terminal_serial,
                confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED")
            session.add(device)
            devices.append(device)
        session.flush()
        yield session, release, devices
    engine.dispose()


def start(session, release, zone):
    from zk_add.ota import create_campaign, preview_campaign_scope
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    run = create_campaign(session, release_public_id=release.release_id, zone_id=zone,
                          reason="Peshawar storage recovery", typed_confirmation=release.version,
                          actor="test-admin", scope_token=scope["scope_token"],
                          idempotency_key=f"recovery-{zone}")
    return scope, run


def offer(session, device):
    from zk_add.ota import assignment_for_connector
    return assignment_for_connector(session, connector=device, public_base="https://test.invalid")


def report(session, device, state, error_code=None, release_digest=APPLICATION):
    from zk_add.ota import FirmwareDeployment, record_progress
    deployment = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.connector_id == device.id).order_by(FirmwareDeployment.id.desc()))
    return record_progress(session, connector=device, deployment_public_id=deployment.deployment_id,
                           state=state, bytes_written=1024, running_version="2.6.24",
                           running_partition="ota_0", image_sha256=release_digest, error_code=error_code)


def test_each_peshawar_target_starts_independently_in_either_order(recovery_session):
    session, release, devices = recovery_session
    for index in (1, 0):
        scope, run = start(session, release, ZONES[index])
        assert [row["connector_id"] for row in scope["eligible"]] == [devices[index].connector_id]
        assert run.eligible_count == 1
        assert offer(session, devices[index])["version"] == "2.6.24"


@pytest.mark.parametrize("change", ["factory", "digest", "version", "spare", "serial", "release"])
def test_recovery_requires_exact_signed_252_predecessor_and_identity(recovery_session, change):
    from zk_add.ota import preview_campaign_scope
    session, release, devices = recovery_session
    device = devices[0]
    if change == "factory":
        device.ota_running_partition = "factory"
    elif change == "digest":
        device.ota_image_sha256 = "0" * 64
    elif change == "version":
        device.firmware_version = "zone-lite-2.4.12"
    elif change == "spare":
        device.is_spare = True
    elif change == "serial":
        device.zkt_device.confirmed_serial = "replacement"
    else:
        release.state = "AVAILABLE"
    with pytest.raises(ValueError):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=ZONES[0])


def test_selected_image_is_never_offered_again_and_outcome_pauses(recovery_session):
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment, resolve_download
    session, release, devices = recovery_session
    _scope, run = start(session, release, ZONES[0])
    grant = offer(session, devices[0])["download_url"].rsplit("/", 1)[1]
    session.flush()
    assert resolve_download(session, grant)[0] == release
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    deployment.status = "READY_TO_BOOT"
    session.flush()
    # The ESP returned to 2.5.2 before any terminal report reached ADD.
    assert offer(session, devices[0]) is None
    with pytest.raises(ValueError, match="never downloaded again"):
        resolve_download(session, grant)
    report(session, devices[0], "BOOTED_PENDING", "STORAGE_RECOVERY_RUNNING")
    report(session, devices[0], "FAILED", "STORAGE_RECOVERY_COMPLETE")
    session.flush()
    session.refresh(run)
    assert run.status == "PAUSED" and run.pause_reason.endswith("FAILED (STORAGE_RECOVERY_COMPLETE)")
    # The rollback image's own later report cannot overwrite the recovery outcome.
    report(session, devices[0], "ROLLED_BACK", "BOOTLOADER_ROLLBACK")
    session.flush()
    session.refresh(deployment)
    assert deployment.status == "FAILED" and deployment.error_code == "STORAGE_RECOVERY_COMPLETE"
    assert session.get(FirmwareCampaign, run.id).status == "PAUSED"


def test_peshawar_installs_are_serialized_once_a_journal_release_exists(recovery_session):
    from zk_add import ota
    session, release, devices = recovery_session
    session.add(ota.FirmwareRelease(
        release_id="zone-lite-2.6.23", version="2.6.23", git_sha="e" * 40, image_sha256="9" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name="hil/bridge.bin",
        manifest_signature="test-signature", state="REVOKED", manifest={}))
    session.flush()
    start(session, release, ZONES[0])
    start(session, release, ZONES[1])
    assert offer(session, devices[0]) is not None
    session.flush()
    assert offer(session, devices[1]) is None  # one Peshawar install at a time
    report(session, devices[0], "FAILED", "STORAGE_RECOVERY_COMPLETE")
    session.flush()
    assert offer(session, devices[1]) is not None


def test_recovery_progress_requires_exact_running_image(recovery_session):
    session, release, devices = recovery_session
    start(session, release, ZONES[0])
    assert offer(session, devices[0]) is not None
    session.flush()
    with pytest.raises(ValueError, match="digest"):
        report(session, devices[0], "BOOTED_PENDING", "STORAGE_RECOVERY_RUNNING", release_digest="0" * 64)
