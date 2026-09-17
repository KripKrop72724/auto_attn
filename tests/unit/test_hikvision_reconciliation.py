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
