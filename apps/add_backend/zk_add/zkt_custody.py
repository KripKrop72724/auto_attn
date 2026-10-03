"""Journal observation custody. A receipt never asserts Oracle delivery.

The caller owns the transaction and connector row lock and sends the returned
ACK only after commit. No network or downstream work belongs in this boundary.
The receiver is disabled per connector until migration/reader gates are met.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.crypto import encrypt_json
from zk_add.zkt_packet import parse_fragment
from zk_add.models import (Connector, TerminalRecordManifest, TerminalSourceEpoch,
                           ZktObservationReceipt, ZktOccurrenceAlias, ZktObservationLink)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def observation_id(serial: str, epoch: str, sequence: int) -> str:
    return digest(["zkt-observation-v1", serial, epoch, sequence])


def occurrence_id(serial: str, epoch: str, ordinal: int, raw_digest: str) -> str:
    return digest(["zkt-occurrence-v1", serial, epoch, ordinal, raw_digest])


def journal_exception_id(serial: str, epoch: str, segment: int,
                         start: int, end: int, raw_digest: str) -> str:
    return digest(["zkt-journal-exception-v1", serial, epoch, segment, start, end, raw_digest])


class OccurrenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_epoch: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    ordinal: int = Field(ge=0, le=2**31 - 1)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    terminal_serial: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    capture_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    capture_sequence: int = Field(ge=1, le=2**63 - 1)
    captured_at: AwareDatetime | None
    captured_at_seconds: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]{0,18})$")
    # Decimal text remains exact through cJSON's binary64 number representation.
    captured_uptime_ms: str | None = Field(default=None, pattern=r"^(0|[1-9][0-9]{0,19})$")
    raw_b64: str = Field(min_length=4, max_length=684)
    raw_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_format: Literal["LIVE_FRAME", "SOURCE_RECORD", "UNKNOWN", "LIVE_PACKET", "PACKET_FRAGMENT"]
    decoder_profile: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    decoder_version: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_.:-]+$")
    time_quality: Literal["VERIFIED", "UNSYNCED", "INVALID", "UNKNOWN"]
    encoded_time: int | None = Field(default=None, ge=0, le=2**32 - 1)
    identity_snapshot: str | None = Field(default=None, max_length=120, pattern=r"^[a-zA-Z0-9_.:-]+$")
    occurrence: OccurrenceReference | None = None

    @model_validator(mode="after")
    def verify(self):
        raw = base64.b64decode(self.raw_b64, validate=True)
        if not 1 <= len(raw) <= 512 or hashlib.sha256(raw).hexdigest() != self.raw_digest:
            raise ValueError("RAW_DIGEST_MISMATCH")
        if self.observation_id != observation_id(self.terminal_serial, self.capture_epoch, self.capture_sequence):
            raise ValueError("CAPTURE_IDENTITY_MISMATCH")
        if self.raw_format == "PACKET_FRAGMENT":
            parse_fragment(raw)
        if self.occurrence and self.raw_format != "SOURCE_RECORD":
            # Live frames have different wire bytes. A guessed ordinal or a
            # matching timestamp cannot prove the one-to-one association.
            raise ValueError("SOURCE_REFERENCE_REQUIRES_SOURCE_BYTES")
        if self.captured_uptime_ms is not None and int(self.captured_uptime_ms) > 2**64 - 1:
            raise ValueError("UPTIME_OUT_OF_RANGE")
        if self.captured_at_seconds is not None:
            seconds = int(self.captured_at_seconds)
            if seconds > 2**63 - 1:
                raise ValueError("CAPTURE_TIME_OUT_OF_RANGE")
            if self.captured_at is not None and self.captured_at.timestamp() != seconds:
                raise ValueError("CAPTURE_TIME_REPRESENTATION_MISMATCH")
        elif self.captured_at is None:
            raise ValueError("CAPTURE_TIME_EVIDENCE_REQUIRED")
        return self


class JournalException(BaseModel):
    """Opaque local bytes cannot be reclassified as an attendance observation.

    Their identity belongs to a precise retained extent, including the bytes'
    digest. Preserving them acknowledges custody only, never Oracle completion.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_type: Literal["JOURNAL_EXCEPTION"]
    observation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    terminal_serial: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    capture_epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    segment_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    start_offset: int = Field(ge=0, le=2**32 - 1)
    end_offset: int = Field(ge=1, le=2**32 - 1)
    exception_kind: Literal["METADATA", "FRAME", "AUTH", "TAIL"]
    raw_b64: str = Field(min_length=4, max_length=684)
    raw_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def verify(self):
        raw = base64.b64decode(self.raw_b64, validate=True)
        if (not 1 <= len(raw) <= 512 or len(raw) != self.end_offset - self.start_offset
                or hashlib.sha256(raw).hexdigest() != self.raw_digest):
            raise ValueError("JOURNAL_EXTENT_MISMATCH")
        segment = int(self.segment_id)
        if segment > 2**63 - 1:
            raise ValueError("JOURNAL_SEGMENT_OUT_OF_RANGE")
        if self.observation_id != journal_exception_id(self.terminal_serial, self.capture_epoch,
                segment, self.start_offset, self.end_offset, self.raw_digest):
            raise ValueError("JOURNAL_EXCEPTION_IDENTITY_MISMATCH")
        return self


def bind_source_occurrence(session: Session, connector: Connector,
                           receipt: ZktObservationReceipt, value: Observation) -> str | None:
    """Bind only to committed, canonical source evidence with matching bytes.

    This handles exact source replay. Ambiguous live/history semantic matching
    is deliberately not inferred from timestamp, enrollment UID or employee ID.
    """
    if not value.occurrence or not connector.zkt_device:
        return None
    zkt = connector.zkt_device
    manifest = session.scalar(select(TerminalRecordManifest).join(TerminalSourceEpoch).where(
        TerminalRecordManifest.zkt_device_id == zkt.id,
        TerminalRecordManifest.connector_id == connector.id,
        TerminalRecordManifest.terminal_serial == value.terminal_serial,
        TerminalRecordManifest.canonical_source.is_(True),
        TerminalSourceEpoch.epoch_id == value.occurrence.source_epoch,
        TerminalSourceEpoch.zkt_device_id == zkt.id,
        TerminalRecordManifest.ordinal == value.occurrence.ordinal,
    ))
    if manifest is None or manifest.raw_record_digest != value.raw_digest:
        return None
    identity = occurrence_id(value.terminal_serial, value.occurrence.source_epoch,
                             value.occurrence.ordinal, value.raw_digest)
    alias = session.scalar(select(ZktOccurrenceAlias).where(
        ZktOccurrenceAlias.zkt_device_id == zkt.id,
        ZktOccurrenceAlias.source_epoch_id == manifest.source_epoch_id,
        ZktOccurrenceAlias.ordinal == manifest.ordinal,
    ))
    if alias is None:
        alias = ZktOccurrenceAlias(occurrence_id=identity, zkt_device_id=zkt.id,
                                  source_epoch_id=manifest.source_epoch_id, ordinal=manifest.ordinal,
                                  manifest_id=manifest.id, raw_digest=value.raw_digest,
                                  attendance_event_id=manifest.attendance_event_id)
        session.add(alias)
        session.flush()
    elif alias.occurrence_id != identity or alias.manifest_id != manifest.id:
        raise ValueError("SOURCE_OCCURRENCE_CONFLICT")
    prior = session.scalar(select(ZktObservationLink).where(ZktObservationLink.receipt_id == receipt.id))
    if prior is None:
        session.add(ZktObservationLink(receipt_id=receipt.id, occurrence_alias_id=alias.id,
                                       proof_kind="EXACT_CANONICAL_SOURCE_BYTES"))
    elif prior.occurrence_alias_id != alias.id:
        raise ValueError("OBSERVATION_ALREADY_BOUND")
    return identity


def settle_observations(session: Session, connector: Connector, payload: dict) -> dict:
    if connector.firmware_family != "zkt" or not connector.zkt_custody_enabled:
        raise ValueError("ZKT_CUSTODY_NOT_ENABLED")
    if (set(payload) != {"schema_version", "observations"} or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1 or not isinstance(payload["observations"], list)
            or not 1 <= len(payload["observations"]) <= 64):
        raise ValueError("INVALID_CUSTODY_BATCH")
    # Bound encrypted quarantine too, including malformed values. Reject a
    # transport violation without an ACK so the sender retains its journal.
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 96 * 1024:
        raise ValueError("CUSTODY_BATCH_TOO_LARGE")
    results = []
    for index, raw in enumerate(payload["observations"]):
        material_digest = digest(raw)
        parsed = None
        error = None
        try:
            kind = raw.get("item_type") if isinstance(raw, dict) else None
            parsed = JournalException.model_validate(raw) if kind == "JOURNAL_EXCEPTION" else Observation.model_validate(raw)
            if isinstance(parsed, JournalException):
                error = f"JOURNAL_{parsed.exception_kind}_EXCEPTION"
            zkt = connector.zkt_device
            if (zkt is None or parsed.terminal_serial != zkt.serial
                    or parsed.terminal_serial != zkt.confirmed_serial):
                error = "TERMINAL_BINDING_MISMATCH"
        except ValidationError:
            error = "OBSERVATION_SCHEMA_INVALID"
        identity = parsed.observation_id if parsed else material_digest
        receipt = session.scalar(select(ZktObservationReceipt).where(
            ZktObservationReceipt.connector_id == connector.id,
            ZktObservationReceipt.observation_id == identity,
            ZktObservationReceipt.payload_digest == material_digest,
        ))
        replay = receipt is not None
        if receipt is None:
            conflict = session.scalar(select(ZktObservationReceipt.id).where(
                ZktObservationReceipt.connector_id == connector.id,
                ZktObservationReceipt.observation_id == identity,
            ).limit(1))
            if conflict is not None:
                error = "CAPTURE_IDENTITY_REUSED"
            receipt = ZktObservationReceipt(
                connector_id=connector.id, observation_id=identity, payload_digest=material_digest,
                raw_digest=parsed.raw_digest if parsed else None,
                terminal_serial=parsed.terminal_serial if parsed else None,
                capture_epoch=parsed.capture_epoch if parsed else None,
                capture_sequence=parsed.capture_sequence if isinstance(parsed, Observation) else None,
                decoder_profile=parsed.decoder_profile if isinstance(parsed, Observation) else None,
                decoder_version=parsed.decoder_version if isinstance(parsed, Observation) else None,
                protected_observation=encrypt_json({"observation": raw}),
                disposition="PRESERVED_EXCEPTION" if error else "PRESERVED_UNRESOLVED",
                error_code=error,
            )
            session.add(receipt)
            session.flush()
        # Stable custody does not change on replay. Derived associations are
        # separate evidence and may become available after a source scan.
        occurrence = None
        if isinstance(parsed, Observation) and receipt.error_code is None:
            occurrence = bind_source_occurrence(session, connector, receipt, parsed)
        from zk_add.zkt_custody_work import attach_work
        attach_work(session, connector, receipt, raw if isinstance(raw, dict) else None)
        results.append({"index": index, "observation_id": identity,
                        "payload_digest": material_digest, "receipt_id": receipt.receipt_id,
                        "custody": receipt.disposition, "error_code": receipt.error_code,
                        "occurrence_id": occurrence, "replay": replay})
    session.flush()
    return {"schema_version": 1, "committed": True, "items": results,
            "delivery_authority": "ADD", "oracle_completion": "NOT_ASSERTED"}
