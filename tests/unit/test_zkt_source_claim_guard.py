"""Real source ingress must not promote a legacy UID-only interpretation."""
import base64
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from test_reconciliation import (
    SERIAL, RAW_RECORD, _certify_one_record_baseline, _source_record,
    reconciliation_db as reconciliation_db,
)
from zk_add.attendance_recovery import RecoveryError, _decode_zkt_record
from zk_add.crypto import decrypt_text, encrypt_text
from zk_add.models import AttendanceEvent, OrdsOutbox, ReconciliationDivergence, TerminalRecordManifest
from zk_add.reconciliation import (
    SOURCE_IDENTITY_GUARD_VERSION, SOURCE_MISSING_REFERENCE,
    apply_reconciliation_anchor, apply_reconciliation_chunk, apply_source_tail_chunk,
    create_reconciliation_job, reconciliation_chain_digest, reconciliation_chunk_digest,
)
from zk_add.schemas import (
    ReconciliationAnchorRequest, ReconciliationChunkRequest, SourceTailChunkRequest,
)
from zk_add.service import ingest_attendance
from zk_add.zkt_source_evidence import list_evidence, reveal_evidence


def missing_raw(kind):
    raw = bytearray(RAW_RECORD)
    if kind == "uid-only":
        return raw[:2] + raw[26:27] + raw[27:31] + raw[31:32]
    raw[2:26] = b" " * 24 if kind == "spaces" else b"\0" * 24
    if kind == "after-null":
        raw[3:7] = b"1007"  # Bytes after the terminator are not a user reference.
    return bytes(raw)


def source(raw, ordinal=0, *, disposition="EVENT", uid=None):
    row = _source_record()
    return row.model_copy(update={
        "ordinal": ordinal, "disposition": disposition,
        "raw_record_digest": hashlib.sha256(raw).hexdigest(),
        "raw_record_b64": base64.b64encode(raw).decode(),
        "terminal_record_key": hashlib.sha256(f"claim-{ordinal}".encode()).hexdigest(),
        "observed_uid": "7", "observed_user_id": "1007",
        "error_code": "ORIGINAL_DEVICE_CLASSIFICATION",
        "event": row.event.model_copy(update={
            "event_uid": uid or hashlib.sha256(f"claim-event-{ordinal}".encode()).hexdigest(),
        }),
    })


def sealed(draft):
    digest = reconciliation_chunk_digest(draft)
    return draft.model_copy(update={
        "chunk_digest": digest,
        "resulting_chain_digest": reconciliation_chain_digest(
            draft.previous_chain_digest, start_ordinal=draft.start_ordinal,
            end_ordinal=draft.end_ordinal, chunk_digest=digest,
        ),
    })


def baseline(db, connector, rows):
    connector.zkt_device.attendance_count = len(rows)
    job = create_reconciliation_job(
        db, connector=connector, actor="test", reason="Exercise source claim isolation",
        confirmation="RECONCILE 1 FROM START", idempotency_key=str(uuid4()),
    )
    size = len(base64.b64decode(rows[0].raw_record_b64))
    apply_reconciliation_anchor(db, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, generation=job.terminal_generation, terminal_serial=SERIAL,
        terminal_generation=job.terminal_generation, cutoff_count=len(rows),
        latest_terminal_count=len(rows), record_size=size,
        source_total_bytes=4 + size * len(rows), first_anchor_digest=rows[0].raw_record_digest,
    ))
    request = sealed(ReconciliationChunkRequest(
        job_id=job.job_id, generation=job.terminal_generation, sequence=0, start_ordinal=0,
        end_ordinal=len(rows), chunk_digest="0" * 64, previous_chain_digest=None,
        resulting_chain_digest="0" * 64, records=rows,
    ))
    return job, request


@pytest.mark.parametrize("kind", ["uid-only", "empty", "spaces", "after-null"])
@pytest.mark.parametrize("path", ["baseline", "tail"])
def test_legacy_claim_is_preserved_without_attendance_and_replay_is_stable(reconciliation_db, kind, path):
    db, connector = reconciliation_db
    raw = missing_raw(kind)
    if path == "baseline":
        row = source(raw)
        job, request = baseline(db, connector, [row])
        apply = apply_reconciliation_chunk
    else:
        coverage = _certify_one_record_baseline(db, connector)
        row = source(raw, 1)
        request = sealed(SourceTailChunkRequest(
            terminal_serial=SERIAL, terminal_generation=coverage.terminal_generation,
            record_size=len(raw), start_ordinal=1, end_ordinal=2, latest_terminal_count=2,
            chunk_digest="0" * 64, previous_chain_digest=coverage.source_committed_chain_digest,
            resulting_chain_digest="0" * 64, records=[row],
        ))
        apply = apply_source_tail_chunk
    events_before = db.scalar(select(func.count(AttendanceEvent.id)))
    outboxes_before = db.scalar(select(func.count(OrdsOutbox.id)))
    result = apply(db, connector=connector, payload=request)
    db.commit()
    manifest = db.scalar(select(TerminalRecordManifest).where(
        TerminalRecordManifest.ordinal == row.ordinal,
    ))
    assert not result[2] and manifest.disposition == "IDENTITY_UNRESOLVED"
    assert manifest.attendance_event_id is None and manifest.observed_user_id is None
    assert manifest.error_code == SOURCE_MISSING_REFERENCE
    assert manifest.declared_disposition == "EVENT"
    assert manifest.interpretation_version == SOURCE_IDENTITY_GUARD_VERSION
    assert base64.b64decode(decrypt_text(manifest.protected_raw_record)) == raw
    claim = json.loads(decrypt_text(manifest.protected_source_claim))
    assert claim["record"] == row.model_dump(mode="json", exclude={"raw_record_b64"})
    assert claim["reason"] == SOURCE_MISSING_REFERENCE
    assert "Ayesha" not in manifest.protected_source_claim
    original = manifest.protected_source_claim
    assert apply(db, connector=connector, payload=request)[2]
    db.commit()
    assert manifest.protected_source_claim == original
    assert db.scalar(select(func.count(AttendanceEvent.id))) == events_before
    assert db.scalar(select(func.count(OrdsOutbox.id))) == outboxes_before
    if path == "baseline":
        assert job.committed_next_ordinal == 1 and job.last_chain_digest == request.resulting_chain_digest
        assert result[1].accepted_count == 0 and result[1].blocked_identity_count == 1
        assert result[1].quarantined_count == 1
    else:
        assert coverage.source_committed_cursor == 2
        assert coverage.source_committed_chain_digest == request.resulting_chain_digest
        assert result[1].event_count == 0 and result[1].blocked_identity_count == 1
        assert result[1].exception_count == 1


def test_missing_reference_cannot_recover_existing_event_and_other_records_continue(reconciliation_db):
    db, connector = reconciliation_db
    held = source(missing_raw("empty"))
    incoming = held.event.model_copy(update={"raw_name": None})
    ingest_attendance(db, connector=connector, events=[incoming])
    db.flush()
    prior = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid))
    before = (prior.ords_status, prior.cnic_lookup_hash, prior.device_user_id, dict(prior.raw_event))
    valid = source(RAW_RECORD, 1)
    _, request = baseline(db, connector, [held, valid])
    _, chunk, _ = apply_reconciliation_chunk(db, connector=connector, payload=request)
    db.commit()
    assert (prior.ords_status, prior.cnic_lookup_hash, prior.device_user_id, prior.raw_event) == before
    assert chunk.accepted_count == 1 and chunk.blocked_identity_count == 1 and chunk.quarantined_count == 1
    manifests = db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.ordinal)).all()
    assert manifests[0].attendance_event_id is None
    assert manifests[1].attendance_event_id is not None and manifests[1].declared_disposition == "EVENT"


def test_claim_storage_failure_rolls_back_custody_and_cursor(reconciliation_db, monkeypatch):
    from zk_add import reconciliation
    db, connector = reconciliation_db
    _, request = baseline(db, connector, [source(missing_raw("empty"))])
    db.commit()
    original = reconciliation.encrypt_text
    def failed_claim(value):
        if value.startswith('{"interpretation_version"'):
            raise RuntimeError("claim store unavailable")
        return original(value)
    monkeypatch.setattr(reconciliation, "encrypt_text", failed_claim)
    with pytest.raises(RuntimeError, match="claim store unavailable"):
        apply_reconciliation_chunk(db, connector=connector, payload=request)
    db.rollback()
    assert not db.scalar(select(TerminalRecordManifest.id))
    assert not db.scalar(select(AttendanceEvent.id))
    monkeypatch.setattr(reconciliation, "encrypt_text", original)
    job, _, duplicate = apply_reconciliation_chunk(db, connector=connector, payload=request)
    assert not duplicate and job.committed_next_ordinal == 1


def test_claim_reveal_is_bound_audited_and_metadata_stays_private(reconciliation_db):
    db, connector = reconciliation_db
    _, request = baseline(db, connector, [source(missing_raw("empty"))])
    apply_reconciliation_chunk(db, connector=connector, payload=request)
    db.commit()
    row = db.scalar(select(TerminalRecordManifest))
    listed = json.dumps(list_evidence(db, connector), default=str)
    assert "Ayesha" not in listed and "1007" not in listed and "3520212345671" not in listed
    arguments = dict(actor="test", reason="Review preserved claim", idempotency_key=str(uuid4()))
    revealed = reveal_evidence(db, connector, row.id, **arguments)
    assert revealed["original_disposition"] == "EVENT"
    assert revealed["original_error_code"] == "ORIGINAL_DEVICE_CLASSIFICATION"
    assert revealed["custody_disposition"] == "IDENTITY_UNRESOLVED"
    assert revealed["submitted_interpretation"]["record"]["event"]["user_id"] == "1007"
    claim = json.loads(decrypt_text(row.protected_source_claim))
    claim["record"]["ordinal"] += 1
    row.protected_source_claim = encrypt_text(json.dumps(claim))
    with pytest.raises(ValueError, match="SOURCE_CLAIM_INTEGRITY"):
        reveal_evidence(db, connector, row.id, **arguments)


def test_epoch_recovery_copies_claim_without_reinterpreting_the_preserved_prefix(reconciliation_db):
    from zk_add.reconciliation import _activate_recovery_epoch
    from zk_add.time_utils import utc_now
    db, connector = reconciliation_db
    job, request = baseline(db, connector, [source(missing_raw("empty"))])
    apply_reconciliation_chunk(db, connector=connector, payload=request)
    db.flush()
    prior = db.scalar(select(TerminalRecordManifest))
    divergence = ReconciliationDivergence(job_id=job.id, source_epoch_id=job.source_epoch_id,
        ordinal=1, old_raw_digest="a" * 64, new_raw_digest="b" * 64)
    db.add(divergence)
    db.flush()
    _activate_recovery_epoch(db, job=job, divergence=divergence, now=utc_now())
    db.commit()
    copied = db.scalar(select(TerminalRecordManifest).where(
        TerminalRecordManifest.source_epoch_id == job.source_epoch_id))
    assert copied.id != prior.id and copied.source_kind == "RECOVERY_PREFIX"
    assert copied.protected_source_claim == prior.protected_source_claim
    assert copied.protected_raw_record == prior.protected_raw_record
    assert copied.declared_disposition == "EVENT" and copied.disposition == "IDENTITY_UNRESOLVED"
    assert copied.interpretation_version == SOURCE_IDENTITY_GUARD_VERSION
    assert copied.attendance_event_id is None and copied.observed_user_id is None


def test_old_uid_only_exception_cannot_gain_identity_through_clock_correction(reconciliation_db):
    raw = missing_raw("uid-only")
    manifest = TerminalRecordManifest(record_size=8,
        protected_raw_record=encrypt_text(base64.b64encode(raw).decode()),
        raw_record_digest=hashlib.sha256(raw).hexdigest(),
        observed_uid="7", observed_user_id="1007")
    with pytest.raises(RecoveryError, match="historical attendance UID"):
        _decode_zkt_record(manifest)
