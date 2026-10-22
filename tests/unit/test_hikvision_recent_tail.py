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
from zk_add.models import AttendanceEvent, Connector, OrdsOutbox, ReconciliationJob, ZKTDevice
from zk_add.reconciliation import serialize_job
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


def history_serials(session):
    return {int(row.source_event_id) for row in session.scalars(select(HikvisionEvidence).where(
        HikvisionEvidence.channel == "HISTORY"))}


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
    # Every distinct serial read once. Only the 95 after the anchor are new;
    # the re-read overlap (20, or 19 when exclusive) is reported apart.
    assert data["records"] == 95 and data["overlap_records"] == (19 if exclusive else 20)
    assert job.scanned_count == 95 + data["overlap_records"] and data["anchor_returned"] is True
    assert data["boundary_serial"] == ANCHOR - (18 if exclusive else 19) and data["stepbacks"] == 0
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
    assert data["last_serial"] is None and data["records"] == 0 and "request" not in data


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


def poll_health(**overrides):
    return {"hikvision_health": {"poll_error": 0, "poll_stage": "events", "poll_http_status": 200,
                                 "consecutive_poll_failures": 0, "last_successful_poll_epoch": 1792600000,
                                 "durable_poll_cursor": ANCHOR + 10, **overrides}}


def follow(session, connector, job, last):
    expire_follow_interval(session, job)
    drive(session, connector, job, terminal_log(last))


def test_tail_ends_without_certification_only_on_proof_the_esp_poll_works_again(db):
    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR + 10))
    terminal = connector.zkt_device
    terminal.last_seen_at = utc_now()
    terminal.capability_profile = poll_health()
    follow(session, connector, job, ANCHOR + 10)
    # One successful poll observed is not two.
    assert job.status == "RUNNING"
    terminal.capability_profile = poll_health(last_successful_poll_epoch=1792600010)
    follow(session, connector, job, ANCHOR + 10)
    # Two successful polls, but none has delivered a record past the stall yet.
    assert job.status == "RUNNING"
    held_by_poll(session, connector, [ANCHOR + 11])
    terminal.capability_profile = poll_health(last_successful_poll_epoch=1792600020,
                                              durable_poll_cursor=ANCHOR + 12)
    follow(session, connector, job, ANCHOR + 11)
    # The ESP cursor is ahead of what the tail holds: ending now could leave a gap.
    assert job.status == "RUNNING"
    terminal.capability_profile = poll_health(last_successful_poll_epoch=1792600030,
                                              durable_poll_cursor=ANCHOR + 12)
    follow(session, connector, job, ANCHOR + 12)
    assert job.status == "COMPLETED" and job.completion_outcome == "LIVE_POLL_RESTORED"
    assert not job.capture_certificate and not job.capture_certified_at and not job.oracle_certificate
    state = session.get(HikvisionReconciliationState, job.id).data
    assert tail_operator_status(job, state)[0] == "TAIL_RECOVERY_ENDED"
    assert assignment(session, job, connector) is None
    summary = serialize_job(session, job)
    assert summary["assurance_outcome"] == "NOT_CERTIFIED"
    assert summary["terminal"]["cutoff_serial"] is None and summary["recent_tail"]["coverage_certified"] is False


@pytest.mark.parametrize("health,seen_ago", [
    # The heartbeat an ESP sends during the first poll after a boot: poll_error
    # is its zero-initialised value and the cursor is the still-mismatching one.
    ({"poll_error": 0, "poll_stage": "checkpoint", "poll_http_status": 0, "consecutive_poll_failures": 0,
      "last_successful_poll_epoch": 0, "durable_poll_cursor": ANCHOR}, 0),
    # Firmware without a failure counter never proves recovery.
    ({"poll_error": 0, "poll_stage": "events", "poll_http_status": 200, "last_successful_poll_epoch": 1,
      "durable_poll_cursor": ANCHOR}, 0),
    # Still failing at the events stage (HIK_BINDING).
    ({"poll_error": 6, "poll_stage": "events", "poll_http_status": 200, "consecutive_poll_failures": 3280,
      "last_successful_poll_epoch": 0, "durable_poll_cursor": ANCHOR}, 0),
    # A healthy-looking but stale heartbeat.
    (poll_health()["hikvision_health"], 10_000),
])
def test_boot_failing_or_stale_health_never_ends_the_tail(db, health, seen_ago):
    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR + 10))
    held_by_poll(session, connector, [ANCHOR + 11])
    terminal = connector.zkt_device
    for step in range(4):
        terminal.last_seen_at = utc_now() - timedelta(seconds=seen_ago)
        epoch = health.get("last_successful_poll_epoch")
        terminal.capability_profile = {"hikvision_health": {
            **health, "last_successful_poll_epoch": epoch + step if epoch else epoch}}
        follow(session, connector, job, ANCHOR + 11)
    assert job.status == "RUNNING" and job.completion_outcome is None


def test_ended_tail_reports_held_conflicts(db):
    session, connector = db
    job = tail_job(session, connector)
    log = [record(s) for s in range(ANCHOR - 100, ANCHOR - 2)]
    log += [record(s, employee="00222", time="2026-10-19T07:10:00+05:00") for s in range(ANCHOR - 2, ANCHOR + 12)]
    drive(session, connector, job, log)
    held_by_poll(session, connector, [ANCHOR + 13])
    terminal = connector.zkt_device
    log += [record(ANCHOR + 12, employee="00222", time="2026-10-19T07:10:00+05:00")]
    for step in range(2):
        terminal.last_seen_at = utc_now()
        terminal.capability_profile = poll_health(last_successful_poll_epoch=1792600000 + step,
                                                  durable_poll_cursor=ANCHOR + 12)
        expire_follow_interval(session, job)
        drive(session, connector, job, log)
    assert job.status == "COMPLETED" and job.review_required
    summary = serialize_job(session, job)
    assert summary["assurance_outcome"] == "NOT_CERTIFIED"
    assert "3 changed terminal serial(s) remain held for source review" in summary["operator_message"]


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
    # A reinstalled or reset ESP restarts its poll at the terminal's newest
    # records. Serials 102 to 699 were never read, so the newer run does not
    # move the resume point past them.
    held_by_poll(session, connector, range(700, 706))
    assert recent_tail_scope(session, connector)["anchor_serial"] == 101
    # An earlier break is listed for the operator; only a full audit covers it.
    held_by_poll(session, connector, [50])
    scope = recent_tail_scope(session, connector)
    assert scope["anchor_serial"] == 101 and scope["earlier_breaks"] == [50]
    assert scope["poll_top_serial"] == 705

    def old_tail(key, begin, last):
        job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="t",
                                reason="t", idempotency_key=key, request_digest="c" * 64,
                                terminal_serial="terminal", mode=MODE, status="CANCELLED")
        session.add(job)
        session.flush()
        session.add(HikvisionReconciliationState(job_id=job.id, source_epoch="verified-epoch",
                                                 data={"scope": TAIL_SCOPE, "begin_serial": begin,
                                                       "last_serial": last}))
        session.flush()

    # A tail job that began after the resume point does not bridge the gap.
    old_tail("late-tail", 300, 900)
    assert recent_tail_scope(session, connector)["anchor_serial"] == 101
    # One that read contiguously from at or before it does move it, and then
    # the later one continues from there.
    old_tail("old-tail", 82, 450)
    assert recent_tail_scope(session, connector)["anchor_serial"] == 900


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
    # Only the recent tail may run against it: a certifying audit still waits.
    assert not result["ready_now"] and result["waitable_blockers"][0]["code"] == "WAITING_FOR_DEVICE"
    assert result["recent_tail"] == {"eligible": True, "ready_now": True, "anchor_serial": ANCHOR,
                                     "begin_serial": ANCHOR - 19, "earlier_breaks": [], "reason": None}
    assert preflight(session, connector, TAIL_SCOPE)["ready_now"]
    # An unreachable terminal (network timeout) still waits.
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 2, "poll_stage": "identity", "poll_http_status": 0}}
    assert preflight(session, connector, TAIL_SCOPE)["waitable_blockers"][0]["code"] == "WAITING_FOR_DEVICE"
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "poll_http_status": 200}}
    terminal.last_seen_at = utc_now() - timedelta(seconds=settings.offline_after_seconds + 5)
    assert not terminal_answering(terminal)
    # While a job runs (the scheduler re-checks it every cycle) no evidence scan
    # is needed: another job cannot start anyway.
    session.add(ReconciliationJob(connector_id=connector.id, zkt_device_id=terminal.id, actor="t", reason="t",
                                  idempotency_key="running", request_digest="d" * 64,
                                  terminal_serial="terminal", mode=MODE, status="RUNNING"))
    session.flush()
    assert preflight(session, connector)["recent_tail"] == {
        "eligible": False, "ready_now": False, "anchor_serial": None, "begin_serial": None, "earlier_breaks": [],
        "reason": "Finish or cancel this device's active reconciliation first."}


def test_following_tail_holds_no_scan_slot_and_skips_backlog_pause(db, monkeypatch):
    from zk_add.hikvision_delivery import HikvisionPolicy
    from zk_add.models import OrdsOutbox
    from zk_add.reconciliation import assignment_rows

    session, connector = db
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "reconciliation_device_concurrency", 1)
    following = tail_job(session, connector)
    drive(session, connector, following, terminal_log(ANCHOR + 5))
    connector.connected = connector.active = True
    terminal = connector.zkt_device
    terminal.online, terminal.last_seen_at = False, utc_now()
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "poll_http_status": 200}}
    # Another device's full audit, queued later, needs the only scan slot.
    other = Connector(connector_id="h2", hardware_id="00:11:22:33:44:66", zone_id="z", zone_name="z",
                      device_id="d2", display_name="d2", firmware_family="hikvision",
                      terminal_vendor="hikvision", terminal_protocol="isapi", connected=True, active=True)
    session.add(other)
    session.flush()
    other.zkt_device = ZKTDevice(connector_id=other.id, serial="terminal-2", confirmed_serial="terminal-2",
                                 online=True)
    session.add(HikvisionPolicy(connector_id=other.id, enabled=True, source_epoch="epoch-2",
                                terminal_serial="terminal-2", profile_id="test", success_codes=[[5, 75]],
                                excluded_codes=[[5, 21]]))
    session.flush()
    audit = ReconciliationJob(connector_id=other.id, zkt_device_id=other.zkt_device.id, actor="t", reason="t",
                              idempotency_key="other-audit", request_digest="e" * 64,
                              terminal_serial="terminal-2", mode=MODE, status="QUEUED",
                              requested_at=utc_now() + timedelta(seconds=1))
    session.add(audit)
    session.flush()
    initialize(session, audit, other)
    session.flush()
    # Between checks the caught-up tail asks nothing of its ESP, so it leaves
    # the slot to the other device.
    assert [message["job_id"] for _, message in assignment_rows(session)] == [audit.job_id]
    assert following.phase == "FOLLOWING_TERMINAL_TAIL" and following.wait_reason is None
    # With the ORDS history backlog over its pause limit, the full audit waits
    # but the tail, the terminal's only capture path, keeps following.
    session.add(OrdsOutbox(delivery_type="FULL_HISTORY", status="PENDING"))
    monkeypatch.setattr(settings, "reconciliation_history_backlog_pause", 1)
    expire_follow_interval(session, following)
    assert [message["job_id"] for _, message in assignment_rows(session)] == [following.job_id]
    assert audit.wait_reason == "HISTORY_BACKLOG_BACKPRESSURE"


def test_create_and_serialize_recent_tail_job(db, monkeypatch):
    from zk_add.reconciliation import create_reconciliation_job, hikvision_tail_job

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
    assert hikvision_tail_job(session, job)
    summary = serialize_job(session, job)
    assert summary["scope"] == TAIL_SCOPE
    assert summary["recent_tail"]["anchor_serial"] == ANCHOR
    assert summary["recent_tail"]["coverage_certified"] is False
    assert summary["capture_certificate"] is None and summary["progress"]["remaining"] is None
    connector.firmware_family = "zkt"
    with pytest.raises(ValueError, match="Hikvision"):
        create_reconciliation_job(session, **{**args, "idempotency_key": "zkt-tail"})


def test_reuse_below_the_resume_point_steps_back_until_a_held_record_matches(db):
    # The terminal lost 30 unsaved events and reused serials from ANCHOR-29:
    # the 20-record overlap alone would never read ANCHOR-29..ANCHOR-20.
    session, connector = db
    job = tail_job(session, connector, poll_serials=range(ANCHOR - 100, ANCHOR + 1))
    log = [record(s) for s in range(ANCHOR - 100, ANCHOR - 29)]
    log += [record(s, employee="00333", time="2026-10-19T07:10:00+05:00") for s in range(ANCHOR - 29, ANCHOR + 41)]
    drive(session, connector, job, log)
    data = session.get(HikvisionReconciliationState, job.id).data
    assert data["stepbacks"] == 1 and data["boundary_serial"] == ANCHOR - 39
    assert set(range(ANCHOR - 29, ANCHOR - 19)) <= history_serials(session)
    assert data["fact_conflicts"] == 30 and job.review_required and job.status == "RUNNING"
    # Only serials after the anchor are new records.
    assert data["records"] == 40 and data["overlap_records"] == 40


def test_reuse_without_a_matching_record_within_the_bound_is_a_safety_hold(db):
    session, connector = db
    job = tail_job(session, connector, poll_serials=range(ANCHOR - 300, ANCHOR + 1))
    log = [record(s, employee="00333", time="2026-10-19T07:10:00+05:00") for s in range(ANCHOR - 400, ANCHOR + 5)]
    for _ in range(20):
        message = assignment(session, job, connector)
        if message is None:
            break
        apply_page(session, connector, reply(message, log))
        if job.status == "NEEDS_ATTENTION":
            break
    data = session.get(HikvisionReconciliationState, job.id).data
    assert job.error_code == "TAIL_REUSE_BEFORE_RESUME_POINT" and data["stepbacks"] == 10
    assert data["last_serial"] is None and data["records"] == 0


def test_zero_new_records_is_reported_as_zero(db):
    # Nothing after the anchor (or new punches numbered below the resume
    # point): the doc's "escalate a zero-record catch-up" check must fire.
    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR))
    data = session.get(HikvisionReconciliationState, job.id).data
    assert data["caught_up"] and data["records"] == 0 and data["overlap_records"] == 20
    summary = serialize_job(session, job)
    assert summary["recent_tail"]["records"] == 0 and summary["recent_tail"]["overlap_records"] == 20
    assert "with 0 new record(s) after serial 597801" in summary["operator_message"]


def test_oracle_confirmed_versions_keep_their_status_when_a_serial_was_reused(db):
    session, connector = db
    job = tail_job(session, connector)
    confirmed_at = utc_now()
    for event in session.scalars(select(AttendanceEvent)):
        event.ords_status, event.oracle_confirmed_at = "CONFIRMED", confirmed_at
    for outbox in session.scalars(select(OrdsOutbox)):
        outbox.status = "CONFIRMED"
    session.flush()
    log = [record(s) for s in range(ANCHOR - 100, ANCHOR - 7)]
    log += [record(s, employee="00222", time="2026-10-19T07:10:00+05:00") for s in range(ANCHOR - 7, ANCHOR + 5)]
    drive(session, connector, job, log)
    data = session.get(HikvisionReconciliationState, job.id).data
    held = session.scalars(select(AttendanceEvent).where(AttendanceEvent.sequence >= ANCHOR - 7,
                                                         AttendanceEvent.sequence <= ANCHOR)).all()
    assert {row.ords_status for row in held} == {"CONFIRMED"}
    assert {row.status for row in session.scalars(select(OrdsOutbox).where(
        OrdsOutbox.attendance_event_id.in_([row.id for row in held])))} == {"CONFIRMED"}
    # The conflict itself stays recorded for review on both evidence rows.
    assert data["fact_conflicts"] == 8 and data["confirmed_conflicts"] == 8
    assert "already confirmed in Oracle and keep that status" in job.error_message
    assert session.scalar(select(func.count()).select_from(HikvisionEvidence).where(
        HikvisionEvidence.disposition == "SOURCE_FACT_CONFLICT")) == 16


def test_new_tail_punches_use_the_current_reconcile_lane(db):
    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR + 3))
    lanes = dict(session.execute(select(AttendanceEvent.sequence, OrdsOutbox.delivery_type).join(
        OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)).all())
    assert {lanes[s] for s in range(ANCHOR + 1, ANCHOR + 4)} == {"CURRENT_RECONCILE"}
    assert {lanes[s] for s in range(ANCHOR - 30, ANCHOR + 1)} == {"LIVE"}


def test_idle_follow_pages_add_no_rows_and_a_late_replay_keeps_its_records(db):
    from zk_add.hikvision_reconciliation import HikvisionReconciliationPage

    session, connector = db
    job = tail_job(session, connector)
    drive(session, connector, job, terminal_log(ANCHOR + 10))
    pages = session.scalar(select(func.count()).select_from(HikvisionReconciliationPage))
    progress = job.last_progress_at
    expire_follow_interval(session, job)
    idle = assignment(session, job, connector)
    apply_page(session, connector, reply(idle, terminal_log(ANCHOR + 10)))
    data = session.get(HikvisionReconciliationState, job.id).data
    assert session.scalar(select(func.count()).select_from(HikvisionReconciliationPage)) == pages
    assert job.last_progress_at == progress and data["last_page_at"] and data["pages"] > 0
    # The ESP's queued second copy of that request runs after a new punch.
    assert apply_page(session, connector, reply(idle, terminal_log(ANCHOR + 11)))["durable"]
    assert ANCHOR + 11 in history_serials(session)
    assert session.get(HikvisionReconciliationState, job.id).data["last_serial"] == ANCHOR + 10
    # A malformed replay is acknowledged but nothing out of scope is kept.
    bad = reply(idle, terminal_log(ANCHOR + 12))
    bad["response"]["AcsEvent"]["searchID"] = "other"
    assert apply_page(session, connector, bad)["durable"]
    assert ANCHOR + 12 not in history_serials(session)


def answering(connector):
    connector.connected = connector.active = True
    terminal = connector.zkt_device
    terminal.online, terminal.last_seen_at = False, utc_now()
    terminal.capability_profile = {"hikvision_health": {
        "poll_error": 6, "poll_stage": "events", "poll_http_status": 200}}


def test_catch_up_is_paced_while_the_history_backlog_is_over_its_limit(db, monkeypatch):
    from zk_add.reconciliation import assignment_rows

    session, connector = db
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    job = tail_job(session, connector)
    answering(connector)
    session.add(OrdsOutbox(delivery_type="FULL_HISTORY", status="PENDING"))
    monkeypatch.setattr(settings, "reconciliation_history_backlog_pause", 1)
    [(_, message)] = assignment_rows(session)
    apply_page(session, connector, reply(message, terminal_log(ANCHOR + 95)))
    # One page per follow interval while ORDS drains history.
    assert assignment_rows(session) == [] and job.phase == "SCANNING_TERMINAL"
    state = session.get(HikvisionReconciliationState, job.id)
    state.data = {**state.data, "last_page_at": (utc_now() - timedelta(seconds=16)).isoformat()}
    session.flush()
    [(_, message)] = assignment_rows(session)
    apply_page(session, connector, reply(message, terminal_log(ANCHOR + 95)))
    # Without a backlog the catch-up is not paced.
    monkeypatch.setattr(settings, "reconciliation_history_backlog_pause", 10)
    assert [m["job_id"] for _, m in assignment_rows(session)] == [job.job_id]


def test_attendance_repair_never_waits_on_a_recent_tail(db):
    from types import SimpleNamespace

    from zk_add.attendance_repair import RepairError, _attach_source_dependency

    session, connector = db
    job = tail_job(session, connector)
    job.status = "RUNNING"
    session.flush()
    with pytest.raises(RepairError) as raised:
        _attach_source_dependency(session, job=SimpleNamespace(), connector=connector)
    assert raised.value.code == "SOURCE_DEPENDENCY_UNAVAILABLE" and job.job_id in str(raised.value)


def test_resume_point_query_at_production_volume(db):
    # BLD1 holds hundreds of thousands of POLL records; the dialog's preflight
    # must still answer quickly on PostgreSQL.
    import time

    session, connector = db
    if session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL volume check")
    policy(session, connector)
    session.execute(text("""
        INSERT INTO add_hikvision_evidence (connector_id, terminal_serial, source_epoch, observation_sha256,
            event_uid, immutable_digest, source_event_id, channel, disposition, raw_encrypted, captured_epoch,
            created_at)
        SELECT :connector, 'terminal', 'verified-epoch', md5(s::text) || md5((s + 1)::text),
               substr(md5('uid' || s::text) || md5('uid2' || s::text), 1, 64), md5('facts' || s::text),
               s::text, 'POLL', 'ATTENDANCE', 'x', 1792500000, now()
        FROM generate_series(1, 300000) AS s
        WHERE s NOT IN (150000, 299000)
    """), {"connector": connector.id})
    session.execute(text("ANALYZE add_hikvision_evidence"))
    started = time.monotonic()
    scope = recent_tail_scope(session, connector)
    elapsed = time.monotonic() - started
    assert scope["anchor_serial"] == 298999 and scope["earlier_breaks"] == [149999]
    assert elapsed < 10, elapsed
    print(f"resume-point query over 300k POLL rows: {elapsed:.2f}s")
