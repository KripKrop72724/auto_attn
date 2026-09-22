"""Append-only attendance delivery recovery for ZKT and Hikvision sources.

The normal ingestion path remains the source of truth.  This module only
creates auditable recovery jobs and requeues already-preserved events.  Source
corrections create a new derived event linked to immutable evidence; they never
edit a terminal manifest, Hikvision observation, or existing attendance row.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import cnic_lookup, decrypt_text, decrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem,
    AttendanceRecoveryJob,
    AttendanceSourceCorrection,
    Connector,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
    TerminalSourceEpoch,
    AuditEvent,
)
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_ACTIVE_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
    ORDS_TERMINAL_REVIEW_STATUSES,
    normalize_ords_status,
)
from zk_add.service import (
    attendance_device_time_is_plausible,
    ensure_attendance_ords_outbox,
    trusted_capture_terminal_serial,
    VERIFIED_IDENTITY_RESOLUTION_STATUSES,
)
from zk_add.identity_provenance import historical_identity_is_supported
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now


SAFE_LANE = "SAFE_DELIVERY"
IDENTITY_LANE = "IDENTITY_HELD"
SOURCE_LANE = "SOURCE_CORRECTION"
REVIEW_LANE = "PERMANENT_REVIEW"

SAFE_ACTIONS = {
    "RETRY_DELIVERY",
    "REBUILD_OUTBOX",
    "RECOVER_STALE_IN_FLIGHT",
}
CORRECTION_ACTION = "CREATE_TIMESTAMP_CORRECTION"
KNOWN_EVENT_STATUSES = (
    ORDS_ACTIVE_STATUSES
    | ORDS_ACKNOWLEDGED_STATUSES
    | ORDS_IDENTITY_HELD_STATUSES
    | ORDS_TERMINAL_REVIEW_STATUSES
    | frozenset(
        {
            "RETRYING",
            "QUARANTINED_SOURCE_CONFLICT",
            "QUARANTINED_IDENTITY_CONFLICT",
            "QUARANTINED_MISSING_TERMINAL_PROVENANCE",
        }
    )
)
MIN_CORRECTION_TIME = datetime(2010, 1, 1, tzinfo=timezone.utc)


def stale_in_flight_seconds() -> int:
    return max(60, int(settings.ords_timeout_seconds * 3))


class RecoveryError(ValueError):
    """A safe, user-facing recovery validation error."""

    def __init__(self, message: str, code: str = "RECOVERY_INVALID"):
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value else None


def sign_recovery_preview(*, digest: str, expires_at: datetime, actor: str, action: str) -> str:
    """Bind approval to the server-issued scope, actor, action and expiry."""
    if not settings.pii_lookup_key:
        raise RecoveryError("The recovery signing key is unavailable.", "SIGNING_UNAVAILABLE")
    material = json.dumps(["attendance-recovery-v1", digest, _iso(expires_at), actor, action], separators=(",", ":"))
    return hmac.new(settings.pii_lookup_key.encode(), material.encode(), hashlib.sha256).hexdigest()


def verify_recovery_preview(*, digest: str, expires_at: datetime, actor: str, action: str, signature: str) -> None:
    expected = sign_recovery_preview(digest=digest, expires_at=expires_at, actor=actor, action=action)
    if not hmac.compare_digest(signature, expected):
        raise RecoveryError("The server-issued preview is invalid; generate a new preview.", "PREVIEW_INVALID")
    if ensure_utc(expires_at) <= utc_now():
        raise RecoveryError("The preview has expired; generate a new preview.", "PREVIEW_EXPIRED")


def _execution_allowed(zone_id: str | None, *, correction: bool = False) -> bool:
    allowed = {value.strip() for value in settings.attendance_recovery_allowed_zones.split(",") if value.strip()}
    return bool(settings.attendance_recovery_execution_enabled
                and (not correction or settings.attendance_source_correction_enabled)
                and (not allowed or zone_id in allowed))


def _filters_dict(filters: Any) -> dict[str, Any]:
    if hasattr(filters, "model_dump"):
        values = filters.model_dump(exclude_none=True)
    else:
        values = {key: value for key, value in dict(filters or {}).items() if value not in (None, "", [])}

    # The filter object is persisted in the job and included in the candidate
    # digest.  Canonicalize datetimes before hashing so the digest is stable
    # across Pydantic, PostgreSQL, and a restarted worker.
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in values.items()
    }


def _connector_filter(statement, filters: dict[str, Any]):
    if filters.get("connector_id"):
        statement = statement.where(Connector.connector_id == filters["connector_id"])
    if filters.get("zone_id"):
        statement = statement.where(Connector.zone_id == filters["zone_id"])
    if filters.get("firmware_family"):
        statement = statement.where(Connector.firmware_family == filters["firmware_family"])
    if filters.get("terminal_serial"):
        statement = statement.where(AttendanceEvent.device_serial == filters["terminal_serial"])
    if filters.get("source"):
        statement = statement.where(AttendanceEvent.source == filters["source"])
    if filters.get("from_at"):
        statement = statement.where(AttendanceEvent.device_event_time >= ensure_utc(_parse_filter_time(filters["from_at"])))
    if filters.get("to_at"):
        statement = statement.where(AttendanceEvent.device_event_time <= ensure_utc(_parse_filter_time(filters["to_at"])))
    if filters.get("statuses"):
        statement = statement.where(AttendanceEvent.ords_status.in_(filters["statuses"]))
    if filters.get("event_ids"):
        statement = statement.where(AttendanceEvent.id.in_(filters["event_ids"]))
    if filters.get("retry_age_minutes") is not None:
        statement = statement.where(func.coalesce(OrdsOutbox.last_attempt_at, AttendanceEvent.received_at) <= utc_now() - timedelta(minutes=filters["retry_age_minutes"]))
    if filters.get("ords_error_category"):
        statement = statement.where(OrdsOutbox.last_error == filters["ords_error_category"])
    if filters.get("provenance") == "MISSING":
        statement = statement.where(AttendanceEvent.device_serial.is_(None))
    elif filters.get("provenance") == "VERIFIED":
        statement = statement.where(AttendanceEvent.device_serial.is_not(None))
    if filters.get("source_epoch") or filters.get("generation") is not None:
        zkt = select(TerminalRecordManifest.attendance_event_id).where(TerminalRecordManifest.attendance_event_id == AttendanceEvent.id)
        if filters.get("source_epoch"):
            zkt = zkt.join(TerminalSourceEpoch, TerminalSourceEpoch.id == TerminalRecordManifest.source_epoch_id).where(TerminalSourceEpoch.epoch_id == filters["source_epoch"])
        if filters.get("generation") is not None:
            zkt = zkt.where(TerminalRecordManifest.generation == filters["generation"])
        hik = select(HikvisionEvidence.id).where(HikvisionEvidence.event_uid == AttendanceEvent.event_uid,
            HikvisionEvidence.source_epoch == filters.get("source_epoch"))
        statement = statement.where(or_(zkt.exists(), hik.exists()) if filters.get("generation") is None else zkt.exists())
    return statement


def _parse_filter_time(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _terminal_provenance_verified(event: AttendanceEvent, connector: Connector | None) -> bool:
    terminal = connector.zkt_device if connector else None
    if terminal is None or not terminal.confirmed_serial:
        return False
    if terminal.serial != terminal.confirmed_serial:
        return False
    serial, _origin = trusted_capture_terminal_serial(
        zkt=terminal, source=event.source, supplied_serial=event.device_serial,
        captured_at=event.captured_at,
    )
    return bool(serial and serial == terminal.confirmed_serial)


def _verified_device_user(
    session: Session,
    event: AttendanceEvent,
    connector: Connector | None,
) -> DeviceUser | None:
    """Return the one exact current ZKT identity that proves this event.

    This query is intentionally shared by preview and execution.  Preview only
    observes the result; the worker applies the same evidence while holding the
    event and outbox locks.  A user id or display name by itself is never enough
    to release an identity hold.
    """

    if not connector or connector.firmware_family == "hikvision":
        return None
    zkt = connector.zkt_device
    if zkt is None or not zkt.confirmed_serial:
        return None
    if event.device_serial and event.device_serial != zkt.confirmed_serial:
        return None
    if event.identity_snapshot_id and zkt.identity_snapshot_id:
        if event.identity_snapshot_id != zkt.identity_snapshot_id:
            return None
    query = select(DeviceUser).where(
        DeviceUser.zkt_device_id == zkt.id,
        DeviceUser.user_id == event.user_id,
        DeviceUser.lifecycle_state == "ACTIVE",
        DeviceUser.present.is_(True),
        DeviceUser.identity_conflict_code.is_(None),
        DeviceUser.cnic_lookup_hash.is_not(None),
        DeviceUser.cnic_encrypted.is_not(None),
    )
    if event.device_user_id is not None:
        query = query.where(DeviceUser.id == event.device_user_id)
    if event.uid:
        query = query.where(DeviceUser.uid == event.uid)
    users = session.scalars(query).all()
    if len(users) != 1:
        return None
    user = users[0]
    if session.scalar(select(DeviceUser.id).where(
        DeviceUser.zkt_device_id == zkt.id,
        DeviceUser.user_id == event.user_id,
        DeviceUser.id != user.id,
    ).limit(1)):
        return None
    try:
        authoritative_cnic = decrypt_cnic(user.cnic_encrypted)
    except Exception:  # malformed encrypted evidence remains fail-closed
        authoritative_cnic = None
    if not authoritative_cnic:
        return None
    try:
        if cnic_lookup(authoritative_cnic) != user.cnic_lookup_hash:
            return None
    except Exception:
        return None
    # A current snapshot revision is part of the identity proof.  If either
    # side is absent, the terminal history cannot be safely tied to this user.
    if (
        user.snapshot_revision is None
        or zkt.identity_snapshot_revision is None
        or user.snapshot_revision != zkt.identity_snapshot_revision
    ):
        return None
    if event.uid and event.uid != user.uid:
        return None
    if event.identity_terminal_fingerprint or user.terminal_identity_fingerprint:
        if not event.identity_terminal_fingerprint or not user.terminal_identity_fingerprint:
            return None
        if event.identity_terminal_fingerprint != user.terminal_identity_fingerprint:
            return None
    fingerprint = event.identity_terminal_fingerprint or user.terminal_identity_fingerprint
    if not historical_identity_is_supported(
        serial=event.device_serial,
        bound_serial=zkt.serial,
        confirmed_serial=zkt.confirmed_serial,
        # The source UID is checked explicitly above.  Older ZKT manifests do
        # not carry a terminal fingerprint, so the continuity helper uses the
        # exact user id in that case while still enforcing serial and snapshot
        # continuity and the capture-time window.
        uid=event.uid if fingerprint else None,
        expected_uid=user.uid if fingerprint else None,
        fingerprint=fingerprint,
        expected_fingerprint=user.terminal_identity_fingerprint,
        event_time=event.device_event_time,
        continuity_started=zkt.last_identity_change_at,
        snapshot_observed=zkt.identity_snapshot_observed_at,
        snapshot_stable=bool(
            zkt.snapshot_complete and zkt.identity_snapshot_stable and zkt.identity_snapshot_id
        ),
        tolerance_seconds=settings.identity_snapshot_capture_tolerance_seconds,
        allow_user_id_only=not fingerprint,
        expected_user_id=user.user_id,
    ):
        return None
    return user


def _identity_provable_without_mutation(
    session: Session,
    event: AttendanceEvent,
    connector: Connector | None,
) -> bool:
    """Return whether a previously held event now has exact identity proof.

    This is deliberately read-only.  Preview must not release an event; it
    only reports that the normal worker can safely reclassify it at execution
    time after taking the event/outbox row locks.
    """

    if not connector or connector.firmware_family == "hikvision":
        return bool(event.cnic_lookup_hash and event.identity_content_status == "VERIFIED")
    return _verified_device_user(session, event, connector) is not None


def _apply_verified_identity(
    session: Session,
    event: AttendanceEvent,
    outbox: OrdsOutbox | None,
    connector: Connector,
) -> bool:
    """Copy exact ZKT identity evidence onto a held event before requeueing.

    The source row and its digest remain unchanged.  Only the attendance
    projection and its durable outbox are advanced after the worker has locked
    both rows and repeated the exact-evidence check used by preview.
    """

    user = _verified_device_user(session, event, connector)
    if user is None:
        return False
    zkt = connector.zkt_device
    event.device_user_id = user.id
    event.identity_snapshot_id = zkt.identity_snapshot_id if zkt else event.identity_snapshot_id
    event.identity_terminal_fingerprint = user.terminal_identity_fingerprint
    event.identity_resolution_status = "RESOLVED_CURRENT_SNAPSHOT"
    event.identity_resolved_at = utc_now()
    event.identity_repaired_at = utc_now()
    event.identity_repair_reason = "RECOVERY_EXACT_TERMINAL_IDENTITY"
    event.display_name = user.display_name
    event.cnic_encrypted = user.cnic_encrypted
    event.cnic_lookup_hash = user.cnic_lookup_hash
    event.cnic_last4 = user.cnic_last4
    event.raw_punch = event.raw_punch or user.shift_worker
    event.ords_status = "PENDING"
    if outbox is not None:
        outbox.status = "PENDING"
        outbox.next_attempt_at = None
        outbox.last_http_status = None
        outbox.last_error = None
    return True


def _event_lane(
    session: Session,
    event: AttendanceEvent,
    outbox: OrdsOutbox | None,
    connector: Connector | None,
    *,
    now: datetime | None = None,
    identity_prover=None,
) -> tuple[str, str]:
    """Classify an event without treating unknown states as retryable."""

    status = normalize_ords_status(event.ords_status)
    if status in ORDS_ACKNOWLEDGED_STATUSES or event.oracle_confirmed_at is not None:
        return "CONFIRMED", "Oracle has acknowledged this event."
    if not _terminal_provenance_verified(event, connector):
        return REVIEW_LANE, "Verified terminal provenance is missing."
    if not re.fullmatch(r"[0-9a-f]{64}", event.event_uid or ""):
        return REVIEW_LANE, "The event UID is invalid."
    if status not in KNOWN_EVENT_STATUSES:
        return REVIEW_LANE, "The delivery state is unknown and is held fail-closed."
    if status in ORDS_TERMINAL_REVIEW_STATUSES or status in {
        "QUARANTINED_SOURCE_CONFLICT", "QUARANTINED_IDENTITY_CONFLICT",
        "QUARANTINED_MISSING_TERMINAL_PROVENANCE",
    } or event.identity_resolution_status == "BLOCKED_SOURCE_CONFLICT":
        return REVIEW_LANE, "The event has a terminal review outcome."
    if event.clock_quality == "INVALID" or not attendance_device_time_is_plausible(event.device_event_time, event.captured_at):
        return SOURCE_LANE, "The preserved source timestamp requires correction review."
    if status == "QUARANTINED_IDENTITY_REUSE" or event.identity_resolution_status == "QUARANTINED_REUSE":
        return IDENTITY_LANE, "The event is pinned because terminal identity reuse was detected."
    if outbox and normalize_ords_status(outbox.status) not in (ORDS_ACTIVE_STATUSES | ORDS_IDENTITY_HELD_STATUSES | {"RETRYING"}):
        return REVIEW_LANE, "The durable outbox has an acknowledged, unknown, or permanent review outcome."
    needs_identity = (
        status in ORDS_IDENTITY_HELD_STATUSES
        or (outbox and outbox.status in ORDS_IDENTITY_HELD_STATUSES)
        or not event.cnic_lookup_hash or not event.cnic_encrypted
        or event.identity_resolution_status not in VERIFIED_IDENTITY_RESOLUTION_STATUSES
    )
    if needs_identity:
        if (identity_prover() if identity_prover is not None else _identity_provable_without_mutation(session, event, connector)):
            return SAFE_LANE, "Exact terminal identity and authoritative CNIC evidence are now verified."
        return IDENTITY_LANE, "Identity evidence is incomplete or reused."
    return SAFE_LANE, "The event can be retried through the idempotent outbox."


def _event_state_digest(event: AttendanceEvent, outbox: OrdsOutbox | None) -> str:
    return _digest(
        {
            "event_id": event.id,
            "event_uid": event.event_uid,
            "event_status": event.ords_status,
            "identity_status": event.identity_resolution_status,
            "cnic_hash": event.cnic_lookup_hash,
            "device_user_id": event.device_user_id,
            "snapshot_id": event.identity_snapshot_id,
            "fingerprint": event.identity_terminal_fingerprint,
            "serial": event.device_serial,
            "uid": event.uid,
            "user_id": event.user_id,
            "device_time": _iso(event.device_event_time),
            "captured_at": _iso(event.captured_at),
            "clock_quality": event.clock_quality,
            "source_digest": _digest(event.raw_event or {}),
            "outbox_status": outbox.status if outbox else None,
            "outbox_attempts": outbox.attempt_count if outbox else None,
            "payload_hash": outbox.payload_hash if outbox else None,
            "last_attempt_at": _iso(outbox.last_attempt_at) if outbox else None,
            "next_attempt_at": _iso(outbox.next_attempt_at) if outbox else None,
        }
    )


def _recovery_state_digest(session: Session, event: AttendanceEvent, outbox: OrdsOutbox | None, connector: Connector | None) -> str:
    terminal = connector.zkt_device if connector else None
    user = _verified_device_user(session, event, connector) if (event.ords_status == "BLOCKED_IDENTITY" or not event.cnic_lookup_hash) else None
    return _digest({"event": _event_state_digest(event, outbox),
                    "binding": [terminal.serial, terminal.confirmed_serial, _iso(terminal.serial_confirmed_at)] if terminal else None,
                    "identity": [user.id, user.cnic_lookup_hash, user.snapshot_revision, user.row_version,
                                 terminal.identity_snapshot_id, _iso(terminal.last_identity_change_at)] if user else None})


def _event_statement(session: Session, filters: dict[str, Any]):
    statement = (
        select(AttendanceEvent, OrdsOutbox, Connector)
        .outerjoin(Connector, Connector.id == AttendanceEvent.connector_id)
        .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
        .where(~AttendanceEvent.ords_status.in_(tuple(ORDS_ACKNOWLEDGED_STATUSES)), AttendanceEvent.oracle_confirmed_at.is_(None))
        .order_by(AttendanceEvent.id.asc())
    )
    return _connector_filter(statement, filters)


def _candidate_row(
    event: AttendanceEvent,
    outbox: OrdsOutbox | None,
    connector: Connector | None,
    lane: str,
    reason: str,
    now: datetime,
) -> dict:
    terminal = connector.zkt_device if connector else None
    raw_event = event.raw_event or {}
    source_epoch = raw_event.get("source_epoch") or raw_event.get("source_epoch_id")
    generation = raw_event.get("generation") or raw_event.get("terminal_generation")
    source_digest = (
        raw_event.get("raw_record_digest")
        or raw_event.get("observation_sha256")
        or raw_event.get("source_digest")
    )
    source_event_id = raw_event.get("source_event_id")
    stale = bool(
        outbox
        and outbox.status == "IN_FLIGHT"
        and outbox.last_attempt_at
        and ensure_utc(outbox.last_attempt_at) <= now - timedelta(seconds=stale_in_flight_seconds())
    )
    return {
        "source_kind": "ATTENDANCE_EVENT",
        "source_ref": f"EVENT:{event.id}",
        "attendance_event_id": event.id,
        "event_uid": event.event_uid,
        "connector_id": connector.connector_id if connector else None,
        "display_name": connector.display_name if connector else None,
        "zone_id": connector.zone_id if connector else None,
        "firmware_family": connector.firmware_family if connector else None,
        "terminal_serial": event.device_serial or (terminal.confirmed_serial if terminal else None),
        "source": event.source,
        "source_epoch": source_epoch,
        "generation": generation,
        "source_digest": source_digest,
        "source_event_id": source_event_id,
        "uid": event.uid,
        "user_id": event.user_id,
        "identity_snapshot_id": event.identity_snapshot_id,
        "clock_quality": event.clock_quality,
        "clock_drift_seconds": event.clock_drift_seconds,
        "terminal_provenance": "VERIFIED" if _terminal_provenance_verified(event, connector) else "MISSING",
        "device_event_time": _iso(event.device_event_time),
        "received_at": _iso(event.received_at),
        "event_status": event.ords_status,
        "outbox_status": outbox.status if outbox else None,
        "lane": lane,
        "reason": reason,
        "stale_in_flight": stale,
        "state_digest": _event_state_digest(event, outbox),
    }


def _action_eligible(action: str, row: dict, outbox: OrdsOutbox | None, now: datetime) -> bool:
    if row["lane"] != SAFE_LANE:
        return False
    if outbox and outbox.status == "IN_FLIGHT" and not row["stale_in_flight"]:
        return False
    if action == "REBUILD_OUTBOX":
        return outbox is None
    if action == "RECOVER_STALE_IN_FLIGHT":
        return row["stale_in_flight"]
    return True


def build_recovery_preview(session: Session, *, action: str, filters: Any) -> dict:
    if action not in SAFE_ACTIONS:
        raise RecoveryError("Only safe delivery actions can use the recovery preview.", "ACTION_NOT_ALLOWED")
    if not settings.attendance_recovery_preview_enabled:
        raise RecoveryError("Attendance recovery preview is disabled.", "PREVIEW_DISABLED")
    filter_values = _filters_dict(filters)
    now = utc_now()
    rows = session.execute(_event_statement(session, filter_values).limit(settings.attendance_recovery_max_items + 1)).all()
    if len(rows) > settings.attendance_recovery_max_items:
        raise RecoveryError("The recovery scope is too large; narrow the filters.", "SCOPE_TOO_LARGE")
    candidates: list[dict] = []
    exclusions: list[dict] = []
    counts = {SAFE_LANE: 0, IDENTITY_LANE: 0, SOURCE_LANE: 0, REVIEW_LANE: 0, "CONFIRMED": 0}
    matching = 0
    for event, outbox, connector in rows:
        lane, reason = _event_lane(session, event, outbox, connector, now=now)
        row = _candidate_row(event, outbox, connector, lane, reason, now)
        row["state_digest"] = _recovery_state_digest(session, event, outbox, connector)
        requested_lane = filter_values.get("lane")
        if requested_lane and lane != requested_lane:
            continue
        if filter_values.get("stale_only") and not row["stale_in_flight"]:
            continue
        matching += 1
        counts[lane] = counts.get(lane, 0) + 1
        if _action_eligible(action, row, outbox, now):
            candidates.append(row)
        else:
            exclusions.append(row)
    material = {
        "action": action,
        "filters": filter_values,
        "candidates": [
            {
                "source_kind": row["source_kind"],
                "source_ref": row["source_ref"],
                "state_digest": row["state_digest"],
            }
            for row in candidates
        ],
    }
    return {
        "schema_version": "1",
        "action": action,
        "filters": filter_values,
        "candidate_digest": _digest(material),
        "preview_expires_at": utc_now() + timedelta(seconds=settings.attendance_recovery_preview_seconds),
        "counts": {
            "matching": matching,
            "eligible": len(candidates),
            "excluded": len(exclusions),
            "safe_delivery": counts.get(SAFE_LANE, 0),
            "identity_held": counts.get(IDENTITY_LANE, 0),
            "source_correction": counts.get(SOURCE_LANE, 0),
            "permanent_review": counts.get(REVIEW_LANE, 0),
            "confirmed": counts.get("CONFIRMED", 0),
        },
        "rows": candidates[:500],
        "_candidate_rows": candidates,
        "excluded_rows": exclusions[:500],
    }


def recovery_summary(session: Session) -> dict:
    now = utc_now()
    status_rows = session.execute(
        select(AttendanceEvent.ords_status, func.count(AttendanceEvent.id)).group_by(AttendanceEvent.ords_status)
    ).all()
    status_counts = {normalize_ords_status(status): int(count) for status, count in status_rows}
    missing_outbox = int(
        session.scalar(
            select(func.count(AttendanceEvent.id))
            .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
            .where(
                OrdsOutbox.id.is_(None),
                ~AttendanceEvent.ords_status.in_(tuple(ORDS_ACKNOWLEDGED_STATUSES)),
            )
        )
        or 0
    )
    stale_in_flight = int(
        session.scalar(
            select(func.count(OrdsOutbox.id)).where(
                OrdsOutbox.status == "IN_FLIGHT",
                OrdsOutbox.last_attempt_at <= now - timedelta(seconds=stale_in_flight_seconds()),
            )
        )
        or 0
    )
    # Active IN_FLIGHT requests already have a worker lease and must not be
    # presented as operator retry candidates. Stale in-flight rows are exposed
    # separately and can be recovered through the audited action below.
    safe_statuses = {"PENDING", "FAILED_RETRYABLE", "RETRYING"}
    safe = sum(status_counts.get(status, 0) for status in safe_statuses)
    active_in_flight = status_counts.get("IN_FLIGHT", 0)
    identity = sum(status_counts.get(status, 0) for status in ORDS_IDENTITY_HELD_STATUSES)
    review = sum(
        count
        for status, count in status_counts.items()
        if status not in (
            safe_statuses
            | {"IN_FLIGHT"}
            | ORDS_IDENTITY_HELD_STATUSES
            | ORDS_ACKNOWLEDGED_STATUSES
        )
    )
    manifests = session.execute(
        select(TerminalRecordManifest.disposition, func.count(TerminalRecordManifest.id))
        .where(
            TerminalRecordManifest.canonical_source.is_(True),
            TerminalRecordManifest.disposition.in_(("INVALID_TIME", "MALFORMED")),
        )
        .group_by(TerminalRecordManifest.disposition)
    ).all()
    source_counts = {str(disposition).lower(): int(count) for disposition, count in manifests}
    hik = session.execute(
        select(HikvisionEvidence.disposition, func.count(HikvisionEvidence.id))
        .where(
            HikvisionEvidence.disposition.in_(
                ("INVALID_TIME", "SOURCE_FACT_CONFLICT", "SOURCE_EPOCH_REVIEW", "UNCLASSIFIED_SOURCE")
            )
        )
        .group_by(HikvisionEvidence.disposition)
    ).all()
    return {
        "schema_version": "1",
        "preview_enabled": settings.attendance_recovery_preview_enabled,
        "execution_enabled": settings.attendance_recovery_execution_enabled,
        "correction_enabled": settings.attendance_source_correction_enabled,
        "allowed_zones": [z.strip() for z in settings.attendance_recovery_allowed_zones.split(",") if z.strip()],
        "delivery": {
            "safe_retryable": safe,
            "missing_outbox": missing_outbox,
            "stale_in_flight": stale_in_flight,
            "active_in_flight": active_in_flight,
            "identity_held": identity,
            "permanent_review": review,
            "status_counts": status_counts,
        },
        "source": {
            "zkt_invalid_time": source_counts.get("invalid_time", 0),
            "zkt_malformed": source_counts.get("malformed", 0),
            "hikvision": {str(disposition): int(count) for disposition, count in hik},
        },
    }


def _job_material(action: str, filters: dict, candidates: list[dict]) -> dict:
    return {
        "action": action,
        "filters": filters,
        "candidates": [
            {
                "source_kind": row["source_kind"],
                "source_ref": row["source_ref"],
                "state_digest": row["state_digest"],
            }
            for row in candidates
        ],
    }


def create_recovery_job(
    session: Session,
    *,
    action: str,
    filters: Any,
    candidate_digest: str,
    actor: str,
    reason: str,
    idempotency_key: str,
    preview_expires_at: datetime | None = None,
) -> AttendanceRecoveryJob:
    if not settings.attendance_recovery_execution_enabled:
        raise RecoveryError("Attendance recovery execution is disabled.", "EXECUTION_DISABLED")
    existing = session.scalar(
        select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.actor != actor or existing.action != action or existing.candidate_digest != candidate_digest or existing.reason != reason.strip():
            raise RecoveryError("Idempotency key was already used for another batch.", "IDEMPOTENCY_CONFLICT")
        return existing
    if preview_expires_at is not None and ensure_utc(preview_expires_at) <= utc_now():
        raise RecoveryError("The recovery preview has expired; generate a new preview.", "PREVIEW_EXPIRED")
    preview = build_recovery_preview(session, action=action, filters=filters)
    if preview["candidate_digest"] != candidate_digest:
        raise RecoveryError("The recovery scope changed; refresh the preview.", "PREVIEW_DRIFT")
    if preview_expires_at is not None:
        preview["preview_expires_at"] = ensure_utc(preview_expires_at)
    candidates = preview["_candidate_rows"]
    if not candidates:
        raise RecoveryError("No event is eligible for the selected action.", "NO_ELIGIBLE_EVENTS")
    if any(not _execution_allowed(row["zone_id"]) for row in candidates):
        raise RecoveryError("The batch contains a zone outside the enabled recovery canary.", "ZONE_NOT_ENABLED")
    filter_values = _filters_dict(filters)
    job = AttendanceRecoveryJob(
        action=action,
        status="QUEUED",
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        scope=filter_values,
        candidate_digest=candidate_digest,
        preview_expires_at=preview["preview_expires_at"],
        requested_count=len(candidates),
        eligible_count=len(candidates),
        excluded_count=preview["counts"]["excluded"],
        identity_held_count=preview["counts"]["identity_held"],
        review_count=preview["counts"]["permanent_review"] + preview["counts"]["source_correction"],
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(AttendanceRecoveryJob).where(
                AttendanceRecoveryJob.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
        raise
    for row in candidates:
        session.add(
            AttendanceRecoveryItem(
                job_id=job.id,
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                connector_id=session.scalar(select(Connector.id).where(Connector.connector_id == row["connector_id"])),
                attendance_event_id=row["attendance_event_id"],
                expected_state_digest=row["state_digest"],
                status="PENDING",
                lane=row["lane"],
                result={
                    "event_uid": row["event_uid"],
                    "terminal_serial": row["terminal_serial"],
                    "source_epoch": row.get("source_epoch"),
                    "generation": row.get("generation"),
                    "source_digest": row.get("source_digest"),
                    "source_event_id": row.get("source_event_id"),
                },
            )
        )
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_RECOVERY_JOB_CREATED",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="QUEUED",
        after={"action": action, "requested_count": len(candidates), "candidate_digest": candidate_digest},
        request_id=idempotency_key,
    )
    return job


def _serialize_item(item: AttendanceRecoveryItem) -> dict:
    return {
        "item_id": item.item_id,
        "source_kind": item.source_kind,
        "source_ref": item.source_ref,
        "attendance_event_id": item.attendance_event_id,
        "manifest_id": item.manifest_id,
        "hikvision_evidence_id": item.hikvision_evidence_id,
        "lane": item.lane,
        "status": item.status,
        "outcome": item.outcome,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "attempt_count": item.attempt_count,
        "corrected_device_time": item.corrected_device_time,
        "result": item.result or {},
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "updated_at": item.updated_at,
    }


def serialize_recovery_job(session: Session, job: AttendanceRecoveryJob, *, include_items: bool = False) -> dict:
    result = {
        "job_id": job.job_id,
        "action": job.action,
        "status": job.status,
        "actor": job.actor,
        "reason": job.reason,
        "scope": job.scope or {},
        "candidate_digest": job.candidate_digest,
        "preview_expires_at": job.preview_expires_at,
        "progress": {
            "requested": job.requested_count,
            "eligible": job.eligible_count,
            "excluded": job.excluded_count,
            "identity_held": job.identity_held_count,
            "review": job.review_count,
            "succeeded": job.succeeded_count,
            "failed": job.failed_count,
            "skipped": job.skipped_count,
            "cursor": job.cursor,
        },
        "lease": {"owner": job.lease_owner, "until": job.lease_until},
        "last_error": job.last_error,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
    }
    if include_items:
        result["items"] = [
            _serialize_item(item)
            for item in session.scalars(
                select(AttendanceRecoveryItem)
                .where(AttendanceRecoveryItem.job_id == job.id)
                .order_by(AttendanceRecoveryItem.id.asc())
                .limit(5_000)
            ).all()
        ]
    return result


def list_recovery_items(session: Session, job: AttendanceRecoveryJob, *, limit: int = 100, cursor: int | None = None) -> dict:
    statement = select(AttendanceRecoveryItem).where(AttendanceRecoveryItem.job_id == job.id)
    if cursor:
        statement = statement.where(AttendanceRecoveryItem.id < cursor)
    rows = session.scalars(statement.order_by(AttendanceRecoveryItem.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    return {
        "rows": [_serialize_item(row) for row in page],
        "next_cursor": page[-1].id if len(rows) > limit else None,
        "filtered_total": int(session.scalar(select(func.count(AttendanceRecoveryItem.id)).where(AttendanceRecoveryItem.job_id == job.id)) or 0),
    }


def _mark_item(item: AttendanceRecoveryItem, *, status: str, outcome: str, error_code: str | None = None, error_message: str | None = None, result: dict | None = None) -> None:
    item.status = status
    item.outcome = outcome
    item.error_code = error_code
    item.error_message = error_message
    if result is not None:
        item.result = result
    item.completed_at = utc_now()
    item.updated_at = utc_now()


def _current_event(session: Session, item: AttendanceRecoveryItem) -> tuple[AttendanceEvent | None, OrdsOutbox | None, Connector | None]:
    outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == item.attendance_event_id).with_for_update())
    event = session.scalar(select(AttendanceEvent).where(AttendanceEvent.id == item.attendance_event_id).with_for_update()) if item.attendance_event_id else None
    if event is None:
        return None, None, None
    return event, outbox, session.get(Connector, event.connector_id)


def _process_delivery_item(session: Session, job: AttendanceRecoveryJob, item: AttendanceRecoveryItem, now: datetime) -> None:
    event, outbox, connector = _current_event(session, item)
    if event is None or connector is None:
        _mark_item(item, status="SKIPPED", outcome="SOURCE_MISSING", error_code="SOURCE_MISSING", error_message="The preserved attendance event is no longer available.")
        job.skipped_count += 1
        return
    if _recovery_state_digest(session, event, outbox, connector) != item.expected_state_digest:
        _mark_item(item, status="SKIPPED", outcome="STATE_DRIFT", error_code="STATE_DRIFT", error_message="The event changed after preview; refresh before retrying.")
        job.skipped_count += 1
        return
    if not _execution_allowed(connector.zone_id):
        raise RecoveryError("Recovery is disabled for this zone.", "ZONE_NOT_ENABLED")
    lane, reason = _event_lane(session, event, outbox, connector, now=now)
    if not _action_eligible(job.action, _candidate_row(event, outbox, connector, lane, reason, now), outbox, now):
        _mark_item(item, status="SKIPPED", outcome="NO_LONGER_ELIGIBLE", error_code="NO_LONGER_ELIGIBLE", error_message=reason)
        job.skipped_count += 1
        return
    identity_repaired = False
    if connector.firmware_family != "hikvision" and (
        not event.cnic_lookup_hash or not event.cnic_encrypted
        or event.identity_resolution_status not in VERIFIED_IDENTITY_RESOLUTION_STATUSES
        or event.ords_status == "BLOCKED_IDENTITY"
    ):
        identity_repaired = _apply_verified_identity(session, event, outbox, connector)
        if not identity_repaired:
            raise RecoveryError("Exact identity evidence is no longer available.", "IDENTITY_HELD")
    if job.action == "RECOVER_STALE_IN_FLIGHT" and outbox is not None and outbox.status == "IN_FLIGHT":
        outbox.status = "PENDING"
    event.ords_status = "PENDING"
    outbox, created = ensure_attendance_ords_outbox(session, event, status="PENDING")
    outbox.next_attempt_at = now
    outbox.last_error = outbox.last_error or None
    session.flush()
    item.attempt_count += 1
    _mark_item(
        item,
        status="SUCCEEDED",
        outcome="QUEUED_FOR_ORDS",
        result={
            "outbox_id": outbox.id,
            "created": created,
            "identity_repaired": identity_repaired,
        },
    )
    job.succeeded_count += 1


def _exact_device_user(session: Session, manifest: TerminalRecordManifest) -> DeviceUser | None:
    if not manifest.observed_user_id:
        return None
    rows = session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == manifest.zkt_device_id,
            DeviceUser.user_id == manifest.observed_user_id,
            or_(DeviceUser.uid == manifest.observed_uid, manifest.observed_uid is None),
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.identity_conflict_code.is_(None),
            DeviceUser.cnic_lookup_hash.is_not(None),
        )
    ).all()
    if len(rows) != 1:
        return None
    user = rows[0]
    reused = session.scalar(select(DeviceUser.id).where(DeviceUser.zkt_device_id == manifest.zkt_device_id,
                            DeviceUser.user_id == manifest.observed_user_id, DeviceUser.id != user.id).limit(1))
    return user if not reused and user.cnic_encrypted else None


def _decode_zkt_record(manifest: TerminalRecordManifest) -> dict:
    if not manifest.protected_raw_record or manifest.record_size not in {8, 16, 40}:
        raise RecoveryError("The preserved ZKT record cannot be decoded safely.", "SOURCE_NOT_PARSEABLE")
    try:
        raw = base64.b64decode(decrypt_text(manifest.protected_raw_record), validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise RecoveryError("The preserved ZKT evidence failed integrity decoding.", "SOURCE_EVIDENCE_INVALID") from exc
    if hashlib.sha256(raw).hexdigest() != manifest.raw_record_digest:
        raise RecoveryError("The preserved source digest does not match its bytes.", "SOURCE_EVIDENCE_INVALID")
    if len(raw) != manifest.record_size:
        raise RecoveryError("The preserved ZKT record size does not match its manifest.", "SOURCE_EVIDENCE_INVALID")
    if manifest.record_size == 8:
        uid = str(int.from_bytes(raw[0:2], "little"))
        user_id = manifest.observed_user_id
        timestamp = int.from_bytes(raw[3:7], "little")
        status, punch = raw[2], raw[7]
    elif manifest.record_size == 16:
        uid = manifest.observed_uid or ""
        user_id = str(int.from_bytes(raw[0:4], "little"))
        timestamp = int.from_bytes(raw[4:8], "little")
        status, punch = raw[8], raw[9]
    else:
        uid = str(int.from_bytes(raw[0:2], "little"))
        try:
            user_id = raw[2:26].split(b"\x00", 1)[0].decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RecoveryError("Source identity bytes are malformed.", "SOURCE_IDENTITY_INVALID") from exc
        if not user_id or any(ord(c) < 32 for c in user_id):
            raise RecoveryError("Source identity is unparseable.", "SOURCE_IDENTITY_INVALID")
        timestamp = int.from_bytes(raw[27:31], "little")
        status, punch = raw[26], raw[31]
    if manifest.observed_uid and uid and uid != manifest.observed_uid:
        raise RecoveryError("The raw ZKT UID does not match the preserved manifest.", "SOURCE_IDENTITY_DRIFT")
    if manifest.observed_user_id and user_id and user_id != manifest.observed_user_id:
        raise RecoveryError("The raw ZKT user ID does not match the preserved manifest.", "SOURCE_IDENTITY_DRIFT")
    return {"uid": uid, "user_id": user_id, "raw_timestamp": timestamp, "status": str(status), "punch": str(punch), "raw_punch": False}


def _correction_reference(source_kind: str, source_ref: str) -> str:
    prefix = "ZKT_MANIFEST:" if source_kind == "ZKT_MANIFEST" else "HIKVISION_EVIDENCE:"
    if source_ref.startswith(prefix):
        source_ref = source_ref[len(prefix):]
    if not source_ref.isascii() or not source_ref.isdigit() or int(source_ref) < 1:
        raise RecoveryError("The source reference is invalid.", "SOURCE_REFERENCE_INVALID")
    return prefix + str(int(source_ref))


def _correction_candidate(session: Session, source_kind: str, source_ref: str, corrected: datetime) -> dict:
    source_ref = _correction_reference(source_kind, source_ref)
    corrected = ensure_utc(corrected)
    if corrected < MIN_CORRECTION_TIME or corrected > utc_now() + timedelta(days=1):
        raise RecoveryError("Corrected device time is outside the permitted clock window.", "CORRECTED_TIME_INVALID")
    if source_kind == "ZKT_MANIFEST":
        manifest_id = int(source_ref.split(":", 1)[1])
        manifest = session.get(TerminalRecordManifest, manifest_id)
        if manifest is None or not manifest.canonical_source or manifest.disposition not in {"INVALID_TIME", "MALFORMED"}:
            raise RecoveryError("The ZKT source exception is not eligible for correction.", "SOURCE_NOT_CORRECTABLE")
        if manifest.disposition == "MALFORMED" and "TIME" not in (manifest.error_code or "").upper():
            raise RecoveryError("Only timestamp-related malformed rows may be corrected.", "SOURCE_NOT_CORRECTABLE")
        user = _exact_device_user(session, manifest)
        if user is None:
            raise RecoveryError("The source identity is not uniquely backed by an active CNIC-bearing user.", "IDENTITY_HELD")
        connector = session.get(Connector, manifest.connector_id)
        terminal = connector.zkt_device if connector else None
        epoch = session.get(TerminalSourceEpoch, manifest.source_epoch_id) if manifest.source_epoch_id else None
        if (not connector or connector.firmware_family != "zkt" or not terminal
                or terminal.id != manifest.zkt_device_id or terminal.serial != manifest.terminal_serial
                or terminal.confirmed_serial != manifest.terminal_serial
                or not epoch or epoch.zkt_device_id != terminal.id
                or epoch.terminal_generation != manifest.generation or epoch.state != "ACTIVE"):
            raise RecoveryError("The source terminal or epoch requires review.", "SOURCE_SCOPE_MISMATCH")
        if (not terminal.identity_snapshot_stable or not terminal.snapshot_complete
                or user.snapshot_revision is None
                or terminal.identity_snapshot_revision is None
                or user.snapshot_revision != terminal.identity_snapshot_revision
                or not terminal.last_identity_change_at or not terminal.identity_snapshot_observed_at
                or corrected < ensure_utc(terminal.last_identity_change_at)
                or corrected > ensure_utc(terminal.identity_snapshot_observed_at) + timedelta(seconds=settings.identity_snapshot_capture_tolerance_seconds)):
            raise RecoveryError("Exact identity evidence does not cover the corrected event time.", "IDENTITY_HELD")
        try:
            authoritative_cnic = decrypt_cnic(user.cnic_encrypted)
        except Exception as exc:  # malformed protected evidence remains review-only
            raise RecoveryError(
                "Authoritative CNIC evidence is unavailable.", "IDENTITY_HELD"
            ) from exc
        if not authoritative_cnic:
            raise RecoveryError("Authoritative CNIC evidence is unavailable.", "IDENTITY_HELD")
        try:
            if cnic_lookup(authoritative_cnic) != user.cnic_lookup_hash:
                raise RecoveryError("Authoritative CNIC evidence is unavailable.", "IDENTITY_HELD")
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError("Authoritative CNIC evidence is unavailable.", "IDENTITY_HELD") from exc
        decoded = _decode_zkt_record(manifest)
        if decoded["user_id"] and decoded["user_id"] != manifest.observed_user_id:
            raise RecoveryError("The decoded source identity changed.", "SOURCE_IDENTITY_DRIFT")
        if not attendance_device_time_is_plausible(corrected, manifest.created_at):
            raise RecoveryError("Corrected device time is not plausible for the preserved capture.", "CORRECTED_TIME_INVALID")
        return {
            "source_kind": source_kind,
            "source_ref": source_ref,
            "manifest_id": manifest.id,
            "connector_id": manifest.connector_id,
            "zkt_device_id": manifest.zkt_device_id,
            "terminal_serial": manifest.terminal_serial,
            "firmware_family": connector.firmware_family,
            "zone_id": connector.zone_id,
            "source_epoch": epoch.epoch_id,
            "generation": manifest.generation,
            "original_digest": manifest.raw_record_digest,
            "captured_at": ensure_utc(manifest.created_at),
            "user": user,
            "decoded": decoded,
            "corrected_device_time": corrected,
            "state_digest": _digest({"manifest": manifest.id, "digest": manifest.raw_record_digest,
                "disposition": manifest.disposition, "epoch": epoch.id, "epoch_state": epoch.state,
                "generation": manifest.generation, "ordinal": manifest.ordinal,
                "serial": manifest.terminal_serial, "user": user.id, "cnic_hash": user.cnic_lookup_hash,
                "snapshot": terminal.identity_snapshot_id, "revision": user.snapshot_revision}),
        }
    evidence_id = int(source_ref.split(":", 1)[1])
    evidence = session.get(HikvisionEvidence, evidence_id)
    if evidence is None or evidence.disposition != "INVALID_TIME":
        raise RecoveryError("The Hikvision evidence is not eligible for correction.", "SOURCE_NOT_CORRECTABLE")
    existing = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == evidence.event_uid))
    if existing is None or not existing.cnic_lookup_hash or existing.ords_status != "QUARANTINED_INVALID_DEVICE_TIME":
        raise RecoveryError("Hikvision identity or source state is not uniquely correctable.", "IDENTITY_HELD")
    connector = session.get(Connector, evidence.connector_id)
    from zk_add.hikvision_delivery import HikvisionPolicy
    from zk_add.hikvision_protocol import normalize_observation
    from zk_add.hikvision_probe import decode_body
    policy = session.get(HikvisionPolicy, evidence.connector_id)
    if (not connector or connector.firmware_family != "hikvision" or not policy or not policy.enabled
            or policy.source_epoch != evidence.source_epoch or policy.terminal_serial != evidence.terminal_serial
            or not _terminal_provenance_verified(existing, connector) or existing.connector_id != evidence.connector_id
            or existing.identity_resolution_status not in VERIFIED_IDENTITY_RESOLUTION_STATUSES
            or not existing.cnic_encrypted or existing.oracle_confirmed_at):
        raise RecoveryError("The HIK source or identity requires review.", "SOURCE_SCOPE_MISMATCH")
    try:
        authoritative_cnic = decrypt_cnic(existing.cnic_encrypted)
    except Exception as exc:  # malformed protected identity remains review-only
        raise RecoveryError("The HIK identity evidence requires review.", "IDENTITY_HELD") from exc
    try:
        authoritative_hash = cnic_lookup(authoritative_cnic)
    except Exception as exc:
        raise RecoveryError("The HIK identity evidence requires review.", "IDENTITY_HELD") from exc
    if not authoritative_cnic or authoritative_hash != existing.cnic_lookup_hash:
        raise RecoveryError("The HIK identity evidence requires review.", "IDENTITY_HELD")
    try:
        raw = decrypt_text(evidence.raw_encrypted)
        if hashlib.sha256(raw.encode()).hexdigest() != evidence.observation_sha256:
            raise ValueError("digest")
        observation = normalize_observation(decode_body(raw.encode()), terminal_serial=evidence.terminal_serial,
                                            source_epoch=evidence.source_epoch)
        if (observation.event_uid != existing.event_uid or observation.immutable_facts_digest != evidence.immutable_digest
                or observation.employee_no != existing.user_id
                or [observation.major, observation.minor] not in policy.success_codes):
            raise ValueError("facts")
    except Exception as exc:
        raise RecoveryError("The HIK source digest or facts failed verification.", "SOURCE_EVIDENCE_INVALID") from exc
    if session.scalar(select(HikvisionEvidence.id).where(HikvisionEvidence.event_uid == evidence.event_uid,
            HikvisionEvidence.immutable_digest != evidence.immutable_digest).limit(1)):
        raise RecoveryError("Conflicting source observations require review.", "SOURCE_FACT_CONFLICT")
    if not attendance_device_time_is_plausible(corrected, existing.captured_at):
        raise RecoveryError("Corrected device time is not plausible for the preserved capture.", "CORRECTED_TIME_INVALID")
    return {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "hikvision_evidence_id": evidence.id,
        "connector_id": evidence.connector_id,
        "zkt_device_id": existing.zkt_device_id,
        "terminal_serial": evidence.terminal_serial,
        "firmware_family": connector.firmware_family,
        "zone_id": connector.zone_id,
        "source_epoch": evidence.source_epoch,
        "generation": None,
        "original_digest": evidence.immutable_digest or evidence.observation_sha256,
        "captured_at": existing.captured_at,
        "existing_event_id": existing.id,
        "user": None,
        "decoded": {"status": existing.status, "punch": existing.punch, "raw_punch": existing.raw_punch},
        "corrected_device_time": corrected,
        "state_digest": _digest({"event": _event_state_digest(existing, session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == existing.id))),
            "observation": evidence.observation_sha256, "facts": evidence.immutable_digest,
            "epoch": evidence.source_epoch, "policy_revision": policy.mapping_revision}),
    }


def build_source_correction_preview(session: Session, corrections: list[Any]) -> dict:
    if not settings.attendance_recovery_preview_enabled:
        raise RecoveryError("Attendance recovery preview is disabled.", "PREVIEW_DISABLED")
    candidates: list[dict] = []
    rejected: list[dict] = []
    if len(corrections) > settings.attendance_recovery_max_correction_items:
        raise RecoveryError("Too many correction rows; split this batch.", "SCOPE_TOO_LARGE")
    seen: set[str] = set()
    for item in corrections:
        source_kind = item.source_kind if hasattr(item, "source_kind") else item["source_kind"]
        source_ref = item.source_ref if hasattr(item, "source_ref") else item["source_ref"]
        corrected = item.corrected_device_time if hasattr(item, "corrected_device_time") else item["corrected_device_time"]
        try:
            reference = _correction_reference(source_kind, source_ref)
            if reference in seen:
                raise RecoveryError("A source reference may appear only once in a correction batch.", "DUPLICATE_SOURCE")
            seen.add(reference)
            candidates.append(_correction_candidate(session, source_kind, source_ref, corrected))
        except RecoveryError as exc:
            rejected.append({"source_kind": source_kind, "source_ref": source_ref, "code": exc.code, "reason": str(exc)})
    material = [
        {
            "source_kind": row["source_kind"],
            "source_ref": row["source_ref"],
            "original_digest": row["original_digest"],
            "corrected_device_time": row["corrected_device_time"].isoformat(),
            "state_digest": row["state_digest"],
        }
        for row in candidates
    ]
    return {
        "schema_version": "1",
        "candidate_digest": _digest(material),
        "counts": {"submitted": len(corrections), "eligible": len(candidates), "rejected": len(rejected)},
        "rows": [
            {
                "source_kind": row["source_kind"],
                "source_ref": row["source_ref"],
                "original_digest": row["original_digest"],
                "terminal_serial": row["terminal_serial"],
                "corrected_device_time": _iso(row["corrected_device_time"]),
                "state_digest": row["state_digest"],
            }
            for row in candidates
        ],
        # Kept inside the service boundary for job creation.  The HTTP route
        # removes this key so protected ORM values never become preview JSON.
        "_candidate_rows": candidates,
        "rejected": rejected,
        "preview_expires_at": utc_now() + timedelta(seconds=settings.attendance_recovery_preview_seconds),
    }


def list_source_correction_candidates(session: Session, *, limit: int = 100) -> dict:
    """List non-secret HIK evidence that can enter the correction preview.

    ZKT exceptions already have the established ``/api/v1/source-exceptions``
    listing.  Hikvision evidence has a separate immutable ledger, so expose a
    compact, digest-only listing for the same Recovery screen.
    """

    rows = session.execute(
        select(HikvisionEvidence, Connector, AttendanceEvent)
        .join(Connector, Connector.id == HikvisionEvidence.connector_id)
        .outerjoin(AttendanceEvent, AttendanceEvent.event_uid == HikvisionEvidence.event_uid)
        .where(
            HikvisionEvidence.disposition.in_(
                (
                    "INVALID_TIME",
                    "SOURCE_FACT_CONFLICT",
                    "SOURCE_EPOCH_REVIEW",
                    "UNCLASSIFIED_SOURCE",
                    "QUALIFICATION_PENDING",
                )
            )
        )
        .order_by(HikvisionEvidence.id.asc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return {
        "schema_version": "1",
        "rows": [
            {
                "id": evidence.id,
                "source_kind": "HIKVISION_EVIDENCE",
                "source_ref": f"HIKVISION_EVIDENCE:{evidence.id}",
                "connector_id": connector.connector_id,
                "display_name": connector.display_name,
                "zone_id": connector.zone_id,
                "terminal_serial": evidence.terminal_serial,
                "source_epoch": evidence.source_epoch,
                "event_uid": evidence.event_uid,
                "source_event_id": evidence.source_event_id,
                "observation_sha256": evidence.observation_sha256,
                "original_digest": evidence.immutable_digest,
                "captured_epoch": evidence.captured_epoch,
                "disposition": evidence.disposition,
                "identity_available": bool(event and event.cnic_lookup_hash),
                "eligible": bool(
                    event
                    and event.cnic_lookup_hash
                    and evidence.disposition == "INVALID_TIME"
                    and event.ords_status == "QUARANTINED_INVALID_DEVICE_TIME"
                ),
            }
            for evidence, connector, event in rows
        ],
    }


def hikvision_source_evidence_detail(session: Session, evidence_id: int) -> dict:
    """Return non-secret HIK source evidence metadata for an operator drawer."""

    evidence = session.get(HikvisionEvidence, evidence_id)
    if evidence is None:
        raise RecoveryError("Hikvision source evidence was not found.", "SOURCE_MISSING")
    connector = session.get(Connector, evidence.connector_id)
    event = session.scalar(
        select(AttendanceEvent).where(AttendanceEvent.event_uid == evidence.event_uid)
    ) if evidence.event_uid else None
    return {
        "id": evidence.id,
        "source_kind": "HIKVISION_EVIDENCE",
        "source_ref": f"HIKVISION_EVIDENCE:{evidence.id}",
        "connector_id": connector.connector_id if connector else None,
        "display_name": connector.display_name if connector else None,
        "zone_id": connector.zone_id if connector else None,
        "terminal_serial": evidence.terminal_serial,
        "source_epoch": evidence.source_epoch,
        "event_uid": evidence.event_uid,
        "source_event_id": evidence.source_event_id,
        "channel": evidence.channel,
        "disposition": evidence.disposition,
        "observation_sha256": evidence.observation_sha256,
        "original_digest": evidence.immutable_digest,
        "captured_epoch": evidence.captured_epoch,
        "identity_available": bool(event and event.cnic_lookup_hash),
        "event_status": event.ords_status if event else None,
        "event_id": event.id if event else None,
    }


def reveal_hikvision_source_evidence(
    session: Session,
    *,
    evidence_id: int,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> dict:
    """Reveal protected HIK bytes only through an audited, no-store path."""

    evidence = session.get(HikvisionEvidence, evidence_id)
    if evidence is None:
        raise RecoveryError("Hikvision source evidence was not found.", "SOURCE_MISSING")
    try:
        raw = decrypt_text(evidence.raw_encrypted)
        raw_bytes = raw.encode()
    except Exception as exc:
        raise RecoveryError("Protected HIK source evidence is unavailable.", "SOURCE_EVIDENCE_INVALID") from exc
    if hashlib.sha256(raw_bytes).hexdigest() != evidence.observation_sha256:
        raise RecoveryError("Protected HIK source evidence failed its digest check.", "SOURCE_EVIDENCE_INVALID")
    existing = session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "HIKVISION_SOURCE_EVIDENCE_REVEALED",
            AuditEvent.target_id == str(evidence.id),
            AuditEvent.request_id == idempotency_key,
        )
    )
    if existing is None:
        append_audit(
            session,
            actor=actor,
            action="HIKVISION_SOURCE_EVIDENCE_REVEALED",
            target_type="hikvision_source_evidence",
            target_id=str(evidence.id),
            outcome="SUCCESS",
            after={"reason": reason.strip(), "revealed_at": utc_now().isoformat()},
            request_id=idempotency_key,
        )
    detail = hikvision_source_evidence_detail(session, evidence_id)
    return {
        **detail,
        "raw": raw,
        "raw_sha256": evidence.observation_sha256,
    }


def create_source_correction_job(
    session: Session,
    *,
    corrections: list[Any],
    candidate_digest: str,
    actor: str,
    reason: str,
    idempotency_key: str,
    preview_expires_at: datetime | None = None,
) -> AttendanceRecoveryJob:
    if not settings.attendance_source_correction_enabled:
        raise RecoveryError("Timestamp correction execution is disabled.", "CORRECTION_DISABLED")
    if not settings.attendance_recovery_execution_enabled:
        raise RecoveryError("Attendance recovery execution is disabled.", "EXECUTION_DISABLED")
    existing = session.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.idempotency_key == idempotency_key))
    if existing:
        if existing.actor != actor or existing.action != CORRECTION_ACTION or existing.candidate_digest != candidate_digest or existing.reason != reason.strip():
            raise RecoveryError("Idempotency key was already used for another batch.", "IDEMPOTENCY_CONFLICT")
        return existing
    if preview_expires_at is not None and ensure_utc(preview_expires_at) <= utc_now():
        raise RecoveryError("The correction preview has expired; generate a new preview.", "PREVIEW_EXPIRED")
    preview = build_source_correction_preview(session, corrections)
    if preview["candidate_digest"] != candidate_digest:
        raise RecoveryError("The correction scope changed; refresh the preview.", "PREVIEW_DRIFT")
    if preview_expires_at is not None:
        preview["preview_expires_at"] = ensure_utc(preview_expires_at)
    candidate_rows = preview["_candidate_rows"]
    if not candidate_rows:
        raise RecoveryError("No correction row is safe to create.", "NO_ELIGIBLE_CORRECTIONS")
    for row in candidate_rows:
        connector = session.get(Connector, row["connector_id"])
        if connector is None or not _execution_allowed(connector.zone_id, correction=True):
            raise RecoveryError("The batch contains a zone outside the enabled correction canary.", "ZONE_NOT_ENABLED")
    scope_rows = [
        {
            "source_kind": row["source_kind"],
            "source_ref": row["source_ref"],
            "manifest_id": row.get("manifest_id"),
            "hikvision_evidence_id": row.get("hikvision_evidence_id"),
            "connector_id": row["connector_id"],
            "firmware_family": row["firmware_family"],
            "zone_id": row["zone_id"],
            "terminal_serial": row["terminal_serial"],
            "source_epoch": row.get("source_epoch"),
            "generation": row.get("generation"),
            "original_digest": row["original_digest"],
            "corrected_device_time": _iso(row["corrected_device_time"]),
            "state_digest": row["state_digest"],
        }
        for row in candidate_rows
    ]
    job = AttendanceRecoveryJob(
        action=CORRECTION_ACTION,
        status="QUEUED",
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        scope={"corrections": scope_rows},
        candidate_digest=candidate_digest,
        preview_expires_at=preview["preview_expires_at"],
        requested_count=len(candidate_rows),
        eligible_count=len(candidate_rows),
        excluded_count=preview["counts"]["rejected"],
        review_count=preview["counts"]["rejected"],
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(AttendanceRecoveryJob).where(
                AttendanceRecoveryJob.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
        raise
    for row in candidate_rows:
        session.add(
            AttendanceRecoveryItem(
                job_id=job.id,
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                connector_id=row["connector_id"],
                manifest_id=int(row["source_ref"].split(":", 1)[1]) if row["source_kind"] == "ZKT_MANIFEST" else None,
                hikvision_evidence_id=int(row["source_ref"].split(":", 1)[1]) if row["source_kind"] == "HIKVISION_EVIDENCE" else None,
                expected_state_digest=row["state_digest"],
                status="PENDING",
                lane=SOURCE_LANE,
                corrected_device_time=row["corrected_device_time"],
                result={
                    "original_digest": row["original_digest"],
                    "terminal_serial": row["terminal_serial"],
                    "connector_id": row["connector_id"],
                    "firmware_family": row["firmware_family"],
                    "zone_id": row["zone_id"],
                    "source_epoch": row.get("source_epoch"),
                    "generation": row.get("generation"),
                },
            )
        )
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_SOURCE_CORRECTION_JOB_CREATED",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="QUEUED",
        after={"requested_count": len(candidate_rows), "candidate_digest": candidate_digest},
        request_id=idempotency_key,
    )
    return job


def _create_timestamp_correction(session: Session, job: AttendanceRecoveryJob, item: AttendanceRecoveryItem) -> None:
    if not item.corrected_device_time:
        raise RecoveryError("A corrected timestamp is required.", "CORRECTED_TIME_REQUIRED")
    connector = session.scalar(select(Connector).where(Connector.id == item.connector_id).with_for_update())
    if connector is None or not _execution_allowed(connector.zone_id, correction=True):
        raise RecoveryError("Timestamp correction is disabled for this zone.", "CORRECTION_DISABLED")
    source_model = TerminalRecordManifest if item.source_kind == "ZKT_MANIFEST" else HikvisionEvidence
    source_id = item.manifest_id if item.source_kind == "ZKT_MANIFEST" else item.hikvision_evidence_id
    session.scalar(select(source_model).where(source_model.id == source_id).with_for_update())
    candidate = _correction_candidate(session, item.source_kind, item.source_ref, item.corrected_device_time)
    if candidate["state_digest"] != item.expected_state_digest:
        raise RecoveryError("The source changed after preview; refresh the correction.", "STATE_DRIFT")
    existing_correction = session.scalar(
        select(AttendanceSourceCorrection)
        .where(
            AttendanceSourceCorrection.source_kind == item.source_kind,
            AttendanceSourceCorrection.source_ref == item.source_ref,
            AttendanceSourceCorrection.status == "CREATED",
        )
        .order_by(AttendanceSourceCorrection.correction_version.desc())
    )
    if item.source_kind == "HIKVISION_EVIDENCE" and not existing_correction:
        evidence = session.get(HikvisionEvidence, item.hikvision_evidence_id)
        existing_correction = session.scalar(select(AttendanceSourceCorrection).join(HikvisionEvidence,
            HikvisionEvidence.id == AttendanceSourceCorrection.hikvision_evidence_id).where(
            HikvisionEvidence.event_uid == evidence.event_uid, AttendanceSourceCorrection.status == "CREATED"))
    if existing_correction:
        item.attempt_count += 1
        _mark_item(item, status="SKIPPED", outcome="ALREADY_CORRECTED", result={"correction_id": existing_correction.correction_id})
        job.skipped_count += 1
        return
    version = int(
        session.scalar(
            select(func.max(AttendanceSourceCorrection.correction_version)).where(
                AttendanceSourceCorrection.source_kind == item.source_kind,
                AttendanceSourceCorrection.source_ref == item.source_ref,
            )
        )
        or 0
    ) + 1
    derived_uid = _digest([
        "attendance-source-correction-v1",
        item.source_kind,
        item.source_ref,
        candidate["original_digest"],
        version,
        ensure_utc(item.corrected_device_time).isoformat(),
    ])
    event = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == derived_uid).with_for_update())
    if event is None:
        user = candidate.get("user")
        if user is not None:
            cnic_encrypted = user.cnic_encrypted
            cnic_lookup_hash = user.cnic_lookup_hash
            cnic_last4 = user.cnic_last4
            display_name = user.display_name
            user_id = user.user_id
            uid = user.uid
            raw_punch = user.shift_worker
            zkt_device_id = candidate["zkt_device_id"]
            identity_snapshot_id = session.get(Connector, candidate["connector_id"]).zkt_device.identity_snapshot_id if session.get(Connector, candidate["connector_id"]).zkt_device else None
        else:
            source_event = session.get(AttendanceEvent, candidate["existing_event_id"])
            if source_event is None:
                raise RecoveryError("The existing Hikvision event disappeared.", "SOURCE_MISSING")
            cnic_encrypted = source_event.cnic_encrypted
            cnic_lookup_hash = source_event.cnic_lookup_hash
            cnic_last4 = source_event.cnic_last4
            display_name = source_event.display_name
            user_id = source_event.user_id
            uid = source_event.uid
            raw_punch = source_event.raw_punch
            zkt_device_id = source_event.zkt_device_id
            identity_snapshot_id = source_event.identity_snapshot_id
        event = AttendanceEvent(
            event_uid=derived_uid,
            connector_id=candidate["connector_id"],
            zkt_device_id=zkt_device_id,
            device_user_id=user.id if user is not None else None,
            identity_snapshot_id=identity_snapshot_id,
            identity_terminal_fingerprint=user.terminal_identity_fingerprint if user is not None else None,
            identity_resolution_status="RESOLVED",
            identity_resolved_at=utc_now(),
            identity_repaired_at=utc_now(),
            identity_repair_reason="OPERATOR_TIMESTAMP_CORRECTION",
            device_serial=candidate["terminal_serial"],
            uid=uid,
            user_id=user_id,
            display_name=display_name,
            cnic_encrypted=cnic_encrypted,
            cnic_lookup_hash=cnic_lookup_hash,
            cnic_last4=cnic_last4,
            device_event_time=ensure_utc(item.corrected_device_time),
            captured_at=candidate["captured_at"],
            source="FULL_HISTORY",
            status=candidate["decoded"].get("status"),
            punch=candidate["decoded"].get("punch"),
            raw_punch=raw_punch,
            clock_quality="OPERATOR_CORRECTED",
            clock_drift_seconds=None,
            raw_event={
                "source_correction": {
                    "source_kind": item.source_kind,
                    "source_ref": item.source_ref,
                    "original_digest": candidate["original_digest"],
                    "correction_version": version,
                    "terminal_serial": candidate["terminal_serial"],
                    "source_epoch": candidate.get("source_epoch"),
                    "generation": candidate.get("generation"),
                    "operator": job.actor,
                    "reason": job.reason,
                }
            },
            ords_status="PENDING",
        )
        session.add(event)
        session.flush()
        ensure_attendance_ords_outbox(session, event, status="PENDING")
    correction = AttendanceSourceCorrection(
        source_kind=item.source_kind,
        source_ref=item.source_ref,
        connector_id=candidate["connector_id"],
        manifest_id=candidate.get("manifest_id"),
        hikvision_evidence_id=candidate.get("hikvision_evidence_id"),
        original_digest=candidate["original_digest"],
        correction_version=version,
        corrected_device_time=ensure_utc(item.corrected_device_time),
        status="CREATED",
        derived_event_uid=derived_uid,
        derived_attendance_event_id=event.id,
        actor=job.actor,
        reason=job.reason,
        idempotency_key=f"{job.job_id}:{item.item_id}",
    )
    session.add(correction)
    session.flush()
    item.attempt_count += 1
    _mark_item(item, status="SUCCEEDED", outcome="CORRECTION_CREATED", result={"correction_id": correction.correction_id, "derived_event_uid": derived_uid, "attendance_event_id": event.id})
    job.succeeded_count += 1


def advance_attendance_recovery_jobs(session: Session, *, limit: int | None = None, owner: str = "maintenance") -> int:
    if not settings.attendance_recovery_execution_enabled:
        return 0
    bounded = max(1, min(limit or settings.attendance_recovery_batch_size, settings.attendance_recovery_batch_size))
    now = utc_now()
    jobs = session.scalars(
        select(AttendanceRecoveryJob)
        .where(
            AttendanceRecoveryJob.status.in_(("QUEUED", "RUNNING")),
            AttendanceRecoveryJob.action != "SAFE_REPAIR",
            or_(AttendanceRecoveryJob.lease_until.is_(None), AttendanceRecoveryJob.lease_until <= now),
        )
        .order_by(AttendanceRecoveryJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).all()
    processed = 0
    for job in jobs:
        job.status = "RUNNING"
        job.lease_owner = owner
        job.lease_until = now + timedelta(seconds=settings.attendance_recovery_lease_seconds)
        job.started_at = job.started_at or now
        job.updated_at = now
        items = session.scalars(
            select(AttendanceRecoveryItem)
            .where(AttendanceRecoveryItem.job_id == job.id, AttendanceRecoveryItem.status == "PENDING")
            .order_by(AttendanceRecoveryItem.id.asc())
            .limit(bounded)
            .with_for_update(skip_locked=True)
        ).all()
        for item in items:
            try:
                with session.begin_nested():
                    if job.action == CORRECTION_ACTION:
                        _create_timestamp_correction(session, job, item)
                    else:
                        _process_delivery_item(session, job, item, now)
                    session.flush()
            except RecoveryError as exc:
                item.attempt_count += 1
                _mark_item(item, status="FAILED", outcome="REJECTED", error_code=exc.code, error_message=str(exc))
                job.failed_count += 1
                job.last_error = f"{exc.code}: {exc}"
            except Exception:  # pragma: no cover - defensive worker boundary
                item.attempt_count += 1
                _mark_item(item, status="FAILED", outcome="ERROR", error_code="RECOVERY_ITEM_ERROR", error_message="Recovery could not complete this item; inspect the worker logs using the job reference.")
                job.failed_count += 1
                job.last_error = "RECOVERY_ITEM_ERROR"
            append_audit(session, actor=job.actor, action="ATTENDANCE_RECOVERY_ITEM_PROCESSED",
                         target_type="attendance_recovery_item", target_id=item.item_id,
                         outcome=item.status, before={"state_digest": item.expected_state_digest},
                         after={"job_id": job.job_id, "outcome": item.outcome, "error_code": item.error_code,
                                "attempt": item.attempt_count, "result": item.result},
                         request_id=f"{item.item_id}:{item.attempt_count}")
            processed += 1
            job.cursor = item.id
        remaining = session.scalar(
            select(func.count(AttendanceRecoveryItem.id)).where(
                AttendanceRecoveryItem.job_id == job.id,
                AttendanceRecoveryItem.status == "PENDING",
            )
        ) or 0
        if not remaining:
            job.status = "COMPLETED_WITH_ATTENTION" if job.failed_count or job.skipped_count else "COMPLETED"
            job.completed_at = now
            job.lease_owner = None
            job.lease_until = None
        else:
            if job.status == "PAUSE_REQUESTED":
                job.status = "PAUSED"
            job.lease_owner = None
            job.lease_until = None
        job.updated_at = utc_now()
    return processed


def control_recovery_job(
    session: Session, *, job: AttendanceRecoveryJob, action: str, actor: str,
    reason: str, idempotency_key: str, candidate_digest: str,
    typed_confirmation: str,
) -> AttendanceRecoveryJob:
    if job.action == "SAFE_REPAIR":
        raise RecoveryError("Use the Repair attendance controls for this run.", "WORKFLOW_MISMATCH")
    job = session.scalar(select(AttendanceRecoveryJob).where(AttendanceRecoveryJob.id == job.id)
                         .with_for_update().execution_options(populate_existing=True))
    if candidate_digest != job.candidate_digest or typed_confirmation != f"{action.upper()} {job.job_id}":
        raise RecoveryError("Confirmation does not match this frozen job.", "CONFIRMATION_MISMATCH")
    prior = session.scalar(select(AuditEvent).where(AuditEvent.action == "ATTENDANCE_RECOVERY_JOB_CONTROLLED",
                           AuditEvent.target_id == job.job_id, AuditEvent.request_id == idempotency_key))
    if prior:
        if prior.actor != actor or prior.after.get("action") != action:
            raise RecoveryError("Control idempotency key was already used.", "IDEMPOTENCY_CONFLICT")
        return job
    if action in {"resume", "retry"} and not settings.attendance_recovery_execution_enabled:
        raise RecoveryError("Attendance recovery execution is disabled.", "EXECUTION_DISABLED")
    if action == "pause" and job.status in {"QUEUED", "RUNNING"}:
        # The job lock serializes this with the bounded worker transaction.
        # Once acquired, no item remains in an uncommitted in-flight batch.
        job.status = "PAUSED"
    elif action == "resume" and job.status == "PAUSED":
        job.status = "QUEUED"
    elif action == "cancel" and job.status in {"QUEUED", "RUNNING", "PAUSED"}:
        for item in session.scalars(
            select(AttendanceRecoveryItem)
            .where(
                AttendanceRecoveryItem.job_id == job.id,
                AttendanceRecoveryItem.status == "PENDING",
            )
            .with_for_update()
        ):
            _mark_item(item, status="CANCELLED", outcome="CANCELLED")
            job.skipped_count += 1
        job.status = "CANCELLED"
        job.completed_at = utc_now()
    elif action == "retry" and job.status == "COMPLETED_WITH_ATTENTION" and job.action in SAFE_ACTIONS:
        failed = session.scalars(select(AttendanceRecoveryItem).where(
            AttendanceRecoveryItem.job_id == job.id, AttendanceRecoveryItem.status == "FAILED",
            AttendanceRecoveryItem.error_code == "RECOVERY_ITEM_ERROR").with_for_update()).all()
        if not failed:
            raise RecoveryError("There are no failed safe items to retry. Changed or rejected rows need a new preview.", "RECOVERY_NOT_RETRYABLE")
        for item in failed:
            item.status = "PENDING"
            item.completed_at = None
        job.failed_count -= len(failed)
        job.status = "QUEUED"
        job.completed_at = None
    else:
        raise RecoveryError("This action is not available in the current job state.", "CONTROL_STATE_CONFLICT")
    job.lease_owner = None
    job.lease_until = None
    job.updated_at = utc_now()
    append_audit(session, actor=actor, action="ATTENDANCE_RECOVERY_JOB_CONTROLLED",
                 target_type="attendance_recovery_job", target_id=job.job_id, outcome=job.status,
                 after={"action": action, "reason": reason.strip(), "candidate_digest": candidate_digest},
                 request_id=idempotency_key)
    return job
