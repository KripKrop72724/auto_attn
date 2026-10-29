import pytest
from sqlalchemy import select

from test_city_hil_scope import city_fixture, start  # noqa: F401
from test_hil_scope import bld5_2615_session, hil_session, parallel_269_session  # noqa: F401
from zk_add.hil_2615_cities import CITY_FACTORY_PREDECESSORS, CITY_TARGETS, SIGNED_BRIDGE_IDENTITIES
from zk_add.legacy_bld5_bridge import BRIDGE_TARGETS, FACTORY_PREDECESSORS
from zk_add.models import Connector, ZKTDevice
from zk_add.ota import (
    FirmwareRelease, HIL_2615_CITY_TARGETS, assignment_for_connector, preview_campaign_scope, resolve_download,
)
from zk_add.zkt270_scope import BY_ID
from zk_add.zkt_life_factory_bridges import LIFE_FACTORY_PREDECESSORS, LIFE_TARGETS

NEW_BLD1 = "LF-ZONE-BLD1-01"


def test_life_bridges_are_exact_identities_outside_the_2615_city_cohort():
    assert set(LIFE_TARGETS) == set(LIFE_FACTORY_PREDECESSORS)
    for zone, target in LIFE_TARGETS.items():
        version, digest, bridge = LIFE_FACTORY_PREDECESSORS[zone]
        assert (version, bridge) == ("2.5.2", "2.4.12")
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert BRIDGE_TARGETS[zone] == target and FACTORY_PREDECESSORS[zone] == (version, digest)
        if zone == NEW_BLD1:
            assert target.connector_id not in BY_ID
        else:
            assert BY_ID[target.connector_id].identity == target
            assert BY_ID[target.connector_id].prerequisite == "EXACT_FACTORY_BRIDGE"
    assert not set(LIFE_TARGETS) & (set(CITY_TARGETS) | set(CITY_FACTORY_PREDECESSORS))
    assert not set(LIFE_TARGETS.values()) & set(HIL_2615_CITY_TARGETS) and len(HIL_2615_CITY_TARGETS) == 14


def life_device(session, zone, *, model="G3"):
    target = LIFE_TARGETS[zone]
    version, digest, _bridge = LIFE_FACTORY_PREDECESSORS[zone]
    device = Connector(
        connector_id=target.connector_id, hardware_id=target.mac, zone_id=zone, zone_name=zone,
        device_id="1", display_name=zone, firmware_family="zkt", connected=True, active=True, is_spare=False,
        firmware_version=f"zone-lite-{version}", ota_capable=True, ota_secure_boot=True,
        ota_rollback_enabled=True, ota_partition_layout="zone-lite-ota-v1", ota_running_partition="factory",
        ota_image_sha256=digest,
    )
    device.zkt_device = ZKTDevice(
        serial=target.terminal_serial, expected_serial=target.terminal_serial,
        confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED", model=model,
    )
    session.add(device)
    # A registered journal release turns on the fleet's exact-target offer guard, as in production.
    session.add(FirmwareRelease(
        release_id="synthetic-journal-2.7.0", version="2.7.0", git_sha="a" * 40, image_sha256="e" * 64,
        image_size=1024, signing_key_id="synthetic", partition_layout="zone-lite-ota-v1",
        minimum_bootstrap_version="2.2.0", storage_name="synthetic/2.7.0.bin",
        manifest={"application_sha256": "f" * 64}, manifest_signature="synthetic", state="HIL_ONLY",
    ))
    session.flush()
    return device


def bridge_release(session):
    return session.scalar(select(FirmwareRelease).where(
        FirmwareRelease.release_id == SIGNED_BRIDGE_IDENTITIES["2.4.12"][0]))


@pytest.mark.parametrize("zone", LIFE_TARGETS)
def test_life_factory_image_takes_only_the_exact_2412_bridge(city_session, zone):
    session, _release, _devices = city_session
    device = life_device(session, zone)
    bridge = bridge_release(session)
    scope = start(session, bridge, zone)
    assert [row["connector_id"] for row in scope["eligible"]] == [device.connector_id]
    offer = assignment_for_connector(session, connector=device, public_base="https://test.invalid")
    assert offer["version"] == "2.4.12"
    session.flush()
    grant = offer["download_url"].rsplit("/", 1)[1]
    assert resolve_download(session, grant)[0] == bridge
    device.ota_image_sha256 = "0" * 64
    assert assignment_for_connector(session, connector=device, public_base="https://test.invalid") is None
    with pytest.raises(ValueError, match="predecessor changed"):
        resolve_download(session, grant)


@pytest.mark.parametrize("changed", ["artifact", "hil_only", "serial", "partition"])
def test_life_bridge_rejects_unreviewed_release_or_device(city_session, changed):
    session, _release, _devices = city_session
    device = life_device(session, "LF-ZONE-BLD9-01")
    bridge = bridge_release(session)
    if changed == "artifact":
        bridge.image_sha256 = "0" * 64
    elif changed == "hil_only":
        bridge.state = "HIL_ONLY"
    elif changed == "serial":
        device.zkt_device.confirmed_serial = "replacement"
    else:
        device.ota_running_partition = "ota_0"
    with pytest.raises(ValueError):
        preview_campaign_scope(session, release_public_id=bridge.release_id, zone_id=device.zone_id)


def test_new_bld1_zkt_waits_unless_its_terminal_model_is_qualified(city_session):
    session, _release, _devices = city_session
    device = life_device(session, NEW_BLD1, model="K50")
    bridge = bridge_release(session)
    start(session, bridge, NEW_BLD1)
    assert assignment_for_connector(session, connector=device, public_base="https://test.invalid") is None
    device.zkt_device.model = "G3"
    assert assignment_for_connector(session, connector=device, public_base="https://test.invalid")["version"] == "2.4.12"
