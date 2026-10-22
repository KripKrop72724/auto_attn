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

So a read of the newest slot says nothing about the punch the slot will hold,
and identity bytes read there may belong to someone else. A tail chunk reports
the count it read against, so its newest read is observed, not assumed. ADD
keeps such a read fail-closed:

* A tail's newest read that decodes as a punch no capture has delivered is
  held, not ingested: its row is ``IDENTITY_UNRESOLVED`` with the code
  ``TERMINAL_NEWEST_RECORD_UNCONFIRMED`` and its claim stays encrypted. LIVE
  capture delivering the same punch, or a later read of the same bytes while
  later records exist, confirms it.
* An attendance event whose only source evidence is an unconfirmed tail newest
  read is never released automatically, by force release or by direct ORDS.

A baseline's final ordinal is only assumed to be the newest slot: no chunk
reports the count it was read against. It is labelled and settled like a
newest read, but a punch read there is ingested as before.

Only a complete source reconcile in the same terminal generation and source
epoch, reading the ordinal while later records existed, changes such a row,
and only through receipts that reconcile commits:

* different bytes settle the slot: the fresh read becomes the canonical row
  through ordinary ingestion (event-UID de-duplication against LIVE rows and
  every identity rule still apply) and the unsettled read stays as
  non-canonical evidence. A read that delivered a punch is settled this way
  only when the same job read its bytes at an earlier ordinal, which proves it
  was a byte copy of that record; otherwise the reconcile stops for review;
* identical bytes confirm the row as a genuine record and are kept as a
  non-canonical re-read receipt; a held punch is then settled and ingested;
* a re-read at the reconcile's own final ordinal is again a possible
  newest-slot read, and is kept only as a non-canonical re-read receipt.

A row that already carries a source time correction is never settled: the
correction derived a punch from bytes that were not the slot's punch, so the
reconcile stops and raises an alert for HR to verify that punch. A tail
replayed after a cursor rewind never changes a canonical row; its reads are
kept as receipts and a punch ADD has not received raises an alert.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import defaultdict
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_text, encrypt_text
from zk_add.models import (
    AttendanceEvent,
    AttendanceSourceCorrection,
    AuditEvent,
    Connector,
    ReconciliationCoverage,
    ReconciliationEvent,
    ReconciliationJob,
    SourceTailChunk,
    TerminalRecordManifest,
    TerminalRecordReview,
)
from zk_add.time_utils import ensure_utc


# Operator-facing conditions. Stored reader codes stay exactly as the reader sent them.
UNSETTLED_CONDITION = "TERMINAL_NEWEST_RECORD_UNSETTLED"
UNCONFIRMED_CONDITION = "TERMINAL_NEWEST_RECORD_UNCONFIRMED"
CORRECTED_CONDITION = "TERMINAL_NEWEST_RECORD_CORRECTED"
# ADD's own code on a held newest-slot punch (the reader's claim stays encrypted).
HELD_CODE = UNCONFIRMED_CONDITION
REPLAY_SUPERSEDED_CODE = "SOURCE_TAIL_REPLAY_SUPERSEDED"
# Reconciliation job events.
SETTLED_STATE = "SOURCE_NEWEST_RECORD_SETTLED"
CONFIRMED_STATE = "SOURCE_NEWEST_RECORD_CONFIRMED"
REREAD_STATE = "SOURCE_NEWEST_RECORD_REREAD"
PROBE_REQUEUED_STATE = "SOURCE_DIVERGENCE_NEWEST_RECORD"
# Reconciliation safety holds, the source-exception assurance state and alerts.
CORRECTED_HOLD = "SOURCE_NEWEST_RECORD_CORRECTED"
OPEN_JOB_HOLD = "SOURCE_NEWEST_RECORD_JOB_OPEN"
CHANGED_HOLD = "SOURCE_NEWEST_RECORD_CHANGED"
PUNCH_PENDING_HOLD = "SOURCE_NEWEST_PUNCH_UNRECEIVED"
PUNCH_PENDING_STATE = "NEWEST_PUNCH_UNRECEIVED"
CORRECTED_ALERT = "TERMINAL_NEWEST_RECORD_CORRECTED"
CHANGED_ALERT = "TERMINAL_NEWEST_RECORD_CHANGED"
UNRECEIVED_ALERT = "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED"
PROBE_SUPERSEDED_STATE = "SUPERSEDED_BY_NEWEST_RECORD_RULE"
# Non-canonical receipts: a reconcile's re-read of a newest-slot row, and a
# verified tail replay's read of a committed newest-slot row.
REREAD_SOURCE_KIND = "BASELINE_REREAD"
REPLAY_SOURCE_KIND = "TAIL_REPLAY"
RECEIPT_SOURCE_KINDS = (REREAD_SOURCE_KIND, REPLAY_SOURCE_KIND)
# The interpretation a newest-slot claim was stored under.
GUARD_VERSION = "zkt-newest-slot-guard-1"
EXCEPTION_DISPOSITIONS = frozenset({"INVALID_TIME", "MALFORMED", "IDENTITY_UNRESOLVED"})
# Rows that delivered no attendance of their own: a fail-closed exception
# (including a held punch), or a byte copy of an earlier record that only links
# that record's event. Raw custody rows are never settled: their interpretation
# belongs to custody.
SETTLEABLE_DISPOSITIONS = EXCEPTION_DISPOSITIONS | {"TERMINAL_DUPLICATE"}
LIVE_SOURCES = frozenset({"LIVE", "LIVE_POLL"})
ALERT_ORDINAL_LIMIT = 50
OPERATOR_REASON = (
    "The terminal reported this ordinal as its newest record before it had written the "
    "record. These bytes are not the punch in this slot: nothing was sent to Oracle and they "
    "cannot be corrected into a punch. The slot is filled by the next punch made on the "
    "terminal, which LIVE capture normally delivers. ADD reads that punch from this slot only "
    "in a later complete source reconcile in the same source epoch, which replaces this row. "
    "A review records that the row was seen; it does not settle the slot or read its punch, "
    "and the row keeps this label until a reconcile does. A reconcile job whose own final "
    "ordinal is such a row cannot re-read it while it is open: an unresolved identity there "
    "stays held until the owner decides, and cancelling the job is the operational exit."
)
UNCONFIRMED_REASON = (
    "The terminal returned these bytes from its newest slot, which it may not yet have "
    "written. They decode as a punch that no capture has delivered to ADD, so ADD holds them "
    "instead of sending a punch that may belong to someone else or never happened. Nothing "
    "was sent to Oracle and they cannot be corrected. LIVE capture delivering the same punch, "
    "or a later complete source reconcile that reads the same bytes while later records "
    "exist, confirms the punch; a reconcile that reads other bytes replaces this row."
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


def is_held(row: TerminalRecordManifest) -> bool:
    """A newest-slot read held because it decodes as a punch no capture delivered."""
    return row.disposition == "IDENTITY_UNRESOLVED" and row.error_code == HELD_CODE


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


def newest_read_ids(session: Session, rows, *, observed_only: bool = False) -> set[int]:
    """IDs of stored rows whose bytes may have come from the terminal's newest slot.

    The committed receipt that read a row says so: a tail chunk that ended at
    its own ``latest_terminal_count`` (observed), or a baseline's final
    ordinal (assumed; skipped with ``observed_only``). Rows stored before this
    rule existed are therefore recognised the same way as new ones. A
    recovery-epoch copy is judged by the read it copies. A row a later
    reconcile re-read with identical bytes while later records existed is a
    confirmed genuine record and is not included.

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
    baseline = [] if observed_only else [
        (row_id, row) for row_id, row in reads.items()
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


def newest_only_event_ids(session: Session, events) -> set[int]:
    """Attendance events whose only source evidence is an unconfirmed tail newest read.

    A LIVE capture is independent evidence of its own punch. Any other event
    linked only from canonical rows that a tail read as the terminal's newest
    slot is not proof of a punch, and may carry someone else's identity bytes.
    A baseline's final ordinal is only assumed to be that slot and keeps the
    ordinary release rules.
    """
    ids = {event.id for event in events
           if event is not None and event.id is not None and event.source not in LIVE_SOURCES}
    if not ids:
        return set()
    rows = session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.attendance_event_id.in_(ids),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
    )).all()
    if not rows:
        return set()
    newest = newest_read_ids(
        session, [row for row in rows if row.disposition != "RAW_PRESERVED"], observed_only=True)
    linked: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        linked[row.attendance_event_id].append(row.id)
    return {event_id for event_id, row_ids in linked.items() if all(row_id in newest for row_id in row_ids)}


def claim_evidence(source, *, reason: str) -> dict:
    """Manifest fields that keep a newest-slot read's own interpretation, encrypted.

    The same claim shape as the identity guard's, so the audited evidence
    reveal verifies and shows it.
    """
    claim = {
        "schema_version": 1,
        "interpretation_version": GUARD_VERSION,
        "reason": reason,
        "record": source.model_dump(mode="json", exclude={"raw_record_b64"}),
    }
    return {
        "declared_disposition": source.disposition,
        "interpretation_version": GUARD_VERSION,
        "protected_source_claim": encrypt_text(json.dumps(claim, sort_keys=True, separators=(",", ":"))),
    }


def claimed_punch(row: TerminalRecordManifest) -> tuple[bool, dict | None]:
    """``(readable, punch)`` for a row's newest-slot claim; ``punch`` is None when it holds none."""
    if row.interpretation_version != GUARD_VERSION or not row.protected_source_claim:
        return True, None
    try:
        claim = json.loads(decrypt_text(row.protected_source_claim))
        record = claim["record"]
        if (claim.get("schema_version") != 1 or claim.get("interpretation_version") != GUARD_VERSION
                or record["raw_record_digest"] != row.raw_record_digest or record["ordinal"] != row.ordinal):
            return False, None
        event = record.get("event")
        if not event:
            return True, None
        if not event.get("event_uid") or not event.get("user_id") or not event.get("device_event_time"):
            return False, None
        return True, event
    except Exception:  # noqa: BLE001 - an unreadable claim proves nothing and stays fail-closed.
        return False, None


def source_punch(source) -> dict | None:
    """The punch a verified source record decodes to, or None when it names no user."""
    from zk_add import reconciliation as rec

    if source.event is None or rec._source_identity_missing(source):
        return None
    return source.event.model_dump(mode="json")


def received_events(session: Session, *, connector_id: int, zkt_device_id: int, punches: dict) -> dict:
    """For each claimed punch, the event of this terminal binding that already holds it, or None.

    Zone Lite derives an event UID from the terminal serial, user ID, device
    time and punch, so an owned event with the same UID and user is the same
    punch delivered by an independent capture.
    """
    uids = {punch["event_uid"] for punch in punches.values()}
    rows = {
        row.event_uid: row for row in session.scalars(
            select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(uids))
        ).all()
    } if uids else {}
    found = {}
    for key, punch in punches.items():
        row = rows.get(punch["event_uid"])
        same = False
        if (row is not None and row.connector_id == connector_id and row.zkt_device_id == zkt_device_id
                and row.user_id == punch.get("user_id")):
            try:
                same = ensure_utc(row.device_event_time) == ensure_utc(
                    datetime.fromisoformat(str(punch["device_event_time"]).replace("Z", "+00:00")))
            except (TypeError, ValueError):
                same = False
        found[key] = row.id if same else None
    return found


def held_newest_uids(session: Session, *, records, newest_ordinal: int | None) -> dict[int, str]:
    """A tail's newest reads that decode as a punch no capture delivered: ``{ordinal: event_uid}``.

    A punch already held by ADD (normally LIVE capture, or an earlier record
    of this read that the newest slot copies) is linked as before.
    """
    if newest_ordinal is None:
        return {}
    candidates = {
        source.ordinal: punch["event_uid"] for source in records
        if source.ordinal == newest_ordinal and source.disposition != "RAW_PRESERVED"
        and (punch := source_punch(source)) is not None
    }
    if not candidates:
        return {}
    others = {source.event.event_uid for source in records
              if source.event is not None and source.ordinal != newest_ordinal}
    known = set(session.scalars(select(AttendanceEvent.event_uid).where(
        AttendanceEvent.event_uid.in_(set(candidates.values()))
    )).all())
    return {ordinal: uid for ordinal, uid in candidates.items() if uid not in known and uid not in others}


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


def corrections_by_row(session: Session, rows) -> dict[int, list[str]]:
    """Live corrections of each row, including those made on the read a recovery copy copies."""
    rows = list(rows)
    if not rows:
        return {}
    reads = _stored_reads(session, rows)
    found = created_corrections(session, {row.id for row in rows} | {read.id for read in reads.values()})
    result = {}
    for row in rows:
        ids = list(found.get(row.id, []))
        read = reads.get(row.id)
        if read is not None and read.id != row.id:
            ids.extend(cid for cid in found.get(read.id, []) if cid not in ids)
        if ids:
            result[row.id] = ids
    return result


def same_source_read(prior: TerminalRecordManifest, source) -> bool:
    return (
        prior.raw_record_digest == source.raw_record_digest
        and prior.terminal_record_key == source.terminal_record_key
        and prior.occurrence_index == source.occurrence_index
    )


def copy_proven(session: Session, *, job: ReconciliationJob, prior: TerminalRecordManifest, records,
                start_ordinal: int) -> bool:
    """Whether this job read ``prior``'s bytes at an earlier ordinal, proving them a byte copy.

    Every canonical row below the chunk's start is this job's own read: each
    either matched it byte for byte or was settled by it.
    """
    if any(source.ordinal < prior.ordinal and source.raw_record_digest == prior.raw_record_digest
           for source in records):
        return True
    return session.scalar(select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
        TerminalRecordManifest.generation == job.terminal_generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, job.source_epoch_id),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.ordinal < min(start_ordinal, prior.ordinal),
        TerminalRecordManifest.raw_record_digest == prior.raw_record_digest,
    ).limit(1)) is not None


def settlement_block(
    session: Session, *, job: ReconciliationJob, priors: list[TerminalRecordManifest],
) -> tuple[str, str, dict] | None:
    """Why a reconcile chunk must not settle these rows, if it must not.

    A live source time correction derived a punch from bytes that were not
    the slot's punch; settling would leave that punch live while its row left
    every canonical scope. A recovery-epoch copy carries the corrections of
    the read it copies. Another open, capture-certified job in the same epoch
    sealed these rows into its exception cohort; settling would break that
    seal. Either way the chunk is not committed.
    """
    from zk_add.reconciliation import TERMINAL_JOB_STATES

    corrected = corrections_by_row(session, priors)
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


def changed_block(changed: list[tuple[TerminalRecordManifest, object]]) -> tuple[str, str, dict]:
    """The hold for newest-slot reads that delivered a punch and now read differently.

    Such a read was ingested (or linked) as attendance before ADD recognised
    the newest slot, and nothing proves it a byte copy of an earlier record.
    Neither a settlement nor a source-change recovery may decide whose punch
    it was.
    """
    rows = sorted(changed, key=lambda item: item[0].ordinal)
    return CHANGED_HOLD, (
        "A terminal newest-record row that delivered or held a punch read differently once later "
        "records existed, and this reconcile did not read its bytes at an earlier ordinal. The "
        "punch ADD took from it may not be this slot's punch; HR must verify it before this "
        "reconcile can continue."
    ), {
        "ordinals": [prior.ordinal for prior, _ in rows],
        "manifest_ids": [prior.id for prior, _ in rows],
        "dispositions": [prior.disposition for prior, _ in rows],
        "attendance_event_ids": [prior.attendance_event_id for prior, _ in rows],
        "committed_raw_record_digests": [prior.raw_record_digest for prior, _ in rows],
        "reread_raw_record_digests": [source.raw_record_digest for _, source in rows],
    }


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
        "superseded_error_code": prior.error_code,
        "superseded_raw_record_digest": prior.raw_record_digest,
        "superseded_attendance_event_id": prior.attendance_event_id,
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


def _receipt(
    *, kind: str, job_id: int | None, chunk_id: int | None, connector_id: int, zkt_device_id: int,
    terminal_serial: str, generation: int, source_epoch_id: int | None, record_size: int, source,
) -> TerminalRecordManifest:
    """A verified read kept with its receipt, never as the slot's source. Nothing is ingested."""
    from zk_add import reconciliation as rec

    identity_missing = rec._source_identity_missing(source)
    if identity_missing or source.event is None:
        claim = rec._source_claim_evidence(source, identity_missing=identity_missing)
    else:
        claim = claim_evidence(source, reason=kind)
    return TerminalRecordManifest(
        job_id=job_id,
        chunk_id=chunk_id,
        connector_id=connector_id,
        zkt_device_id=zkt_device_id,
        terminal_serial=terminal_serial,
        generation=generation,
        source_epoch_id=source_epoch_id,
        ordinal=source.ordinal,
        source_kind=kind,
        canonical_source=False,
        record_size=record_size,
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
        **claim,
    )


def reread_receipt(
    *, job: ReconciliationJob, chunk_id: int, connector: Connector, source,
) -> TerminalRecordManifest:
    """A reconcile's re-read of a newest-slot row, kept with its chunk receipt."""
    return _receipt(
        kind=REREAD_SOURCE_KIND, job_id=job.id, chunk_id=chunk_id, connector_id=connector.id,
        zkt_device_id=job.zkt_device_id, terminal_serial=job.terminal_serial or "unknown",
        generation=job.terminal_generation, source_epoch_id=job.source_epoch_id,
        record_size=job.record_size, source=source,
    )


def unreceived_ordinals(session: Session, *, connector: Connector, records) -> list[int]:
    """Ordinals of verified reads that decode as a punch this terminal binding has not received."""
    zkt = connector.zkt_device
    punches = {source.ordinal: punch for source in records if (punch := source_punch(source)) is not None}
    if not punches or zkt is None:
        return sorted(punches)
    found = received_events(session, connector_id=connector.id, zkt_device_id=zkt.id, punches=punches)
    return sorted(ordinal for ordinal, event_id in found.items() if event_id is None)


def alert_unreceived(session: Session, connector: Connector, *, cause: str, ordinals, details: dict) -> None:
    """Latch one alert per device naming every slot with a punch ADD has not received."""
    from zk_add.service import open_alert_row, upsert_alert

    previous = open_alert_row(session, connector, UNRECEIVED_ALERT)
    known = list((previous.details or {}).get("ordinals", [])) if previous is not None else []
    merged = sorted({*known, *ordinals})[-ALERT_ORDINAL_LIMIT:]
    upsert_alert(
        session,
        connector,
        code="TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED",
        severity="HIGH",
        message=(
            "The terminal's newest-record slot holds a punch ADD has not received. Run a complete "
            "source reconcile after the next punch on this terminal so ADD reads the slot while a "
            "later record exists; LIVE capture delivering the same punch also confirms it."
        ),
        details={
            "failure_category": UNRECEIVED_ALERT,
            "cause": cause,
            "ordinals": merged,
            **details,
            "inspector_path": (
                "/reconciliation?tab=source-exceptions&device_id=" f"{connector.connector_id}"
            ),
        },
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
    """A durable, idempotent record of a replay answered with ADD's unchanged coverage.

    A replayed read made while a later record existed is the slot's real
    content. It is kept as an encrypted non-canonical receipt, and a punch ADD
    has not received raises an alert. It is never ingested: only a complete
    reconcile's own receipt settles a slot.
    """
    from zk_add.reconciliation import _event
    from zk_add.service import upsert_alert

    key = "tail-replay-superseded:" + hashlib.sha256(json.dumps(
        [coverage.id, payload.start_ordinal, payload.end_ordinal, payload.chunk_digest,
         payload.resulting_chain_digest], separators=(",", ":")).encode()).hexdigest()
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is not None:
        if session.scalar(select(ReconciliationEvent.id).where(
            ReconciliationEvent.job_id == job.id, ReconciliationEvent.idempotency_key == key,
        )) is not None:
            return
    elif session.scalar(select(AuditEvent.id).where(
        AuditEvent.action == REPLAY_SUPERSEDED_CODE, AuditEvent.request_id == key,
    ).limit(1)) is not None:
        return
    # Only a read made while a later record existed is the slot's content.
    settled_reads = [source for _, source in differing
                     if not read_was_newest(source.ordinal, payload.latest_terminal_count)]
    receipts = {}
    for source in settled_reads:
        receipts[source.ordinal] = _receipt(
            kind=REPLAY_SOURCE_KIND, job_id=None, chunk_id=None, connector_id=connector.id,
            zkt_device_id=coverage.zkt_device_id, terminal_serial=coverage.terminal_serial,
            generation=payload.terminal_generation, source_epoch_id=coverage.source_epoch_id,
            record_size=payload.record_size, source=source,
        )
        session.add(receipts[source.ordinal])
    if receipts:
        session.flush()
    unreceived = unreceived_ordinals(session, connector=connector, records=settled_reads)
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
                "receipt_manifest_id": receipts[source.ordinal].id if source.ordinal in receipts else None,
                "punch_unreceived": source.ordinal in unreceived,
            }
            for prior, source in differing
        ],
    }
    if job is not None:
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
    if unreceived:
        alert_unreceived(session, connector, cause=REPLAY_SOURCE_KIND, ordinals=unreceived, details={
            "coverage_id": coverage.coverage_id,
            "receipt_manifest_ids": [receipts[ordinal].id for ordinal in unreceived],
        })
    # A replay that read the slot while a later record existed proves the
    # slot holds other bytes than the ones a correction was derived from.
    corrected = corrections_by_row(session, [
        prior for prior, source in differing
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

    The replay never changes a canonical row, cursor or chain. Its differing
    reads are kept as receipts (see ``_record_superseded_replay``); a
    complete reconcile settles such rows through its own receipts. Returns
    ``None`` when benign, otherwise the invalidation code.
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


def cohort_newest_evidence(
    session: Session, job: ReconciliationJob, manifests,
) -> tuple[set[int], set[int], set[int]]:
    """``(held, pending, received_event_ids)`` for a job's certified exception cohort.

    ``held`` rows are newest-slot reads ADD held as unconfirmed punches.
    ``pending`` rows are cohort rows for which ADD holds evidence of a punch
    it has not received: a held punch, this job's re-read of its own final
    ordinal, or a tail replay read while a later record existed. A review
    cannot exclude such a row. ``received_event_ids`` are the events that
    independently hold such a punch; they join the job's Oracle membership.
    """
    manifests = list(manifests)
    held = {row.id for row in manifests if is_held(row)}
    cutoff = job.cutoff_count or 0
    by_ordinal = {row.ordinal: row for row in manifests}
    punches: dict[tuple[int, int], dict] = {}
    unreadable: set[int] = set()

    def take(row_id: int, holder: TerminalRecordManifest) -> None:
        readable, punch = claimed_punch(holder)
        if not readable:
            unreadable.add(row_id)
        elif punch is not None:
            punches[(row_id, holder.id)] = punch

    for row in manifests:
        if row.id in held:
            take(row.id, row)
    if by_ordinal:
        for receipt in session.scalars(select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
            TerminalRecordManifest.generation == job.terminal_generation,
            _epoch_clause(TerminalRecordManifest.source_epoch_id, job.source_epoch_id),
            TerminalRecordManifest.canonical_source == False,  # noqa: E712
            TerminalRecordManifest.interpretation_version == GUARD_VERSION,
            TerminalRecordManifest.ordinal < cutoff,
            or_(
                and_(TerminalRecordManifest.source_kind == REREAD_SOURCE_KIND,
                     TerminalRecordManifest.job_id == job.id),
                TerminalRecordManifest.source_kind == REPLAY_SOURCE_KIND,
            ),
        ).order_by(TerminalRecordManifest.id.asc())).all():
            if receipt.ordinal in by_ordinal:
                take(by_ordinal[receipt.ordinal].id, receipt)
    found = received_events(
        session, connector_id=job.connector_id, zkt_device_id=job.zkt_device_id, punches=punches,
    ) if punches else {}
    pending = unreadable | {row_id for (row_id, _), event_id in found.items() if event_id is None}
    events = {event_id for event_id in found.values() if event_id is not None}
    return held, pending, events


def sealed_cohort(session: Session, job: ReconciliationJob, manifests, *, dispositions) -> list:
    """A certified job's cohort as it was sealed, despite slots settled after certification.

    A later job in the same epoch may settle this job's final ordinal once a
    later record exists. The settled read replaces the canonical row, but the
    certificate, its counts and its reviews describe the row it replaced.
    """
    manifests = list(manifests)
    if job.capture_certified_at is None or not job.cutoff_count:
        return manifests
    certified_at = ensure_utc(job.capture_certified_at)
    settled_ids: set[int] = set()
    superseded_ids: set[int] = set()
    for event in session.scalars(
        select(ReconciliationEvent)
        .join(ReconciliationJob, ReconciliationJob.id == ReconciliationEvent.job_id)
        .where(
            ReconciliationEvent.state == SETTLED_STATE,
            ReconciliationJob.zkt_device_id == job.zkt_device_id,
            ReconciliationJob.terminal_generation == job.terminal_generation,
            _epoch_clause(ReconciliationJob.source_epoch_id, job.source_epoch_id),
        )
    ).all():
        if ensure_utc(event.created_at) <= certified_at:
            continue
        for item in (event.details or {}).get("rows", []):
            if isinstance(item, dict) and isinstance(item.get("ordinal"), int) and item["ordinal"] < job.cutoff_count:
                settled_ids.add(item.get("settled_manifest_id"))
                superseded_ids.add(item.get("superseded_manifest_id"))
    if not settled_ids:
        return manifests
    rows = {row.id: row for row in manifests if row.id not in settled_ids}
    rows.update({row.id: row for row in session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.id.in_({value for value in superseded_ids if isinstance(value, int)}),
        TerminalRecordManifest.disposition.in_(dispositions),
    )).all()})
    return sorted(rows.values(), key=lambda row: (row.ordinal, row.id))


def unconfirmed_newest_ordinals(session: Session, job: ReconciliationJob) -> list[int]:
    """The job's final ordinal when its canonical row may have been read as the newest slot."""
    ordinal = baseline_newest_ordinal(job)
    if ordinal is None:
        return []
    row = session.scalar(select(TerminalRecordManifest).where(
        TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
        TerminalRecordManifest.generation == job.terminal_generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, job.source_epoch_id),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.ordinal == ordinal,
    ))
    if row is None or row.disposition == "RAW_PRESERVED":
        return []
    return [ordinal] if row.id in newest_read_ids(session, [row]) else []


def slot_receipts(session: Session, row: TerminalRecordManifest) -> list[dict]:
    """Every receipt that re-read this row's slot, with whether ADD holds the punch it decodes to."""
    receipts = session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.zkt_device_id == row.zkt_device_id,
        TerminalRecordManifest.generation == row.generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, row.source_epoch_id),
        TerminalRecordManifest.ordinal == row.ordinal,
        TerminalRecordManifest.canonical_source == False,  # noqa: E712
        TerminalRecordManifest.source_kind.in_(RECEIPT_SOURCE_KINDS),
    ).order_by(TerminalRecordManifest.id.asc())).all()
    holders = ([row] if is_held(row) else []) + list(receipts)
    punches, readable = {}, {}
    for holder in holders:
        readable[holder.id], punch = claimed_punch(holder)
        if punch is not None:
            punches[holder.id] = punch
    found = received_events(
        session, connector_id=row.connector_id, zkt_device_id=row.zkt_device_id, punches=punches,
    ) if punches else {}
    jobs = {
        job.id: job.job_id for job in session.scalars(select(ReconciliationJob).where(
            ReconciliationJob.id.in_({holder.job_id for holder in holders if holder.job_id is not None})
        )).all()
    } if any(holder.job_id is not None for holder in holders) else {}
    return [
        {
            "manifest_id": holder.id,
            "source_kind": holder.source_kind,
            "job_id": jobs.get(holder.job_id),
            "raw_record_digest": holder.raw_record_digest,
            "disposition": holder.declared_disposition or holder.disposition,
            "decodes_as_punch": holder.id in punches or not readable[holder.id],
            "punch_received": (found.get(holder.id) is not None) if holder.id in punches else None,
            "observed_at": holder.created_at,
        }
        for holder in holders
    ]
