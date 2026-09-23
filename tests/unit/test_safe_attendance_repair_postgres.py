"""Real PostgreSQL locking and 100,000-record qualification in a disposable schema."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from test_safe_attendance_repair import store as store, start, tick
from test_attendance_repair import repair_store as repair_store
from zk_add import attendance_safe_repair as repair
from zk_add.db import Base
from zk_add.models import AttendanceEvent, AttendanceRecoveryJob, Connector, DeviceUser
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import onboard_connector, replace_user_snapshot
from zk_add.settings import settings
from zk_add.time_utils import utc_now


def test_overlapping_device_sockets_commit_a_sequence_once(postgres_store):
    from threading import Barrier
    from zk_add.schemas import Envelope
    from zk_add.web import persist_envelope
    from zk_add.models import DeviceLog

    sessions, connector_id = postgres_store
    with sessions() as db:
        connector_pk = db.scalar(select(Connector.id).where(Connector.connector_id == connector_id))
    envelope = Envelope(
        schema_version="2", message_id="overlapping-sockets", connector_id=connector_id,
        boot_id="test-boot", seq=1, sent_at=utc_now(), type="log",
        payload={"level": "INFO", "message": "overlapping sockets regression", "code": "SOCKET_TEST"},
    )
    start = Barrier(2)

    def receive():
        start.wait(timeout=5)
        return persist_envelope(connector_pk, envelope)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: receive(), range(2)))
    assert sum(result.ack.get("duplicate", False) for result in results) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(DeviceLog.id)).where(DeviceLog.code == "SOCKET_TEST")) == 1
        assert db.get(Connector, connector_pk).last_sequence == 1


@pytest.fixture()
def postgres_store(store, monkeypatch):
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "safe_repair_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"
        },
    )
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(settings, "attendance_safe_repair_batch_size", 100)
        monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
        with sessions() as db:
            connector, _, _ = onboard_connector(
                db,
                hardware_id="e0:72:a1:00:00:01",
                zone_id="TEST",
                zone_name="Test",
                device_id="TEST",
                firmware_version="2.6.0",
                expected_serial="TEST-REPAIR-SERIAL",
                actor="test",
                ip_address=None,
            )
            connector.zkt_device.serial = "TEST-REPAIR-SERIAL"
            replace_user_snapshot(
                db,
                connector=connector,
                snapshot=UserSnapshotRequest(
                    snapshot_id="pg-proof",
                    complete=True,
                    stable=True,
                    observed_at=utc_now(),
                    users=[
                        UserSnapshotRow(uid="7", user_id="1007", name="Test Person-3520212345671")
                    ],
                ),
            )
            user = db.scalar(
                select(DeviceUser).where(DeviceUser.zkt_device_id == connector.zkt_device.id)
            )
            event = AttendanceEvent(
                event_uid="a" * 64,
                connector_id=connector.id,
                zkt_device_id=connector.zkt_device.id,
                device_user_id=user.id,
                user_id=user.user_id,
                uid=user.uid,
                device_serial=connector.zkt_device.serial,
                device_event_time=utc_now(),
                captured_at=utc_now(),
                source="FULL_HISTORY",
                raw_event={"reconciliation_source": "VERIFIED_TERMINAL_SOURCE"},
                clock_quality="OK",
                identity_resolution_status="BLOCKED_PROVENANCE",
                ords_status="BLOCKED_IDENTITY",
            )
            db.add(event)
            db.commit()
            identifier = connector.connector_id
        yield sessions, identifier
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def frozen(sessions, connector_id, key):
    with sessions() as db:
        job = repair.create_check(db, actor="operator", key=key, connector_ids=[connector_id])
        job_id = job.job_id
        db.commit()
    for _ in range(1100):
        with sessions() as db:
            repair.advance_once(db)
            db.commit()
            job = db.scalar(
                select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id)
            )
            if job.status == "CHECKED":
                return job_id
    raise AssertionError("Bounded cursor did not finish")


def test_concurrent_admins_cannot_start_overlapping_runs(postgres_store):
    sessions, connector_id = postgres_store
    ids = [frozen(sessions, connector_id, f"concurrent-check-{i}") for i in range(2)]

    def attempt(job_id):
        try:
            start(sessions, job_id)
            return "STARTED"
        except repair.RecoveryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ids))
    assert sorted(outcomes) == ["REPAIR_ALREADY_RUNNING", "STARTED"]


def test_100000_saved_records_restart_cursor_and_live_delivery(postgres_store):
    from zk_add.models import OrdsOutbox
    from zk_add.worker import claim_ords_batch

    sessions, connector_id = postgres_store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        connector = db.scalar(select(Connector))
        for page in range(100):
            db.execute(
                AttendanceEvent.__table__.insert(),
                [
                    dict(
                        event_uid=hashlib.sha256(
                            f"qualification-{page}-{index}".encode()
                        ).hexdigest(),
                        connector_id=connector.id,
                        zkt_device_id=event.zkt_device_id,
                        device_serial=event.device_serial,
                        user_id="unknown",
                        uid="999",
                        source="FULL_HISTORY",
                        device_event_time=event.device_event_time,
                        captured_at=event.captured_at,
                        identity_resolution_status="BLOCKED_PROVENANCE",
                        ords_status="BLOCKED_IDENTITY",
                    )
                    for index in range(1000)
                ],
            )
        # Live capture and delivery remain independent while the historical scan runs.
        live = AttendanceEvent(
            event_uid="b" * 64,
            connector_id=connector.id,
            zkt_device_id=event.zkt_device_id,
            device_serial=event.device_serial,
            user_id=event.user_id,
            source="LIVE",
            device_event_time=utc_now(),
            captured_at=utc_now(),
            clock_quality="OK",
            identity_resolution_status="RESOLVED",
            cnic_encrypted=db.get(DeviceUser, event.device_user_id).cnic_encrypted,
            cnic_lookup_hash=db.get(DeviceUser, event.device_user_id).cnic_lookup_hash,
        )
        db.add(live)
        db.flush()
        db.add(OrdsOutbox(attendance_event_id=live.id, delivery_type="LIVE", status="PENDING"))
        db.commit()
    assert len(claim_ords_batch(1)) == 1
    job_id = frozen(sessions, connector_id, "qualification-100000")
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        assert repair.counts(db, job)["checked"] == 100002
        assert repair.counts(db, job)["review"] == 100000
        assert repair.counts(db, job)["ready"] == 2
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 100002
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        assert repair.counts(db, job)["waiting"] == 2
        # A server retry after a lost reply preserves a single outbox per punch.
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 2
        job.updated_at = utc_now() - timedelta(minutes=11)
        assert "10 minutes" in repair.serialize(db, job, "operator")["last_error"]


def test_large_deliverable_backlog_does_not_hide_behind_live_traffic(postgres_store):
    from zk_add.models import OrdsOutbox
    from zk_add.worker import claim_ords_batch

    sessions, _ = postgres_store
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent))
        user = db.get(DeviceUser, original.device_user_id)
        next_id = 2
        for page in range(101):
            rows = []
            for offset in range(1000):
                index = page * 1000 + offset
                rows.append(
                    dict(
                        id=next_id + index,
                        event_uid=hashlib.sha256(f"delivery-load-{index}".encode()).hexdigest(),
                        connector_id=original.connector_id,
                        zkt_device_id=original.zkt_device_id,
                        device_serial=original.device_serial,
                        user_id=original.user_id,
                        uid=original.uid,
                        source="FULL_HISTORY" if page < 100 else "LIVE",
                        identity_resolution_status="RESOLVED",
                        cnic_encrypted=user.cnic_encrypted,
                        cnic_lookup_hash=user.cnic_lookup_hash,
                        clock_quality="OK",
                        device_event_time=utc_now(),
                        captured_at=utc_now(),
                        ords_status="PENDING",
                    )
                )
            db.execute(AttendanceEvent.__table__.insert(), rows)
            db.execute(
                OrdsOutbox.__table__.insert(),
                [
                    dict(
                        attendance_event_id=row["id"],
                        delivery_type="FULL_HISTORY" if page < 100 else "LIVE",
                        status="PENDING",
                    )
                    for row in rows
                ],
            )
        db.commit()
    # Live traffic remains present in every slice; the historical queue still
    # receives its reserved share despite 100,000 older deliverable rows.
    for _ in range(3):
        claims = claim_ords_batch(50)
        assert len(claims) == 50
        with sessions() as db:
            types = dict(
                db.execute(
                    select(OrdsOutbox.delivery_type, func.count(OrdsOutbox.id))
                    .where(OrdsOutbox.id.in_([claim[0] for claim in claims]))
                    .group_by(OrdsOutbox.delivery_type)
                ).all()
            )
            assert types == {"LIVE": 40, "FULL_HISTORY": 10}
    with sessions() as db:
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 101001
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 101000
