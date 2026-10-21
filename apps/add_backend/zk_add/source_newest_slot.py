"""A ZKT terminal's newest attendance slot is not yet evidence of a punch.

Production evidence from 21 October 2026, on every ZKT model and every Zone
Lite generation that runs source tails:

* Every tail source exception sat at the read's newest ordinal, that is
  ``latest_terminal_count - 1``. Tail ranges capped at 100 records, which end
  before the newest slot, ended cleanly.
* That slot read back as zero bytes, unrelated bytes, or a byte copy of an
  earlier record. Once a later record existed, a fresh read of the same
  ordinal returned an ordinary punch. MULTAN-01's generation-2 baseline
  re-read 391 of 392 such ordinals as valid attendance with new digests.
* The punch that later fills the slot is the first punch after the read. If
  LIVE capture missed that punch, the tail never saw it either, because its
  cursor had already moved past the slot.

So a read of the newest slot says nothing about the punch the slot will
hold. ADD keeps such a row fail-closed and replaces it only with a fresh read
of the same ordinal, in the same terminal generation and source epoch, made
while a later record existed. The replacement goes through ordinary source
ingestion, so event-UID de-duplication against LIVE rows and every identity
rule still apply. The superseded row stays as non-canonical evidence.
"""
from __future__ import annotations

import base64
import binascii
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import encrypt_text
from zk_add.models import (
    AttendanceEvent,
    Connector,
    ReconciliationCoverage,
    ReconciliationJob,
    SourceTailChunk,
    TerminalRecordManifest,
)


# Operator-facing condition. Stored error codes stay exactly as the reader sent them.
UNSETTLED_CONDITION = "TERMINAL_NEWEST_RECORD_UNSETTLED"
REPLAY_SUPERSEDED_CODE = "SOURCE_TAIL_REPLAY_SUPERSEDED"
SETTLED_STATE = "SOURCE_NEWEST_RECORD_SETTLED"
EXCEPTION_DISPOSITIONS = frozenset({"INVALID_TIME", "MALFORMED", "IDENTITY_UNRESOLVED"})
# Rows that delivered no attendance of their own: a fail-closed exception, or
# a byte copy of an earlier record that only links that record's event.
SETTLEABLE_DISPOSITIONS = EXCEPTION_DISPOSITIONS | {"TERMINAL_DUPLICATE"}
OPERATOR_REASON = (
    "The terminal reported this ordinal as its newest record before it had written the "
    "record. These bytes are not the punch in this slot, so nothing was sent to Oracle and "
    "they must not be corrected into a punch. ADD replaces this row when the slot is read "
    "again after a later record exists, for example by a full source reconcile."
)


def read_was_newest(ordinal: int, latest_terminal_count: int | None) -> bool:
    """Whether ``ordinal`` was the terminal's newest slot when it was read."""
    return latest_terminal_count is not None and ordinal + 1 == latest_terminal_count


def baseline_newest_ordinal(job: ReconciliationJob) -> int | None:
    """The ordinal a baseline read while it may still have been the newest slot.

    The anchor's prepared buffer held ``(source_total_bytes - 4) / record_size``
    records. Only when that equals the cutoff could the final ordinal still be
    the newest slot when its chunk was read; the count never shrinks in
    between without a separate regression hold.
    """
    if (not job.cutoff_count or not job.record_size or job.source_total_bytes is None
            or job.source_total_bytes != 4 + job.cutoff_count * job.record_size):
        return None
    return job.cutoff_count - 1


def _epoch_clause(column, value):
    return column.is_(None) if value is None else column == value


def unsettled_newest_ids(session: Session, rows) -> set[int]:
    """IDs of stored rows whose bytes came from the terminal's newest slot.

    The committed receipt that read a row says so: a tail chunk that ended at
    its own ``latest_terminal_count``, or a baseline whose anchor ended at its
    cutoff. Rows stored before this rule existed are therefore recognised the
    same way as new ones. A recovery-epoch copy is judged by the read it
    copies. Rows that produced their own attendance are never included.
    """
    found: set[int] = set()
    pending = {row.id: row for row in rows if row.disposition in SETTLEABLE_DISPOSITIONS}
    copies = [row for row in pending.values() if row.source_kind == "RECOVERY_PREFIX"]
    if copies:
        originals: dict[tuple, TerminalRecordManifest] = {}
        for original in session.scalars(
            select(TerminalRecordManifest).where(
                TerminalRecordManifest.zkt_device_id.in_({row.zkt_device_id for row in copies}),
                TerminalRecordManifest.raw_record_digest.in_({row.raw_record_digest for row in copies}),
                TerminalRecordManifest.source_kind.in_(("TAIL", "BASELINE")),
            ).order_by(TerminalRecordManifest.id.asc())
        ).all():
            key = (original.zkt_device_id, original.generation, original.ordinal,
                   original.raw_record_digest)
            originals.setdefault(key, original)
        for row in copies:
            original = originals.get(
                (row.zkt_device_id, row.generation, row.ordinal, row.raw_record_digest)
            )
            if original is None:
                pending.pop(row.id)
            else:
                pending[row.id] = original
    tail = [(row_id, row) for row_id, row in pending.items() if row.source_kind == "TAIL"]
    if tail:
        receipts = set(session.execute(
            select(
                SourceTailChunk.zkt_device_id,
                SourceTailChunk.generation,
                SourceTailChunk.end_ordinal,
                ReconciliationCoverage.source_epoch_id,
            )
            .join(ReconciliationCoverage, ReconciliationCoverage.id == SourceTailChunk.coverage_id)
            .where(
                SourceTailChunk.zkt_device_id.in_({row.zkt_device_id for _, row in tail}),
                SourceTailChunk.end_ordinal.in_({row.ordinal + 1 for _, row in tail}),
                SourceTailChunk.latest_terminal_count == SourceTailChunk.end_ordinal,
            )
        ).all())
        found |= {
            row_id for row_id, row in tail
            if (row.zkt_device_id, row.generation, row.ordinal + 1, row.source_epoch_id) in receipts
        }
    baseline = [(row_id, row) for row_id, row in pending.items()
                if row.source_kind == "BASELINE" and row.job_id is not None]
    if baseline:
        jobs = {
            job.id: job for job in session.scalars(
                select(ReconciliationJob).where(
                    ReconciliationJob.id.in_({row.job_id for _, row in baseline})
                )
            ).all()
        }
        found |= {
            row_id for row_id, row in baseline
            if (job := jobs.get(row.job_id)) is not None
            and baseline_newest_ordinal(job) == row.ordinal
        }
    return found


def is_unsettled_newest(session: Session, row: TerminalRecordManifest) -> bool:
    return row.id in unsettled_newest_ids(session, [row])


def same_source_read(prior: TerminalRecordManifest, source) -> bool:
    return (
        prior.raw_record_digest == source.raw_record_digest
        and prior.terminal_record_key == source.terminal_record_key
        and prior.occurrence_index == source.occurrence_index
    )


def retire_unsettled_row(
    session: Session,
    *,
    prior: TerminalRecordManifest,
    source,
    origin: str,
    job_id: str | None = None,
) -> dict:
    """Keep the unsettled read as evidence, but no longer as the slot's source.

    The caller inserts the settled read as the canonical row in the same
    transaction. The unique canonical index requires this flush first.
    """
    prior.canonical_source = False
    session.flush()
    details = {
        "ordinal": prior.ordinal,
        "terminal_generation": prior.generation,
        "superseded_manifest_id": prior.id,
        "superseded_disposition": prior.disposition,
        "superseded_raw_record_digest": prior.raw_record_digest,
        "settled_raw_record_digest": source.raw_record_digest,
        "settled_disposition": source.disposition,
        "origin": origin,
    }
    if job_id is not None:
        details["job_id"] = job_id
    append_audit(
        session,
        actor="system:source-reconciliation",
        action=SETTLED_STATE,
        target_type="terminal_source_record",
        target_id=str(prior.id),
        outcome="SUCCESS",
        after=details,
    )
    return details


def coverage_authority(session: Session, coverage: ReconciliationCoverage) -> dict:
    """The wire coverage every Zone Lite generation applies as its tail cursor."""
    from zk_add.reconciliation import source_epoch_uuid

    return {
        "type": "source_coverage",
        "coverage_id": coverage.coverage_id,
        "source_epoch_id": coverage.source_epoch_id,
        "source_epoch": source_epoch_uuid(session, coverage),
        "terminal_serial": coverage.terminal_serial,
        "terminal_generation": coverage.terminal_generation,
        "certified_source_cursor": coverage.certified_source_cursor,
        "source_chain_digest": coverage.source_chain_digest,
        "source_committed_cursor": coverage.source_committed_cursor,
        "source_committed_chain_digest": coverage.source_committed_chain_digest,
        "active": True,
    }


def _settle_tail_read(
    session: Session,
    *,
    connector: Connector,
    coverage: ReconciliationCoverage,
    payload,
    prior: TerminalRecordManifest,
    source,
) -> bool:
    """Replace one unsettled tail row with a settled replay read of its ordinal."""
    from zk_add import reconciliation as rec
    from zk_add.service import ingest_attendance

    if source.disposition == "RAW_PRESERVED":
        return False
    identity_missing = rec._source_identity_missing(source)
    attendance = []
    if source.event is not None and not identity_missing:
        try:
            attendance, owned_elsewhere = rec._bound_source_attendance(
                session, connector=connector, terminal_serial=coverage.terminal_serial,
                records=[source], link_retained_elsewhere=False,
            )
        except rec.EvidenceRejected:
            # A conflicting retained punch is not settled here. The row stays
            # an open exception; the conflict surfaces on the next full read.
            return False
        if owned_elsewhere:
            return False
    if attendance:
        ingest_attendance(
            session, connector=connector, events=attendance, defer_synced_identity_release=True,
        )
    session.flush()
    event = session.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == attendance[0].event_uid)
    ) if attendance else None
    disposition = "IDENTITY_UNRESOLVED" if identity_missing else source.disposition
    if event is not None:
        earlier_copy = session.scalar(
            select(TerminalRecordManifest.id).where(
                TerminalRecordManifest.zkt_device_id == prior.zkt_device_id,
                TerminalRecordManifest.generation == prior.generation,
                _epoch_clause(TerminalRecordManifest.source_epoch_id, prior.source_epoch_id),
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal < source.ordinal,
                TerminalRecordManifest.terminal_record_key == source.terminal_record_key,
            ).limit(1)
        )
        if earlier_copy is not None:
            disposition = "TERMINAL_DUPLICATE"
        elif event.ords_status == "BLOCKED_IDENTITY":
            disposition = "BLOCKED_IDENTITY"
        elif disposition == "BLOCKED_IDENTITY" and event.cnic_lookup_hash:
            disposition = "EVENT"
    retire_unsettled_row(session, prior=prior, source=source, origin="SOURCE_TAIL_REPLAY")
    session.add(TerminalRecordManifest(
        job_id=None,
        chunk_id=None,
        connector_id=connector.id,
        zkt_device_id=prior.zkt_device_id,
        terminal_serial=coverage.terminal_serial,
        generation=prior.generation,
        source_epoch_id=prior.source_epoch_id,
        ordinal=source.ordinal,
        source_kind="TAIL",
        canonical_source=True,
        record_size=payload.record_size,
        raw_record_digest=source.raw_record_digest,
        terminal_record_key=source.terminal_record_key,
        occurrence_index=source.occurrence_index,
        attendance_event_id=event.id if event else None,
        disposition=disposition,
        protected_raw_record=encrypt_text(source.raw_record_b64),
        error_code=rec.SOURCE_MISSING_REFERENCE if identity_missing else source.error_code,
        raw_timestamp=source.raw_timestamp,
        observed_uid=source.observed_uid,
        observed_user_id=None if identity_missing else source.observed_user_id,
        **rec._source_claim_evidence(source, identity_missing=identity_missing),
    ))
    session.flush()
    rec._release_source_synced_attendance(
        session, connector=connector, events=[event] if event is not None else [],
    )
    return True


def answer_tail_replay(
    session: Session,
    *,
    connector: Connector,
    coverage: ReconciliationCoverage,
    existing: SourceTailChunk,
    payload,
) -> str | None:
    """Judge a tail that restarts at a committed chunk with different content.

    A stale coverage push (for example one built before a reconnect and
    delivered after a later commit) rewinds a reader's cursor. Its next tail
    re-reads a committed range, usually ending further on, and its formerly
    newest slot now holds the real punch. That is not a source mutation. It
    is benign when every committed canonical row in the overlap matches the
    replay, except rows that were themselves unsettled newest-slot reads.
    Those are settled from the replay when the replay read them while a later
    record existed. ADD's committed cursor and chain never move here.

    Returns ``None`` when benign, otherwise the code for invalidating coverage.
    """
    from zk_add import reconciliation as rec

    start, end = payload.start_ordinal, payload.end_ordinal
    committed_cursor = coverage.source_committed_cursor
    if not start < committed_cursor:
        return "SOURCE_TAIL_REPLAY_DIVERGED"
    if payload.latest_terminal_count < committed_cursor:
        return "SOURCE_TAIL_COUNT_REGRESSION"
    for source in payload.records:
        try:
            raw = base64.b64decode(source.raw_record_b64, validate=True)
        except (binascii.Error, ValueError):
            return "SOURCE_TAIL_EVIDENCE_INVALID"
        if len(raw) != payload.record_size or hashlib.sha256(raw).hexdigest() != source.raw_record_digest:
            return "SOURCE_TAIL_EVIDENCE_INVALID"
    if (rec.reconciliation_chunk_digest(payload) != payload.chunk_digest
            or payload.resulting_chain_digest != rec.reconciliation_chain_digest(
                payload.previous_chain_digest, start_ordinal=start, end_ordinal=end,
                chunk_digest=payload.chunk_digest)):
        return "SOURCE_TAIL_REPLAY_DIVERGED"
    if existing.previous_chain_digest != payload.previous_chain_digest:
        return "SOURCE_TAIL_REPLAY_DIVERGED"
    stop = min(end, committed_cursor)
    committed = {
        row.ordinal: row for row in session.scalars(
            select(TerminalRecordManifest).where(
                TerminalRecordManifest.zkt_device_id == coverage.zkt_device_id,
                TerminalRecordManifest.generation == payload.terminal_generation,
                _epoch_clause(TerminalRecordManifest.source_epoch_id, coverage.source_epoch_id),
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal >= start,
                TerminalRecordManifest.ordinal < stop,
            )
        ).all()
    }
    if len(committed) != stop - start:
        return "SOURCE_TAIL_REPLAY_DIVERGED"
    unsettled = unsettled_newest_ids(session, committed.values())
    settle = []
    for source in payload.records:
        if source.ordinal >= stop:
            break
        prior = committed[source.ordinal]
        if same_source_read(prior, source):
            continue
        if prior.id not in unsettled or prior.record_size != payload.record_size:
            return "SOURCE_TAIL_REPLAY_DIVERGED"
        if not read_was_newest(source.ordinal, payload.latest_terminal_count):
            settle.append((prior, source))
    for prior, source in settle:
        _settle_tail_read(
            session, connector=connector, coverage=coverage, payload=payload,
            prior=prior, source=source,
        )
    return None
