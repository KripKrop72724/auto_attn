"""Actual assignment admission, including simultaneous PostgreSQL transactions."""
from datetime import datetime, timezone
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from zk_add.db import Base
from zk_add.models import Connector, ZKTDevice
from zk_add.ota import (FirmwareCampaign, FirmwareDeployment, FirmwareDownloadGrant,
                        FirmwareRelease, assignment_for_connector)
from zk_add.settings import settings
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_ota_admission import pending_offer_hold, try_assignment_lock


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
