"""Actual assignment admission, including simultaneous PostgreSQL transactions."""
from datetime import datetime, timedelta, timezone
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from zk_add.db import Base
from zk_add.models import Connector, ZKTDevice
from zk_add.ota import (FirmwareCampaign, FirmwareDeployment, FirmwareDownloadGrant, FirmwareEvent,
                        FirmwareRelease, assignment_for_connector)
from zk_add.settings import settings
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_ota_admission import pending_offer_hold, reservation_snapshot, try_assignment_lock


@pytest.fixture(params=["sqlite", "postgres"])
def store(tmp_path, request, monkeypatch):
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "zkt_admission_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    request.addfinalizer(cleanup)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        def release(version):
            return FirmwareRelease(release_id="synthetic-" + version, version=version,
                git_sha="a"*40, image_sha256=("b" if version == "2.2.1" else "d")*64,
                image_size=1024, signing_key_id="synthetic",
                partition_layout="zone-lite-ota-v1", minimum_bootstrap_version="2.2.0",
                storage_name=f"{version}/synthetic.bin", manifest={"application_sha256": "c"*64},
                manifest_signature="synthetic", state="AVAILABLE")
        legacy, candidate = release("2.2.1"), release("2.7.0")
        # Direct synthetic fixture registration is NOT a qualified publication.
        # The real 2.7.0 assignment remains rejected by its storage contract.
        db.add_all([legacy, candidate])
        db.flush()
        for target in (TARGETS[0], TARGETS[1], TARGETS[2], TARGETS[11]):
            connector = Connector(connector_id=target.identity.connector_id, hardware_id=target.identity.mac,
                zone_id=target.name, zone_name=target.name, device_id="test", display_name=target.name,
                firmware_family="zkt", firmware_version="2.2.0", connected=True, active=True, is_spare=False,
                ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
                ota_partition_layout="zone-lite-ota-v1", ota_running_partition="ota_0")
            connector.zkt_device = ZKTDevice(serial=target.identity.terminal_serial,
                expected_serial=target.identity.terminal_serial, confirmed_serial=target.identity.terminal_serial,
                terminal_binding_state="CONFIRMED")
            db.add(connector)
            db.flush()
            campaign = FirmwareCampaign(campaign_id="synthetic-"+str(connector.id), release_id=legacy.id,
                zone_id=connector.zone_id, actor="synthetic", idempotency_key=target.name,
                reason="Synthetic admission concurrency test", typed_confirmation="2.2.1")
            db.add(campaign)
            db.flush()
            db.add(FirmwareDeployment(deployment_id="synthetic-"+str(connector.id), campaign_id=campaign.id,
                release_id=legacy.id, connector_id=connector.id, target_version="2.2.1"))
        db.commit()
    yield sessions


def connector(db, index):
    return db.scalar(select(Connector).where(Connector.connector_id == TARGETS[index].identity.connector_id))


def deployment(db, device):
    return db.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == device.id))


def offer(db, device):
    return assignment_for_connector(db, connector=device, public_base="https://test.invalid")


def test_any_number_of_targets_upgrade_at_once_even_at_one_location(store):
    # Owner decision (23 Oct 2026): no nationwide or per-location concurrency limit.
    with store() as db:
        tower, swat, faisalabad, upstairs = [connector(db, i) for i in [0, 1, 2, 11]]
        assert offer(db, tower)
        assert offer(db, upstairs)  # The same SLICTOWER location.
        assert offer(db, swat)
        assert offer(db, faisalabad)
        assert all(deployment(db, device).error_code is None for device in (tower, upstairs, swat, faisalabad))
        assert db.scalar(select(func.count(FirmwareDownloadGrant.id))) == 4
        assert offer(db, tower)  # Resume reuses its offer.
        assert deployment(db, tower).attempt_count == 1


@pytest.mark.parametrize("state", ["OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING",
                                  "RECONCILING", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"])
def test_unsettled_offers_at_a_location_no_longer_hold_another_upgrade(store, state):
    with store() as db:
        tower, upstairs = connector(db, 0), connector(db, 11)
        row = deployment(db, tower)
        row.status, row.offered_at = state, datetime.now(timezone.utc)
        tower.connected = False
        db.get(FirmwareCampaign, row.campaign_id).status = "PAUSED"
        assert offer(db, upstairs)
        assert deployment(db, upstairs).error_code is None
        assert db.scalar(select(func.count(FirmwareDownloadGrant.id))) == 1


@pytest.mark.parametrize("fault", ["offline", "spare", "serial", "mac", "unknown"])
def test_new_offers_require_the_exact_online_active_target(store, fault):
    with store() as db:
        device = connector(db, 0)
        if fault == "offline":
            device.connected = False
        elif fault == "spare":
            device.is_spare = True
        elif fault == "serial":
            device.zkt_device.confirmed_serial = "REPLACED"
        elif fault == "mac":
            device.hardware_id = "00:11:22:33:44:55"
        else:
            device.connector_id = "unknown-location"
        assert offer(db, device) is None
        assert deployment(db, device).status == "PENDING" and not deployment(db, device).attempt_count
        assert db.scalar(select(func.count(FirmwareDownloadGrant.id))) == 0


def test_an_unknown_upgrading_device_does_not_block_and_hikvision_is_separate(store):
    with store() as db:
        tower, swat = connector(db, 0), connector(db, 1)
        row = deployment(db, tower)
        row.status, row.offered_at = "DOWNLOADING", datetime.now(timezone.utc)
        tower.connector_id = "unknown-location"
        assert pending_offer_hold(db, swat) is None
        tower.firmware_family = "hikvision"
        assert try_assignment_lock(db, tower) and pending_offer_hold(db, tower) is None
        assert offer(db, swat)


def test_limits_do_not_qualify_a_candidate_or_replace_existing_release_checks(store):
    with store() as db:
        device = connector(db, 0)
        row = deployment(db, device)
        candidate = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.7.0"))
        row.release_id, row.target_version = candidate.id, candidate.version
        db.get(FirmwareCampaign, row.campaign_id).release_id = candidate.id
        assert offer(db, device) is None and row.status == "PENDING" and not row.attempt_count


def test_previous_release_offers_keep_their_policy_before_journal_registration(store):
    with store() as db:
        candidate = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.7.0"))
        db.delete(candidate)
        assert all(offer(db, connector(db, index)) for index in [0, 1, 2, 11])


def test_postgres_simultaneous_requests_are_serialized_without_a_slot_limit(store):
    if store.kw["bind"].dialect.name != "postgresql":
        return
    with store() as first, store() as second, store() as third:
        a, b, c = connector(first, 0), connector(second, 1), connector(third, 2)
        assert offer(first, a)
        started = time.monotonic()
        # The assignment lock still serializes offers; a busy lock never waits.
        assert offer(second, b) is None and time.monotonic()-started < 1
        first.commit()
        assert offer(second, b)
        second.commit()
        assert offer(third, c)
        third.commit()
        assert third.scalar(select(func.count(FirmwareDownloadGrant.id))) == 3


def test_postgres_failed_assignment_commit_releases_its_reservation(store):
    if store.kw["bind"].dialect.name != "postgresql":
        return
    with store() as first, store() as second:
        a, b = connector(first, 0), connector(second, 11)
        assert offer(first, a) and offer(second, b) is None
        first.rollback()
        assert offer(second, b)
        second.commit()
        assert deployment(second, b).attempt_count == 1
        assert second.scalar(select(func.count(FirmwareDownloadGrant.id))) == 1


def cancelled_then_installed(db, index=0, *, state="BOOTED_PENDING"):
    """An August cancelled attempt followed by a complete, authenticated boot."""
    device = connector(db, index)
    old = deployment(db, device)
    campaign = db.get(FirmwareCampaign, old.campaign_id)
    start = datetime.now(timezone.utc) - timedelta(days=2)
    old.status, old.offered_at, old.updated_at = state, start, start + timedelta(minutes=5)
    campaign.status = "CANCELLED"
    db.flush()
    later_campaign = FirmwareCampaign(campaign_id="later-"+str(device.id), release_id=old.release_id,
        zone_id=device.zone_id, actor="synthetic", idempotency_key="later-"+str(device.id),
        reason="Synthetic verified subsequent installation", typed_confirmation=old.target_version)
    db.add(later_campaign)
    db.flush()
    later = FirmwareDeployment(deployment_id="later-"+str(device.id), campaign_id=later_campaign.id,
        release_id=old.release_id, connector_id=device.id, target_version=old.target_version,
        status="SUCCEEDED", bytes_written=1024, offered_at=start+timedelta(days=1),
        completed_at=start+timedelta(days=1, minutes=5))
    db.add(later)
    db.flush()
    evidence = FirmwareEvent(deployment_id=later.id, state="SUCCEEDED",
        created_at=later.completed_at-timedelta(seconds=1), details={"bytes_written":1024,
            "image_sha256":"c"*64, "running_version":old.target_version, "running_partition":"ota_1"})
    db.add(evidence)
    db.flush()
    return device, old, campaign, later, evidence


@pytest.mark.parametrize("state", ["OFFERED", "DOWNLOADING", "BOOTED_PENDING", "RECONCILING",
                                  "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"])
def test_verified_later_installation_releases_old_reservation_without_rewriting_history(store, state):
    with store() as db:
        device, old, campaign, later, event = cancelled_then_installed(db, state=state)
        before = (old.status, old.updated_at, old.completed_at, campaign.status)
        # Stale/offline current telemetry is irrelevant to the committed later
        # installation; any subsequent active attempt will reserve its own slot.
        device.connected = False
        device.firmware_version = "unavailable"
        snapshot = reservation_snapshot(db)
        settled = snapshot["reservations"][0]["later_installation"]
        assert settled["deployment_id"] == later.deployment_id and settled["event_id"] == event.id
        assert offer(db, connector(db, 11))
        assert offer(db, connector(db, 1))
        assert offer(db, connector(db, 2))
        assert (old.status, old.updated_at, old.completed_at, campaign.status) == before


@pytest.mark.parametrize("fault", ["active", "paused", "missing_event", "other_connector", "incomplete",
    "bad_digest", "bad_partition", "bad_version", "bad_target", "failed_event", "failed_deployment",
    "missing_completed", "old_offer", "old_event", "future_event", "missing_offer", "wrong_bytes", "error"])
def test_unproven_later_installation_stays_unsettled_without_holding_offers(store, fault):
    with store() as db:
        device, old, campaign, later, event = cancelled_then_installed(db)
        if fault in {"active", "paused"}:
            db.get(FirmwareCampaign, later.campaign_id).status = "CANCELLED"
            db.flush()
            campaign.status = fault.upper()
        elif fault == "missing_event":
            db.delete(event)
        elif fault == "other_connector":
            later.connector_id = connector(db, 1).id
        elif fault == "incomplete":
            later.bytes_written = 1000
        elif fault == "failed_event":
            event.state = "BOOTED_PENDING"
        elif fault == "failed_deployment":
            later.status = "FAILED"
        elif fault == "missing_completed":
            later.completed_at = None
        elif fault == "old_offer":
            later.offered_at = old.offered_at
        elif fault == "old_event":
            event.created_at = old.offered_at
        elif fault == "future_event":
            event.created_at = later.completed_at + timedelta(seconds=10)
        elif fault == "missing_offer":
            old.offered_at = None
        elif fault == "bad_target":
            later.target_version = "2.0.0"
        else:
            key, value = {"bad_digest": ("image_sha256", "f"*64), "bad_partition": ("running_partition", "factory"),
                "bad_version": ("running_version", "2.0.0"), "wrong_bytes": ("bytes_written", True),
                "error": ("error_code", "BOOT_HEALTH_TIMEOUT")}[fault]
            event.details = {**event.details, key:value}
        db.flush()
        assert reservation_snapshot(db)["reservations"][0]["later_installation"] is None
        assert offer(db, connector(db, 11))
        assert deployment(db, connector(db, 11)).error_code is None


def test_unsettled_history_of_any_size_never_holds_an_offer(store, monkeypatch):
    with store() as db:
        device, old, campaign, later, event = cancelled_then_installed(db)
        later.status, later.offered_at = "CANCELLED", old.offered_at
        assert offer(db, connector(db, 1))
        assert offer(db, connector(db, 2))
        monkeypatch.setattr("zk_add.zkt_ota_admission.RESERVATION_SCAN_LIMIT", 1)
        assert not reservation_snapshot(db)["scan_complete"]
        assert pending_offer_hold(db, connector(db, 11)) is None


def test_real_progress_commit_settles_cancelled_attempt_and_new_attempt_reserves_again(store):
    from zk_add.ota import record_progress

    with store() as db:
        device, old, campaign, later, event = cancelled_then_installed(db)
        db.delete(event)
        later.status, later.completed_at = "RECONCILING", None
        db.commit()
        record_progress(db, connector=device, deployment_public_id=later.deployment_id, state="SUCCEEDED",
            bytes_written=1024, running_version=later.target_version, running_partition="ota_1", image_sha256="c"*64)
        db.commit()
        assert pending_offer_hold(db, connector(db, 11)) is None
        # A new attempt on this device is shown as a new reservation, and still
        # never holds another device's offer.
        db.add(FirmwareDeployment(deployment_id="new-attempt", campaign_id=later.campaign_id,
            release_id=later.release_id, connector_id=device.id, target_version=later.target_version,
            status="OFFERED", offered_at=datetime.now(timezone.utc)))
        db.commit()
        assert any(row["deployment_id"] == "new-attempt" and row["later_installation"] is None
                   for row in reservation_snapshot(db)["reservations"])
        assert pending_offer_hold(db, connector(db, 11)) is None


# ZONE-BLD9-01's K50 sits outside the 17. Only its exact signed 2.4.12 bridge
# and 2.7.4 may be offered to it.
def k50_release(db, identity, state):
    release_id, version, git_sha, image, application = identity
    row = FirmwareRelease(release_id=release_id, version=version, git_sha=git_sha, image_sha256=image,
        image_size=1024, signing_key_id="synthetic", partition_layout="zone-lite-ota-v1",
        minimum_bootstrap_version="2.2.0", storage_name=f"{version}/k50.bin",
        manifest={"application_sha256": application}, manifest_signature="synthetic", state=state)
    db.add(row)
    db.flush()
    return row


def add_device(db, target, zone, *, firmware="2.5.2", partition="factory", digest=None):
    device = Connector(connector_id=target.connector_id, hardware_id=target.mac, zone_id=zone, zone_name=zone,
        device_id="test", display_name=zone, firmware_family="zkt", firmware_version=firmware, connected=True,
        active=True, is_spare=False, ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
        ota_partition_layout="zone-lite-ota-v1", ota_running_partition=partition, ota_image_sha256=digest)
    device.zkt_device = ZKTDevice(serial=target.terminal_serial, expected_serial=target.terminal_serial,
        confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED")
    db.add(device)
    db.flush()
    return device


def k50_store(db):
    from zk_add.hil_2615_cities import SIGNED_BRIDGE_IDENTITIES
    from zk_add.zkt274_bld9_k50 import FACTORY_PREDECESSOR, RELEASE_IDENTITY, TARGET, ZONE
    bridge = k50_release(db, SIGNED_BRIDGE_IDENTITIES["2.4.12"], "AVAILABLE")
    release = k50_release(db, RELEASE_IDENTITY, "HIL_ONLY")
    return add_device(db, TARGET, ZONE, digest=FACTORY_PREDECESSOR[1]), bridge, release


def hold(db, device, release, state="DOWNLOADING"):
    campaign = FirmwareCampaign(campaign_id="hold-" + str(device.id), release_id=release.id,
        zone_id=device.zone_id, actor="synthetic", idempotency_key="hold-" + str(device.id),
        reason="Synthetic occupied slot", typed_confirmation=release.version)
    db.add(campaign)
    db.flush()
    db.add(FirmwareDeployment(deployment_id="hold-" + str(device.id), campaign_id=campaign.id,
        release_id=release.id, connector_id=device.id, target_version=release.version, status=state,
        offered_at=datetime.now(timezone.utc)))
    db.flush()


def test_k50_queues_only_for_its_exact_bridge_and_2_7_4(store):
    with store() as db:
        k50, bridge, release = k50_store(db)
        assert pending_offer_hold(db, k50, bridge) is None
        assert pending_offer_hold(db, k50, release) is None
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        for other in (None, legacy):
            assert pending_offer_hold(db, k50, other) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
        # Each release needs its exact state and signed identity.
        for row, field, value in ((bridge, "state", "HIL_ONLY"), (release, "state", "AVAILABLE"),
                                  (release, "git_sha", "0" * 40), (release, "image_sha256", "0" * 64),
                                  (release, "version", "2.7.3"), (bridge, "image_sha256", "0" * 64)):
            saved = getattr(row, field)
            setattr(row, field, value)
            assert pending_offer_hold(db, k50, row) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
            setattr(row, field, saved)
        release.manifest = {"application_sha256": "0" * 64}
        assert pending_offer_hold(db, k50, release) == "NATIONWIDE_EXACT_TARGET_REQUIRED"


@pytest.mark.parametrize("fault", ["serial", "mac", "zone", "spare", "inactive", "connector"])
def test_k50_exception_is_its_exact_identity_and_zone(store, fault):
    with store() as db:
        k50, bridge, release = k50_store(db)
        if fault == "serial":
            k50.zkt_device.confirmed_serial = "REPLACED"
        elif fault == "mac":
            k50.hardware_id = "00:11:22:33:44:55"
        elif fault == "zone":
            k50.zone_id = "ZONE-BLD9-02"
        elif fault == "spare":
            k50.is_spare = True
        elif fault == "inactive":
            k50.active = False
        else:
            k50.connector_id = "another-k50"
        for row in (bridge, release):
            assert pending_offer_hold(db, k50, row) == "NATIONWIDE_EXACT_TARGET_REQUIRED"


def test_k50_upgrades_alongside_bld9_and_the_rest_of_the_fleet(store):
    from zk_add.zkt274_bld9_k50 import TARGET
    with store() as db:
        k50, bridge, release = k50_store(db)
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        bld9_01, bld9_02 = (add_device(db, TARGETS[index].identity, TARGETS[index].name, firmware="2.2.0",
                                       partition="ota_0") for index in (15, 16))
        k50.connected = False
        assert pending_offer_hold(db, k50, release) == "NATIONWIDE_TARGET_OFFLINE"
        k50.connected = True
        # BLD9 01 upgrading at the same location no longer keeps the K50 waiting.
        hold(db, bld9_01, legacy)
        assert pending_offer_hold(db, k50, bridge) is None
        hold(db, k50, bridge)
        assert {row["connector_id"] for row in reservation_snapshot(db)["reservations"]} >= {TARGET.connector_id}
        assert pending_offer_hold(db, bld9_02, legacy) is None
        tower, swat = connector(db, 0), connector(db, 1)
        for device in (tower, swat):
            row = deployment(db, device)
            row.status, row.offered_at = "DOWNLOADING", datetime.now(timezone.utc)
        assert pending_offer_hold(db, k50, release) is None


def test_other_devices_outside_the_17_still_wait_with_the_k50_exception(store):
    from zk_add.hil_scope import HilTarget
    from zk_add.zkt274_bld9_k50 import ZONE
    with store() as db:
        _k50, bridge, release = k50_store(db)
        stranger = add_device(db, HilTarget(connector_id="11111111-2222-3333-4444-555555555555",
                                            mac="ac:27:6e:00:00:01", terminal_serial="ANM0000000001"), ZONE)
        for row in (bridge, release):
            assert pending_offer_hold(db, stranger, row) == "NATIONWIDE_EXACT_TARGET_REQUIRED"


def test_k50_bridge_offer_through_real_assignment(store):
    with store() as db:
        k50, bridge, _release = k50_store(db)
        bld9_01 = add_device(db, TARGETS[15].identity, TARGETS[15].name, firmware="2.2.0", partition="ota_0")
        campaign = FirmwareCampaign(campaign_id="k50-bridge", release_id=bridge.id, zone_id=k50.zone_id,
            actor="synthetic", idempotency_key="k50-bridge", reason="Synthetic K50 bridge",
            typed_confirmation=bridge.version)
        db.add(campaign)
        db.flush()
        db.add(FirmwareDeployment(deployment_id="k50-bridge", campaign_id=campaign.id, release_id=bridge.id,
            connector_id=k50.id, previous_version=k50.firmware_version, target_version=bridge.version))
        db.flush()
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        hold(db, bld9_01, legacy)
        # BLD9 01 mid-upgrade at the same location does not hold the K50's offer.
        assert offer(db, k50)["version"] == "2.4.12"
        assert deployment(db, k50).status == "OFFERED" and deployment(db, k50).error_code is None


def addition(db, *, model="G3", connector_id="d4b8e5ee-8539-4719-b92f-a9458f6600d3"):
    from zk_add.hil_scope import HilTarget
    device = add_device(db, HilTarget(connector_id=connector_id, mac="14:c1:9f:46:ae:1c",
                                      terminal_serial="PGB1261300043"), "LF-ZONE-BLD1-01")
    device.zkt_device.model = model
    return device


def test_a_qualified_later_zkt_takes_production_images_only(store):
    with store() as db:
        device = addition(db)
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        assert pending_offer_hold(db, device, legacy) is None
        # The 17 remain the exact scope of every HIL_ONLY image.
        legacy.state = "HIL_ONLY"
        assert pending_offer_hold(db, device, legacy) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
        assert pending_offer_hold(db, device, None) == "NATIONWIDE_EXACT_TARGET_REQUIRED"
        legacy.state = "AVAILABLE"
        device.connected = False
        assert pending_offer_hold(db, device, legacy) == "NATIONWIDE_TARGET_OFFLINE"


@pytest.mark.parametrize("fault", ["k50_model", "no_model", "unbound", "serial", "spare", "inactive",
                                   "k50_connector", "changed_target"])
def test_later_zkt_needs_a_confirmed_qualified_terminal(store, fault):
    from zk_add.zkt274_bld9_k50 import TARGET as K50
    with store() as db:
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        if fault == "changed_target":
            # One of the 17 whose terminal changed is not a new addition.
            device = connector(db, 0)
            device.zkt_device.model = "G3"
            device.zkt_device.serial = device.zkt_device.confirmed_serial = "REPLACED"
        elif fault == "k50_connector":
            device = addition(db, connector_id=K50.connector_id)
        else:
            device = addition(db, model={"k50_model": "K50", "no_model": None}.get(fault, "G3"))
        if fault == "unbound":
            device.zkt_device.terminal_binding_state = "PENDING_DEVICE_ACK"
        elif fault == "serial":
            device.zkt_device.serial = "REPLACED"
        elif fault == "spare":
            device.is_spare = True
        elif fault == "inactive":
            device.active = False
        assert pending_offer_hold(db, device, legacy) == "NATIONWIDE_EXACT_TARGET_REQUIRED"


def test_a_qualified_later_zkt_is_offered_through_real_assignment(store):
    with store() as db:
        device = addition(db)
        legacy = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.2.1"))
        campaign = FirmwareCampaign(campaign_id="later-zkt", release_id=legacy.id, zone_id=device.zone_id,
            actor="synthetic", idempotency_key="later-zkt", reason="Synthetic later ZKT",
            typed_confirmation=legacy.version)
        db.add(campaign)
        db.flush()
        db.add(FirmwareDeployment(deployment_id="later-zkt", campaign_id=campaign.id, release_id=legacy.id,
            connector_id=device.id, previous_version=device.firmware_version, target_version=legacy.version))
        db.flush()
        assert offer(db, device)["version"] == "2.2.1"
        assert deployment(db, device).status == "OFFERED" and deployment(db, device).error_code is None
