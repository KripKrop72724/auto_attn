"""Durable custody of unresolved firmware queue bytes, never identity approval."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class QueueEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    connector_id: str = Field(min_length=1, max_length=100)
    queue: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    queue_generation: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    record_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_:-]+$")
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_b64: str = Field(min_length=4, max_length=10924)
    provenance: EvidenceProvenance

    @model_validator(mode="after")
    def verify_bytes(self):
        try:
            raw = base64.b64decode(self.raw_b64, validate=True)
        except ValueError as exc:
            raise ValueError("Queue evidence requires valid base64") from exc
        if not 1 <= len(raw) <= 8192:
            raise ValueError("Queue evidence record must contain one to 8192 bytes")
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), self.payload_digest):
            raise ValueError("Queue evidence payload digest mismatch")
        self.raw_b64 = base64.b64encode(raw).decode("ascii")
        return self


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
