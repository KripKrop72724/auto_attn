"""A terminal's newest attendance slot is settled from a later reconcile, never trusted.

The record shapes are the 40-byte uFace800 rows ADD received from MULTAN-01
and KARACHI-01 on 21 October 2026: the newest slot of every tail read was zero
bytes, a bare timestamp, an attendance UID with no user, unparseable text, or
a byte copy of an earlier record. A later read of the same ordinal returned
the next punch.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
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
    AttendanceSourceCorrection,
    AuditEvent,
    Connector,
    DeviceAlert,
    ReconciliationChunk,
    ReconciliationCoverage,
    ReconciliationDivergence,
    ReconciliationEvent,
    ReconciliationJob,
    TerminalRecordManifest,
    TerminalRecordReview,
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
from zk_add.source_exceptions import list_source_exceptions, review_source_exception, source_exception_detail
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


def live_capture(session, connector, *punches):
    """LIVE capture delivered these punches when they were made."""
    ingest_attendance(session, connector=connector, events=[punch.event("LIVE") for punch in punches])
    session.commit()


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


def chunk_request(session, job, records, *, start=0, previous=None, sequence=0):
    draft = ReconciliationChunkRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, sequence=sequence, start_ordinal=start,
        end_ordinal=start + len(records), chunk_digest="0" * 64, previous_chain_digest=previous,
        resulting_chain_digest="0" * 64, records=records,
    )
    digest = service.reconciliation_chunk_digest(draft)
    chain = service.reconciliation_chain_digest(
        previous, start_ordinal=start, end_ordinal=start + len(records), chunk_digest=digest)
    return draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain})


def commit_chunk(session, connector, job, records, *, start=0, previous=None, sequence=0):
    payload = chunk_request(session, job, records, start=start, previous=previous, sequence=sequence)
    service.apply_reconciliation_chunk(session, connector=connector, payload=payload)
    session.commit()
    return payload.resulting_chain_digest


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
    """Baseline [0,1), then a tail [1,4) whose newest slot (3) read as ``newest``.

    LIVE capture delivered P0, so the baseline's own final ordinal links it.
    """
    live_capture(session, connector, P0)
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
    live_capture(session, connector, P0)
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


ZERO_DIGEST = hashlib.sha256(ZERO).hexdigest()


def history(*rows):
    """Reconcile rows for ordinals 0.., given as punches or ready source rows."""
    return [row.row(index, source="FULL_HISTORY") if isinstance(row, Punch) else row
            for index, row in enumerate(rows)]


def anchor_job(session, connector, *, cutoff, padding=0, key=None):
    """A complete reconcile in the active epoch, anchored the way every firmware anchors it."""
    connector.zkt_device.attendance_count = cutoff
    job = service.create_reconciliation_job(
        session, connector=connector, actor="operator", reason="Re-read terminal source after deployment.",
        confirmation="RECONCILE 1 FROM START", idempotency_key=key or f"newest-slot-{uuid4().hex}",
    )
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=cutoff, latest_terminal_count=cutoff,
        record_size=40, source_total_bytes=4 + cutoff * 40 + padding,
        first_anchor_digest=P0.row(0).raw_record_digest,
    ))
    session.commit()
    return job


def seal(session, connector, job, chain):
    service.apply_reconciliation_manifest(session, connector=connector, payload=ReconciliationManifestRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=job.cutoff_count,
        latest_terminal_count=job.cutoff_count, final_chain_digest=chain,
    ))
    session.commit()


def rows_at(session, ordinal):
    return session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.ordinal == ordinal).order_by(TerminalRecordManifest.id)).all()


def job_states(session, job):
    return session.scalars(select(ReconciliationEvent.state).where(ReconciliationEvent.job_id == job.id)).all()


def replay(session, connector, coverage, before, records, *, latest):
    request = tail_request(session, coverage, records, latest=latest, previous=before)
    result = service.apply_source_tail_chunk(session, connector=connector, payload=request)
    session.commit()
    return result


def correct(session, connector, row, *, key="pre-deploy-correction"):
    """A correction made before ADD refused them on newest-slot rows: user 44's bytes, a new time."""
    derived = Punch(uid=17, user_id="44", local=at(12))
    ingest_attendance(session, connector=connector, events=[derived.event("CURRENT_RECONCILE")])
    session.flush()
    event = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == derived.event_uid))
    correction = AttendanceSourceCorrection(
        source_kind="ZKT_MANIFEST", source_ref=f"ZKT_MANIFEST:{row.id}", connector_id=connector.id,
        manifest_id=row.id, original_digest=row.raw_record_digest, correction_version=1,
        corrected_device_time=derived.local, status="CREATED", derived_event_uid=derived.event_uid,
        derived_attendance_event_id=event.id, actor="operator", reason="Pre-deploy correction",
        idempotency_key=key,
    )
    session.add(correction)
    session.commit()
    return correction, event


CANONICAL_ROWS = select(func.count(TerminalRecordManifest.id)).where(
    TerminalRecordManifest.canonical_source == True)  # noqa: E712


def test_rewound_tail_replay_is_superseded_without_changing_any_canonical_row(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    cursor, chain = coverage.source_committed_cursor, coverage.source_committed_chain_digest
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    rows_before = session.scalar(CANONICAL_ROWS)
    # A stale coverage push rewound the reader to ordinal 1. Two more punches
    # arrived, so slot 3 now holds P3 and slot 5 is the new unwritten slot.
    records = [P1.row(1), P2.row(2), P3.row(3), P4.row(4), junk(5, "bare_timestamp")]
    result, chunk, duplicate, error = replay(session, connector, coverage, before, records, latest=6)
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE and chunk is None and not duplicate
    assert result.active and result.capture_state != "SOURCE_COVERAGE_INVALIDATED"
    assert (result.source_committed_cursor, result.source_committed_chain_digest) == (cursor, chain)
    # The replay has no chunk receipt, so it changes no canonical row and ingests nothing.
    assert canonical(session, 3).raw_record_digest == ZERO_DIGEST
    assert canonical(session, 3).source_kind == "TAIL"
    assert session.scalar(CANONICAL_ROWS) == rows_before
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before
    assert newest_slot.is_unsettled_newest(session, canonical(session, 3))
    # One durable record per distinct replay, however often it is resent.
    replay(session, connector, coverage, before, records, latest=6)
    job = session.get(ReconciliationJob, coverage.job_id)
    events = session.scalars(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id,
        ReconciliationEvent.state == newest_slot.REPLAY_SUPERSEDED_CODE)).all()
    assert len(events) == 1
    details = events[0].details
    assert (details["start_ordinal"], details["end_ordinal"], details["latest_terminal_count"]) == (1, 6, 6)
    assert details["source_committed_cursor"] == cursor
    assert [(row["ordinal"], row["committed_raw_record_digest"], row["replay_read_newest"],
             row["punch_unreceived"]) for row in details["unsettled_rows"]] == [(3, ZERO_DIGEST, False, True)]
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.action == newest_slot.REPLAY_SUPERSEDED_CODE)) == 1
    # The reader adopts ADD's cursor and continues with an ordinary tail.
    next_tail = tail_request(session, result, [P4.row(4), junk(5, "bare_timestamp")], latest=6)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=next_tail)[3] is None
    session.commit()
    assert result.source_committed_cursor == 6


def test_replay_that_changes_a_committed_row_still_invalidates(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    changed = Punch(uid=17, user_id="44", local=at(3))
    result, chunk, _, error = replay(session, connector, coverage, before,
                                     [P1.row(1), changed.row(2), P3.row(3), P4.row(4)], latest=5)
    assert error == "SOURCE_TAIL_REPLAY_DIVERGED" and chunk is None
    assert result.active is False and result.capture_state == "SOURCE_COVERAGE_INVALIDATED"
    assert canonical(session, 3).raw_record_digest == ZERO_DIGEST


def test_replay_that_still_reads_the_newest_slot_is_superseded(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    cursor = coverage.source_committed_cursor
    # No punch since: the replay ends at the same unwritten slot, with other bytes.
    result, _, _, error = replay(session, connector, coverage, before,
                                 [P1.row(1), P2.row(2), junk(3, "bare_timestamp")], latest=4)
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE and result.active
    assert result.source_committed_cursor == cursor
    assert [row.raw_record_digest for row in rows_at(session, 3)] == [ZERO_DIGEST]


@pytest.mark.parametrize("owner", ["foreign", "conflicting"])
def test_superseded_replay_keeps_the_ordinary_tail_conflict_signals(source_db, owner):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    if owner == "foreign":
        # Another connector owns P3's event UID: a second reader on this terminal.
        other, _token, _created = onboard_connector(
            session, hardware_id="e0:72:a1:d6:f3:42", zone_id="ZONE-MULTAN-01",
            zone_name="ZONE-MULTAN-01", device_id="2", firmware_version="zone-lite-2.7.5",
            expected_serial="OTHER-TERMINAL", actor="test", ip_address="127.0.0.1",
        )
        session.flush()
        ingest_attendance(session, connector=other, events=[P3.event("LIVE").model_copy(
            update={"terminal_serial": None})])
        expected = "SOURCE_TAIL_FOREIGN_OWNER"
    else:
        # This connector holds the same event UID for a different person.
        ingest_attendance(session, connector=connector, events=[P3.event("LIVE").model_copy(
            update={"user_id": "44"})])
        expected = "SOURCE_REPLAY_USER_MISMATCH"
    session.commit()
    result, _, _, error = replay(session, connector, coverage, before,
                                 [P1.row(1), P2.row(2), P3.row(3), junk(4, "zero")], latest=5)
    assert error == expected
    assert result.active is False and result.invalidated_reason == expected
    assert canonical(session, 3).raw_record_digest == ZERO_DIGEST


def test_superseded_coverage_push_is_applied_by_every_firmware_generation(source_db, monkeypatch):
    from zk_add import web
    session, connector, factory = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "uid_without_user"))
    connector.boot_id, connector.last_sequence = "newest-slot-boot", 10
    session.commit()
    monkeypatch.setattr("zk_add.db.SessionLocal", factory)
    request = tail_request(session, coverage, [P1.row(1), P2.row(2), P3.row(3), junk(4, "zero")],
                           latest=5, previous=before)
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()),
                        boot_id=connector.boot_id, seq=11, sent_at=utc_now(), type="source_tail_chunk",
                        payload=request.model_dump(mode="json"))
    outcome = web.persist_envelope(connector.id, envelope)
    assert outcome.ack["type"] == "error"
    assert outcome.ack["code"] == newest_slot.REPLAY_SUPERSEDED_CODE
    assert outcome.ack["committed_next_ordinal"] == 4
    # The push goes before the error ACK (source_tail_authority), so the
    # reader applies ADD's cursor before its next tail.
    assert outcome.coverage is None
    push = json.loads(json.dumps(outcome.coverage_before_ack))
    assert push["source_committed_cursor"] == 4
    assert push["source_committed_chain_digest"] == coverage.source_committed_chain_digest

    def number(value, minimum=0):
        return type(value) in {int, float} and minimum <= value <= 2**32 - 1 and value == int(value)

    # 2.4.12 (45690c4), 2.5.2 (c8c5a0e) and 2.6.15 (3d62ecf): the
    # "source_coverage" handler in add_connector.c.
    assert (push["type"] == "source_coverage" and isinstance(push["terminal_serial"], str)
            and push["terminal_serial"] and type(push["terminal_generation"]) in {int, float}
            and type(push["source_committed_cursor"]) in {int, float}
            and isinstance(push["source_committed_chain_digest"], str)
            and len(push["source_committed_chain_digest"]) == 64 and isinstance(push["active"], bool))
    # 2.6.16 and later: add_source_parse_coverage in add_source_wire.c, with
    # the epoch required for an active push.
    epoch = push["source_epoch"]
    assert push["active"] is True and isinstance(epoch, str) and len(epoch) == 36
    assert all(char == "-" if index in {8, 13, 18, 23} else char in "0123456789abcdef"
               for index, char in enumerate(epoch))
    assert 0 < len(push["terminal_serial"]) < 80
    assert number(push["terminal_generation"], 1) and number(push["source_committed_cursor"])
    assert all(char in "0123456789abcdef" for char in push["source_committed_chain_digest"])
    # The same push a stale-epoch refusal and a manifest handoff already send
    # to every generation.
    from zk_add.source_tail_authority import active_coverage_push
    with factory() as db:
        held = db.get(ReconciliationCoverage, coverage.id)
        assert push == json.loads(json.dumps(active_coverage_push(db, held)))
        assert push == json.loads(json.dumps(service.coverage_authority_payload(db, held)))
        connector_row = db.get(type(connector), connector.id)
        assert connector_row.lifecycle_state != "DEGRADED"
        assert db.scalar(select(TerminalRecordManifest.disposition).where(
            TerminalRecordManifest.ordinal == 3,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
        )) == "IDENTITY_UNRESOLVED"


@pytest.mark.parametrize("live_captured", [True, False])
def test_same_epoch_reconcile_settles_newest_slot_instead_of_diverging(source_db, live_captured):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    if live_captured:
        ingest_attendance(session, connector=connector, events=[P3.event("LIVE")])
        session.commit()
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    epoch = session.scalar(select(TerminalSourceEpoch))
    job = anchor_job(session, connector, cutoff=5)
    assert job.source_epoch_id == epoch.id
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, junk(4, "zero")))
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 5
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    settled = canonical(session, 3)
    assert settled.source_kind == "BASELINE" and settled.job_id == job.id and settled.chunk_id is not None
    assert settled.raw_record_digest == P3.row(3).raw_record_digest
    assert session.get(AttendanceEvent, settled.attendance_event_id).event_uid == P3.event_uid
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before + (0 if live_captured else 1)
    retired = [row for row in rows_at(session, 3) if not row.canonical_source]
    assert [(row.source_kind, row.raw_record_digest) for row in retired] == [("TAIL", ZERO_DIGEST)]
    event = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.SETTLED_STATE))
    [entry] = event.details["rows"]
    # The audit names the stored disposition and both rows.
    assert (entry["settled_disposition"], entry["settled_manifest_id"]) == (settled.disposition, settled.id)
    assert (entry["superseded_manifest_id"], entry["superseded_raw_record_digest"]) == (retired[0].id, ZERO_DIGEST)
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == newest_slot.SETTLED_STATE))
    assert audit.target_id == str(retired[0].id)
    # This job's own final ordinal may have been the newest slot when read.
    assert newest_slot.is_unsettled_newest(session, canonical(session, 4))
    assert not newest_slot.is_unsettled_newest(session, settled)


@pytest.mark.parametrize("padding", [0, 8])
def test_reread_at_the_jobs_own_final_ordinal_is_kept_only_as_a_receipt(source_db, padding):
    """The final ordinal may still be the newest slot, whatever the prepared buffer's size."""
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    job = anchor_job(session, connector, cutoff=4, padding=padding)
    assert newest_slot.baseline_newest_ordinal(job) == 3
    # No punch since: slot 3 is still unwritten and returns stale bytes that
    # decode as a valid punch for user 44.
    stale = Punch(uid=17, user_id="44", local=at(7))
    chain = commit_chunk(session, connector, job, history(P0, P1, P2, stale.row(3, source="FULL_HISTORY")))
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 4
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before
    assert canonical(session, 3).raw_record_digest == ZERO_DIGEST
    receipt = next(row for row in rows_at(session, 3) if not row.canonical_source)
    assert (receipt.source_kind, receipt.job_id) == (newest_slot.REREAD_SOURCE_KIND, job.id)
    assert receipt.chunk_id is not None and receipt.attendance_event_id is None
    assert receipt.raw_record_digest == stale.row(3).raw_record_digest
    assert newest_slot.claimed_punch(receipt)[1]["event_uid"] == stale.event_uid
    assert newest_slot.REREAD_STATE in job_states(session, job)
    # A re-read of the unwritten slot does not confirm or settle it.
    assert newest_slot.is_unsettled_newest(session, canonical(session, 3))
    # Every committed byte is accounted for when the job certifies, and the
    # punch the re-read decodes to is neither dropped nor excluded by review.
    seal(session, connector, job, chain)
    assert job.capture_certified_at is not None
    assert job.quarantined_count == 1
    assert job.capture_certificate["unconfirmed_newest_ordinals"] == [3]
    alert = session.scalar(select(DeviceAlert).where(DeviceAlert.code == newest_slot.UNRECEIVED_ALERT))
    assert alert.details["ordinals"] == [3] and alert.details["receipt_manifest_ids"] == [receipt.id]
    assert service.source_exception_assurance(session, job)["state"] == newest_slot.PUNCH_PENDING_STATE


def test_raw_custody_reread_of_an_unsettled_row_stays_a_source_change(source_db):
    """A 2.7.6 raw-custody re-read is never settled, dropped or certified over."""
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    connector.zkt_custody_enabled = True
    session.commit()

    def raw(ordinal, value):
        digest = hashlib.sha256(value).hexdigest()
        return ReconciliationSourceRecord(
            ordinal=ordinal, raw_record_digest=digest,
            terminal_record_key=hashlib.sha256(f"{SERIAL}:{digest}".encode()).hexdigest(),
            occurrence_index=ordinal + 1, disposition="RAW_PRESERVED", event=None,
            raw_record_b64=base64.b64encode(value).decode())

    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, raw(3, P3.raw), raw(4, ZERO)))
    assert job.phase == "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 0
    divergence = session.scalar(select(ReconciliationDivergence))
    assert (divergence.ordinal, divergence.new_raw_digest) == (3, hashlib.sha256(P3.raw).hexdigest())
    assert canonical(session, 3).raw_record_digest == ZERO_DIGEST
    assert job.capture_certified_at is None


@pytest.mark.parametrize("shape", ["copied_identity_bad_time", "uid_without_user"])
def test_settled_row_is_not_newest_and_a_later_change_is_a_source_change(source_db, shape):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    # Slot 3 now holds a genuine bad-clock punch or a deleted user's punch.
    genuine = junk(3, shape)
    first = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, first, history(P0, P1, P2, genuine, junk(4, "zero")))
    settled = canonical(session, 3)
    assert settled.raw_record_digest == genuine.raw_record_digest and settled.job_id == first.id
    assert not newest_slot.is_unsettled_newest(session, settled)
    listed = {row["ordinal"]: row for row in list_source_exceptions(session)["rows"]}
    assert listed[3]["source_condition"] is None
    assert listed[4]["source_condition"] == newest_slot.UNSETTLED_CONDITION
    if shape == "copied_identity_bad_time":
        from zk_add.attendance_recovery import RecoveryError, _correction_candidate
        # A genuine clock fault is correctable under the ordinary rules again.
        try:
            _correction_candidate(session, "ZKT_MANIFEST", f"ZKT_MANIFEST:{settled.id}",
                                  datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
        except RecoveryError as other:
            assert other.code != newest_slot.UNSETTLED_CONDITION
    service.control_reconciliation_job(session, job=first, action="cancel", actor="operator",
                                       reason="Superseded by a later re-read.", idempotency_key="cancel-first")
    session.commit()
    # A later read of the now-written slot with other bytes is a real source change.
    second = anchor_job(session, connector, cutoff=6)
    commit_chunk(session, connector, second, history(P0, P1, P2, P3, P4, junk(5, "zero")))
    assert second.phase == "VERIFYING_SOURCE_CHANGE"
    assert session.scalar(select(ReconciliationDivergence.ordinal)) == 3
    assert canonical(session, 3).id == settled.id


def test_identical_reread_confirms_a_genuine_newest_record(source_db):
    session, connector, _ = source_db
    # A genuine bad-clock punch happened to be the tail's newest slot.
    genuine = junk(3, "copied_identity_bad_time")
    first_tail(session, connector, genuine)
    row = canonical(session, 3)
    assert newest_slot.is_unsettled_newest(session, row)
    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, genuine, junk(4, "zero")))
    assert canonical(session, 3).id == row.id  # the original read keeps the slot
    receipt = next(item for item in rows_at(session, 3) if not item.canonical_source)
    assert (receipt.source_kind, receipt.job_id, receipt.raw_record_digest) == (
        newest_slot.REREAD_SOURCE_KIND, job.id, row.raw_record_digest)
    assert newest_slot.CONFIRMED_STATE in job_states(session, job)
    assert not newest_slot.is_unsettled_newest(session, row)
    assert {item["ordinal"]: item for item in list_source_exceptions(session)["rows"]}[3][
        "source_condition"] is None
    service.control_reconciliation_job(session, job=job, action="cancel", actor="operator",
                                       reason="Confirmed.", idempotency_key="cancel-confirm")
    session.commit()
    # Once confirmed, a different later read is a source change, not a settlement.
    later = anchor_job(session, connector, cutoff=6)
    commit_chunk(session, connector, later, history(P0, P1, P2, P3, P4, junk(5, "zero")))
    assert later.phase == "VERIFYING_SOURCE_CHANGE"


def test_corrected_newest_row_is_never_settled_and_raises_an_alert(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "copied_identity_bad_time"))
    prior = canonical(session, 3)
    correction, derived = correct(session, connector, prior)
    listed = list_source_exceptions(session, corrected=True)["rows"]
    assert [(row["ordinal"], row["source_condition"], row["source_correction_ids"]) for row in listed] == [
        (3, newest_slot.CORRECTED_CONDITION, [correction.correction_id])]
    assert "HR must verify" in listed[0]["operator_reason"]
    assert 3 not in {row["ordinal"] for row in list_source_exceptions(session, corrected=False)["rows"]}
    # A rewound replay shows the slot now holds P3: rows stay, HR is alerted.
    _, _, _, error = replay(session, connector, coverage, before,
                            [P1.row(1), P2.row(2), P3.row(3), P4.row(4), junk(5, "zero")], latest=6)
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE
    alert = session.scalar(select(DeviceAlert).where(DeviceAlert.code == newest_slot.CORRECTED_ALERT))
    assert alert is not None and alert.severity == "CRITICAL"
    assert alert.details["correction_ids"] == [correction.correction_id]
    session.delete(alert)
    session.commit()
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    # A reconcile that reads P3 there stops instead of hiding the corrected punch.
    job = anchor_job(session, connector, cutoff=6)
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, P4, junk(5, "zero")))
    assert (job.status, job.error_code) == ("NEEDS_ATTENTION", newest_slot.CORRECTED_HOLD)
    assert job.committed_next_ordinal == 0
    assert session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id)) == 0
    assert canonical(session, 3).id == prior.id
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before
    assert session.get(AttendanceSourceCorrection, correction.id).status == "CREATED"
    hold = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.CORRECTED_HOLD))
    assert hold.details["correction_ids"] == [correction.correction_id]
    alert = session.scalar(select(DeviceAlert).where(DeviceAlert.code == newest_slot.CORRECTED_ALERT))
    assert alert is not None and alert.details["job_id"] == job.job_id


def test_reviewed_newest_row_settles_and_its_review_stays_with_the_excluded_bytes(source_db):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    prior = canonical(session, 3)
    review = review_source_exception(session, row=prior, actor="operator",
                                     reason="Unwritten newest record", idempotency_key="review-3")
    session.commit()
    assert list_source_exceptions(session)["totals"]["reviewed"] == 1
    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, junk(4, "zero")))
    assert canonical(session, 3).raw_record_digest == P3.row(3).raw_record_digest
    assert session.get(TerminalRecordReview, review.id).manifest_id == prior.id
    assert not session.get(TerminalRecordManifest, prior.id).canonical_source
    event = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.SETTLED_STATE))
    assert event.details["rows"][0]["superseded_review_ids"] == [review.review_id]
    totals = list_source_exceptions(session)["totals"]
    # Only ordinal 4, this job's own unreviewed final ordinal, is still an exception.
    assert (totals["all"], totals["reviewed"], totals["open"]) == (1, 0, 1)


def test_tail_replay_cannot_break_an_open_jobs_sealed_cohort(source_db):
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    # The runbook's quiet-window reconcile: no punch since the last tail.
    job = anchor_job(session, connector, cutoff=4)
    chain = commit_chunk(session, connector, job, history(P0, P1, P2, junk(3, "bare_timestamp")))
    seal(session, connector, job, chain)
    assert "retained_coverage" in job.capture_certificate
    assert service.source_exception_assurance(session, job)["state"] == "REVIEW_REQUIRED"
    _, _, _, error = replay(session, connector, coverage, before,
                            [P1.row(1), P2.row(2), P3.row(3), P4.row(4), junk(5, "zero")], latest=6)
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE
    service.refresh_reconciliation_assurance(session, job)
    session.commit()
    assurance = service.source_exception_assurance(session, job)
    # The sealed cohort is unchanged, but the replay read P3 in the job's final
    # slot: a review can no longer exclude a punch ADD has not received.
    assert assurance["mismatch_reasons"] == [] and assurance["total"] == 1
    assert assurance["state"] == newest_slot.PUNCH_PENDING_STATE
    assert job.wait_reason == newest_slot.PUNCH_PENDING_HOLD


def test_reconcile_never_settles_rows_another_open_job_sealed(source_db):
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    sealed = anchor_job(session, connector, cutoff=4)
    seal(session, connector, sealed, commit_chunk(
        session, connector, sealed, history(P0, P1, P2, junk(3, "bare_timestamp"))))
    assert sealed.capture_certified_at is not None and sealed.status == "NEEDS_ATTENTION"
    # A connector runs one job at a time, so two open jobs on one terminal
    # need a job left open under an earlier connector binding.
    other, _token, _created = onboard_connector(
        session, hardware_id="e0:72:a1:d6:f3:43", zone_id="ZONE-MULTAN-01", zone_name="ZONE-MULTAN-01",
        device_id="3", firmware_version="zone-lite-2.7.5", expected_serial="EARLIER-BINDING",
        actor="test", ip_address="127.0.0.1",
    )
    session.flush()
    sealed.connector_id = other.id
    session.commit()
    job = anchor_job(session, connector, cutoff=6)
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, P4, junk(5, "zero")))
    assert (job.status, job.error_code) == ("NEEDS_ATTENTION", newest_slot.OPEN_JOB_HOLD)
    assert job.committed_next_ordinal == 0 and canonical(session, 3).raw_record_digest == ZERO_DIGEST
    assert service.source_exception_assurance(session, sealed)["mismatch_reasons"] == []


def test_job_held_by_its_own_final_identity_row_is_not_promised_a_reread_exit(source_db):
    """Owner decision pending: an unwritten final ordinal with an unresolved identity holds the job."""
    session, connector, _ = source_db
    job = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), junk(1, "uid_without_user")])
    row = canonical(session, 1)
    assert newest_slot.is_unsettled_newest(session, row)
    review_source_exception(session, row=row, actor="operator", reason="Unwritten newest record",
                            idempotency_key="review-own-final")
    session.commit()
    service.refresh_reconciliation_assurance(session, job)
    session.commit()
    assert job.wait_reason == service.SOURCE_IDENTITY_HOLD
    reason = source_exception_detail(session, row)["operator_reason"]
    assert "stays held until the owner decides" in reason
    # A re-read cannot run while the job is open; cancelling it is the exit.
    with pytest.raises(ValueError):
        anchor_job(session, connector, cutoff=2)


@pytest.mark.parametrize("latest, padding", [(2, 0), (2, 8), (3, 0)])
def test_baseline_final_ordinal_is_always_possibly_newest(source_db, latest, padding):
    session, connector, _ = source_db
    job = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), junk(1, "zero")],
                           latest=latest)
    if padding:
        job.source_total_bytes += padding
    row = canonical(session, 1)
    assert row.job_id == job.id
    assert newest_slot.baseline_newest_ordinal(job) == 1
    assert newest_slot.is_unsettled_newest(session, row)
    job.source_total_bytes = None
    assert newest_slot.is_unsettled_newest(session, row)


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


def test_source_time_correction_refuses_newest_slot_bytes(source_db):
    from zk_add.attendance_recovery import RecoveryError, _correction_candidate
    session, connector, _ = source_db
    live_capture(session, connector, P0)
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


def test_concurrent_delivery_of_a_settling_chunk_settles_once(source_db):
    session, connector, factory = source_db
    if session.get_bind().dialect.name != "postgresql":
        pytest.skip("Row locks between concurrent transactions need PostgreSQL")
    first_tail(session, connector, junk(3, "zero"))
    job = anchor_job(session, connector, cutoff=5)
    records = history(P0, P1, P2, P3, junk(4, "zero"))
    draft = ReconciliationChunkRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, sequence=0, start_ordinal=0, end_ordinal=5,
        chunk_digest="0" * 64, previous_chain_digest=None, resulting_chain_digest="0" * 64, records=records,
    )
    digest = service.reconciliation_chunk_digest(draft)
    payload = draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": (
        service.reconciliation_chain_digest(None, start_ordinal=0, end_ordinal=5, chunk_digest=digest))})

    def deliver(db):
        return service.apply_reconciliation_chunk(db, connector=db.get(Connector, connector.id), payload=payload)

    with factory() as first, factory() as second, ThreadPoolExecutor(max_workers=1) as pool:
        _, chunk, duplicate = deliver(first)
        assert chunk is not None and not duplicate
        # The same chunk, redelivered over another stream before the first commits.
        pending = pool.submit(lambda: (deliver(second), second.commit()))
        time.sleep(1)
        assert not pending.done()
        first.commit()
        (_, replayed, duplicate), _ = pending.result(timeout=30)
        assert duplicate and replayed.id == chunk.id
    session.expire_all()
    assert [row.raw_record_digest for row in rows_at(session, 3) if row.canonical_source] == [
        P3.row(3).raw_record_digest]
    assert [row.raw_record_digest for row in rows_at(session, 3) if not row.canonical_source] == [ZERO_DIGEST]
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.action == newest_slot.SETTLED_STATE)) == 1


def alert(session, code):
    return session.scalar(select(DeviceAlert).where(DeviceAlert.code == code, DeviceAlert.state == "OPEN"))


def event_id(session, punch):
    return session.scalar(select(AttendanceEvent.id).where(AttendanceEvent.event_uid == punch.event_uid))


@pytest.mark.parametrize("live_captured", [False, True])
def test_superseded_replay_keeps_a_missed_punch_as_evidence_and_alerts(source_db, live_captured):
    """A rewound replay read P3 in slot 3 while later records existed; LIVE may have missed it."""
    session, connector, _ = source_db
    coverage, before, _ = first_tail(session, connector, junk(3, "zero"))
    if live_captured:
        live_capture(session, connector, P3)
    records = [P1.row(1), P2.row(2), P3.row(3), P4.row(4), junk(5, "bare_timestamp")]
    result, _, _, error = replay(session, connector, coverage, before, records, latest=6)
    assert error == newest_slot.REPLAY_SUPERSEDED_CODE and result.active
    [receipt] = [row for row in rows_at(session, 3) if not row.canonical_source]
    assert (receipt.source_kind, receipt.job_id, receipt.attendance_event_id) == (
        newest_slot.REPLAY_SOURCE_KIND, None, None)
    assert receipt.raw_record_digest == P3.row(3).raw_record_digest
    assert newest_slot.claimed_punch(receipt)[1]["event_uid"] == P3.event_uid
    # The replay never ingests: only a reconcile's own receipt settles a slot.
    assert (event_id(session, P3) is not None) is live_captured
    unreceived = alert(session, newest_slot.UNRECEIVED_ALERT)
    if live_captured:
        assert unreceived is None
    else:
        assert unreceived.severity == "HIGH"
        assert (unreceived.details["ordinals"], unreceived.details["receipt_manifest_ids"]) == ([3], [receipt.id])
    detail = source_exception_detail(session, canonical(session, 3))
    assert [(row["manifest_id"], row["source_kind"], row["punch_received"]) for row in detail["slot_receipts"]] == [
        (receipt.id, newest_slot.REPLAY_SOURCE_KIND, live_captured)]
    # A resent replay is recorded once.
    replay(session, connector, coverage, before, records, latest=6)
    assert len([row for row in rows_at(session, 3) if not row.canonical_source]) == 1


def test_final_ordinal_reread_of_a_missed_punch_holds_assurance_until_add_has_it(source_db):
    """The runbook's quiet-window reconcile: P3 is punched during the scan and LIVE misses it."""
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    job = anchor_job(session, connector, cutoff=4)
    chain = commit_chunk(session, connector, job, history(P0, P1, P2, P3))
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert event_id(session, P3) is None
    receipt = next(row for row in rows_at(session, 3) if not row.canonical_source)
    reread = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.REREAD_STATE))
    assert reread.details["rows"] == [{
        "ordinal": 3, "manifest_id": canonical(session, 3).id, "receipt_manifest_id": receipt.id,
        "raw_record_digest": P3.row(3).raw_record_digest, "committed_raw_record_digest": ZERO_DIGEST,
        "punch_unreceived": True}]
    unreceived = alert(session, newest_slot.UNRECEIVED_ALERT)
    assert unreceived.details["job_id"] == job.job_id and unreceived.details["ordinals"] == [3]
    seal(session, connector, job, chain)
    review_source_exception(session, row=canonical(session, 3), actor="operator",
                            reason="Unwritten newest record", idempotency_key="review-final-3")
    session.commit()
    service.refresh_reconciliation_assurance(session, job)
    session.commit()
    assurance = service.source_exception_assurance(session, job)
    # The review is recorded, but it cannot exclude a punch ADD has not received.
    assert (assurance["state"], assurance["reviewed"], assurance["newest_punch_unreceived"]) == (
        newest_slot.PUNCH_PENDING_STATE, 1, 1)
    assert (job.status, job.wait_reason) == ("NEEDS_ATTENTION", newest_slot.PUNCH_PENDING_HOLD)
    serialized = service.serialize_job(session, job)["source_exception_assurance"]
    assert serialized["newest_punch_unreceived"] == 1
    with pytest.raises(ValueError, match="newest-record slot"):
        service.control_reconciliation_job(session, job=job, action="retry", actor="operator",
                                           reason="Retry", idempotency_key="retry-final-3")
    session.rollback()
    target = job.ords_target_count
    # LIVE capture delivers the same punch late: assurance resumes on its own,
    # and the punch joins this job's Oracle membership.
    live_capture(session, connector, P3)
    service.refresh_reconciliation_assurance(session, job)
    session.commit()
    assert service.source_exception_assurance(session, job)["state"] == "REVIEWED_EXCLUSIONS"
    assert job.wait_reason != newest_slot.PUNCH_PENDING_HOLD and job.status != "NEEDS_ATTENTION"
    assert job.ords_target_count == target + 1
    assert source_exception_detail(session, canonical(session, 3))["slot_receipts"][0]["punch_received"] is True


@pytest.mark.parametrize("later", ["live", "same_bytes", "other_bytes"])
def test_newest_slot_punch_no_capture_delivered_is_held_not_ingested(source_db, later):
    """Stale bytes in the unwritten slot decode as a plausible punch for user 44."""
    from zk_add.attendance_recovery import RecoveryError, _correction_candidate
    session, connector, _ = source_db
    stale = Punch(uid=17, user_id="44", local=at(7))
    first_tail(session, connector, stale.row(3))
    row = canonical(session, 3)
    assert (row.disposition, row.error_code, row.attendance_event_id) == (
        "IDENTITY_UNRESOLVED", newest_slot.HELD_CODE, None)
    assert (row.declared_disposition, row.observed_user_id) == ("EVENT", "44")
    assert newest_slot.claimed_punch(row)[1]["event_uid"] == stale.event_uid
    assert event_id(session, stale) is None
    held = alert(session, newest_slot.UNRECEIVED_ALERT)
    assert (held.details["cause"], held.details["ordinals"], held.details["held_manifest_ids"]) == (
        "TAIL", [3], [row.id])
    listed = {item["ordinal"]: item for item in list_source_exceptions(session)["rows"]}
    assert listed[3]["source_condition"] == newest_slot.UNCONFIRMED_CONDITION
    assert "ADD holds them" in listed[3]["operator_reason"]
    with pytest.raises(RecoveryError):  # a held punch is never corrected into a punch
        _correction_candidate(session, "ZKT_MANIFEST", f"ZKT_MANIFEST:{row.id}",
                              datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))
    if later == "live":
        live_capture(session, connector, stale)
        assert source_exception_detail(session, row)["slot_receipts"] == [{
            "manifest_id": row.id, "source_kind": "TAIL", "job_id": None,
            "raw_record_digest": row.raw_record_digest, "disposition": "EVENT",
            "decodes_as_punch": True, "punch_received": True, "observed_at": row.created_at}]
    events_before = session.scalar(select(func.count(AttendanceEvent.id)))
    # A later reconcile reads slot 3 while later records exist.
    third = P3.row(3) if later == "other_bytes" else stale.row(3)
    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, third, junk(4, "zero")))
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 5
    settled = canonical(session, 3)
    assert (settled.source_kind, settled.job_id, settled.raw_record_digest) == (
        "BASELINE", job.id, third.raw_record_digest)
    assert not session.get(TerminalRecordManifest, row.id).canonical_source
    expected = P3 if later == "other_bytes" else stale
    assert session.get(AttendanceEvent, settled.attendance_event_id).event_uid == expected.event_uid
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events_before + (0 if later == "live" else 1)
    if later == "other_bytes":
        assert event_id(session, stale) is None  # the held bytes were never a punch
    settlement = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.SETTLED_STATE))
    assert [(item["ordinal"], item["superseded_error_code"]) for item in settlement.details["rows"]] == [
        (3, newest_slot.HELD_CODE)]


def test_baseline_final_ordinal_is_assumed_newest_and_a_later_change_stops_for_review(source_db):
    """No chunk reports the count a baseline's final ordinal was read against.

    The row is labelled and certified as possibly the newest slot, but its
    punch is ingested and released under the ordinary rules. A later read of
    other bytes there, while later records exist, stops for HR.
    """
    session, connector, _ = source_db
    stale = Punch(uid=17, user_id="44", local=at(7))
    first = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), stale.row(1)])
    row = canonical(session, 1)
    event = session.get(AttendanceEvent, event_id(session, stale))
    assert row.attendance_event_id == event.id and not newest_slot.is_held(row)
    assert newest_slot.newest_read_ids(session, [row]) == {row.id}
    assert newest_slot.newest_only_event_ids(session, [event]) == set()
    assert first.capture_certificate["unconfirmed_newest_ordinals"] == [1]
    assert alert(session, newest_slot.UNRECEIVED_ALERT) is None
    first.status = "COMPLETED"
    session.commit()
    job = anchor_job(session, connector, cutoff=3)
    commit_chunk(session, connector, job, history(P0, P1, junk(2, "zero")))
    assert (job.status, job.error_code, job.committed_next_ordinal) == (
        "NEEDS_ATTENTION", newest_slot.CHANGED_HOLD, 0)
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert alert(session, newest_slot.CHANGED_ALERT).details["attendance_event_ids"] == [event.id]



@pytest.mark.parametrize("live_captured", [True, False])
def test_newest_slot_byte_copy_of_an_earlier_punch_settles_on_reconcile(source_db, live_captured):
    """Slot 4 read back as a byte copy of slot 3's punch while ADD's slot 3 was still junk."""
    session, connector, _ = source_db
    coverage, _, _ = first_tail(session, connector, junk(3, "zero"))
    if live_captured:
        live_capture(session, connector, P3)
    request = tail_request(session, coverage, [P3.row(4)], latest=5)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=request)[3] is None
    connector.zkt_device.attendance_count = 5
    session.commit()
    copy = canonical(session, 4)
    if live_captured:
        # Linked to LIVE's P3 (identity-held until its roster proof); not a byte-copy duplicate,
        # because ADD's slot 3 was still junk.
        assert copy.disposition in {"EVENT", "BLOCKED_IDENTITY"}
        assert copy.attendance_event_id == event_id(session, P3)
        assert newest_slot.newest_read_ids(session, [copy]) == {copy.id}
    else:
        assert newest_slot.is_held(copy)
    job = anchor_job(session, connector, cutoff=6)
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, P4, junk(5, "zero")))
    assert job.phase != "VERIFYING_SOURCE_CHANGE" and job.committed_next_ordinal == 6
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert canonical(session, 3).raw_record_digest == P3.row(3).raw_record_digest
    assert canonical(session, 4).raw_record_digest == P4.row(4).raw_record_digest
    assert canonical(session, 3).attendance_event_id == event_id(session, P3)
    assert canonical(session, 4).attendance_event_id == event_id(session, P4)
    retired = session.get(TerminalRecordManifest, copy.id)
    assert not retired.canonical_source and retired.attendance_event_id == (
        event_id(session, P3) if live_captured else None)
    assert session.scalar(select(func.count(AttendanceEvent.id)).where(
        AttendanceEvent.event_uid == P3.event_uid)) == 1


def test_changed_newest_row_that_delivered_a_punch_holds_instead_of_diverging(source_db):
    """A newest-slot read linked an existing punch, is not a byte copy, and later reads differently."""
    from zk_add import web
    session, connector, _ = source_db
    stale = Punch(uid=17, user_id="44", local=at(7))
    live_capture(session, connector, stale)
    first_tail(session, connector, stale.row(3))
    row = canonical(session, 3)
    assert (row.disposition, row.attendance_event_id) == ("EVENT", event_id(session, stale))
    job = anchor_job(session, connector, cutoff=5)
    payload = chunk_request(session, job, history(P0, P1, P2, P3, junk(4, "zero")))
    # The HTTP chunk route answers a held chunk instead of failing.
    response, _ = web.persist_device_reconciliation_chunk(payload, (session, connector))
    assert response == {
        "ok": False, "job_id": job.job_id, "generation": payload.generation, "sequence": payload.sequence,
        "committed_next_ordinal": 0, "resulting_chain_digest": payload.resulting_chain_digest,
        "duplicate": False,
    }
    assert (job.status, job.error_code) == ("NEEDS_ATTENTION", newest_slot.CHANGED_HOLD)
    assert session.scalar(select(func.count(ReconciliationDivergence.id))) == 0
    assert session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id)) == 0
    assert canonical(session, 3).id == row.id and event_id(session, P3) is None
    hold = session.scalar(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.state == newest_slot.CHANGED_HOLD))
    assert (hold.details["ordinals"], hold.details["attendance_event_ids"]) == ([3], [event_id(session, stale)])
    changed = alert(session, newest_slot.CHANGED_ALERT)
    assert changed.severity == "CRITICAL" and changed.details["job_id"] == job.job_id


def test_events_only_read_from_a_newest_slot_are_never_released(source_db, monkeypatch):
    from zk_add import attendance_direct_ords as direct
    from zk_add import attendance_force_release as force
    from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
    from zk_add.models import AttendanceRecoveryItem, DeviceUser
    from zk_add.service import release_synced_cnic_attendance_event
    session, connector, _ = source_db
    # A reconcile-sourced, identity-held punch that an older tail linked from
    # the newest slot: its terminal UID does not match user 44's enrolment.
    held = Punch(uid=99, user_id="44", local=at(7))
    ingest_attendance(session, connector=connector, events=[held.event("CURRENT_RECONCILE")])
    session.commit()
    first_tail(session, connector, held.row(3))
    event = session.get(AttendanceEvent, event_id(session, held))
    assert event.ords_status == "BLOCKED_IDENTITY"
    assert newest_slot.newest_only_event_ids(session, [event]) == {event.id}
    assert force.classify(session, event, connector, None)[:2] == (
        "NEEDS_REVIEW", newest_slot.UNCONFIRMED_CONDITION)
    job = direct.create(session, actor="operator", request=DirectOrdsStartRequest(
        event_ids=[event.id], reason="Deliver selected punch", password="unused", idempotency_key="direct-newest"))
    item = session.scalar(select(AttendanceRecoveryItem).where(AttendanceRecoveryItem.job_id == job.id))
    assert (item.status, item.error_code) == ("SKIPPED", newest_slot.UNCONFIRMED_CONDITION)
    session.rollback()
    monkeypatch.setattr("zk_add.service.synced_cnic_identity_proven", lambda *args: True)
    monkeypatch.setattr("zk_add.attendance_auto_release_guard.database_synced_cnic_release_proven",
                        lambda *args, **kwargs: True)
    user = session.scalar(select(DeviceUser).where(DeviceUser.user_id == "44"))
    assert release_synced_cnic_attendance_event(session, zkt=connector.zkt_device, user=user, row=event) is False
    session.rollback()
    # A later reconcile reads the same bytes while later records exist: the
    # read is confirmed, and the ordinary identity proof releases it again.
    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, held, junk(4, "zero")))
    assert newest_slot.CONFIRMED_STATE in job_states(session, job)
    event = session.get(AttendanceEvent, event.id)
    assert newest_slot.newest_only_event_ids(session, [event]) == set()
    assert event.ords_status == "PENDING"


def test_settling_a_completed_jobs_final_ordinal_keeps_its_sealed_cohort(source_db):
    session, connector, _ = source_db
    first = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), junk(1, "zero")])
    review_source_exception(session, row=canonical(session, 1), actor="operator",
                            reason="Unwritten newest record", idempotency_key="review-first-final")
    session.commit()
    service.refresh_reconciliation_assurance(session, first)
    first.status = "COMPLETED"
    session.commit()
    before = service.serialize_job(session, first)["source_exception_assurance"]
    assert (before["state"], before["total"], before["reviewed"]) == ("REVIEWED_EXCLUSIONS", 1, 1)
    coverage = active_coverage(session)
    request = tail_request(session, coverage, [P2.row(2), junk(3, "zero")], latest=4)
    assert service.apply_source_tail_chunk(session, connector=connector, payload=request)[3] is None
    connector.zkt_device.attendance_count = 4
    session.commit()
    second = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, second, history(P0, P1, P2, P3, junk(4, "zero")))
    assert canonical(session, 1).job_id == second.id  # the first job's final ordinal was settled
    after = service.serialize_job(session, first)["source_exception_assurance"]
    assert {key: after[key] for key in ("state", "total", "reviewed", "open", "cohort_digest")} == {
        key: before[key] for key in ("state", "total", "reviewed", "open", "cohort_digest")}


def test_recovery_copy_of_a_corrected_read_is_never_settled(source_db):
    session, connector, _ = source_db
    coverage, _, _ = first_tail(session, connector, junk(3, "copied_identity_bad_time"))
    original = canonical(session, 3)
    correction, _ = correct(session, connector, original)
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
            "protected_raw_record", "error_code", "raw_timestamp", "observed_uid", "observed_user_id")
    }, source_epoch_id=replacement.id, source_kind="RECOVERY_PREFIX", canonical_source=True)
    session.add(copy)
    session.commit()
    job = session.get(ReconciliationJob, coverage.job_id)
    code, _, details = newest_slot.settlement_block(session, job=job, priors=[copy])
    assert (code, details["correction_ids"]) == (newest_slot.CORRECTED_HOLD, [correction.correction_id])
    listed = {item["id"]: item for item in list_source_exceptions(session)["rows"]}
    assert listed[copy.id]["source_condition"] == newest_slot.CORRECTED_CONDITION
    assert listed[copy.id]["source_correction_ids"] == [correction.correction_id]


def test_divergence_probe_opened_on_a_newest_slot_row_requeues_its_chunk(source_db):
    """A probe running at deploy time on an unwritten newest-slot row does not recover an epoch."""
    from zk_add.schemas import SourceProbeResultRequest
    session, connector, _ = source_db
    first_tail(session, connector, junk(3, "zero"))
    job = anchor_job(session, connector, cutoff=5)
    service._begin_source_divergence(session, job=job, prior=canonical(session, 3), source=P3.row(3))
    session.commit()
    assert job.phase == "VERIFYING_SOURCE_CHANGE"
    epochs = session.scalar(select(func.count(TerminalSourceEpoch.id)))
    service.apply_source_probe_result(session, connector=connector, payload=SourceProbeResultRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_serial=SERIAL, latest_terminal_count=5,
        record_size=40, ordinal=3, record=P3.row(3)))
    session.commit()
    divergence = session.scalar(select(ReconciliationDivergence))
    assert divergence.state == newest_slot.PROBE_SUPERSEDED_STATE
    assert (job.status, job.phase) == ("QUEUED", "RECOVERING_AFTER_INTERRUPTION")
    assert newest_slot.PROBE_REQUEUED_STATE in job_states(session, job)
    assert session.scalar(select(func.count(TerminalSourceEpoch.id))) == epochs
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, junk(4, "zero")))
    assert canonical(session, 3).raw_record_digest == P3.row(3).raw_record_digest


def test_certificate_names_the_unconfirmed_final_ordinal_and_review_is_not_the_exit(source_db):
    session, connector, _ = source_db
    job = certify_baseline(session, connector, [P0.row(0, source="FULL_HISTORY"), junk(1, "zero")])
    assert job.capture_certificate["unconfirmed_newest_ordinals"] == [1]
    reason = source_exception_detail(session, canonical(session, 1))["operator_reason"]
    assert "does not settle the slot" in reason and "review the row if" not in reason


def _guard_manifest_invariants(session, event):
    """The 0053 Postgres release guard's conditions on every manifest row linked to an event."""
    rows = session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.attendance_event_id == event.id)).all()
    assert rows
    for row in rows:
        assert (row.connector_id, row.zkt_device_id, row.terminal_serial) == (
            event.connector_id, event.zkt_device_id, event.device_serial)
        assert row.observed_user_id in {None, event.user_id} and row.observed_uid in {None, event.uid}
        assert row.disposition in {"EVENT", "BLOCKED_IDENTITY", "TERMINAL_DUPLICATE"}
    assert any(row.canonical_source and row.disposition in {"EVENT", "BLOCKED_IDENTITY"} for row in rows)
    return rows


def _postgres_guard(session, event, *, captured_after):
    """Evaluate migration 0053's guard for ``event`` as an automatic CNIC release would."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from zk_add.models import AttendanceIdentityHistory, DeviceUser

    path = (Path(__file__).parents[2] / "apps/add_backend/migrations/versions"
            / "20261012_0053_reconciled_record_uid_sources.py")
    spec = spec_from_file_location("guard_0053", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.upgrade()
    zkt = session.get(Connector, event.connector_id).zkt_device
    user = session.scalar(select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id,
                                                   DeviceUser.user_id == event.user_id))
    if session.scalar(select(AttendanceIdentityHistory.id).where(
            AttendanceIdentityHistory.device_user_id == user.id)) is None:
        session.add(AttendanceIdentityHistory(
            zkt_device_id=zkt.id, device_user_id=user.id, terminal_serial=zkt.serial,
            user_id=user.user_id, uid=user.uid, fingerprint=FINGERPRINT,
            cnic_encrypted=user.cnic_encrypted, cnic_lookup_hash=user.cnic_lookup_hash,
            first_snapshot_id=zkt.identity_snapshot_id, last_snapshot_id=zkt.identity_snapshot_id,
            last_revision=zkt.identity_snapshot_revision,
            observed_from=event.device_event_time - timedelta(days=1),
            observed_until=event.device_event_time + timedelta(days=1),
        ))
        session.flush()
    captured = event.device_event_time + captured_after
    return session.scalar(text(
        "SELECT add_auto_reconciled_cnic_verified(jsonb_populate_record("
        "NULL::add_attendance_events, to_jsonb(e) || jsonb_build_object("
        "'identity_resolution_status','RESOLVED_SYNCED_CNIC',"
        "'identity_repair_reason','VERIFIED_SYNCED_CNIC',"
        "'device_user_id',u.id,'cnic_lookup_hash',u.cnic_lookup_hash,'cnic_encrypted',u.cnic_encrypted,"
        "'identity_snapshot_id',z.identity_snapshot_id,'device_serial',z.confirmed_serial,"
        "'captured_at',CAST(:captured AS timestamptz),"
        "'received_at',CAST(:captured AS timestamptz) + INTERVAL '1 second'))) "
        "FROM add_attendance_events e JOIN add_device_users u ON u.id=:user "
        "JOIN add_zkt_devices z ON z.id=e.zkt_device_id WHERE e.id=:id"
    ), {"id": event.id, "user": user.id, "captured": captured.isoformat()})


def test_settled_terminal_duplicate_keeps_the_postgres_release_guard_invariants(source_db):
    """Slot 3 read back as a byte copy of slot 2 (a TERMINAL_DUPLICATE) before P3 was written."""
    session, connector, _ = source_db
    postgres = session.get_bind().dialect.name == "postgresql"
    first_tail(session, connector, P2.row(3))
    copy = canonical(session, 3)
    p2 = session.get(AttendanceEvent, event_id(session, P2))
    assert (copy.disposition, copy.attendance_event_id) == ("TERMINAL_DUPLICATE", p2.id)
    assert newest_slot.is_unsettled_newest(session, copy)
    _guard_manifest_invariants(session, p2)
    # Captured more than ten minutes after the punch, so the guard needs its saved source row.
    if postgres:
        assert _postgres_guard(session, p2, captured_after=timedelta(minutes=15)) is True
        session.rollback()
    job = anchor_job(session, connector, cutoff=5)
    commit_chunk(session, connector, job, history(P0, P1, P2, P3, junk(4, "zero")))
    assert canonical(session, 3).raw_record_digest == P3.row(3).raw_record_digest
    assert canonical(session, 3).attendance_event_id == event_id(session, P3)
    retired = session.get(TerminalRecordManifest, copy.id)
    assert (retired.canonical_source, retired.disposition, retired.attendance_event_id) == (
        False, "TERMINAL_DUPLICATE", p2.id)
    rows = _guard_manifest_invariants(session, p2)
    assert {(row.ordinal, row.canonical_source) for row in rows} == {(2, True), (3, False)}
    if postgres:
        # The guard scans retired rows too: the settled copy keeps it true, and
        # a retired row it would refuse makes it false.
        assert _postgres_guard(session, p2, captured_after=timedelta(minutes=15)) is True
        retired.disposition = "INVALID_TIME"
        session.flush()
        assert _postgres_guard(session, p2, captured_after=timedelta(minutes=15)) is False
        session.rollback()


def test_source_evidence_lists_newest_slot_rows_for_a_pre_reconcile_check(source_db):
    """The runbook's read-only pre-check: newest-slot rows that delivered or held a punch."""
    from zk_add.zkt_source_evidence import list_evidence
    session, connector, _ = source_db
    stale = Punch(uid=17, user_id="44", local=at(7))
    live_capture(session, connector, stale)
    first_tail(session, connector, stale.row(3))
    listed = list_evidence(session, connector, newest=True, limit=50)["rows"]
    assert [(row["ordinal"], row["source_kind"], row["disposition"], row["newest_read"],
             row["newest_read_observed"]) for row in listed] == [
        (3, "TAIL", "EVENT", True, True), (0, "BASELINE", "EVENT", True, False)]
    assert [row["ordinal"] for row in list_evidence(session, connector, limit=50)["rows"]] == [3, 2, 1, 0]
