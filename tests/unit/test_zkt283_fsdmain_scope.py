"""ZKT 2.8.3's reviewed HIL scope: the FSDMAIN ESPs (7 November 2026).

Their uFace800 terminals hold 2,500 users; 2.8.2 and earlier keep at most
2048 (USER_SNAPSHOT_TRUNCATED, then a disconnect). None of the FSDMAIN ESPs is
one of the nationwide 17, so 2.8.3 may be published HIL_ONLY to an exact
ordered prefix of their pins. Owner-approved exception: for this scope only, a
target also matches while its terminal binding is PENDING_DEVICE_ACK (2.8.1
never stays connected long enough to acknowledge PIN_TERMINAL_SERIAL).
Connector IDs here are synthetic."""
import hashlib
import json
from pathlib import Path
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.hil_scope import target_matches
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, RECORD49_BASELINE_IMAGES, platform_baselines
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt282_lhrcentral import PINS as LHRCENTRAL_PINS
from zk_add.zkt283_fsdmain import MAX_TARGETS, PINS, RELEASE_ID, VERSION, admits, exposure, matches

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = "e" * 64
APPLICATION_2_8_1 = "21238fa36df844642545e8c2f6bcfec03be37470890b776a600c39c0cdd1f5f0"
CONNECTORS = ("0c1e0101-0000-4000-8000-000000000001", "0c1e0102-0000-4000-8000-000000000002",
              "0c1e0103-0000-4000-8000-000000000003")
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


def test_pins_are_the_reviewed_fsdmain_esps():
    assert (RELEASE_ID, VERSION, MAX_TARGETS) == ("zone-lite-2.8.3", "2.8.3", 3)
    assert [(pin.name, pin.mac, pin.terminal_serial) for pin in PINS] == [
        ("FSDMAIN-01", "30:30:f9:17:87:2c", "CKPG231060360"),
        ("FSDMAIN-02", "8c:bf:ea:17:95:d0", "AF4C204260255"),
        ("FSDMAIN-03", "30:30:f9:1f:90:10", "AF4C225060506"),
    ]
    # The release pipeline pins the same ESPs (firmware-2-8-3-hil-scope.ps1,
    # firmware-hil-candidate.yml).
    pins = json.loads((ROOT / "deploy/add/hil-pins-2.8.3-fsdmain.json").read_text())
    assert pins == [pin._asdict() for pin in PINS]
    # None is one of the nationwide 17 or the LHRCENTRAL ESPs, whose lists stay unchanged.
    others = {target.identity.mac for target in TARGETS} | {target.identity.terminal_serial for target in TARGETS}
    others |= {value for pin in LHRCENTRAL_PINS for value in (pin.mac, pin.terminal_serial)}
    assert not others & {value for pin in PINS for value in (pin.mac, pin.terminal_serial)}
    assert len(TARGETS) == len(signed_hil_targets()) == 17
    # 2.8.3 keeps exactly 2.8.2's predecessors (option A): 2.8.2 is not one.
    assert platform_baselines(VERSION) == platform_baselines("2.8.2")
    assert "2.8.2" not in platform_baselines(VERSION)[0]


def test_scope_is_an_exact_ordered_prefix_of_the_pins():
    from zk_add.ota import _parse_release_hil_targets
    for count in range(1, len(PINS) + 1):
        assert [target.model_dump() for target in _parse_release_hil_targets(IDENTITY, SCOPE[:count])] == SCOPE[:count]
        assert [target.model_dump() for target in exposure(IDENTITY, SCOPE[:count])] == SCOPE[:count]
    other = {"connector_id": "0c1e0109-0000-4000-8000-000000000009", "mac": "00:11:22:33:44:55",
             "terminal_serial": "CKPG231060999"}
    refused = [
        [SCOPE[1], SCOPE[0]], [*SCOPE, other], [*SCOPE, SCOPE[0]],
        [SCOPE[0], {**SCOPE[1], "terminal_serial": PINS[0].terminal_serial}],
        [SCOPE[0], {**SCOPE[1], "mac": other["mac"]}],
        [{**SCOPE[0], "terminal_serial": other["terminal_serial"]}],
        [{**SCOPE[0], "connector_id": CONNECTORS[0].upper()}],
        [{**SCOPE[0], "connector_id": "FSDMAIN-01"}],
        [SCOPE[0], {**SCOPE[1], "connector_id": CONNECTORS[0]}],
        [{**SCOPE[0], "zone_id": "LF-ZONE-FSDMAIN-01"}],
        [{key: value for key, value in SCOPE[0].items() if key != "connector_id"}],
        [*signed_hil_targets()[:1], SCOPE[0]],
    ]
    for scope in refused:
        with pytest.raises(ValueError):
            _parse_release_hil_targets(IDENTITY, scope)
    # A list that does not start with FSDMAIN-01 is not this scope; 2.8.3
    # keeps the nationwide prefix rule of every 2.7 platform image.
    assert exposure(IDENTITY, signed_hil_targets()[:2]) is None
    assert ([target.model_dump() for target in _parse_release_hil_targets(IDENTITY, signed_hil_targets()[:2])]
            == signed_hil_targets()[:2])
    for scope in (SCOPE[1:], [{**SCOPE[0], "mac": SCOPE[0]["mac"].upper()}], []):
        with pytest.raises(ValueError):
            _parse_release_hil_targets(IDENTITY, scope)


def test_scope_never_exceeds_three_targets(monkeypatch):
    """FSDMAIN-03 is a one-line pin addition; a fourth pin still exposes at most three."""
    from zk_add import zkt283_fsdmain
    extra = [zkt283_fsdmain.Pin(f"FSDMAIN-0{index}", f"30:30:f9:17:87:0{index}", f"CKPG23106036{index}")
             for index in (3, 4)]
    monkeypatch.setattr(zkt283_fsdmain, "PINS", (*PINS, *extra))
    rows = [*SCOPE, *({"connector_id": f"0c1e010{index}-0000-4000-8000-00000000000{index}", "mac": pin.mac,
                       "terminal_serial": pin.terminal_serial} for index, pin in zip((3, 4), extra))]
    assert [target.model_dump() for target in exposure(IDENTITY, rows[:3])] == rows[:3]
    with pytest.raises(ValueError, match="at most three"):
        exposure(IDENTITY, rows)


@pytest.mark.parametrize("version", ["2.7.8", "2.8.0", "2.8.1", "2.8.2"])
def test_no_other_image_may_reach_fsdmain_as_hil(version):
    from zk_add.ota import _parse_release_hil_targets
    identity = (f"zone-lite-{version}", version, *IDENTITY[2:])
    assert exposure(identity, SCOPE) is None
    with pytest.raises(ValueError):
        _parse_release_hil_targets(identity, SCOPE[:1])
    # Nor another release ID for the 2.8.3 version.
    assert exposure(("zone-lite-2.8.3-fsd", VERSION, *IDENTITY[2:]), SCOPE) is None


def release_row(ota, tmp_path, version, state, application, **fields):
    row = ota.FirmwareRelease(
        release_id=f"zone-lite-{version}", version=version, git_sha="c" * 40, image_sha256=hashlib.sha256(version.encode()).hexdigest(),
        image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name=f"hil/{version}.bin",
        manifest_signature="test-signature", state=state, manifest={"application_sha256": application},
        **fields)
    (tmp_path / row.storage_name).write_bytes(b"test artifact " + version.encode())
    return row


@pytest.fixture
def fsdmain(monkeypatch, tmp_path):
    """The FSDMAIN ESPs on the exact AVAILABLE 2.8.1 in ota_0, their terminal
    bindings PENDING_DEVICE_ACK, the HIL_ONLY 2.8.3 published to them, and one
    of the 17 on 2.8.1."""
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
    assert RECORD49_BASELINE_IMAGES["2.8.1"] == APPLICATION_2_8_1

    with Session(engine) as session:
        session.add(release_row(ota, tmp_path, "2.5.2", "AVAILABLE", FLEET_BASELINE_IMAGES["2.5.2"]))
        session.add(release_row(ota, tmp_path, "2.8.1", "AVAILABLE", APPLICATION_2_8_1))
        # A registered journal release activates the fleet offer guard, as in production.
        session.add(release_row(ota, tmp_path, "2.7.0", "REVOKED", "f" * 64))
        candidate = release_row(ota, tmp_path, VERSION, "HIL_ONLY", APPLICATION)
        candidate.git_sha, candidate.image_sha256 = "a" * 40, "b" * 64
        candidate.manifest = {**manifest(VERSION), "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                              "_hil_targets": list(SCOPE)}
        session.add(candidate)
        devices = []
        for connector_id, pin in zip(CONNECTORS, PINS):
            zone = f"LF-ZONE-{pin.name}"
            device = Connector(
                connector_id=connector_id, hardware_id=pin.mac, zone_id=zone, zone_name=zone, device_id="1",
                display_name=pin.name, firmware_family="zkt", firmware_version="zone-lite-2.8.1", connected=True,
                active=True, is_spare=False, ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
                ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_0",
                ota_image_sha256=APPLICATION_2_8_1)
            device.zkt_device = ZKTDevice(
                serial=pin.terminal_serial, expected_serial=pin.terminal_serial,
                confirmed_serial=pin.terminal_serial, terminal_binding_state="PENDING_DEVICE_ACK",
                model="uFace800")
            session.add(device)
            devices.append(device)
        swat = TARGETS[1].identity
        other = Connector(
            connector_id=swat.connector_id, hardware_id=swat.mac, zone_id="ZONE-SWAT-01", zone_name="ZONE-SWAT-01",
            device_id="1", display_name="Swat 01", firmware_family="zkt", firmware_version="zone-lite-2.8.1",
            connected=True, active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
            ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_1",
            ota_image_sha256=APPLICATION_2_8_1)
        other.zkt_device = ZKTDevice(serial=swat.terminal_serial, expected_serial=swat.terminal_serial,
                                     confirmed_serial=swat.terminal_serial, terminal_binding_state="PENDING_DEVICE_ACK")
        session.add(other)
        session.flush()
        yield session, candidate, devices, other
    engine.dispose()


def start(session, release, zone):
    from zk_add.ota import create_campaign, preview_campaign_scope
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    run = create_campaign(session, release_public_id=release.release_id, zone_id=zone,
                          reason="2.8.3 FSDMAIN HIL", typed_confirmation=release.version, actor="test-admin",
                          scope_token=scope["scope_token"], idempotency_key=f"fsdmain-{zone}")
    return scope, run


def offer(session, device):
    from zk_add.ota import assignment_for_connector
    return assignment_for_connector(session, connector=device, public_base="https://test.invalid")


@pytest.mark.parametrize("binding", ["PENDING_DEVICE_ACK", "CONFIRMED"])
def test_each_fsdmain_esp_takes_2_8_3_straight_from_2_8_1(fsdmain, binding):
    """Campaign preview, creation, the fleet offer guard, the offer and the
    download grant all admit an exact pin, its binding confirmed or still
    awaiting the device ACK."""
    from zk_add.ota import _permitted_hil_targets, resolve_download
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = fsdmain
    assert [target.model_dump() for target in _permitted_hil_targets(session, release)] == SCOPE
    for device in devices:
        device.zkt_device.terminal_binding_state = binding
    session.flush()
    for device in devices:
        assert admits(device, release) and pending_offer_hold(session, device, release) is None
        scope, run = start(session, release, device.zone_id)
        assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
        assert run.eligible_count == 1
    # Each starts independently; no fleet-wide limit.
    for device in devices:
        assignment = offer(session, device)
        assert assignment["version"] == "2.8.3"
        session.flush()
        assert resolve_download(session, assignment["download_url"].rsplit("/", 1)[1])[0] == release


def test_the_pending_ack_exception_is_this_scope_only(fsdmain):
    """hil_scope.target_matches is unchanged: a pending binding matches only
    through zkt283_fsdmain, and only for an exposed pin of the HIL_ONLY 2.8.3."""
    from zk_add.ota import _hil_target_admits, _permitted_hil_targets
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, other = fsdmain
    targets = _permitted_hil_targets(session, release)
    for target, device in zip(targets, devices):
        assert not target_matches(target, device)
        assert matches(target, device) and _hil_target_admits(release, target, device)
    # One of the 17 whose binding awaits its ACK is not a target of this publication.
    assert not admits(other, release)
    assert pending_offer_hold(session, other, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"


@pytest.mark.parametrize("change", ["connector", "mac", "serial", "confirmed_serial", "expected_serial",
                                    "serial_confirmation_required", "recovery_expected_serial", "spare",
                                    "inactive"])
def test_an_fsdmain_esp_needs_its_exact_identity(fsdmain, change):
    from zk_add.ota import preview_campaign_scope
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = fsdmain
    device = devices[0]
    terminal = device.zkt_device
    if change == "connector":
        # The marker names another connector for this ESP.
        release.manifest = {**release.manifest, "_hil_targets": [
            {**SCOPE[0], "connector_id": "0c1e0109-0000-4000-8000-000000000009"}, *SCOPE[1:]]}
    elif change == "mac":
        device.hardware_id = "00:11:22:33:44:55"
    elif change in {"serial", "confirmed_serial", "expected_serial"}:
        setattr(terminal, change, "CKPG231060999")
    elif change == "serial_confirmation_required":
        terminal.terminal_binding_state = "SERIAL_CONFIRMATION_REQUIRED"
    elif change == "recovery_expected_serial":
        terminal.terminal_binding_state = "RECOVERY_EXPECTED_SERIAL"
    elif change == "spare":
        device.is_spare = True
    else:
        device.active = False
    session.flush()
    assert not admits(device, release)
    assert pending_offer_hold(session, device, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
    with pytest.raises(ValueError):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)


def test_a_shorter_prefix_leaves_the_rest_outside(fsdmain):
    from zk_add.ota import preview_campaign_scope
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = fsdmain
    release.manifest = {**release.manifest, "_hil_targets": SCOPE[:1]}
    session.flush()
    for device in devices[1:]:
        assert not admits(device, release)
        assert pending_offer_hold(session, device, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
        with pytest.raises(ValueError, match="exactly one eligible connector"):
            preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    scope, _run = start(session, release, devices[0].zone_id)
    assert [row["connector_id"] for row in scope["eligible"]] == [devices[0].connector_id]


def test_no_other_hil_only_image_takes_the_exception(fsdmain, tmp_path):
    """2.8.2 (and every other platform image) keeps the nationwide 17 and the
    CONFIRMED binding: an FSDMAIN ESP pending its ACK is never its target."""
    from zk_add import ota
    from zk_add.zkt282_lhrcentral import admits as lhrcentral_admits
    from zk_add.zkt_ota_admission import pending_offer_hold
    session, release, devices, _other = fsdmain
    previous = release_row(ota, tmp_path, "2.8.2", "HIL_ONLY", "d" * 64)
    previous.manifest = {**manifest("2.8.2"), "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                         "_hil_targets": list(SCOPE)}
    session.add(previous)
    session.flush()
    for device in devices:
        assert not admits(device, previous) and not lhrcentral_admits(device, previous)
        assert pending_offer_hold(session, device, previous) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
    with pytest.raises(ValueError):
        ota._permitted_hil_targets(session, previous)
    # Nor does the AVAILABLE or a revoked 2.8.3 use the HIL exception.
    for state in ("AVAILABLE", "REVOKED"):
        release.state = state
        assert not any(admits(device, release) for device in devices)


@pytest.mark.parametrize("case", ["pin", "confirmed", "serial_confirmation_required", "release_2_8_2", "not_a_pin"])
def test_clock_recovery_admits_a_pending_ack_pin_of_2_8_3_only(fsdmain, tmp_path, case):
    """Bounded clock recovery for a booted 2.8.3 uses the same exception: an
    exact FSDMAIN pin awaiting its device ACK, and nothing wider."""
    from datetime import timedelta
    from zk_add import ota
    from zk_add.hil_clock_recovery import trusted_hil_clock_matches
    from zk_add.models import DeviceTelemetry
    from zk_add.time_utils import utc_now
    session, release, devices, _other = fsdmain
    device = devices[0]
    _scope, run = start(session, release, device.zone_id)
    deployment = session.query(ota.FirmwareDeployment).filter_by(campaign_id=run.id).one()
    deployment.status = "BOOTED_PENDING"
    device.firmware_version, device.ota_image_sha256 = "zone-lite-2.8.3", APPLICATION
    device.boot_id = "boot-283"
    if case == "confirmed":
        device.zkt_device.terminal_binding_state = "CONFIRMED"
    elif case == "serial_confirmation_required":
        device.zkt_device.terminal_binding_state = "SERIAL_CONFIRMATION_REQUIRED"
    elif case == "release_2_8_2":
        # The same marker on a 2.8.2 version never takes the exception.
        release.version = "2.8.2"
        device.firmware_version = "zone-lite-2.8.2"
    elif case == "not_a_pin":
        device.hardware_id = "00:11:22:33:44:55"
    now = utc_now()
    for ago in (45, 5):
        observed = now - timedelta(seconds=ago)
        session.add(DeviceTelemetry(connector_id=device.id, boot_id="boot-283", created_at=observed, payload={
            "_trusted_envelope_sent_at": (observed - timedelta(hours=2)).isoformat()}))
    session.flush()
    assert trusted_hil_clock_matches(session, connector=device, request_timestamp=now - timedelta(hours=2),
                                     now=now) is (case in {"pin", "confirmed"})
