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

So a read of the newest slot says nothing about the punch the slot will hold.
ADD keeps such a row fail-closed. Only a complete source reconcile in the same
terminal generation and source epoch, reading the ordinal while later records
existed, changes it, and only through receipts that reconcile commits:

* different bytes settle the slot: the fresh read becomes the canonical row
  through ordinary ingestion (event-UID de-duplication against LIVE rows and
  every identity rule still apply) and the unsettled read stays as
  non-canonical evidence;
* identical bytes confirm the row as a genuine record and are kept as a
  non-canonical re-read receipt;
* a re-read at the reconcile's own final ordinal is again a possible
  newest-slot read, and is kept only as a non-canonical re-read receipt.

A row that already carries a source time correction is never settled: the
correction derived a punch from bytes that were not the slot's punch, so the
reconcile stops and raises an alert for HR to verify that punch. A tail
replayed after a cursor rewind never changes a row; it is only judged.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import encrypt_text
from zk_add.models import (
    AttendanceEvent,
    AttendanceSourceCorrection,
    Connector,
    ReconciliationCoverage,
    ReconciliationEvent,
    ReconciliationJob,
    SourceTailChunk,
    TerminalRecordManifest,
    TerminalRecordReview,
)


# Operator-facing conditions. Stored error codes stay exactly as the reader sent them.
UNSETTLED_CONDITION = "TERMINAL_NEWEST_RECORD_UNSETTLED"
CORRECTED_CONDITION = "TERMINAL_NEWEST_RECORD_CORRECTED"
REPLAY_SUPERSEDED_CODE = "SOURCE_TAIL_REPLAY_SUPERSEDED"
# Reconciliation job events.
SETTLED_STATE = "SOURCE_NEWEST_RECORD_SETTLED"
CONFIRMED_STATE = "SOURCE_NEWEST_RECORD_CONFIRMED"
REREAD_STATE = "SOURCE_NEWEST_RECORD_REREAD"
# Reconciliation safety holds and the device alert for a corrected row.
CORRECTED_HOLD = "SOURCE_NEWEST_RECORD_CORRECTED"
OPEN_JOB_HOLD = "SOURCE_NEWEST_RECORD_JOB_OPEN"
CORRECTED_ALERT = "TERMINAL_NEWEST_RECORD_CORRECTED"
# A non-canonical receipt of a reconcile's re-read of an unsettled row.
REREAD_SOURCE_KIND = "BASELINE_REREAD"
EXCEPTION_DISPOSITIONS = frozenset({"INVALID_TIME", "MALFORMED", "IDENTITY_UNRESOLVED"})
# Rows that delivered no attendance of their own: a fail-closed exception, or
# a byte copy of an earlier record that only links that record's event. Raw
# custody rows are never settled: their interpretation belongs to custody.
SETTLEABLE_DISPOSITIONS = EXCEPTION_DISPOSITIONS | {"TERMINAL_DUPLICATE"}
OPERATOR_REASON = (
    "The terminal reported this ordinal as its newest record before it had written the "
    "record. These bytes are not the punch in this slot: nothing was sent to Oracle and they "
    "cannot be corrected into a punch. The next punch made on the terminal fills this slot; "
    "LIVE capture normally delivers it. A later complete source reconcile in the same source "
    "epoch re-reads the slot and replaces this row. A reconcile job held only because its own "
    "final ordinal is such a row is not cleared by that: review the row if it is an invalid "
    "time or malformed record; an unresolved identity stays held until the owner decides."
)
CORRECTED_REASON = (
    "A source time correction was created from this row before ADD recognised it as the "
    "terminal's unwritten newest record. Its bytes are not the punch in this slot, so the "
    "corrected punch may belong to someone else. HR must verify that punch. ADD will not "
    "settle this slot automatically; a reconcile that reads different bytes here stops."
)


def read_was_newest(ordinal: int, latest_terminal_count: int | None) -> bool:
    """Whether ``ordinal`` was the terminal's newest slot when it was read."""
    return latest_terminal_count is not None and ordinal + 1 == latest_terminal_count


def baseline_newest_ordinal(job: ReconciliationJob) -> int | None:
    """The final ordinal of a baseline, which may have been read as the newest slot.

    Every Zone Lite generation anchors a baseline at ``cutoff_count`` equal
    to the terminal's latest count. The chunk carrying ``cutoff_count - 1``
    is read later, from a freshly prepared buffer, and no chunk reports the
    count at read time. ADD therefore cannot tell whether a later record
    existed when that ordinal was read, and assumes it may not have. The
    prepared buffer's size proves nothing here: firmware accepts a buffer
    larger than its declared records.
    """
    return job.cutoff_count - 1 if job.cutoff_count else None


def _epoch_clause(column, value):
    return column.is_(None) if value is None else column == value


def _read_key(row: TerminalRecordManifest) -> tuple:
    return (row.zkt_device_id, row.generation, row.source_epoch_id, row.ordinal, row.raw_record_digest)


def _stored_reads(session: Session, rows) -> dict[int, TerminalRecordManifest]:
    """Each row's own read; a recovery-epoch copy is judged by the read it copies."""
    reads = {row.id: row for row in rows}
    copies = [row for row in rows if row.source_kind == "RECOVERY_PREFIX"]
    if not copies:
        return reads
    originals: dict[tuple, TerminalRecordManifest] = {}
    for original in session.scalars(
        select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id.in_({row.zkt_device_id for row in copies}),
            TerminalRecordManifest.raw_record_digest.in_({row.raw_record_digest for row in copies}),
            TerminalRecordManifest.source_kind.in_(("TAIL", "BASELINE")),
        ).order_by(TerminalRecordManifest.id.asc())
    ).all():
        key = (original.zkt_device_id, original.generation, original.ordinal, original.raw_record_digest)
        originals.setdefault(key, original)
    for row in copies:
        original = originals.get((row.zkt_device_id, row.generation, row.ordinal, row.raw_record_digest))
        if original is None:
            reads.pop(row.id)
        else:
            reads[row.id] = original
    return reads


def _confirmed_reads(session: Session, reads) -> set[tuple]:
    """Read keys a later reconcile re-read with identical bytes away from its final ordinal."""
    reads = list(reads)
    if not reads:
        return set()
    confirmed = set()
    for receipt, cutoff in session.execute(
        select(TerminalRecordManifest, ReconciliationJob.cutoff_count)
        .join(ReconciliationJob, ReconciliationJob.id == TerminalRecordManifest.job_id)
        .where(
            TerminalRecordManifest.source_kind == REREAD_SOURCE_KIND,
            TerminalRecordManifest.canonical_source == False,  # noqa: E712
            TerminalRecordManifest.zkt_device_id.in_({row.zkt_device_id for row in reads}),
            TerminalRecordManifest.raw_record_digest.in_({row.raw_record_digest for row in reads}),
        )
    ).all():
        if cutoff is not None and receipt.ordinal + 1 != cutoff:
            confirmed.add(_read_key(receipt))
    return confirmed


def newest_read_ids(session: Session, rows) -> set[int]:
    """IDs of stored rows whose bytes may have come from the terminal's newest slot.

    The committed receipt that read a row says so: a tail chunk that ended at
    its own ``latest_terminal_count``, or a baseline's final ordinal. Rows
    stored before this rule existed are therefore recognised the same way as
    new ones. A recovery-epoch copy is judged by the read it copies. A row a
    later reconcile re-read with identical bytes while later records existed
    is a confirmed genuine record and is not included.

    Reconciliation only settles canonical tail rows into baseline rows away
    from a job's final ordinal, so a settled row is never included either.
    """
    rows = list(rows)
    by_id = {row.id: row for row in rows}
    reads = _stored_reads(session, rows)
    found: dict[int, TerminalRecordManifest] = {}
    tail = [(row_id, row) for row_id, row in reads.items() if row.source_kind == "TAIL"]
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
        found.update(
            (row_id, row) for row_id, row in tail
            if (row.zkt_device_id, row.generation, row.ordinal + 1, row.source_epoch_id) in receipts
        )
    baseline = [(row_id, row) for row_id, row in reads.items()
                if row.source_kind == "BASELINE" and row.job_id is not None]
    if baseline:
        jobs = {
            job.id: job for job in session.scalars(
                select(ReconciliationJob).where(
                    ReconciliationJob.id.in_({row.job_id for _, row in baseline})
                )
            ).all()
        }
        found.update(
            (row_id, row) for row_id, row in baseline
            if (job := jobs.get(row.job_id)) is not None
            and baseline_newest_ordinal(job) == row.ordinal
        )
    # A copy is confirmed by a re-read of itself or of the read it copies.
    confirmed = _confirmed_reads(session, [*found.values(), *(by_id[row_id] for row_id in found)])
    return {
        row_id for row_id, read in found.items()
        if _read_key(read) not in confirmed and _read_key(by_id[row_id]) not in confirmed
    }


def unsettled_newest_ids(session: Session, rows) -> set[int]:
    """Newest-slot rows that produced no attendance of their own, so a re-read may settle them."""
    return newest_read_ids(
        session, [row for row in rows if row.disposition in SETTLEABLE_DISPOSITIONS]
    )


def is_unsettled_newest(session: Session, row: TerminalRecordManifest) -> bool:
    return row.id in unsettled_newest_ids(session, [row])


def created_corrections(session: Session, manifest_ids) -> dict[int, list[str]]:
    """Live source time corrections, by the manifest they were derived from."""
    ids = list(manifest_ids)
    found: dict[int, list[str]] = {}
    if not ids:
        return found
    for manifest_id, correction_id in session.execute(
        select(AttendanceSourceCorrection.manifest_id, AttendanceSourceCorrection.correction_id)
        .where(
            AttendanceSourceCorrection.manifest_id.in_(ids),
            AttendanceSourceCorrection.status == "CREATED",
        )
        .order_by(AttendanceSourceCorrection.id.asc())
    ).all():
        found.setdefault(manifest_id, []).append(correction_id)
    return found


def same_source_read(prior: TerminalRecordManifest, source) -> bool:
    return (
        prior.raw_record_digest == source.raw_record_digest
        and prior.terminal_record_key == source.terminal_record_key
        and prior.occurrence_index == source.occurrence_index
    )


def settlement_block(
    session: Session, *, job: ReconciliationJob, priors: list[TerminalRecordManifest],
) -> tuple[str, str, dict] | None:
    """Why a reconcile chunk must not settle these unsettled rows, if it must not.

    A live source time correction derived a punch from bytes that were not
    the slot's punch; settling would leave that punch live while its row left
    every canonical scope. Another open, capture-certified job in the same
    epoch sealed these rows into its exception cohort; settling would break
    that seal. Either way the chunk is not committed.
    """
    from zk_add.reconciliation import TERMINAL_JOB_STATES

    corrected = created_corrections(session, [prior.id for prior in priors])
    if corrected:
        rows = sorted((prior for prior in priors if prior.id in corrected), key=lambda row: row.ordinal)
        return CORRECTED_HOLD, (
            "A re-read changed a terminal newest-record row that already has a source time "
            "correction. The corrected punch was derived from bytes that were not this slot's "
            "punch; HR must verify it before this reconcile can continue."
        ), {
            "ordinals": [row.ordinal for row in rows],
            "manifest_ids": [row.id for row in rows],
            "correction_ids": [cid for row in rows for cid in corrected[row.id]],
        }
    lowest = min(prior.ordinal for prior in priors)
    sealed = session.scalar(
        select(ReconciliationJob.job_id).where(
            ReconciliationJob.zkt_device_id == job.zkt_device_id,
            ReconciliationJob.terminal_generation == job.terminal_generation,
            _epoch_clause(ReconciliationJob.source_epoch_id, job.source_epoch_id),
            ReconciliationJob.id != job.id,
            ReconciliationJob.capture_certified_at.is_not(None),
            ReconciliationJob.status.not_in(TERMINAL_JOB_STATES),
            ReconciliationJob.cutoff_count > lowest,
        ).order_by(ReconciliationJob.id.asc()).limit(1)
    )
    if sealed is not None:
        return OPEN_JOB_HOLD, (
            "Another open reconciliation sealed these terminal newest-record rows into its "
            "certified exception cohort. Finish or cancel it, then retry this reconcile."
        ), {"ordinals": sorted(prior.ordinal for prior in priors), "open_job_id": sealed}
    return None


def retire(session: Session, prior: TerminalRecordManifest) -> None:
    """Keep the unsettled read as evidence, but no longer as the slot's source.

    The caller inserts the settled read as the canonical row in the same
    transaction. The unique canonical index requires this flush first.
    """
    prior.canonical_source = False
    session.flush()


def record_settlement(
    session: Session,
    *,
    job: ReconciliationJob,
    chunk_id: int,
    prior: TerminalRecordManifest,
    settled: TerminalRecordManifest,
) -> dict:
    """The audit trail of one settled slot, written after the settled row is stored."""
    reviews = session.scalars(
        select(TerminalRecordReview.review_id)
        .where(TerminalRecordReview.manifest_id == prior.id)
        .order_by(TerminalRecordReview.id.asc())
    ).all()
    details = {
        "job_id": job.job_id,
        "chunk_id": chunk_id,
        "ordinal": prior.ordinal,
        "terminal_generation": prior.generation,
        "superseded_manifest_id": prior.id,
        "superseded_source_kind": prior.source_kind,
        "superseded_disposition": prior.disposition,
        "superseded_raw_record_digest": prior.raw_record_digest,
        # A review recorded the excluded bytes; it stays with them.
        "superseded_review_ids": list(reviews),
        "settled_manifest_id": settled.id,
        "settled_raw_record_digest": settled.raw_record_digest,
        "settled_disposition": settled.disposition,
        "settled_attendance_event_id": settled.attendance_event_id,
    }
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


def reread_receipt(
    *, job: ReconciliationJob, chunk_id: int, connector: Connector, source,
) -> TerminalRecordManifest:
    """A reconcile's re-read of an unsettled row, kept with its receipt but not as the source.

    Nothing is ingested from it: the canonical row keeps the slot.
    """
    from zk_add import reconciliation as rec

    identity_missing = rec._source_identity_missing(source)
    return TerminalRecordManifest(
        job_id=job.id,
        chunk_id=chunk_id,
        connector_id=connector.id,
        zkt_device_id=job.zkt_device_id,
        terminal_serial=job.terminal_serial or "unknown",
        generation=job.terminal_generation,
        source_epoch_id=job.source_epoch_id,
        ordinal=source.ordinal,
        source_kind=REREAD_SOURCE_KIND,
        canonical_source=False,
        record_size=job.record_size,
        raw_record_digest=source.raw_record_digest,
        terminal_record_key=source.terminal_record_key,
        occurrence_index=source.occurrence_index,
        attendance_event_id=None,
        disposition="IDENTITY_UNRESOLVED" if identity_missing else source.disposition,
        protected_raw_record=encrypt_text(source.raw_record_b64),
        error_code=rec.SOURCE_MISSING_REFERENCE if identity_missing else source.error_code,
        raw_timestamp=source.raw_timestamp,
        observed_uid=source.observed_uid,
        observed_user_id=None if identity_missing else source.observed_user_id,
        **rec._source_claim_evidence(source, identity_missing=identity_missing),
    )


def _replay_attendance_conflict(
    session: Session, *, connector: Connector, coverage: ReconciliationCoverage, records,
) -> str | None:
    """The code the ordinary tail path would refuse these replayed punches with, read-only."""
    from zk_add import reconciliation as rec

    zkt = connector.zkt_device
    incoming = {}
    for source in records:
        event = source.event
        if event is None or rec._source_identity_missing(source):
            continue
        if event.terminal_serial not in {None, coverage.terminal_serial}:
            return "SOURCE_EVENT_TERMINAL_MISMATCH"
        if event.source not in {"FULL_HISTORY", "CURRENT_RECONCILE"}:
            return "SOURCE_EVENT_CAPTURE_TYPE_INVALID"
        incoming[event.event_uid] = event
    if not incoming:
        return None
    for row in session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(list(incoming)))
    ).all():
        if zkt is None or row.connector_id != connector.id or row.zkt_device_id != zkt.id:
            # Tail ordinals follow the certified cutoff: another owner means a second reader.
            return "SOURCE_TAIL_FOREIGN_OWNER"
        conflict = rec._replay_conflict(
            row, incoming[row.event_uid], terminal_serial=coverage.terminal_serial, owned=True,
        )
        if conflict is not None:
            return conflict
    return None


def _record_superseded_replay(
    session: Session, *, connector: Connector, coverage: ReconciliationCoverage, payload, differing,
) -> None:
    """A durable, idempotent record of a replay answered with ADD's unchanged coverage."""
    from zk_add.reconciliation import _event
    from zk_add.service import upsert_alert

    details = {
        "coverage_id": coverage.coverage_id,
        "start_ordinal": payload.start_ordinal,
        "end_ordinal": payload.end_ordinal,
        "latest_terminal_count": payload.latest_terminal_count,
        "chunk_digest": payload.chunk_digest,
        "previous_chain_digest": payload.previous_chain_digest,
        "resulting_chain_digest": payload.resulting_chain_digest,
        "source_committed_cursor": coverage.source_committed_cursor,
        "source_committed_chain_digest": coverage.source_committed_chain_digest,
        "unsettled_rows": [
            {
                "ordinal": prior.ordinal,
                "manifest_id": prior.id,
                "committed_raw_record_digest": prior.raw_record_digest,
                "replayed_raw_record_digest": source.raw_record_digest,
                "replay_read_newest": read_was_newest(source.ordinal, payload.latest_terminal_count),
            }
            for prior, source in differing
        ],
    }
    key = "tail-replay-superseded:" + hashlib.sha256(json.dumps(
        [coverage.id, payload.start_ordinal, payload.end_ordinal, payload.chunk_digest,
         payload.resulting_chain_digest], separators=(",", ":")).encode()).hexdigest()
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is not None:
        if session.scalar(select(ReconciliationEvent.id).where(
            ReconciliationEvent.job_id == job.id, ReconciliationEvent.idempotency_key == key,
        )) is not None:
            return
        _event(session, job, REPLAY_SUPERSEDED_CODE, details, idempotency_key=key)
    append_audit(
        session,
        actor="system:source-reconciliation",
        action=REPLAY_SUPERSEDED_CODE,
        target_type="reconciliation_coverage",
        target_id=coverage.coverage_id,
        outcome="SUCCESS",
        after=details,
        request_id=key,
    )
    # A replay that read the slot while a later record existed proves the
    # slot holds other bytes than the ones a correction was derived from.
    corrected = created_corrections(session, [
        prior.id for prior, source in differing
        if not read_was_newest(source.ordinal, payload.latest_terminal_count)
    ])
    if corrected:
        upsert_alert(
            session,
            connector,
            code="TERMINAL_NEWEST_RECORD_CORRECTED",
            severity="CRITICAL",
            message=(
                "A source time correction was made from a terminal newest-record row that a "
                "later read showed was not the slot's punch. HR must verify the corrected punch."
            ),
            details={
                "failure_category": CORRECTED_HOLD,
                "manifest_ids": sorted(corrected),
                "correction_ids": [cid for ids in corrected.values() for cid in ids],
            },
        )


def judge_tail_replay(
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
    replay except rows that are themselves unsettled newest-slot reads, and
    the replay makes no claim the ordinary tail path would refuse.

    The replay is evidence of nothing ADD stores: no row, cursor or chain
    changes here. A complete reconcile settles such rows through its own
    receipts. Returns ``None`` when benign, otherwise the invalidation code.
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
    differing = []
    for source in payload.records:
        if source.ordinal >= stop:
            break
        prior = committed[source.ordinal]
        if same_source_read(prior, source):
            continue
        if prior.id not in unsettled or prior.record_size != payload.record_size:
            return "SOURCE_TAIL_REPLAY_DIVERGED"
        differing.append((prior, source))
    code = _replay_attendance_conflict(
        session, connector=connector, coverage=coverage, records=[source for _, source in differing],
    )
    if code is not None:
        return code
    _record_superseded_replay(
        session, connector=connector, coverage=coverage, payload=payload, differing=differing,
    )
    return None
