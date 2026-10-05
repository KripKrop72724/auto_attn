"""Journal observation custody. A receipt never asserts Oracle delivery.

The caller owns the transaction and connector row lock and sends the returned
ACK only after commit. No network or downstream work belongs in this boundary.
The receiver is disabled per connector until migration/reader gates are met.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session

from zk_add.crypto import encrypt_json
from zk_add.zkt_packet import parse_fragment
from zk_add.models import (AttendanceEvent, Connector, TerminalRecordManifest, TerminalSourceEpoch,
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


class SourceAssociationError(ValueError):
    """A derived source conflict must not roll back custody of unrelated items."""


def bind_manifest_occurrences(session: Session, connector: Connector,
                              manifests: list[TerminalRecordManifest]) -> dict[int, ZktOccurrenceAlias]:
    """Bind a bounded canonical source batch inside its custody transaction.

    The caller owns the connector lock and has authenticated the source bytes.
    Coordinates identify an occurrence before interpretation. An existing
    attendance link is retained, never inferred, replaced or released here.
    Validate the entire batch before inserting any new aliases; one corrupt
    retained alias must prevent a successful source-custody acknowledgement.
    """
    if len(manifests) > 100 or len({row.id for row in manifests}) != len(manifests):
        raise SourceAssociationError("SOURCE_OCCURRENCE_BATCH_BOUNDS")
    if not manifests:
        return {}
    terminal = connector.zkt_device
    epochs = {row.id: row for row in session.scalars(select(TerminalSourceEpoch).where(
        TerminalSourceEpoch.id.in_({row.source_epoch_id for row in manifests})))}
    identities = {}
    coordinates = []
    for row in manifests:
        epoch = epochs.get(row.source_epoch_id)
        if (connector.firmware_family != "zkt" or terminal is None or row.id is None
                or row.connector_id != connector.id or row.zkt_device_id != terminal.id
                or not row.canonical_source or not row.terminal_serial
                or row.terminal_serial != terminal.serial or row.terminal_serial != terminal.confirmed_serial
                or epoch is None or epoch.zkt_device_id != terminal.id
                or epoch.terminal_generation != row.generation
                or not isinstance(epoch.epoch_id, str)
                or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", epoch.epoch_id)
                or type(row.ordinal) is not int or not 0 <= row.ordinal < 2**31
                or not isinstance(row.raw_record_digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", row.raw_record_digest)):
            raise SourceAssociationError("SOURCE_OCCURRENCE_BINDING")
        identities[row.id] = occurrence_id(row.terminal_serial, epoch.epoch_id, row.ordinal, row.raw_record_digest)
        coordinates.append((row.zkt_device_id, row.source_epoch_id, row.ordinal))
    if len(set(coordinates)) != len(coordinates) or len(set(identities.values())) != len(identities):
        raise SourceAssociationError("SOURCE_OCCURRENCE_CONFLICT")
    aliases = list(session.scalars(select(ZktOccurrenceAlias).where(or_(
        ZktOccurrenceAlias.manifest_id.in_(identities),
        ZktOccurrenceAlias.occurrence_id.in_(identities.values()),
        tuple_(ZktOccurrenceAlias.zkt_device_id, ZktOccurrenceAlias.source_epoch_id,
               ZktOccurrenceAlias.ordinal).in_(coordinates),
    ))))
    by_manifest = {row.manifest_id: row for row in aliases}
    by_identity = {row.occurrence_id: row for row in aliases}
    by_coordinate = {(row.zkt_device_id, row.source_epoch_id, row.ordinal): row for row in aliases}
    result = {}
    pending = []
    for manifest, coordinate in zip(manifests, coordinates):
        identity = identities[manifest.id]
        candidates = [row for row in (by_manifest.get(manifest.id), by_identity.get(identity),
                                       by_coordinate.get(coordinate)) if row is not None]
        alias = candidates[0] if candidates else None
        if alias is not None:
            if (any(row.id != alias.id for row in candidates)
                    or (alias.manifest_id, alias.occurrence_id, alias.raw_digest,
                        alias.zkt_device_id, alias.source_epoch_id, alias.ordinal) !=
                       (manifest.id, identity, manifest.raw_record_digest, *coordinate)):
                raise SourceAssociationError("SOURCE_OCCURRENCE_CONFLICT")
        else:
            alias = ZktOccurrenceAlias(occurrence_id=identity, zkt_device_id=manifest.zkt_device_id,
                source_epoch_id=manifest.source_epoch_id, ordinal=manifest.ordinal,
                manifest_id=manifest.id, raw_digest=manifest.raw_record_digest,
                attendance_event_id=manifest.attendance_event_id)
            pending.append(alias)
        result[manifest.id] = alias
    session.add_all(pending)
    if pending:
        session.flush()
    return result


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
    exception_kind: Literal["METADATA", "FRAME", "AUTH", "TAIL", "CHECKPOINT"]
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
    if (not value.occurrence or not connector.zkt_device
            or connector.zkt_device.serial != value.terminal_serial
            or connector.zkt_device.confirmed_serial != value.terminal_serial):
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
    if manifest is None:
        return None
    if manifest.raw_record_digest != value.raw_digest:
        raise SourceAssociationError("SOURCE_RAW_DIGEST_MISMATCH")
    alias = bind_manifest_occurrences(session, connector, [manifest])[manifest.id]
    prior = session.scalar(select(ZktObservationLink).where(ZktObservationLink.receipt_id == receipt.id))
    if prior is None:
        session.add(ZktObservationLink(receipt_id=receipt.id, occurrence_alias_id=alias.id,
                                       proof_kind="EXACT_CANONICAL_SOURCE_BYTES"))
    elif prior.occurrence_alias_id != alias.id:
        raise SourceAssociationError("OBSERVATION_ALREADY_BOUND")
    return alias.occurrence_id


def occurrence_attendance_id(session: Session, connector: Connector, alias: ZktOccurrenceAlias) -> int | None:
    """Resolve an existing link without modifying an immutable raw manifest."""
    if alias.attendance_event_id is not None:
        return alias.attendance_event_id
    from zk_add.zkt_source_attendance import binding_event
    event = binding_event(session, connector, alias)
    return event.id if event else None


def source_occurrence_delivery_hold(session: Session, connector: Connector, identity: str) -> str | None:
    """Reject an ambiguous legacy delivery link, never mint a replacement UID.

    This is a negative check on existing links, not profile, employee or Oracle
    verification. Different ordinals in one source epoch are different source
    occurrences even when their raw bytes and legacy event UID are identical.
    Recovery copies in a later epoch do not alone establish that collision.
    """
    alias = session.scalar(select(ZktOccurrenceAlias).where(ZktOccurrenceAlias.occurrence_id == identity))
    manifest = session.get(TerminalRecordManifest, alias.manifest_id) if alias else None
    zkt = connector.zkt_device
    if (alias is None or manifest is None or zkt is None
            or alias.zkt_device_id != zkt.id or manifest.connector_id != connector.id
            or manifest.zkt_device_id != zkt.id or not manifest.canonical_source
            or manifest.terminal_serial != zkt.confirmed_serial
            or (alias.source_epoch_id, alias.ordinal, alias.raw_digest) !=
               (manifest.source_epoch_id, manifest.ordinal, manifest.raw_record_digest)):
        return "SOURCE_OCCURRENCE_LINK_CONFLICT"
    if alias.attendance_event_id != manifest.attendance_event_id:
        return "SOURCE_ATTENDANCE_LINK_CHANGED"
    from zk_add.zkt_source_attendance import SourceAttendanceHold
    try:
        event_id = occurrence_attendance_id(session, connector, alias)
    except (SourceAttendanceHold, ValueError, RuntimeError):
        return "SOURCE_DERIVED_ATTENDANCE_CHANGED"
    if event_id is None:
        return None  # Preserved exceptions have no attendance delivery claim.
    event = session.get(AttendanceEvent, event_id)
    if (event is None or event.connector_id != connector.id or event.zkt_device_id != zkt.id
            or event.device_serial != manifest.terminal_serial):
        return "SOURCE_ATTENDANCE_BINDING_UNVERIFIED"
    other = session.scalar(select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.attendance_event_id == event.id,
        TerminalRecordManifest.zkt_device_id == zkt.id,
        TerminalRecordManifest.source_epoch_id == alias.source_epoch_id,
        TerminalRecordManifest.canonical_source.is_(True),
        TerminalRecordManifest.ordinal != alias.ordinal,
    ).limit(1))
    return "LEGACY_EVENT_SHARED_BY_SOURCE_OCCURRENCES" if other is not None else None


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
        association_error = None
        if isinstance(parsed, Observation) and parsed.occurrence is not None and receipt.error_code is None:
            try:
                with session.begin_nested():
                    occurrence = bind_source_occurrence(session, connector, receipt, parsed)
            except SourceAssociationError as exc:
                association_error = str(exc)
        from zk_add.zkt_custody_work import attach_work
        work = attach_work(session, connector, receipt, raw if isinstance(raw, dict) else None)
        if association_error:
            work.state, work.reason_code, work.owner = "HELD_EXCEPTION", association_error, "ADD_EVIDENCE_REVIEW"
            work.next_attempt_at = None
        results.append({"index": index, "observation_id": identity,
                        "payload_digest": material_digest, "receipt_id": receipt.receipt_id,
                        "custody": receipt.disposition, "error_code": receipt.error_code,
                        "occurrence_id": occurrence, "replay": replay})
    session.flush()
    return {"schema_version": 1, "committed": True, "items": results,
            "delivery_authority": "ADD", "oracle_completion": "NOT_ASSERTED"}
