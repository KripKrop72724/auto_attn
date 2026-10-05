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


def test_two_fleet_slots_and_physical_location_are_enforced_in_real_assignment(store):
    with store() as db:
        tower, swat, faisalabad, upstairs = [connector(db, i) for i in [0, 1, 2, 11]]
        assert offer(db, tower)
        assert offer(db, upstairs) is None
        assert deployment(db, upstairs).error_code == "NATIONWIDE_LOCATION_BUSY"
        assert offer(db, swat)
        assert offer(db, faisalabad) is None
        assert deployment(db, faisalabad).error_code == "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
        assert db.scalar(select(func.count(FirmwareDownloadGrant.id))) == 2
        assert offer(db, tower)  # Resume reuses its reservation.
        assert deployment(db, tower).attempt_count == 1
        deployment(db, tower).status = "SUCCEEDED"
        assert offer(db, upstairs)
        assert deployment(db, upstairs).error_code is None
        assert deployment(db, faisalabad).status == "PENDING"


@pytest.mark.parametrize("state", ["OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING",
                                  "RECONCILING", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"])
def test_offline_paused_and_unconfirmed_cancelled_offers_still_reserve_a_location(store, state):
    with store() as db:
        tower, upstairs = connector(db, 0), connector(db, 11)
        row = deployment(db, tower)
        row.status, row.offered_at = state, datetime.now(timezone.utc)
        tower.connected = False
        db.get(FirmwareCampaign, row.campaign_id).status = "PAUSED"
        assert offer(db, upstairs) is None
        assert deployment(db, upstairs).error_code == "NATIONWIDE_LOCATION_BUSY"
        assert db.scalar(select(func.count(FirmwareDownloadGrant.id))) == 0


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


def test_unknown_active_location_blocks_and_hikvision_is_separate(store):
    with store() as db:
        tower, swat = connector(db, 0), connector(db, 1)
        row = deployment(db, tower)
        row.status, row.offered_at = "DOWNLOADING", datetime.now(timezone.utc)
        tower.connector_id = "unknown-location"
        assert offer(db, swat) is None
        assert deployment(db, swat).error_code == "NATIONWIDE_ACTIVE_LOCATION_UNKNOWN"
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


def test_postgres_simultaneous_requests_cannot_reserve_three_slots(store):
    if store.kw["bind"].dialect.name != "postgresql":
        return
    with store() as first, store() as second, store() as third:
        a, b, c = connector(first, 0), connector(second, 1), connector(third, 2)
        assert offer(first, a)
        started = time.monotonic()
        assert offer(second, b) is None and time.monotonic()-started < 1
        assert offer(third, c) is None
        first.commit()
        assert offer(second, b)
        assert offer(third, c) is None
        second.commit()
        assert offer(third, c) is None
        assert deployment(third, c).error_code == "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
        assert third.scalar(select(func.count(FirmwareDownloadGrant.id))) == 2


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
        assert offer(db, connector(db, 2)) is None
        assert (old.status, old.updated_at, old.completed_at, campaign.status) == before


@pytest.mark.parametrize("fault", ["active", "paused", "missing_event", "other_connector", "incomplete",
    "bad_digest", "bad_partition", "bad_version", "bad_target", "failed_event", "failed_deployment",
    "missing_completed", "old_offer", "old_event", "future_event", "missing_offer", "wrong_bytes", "error"])
def test_unproven_later_installation_cannot_release_reservation(store, fault):
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
        assert offer(db, connector(db, 11)) is None
        assert deployment(db, connector(db, 11)).error_code == "NATIONWIDE_LOCATION_BUSY"


def test_same_connector_uncertainty_counts_once_and_unbounded_history_is_held(store, monkeypatch):
    with store() as db:
        device, old, campaign, later, event = cancelled_then_installed(db)
        later.status, later.offered_at = "CANCELLED", old.offered_at
        assert offer(db, connector(db, 1))  # One unknown ESP plus this offer, not three ESPs.
        assert offer(db, connector(db, 2)) is None
        monkeypatch.setattr("zk_add.zkt_ota_admission.RESERVATION_SCAN_LIMIT", 1)
        assert pending_offer_hold(db, connector(db, 2)) == "NATIONWIDE_RESERVATION_SCAN_LIMIT"


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
        # A new attempt on this device is a new reservation, regardless of the
        # historical cancelled attempts already settled by that valid boot.
        db.add(FirmwareDeployment(deployment_id="new-attempt", campaign_id=later.campaign_id,
            release_id=later.release_id, connector_id=device.id, target_version=later.target_version,
            status="OFFERED", offered_at=datetime.now(timezone.utc)))
        db.commit()
        assert pending_offer_hold(db, connector(db, 11)) == "NATIONWIDE_LOCATION_BUSY"
