"""Recent-tail recovery for a Hikvision terminal whose own ESP poll is blocked.

Modelled on LF-ZONE-BLD1-01 (Oct 2026): the ESP's checkpointed poll stopped at
serial 597801 with HIK_ANCHOR_MISMATCH, so ADD reads the terminal's retained
log after that serial through the ESP's existing history-page worker.
"""
import hashlib
import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import Session

from test_hikvision_delivery import policy
from test_hikvision_evidence import db as evidence_db
from zk_add.hikvision_evidence import HikvisionEvidence, ObservationIn, preserve_observation
from zk_add.hikvision_reconciliation import (
    MODE,
    TAIL_SCOPE,
    HikvisionReconciliationState,
    apply_page,
    assignment,
    initialize,
    preflight,
    recent_tail_scope,
    tail_operator_status,
    terminal_answering,
)
from zk_add.db import Base
from zk_add.models import AttendanceEvent, Connector, ReconciliationJob, ZKTDevice
from zk_add.settings import settings
from zk_add.time_utils import utc_now

ANCHOR = 597801


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, monkeypatch):
    if request.param == "sqlite":
        yield from evidence_db.__wrapped__(monkeypatch)
        return
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-only-hikvision-lookup-key")
    schema = "hikvision_tail_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=30000"})
    try:
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        with Session(engine) as session:
            connector = Connector(connector_id="h", hardware_id="00:11:22:33:44:55", zone_id="z", zone_name="z",
                                  device_id="d", display_name="d", firmware_family="hikvision",
                                  terminal_vendor="hikvision", terminal_protocol="isapi")
            session.add(connector)
            session.flush()
            connector.zkt_device = ZKTDevice(connector_id=connector.id, serial="terminal",
                                             confirmed_serial="terminal")
            session.commit()
            yield session, connector
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def record(serial, employee="00111", time="2026-10-18T18:59:00+05:00"):
    return {"serialNo": serial, "major": 5, "minor": 75, "employeeNoString": employee,
            "name": "Test-1234512345671", "time": time}


def held_by_poll(session, connector, serials):
    for serial in serials:
        raw = json.dumps(record(serial))
        preserve_observation(session, connector, ObservationIn(
            schema_version=1, source_protocol="hikvision-isapi-v1", terminal_serial="terminal",
            source_epoch="verified-epoch", observation_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            channel="POLL", raw=raw, captured_epoch=1792500000))
    session.flush()


def tail_job(session, connector, *, poll_serials=range(ANCHOR - 30, ANCHOR + 1)):
    policy(session, connector)
    held_by_poll(session, connector, poll_serials)
    job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
                            reason="test", idempotency_key="tail", request_digest="b" * 64,
                            terminal_serial="terminal", mode=MODE)
    session.add(job)
    session.flush()
    initialize(session, job, connector, TAIL_SCOPE)
    session.flush()
    return job


def reply(message, rows, *, exclusive=False):
    cond = message["request"]["AcsEventCond"]
    begin, end = cond["beginSerialNo"], cond["endSerialNo"]
    scope = [r for r in rows
             if (r["serialNo"] > begin if exclusive else r["serialNo"] >= begin) and r["serialNo"] <= end]
    page = scope[:cond["maxResults"]]
    return {**message, "response": {"AcsEvent": {
        "searchID": cond["searchID"], "numOfMatches": len(page), "totalMatches": len(scope),
        "InfoList": page,
        "responseStatusStrg": "NO MATCH" if not scope else ("OK" if len(page) == len(scope) else "MORE"),
    }}}


def drive(session, connector, job, rows, *, exclusive=False, limit=100):
    """Feed pages until the job is caught up (assignment withheld for the follow interval)."""
    for _ in range(limit):
        message = assignment(session, job, connector)
        if message is None:
            return
        receipt = apply_page(session, connector, reply(message, rows, exclusive=exclusive))
        assert receipt["durable"]
        session.flush()
    raise AssertionError("tail did not catch up")


def expire_follow_interval(session, job):
    state = session.get(HikvisionReconciliationState, job.id)
    state.data = {**state.data, "follow_after": (utc_now() - timedelta(seconds=1)).isoformat()}
    session.flush()


def terminal_log(last):
    return [record(serial) for serial in range(ANCHOR - 100, last + 1)]


def attendance_serials(session):
    return sorted(row.sequence for row in session.scalars(select(AttendanceEvent)))


@pytest.mark.parametrize("exclusive", [False, True])
def test_tail_resumes_after_the_poll_anchor_and_keeps_following(db, exclusive):
    session, connector = db
    job = tail_job(session, connector)
    state = session.get(HikvisionReconciliationState, job.id)
    assert state.data["begin_serial"] == ANCHOR - 19 and state.data["anchor_serial"] == ANCHOR
    first = assignment(session, job, connector)
    cond = first["request"]["AcsEventCond"]
    # Within the ESP's history-page limits: 20 rows, position 0, no pictures, bounded serials.
    assert (cond["beginSerialNo"], cond["endSerialNo"], cond["maxResults"]) == (ANCHOR - 19, 3_000_000_000, 20)
    assert cond["searchResultPosition"] == 0 and cond["picEnable"] is False
    assert (first["terminal_serial"], first["source_epoch"]) == ("terminal", "verified-epoch")
    apply_page(session, connector, reply(first, terminal_log(ANCHOR + 95), exclusive=exclusive))
    drive(session, connector, job, terminal_log(ANCHOR + 95), exclusive=exclusive)
    data = session.get(HikvisionReconciliationState, job.id).data
    assert data["caught_up"] and data["last_serial"] == ANCHOR + 95
    # Every distinct serial read once: the overlap (20, or 19 when exclusive) plus 95 new.
    assert data["records"] == 95 + (19 if exclusive else 20) and data["anchor_returned"] is True
    assert data["fact_conflicts"] == 0 and not job.review_required
    # The re-read overlap is deduplicated; only the 95 new punches became attendance.
    new = list(range(ANCHOR + 1, ANCHOR + 96))
    assert [s for s in attendance_serials(session) if s > ANCHOR] == new
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 31 + 95
    assert job.status == "RUNNING" and job.phase == "FOLLOWING_TERMINAL_TAIL"
    assert not job.capture_certified_at and not job.capture_certificate and job.cutoff_count is None
    assert tail_operator_status(job, data)[0] == "FOLLOWING_TERMINAL_TAIL"
    # Within the follow interval nothing is requested; afterwards new punches arrive.
    assert assignment(session, job, connector) is None
    expire_follow_interval(session, job)
    drive(session, connector, job, terminal_log(ANCHOR + 97), exclusive=exclusive)
    assert [s for s in attendance_serials(session) if s > ANCHOR] == list(range(ANCHOR + 1, ANCHOR + 98))


def test_reused_serials_are_held_and_later_punches_still_flow(db):
    # The terminal lost its last records at a power cut and reused serials
    # 597794-597801 for new punches: ADD must hold both versions, never merge.
    session, connector = db
    job = tail_job(session, connector)
    log = [record(s) for s in range(ANCHOR - 100, ANCHOR - 7)]
    log += [record(s, employee="00222", time="2026-10-19T07:10:00+05:00") for s in range(ANCHOR - 7, ANCHOR + 1)]
    log += [record(s, employee="00222", time="2026-10-19T07:20:00+05:00") for s in range(ANCHOR + 1, ANCHOR + 41)]
    drive(session, connector, job, log)
    data = session.get(HikvisionReconciliationState, job.id).data
    assert data["fact_conflicts"] == 8 and job.review_required and job.status == "RUNNING"
    assert "held for source review" in job.error_message
    conflicted = session.scalars(select(HikvisionEvidence).where(
        HikvisionEvidence.disposition == "SOURCE_FACT_CONFLICT")).all()
    assert sorted({int(row.source_event_id) for row in conflicted}) == list(range(ANCHOR - 7, ANCHOR + 1))
    held = session.scalars(select(AttendanceEvent).where(AttendanceEvent.sequence >= ANCHOR - 7,
                                                         AttendanceEvent.sequence <= ANCHOR)).all()
    assert len(held) == 8 and {row.ords_status for row in held} == {"QUARANTINED_SOURCE_CONFLICT"}
    # Never re-attributed: the original 00111 rows keep their identity.
    assert {row.user_id for row in held} == {"00111"}
    later = session.scalars(select(AttendanceEvent).where(AttendanceEvent.sequence > ANCHOR)).all()
    assert len(later) == 40 and {row.user_id for row in later} == {"00222"}
    assert {row.ords_status for row in later} == {"PENDING"}


@pytest.mark.parametrize("log", [[], [record(s) for s in range(ANCHOR + 5, ANCHOR + 30)]])
def test_unretained_resume_point_is_a_safety_hold(db, log):
    session, connector = db
    job = tail_job(session, connector)
    apply_page(session, connector, reply(assignment(session, job, connector), log))
    assert job.status == "NEEDS_ATTENTION" and job.error_code == "TAIL_RESUME_POINT_NOT_RETAINED"
    # Raw custody of what the terminal returned is kept; the resume point never advances.
    data = session.get(HikvisionReconciliationState, job.id).data
    assert data["last_serial"] is None and data["pages"] == 0 and "request" not in data


def test_disordered_or_out_of_scope_page_is_a_safety_hold(db):
    session, connector = db
    job = tail_job(session, connector)
    message = assignment(session, job, connector)
    body = reply(message, terminal_log(ANCHOR + 5))
    rows = body["response"]["AcsEvent"]["InfoList"]
    rows[1], rows[2] = rows[2], rows[1]
    apply_page(session, connector, body)
    assert job.status == "NEEDS_ATTENTION" and job.error_code == "SERIAL_REUSE_ORDER_OR_SCOPE_CONFLICT"


def test_page_replay_is_idempotent_and_interrupted_page_reissues_the_same_request(db):
    session, connector = db
    job = tail_job(session, connector)
    message = assignment(session, job, connector)
    session.commit()
    before = dict(session.get(HikvisionReconciliationState, job.id).data)
    apply_page(session, connector, reply(message, terminal_log(ANCHOR + 50)))
    session.rollback()
    assert session.get(HikvisionReconciliationState, job.id).data == before
    assert assignment(session, job, connector)["token"] == message["token"]
    body = reply(message, terminal_log(ANCHOR + 50))
    receipt = apply_page(session, connector, body)
    session.commit()
    assert apply_page(session, connector, body) == receipt


def test_twice_executed_open_ended_request_is_acknowledged_without_rewinding(db):
    # The ESP keeps one queued copy of an assignment, so a slow poll cycle can
    # run the same request twice; totals grow as new punches arrive meanwhile.
    session, connector = db
    job = tail_job(session, connector)
    message = assignment(session, job, connector)
    first = reply(message, terminal_log(ANCHOR + 5))
    apply_page(session, connector, first)
    before = dict(session.get(HikvisionReconciliationState, job.id).data)
    later = reply(message, terminal_log(ANCHOR + 6))
    assert later["response"] != first["response"]
    assert apply_page(session, connector, later)["durable"] is True
    assert session.get(HikvisionReconciliationState, job.id).data == before
    assert job.status == "RUNNING"
    # The tail picks the new punch up from where the first answer ended.
    drive(session, connector, job, terminal_log(ANCHOR + 6))
    assert max(attendance_serials(session)) == ANCHOR + 6
    assert message["request"]["AcsEventCond"]["beginSerialNo"] == ANCHOR - 19


def test_tail_ends_without_certification_once_the_esp_poll_works_again(db):
    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR + 10))
    terminal = connector.zkt_device
    terminal.capability_profile = {"hikvision_health": {"poll_error": 0, "durable_poll_cursor": ANCHOR + 12}}
    expire_follow_interval(session, job)
    drive(session, connector, job, terminal_log(ANCHOR + 11))
    # The ESP cursor is ahead of what the tail holds: ending now could leave a gap.
    assert job.status == "RUNNING"
    expire_follow_interval(session, job)
    drive(session, connector, job, terminal_log(ANCHOR + 14))
    assert job.status == "COMPLETED" and job.completion_outcome == "LIVE_POLL_RESTORED"
    assert not job.capture_certificate and not job.capture_certified_at and not job.oracle_certificate
    state = session.get(HikvisionReconciliationState, job.id).data
    assert tail_operator_status(job, state)[0] == "TAIL_RECOVERY_ENDED"
    assert assignment(session, job, connector) is None


def test_resume_point_uses_only_gap_free_custody(db):
    session, connector = db
    policy(session, connector)
    with pytest.raises(ValueError, match="no checkpointed live-poll record"):
        recent_tail_scope(session, connector)
    held_by_poll(session, connector, [100, 101])
    # A live-stream record beyond the poll does not move the resume point:
    # the stream can drop events while disconnected.
    raw = json.dumps({"AccessControllerEvent": {"serialNo": 900, "majorEventType": 5, "subEventType": 75,
                                                "employeeNoString": "00111", "currentEvent": True},
                      "dateTime": "2026-10-18T18:59:00+05:00"})
    preserve_observation(session, connector, ObservationIn(
        schema_version=1, source_protocol="hikvision-isapi-v1", terminal_serial="terminal",
        source_epoch="verified-epoch", observation_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        channel="STREAM", raw=raw, captured_epoch=1792500000))
    session.flush()
    assert recent_tail_scope(session, connector)["anchor_serial"] == 101
    # An earlier tail job's contiguous progress does move it.
    job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="t",
                            reason="t", idempotency_key="old-tail", request_digest="c" * 64,
                            terminal_serial="terminal", mode=MODE, status="CANCELLED")
    session.add(job)
    session.flush()
    session.add(HikvisionReconciliationState(job_id=job.id, source_epoch="verified-epoch",
                                             data={"scope": TAIL_SCOPE, "last_serial": 450}))
    session.flush()
    assert recent_tail_scope(session, connector)["anchor_serial"] == 450


def test_preflight_treats_an_anchor_mismatch_terminal_as_answering(db, monkeypatch):
    session, connector = db
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    policy(session, connector)
    held_by_poll(session, connector, [ANCHOR])
    connector.connected = True
    connector.active = True
    terminal = connector.zkt_device
    terminal.online = False
    terminal.connection_state = "OFFLINE"
    terminal.last_seen_at = utc_now()
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "poll_http_status": 200}}
    result = preflight(session, connector)
    assert terminal_answering(terminal)
    assert result["ready_now"] and result["recent_tail"] == {
        "eligible": True, "anchor_serial": ANCHOR, "begin_serial": ANCHOR - 19, "reason": None}
    # An unreachable terminal (network timeout) still waits.
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 2, "poll_stage": "identity", "poll_http_status": 0}}
    assert preflight(session, connector)["waitable_blockers"][0]["code"] == "WAITING_FOR_DEVICE"
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "poll_http_status": 200}}
    terminal.last_seen_at = utc_now() - timedelta(seconds=settings.offline_after_seconds + 5)
    assert not terminal_answering(terminal)


def test_create_and_serialize_recent_tail_job(db, monkeypatch):
    from zk_add.reconciliation import _hikvision_tail_job, create_reconciliation_job, serialize_job

    session, connector = db
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    policy(session, connector)
    held_by_poll(session, connector, [ANCHOR])
    args = dict(connector=connector, actor="test", reason="BLD1 poll anchor is blocked",
                confirmation="RECONCILE d RECENT TAIL", idempotency_key="tail-key", scope=TAIL_SCOPE)
    with pytest.raises(ValueError, match="RECONCILE d RECENT TAIL"):
        create_reconciliation_job(session, **{**args, "confirmation": "RECONCILE d FROM START"})
    job = create_reconciliation_job(session, **args)
    session.commit()
    assert _hikvision_tail_job(session, job)
    summary = serialize_job(session, job)
    assert summary["scope"] == TAIL_SCOPE
    assert summary["recent_tail"]["anchor_serial"] == ANCHOR
    assert summary["recent_tail"]["coverage_certified"] is False
    assert summary["capture_certificate"] is None and summary["progress"]["remaining"] is None
    connector.firmware_family = "zkt"
    with pytest.raises(ValueError, match="Hikvision"):
        create_reconciliation_job(session, **{**args, "idempotency_key": "zkt-tail"})
