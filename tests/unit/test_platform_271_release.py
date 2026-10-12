"""ZKT 2.7.1: the 2.7 platform in the 2.6.15 fleet's legacy authority.

Its HIL scope is an exact prefix of the signed nationwide list. Each target
starts independently; the fleet reservation guard paces the offers."""
import json
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.storage_contract import (FLEET_BASELINE_IMAGES, FLEET_BASELINES, FLEET_PLATFORM_VERSION,
                                     validate_storage_contract)
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt270_scope import TARGETS

FLEET_2615 = FLEET_BASELINE_IMAGES["2.6.15"]
APPLICATION = "e" * 64
# 3FL and Swat 01 run 2.6.15 in different slots; Peshawar 02 still runs 2.5.2.
DEVICES = (
    (0, "ZONE-SLICTOWER-3FL", "2.6.15", "ota_0"),
    (1, "ZONE-SWAT-01", "2.6.15", "ota_1"),
    (5, "ZONE-PESHAWAR-02", "2.5.2", "ota_1"),
)


def manifest(**changes):
    value = {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(FLEET_BASELINES),
                          "allowed_bootstrap_images": dict(FLEET_BASELINE_IMAGES)},
        "release_id": f"zone-lite-{FLEET_PLATFORM_VERSION}", "version": FLEET_PLATFORM_VERSION,
    }
    value.update(changes)
    return value


@pytest.fixture
def platform(monkeypatch, tmp_path):
    from zk_add import ota
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
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    (tmp_path / "hil").mkdir()
    with Session(engine) as session:
        for fill, version, state in (("1", "2.4.12", "AVAILABLE"), ("2", "2.5.2", "AVAILABLE"),
                                     ("3", "2.6.15", "HIL_ONLY")):
            session.add(ota.FirmwareRelease(
                release_id=f"zone-lite-{version}", version=version, git_sha="c" * 40, image_sha256=fill * 64,
                image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
                minimum_bootstrap_version="2.2.0", storage_name=f"hil/{version}.bin",
                manifest_signature="test-signature", state=state,
                manifest={"application_sha256": FLEET_BASELINE_IMAGES[version]}))
        signed = manifest()
        release = ota.FirmwareRelease(
            release_id=signed["release_id"], version=FLEET_PLATFORM_VERSION, git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1024, signing_key_id="production-key",
            partition_layout=ota.OTA_LAYOUT, minimum_bootstrap_version="2.4.12",
            storage_name="hil/platform.bin", manifest_signature="test-signature", state="HIL_ONLY",
            manifest={**signed, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                      "_hil_targets": signed_hil_targets()})
        session.add(release)
        (tmp_path / release.storage_name).write_bytes(b"test platform artifact")
        devices = []
        for index, zone, version, slot in DEVICES:
            target = TARGETS[index].identity
            device = Connector(
                connector_id=target.connector_id, hardware_id=target.mac, zone_id=zone, zone_name=zone,
                device_id="1", display_name=zone, firmware_version=f"zone-lite-{version}", connected=True,
                active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
                ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition=slot,
                ota_image_sha256=FLEET_BASELINE_IMAGES[version])
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
                          reason="2.7.1 HIL", typed_confirmation=release.version, actor="test-admin",
                          scope_token=scope["scope_token"], idempotency_key=f"platform-{zone}")
    return scope, run


def offer(session, device):
    from zk_add.ota import assignment_for_connector
    return assignment_for_connector(session, connector=device, public_base="https://test.invalid")


def test_contract_admits_the_fleet_image_as_a_rollback_target():
    contract = validate_storage_contract(manifest(), FLEET_PLATFORM_VERSION)
    assert contract["allowed_bootstrap_versions"][-1] == "2.6.15"
    assert contract["allowed_bootstrap_images"]["2.6.15"] == FLEET_2615
    with pytest.raises(ValueError):
        validate_storage_contract(manifest(minimum_bootstrap_version="2.2.0"), FLEET_PLATFORM_VERSION)
    without_fleet = manifest()
    del without_fleet["queue_storage"]["allowed_bootstrap_images"]["2.6.15"]
    without_fleet["queue_storage"]["allowed_bootstrap_versions"].remove("2.6.15")
    with pytest.raises(ValueError):
        validate_storage_contract(without_fleet, FLEET_PLATFORM_VERSION)


def test_targets_start_independently_from_either_predecessor(platform):
    session, release, devices = platform
    # 3FL first, then Peshawar 02 on 2.5.2, ahead of Swat 01 in the list.
    for device in (devices[0], devices[2]):
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert run.eligible_count == 1
        assert offer(session, device)["version"] == FLEET_PLATFORM_VERSION


def test_fleet_guard_paces_offers_once_journal_releases_exist(platform):
    from zk_add import ota
    session, release, devices = platform
    session.add(ota.FirmwareRelease(
        release_id="zone-lite-2.7.0", version="2.7.0", git_sha="f" * 40, image_sha256="f" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name="hil/writer.bin", manifest_signature="test-signature",
        state="REVOKED", manifest={"application_sha256": "f" * 64}))
    session.flush()
    for device in devices:
        start(session, release, device.zone_id)
    assert offer(session, devices[0])["version"] == FLEET_PLATFORM_VERSION
    assert offer(session, devices[2])["version"] == FLEET_PLATFORM_VERSION
    # A third unsettled upgrade waits for one of the first two to settle.
    assert offer(session, devices[1]) is None


@pytest.mark.parametrize("change", ["digest", "factory", "version", "serial"])
def test_requires_the_exact_signed_predecessor_and_identity(platform, change):
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    device = devices[0]
    if change == "digest":
        device.ota_image_sha256 = "0" * 64
    elif change == "factory":
        device.ota_running_partition = "factory"
    elif change == "version":
        device.firmware_version = "zone-lite-2.6.11"
    else:
        device.zkt_device.confirmed_serial = "replacement"
    session.flush()
    with pytest.raises(ValueError, match="exactly one eligible connector"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_scope_is_an_exact_nationwide_prefix_and_needs_hil(platform, monkeypatch):
    from zk_add.ota import _parse_release_hil_targets, _permitted_hil_targets
    from zk_add.settings import settings
    session, release, _devices = platform
    identity = (release.release_id, release.version, release.git_sha, release.image_sha256, APPLICATION)
    full = signed_hil_targets()
    assert [target.model_dump() for target in _parse_release_hil_targets(identity, full[:1])] == full[:1]
    for scope in (full[1:2], [full[1], full[0]], full + full[:1], []):
        with pytest.raises(ValueError):
            _parse_release_hil_targets(identity, scope)
    assert len(_permitted_hil_targets(session, release)) == len(TARGETS)
    monkeypatch.setattr(settings, "firmware_hil_enabled", False)
    with pytest.raises(ValueError, match="disabled"):
        _permitted_hil_targets(session, release)
