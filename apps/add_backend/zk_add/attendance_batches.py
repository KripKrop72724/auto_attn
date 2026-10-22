from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import case, event, func, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.models import (
    AttendanceBatchItem,
    AttendanceBatchReceipt,
    AttendanceEvent,
    AuditEvent,
    Connector,
)
from zk_add.schemas import AttendanceEventIn
from zk_add.service import (
    ingest_attendance,
    resolve_alert,
    resolve_message_rejection,
    upsert_alert,
)
from zk_add.time_utils import utc_now


MAX_BATCH_EVENTS = 100
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

logger = logging.getLogger(__name__)

# Every ZKT firmware generation (2.4.12 through 2.7.6) writes the moment it
# serialised a row into ``captured_at``. Reconcile batches are named after
# their first punch ("truth-<event_uid>") and are rebuilt on every truth pass,
# so a re-send of the same punches under the same batch id differs from the
# settled batch in this one field.
CAPTURE_VOLATILE_FIELDS = frozenset({"captured_at"})
_CONTENT_DIGEST_SCHEMA = "attendance-batch-content-2"
_CAPTURE_TIME = TypeAdapter(datetime)
# Receipts committed before content digests existed are compared through their
# own encrypted evidence; only the newest few are read per replay.
_LEGACY_COMPARE_LIMIT = 3
# Stored on a pre-digest receipt whose evidence cannot be rebuilt exactly, so
# it is read once and never again. It can never equal a 64-hex digest.
CONTENT_DIGEST_UNAVAILABLE = "UNAVAILABLE"
_COMMITTED_OUTCOMES = ("COMMITTED", "COMMITTED_WITH_DUPLICATES")
REPLAY_LOG_INTERVAL_SECONDS = 300.0


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        default=_json_default,
    )


def payload_digest(events: object) -> str:
    return hashlib.sha256(_canonical_json(events).encode("utf-8")).hexdigest()


def _item_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _well_formed_capture_time(value: object) -> bool:
    """The same rule AttendanceEventIn applies to ``captured_at``."""

    if not isinstance(value, str):
        return False
    try:
        _CAPTURE_TIME.validate_python(value)
    except ValidationError:
        return False
    return True


def _content_row(item: object) -> object:
    if not isinstance(item, dict):
        return item
    # Only a well-formed send time is ignored. A missing key stays missing and
    # a malformed value stays verbatim, so neither matches a well-formed send;
    # both shapes are wrapped so no device value can imitate the marker.
    return {
        key: (
            (["WELL_FORMED"] if _well_formed_capture_time(value) else ["AS_SENT", value])
            if key in CAPTURE_VOLATILE_FIELDS
            else value
        )
        for key, value in item.items()
    }


def replay_content_digest(events: object) -> str | None:
    """Digest a batch's events without the send time firmware rewrites per send.

    Used only to recognise a replay of a settled batch. ``captured_at`` is
    still validated and stored for every row that is ingested.
    """

    if not isinstance(events, list):
        return None
    return payload_digest(
        {
            "schema": _CONTENT_DIGEST_SCHEMA,
            "events": [_content_row(item) for item in events],
        }
    )


def _batch_ref(batch_id: str) -> str:
    # Batch ids are device-supplied text (CNIC-shaped values and newlines are
    # possible); logs carry a short digest instead.
    return hashlib.sha256(batch_id.encode("utf-8", "replace")).hexdigest()[:16]


class _ReplayMeter:
    """Per-connector replay counters with at most one log line per interval."""

    def __init__(self, interval_seconds: float = REPLAY_LOG_INTERVAL_SECONDS) -> None:
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._totals: dict[str, dict[str, int]] = {}
        self._window: dict[str, dict[str, int]] = {}
        self._last_logged: dict[str, float] = {}

    def record(
        self,
        connector_id: str,
        kind: str,
        *,
        batch_id: str,
        now: float | None = None,
    ) -> bool:
        observed = time.monotonic() if now is None else now
        with self._lock:
            totals = self._totals.setdefault(connector_id, {})
            totals[kind] = totals.get(kind, 0) + 1
            window = self._window.setdefault(connector_id, {})
            window[kind] = window.get(kind, 0) + 1
            last = self._last_logged.get(connector_id)
            if last is not None and observed - last < self.interval_seconds:
                return False
            self._last_logged[connector_id] = observed
            counts = dict(sorted(window.items()))
            self._window[connector_id] = {}
            lifetime = dict(sorted(totals.items()))
        logger.warning(
            "Attendance batch replays connector=%s interval_seconds=%d "
            "counts=%s process_totals=%s last_batch_ref=%s",
            connector_id,
            int(self.interval_seconds),
            counts,
            lifetime,
            _batch_ref(batch_id),
        )
        return True

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                connector_id: dict(counts)
                for connector_id, counts in self._totals.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._totals.clear()
            self._window.clear()
            self._last_logged.clear()


replay_meter = _ReplayMeter()
_PENDING_REPLAYS = "zk_add.attendance_batch_replays"


def _count_after_commit(
    session: Session, connector: Connector, kind: str, batch_id: str
) -> None:
    """Count a settlement only once its transaction has committed."""

    session.info.setdefault(_PENDING_REPLAYS, []).append(
        (connector.connector_id, kind, batch_id)
    )


@event.listens_for(Session, "after_commit")
def _record_committed_replays(session: Session) -> None:
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_PENDING_REPLAYS, None)
    if not pending:
        return
    try:
        for connector_id, kind, batch_id in pending:
            replay_meter.record(connector_id, kind, batch_id=batch_id)
    except Exception:  # pragma: no cover - counters never fail a commit
        logger.exception("Attendance batch replay counters could not be updated")


@event.listens_for(Session, "after_transaction_end")
def _drop_uncommitted_replays(session: Session, transaction: Any) -> None:
    # A rolled-back outer transaction settled nothing; a committed one was
    # already drained by after_commit.
    if transaction.parent is None:
        session.info.pop(_PENDING_REPLAYS, None)


def attendance_batch_replay_metrics() -> dict[str, dict[str, int]]:
    """Committed counters since process start, keyed by connector and kind."""

    return replay_meter.snapshot()


def _reported_digest_key(value: object, *, valid: bool) -> str:
    state = "ABSENT" if value is None else ("VALID" if valid else "INVALID")
    return payload_digest({"state": state, "value": value})


def _safe_event_uid(value: object) -> str | None:
    return value if isinstance(value, str) and _HEX_64.fullmatch(value) else None


def _validation_summary(error: ValidationError) -> list[dict[str, str]]:
    summary: list[dict[str, str]] = []
    for detail in error.errors(include_input=False, include_url=False)[:10]:
        location = ".".join(str(part) for part in detail.get("loc", ()))
        summary.append(
            {
                "path": location[:255],
                "type": str(detail.get("type", "validation_error"))[:80],
            }
        )
    return summary


def _error_code(validation_type: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", validation_type).strip("_")
    return f"ATTENDANCE_EVENT_{normalized.upper() or 'INVALID'}"[:120]


@dataclass(frozen=True)
class AttendanceBatchSettlement:
    receipt_id: str
    batch_id: str
    payload_digest: str
    outcome: str
    accepted_event_uids: list[str]
    duplicate_event_uids: list[str]
    rejected: list[dict[str, Any]]
    accepted_total: int
    duplicate_total: int
    quarantined_total: int
    duplicate_batch: bool = False
    # EXACT_RETRY or CAPTURE_REPLAY when this ACK repeats an earlier receipt.
    replay: str | None = None

    @property
    def accepted_count(self) -> int:
        return self.accepted_total

    @property
    def duplicate_count(self) -> int:
        return self.duplicate_total

    @property
    def quarantined_count(self) -> int:
        return self.quarantined_total

    def response(self) -> dict[str, Any]:
        response = {
            "ok": True,
            "receipt_id": self.receipt_id,
            "batch_id": self.batch_id,
            "payload_digest": self.payload_digest,
            "outcome": self.outcome,
            "accepted": self.accepted_count,
            "duplicates": self.duplicate_count,
            "quarantined": self.quarantined_count,
            "accepted_event_uids": self.accepted_event_uids,
            "duplicate_event_uids": self.duplicate_event_uids,
            "rejected": self.rejected,
            "duplicate_batch": self.duplicate_batch,
        }
        if self.replay is not None:
            # Additive: deployed firmware matches batch_id, the counts and the
            # outcome, and ignores unknown fields.
            response["replay"] = self.replay
        return response

    def ack(self, *, message_id: str, sequence: int) -> dict[str, Any]:
        # Deployed firmware accepts this as an ordinary ACK and ignores all
        # additive settlement fields. New firmware consumes the dispositions.
        return {
            "type": "ack",
            "message_id": message_id,
            "message_type": "attendance_batch",
            "seq": sequence,
            **self.response(),
        }


def _settlement_from_receipt(
    session: Session,
    receipt: AttendanceBatchReceipt,
    *,
    duplicate_batch: bool,
    replay: str | None = None,
) -> AttendanceBatchSettlement:
    items = session.scalars(
        select(AttendanceBatchItem)
        .where(AttendanceBatchItem.receipt_id == receipt.id)
        .order_by(AttendanceBatchItem.item_index.asc())
    ).all()
    return AttendanceBatchSettlement(
        receipt_id=receipt.receipt_id,
        batch_id=receipt.batch_id,
        payload_digest=receipt.payload_digest,
        outcome=receipt.outcome,
        accepted_event_uids=[
            row.event_uid
            for row in items
            if row.disposition == "ACCEPTED" and row.event_uid
        ],
        duplicate_event_uids=[
            row.event_uid
            for row in items
            if row.disposition == "DUPLICATE" and row.event_uid
        ],
        rejected=[
            {
                "index": row.item_index,
                "item_id": row.id,
                "code": row.error_code,
                "path": row.error_path,
            }
            for row in items
            if row.disposition == "QUARANTINED"
        ],
        accepted_total=receipt.accepted_count,
        duplicate_total=receipt.duplicate_count,
        quarantined_total=receipt.quarantined_count,
        duplicate_batch=duplicate_batch,
        replay=replay,
    )


def _create_receipt(
    session: Session,
    *,
    connector: Connector,
    batch_id: str,
    digest: str,
    reported_digest: str | None,
    reported_digest_key: str,
    item_count: int,
    content_digest: str | None,
) -> AttendanceBatchReceipt:
    if connector.zkt_device is None:
        raise ValueError("Connector has no assigned ZKT device.")
    now = utc_now()
    receipt = AttendanceBatchReceipt(
        connector_id=connector.id,
        zkt_device_id=connector.zkt_device.id,
        batch_id=batch_id,
        payload_digest=digest,
        reported_payload_digest=reported_digest,
        reported_digest_key=reported_digest_key,
        outcome="PENDING",
        item_count=item_count,
        first_seen_at=now,
        last_seen_at=now,
        committed_at=now,
        content_digest=content_digest,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _quarantine_item(
    session: Session,
    *,
    receipt: AttendanceBatchReceipt,
    item_index: int,
    raw: object,
    error_code: str,
    error_path: str | None = None,
    validation_summary: list[dict[str, str]] | None = None,
    evidence_context: dict[str, object] | None = None,
) -> AttendanceBatchItem:
    wrapped = raw if isinstance(raw, dict) else {"value": raw}
    protected = dict(wrapped)
    if evidence_context:
        protected["__batch_context"] = evidence_context
    item = AttendanceBatchItem(
        receipt_id=receipt.id,
        item_index=item_index,
        disposition="QUARANTINED",
        event_uid=_safe_event_uid(wrapped.get("event_uid")),
        payload_digest=_item_digest(raw),
        error_code=error_code,
        error_path=error_path,
        validation_summary=validation_summary or [],
        protected_payload=encrypt_json(protected),
        review_state="OPEN",
    )
    session.add(item)
    return item


def _finish_receipt(
    session: Session,
    *,
    connector: Connector,
    receipt: AttendanceBatchReceipt,
    accepted: int,
    duplicates: int,
    quarantined: int,
) -> AttendanceBatchSettlement:
    session.flush()
    disposition_count = session.scalar(
        select(func.count(AttendanceBatchItem.id)).where(
            AttendanceBatchItem.receipt_id == receipt.id
        )
    ) or 0
    settlement_count = accepted + duplicates + quarantined
    if disposition_count != settlement_count:
        raise RuntimeError(
            "Attendance batch settlement totals do not match durable item dispositions."
        )
    if 1 <= receipt.item_count <= MAX_BATCH_EVENTS and settlement_count != receipt.item_count:
        raise RuntimeError(
            "Attendance batch settlement does not cover every bounded input row."
        )
    receipt.accepted_count = accepted
    receipt.duplicate_count = duplicates
    receipt.quarantined_count = quarantined
    receipt.committed_at = utc_now()
    if quarantined and not accepted and not duplicates:
        receipt.outcome = "QUARANTINED"
    elif quarantined:
        receipt.outcome = "COMMITTED_WITH_QUARANTINE"
    elif duplicates:
        receipt.outcome = "COMMITTED_WITH_DUPLICATES"
    else:
        receipt.outcome = "COMMITTED"

    # A successfully settled attendance message is not a connector transport
    # rejection, even when one of its rows requires operator review.
    resolve_message_rejection(
        session, connector, message_type="attendance_batch"
    )
    if quarantined:
        upsert_alert(
            session,
            connector,
            code="ATTENDANCE_EVENT_QUARANTINED",
            severity="MEDIUM",
            message=(
                f"{quarantined} attendance record(s) were quarantined; "
                "later punches continued normally."
            ),
            details={
                "receipt_id": receipt.receipt_id,
                "quarantined": quarantined,
                "accepted": accepted,
                "duplicates": duplicates,
                "handling": "NON_BLOCKING_DURABLE_QUARANTINE",
            },
        )
    session.flush()
    return _settlement_from_receipt(session, receipt, duplicate_batch=False)


def _preserved_content_digest(
    session: Session,
    receipt: AttendanceBatchReceipt,
) -> str | None:
    """Rebuild a quarantined receipt's content digest from its own evidence.

    Every input row of a batch-level quarantine was encrypted verbatim with a
    ``__batch_context`` annotation. Anything that cannot be rebuilt exactly
    returns None and is never treated as a match.
    """

    items = session.scalars(
        select(AttendanceBatchItem)
        .where(AttendanceBatchItem.receipt_id == receipt.id)
        .order_by(AttendanceBatchItem.item_index.asc())
    ).all()
    if (
        len(items) != receipt.item_count
        or [row.item_index for row in items] != list(range(receipt.item_count))
        or any(
            row.disposition != "QUARANTINED" or not row.protected_payload
            for row in items
        )
    ):
        return None
    events: list[object] = []
    for row in items:
        try:
            preserved = decrypt_json(row.protected_payload)
        except (InvalidToken, ValueError):
            return None
        raw = {key: value for key, value in preserved.items() if key != "__batch_context"}
        # A non-object input was stored as {"value": input}; that wrapper is
        # ambiguous, and no attendance row has this shape.
        if not raw or set(raw) == {"value"}:
            return None
        if _item_digest(raw) != row.payload_digest:
            return None
        events.append(raw)
    return replay_content_digest(events)


def legacy_replay_candidates(connector_pk: int, batch_id: str, item_count: int):
    """Pre-digest batch-level quarantines of this batch id, ids only.

    No ORDER BY/LIMIT: with ``ORDER BY id DESC LIMIT n`` PostgreSQL walks the
    primary key backwards and filters the whole receipts table whenever fewer
    than n rows match. Unordered, the (connector_id, batch_id) index bounds
    the read and the caller picks the newest in Python.
    """

    return select(AttendanceBatchReceipt.id).where(
        AttendanceBatchReceipt.connector_id == connector_pk,
        AttendanceBatchReceipt.batch_id == batch_id,
        AttendanceBatchReceipt.content_digest.is_(None),
        AttendanceBatchReceipt.outcome == "QUARANTINED",
        AttendanceBatchReceipt.item_count == item_count,
        AttendanceBatchReceipt.quarantined_count == item_count,
    )


def _replayed_receipt(
    session: Session,
    *,
    connector: Connector,
    batch_id: str,
    content_digest: str,
    item_count: int,
) -> AttendanceBatchReceipt | None:
    """Find the settled receipt whose events this batch repeats exactly.

    Only ``captured_at`` may differ. A committed original is preferred over an
    earlier conflict quarantine of the same content.
    """

    prior = session.scalar(
        select(AttendanceBatchReceipt)
        .where(
            AttendanceBatchReceipt.connector_id == connector.id,
            AttendanceBatchReceipt.batch_id == batch_id,
            AttendanceBatchReceipt.content_digest == content_digest,
        )
        .order_by(
            case(
                (AttendanceBatchReceipt.outcome.in_(_COMMITTED_OUTCOMES), 0),
                else_=1,
            ),
            AttendanceBatchReceipt.id.asc(),
        )
        .limit(1)
    )
    if prior is not None:
        return prior
    # Receipts written before content digests existed: a batch-level
    # quarantine kept every row encrypted, so its digest can be derived and
    # stored. A committed receipt kept no raw rows and is never assumed equal.
    # Every candidate read here leaves the NULL set (its digest or the
    # UNAVAILABLE marker is stored), so older history is reached on later
    # misses and nothing is decrypted twice.
    candidate_ids = sorted(
        session.scalars(
            legacy_replay_candidates(connector.id, batch_id, item_count)
        ).all(),
        reverse=True,
    )[:_LEGACY_COMPARE_LIMIT]
    for candidate_id in candidate_ids:
        candidate = session.get(AttendanceBatchReceipt, candidate_id)
        if candidate is None:  # pragma: no cover - read in this transaction
            continue
        derived = _preserved_content_digest(session, candidate)
        candidate.content_digest = (
            CONTENT_DIGEST_UNAVAILABLE if derived is None else derived
        )
        if derived is not None and derived == content_digest:
            return candidate
    return None


def _replay_rows_valid(
    session: Session,
    receipt: AttendanceBatchReceipt,
    raw_events: list[object],
) -> bool:
    """A replayed row may repeat an accepted settlement only if it is valid.

    Rows the receipt quarantined repeat that quarantine; their evidence is
    already preserved under the receipt.
    """

    settled = dict(
        session.execute(
            select(AttendanceBatchItem.item_index, AttendanceBatchItem.event_uid).where(
                AttendanceBatchItem.receipt_id == receipt.id,
                AttendanceBatchItem.disposition.in_(("ACCEPTED", "DUPLICATE")),
            )
        ).all()
    )
    for index, raw in enumerate(raw_events):
        if index not in settled:
            continue
        try:
            parsed = AttendanceEventIn.model_validate(raw)
        except ValidationError:
            return False
        if parsed.event_uid != settled[index]:
            return False
    return True


def settle_attendance_batch(
    session: Session,
    *,
    connector: Connector,
    payload: object,
) -> AttendanceBatchSettlement:
    """Settle every input row durably before the caller emits an ACK.

    Semantic poison is encrypted and quarantined. Database, encryption, or
    other infrastructure failures escape this function so the transaction is
    rolled back and the device retries instead of losing a punch.
    """

    mapping = payload if isinstance(payload, dict) else {}
    raw_events = mapping.get("events")
    digest_material: object = raw_events if isinstance(raw_events, list) else mapping
    digest = payload_digest(digest_material)
    raw_batch_id = mapping.get("batch_id")
    batch_id_valid = (
        isinstance(raw_batch_id, str)
        and 1 <= len(raw_batch_id) <= 120
        and "\x00" not in raw_batch_id
    )
    batch_id = raw_batch_id if batch_id_valid else f"invalid-{digest[:32]}"
    reported_value = mapping.get("payload_digest")
    reported_digest_valid = (
        isinstance(reported_value, str) and _HEX_64.fullmatch(reported_value) is not None
    )
    reported_digest = reported_value if reported_digest_valid else None
    reported_digest_key = _reported_digest_key(
        reported_value,
        valid=reported_digest_valid,
    )
    item_count = len(raw_events) if isinstance(raw_events, list) else 0

    existing = session.scalar(
        select(AttendanceBatchReceipt).where(
            AttendanceBatchReceipt.connector_id == connector.id,
            AttendanceBatchReceipt.batch_id == batch_id,
            AttendanceBatchReceipt.payload_digest == digest,
            AttendanceBatchReceipt.reported_digest_key == reported_digest_key,
        )
    )
    content_digest = replay_content_digest(raw_events)
    if existing is not None:
        existing.observation_count += 1
        existing.last_seen_at = utc_now()
        if (
            existing.content_digest in (None, CONTENT_DIGEST_UNAVAILABLE)
            and content_digest is not None
        ):
            # Identical events: this payload is the receipt's own content.
            existing.content_digest = content_digest
        _count_after_commit(session, connector, "EXACT_RETRY", batch_id)
        return _settlement_from_receipt(
            session, existing, duplicate_batch=True, replay="EXACT_RETRY"
        )

    batch_id_conflict = session.scalar(
        select(AttendanceBatchReceipt.id).where(
            AttendanceBatchReceipt.connector_id == connector.id,
            AttendanceBatchReceipt.batch_id == batch_id,
            AttendanceBatchReceipt.payload_digest != digest,
        )
    )
    if (
        batch_id_conflict is not None
        and content_digest is not None
        and isinstance(payload, dict)
        and batch_id_valid
        and isinstance(raw_events, list)
        and 1 <= len(raw_events) <= MAX_BATCH_EVENTS
        # A device-reported digest must equal the ACK digest, which a replay
        # of another receipt cannot satisfy; no deployed firmware reports one.
        and reported_value is None
    ):
        prior = _replayed_receipt(
            session,
            connector=connector,
            batch_id=batch_id,
            content_digest=content_digest,
            item_count=item_count,
        )
        if prior is not None and _replay_rows_valid(session, prior, raw_events):
            prior.observation_count += 1
            prior.last_seen_at = utc_now()
            kind = (
                "CAPTURE_REPLAY_OF_COMMITTED"
                if prior.outcome in _COMMITTED_OUTCOMES
                else "CAPTURE_REPLAY_OF_QUARANTINE"
            )
            _count_after_commit(session, connector, kind, batch_id)
            # Settled like the conflict quarantine it replaces: an accepted
            # attendance message clears that type's transport rejection.
            resolve_message_rejection(
                session, connector, message_type="attendance_batch"
            )
            return _settlement_from_receipt(
                session, prior, duplicate_batch=True, replay="CAPTURE_REPLAY"
            )
    receipt = _create_receipt(
        session,
        connector=connector,
        batch_id=batch_id,
        digest=digest,
        reported_digest=reported_digest,
        reported_digest_key=reported_digest_key,
        item_count=item_count,
        content_digest=content_digest,
    )

    batch_error: tuple[str, str | None] | None = None
    if not isinstance(payload, dict):
        batch_error = ("ATTENDANCE_BATCH_NOT_OBJECT", None)
    elif not batch_id_valid:
        batch_error = ("ATTENDANCE_BATCH_ID_INVALID", "batch_id")
    elif not isinstance(raw_events, list) or not 1 <= len(raw_events) <= MAX_BATCH_EVENTS:
        batch_error = ("ATTENDANCE_BATCH_EVENT_COUNT_INVALID", "events")
    elif batch_id_conflict is not None:
        batch_error = ("ATTENDANCE_BATCH_ID_CONFLICT", "batch_id")
        _count_after_commit(
            session, connector, "BATCH_ID_CONFLICT_QUARANTINED", batch_id
        )
    elif reported_value is not None and (
        not reported_digest_valid
        or not secrets.compare_digest(digest, reported_digest or "")
    ):
        batch_error = ("ATTENDANCE_BATCH_DIGEST_MISMATCH", "payload_digest")

    if batch_error is not None:
        bounded_events = (
            raw_events
            if isinstance(raw_events, list)
            and 1 <= len(raw_events) <= MAX_BATCH_EVENTS
            else None
        )
        if bounded_events is not None:
            batch_context = {
                "batch_id": raw_batch_id,
                "payload_digest": reported_value,
            }
            for index, raw in enumerate(bounded_events):
                _quarantine_item(
                    session,
                    receipt=receipt,
                    item_index=index,
                    raw=raw,
                    error_code=batch_error[0],
                    error_path=batch_error[1],
                    evidence_context=batch_context,
                )
            quarantined_count = len(bounded_events)
        else:
            _quarantine_item(
                session,
                receipt=receipt,
                item_index=-1,
                raw={"batch": payload},
                error_code=batch_error[0],
                error_path=batch_error[1],
            )
            quarantined_count = 1
        return _finish_receipt(
            session,
            connector=connector,
            receipt=receipt,
            accepted=0,
            duplicates=0,
            quarantined=quarantined_count,
        )

    valid: list[tuple[int, AttendanceEventIn, object]] = []
    quarantined = 0
    assert isinstance(raw_events, list)
    evidence_context = {
        "batch_id": raw_batch_id,
        "payload_digest": reported_value,
    }
    for index, raw in enumerate(raw_events):
        try:
            parsed = AttendanceEventIn.model_validate(raw)
        except ValidationError as error:
            summary = _validation_summary(error)
            first = summary[0] if summary else {"path": "", "type": "invalid"}
            _quarantine_item(
                session,
                receipt=receipt,
                item_index=index,
                raw=raw,
                error_code=_error_code(first["type"]),
                error_path=first["path"] or None,
                validation_summary=summary,
                evidence_context=evidence_context,
            )
            quarantined += 1
            continue
        valid.append((index, parsed, raw))

    accepted_uids: list[str] = []
    duplicate_uids: list[str] = []
    if valid:
        accepted_uids, duplicate_uids = ingest_attendance(
            session,
            connector=connector,
            events=[parsed for _index, parsed, _raw in valid],
        )
        event_rows = {
            row.event_uid: row
            for row in session.scalars(
                select(AttendanceEvent).where(
                    AttendanceEvent.event_uid.in_(
                        [parsed.event_uid for _index, parsed, _raw in valid]
                    )
                )
            ).all()
        }
        accepted_remaining = set(accepted_uids)
        for index, parsed, raw in valid:
            disposition = (
                "ACCEPTED" if parsed.event_uid in accepted_remaining else "DUPLICATE"
            )
            accepted_remaining.discard(parsed.event_uid)
            event_row = event_rows.get(parsed.event_uid)
            session.add(
                AttendanceBatchItem(
                    receipt_id=receipt.id,
                    item_index=index,
                    disposition=disposition,
                    event_uid=parsed.event_uid,
                    attendance_event_id=event_row.id if event_row else None,
                    payload_digest=_item_digest(raw),
                    validation_summary=[],
                    review_state="NOT_REQUIRED",
                )
            )

    return _finish_receipt(
        session,
        connector=connector,
        receipt=receipt,
        accepted=len(accepted_uids),
        duplicates=len(duplicate_uids),
        quarantined=quarantined,
    )


def attendance_quarantine_summary(
    session: Session,
    *,
    connector_id: int | None = None,
    review_state: str | None = None,
    cursor: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    statement = (
        select(AttendanceBatchItem, AttendanceBatchReceipt, Connector)
        .join(
            AttendanceBatchReceipt,
            AttendanceBatchReceipt.id == AttendanceBatchItem.receipt_id,
        )
        .join(Connector, Connector.id == AttendanceBatchReceipt.connector_id)
        .where(AttendanceBatchItem.disposition == "QUARANTINED")
    )
    if connector_id is not None:
        statement = statement.where(AttendanceBatchReceipt.connector_id == connector_id)
    if review_state is not None:
        statement = statement.where(AttendanceBatchItem.review_state == review_state)
    filtered_total = session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ) or 0
    if cursor is not None:
        statement = statement.where(AttendanceBatchItem.id < cursor)
    rows = session.execute(
        statement.order_by(AttendanceBatchItem.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    totals_statement = (
        select(
            func.count(AttendanceBatchItem.id),
            func.sum(case((AttendanceBatchItem.review_state == "OPEN", 1), else_=0)),
        )
        .join(
            AttendanceBatchReceipt,
            AttendanceBatchReceipt.id == AttendanceBatchItem.receipt_id,
        )
        .where(AttendanceBatchItem.disposition == "QUARANTINED")
    )
    if connector_id is not None:
        totals_statement = totals_statement.where(
            AttendanceBatchReceipt.connector_id == connector_id
        )
    totals = session.execute(totals_statement).one()
    return {
        "totals": {
            "all": int(totals[0] or 0),
            "open": int(totals[1] or 0),
        },
        "filtered_total": int(filtered_total),
        "rows": [
            {
                "id": item.id,
                "receipt_id": receipt.receipt_id,
                "connector_id": connector.connector_id,
                "device_id": connector.device_id,
                "display_name": connector.display_name,
                "zone_id": connector.zone_id,
                "item_index": item.item_index,
                "error_code": item.error_code,
                "error_path": item.error_path,
                "payload_digest": item.payload_digest,
                "review_state": item.review_state,
                "reviewed_by": item.reviewed_by,
                "review_reason": item.review_reason,
                "reviewed_at": item.reviewed_at,
                "observed_at": item.created_at,
                "evidence_available": bool(item.protected_payload),
                "handling": "QUARANTINED_NON_BLOCKING",
            }
            for item, receipt, connector in page
        ],
        "next_cursor": page[-1][0].id if len(rows) > limit and page else None,
    }


def attendance_quarantine_item(
    session: Session, item_id: int
) -> AttendanceBatchItem | None:
    return session.scalar(
        select(AttendanceBatchItem).where(
            AttendanceBatchItem.id == item_id,
            AttendanceBatchItem.disposition == "QUARANTINED",
        )
    )


def review_attendance_quarantine(
    session: Session,
    *,
    item: AttendanceBatchItem,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> None:
    receipt = session.get(AttendanceBatchReceipt, item.receipt_id)
    connector = None
    if receipt is not None:
        connector = session.scalar(
            select(Connector)
            .where(Connector.id == receipt.connector_id)
            .with_for_update()
        )
        session.refresh(item)
    if item.review_idempotency_key == idempotency_key:
        return
    if item.review_state == "REVIEWED":
        raise ValueError("Attendance quarantine item has already been reviewed.")
    existing = session.scalar(
        select(AttendanceBatchItem).where(
            AttendanceBatchItem.review_idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        raise ValueError("Idempotency key was already used for another item.")
    item.review_state = "REVIEWED"
    item.reviewed_by = actor
    item.review_reason = reason.strip()
    item.review_idempotency_key = idempotency_key
    item.reviewed_at = utc_now()
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_QUARANTINE_REVIEWED",
        target_type="attendance_batch_item",
        target_id=str(item.id),
        before={"review_state": "OPEN"},
        after={
            "review_state": "REVIEWED",
            "error_code": item.error_code,
            "payload_digest": item.payload_digest,
        },
        outcome="REVIEWED",
        request_id=idempotency_key,
    )
    if receipt is not None:
        remaining = session.scalar(
            select(func.count(AttendanceBatchItem.id))
            .join(
                AttendanceBatchReceipt,
                AttendanceBatchReceipt.id == AttendanceBatchItem.receipt_id,
            )
            .where(
                AttendanceBatchReceipt.connector_id == receipt.connector_id,
                AttendanceBatchItem.disposition == "QUARANTINED",
                AttendanceBatchItem.review_state == "OPEN",
            )
        ) or 0
        if remaining == 0 and connector is not None:
            resolve_alert(
                session, connector, code="ATTENDANCE_EVENT_QUARANTINED"
            )


def reveal_attendance_quarantine(
    session: Session,
    *,
    item: AttendanceBatchItem,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    existing_audit = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "ATTENDANCE_QUARANTINE_EVIDENCE_REVEALED",
            AuditEvent.target_id == str(item.id),
            AuditEvent.request_id == idempotency_key,
        )
    )
    if existing_audit is None:
        append_audit(
            session,
            actor=actor,
            action="ATTENDANCE_QUARANTINE_EVIDENCE_REVEALED",
            target_type="attendance_batch_item",
            target_id=str(item.id),
            request_id=idempotency_key,
            before={},
            after={
                "reason": reason.strip(),
                "payload_digest": item.payload_digest,
            },
            outcome="REVEALED",
        )
    return {
        "id": item.id,
        "payload_digest": item.payload_digest,
        "error_code": item.error_code,
        "error_path": item.error_path,
        "payload": decrypt_json(item.protected_payload),
    }
