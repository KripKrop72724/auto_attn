"""ZKT 2.8.2's reviewed HIL scope: the three LHRCENTRAL ESPs (4 November 2026).

Their MB40-VL/ID terminals (ZAM180_TFT) return 49-byte attendance records,
which 2.8.1 and earlier refuse. None of them is one of the nationwide 17, so
2.8.2 may be published HIL_ONLY to an exact ordered prefix of the three pins:
ESP hardware ID and terminal serial, with the connector ID ADD shows for
that ESP. Connector IDs here are synthetic."""
import json
from pathlib import Path
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt282_lhrcentral import PINS, RELEASE_ID, VERSION, admits, exposure

APPLICATION = "e" * 64
APPLICATION_2_7_8 = "b8d0ca7e20d28d4114cff56fc69676e681a37bf9f7e831a243a65f9a1a972a3e"
CONNECTORS = ("0c1e0001-0000-4000-8000-000000000001", "0c1e0002-0000-4000-8000-000000000002",
              "0c1e0003-0000-4000-8000-000000000003")
SCOPE = [{"connector_id": connector, "mac": pin.mac, "terminal_serial": pin.terminal_serial}
         for connector, pin in zip(CONNECTORS, PINS)]
IDENTITY = (RELEASE_ID, VERSION, "a" * 40, "b" * 64, APPLICATION)


def manifest(version):
    baselines, images = platform_baselines(version)
    return {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(baselines),
                          "allowed_bootstrap_images": dict(images)},
        "release_id": f"zone-lite-{version}", "version": version,
    }


def test_pins_are_the_three_reviewed_lhrcentral_esps():
    assert (RELEASE_ID, VERSION) == ("zone-lite-2.8.2", "2.8.2")
    assert [(pin.name, pin.mac, pin.terminal_serial) for pin in PINS] == [
        ("LHRCENTRAL-01", "ac:27:6e:a3:44:68", "COVP232060159"),
        ("LHRCENTRAL-02", "14:c1:9f:46:97:d0", "COVP232060174"),
        ("LHRCENTRAL-03", "a4:cb:8f:d4:55:28", "COVP232060177"),
    ]
    # The release pipeline pins the same three (firmware-2-8-2-hil-scope.ps1,
    # firmware-hil-candidate.yml).
    pins = json.loads((Path(__file__).resolve().parents[2] / "deploy/add/hil-pins-2.8.2-lhrcentral.json").read_text())
    assert pins == [pin._asdict() for pin in PINS]
    # None is one of the nationwide 17, whose inventory stays unchanged.
    nationwide = {target.identity.mac for target in TARGETS} | {target.identity.terminal_serial for target in TARGETS}
    assert not nationwide & {value for pin in PINS for value in (pin.mac, pin.terminal_serial)}
    assert len(TARGETS) == len(signed_hil_targets()) == 17


def test_scope_is_an_exact_ordered_prefix_of_the_three_pins():
    from zk_add.ota import _parse_release_hil_targets
    for count in (1, 2, 3):
        assert [target.model_dump() for target in _parse_release_hil_targets(IDENTITY, SCOPE[:count])] == SCOPE[:count]
        assert [target.model_dump() for target in exposure(IDENTITY, SCOPE[:count])] == SCOPE[:count]
    other = {"connector_id": "0c1e0009-0000-4000-8000-000000000009", "mac": "00:11:22:33:44:55",
             "terminal_serial": "COVP232060999"}
    refused = [
        [SCOPE[0], SCOPE[2]], [SCOPE[0], SCOPE[2], SCOPE[1]], [*SCOPE, other], [*SCOPE, SCOPE[0]],
        [SCOPE[0], {**SCOPE[1], "terminal_serial": PINS[2].terminal_serial}],
        [SCOPE[0], {**SCOPE[1], "mac": PINS[2].mac}],
        [{**SCOPE[0], "terminal_serial": "COVP232060999"}],
        [{**SCOPE[0], "connector_id": CONNECTORS[0].upper()}],
        [{**SCOPE[0], "connector_id": "LHRCENTRAL-01"}],
        [SCOPE[0], {**SCOPE[1], "connector_id": CONNECTORS[0]}],
        [{**SCOPE[0], "zone_id": "LF-ZONE-LHRCENTRAL-01"}],
        [{key: value for key, value in SCOPE[0].items() if key != "connector_id"}],
        [*signed_hil_targets()[:1], SCOPE[0]],
    ]
    for scope in refused:
        with pytest.raises(ValueError):
            _parse_release_hil_targets(IDENTITY, scope)
    # A list that does not start with LHRCENTRAL-01 is not this scope; 2.8.2
    # keeps the nationwide prefix rule of every 2.7 platform image.
    assert exposure(IDENTITY, signed_hil_targets()[:2]) is None
    assert ([target.model_dump() for target in _parse_release_hil_targets(IDENTITY, signed_hil_targets()[:2])]
            == signed_hil_targets()[:2])
    for scope in (SCOPE[1:], [SCOPE[1], SCOPE[0]], [{**SCOPE[0], "mac": SCOPE[0]["mac"].upper()}], []):
        with pytest.raises(ValueError):
            _parse_release_hil_targets(IDENTITY, scope)


@pytest.mark.parametrize("version", ["2.7.8", "2.8.0", "2.8.1"])
def test_no_other_image_may_reach_lhrcentral_as_hil(version):
    from zk_add.ota import _parse_release_hil_targets
    identity = (f"zone-lite-{version}", version, *IDENTITY[2:])
    assert exposure(identity, SCOPE) is None
    with pytest.raises(ValueError):
        _parse_release_hil_targets(identity, SCOPE[:1])
    # Nor another release ID for the 2.8.2 version.
    assert exposure(("zone-lite-2.8.2-lhr", VERSION, *IDENTITY[2:]), SCOPE) is None


@pytest.fixture
def lhrcentral(monkeypatch, tmp_path):
    """The three LHRCENTRAL ESPs on the exact production 2.7.8 in ota_0 (2.5.2 in
    ota_1), the HIL_ONLY 2.8.2 published to all three, and one of the 17 on 2.7.8."""
    from zk_add import ota
    from zk_add.models import Base, Connector, ZKTDevice
    from zk_add.ota import HIL_269_EXACT_TARGETS
    from zk_add.settings import settings

    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_hil_targets_json",
                        json.dumps([row.model_dump() for row in HIL_269_EXACT_TARGETS]))
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    (tmp_path / "hil").mkdir()

    def release(version, state, application, **fields):
        row = ota.FirmwareRelease(
            release_id=f"zone-lite-{version}", version=version, git_sha="c" * 40, image_sha256=version[-1] * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name=f"hil/{version}.bin",
            manifest_signature="test-signature", state=state, manifest={"application_sha256": application},
            **fields)
        session.add(row)
        (tmp_path / row.storage_name).write_bytes(b"test artifact " + version.encode())
        return row

    with Session(engine) as session:
        release("2.5.2", "AVAILABLE", FLEET_BASELINE_IMAGES["2.5.2"])
        release("2.7.8", "AVAILABLE", APPLICATION_2_7_8)
        # A registered journal release activates the fleet offer guard, as in production.
        release("2.7.0", "REVOKED", "f" * 64)
        signed = manifest(VERSION)
        candidate = release(VERSION, "HIL_ONLY", APPLICATION)
        candidate.git_sha, candidate.image_sha256 = "a" * 40, "b" * 64
        candidate.manifest = {**signed, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                              "_hil_targets": list(SCOPE)}
        devices = []
        for index, (connector_id, pin) in enumerate(zip(CONNECTORS, PINS)):
            zone = f"LF-ZONE-{pin.name}"
            device = Connector(
                connector_id=connector_id, hardware_id=pin.mac, zone_id=zone, zone_name=zone, device_id="1",
                display_name=pin.name, firmware_family="zkt", firmware_version="zone-lite-2.7.8", connected=True,
                active=True, is_spare=False, ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
                ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_0",
                ota_image_sha256=APPLICATION_2_7_8)
            device.zkt_device = ZKTDevice(
                serial=pin.terminal_serial, expected_serial=pin.terminal_serial,
                confirmed_serial=pin.terminal_serial, terminal_binding_state="CONFIRMED",
                model="MB40-VL/ID", platform="ZAM180_TFT")
            session.add(device)
            devices.append(device)
        swat = TARGETS[1].identity
        other = Connector(
            connector_id=swat.connector_id, hardware_id=swat.mac, zone_id="ZONE-SWAT-01", zone_name="ZONE-SWAT-01",
            device_id="1", display_name="Swat 01", firmware_family="zkt", firmware_version="zone-lite-2.7.8",
            connected=True, active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
            ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_1",
            ota_image_sha256=APPLICATION_2_7_8)
        other.zkt_device = ZKTDevice(serial=swat.terminal_serial, expected_serial=swat.terminal_serial,
                                     confirmed_serial=swat.terminal_serial, terminal_binding_state="CONFIRMED")
        session.add(other)
        session.flush()
        yield session, candidate, devices, other
    engine.dispose()


def start(session, release, zone):
    from zk_add.ota import create_campaign, preview_campaign_scope
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    run = create_campaign(session, release_public_id=release.release_id, zone_id=zone,
                          reason="2.8.2 LHRCENTRAL HIL", typed_confirmation=release.version, actor="test-admin",
                          scope_token=scope["scope_token"], idempotency_key=f"lhrcentral-{zone}")
    return scope, run


def offer(session, device):
    from zk_add.ota import assignment_for_connector
    return assignment_for_connector(session, connector=device, public_base="https://test.invalid")


def test_each_lhrcentral_esp_takes_2_8_2_straight_from_2_7_8(lhrcentral):
    from zk_add.ota import _permitted_hil_targets
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = lhrcentral
    assert [target.model_dump() for target in _permitted_hil_targets(session, release)] == SCOPE
    for device in devices:
        assert admits(device, release) and pending_offer_hold(session, device, release) is None
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert run.eligible_count == 1
    # Each starts independently; no fleet-wide limit.
    for device in devices:
        assert offer(session, device)["version"] == "2.8.2"


def test_2_8_2_never_reaches_an_esp_its_marker_does_not_name(lhrcentral):
    from zk_add.ota import preview_campaign_scope
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, other = lhrcentral
    # One of the 17 is not a target of the LHRCENTRAL publication.
    assert not admits(other, release)
    with pytest.raises(ValueError, match="exactly one eligible connector"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=other.zone_id)
    # A shorter prefix leaves LHRCENTRAL-02 and -03 outside the scope and the offer guard.
    release.manifest = {**release.manifest, "_hil_targets": SCOPE[:1]}
    session.flush()
    for device in devices[1:]:
        assert not admits(device, release)
        assert pending_offer_hold(session, device, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    scope, _run = start(session, release, devices[0].zone_id)
    assert [row["connector_id"] for row in scope["eligible"]] == [devices[0].connector_id]


@pytest.mark.parametrize("change", ["connector", "mac", "serial", "unbound", "spare", "inactive"])
def test_an_lhrcentral_esp_needs_its_exact_identity(lhrcentral, change):
    from zk_add.ota import preview_campaign_scope
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = lhrcentral
    device = devices[0]
    if change == "connector":
        # The marker names another connector for this ESP.
        release.manifest = {**release.manifest, "_hil_targets": [
            {**SCOPE[0], "connector_id": "0c1e0009-0000-4000-8000-000000000009"}, *SCOPE[1:]]}
    elif change == "mac":
        device.hardware_id = "00:11:22:33:44:55"
    elif change == "serial":
        device.zkt_device.confirmed_serial = "COVP232060999"
    elif change == "unbound":
        device.zkt_device.terminal_binding_state = "PENDING_DEVICE_ACK"
    elif change == "spare":
        device.is_spare = True
    else:
        device.active = False
    session.flush()
    assert not admits(device, release)
    assert pending_offer_hold(session, device, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
    with pytest.raises(ValueError):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_no_other_hil_only_image_reaches_lhrcentral(lhrcentral):
    """2.8.1 (and every other platform image) keeps the nationwide 17: an LHRCENTRAL
    ESP waits for the AVAILABLE one, as every ESP added after the 17."""
    from zk_add import ota
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = lhrcentral
    previous = ota.FirmwareRelease(
        release_id="zone-lite-2.8.1", version="2.8.1", git_sha="d" * 40, image_sha256="d" * 64, image_size=1024,
        signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT, minimum_bootstrap_version="2.4.12",
        storage_name="hil/2.8.1.bin", manifest_signature="test-signature", state="HIL_ONLY",
        manifest={**manifest("2.8.1"), "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                  "_hil_targets": list(SCOPE)})
    session.add(previous)
    session.flush()
    for device in devices:
        assert not admits(device, previous)
        assert pending_offer_hold(session, device, previous) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
    with pytest.raises(ValueError):
        ota._permitted_hil_targets(session, previous)
    # A revoked 2.8.2 admits no one.
    release.state = "REVOKED"
    assert not any(admits(device, release) for device in devices)
