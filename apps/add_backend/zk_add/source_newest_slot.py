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
the count it read against, so its newest read is observed. A baseline's final
ordinal is assumed to be such a read: firmware anchors the cutoff at the
latest count and no chunk reports the count it was read against.

ADD keeps such reads fail-closed:

* A newest read (tail or baseline final ordinal) that decodes as a punch no
  capture has delivered is held, not ingested: its row is
  ``IDENTITY_UNRESOLVED`` with the code ``TERMINAL_NEWEST_RECORD_UNCONFIRMED``,
  its claim stays encrypted and the HIGH unreceived-punch alert is raised. If
  LIVE capture delivers the same punch, that delivery stands.
* An attendance event whose only source evidence is a newest read, or a read a
  reconcile has since replaced, is never released automatically, by force
  release or by direct ORDS. The automatic path raises an alert when it holds
  one back.
* After a tail moves past a newest read, its next tail shows the terminal has
  written that slot. When no LIVE punch ADD holds can be the one that filled
  it, ADD raises the unreceived-punch alert so an operator runs a reconcile.

Only a complete source reconcile in the same terminal generation and source
epoch, reading the ordinal while later records existed (anywhere but its own
final ordinal), changes such a row, through receipts that reconcile commits:

* different bytes settle the slot: the fresh read becomes the canonical row
  through ordinary ingestion (event-UID de-duplication against LIVE rows and
  every identity rule still apply) and the earlier read stays as
  non-canonical evidence. When the earlier read had delivered a punch that no
  other evidence holds, or carries a source time correction, the reconcile
  still settles the slot and raises a CRITICAL alert for HR to verify that
  punch, which is held back from every guarded release path;
* identical bytes confirm the row as a genuine record and are kept as a
  non-canonical re-read receipt. A held punch confirmed this way is settled
  and ingested when its time lies between its neighbours' punches; otherwise
  it stays excluded as an out-of-order record for review;
* a re-read at the reconcile's own final ordinal is again a possible
  newest-slot read, and is kept only as a non-canonical re-read receipt. One
  that decodes as a punch ADD has not received holds the job's assurance.

A tail replayed after a cursor rewind never changes a canonical row; its reads
made while later records existed are kept as receipts. A new terminal
generation compares its reads with the previous generation's ledger, except
where that ledger holds a newest read.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_text, encrypt_text
from zk_add.models import (
    AttendanceEvent,
    AttendanceSourceCorrection,
    AuditEvent,
    Connector,
    DeviceAlert,
    ReconciliationCoverage,
    ReconciliationEvent,
    ReconciliationJob,
    SourceTailChunk,
    TerminalRecordManifest,
    TerminalRecordReview,
)
from zk_add.time_utils import ensure_utc, utc_now


# Operator-facing conditions. Stored reader codes stay exactly as the reader sent them.
UNSETTLED_CONDITION = "TERMINAL_NEWEST_RECORD_UNSETTLED"
UNCONFIRMED_CONDITION = "TERMINAL_NEWEST_RECORD_UNCONFIRMED"
CORRECTED_CONDITION = "TERMINAL_NEWEST_RECORD_CORRECTED"
OUT_OF_ORDER_CONDITION = "TERMINAL_NEWEST_RECORD_OUT_OF_ORDER"
# ADD's own code on a held newest-slot punch (the reader's claim stays encrypted).
HELD_CODE = UNCONFIRMED_CONDITION
REPLAY_SUPERSEDED_CODE = "SOURCE_TAIL_REPLAY_SUPERSEDED"
# Reconciliation job events.
SETTLED_STATE = "SOURCE_NEWEST_RECORD_SETTLED"
CONFIRMED_STATE = "SOURCE_NEWEST_RECORD_CONFIRMED"
REREAD_STATE = "SOURCE_NEWEST_RECORD_REREAD"
HELD_STATE = "SOURCE_NEWEST_RECORD_HELD"
OUT_OF_ORDER_STATE = "SOURCE_NEWEST_RECORD_OUT_OF_ORDER"
LINEAGE_STATE = "SOURCE_NEWEST_RECORD_LINEAGE"
PROBE_REQUEUED_STATE = "SOURCE_DIVERGENCE_NEWEST_RECORD"
# Reconciliation safety holds and the source-exception assurance state.
OPEN_JOB_HOLD = "SOURCE_NEWEST_RECORD_JOB_OPEN"
PUNCH_PENDING_HOLD = "SOURCE_NEWEST_PUNCH_UNRECEIVED"
PUNCH_PENDING_STATE = "NEWEST_PUNCH_UNRECEIVED"
# Alerts.
CORRECTED_ALERT = "TERMINAL_NEWEST_RECORD_CORRECTED"
CHANGED_ALERT = "TERMINAL_NEWEST_RECORD_CHANGED"
UNRECEIVED_ALERT = "TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED"
RELEASE_HELD_ALERT = "TERMINAL_NEWEST_RECORD_RELEASE_HELD"
PROBE_SUPERSEDED_STATE = "SUPERSEDED_BY_NEWEST_RECORD_RULE"
# Why a slot is on the unreceived-punch alert.
HELD_CAUSE = "HELD_NEWEST_READ"
FILLED_CAUSE = "SLOT_FILLED_UNSEEN"
# Non-canonical receipts: a reconcile's re-read of a newest-slot row, and a
# verified tail replay's read of a committed newest-slot row.
REREAD_SOURCE_KIND = "BASELINE_REREAD"
REPLAY_SOURCE_KIND = "TAIL_REPLAY"
RECEIPT_SOURCE_KINDS = (REREAD_SOURCE_KIND, REPLAY_SOURCE_KIND)
# Source kinds of a terminal read; a non-canonical one was replaced by a settlement.
READ_SOURCE_KINDS = ("TAIL", "BASELINE", "RECOVERY_PREFIX")
# The interpretation a newest-slot claim was stored under.
GUARD_VERSION = "zkt-newest-slot-guard-1"
EXCEPTION_DISPOSITIONS = frozenset({"INVALID_TIME", "MALFORMED", "IDENTITY_UNRESOLVED"})
# Rows labelled as unsettled newest reads in the exception inspector.
SETTLEABLE_DISPOSITIONS = EXCEPTION_DISPOSITIONS | {"TERMINAL_DUPLICATE"}
PARSED_DISPOSITIONS = frozenset({"EVENT", "BLOCKED_IDENTITY", "TERMINAL_DUPLICATE"})
LIVE_SOURCES = frozenset({"LIVE", "LIVE_POLL"})
RELEASE_BLOCKED_STATUSES = frozenset({"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"})
# Entries an open alert keeps; its count and oldest ordinal cover the rest.
ALERT_SLOT_LIMIT = 200
ALERT_REFRESH_LIMIT = 100
# How far down a filled-slot check walks to the last genuine read, and the
# terminal clock skew allowed when the window ends now.
FILLED_WINDOW_LIMIT = 1000
FILLED_CLOCK_SKEW = timedelta(minutes=5)
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
    "was sent to Oracle and they cannot be corrected. If LIVE capture delivers the same punch, "
    "that delivery stands. A later complete source reconcile that reads this slot while later "
    "records exist ingests the punch when it reads the same bytes, or replaces this row."
)
CORRECTED_REASON = (
    "A source time correction was created from this row before ADD recognised it as the "
    "terminal's unwritten newest record. Its bytes are not the punch in this slot, so the "
    "corrected punch may belong to someone else. HR must verify that punch."
)
OUT_OF_ORDER_REASON = (
    "A complete reconcile read the same bytes again while later records existed, so the "
    "terminal really holds them in this slot, but their time does not lie between the "
    "punches before and after it. They may be stale bytes the terminal never overwrote, with "
    "another person's identity, so ADD did not ingest them. Nothing was sent to Oracle. "
    "Review the row; it cannot be corrected."
)
UNRECEIVED_MESSAGE = (
    "A terminal newest-record slot holds, or may hold, a punch ADD has not received. Run a "
    "complete source reconcile after the next punch on this terminal so ADD reads the slot "
    "while a later record exists. LIVE capture delivering the same punch also clears a slot."
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
    rows = [row for row in rows if row.disposition != "RAW_PRESERVED"]
    if not rows:
        return set()
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
    """Newest-slot rows that produced no attendance of their own, as the inspector labels them."""
    return newest_read_ids(
        session, [row for row in rows if row.disposition in SETTLEABLE_DISPOSITIONS]
    )


def is_unsettled_newest(session: Session, row: TerminalRecordManifest) -> bool:
    return row.id in unsettled_newest_ids(session, [row])


def out_of_order_ids(session: Session, rows) -> set[int]:
    """Held rows a reconcile confirmed but left excluded because their time is out of order.

    A held row is always a newest read; once a re-read with identical bytes
    while later records existed confirms it, it is no longer one. Such a row
    was not settled because its punch time did not lie between its
    neighbours'. It is an ordinary reviewable exception, never a pending punch.
    """
    held = [row for row in rows if is_held(row)]
    if not held:
        return set()
    return {row.id for row in held} - newest_read_ids(session, held)


def newest_only_event_ids(session: Session, events) -> set[int]:
    """Attendance events whose only source evidence is a newest read or a replaced read.

    A LIVE capture is independent evidence of its own punch. Any other event
    whose every source row is a read of the terminal's newest slot (observed
    by a tail or assumed at a baseline's final ordinal, and not confirmed
    since), or a read a reconcile has since replaced with the slot's real
    record, is not proof of a punch and may carry someone else's identity
    bytes. An event derived by a source time correction from such a row is
    treated the same way.
    """
    ids = {event.id for event in events
           if event is not None and event.id is not None and event.source not in LIVE_SOURCES}
    if not ids:
        return set()
    linked: dict[int, list[TerminalRecordManifest]] = defaultdict(list)
    for row in session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.attendance_event_id.in_(ids),
        or_(TerminalRecordManifest.canonical_source == True,  # noqa: E712
            TerminalRecordManifest.source_kind.in_(READ_SOURCE_KINDS)),
    )).all():
        linked[row.attendance_event_id].append(row)
    derived: dict[int, list[TerminalRecordManifest]] = defaultdict(list)
    corrections = session.execute(select(
        AttendanceSourceCorrection.derived_attendance_event_id, AttendanceSourceCorrection.manifest_id,
    ).where(
        AttendanceSourceCorrection.derived_attendance_event_id.in_(ids),
        AttendanceSourceCorrection.status == "CREATED",
        AttendanceSourceCorrection.manifest_id.is_not(None),
    )).all()
    if corrections:
        sources = {row.id: row for row in session.scalars(select(TerminalRecordManifest).where(
            TerminalRecordManifest.id.in_({manifest_id for _, manifest_id in corrections}))).all()}
        for event_id, manifest_id in corrections:
            if manifest_id in sources:
                derived[event_id].append(sources[manifest_id])
    rows = [row for items in (*linked.values(), *derived.values()) for row in items]
    if not rows:
        return set()
    newest = newest_read_ids(session, [row for row in rows if row.canonical_source])

    def unproven(row: TerminalRecordManifest) -> bool:
        if row.disposition == "RAW_PRESERVED":
            return False
        return not row.canonical_source or row.id in newest

    return {
        event_id for event_id in ids
        if (items := [*linked.get(event_id, []), *derived.get(event_id, [])])
        and all(unproven(row) for row in items)
    }


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


def _punch_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


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
            claimed = _punch_time(punch.get("device_event_time"))
            same = claimed is not None and ensure_utc(row.device_event_time) == claimed
        found[key] = row.id if same else None
    return found


def held_newest_uids(session: Session, *, records, newest_ordinal: int | None) -> dict[int, str]:
    """Newest reads that decode as a punch no capture delivered: ``{ordinal: event_uid}``.

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


def _source_time(source) -> datetime | None:
    from zk_add import reconciliation as rec

    if (source.event is None or source.disposition not in {"EVENT", "BLOCKED_IDENTITY"}
            or rec._source_identity_missing(source)):
        return None
    return ensure_utc(source.event.device_event_time)


def in_time_order(session: Session, *, job: ReconciliationJob, ordinal: int, source, records) -> bool:
    """Whether a confirmed newest-slot punch lies between its neighbours' punch times.

    The nearest decodable punch below and above the ordinal comes from this
    chunk, else from the epoch's canonical rows. A missing neighbour leaves
    that side open. A terminal that never rewrote a stale slot would return
    an older punch there, possibly another person's, which fails this check.
    """
    punch_time = _source_time(source)
    if punch_time is None:
        return False
    timed = [(row.ordinal, value) for row in records if (value := _source_time(row)) is not None]
    lower = max(((o, t) for o, t in timed if o < ordinal), default=None)
    upper = min(((o, t) for o, t in timed if o > ordinal), default=None)

    def canonical(bound):
        statement = (
            select(AttendanceEvent.device_event_time)
            .join(TerminalRecordManifest, TerminalRecordManifest.attendance_event_id == AttendanceEvent.id)
            .where(
                TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
                TerminalRecordManifest.generation == job.terminal_generation,
                _epoch_clause(TerminalRecordManifest.source_epoch_id, job.source_epoch_id),
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.disposition.in_(PARSED_DISPOSITIONS),
            )
        )
        if bound == "lower":
            statement = statement.where(TerminalRecordManifest.ordinal < ordinal).order_by(
                TerminalRecordManifest.ordinal.desc())
        else:
            statement = statement.where(TerminalRecordManifest.ordinal > ordinal).order_by(
                TerminalRecordManifest.ordinal.asc())
        value = session.scalar(statement.limit(1))
        return ensure_utc(value) if value is not None else None

    low = lower[1] if lower is not None else canonical("lower")
    high = upper[1] if upper is not None else canonical("upper")
    return (low is None or low <= punch_time) and (high is None or punch_time <= high)


def orphaned_events(session: Session, pairs, *, records=()) -> dict[int, int]:
    """``{prior_id: event_id}`` for replaced reads that delivered a punch nothing else holds.

    A punch is held elsewhere when LIVE captured it, the fresh read decodes to
    the same event, or a canonical row at another ordinal links it (the
    newest read was then a byte copy of that record), including a row the
    same chunk is about to store from ``records``.
    """
    pairs = [(prior, source) for prior, source in pairs if prior.attendance_event_id is not None]
    read_elsewhere = {(source.event.event_uid, source.ordinal) for source in records if source.event is not None}
    if not pairs:
        return {}
    event_ids = {prior.attendance_event_id for prior, _ in pairs}
    events = {row.id: row for row in session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(event_ids))).all()}
    linked: dict[int, set[int]] = defaultdict(set)
    for event_id, ordinal in session.execute(select(
        TerminalRecordManifest.attendance_event_id, TerminalRecordManifest.ordinal,
    ).where(
        TerminalRecordManifest.attendance_event_id.in_(event_ids),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
    )).all():
        linked[event_id].add(ordinal)
    found = {}
    for prior, source in pairs:
        event = events.get(prior.attendance_event_id)
        if event is None or event.source in LIVE_SOURCES:
            continue
        if source is not None and source.event is not None and source.event.event_uid == event.event_uid:
            continue
        # A copy of the same read at the same ordinal (a recovery epoch's) is not other evidence.
        if linked.get(event.id, set()) - {prior.ordinal}:
            continue
        if any(uid == event.event_uid and ordinal != prior.ordinal for uid, ordinal in read_elsewhere):
            continue
        found[prior.id] = event.id
    return found


def open_job_block(
    session: Session, *, job: ReconciliationJob, priors: list[TerminalRecordManifest],
) -> tuple[str, str, dict] | None:
    """Why a reconcile chunk must not settle these rows, if it must not.

    Another open, capture-certified job in the same epoch sealed these rows
    into its exception cohort; settling would break that seal while that job
    is still being assured. The chunk is not committed.
    """
    lowest = min(prior.ordinal for prior in priors)
    sealed = other_open_certified_job(session, job, below=lowest)
    if sealed is not None:
        return OPEN_JOB_HOLD, (
            "Another open reconciliation sealed these terminal newest-record rows into its "
            "certified exception cohort. Finish or cancel it, then retry this reconcile."
        ), {"ordinals": sorted(prior.ordinal for prior in priors), "open_job_id": sealed}
    return None


def other_open_certified_job(session: Session, job: ReconciliationJob, *, below: int | None = None) -> str | None:
    """Another open, capture-certified job of the same terminal epoch, by job ID."""
    from zk_add.reconciliation import TERMINAL_JOB_STATES

    statement = select(ReconciliationJob.job_id).where(
        ReconciliationJob.zkt_device_id == job.zkt_device_id,
        ReconciliationJob.terminal_generation == job.terminal_generation,
        _epoch_clause(ReconciliationJob.source_epoch_id, job.source_epoch_id),
        ReconciliationJob.id != job.id,
        ReconciliationJob.capture_certified_at.is_not(None),
        ReconciliationJob.status.not_in(TERMINAL_JOB_STATES),
    )
    if below is not None:
        statement = statement.where(ReconciliationJob.cutoff_count > below)
    return session.scalar(statement.order_by(ReconciliationJob.id.asc()).limit(1))


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
    orphaned_event_id: int | None = None,
    correction_ids: list[str] | None = None,
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
        "confirmed_identical": prior.raw_record_digest == settled.raw_record_digest,
    }
    if orphaned_event_id is not None:
        # The punch ADD took from these bytes has no other source evidence.
        details["unverified_attendance_event_id"] = orphaned_event_id
    if correction_ids:
        details["unverified_correction_ids"] = list(correction_ids)
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


# --- Alerts -----------------------------------------------------------------


def _locked_alert(session: Session, connector: Connector, code: str) -> DeviceAlert | None:
    """The connector's open alert for ``code``, row-locked so merges never drop entries."""
    from zk_add.service import open_alert_row

    row = open_alert_row(session, connector, code)
    if row is None or row.id is None:
        return row
    return session.scalar(
        select(DeviceAlert).where(DeviceAlert.id == row.id).with_for_update()
        .execution_options(populate_existing=True)
    )


def _slot_key(entry: dict) -> tuple:
    return (entry.get("generation"), entry.get("source_epoch_id"), entry.get("ordinal"))


def _bounded(entries: list[dict], *, key, overflow: int) -> dict:
    """Keep the oldest entries an alert can show; the count and overflow cover the rest."""
    entries = sorted(entries, key=key)
    kept = entries[:ALERT_SLOT_LIMIT]
    return {"kept": kept, "overflow": overflow + max(0, len(entries) - len(kept))}


def _write_unreceived(session: Session, connector: Connector, alert, slots: list[dict], *,
                      overflow: int, details: dict) -> None:
    from zk_add.service import resolve_alert, upsert_alert

    if not slots and not overflow:
        if alert is not None:
            resolve_alert(session, connector, code=UNRECEIVED_ALERT, touch_last_seen=False,
                          resolution={"kind": "NEWEST_RECORD_SLOTS_ACCOUNTED"})
        return
    bounded = _bounded(slots, key=lambda entry: (entry.get("ordinal") or 0, _slot_key(entry)), overflow=overflow)
    kept = bounded["kept"]
    upsert_alert(
        session,
        connector,
        code="TERMINAL_NEWEST_RECORD_PUNCH_UNRECEIVED",
        severity="HIGH",
        message=UNRECEIVED_MESSAGE,
        details={
            **details,
            "failure_category": UNRECEIVED_ALERT,
            "slots": kept,
            "ordinals": sorted({entry["ordinal"] for entry in kept}),
            "slot_count": len(kept) + bounded["overflow"],
            "slots_not_listed": bounded["overflow"],
            "oldest_ordinal": min((entry["ordinal"] for entry in kept), default=None),
            "inspector_path": (
                "/reconciliation?tab=source-exceptions&device_id=" f"{connector.connector_id}"
            ),
        },
    )


def alert_unreceived(session: Session, connector: Connector, *, cause: str, slots: list[dict],
                     details: dict | None = None) -> None:
    """Latch one alert per device naming every slot that holds, or may hold, a missed punch.

    ``slots`` are ``{ordinal, generation, source_epoch_id}`` plus optional
    evidence. Entries merge by slot, so repeated evidence for one slot is one
    entry. The assurance refresh drops a slot once ADD accounts for it.
    """
    if not slots:
        return
    alert = _locked_alert(session, connector, UNRECEIVED_ALERT)
    previous = (alert.details or {}) if alert is not None else {}
    merged = {_slot_key(entry): dict(entry) for entry in previous.get("slots", []) if isinstance(entry, dict)}
    now = utc_now().isoformat()
    for entry in slots:
        key = _slot_key(entry)
        current = merged.get(key, {"first_seen_at": now})
        causes = sorted({*current.get("causes", []), cause})
        merged[key] = {**current, **entry, "causes": causes}
    extra = {key: value for key, value in previous.items() if key in {"job_id", "coverage_id"}}
    extra.update(details or {})
    _write_unreceived(session, connector, alert, list(merged.values()),
                      overflow=int(previous.get("slots_not_listed") or 0), details=extra)


def _hr_details(session: Session, connector: Connector, code: str, details: dict) -> dict:
    """Details for a CRITICAL HR alert, merged under a row lock with the open alert's."""
    alert = _locked_alert(session, connector, code)
    previous = (alert.details or {}) if alert is not None else {}
    merged: dict[str, list] = {}
    for key in ("manifest_ids", "attendance_event_ids", "correction_ids", "ordinals", "job_ids"):
        values = [*previous.get(key, []), *details.get(key, [])]
        merged[key] = sorted({value for value in values if value is not None}, key=str)[:ALERT_SLOT_LIMIT]
    return {
        **{key: value for key, value in details.items() if key not in merged},
        **merged,
        "failure_category": code,
        "inspector_path": "/reconciliation?tab=source-exceptions&device_id=" f"{connector.connector_id}",
    }


CHANGED_MESSAGE = (
    "A terminal newest-record row that delivered a punch read differently once later records "
    "existed. The punch ADD took from it may not be the slot's punch and may belong to someone "
    "else; no other source evidence holds it, and ADD holds it back from every guarded release "
    "path. HR must verify it."
)
CORRECTED_MESSAGE = (
    "A source time correction was made from a terminal newest-record row that a later read "
    "showed was not the slot's punch. HR must verify the corrected punch; ADD holds it back "
    "from every guarded release path."
)


def alert_changed(session: Session, connector: Connector, *, rows: list[tuple[TerminalRecordManifest, int]],
                  job_id: str | None = None, coverage_id: str | None = None) -> None:
    from zk_add.service import upsert_alert

    if not rows:
        return
    upsert_alert(session, connector, code="TERMINAL_NEWEST_RECORD_CHANGED", severity="CRITICAL",
                 message=CHANGED_MESSAGE, details=_hr_details(session, connector, CHANGED_ALERT, {
                     "manifest_ids": [prior.id for prior, _ in rows],
                     "attendance_event_ids": [event_id for _, event_id in rows],
                     "ordinals": [prior.ordinal for prior, _ in rows],
                     "job_ids": [job_id] if job_id else [],
                     **({"coverage_id": coverage_id} if coverage_id else {}),
                 }))


def alert_corrected(session: Session, connector: Connector, *, corrected: dict[int, list[str]],
                    rows: list[TerminalRecordManifest], job_id: str | None = None) -> None:
    from zk_add.service import upsert_alert

    if not corrected:
        return
    by_id = {row.id: row for row in rows}
    upsert_alert(session, connector, code="TERMINAL_NEWEST_RECORD_CORRECTED", severity="CRITICAL",
                 message=CORRECTED_MESSAGE, details=_hr_details(session, connector, CORRECTED_ALERT, {
                     "manifest_ids": sorted(corrected),
                     "correction_ids": [cid for ids in corrected.values() for cid in ids],
                     "ordinals": [by_id[row_id].ordinal for row_id in corrected if row_id in by_id],
                     "job_ids": [job_id] if job_id else [],
                 }))


def alert_release_held(session: Session, connector: Connector, event: AttendanceEvent) -> None:
    """Say that automatic release held back a punch read only from a newest slot."""
    from zk_add.service import upsert_alert

    alert = _locked_alert(session, connector, RELEASE_HELD_ALERT)
    previous = (alert.details or {}) if alert is not None else {}
    ids = sorted({*previous.get("attendance_event_ids", []), event.id})
    if alert is not None and ids == sorted(previous.get("attendance_event_ids", [])):
        return
    kept = ids[:ALERT_SLOT_LIMIT]
    not_listed = int(previous.get("events_not_listed") or 0) + len(ids) - len(kept)
    upsert_alert(session, connector, code="TERMINAL_NEWEST_RECORD_RELEASE_HELD", severity="HIGH", message=(
        "Automatic release held back identity-blocked punches whose only source is a read of the "
        "terminal's newest slot, or a read a reconcile replaced. Their identity bytes may belong to "
        "someone else. A complete source reconcile that confirms the slot releases them normally."
    ), details={
        "failure_category": RELEASE_HELD_ALERT,
        "attendance_event_ids": kept,
        "event_count": len(kept) + not_listed,
        "events_not_listed": not_listed,
    })


# --- Slot evaluation --------------------------------------------------------


def _evaluate_slots(
    session: Session, *, connector_id: int, zkt_device_id: int,
    holders: dict[int, list[tuple[TerminalRecordManifest, bool]]],
    canonical: dict[int, TerminalRecordManifest],
) -> tuple[set[int], set[int]]:
    """``(pending_ordinals, received_event_ids)`` for slots with newest-slot claims.

    ``holders`` maps an ordinal to rows whose read decodes a claim at that
    slot, each marked authoritative when it was read while later records
    existed (a tail replay receipt). ``canonical`` maps the ordinal to its
    canonical row, which is authoritative once it is no longer a newest read.

    With an authoritative read, the slot holds that read: it is pending only
    when such a read decodes to a punch ADD has not received. Otherwise the
    slot is accounted for when a claim decodes to a punch LIVE capture holds
    and no canonical row at another ordinal holds it (which would make the
    read a byte copy of that record), and pending when any claim decodes to a
    punch ADD has not received or cannot be read.
    """
    rows = list(canonical.values())
    canonical_newest = (
        newest_read_ids(session, rows) | {row.id for row in rows if is_held(row)}
    ) - out_of_order_ids(session, rows)
    punches: dict[tuple, dict] = {}
    unreadable: dict[int, list[bool]] = defaultdict(list)
    for ordinal, items in holders.items():
        for holder, authoritative in items:
            readable, punch = claimed_punch(holder)
            if not readable:
                unreadable[ordinal].append(authoritative)
            elif punch is not None:
                punches[(ordinal, holder.id, authoritative)] = punch
    found = received_events(
        session, connector_id=connector_id, zkt_device_id=zkt_device_id, punches=punches,
    ) if punches else {}
    event_ids = {event_id for event_id in found.values() if event_id is not None}
    linked: dict[int, set[int]] = defaultdict(set)
    live: set[int] = set()
    if event_ids:
        for event_id, ordinal in session.execute(select(
            TerminalRecordManifest.attendance_event_id, TerminalRecordManifest.ordinal,
        ).where(
            TerminalRecordManifest.attendance_event_id.in_(event_ids),
            TerminalRecordManifest.zkt_device_id == zkt_device_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
        )).all():
            linked[event_id].add(ordinal)
        live = set(session.scalars(select(AttendanceEvent.id).where(
            AttendanceEvent.id.in_(event_ids), AttendanceEvent.source.in_(LIVE_SOURCES))).all())
    pending: set[int] = set()
    for ordinal in set(holders) | set(canonical):
        row = canonical.get(ordinal)
        claims = [(key, event_id) for key, event_id in found.items() if key[0] == ordinal]
        authoritative_claims = [(key, event_id) for key, event_id in claims if key[2]]
        authoritative_row = row is not None and row.id not in canonical_newest
        if authoritative_row or authoritative_claims or any(unreadable.get(ordinal, [])):
            # The slot's content is known; only an authoritative unreceived punch is pending.
            if (any(event_id is None for _, event_id in authoritative_claims)
                    or any(unreadable.get(ordinal, []))):
                pending.add(ordinal)
            continue
        genuine = any(
            event_id is not None and event_id in live and not (linked[event_id] - {ordinal})
            for _, event_id in claims
        )
        if genuine:
            continue
        if any(event_id is None for _, event_id in claims) or unreadable.get(ordinal):
            pending.add(ordinal)
    return pending, event_ids


def _canonical_rows(session: Session, *, zkt_device_id: int, generation: int, epoch_id, ordinals):
    ordinals = set(ordinals)
    if not ordinals:
        return {}
    return {row.ordinal: row for row in session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.zkt_device_id == zkt_device_id,
        TerminalRecordManifest.generation == generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, epoch_id),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.ordinal.in_(ordinals),
    )).all()}


def newest_evidence(session: Session, job: ReconciliationJob, manifests) -> dict:
    """Newest-slot punch evidence for a job's certified range.

    ``held`` are cohort rows ADD held as unconfirmed punches; they resolve
    once their slot is accounted for. ``out_of_order`` are confirmed held
    rows left excluded; they are ordinary reviewable exceptions.
    ``pending_ordinals`` are slots below the cutoff for which ADD holds
    evidence of a punch it has not received: a held punch, this job's re-read
    of its own final ordinal (whatever row it re-read), or a tail replay read
    while a later record existed. ``pending_ids`` are cohort rows at those
    slots; a review cannot exclude them. ``received_event_ids`` are events
    that independently hold such a punch; they join the job's Oracle
    membership.
    """
    manifests = list(manifests)
    cutoff = job.cutoff_count or 0
    held_rows = [row for row in manifests if is_held(row)]
    out_of_order = out_of_order_ids(session, held_rows)
    held = {row.id for row in held_rows} - out_of_order
    holders: dict[int, list[tuple[TerminalRecordManifest, bool]]] = defaultdict(list)
    for row in held_rows:
        if row.id in held:
            holders[row.ordinal].append((row, False))
    if cutoff:
        for receipt in session.scalars(select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
            TerminalRecordManifest.generation == job.terminal_generation,
            _epoch_clause(TerminalRecordManifest.source_epoch_id, job.source_epoch_id),
            TerminalRecordManifest.canonical_source == False,  # noqa: E712
            TerminalRecordManifest.interpretation_version == GUARD_VERSION,
            TerminalRecordManifest.ordinal < cutoff,
            or_(
                and_(TerminalRecordManifest.source_kind == REREAD_SOURCE_KIND,
                     TerminalRecordManifest.job_id == job.id,
                     TerminalRecordManifest.ordinal == cutoff - 1),
                TerminalRecordManifest.source_kind == REPLAY_SOURCE_KIND,
            ),
        ).order_by(TerminalRecordManifest.id.asc())).all():
            holders[receipt.ordinal].append((receipt, receipt.source_kind == REPLAY_SOURCE_KIND))
    if not holders:
        return {"held": held, "out_of_order": out_of_order, "pending_ordinals": set(),
                "pending_ids": set(), "received_event_ids": set()}
    # The slot is judged by its current canonical row: a later job that
    # settled it read the slot while later records existed.
    canonical = _canonical_rows(session, zkt_device_id=job.zkt_device_id, generation=job.terminal_generation,
                                epoch_id=job.source_epoch_id, ordinals=holders)
    pending, received = _evaluate_slots(
        session, connector_id=job.connector_id, zkt_device_id=job.zkt_device_id,
        holders=holders, canonical=canonical,
    )
    return {
        "held": held,
        "out_of_order": out_of_order,
        "pending_ordinals": pending,
        "pending_ids": {row.id for row in manifests if row.ordinal in pending},
        "received_event_ids": received,
    }


def slot_pending(session: Session, *, connector_id: int, zkt_device_id: int, generation: int,
                 epoch_id, ordinals) -> set[int]:
    """Device-level: the given slots that still hold evidence of a punch ADD has not received."""
    ordinals = set(ordinals)
    if not ordinals:
        return set()
    holders: dict[int, list[tuple[TerminalRecordManifest, bool]]] = defaultdict(list)
    canonical = _canonical_rows(session, zkt_device_id=zkt_device_id, generation=generation,
                                epoch_id=epoch_id, ordinals=ordinals)
    stale = out_of_order_ids(session, list(canonical.values()))
    for ordinal, row in canonical.items():
        if is_held(row) and row.id not in stale:
            holders[ordinal].append((row, False))
    for receipt, cutoff in session.execute(
        select(TerminalRecordManifest, ReconciliationJob.cutoff_count)
        .outerjoin(ReconciliationJob, ReconciliationJob.id == TerminalRecordManifest.job_id)
        .where(
            TerminalRecordManifest.zkt_device_id == zkt_device_id,
            TerminalRecordManifest.generation == generation,
            _epoch_clause(TerminalRecordManifest.source_epoch_id, epoch_id),
            TerminalRecordManifest.canonical_source == False,  # noqa: E712
            TerminalRecordManifest.interpretation_version == GUARD_VERSION,
            TerminalRecordManifest.source_kind.in_(RECEIPT_SOURCE_KINDS),
            TerminalRecordManifest.ordinal.in_(ordinals),
        )
    ).all():
        if receipt.source_kind == REPLAY_SOURCE_KIND:
            holders[receipt.ordinal].append((receipt, True))
        elif cutoff is not None and receipt.ordinal + 1 == cutoff:
            holders[receipt.ordinal].append((receipt, False))
    if not holders:
        return set()
    pending, _ = _evaluate_slots(session, connector_id=connector_id, zkt_device_id=zkt_device_id,
                                 holders=holders, canonical=canonical)
    return pending & ordinals


# --- Detecting a filled slot after a tail ------------------------------------


def _unlinked_captures(session: Session, *, connector_id: int, zkt_device_id: int, after, before,
                       limit: int) -> int:
    """Captured punches of this binding inside the window that no canonical source row holds (up to ``limit``).

    LIVE capture is the usual one; older firmware also delivers punches in
    reconnect dumps and periodic reconcile batches. A punch some source read
    created and only a replaced read still links is not an independent
    capture. A capture whose slot a source read saw is linked by that row.
    """
    canonical = select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.attendance_event_id == AttendanceEvent.id,
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
    ).exists()
    any_read = select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.attendance_event_id == AttendanceEvent.id,
    ).exists()
    statement = select(AttendanceEvent.id).where(
        AttendanceEvent.connector_id == connector_id,
        AttendanceEvent.zkt_device_id == zkt_device_id,
        AttendanceEvent.source != "FULL_HISTORY",
        ~canonical,
        or_(AttendanceEvent.source.in_(LIVE_SOURCES), ~any_read),
    )
    if after is not None:
        statement = statement.where(AttendanceEvent.device_event_time >= after)
    if before is not None:
        statement = statement.where(AttendanceEvent.device_event_time <= before)
    return len(session.scalars(statement.limit(limit)).all())


def _live_genuine_ids(session: Session, rows) -> set[int]:
    """Newest reads that linked a LIVE punch no other ordinal holds: the slot was already written."""
    rows = [row for row in rows if row.attendance_event_id is not None]
    if not rows:
        return set()
    event_ids = {row.attendance_event_id for row in rows}
    live = set(session.scalars(select(AttendanceEvent.id).where(
        AttendanceEvent.id.in_(event_ids), AttendanceEvent.source.in_(LIVE_SOURCES))).all())
    linked: dict[int, set[int]] = defaultdict(set)
    for event_id, ordinal in session.execute(select(
        TerminalRecordManifest.attendance_event_id, TerminalRecordManifest.ordinal,
    ).where(
        TerminalRecordManifest.attendance_event_id.in_(live or {-1}),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
    )).all():
        linked[event_id].add(ordinal)
    return {row.id for row in rows
            if row.attendance_event_id in live and not (linked[row.attendance_event_id] - {row.ordinal})}


def _unseen_slots(session: Session, *, zkt_device_id: int, generation: int, epoch_id, through: int):
    """``(unseen_ordinals, lower_bound_time)`` walking down from ``through``.

    Unseen slots are newest reads the terminal has since filled with a punch
    no source read saw. The walk stops at the nearest genuine read with a
    punch time, which bounds the window from below, or after
    ``FILLED_WINDOW_LIMIT`` rows.
    """
    rows = session.scalars(select(TerminalRecordManifest).where(
        TerminalRecordManifest.zkt_device_id == zkt_device_id,
        TerminalRecordManifest.generation == generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, epoch_id),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.ordinal <= through,
        TerminalRecordManifest.ordinal >= through - FILLED_WINDOW_LIMIT,
    ).order_by(TerminalRecordManifest.ordinal.desc())).all()
    newest = ledger_newest_ids(session, rows) - _live_genuine_ids(session, rows) - out_of_order_ids(session, rows)
    unseen: list[int] = []
    for row in rows:
        if row.id in newest:
            unseen.append(row.ordinal)
            continue
        if row.attendance_event_id is not None and row.disposition in PARSED_DISPOSITIONS:
            value = session.scalar(select(AttendanceEvent.device_event_time).where(
                AttendanceEvent.id == row.attendance_event_id))
            return unseen, (ensure_utc(value) if value is not None else None)
    return unseen, None


def note_filled_newest_slot(
    session: Session, *, connector: Connector, coverage: ReconciliationCoverage, payload,
) -> list[dict]:
    """After a tail moves past a newest read, check that captures account for the filled slots.

    A tail that starts at ``s`` shows the terminal holds records past
    ``s - 1``. When ``s - 1`` was read as the newest slot, the terminal has
    written it since with the next punch, which no source read has seen; so
    has every earlier newest read back to the last genuine read. Those
    punches lie between the last genuine read's punch and the first punch
    this tail read (or now). Each should be a captured punch of this binding
    that no source row holds. When there are fewer such captures than unseen
    slots, capture missed at least the difference, and ADD puts the unseen
    slots on the unreceived-punch alert so an operator reconciles. Clock
    faults can raise it spuriously; they never hide a source row.
    """
    ordinal = payload.start_ordinal - 1
    zkt = connector.zkt_device
    if ordinal < 0 or zkt is None:
        return []
    unseen, after = _unseen_slots(session, zkt_device_id=zkt.id, generation=payload.terminal_generation,
                                  epoch_id=coverage.source_epoch_id, through=ordinal)
    if not unseen or unseen[0] != ordinal:
        return []
    newest = payload.latest_terminal_count - 1
    times = [value for source in payload.records
             if source.ordinal != newest and (value := _source_time(source)) is not None]
    # Without a later punch in this tail, the window ends now (plus a little
    # terminal clock skew).
    before = min(times) if times else utc_now() + FILLED_CLOCK_SKEW
    captured = _unlinked_captures(session, connector_id=connector.id, zkt_device_id=zkt.id,
                                  after=after, before=before, limit=len(unseen))
    if captured >= len(unseen):
        return []
    window = {"after": after.isoformat() if after else None, "before": before.isoformat(),
              "window_unseen": len(unseen), "window_captured": captured}
    slots = [{"ordinal": value, "generation": payload.terminal_generation,
              "source_epoch_id": coverage.source_epoch_id, **window} for value in sorted(unseen)]
    alert_unreceived(session, connector, cause=FILLED_CAUSE, slots=slots,
                     details={"coverage_id": coverage.coverage_id})
    return slots


def refresh_alerts(session: Session, *, connector: Connector | None = None) -> int:
    """Re-evaluate open newest-record alerts; drop accounted slots and resolve empty alerts.

    Bounded per call. A slot clears when its evidence is accounted for (see
    ``slot_pending``), when a reconcile replaced or confirmed its newest read,
    or, for a slot inferred filled after a tail, when a LIVE punch in its
    window arrives. Release entries clear once the punch is no longer held or
    no longer rests only on a newest read.
    """
    from zk_add.service import resolve_alert

    statement = select(DeviceAlert).where(
        DeviceAlert.code.in_((UNRECEIVED_ALERT, RELEASE_HELD_ALERT)), DeviceAlert.state == "OPEN",
    )
    if connector is not None:
        statement = statement.where(DeviceAlert.connector_id == connector.id)
    changed = 0
    for alert in session.scalars(statement.order_by(DeviceAlert.id.asc()).limit(ALERT_REFRESH_LIMIT)).all():
        owner = connector if connector is not None else session.get(Connector, alert.connector_id)
        zkt = owner.zkt_device if owner is not None else None
        if owner is None or zkt is None:
            continue
        details = alert.details or {}
        if alert.code == RELEASE_HELD_ALERT:
            ids = [value for value in details.get("attendance_event_ids", []) if isinstance(value, int)]
            events = session.scalars(select(AttendanceEvent).where(AttendanceEvent.id.in_(ids))).all() if ids else []
            still = newest_only_event_ids(
                session, [event for event in events if event.ords_status in RELEASE_BLOCKED_STATUSES])
            if set(ids) == still:
                continue
            changed += 1
            if not still and not details.get("events_not_listed"):
                resolve_alert(session, owner, code=RELEASE_HELD_ALERT, touch_last_seen=False,
                              resolution={"kind": "NEWEST_RECORD_PUNCHES_NO_LONGER_HELD"})
            else:
                alert.details = {**details, "attendance_event_ids": sorted(still),
                                 "event_count": len(still) + int(details.get("events_not_listed") or 0)}
            continue
        slots = [entry for entry in details.get("slots", []) if isinstance(entry, dict)
                 and isinstance(entry.get("ordinal"), int)]
        remaining = []
        by_scope: dict[tuple, list[dict]] = defaultdict(list)
        for entry in slots:
            by_scope[(entry.get("generation"), entry.get("source_epoch_id"))].append(entry)
        for (generation, epoch_id), entries in by_scope.items():
            if generation is None:
                remaining.extend(entries)
                continue
            ordinals = {entry["ordinal"] for entry in entries}
            pending = slot_pending(session, connector_id=owner.id, zkt_device_id=zkt.id, generation=generation,
                                   epoch_id=epoch_id, ordinals=ordinals)
            canonical = _canonical_rows(session, zkt_device_id=zkt.id, generation=generation,
                                        epoch_id=epoch_id, ordinals=ordinals)
            rows = list(canonical.values())
            still_unseen = {row.ordinal for row in rows if row.id in (
                ledger_newest_ids(session, rows) - _live_genuine_ids(session, rows) - out_of_order_ids(session, rows))}
            # Slots inferred filled are judged per window: enough captures there account for all of them.
            windows: dict[tuple, list[dict]] = defaultdict(list)
            for entry in entries:
                causes = set(entry.get("causes", []))
                if entry["ordinal"] in pending:
                    remaining.append(entry)
                elif FILLED_CAUSE in causes and entry["ordinal"] in still_unseen:
                    windows[(entry.get("after"), entry.get("before"))].append(entry)
            for (after, before), members in windows.items():
                captured = _unlinked_captures(
                    session, connector_id=owner.id, zkt_device_id=zkt.id,
                    after=_punch_time(after) if after else None, before=_punch_time(before) if before else None,
                    limit=len(members))
                if captured < len(members):
                    remaining.extend(members)
        if len(remaining) == len(slots):
            continue
        changed += 1
        _write_unreceived(session, owner, alert, remaining, overflow=int(details.get("slots_not_listed") or 0),
                          details={key: value for key, value in details.items()
                                   if key in {"job_id", "coverage_id"}})
    return changed


# --- Tail replays -------------------------------------------------------------


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
    confirming,
) -> None:
    """A durable, idempotent record of a replay answered with ADD's unchanged coverage.

    A replayed read made while a later record existed is the slot's real
    content. It is kept as an encrypted non-canonical receipt, and a punch ADD
    has not received raises an alert. It is never ingested: only a complete
    reconcile's own receipt settles a slot.
    """
    from zk_add.reconciliation import _event

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
    settled_reads = [(prior, source) for prior, source in [*differing, *confirming]
                     if not read_was_newest(source.ordinal, payload.latest_terminal_count)]
    receipts = {}
    for _, source in settled_reads:
        receipts[source.ordinal] = _receipt(
            kind=REPLAY_SOURCE_KIND, job_id=None, chunk_id=None, connector_id=connector.id,
            zkt_device_id=coverage.zkt_device_id, terminal_serial=coverage.terminal_serial,
            generation=payload.terminal_generation, source_epoch_id=coverage.source_epoch_id,
            record_size=payload.record_size, source=source,
        )
        session.add(receipts[source.ordinal])
    if receipts:
        session.flush()
    unreceived = unreceived_ordinals(session, connector=connector, records=[source for _, source in settled_reads])
    orphans = orphaned_events(session, [(prior, source) for prior, source in differing
                                        if source.ordinal in receipts])
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
                "committed_disposition": prior.disposition,
                "committed_raw_record_digest": prior.raw_record_digest,
                "replayed_raw_record_digest": source.raw_record_digest,
                "replay_read_newest": read_was_newest(source.ordinal, payload.latest_terminal_count),
                "receipt_manifest_id": receipts[source.ordinal].id if source.ordinal in receipts else None,
                "punch_unreceived": source.ordinal in unreceived,
                "unverified_attendance_event_id": orphans.get(prior.id),
            }
            for prior, source in [*differing, *confirming]
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
        alert_unreceived(session, connector, cause=REPLAY_SOURCE_KIND, slots=[
            {"ordinal": ordinal, "generation": payload.terminal_generation,
             "source_epoch_id": coverage.source_epoch_id, "manifest_id": receipts[ordinal].id}
            for ordinal in unreceived
        ], details={"coverage_id": coverage.coverage_id})
    priors = [prior for prior, _ in differing]
    if orphans:
        alert_changed(session, connector, rows=[(prior, orphans[prior.id]) for prior in priors if prior.id in orphans],
                      coverage_id=coverage.coverage_id)
    # A replay that read the slot while a later record existed proves the
    # slot holds other bytes than the ones a correction was derived from.
    alert_corrected(session, connector, corrected=corrections_by_row(session, [
        prior for prior, source in differing if source.ordinal in receipts
    ]), rows=priors)


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
    is benign when every committed canonical row in the overlap holds the
    replay's raw bytes except rows that are themselves newest-slot reads, and
    the replay makes no claim the ordinary tail path would refuse.

    The replay never changes a canonical row, cursor or chain. Its reads of
    newest-slot rows made while a later record existed are kept as receipts
    (see ``_record_superseded_replay``); a complete reconcile settles such
    rows through its own receipts. Returns ``None`` when benign, otherwise
    the invalidation code.
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
    newest = newest_read_ids(session, committed.values()) | {
        row.id for row in committed.values() if is_held(row)}
    differing = []
    confirming = []
    for source in payload.records:
        if source.ordinal >= stop:
            break
        prior = committed[source.ordinal]
        if same_source_read(prior, source):
            if is_held(prior) and not read_was_newest(source.ordinal, payload.latest_terminal_count):
                # The held punch read again while a later record existed.
                confirming.append((prior, source))
            continue
        if (prior.id not in newest or prior.record_size != payload.record_size
                or prior.raw_record_digest == source.raw_record_digest
                or "RAW_PRESERVED" in {prior.disposition, source.disposition}):
            return "SOURCE_TAIL_REPLAY_DIVERGED"
        differing.append((prior, source))
    code = _replay_attendance_conflict(
        session, connector=connector, coverage=coverage,
        records=[source for _, source in [*differing, *confirming]],
    )
    if code is not None:
        return code
    _record_superseded_replay(
        session, connector=connector, coverage=coverage, payload=payload, differing=differing,
        confirming=confirming,
    )
    return None


# --- Generation lineage -----------------------------------------------------------


def ledger_newest_ids(session: Session, rows) -> set[int]:
    """Rows of an older generation's ledger that are newest reads (or held punches)."""
    rows = [row for row in rows if row.disposition != "RAW_PRESERVED"]
    return newest_read_ids(session, rows) | {row.id for row in rows if is_held(row)}


def ledger_newest_ordinals(session: Session, *, zkt_device_id: int, generation: int, epoch_id, stop: int) -> set[int]:
    """Ordinals below ``stop`` whose canonical row in that ledger is a newest read.

    Candidates are the ordinals any tail of that terminal generation read as
    its newest slot and every job's final ordinal; ``newest_read_ids`` then
    decides from each row's own receipt, so a recovery copy and a confirmed
    row are judged correctly.
    """
    candidates = set(session.scalars(select(SourceTailChunk.end_ordinal - 1).where(
        SourceTailChunk.zkt_device_id == zkt_device_id,
        SourceTailChunk.generation == generation,
        SourceTailChunk.latest_terminal_count == SourceTailChunk.end_ordinal,
        SourceTailChunk.end_ordinal <= stop,
    )).all())
    candidates |= {cutoff - 1 for cutoff in session.scalars(select(ReconciliationJob.cutoff_count).where(
        ReconciliationJob.zkt_device_id == zkt_device_id,
        ReconciliationJob.terminal_generation == generation,
        ReconciliationJob.cutoff_count.is_not(None),
        ReconciliationJob.cutoff_count <= stop,
    )).all() if cutoff}
    held = set(session.scalars(select(TerminalRecordManifest.ordinal).where(
        TerminalRecordManifest.zkt_device_id == zkt_device_id,
        TerminalRecordManifest.generation == generation,
        _epoch_clause(TerminalRecordManifest.source_epoch_id, epoch_id),
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.error_code == HELD_CODE,
        TerminalRecordManifest.ordinal < stop,
    )).all())
    candidates = {ordinal for ordinal in candidates | held if 0 <= ordinal < stop}
    if not candidates:
        return set()
    rows = _canonical_rows(session, zkt_device_id=zkt_device_id, generation=generation, epoch_id=epoch_id,
                           ordinals=candidates)
    newest = ledger_newest_ids(session, rows.values())
    return {ordinal for ordinal, row in rows.items() if row.id in newest}


def note_lineage_newest(session: Session, connector: Connector, *, pairs, job: ReconciliationJob | None = None,
                        coverage: ReconciliationCoverage | None = None) -> None:
    """Record reads that differ from an older generation's newest-slot rows, and alert HR when needed.

    The newer read is the slot's record. The older row stays in its ledger
    unchanged; a punch ADD took from it with no other evidence is flagged.
    """
    if not pairs:
        return
    from zk_add.reconciliation import _event

    orphans = orphaned_events(session, pairs)
    details = {"rows": [
        {"ordinal": prior.ordinal, "previous_manifest_id": prior.id, "previous_generation": prior.generation,
         "previous_disposition": prior.disposition, "previous_raw_record_digest": prior.raw_record_digest,
         "raw_record_digest": source.raw_record_digest,
         "unverified_attendance_event_id": orphans.get(prior.id)}
        for prior, source in sorted(pairs, key=lambda item: item[0].ordinal)
    ]}
    if job is not None:
        _event(session, job, LINEAGE_STATE, details)
    else:
        append_audit(session, actor="system:source-reconciliation", action=LINEAGE_STATE,
                     target_type="reconciliation_coverage",
                     target_id=coverage.coverage_id if coverage is not None else "unknown",
                     outcome="SUCCESS", after=details)
    priors = [prior for prior, _ in pairs]
    if orphans:
        alert_changed(session, connector, rows=[(prior, orphans[prior.id]) for prior in priors if prior.id in orphans],
                      job_id=job.job_id if job is not None else None,
                      coverage_id=coverage.coverage_id if coverage is not None else None)
    alert_corrected(session, connector, corrected=corrections_by_row(session, priors), rows=priors,
                    job_id=job.job_id if job is not None else None)


# --- Sealed cohorts and inspection -------------------------------------------------


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
