"""A terminal's newest attendance slot is settled from a later read, never trusted.

The record shapes are the 40-byte uFace800 rows ADD received from MULTAN-01
and KARACHI-01 on 21 October 2026: the newest slot of every tail read was zero
bytes, a bare timestamp, an attendance UID with no user, unparseable text, or
a byte copy of an earlier record. A later read of the same ordinal returned
the next punch.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import reconciliation as service
from zk_add import source_newest_slot as newest_slot
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent,
    AuditEvent,
    ReconciliationCoverage,
    ReconciliationDivergence,
    ReconciliationEvent,
    ReconciliationJob,
    TerminalRecordManifest,
    TerminalSourceEpoch,
)
from zk_add.schemas import (
    AttendanceEventIn,
    Envelope,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
    ReconciliationSourceRecord,
    SourceTailChunkRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import ingest_attendance, onboard_connector, replace_user_snapshot
from zk_add.settings import settings
from zk_add.source_exceptions import list_source_exceptions, source_exception_detail
from zk_add.time_utils import utc_now
from zk_add.zkt_decode import PAKISTAN_TIME, encode_time


SERIAL = "NEWEST-SLOT-UFACE800"
FINGERPRINT = "a" * 64
ZERO = bytes(40)


def _postgres_url() -> str | None:
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None
    )
    return url if url and url.startswith("postgresql") else None


@pytest.fixture(params=["sqlite", "postgresql"])
def source_db(request, monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "reconciliation_self_healing_enabled", True)
    monkeypatch.setattr(settings, "identity_snapshot_gate_enabled", False)
    schema = None
    admin = None
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        url = _postgres_url()
        if url is None:
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "newest_slot_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
        with factory() as session:
            connector = _onboard(session)
            session.commit()
            yield session, connector, factory
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def _onboard(session: Session):
    connector, _token, _created = onboard_connector(
        session, hardware_id="e0:72:a1:d6:f3:41", zone_id="ZONE-MULTAN-01",
        zone_name="ZONE-MULTAN-01", device_id="1", firmware_version="zone-lite-2.7.5",
        expected_serial=SERIAL, actor="test", ip_address="127.0.0.1",
    )
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    zkt.serial = zkt.confirmed_serial = SERIAL
    zkt.model = "uFace800"
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = zkt.identity_snapshot_stable = True
    zkt.user_count = 2
    zkt.attendance_count = 1
    zkt.capability_profile = {
        "read_attendance": True, "history_stream_v1": True, "history_range_resume_verified": True,
    }
    users = [
        UserSnapshotRow(uid="17", user_id="44", name="Ayesha-3520212345671", privilege=0,
                        terminal_identity_fingerprint=FINGERPRINT),
        UserSnapshotRow(uid="22", user_id="50", name="Bilal-3520212345672", privilege=0,
                        terminal_identity_fingerprint="b" * 64),
    ]
    for snapshot_id, observed in (("initial", datetime(2026, 10, 1, tzinfo=timezone.utc)),
                                  ("stable", utc_now())):
        replace_user_snapshot(session, connector=connector, snapshot=UserSnapshotRequest(
            snapshot_id=f"{snapshot_id}-newest-slot", complete=True, observed_at=observed, users=users))
    zkt.identity_snapshot_stable = True
    return connector


class Punch:
    """One terminal punch: its 40-byte record and the reader's interpretation."""

    def __init__(self, *, uid: int, user_id: str, local: datetime):
        self.uid, self.user_id, self.local = uid, user_id, local
        raw = bytearray(40)
        raw[0:2] = uid.to_bytes(2, "little")
        raw[2:2 + len(user_id)] = user_id.encode()
        raw[26] = 1
        raw[27:31] = encode_time(local).to_bytes(4, "little")
        raw[31] = 1
        self.raw = bytes(raw)
        self.event_uid = hashlib.sha256(f"{SERIAL}|{user_id}|{local.isoformat()}|1".encode()).hexdigest()

    def event(self, source: str) -> AttendanceEventIn:
        return AttendanceEventIn(
            event_uid=self.event_uid, uid=str(self.uid), user_id=self.user_id,
            device_event_time=self.local.astimezone(timezone.utc), captured_at=utc_now(),
            source=source, terminal_serial=SERIAL, terminal_identity_fingerprint=FINGERPRINT,
            punch=1, status=1, clock_quality="OK",
            raw_event={"attendance_record_uid": str(self.uid)},
        )

    def row(self, ordinal: int, *, source: str = "CURRENT_RECONCILE") -> ReconciliationSourceRecord:
        return _row(ordinal, self.raw, "EVENT", None, event=self.event(source),
                    uid=str(self.uid), user_id=self.user_id)


def _row(ordinal, raw, disposition, error_code, *, event=None, uid=None, user_id=None):
    digest = hashlib.sha256(raw).hexdigest()
    return ReconciliationSourceRecord(
        ordinal=ordinal, raw_record_digest=digest,
        # Zone Lite keys a record by terminal serial and raw digest.
        terminal_record_key=hashlib.sha256(f"{SERIAL}:{digest}".encode()).hexdigest(),
        occurrence_index=ordinal + 1, disposition=disposition, event=event,
        raw_record_b64=base64.b64encode(raw).decode(), error_code=error_code,
        raw_timestamp=int.from_bytes(raw[27:31], "little"),
        observed_uid=uid if uid is not None else str(int.from_bytes(raw[0:2], "little")),
        observed_user_id=user_id,
    )


def junk(ordinal: int, shape: str) -> ReconciliationSourceRecord:
    """The newest-slot shapes ADD received from 2.7.5 uFace800 terminals."""
    raw = bytearray(40)
    if shape == "zero":
        return _row(ordinal, ZERO, "INVALID_TIME", "IMPLAUSIBLE_TERMINAL_TIME")
    if shape == "bare_timestamp":
        raw[27:31] = (383).to_bytes(4, "little")
        return _row(ordinal, bytes(raw), "INVALID_TIME", "IMPLAUSIBLE_TERMINAL_TIME")
    if shape == "uid_without_user":
        raw[0:2] = (40).to_bytes(2, "little")
        raw[27:31] = (2399188992).to_bytes(4, "little")
        return _row(ordinal, bytes(raw), "IDENTITY_UNRESOLVED", "HISTORICAL_IDENTITY_EVIDENCE_REQUIRED")
    if shape == "unparseable":
        raw[2:7] = b"\x01\x02\x03\x04\x05"
        raw[27:31] = (2919261952).to_bytes(4, "little")
        return _row(ordinal, bytes(raw), "MALFORMED", "UNPARSEABLE_TERMINAL_RECORD")
    if shape == "copied_identity_bad_time":
        # A real user's identity bytes with a 2103 timestamp: the shape that a
        # source time correction would otherwise accept for that user.
        raw[0:2] = (17).to_bytes(2, "little")
        raw[2:4] = b"44"
        raw[27:31] = (3338740992).to_bytes(4, "little")
        return _row(ordinal, bytes(raw), "INVALID_TIME", "IMPLAUSIBLE_TERMINAL_TIME",
                    uid="17", user_id="44")
    raise AssertionError(shape)


def at(minute: int) -> datetime:
    return datetime(2026, 10, 21, 17, 0, tzinfo=PAKISTAN_TIME) + timedelta(minutes=minute)


P0 = Punch(uid=17, user_id="44", local=at(0))
P1 = Punch(uid=22, user_id="50", local=at(1))
P2 = Punch(uid=17, user_id="44", local=at(2))
# The punch that later fills the slot the first tail read as its newest.
P3 = Punch(uid=22, user_id="50", local=at(13))
P4 = Punch(uid=17, user_id="44", local=at(14))


def certify_baseline(session, connector, records, *, latest=None):
    """Each device message commits on its own, as ADD's autoflush-free sessions do."""
    cutoff = len(records)
    latest = cutoff if latest is None else latest
    connector.zkt_device.attendance_count = latest
    job = service.create_reconciliation_job(
        session, connector=connector, actor="operator",
        reason="Establish certified source coverage for newest-slot tests.",
        confirmation="RECONCILE 1 FROM START", idempotency_key=f"newest-slot-baseline-{uuid4().hex}",
    )
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=cutoff, latest_terminal_count=latest,
        record_size=40, source_total_bytes=4 + latest * 40,
        first_anchor_digest=records[0].raw_record_digest,
    ))
    session.commit()
    chain = commit_chunk(session, connector, job, records)
    service.apply_reconciliation_manifest(session, connector=connector, payload=ReconciliationManifestRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=cutoff,
        latest_terminal_count=latest, final_chain_digest=chain,
    ))
    session.commit()
    assert job.capture_certified_at is not None, (job.status, job.error_code)
    return job


def commit_chunk(session, connector, job, records, *, start=0, previous=None, sequence=0):
    draft = ReconciliationChunkRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, sequence=sequence, start_ordinal=start,
        end_ordinal=start + len(records), chunk_digest="0" * 64, previous_chain_digest=previous,
        resulting_chain_digest="0" * 64, records=records,
    )
    digest = service.reconciliation_chunk_digest(draft)
    chain = service.reconciliation_chain_digest(
        previous, start_ordinal=start, end_ordinal=start + len(records), chunk_digest=digest)
    service.apply_reconciliation_chunk(session, connector=connector, payload=draft.model_copy(
        update={"chunk_digest": digest, "resulting_chain_digest": chain}))
    session.commit()
    return chain


def tail_request(session, coverage, records, *, latest, previous=None):
    start = records[0].ordinal
    previous = coverage.source_committed_chain_digest if previous is None else previous
    draft = SourceTailChunkRequest(
        source_epoch=service.source_epoch_uuid(session, coverage), terminal_serial=SERIAL,
        terminal_generation=coverage.terminal_generation, record_size=40, start_ordinal=start,
        end_ordinal=start + len(records), latest_terminal_count=latest,
        previous_chain_digest=previous, chunk_digest="0" * 64, resulting_chain_digest="0" * 64,
        records=records,
    )
    digest = service.reconciliation_chunk_digest(draft)
    chain = service.reconciliation_chain_digest(
        previous, start_ordinal=start, end_ordinal=start + len(records), chunk_digest=digest)
    return draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain})


def active_coverage(session):
    return session.scalar(select(ReconciliationCoverage).where(ReconciliationCoverage.active == True))  # noqa: E712


def canonical(session, ordinal):
    return session.scalar(select(TerminalRecordManifest).where(
        TerminalRecordManifest.ordinal == ordinal,
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
    ))


def first_tail(session, connector, newest):
    """Baseline [0,1), then a tail [1,4) whose newest slot (3) read as ``newest``."""
    baseline = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY")])
    # Oracle confirmed the baseline's punch; an operator may start another job.
    baseline.status, baseline.phase = "COMPLETED", "COMPLETED"
    coverage = active_coverage(session)
    before = coverage.source_committed_chain_digest
    request = tail_request(session, coverage, [P1.row(1), P2.row(2), newest], latest=4)
    _, chunk, duplicate, error = service.apply_source_tail_chunk(session, connector=connector, payload=request)
    assert error is None and chunk is not None and not duplicate
    connector.zkt_device.attendance_count = 4
    session.commit()
    return coverage, before, request


@pytest.mark.parametrize("shape", ["zero", "bare_timestamp", "uid_without_user", "unparseable"])
def test_newest_slot_exception_is_labelled_without_rewriting_reader_evidence(source_db, shape):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, shape))
    row = canonical(session, 3)
    assert row.disposition == junk(3, shape).disposition
    assert row.error_code == junk(3, shape).error_code  # the reader's own code is kept
    listed = {item["ordinal"]: item for item in list_source_exceptions(session)["rows"]}
    assert listed[3]["source_condition"] == newest_slot.UNSETTLED_CONDITION
    assert "not the punch in this slot" in listed[3]["operator_reason"]
    detail = source_exception_detail(session, row)
    assert detail["source_condition"] == newest_slot.UNSETTLED_CONDITION
    assert detail["oracle_action"] in {"EXCLUDED_FAIL_CLOSED", "HELD_IDENTITY_EVIDENCE_REQUIRED"}


def test_genuine_bad_clock_and_capped_range_rows_are_not_newest_slot(source_db):
    session, connector, _ = source_db
    certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY")])
    coverage = active_coverage(session)
    # A terminal clock fault mid-range (QUETTA-01's 2103 punches) and a range
    # capped before the newest slot: neither row was the newest slot.
    request = tail_request(session, coverage, [P1.row(1), junk(2, "copied_identity_bad_time"),
                                               junk(3, "zero")], latest=9)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=request)[3] is None
    session.commit()
    listed = list_source_exceptions(session)["rows"]
    assert {row["ordinal"] for row in listed} == {2, 3}
    assert all(row["source_condition"] is None and row["operator_reason"] is None for row in listed)


@pytest.mark.parametrize("live_captured", [True, False])
def test_rewound_tail_replay_settles_newest_slot_without_invalidating_coverage(source_db, live_captured):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    cursor, chain = coverage.source_committed_cursor, coverage.source_committed_chain_digest
    if live_captured:
        ingest_attendance(session, connector=connector, events=[P3.event("LIVE")])
        session.commit()
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    # A stale coverage push rewound the reader to ordinal 1. Two more punches
    # arrived, so slot 3 now holds P3 and slot 5 is the new unwritten slot.
    replay = tail_request(session, coverage, [P1.row(1), P2.row(2), P3.row(3), P4.row(4),
                                              junk(5, "bare_timestamp")], latest=6, previous=before)
    result, chunk, duplicate, error = service.apply_source_tail_chunk(
        session, connector=connector, payload=replay)
    session.commit()
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE and chunk is None and not duplicate
    assert result.active and result.capture_state != "SOURCE_COVERAGE_INVALIDATED"
    assert (result.source_committed_cursor, result.source_committed_chain_digest) == (cursor, chain)
    settled = canonical(session, 3)
    assert settled.source_kind == "TAIL" and settled.disposition in {"EVENT", "BLOCKED_IDENTITY"}
    assert settled.raw_record_digest == P3.row(3).raw_record_digest
    superseded = session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.ordinal == 3,
        TerminalRecordManifest.canonical_source == False,  # noqa: E712
    )).all()
    assert [row.raw_record_digest for row in superseded] == [hashlib.sha256(ZERO).hexdigest()]
    event = session.get(AttendanceEvent, settled.attendance_event_id)
    assert event.event_uid == P3.event_uid and event.user_id == "50"
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before + (0 if live_captured else 1)
    assert session.scalar(select(func.count(AttendanceEvent.id)).where(
        AttendanceEvent.event_uid == P3.event_uid)) == 1
    # Ordinals beyond ADD's cursor are not committed by a replay.
    assert canonical(session, 4) is None and canonical(session, 5) is None
    assert 3 not in {row["ordinal"] for row in list_source_exceptions(session)["rows"]}
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == newest_slot.SETTLED_STATE))
    assert audit is not None and audit.target_id == str(superseded[0].id)
    # The reader adopts ADD's cursor and continues; its newest slot is new junk.
    next_tail = tail_request(session, result, [P4.row(4), junk(5, "bare_timestamp")], latest=6)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=next_tail)[3] is None
    session.commit()
    assert result.source_committed_cursor == 6


def test_replay_that_changes_a_settled_row_still_invalidates(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    changed = Punch(uid=17, user_id="44", local=at(3))
    replay = tail_request(session, coverage, [P1.row(1), changed.row(2), P3.row(3), P4.row(4)],
                          latest=5, previous=before)
    result, chunk, _, error = service.apply_source_tail_chunk(session, connector=connector, payload=replay)
    assert error == "SOURCE_TAIL_REPLAY_DIVERGED" and chunk is None
    assert result.active is False and result.capture_state == "SOURCE_COVERAGE_INVALIDATED"
    assert canonical(session, 3).raw_record_digest == hashlib.sha256(ZERO).hexdigest()


def test_replay_that_still_reads_the_newest_slot_settles_nothing(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    cursor = coverage.source_committed_cursor
    # No punch since: the replay ends at the same unwritten slot, with other bytes.
    replay = tail_request(session, coverage, [P1.row(1), P2.row(2), junk(3, "bare_timestamp")],
                          latest=4, previous=before)
    result, _, _, error = service.apply_source_tail_chunk(session, connector=connector, payload=replay)
    session.commit()
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE and result.active
    assert result.source_committed_cursor == cursor
    assert canonical(session, 3).raw_record_digest == hashlib.sha256(ZERO).hexdigest()
    assert session.scalar(select(func.count(TerminalRecordManifest.id)).where(
        TerminalRecordManifest.ordinal == 3)) == 1


def test_superseded_replay_answers_with_current_coverage_over_the_device_stream(source_db, monkeypatch):
    from zk_add import web
    session, connector, factory = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "uid_without_user"))
    connector.boot_id, connector.last_sequence = "newest-slot-boot", 10
    session.commit()
    monkeypatch.setattr("zk_add.db.SessionLocal", factory)
    replay = tail_request(session, coverage, [P1.row(1), P2.row(2), P3.row(3), junk(4, "zero")],
                          latest=5, previous=before)
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()),
                        boot_id=connector.boot_id, seq=11, sent_at=utc_now(), type="source_tail_chunk",
                        payload=replay.model_dump(mode="json"))
    outcome = web.persist_envelope(connector.id, envelope)
    assert outcome.ack["type"] == "error"
    assert outcome.ack["code"] == newest_slot.REPLAY_SUPERSEDED_CODE
    assert outcome.ack["committed_next_ordinal"] == 4
    assert outcome.coverage["type"] == "source_coverage" and outcome.coverage["active"] is True
    assert outcome.coverage["source_committed_cursor"] == 4
    assert outcome.coverage["source_committed_chain_digest"] == coverage.source_committed_chain_digest
    assert outcome.coverage["terminal_serial"] == SERIAL
    json.dumps(outcome.coverage)
    with factory() as db:
        connector_row = db.get(type(connector), connector.id)
        assert connector_row.lifecycle_state != "DEGRADED"
        assert db.scalar(select(TerminalRecordManifest.disposition).where(
            TerminalRecordManifest.ordinal == 3,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
        )) in {"EVENT", "BLOCKED_IDENTITY"}


@pytest.mark.parametrize("live_captured", [True, False])
def test_same_epoch_reconcile_settles_newest_slot_instead_of_diverging(source_db, live_captured):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    if live_captured:
        ingest_attendance(session, connector=connector, events=[P3.event("LIVE")])
        session.commit()
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    epoch = session.scalar(select(TerminalSourceEpoch))
    connector.zkt_device.attendance_count = 5
    job = service.create_reconciliation_job(
        session, connector=connector, actor="operator", reason="Re-read terminal source after deployment.",
        confirmation="RECONCILE 1 FROM START", idempotency_key="newest-slot-reread",
    )
    assert job.source_epoch_id == epoch.id
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=5, latest_terminal_count=5,
        record_size=40, source_total_bytes=4 + 5 * 40, first_anchor_digest=P0.row(0).raw_record_digest,
    ))
    session.commit()
    history = [P0.row(0, source="FULL_HISTORY"), P1.row(1, source="FULL_HISTORY"),
               P2.row(2, source="FULL_HISTORY"), P3.row(3, source="FULL_HISTORY"), junk(4, "zero")]
    commit_chunk(session, connector, job, history)
    session.commit()
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 5
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    settled = canonical(session, 3)
    assert settled.source_kind == "BASELINE" and settled.job_id == job.id
    assert settled.raw_record_digest == P3.row(3).raw_record_digest
    assert session.get(AttendanceEvent, settled.attendance_event_id).event_uid == P3.event_uid
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before + (0 if live_captured else 1)
    states = session.scalars(select(ReconciliationEvent.state).where(ReconciliationEvent.job_id == job.id)).all()
    assert newest_slot.SETTLED_STATE in states
    # This job's own final ordinal was the newest slot when anchored.
    assert newest_slot.is_unsettled_newest(session, canonical(session, 4))
    assert not newest_slot.is_unsettled_newest(session, settled)


def test_same_epoch_reconcile_that_reads_the_slot_as_newest_again_keeps_it(source_db):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    job = service.create_reconciliation_job(
        session, connector=connector, actor="operator", reason="Re-read terminal source after deployment.",
        confirmation="RECONCILE 1 FROM START", idempotency_key="newest-slot-reread-same",
    )
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=4, latest_terminal_count=4,
        record_size=40, source_total_bytes=4 + 4 * 40, first_anchor_digest=P0.row(0).raw_record_digest,
    ))
    session.commit()
    commit_chunk(session, connector, job, [P0.row(0, source="FULL_HISTORY"), P1.row(1, source="FULL_HISTORY"),
                                           P2.row(2, source="FULL_HISTORY"), junk(3, "bare_timestamp")])
    session.commit()
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 4
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert canonical(session, 3).raw_record_digest == hashlib.sha256(ZERO).hexdigest()
    assert session.scalar(select(func.count(TerminalRecordManifest.id)).where(
        TerminalRecordManifest.ordinal == 3)) == 1


def test_same_epoch_reconcile_still_diverges_on_a_changed_settled_row(source_db):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    job = service.create_reconciliation_job(
        session, connector=connector, actor="operator", reason="Re-read terminal source after deployment.",
        confirmation="RECONCILE 1 FROM START", idempotency_key="newest-slot-reread-changed",
    )
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=5, latest_terminal_count=5,
        record_size=40, source_total_bytes=4 + 5 * 40, first_anchor_digest=P0.row(0).raw_record_digest,
    ))
    session.commit()
    changed = Punch(uid=17, user_id="44", local=at(3))
    commit_chunk(session, connector, job, [P0.row(0, source="FULL_HISTORY"), P1.row(1, source="FULL_HISTORY"),
                                           changed.row(2, source="FULL_HISTORY"),
                                           P3.row(3, source="FULL_HISTORY"), junk(4, "zero")])
    assert job.phase == "VERIFYING_SOURCE_CHANGE"
    assert session.scalar(select(ReconciliationDivergence.ordinal)) == 2


@pytest.mark.parametrize("latest, newest", [(2, True), (3, False)])
def test_baseline_final_ordinal_is_newest_only_when_the_anchor_ended_there(source_db, latest, newest):
    session, connector, _ = source_db
    job = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), junk(1, "zero")],
                           latest=latest)
    row = canonical(session, 1)
    assert row.job_id == job.id
    assert newest_slot.is_unsettled_newest(session, row) is newest


def test_recovery_epoch_copy_is_judged_by_the_read_it_copies(source_db):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    original = canonical(session, 3)
    epoch = session.scalar(select(TerminalSourceEpoch))
    replacement = TerminalSourceEpoch(
        zkt_device_id=epoch.zkt_device_id, terminal_generation=epoch.terminal_generation,
        sequence=epoch.sequence + 1, state="ACTIVE", parent_epoch_id=epoch.id,
        activated_at=utc_now(), created_at=utc_now(), updated_at=utc_now(),
    )
    session.add(replacement)
    session.flush()
    copy = TerminalRecordManifest(**{
        column: getattr(original, column) for column in (
            "connector_id", "zkt_device_id", "terminal_serial", "generation", "ordinal", "record_size",
            "raw_record_digest", "terminal_record_key", "occurrence_index", "disposition",
            "protected_raw_record", "error_code", "raw_timestamp", "observed_uid")
    }, source_epoch_id=replacement.id, source_kind="RECOVERY_PREFIX", canonical_source=True)
    session.add(copy)
    session.flush()
    assert newest_slot.unsettled_newest_ids(session, [copy, canonical(session, 2)]) == {copy.id}


def test_source_time_correction_refuses_newest_slot_bytes(source_db, monkeypatch):
    from zk_add.attendance_recovery import RecoveryError, _correction_candidate
    session, connector, _ = source_db
    certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY")])
    coverage = active_coverage(session)
    request = tail_request(session, coverage, [P1.row(1), junk(2, "copied_identity_bad_time"),
                                               junk(3, "copied_identity_bad_time")], latest=4)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=request)[3] is None
    session.commit()
    corrected = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(RecoveryError) as refused:
        _correction_candidate(session, "ZKT_MANIFEST", f"ZKT_MANIFEST:{canonical(session, 3).id}", corrected)
    assert refused.value.code == newest_slot.UNSETTLED_CONDITION
    # The same bytes read mid-range are a terminal clock fault and keep the
    # existing correction rules.
    try:
        _correction_candidate(session, "ZKT_MANIFEST", f"ZKT_MANIFEST:{canonical(session, 2).id}", corrected)
    except RecoveryError as other:
        assert other.code != newest_slot.UNSETTLED_CONDITION


def test_job_scoped_exception_listing_and_assurance_drop_a_settled_row(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    assert list_source_exceptions(session)["totals"]["invalid_time"] == 1
    replay = tail_request(session, coverage, [P1.row(1), P2.row(2), P3.row(3), junk(4, "zero")],
                          latest=5, previous=before)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=replay)[3] == \
        newest_slot.REPLAY_SUPERSEDED_CODE
    session.commit()
    totals = list_source_exceptions(session)["totals"]
    assert totals["invalid_time"] == 0 and totals["all"] == 0
    job = session.scalar(select(ReconciliationJob))
    assert service.source_exception_assurance(session, job)["open"] == 0
