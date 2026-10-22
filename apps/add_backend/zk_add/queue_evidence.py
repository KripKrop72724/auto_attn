"""Durable custody of unresolved firmware queue bytes, never identity approval."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
import zlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.crypto import encrypt_json
from zk_add.models import Connector, QueueEvidence
from zk_add.rejection import EvidenceRejected


class EvidenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terminal_serial: str | None = Field(default=None, max_length=120)
    source_generation: int | None = Field(default=None, ge=0, le=2**32 - 1)
    source_offset: int | None = Field(default=None, ge=0, le=2**63 - 1)
    reason: Literal["MALFORMED", "IDENTITY_UNRESOLVED", "IDENTITY_CONFLICT", "TIMESTAMP_INVALID", "LEGACY_RECOVERY"]
    encoding: Literal["JSON", "LEGACY_ROW", "RAW_RECORD"]


# The custody identity: (field, longest value, pattern). Shared by the schema
# and by refused_copy(), so a refusal records only values the schema accepts.
IDENTITY_FIELDS = (
    ("queue", 40, r"^[a-z0-9_]+$"),
    ("queue_generation", 80, r"^[a-zA-Z0-9_-]+$"),
    ("record_id", 120, r"^[a-zA-Z0-9_:-]+$"),
)
DIGEST_PATTERN = r"^[a-f0-9]{64}$"
MAX_RAW_B64 = 10924
MAX_RECORD_BYTES = 8192
# Why a copy was refused, recorded as the rejection reason. The device gets
# the same MESSAGE_REJECTED for each, so every firmware generation behaves
# exactly as before; only operators learn which check failed.
BASE64_INVALID = "QUEUE_EVIDENCE_BASE64_INVALID"
LENGTH_INVALID = "QUEUE_EVIDENCE_LENGTH_INVALID"
DIGEST_MISMATCH = "QUEUE_EVIDENCE_DIGEST_MISMATCH"
# preserve_queue_evidence() refusals of a copy whose bytes did verify.
IDENTITY_REUSED = "QUEUE_EVIDENCE_IDENTITY_REUSED"
CONNECTOR_MISMATCH = "QUEUE_EVIDENCE_CONNECTOR_MISMATCH"
# The device and ADD disagree about what this record is: ADD already holds
# other bytes or provenance under its identity. Nothing ADD accepts later can
# show which one the device meant, so only an operator resolves it.
OPERATOR_ONLY_REASONS = frozenset({IDENTITY_REUSED})


def _identity_field(name: str):
    limit, pattern = next((limit, pattern) for field, limit, pattern in IDENTITY_FIELDS if field == name)
    return Field(min_length=1, max_length=limit, pattern=pattern)


class QueueEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    connector_id: str = Field(min_length=1, max_length=100)
    queue: str = _identity_field("queue")
    queue_generation: str = _identity_field("queue_generation")
    record_id: str = _identity_field("record_id")
    payload_digest: str = Field(pattern=DIGEST_PATTERN)
    raw_b64: str = Field(min_length=4, max_length=MAX_RAW_B64)
    provenance: EvidenceProvenance

    @model_validator(mode="after")
    def verify_bytes(self):
        # Each failure is a PydanticCustomError whose type is a reason code;
        # rejection.rejection_reason() records it. Never the bytes themselves.
        try:
            raw = base64.b64decode(self.raw_b64, validate=True)
        except ValueError as exc:
            raise PydanticCustomError(BASE64_INVALID, "Queue evidence requires valid base64") from exc
        if not 1 <= len(raw) <= MAX_RECORD_BYTES:
            raise PydanticCustomError(LENGTH_INVALID, "Queue evidence record must contain one to 8192 bytes")
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), self.payload_digest):
            raise PydanticCustomError(DIGEST_MISMATCH, "Queue evidence payload digest mismatch")
        self.raw_b64 = base64.b64encode(raw).decode("ascii")
        return self


def refused_copy(payload: object) -> dict:
    """What ADD records about a queue_evidence copy it refused.

    The custody identity and two digests, each only when well-formed, so a
    damaged value is never echoed; never the bytes or the provenance. The
    identity is what a later acceptance must match before the refusal is
    considered held. The claimed digest (what the ESP hashed) and the received
    digest (what arrived) show, once the honest copy is held, whether the
    bytes or the digest changed. Both are SHA-256 values of the kind ADD
    already lists for every held record (GET /devices/{id}/queue-evidence).
    The CRC-32 of what arrived is checked against a legacy record id, which
    ends with the CRC-32 of its row (lq_peek), to show those bytes are that row.
    """
    payload = payload if isinstance(payload, dict) else {}
    facts: dict = {}
    for name, limit, pattern in IDENTITY_FIELDS:
        value = payload.get(name)
        facts[name] = value if (isinstance(value, str) and 1 <= len(value) <= limit
                                and re.fullmatch(pattern, value)) else None
    claimed = payload.get("payload_digest")
    facts["claimed_digest"] = claimed if isinstance(claimed, str) and re.fullmatch(DIGEST_PATTERN, claimed) else None
    facts["received_digest"] = facts["received_bytes"] = facts["received_crc32"] = None
    encoded = payload.get("raw_b64")
    if isinstance(encoded, str) and 4 <= len(encoded) <= MAX_RAW_B64:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raw = None
        if raw is not None:
            facts["received_digest"] = hashlib.sha256(raw).hexdigest()
            facts["received_bytes"] = len(raw)
            facts["received_crc32"] = zlib.crc32(raw)
    return facts


def evidence_identity(facts: dict | None) -> tuple[str, str, str] | None:
    """(queue, queue_generation, record_id) when all three are known."""
    if not isinstance(facts, dict):
        return None
    identity = tuple(facts.get(name) for name, _limit, _pattern in IDENTITY_FIELDS)
    return identity if all(isinstance(part, str) and part for part in identity) else None


def _digest(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(DIGEST_PATTERN, value) else None


def _names_received_bytes(refused: dict) -> bool:
    """Whether the refused copy's record id ends with the CRC-32 of the bytes that arrived.

    Legacy-lane record ids are "offset:crc" or "0:offset:crc", where crc is
    dq_crc32 (zlib's CRC-32) of exactly the row sent. Segmented and ORDS
    segment ids ("segment:offset:sequence") carry no CRC and never match.
    """
    crc = refused.get("received_crc32")
    record_id = refused.get("record_id")
    return (type(crc) is int and isinstance(record_id, str) and ":" in record_id
            and record_id.rsplit(":", 1)[1] == str(crc))


def holds_refused_copy(refused: dict, held: dict | None) -> tuple[str, str] | None:
    """(match, finding) when the held record accounts for one refused copy, else None.

    ``refused`` is a pending copy: its identity, the digest the ESP claimed and
    the digest of the bytes that arrived. ``held`` is a record ADD holds: its
    identity and verified payload_digest. Only two things show that ADD now
    holds what the refused copy carried:

    - SAME_IDENTITY: ADD holds that identity with the claimed digest (the bytes
      the ESP hashed) or the received digest (the bytes that arrived). Holding
      the identity with other bytes proves nothing about this copy.
    - SAME_BYTES: ADD holds, under another generation of the same queue (a
      re-blocked row gets a new generation), the claimed digest, or the
      received digest under the same record id when that record id names the
      bytes that arrived (_names_received_bytes). The received digest alone
      says what arrived, not which record it was, so it may be another
      record's bytes; a record id that carries their CRC-32 shows they are
      that record, re-sent after its digest was damaged.

    The finding says what was damaged: COPY_INTACT (nothing; ADD failed or
    refused for another reason), SENT_BYTES_CHANGED or DIGEST_CHANGED. A copy
    whose bytes and digest both differ from the held record is never held.
    """
    if not isinstance(refused, dict) or not isinstance(held, dict):
        return None
    if refused.get("reason") in OPERATOR_ONLY_REASONS:
        return None
    refused_identity, held_identity = evidence_identity(refused), evidence_identity(held)
    digest = _digest(held.get("payload_digest"))
    claimed, received = _digest(refused.get("claimed_digest")), _digest(refused.get("received_digest"))
    if refused_identity is None or held_identity is None or digest is None:
        return None
    if held_identity == refused_identity and digest in {claimed, received}:
        match = "SAME_IDENTITY"
    elif (held_identity[0] == refused_identity[0] and held_identity[1] != refused_identity[1]
          and (digest == claimed or (digest == received and held_identity[2] == refused_identity[2]
                                     and _names_received_bytes(refused)))):
        match = "SAME_BYTES"
    else:
        return None
    if digest == claimed == received:
        return match, "COPY_INTACT"
    return match, "SENT_BYTES_CHANGED" if digest == claimed else "DIGEST_CHANGED"


def held_record(row: QueueEvidence) -> dict:
    """What a refused copy is matched against: never the bytes."""
    return {"queue": row.queue, "queue_generation": row.queue_generation, "record_id": row.record_id,
            "payload_digest": row.payload_digest, "receipt_id": row.receipt_id, "byte_count": row.byte_count}


def find_held_copy(session: Session, connector: Connector,
                   refused: dict) -> tuple[dict, tuple[str, str]] | None:
    """A record ADD already holds that accounts for a copy refused just now.

    Sockets overlap on reconnect, so the honest copy can commit and be
    acknowledged before the refusal of a damaged one is recorded; the device
    then never sends that record again. The caller holds the connector lock,
    as every acceptance does.
    """
    identity = evidence_identity(refused)
    if identity is None or refused.get("reason") in OPERATOR_ONLY_REASONS:
        return None
    candidates = [session.scalar(select(QueueEvidence).where(
        QueueEvidence.connector_id == connector.id, QueueEvidence.queue == identity[0],
        QueueEvidence.queue_generation == identity[1], QueueEvidence.record_id == identity[2]))]
    claimed = _digest(refused.get("claimed_digest"))
    if claimed is not None:
        candidates.append(session.scalar(select(QueueEvidence).where(
            QueueEvidence.connector_id == connector.id, QueueEvidence.queue == identity[0],
            QueueEvidence.queue_generation != identity[1], QueueEvidence.payload_digest == claimed,
        ).order_by(QueueEvidence.id).limit(1)))
    received = _digest(refused.get("received_digest"))
    if received is not None and received != claimed and _names_received_bytes(refused):
        candidates.append(session.scalar(select(QueueEvidence).where(
            QueueEvidence.connector_id == connector.id, QueueEvidence.queue == identity[0],
            QueueEvidence.queue_generation != identity[1], QueueEvidence.record_id == identity[2],
            QueueEvidence.payload_digest == received,
        ).order_by(QueueEvidence.id).limit(1)))
    for row in candidates:
        if row is not None:
            held = held_record(row)
            result = holds_refused_copy(refused, held)
            if result is not None:
                return held, result
    return None


def unheld_evidence(entry: object) -> tuple[int, int]:
    """(refused copies ADD does not hold yet, refusals it cannot match) in one rejection entry.

    Refusals recorded before identities were tracked (``legacy_refusals``)
    are not counted: they keep the rule they were written under.
    """
    if not isinstance(entry, dict):
        return 0, 0
    pending = entry.get("pending")
    return (len([item for item in pending if isinstance(item, dict)]) if isinstance(pending, list) else 0,
            int(entry.get("untracked_refusals") or 0))


# Set on a rejection entry when ADD accepts a queue_evidence after its latest
# refusal without holding every refused copy; a new refusal removes it. Before
# identities were tracked, that acceptance closed the rejection. What is left
# stays open for an operator to verify on the device, but like then it no
# longer holds HIL or factory work (device_health).
LATER_ACCEPTANCE = "later_acceptance_at"


def custody_only(entry: object) -> bool:
    """Whether a rejection entry is only refused copies ADD does not hold, left after a later acceptance."""
    return (isinstance(entry, dict) and isinstance(entry.get(LATER_ACCEPTANCE), str)
            and unheld_evidence(entry) != (0, 0))


def preserve_queue_evidence(session: Session, connector: Connector, request: QueueEvidenceRequest) -> QueueEvidence:
    if request.connector_id != connector.connector_id:
        raise EvidenceRejected(CONNECTOR_MISMATCH, "Queue evidence connector ownership mismatch")
    provenance = request.provenance.model_dump()
    provenance_digest = hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    scope = select(QueueEvidence).where(
        QueueEvidence.connector_id == connector.id,
        QueueEvidence.queue == request.queue,
        QueueEvidence.queue_generation == request.queue_generation,
        QueueEvidence.record_id == request.record_id,
    )
    existing = session.scalar(scope)
    if existing is None:
        row = QueueEvidence(
            receipt_id=str(uuid.uuid4()), connector_id=connector.id,
            queue=request.queue, queue_generation=request.queue_generation,
            record_id=request.record_id, payload_digest=request.payload_digest,
            provenance_digest=provenance_digest,
            byte_count=len(base64.b64decode(request.raw_b64)),
            protected_evidence=encrypt_json({"raw_b64": request.raw_b64, "provenance": provenance}),
            disposition="PRESERVED_UNRESOLVED",
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
            return row
        except IntegrityError:
            # Concurrent replay may have committed the same custody record.
            existing = session.scalar(scope)
            if existing is None:
                raise
    if (existing.payload_digest != request.payload_digest or
            existing.provenance_digest != provenance_digest):
        raise EvidenceRejected(IDENTITY_REUSED,
                               "Queue evidence identity was reused with different bytes or provenance")
    return existing


def evidence_ack(row: QueueEvidence, connector: Connector, message_id: str) -> dict:
    # The WebSocket handler sends this only after its transaction commits.
    return {
        "type": "queue_evidence_ack", "schema_version": 1,
        "message_id": message_id, "connector_id": connector.connector_id,
        "queue": row.queue, "queue_generation": row.queue_generation,
        "record_id": row.record_id, "payload_digest": row.payload_digest,
        "receipt_id": row.receipt_id, "disposition": row.disposition,
    }
