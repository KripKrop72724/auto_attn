"""Re-checking stored Hikvision events after an event-code policy change."""
from contextlib import contextmanager
import importlib.util
import hashlib
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select
import pytest

from test_hikvision_reconciliation import (  # noqa: F401
    apply_page, assignment, db, device_reply, refresh_assurance, setup_job,
)
from zk_add import hikvision_recheck
from zk_add.crypto import encrypt_text
from zk_add.db import Base
from zk_add.hikvision_delivery import HikvisionPolicy, configure_policy
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.hikvision_recheck import recheck_once
from zk_add.models import AttendanceEvent, AuditEvent, OrdsOutbox
from zk_add.time_utils import utc_now


def row(serial, minor, employee=None):
    value = {"serialNo": serial, "major": 5, "minor": minor, "time": "2026-09-17T10:00:00+05:00"}
    if employee:
        value.update(employeeNoString=employee, name="Test-1234512345671")
    return value


# Face punch, door unlock, door lock, fingerprint punch, face failed.
ROWS = [row(100, 75, "00111"), row(101, 21), row(102, 22), row(103, 38, "00222"), row(104, 76)]
FIXED = ([[5, 38], [5, 75]], [[5, 21], [5, 22], [5, 76]])


def scan(session, connector, job, rows=ROWS):
    for _ in range(12):
        message = assignment(session, job, connector)
        if message is None:
            return
        apply_page(session, connector, device_reply(message, rows))
        session.flush()


def set_policy(session, connector, key, success, excluded, enabled=True):
    current = session.get(HikvisionPolicy, connector.id)
    return configure_policy(
        session, connector, terminal_serial="terminal", source_epoch=current.source_epoch,
        profile_id=current.profile_id, success_codes=success, excluded_codes=excluded,
        enabled=enabled, actor="test", reason="Mark door and failed events as non-attendance",
        idempotency_key=key,
    )


def face_only(session, connector):
    policy = session.get(HikvisionPolicy, connector.id)
    policy.success_codes, policy.excluded_codes = [[5, 75]], []
    session.flush()


def dispositions(session):
    return {e.source_event_id: e.disposition for e in session.scalars(select(HikvisionEvidence))}


def finished_face_only_job(session, connector):
    job = setup_job(session, connector)
    face_only(session, connector)
    scan(session, connector, job)
    refresh_assurance(session, job)
    assert job.status == "NEEDS_ATTENTION" and job.ords_review_count == 4
    return job


def drain(session, connector, batch=500):
    for _ in range(20):
        if not recheck_once(session, connector.id, batch=batch):
            return
    raise AssertionError("re-check did not finish")


def test_policy_save_rechecks_stored_events_and_the_held_job_completes(db):  # noqa: F811
    session, connector = db
    job = finished_face_only_job(session, connector)

    policy = set_policy(session, connector, "fix-1", *FIXED)
    assert policy.recheck_after_id == 0
    drain(session, connector)

    found = dispositions(session)
    assert found["103"] == "ATTENDANCE"
    assert {found["101"], found["102"], found["104"]} == {"NON_ATTENDANCE"}
    assert policy.recheck_after_id is None
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 2
    fingerprint = session.scalar(select(AttendanceEvent).where(AttendanceEvent.user_id == "00222"))
    assert fingerprint.source == "FULL_HISTORY" and fingerprint.raw_event["mapping_revision"] == policy.mapping_revision
    outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == fingerprint.id))
    assert outbox.status == "PENDING" and outbox.delivery_type == "FULL_HISTORY"

    # The finished job is re-assessed by the existing refresh, without a rescan.
    refresh_assurance(session, job)
    assert job.status == "RUNNING" and job.ords_review_count == 0 and job.ords_target_count == 2
    for event in session.scalars(select(AttendanceEvent)):
        event.oracle_confirmed_at = utc_now()
        event.ords_status = "ACKED"
    refresh_assurance(session, job)
    assert job.status == "COMPLETED" and job.oracle_certificate["non_attendance"] == 3

    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "HIKVISION_POLICY_RECHECK_COMPLETED"))
    assert audit.target_id == connector.connector_id
    assert audit.after["checked"] == 4 and audit.after["NON_ATTENDANCE"] == 3 and audit.after["ATTENDANCE"] == 1


def test_recheck_is_bounded_and_resumes_from_its_cursor(db):  # noqa: F811
    session, connector = db
    finished_face_only_job(session, connector)
    policy = set_policy(session, connector, "fix-2", *FIXED)

    assert recheck_once(session, connector.id, batch=2) is True
    first = policy.recheck_after_id
    assert first and policy.recheck_counts["checked"] == 2
    session.commit()  # a restart resumes from the committed cursor
    assert recheck_once(session, connector.id, batch=2) is True
    assert policy.recheck_after_id > first and policy.recheck_counts["checked"] == 4
    assert recheck_once(session, connector.id, batch=2) is False
    assert policy.recheck_after_id is None and policy.recheck_counts["checked"] == 4
    assert recheck_once(session, connector.id, batch=2) is False
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == "HIKVISION_POLICY_RECHECK_COMPLETED")) == 1


def test_only_held_events_of_the_policy_epoch_are_touched(db):  # noqa: F811
    session, connector = db
    finished_face_only_job(session, connector)
    other = HikvisionEvidence(
        connector_id=connector.id, terminal_serial="terminal", source_epoch="older-epoch",
        observation_sha256="f" * 64, event_uid=None, source_event_id="900", channel="HISTORY",
        disposition="UNCLASSIFIED_SOURCE", captured_epoch=1790000000,
        raw_encrypted=encrypt_text(json.dumps(row(900, 21))),
    )
    unreadable = HikvisionEvidence(
        connector_id=connector.id, terminal_serial="terminal", source_epoch="verified-epoch",
        observation_sha256=hashlib.sha256(b"x").hexdigest(), event_uid=None, source_event_id="901",
        channel="HISTORY", disposition="UNCLASSIFIED_SOURCE", captured_epoch=1790000000,
        raw_encrypted=encrypt_text("not json"),
    )
    session.add_all([other, unreadable])
    session.flush()
    before = dispositions(session)
    assert before["100"] == "ATTENDANCE"

    policy = set_policy(session, connector, "fix-3", *FIXED)
    drain(session, connector)

    after = dispositions(session)
    assert after["100"] == "ATTENDANCE" and after["900"] == "UNCLASSIFIED_SOURCE"
    assert after["901"] == "UNCLASSIFIED_SOURCE"
    assert policy.recheck_counts["UNREADABLE"] == 1
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 2


def test_nothing_runs_until_a_policy_is_saved_and_replays_do_not_restart(db, monkeypatch):  # noqa: F811
    session, connector = db
    finished_face_only_job(session, connector)
    policy = session.get(HikvisionPolicy, connector.id)
    policy.success_codes, policy.excluded_codes = FIXED
    session.flush()
    # An existing policy after the deploy has no cursor: stored events are untouched.
    assert policy.recheck_after_id is None
    assert recheck_once(session, connector.id) is False
    assert dispositions(session)["101"] == "UNCLASSIFIED_SOURCE"

    @contextmanager
    def same_session():
        yield session
        session.flush()

    monkeypatch.setattr(hikvision_recheck, "session_scope", same_session)
    set_policy(session, connector, "fix-4", *FIXED)
    hikvision_recheck.tick()
    assert dispositions(session)["101"] == "NON_ATTENDANCE" and policy.recheck_after_id is None
    # Replaying the same saved request is idempotent and does not queue another sweep.
    set_policy(session, connector, "fix-4", *FIXED)
    assert policy.recheck_after_id is None


def test_a_disabled_policy_keeps_its_cursor_and_changes_nothing(db):  # noqa: F811
    session, connector = db
    finished_face_only_job(session, connector)
    policy = set_policy(session, connector, "fix-5", *FIXED, enabled=False)
    assert recheck_once(session, connector.id) is False
    assert policy.recheck_after_id == 0
    assert dispositions(session)["101"] == "UNCLASSIFIED_SOURCE"


@pytest.mark.parametrize("columns_present", [False, True])
def test_migration_adds_the_cursor_columns_idempotently(columns_present):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        if not columns_present:
            for column in ("recheck_after_id", "recheck_counts"):
                connection.exec_driver_sql(f"ALTER TABLE add_hikvision_policies DROP COLUMN {column}")
        path = (Path(__file__).resolve().parents[2]
                / "apps/add_backend/migrations/versions/20261119_0063_hikvision_policy_recheck.py")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        columns = {c["name"] for c in inspect(connection).get_columns("add_hikvision_policies")}
        assert {"recheck_after_id", "recheck_counts"} <= columns
    engine.dispose()
