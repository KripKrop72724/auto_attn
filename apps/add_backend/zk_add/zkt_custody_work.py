"""Durable processing obligations attached in the custody transaction.

No employee attribution or Oracle completion is inferred here. Reassembly is
bounded; decoding waits for separately qualified profile evidence. Callers
serialize mutations using the connector row before the work row, including
when a new fragment arrives while a worker inspects its group.
"""
from __future__ import annotations

import base64
from datetime import timedelta
import hashlib
import json

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.crypto import decrypt_json
from zk_add.models import Connector, ZktCustodyWork, ZktCustodyWorkReceipt, ZktObservationReceipt
from zk_add.time_utils import utc_now
from zk_add.settings import settings
from zk_add.zkt_packet import FRAGMENT_DATA, PACKET_MAX, parse_fragment, reassemble


def key(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class EvidenceInvalid(ValueError):
    pass


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


def inspect_source(session: Session, work: ZktCustodyWork) -> None:
    """Associate late source evidence without manufacturing an attendance row."""
    from zk_add.zkt_custody import Observation, SourceAssociationError, bind_source_occurrence, digest

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
    if (receipt.error_code or receipt.connector_id != work.connector_id
            or parsed.raw_format != "SOURCE_RECORD" or parsed.terminal_serial != work.terminal_serial
            or parsed.capture_epoch != work.capture_epoch
            or (parsed.decoder_profile, parsed.decoder_version) != (work.decoder_profile, work.decoder_version)
            or len(base64.b64decode(parsed.raw_b64)) != work.expected_bytes
            or parsed.raw_digest != work.expected_digest):
        raise EvidenceInvalid("SOURCE_EVIDENCE_BINDING")
    connector = session.get(Connector, work.connector_id)
    terminal = connector.zkt_device if connector else None
    work.owner = "ADD_RECONCILIATION"
    if (terminal is None or terminal.serial != parsed.terminal_serial
            or terminal.confirmed_serial != parsed.terminal_serial):
        work.state, work.reason_code = "WAIT_SOURCE", "TERMINAL_BINDING_CHANGED"
        return
    if parsed.occurrence is None:
        work.state, work.reason_code = "WAIT_SOURCE", "SOURCE_REFERENCE_REQUIRED"
        return
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
        return
    work.state, work.reason_code = "SOURCE_ASSOCIATED", "EXACT_CANONICAL_SOURCE_BYTES"
    # Identity and downstream receipt verification are separate obligations.
    # An association alone cannot create attendance or mark Oracle complete.


def inspect_work(session: Session, work: ZktCustodyWork) -> None:
    """Reassembly is not qualification. Unqualified profiles remain a hold."""
    if work.state == "HELD_EXCEPTION":
        return
    work.next_attempt_at = None
    work.processed_revision = work.evidence_revision
    work.attempt_count += 1
    work.updated_at = utc_now()
    if work.kind not in {"SOURCE_RECORD", "LIVE_PACKET", "LIVE_FRAME", "PACKET_FRAGMENT"}:
        work.state, work.reason_code, work.owner = "WAIT_PROFILE", "UNKNOWN_RAW_FORMAT", "ADD_PROTOCOL"
        return
    try:
        if work.kind == "SOURCE_RECORD":
            inspect_source(session, work)
            return
        packet = materialize_packet(session, work)
    except EvidenceInvalid as exc:
        work.state, work.reason_code, work.owner = "HELD_EXCEPTION", str(exc), "ADD_EVIDENCE_REVIEW"
        return
    except (InvalidToken, ValueError, RuntimeError):
        # A key/configuration problem cannot be reclassified as invalid source
        # data. Keep a retry obligation; one unreadable receipt cannot block
        # later packets/devices. The original ciphertext remains untouched.
        work.state, work.reason_code, work.owner = "RETRY_SYSTEM", "CUSTODY_DECRYPT_UNAVAILABLE", "ADD_OPERATIONS"
        work.next_attempt_at = utc_now() + timedelta(seconds=60)
        return
    if packet is None:
        work.state, work.reason_code, work.owner = "WAIT_FRAGMENTS", "INCOMPLETE_PACKET", "ADD_PROTOCOL"
        return
    work.assembled_digest = hashlib.sha256(packet).hexdigest()
    work.assembled_at = utc_now()
    work.state, work.reason_code, work.owner = "WAIT_PROFILE", "PROFILE_QUALIFICATION_REQUIRED", "ADD_PROTOCOL"


def advance_work(session: Session, *, limit: int = 100) -> int:
    """Fair bounded inspection; connector-first locks match custody ingestion.

    This stage deliberately stops at profile qualification. It never emits an
    attendance event or an Oracle outbox based on an unqualified interpretation.
    """
    maximum = max(1, min(limit, 500))
    now = utc_now()
    due = select(ZktCustodyWork.id).where(ZktCustodyWork.connector_id == Connector.id,
        ZktCustodyWork.next_attempt_at <= now).exists()
    connectors = session.scalars(select(Connector.id).where(Connector.zkt_custody_enabled.is_(True), due)
        .order_by(Connector.id).limit(maximum)).all()
    quota = max(1, maximum // max(1, len(connectors)))
    processed = 0
    for connector_id in connectors:
        connector = session.scalar(select(Connector).where(Connector.id == connector_id)
            .with_for_update(skip_locked=True))
        if connector is None:
            continue
        work = session.scalars(select(ZktCustodyWork).where(ZktCustodyWork.connector_id == connector_id,
            ZktCustodyWork.next_attempt_at <= now).order_by(ZktCustodyWork.next_attempt_at, ZktCustodyWork.id)
            .limit(quota).with_for_update(skip_locked=True)).all()
        for row in work:
            inspect_work(session, row)
            processed += 1
    session.flush()
    return processed


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
    page = rows[:maximum]
    return {"connector_id": connector.connector_id, "enabled": connector.zkt_custody_enabled, "sampled_at": utc_now(),
        "oracle_completion": "NOT_ASSERTED", "missing_processing_obligation": missing is not None,
        "counts": [{"state": state, "owner": owner, "count": count} for state, owner, count in counts],
        "rows": [{"id": row.id, "kind": row.kind, "state": row.state, "reason_code": row.reason_code,
                  "owner": row.owner, "evidence_revision": row.evidence_revision,
                  "processed_revision": row.processed_revision, "attempt_count": row.attempt_count,
                  "expected_bytes": row.expected_bytes, "assembled_at": row.assembled_at,
                  "created_at": row.created_at, "updated_at": row.updated_at,
                  "next_attempt_at": row.next_attempt_at} for row in page],
        "next_cursor": page[-1].id if len(rows) > maximum and page else None}
