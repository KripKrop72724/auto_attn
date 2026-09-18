from copy import deepcopy

import pytest
from sqlalchemy import select, func

from test_hikvision_evidence import db as evidence_db
from test_hikvision_delivery import policy
from zk_add.hikvision_reconciliation import (
    MODE,
    HikvisionReconciliationState,
    HikvisionReconciliationPage,
    initialize,
    assignment,
    apply_page,
    refresh_assurance,
)
from zk_add.models import ReconciliationJob, AttendanceEvent
from zk_add.time_utils import utc_now


@pytest.fixture
def db(monkeypatch):
    yield from evidence_db.__wrapped__(monkeypatch)


def setup_job(session, connector):
    policy(session, connector)
    job = ReconciliationJob(
        connector_id=connector.id,
        zkt_device_id=connector.zkt_device.id,
        actor="test",
        reason="test",
        idempotency_key="test",
        request_digest="a" * 64,
        terminal_serial="terminal",
        mode=MODE,
    )
    session.add(job)
    session.flush()
    initialize(session, job, connector)
    session.flush()
    return job


def device_reply(message, records):
    request = message["request"]["AcsEventCond"]
    scope = [
        r
        for r in records
        if request.get("beginSerialNo", 1)
        <= r["serialNo"]
        <= request.get("endSerialNo", 3000000000)
    ]
    start = request["searchResultPosition"]
    rows = scope[start : start + request["maxResults"]]
    return {
        **message,
        "response": {
            "AcsEvent": {
                "searchID": request["searchID"],
                "numOfMatches": len(rows),
                "totalMatches": len(scope),
                "InfoList": rows,
                "responseStatusStrg": "NO MATCH"
                if not scope
                else ("OK" if start + len(rows) == len(scope) else "MORE"),
            }
        },
    }


def records():
    return [
        {
            "serialNo": s,
            "major": 5,
            "minor": 75,
            "employeeNoString": "00111",
            "name": "Test-1234512345671",
            "time": "2026-09-17T10:00:00+05:00",
        }
        for s in [100, 105, 110]
    ]


def test_two_scans_page_replays_and_oracle_assurance_are_separate(db):
    session, connector = db
    job = setup_job(session, connector)
    for _ in range(7):
        message = assignment(session, job, connector)
        body = device_reply(message, records())
        receipt = apply_page(session, connector, body)
        session.commit()
        assert apply_page(session, connector, body) == receipt
    assert job.capture_certified_at and not job.oracle_certified_at
    assert job.ords_target_count == 3 and job.ords_pending_count == 3
    assert session.scalar(select(func.count()).select_from(HikvisionReconciliationPage)) == 7
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 3
    assert assignment(session, job, connector) is None
    for row in session.scalars(select(AttendanceEvent)):
        row.oracle_confirmed_at = utc_now()
    refresh_assurance(session, job)
    assert job.status == "COMPLETED" and job.oracle_certificate["confirmed"] == 3


def test_retained_boundary_changes_never_seal_even_when_counts_match(db):
    session, connector = db
    job = setup_job(session, connector)
    for _ in range(5):
        apply_page(session, connector, device_reply(assignment(session, job, connector), records()))
    changed = records()
    changed[0]["serialNo"] = 101
    apply_page(session, connector, device_reply(assignment(session, job, connector), changed))
    assert not job.capture_certified_at and job.status == "NEEDS_ATTENTION"
    assert job.error_code == "RETAINED_BOUNDARY_CHANGED"


def test_interrupted_page_does_not_advance_checkpoint(db):
    session, connector = db
    job = setup_job(session, connector)
    for _ in range(3):
        apply_page(session, connector, device_reply(assignment(session, job, connector), records()))
    message = assignment(session, job, connector)
    session.commit()
    state = session.get(HikvisionReconciliationState, job.id)
    before = deepcopy(state.data)
    apply_page(session, connector, device_reply(message, records()))
    session.rollback()
    assert state.data == before
    assert assignment(session, job, connector)["token"] == message["token"]


def test_empty_history_requires_two_observations(db):
    session, connector = db
    job = setup_job(session, connector)
    apply_page(session, connector, device_reply(assignment(session, job, connector), []))
    assert not job.capture_certified_at
    apply_page(session, connector, device_reply(assignment(session, job, connector), []))
    assert job.status == "COMPLETED" and job.cutoff_count == 0


def active_setup(session, connector, employees=("00111", "00222")):
    from zk_add.models import DeviceUser, DeviceUserSnapshot
    from zk_add.hikvision_delivery import HikvisionPolicy
    from zk_add.hikvision_reconciliation import EMPLOYEE_FILTER_PROFILE

    job = setup_job(session, connector)
    state = session.get(HikvisionReconciliationState, job.id)
    session.delete(state)
    session.flush()
    session.get(HikvisionPolicy, connector.id).profile_id = EMPLOYEE_FILTER_PROFILE
    terminal = connector.zkt_device
    snapshot = DeviceUserSnapshot(
        zkt_device_id=terminal.id,
        snapshot_id="active-snapshot",
        revision=1,
        state_hash="a" * 64,
        complete=True,
        stable=True,
        user_count=len(employees),
        observed_at=utc_now(),
        received_at=utc_now(),
    )
    session.add(snapshot)
    session.flush()
    terminal.identity_snapshot_id = snapshot.id
    terminal.identity_snapshot_revision = 1
    terminal.snapshot_complete = terminal.identity_snapshot_stable = True
    for employee in employees:
        session.add(
            DeviceUser(
                zkt_device_id=terminal.id,
                uid=employee,
                user_id=employee,
                display_name="Test",
                present=True,
                lifecycle_state="ACTIVE",
                snapshot_revision=1,
            )
        )
    session.flush()
    initialize(session, job, connector, "ACTIVE_USERS")
    session.flush()
    return job


def filtered_reply(message, rows):
    employee = message["request"]["AcsEventCond"].get("employeeNoString")
    return device_reply(
        message, [r for r in rows if employee is None or r["employeeNoString"] == employee]
    )


def drive_active(session, connector, job, rows, limit=500):
    for _ in range(limit):
        message = assignment(session, job, connector)
        if message is None:
            return
        payload = filtered_reply(message, rows)
        receipt = apply_page(session, connector, payload)
        session.commit()
        assert apply_page(session, connector, payload) == receipt
        assert job.status != "NEEDS_ATTENTION", job.error_code
    raise AssertionError("job did not seal")


def test_active_users_bounded_pages_empty_user_exclusion_and_frozen_membership(db):
    from zk_add.models import DeviceUser

    session, connector = db
    job = active_setup(session, connector)
    active = [{**records()[0], "serialNo": i * 3 + 100} for i in range(45)]
    former = [
        {**records()[0], "employeeNoString": "former", "serialNo": i * 3 + 101} for i in range(45)
    ]
    rows = sorted(active + former, key=lambda r: r["serialNo"])
    # A later profile change must not change this job's scope.
    session.add(
        DeviceUser(
            zkt_device_id=connector.zkt_device.id, uid="new", user_id="new", display_name="New"
        )
    )
    drive_active(session, connector, job, rows)
    assert job.capture_certified_at and job.cutoff_count == 45
    certificate = job.capture_certificate
    assert certificate["scope"] == "ACTIVE_USERS"
    assert certificate["employee_numbers"] == ["00111", "00222"]
    assert certificate["user_certificates"][1]["record_count"] == 0
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 45
    assert job.scanned_count == job.add_durable_count == 45
    assert job.status == "RUNNING" and job.ords_pending_count == 45
    for row in session.scalars(select(AttendanceEvent)):
        row.oracle_confirmed_at = utc_now()
    refresh_assurance(session, job)
    assert job.status == "COMPLETED"
    assert job.oracle_certificate["scope"] == "ACTIVE_USERS"


def test_active_scope_rejects_ignored_filter_before_ingesting_former_users(db):
    session, connector = db
    job = active_setup(session, connector)
    rows = [{**records()[0], "employeeNoString": "former"}]
    for _ in range(2):
        apply_page(session, connector, device_reply(assignment(session, job, connector), rows))
    message = assignment(session, job, connector)
    assert message["request"]["AcsEventCond"]["employeeNoString"] == "00111"
    apply_page(session, connector, device_reply(message, rows))
    assert job.status == "NEEDS_ATTENTION"
    assert job.error_code == "EMPLOYEE_FILTER_OR_SCOPE_MISMATCH"
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 0


def test_active_scope_fixed_cutoff_excludes_new_punches_and_resumes(db):
    session, connector = db
    job = active_setup(session, connector)
    rows = records()
    for _ in range(2):
        apply_page(session, connector, filtered_reply(assignment(session, job, connector), rows))
    message = assignment(session, job, connector)
    session.commit()
    apply_page(session, connector, filtered_reply(message, rows))
    session.rollback()
    assert assignment(session, job, connector)["token"] == message["token"]
    rows.append({**records()[0], "serialNo": 200})
    drive_active(session, connector, job, rows)
    assert job.capture_certificate["cutoff_serial"] == 110
    assert job.cutoff_count == 3


def test_active_scope_empty_terminal_and_retention_change(db):
    session, connector = db
    job = active_setup(session, connector)
    drive_active(session, connector, job, [])
    assert job.status == "COMPLETED" and job.cutoff_count == 0
    assert job.capture_certificate["user_count"] == 2


def test_active_scope_retention_loss_after_last_employee_cannot_seal(db):
    session, connector = db
    job = active_setup(session, connector)
    state = session.get(HikvisionReconciliationState, job.id)
    for _ in range(30):
        if state.data["phase"] == "SCOPE_VERIFY":
            break
        apply_page(
            session, connector, filtered_reply(assignment(session, job, connector), records())
        )
    assert state.data["phase"] == "SCOPE_VERIFY"
    apply_page(
        session, connector, filtered_reply(assignment(session, job, connector), records()[1:])
    )
    assert job.status == "NEEDS_ATTENTION" and not job.capture_certified_at


@pytest.mark.parametrize("bad", ["incomplete", "stale", "missing_user", "wrong_profile"])
def test_active_snapshot_must_be_complete_fresh_and_qualified(db, bad):
    from datetime import timedelta
    from zk_add.models import DeviceUser, DeviceUserSnapshot
    from zk_add.hikvision_delivery import HikvisionPolicy
    from zk_add.hikvision_reconciliation import active_user_snapshot

    session, connector = db
    active_setup(session, connector)
    if bad == "incomplete":
        connector.zkt_device.snapshot_complete = False
    elif bad == "stale":
        session.get(DeviceUserSnapshot, connector.zkt_device.identity_snapshot_id).received_at = (
            utc_now() - timedelta(minutes=16)
        )
    elif bad == "missing_user":
        session.scalar(select(DeviceUser)).present = False
    else:
        session.get(HikvisionPolicy, connector.id).profile_id = "unqualified"
    with pytest.raises(ValueError):
        active_user_snapshot(session, connector)


def test_scope_is_part_of_idempotency_identity(db):
    from zk_add.reconciliation import _request_digest

    session, connector = db
    args = dict(connector=connector, reason="Operator requested history", confirmation="same")
    assert _request_digest(**args) == _request_digest(**args, scope="ALL_RECORDS")
    assert _request_digest(**args) != _request_digest(**args, scope="ACTIVE_USERS")


def test_create_active_job_scope_audit_and_idempotent_retry(db, monkeypatch):
    from zk_add.reconciliation import create_reconciliation_job, serialize_job
    from zk_add.settings import settings

    session, connector = db
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    old = active_setup(session, connector)
    old.status = "CANCELLED"
    session.flush()
    args = dict(
        connector=connector,
        actor="test",
        reason="Active user recovery test",
        confirmation="RECONCILE d ACTIVE USERS",
        idempotency_key="new-active-key",
        scope="ACTIVE_USERS",
    )
    new = create_reconciliation_job(session, **args)
    session.commit()
    assert create_reconciliation_job(session, **args).job_id == new.job_id
    summary = serialize_job(session, new)
    assert summary["scope"] == "ACTIVE_USERS"
    assert summary["active_user_scope"]["user_count"] == 2
    assert summary["progress"]["remaining"] is None
    with pytest.raises(ValueError, match="different request"):
        create_reconciliation_job(session, **{**args, "scope": "ALL_RECORDS"})
    connector.firmware_family = "zkt"
    with pytest.raises(ValueError, match="Hikvision"):
        create_reconciliation_job(session, **{**args, "idempotency_key": "zkt-active-invalid"})
