"""ZKT 2.7 platform images (2.7.1-2.7.6) in the 2.6.15 fleet's legacy authority.

Their HIL scope is an exact prefix of the signed nationwide list. Each target
starts independently; the fleet reservation guard paces the offers."""
import json
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.storage_contract import (FLEET_BASELINE_IMAGES, FLEET_PLATFORM_VERSIONS, INVENTORY_BASELINE_IMAGES,
                                     PLATFORM_BASELINE_IMAGES, REPAIR_BASELINE_IMAGES, TRACE_BASELINE_IMAGES,
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
    # 2.7.3 and later accept 2.7.2, the image 3FL runs; 2.7.4 and later accept
    # 2.7.3; 2.7.5 and later accept 2.7.4; only 2.7.6 accepts 2.7.5.
    assert ("2.7.2" in contract["allowed_bootstrap_versions"]) == (version in {"2.7.3", "2.7.4", "2.7.5", "2.7.6"})
    assert ("2.7.3" in contract["allowed_bootstrap_versions"]) == (version in {"2.7.4", "2.7.5", "2.7.6"})
    assert ("2.7.4" in contract["allowed_bootstrap_versions"]) == (version in {"2.7.5", "2.7.6"})
    assert ("2.7.5" in contract["allowed_bootstrap_versions"]) == (version == "2.7.6")
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


def test_later_platform_images_upgrade_3fl_in_place_from_2_7_2(platform):
    """3FL runs the exact HIL-only 2.7.2; 2.7.3 to 2.7.6 may replace it in place."""
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
    if release.version in {"2.7.3", "2.7.4", "2.7.5", "2.7.6"}:
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert offer(session, device)["version"] == release.version
        # A 2.7.2 claim on another image is still refused.
        device.ota_image_sha256 = "0" * 64
        session.flush()
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    else:
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_later_platform_images_upgrade_a_2_7_3_device_in_place(platform):
    """Swat, Quetta and Peshawar 06 run the exact HIL-only 2.7.3 (13 October 2026)."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version != "2.7.3":
        session.add(ota.FirmwareRelease(
            release_id="zone-lite-2.7.3", version="2.7.3", git_sha="7" * 40, image_sha256="7" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.3.bin", manifest_signature="test-signature",
            state="HIL_ONLY", manifest={"application_sha256": TRACE_BASELINE_IMAGES["2.7.3"]}))
    device = devices[1]
    device.firmware_version = "zone-lite-2.7.3"
    device.ota_image_sha256 = TRACE_BASELINE_IMAGES["2.7.3"]
    session.flush()
    if release.version in {"2.7.4", "2.7.5", "2.7.6"}:
        scope, _run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
    else:
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_later_platform_images_upgrade_a_2_7_4_device_in_place(platform):
    """The 2.7.4 HIL devices (Peshawar 06 before 2.7.5, ZONE-BLD9-01's K50 and
    the others it reached) take 2.7.5 or 2.7.6 in place, and only from the
    exact signed 2.7.4 bytes."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version != "2.7.4":
        session.add(ota.FirmwareRelease(
            release_id="zone-lite-2.7.4", version="2.7.4", git_sha="8" * 40, image_sha256="8" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.4.bin", manifest_signature="test-signature",
            state="HIL_ONLY", manifest={"application_sha256": INVENTORY_BASELINE_IMAGES["2.7.4"]}))
    device = devices[2]
    device.firmware_version = "zone-lite-2.7.4"
    device.ota_image_sha256 = INVENTORY_BASELINE_IMAGES["2.7.4"]
    session.flush()
    if release.version in {"2.7.5", "2.7.6"}:
        scope, _run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert offer(session, device)["version"] == release.version
        # A 2.7.4 claim on other bytes is refused.
        device.ota_image_sha256 = TRACE_BASELINE_IMAGES["2.7.3"]
        session.flush()
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    else:
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_later_platform_images_need_the_published_2_7_4_to_upgrade_in_place(platform):
    """ADD offers 2.7.5 or 2.7.6 to a 2.7.4 device only while that exact
    release is published (HIL_ONLY or AVAILABLE), as for every earlier
    predecessor."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version not in {"2.7.5", "2.7.6"}:
        return
    device = devices[2]
    device.firmware_version = "zone-lite-2.7.4"
    device.ota_image_sha256 = INVENTORY_BASELINE_IMAGES["2.7.4"]
    predecessor = ota.FirmwareRelease(
        release_id="zone-lite-2.7.4", version="2.7.4", git_sha="8" * 40, image_sha256="8" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.4.bin", manifest_signature="test-signature",
        state="REVOKED", manifest={"application_sha256": INVENTORY_BASELINE_IMAGES["2.7.4"]})
    session.add(predecessor)
    session.flush()
    with pytest.raises(ValueError, match="exactly one eligible connector"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    predecessor.state = "HIL_ONLY"
    session.flush()
    scope, _run = start(session, release, device.zone_id)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]


def test_only_2_7_6_upgrades_a_2_7_5_device_in_place(platform):
    """The 2.7.5 HIL devices (Peshawar 06, Karachi 01 and the others it
    reaches) take 2.7.6 in place, and only from the exact signed 2.7.5 bytes
    (application 430e2daf..., HIL run 39371984527)."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version != "2.7.5":
        session.add(ota.FirmwareRelease(
            release_id="zone-lite-2.7.5", version="2.7.5", git_sha="9" * 40, image_sha256="9" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.5.bin", manifest_signature="test-signature",
            state="HIL_ONLY", manifest={"application_sha256": REPAIR_BASELINE_IMAGES["2.7.5"]}))
    device = devices[1]
    device.firmware_version = "zone-lite-2.7.5"
    device.ota_image_sha256 = REPAIR_BASELINE_IMAGES["2.7.5"]
    session.flush()
    if release.version == "2.7.6":
        scope, _run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert offer(session, device)["version"] == "2.7.6"
        # A 2.7.5 claim on other bytes (2.7.4's) is refused.
        device.ota_image_sha256 = INVENTORY_BASELINE_IMAGES["2.7.4"]
        session.flush()
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    else:
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_2_7_6_needs_the_published_2_7_5_to_upgrade_in_place(platform):
    """ADD offers 2.7.6 to a 2.7.5 device only while that exact release is
    published (HIL_ONLY or AVAILABLE), as for every earlier predecessor."""
    from zk_add import ota
    from zk_add.ota import preview_campaign_scope
    session, release, devices = platform
    if release.version != "2.7.6":
        return
    device = devices[1]
    device.firmware_version = "zone-lite-2.7.5"
    device.ota_image_sha256 = REPAIR_BASELINE_IMAGES["2.7.5"]
    predecessor = ota.FirmwareRelease(
        release_id="zone-lite-2.7.5", version="2.7.5", git_sha="9" * 40, image_sha256="9" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name="hil/2.7.5.bin", manifest_signature="test-signature",
        state="REVOKED", manifest={"application_sha256": REPAIR_BASELINE_IMAGES["2.7.5"]})
    session.add(predecessor)
    session.flush()
    with pytest.raises(ValueError, match="exactly one eligible connector"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    predecessor.state = "HIL_ONLY"
    session.flush()
    scope, _run = start(session, release, device.zone_id)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]


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
    ("zone-lite-2.7.3", False, False), ("zone-lite-2.7.4", False, False), ("zone-lite-2.7.5", False, False),
    ("zone-lite-2.7.6", False, False), ("zone-lite-2.7.6", True, True), ("zone-lite-2.7.0", False, True),
    ("zone-lite-2.6.16", False, True),
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


# ZONE-BLD9-01's K50 (19 October 2026): one reviewed exposure on the exact
# signed 2.7.4 bytes, outside the nationwide 17, behind the exact 2.4.12 bridge.
def k50_identity(**changes):
    from zk_add.zkt274_bld9_k50 import RELEASE_IDENTITY
    fields = dict(zip(("release_id", "version", "git_sha", "image_sha256", "application_sha256"), RELEASE_IDENTITY))
    fields.update(changes)
    return tuple(fields.values())


def test_k50_exposure_is_exact_to_the_signed_2_7_4_bytes_and_list():
    import json
    from pathlib import Path
    from zk_add.ota import _parse_release_hil_targets
    from zk_add.zkt270_scope import BY_ID
    from zk_add.zkt274_bld9_k50 import RELEASE_IDENTITY, TARGET, exposed_targets
    signed = signed_hil_targets()
    exposed = exposed_targets()
    assert exposed == [*signed, TARGET.model_dump()]
    assert _parse_release_hil_targets(RELEASE_IDENTITY, exposed) == [*(row.identity for row in TARGETS), TARGET]
    # The published 17 and any shorter prefix keep their meaning.
    for count in (1, 17):
        assert [row.model_dump() for row in _parse_release_hil_targets(RELEASE_IDENTITY, signed[:count])] == signed[:count]
    other = {"connector_id": "00000000-0000-0000-0000-000000000000", "mac": "00:11:22:33:44:55",
             "terminal_serial": "OTHER"}
    refused = [
        [exposed[1], exposed[0], *exposed[2:]], [*signed[:16], exposed[17], signed[16]],
        [*signed, {**exposed[17], "connector_id": other["connector_id"]}],
        [*signed, {**exposed[17], "mac": other["mac"]}],
        [*signed, {**exposed[17], "terminal_serial": other["terminal_serial"]}],
        [*signed, {**exposed[17], "display_name": "ZONE-BLD9-01"}],
        [*exposed, other], [exposed[17]], [*signed[:16], exposed[17]], [*signed, other],
    ]
    for scope in refused:
        with pytest.raises(ValueError):
            _parse_release_hil_targets(RELEASE_IDENTITY, scope)
    # Only the exact 2.7.4 identity: not another 2.7 image, nor other source or bytes.
    for identity in (*(k50_identity(release_id=f"zone-lite-{version}", version=version)
                       for version in ("2.7.1", "2.7.2", "2.7.3", "2.7.5", "2.7.6")),
                     k50_identity(git_sha="0" * 40), k50_identity(image_sha256="0" * 64),
                     k50_identity(application_sha256="0" * 64), k50_identity(release_id="zone-lite-2.7.4-k50")):
        with pytest.raises(ValueError):
            _parse_release_hil_targets(identity, exposed)
    # The nationwide inventory and its tracked publication list stay at 17.
    root = Path(__file__).resolve().parents[2] / "deploy/add"
    assert len(TARGETS) == len(BY_ID) == len(signed) == 17
    assert TARGET.connector_id not in BY_ID
    assert json.loads((root / "hil-targets-zkt-270.json").read_text()) == signed
    assert json.loads((root / "hil-targets-2.7.4-bld9-k50.json").read_text()) == exposed


@pytest.fixture
def k50(platform, monkeypatch):
    """Release, 2.4.12 bridge and K50 exactly as ADD reported them on 19 October 2026."""
    from zk_add import ota
    from zk_add.hil_2615_cities import SIGNED_BRIDGE_IDENTITIES
    from zk_add.models import Connector, ZKTDevice
    from zk_add.settings import settings
    from zk_add.zkt274_bld9_k50 import FACTORY_PREDECESSOR, RELEASE_IDENTITY, TARGET, ZONE, exposed_targets
    from pathlib import Path
    from sqlalchemy import select
    session, release, devices = platform
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    _release_id, _version, release.git_sha, release.image_sha256, application = RELEASE_IDENTITY
    release.manifest = {**release.manifest, "application_sha256": application, "_hil_targets": exposed_targets()}
    bridge = session.scalar(select(ota.FirmwareRelease).where(ota.FirmwareRelease.version == "2.4.12"))
    _release_id, _version, bridge.git_sha, bridge.image_sha256, application = SIGNED_BRIDGE_IDENTITIES["2.4.12"]
    bridge.manifest = {"application_sha256": application}
    (Path(settings.firmware_store_path) / bridge.storage_name).write_bytes(b"test bridge artifact")
    # A registered journal release activates the fleet offer guard, as in production.
    session.add(ota.FirmwareRelease(
        release_id="zone-lite-2.7.0", version="2.7.0", git_sha="f" * 40, image_sha256="f" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name="hil/writer.bin", manifest_signature="test-signature",
        state="REVOKED", manifest={"application_sha256": "f" * 64}))
    device = Connector(
        connector_id=TARGET.connector_id, hardware_id=TARGET.mac, zone_id=ZONE, zone_name=ZONE,
        device_id="1", display_name=ZONE, firmware_version="zone-lite-2.5.2", connected=True,
        active=True, is_spare=False, ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
        ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="factory",
        ota_image_sha256=FACTORY_PREDECESSOR[1])
    device.zkt_device = ZKTDevice(
        serial=TARGET.terminal_serial, expected_serial=TARGET.terminal_serial,
        confirmed_serial=TARGET.terminal_serial, terminal_binding_state="CONFIRMED")
    session.add(device)
    session.flush()
    return session, release, bridge, device, devices


def start_k50(session, release, zone):
    from zk_add.ota import create_campaign, preview_campaign_scope
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    run = create_campaign(session, release_public_id=release.release_id, zone_id=zone,
                          reason=f"{release.version} K50 HIL", typed_confirmation=release.version,
                          actor="test-admin", scope_token=scope["scope_token"],
                          idempotency_key=f"k50-{release.version}-{zone}")
    return scope, run


def bridged(session, device):
    device.firmware_version = "zone-lite-2.4.12"
    device.ota_running_partition = "ota_0"
    device.ota_image_sha256 = FLEET_BASELINE_IMAGES["2.4.12"]
    session.flush()


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
def test_k50_takes_the_exact_2_4_12_bridge_from_factory_then_2_7_4(k50):
    from zk_add.ota import FirmwareDeployment, _permitted_hil_targets, preview_campaign_scope, resolve_download
    from zk_add.zkt274_bld9_k50 import TARGET, ZONE
    from sqlalchemy import select
    session, release, bridge, device, devices = k50
    assert _permitted_hil_targets(session, release) == [*(row.identity for row in TARGETS), TARGET]
    # 2.7.4 still refuses the factory partition: no qualified rollback image.
    with pytest.raises(ValueError, match="DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=ZONE)
    scope, run = start_k50(session, bridge, ZONE)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
    assignment = offer(session, device)
    assert assignment["version"] == "2.4.12"
    session.flush()
    assert resolve_download(session, assignment["download_url"].rsplit("/", 1)[1])[0] == bridge
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    deployment.status = "SUCCEEDED"
    run.status = "CANCELLED"  # The operator closes the finished bridge campaign.
    bridged(session, device)
    # 2.4.12 in ota_0 is an ordinary qualified 2.7.4 predecessor.
    scope, _run = start_k50(session, release, ZONE)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
    assert offer(session, device)["version"] == "2.7.4"
    # The nationwide targets keep their own scope under the 18-target marker.
    scope, _run = start_k50(session, release, devices[0].zone_id)
    assert [row["connector_id"] for row in scope["eligible"]] == [devices[0].connector_id]


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
@pytest.mark.parametrize("change", ["factory_digest", "factory_version", "serial", "spare",
                                    "bridge_source", "bridge_artifact", "bridge_state"])
def test_k50_bridge_requires_the_exact_device_factory_image_and_signed_bridge(k50, change):
    from zk_add.ota import preview_campaign_scope
    from zk_add.zkt274_bld9_k50 import ZONE
    session, _release, bridge, device, _devices = k50
    if change == "factory_digest":
        device.ota_image_sha256 = FLEET_BASELINE_IMAGES["2.5.2"]
    elif change == "factory_version":
        device.firmware_version = "zone-lite-2.4.12"
    elif change == "serial":
        device.zkt_device.confirmed_serial = "replacement"
    elif change == "spare":
        device.is_spare = True
    elif change == "bridge_source":
        bridge.git_sha = "0" * 40
    elif change == "bridge_artifact":
        bridge.image_sha256 = "0" * 64
    else:
        bridge.state = "HIL_ONLY"
    session.flush()
    expected = {"bridge_source": "exact published signed 2.4.12", "bridge_artifact": "exact published signed 2.4.12",
                "bridge_state": "exact published signed 2.4.12", "serial": "BRIDGE_EXACT_IDENTITY_MISMATCH",
                "spare": "BRIDGE_EXACT_IDENTITY_MISMATCH"}.get(change, "BRIDGE_FACTORY_PREDECESSOR_MISMATCH")
    with pytest.raises(ValueError, match=expected):
        preview_campaign_scope(session, release_public_id=bridge.release_id, zone_id=ZONE)


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
def test_k50_receives_no_bridge_bytes_outside_its_own_zone(k50):
    """An ordinary 2.4.12 campaign elsewhere may list the device; the offer guard pins the zone."""
    from zk_add.ota import FirmwareDeployment
    from sqlalchemy import select
    session, _release, bridge, device, _devices = k50
    device.zone_id = "ZONE-BLD9-02"
    _scope, run = start_k50(session, bridge, device.zone_id)
    assert offer(session, device) is None
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    assert deployment.status == "PENDING" and deployment.error_code == "NATIONWIDE_EXACT_TARGET_REQUIRED"


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
def test_k50_is_a_2_7_4_target_only_in_its_own_zone(k50):
    """Campaign scope and download pin the K50's zone; the 17 still follow identity."""
    from zk_add.ota import preview_campaign_scope, resolve_download
    from zk_add.zkt274_bld9_k50 import ZONE
    session, release, _bridge, device, devices = k50
    bridged(session, device)
    device.zone_id = "ZONE-BLD9-02"
    session.flush()
    with pytest.raises(ValueError, match="target exclusion: HIL_TARGET_ZONE_MISMATCH"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    # A grant issued in ZONE-BLD9-01 does not follow the device to another zone.
    device.zone_id = ZONE
    session.flush()
    _scope, run = start_k50(session, release, ZONE)
    assignment = offer(session, device)
    assert assignment["version"] == "2.7.4"
    session.flush()
    token = assignment["download_url"].rsplit("/", 1)[1]
    device.zone_id = "ZONE-BLD9-02"
    session.flush()
    with pytest.raises(ValueError, match="exact target mismatch"):
        resolve_download(session, token)
    # Nor does a campaign row moved with it, which the scope check would no longer create.
    run.zone_id = "ZONE-BLD9-02"
    session.flush()
    assert offer(session, device) is None
    with pytest.raises(ValueError, match="exact target mismatch"):
        resolve_download(session, token)
    device.zone_id = run.zone_id = ZONE
    session.flush()
    assert resolve_download(session, token)[0] == release
    assert offer(session, device)["version"] == "2.7.4"
    # A nationwide target is unaffected by the K50's zone pin.
    devices[0].zone_id = "ZONE-ELSEWHERE"
    session.flush()
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id="ZONE-ELSEWHERE")
    assert [row["connector_id"] for row in scope["eligible"]] == [devices[0].connector_id]


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
@pytest.mark.parametrize("moved", [False, True])
def test_k50_clock_leniency_only_in_its_own_zone(k50, moved):
    """Signed-request clock recovery treats the K50 as a target only in ZONE-BLD9-01.

    The moved case simulates a campaign row in another zone that the scope
    check would no longer create; HMAC and nonce checks are unaffected.
    """
    from datetime import timedelta
    from zk_add.hil_clock_recovery import trusted_hil_clock_matches
    from zk_add.models import DeviceTelemetry
    from zk_add.time_utils import utc_now
    from zk_add.zkt274_bld9_k50 import ZONE
    session, release, _bridge, device, _devices = k50
    bridged(session, device)
    device.boot_id = "boot-1"
    _scope, run = start_k50(session, release, ZONE)
    if moved:
        device.zone_id = run.zone_id = "ZONE-BLD9-02"
    now = utc_now()
    for ago in (45, 5):
        observed = now - timedelta(seconds=ago)
        session.add(DeviceTelemetry(connector_id=device.id, boot_id="boot-1", created_at=observed, payload={
            "_trusted_envelope_sent_at": (observed - timedelta(hours=2)).isoformat()}))
    session.flush()
    assert trusted_hil_clock_matches(session, connector=device, request_timestamp=now - timedelta(hours=2),
                                     now=now) is (not moved)


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
def test_k50_needs_the_18_target_marker_and_restoring_the_17_withdraws_it(k50):
    """With the published 17 (before apply, or the backup restored) ADD refuses the K50."""
    from zk_add.ota import _permitted_hil_targets, preview_campaign_scope, resolve_download
    from zk_add.zkt274_bld9_k50 import ZONE, exposed_targets
    session, release, _bridge, device, _devices = k50
    bridged(session, device)

    def marker(targets):
        release.manifest = {**release.manifest, "_hil_targets": targets}
        session.flush()

    marker(signed_hil_targets())
    assert _permitted_hil_targets(session, release) == [row.identity for row in TARGETS]
    # Outside the 17 the exclusion is HIL_EXACT_IDENTITY_MISMATCH, reported as no exact target in the zone.
    with pytest.raises(ValueError, match="exact target is not active in this zone"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=ZONE)
    marker(exposed_targets())
    start_k50(session, release, ZONE)
    assignment = offer(session, device)
    assert assignment["version"] == "2.7.4"
    session.flush()
    marker(signed_hil_targets())
    assert offer(session, device) is None
    with pytest.raises(ValueError, match="exact target mismatch"):
        resolve_download(session, assignment["download_url"].rsplit("/", 1)[1])


@pytest.mark.parametrize("platform", ["2.7.4"], indirect=True)
def test_k50_bridge_download_rechecks_the_factory_image(k50):
    from zk_add.ota import resolve_download
    from zk_add.zkt274_bld9_k50 import ZONE
    session, _release, bridge, device, _devices = k50
    start_k50(session, bridge, ZONE)
    assignment = offer(session, device)
    session.flush()
    device.ota_image_sha256 = "0" * 64
    with pytest.raises(ValueError, match="predecessor changed"):
        resolve_download(session, assignment["download_url"].rsplit("/", 1)[1])
