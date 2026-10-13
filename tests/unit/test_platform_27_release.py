"""ZKT 2.7 platform images (2.7.1-2.7.3) in the 2.6.15 fleet's legacy authority.

Their HIL scope is an exact prefix of the signed nationwide list. Each target
starts independently; the fleet reservation guard paces the offers."""
import json
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.storage_contract import (FLEET_BASELINE_IMAGES, FLEET_PLATFORM_VERSIONS, PLATFORM_BASELINE_IMAGES,
                                     platform_baselines, validate_storage_contract)
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


def manifest(version, **changes):
    baselines, images = platform_baselines(version)
    value = {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(baselines),
                          "allowed_bootstrap_images": dict(images)},
        "release_id": f"zone-lite-{version}", "version": version,
    }
    value.update(changes)
    return value


@pytest.fixture(params=FLEET_PLATFORM_VERSIONS)
def platform(monkeypatch, tmp_path, request):
    platform_version = request.param
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
        signed = manifest(platform_version)
        release = ota.FirmwareRelease(
            release_id=signed["release_id"], version=platform_version, git_sha="a" * 40,
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
                          reason=f"{release.version} HIL", typed_confirmation=release.version, actor="test-admin",
                          scope_token=scope["scope_token"], idempotency_key=f"platform-{zone}")
    return scope, run


def offer(session, device):
    from zk_add.ota import assignment_for_connector
    return assignment_for_connector(session, connector=device, public_base="https://test.invalid")


@pytest.mark.parametrize("version", FLEET_PLATFORM_VERSIONS)
def test_contract_admits_the_fleet_image_as_a_rollback_target(version):
    contract = validate_storage_contract(manifest(version), version)
    assert "2.6.15" in contract["allowed_bootstrap_versions"]
    assert contract["allowed_bootstrap_images"]["2.6.15"] == FLEET_2615
    # Only 2.7.3 accepts 2.7.2, the image 3FL runs.
    assert ("2.7.2" in contract["allowed_bootstrap_versions"]) == (version == "2.7.3")
    with pytest.raises(ValueError):
        validate_storage_contract(manifest(version, minimum_bootstrap_version="2.2.0"), version)
    without_fleet = manifest(version)
    del without_fleet["queue_storage"]["allowed_bootstrap_images"]["2.6.15"]
    without_fleet["queue_storage"]["allowed_bootstrap_versions"].remove("2.6.15")
    with pytest.raises(ValueError):
        validate_storage_contract(without_fleet, version)


def test_targets_start_independently_from_either_predecessor(platform):
    session, release, devices = platform
    # 3FL first, then Peshawar 02 on 2.5.2, ahead of Swat 01 in the list.
    for device in (devices[0], devices[2]):
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert run.eligible_count == 1
        assert offer(session, device)["version"] == release.version


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
    assert offer(session, devices[0])["version"] == release.version
    assert offer(session, devices[2])["version"] == release.version
    # A third unsettled upgrade waits for one of the first two to settle.
    assert offer(session, devices[1]) is None


def test_only_2_7_3_upgrades_3fl_in_place_from_2_7_2(platform):
    """3FL runs the exact HIL-only 2.7.2; 2.7.3 may replace it in place."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version != "2.7.2":
        session.add(ota.FirmwareRelease(
            release_id="zone-lite-2.7.2", version="2.7.2", git_sha="6" * 40, image_sha256="6" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.2.bin", manifest_signature="test-signature",
            state="HIL_ONLY", manifest={"application_sha256": PLATFORM_BASELINE_IMAGES["2.7.2"]}))
    device = devices[0]
    device.firmware_version = "zone-lite-2.7.2"
    device.ota_running_partition = "ota_1"
    device.ota_image_sha256 = PLATFORM_BASELINE_IMAGES["2.7.2"]
    session.flush()
    if release.version == "2.7.3":
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert offer(session, device)["version"] == "2.7.3"
        # A 2.7.2 claim on another image is still refused.
        device.ota_image_sha256 = "0" * 64
        session.flush()
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    else:
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


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


@pytest.mark.parametrize("firmware,custody,required", [
    ("zone-lite-2.6.15", False, False), ("zone-lite-2.7.1", False, False), ("zone-lite-2.7.2", False, False),
    ("zone-lite-2.7.3", False, False), ("zone-lite-2.7.3", True, True), ("zone-lite-2.7.0", False, True), ("zone-lite-2.6.16", False, True),
])
def test_platform_follows_the_legacy_source_epoch_rule(firmware, custody, required):
    """Like 2.6.15, a 2.7 platform tail without an epoch is admitted, and a
    supplied epoch is still checked."""
    from types import SimpleNamespace
    from zk_add.reconciliation import _require_source_epoch
    connector = SimpleNamespace(firmware_family="zkt", zkt_custody_enabled=custody, firmware_version=firmware)
    row = SimpleNamespace(source_epoch_id=None)
    if required:
        with pytest.raises(ValueError, match="Source epoch"):
            _require_source_epoch(None, connector, row, None)
    else:
        _require_source_epoch(None, connector, row, None)
    with pytest.raises(ValueError, match="Source epoch"):
        _require_source_epoch(None, connector, row, "stale-epoch")
