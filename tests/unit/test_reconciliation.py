from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zk_add.db import Base
from zk_add.models import AttendanceEvent, ReconciliationCoverage
from zk_add.reconciliation import (
    _version_tuple,
    apply_reconciliation_device_fault,
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    apply_reconciliation_manifest,
    assignment_rows,
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
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
    ReconciliationSourceRecord,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import onboard_connector, replace_user_snapshot
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
    assert reconciliation_scheduler_state(session) == {
        "policy": "BOUNDED_PARALLEL_PER_DEVICE",
        "device_concurrency": 6,
        "active_scan_jobs": 6,
        "waiting_scan_jobs": 1,
        "available_scan_slots": 0,
    }

    first_connector.connected = False
    second = assignment_rows(session)
    assert [payload["job_id"] for _connector_id, payload in second] == [jobs[6].job_id]
    assert jobs[0].wait_reason == "WAITING_FOR_DEVICE"


def test_firmware_source_divergence_immediately_holds_add_job(reconciliation_db):
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
        code="SOURCE_COMMITTED_BOUNDARY_DIVERGED",
    )
    assert changed is True
    assert job.status == "NEEDS_ATTENTION"
    assert job.error_code == "SOURCE_COMMITTED_BOUNDARY_DIVERGED"
    assert assignment_rows(session) == []
