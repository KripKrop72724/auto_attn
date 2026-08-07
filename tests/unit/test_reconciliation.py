from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.db import Base
from zk_add.crypto import decrypt_text
from zk_add.models import (
    AttendanceEvent,
    ReconciliationCoverage,
    ReconciliationDivergence,
    SourceTailChunk,
    TerminalRecordManifest,
    TerminalRecordReview,
)
from zk_add.reconciliation import (
    _version_tuple,
    apply_reconciliation_device_fault,
    apply_reconciliation_anchor,
    apply_reconciliation_assignment_release,
    apply_reconciliation_chunk,
    apply_reconciliation_manifest,
    apply_source_tail_chunk,
    apply_source_probe_result,
    assignment_rows,
    control_reconciliation_job,
    create_reconciliation_job,
    preflight_reconciliation,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
    reconciliation_scheduler_state,
    refresh_reconciliation_assurance,
)
from zk_add.schemas import (
    AttendanceEventIn,
    ReconciliationAnchorRequest,
    ReconciliationAssignmentReleaseRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
    ReconciliationSourceRecord,
    SourceProbeResultRequest,
    SourceTailChunkRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import onboard_connector, replace_user_snapshot
from zk_add.source_exceptions import (
    list_source_exceptions,
    reveal_source_exception,
    review_source_exception,
    source_exception_detail,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now


SERIAL = "ADZV211860253"
RAW_RECORD = bytes.fromhex("0700000102030400")


def test_production_firmware_version_format_is_supported() -> None:
    assert _version_tuple("zone-lite-2.3.0") == (2, 3, 0)
    assert _version_tuple("2.3.0") == (2, 3, 0)


@pytest.fixture()
def reconciliation_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(
        settings,
        "fleet_root_secret",
        "test-fleet-root-secret-with-enough-entropy",
    )
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "reconciliation_self_healing_enabled", True)
    monkeypatch.setattr(settings, "identity_snapshot_gate_enabled", False)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        connector, _token, _created = onboard_connector(
            session,
            hardware_id="e0:72:a1:d6:f3:28",
            zone_id="ZONE-SLICTOWER-3FL",
            zone_name="ZONE-SLICTOWER-3FL",
            device_id="1",
            firmware_version="2.3.0",
            expected_serial=SERIAL,
            actor="test",
            ip_address="127.0.0.1",
        )
        connector.connected = True
        connector.lifecycle_state = "ONLINE"
        zkt = connector.zkt_device
        assert zkt is not None
        zkt.serial = SERIAL
        zkt.online = True
        zkt.connection_state = "ONLINE"
        zkt.certification_state = "CERTIFIED"
        zkt.snapshot_complete = True
        zkt.identity_snapshot_stable = True
        zkt.user_count = 1
        zkt.attendance_count = 1
        zkt.capability_profile = {
            "read_attendance": True,
            "history_stream_v1": True,
            "history_range_resume_verified": True,
        }
        replace_user_snapshot(
            session,
            connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id="stable-source-snapshot",
                complete=True,
                observed_at=utc_now(),
                users=[
                    UserSnapshotRow(
                        uid="7",
                        user_id="1007",
                        name="Ayesha-3520212345671",
                        privilege=0,
                    )
                ],
            ),
        )
        zkt.identity_snapshot_stable = True
        session.commit()
        yield session, connector


def _source_record() -> ReconciliationSourceRecord:
    return ReconciliationSourceRecord(
        ordinal=0,
        raw_record_digest=hashlib.sha256(RAW_RECORD).hexdigest(),
        terminal_record_key=hashlib.sha256(b"terminal-record-0").hexdigest(),
        occurrence_index=1,
        disposition="EVENT",
        raw_record_b64=base64.b64encode(RAW_RECORD).decode(),
        event=AttendanceEventIn(
            event_uid=hashlib.sha256(b"event-0").hexdigest(),
            uid="7",
            user_id="1007",
            raw_name="Ayesha-3520212345671",
            device_event_time=datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc),
            captured_at=utc_now(),
            source="FULL_HISTORY",
            punch=0,
            status=0,
            clock_quality="OK",
            raw_event={"terminal_uid": "7"},
        ),
    )


def _certify_one_record_baseline(session: Session, connector):
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Establish a one-row source baseline for tail protocol tests.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="tail-baseline-0001",
    )
    raw_digest = hashlib.sha256(RAW_RECORD).hexdigest()
    apply_reconciliation_anchor(
        session,
        connector=connector,
        payload=ReconciliationAnchorRequest(
            job_id=job.job_id,
            generation=job.terminal_generation,
            terminal_serial=SERIAL,
            terminal_generation=job.terminal_generation,
            cutoff_count=1,
            latest_terminal_count=1,
            record_size=8,
            source_total_bytes=12,
            first_anchor_digest=raw_digest,
        ),
    )
    draft = ReconciliationChunkRequest(
        job_id=job.job_id,
        generation=job.terminal_generation,
        sequence=0,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest="0" * 64,
        previous_chain_digest=None,
        resulting_chain_digest="0" * 64,
        records=[_source_record()],
    )
    chunk_digest = reconciliation_chunk_digest(draft)
    chain_digest = reconciliation_chain_digest(
        None,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest=chunk_digest,
    )
    apply_reconciliation_chunk(
        session,
        connector=connector,
        payload=draft.model_copy(
            update={
                "chunk_digest": chunk_digest,
                "resulting_chain_digest": chain_digest,
            }
        ),
    )
    apply_reconciliation_manifest(
        session,
        connector=connector,
        payload=ReconciliationManifestRequest(
            job_id=job.job_id,
            generation=job.terminal_generation,
            terminal_serial=SERIAL,
            terminal_generation=job.terminal_generation,
            cutoff_count=1,
            latest_terminal_count=1,
            final_chain_digest=chain_digest,
        ),
    )
    coverage = session.scalar(
        select(ReconciliationCoverage).where(ReconciliationCoverage.active == True)  # noqa: E712
    )
    assert coverage is not None
    return coverage


def _tail_source(
    ordinal: int,
    disposition: str,
    *,
    event: bool = False,
) -> ReconciliationSourceRecord:
    raw = bytes([ordinal + 1]) * 8
    attendance = None
    if event:
        attendance = AttendanceEventIn(
            event_uid=hashlib.sha256(f"tail-event-{ordinal}".encode()).hexdigest(),
            uid="7",
            user_id="1007",
            raw_name="Ayesha-3520212345671",
            device_event_time=datetime(2026, 8, 6, 9, ordinal, tzinfo=timezone.utc),
            captured_at=utc_now(),
            source="CURRENT_RECONCILE",
            punch=0,
            status=0,
            clock_quality="OK",
            raw_event={"terminal_uid": "7"},
        )
    return ReconciliationSourceRecord(
        ordinal=ordinal,
        raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(f"tail-record-{ordinal}".encode()).hexdigest(),
        occurrence_index=1,
        disposition=disposition,
        raw_record_b64=base64.b64encode(raw).decode(),
        raw_timestamp=0xFFFFFFFF if disposition == "INVALID_TIME" else ordinal,
        observed_uid="7",
        observed_user_id="1007",
        error_code=(
            "ZKT_TIMESTAMP_OUT_OF_RANGE"
            if disposition == "INVALID_TIME"
            else "ZKT_RECORD_MALFORMED" if disposition == "MALFORMED" else None
        ),
        event=attendance,
    )


def _add_ready_connector(session: Session, index: int):
    serial = f"TEST-SERIAL-{index}"
    connector, _token, _created = onboard_connector(
        session,
        hardware_id=f"02:00:00:00:00:{index:02x}",
        zone_id=f"ZONE-TEST-{index}",
        zone_name=f"ZONE-TEST-{index}",
        device_id=str(index),
        firmware_version="2.3.0",
        expected_serial=serial,
        actor="test",
        ip_address="127.0.0.1",
    )
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.serial = serial
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = True
    zkt.identity_snapshot_stable = True
    zkt.user_count = 1
    zkt.attendance_count = 1
    zkt.capability_profile = {
        "read_attendance": True,
        "history_stream_v1": True,
        "history_range_resume_verified": True,
    }
    replace_user_snapshot(
        session,
        connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=f"stable-source-snapshot-{index}",
            complete=True,
            observed_at=utc_now(),
            users=[
                UserSnapshotRow(
                    uid=str(index),
                    user_id=f"user-{index}",
                    name=f"Worker {index}-3520212345{index:03d}",
                    privilege=0,
                )
            ],
        ),
    )
    zkt.identity_snapshot_stable = True
    session.flush()
    return connector


def test_full_history_reconciliation_is_contiguous_resumable_and_separately_certified(
    reconciliation_db,
):
    session, connector = reconciliation_db
    preflight = preflight_reconciliation(session, connector)
    assert preflight["eligible"] is True
    assert preflight["ready_now"] is True

    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Establish the immutable start-of-time source baseline.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-test-0001",
    )
    raw_digest = hashlib.sha256(RAW_RECORD).hexdigest()
    apply_reconciliation_anchor(
        session,
        connector=connector,
        payload=ReconciliationAnchorRequest(
            job_id=job.job_id,
            generation=job.terminal_generation,
            terminal_serial=SERIAL,
            terminal_generation=job.terminal_generation,
            cutoff_count=1,
            latest_terminal_count=1,
            record_size=8,
            source_total_bytes=12,
            first_anchor_digest=raw_digest,
        ),
    )
    source = _source_record()
    draft = ReconciliationChunkRequest(
        job_id=job.job_id,
        generation=job.terminal_generation,
        sequence=0,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest="0" * 64,
        previous_chain_digest=None,
        resulting_chain_digest="0" * 64,
        records=[source],
    )
    chunk_digest = reconciliation_chunk_digest(draft)
    chain_digest = reconciliation_chain_digest(
        None,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest=chunk_digest,
    )
    chunk_request = draft.model_copy(
        update={
            "chunk_digest": chunk_digest,
            "resulting_chain_digest": chain_digest,
        }
    )
    _job, chunk, duplicate = apply_reconciliation_chunk(
        session,
        connector=connector,
        payload=chunk_request,
    )
    assert duplicate is False
    replay_job, replay_chunk, duplicate = apply_reconciliation_chunk(
        session,
        connector=connector,
        payload=chunk_request,
    )
    assert duplicate is True
    assert replay_chunk.id == chunk.id
    assert replay_job.committed_next_ordinal == 1

    divergent_replay = chunk_request.model_copy(
        update={
            "chunk_digest": "f" * 64,
            "resulting_chain_digest": "e" * 64,
        }
    )
    held_job, held_chunk, duplicate = apply_reconciliation_chunk(
        session,
        connector=connector,
        payload=divergent_replay,
    )
    assert duplicate is False
    assert held_chunk.id == chunk.id
    assert held_job.status == "NEEDS_ATTENTION"
    assert held_job.error_code == "COMMITTED_RANGE_DIVERGED"

    held_job.status = "RUNNING"
    held_job.error_code = None
    held_job.error_message = None
    held_job.wait_reason = None

    apply_reconciliation_manifest(
        session,
        connector=connector,
        payload=ReconciliationManifestRequest(
            job_id=job.job_id,
            generation=job.terminal_generation,
            terminal_serial=SERIAL,
            terminal_generation=job.terminal_generation,
            cutoff_count=1,
            latest_terminal_count=1,
            final_chain_digest=chain_digest,
        ),
    )
    coverage = session.scalar(select(ReconciliationCoverage))
    assert coverage is not None
    assert coverage.capture_state == "SOURCE_CAPTURE_CERTIFIED"
    assert coverage.oracle_state == "ORACLE_MEMBERSHIP_PENDING"
    assert job.status == "RUNNING"

    event = session.scalar(select(AttendanceEvent))
    assert event is not None
    event.ords_status = "ACKED"
    refresh_reconciliation_assurance(session, job)
    assert job.status == "COMPLETED"
    assert job.capture_certificate["evidence_signature"]
    assert job.oracle_certificate["evidence_signature"]
    assert coverage.oracle_state == "ORACLE_MEMBERSHIP_CERTIFIED"


def test_source_record_digest_must_match_protected_raw_evidence(reconciliation_db):
    session, connector = reconciliation_db
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Test fail-closed source evidence validation.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-test-0002",
    )
    raw_digest = hashlib.sha256(RAW_RECORD).hexdigest()
    apply_reconciliation_anchor(
        session,
        connector=connector,
        payload=ReconciliationAnchorRequest(
            job_id=job.job_id,
            generation=job.terminal_generation,
            terminal_serial=SERIAL,
            terminal_generation=job.terminal_generation,
            cutoff_count=1,
            latest_terminal_count=1,
            record_size=8,
            source_total_bytes=12,
            first_anchor_digest=raw_digest,
        ),
    )
    source = _source_record().model_copy(
        update={"raw_record_b64": base64.b64encode(b"tampered").decode()}
    )
    request = ReconciliationChunkRequest(
        job_id=job.job_id,
        generation=job.terminal_generation,
        sequence=0,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest="0" * 64,
        previous_chain_digest=None,
        resulting_chain_digest="0" * 64,
        records=[source],
    )
    with pytest.raises(ValueError, match="digest does not match"):
        apply_reconciliation_chunk(session, connector=connector, payload=request)


def test_source_tail_accounts_for_poison_rows_and_replays_after_ack_loss(
    reconciliation_db,
):
    session, connector = reconciliation_db
    coverage = _certify_one_record_baseline(session, connector)
    connector.zkt_device.attendance_count = 4
    records = [
        _tail_source(1, "INVALID_TIME"),
        _tail_source(2, "EVENT", event=True),
        _tail_source(3, "MALFORMED"),
    ]
    draft = SourceTailChunkRequest(
        terminal_serial=SERIAL,
        terminal_generation=coverage.terminal_generation,
        record_size=8,
        start_ordinal=1,
        end_ordinal=4,
        latest_terminal_count=4,
        chunk_digest="0" * 64,
        previous_chain_digest=coverage.source_committed_chain_digest,
        resulting_chain_digest="0" * 64,
        records=records,
    )
    chunk_digest = reconciliation_chunk_digest(draft)
    resulting_chain = reconciliation_chain_digest(
        coverage.source_committed_chain_digest,
        start_ordinal=1,
        end_ordinal=4,
        chunk_digest=chunk_digest,
    )
    request = draft.model_copy(
        update={
            "chunk_digest": chunk_digest,
            "resulting_chain_digest": resulting_chain,
        }
    )

    committed, chunk, duplicate, error_code = apply_source_tail_chunk(
        session, connector=connector, payload=request
    )
    session.flush()
    assert error_code is None
    assert duplicate is False
    assert chunk is not None
    assert committed.source_committed_cursor == 4
    assert committed.source_committed_chain_digest == resulting_chain
    assert chunk.exception_count == 2
    assert chunk.event_count == 1

    manifests = session.scalars(
        select(TerminalRecordManifest)
        .where(TerminalRecordManifest.canonical_source == True)  # noqa: E712
        .order_by(TerminalRecordManifest.ordinal)
    ).all()
    assert [row.ordinal for row in manifests] == [0, 1, 2, 3]
    assert [row.disposition for row in manifests[1:]] == [
        "INVALID_TIME",
        "EVENT",
        "MALFORMED",
    ]
    assert manifests[1].attendance_event_id is None
    assert manifests[2].attendance_event_id is not None
    assert manifests[3].attendance_event_id is None
    assert manifests[1].protected_raw_record != records[0].raw_record_b64
    assert decrypt_text(manifests[1].protected_raw_record) == records[0].raw_record_b64

    # The same exact range is the expected retry after transport ACK loss. It
    # must not create a second source row or attendance event.
    replay_coverage, replay_chunk, duplicate, error_code = apply_source_tail_chunk(
        session, connector=connector, payload=request
    )
    assert error_code is None
    assert duplicate is True
    assert replay_chunk is not None and replay_chunk.id == chunk.id
    assert replay_chunk.end_ordinal == 4
    assert replay_coverage.source_committed_cursor == 4
    assert session.scalar(select(func.count(SourceTailChunk.id))) == 1
    assert session.scalar(select(func.count(TerminalRecordManifest.id))) == 4
    assert session.scalar(select(func.count(AttendanceEvent.id))) == 2

    report = list_source_exceptions(session)
    assert report["totals"] == {
        "all": 2,
        "open": 2,
        "reviewed": 0,
        "invalid_time": 1,
        "malformed": 1,
        "affected_terminals": 1,
    }
    assert all(row["cursor_advanced"] for row in report["rows"])
    assert all(row["evidence_available"] for row in report["rows"])

    exception = manifests[1]
    first_review = review_source_exception(
        session,
        row=exception,
        actor="operator",
        reason="Reviewed immutable terminal timestamp evidence.",
        idempotency_key="review-poison-0001",
    )
    repeated_review = review_source_exception(
        session,
        row=exception,
        actor="operator",
        reason="Reviewed immutable terminal timestamp evidence.",
        idempotency_key="review-poison-0001",
    )
    session.flush()
    assert repeated_review.id == first_review.id
    assert session.scalar(select(func.count(TerminalRecordReview.id))) == 1
    detail = source_exception_detail(session, exception)
    assert detail["review_state"] == "REVIEWED"
    assert len(detail["reviews"]) == 1
    revealed = reveal_source_exception(
        session,
        row=exception,
        actor="operator",
        reason="Investigate the original terminal timestamp bytes.",
        idempotency_key="reveal-poison-0001",
    )
    assert revealed["raw_record_b64"] == records[0].raw_record_b64
    assert bytes.fromhex(revealed["raw_record_hex"]) == bytes([2]) * 8


def test_source_tail_digest_mutation_invalidates_coverage(reconciliation_db):
    session, connector = reconciliation_db
    coverage = _certify_one_record_baseline(session, connector)
    connector.zkt_device.attendance_count = 2
    record = _tail_source(1, "INVALID_TIME")
    draft = SourceTailChunkRequest(
        terminal_serial=SERIAL,
        terminal_generation=coverage.terminal_generation,
        record_size=8,
        start_ordinal=1,
        end_ordinal=2,
        latest_terminal_count=2,
        chunk_digest="0" * 64,
        previous_chain_digest=coverage.source_committed_chain_digest,
        resulting_chain_digest="0" * 64,
        records=[record],
    )
    chunk_digest = reconciliation_chunk_digest(draft)
    resulting_chain = reconciliation_chain_digest(
        coverage.source_committed_chain_digest,
        start_ordinal=1,
        end_ordinal=2,
        chunk_digest=chunk_digest,
    )
    request = draft.model_copy(
        update={
            "chunk_digest": chunk_digest,
            "resulting_chain_digest": resulting_chain,
        }
    )
    apply_source_tail_chunk(session, connector=connector, payload=request)
    divergent = request.model_copy(update={"chunk_digest": "f" * 64})
    held, chunk, duplicate, error_code = apply_source_tail_chunk(
        session, connector=connector, payload=divergent
    )
    assert chunk is None
    assert duplicate is False
    assert error_code == "SOURCE_TAIL_REPLAY_DIVERGED"
    assert held.active is False
    assert held.capture_state == "SOURCE_COVERAGE_INVALIDATED"


def test_scan_slot_stays_owned_during_assignment_cooldown(reconciliation_db):
    session, connector = reconciliation_db
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify globally serialized terminal scan ownership.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-test-0003",
    )
    first = assignment_rows(session)
    second = assignment_rows(session)
    assert first and first[0][1]["job_id"] == job.job_id
    assert second == []


def test_stream_v2_grants_one_durable_credit_without_resetting_checkpoint(
    reconciliation_db,
):
    session, connector = reconciliation_db
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.capability_profile = {
        **(zkt.capability_profile or {}),
        "history_stream_v2": True,
        "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    zkt.attendance_count = 500
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify durable stream-v2 source credits and cursor continuity.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-stream-v2-0001",
    )
    job.cutoff_count = 500
    job.record_size = 8
    job.first_anchor_digest = hashlib.sha256(RAW_RECORD).hexdigest()
    job.committed_next_ordinal = 100
    job.scanned_count = 100
    job.last_chain_digest = hashlib.sha256(b"checkpoint-100").hexdigest()
    assignment = assignment_rows(session)[0][1]
    assert assignment["protocol"] == "history_stream_v2"
    assert assignment["committed_next_ordinal"] == 100
    assert assignment["chunk_records"] == 100
    assert assignment["credit_end_ordinal"] == 500
    assert assignment["max_chunks"] == 4
    assert assignment["assignment_id"] == job.active_assignment_id
    assert assignment_rows(session) == []
    assert job.committed_next_ordinal == 100
    assert job.last_chain_digest == hashlib.sha256(b"checkpoint-100").hexdigest()
    apply_reconciliation_assignment_release(
        session,
        connector=connector,
        payload=ReconciliationAssignmentReleaseRequest(
            assignment_id=assignment["assignment_id"],
            job_id=job.job_id,
            generation=job.terminal_generation,
            committed_next_ordinal=100,
            reason="COMMAND_PENDING",
        ),
    )
    assert job.active_assignment_id is None
    assert job.committed_next_ordinal == 100
    assert job.last_chain_digest == hashlib.sha256(b"checkpoint-100").hexdigest()


@pytest.mark.parametrize(
    ("remaining", "expected_credit"),
    [(1, 1), (31, 31), (99, 99), (100, 100), (101, 101)],
)
def test_stream_v2_grants_final_partial_credit(
    reconciliation_db,
    remaining: int,
    expected_credit: int,
):
    session, connector = reconciliation_db
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.capability_profile = {
        **(zkt.capability_profile or {}),
        "history_stream_v2": True,
        "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    cutoff = 5_100 + remaining
    zkt.attendance_count = cutoff
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify final partial source credits remain durable.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key=f"partial-credit-{remaining:04d}",
    )
    job.cutoff_count = cutoff
    job.record_size = 40
    job.committed_next_ordinal = 5_100
    job.scanned_count = 5_100
    job.last_chain_digest = hashlib.sha256(b"checkpoint-5100").hexdigest()

    assignment = assignment_rows(session)[0][1]

    assert assignment["committed_next_ordinal"] == 5_100
    assert assignment["credit_end_ordinal"] == 5_100 + expected_credit
    assert assignment["chunk_records"] == 100
    assert job.wait_reason is None


def test_stream_v2_offers_manifest_handshake_at_committed_cutoff(
    reconciliation_db,
):
    session, connector = reconciliation_db
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.capability_profile = {
        **(zkt.capability_profile or {}),
        "history_stream_v2": True,
        "history_chunk_max_records": 100,
        "history_credit_max_records": 400,
    }
    zkt.attendance_count = 500
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify the final manifest is offered after the source cursor reaches cutoff.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-stream-v2-manifest-0001",
    )
    job.status = "RUNNING"
    job.cutoff_count = 500
    job.record_size = 8
    job.first_anchor_digest = hashlib.sha256(RAW_RECORD).hexdigest()
    job.committed_next_ordinal = 500
    job.scanned_count = 500
    job.last_chain_digest = hashlib.sha256(b"checkpoint-500").hexdigest()

    assignment = assignment_rows(session)[0][1]

    assert assignment["protocol"] == "history_stream_v2"
    assert assignment["committed_next_ordinal"] == 500
    assert assignment["cutoff_count"] == 500
    assert assignment["credit_end_ordinal"] == 500
    assert assignment["max_chunks"] == 1


def test_retry_releases_transport_lease_but_preserves_source_checkpoint(
    reconciliation_db,
):
    session, connector = reconciliation_db
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify a held job resumes without losing durable source progress.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-retry-preserves-0001",
    )
    digest = hashlib.sha256(b"preserved-checkpoint").hexdigest()
    job.status = "NEEDS_ATTENTION"
    job.committed_next_ordinal = 2250
    job.scanned_count = 2250
    job.last_chain_digest = digest
    job.active_assignment_id = "11111111-1111-1111-1111-111111111111"
    resumed = control_reconciliation_job(
        session,
        job=job,
        action="retry",
        actor="operator",
        reason="Revalidate the same source boundary after the firmware demultiplexer upgrade.",
        idempotency_key="retry-preserved-checkpoint-0001",
    )
    assert resumed.status == "QUEUED"
    assert resumed.committed_next_ordinal == 2250
    assert resumed.scanned_count == 2250
    assert resumed.last_chain_digest == digest
    assert resumed.active_assignment_id is None


def test_six_parallel_scan_slots_are_bounded_and_device_isolated(
    reconciliation_db,
    monkeypatch: pytest.MonkeyPatch,
):
    session, first_connector = reconciliation_db
    monkeypatch.setattr(settings, "reconciliation_device_concurrency", 6)
    connectors = [first_connector]
    connectors.extend(_add_ready_connector(session, index) for index in range(2, 8))
    jobs = [
        create_reconciliation_job(
            session,
            connector=connector,
            actor="operator",
            reason="Verify bounded parallel source scan scheduling.",
            confirmation=f"RECONCILE {connector.device_id} FROM START",
            idempotency_key=f"parallel-reconcile-{index}",
        )
        for index, connector in enumerate(connectors, start=1)
    ]

    first = assignment_rows(session)
    assert [payload["job_id"] for _connector_id, payload in first] == [
        job.job_id for job in jobs[:6]
    ]
    assert jobs[6].phase == "WAITING_FOR_CAPACITY"
    assert jobs[6].wait_reason == "WAITING_FOR_SCAN_SLOT"
    scheduler = reconciliation_scheduler_state(session)
    assert scheduler == {
        "policy": "BOUNDED_PARALLEL_PER_DEVICE",
        "device_concurrency": 6,
        "active_scan_jobs": 6,
        "waiting_scan_jobs": 1,
        "available_scan_slots": 0,
        "history_backlog": 0,
        "history_backlog_limit": 10_000,
        "reserved_credit": 0,
        "available_credit": 10_000,
    }

    first_connector.connected = False
    second = assignment_rows(session)
    assert [payload["job_id"] for _connector_id, payload in second] == [jobs[6].job_id]
    assert jobs[0].wait_reason == "WAITING_FOR_DEVICE"


@pytest.mark.parametrize(
    "fault_code",
    ["SOURCE_COMMITTED_BOUNDARY_DIVERGED", "SOURCE_RANGE_COUNT_REGRESSION"],
)
def test_unconfirmed_firmware_source_divergence_holds_add_job(
    reconciliation_db, fault_code
):
    session, connector = reconciliation_db
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify firmware safety faults become durable ADD state.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="reconcile-test-0004",
    )
    changed = apply_reconciliation_device_fault(
        session,
        connector=connector,
        code=fault_code,
    )
    assert changed is True
    assert job.status == "NEEDS_ATTENTION"
    assert job.error_code == fault_code
    assert assignment_rows(session) == []


def test_raw_source_divergence_uses_fresh_probes_and_activates_recovery_epoch(
    reconciliation_db,
):
    session, connector = reconciliation_db
    zkt = connector.zkt_device
    assert zkt is not None
    zkt.capability_profile = {
        **(zkt.capability_profile or {}),
        "history_stream_v2": True,
        "source_divergence_probe_v1": True,
    }
    job = create_reconciliation_job(
        session,
        connector=connector,
        actor="operator",
        reason="Verify a stable source mutation creates a preserved recovery epoch.",
        confirmation="RECONCILE 1 FROM START",
        idempotency_key="source-divergence-probe-0001",
    )
    changed_raw = bytes.fromhex("0800000102030400")
    changed = _source_record().model_copy(
        update={
            "raw_record_digest": hashlib.sha256(changed_raw).hexdigest(),
            "terminal_record_key": hashlib.sha256(b"changed-terminal-record").hexdigest(),
            "raw_record_b64": base64.b64encode(changed_raw).decode(),
        }
    )
    job.status = "RUNNING"
    job.cutoff_count = 1
    job.record_size = 8
    job.first_anchor_digest = changed.raw_record_digest
    session.add(
        TerminalRecordManifest(
            job_id=None,
            chunk_id=None,
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            terminal_serial=SERIAL,
            generation=job.terminal_generation,
            source_epoch_id=job.source_epoch_id,
            ordinal=0,
            source_kind="TAIL",
            canonical_source=True,
            record_size=8,
            raw_record_digest=hashlib.sha256(RAW_RECORD).hexdigest(),
            terminal_record_key=hashlib.sha256(b"terminal-record-0").hexdigest(),
            occurrence_index=1,
            disposition="EVENT",
            protected_raw_record="protected-existing-evidence",
        )
    )
    session.flush()
    draft = ReconciliationChunkRequest(
        job_id=job.job_id,
        generation=job.terminal_generation,
        sequence=0,
        start_ordinal=0,
        end_ordinal=1,
        chunk_digest="0" * 64,
        previous_chain_digest=None,
        resulting_chain_digest="0" * 64,
        records=[changed],
    )
    chunk_digest = reconciliation_chunk_digest(draft)
    request = draft.model_copy(
        update={
            "chunk_digest": chunk_digest,
            "resulting_chain_digest": reconciliation_chain_digest(
                None,
                start_ordinal=0,
                end_ordinal=1,
                chunk_digest=chunk_digest,
            ),
        }
    )

    held, chunk, duplicate = apply_reconciliation_chunk(
        session, connector=connector, payload=request
    )

    assert chunk is None and duplicate is False
    assert held.phase == "VERIFYING_SOURCE_CHANGE"
    divergence = session.scalar(select(ReconciliationDivergence))
    assert divergence is not None
    original_epoch_id = job.source_epoch_id
    probe = SourceProbeResultRequest(
        job_id=job.job_id,
        generation=job.terminal_generation,
        terminal_serial=SERIAL,
        latest_terminal_count=1,
        record_size=8,
        ordinal=0,
        record=changed,
    )
    apply_source_probe_result(session, connector=connector, payload=probe)
    apply_source_probe_result(session, connector=connector, payload=probe)

    assert divergence.state == "CONFIRMED_NEW_EPOCH"
    assert job.source_epoch_id != original_epoch_id
    assert job.status == "QUEUED"
    assert job.committed_next_ordinal == 0
    assert job.review_required is True
    assert job.completion_outcome == "CURRENT_TRUTH_CERTIFIED_WITH_SOURCE_CHANGE"
