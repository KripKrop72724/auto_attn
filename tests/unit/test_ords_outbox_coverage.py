"""Held punches without an ORDS outbox row are held by design, not missing.

Production on 22 Oct 2026 reported 75,145 "missing outbox" rows while every
unacknowledged event was identity-held or quarantined. These tests pin the
split on SQLite and, when a PostgreSQL URL is configured, on PostgreSQL.
"""

from datetime import timedelta
import os
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from test_add_backend import connector_fixture, event, make_writable, snapshot_user
from zk_add import web
from zk_add.attendance_manual_guard import generic_confirmation_allowed
from zk_add.attendance_recovery import (
    advance_attendance_recovery_jobs,
    build_recovery_preview,
    create_recovery_job,
    recovery_summary,
)
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision,
    AttendanceRecoveryItem,
    AttendanceRecoveryJob,
    Connector,
    OracleReceipt,
    OrdsOutbox,
)
from zk_add.ords_outbox_coverage import (
    IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES,
    ords_outbox_coverage,
    outbox_coverage_bucket,
)
from zk_add.schemas import AttendanceRecoveryFilter, OracleReceiptBatchRequest
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.service import (
    ensure_attendance_ords_outbox,
    ingest_attendance,
    record_oracle_receipts,
    repair_attendance_delivery_backlog,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.web import app, get_db
from zk_add.worker import ords_delivery_metrics


def _postgres_url() -> str | None:
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    return url if url and url.startswith("postgresql") else None


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, monkeypatch) -> Session:
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        try:
            with Session(engine) as session:
                yield session
        finally:
            app.dependency_overrides.clear()
            engine.dispose()
        return
    url = _postgres_url()
    if url is None:
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "outbox_coverage_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            yield session
            session.rollback()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _row(db: Session, event_uid: str) -> AttendanceEvent:
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid))
    assert row is not None
    return row


def _outbox(db: Session, row: AttendanceEvent) -> OrdsOutbox | None:
    return db.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id))


def _fleet(db: Session) -> dict[str, AttendanceEvent]:
    """One terminal with each kind of unacknowledged, outbox-less punch."""
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_user(db, connector)
    future = event(event_uid="c" * 64).model_copy(
        update={"device_event_time": utc_now() + timedelta(days=400)}
    )
    accepted, duplicates = ingest_attendance(db, connector=connector, events=[
        event(event_uid="a" * 64),  # known CNIC: queued for Oracle
        event(event_uid="b" * 64, user_id="unknown", uid="99"),  # identity-held
        future,  # impossible terminal time: quarantined
        event(event_uid="d" * 64),
        event(event_uid="e" * 64),
    ])
    assert len(accepted) == 5 and duplicates == []
    db.flush()
    rows = {name: _row(db, uid * 64) for name, uid in (
        ("queued", "a"), ("held", "b"), ("invalid_time", "c"), ("confirmed", "d"), ("unknown", "e"))}
    assert rows["queued"].ords_status == "PENDING" and _outbox(db, rows["queued"]) is not None
    # ZKT ingestion deliberately creates no Oracle work item for these two.
    assert rows["held"].ords_status == "BLOCKED_IDENTITY" and _outbox(db, rows["held"]) is None
    assert rows["invalid_time"].ords_status == "QUARANTINED_INVALID_DEVICE_TIME"
    assert _outbox(db, rows["invalid_time"]) is None
    # A confirmed punch needs no outbox; an unknown state is never hidden.
    for name, status in (("confirmed", "ACKED_CHECK"), ("unknown", "SOME_FUTURE_STATE")):
        db.delete(_outbox(db, rows[name]))
        rows[name].ords_status = status
    db.flush()
    return rows


@pytest.mark.parametrize(
    ("status", "bucket"),
    [
        # The custody boundary's identity holds (attendance_manual_guard.HELD).
        ("BLOCKED_IDENTITY", "held"),
        ("QUARANTINED_IDENTITY_REUSE", "held"),
        ("WAITING_FOR_SNAPSHOT", "held"),
        ("blocked_identity", "held"),
        # Deliverable states: an outbox-less row is a real gap.
        ("PENDING", "missing"),
        ("FAILED_RETRYABLE", "missing"),
        ("RETRYING", "missing"),
        ("IN_FLIGHT", "missing"),
        ("FIRMWARE_RECEIPT_UNVERIFIED", "missing"),
        ("FIRMWARE_RECEIPT_VERIFYING", "missing"),
        ("ACKED_FIRMWARE", "missing"),
        ("MEMBERSHIP_REVERIFYING", "missing"),
        ("MEMBERSHIP_REVERIFY_RETRY", "missing"),
        # Non-identity quarantines and unknown states stay visible as review.
        ("QUARANTINED_INVALID_DEVICE_TIME", "review"),
        ("QUARANTINED_INVALID_EVENT_UID", "review"),
        ("QUARANTINED_MISSING_TERMINAL_PROVENANCE", "review"),
        ("QUARANTINED_ORDS_REJECTED", "review"),
        ("QUARANTINED_SOURCE_CONFLICT", "review"),
        ("SOME_FUTURE_STATE", "review"),
        ("", "review"),
        (None, "review"),
    ],
)
def test_every_status_has_one_coverage_bucket(status, bucket):
    assert outbox_coverage_bucket(status) == bucket


@pytest.mark.parametrize(
    ("status", "bucket"),
    [
        # Force release and Direct ORDS create the outbox with the decision, so
        # a decided hold without one is a lost work item, not a hold.
        ("BLOCKED_IDENTITY", "missing"),
        ("QUARANTINED_IDENTITY_REUSE", "missing"),
        ("WAITING_FOR_SNAPSHOT", "missing"),
        ("PENDING", "missing"),
        # A decision never folds a review state into missing or held.
        ("QUARANTINED_INVALID_DEVICE_TIME", "review"),
        ("SOME_FUTURE_STATE", "review"),
    ],
)
def test_a_release_decision_moves_only_held_rows_to_missing(status, bucket):
    assert outbox_coverage_bucket(status, release_decided=True) == bucket


def _decide(db: Session, row: AttendanceEvent) -> None:
    """Record a Force release decision without its outbox: a lost work item."""
    job = AttendanceRecoveryJob(
        action="FORCE_RELEASE",
        actor="test-admin",
        reason="Test approval whose outbox row is missing.",
        idempotency_key=uuid4().hex,
        candidate_digest="0" * 64,
    )
    db.add(job)
    db.flush()
    item = AttendanceRecoveryItem(
        job_id=job.id,
        source_kind="ATTENDANCE_EVENT",
        source_ref=row.event_uid,
        attendance_event_id=row.id,
        expected_state_digest="0" * 64,
        lane="IDENTITY_HELD",
    )
    db.add(item)
    db.flush()
    db.add(AttendanceForceReleaseDecision(
        item_id=item.id,
        attendance_event_id=row.id,
        job_id=job.id,
        actor="test-admin",
        reason_encrypted="sealed",
        proof={},
        prior_state_encrypted="sealed",
        payload_encrypted="sealed",
        payload_digest="0" * 64,
        operation_id=str(uuid4()),
    ))
    db.flush()


def test_held_and_quarantined_rows_are_not_reported_as_missing_outbox(db: Session):
    rows = _fleet(db)

    coverage = ords_outbox_coverage(db)
    assert coverage == {
        "missing_outbox": 0,
        "held_without_outbox": 1,
        "review_without_outbox": 2,
        "decided_held_without_outbox": 0,
        "without_outbox_total": 3,
        "without_outbox_by_status": {
            "BLOCKED_IDENTITY": 1,
            "QUARANTINED_INVALID_DEVICE_TIME": 1,
            "SOME_FUTURE_STATE": 1,
        },
    }

    # A deliverable punch that lost its outbox row is the only real gap.
    db.delete(_outbox(db, rows["queued"]))
    db.flush()
    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 1
    assert coverage["held_without_outbox"] == 1
    assert coverage["without_outbox_by_status"]["PENDING"] == 1

    metrics = ords_delivery_metrics(db)
    summary = recovery_summary(db)["delivery"]
    assert metrics["missing_outbox"] == summary["missing_outbox"] == 1
    assert metrics["held_without_outbox"] == summary["held_without_outbox"] == 1
    assert metrics["review_without_outbox"] == summary["review_without_outbox"] == 2
    assert metrics["without_outbox_total"] == 4
    assert metrics["historical_sweep"]["counter_scope"] == "CUMULATIVE_SINCE_STARTED_AT"


def test_sweep_rebuilds_only_the_real_gap_and_never_queues_a_held_punch(db: Session):
    rows = _fleet(db)
    db.delete(_outbox(db, rows["queued"]))
    db.flush()
    assert ords_outbox_coverage(db)["missing_outbox"] == 1

    for _ in range(3):
        repair_attendance_delivery_backlog(db, limit=10)
        db.flush()

    restored = _outbox(db, rows["queued"])
    assert restored is not None and restored.status == "PENDING"
    assert db.scalars(select(OrdsOutbox.attendance_event_id)).all().count(rows["queued"].id) == 1
    assert rows["held"].ords_status == "BLOCKED_IDENTITY" and _outbox(db, rows["held"]) is None
    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 0
    assert coverage["held_without_outbox"] == 1
    assert coverage["review_without_outbox"] == 2


def test_overview_runs_the_outbox_anti_join_once_and_reports_the_split(db: Session, monkeypatch):
    _fleet(db)
    calls = []

    def counted(session):
        calls.append(session)
        return ords_outbox_coverage(session)

    def forbidden(_session):
        raise AssertionError("the overview must pass its single coverage result through")

    monkeypatch.setattr(web, "ords_outbox_coverage", counted)
    monkeypatch.setattr("zk_add.worker.ords_outbox_coverage", forbidden)
    monkeypatch.setattr("zk_add.attendance_recovery.ords_outbox_coverage", forbidden)
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    response = client.get("/api/v1/overview")
    assert response.status_code == 200
    assert len(calls) == 1
    body = response.json()
    for section in (body["ords_delivery"], body["attendance_recovery"]["delivery"]):
        assert section["missing_outbox"] == 0
        assert section["held_without_outbox"] == 1
        assert section["review_without_outbox"] == 2
    assert body["ords_delivery"]["without_outbox_by_status"] == {
        "BLOCKED_IDENTITY": 1,
        "QUARANTINED_INVALID_DEVICE_TIME": 1,
        "SOME_FUTURE_STATE": 1,
    }


def test_identity_holds_count_as_held_and_placeholder_rows_are_not_counted(db: Session):
    """Production's 3,196 identity-reuse quarantines are held, not review.

    A Hikvision or journal punch keeps a held placeholder outbox, and a sweep
    quarantine writes one too, so neither belongs to any outbox-less bucket.
    """
    _fleet(db)
    connector = db.get(Connector, _row(db, "b" * 64).connector_id)
    accepted, _ = ingest_attendance(db, connector=connector, events=[
        event(event_uid="1" * 64, user_id="reused", uid="91"),
        event(event_uid="2" * 64, user_id="waiting", uid="92"),
        event(event_uid="3" * 64, user_id="hik-style", uid="93"),
        event(event_uid="4" * 64, user_id="swept", uid="94"),
    ])
    assert len(accepted) == 4
    db.flush()
    reused, waiting, placeholder, swept = (_row(db, uid * 64) for uid in "1234")
    for row in (reused, waiting, placeholder, swept):
        assert row.ords_status == "BLOCKED_IDENTITY" and _outbox(db, row) is None
    reused.ords_status = "QUARANTINED_IDENTITY_REUSE"
    waiting.ords_status = "WAITING_FOR_SNAPSHOT"
    ensure_attendance_ords_outbox(db, placeholder, status="BLOCKED_IDENTITY")
    swept.ords_status = "QUARANTINED_INVALID_DEVICE_TIME"
    ensure_attendance_ords_outbox(db, swept, status="QUARANTINED_INVALID_DEVICE_TIME")
    db.flush()
    assert reused.ords_status == "QUARANTINED_IDENTITY_REUSE"
    assert waiting.ords_status == "WAITING_FOR_SNAPSHOT"

    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 0
    # b (BLOCKED_IDENTITY), the identity-reuse quarantine and WAITING_FOR_SNAPSHOT.
    assert coverage["held_without_outbox"] == 3
    # c (invalid time) and e (unknown state) only.
    assert coverage["review_without_outbox"] == 2
    assert coverage["without_outbox_total"] == 5
    assert coverage["without_outbox_by_status"] == {
        "BLOCKED_IDENTITY": 1,
        "QUARANTINED_IDENTITY_REUSE": 1,
        "QUARANTINED_INVALID_DEVICE_TIME": 1,
        "SOME_FUTURE_STATE": 1,
        "WAITING_FOR_SNAPSHOT": 1,
    }


def test_deliverable_states_the_sweep_skips_stay_missing_until_rebuild_outbox(db: Session, monkeypatch):
    """The sweep rebuilds only PENDING/FAILED_RETRYABLE/RETRYING rows.

    In-flight, firmware-receipt and membership-check events without an outbox
    stay missing after every sweep pass; the runbook's Rebuild missing outbox
    action is what queues them, once each.
    """
    monkeypatch.setattr(settings, "attendance_recovery_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_recovery_execution_enabled", True)
    monkeypatch.setattr(settings, "attendance_recovery_allowed_zones", "")
    _fleet(db)
    connector = db.get(Connector, _row(db, "a" * 64).connector_id)
    statuses = ("IN_FLIGHT", "FIRMWARE_RECEIPT_UNVERIFIED", "MEMBERSHIP_REVERIFYING")
    uids = ("5" * 64, "6" * 64, "7" * 64)
    accepted, _ = ingest_attendance(
        db, connector=connector, events=[event(event_uid=uid) for uid in uids]
    )
    assert len(accepted) == 3
    db.flush()
    stranded = []
    for uid, status in zip(uids, statuses):
        row = _row(db, uid)
        db.delete(_outbox(db, row))
        row.ords_status = status
        stranded.append(row)
    db.flush()
    assert ords_outbox_coverage(db)["missing_outbox"] == 3

    for _ in range(3):
        repair_attendance_delivery_backlog(db, limit=50)
        db.flush()
    assert all(_outbox(db, row) is None for row in stranded)
    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 3
    assert {status: coverage["without_outbox_by_status"][status] for status in statuses} == {
        status: 1 for status in statuses
    }

    filters = AttendanceRecoveryFilter(zone_id=connector.zone_id)
    preview = build_recovery_preview(db, action="REBUILD_OUTBOX", filters=filters)
    eligible = {row["attendance_event_id"] for row in preview["rows"]}
    assert {row.id for row in stranded} <= eligible
    # Held, quarantined and unknown-state punches are never rebuild candidates.
    for uid in ("b" * 64, "c" * 64, "e" * 64):
        assert _row(db, uid).id not in eligible
    create_recovery_job(
        db,
        action="REBUILD_OUTBOX",
        filters=filters,
        candidate_digest=preview["candidate_digest"],
        actor="test-admin",
        reason="Queue deliverable punches whose outbox row was lost.",
        idempotency_key="missing-outbox-rebuild",
    )
    db.commit()
    for _ in range(10):
        processed = advance_attendance_recovery_jobs(db, owner="test-worker")
        db.commit()
        if not processed:
            break

    for row in stranded:
        assert db.scalar(
            select(func.count(OrdsOutbox.id)).where(OrdsOutbox.attendance_event_id == row.id)
        ) == 1
        assert _outbox(db, row).status == "PENDING"
    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 0
    assert coverage["held_without_outbox"] == 1
    assert coverage["review_without_outbox"] == 2


def test_a_held_punch_with_a_release_decision_but_no_outbox_is_missing(db: Session):
    """A stuck approval must not hide inside the held count."""
    rows = _fleet(db)
    held = rows["held"]
    # Two decisions for one punch still count it once; a decided punch that
    # has its outbox is not a gap.
    _decide(db, held)
    _decide(db, held)
    _decide(db, rows["queued"])
    assert held.ords_status == "BLOCKED_IDENTITY" and _outbox(db, held) is None

    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 1
    assert coverage["decided_held_without_outbox"] == 1
    assert coverage["held_without_outbox"] == 0
    assert coverage["review_without_outbox"] == 2
    assert coverage["without_outbox_total"] == 3
    assert coverage["without_outbox_by_status"]["BLOCKED_IDENTITY"] == 1
    metrics = ords_delivery_metrics(db)
    assert metrics["missing_outbox"] == metrics["decided_held_without_outbox"] == 1
    assert recovery_summary(db)["delivery"]["missing_outbox"] == 1

    # The sweep never queues a punch that still needs approval, so the gap
    # stays visible until someone investigates it.
    for _ in range(2):
        repair_attendance_delivery_backlog(db, limit=10)
        db.flush()
    assert held.ords_status == "BLOCKED_IDENTITY" and _outbox(db, held) is None
    assert ords_outbox_coverage(db)["decided_held_without_outbox"] == 1


def test_recovery_tiles_use_the_same_identity_hold_set_as_the_split(db: Session):
    """WAITING_FOR_SNAPSHOT is held on both tiles, and every review row without
    an outbox is also on the Source review tile."""
    rows = _fleet(db)
    rows["held"].ords_status = "WAITING_FOR_SNAPSHOT"
    db.flush()

    delivery = recovery_summary(db)["delivery"]
    assert delivery["identity_held"] == 1
    assert delivery["held_without_outbox"] == 1
    # c (invalid time) and e (unknown state).
    assert delivery["review_without_outbox"] == 2
    assert delivery["permanent_review"] == 2
    assert delivery["missing_outbox"] == 0


def test_held_punches_with_a_terminal_receipt_exist_with_and_without_an_outbox(db: Session):
    """A legacy terminal may already have posted a held punch to Oracle.

    A receipt that arrives with a punch that has no CNIC gives it a receipt
    outbox, which the worker blocks with the punch (a held placeholder). A
    receipt that arrives after the punch is held is kept as evidence only. The
    documented receipt join must find both, not only the outbox-less one.
    """
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_user(db, connector)
    early, late = "f" * 64, "9" * 64

    def receipt(event_uid: str) -> tuple[int, int, int]:
        return record_oracle_receipts(db, connector=connector, batch=OracleReceiptBatchRequest(
            confirmation_path="FIRMWARE_RECONCILE",
            oracle_observed_at=utc_now(),
            event_uids=[event_uid],
        ))

    assert receipt(early) == (0, 1, 0)
    accepted, _ = ingest_attendance(db, connector=connector, events=[
        event(event_uid=early, user_id="unknown", uid="99"),
        event(event_uid=late, user_id="unknown", uid="98"),
    ])
    assert len(accepted) == 2
    db.flush()
    early_row, late_row = _row(db, early), _row(db, late)
    early_outbox = _outbox(db, early_row)
    assert early_row.ords_status == "FIRMWARE_RECEIPT_UNVERIFIED"
    assert early_outbox is not None and early_outbox.status == "FIRMWARE_RECEIPT_UNVERIFIED"
    # worker.claim_firmware_receipt_audit_batch blocks both without approval.
    assert not generic_confirmation_allowed(db, early_row)
    early_row.ords_status = early_outbox.status = "BLOCKED_IDENTITY"
    db.flush()

    assert late_row.ords_status == "BLOCKED_IDENTITY" and _outbox(db, late_row) is None
    assert receipt(late) == (0, 1, 0)
    db.flush()
    assert late_row.ords_status == "BLOCKED_IDENTITY" and _outbox(db, late_row) is None

    coverage = ords_outbox_coverage(db)
    assert coverage["missing_outbox"] == 0
    # Only the late-receipt punch lacks a row; the placeholder is not counted.
    assert coverage["held_without_outbox"] == 1
    held_with_receipt = db.execute(
        select(AttendanceEvent.id, OrdsOutbox.id)
        .join(OracleReceipt, OracleReceipt.attendance_event_id == AttendanceEvent.id)
        .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
        .where(AttendanceEvent.ords_status.in_(tuple(IDENTITY_HELD_WITHOUT_OUTBOX_STATUSES)))
    ).all()
    assert {(event_id, outbox_id is not None) for event_id, outbox_id in held_with_receipt} == {
        (early_row.id, True),
        (late_row.id, False),
    }
