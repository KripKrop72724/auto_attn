import json
from pathlib import Path

import pytest
from sqlalchemy import select

from test_hil_scope import bld5_2615_session, hil_session, parallel_269_session  # noqa: F401
from zk_add.hil_2615_cities import CITY_FACTORY_PREDECESSORS, CITY_TARGETS, SIGNED_BRIDGE_IDENTITIES
from zk_add.hil_scope import parse_hil_targets
from zk_add.models import Connector, ZKTDevice
from zk_add.ota import (
    FirmwareEvent, FirmwareRelease, HIL_2615_BLD5_IDENTITY, HIL_2615_CITY_TARGETS,
    _parse_release_hil_targets, _permitted_hil_targets, assignment_for_connector,
    create_campaign, preview_campaign_scope, resolve_download,
)


@pytest.fixture(name="city_session")
def city_fixture(request, monkeypatch):
    from zk_add.settings import settings

    session, release, devices, zones = request.getfixturevalue("bld5_2615_session")
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    release.manifest = {**release.manifest, "_hil_targets": [
        row.model_dump() for row in HIL_2615_CITY_TARGETS
    ]}
    mapped = dict(zip(zones, devices))
    for zone, target in CITY_TARGETS.items():
        device = Connector(
            connector_id=target.connector_id, hardware_id=target.mac,
            zone_id=zone, zone_name=zone, device_id="1", display_name=zone,
            connected=True, active=True, is_spare=False, firmware_version="zone-lite-2.5.2",
            ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
            ota_partition_layout="zone-lite-ota-v1", ota_running_partition="ota_0",
            ota_image_sha256=SIGNED_BRIDGE_IDENTITIES["2.5.2"][4],
        )
        device.zkt_device = ZKTDevice(
            serial=target.terminal_serial, expected_serial=target.terminal_serial,
            confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED",
        )
        session.add(device)
        mapped[zone] = device
    for identity in SIGNED_BRIDGE_IDENTITIES.values():
        baseline = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == identity[0]))
        if baseline is None:
            baseline = FirmwareRelease(
                image_size=1024, signing_key_id="production-key", partition_layout="zone-lite-ota-v1",
                minimum_bootstrap_version="2.2.0", storage_name=f"hil/{identity[1]}.bin",
                manifest_signature="test-signature", state="AVAILABLE",
            )
            session.add(baseline)
        baseline.release_id, baseline.version, baseline.git_sha, baseline.image_sha256, application = identity
        baseline.manifest = {"application_sha256": application}
        # Fixture artifact, never used as deployable firmware.
        baseline.storage_name = f"hil/{identity[1]}.bin"
        (Path(settings.firmware_store_path) / baseline.storage_name).write_bytes(b"test bridge artifact")
    session.flush()
    return session, release, mapped


def start(session, release, zone):
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    run = create_campaign(
        session, release_public_id=release.release_id, zone_id=zone,
        reason="Requested exact city HIL", typed_confirmation=release.version,
        actor="test-admin", scope_token=scope["scope_token"], idempotency_key=f"city-{zone}",
    )
    assert run.eligible_count == 1
    return scope


def test_all_ten_city_devices_can_start_independent_hil_without_fabricating_acceptance(city_session):
    session, release, devices = city_session
    assert _permitted_hil_targets(session, release) == list(HIL_2615_CITY_TARGETS)
    for zone in ["ZONE-PESHAWAR-06", *CITY_TARGETS, "ZONE-PESHAWAR-02"]:
        scope = start(session, release, zone)
        assert [row["connector_id"] for row in scope["eligible"]] == [devices[zone].connector_id]
        assert assignment_for_connector(session, connector=devices[zone], public_base="https://test.invalid")
    assert session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "HIL_ACCEPTED")) is None
    assert release.state == "HIL_ONLY"


@pytest.mark.parametrize("changed", ["source", "artifact", "application", "order", "serial", "extra", "missing"])
def test_fourteen_device_exception_requires_exact_signed_identity_and_scope(city_session, changed):
    session, release, _devices = city_session
    rows = [dict(row) for row in release.manifest["_hil_targets"]]
    if changed == "source":
        release.git_sha = "0" * 40
    elif changed == "artifact":
        release.image_sha256 = "0" * 64
    elif changed == "application":
        release.manifest = {**release.manifest, "application_sha256": "0" * 64}
    elif changed == "order":
        rows[6], rows[7] = rows[7], rows[6]
    elif changed == "serial":
        rows[6]["terminal_serial"] = "replacement"
    elif changed == "extra":
        rows[6]["display_name"] = "Faisalabad"
    else:
        rows.pop()
    release.manifest = {**release.manifest, "_hil_targets": rows}
    with pytest.raises(ValueError):
        _permitted_hil_targets(session, release)


def test_general_scope_limit_and_original_prefix_are_preserved():
    root = Path(__file__).resolve().parents[2] / "deploy/add"
    rows = json.loads((root / "hil-targets-2.6.15-cities.json").read_text())
    original = json.loads((root / "hil-targets-2.6.15-bld5.json").read_text())
    assert rows[:6] == original
    assert _parse_release_hil_targets(HIL_2615_BLD5_IDENTITY, rows) == list(HIL_2615_CITY_TARGETS)
    with pytest.raises(ValueError):
        parse_hil_targets(rows)


@pytest.mark.parametrize("changed", ["spare", "inactive", "serial", "pending_ack"])
def test_city_campaign_and_download_recheck_active_nonspare_confirmed_identity(city_session, changed):
    session, release, devices = city_session
    device = devices["ZONE-KARACHI-01"]
    scope = start(session, release, device.zone_id)
    offer = assignment_for_connector(session, connector=device, public_base="https://test.invalid")
    grant = offer["download_url"].rsplit("/", 1)[1]
    session.flush()
    assert resolve_download(session, grant)[0] == release
    if changed == "spare":
        device.is_spare = True
    elif changed == "inactive":
        device.active = False
    elif changed == "serial":
        device.zkt_device.confirmed_serial = "replacement"
    else:
        device.zkt_device.terminal_binding_state = "PENDING_DEVICE_ACK"
    assert assignment_for_connector(session, connector=device, public_base="https://test.invalid") is None
    with pytest.raises(ValueError, match="exact target mismatch"):
        resolve_download(session, grant)
    assert scope["counts"]["eligible"] == 1


@pytest.mark.parametrize("zone", CITY_FACTORY_PREDECESSORS)
def test_exact_factory_bridges_do_not_bypass_2615_predecessor_or_changed_digest(city_session, zone):
    session, release, devices = city_session
    device = devices[zone]
    predecessor, digest, bridge_version = CITY_FACTORY_PREDECESSORS[zone]
    device.firmware_version = f"zone-lite-{predecessor}"
    device.ota_running_partition = "factory"
    device.ota_image_sha256 = digest
    with pytest.raises(ValueError, match="DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zone)
    bridge = session.scalar(select(FirmwareRelease).where(
        FirmwareRelease.release_id == SIGNED_BRIDGE_IDENTITIES[bridge_version][0],
    ))
    scope = start(session, bridge, zone)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
    offer = assignment_for_connector(session, connector=device, public_base="https://test.invalid")
    session.flush()
    grant = offer["download_url"].rsplit("/", 1)[1]
    assert resolve_download(session, grant)[0] == bridge
    device.ota_image_sha256 = "0" * 64
    assert assignment_for_connector(session, connector=device, public_base="https://test.invalid") is None
    with pytest.raises(ValueError, match="predecessor changed"):
        resolve_download(session, grant)


@pytest.mark.parametrize("changed", ["source", "artifact", "spare", "serial"])
def test_factory_bridge_rejects_unreviewed_release_and_target(city_session, changed):
    session, _release, devices = city_session
    zone = "ZONE-FAISALABAD-01"
    device = devices[zone]
    predecessor, digest, version = CITY_FACTORY_PREDECESSORS[zone]
    device.firmware_version = f"zone-lite-{predecessor}"
    device.ota_running_partition = "factory"
    device.ota_image_sha256 = digest
    bridge = session.scalar(select(FirmwareRelease).where(FirmwareRelease.version == version))
    if changed == "source":
        bridge.git_sha = "0" * 40
    elif changed == "artifact":
        bridge.image_sha256 = "0" * 64
    elif changed == "spare":
        device.is_spare = True
    else:
        device.zkt_device.confirmed_serial = "replacement"
    if changed in {"source", "artifact"}:
        with pytest.raises(ValueError, match="exact published signed image"):
            preview_campaign_scope(session, release_public_id=bridge.release_id, zone_id=zone)
    else:
        with pytest.raises(ValueError, match="BRIDGE_EXACT_IDENTITY_MISMATCH"):
            preview_campaign_scope(session, release_public_id=bridge.release_id, zone_id=zone)
