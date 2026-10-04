"""Durable processing obligations attached in the custody transaction.

No employee attribution or Oracle completion is inferred here. Reassembly and
derived interpretation are bounded; authority waits for qualified profiles. Callers
serialize mutations using the connector row before the work row, including
when a new fragment arrives while a worker inspects its group.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import json
import time

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_json, decrypt_text
from zk_add.models import Connector, TerminalRecordManifest, TerminalSourceEpoch, ZktCustodyWork, ZktCustodyWorkReceipt, ZktCustodySchedule, ZktDerivedEvidence, ZktObservationReceipt
from zk_add import zkt_derived_evidence as derived
from zk_add.time_utils import utc_now
from zk_add.settings import settings
from zk_add.zkt_packet import FRAGMENT_DATA, PACKET_MAX, parse_fragment, reassemble

PRIORITY_BURST = 8
RECENT_LIVE_SECONDS = 60
LIVE_KINDS = frozenset({"LIVE_PACKET", "LIVE_FRAME", "PACKET_FRAGMENT"})


def key(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class EvidenceInvalid(ValueError):
    pass


def source_work_key(connector: Connector, manifest: TerminalRecordManifest) -> str:
    return key(["zkt-source-work-v1", connector.connector_id, manifest.id, manifest.zkt_device_id,
                manifest.terminal_serial, manifest.generation, manifest.source_epoch_id, manifest.ordinal,
                manifest.record_size, manifest.raw_record_digest, manifest.terminal_record_key])


def attach_source_work(session: Session, connector: Connector,
                       manifests: list[TerminalRecordManifest]) -> None:
    """The source chunk, raw rows, obligations and cursor share one commit.

    A reconciliation receipt is already a durable range receipt. It must not
    manufacture a second journal observation or claim a decoded attendance.
    The connector lock held by source ingestion serializes these associations.
    """
    if len(manifests) > 100:
        raise ValueError("SOURCE_WORK_BATCH_BOUNDS")
    if not manifests:
        return
    session.flush()
    existing = {row.source_manifest_id: row for row in session.scalars(select(ZktCustodyWork).where(
        ZktCustodyWork.source_manifest_id.in_([row.id for row in manifests])))}
    for manifest in manifests:
        if (manifest.connector_id != connector.id or not connector.zkt_device
                or manifest.zkt_device_id != connector.zkt_device.id
                or manifest.terminal_serial != connector.zkt_device.confirmed_serial
                or manifest.terminal_serial != connector.zkt_device.serial
                or not manifest.canonical_source or not manifest.source_epoch_id
                or manifest.disposition != "RAW_PRESERVED" or manifest.attendance_event_id is not None
                or not manifest.protected_raw_record or manifest.record_size not in {8, 16, 40}):
            raise ValueError("SOURCE_WORK_BINDING")
        prior = existing.get(manifest.id)
        if prior:
            if (prior.connector_id, prior.kind, prior.terminal_serial, prior.expected_digest,
                    prior.expected_bytes, prior.work_key) != (
                    connector.id, "SOURCE_LEDGER", manifest.terminal_serial, manifest.raw_record_digest,
                    manifest.record_size, source_work_key(connector, manifest)):
                raise ValueError("SOURCE_WORK_CONFLICT")
            continue
        session.add(ZktCustodyWork(
            work_key=source_work_key(connector, manifest),
            connector_id=connector.id, source_manifest_id=manifest.id, kind="SOURCE_LEDGER",
            terminal_serial=manifest.terminal_serial, expected_bytes=manifest.record_size,
            expected_digest=manifest.raw_record_digest, state="PENDING",
            reason_code="AWAITING_INTERPRETATION", owner="ADD_PROTOCOL",
            evidence_revision=1, processed_revision=0, next_attempt_at=utc_now(),
        ))
    session.flush()


def inspect_source_ledger(session: Session, work: ZktCustodyWork) -> bytes:
    """Authenticate original source bytes before deriving any interpretation."""
    if not settings.pii_fernet_key:
        raise RuntimeError("CUSTODY_KEY_UNAVAILABLE")
    manifest = session.get(TerminalRecordManifest, work.source_manifest_id) if work.source_manifest_id else None
    connector = session.get(Connector, work.connector_id)
    terminal = connector.zkt_device if connector else None
    epoch = session.get(TerminalSourceEpoch, manifest.source_epoch_id) if manifest and manifest.source_epoch_id else None
    if (manifest is None or terminal is None or manifest.connector_id != work.connector_id
            or manifest.zkt_device_id != terminal.id or not manifest.canonical_source
            or epoch is None or epoch.zkt_device_id != terminal.id or epoch.terminal_generation != manifest.generation
            or manifest.terminal_serial != work.terminal_serial or manifest.terminal_serial != terminal.confirmed_serial
            or manifest.terminal_serial != terminal.serial or source_work_key(connector, manifest) != work.work_key
            or manifest.raw_record_digest != work.expected_digest or manifest.record_size != work.expected_bytes
            or manifest.disposition != "RAW_PRESERVED" or manifest.attendance_event_id is not None):
        raise EvidenceInvalid("SOURCE_WORK_EVIDENCE_CHANGED")
    encoded = decrypt_text(manifest.protected_raw_record)
    if not encoded or len(encoded) > 684:
        raise EvidenceInvalid("SOURCE_WORK_BYTES_UNAVAILABLE")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise EvidenceInvalid("SOURCE_WORK_BYTES_CHANGED") from exc
    if len(raw) != work.expected_bytes or hashlib.sha256(raw).hexdigest() != work.expected_digest:
        raise EvidenceInvalid("SOURCE_WORK_BYTES_CHANGED")
    work.state, work.reason_code, work.owner = "WAIT_PROFILE", "PROFILE_QUALIFICATION_REQUIRED", "ADD_PROTOCOL"
    return raw


def attach_work(session: Session, connector: Connector, receipt: ZktObservationReceipt,
                value: dict | None) -> ZktCustodyWork:
    """Replay returns the original work without resetting a hold or a result."""
    link = session.scalar(select(ZktCustodyWorkReceipt).where(ZktCustodyWorkReceipt.receipt_id == receipt.id))
    if link is not None:
        return session.get(ZktCustodyWork, link.work_id)
    kind = "EXCEPTION" if receipt.error_code else (value or {}).get("raw_format", "UNKNOWN")
    fragment = None
    raw = None
    if value and not receipt.error_code:
        raw = base64.b64decode(value["raw_b64"], validate=True)
        if kind == "PACKET_FRAGMENT":
            fragment = parse_fragment(raw)
    identity = key(["zkt-packet-work-v1", connector.connector_id, receipt.terminal_serial,
                    receipt.capture_epoch, fragment.group.hex()]) if fragment else key([
                        "zkt-receipt-work-v1", connector.connector_id, receipt.receipt_id])
    work = session.scalar(select(ZktCustodyWork).where(ZktCustodyWork.work_key == identity))
    if work is None:
        work = ZktCustodyWork(work_key=identity, connector_id=connector.id, kind=kind,
            terminal_serial=receipt.terminal_serial, capture_epoch=receipt.capture_epoch,
            decoder_profile=receipt.decoder_profile, decoder_version=receipt.decoder_version,
            state="PENDING", reason_code="AWAITING_INTERPRETATION", owner="ADD_PROTOCOL",
            evidence_revision=1, processed_revision=0, next_attempt_at=utc_now(),
            expected_bytes=fragment.total if fragment else len(raw) if raw is not None else None,
            expected_digest=fragment.packet_digest.hex() if fragment else receipt.raw_digest)
        if receipt.error_code:
            work.state, work.reason_code, work.owner = "HELD_EXCEPTION", receipt.error_code, "ADD_EVIDENCE_REVIEW"
            work.next_attempt_at = None
        session.add(work)
        session.flush()
    else:
        work.evidence_revision += 1
        if (not fragment or work.expected_bytes != fragment.total
                or work.expected_digest != fragment.packet_digest.hex()
                or (work.decoder_profile, work.decoder_version) != (receipt.decoder_profile, receipt.decoder_version)):
            work.state, work.reason_code = "HELD_EXCEPTION", "CONFLICTING_PACKET_METADATA"
            work.owner, work.next_attempt_at = "ADD_EVIDENCE_REVIEW", None
        elif work.state != "HELD_EXCEPTION":
            # A newly committed extent wakes the group; unchanged ACK replay
            # never repeatedly schedules a missing fragment/profile hold.
            work.state, work.reason_code = "PENDING", "NEW_PACKET_EVIDENCE"
            work.next_attempt_at = utc_now()
    work.updated_at = utc_now()
    session.add(ZktCustodyWorkReceipt(work_id=work.id, receipt_id=receipt.id,
        packet_offset=fragment.offset if fragment else 0,
        data_length=len(fragment.data) if fragment else len(raw) if raw is not None else None))
    session.flush()
    return work


def materialize_packet(session: Session, work: ZktCustodyWork) -> bytes | None:
    """Check protected bytes against their original receipt and packet proof.

    This function never treats missing rows, corruption or contradictory
    extents as a successful decode. It creates no attendance/outbox row.
    """
    from zk_add.zkt_custody import Observation, digest

    maximum = 2 * ((PACKET_MAX + FRAGMENT_DATA - 1) // FRAGMENT_DATA)
    receipts = session.scalars(select(ZktObservationReceipt).join(ZktCustodyWorkReceipt).where(
        ZktCustodyWorkReceipt.work_id == work.id).order_by(ZktObservationReceipt.id).limit(maximum + 1)).all()
    if not receipts or len(receipts) > maximum:
        raise EvidenceInvalid("PACKET_EVIDENCE_BOUNDS")
    if not settings.pii_fernet_key:
        raise RuntimeError("CUSTODY_KEY_UNAVAILABLE")
    fragments = []
    packet = None
    for receipt in receipts:
        protected = decrypt_json(receipt.protected_observation)
        value = protected.get("observation")
        if digest(value) != receipt.payload_digest:
            raise EvidenceInvalid("RECEIPT_DIGEST_MISMATCH")
        try:
            parsed = Observation.model_validate(value)
        except ValidationError as exc:
            raise EvidenceInvalid("PACKET_SCHEMA_INVALID") from exc
        if (receipt.error_code or receipt.connector_id != work.connector_id
                or parsed.terminal_serial != work.terminal_serial or parsed.capture_epoch != work.capture_epoch
                or (parsed.decoder_profile, parsed.decoder_version) != (work.decoder_profile, work.decoder_version)):
            raise EvidenceInvalid("PACKET_EVIDENCE_BINDING")
        raw = base64.b64decode(parsed.raw_b64, validate=True)
        if work.kind == "PACKET_FRAGMENT" and parsed.raw_format == "PACKET_FRAGMENT":
            fragments.append(parse_fragment(raw))
        elif work.kind in {"LIVE_PACKET", "LIVE_FRAME"} and parsed.raw_format == work.kind and packet is None:
            packet = raw
        else:
            raise EvidenceInvalid("PACKET_EVIDENCE_KIND")
    if fragments:
        try:
            packet = reassemble(fragments)
        except ValueError as exc:
            raise EvidenceInvalid("PACKET_FRAGMENT_CONFLICT") from exc
    if packet is not None and (len(packet) != work.expected_bytes
            or hashlib.sha256(packet).hexdigest() != work.expected_digest):
        raise EvidenceInvalid("PACKET_DIGEST_MISMATCH")
    return packet


def inspect_source(session: Session, work: ZktCustodyWork) -> bytes:
    """Associate late source evidence without manufacturing an attendance row."""
    from zk_add.zkt_custody import (Observation, SourceAssociationError, bind_source_occurrence,
                                   digest, source_occurrence_delivery_hold)

    receipts = session.scalars(select(ZktObservationReceipt).join(ZktCustodyWorkReceipt).where(
        ZktCustodyWorkReceipt.work_id == work.id).limit(2)).all()
    if len(receipts) != 1:
        raise EvidenceInvalid("SOURCE_EVIDENCE_BOUNDS")
    if not settings.pii_fernet_key:
        raise RuntimeError("CUSTODY_KEY_UNAVAILABLE")
    receipt = receipts[0]
    value = decrypt_json(receipt.protected_observation).get("observation")
    if digest(value) != receipt.payload_digest:
        raise EvidenceInvalid("RECEIPT_DIGEST_MISMATCH")
    try:
        parsed = Observation.model_validate(value)
    except ValidationError as exc:
        raise EvidenceInvalid("SOURCE_SCHEMA_INVALID") from exc
    raw = base64.b64decode(parsed.raw_b64, validate=True)
    if (receipt.error_code or receipt.connector_id != work.connector_id
            or parsed.raw_format != "SOURCE_RECORD" or parsed.terminal_serial != work.terminal_serial
            or parsed.capture_epoch != work.capture_epoch
            or (parsed.decoder_profile, parsed.decoder_version) != (work.decoder_profile, work.decoder_version)
            or len(raw) != work.expected_bytes
            or parsed.raw_digest != work.expected_digest):
        raise EvidenceInvalid("SOURCE_EVIDENCE_BINDING")
    connector = session.get(Connector, work.connector_id)
    terminal = connector.zkt_device if connector else None
    work.owner = "ADD_RECONCILIATION"
    if (terminal is None or terminal.serial != parsed.terminal_serial
            or terminal.confirmed_serial != parsed.terminal_serial):
        work.state, work.reason_code = "WAIT_SOURCE", "TERMINAL_BINDING_CHANGED"
        return raw
    if parsed.occurrence is None:
        work.state, work.reason_code = "WAIT_SOURCE", "SOURCE_REFERENCE_REQUIRED"
        return raw
    try:
        # Any failed derived association rolls back locally. The committed
        # custody receipt and unrelated work remain available for recovery.
        with session.begin_nested():
            identity = bind_source_occurrence(session, connector, receipt, parsed)
    except SourceAssociationError as exc:
        raise EvidenceInvalid(str(exc)) from exc
    if identity is None:
        work.state, work.reason_code = "WAIT_SOURCE", "CANONICAL_SOURCE_PENDING"
        # Missing source evidence is an ordering obligation, not terminal data
        # corruption. The bounded fair worker retries; unchanged profile holds
        # and unreferenced records are not continuously rescanned.
        work.next_attempt_at = utc_now() + timedelta(seconds=30)
        return raw
    hold = source_occurrence_delivery_hold(session, connector, identity)
    if hold:
        work.state, work.reason_code = "HELD_OCCURRENCE", hold
        return raw
    work.state, work.reason_code = "SOURCE_ASSOCIATED", "EXACT_CANONICAL_SOURCE_BYTES"
    # Identity and downstream receipt verification are separate obligations.
    # An association alone cannot create attendance or mark Oracle complete.
    return raw


def inspect_work(session: Session, work: ZktCustodyWork) -> None:
    """Reassembly is not qualification. Unqualified profiles remain a hold."""
    if work.state == "HELD_EXCEPTION":
        return
    work.next_attempt_at = None
    work.processed_revision = work.evidence_revision
    work.attempt_count += 1
    work.interpretation_version = derived.INTERPRETATION_VERSION
    work.updated_at = utc_now()
    if work.kind not in {"SOURCE_LEDGER", "SOURCE_RECORD", "LIVE_PACKET", "LIVE_FRAME", "PACKET_FRAGMENT"}:
        work.state, work.reason_code, work.owner = "WAIT_PROFILE", "UNKNOWN_RAW_FORMAT", "ADD_PROTOCOL"
        return
    try:
        if work.kind == "SOURCE_LEDGER":
            raw = inspect_source_ledger(session, work)
        elif work.kind == "SOURCE_RECORD":
            raw = inspect_source(session, work)
        else:
            raw = materialize_packet(session, work)
            if raw is None:
                work.state, work.reason_code, work.owner = "WAIT_FRAGMENTS", "INCOMPLETE_PACKET", "ADD_PROTOCOL"
                return
            work.assembled_digest = hashlib.sha256(raw).hexdigest()
            work.assembled_at = work.assembled_at or utc_now()
            work.state, work.reason_code, work.owner = "WAIT_PROFILE", "PROFILE_QUALIFICATION_REQUIRED", "ADD_PROTOCOL"
        # The batch visits each group at most once and flushes before returning.
        # Defer independent inserts/updates so the driver can batch them without
        # a write round trip for every proposed interpretation. Nothing is
        # published or counted as useful progress before the outer commit.
        step = derived.derive_step(session, work, raw, flush=False)
        if step.result == "PENDING":
            work.state, work.reason_code, work.owner = "INTERPRETING", "DERIVED_EVIDENCE_INCOMPLETE", "ADD_PROTOCOL"
            work.next_attempt_at = utc_now()
    except (EvidenceInvalid, derived.DerivedEvidenceInvalid) as exc:
        work.state, work.reason_code, work.owner = "HELD_EXCEPTION", str(exc), "ADD_EVIDENCE_REVIEW"
        return
    except (InvalidToken, ValueError, RuntimeError):
        # A key/configuration problem cannot be reclassified as invalid source
        # data. Keep a retry obligation; one unreadable receipt cannot block
        # later packets/devices. The original ciphertext remains untouched.
        work.state, work.reason_code, work.owner = "RETRY_SYSTEM", "CUSTODY_DECRYPT_UNAVAILABLE", "ADD_OPERATIONS"
        work.next_attempt_at = utc_now() + timedelta(seconds=60)
        return


@dataclass(frozen=True)
class InspectionBatch:
    processed: int
    after_connector: int
    attempted_connectors: int
    locked_connectors: int = 0


def _revision_ranges():
    """Disjoint index ranges skip unchanged holds, even with a generic plan."""
    version = ZktCustodyWork.interpretation_version
    return (version.is_(None), version < derived.INTERPRETATION_VERSION,
            version > derived.INTERPRETATION_VERSION)


def _revision_hold():
    # These are fixed implementation constants, rendered safely by SQLAlchemy.
    # A prepared statement's generic plan must be able to prove the partial
    # index predicate; bound kind/state parameters cannot provide that proof.
    return and_(ZktCustodyWork.next_attempt_at.is_(None),
        ZktCustodyWork.kind.in_([literal(kind, literal_execute=True)
                                for kind in sorted(derived.SUPPORTED_KINDS)]),
        ZktCustodyWork.state != literal("HELD_EXCEPTION", literal_execute=True))


def _oldest_candidates(session: Session, connector_id: int, now, quota: int):
    """Bounded pages avoid sorting a connector's entire retained history.

    Explicit retries retain their deadlines. Revision-only holds drain in
    version/index order within each range; unchanged versions are never read.
    """
    base = select(ZktCustodyWork).where(ZktCustodyWork.connector_id == connector_id)
    candidates = list(session.scalars(base.where(ZktCustodyWork.next_attempt_at <= now)
        .order_by(ZktCustodyWork.next_attempt_at, ZktCustodyWork.id)
        .limit(quota).with_for_update(skip_locked=True)))
    for version_range in _revision_ranges():
        candidates.extend(session.scalars(base.where(_revision_hold(), version_range)
            .order_by(ZktCustodyWork.interpretation_version, ZktCustodyWork.created_at, ZktCustodyWork.id)
            .limit(quota).with_for_update(skip_locked=True)))
    # SQLite returns naive stored UTC, while PostgreSQL returns aware values.
    def age(row):
        stamp = row.next_attempt_at or row.created_at
        return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp, row.id)
    return sorted(candidates, key=age)[:quota]


def advance_work_batch(session: Session, *, limit: int = 100, after_connector: int = 0,
                       time_budget_ms: int | None = 250, clock=None) -> InspectionBatch:
    """Fair bounded inspection; connector-first locks match custody ingestion.

    This stage deliberately stops at profile qualification. It never emits an
    attendance event or an Oracle outbox based on an unqualified interpretation.
    """
    maximum = max(1, min(limit, 500))
    clock = clock or time.monotonic
    deadline = None if time_budget_ms is None else clock() + max(1, min(time_budget_ms, 1000)) / 1000
    now = utc_now()
    # A decoder revision wakes each supported hold once. Unchanged holds do
    # not repeatedly decrypt/scan receipts, and disabled connectors stay idle.
    base = select(ZktCustodyWork.id).where(ZktCustodyWork.connector_id == Connector.id)
    # EXISTS can pick a sequential scan for a generic plan's estimated first
    # match. Ordered one-row probes retain index ordering even when a site's
    # only pending row has just settled and most retained rows are holds.
    due = or_(base.where(ZktCustodyWork.next_attempt_at <= now)
        .order_by(ZktCustodyWork.next_attempt_at, ZktCustodyWork.id)
        .limit(1).scalar_subquery().is_not(None), *(
            base.where(_revision_hold(), version_range)
            .order_by(ZktCustodyWork.interpretation_version, ZktCustodyWork.created_at, ZktCustodyWork.id)
            .limit(1).scalar_subquery().is_not(None) for version_range in _revision_ranges()))
    connectors = session.scalars(select(Connector.id).where(Connector.zkt_custody_enabled.is_(True), due)
        .order_by(Connector.id <= after_connector, Connector.id).limit(maximum)).all()
    quota = max(1, maximum // max(1, len(connectors)))
    processed = 0
    attempted = 0
    locked = 0
    cursor = after_connector
    for connector_id in connectors:
        # Allow one bounded attempt even when the candidate query consumed its
        # time budget. Rotate after every attempted connector so a saturated
        # first site cannot repeatedly consume the entire budget.
        if attempted and deadline is not None and clock() >= deadline:
            break
        attempted += 1
        connector = session.scalar(select(Connector).where(Connector.id == connector_id)
            .with_for_update(skip_locked=True))
        if connector is None:
            locked += 1
            cursor = connector_id
            continue
        # Both reads and scheduling state are under the connector lock. Recent
        # intake is a scheduling hint only, never terminal clock/profile proof.
        # Select bounded candidate pages, not all of a site's retained history.
        recent = session.scalars(select(ZktCustodyWork).where(
            ZktCustodyWork.connector_id == connector_id, ZktCustodyWork.next_attempt_at <= now,
            ZktCustodyWork.kind.in_([literal(kind, literal_execute=True) for kind in sorted(LIVE_KINDS)]),
            ZktCustodyWork.created_at >= now - timedelta(seconds=RECENT_LIVE_SECONDS),
            ZktCustodyWork.created_at <= now)
            .order_by(ZktCustodyWork.created_at.desc(), ZktCustodyWork.id.desc())
            .limit(quota).with_for_update(skip_locked=True)).all()
        oldest = _oldest_candidates(session, connector_id, now, quota)
        schedule = session.scalar(select(ZktCustodySchedule).where(ZktCustodySchedule.connector_id == connector_id))
        if schedule is None:
            schedule = ZktCustodySchedule(connector_id=connector_id, priority_burst=0)
            session.add(schedule)
        if not 0 <= schedule.priority_burst <= PRIORITY_BURST:
            raise RuntimeError("CUSTODY_SCHEDULE_INVALID")
        prior_processed = processed
        selected = set()
        recent_index = oldest_index = 0
        candidates = list({row.id: row for row in [*recent, *oldest]}.values())
        # The existing connector/work locks make this short-lived empty-history
        # proof stable. Avoid two metadata round trips for each brand-new packet.
        # Retried and corrected histories still execute their full chain checks.
        with derived.initial_evidence_batch(session, candidates):
            while len(selected) < quota:
                while recent_index < len(recent) and recent[recent_index].id in selected:
                    recent_index += 1
                while oldest_index < len(oldest) and oldest[oldest_index].id in selected:
                    oldest_index += 1
                has_recent, has_oldest = recent_index < len(recent), oldest_index < len(oldest)
                if not has_recent and not has_oldest:
                    break
                if processed and deadline is not None and clock() >= deadline:
                    break
                priority = has_recent and (schedule.priority_burst < PRIORITY_BURST or not has_oldest)
                row = recent[recent_index] if priority else oldest[oldest_index]
                inspect_work(session, row)
                schedule.priority_burst = min(PRIORITY_BURST, schedule.priority_burst + 1) if priority else 0
                schedule.updated_at = utc_now()
                selected.add(row.id)
                processed += 1
        if (recent or oldest) and processed == prior_processed:
            # Its lock/query used the remaining budget, but no group was
            # inspected. Keep it first next time rather than skipping it on
            # every full rotation through an even-sized saturated fleet.
            break
        cursor = connector_id
    session.flush()
    return InspectionBatch(processed, cursor, attempted, locked)


def advance_work(session: Session, *, limit: int = 100) -> int:
    """Compatibility helper for explicit bounded batch callers."""
    return advance_work_batch(session, limit=limit, time_budget_ms=None).processed


def backfill_work(session: Session, *, limit: int = 100) -> int:
    """Bounded idempotent repair for receipts predating the work-row contract.

    Missing-work detection is also an activation gate. The caller commits this
    local repair; no source or custody row is deleted or rewritten.
    """
    from zk_add.zkt_custody import digest

    maximum = max(1, min(limit, 500))
    rows = session.scalars(select(ZktObservationReceipt).where(~select(ZktCustodyWorkReceipt.id).where(
        ZktCustodyWorkReceipt.receipt_id == ZktObservationReceipt.id).exists())
        .order_by(ZktObservationReceipt.id).limit(maximum)).all()
    repaired = 0
    for receipt in rows:
        connector = session.scalar(select(Connector).where(Connector.id == receipt.connector_id)
                                   .with_for_update(skip_locked=True))
        if connector is None:
            continue
        if session.scalar(select(ZktCustodyWorkReceipt.id).where(
                ZktCustodyWorkReceipt.receipt_id == receipt.id)) is not None:
            continue
        value = decrypt_json(receipt.protected_observation).get("observation")
        if digest(value) != receipt.payload_digest:
            raise ValueError("RECEIPT_DIGEST_MISMATCH")
        attach_work(session, connector, receipt, value if isinstance(value, dict) else None)
        repaired += 1
    return repaired


def work_status(session: Session, connector: Connector, *, before: int | None = None, limit: int = 50) -> dict:
    maximum = max(1, min(limit, 100))
    counts = session.execute(select(ZktCustodyWork.state, ZktCustodyWork.owner, func.count())
        .where(ZktCustodyWork.connector_id == connector.id)
        .group_by(ZktCustodyWork.state, ZktCustodyWork.owner)).all()
    statement = select(ZktCustodyWork).where(ZktCustodyWork.connector_id == connector.id)
    if before is not None:
        statement = statement.where(ZktCustodyWork.id < before)
    rows = session.scalars(statement.order_by(ZktCustodyWork.id.desc()).limit(maximum + 1)).all()
    missing = session.scalar(select(ZktObservationReceipt.id).where(
        ZktObservationReceipt.connector_id == connector.id,
        ~select(ZktCustodyWorkReceipt.id).where(ZktCustodyWorkReceipt.receipt_id == ZktObservationReceipt.id).exists())
        .limit(1))
    source_missing = session.scalar(select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.connector_id == connector.id,
        TerminalRecordManifest.canonical_source.is_(True),
        TerminalRecordManifest.disposition == "RAW_PRESERVED",
        ~select(ZktCustodyWork.id).where(ZktCustodyWork.source_manifest_id == TerminalRecordManifest.id).exists())
        .limit(1))
    page = rows[:maximum]
    fingerprints = {row.id: derived.input_fingerprint(row) for row in page}
    latest = select(func.max(ZktDerivedEvidence.id).label("id")).where(
        ZktDerivedEvidence.work_id.in_([row.id for row in page])).group_by(ZktDerivedEvidence.work_id).subquery()
    # Metadata only: no ciphertext, user reference or punch time is loaded by
    # the ordinary device-status projection. It never constitutes assurance.
    evidence = {row.work_id: dict(version=row.interpretation_version, result=row.result,
        step_index=row.step_index, sampled_at=row.created_at, authority="UNQUALIFIED",
        current_input=row.input_fingerprint == fingerprints[row.work_id],
        current_decoder=row.interpretation_version == derived.INTERPRETATION_VERSION)
        for row in session.execute(select(ZktDerivedEvidence.work_id, ZktDerivedEvidence.interpretation_version,
            ZktDerivedEvidence.input_fingerprint, ZktDerivedEvidence.result,
            ZktDerivedEvidence.step_index, ZktDerivedEvidence.created_at)
            .join(latest, latest.c.id == ZktDerivedEvidence.id))}
    return {"connector_id": connector.connector_id, "enabled": connector.zkt_custody_enabled, "sampled_at": utc_now(),
        "oracle_completion": "NOT_ASSERTED", "missing_processing_obligation": missing is not None or source_missing is not None,
        "counts": [{"state": state, "owner": owner, "count": count} for state, owner, count in counts],
        "rows": [{"id": row.id, "kind": row.kind, "state": row.state, "reason_code": row.reason_code,
                  "source_manifest_id": row.source_manifest_id,
                  "owner": row.owner, "evidence_revision": row.evidence_revision,
                  "processed_revision": row.processed_revision, "attempt_count": row.attempt_count,
                  "decoding": evidence.get(row.id),
                  "expected_bytes": row.expected_bytes, "assembled_at": row.assembled_at,
                  "created_at": row.created_at, "updated_at": row.updated_at,
                  "next_attempt_at": row.next_attempt_at} for row in page],
        "next_cursor": page[-1].id if len(rows) > maximum and page else None}
