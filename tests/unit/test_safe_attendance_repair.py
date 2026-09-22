from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from test_attendance_repair import repair_store as repair_store
from zk_add import attendance_safe_repair as repair
from zk_add.attendance_identity_evidence import identity_evidence
from zk_add.attendance_recovery import RecoveryError
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem,
    AttendanceRecoveryJob,
    AttendanceSafeRepairDecision,
    Connector,
    DeviceUser,
    OrdsOutbox,
)
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import replace_user_snapshot
from zk_add.settings import settings
from zk_add.time_utils import utc_now


@pytest.fixture()
def store(repair_store, monkeypatch):
    sessions, connector_id, user_key, event_uid = repair_store
    monkeypatch.setattr(settings, "attendance_safe_repair_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_safe_repair_execution_enabled", True)
    monkeypatch.setattr(settings, "attendance_safe_repair_automatic_enabled", False)
    monkeypatch.setattr(settings, "attendance_safe_repair_batch_size", 2)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid))
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        user = db.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        event.ords_status = "BLOCKED_IDENTITY"
        event.identity_resolution_status = "BLOCKED_PROVENANCE"
        event.cnic_encrypted = event.cnic_lookup_hash = None
        event.oracle_confirmed_at = None
        event.device_event_time = event.captured_at = utc_now() - timedelta(seconds=30)
        connector.zkt_device.last_identity_change_at = event.device_event_time - timedelta(
            minutes=1
        )
        connector.zkt_device.identity_snapshot_observed_at = utc_now()
        user.terminal_identity_fingerprint = "a" * 64
        db.commit()
    return sessions, connector_id, event_uid


def check(sessions, connector_id, key="check-request-1"):
    with sessions() as db:
        job = repair.create_check(db, actor="operator", key=key, connector_ids=[connector_id])
        job_id = job.job_id
        db.commit()
    for _ in range(100):
        with sessions() as db:
            job = db.scalar(
                select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id)
            )
            if job.status == "CHECKED":
                return job_id
            repair.advance_once(db)
            db.commit()
    raise AssertionError("Check did not finish")


def start(sessions, job_id):
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        signature = repair.serialize(db, job, "operator")["signature"]
        repair.start_check(db, job, actor="operator", signature=signature)
        db.commit()


def tick(sessions):
    with sessions() as db:
        repair.advance_once(db)
        db.commit()


def test_preview_is_read_only_and_oracle_receipt_is_required(store):
    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        assert event.ords_status == "BLOCKED_IDENTITY"
        assert event.cnic_lookup_hash is None
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 0
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        assert job.status == "WAITING_ORACLE"
        assert repair.counts(db, job) == dict(
            checked=1, ready=0, waiting=1, confirmed=0, review=0, stopped=0
        )
        assert event.raw_event == {"source_record": "preserved"}
        assert repair.delivery_proof_valid(db, event, connector)
        assert db.scalar(select(func.count(AttendanceSafeRepairDecision.id))) == 1
        event.ords_status = "ACKED_CHECK"
        event.oracle_confirmed_at = utc_now()
        db.commit()
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        assert job.status == "COMPLETED"
        assert repair.counts(db, job)["confirmed"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "serial",
        "uid",
        "fingerprint",
        "outside_window",
        "reuse",
        "conflict",
        "bad_ciphertext",
        "wrong_cnic_hash",
    ],
)
def test_unsafe_identity_is_never_eligible(store, change):
    sessions, connector_id, uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        user = db.get(DeviceUser, event.device_user_id)
        if change == "serial":
            event.device_serial = "DIFFERENT-TERMINAL"
        if change == "uid":
            event.uid = "99"
        if change == "fingerprint":
            event.identity_terminal_fingerprint = "b" * 64
        if change == "outside_window":
            event.device_event_time -= timedelta(days=1)
        if change == "reuse":
            event.identity_resolution_status = "QUARANTINED_REUSE"
        if change == "conflict":
            user.identity_conflict_code = "DUPLICATE_CNIC"
        if change == "bad_ciphertext":
            user.cnic_encrypted = "broken"
        if change == "wrong_cnic_hash":
            user.cnic_lookup_hash = "0" * 64
        db.flush()
        assert identity_evidence(db, event, connector) is None
        assert repair.classify(db, event, connector, None)[0] == "NEEDS_REVIEW"


def test_recheck_at_commit_and_at_send(store):
    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        event.uid = "changed"
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(AttendanceRecoveryItem)).status == "NEEDS_REVIEW"
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 0


def test_transaction_rollback_replays_without_duplicate_decision(store):
    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    with sessions() as db:
        repair.advance_once(db)
        db.flush()
        db.rollback()  # Process dies between outbox write and commit.
    tick(sessions)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 1
        assert db.scalar(select(func.count(AttendanceSafeRepairDecision.id))) == 1
        assert db.scalar(select(AttendanceRecoveryItem)).status == "WAITING_ORACLE"


def test_frozen_boundary_and_stop_preserve_records(store):
    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        db.add(
            AttendanceEvent(
                event_uid="f" * 64,
                connector_id=original.connector_id,
                zkt_device_id=original.zkt_device_id,
                user_id="later",
                source="LIVE",
                device_event_time=utc_now(),
                captured_at=utc_now(),
            )
        )
        db.commit()
    start(sessions, job_id)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        repair.control(db, job, action="STOP", actor="operator")
        db.commit()
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        assert job.status == "STOPPED"
        assert repair.counts(db, job)["stopped"] == 1
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 2
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 0


def test_overlapping_approvals_and_actor_expiry_are_rejected(store):
    sessions, connector_id, uid = store
    first = check(sessions, connector_id)
    second = check(sessions, connector_id, "check-request-2")
    start(sessions, first)
    with pytest.raises(RecoveryError, match="already working"):
        start(sessions, second)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == second))
        with pytest.raises(RecoveryError, match="admin account"):
            repair.start_check(db, job, actor="someone-else", signature="bad")
        job.preview_expires_at = utc_now() - timedelta(seconds=1)
        with pytest.raises(RecoveryError):
            repair.start_check(db, job, actor="operator", signature="0" * 64)


def test_per_employee_history_survives_unrelated_user_change(store):
    sessions, connector_id, uid = store
    with sessions() as db:
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        start_at = utc_now() - timedelta(minutes=5)
        for index in range(2):
            replace_user_snapshot(
                db,
                connector=connector,
                snapshot=UserSnapshotRequest(
                    snapshot_id=f"history-{index}",
                    complete=True,
                    stable=True,
                    observed_at=start_at + timedelta(minutes=index),
                    users=[
                        UserSnapshotRow(uid="7", user_id="1007", name="Correct Name-3520212345671"),
                        UserSnapshotRow(
                            uid="8", user_id="1008", name=f"Other {index}-3520212345672"
                        ),
                    ],
                ),
            )
        db.flush()
        event.device_event_time = start_at + timedelta(seconds=20)
        event.identity_terminal_fingerprint = None
        evidence = identity_evidence(db, event, connector)
        assert evidence and evidence.proof["kind"] == "RETAINED_INTERVAL"
        assert (
            db.scalar(
                select(func.count(AttendanceIdentityHistory.id)).where(
                    AttendanceIdentityHistory.device_user_id == event.device_user_id,
                    AttendanceIdentityHistory.closed.is_(False),
                )
            )
            == 1
        )


def test_check_batches_have_no_legacy_preview_limit(store, monkeypatch):
    sessions, connector_id, uid = store
    monkeypatch.setattr(settings, "attendance_safe_repair_batch_size", 100)
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        # Genuine database cursor processing, not mocked counts or source-text assertions.
        rows = [
            dict(
                event_uid=hashlib.sha256(f"saved-{i}".encode()).hexdigest(),
                connector_id=original.connector_id,
                zkt_device_id=original.zkt_device_id,
                device_serial=original.device_serial,
                user_id="unknown",
                source="FULL_HISTORY",
                device_event_time=original.device_event_time,
                captured_at=original.captured_at,
                identity_resolution_status="BLOCKED_PROVENANCE",
                ords_status="BLOCKED_IDENTITY",
            )
            for i in range(5001)
        ]
        db.execute(AttendanceEvent.__table__.insert(), rows)
        db.commit()
    job_id = check(sessions, connector_id)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        assert repair.counts(db, job)["checked"] == 5002
        assert repair.counts(db, job)["ready"] == 1
        assert repair.items_page(db, job, limit=25)["next_cursor"]


def test_automatic_recheck_is_opt_in_and_scoped(store, monkeypatch):
    sessions, connector_id, _ = store
    with sessions() as db:
        repair.schedule_automatic(db)
        assert db.scalar(select(func.count(AttendanceRecoveryJob.id))) == 0
        monkeypatch.setattr(settings, "attendance_safe_repair_automatic_enabled", True)
        repair.schedule_automatic(db)
        db.flush()
        assert db.scalar(select(func.count(AttendanceRecoveryJob.id))) == 1
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(AttendanceRecoveryJob)).status == "RUNNING"


def test_repaired_evidence_is_rechecked_before_network_claim(store):
    from zk_add.worker import claim_ords_batch

    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        db.get(DeviceUser, event.device_user_id).identity_conflict_code = "DUPLICATE_CNIC"
        db.commit()
    assert claim_ords_batch(10) == []
    with sessions() as db:
        assert db.scalar(select(OrdsOutbox)).status == "BLOCKED_IDENTITY"


def test_bulk_gets_service_even_with_batch_size_one(store):
    from zk_add.worker import claim_ords_batch
    from zk_add.service import ensure_attendance_ords_outbox

    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        for index in range(10):
            event = AttendanceEvent(
                event_uid=hashlib.sha256(f"live-{index}".encode()).hexdigest(),
                connector_id=original.connector_id,
                zkt_device_id=original.zkt_device_id,
                device_serial=original.device_serial,
                user_id=original.user_id,
                uid=original.uid,
                cnic_encrypted=original.cnic_encrypted,
                cnic_lookup_hash=original.cnic_lookup_hash,
                source="LIVE",
                identity_resolution_status="RESOLVED",
                clock_quality="OK",
                device_event_time=utc_now(),
                captured_at=utc_now(),
                ords_status="PENDING",
            )
            db.add(event)
            db.flush()
            ensure_attendance_ords_outbox(db, event)
        db.commit()
    claimed = [claim_ords_batch(1)[0][1]["event_uid"] for _ in range(5)]
    assert claimed[4] == uid
    assert all(value != uid for value in claimed[:4])


def test_pause_observes_receipts_without_admitting_more(store):
    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        repair.control(db, job, action="PAUSE", actor="operator")
        repair.control(db, job, action="PAUSE", actor="operator")
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 0
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        repair.control(db, job, action="RESUME", actor="operator")
        db.commit()
    tick(sessions)
    with sessions() as db:
        job = db.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.job_id == job_id))
        repair.control(db, job, action="PAUSE", actor="operator")
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        event.ords_status, event.oracle_confirmed_at = "ACKED_CHECK", utc_now()
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(AttendanceRecoveryJob)).status == "COMPLETED"


def test_checks_api_requires_session_csrf_and_password(store, monkeypatch):
    from fastapi.testclient import TestClient
    import zk_add.web as web
    from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password

    sessions, connector_id, _ = store
    monkeypatch.setattr(web, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "admin_username", "operator")
    monkeypatch.setattr(
        settings, "admin_password_hash", hash_admin_password("test-repair-password")
    )
    client = TestClient(web.app)
    assert client.get("/api/v2/attendance-recovery/checks").status_code == 401
    with sessions() as db:
        cookie, admin = create_admin_session(
            db, username="operator", ip_address=None, user_agent=None
        )
        csrf = admin.csrf_token
        db.commit()
    client.cookies.set(ADMIN_COOKIE, cookie)
    body = {"connector_ids": [connector_id], "idempotency_key": "api-repair-check"}
    assert client.post("/api/v2/attendance-recovery/checks", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    response = client.post("/api/v2/attendance-recovery/checks", json=body)
    assert response.status_code == 202, response.text
    check_id = response.json()["job_id"]
    assert client.post("/api/v2/attendance-recovery/checks", json=body).json()["job_id"] == check_id
    tick(sessions)
    result = client.get(f"/api/v2/attendance-recovery/checks/{check_id}").json()
    start_body = {
        "action": "SAFE_REPAIR",
        "check_id": check_id,
        "signature": result["signature"],
        "password": "wrong",
    }
    assert client.post("/api/v2/attendance-recovery/jobs", json=start_body).status_code == 403
    start_body["password"] = "test-repair-password"
    started = client.post("/api/v2/attendance-recovery/jobs", json=start_body)
    assert started.status_code == 201, started.text
    assert started.json()["status"] == "RUNNING"
    assert "test-repair-password" not in started.text


def test_hikvision_verified_identity_reuses_durable_delivery(store):
    from zk_add.crypto import encrypt_cnic, cnic_lookup

    sessions, connector_id, uid = store
    with sessions() as db:
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        connector.firmware_family = "hikvision"
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        event.cnic_encrypted = encrypt_cnic("3520212345671")
        event.cnic_lookup_hash = cnic_lookup("3520212345671")
        event.identity_content_status = "VERIFIED"
        db.commit()
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(AttendanceRecoveryItem)).status == "WAITING_ORACLE"
        assert (
            db.scalar(select(AttendanceEvent)).identity_resolution_status
            == "RESOLVED_HIKVISION_NAME"
        )


def test_all_saved_coverage_includes_holds_outside_latest_reconciliation(store):
    sessions, connector_id, _ = store
    with sessions() as db:
        coverage = repair.delivery_coverage(db, connector_id)
        assert coverage["total"] == coverage["identity_held"] == 1
        assert coverage["confirmed"] == 0
        assert coverage["scope"] == "ALL_SAVED_ATTENDANCE"


def test_source_record_withdrawal_prevents_delivery(store):
    from zk_add.models import TerminalRecordManifest

    sessions, connector_id, uid = store
    job_id = check(sessions, connector_id)
    start(sessions, job_id)
    tick(sessions)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        manifest = db.scalar(
            select(TerminalRecordManifest).where(
                TerminalRecordManifest.attendance_event_id == event.id
            )
        )
        manifest.canonical_source = False
        db.flush()
        assert not repair.delivery_proof_valid(db, event, connector)


def test_automatic_sweeps_do_not_duplicate_unrepairable_record_storage(store, monkeypatch):
    sessions, connector_id, uid = store
    monkeypatch.setattr(settings, "attendance_safe_repair_automatic_enabled", True)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == uid))
        event.user_id = "unknown"
        db.flush()
        job = repair.create_check(
            db,
            actor="system:test",
            key="auto-held-check",
            connector_ids=[connector_id],
            automatic=True,
        )
        repair.advance_once(db)
        db.flush()
        assert repair.counts(db, job)["checked"] == 1
        assert repair.counts(db, job)["review"] == 1
        assert db.scalar(select(func.count(AttendanceRecoveryItem.id))) == 0
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 1
