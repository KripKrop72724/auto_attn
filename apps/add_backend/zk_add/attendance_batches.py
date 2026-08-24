from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import case, func, select
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
        return {
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
) -> AttendanceBatchItem:
    wrapped = raw if isinstance(raw, dict) else {"value": raw}
    item = AttendanceBatchItem(
        receipt_id=receipt.id,
        item_index=item_index,
        disposition="QUARANTINED",
        event_uid=_safe_event_uid(wrapped.get("event_uid")),
        payload_digest=_item_digest(raw),
        error_code=error_code,
        error_path=error_path,
        validation_summary=validation_summary or [],
        protected_payload=encrypt_json(wrapped),
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
                "batch_id": receipt.batch_id,
                "quarantined": quarantined,
                "accepted": accepted,
                "duplicates": duplicates,
                "handling": "NON_BLOCKING_DURABLE_QUARANTINE",
            },
        )
    session.flush()
    return _settlement_from_receipt(session, receipt, duplicate_batch=False)


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
    if existing is not None:
        existing.observation_count += 1
        existing.last_seen_at = utc_now()
        return _settlement_from_receipt(session, existing, duplicate_batch=True)

    batch_id_conflict = session.scalar(
        select(AttendanceBatchReceipt.id).where(
            AttendanceBatchReceipt.connector_id == connector.id,
            AttendanceBatchReceipt.batch_id == batch_id,
            AttendanceBatchReceipt.payload_digest != digest,
        )
    )
    receipt = _create_receipt(
        session,
        connector=connector,
        batch_id=batch_id,
        digest=digest,
        reported_digest=reported_digest,
        reported_digest_key=reported_digest_key,
        item_count=item_count,
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
    elif reported_value is not None and (
        not reported_digest_valid
        or not secrets.compare_digest(digest, reported_digest or "")
    ):
        batch_error = ("ATTENDANCE_BATCH_DIGEST_MISMATCH", "payload_digest")

    if batch_error is not None:
        _quarantine_item(
            session,
            receipt=receipt,
            item_index=-1,
            raw={"batch": payload},
            error_code=batch_error[0],
            error_path=batch_error[1],
        )
        return _finish_receipt(
            session,
            connector=connector,
            receipt=receipt,
            accepted=0,
            duplicates=0,
            quarantined=max(1, item_count),
        )

    valid: list[tuple[int, AttendanceEventIn, object]] = []
    quarantined = 0
    assert isinstance(raw_events, list)
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
                "batch_id": receipt.batch_id,
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
    receipt = session.get(AttendanceBatchReceipt, item.receipt_id)
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
        if remaining == 0:
            connector = session.get(Connector, receipt.connector_id)
            if connector is not None:
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
