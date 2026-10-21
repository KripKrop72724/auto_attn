"""Durable custody of unresolved firmware queue bytes, never identity approval."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.crypto import encrypt_json
from zk_add.models import Connector, QueueEvidence


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
    """
    payload = payload if isinstance(payload, dict) else {}
    facts: dict = {}
    for name, limit, pattern in IDENTITY_FIELDS:
        value = payload.get(name)
        facts[name] = value if (isinstance(value, str) and 1 <= len(value) <= limit
                                and re.fullmatch(pattern, value)) else None
    claimed = payload.get("payload_digest")
    facts["claimed_digest"] = claimed if isinstance(claimed, str) and re.fullmatch(DIGEST_PATTERN, claimed) else None
    facts["received_digest"] = facts["received_bytes"] = None
    encoded = payload.get("raw_b64")
    if isinstance(encoded, str) and 4 <= len(encoded) <= MAX_RAW_B64:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raw = None
        if raw is not None:
            facts["received_digest"] = hashlib.sha256(raw).hexdigest()
            facts["received_bytes"] = len(raw)
    return facts


def evidence_identity(facts: dict | None) -> tuple[str, str, str] | None:
    """(queue, queue_generation, record_id) when all three are known."""
    if not isinstance(facts, dict):
        return None
    identity = tuple(facts.get(name) for name, _limit, _pattern in IDENTITY_FIELDS)
    return identity if all(isinstance(part, str) and part for part in identity) else None


def preserve_queue_evidence(session: Session, connector: Connector, request: QueueEvidenceRequest) -> QueueEvidence:
    if request.connector_id != connector.connector_id:
        raise ValueError("Queue evidence connector ownership mismatch")
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
        raise ValueError("Queue evidence identity was reused with different bytes or provenance")
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
