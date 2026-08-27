"""Durable, fail-closed employee attendance identity repair workflow.

Physical punch facts are inputs to this module, never writable outputs.  The
only local mutation after Oracle content verification is activation of a new
effective identity revision plus compatibility materialization of identity
fields on ``AttendanceEvent``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import hashlib
import hmac
import json
import secrets
from typing import Any, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import decrypt_cnic, decrypt_text, encrypt_text
from zk_add.identity_conflicts import valid_identity_resolutions
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityRevision,
    AttendanceRepairCohort,
    AttendanceRepairEvent,
    AttendanceRepairItem,
    AttendanceRepairJob,
    AttendanceRepairOracleSlot,
    AttendanceRepairTarget,
    AttendanceRepairWorkerHeartbeat,
    Connector,
    DeviceUser,
    IdentityTombstone,
    OracleIdentityRepairReceipt,
    OrdsOutbox,
    ReconciliationCoverage,
    ReconciliationJob,
    TerminalRecordManifest,
    ZKTDevice,
)
from zk_add.ords_states import ORDS_ACTIVE_STATUSES
from zk_add.reconciliation import (
    TERMINAL_JOB_STATES as RECONCILIATION_TERMINAL_STATES,
    active_coverage,
    create_reconciliation_job,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now


PAKISTAN_TZ = ZoneInfo("Asia/Karachi")
JOB_TERMINAL_STATES = frozenset({"COMPLETED", "COMPLETED_WITH_ATTENTION", "CANCELLED"})
ITEM_TERMINAL_STATES = frozenset({"COMPLETE", "NEEDS_REVIEW", "CANCELLED"})
SAFE_ORACLE_CLASSIFICATIONS = frozenset({"MATCH", "MISSING", "MISMATCH"})
UNSAFE_ORACLE_CLASSIFICATIONS = frozenset(
    {"IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION", "AMBIGUOUS", "NOT_FOUND"}
)
ORACLE_UNKNOWN_RESPONSE_CODES = frozenset(
    {
        "ORDS_MALFORMED_RESPONSE",
        "ORDS_MEMBERSHIP_MISMATCH",
        "ORDS_RECEIPT_MISMATCH",
    }
)
FORWARD_COMPLETION_STATES = frozenset({"ORACLE_VERIFY", "ADD_ACTIVATE", "DOWNSTREAM_VERIFY"})
REPAIR_CONTRACT_VERSION = "1"
MAX_CANDIDATE_COHORT_PAIRS = 25_000
_oracle_mutation_slots = asyncio.Semaphore(settings.attendance_repair_oracle_concurrency)
_repair_worker_id = f"attendance-repair-{uuid4()}"


class RepairError(ValueError):
    """An operator-correctable or fail-closed repair error."""

    def __init__(self, message: str, *, code: str = "ATTENDANCE_REPAIR_REJECTED") -> None:
        super().__init__(message)
        self.code = code


class OracleRepairError(RuntimeError):
    """A classified Oracle contract/transport failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


def _canonical(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _protected_digest(value: Any) -> str:
    if not settings.pii_lookup_key:
        raise RepairError(
            "ADD_PII_LOOKUP_KEY is required for protected repair evidence.",
            code="PII_LOOKUP_KEY_MISSING",
        )
    return hmac.new(settings.pii_lookup_key.encode(), _canonical(value), hashlib.sha256).hexdigest()


def _reason_digest(value: str) -> str:
    """Create non-reversible audit evidence for potentially sensitive operator text."""

    return _protected_digest({"purpose": "attendance-repair-reason", "value": value.strip()})


def _identity_digest(display_name: str | None, cnic: str | None) -> str:
    return _protected_digest(
        {
            # Terminal-form display content is exact. Whitespace differences
            # must produce a different approved identity digest.
            "display_name": display_name or "",
            "cnic": cnic or "",
        }
    )


def _immutable_facts(event: AttendanceEvent) -> dict[str, Any]:
    return {
        "event_uid": event.event_uid,
        "device_serial": event.device_serial,
        "source_uid": event.uid,
        "source_user_id": event.user_id,
        "device_event_time": ensure_utc(event.device_event_time).isoformat(),
        "punch": event.punch,
        "status": event.status,
        "raw_punch": "T" if event.raw_punch else "F",
        "source": event.source,
    }


def _immutable_digest(event: AttendanceEvent) -> str:
    return _protected_digest(_immutable_facts(event))


def _source_ownership_digest(event: AttendanceEvent) -> str:
    return _protected_digest(
        {
            "connector_id": event.connector_id,
            "zkt_device_id": event.zkt_device_id,
            "device_user_id": event.device_user_id,
            "device_serial": event.device_serial,
            "source_uid": event.uid,
            "source_user_id": event.user_id,
        }
    )


def _date_scope(
    date_from: date | None, date_to: date | None
) -> tuple[datetime | None, datetime | None]:
    if (date_from is None) != (date_to is None):
        raise RepairError(
            "Provide both Pakistan date bounds, or neither.", code="DATE_SCOPE_INVALID"
        )
    if date_from is None or date_to is None:
        return None, None
    if date_to < date_from:
        raise RepairError(
            "The end date cannot be before the start date.", code="DATE_SCOPE_INVALID"
        )
    if date_to == date.max:
        raise RepairError(
            "The end date is outside the supported attendance range.",
            code="DATE_SCOPE_INVALID",
        )
    start = datetime.combine(date_from, time.min, tzinfo=PAKISTAN_TZ).astimezone(ZoneInfo("UTC"))
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=PAKISTAN_TZ).astimezone(
        ZoneInfo("UTC")
    )
    return start, end


def _mask_source_identifier(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 13:
        return f"*****-****{digits[-4:-1]}-{digits[-1]}"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * min(8, len(value) - 4)}{value[-2:]}"


def _mask_cnic_last4(value: str | None) -> str | None:
    if not value or len(value) != 4:
        return None
    return f"*****-****{value[:-1]}-{value[-1]}"


def _mask_display_name(value: str | None) -> str | None:
    words = (value or "").strip().split()
    if not words:
        return None
    return " ".join(
        "*" if len(word) == 1 else f"{word[0]}{'*' * min(len(word) - 1, 8)}"
        for word in words
    )


def _masked_identity_evidence(events: list[AttendanceEvent]) -> dict[str, Any]:
    variant_keys: set[str] = set()
    visible_variants: dict[str, dict[str, str | None]] = {}
    for event in events:
        key = _protected_digest(
            {
                "display_name": event.display_name or "",
                "cnic_lookup_hash": event.cnic_lookup_hash or "",
            }
        )
        if key in variant_keys:
            continue
        variant_keys.add(key)
        if len(visible_variants) < 20:
            visible_variants[key] = {
                "display_name_masked": _mask_display_name(event.display_name),
                "cnic_masked": _mask_cnic_last4(event.cnic_last4),
            }
    ordered = [visible_variants[key] for key in sorted(visible_variants)]
    return {
        "identity_variants": ordered,
        "identity_variant_count": len(variant_keys),
        "identity_variants_truncated": len(variant_keys) > len(ordered),
    }


def _query_events(
    session: Session,
    *,
    zkt_device_id: int,
    start_utc: datetime | None,
    end_utc: datetime | None,
    limit: int | None = None,
) -> list[AttendanceEvent]:
    statement = select(AttendanceEvent).where(AttendanceEvent.zkt_device_id == zkt_device_id)
    if start_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time >= start_utc)
    if end_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time < end_utc)
    statement = statement.order_by(AttendanceEvent.device_event_time, AttendanceEvent.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement).all())


def _manifested_event_ids(
    session: Session,
    *,
    zkt_device_id: int,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> set[int]:
    statement = (
        select(TerminalRecordManifest.attendance_event_id)
        .join(
            AttendanceEvent,
            AttendanceEvent.id == TerminalRecordManifest.attendance_event_id,
        )
        .where(
            TerminalRecordManifest.zkt_device_id == zkt_device_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
        )
    )
    if start_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time >= start_utc)
    if end_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time < end_utc)
    return set(session.scalars(statement).all())


def _source_certificate(
    session: Session, connector: Connector
) -> tuple[bool, dict[str, Any], ReconciliationCoverage | None]:
    zkt = connector.zkt_device
    if zkt is None:
        return False, {"code": "NO_TERMINAL"}, None
    coverage = active_coverage(session, zkt)
    if coverage is None:
        return False, {"code": "SOURCE_COVERAGE_MISSING"}, None
    source_count, first_ordinal, last_ordinal = session.execute(
        select(
            func.count(TerminalRecordManifest.id),
            func.min(TerminalRecordManifest.ordinal),
            func.max(TerminalRecordManifest.ordinal),
        ).where(
            TerminalRecordManifest.zkt_device_id == zkt.id,
            TerminalRecordManifest.generation == coverage.terminal_generation,
            TerminalRecordManifest.source_epoch_id == coverage.source_epoch_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
            TerminalRecordManifest.ordinal < coverage.source_committed_cursor,
        )
    ).one()
    cursor = int(coverage.source_committed_cursor or 0)
    ledger_complete = int(source_count or 0) == cursor and (
        cursor == 0 or (first_ordinal == 0 and last_ordinal == cursor - 1)
    )
    certified_at = coverage.tail_last_committed_at or coverage.captured_at
    source_age_seconds = max(
        0,
        int((utc_now() - ensure_utc(certified_at)).total_seconds()),
    )
    source_fresh = source_age_seconds <= settings.attendance_repair_source_max_age_seconds
    current = bool(
        coverage.active
        and coverage.capture_state
        in {"SOURCE_CAPTURE_CERTIFIED", "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"}
        and coverage.terminal_serial == zkt.serial
        and cursor == int(zkt.attendance_count or 0)
        and ledger_complete
        and source_fresh
        and len(coverage.source_committed_chain_digest or "") == 64
    )
    certificate = {
        "coverage_id": coverage.coverage_id,
        "active": coverage.active,
        "source_epoch_id": coverage.source_epoch_id,
        "terminal_generation": coverage.terminal_generation,
        "terminal_serial_digest": _protected_digest(coverage.terminal_serial),
        "source_cursor": cursor,
        "source_chain_digest": coverage.source_committed_chain_digest,
        "capture_state": coverage.capture_state,
        "ledger_count": int(source_count or 0),
        "ledger_complete": ledger_complete,
        "certified_at": ensure_utc(certified_at).isoformat(),
        "terminal_source_parity": cursor == int(zkt.attendance_count or 0),
        "tail_exception_count": coverage.tail_exception_count,
    }
    certificate["certificate_digest"] = _sha(certificate)
    # Age is operational readiness, not certificate identity. Including it in
    # the digest would make every otherwise immutable preview drift each second.
    certificate["source_age_seconds"] = source_age_seconds
    certificate["source_fresh"] = source_fresh
    return current, certificate, coverage


def _terminal_eligibility(
    session: Session, connector: Connector
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    hard: list[dict[str, str]] = []
    waitable: list[dict[str, str]] = []
    zkt = connector.zkt_device
    if not settings.attendance_repair_preview_enabled:
        hard.append(
            {
                "code": "PREVIEW_DISABLED",
                "message": "Employee attendance repair preview is disabled.",
            }
        )
    if not connector.active:
        hard.append({"code": "CONNECTOR_INACTIVE", "message": "Connector is inactive."})
    if connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL":
        hard.append(
            {
                "code": "DUPLICATE_SERIAL_QUARANTINE",
                "message": "Resolve duplicate terminal serial quarantine first.",
            }
        )
    if zkt is None:
        hard.append({"code": "NO_TERMINAL", "message": "No ZKT terminal is assigned."})
    else:
        if not zkt.serial:
            hard.append({"code": "SERIAL_UNKNOWN", "message": "Terminal serial is unknown."})
        if (
            not zkt.snapshot_complete
            or not zkt.identity_snapshot_stable
            or not zkt.identity_snapshot_id
        ):
            waitable.append(
                {
                    "code": "IDENTITY_SNAPSHOT_UNSTABLE",
                    "message": "A complete stable terminal identity snapshot is required.",
                }
            )
    backlog = int(
        session.scalar(
            select(func.count(OrdsOutbox.id)).where(OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES))
        )
        or 0
    )
    if backlog >= settings.reconciliation_history_backlog_pause:
        waitable.append(
            {
                "code": "LIVE_ORDS_BACKLOG_HIGH",
                "message": "Repair intake is paused while live Oracle delivery catches up.",
            }
        )
    return hard, waitable


def repair_preflight(session: Session, connector: Connector) -> dict[str, Any]:
    hard, waitable = _terminal_eligibility(session, connector)
    source_current, certificate, _coverage = _source_certificate(session, connector)
    if not source_current:
        if not settings.reconciliation_enabled:
            hard.append(
                {
                    "code": "SOURCE_RECONCILIATION_DISABLED",
                    "message": "Full-device source certification is disabled.",
                }
            )
        waitable.append(
            {
                "code": certificate.get("code", "SOURCE_RECERTIFICATION_REQUIRED"),
                "message": (
                    "A bounded full-device source reconciliation will run before the final preview. "
                    "The terminal cannot scan only one employee and may recover other missing punches."
                ),
            }
        )
    return {
        "preview_enabled": settings.attendance_repair_preview_enabled,
        "execution_enabled": settings.attendance_repair_execution_enabled,
        "eligible": not hard,
        "ready_now": not hard and not waitable,
        "requires_source_reconciliation": not source_current,
        "hard_blockers": hard,
        "waitable_blockers": waitable,
        "limits": {
            "employees": settings.attendance_repair_max_employees,
            "events": settings.attendance_repair_max_events,
            "oracle_batch": settings.attendance_repair_oracle_batch_size,
        },
        "source_certificate": certificate,
        "terminal": None
        if connector.zkt_device is None
        else {
            "serial": connector.zkt_device.serial,
            "snapshot_complete": connector.zkt_device.snapshot_complete,
            "snapshot_stable": connector.zkt_device.identity_snapshot_stable,
            "snapshot_revision": connector.zkt_device.identity_snapshot_revision,
            "attendance_count": connector.zkt_device.attendance_count,
        },
    }


def _eligible_target(
    session: Session, *, zkt: ZKTDevice, user: DeviceUser
) -> tuple[bool, str | None]:
    if user.zkt_device_id != zkt.id:
        return False, "TARGET_WRONG_TERMINAL"
    if not user.present or user.lifecycle_state != "ACTIVE":
        return False, "TARGET_NOT_ACTIVE"
    if not user.display_name.strip():
        return False, "TARGET_NAME_MISSING"
    if len(user.display_name) > 200:
        return False, "TARGET_NAME_TOO_LONG"
    if any(character in user.display_name for character in ("\0", "\x1f")):
        return False, "TARGET_NAME_INVALID"
    if not user.cnic_encrypted or not user.cnic_lookup_hash or not user.cnic_last4:
        return False, "TARGET_CNIC_MISSING"
    try:
        cnic = decrypt_cnic(user.cnic_encrypted)
    except Exception:
        return False, "TARGET_CNIC_UNREADABLE"
    if cnic is None or len(cnic) != 13 or not cnic.isdigit():
        return False, "TARGET_CNIC_INVALID"
    if (
        not zkt.snapshot_complete
        or not zkt.identity_snapshot_stable
        or not zkt.identity_snapshot_id
    ):
        return False, "TARGET_SNAPSHOT_UNSTABLE"
    if user.snapshot_revision != zkt.identity_snapshot_revision:
        return False, "TARGET_NOT_IN_CURRENT_SNAPSHOT"
    if user.identity_conflict_code:
        resolutions = valid_identity_resolutions(session, zkt=zkt)
        if user.cnic_lookup_hash not in resolutions:
            return False, "TARGET_DUPLICATE_CNIC_UNRESOLVED"
    return True, None


def _group_events(
    events: Iterable[AttendanceEvent],
) -> dict[tuple[int | None, str, str], list[AttendanceEvent]]:
    grouped: dict[tuple[int | None, str, str], list[AttendanceEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.device_user_id, event.uid or "", event.user_id)].append(event)
    return grouped


def _membership_digest(events: list[AttendanceEvent]) -> str:
    return _protected_digest(
        [
            {
                "event_uid": event.event_uid,
                "immutable": _immutable_digest(event),
            }
            for event in sorted(events, key=lambda row: row.event_uid)
        ]
    )


def _cohort_token(
    *,
    target: DeviceUser,
    key: tuple[int | None, str, str],
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> str:
    return _protected_digest(
        {
            # The token identifies the exact source alias, not its mutable
            # membership. A source-reconciliation dependency may discover new
            # events; the final preview freezes and separately digests that
            # refreshed membership before approval.
            "purpose": "attendance-repair-cohort-v2",
            "target_user_key": target.user_key,
            "source_device_user_id": key[0],
            "source_uid": key[1],
            "source_user_id": key[2],
            "date_start_utc": start_utc.isoformat() if start_utc else None,
            "date_end_utc": end_utc.isoformat() if end_utc else None,
        }
    )


def build_repair_candidates(
    session: Session,
    *,
    connector: Connector,
    user_keys: list[str],
    date_from: date | None,
    date_to: date | None,
) -> dict[str, Any]:
    if not settings.attendance_repair_preview_enabled:
        raise RepairError(
            "Employee attendance repair preview is disabled.", code="PREVIEW_DISABLED"
        )
    if not user_keys or len(user_keys) > settings.attendance_repair_max_employees:
        raise RepairError("Select between 1 and 500 employees.", code="TARGET_LIMIT")
    if len(set(user_keys)) != len(user_keys):
        raise RepairError("Each employee may be selected once.", code="DUPLICATE_TARGET")
    zkt = connector.zkt_device
    if zkt is None:
        raise RepairError("No ZKT terminal is assigned.", code="NO_TERMINAL")
    start_utc, end_utc = _date_scope(date_from, date_to)
    users = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.user_key.in_(user_keys),
            )
        ).all()
    )
    by_key = {user.user_key: user for user in users}
    if len(by_key) != len(user_keys):
        raise RepairError(
            "One or more selected employees no longer exist.", code="TARGET_NOT_FOUND"
        )
    events = _query_events(
        session,
        zkt_device_id=zkt.id,
        start_utc=start_utc,
        end_utc=end_utc,
        limit=settings.attendance_repair_max_events + 1,
    )
    if len(events) > settings.attendance_repair_max_events:
        raise RepairError(
            "The selected date scope exceeds 250,000 events; split it into smaller date ranges.",
            code="EVENT_LIMIT",
        )
    grouped = _group_events(events)
    tombstones = {
        (row.device_user_id, row.uid or "", row.user_id): row
        for row in session.scalars(
            select(IdentityTombstone).where(IdentityTombstone.zkt_device_id == zkt.id)
        ).all()
    }
    active_users = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all()
    )
    active_uid_owners: dict[str, set[int]] = defaultdict(set)
    active_user_id_owners: dict[str, set[int]] = defaultdict(set)
    for row in active_users:
        active_uid_owners[row.uid or ""].add(row.id)
        active_user_id_owners[row.user_id].add(row.id)
    manifested_event_ids = _manifested_event_ids(
        session,
        zkt_device_id=zkt.id,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    group_metadata: dict[tuple[int | None, str, str], dict[str, Any]] = {}
    for group_key, rows in grouped.items():
        manifest_count = sum(1 for row in rows if row.id in manifested_event_ids)
        group_metadata[group_key] = {
            "membership_digest": _membership_digest(rows),
            "manifest_count": manifest_count,
            "manifest_complete": manifest_count == len(rows),
            "source_types": sorted({row.source for row in rows}),
            **_masked_identity_evidence(rows),
        }
    result_targets: list[dict[str, Any]] = []
    candidate_cohort_pairs = 0
    for user_key in user_keys:
        target = by_key[user_key]
        eligible, exclusion = _eligible_target(session, zkt=zkt, user=target)
        cohorts: list[dict[str, Any]] = []
        if eligible:
            for group_key, rows in grouped.items():
                is_current = group_key[0] == target.id
                tombstone = tombstones.get(group_key)
                metadata = group_metadata[group_key]
                if not is_current and tombstone is None and not metadata["manifest_complete"]:
                    continue
                membership = metadata["membership_digest"]
                token = _cohort_token(
                    target=target,
                    key=group_key,
                    start_utc=start_utc,
                    end_utc=end_utc,
                )
                owners = set(active_user_id_owners.get(group_key[2], set()))
                if group_key[1]:
                    owners |= active_uid_owners.get(group_key[1], set())
                ambiguous = bool(not is_current and owners and owners != {target.id})
                evidence = (
                    "CURRENT_USER_LINEAGE"
                    if is_current
                    else "EXACT_TOMBSTONE"
                    if tombstone is not None
                    else "SOURCE_MANIFEST_COHORT"
                )
                cohorts.append(
                    {
                        "cohort_token": token,
                        "evidence_classification": evidence,
                        "selectable": not ambiguous,
                        "exclusion_code": "UID_OR_USER_ID_REUSED" if ambiguous else None,
                        "source_device_user_key": (
                            target.user_key if is_current else _protected_digest(group_key[0])[:16]
                        ),
                        "source_uid": _mask_source_identifier(group_key[1]),
                        "source_user_id": _mask_source_identifier(group_key[2]),
                        "first_event_at": rows[0].device_event_time,
                        "last_event_at": rows[-1].device_event_time,
                        "event_count": len(rows),
                        "membership_digest": membership,
                        "masked_identity": {
                            "variants": metadata["identity_variants"],
                            "variant_count": metadata["identity_variant_count"],
                            "truncated": metadata["identity_variants_truncated"],
                        },
                        "source_evidence": {
                            "terminal_manifest_events": metadata["manifest_count"],
                            "exact_tombstone": tombstone is not None,
                            "source_types": metadata["source_types"],
                        },
                    }
                )
                candidate_cohort_pairs += 1
                if candidate_cohort_pairs > MAX_CANDIDATE_COHORT_PAIRS:
                    raise RepairError(
                        "The candidate evidence set is too broad; select fewer employees "
                        "or split the request into smaller Pakistan date ranges.",
                        code="CANDIDATE_SCOPE_TOO_BROAD",
                    )
        result_targets.append(
            {
                "user_key": target.user_key,
                "row_version": target.row_version,
                "display_name": target.display_name,
                "cnic_masked": _mask_cnic_last4(target.cnic_last4),
                "eligible": eligible,
                "exclusion_code": exclusion,
                "cohorts": sorted(
                    cohorts,
                    key=lambda item: (
                        item["evidence_classification"] != "CURRENT_USER_LINEAGE",
                        item["first_event_at"],
                    ),
                ),
            }
        )
    source_current, certificate, _coverage = _source_certificate(session, connector)
    return {
        "connector_id": connector.connector_id,
        "device_id": connector.device_id,
        "source_current": source_current,
        "source_certificate": certificate,
        "date_scope": {
            "timezone": "Asia/Karachi",
            "start_utc": start_utc,
            "end_utc_exclusive": end_utc,
        },
        "targets": result_targets,
    }


def _lock_job(session: Session, job_id: int) -> AttendanceRepairJob:
    job = session.scalar(
        select(AttendanceRepairJob).where(AttendanceRepairJob.id == job_id).with_for_update()
    )
    if job is None:
        raise RepairError("Attendance repair job no longer exists.", code="JOB_NOT_FOUND")
    return job


def _upsert_repair_alert(
    session: Session,
    job: AttendanceRepairJob,
    *,
    error_code: str,
) -> None:
    from zk_add.service import upsert_alert

    connector = session.get(Connector, job.connector_id)
    if connector is None:
        return
    upsert_alert(
        session,
        connector,
        code="ATTENDANCE_REPAIR_NEEDS_ATTENTION",
        severity="HIGH",
        message="Employee attendance repair requires operator attention.",
        details={
            "job_id": job.job_id,
            "phase": job.phase,
            "error_code": error_code,
        },
    )


def _resolve_repair_alert(session: Session, job: AttendanceRepairJob) -> None:
    from zk_add.service import resolve_alert

    connector = session.get(Connector, job.connector_id)
    if connector is not None:
        resolve_alert(session, connector, code="ATTENDANCE_REPAIR_NEEDS_ATTENTION")


def _repair_event(
    session: Session,
    job: AttendanceRepairJob,
    state: str,
    *,
    details: dict[str, Any] | None = None,
    item_id: int | None = None,
    idempotency_key: str | None = None,
) -> AttendanceRepairEvent:
    key = idempotency_key or str(uuid4())
    existing = None
    pending = [
        row
        for row in session.new
        if isinstance(row, AttendanceRepairEvent) and row.job_id == job.id
    ]
    if idempotency_key:
        pending_match = next(
            (row for row in pending if row.idempotency_key == idempotency_key), None
        )
        if pending_match is not None:
            return pending_match
        existing = session.scalar(
            select(AttendanceRepairEvent).where(
                AttendanceRepairEvent.job_id == job.id,
                AttendanceRepairEvent.idempotency_key == idempotency_key,
            )
        )
    if existing is not None:
        return existing
    previous = session.scalar(
        select(AttendanceRepairEvent)
        .where(AttendanceRepairEvent.job_id == job.id)
        .order_by(AttendanceRepairEvent.sequence.desc())
        .limit(1)
    )
    if pending:
        pending_previous = max(pending, key=lambda row: row.sequence)
        if previous is None or pending_previous.sequence > previous.sequence:
            previous = pending_previous
    sequence = (previous.sequence if previous else 0) + 1
    created_at = utc_now()
    safe_details = details or {}
    material = {
        "job_id": job.job_id,
        "sequence": sequence,
        "state": state,
        "item_id": item_id,
        "idempotency_key": key,
        "details": safe_details,
        "previous_hash": previous.row_hash if previous else None,
        "created_at": created_at.isoformat(),
    }
    row = AttendanceRepairEvent(
        job_id=job.id,
        sequence=sequence,
        state=state,
        item_id=item_id,
        idempotency_key=key,
        details=safe_details,
        previous_hash=previous.row_hash if previous else None,
        row_hash=_sha(material),
        created_at=created_at,
    )
    session.add(row)
    return row


def _target_from_selection(
    session: Session,
    *,
    job: AttendanceRepairJob,
    zkt: ZKTDevice,
    user_key: str,
    expected_row_version: int,
    all_provable_history: bool,
    alias_tokens: list[str],
) -> AttendanceRepairTarget:
    user = session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == user_key,
        )
    )
    if user is None:
        raise RepairError("A selected employee no longer exists.", code="TARGET_NOT_FOUND")
    eligible, code = _eligible_target(session, zkt=zkt, user=user)
    if not eligible:
        raise RepairError(
            f"Employee {user.user_key} is not eligible ({code}).",
            code=code or "TARGET_INELIGIBLE",
        )
    if user.row_version != expected_row_version:
        raise RepairError(
            "A selected employee changed; refresh candidates before preparing.",
            code="TARGET_VERSION_CHANGED",
        )
    cnic = decrypt_cnic(user.cnic_encrypted)
    assert cnic is not None and user.cnic_encrypted is not None
    target = AttendanceRepairTarget(
        job_id=job.id,
        device_user_id=user.id,
        user_key=user.user_key,
        expected_row_version=user.row_version,
        all_provable_history=all_provable_history,
        selected_alias_tokens=sorted(set(alias_tokens)),
        identity_snapshot_id=zkt.identity_snapshot_id,
        terminal_identity_fingerprint=user.terminal_identity_fingerprint,
        desired_display_name_encrypted=encrypt_text(user.display_name),
        desired_cnic_encrypted=user.cnic_encrypted,
        desired_cnic_lookup_hash=user.cnic_lookup_hash,
        desired_cnic_last4=user.cnic_last4,
        desired_identity_digest=_identity_digest(user.display_name, cnic),
        status="SOURCE_PENDING",
    )
    session.add(target)
    session.flush()
    return target


def _attach_source_dependency(
    session: Session,
    *,
    job: AttendanceRepairJob,
    connector: Connector,
) -> ReconciliationJob:
    active = session.scalar(
        select(ReconciliationJob).where(
            ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status.not_in(RECONCILIATION_TERMINAL_STATES),
        )
    )
    if active is None:
        try:
            active = create_reconciliation_job(
                session,
                connector=connector,
                actor=job.actor,
                reason=(
                    "Source coverage dependency for requested employee attendance repair preparation."
                ),
                confirmation=f"RECONCILE {connector.device_id} FROM START",
                idempotency_key=f"repair-source-{job.job_id}",
            )
        except ValueError as exc:
            raise RepairError(
                "The required full-device source reconciliation could not be queued.",
                code="SOURCE_DEPENDENCY_UNAVAILABLE",
            ) from exc
    job.source_reconciliation_job_id = active.id
    job.status = "PREPARING_SOURCE"
    job.phase = "WAITING_SOURCE_RECONCILIATION"
    job.wait_reason = "SOURCE_RECERTIFICATION_REQUIRED"
    _repair_event(
        session,
        job,
        "PREPARING_SOURCE",
        details={"source_dependency_job_id": active.job_id},
    )
    return active


def create_repair_job(
    session: Session,
    *,
    connector: Connector,
    actor: str,
    selections: list[dict[str, Any]],
    date_from: date | None,
    date_to: date | None,
    idempotency_key: str,
) -> AttendanceRepairJob:
    if not settings.attendance_repair_preview_enabled:
        raise RepairError(
            "Employee attendance repair preview is disabled.", code="PREVIEW_DISABLED"
        )
    if not selections or len(selections) > settings.attendance_repair_max_employees:
        raise RepairError("Select between 1 and 500 employees.", code="TARGET_LIMIT")
    user_keys = [str(row["user_key"]) for row in selections]
    if len(set(user_keys)) != len(user_keys):
        raise RepairError("Each employee may be selected once.", code="DUPLICATE_TARGET")
    alias_count = sum(len(set(row.get("cohort_tokens") or [])) for row in selections)
    if alias_count > MAX_CANDIDATE_COHORT_PAIRS:
        raise RepairError(
            "The historical alias selection is too broad; split the request into smaller "
            "employee or Pakistan-date batches.",
            code="CANDIDATE_SCOPE_TOO_BROAD",
        )
    start_utc, end_utc = _date_scope(date_from, date_to)
    locked_connector = session.scalar(
        select(Connector).where(Connector.id == connector.id).with_for_update()
    )
    if locked_connector is None or locked_connector.zkt_device is None:
        raise RepairError("The selected terminal no longer exists.", code="NO_TERMINAL")
    connector = locked_connector
    zkt = connector.zkt_device
    hard, waitable = _terminal_eligibility(session, connector)
    if hard:
        raise RepairError(hard[0]["message"], code=hard[0]["code"])
    if waitable:
        # Source coverage has its own durable dependency below. Terminal
        # snapshot instability and live-delivery backpressure must stop new
        # repair intake rather than creating a draft that looks actionable.
        raise RepairError(waitable[0]["message"], code=waitable[0]["code"])
    request_material = {
        "connector_id": connector.connector_id,
        "targets": [
            {
                "user_key": row["user_key"],
                "expected_row_version": int(row["expected_row_version"]),
                "all_provable_history": bool(row.get("all_provable_history", True)),
                "cohort_tokens": sorted(set(row.get("cohort_tokens") or [])),
            }
            for row in selections
        ],
        "date_start_utc": start_utc.isoformat() if start_utc else None,
        "date_end_utc": end_utc.isoformat() if end_utc else None,
    }
    request_digest = _sha(request_material)
    replay = session.scalar(
        select(AttendanceRepairJob).where(
            AttendanceRepairJob.connector_id == connector.id,
            AttendanceRepairJob.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        if not secrets.compare_digest(replay.request_digest, request_digest):
            raise RepairError(
                "That idempotency key was used for a different repair request.",
                code="IDEMPOTENCY_CONFLICT",
            )
        return replay
    active = session.scalar(
        select(AttendanceRepairJob).where(
            AttendanceRepairJob.connector_id == connector.id,
            AttendanceRepairJob.status.not_in(JOB_TERMINAL_STATES),
        )
    )
    if active is not None:
        raise RepairError(
            f"This terminal already has active employee repair {active.job_id}.",
            code="ACTIVE_REPAIR_EXISTS",
        )
    job = AttendanceRepairJob(
        connector_id=connector.id,
        zkt_device_id=zkt.id,
        actor=actor,
        status="PREPARING_SOURCE",
        phase="SOURCE_PREFLIGHT",
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        date_start_utc=start_utc,
        date_end_utc=end_utc,
        target_count=len(selections),
    )
    session.add(job)
    session.flush()
    for selection in selections:
        _target_from_selection(
            session,
            job=job,
            zkt=zkt,
            user_key=str(selection["user_key"]),
            expected_row_version=int(selection["expected_row_version"]),
            all_provable_history=bool(selection.get("all_provable_history", True)),
            alias_tokens=list(selection.get("cohort_tokens") or []),
        )
    source_current, certificate, _coverage = _source_certificate(session, connector)
    if source_current:
        job.source_certificate_digest = certificate["certificate_digest"]
        job.status = "PREPARING_SOURCE"
        job.phase = "MEMBERSHIP_FREEZE"
        job.wait_reason = None
        _repair_event(
            session,
            job,
            "SOURCE_CERTIFIED",
            details={"source_certificate_digest": job.source_certificate_digest},
        )
    else:
        _attach_source_dependency(session, job=job, connector=connector)
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_REPAIR_PREPARED",
        target_type="attendance_repair_job",
        target_id=job.job_id,
        outcome=job.status,
        after={
            "connector_id": connector.connector_id,
            "target_count": job.target_count,
            "date_scoped": start_utc is not None,
            "source_dependency": bool(job.source_reconciliation_job_id),
        },
    )
    return job


def _candidate_map_for_target(
    session: Session,
    *,
    connector: Connector,
    target_user: DeviceUser,
    start_utc: datetime | None,
    end_utc: datetime | None,
    events: list[AttendanceEvent] | None = None,
    tombstones: set[tuple[int | None, str, str]] | None = None,
    manifested_event_ids: set[int] | None = None,
    active_uid_owners: dict[str, set[int]] | None = None,
    active_user_id_owners: dict[str, set[int]] | None = None,
) -> dict[str, tuple[tuple[int | None, str, str], list[AttendanceEvent], str]]:
    if events is None:
        events = _query_events(
            session,
            zkt_device_id=connector.zkt_device.id,
            start_utc=start_utc,
            end_utc=end_utc,
            limit=settings.attendance_repair_max_events + 1,
        )
        if len(events) > settings.attendance_repair_max_events:
            raise RepairError(
                "The selected date scope exceeds 250,000 events; split it into smaller date ranges.",
                code="EVENT_LIMIT",
            )
    grouped = _group_events(events)
    if tombstones is None:
        tombstones = {
            (row.device_user_id, row.uid or "", row.user_id)
            for row in session.scalars(
                select(IdentityTombstone).where(
                    IdentityTombstone.zkt_device_id == connector.zkt_device.id
                )
            ).all()
        }
    if manifested_event_ids is None:
        manifested_event_ids = _manifested_event_ids(
            session,
            zkt_device_id=connector.zkt_device.id,
            start_utc=start_utc,
            end_utc=end_utc,
        )
    if active_uid_owners is None or active_user_id_owners is None:
        active_uid_owners = defaultdict(set)
        active_user_id_owners = defaultdict(set)
        for row in session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == connector.zkt_device.id,
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all():
            active_uid_owners[row.uid or ""].add(row.id)
            active_user_id_owners[row.user_id].add(row.id)
    result = {}
    for key, rows in grouped.items():
        classification = None
        if key[0] == target_user.id:
            classification = "CURRENT_USER_LINEAGE"
        elif key in tombstones or all(row.id in manifested_event_ids for row in rows):
            owners = set(active_user_id_owners.get(key[2], set()))
            if key[1]:
                owners |= active_uid_owners.get(key[1], set())
            if owners and owners != {target_user.id}:
                continue
            classification = "EXACT_TOMBSTONE" if key in tombstones else "SOURCE_MANIFEST_COHORT"
        if classification is None:
            continue
        token = _cohort_token(
            target=target_user,
            key=key,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        result[token] = (key, rows, classification)
    return result


def _freeze_membership(session: Session, job: AttendanceRepairJob) -> None:
    connector = session.get(Connector, job.connector_id)
    zkt = session.get(ZKTDevice, job.zkt_device_id)
    if connector is None or zkt is None:
        raise RepairError("The terminal disappeared during preparation.", code="TERMINAL_DRIFT")
    selected_event_ids: set[int] = set()
    cohort_digests: list[str] = []
    total = 0
    targets = list(
        session.scalars(
            select(AttendanceRepairTarget).where(AttendanceRepairTarget.job_id == job.id)
        ).all()
    )
    scoped_events = _query_events(
        session,
        zkt_device_id=zkt.id,
        start_utc=job.date_start_utc,
        end_utc=job.date_end_utc,
        limit=settings.attendance_repair_max_events + 1,
    )
    if len(scoped_events) > settings.attendance_repair_max_events:
        raise RepairError(
            "The selected date scope exceeds 250,000 events; split it into smaller date ranges.",
            code="EVENT_LIMIT",
        )
    tombstones = {
        (row.device_user_id, row.uid or "", row.user_id)
        for row in session.scalars(
            select(IdentityTombstone).where(IdentityTombstone.zkt_device_id == zkt.id)
        ).all()
    }
    manifested_event_ids = _manifested_event_ids(
        session,
        zkt_device_id=zkt.id,
        start_utc=job.date_start_utc,
        end_utc=job.date_end_utc,
    )
    active_uid_owners: dict[str, set[int]] = defaultdict(set)
    active_user_id_owners: dict[str, set[int]] = defaultdict(set)
    for row in session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.present == True,  # noqa: E712
            DeviceUser.lifecycle_state == "ACTIVE",
        )
    ).all():
        active_uid_owners[row.uid or ""].add(row.id)
        active_user_id_owners[row.user_id].add(row.id)
    for target in targets:
        user = session.get(DeviceUser, target.device_user_id)
        if user is None:
            raise RepairError("A target disappeared during preparation.", code="TARGET_DRIFT")
        if user.row_version != target.expected_row_version:
            raise RepairError("A target changed during source preparation.", code="TARGET_DRIFT")
        eligible, eligibility_code = _eligible_target(session, zkt=zkt, user=user)
        if not eligible or zkt.identity_snapshot_id != target.identity_snapshot_id:
            raise RepairError(
                "A target is no longer part of the certified current identity snapshot.",
                code=eligibility_code or "TARGET_DRIFT",
            )
        try:
            current_target_cnic = decrypt_cnic(user.cnic_encrypted)
        except Exception as exc:
            raise RepairError(
                "A target's protected identity became unreadable during preparation.",
                code="TARGET_PII_UNREADABLE",
            ) from exc
        if _identity_digest(user.display_name, current_target_cnic) != target.desired_identity_digest:
            raise RepairError(
                "A target identity changed during source preparation.",
                code="TARGET_DRIFT",
            )
        candidates = _candidate_map_for_target(
            session,
            connector=connector,
            target_user=user,
            start_utc=job.date_start_utc,
            end_utc=job.date_end_utc,
            events=scoped_events,
            tombstones=tombstones,
            manifested_event_ids=manifested_event_ids,
            active_uid_owners=active_uid_owners,
            active_user_id_owners=active_user_id_owners,
        )
        chosen: list[
            tuple[str, tuple[tuple[int | None, str, str], list[AttendanceEvent], str]]
        ] = []
        if target.all_provable_history:
            chosen.extend(
                (token, value)
                for token, value in candidates.items()
                if value[2] == "CURRENT_USER_LINEAGE"
            )
        for token in target.selected_alias_tokens or []:
            value = candidates.get(token)
            if value is None or value[2] == "CURRENT_USER_LINEAGE":
                raise RepairError(
                    "A selected historical alias cohort changed; regenerate candidates.",
                    code="COHORT_DRIFT",
                )
            chosen.append((token, value))
        if not chosen:
            raise RepairError(
                "No provable attendance events are selected for an employee.",
                code="EMPTY_TARGET",
            )
        for token, (key, rows, classification) in chosen:
            membership = _membership_digest(rows)
            cohort = AttendanceRepairCohort(
                target_id=target.id,
                cohort_token=token,
                evidence_classification=classification,
                source_device_user_id=key[0],
                source_uid_digest=_protected_digest(key[1]),
                source_user_id_digest=_protected_digest(key[2]),
                membership_digest=membership,
                first_event_at=rows[0].device_event_time,
                last_event_at=rows[-1].device_event_time,
                event_count=len(rows),
                selected=True,
            )
            session.add(cohort)
            session.flush()
            cohort_digests.append(membership)
            for event in rows:
                if event.id in selected_event_ids:
                    raise RepairError(
                        "One physical event was selected for multiple employees.",
                        code="CROSS_TARGET_EVENT_COLLISION",
                    )
                selected_event_ids.add(event.id)
                total += 1
                if total > settings.attendance_repair_max_events:
                    raise RepairError(
                        "This repair exceeds 250,000 events; split it into date ranges.",
                        code="EVENT_LIMIT",
                    )
                try:
                    before_cnic = decrypt_cnic(event.cnic_encrypted)
                except Exception as exc:
                    raise RepairError(
                        "A selected event contains unreadable protected identity data.",
                        code="EVENT_PII_UNREADABLE",
                    ) from exc
                before_name = event.display_name or ""
                item = AttendanceRepairItem(
                    job_id=job.id,
                    target_id=target.id,
                    cohort_id=cohort.id,
                    attendance_event_id=event.id,
                    event_uid=event.event_uid,
                    immutable_facts_digest=_immutable_digest(event),
                    source_ownership_digest=_source_ownership_digest(event),
                    before_device_user_id=event.device_user_id,
                    before_display_name_encrypted=encrypt_text(event.display_name),
                    before_cnic_encrypted=event.cnic_encrypted,
                    before_cnic_lookup_hash=event.cnic_lookup_hash,
                    before_cnic_last4=event.cnic_last4,
                    before_identity_digest=_identity_digest(before_name, before_cnic),
                    desired_display_name_encrypted=target.desired_display_name_encrypted,
                    desired_cnic_encrypted=target.desired_cnic_encrypted,
                    desired_cnic_lookup_hash=target.desired_cnic_lookup_hash,
                    desired_cnic_last4=target.desired_cnic_last4,
                    desired_identity_digest=target.desired_identity_digest,
                    state="FROZEN",
                )
                session.add(item)
            target.event_count += len(rows)
            target.status = "FROZEN"
    if total == 0:
        raise RepairError("The selection contains no provable events.", code="EMPTY_REPAIR")
    job.event_count = total
    job.cohort_digest = _sha(sorted(cohort_digests))
    job.status = "PREPARING_SOURCE"
    job.phase = "ORACLE_CLASSIFICATION"
    job.wait_reason = None
    job.updated_at = utc_now()
    _repair_event(
        session,
        job,
        "MEMBERSHIP_FROZEN",
        details={"target_count": len(targets), "event_count": total},
    )


def _ords_url(path: str) -> str:
    if not settings.ords_base_url:
        raise OracleRepairError(
            "Oracle repair endpoint is not configured.",
            code="ORDS_NOT_CONFIGURED",
            retryable=False,
        )
    return f"{settings.ords_base_url.rstrip('/')}/{path.lstrip('/')}"


async def _ords_request(path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if (
        not settings.attendance_repair_ords_username
        or not settings.attendance_repair_ords_password
    ):
        raise OracleRepairError(
            "The ADD-only Oracle repair credential is not configured.",
            code="ORDS_AUTHENTICATION_NOT_CONFIGURED",
            retryable=False,
        )
    method = "GET" if payload is None else "POST"
    try:
        async with httpx.AsyncClient(
            timeout=settings.ords_timeout_seconds,
            headers={
                "X-API-Username": settings.attendance_repair_ords_username,
                "X-API-Password": settings.attendance_repair_ords_password,
            },
        ) as client:
            response = await client.request(method, _ords_url(path), json=payload)
    except httpx.RequestError as exc:
        raise OracleRepairError(
            "Oracle repair transport is temporarily unavailable.",
            code="ORDS_TRANSPORT_ERROR",
            retryable=True,
        ) from exc
    if response.status_code in {401, 403}:
        raise OracleRepairError(
            "Oracle rejected the ADD-only repair credential.",
            code="ORDS_AUTHENTICATION_FAILED",
            retryable=False,
            status_code=response.status_code,
        )
    if response.status_code in {408, 429} or response.status_code >= 500:
        raise OracleRepairError(
            "Oracle repair service is temporarily unavailable.",
            code=f"ORDS_HTTP_{response.status_code}",
            retryable=True,
            status_code=response.status_code,
        )
    if response.status_code == 409:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = None
        server_code = (
            str(error_payload.get("error_code") or "").upper()
            if isinstance(error_payload, dict)
            else ""
        )
        if server_code == "DOWNSTREAM_ADAPTER_NOT_READY":
            raise OracleRepairError(
                "Oracle downstream repair capability is not ready.",
                code="ORDS_CAPABILITY_MISSING",
                retryable=False,
                status_code=response.status_code,
            )
        if server_code == "OPERATION_ID_CONTENT_CONFLICT":
            raise OracleRepairError(
                "Oracle operation ID was replayed with conflicting content.",
                code="ORDS_OPERATION_CONFLICT",
                retryable=False,
                status_code=response.status_code,
            )
    if response.status_code >= 400:
        raise OracleRepairError(
            "Oracle rejected the repair contract request.",
            code=f"ORDS_HTTP_{response.status_code}",
            retryable=False,
            status_code=response.status_code,
        )
    try:
        decoded = response.json()
    except ValueError as exc:
        raise OracleRepairError(
            "Oracle returned malformed repair evidence.",
            code="ORDS_MALFORMED_RESPONSE",
            retryable=False,
            status_code=response.status_code,
        ) from exc
    if not isinstance(decoded, dict):
        raise OracleRepairError(
            "Oracle returned malformed repair evidence.",
            code="ORDS_MALFORMED_RESPONSE",
            retryable=False,
            status_code=response.status_code,
        )
    return decoded


async def oracle_repair_capabilities() -> dict[str, Any]:
    payload = await _ords_request("raw-captures/identity-repairs/capabilities")
    required = {
        "contract_version": REPAIR_CONTRACT_VERSION,
        "add_only_auth": True,
        "content_preconditions": True,
        "operation_replay": True,
        "raw_content_verification": True,
        "downstream_verification": True,
        "old_identity_absence_verification": True,
        "execution_ready": True,
        "batch_limit": 100,
    }
    missing = [key for key, value in required.items() if payload.get(key) != value]
    if missing:
        raise OracleRepairError(
            "Oracle identity repair capability is incomplete: " + ", ".join(missing),
            code="ORDS_CAPABILITY_MISSING",
            retryable=False,
        )
    return payload


def _operation_results(
    response: dict[str, Any],
    *,
    expected_operation_ids: set[str],
    context: str,
) -> dict[str, dict[str, Any]]:
    results = response.get("results")
    if not isinstance(results, list):
        raise OracleRepairError(
            f"{context} omitted results.",
            code="ORDS_MALFORMED_RESPONSE",
            retryable=False,
        )
    indexed: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("operation_id"), str):
            raise OracleRepairError(
                f"{context} returned an invalid operation result.",
                code="ORDS_MALFORMED_RESPONSE",
                retryable=False,
            )
        operation_id = str(result["operation_id"])
        if operation_id in indexed:
            raise OracleRepairError(
                f"{context} returned a duplicate operation ID.",
                code="ORDS_MALFORMED_RESPONSE",
                retryable=False,
            )
        indexed[operation_id] = result
    if set(indexed) != expected_operation_ids:
        raise OracleRepairError(
            f"{context} did not settle the exact operation batch.",
            code="ORDS_MEMBERSHIP_MISMATCH",
            retryable=False,
        )
    return indexed


def _oracle_operation_digest(payload: dict[str, Any]) -> str:
    """Digest every accepted Oracle item field using the v1 wire contract.

    Oracle implements the same unit-separator canonicalization.  NUL and unit
    separator are forbidden so no two field sequences can share a material
    string.  Production preflight requires AL32UTF8 for matching Unicode bytes.
    """

    immutable = payload["immutable_facts"]
    desired = payload["desired_identity"]
    insert = payload["insert_facts"]
    values = [
        REPAIR_CONTRACT_VERSION,
        payload.get("operation_id"),
        payload.get("expected_content_token"),
        payload.get("event_uid"),
        payload.get("immutable_facts_digest"),
        immutable.get("device_serial"),
        immutable.get("source_uid"),
        immutable.get("source_user_id"),
        immutable.get("device_event_time"),
        immutable.get("punch"),
        immutable.get("status"),
        immutable.get("raw_punch"),
        immutable.get("source"),
        desired.get("employee_name"),
        desired.get("cnic"),
        desired.get("identity_digest"),
        payload.get("connector_id"),
        insert.get("zone_id"),
        insert.get("zone_name"),
        insert.get("device_id"),
        insert.get("capture_type"),
        insert.get("trust_status"),
        insert.get("clock_diff_seconds"),
    ]
    components = ["\0" if value is None else str(value) for value in values]
    if any("\0" in value or "\x1f" in value for value in components if value != "\0"):
        raise RepairError(
            "A frozen Oracle field contains a forbidden control character.",
            code="ORACLE_PAYLOAD_CONTROL_CHARACTER",
        )
    return hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()


def _oracle_item_payload(
    event: AttendanceEvent,
    item: AttendanceRepairItem,
    *,
    connector: Connector,
    include_operation: bool,
) -> dict[str, Any]:
    try:
        desired_name = decrypt_text(item.desired_display_name_encrypted)
        desired_cnic = decrypt_cnic(item.desired_cnic_encrypted)
    except Exception as exc:
        raise RepairError(
            "Frozen desired identity cannot be decrypted.",
            code="FROZEN_PII_UNREADABLE",
        ) from exc
    if not desired_name or not desired_cnic:
        raise RepairError(
            "Frozen desired identity cannot be decrypted.", code="FROZEN_PII_UNREADABLE"
        )
    if any(character in desired_name for character in ("\0", "\x1f")):
        raise RepairError(
            "The current terminal name contains a forbidden control character.",
            code="ORACLE_PAYLOAD_CONTROL_CHARACTER",
        )
    payload: dict[str, Any] = {
        "connector_id": connector.connector_id,
        "event_uid": item.event_uid,
        "immutable_facts": _immutable_facts(event),
        "immutable_facts_digest": item.immutable_facts_digest,
        "desired_identity": {
            "employee_name": desired_name,
            "cnic": desired_cnic,
            "identity_digest": item.desired_identity_digest,
        },
        "insert_facts": {
            "zone_id": connector.zone_id,
            "zone_name": connector.zone_name,
            "device_id": connector.device_id,
            "capture_type": (
                "DUMP_RECONNECT"
                if event.source == "RECONCILE_15M"
                else event.source
                if event.source
                in {
                    "LIVE",
                    "LIVE_POLL",
                    "DUMP_RECONNECT",
                    "DUMP_STARTUP",
                    "MANUAL_REPROCESS",
                }
                else "MANUAL_REPROCESS"
            ),
            "trust_status": (
                "TRUSTED_LIVE"
                if event.clock_quality == "OK" and event.source in {"LIVE", "LIVE_POLL"}
                else "BACKFILL_ACCEPTED_CLOCK_OK"
                if event.clock_quality == "OK"
                else "SUSPECT_DEVICE_TIME"
            ),
            "clock_diff_seconds": event.clock_drift_seconds,
        },
    }
    if include_operation:
        expected_token = decrypt_text(item.expected_oracle_token_encrypted)
        if not expected_token or len(expected_token) != 64:
            raise RepairError(
                "Frozen Oracle content evidence is missing.",
                code="ORACLE_PRECONDITION_MISSING",
            )
        payload.update(
            {
                "operation_id": item.operation_id,
                "expected_content_token": expected_token,
            }
        )
        if payload["insert_facts"]["clock_diff_seconds"] is not None:
            payload["insert_facts"]["clock_diff_seconds"] = format(
                float(payload["insert_facts"]["clock_diff_seconds"]), ".6f"
            )
        payload["payload_digest"] = _oracle_operation_digest(payload)
    return payload


def _preview_items_certificate(session: Session, job_id: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    statement = (
        select(
            AttendanceRepairItem.event_uid,
            AttendanceRepairItem.immutable_facts_digest,
            AttendanceRepairItem.source_ownership_digest,
            AttendanceRepairItem.before_identity_digest,
            AttendanceRepairItem.desired_identity_digest,
            AttendanceRepairItem.oracle_classification,
            AttendanceRepairItem.expected_oracle_token_encrypted,
        )
        .where(AttendanceRepairItem.job_id == job_id)
        .order_by(AttendanceRepairItem.event_uid)
        .execution_options(yield_per=1000)
    )
    for row in session.execute(statement):
        material = _canonical(
            {
                "event_uid": row.event_uid,
                "immutable_facts_digest": row.immutable_facts_digest,
                "source_ownership_digest": row.source_ownership_digest,
                "before_identity_digest": row.before_identity_digest,
                "desired_identity_digest": row.desired_identity_digest,
                "oracle_classification": row.oracle_classification,
                "oracle_token_digest": (
                    _sha(row.expected_oracle_token_encrypted)
                    if row.expected_oracle_token_encrypted
                    else None
                ),
            }
        )
        digest.update(len(material).to_bytes(8, "big"))
        digest.update(material)
        count += 1
    return {"count": count, "digest": digest.hexdigest()}


def _downstream_impact_summary(session: Session, job_id: int) -> dict[str, Any]:
    """Summarize frozen employee/day projection groups without exposing PII."""

    employee_days: set[tuple[int, str]] = set()
    before_groups: set[tuple[str, str]] = set()
    desired_groups: set[tuple[str, str]] = set()
    days: set[str] = set()
    statement = (
        select(
            AttendanceRepairItem.target_id,
            AttendanceRepairItem.before_identity_digest,
            AttendanceRepairItem.desired_identity_digest,
            AttendanceEvent.device_event_time,
        )
        .join(
            AttendanceEvent,
            AttendanceEvent.id == AttendanceRepairItem.attendance_event_id,
        )
        .where(AttendanceRepairItem.job_id == job_id)
        .execution_options(yield_per=1000)
    )
    for row in session.execute(statement):
        pakistan_day = ensure_utc(row.device_event_time).astimezone(PAKISTAN_TZ).date().isoformat()
        days.add(pakistan_day)
        employee_days.add((int(row.target_id), pakistan_day))
        before_groups.add((str(row.before_identity_digest), pakistan_day))
        desired_groups.add((str(row.desired_identity_digest), pakistan_day))
    ordered_days = sorted(days)
    return {
        "timezone": "Asia/Karachi",
        "calendar_days": len(days),
        "employee_days": len(employee_days),
        "before_identity_day_groups": len(before_groups),
        "desired_identity_day_groups": len(desired_groups),
        "first_date": ordered_days[0] if ordered_days else None,
        "last_date": ordered_days[-1] if ordered_days else None,
    }


def _preview_material(session: Session, job: AttendanceRepairJob) -> dict[str, Any]:
    targets = list(
        session.scalars(
            select(AttendanceRepairTarget)
            .where(AttendanceRepairTarget.job_id == job.id)
            .order_by(AttendanceRepairTarget.id)
        ).all()
    )
    cohorts = list(
        session.scalars(
            select(AttendanceRepairCohort)
            .join(
                AttendanceRepairTarget,
                AttendanceRepairCohort.target_id == AttendanceRepairTarget.id,
            )
            .where(AttendanceRepairTarget.job_id == job.id)
            .order_by(AttendanceRepairCohort.id)
        ).all()
    )
    return {
        "schema_version": "1",
        "job_id": job.job_id,
        "connector_id": job.connector_id,
        "zkt_device_id": job.zkt_device_id,
        "source_certificate_digest": job.source_certificate_digest,
        "date_start_utc": job.date_start_utc.isoformat() if job.date_start_utc else None,
        "date_end_utc": job.date_end_utc.isoformat() if job.date_end_utc else None,
        "targets": [
            {
                "user_key": row.user_key,
                "expected_row_version": row.expected_row_version,
                "identity_snapshot_id": row.identity_snapshot_id,
                "desired_identity_digest": row.desired_identity_digest,
            }
            for row in targets
        ],
        "cohorts": [
            {
                "token": row.cohort_token,
                "membership_digest": row.membership_digest,
                "evidence": row.evidence_classification,
                "event_count": row.event_count,
            }
            for row in cohorts
        ],
        "items_certificate": _preview_items_certificate(session, job.id),
        "downstream_impact": _downstream_impact_summary(session, job.id),
    }


async def classify_repair_preview(job_public_id: str, *, max_batches: int = 5) -> None:
    """Checkpoint bounded read-only Oracle classification for one preview."""

    from zk_add.db import session_scope

    try:
        await oracle_repair_capabilities()
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_public_id)
            )
            if (
                job is None
                or job.status != "PREPARING_SOURCE"
                or job.phase != "ORACLE_CLASSIFICATION"
            ):
                return
            item_ids = list(
                session.scalars(
                    select(AttendanceRepairItem.id)
                    .where(
                        AttendanceRepairItem.job_id == job.id,
                        AttendanceRepairItem.state == "FROZEN",
                    )
                    .order_by(AttendanceRepairItem.id)
                    .limit(
                        settings.attendance_repair_oracle_batch_size
                        * max(1, max_batches)
                    )
                ).all()
            )
        for offset in range(0, len(item_ids), settings.attendance_repair_oracle_batch_size):
            batch_ids = item_ids[offset : offset + settings.attendance_repair_oracle_batch_size]
            with session_scope() as session:
                job = session.scalar(
                    select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_public_id)
                )
                if (
                    job is None
                    or job.status != "PREPARING_SOURCE"
                    or job.phase != "ORACLE_CLASSIFICATION"
                ):
                    return
                connector = session.get(Connector, job.connector_id)
                if connector is None or connector.zkt_device is None:
                    raise RepairError("Connector disappeared.", code="CONNECTOR_DRIFT")
                current_items = list(
                    session.scalars(
                        select(AttendanceRepairItem)
                        .where(AttendanceRepairItem.id.in_(batch_ids))
                        .order_by(AttendanceRepairItem.id)
                    ).all()
                )
                if len(current_items) != len(batch_ids):
                    raise RepairError("Frozen preview membership changed.", code="EVENT_DRIFT")
                events = {
                    row.id: row
                    for row in session.scalars(
                        select(AttendanceEvent).where(
                            AttendanceEvent.id.in_(
                                [row.attendance_event_id for row in current_items]
                            )
                        )
                    ).all()
                }
                if len(events) != len(current_items):
                    raise RepairError(
                        "A frozen attendance event disappeared.",
                        code="EVENT_DRIFT",
                    )
                payload_items = [
                    _oracle_item_payload(
                        events[row.attendance_event_id],
                        row,
                        connector=connector,
                        include_operation=False,
                    )
                    for row in current_items
                ]
                connector_id = connector.connector_id
                terminal_serial = connector.zkt_device.serial
                expected_uids = {row.event_uid for row in current_items}
            response = await _ords_request(
                "raw-captures/identity-repairs/check",
                payload={
                    "contract_version": REPAIR_CONTRACT_VERSION,
                    "connector_id": connector_id,
                    "terminal_serial": terminal_serial,
                    "items": payload_items,
                },
            )
            results = response.get("results")
            if not isinstance(results, list):
                raise OracleRepairError(
                    "Oracle check omitted its results.",
                    code="ORDS_MALFORMED_RESPONSE",
                    retryable=False,
                )
            by_uid: dict[str, dict[str, Any]] = {}
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("event_uid"), str):
                    raise OracleRepairError(
                        "Oracle check returned an invalid result.",
                        code="ORDS_MALFORMED_RESPONSE",
                        retryable=False,
                    )
                if result["event_uid"] in by_uid:
                    raise OracleRepairError(
                        "Oracle check returned a duplicate event UID.",
                        code="ORDS_MALFORMED_RESPONSE",
                        retryable=False,
                    )
                by_uid[result["event_uid"]] = result
            if set(by_uid) != expected_uids:
                raise OracleRepairError(
                    "Oracle check did not return the exact frozen event membership.",
                    code="ORDS_MEMBERSHIP_MISMATCH",
                    retryable=False,
                )
            with session_scope() as session:
                job = session.scalar(
                    select(AttendanceRepairJob)
                    .where(AttendanceRepairJob.job_id == job_public_id)
                    .with_for_update()
                )
                if (
                    job is None
                    or job.status != "PREPARING_SOURCE"
                    or job.phase != "ORACLE_CLASSIFICATION"
                ):
                    return
                job.next_attempt_at = None
                job.error_code = None
                job.error_message = None
                job.wait_reason = None
                current_items = list(
                    session.scalars(
                        select(AttendanceRepairItem)
                        .where(AttendanceRepairItem.id.in_(batch_ids))
                        .with_for_update()
                    ).all()
                )
                for item in current_items:
                    result = by_uid[item.event_uid]
                    classification = str(result.get("classification") or "").upper()
                    if (
                        classification
                        not in SAFE_ORACLE_CLASSIFICATIONS | UNSAFE_ORACLE_CLASSIFICATIONS
                    ):
                        raise OracleRepairError(
                            "Oracle check returned an unknown classification.",
                            code="ORDS_MALFORMED_RESPONSE",
                            retryable=False,
                        )
                    token = result.get("current_content_token")
                    if classification in SAFE_ORACLE_CLASSIFICATIONS and (
                        not isinstance(token, str)
                        or len(token) != 64
                        or any(character not in "0123456789abcdef" for character in token)
                    ):
                        raise OracleRepairError(
                            "Oracle check omitted the content precondition token.",
                            code="ORDS_MALFORMED_RESPONSE",
                            retryable=False,
                        )
                    item.oracle_classification = classification
                    item.expected_oracle_token_encrypted = encrypt_text(token) if token else None
                    item.updated_at = utc_now()
                    if classification in SAFE_ORACLE_CLASSIFICATIONS:
                        item.state = "ORACLE_APPLY"
                        item.outcome = None
                        item.error_code = None
                        item.completed_at = None
                    else:
                        item.state = "NEEDS_REVIEW"
                        item.outcome = classification
                        item.error_code = classification
                        item.completed_at = utc_now()

        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if (
                job is None
                or job.status != "PREPARING_SOURCE"
                or job.phase != "ORACLE_CLASSIFICATION"
            ):
                return
            total, classified, exclusions, remaining = session.execute(
                select(
                    func.count(AttendanceRepairItem.id),
                    func.count(AttendanceRepairItem.id).filter(
                        AttendanceRepairItem.state.in_({"ORACLE_APPLY", "NEEDS_REVIEW"})
                    ),
                    func.count(AttendanceRepairItem.id).filter(
                        AttendanceRepairItem.state == "NEEDS_REVIEW"
                    ),
                    func.count(AttendanceRepairItem.id).filter(
                        AttendanceRepairItem.state == "FROZEN"
                    ),
                ).where(AttendanceRepairItem.job_id == job.id)
            ).one()
            if int(remaining or 0):
                return
            if int(total or 0) != int(classified or 0):
                raise OracleRepairError(
                    "Oracle check did not classify every frozen event.",
                    code="ORDS_MEMBERSHIP_MISMATCH",
                    retryable=False,
                )
            exclusions = int(exclusions or 0)
            job.excluded_count = exclusions
            job.attention_event_count = exclusions
            job.next_attempt_at = None
            job.error_code = None
            job.error_message = None
            job.preview_digest = _sha(_preview_material(session, job))
            job.preview_expires_at = utc_now() + timedelta(
                seconds=settings.attendance_repair_preview_seconds
            )
            if exclusions == int(total or 0):
                job.status = "COMPLETED_WITH_ATTENTION"
                job.phase = "CERTIFIED_NO_SAFE_EVENTS"
                job.wait_reason = "NO_SAFE_EVENTS"
                job.error_code = "NO_SAFE_EVENTS"
                job.error_message = "Every frozen event failed Oracle safety classification; no correction was made."
                job.completed_at = utc_now()
            else:
                job.status = "AWAITING_APPROVAL"
                job.phase = "PREVIEW_FROZEN"
                job.wait_reason = None
            job.updated_at = utc_now()
            _repair_event(
                session,
                job,
                job.status,
                details={
                    "preview_digest": job.preview_digest,
                    "event_count": job.event_count,
                    "excluded_count": exclusions,
                },
            )
            if exclusions == int(total or 0):
                _refresh_repair_totals(session, job)
    except (OracleRepairError, RepairError) as exc:
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None or job.status in JOB_TERMINAL_STATES:
                return
            job.phase = "ORACLE_CLASSIFICATION"
            job.error_code = getattr(exc, "code", "PREVIEW_CLASSIFICATION_FAILED")
            job.error_message = str(exc)[:500]
            job.wait_reason = job.error_code
            job.updated_at = utc_now()
            retryable = isinstance(exc, OracleRepairError) and exc.retryable
            if retryable:
                job.preparation_attempt_count += 1
                if job.preparation_attempt_count < settings.attendance_repair_retry_limit:
                    job.status = "PREPARING_SOURCE"
                    job.next_attempt_at = _retry_at(job.preparation_attempt_count)
                    _repair_event(
                        session,
                        job,
                        "ORACLE_CLASSIFICATION_RETRY",
                        details={
                            "error_code": job.error_code,
                            "attempt": job.preparation_attempt_count,
                        },
                        idempotency_key=(
                            f"classification-retry-{job.job_id}-"
                            f"{job.preparation_attempt_count}"
                        ),
                    )
                    return
                job.error_code = "PREVIEW_RETRY_EXHAUSTED"
                job.error_message = (
                    "Automatic Oracle preview retries were exhausted; correct the external "
                    "condition and retry safe work."
                )
                job.wait_reason = job.error_code
            job.status = "NEEDS_ATTENTION"
            job.next_attempt_at = None
            _repair_event(
                session,
                job,
                "NEEDS_ATTENTION",
                details={"error_code": job.error_code},
            )
            _upsert_repair_alert(session, job, error_code=job.error_code)


def _expected_confirmation(job: AttendanceRepairJob, connector: Connector) -> str:
    prefix = (job.preview_digest or "")[:12]
    return (
        f"REPAIR {job.target_count} EMPLOYEES / {job.event_count} EVENTS "
        f"ON {connector.device_id} {prefix}"
    )


def _events_for_frozen_cohort(
    session: Session,
    job: AttendanceRepairJob,
    cohort: AttendanceRepairCohort,
) -> list[AttendanceEvent]:
    statement = select(AttendanceEvent).where(
        AttendanceEvent.zkt_device_id == job.zkt_device_id,
        AttendanceEvent.device_user_id == cohort.source_device_user_id,
    )
    if job.date_start_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time >= job.date_start_utc)
    if job.date_end_utc is not None:
        statement = statement.where(AttendanceEvent.device_event_time < job.date_end_utc)
    rows = []
    for event in session.scalars(statement).all():
        if cohort.source_uid_digest is None:
            if event.uid not in {None, ""}:
                continue
        elif _protected_digest(event.uid or "") != cohort.source_uid_digest:
            continue
        if _protected_digest(event.user_id) != cohort.source_user_id_digest:
            continue
        rows.append(event)
    return rows


def assert_preview_current(session: Session, job: AttendanceRepairJob) -> None:
    connector = session.get(Connector, job.connector_id)
    zkt = session.get(ZKTDevice, job.zkt_device_id)
    if connector is None or zkt is None:
        raise RepairError("The terminal changed after preview.", code="TERMINAL_DRIFT")
    _source_current, certificate, _coverage = _source_certificate(session, connector)
    # Freshness gates the start of a freeze. Once exact membership is frozen,
    # elapsed wall time alone is not drift: certificate identity, terminal
    # parity, cohorts, target versions and event facts below must still match.
    if certificate.get("certificate_digest") != job.source_certificate_digest:
        raise RepairError(
            "Terminal source coverage changed; regenerate preview.", code="SOURCE_DRIFT"
        )
    targets = list(
        session.scalars(
            select(AttendanceRepairTarget).where(AttendanceRepairTarget.job_id == job.id)
        ).all()
    )
    for target in targets:
        user = session.get(DeviceUser, target.device_user_id)
        if user is None or user.row_version != target.expected_row_version:
            raise RepairError("An employee changed; regenerate preview.", code="TARGET_DRIFT")
        try:
            current_cnic = decrypt_cnic(user.cnic_encrypted)
        except Exception as exc:
            raise RepairError(
                "An employee's protected identity is unreadable; regenerate after repair.",
                code="TARGET_PII_UNREADABLE",
            ) from exc
        if _identity_digest(user.display_name, current_cnic) != target.desired_identity_digest:
            raise RepairError(
                "An employee identity changed; regenerate preview.", code="TARGET_DRIFT"
            )
        if zkt.identity_snapshot_id != target.identity_snapshot_id:
            raise RepairError(
                "The terminal identity snapshot changed; regenerate preview.", code="TARGET_DRIFT"
            )
    cohorts = list(
        session.scalars(
            select(AttendanceRepairCohort)
            .join(
                AttendanceRepairTarget,
                AttendanceRepairCohort.target_id == AttendanceRepairTarget.id,
            )
            .where(AttendanceRepairTarget.job_id == job.id)
        ).all()
    )
    for cohort in cohorts:
        rows = _events_for_frozen_cohort(session, job, cohort)
        if len(rows) != cohort.event_count or _membership_digest(rows) != cohort.membership_digest:
            raise RepairError("A source cohort changed; regenerate preview.", code="COHORT_DRIFT")
    item_events = session.execute(
        select(AttendanceRepairItem, AttendanceEvent)
        .join(
            AttendanceEvent,
            AttendanceEvent.id == AttendanceRepairItem.attendance_event_id,
        )
        .where(AttendanceRepairItem.job_id == job.id)
        .order_by(AttendanceRepairItem.id)
        .execution_options(yield_per=1000)
    )
    observed_items = 0
    for item, event in item_events:
        observed_items += 1
        if event.event_uid != item.event_uid:
            raise RepairError("A frozen event disappeared; regenerate preview.", code="EVENT_DRIFT")
        if _immutable_digest(event) != item.immutable_facts_digest:
            raise RepairError(
                "A physical punch fact changed; repair is blocked.", code="IMMUTABLE_DRIFT"
            )
        if _source_ownership_digest(event) != item.source_ownership_digest:
            raise RepairError(
                "A frozen event's source ownership changed; regenerate preview.",
                code="SOURCE_OWNERSHIP_DRIFT",
            )
        try:
            current_event_cnic = decrypt_cnic(event.cnic_encrypted)
        except Exception as exc:
            raise RepairError(
                "A frozen event's protected identity is unreadable.",
                code="EVENT_PII_UNREADABLE",
            ) from exc
        current_identity_digest = _identity_digest(
            event.display_name or "",
            current_event_cnic,
        )
        if current_identity_digest != item.before_identity_digest:
            raise RepairError(
                "A frozen event's effective identity changed; regenerate preview.",
                code="EVENT_IDENTITY_DRIFT",
            )
    if observed_items != job.event_count:
        raise RepairError("A frozen event disappeared; regenerate preview.", code="EVENT_DRIFT")
    if not job.preview_digest or _sha(_preview_material(session, job)) != job.preview_digest:
        raise RepairError("The frozen preview digest changed.", code="PREVIEW_DRIFT")


def approve_repair_job(
    session: Session,
    *,
    job: AttendanceRepairJob,
    actor: str,
    reason: str,
    typed_confirmation: str,
    preview_digest: str,
    idempotency_key: str,
) -> AttendanceRepairJob:
    if not settings.attendance_repair_execution_enabled:
        raise RepairError(
            "Employee attendance repair execution remains disabled.",
            code="EXECUTION_DISABLED",
        )
    job = _lock_job(session, job.id)
    approval_request_digest = _protected_digest(
        {
            "action": "approve",
            "actor": actor,
            "reason": reason.strip(),
            "typed_confirmation": typed_confirmation,
            "preview_digest": preview_digest,
        }
    )
    replay = session.scalar(
        select(AttendanceRepairEvent).where(
            AttendanceRepairEvent.job_id == job.id,
            AttendanceRepairEvent.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        if not secrets.compare_digest(
            str((replay.details or {}).get("request_digest") or ""),
            approval_request_digest,
        ):
            raise RepairError(
                "That idempotency key was used for a different approval request.",
                code="IDEMPOTENCY_CONFLICT",
            )
        return job
    if job.status != "AWAITING_APPROVAL":
        raise RepairError("This repair is not awaiting approval.", code="JOB_STATE_CONFLICT")
    now = utc_now()
    if job.preview_expires_at is None or ensure_utc(job.preview_expires_at) <= now:
        raise RepairError("The 15-minute preview expired; regenerate it.", code="PREVIEW_EXPIRED")
    if not job.preview_digest or not secrets.compare_digest(job.preview_digest, preview_digest):
        raise RepairError("Preview digest mismatch.", code="PREVIEW_DIGEST_MISMATCH")
    connector = session.get(Connector, job.connector_id)
    if connector is None:
        raise RepairError("Connector no longer exists.", code="CONNECTOR_DRIFT")
    expected = _expected_confirmation(job, connector)
    if typed_confirmation != expected:
        raise RepairError(f"Type {expected} exactly to approve.", code="CONFIRMATION_MISMATCH")
    backlog = int(
        session.scalar(
            select(func.count(OrdsOutbox.id)).where(OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES))
        )
        or 0
    )
    if backlog >= settings.reconciliation_history_backlog_pause:
        raise RepairError(
            "Repair intake is paused while live Oracle delivery catches up.",
            code="LIVE_ORDS_BACKLOG_HIGH",
        )
    assert_preview_current(session, job)
    # Reasons can accidentally contain identity data. Keep the operator's text
    # encrypted at rest and place only its digest in audit/repair ledgers.
    job.reason = encrypt_text(reason.strip())
    job.status = "QUEUED"
    job.phase = "ORACLE_REPAIR"
    job.approved_at = now
    job.updated_at = now
    _repair_event(
        session,
        job,
        "QUEUED",
        details={
            "preview_digest": preview_digest,
            "reason_digest": _reason_digest(reason),
            "request_digest": approval_request_digest,
        },
        idempotency_key=idempotency_key,
    )
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_REPAIR_APPROVED",
        target_type="attendance_repair_job",
        target_id=job.job_id,
        outcome="QUEUED",
        after={
            "connector_id": connector.connector_id,
            "target_count": job.target_count,
            "event_count": job.event_count,
            "preview_digest": preview_digest,
            "reason_digest": _reason_digest(reason),
        },
    )
    return job


def control_repair_job(
    session: Session,
    *,
    job: AttendanceRepairJob,
    action: str,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> AttendanceRepairJob:
    job = _lock_job(session, job.id)
    control_request_digest = _protected_digest(
        {
            "action": action,
            "actor": actor,
            "reason": reason.strip(),
        }
    )
    replay = session.scalar(
        select(AttendanceRepairEvent).where(
            AttendanceRepairEvent.job_id == job.id,
            AttendanceRepairEvent.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        if not secrets.compare_digest(
            str((replay.details or {}).get("request_digest") or ""),
            control_request_digest,
        ):
            raise RepairError(
                "That idempotency key was used for a different repair control request.",
                code="IDEMPOTENCY_CONFLICT",
            )
        return job
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise RepairError("Unknown repair action.", code="UNKNOWN_ACTION")
    before = job.status
    now = utc_now()
    if action == "pause":
        if job.status not in {"QUEUED", "RUNNING", "WAITING_ORACLE", "WAITING_DOWNSTREAM"}:
            raise RepairError("This repair cannot be paused now.", code="JOB_STATE_CONFLICT")
        job.status = "PAUSED"
        job.wait_reason = "OPERATOR_PAUSED"
    elif action == "resume":
        if job.status != "PAUSED":
            raise RepairError("Only a paused repair can resume.", code="JOB_STATE_CONFLICT")
        job.status = "RUNNING"
        job.wait_reason = None
    elif action == "cancel":
        if job.status in JOB_TERMINAL_STATES:
            raise RepairError("This repair is already finished.", code="JOB_STATE_CONFLICT")
        if job.first_oracle_mutation_at is None:
            job.status = "CANCELLED"
            job.completed_at = now
            for item in session.scalars(
                select(AttendanceRepairItem).where(
                    AttendanceRepairItem.job_id == job.id,
                    AttendanceRepairItem.state.not_in(ITEM_TERMINAL_STATES),
                )
            ).all():
                item.state = "CANCELLED"
                item.outcome = "CANCELLED_BEFORE_EXECUTION"
                item.completed_at = now
        else:
            job.cancellation_requested = True
            for item in session.scalars(
                select(AttendanceRepairItem).where(
                    AttendanceRepairItem.job_id == job.id,
                    AttendanceRepairItem.state == "ORACLE_APPLY",
                )
            ).all():
                item.state = "CANCELLED"
                item.outcome = "CANCELLED_UNTOUCHED"
                item.completed_at = now
    else:
        if job.status not in {"NEEDS_ATTENTION", "COMPLETED_WITH_ATTENTION"}:
            raise RepairError("Only a held repair can retry.", code="JOB_STATE_CONFLICT")
        other_active = session.scalar(
            select(AttendanceRepairJob.id).where(
                AttendanceRepairJob.connector_id == job.connector_id,
                AttendanceRepairJob.id != job.id,
                AttendanceRepairJob.status.not_in(JOB_TERMINAL_STATES),
            )
        )
        if other_active is not None:
            raise RepairError(
                "Another employee repair is active on this terminal; finish it before "
                "retrying this job.",
                code="ACTIVE_REPAIR_EXISTS",
            )
        if job.first_oracle_mutation_at is None and job.phase in {
            "ORACLE_CLASSIFICATION",
            "SOURCE_REVIEW",
        }:
            connector = session.get(Connector, job.connector_id)
            if connector is None:
                raise RepairError("Connector no longer exists.", code="CONNECTOR_DRIFT")
            source_current, certificate, _coverage = _source_certificate(session, connector)
            frozen_count = int(
                session.scalar(
                    select(func.count(AttendanceRepairItem.id)).where(
                        AttendanceRepairItem.job_id == job.id
                    )
                )
                or 0
            )
            if frozen_count:
                if certificate.get("certificate_digest") != job.source_certificate_digest:
                    raise RepairError(
                        "Source evidence changed; cancel this draft and prepare a new preview.",
                        code="SOURCE_DRIFT",
                    )
                for item in session.scalars(
                    select(AttendanceRepairItem).where(
                        AttendanceRepairItem.job_id == job.id,
                        AttendanceRepairItem.state.not_in(ITEM_TERMINAL_STATES),
                    )
                ).all():
                    item.oracle_classification = "NOT_CHECKED"
                    item.expected_oracle_token_encrypted = None
                    item.state = "FROZEN"
                    item.error_code = None
                    item.error_message = None
                    item.next_attempt_at = None
                job.status = "PREPARING_SOURCE"
                job.phase = "ORACLE_CLASSIFICATION"
                job.preparation_attempt_count = 0
                job.next_attempt_at = None
            elif source_current:
                job.source_certificate_digest = certificate["certificate_digest"]
                _freeze_membership(session, job)
            else:
                _attach_source_dependency(session, job=job, connector=connector)
        else:
            retryable = list(
                session.scalars(
                    select(AttendanceRepairItem).where(
                        AttendanceRepairItem.job_id == job.id,
                        AttendanceRepairItem.state == "NEEDS_REVIEW",
                        AttendanceRepairItem.error_code.in_(
                            [
                                "RETRY_EXHAUSTED",
                                "ORDS_TRANSPORT_ERROR",
                                "ORDS_HTTP_429",
                                "ORDS_HTTP_500",
                                "ORDS_HTTP_502",
                                "ORDS_HTTP_503",
                                "ORDS_HTTP_504",
                                "ORDS_AUTHENTICATION_FAILED",
                                "ORDS_AUTHENTICATION_NOT_CONFIGURED",
                                "ORDS_CAPABILITY_MISSING",
                                "ORDS_NOT_CONFIGURED",
                                "ORDS_MALFORMED_RESPONSE",
                                "ORDS_MEMBERSHIP_MISMATCH",
                                "FROZEN_PII_UNREADABLE",
                                "DOWNSTREAM_TIMEOUT",
                                "DOWNSTREAM_PENDING",
                                "DOWNSTREAM_EVIDENCE_MISSING",
                                "DOWNSTREAM_IDENTITY_MISMATCH",
                                "ORACLE_CONTENT_REGRESSED",
                            ]
                        ),
                    )
                ).all()
            )
            if not retryable:
                raise RepairError("No safely retryable items remain.", code="NO_RETRYABLE_ITEMS")
            newer_overlap = session.scalar(
                select(AttendanceRepairItem.id)
                .join(
                    AttendanceRepairJob,
                    AttendanceRepairItem.job_id == AttendanceRepairJob.id,
                )
                .where(
                    AttendanceRepairJob.id > job.id,
                    AttendanceRepairJob.status != "CANCELLED",
                    AttendanceRepairItem.attendance_event_id.in_(
                        [item.attendance_event_id for item in retryable]
                    ),
                )
                .limit(1)
            )
            if newer_overlap is not None:
                raise RepairError(
                    "A newer repair already owns one or more of these events; this older "
                    "frozen identity cannot be replayed.",
                    code="REPAIR_SUPERSEDED",
                )
            receipt_item_ids = set(
                session.scalars(
                    select(OracleIdentityRepairReceipt.repair_item_id).where(
                        OracleIdentityRepairReceipt.repair_item_id.in_(
                            [item.id for item in retryable]
                        )
                    )
                ).all()
            )
            activated_item_ids = set(
                session.scalars(
                    select(AttendanceIdentityRevision.repair_item_id).where(
                        AttendanceIdentityRevision.repair_item_id.in_(
                            [item.id for item in retryable]
                        ),
                        AttendanceIdentityRevision.state == "ACTIVE",
                    )
                ).all()
            )
            for item in retryable:
                item.state = (
                    "DOWNSTREAM_VERIFY"
                    if item.id in receipt_item_ids and item.id in activated_item_ids
                    else "ADD_ACTIVATE"
                    if item.id in receipt_item_ids
                    else "ORACLE_VERIFY"
                    if item.operation_payload_digest
                    else "ORACLE_APPLY"
                )
                item.error_code = None
                item.error_message = None
                item.next_attempt_at = now
                item.completed_at = None
                if item.state == "DOWNSTREAM_VERIFY":
                    item.downstream_attempt_count = 0
                elif item.state in {"ORACLE_APPLY", "ORACLE_VERIFY"}:
                    item.oracle_attempt_count = 0
            job.status = "RUNNING"
        job.completed_at = None
        job.evidence_digest = None
        job.error_code = None
        job.error_message = None
        job.wait_reason = None
    job.updated_at = now
    _repair_event(
        session,
        job,
        job.status,
        details={
            "action": action,
            "reason_digest": _reason_digest(reason),
            "request_digest": control_request_digest,
        },
        idempotency_key=idempotency_key,
    )
    append_audit(
        session,
        actor=actor,
        action=f"ATTENDANCE_REPAIR_{action.upper()}",
        target_type="attendance_repair_job",
        target_id=job.job_id,
        outcome=job.status,
        before={"status": before},
        after={"status": job.status, "reason_digest": _reason_digest(reason)},
    )
    return job


def _serialize_target(target: AttendanceRepairTarget) -> dict[str, Any]:
    return {
        "user_key": target.user_key,
        "display_name": decrypt_text(target.desired_display_name_encrypted),
        "cnic_masked": _mask_cnic_last4(target.desired_cnic_last4),
        "expected_row_version": target.expected_row_version,
        "desired_identity_digest": target.desired_identity_digest,
        "status": target.status,
        "event_count": target.event_count,
        "completed_event_count": target.completed_event_count,
        "attention_event_count": target.attention_event_count,
    }


def serialize_repair_job(
    session: Session,
    job: AttendanceRepairJob,
    *,
    include_targets: bool = True,
    include_items: bool = False,
    item_cursor: int | None = None,
    item_limit: int = 500,
) -> dict[str, Any]:
    connector = session.get(Connector, job.connector_id)
    targets = (
        list(
            session.scalars(
                select(AttendanceRepairTarget)
                .where(AttendanceRepairTarget.job_id == job.id)
                .order_by(AttendanceRepairTarget.id)
            ).all()
        )
        if include_targets or include_items
        else []
    )
    response: dict[str, Any] = {
        "job_id": job.job_id,
        "connector_id": connector.connector_id if connector else None,
        "device_id": connector.device_id if connector else None,
        "actor": job.actor,
        "status": job.status,
        "phase": job.phase,
        "date_scope": {
            "timezone": "Asia/Karachi",
            "start_utc": job.date_start_utc,
            "end_utc_exclusive": job.date_end_utc,
        },
        "request_digest": job.request_digest,
        "preview_digest": job.preview_digest,
        "preview_expires_at": job.preview_expires_at,
        "source_dependency_job_id": (
            session.get(ReconciliationJob, job.source_reconciliation_job_id).job_id
            if job.source_reconciliation_job_id
            and session.get(ReconciliationJob, job.source_reconciliation_job_id)
            else None
        ),
        "totals": {
            "employees": job.target_count,
            "events": job.event_count,
            "excluded": job.excluded_count,
            "completed_employees": job.completed_target_count,
            "completed_events": job.completed_event_count,
            "attention_events": job.attention_event_count,
        },
        "wait_reason": job.wait_reason,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "cancellation_requested": job.cancellation_requested,
        "preparation_attempt_count": job.preparation_attempt_count,
        "next_attempt_at": job.next_attempt_at,
        "created_at": job.created_at,
        "approved_at": job.approved_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "targets": [_serialize_target(target) for target in targets],
    }
    if job.status == "AWAITING_APPROVAL" and connector is not None:
        response["typed_confirmation"] = _expected_confirmation(job, connector)
    if include_items:
        response["downstream_impact"] = _downstream_impact_summary(session, job.id)
        item_limit = max(1, min(item_limit, 500))
        item_statement = select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        if item_cursor is not None:
            item_statement = item_statement.where(AttendanceRepairItem.id > item_cursor)
        fetched_items = list(
            session.scalars(
                item_statement.order_by(AttendanceRepairItem.id).limit(item_limit + 1)
            ).all()
        )
        items = fetched_items[:item_limit]
        event_by_id = {
            row.id: row
            for row in session.scalars(
                select(AttendanceEvent).where(
                    AttendanceEvent.id.in_([item.attendance_event_id for item in items])
                )
            ).all()
        }
        target_by_id = {target.id: target.user_key for target in targets}
        response["items"] = [
            {
                "event_uid": item.event_uid,
                "user_key": target_by_id.get(item.target_id),
                "event_time": (
                    event_by_id[item.attendance_event_id].device_event_time
                    if item.attendance_event_id in event_by_id
                    else None
                ),
                "state": item.state,
                "oracle_classification": item.oracle_classification,
                "outcome": item.outcome,
                "attempt_count": item.attempt_count,
                "oracle_attempt_count": item.oracle_attempt_count,
                "downstream_attempt_count": item.downstream_attempt_count,
                "next_attempt_at": item.next_attempt_at,
                "error_code": item.error_code,
            }
            for item in items
        ]
        response["items_next_cursor"] = (
            items[-1].id if len(fetched_items) > item_limit and items else None
        )
    return response


def _certificate_stream(rows: Iterable[Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for material_value in rows:
        material = _canonical(material_value)
        digest.update(len(material).to_bytes(8, "big"))
        digest.update(material)
        count += 1
    return {"count": count, "digest": digest.hexdigest()}


def _repair_certificate_digest(session: Session, job: AttendanceRepairJob) -> str:
    targets = session.execute(
        select(
            AttendanceRepairTarget.user_key,
            AttendanceRepairTarget.desired_identity_digest,
            AttendanceRepairTarget.status,
            AttendanceRepairTarget.event_count,
            AttendanceRepairTarget.completed_event_count,
            AttendanceRepairTarget.attention_event_count,
        )
        .where(AttendanceRepairTarget.job_id == job.id)
        .order_by(AttendanceRepairTarget.user_key)
        .execution_options(yield_per=500)
    )
    target_certificate = _certificate_stream(
        {
            "user_key": row.user_key,
            "identity_digest": row.desired_identity_digest,
            "status": row.status,
            "events": row.event_count,
            "completed": row.completed_event_count,
            "attention": row.attention_event_count,
        }
        for row in targets
    )
    items = session.execute(
        select(
            AttendanceRepairItem.event_uid,
            AttendanceRepairItem.immutable_facts_digest,
            AttendanceRepairItem.source_ownership_digest,
            AttendanceRepairItem.before_identity_digest,
            AttendanceRepairItem.desired_identity_digest,
            AttendanceRepairItem.oracle_classification,
            AttendanceRepairItem.operation_id,
            AttendanceRepairItem.operation_payload_digest,
            AttendanceRepairItem.state,
            AttendanceRepairItem.outcome,
            AttendanceRepairItem.error_code,
            AttendanceRepairItem.attempt_count,
            AttendanceRepairItem.oracle_attempt_count,
            AttendanceRepairItem.downstream_attempt_count,
        )
        .where(AttendanceRepairItem.job_id == job.id)
        .order_by(AttendanceRepairItem.event_uid)
        .execution_options(yield_per=1000)
    )
    item_certificate = _certificate_stream(
        {
            "event_uid": row.event_uid,
            "immutable": row.immutable_facts_digest,
            "source": row.source_ownership_digest,
            "before": row.before_identity_digest,
            "desired": row.desired_identity_digest,
            "classification": row.oracle_classification,
            "operation_id": row.operation_id,
            "payload_digest": row.operation_payload_digest,
            "state": row.state,
            "outcome": row.outcome,
            "error_code": row.error_code,
            "attempt_count": row.attempt_count,
            "oracle_attempt_count": row.oracle_attempt_count,
            "downstream_attempt_count": row.downstream_attempt_count,
        }
        for row in items
    )
    receipts = session.execute(
        select(
            OracleIdentityRepairReceipt.operation_id,
            OracleIdentityRepairReceipt.payload_digest,
            OracleIdentityRepairReceipt.action,
            OracleIdentityRepairReceipt.oracle_receipt_id,
            OracleIdentityRepairReceipt.verified_identity_digest,
            OracleIdentityRepairReceipt.downstream_status,
        )
        .join(
            AttendanceRepairItem,
            OracleIdentityRepairReceipt.repair_item_id == AttendanceRepairItem.id,
        )
        .where(AttendanceRepairItem.job_id == job.id)
        .order_by(OracleIdentityRepairReceipt.operation_id)
        .execution_options(yield_per=1000)
    )
    receipt_certificate = _certificate_stream(
        {
            "operation_id": row.operation_id,
            "payload_digest": row.payload_digest,
            "action": row.action,
            "receipt_id": row.oracle_receipt_id,
            "identity_digest": row.verified_identity_digest,
            "downstream_status": row.downstream_status,
        }
        for row in receipts
    )
    return _sha(
        {
            "schema_version": "1",
            "job_id": job.job_id,
            "actor": job.actor,
            "request_digest": job.request_digest,
            "preview_digest": job.preview_digest,
            "cohort_digest": job.cohort_digest,
            "source_certificate_digest": job.source_certificate_digest,
            "status": job.status,
            "target_count": job.target_count,
            "event_count": job.event_count,
            "excluded_count": job.excluded_count,
            "completed_target_count": job.completed_target_count,
            "completed_event_count": job.completed_event_count,
            "attention_event_count": job.attention_event_count,
            "preparation_attempt_count": job.preparation_attempt_count,
            "targets": target_certificate,
            "items": item_certificate,
            "oracle_receipts": receipt_certificate,
        }
    )


def _repair_ledger_proof(
    job: AttendanceRepairJob,
    ledger: list[AttendanceRepairEvent],
) -> dict[str, Any]:
    return _repair_ledger_proof_values(
        job,
        (
            {
                "sequence": row.sequence,
                "state": row.state,
                "item_id": row.item_id,
                "idempotency_key": row.idempotency_key,
                "details": row.details,
                "previous_hash": row.previous_hash,
                "row_hash": row.row_hash,
                "created_at": row.created_at,
            }
            for row in ledger
        ),
    )


def _repair_ledger_proof_values(
    job: AttendanceRepairJob,
    ledger: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    previous_hash: str | None = None
    expected_sequence = 1
    valid = True
    event_count = 0
    last_sequence = 0
    for row in ledger:
        material = {
            "job_id": job.job_id,
            "sequence": row["sequence"],
            "state": row["state"],
            "item_id": row["item_id"],
            "idempotency_key": row["idempotency_key"],
            "details": row["details"] or {},
            "previous_hash": row["previous_hash"],
            "created_at": ensure_utc(row["created_at"]).isoformat(),
        }
        expected_hash = _sha(material)
        if (
            row["sequence"] != expected_sequence
            or row["previous_hash"] != previous_hash
            or not secrets.compare_digest(row["row_hash"], expected_hash)
        ):
            valid = False
        previous_hash = row["row_hash"]
        last_sequence = int(row["sequence"])
        event_count += 1
        expected_sequence += 1
    return {
        "valid": valid,
        "event_count": event_count,
        "last_sequence": last_sequence,
        "last_hash": previous_hash,
    }


def _evidence_job(job: AttendanceRepairJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "actor": job.actor,
        "request_digest": job.request_digest,
        "preview_digest": job.preview_digest,
        "cohort_digest": job.cohort_digest,
        "source_certificate_digest": job.source_certificate_digest,
        "status": job.status,
        "target_count": job.target_count,
        "event_count": job.event_count,
        "excluded_count": job.excluded_count,
        "preparation_attempt_count": job.preparation_attempt_count,
        "created_at": job.created_at,
        "approved_at": job.approved_at,
        "completed_at": job.completed_at,
    }


def _evidence_targets(session: Session, job_id: int) -> Iterable[dict[str, Any]]:
    rows = session.execute(
        select(
            AttendanceRepairTarget.user_key,
            AttendanceRepairTarget.desired_cnic_last4,
            AttendanceRepairTarget.desired_identity_digest,
            AttendanceRepairTarget.status,
            AttendanceRepairTarget.event_count,
        )
        .where(AttendanceRepairTarget.job_id == job_id)
        .order_by(AttendanceRepairTarget.user_key, AttendanceRepairTarget.id)
        .execution_options(yield_per=500)
    )
    for row in rows:
        yield {
            "user_key": row.user_key,
            "cnic_last4": row.desired_cnic_last4,
            "desired_identity_digest": row.desired_identity_digest,
            "status": row.status,
            "event_count": row.event_count,
        }


def _evidence_items(session: Session, job_id: int) -> Iterable[dict[str, Any]]:
    rows = session.execute(
        select(
            AttendanceRepairItem.event_uid,
            AttendanceRepairItem.immutable_facts_digest,
            AttendanceRepairItem.source_ownership_digest,
            AttendanceRepairItem.before_identity_digest,
            AttendanceRepairItem.desired_identity_digest,
            AttendanceRepairItem.oracle_classification,
            AttendanceRepairItem.operation_id,
            AttendanceRepairItem.operation_payload_digest,
            AttendanceRepairItem.state,
            AttendanceRepairItem.outcome,
            AttendanceRepairItem.error_code,
            AttendanceRepairItem.attempt_count,
            AttendanceRepairItem.oracle_attempt_count,
            AttendanceRepairItem.downstream_attempt_count,
            AttendanceRepairItem.id,
        )
        .where(AttendanceRepairItem.job_id == job_id)
        .order_by(AttendanceRepairItem.event_uid, AttendanceRepairItem.id)
        .execution_options(yield_per=1000)
    )
    for row in rows:
        yield {
            "event_uid": row.event_uid,
            "immutable_facts_digest": row.immutable_facts_digest,
            "source_ownership_digest": row.source_ownership_digest,
            "before_identity_digest": row.before_identity_digest,
            "desired_identity_digest": row.desired_identity_digest,
            "oracle_classification": row.oracle_classification,
            "operation_id": row.operation_id,
            "operation_payload_digest": row.operation_payload_digest,
            "state": row.state,
            "outcome": row.outcome,
            "error_code": row.error_code,
            "attempt_count": row.attempt_count,
            "oracle_attempt_count": row.oracle_attempt_count,
            "downstream_attempt_count": row.downstream_attempt_count,
        }


def _evidence_receipts(session: Session, job_id: int) -> Iterable[dict[str, Any]]:
    rows = session.execute(
        select(
            OracleIdentityRepairReceipt.operation_id,
            OracleIdentityRepairReceipt.payload_digest,
            OracleIdentityRepairReceipt.action,
            OracleIdentityRepairReceipt.oracle_receipt_id,
            OracleIdentityRepairReceipt.verified_identity_digest,
            OracleIdentityRepairReceipt.raw_content_verified_at,
            OracleIdentityRepairReceipt.downstream_status,
            OracleIdentityRepairReceipt.downstream_verified_at,
        )
        .join(
            AttendanceRepairItem,
            OracleIdentityRepairReceipt.repair_item_id == AttendanceRepairItem.id,
        )
        .where(AttendanceRepairItem.job_id == job_id)
        .order_by(OracleIdentityRepairReceipt.operation_id)
        .execution_options(yield_per=1000)
    )
    for row in rows:
        yield {
            "operation_id": row.operation_id,
            "payload_digest": row.payload_digest,
            "action": row.action,
            "oracle_receipt_id": row.oracle_receipt_id,
            "verified_identity_digest": row.verified_identity_digest,
            "raw_content_verified_at": row.raw_content_verified_at,
            "downstream_status": row.downstream_status,
            "downstream_verified_at": row.downstream_verified_at,
        }


def _evidence_ledger(session: Session, job_id: int) -> Iterable[dict[str, Any]]:
    rows = session.execute(
        select(
            AttendanceRepairEvent.sequence,
            AttendanceRepairEvent.state,
            AttendanceRepairEvent.item_id,
            AttendanceRepairEvent.idempotency_key,
            AttendanceRepairEvent.details,
            AttendanceRepairEvent.previous_hash,
            AttendanceRepairEvent.row_hash,
            AttendanceRepairEvent.created_at,
        )
        .where(AttendanceRepairEvent.job_id == job_id)
        .order_by(AttendanceRepairEvent.sequence)
        .execution_options(yield_per=1000)
    )
    for row in rows:
        yield {
            "sequence": row.sequence,
            "state": row.state,
            "item_id": row.item_id,
            "idempotency_key": row.idempotency_key,
            "details": row.details,
            "previous_hash": row.previous_hash,
            "row_hash": row.row_hash,
            "created_at": row.created_at,
        }


def _evidence_certificate(
    session: Session,
    job: AttendanceRepairJob,
) -> dict[str, Any]:
    calculated_certificate = _repair_certificate_digest(session, job)
    ledger_proof = _repair_ledger_proof_values(job, _evidence_ledger(session, job.id))
    return {
        "issued_digest": job.evidence_digest,
        "calculated_digest": calculated_certificate,
        "valid": bool(
            job.evidence_digest
            and secrets.compare_digest(job.evidence_digest, calculated_certificate)
            and ledger_proof["valid"]
        ),
        "repair_ledger": ledger_proof,
    }


def _evidence_fragments(
    session: Session,
    job: AttendanceRepairJob,
    certificate: dict[str, Any],
    *,
    close_object: bool,
) -> Iterable[bytes]:
    """Yield canonical JSON for the evidence object, excluding export_digest."""

    # Keys remain sorted so hashing these fragments is byte-identical to
    # _canonical(evidence) without constructing the 250k-item object in RAM.
    yield b'{"certificate":'
    yield _canonical(certificate)
    yield b',"items":['
    comma = False
    for row in _evidence_items(session, job.id):
        if comma:
            yield b","
        yield _canonical(row)
        comma = True
    yield b'],"job":'
    yield _canonical(_evidence_job(job))
    yield b',"ledger":['
    comma = False
    for row in _evidence_ledger(session, job.id):
        if comma:
            yield b","
        yield _canonical(row)
        comma = True
    yield b'],"oracle_receipts":['
    comma = False
    for row in _evidence_receipts(session, job.id):
        if comma:
            yield b","
        yield _canonical(row)
        comma = True
    yield b'],"policy":'
    yield _canonical("IMMUTABLE_PUNCH_EFFECTIVE_IDENTITY_REPAIR")
    yield b',"schema_version":"1","targets":['
    comma = False
    for row in _evidence_targets(session, job.id):
        if comma:
            yield b","
        yield _canonical(row)
        comma = True
    yield b"]"
    if close_object:
        yield b"}"


def repair_evidence(session: Session, job: AttendanceRepairJob) -> dict[str, Any]:
    targets = list(_evidence_targets(session, job.id))
    items = list(_evidence_items(session, job.id))
    receipts = list(_evidence_receipts(session, job.id))
    ledger = list(_evidence_ledger(session, job.id))
    evidence = {
        "schema_version": "1",
        "policy": "IMMUTABLE_PUNCH_EFFECTIVE_IDENTITY_REPAIR",
        "job": _evidence_job(job),
        "targets": targets,
        "items": items,
        "oracle_receipts": receipts,
        "ledger": ledger,
    }
    evidence["certificate"] = _evidence_certificate(session, job)
    evidence["export_digest"] = _sha(evidence)
    return evidence


def stream_repair_evidence(job_public_id: str) -> Iterable[bytes]:
    """Stream large evidence exports from one consistent database snapshot."""

    from zk_add.db import SessionLocal

    def generate() -> Iterable[bytes]:
        session = SessionLocal()
        try:
            if session.get_bind().dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            job = session.scalar(
                select(AttendanceRepairJob).where(
                    AttendanceRepairJob.job_id == job_public_id
                )
            )
            if job is None:
                return
            certificate = _evidence_certificate(session, job)
            export_hash = hashlib.sha256()
            for fragment in _evidence_fragments(
                session,
                job,
                certificate,
                close_object=True,
            ):
                export_hash.update(fragment)
            for fragment in _evidence_fragments(
                session,
                job,
                certificate,
                close_object=False,
            ):
                yield fragment
            yield b',"export_digest":'
            yield _canonical(export_hash.hexdigest())
            yield b"}"
        finally:
            session.close()

    return generate()


def _retry_at(attempt: int) -> datetime:
    seconds = min(15 * 60, 5 * (2 ** min(max(0, attempt - 1), 8)))
    return utc_now() + timedelta(seconds=seconds)


def _hold_item(
    item: AttendanceRepairItem,
    *,
    code: str,
    message: str,
    outcome: str = "REVIEW_REQUIRED",
) -> None:
    item.state = "NEEDS_REVIEW"
    item.outcome = outcome
    item.error_code = code
    item.error_message = message[:500]
    item.lease_owner = None
    item.lease_expires_at = None
    item.completed_at = utc_now()
    item.updated_at = utc_now()


def _release_retry(
    item: AttendanceRepairItem,
    *,
    code: str,
    message: str,
    status_code: int | None,
) -> None:
    item.error_code = code
    item.error_message = message[:500]
    item.last_http_status = status_code
    item.lease_owner = None
    item.lease_expires_at = None
    item.updated_at = utc_now()
    phase_attempt_count = (
        item.downstream_attempt_count
        if item.state == "DOWNSTREAM_VERIFY"
        else item.oracle_attempt_count
    )
    if phase_attempt_count >= settings.attendance_repair_retry_limit:
        _hold_item(
            item,
            code="RETRY_EXHAUSTED",
            message="Automatic Oracle repair retry limit was reached.",
        )
    else:
        item.next_attempt_at = _retry_at(phase_attempt_count)


def _round_robin(items: list[AttendanceRepairItem], limit: int) -> list[AttendanceRepairItem]:
    by_target: dict[int, list[AttendanceRepairItem]] = defaultdict(list)
    for item in items:
        by_target[item.target_id].append(item)
    selected: list[AttendanceRepairItem] = []
    target_ids = sorted(by_target)
    while target_ids and len(selected) < limit:
        remaining: list[int] = []
        for target_id in target_ids:
            queue = by_target[target_id]
            if queue and len(selected) < limit:
                selected.append(queue.pop(0))
            if queue:
                remaining.append(target_id)
        target_ids = remaining
    return selected


def _refresh_repair_totals(session: Session, job: AttendanceRepairJob) -> None:
    # This service deliberately disables SQLAlchemy autoflush. Aggregate
    # state must include transitions made earlier in the same transaction.
    session.flush()
    now = utc_now()
    targets = list(
        session.scalars(
            select(AttendanceRepairTarget).where(AttendanceRepairTarget.job_id == job.id)
        ).all()
    )
    state_counts: dict[int, dict[str, int]] = defaultdict(dict)
    for target_id, state, count in session.execute(
        select(
            AttendanceRepairItem.target_id,
            AttendanceRepairItem.state,
            func.count(AttendanceRepairItem.id),
        )
        .where(AttendanceRepairItem.job_id == job.id)
        .group_by(AttendanceRepairItem.target_id, AttendanceRepairItem.state)
    ):
        state_counts[int(target_id)][str(state)] = int(count)
    completed_targets = 0
    for target in targets:
        counts = state_counts.get(target.id, {})
        row_count = sum(counts.values())
        target.completed_event_count = counts.get("COMPLETE", 0)
        target.attention_event_count = counts.get("NEEDS_REVIEW", 0) + counts.get("CANCELLED", 0)
        terminal_count = sum(counts.get(state, 0) for state in ITEM_TERMINAL_STATES)
        if row_count and counts.get("COMPLETE", 0) == row_count:
            target.status = "COMPLETE"
            target.completed_at = target.completed_at or now
            completed_targets += 1
        elif row_count and terminal_count == row_count:
            target.status = "COMPLETE_WITH_ATTENTION"
            target.completed_at = target.completed_at or now
            completed_targets += 1
        elif any(counts.get(state, 0) for state in FORWARD_COMPLETION_STATES):
            target.status = "FORWARD_COMPLETING"
        elif row_count:
            target.status = "RUNNING"
    job.completed_target_count = completed_targets
    aggregate_states: dict[str, int] = defaultdict(int)
    for counts in state_counts.values():
        for state, count in counts.items():
            aggregate_states[state] += count
    total_items = sum(aggregate_states.values())
    job.completed_event_count = aggregate_states.get("COMPLETE", 0)
    job.attention_event_count = aggregate_states.get("NEEDS_REVIEW", 0) + aggregate_states.get(
        "CANCELLED", 0
    )
    review_item_count = aggregate_states.get("NEEDS_REVIEW", 0)
    if review_item_count:
        _upsert_repair_alert(
            session,
            job,
            error_code=job.error_code or "ITEM_REVIEW_REQUIRED",
        )
    terminal_items = sum(aggregate_states.get(state, 0) for state in ITEM_TERMINAL_STATES)
    if total_items and terminal_items == total_items:
        job.status = "COMPLETED_WITH_ATTENTION" if job.attention_event_count else "COMPLETED"
        job.phase = "CERTIFIED"
        job.completed_at = job.completed_at or now
        job.wait_reason = None
        session.flush()
        job.evidence_digest = _repair_certificate_digest(session, job)
        if not review_item_count:
            _resolve_repair_alert(session, job)
        _repair_event(
            session,
            job,
            job.status,
            details={
                "completed_events": job.completed_event_count,
                "attention_events": job.attention_event_count,
                "evidence_digest": job.evidence_digest,
            },
            idempotency_key=f"complete-{job.job_id}-{job.evidence_digest}",
        )
    elif job.status != "PAUSED":
        nonterminal = {
            state: count
            for state, count in aggregate_states.items()
            if state not in ITEM_TERMINAL_STATES and count
        }
        if nonterminal and set(nonterminal) == {"DOWNSTREAM_VERIFY"}:
            job.status = "WAITING_DOWNSTREAM"
            job.phase = "DOWNSTREAM_VERIFY"
        elif nonterminal and set(nonterminal) <= {"ORACLE_VERIFY", "ADD_ACTIVATE"}:
            job.status = "WAITING_ORACLE"
            job.phase = "ORACLE_VERIFY"
        elif nonterminal:
            job.status = "RUNNING"
            job.phase = "ORACLE_REPAIR"
    job.updated_at = now


def _persist_oracle_receipt(
    session: Session,
    *,
    item: AttendanceRepairItem,
    result: dict[str, Any],
) -> None:
    if result.get("operation_id") != item.operation_id or result.get("event_uid") != item.event_uid:
        raise OracleRepairError(
            "Oracle receipt did not match the frozen operation.",
            code="ORDS_RECEIPT_MISMATCH",
            retryable=False,
        )
    if result.get("identity_digest") != item.desired_identity_digest:
        raise OracleRepairError(
            "Oracle receipt identity digest did not match the approved identity.",
            code="ORDS_CONTENT_MISMATCH",
            retryable=False,
        )
    if not all(
        result.get(flag) is True
        for flag in (
            "raw_content_verified",
            "immutable_facts_unchanged",
            "event_count_preserved",
            "event_uid_unique",
        )
    ):
        raise OracleRepairError(
            "Oracle could not certify preserved physical punch facts.",
            code="ORDS_IMMUTABLE_ASSERTION_FAILED",
            retryable=False,
        )
    receipt_id = result.get("receipt_id")
    content_token = result.get("current_content_token")
    action = str(result.get("action") or "").upper()
    if (
        not isinstance(receipt_id, str)
        or not receipt_id
        or len(receipt_id) > 120
        or not isinstance(content_token, str)
        or len(content_token) != 64
        or any(character not in "0123456789abcdef" for character in content_token)
    ):
        raise OracleRepairError(
            "Oracle receipt omitted durable proof.",
            code="ORDS_MALFORMED_RESPONSE",
            retryable=False,
        )
    if action not in {"NOOP", "INSERTED", "UPDATED"}:
        raise OracleRepairError(
            "Oracle receipt returned an unknown action.",
            code="ORDS_MALFORMED_RESPONSE",
            retryable=False,
        )
    existing = session.scalar(
        select(OracleIdentityRepairReceipt).where(
            OracleIdentityRepairReceipt.repair_item_id == item.id
        )
    )
    if existing is not None:
        try:
            existing_content_token = decrypt_text(existing.current_content_token_encrypted)
        except Exception as exc:
            raise OracleRepairError(
                "Stored Oracle receipt proof cannot be decrypted.",
                code="ORDS_RECEIPT_CONFLICT",
                retryable=False,
            ) from exc
        if (
            existing.operation_id != item.operation_id
            or existing.payload_digest != item.operation_payload_digest
            or existing.action != action
            or existing.oracle_receipt_id != receipt_id
            or existing.verified_identity_digest != item.desired_identity_digest
            or existing_content_token != content_token
        ):
            raise OracleRepairError(
                "Replayed Oracle receipt conflicts with the original durable proof.",
                code="ORDS_RECEIPT_CONFLICT",
                retryable=False,
            )
        existing.observation_count += 1
        existing.updated_at = utc_now()
    else:
        session.add(
            OracleIdentityRepairReceipt(
                repair_item_id=item.id,
                operation_id=item.operation_id,
                payload_digest=item.operation_payload_digest,
                action=action,
                oracle_receipt_id=receipt_id,
                current_content_token_encrypted=encrypt_text(content_token),
                verified_identity_digest=item.desired_identity_digest,
                raw_content_verified_at=utc_now(),
                downstream_status="PENDING",
            )
        )
    item.state = "ADD_ACTIVATE"
    item.outcome = action
    item.error_code = None
    item.error_message = None
    item.lease_owner = None
    item.lease_expires_at = None
    item.next_attempt_at = None
    item.updated_at = utc_now()


def _claim_apply_batch() -> tuple[str, list[int], dict[str, Any], int, str] | None:
    from zk_add.db import session_scope

    now = utc_now()
    worker_id = f"repair-{uuid4()}"
    with session_scope() as session:
        live_backlog = int(
            session.scalar(
                select(func.count(OrdsOutbox.id)).where(OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES))
            )
            or 0
        )
        if live_backlog >= settings.reconciliation_history_backlog_pause:
            return None
        jobs = list(
            session.scalars(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.status.in_(["QUEUED", "RUNNING", "WAITING_ORACLE"]))
                .order_by(AttendanceRepairJob.id)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for job in jobs:
            if job.cancellation_requested:
                continue
            if job.first_oracle_mutation_at is None:
                try:
                    assert_preview_current(session, job)
                except RepairError as exc:
                    job.status = "NEEDS_ATTENTION"
                    job.error_code = exc.code
                    job.error_message = str(exc)[:500]
                    job.wait_reason = exc.code
                    for item in session.scalars(
                        select(AttendanceRepairItem).where(
                            AttendanceRepairItem.job_id == job.id,
                            AttendanceRepairItem.state == "ORACLE_APPLY",
                        )
                    ).all():
                        _hold_item(item, code=exc.code, message=str(exc))
                    _repair_event(
                        session,
                        job,
                        "NEEDS_ATTENTION",
                        details={"error_code": exc.code},
                    )
                    _upsert_repair_alert(session, job, error_code=exc.code)
                    continue
            eligible = (
                AttendanceRepairItem.job_id == job.id,
                AttendanceRepairItem.state == "ORACLE_APPLY",
                or_(
                    AttendanceRepairItem.next_attempt_at == None,  # noqa: E711
                    AttendanceRepairItem.next_attempt_at <= now,
                ),
                or_(
                    AttendanceRepairItem.lease_expires_at == None,  # noqa: E711
                    AttendanceRepairItem.lease_expires_at <= now,
                ),
            )
            ranked = (
                select(
                    AttendanceRepairItem.id.label("item_id"),
                    AttendanceRepairItem.target_id.label("target_id"),
                    func.row_number()
                    .over(
                        partition_by=AttendanceRepairItem.target_id,
                        order_by=AttendanceRepairItem.id,
                    )
                    .label("target_round"),
                )
                .where(*eligible)
                .subquery()
            )
            candidate_ids = list(
                session.scalars(
                    select(ranked.c.item_id)
                    .order_by(ranked.c.target_round, ranked.c.target_id)
                    .limit(settings.attendance_repair_oracle_batch_size)
                ).all()
            )
            if not candidate_ids:
                continue
            locked_candidates = list(
                session.scalars(
                    select(AttendanceRepairItem)
                    .where(AttendanceRepairItem.id.in_(candidate_ids), *eligible)
                    .with_for_update(skip_locked=True)
                ).all()
            )
            candidate_order = {item_id: index for index, item_id in enumerate(candidate_ids)}
            selected = sorted(
                locked_candidates,
                key=lambda item: candidate_order[item.id],
            )
            if not selected:
                continue
            if not session.scalar(select(func.count(AttendanceRepairOracleSlot.id))):
                # create_all databases do not run Alembic's seed INSERT.
                # Production migrations always precreate these two rows.
                session.add_all(
                    [
                        AttendanceRepairOracleSlot(id=1, updated_at=now),
                        AttendanceRepairOracleSlot(id=2, updated_at=now),
                    ]
                )
                session.flush()
            slot = session.scalar(
                select(AttendanceRepairOracleSlot)
                .where(
                    AttendanceRepairOracleSlot.id
                    <= settings.attendance_repair_oracle_concurrency,
                    or_(
                        AttendanceRepairOracleSlot.lease_expires_at == None,  # noqa: E711
                        AttendanceRepairOracleSlot.lease_expires_at <= now,
                    )
                )
                .order_by(AttendanceRepairOracleSlot.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if slot is None:
                return None
            slot.lease_owner = worker_id
            slot.lease_expires_at = now + timedelta(
                seconds=settings.attendance_repair_lease_seconds
            )
            slot.updated_at = now
            events = {
                row.id: row
                for row in session.scalars(
                    select(AttendanceEvent).where(
                        AttendanceEvent.id.in_([item.attendance_event_id for item in selected])
                    )
                ).all()
            }
            payload_items = []
            claimed_item_ids: list[int] = []
            connector = session.get(Connector, job.connector_id)
            for item in selected:
                event = events.get(item.attendance_event_id)
                if event is None or _immutable_digest(event) != item.immutable_facts_digest:
                    _hold_item(
                        item,
                        code="IMMUTABLE_DRIFT",
                        message="Physical punch facts changed before Oracle mutation.",
                    )
                    continue
                if connector is None:
                    _hold_item(
                        item,
                        code="CONNECTOR_DRIFT",
                        message="Connector disappeared before Oracle mutation.",
                    )
                    continue
                try:
                    payload = _oracle_item_payload(
                        event,
                        item,
                        connector=connector,
                        include_operation=True,
                    )
                except RepairError as exc:
                    _hold_item(item, code=exc.code, message=str(exc))
                    continue
                item.operation_payload_digest = payload["payload_digest"]
                item.state = "ORACLE_VERIFY"
                item.attempt_count += 1
                item.oracle_attempt_count += 1
                item.lease_owner = worker_id
                item.lease_expires_at = now + timedelta(
                    seconds=settings.attendance_repair_lease_seconds
                )
                item.updated_at = now
                payload_items.append(payload)
                claimed_item_ids.append(item.id)
            if not payload_items:
                slot.lease_owner = None
                slot.lease_expires_at = None
                slot.updated_at = utc_now()
                _refresh_repair_totals(session, job)
                continue
            job.status = "RUNNING"
            job.phase = "ORACLE_APPLY"
            job.started_at = job.started_at or now
            # Mark before sending. A lost response now always forward-recovers
            # by operation ID instead of assuming Oracle did not commit.
            job.first_oracle_mutation_at = job.first_oracle_mutation_at or now
            job.updated_at = now
            payload = {
                "contract_version": REPAIR_CONTRACT_VERSION,
                "connector_id": connector.connector_id if connector else None,
                "terminal_serial": connector.zkt_device.serial
                if connector and connector.zkt_device
                else None,
                "items": payload_items,
            }
            return (
                job.job_id,
                claimed_item_ids,
                payload,
                slot.id,
                worker_id,
            )
    return None


def _release_oracle_slot(slot_id: int, lease_owner: str) -> None:
    from zk_add.db import session_scope

    with session_scope() as session:
        slot = session.scalar(
            select(AttendanceRepairOracleSlot)
            .where(
                AttendanceRepairOracleSlot.id == slot_id,
                AttendanceRepairOracleSlot.lease_owner == lease_owner,
            )
            .with_for_update()
        )
        if slot is not None:
            slot.lease_owner = None
            slot.lease_expires_at = None
            slot.updated_at = utc_now()


async def _apply_claimed_batch(
    job_public_id: str,
    item_ids: list[int],
    payload: dict[str, Any],
    slot_id: int,
    slot_owner: str,
) -> None:
    from zk_add.db import session_scope

    try:
        async with _oracle_mutation_slots:
            response = await _ords_request("raw-captures/identity-repairs", payload=payload)
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            items = list(
                session.scalars(
                    select(AttendanceRepairItem)
                    .where(AttendanceRepairItem.id.in_(item_ids))
                    .with_for_update()
                ).all()
            )
            by_operation = _operation_results(
                response,
                expected_operation_ids={item.operation_id for item in items},
                context="Oracle correction",
            )
            for item in items:
                result = by_operation[item.operation_id]
                result_state = str(result.get("state") or "").upper()
                if result_state in {"PRECONDITION_FAILED", "REVIEW_REQUIRED"}:
                    error_code = str(result.get("error_code") or "ORACLE_PRECONDITION_FAILED")
                    _hold_item(
                        item,
                        code=error_code,
                        message="Oracle rejected the frozen content precondition.",
                    )
                    _repair_event(
                        session,
                        job,
                        "NEEDS_REVIEW",
                        details={
                            "operation_id": item.operation_id,
                            "error_code": error_code,
                        },
                        item_id=item.id,
                        idempotency_key=f"review-{item.operation_id}-{error_code}",
                    )
                    continue
                if result_state != "COMMITTED":
                    raise OracleRepairError(
                        "Oracle correction returned an unknown operation state.",
                        code="ORDS_MALFORMED_RESPONSE",
                        retryable=False,
                    )
                _persist_oracle_receipt(
                    session,
                    item=item,
                    result=result,
                )
                _repair_event(
                    session,
                    job,
                    "ORACLE_VERIFIED",
                    details={
                        "operation_id": item.operation_id,
                        "action": item.outcome,
                    },
                    item_id=item.id,
                    idempotency_key=f"oracle-{item.operation_id}",
                )
            _refresh_repair_totals(session, job)
    except OracleRepairError as exc:
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            items = list(
                session.scalars(
                    select(AttendanceRepairItem)
                    .where(AttendanceRepairItem.id.in_(item_ids))
                    .with_for_update()
                ).all()
            )
            outcome_unknown = exc.retryable or exc.code in ORACLE_UNKNOWN_RESPONSE_CODES
            for item in items:
                if outcome_unknown:
                    _release_retry(
                        item,
                        code=exc.code,
                        message=str(exc),
                        status_code=exc.status_code,
                    )
                else:
                    _hold_item(item, code=exc.code, message=str(exc))
            job.error_code = exc.code
            job.error_message = str(exc)[:500]
            if not outcome_unknown:
                job.status = "NEEDS_ATTENTION"
                job.wait_reason = exc.code
            _repair_event(
                session,
                job,
                "ORACLE_APPLY_UNCERTAIN" if outcome_unknown else "NEEDS_ATTENTION",
                details={"error_code": exc.code, "item_count": len(items)},
            )
            _refresh_repair_totals(session, job)
    finally:
        _release_oracle_slot(slot_id, slot_owner)


async def _recover_oracle_operations() -> None:
    from zk_add.db import session_scope

    now = utc_now()
    with session_scope() as session:
        first_item = session.scalar(
            select(AttendanceRepairItem)
            .join(AttendanceRepairJob, AttendanceRepairItem.job_id == AttendanceRepairJob.id)
            .where(
                AttendanceRepairItem.state == "ORACLE_VERIFY",
                AttendanceRepairJob.status.in_(
                    ["RUNNING", "WAITING_ORACLE", "PAUSED", "NEEDS_ATTENTION"]
                ),
                AttendanceRepairJob.first_oracle_mutation_at != None,  # noqa: E711
                or_(
                    AttendanceRepairItem.next_attempt_at == None,  # noqa: E711
                    AttendanceRepairItem.next_attempt_at <= now,
                ),
                or_(
                    AttendanceRepairItem.lease_expires_at == None,  # noqa: E711
                    AttendanceRepairItem.lease_expires_at <= now,
                ),
            )
            .order_by(AttendanceRepairItem.id)
            .with_for_update(skip_locked=True)
        )
        if first_item is None:
            return
        job = session.get(AttendanceRepairJob, first_item.job_id)
        assert job is not None
        items = list(
            session.scalars(
                select(AttendanceRepairItem)
                .where(
                    AttendanceRepairItem.job_id == job.id,
                    AttendanceRepairItem.state == "ORACLE_VERIFY",
                    or_(
                        AttendanceRepairItem.next_attempt_at == None,  # noqa: E711
                        AttendanceRepairItem.next_attempt_at <= now,
                    ),
                    or_(
                        AttendanceRepairItem.lease_expires_at == None,  # noqa: E711
                        AttendanceRepairItem.lease_expires_at <= now,
                    ),
                )
                .order_by(AttendanceRepairItem.id)
                .limit(settings.attendance_repair_oracle_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        if not items:
            return
        job_public_id = job.job_id
        operation_ids = [item.operation_id for item in items]
        item_ids = [item.id for item in items]
        for item in items:
            item.attempt_count += 1
            item.oracle_attempt_count += 1
            item.lease_owner = f"recover-{uuid4()}"
            item.lease_expires_at = now + timedelta(
                seconds=settings.attendance_repair_lease_seconds
            )
    try:
        response = await _ords_request(
            "raw-captures/identity-repairs/status",
            payload={
                "contract_version": REPAIR_CONTRACT_VERSION,
                "mode": "OPERATION_RECOVERY",
                "operation_ids": operation_ids,
            },
        )
        by_operation = _operation_results(
            response,
            expected_operation_ids=set(operation_ids),
            context="Oracle status",
        )
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            for item in session.scalars(
                select(AttendanceRepairItem)
                .where(AttendanceRepairItem.id.in_(item_ids))
                .with_for_update()
            ).all():
                result = by_operation.get(item.operation_id)
                if result is None:
                    _hold_item(
                        item,
                        code="ORDS_MEMBERSHIP_MISMATCH",
                        message="Oracle status omitted a frozen operation.",
                    )
                    continue
                state = str(result.get("state") or "").upper()
                if state == "NOT_FOUND":
                    if (
                        item.oracle_attempt_count
                        >= settings.attendance_repair_retry_limit
                    ):
                        _hold_item(
                            item,
                            code="RETRY_EXHAUSTED",
                            message="Oracle never observed this operation.",
                        )
                    elif job.cancellation_requested:
                        item.state = "CANCELLED"
                        item.outcome = "CANCELLED_UNTOUCHED"
                        item.lease_owner = None
                        item.lease_expires_at = None
                        item.next_attempt_at = None
                        item.completed_at = utc_now()
                    else:
                        item.state = "ORACLE_APPLY"
                        item.lease_owner = None
                        item.lease_expires_at = None
                        item.next_attempt_at = _retry_at(item.oracle_attempt_count)
                elif state == "COMMITTED":
                    _persist_oracle_receipt(session, item=item, result=result)
                    _repair_event(
                        session,
                        job,
                        "ORACLE_VERIFIED",
                        details={"operation_id": item.operation_id, "recovered": True},
                        item_id=item.id,
                        idempotency_key=f"oracle-{item.operation_id}",
                    )
                elif state in {"PRECONDITION_FAILED", "REVIEW_REQUIRED"}:
                    _hold_item(
                        item,
                        code=str(result.get("error_code") or "ORACLE_PRECONDITION_FAILED"),
                        message="Oracle rejected the frozen content precondition.",
                    )
                else:
                    _release_retry(
                        item,
                        code="ORACLE_OUTCOME_UNKNOWN",
                        message="Oracle operation outcome is still unknown.",
                        status_code=None,
                    )
            _refresh_repair_totals(session, job)
    except OracleRepairError as exc:
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            outcome_unknown = exc.retryable or exc.code in ORACLE_UNKNOWN_RESPONSE_CODES
            for item in session.scalars(
                select(AttendanceRepairItem)
                .where(AttendanceRepairItem.id.in_(item_ids))
                .with_for_update()
            ).all():
                if outcome_unknown:
                    _release_retry(
                        item,
                        code=exc.code,
                        message=str(exc),
                        status_code=exc.status_code,
                    )
                else:
                    _hold_item(item, code=exc.code, message=str(exc))
            if not outcome_unknown:
                job.status = "NEEDS_ATTENTION"
                job.wait_reason = exc.code
            job.error_code = exc.code
            job.error_message = str(exc)[:500]
            _refresh_repair_totals(session, job)


def _activate_verified_items() -> None:
    from zk_add.db import session_scope

    with session_scope() as session:
        items = list(
            session.scalars(
                select(AttendanceRepairItem)
                .where(AttendanceRepairItem.state == "ADD_ACTIVATE")
                .order_by(AttendanceRepairItem.id)
                .limit(settings.attendance_repair_oracle_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        jobs: dict[int, AttendanceRepairJob] = {}
        for item in items:
            job = jobs.setdefault(item.job_id, _lock_job(session, item.job_id))
            event = session.scalar(
                select(AttendanceEvent)
                .where(AttendanceEvent.id == item.attendance_event_id)
                .with_for_update()
            )
            target = session.get(AttendanceRepairTarget, item.target_id)
            receipt = session.scalar(
                select(OracleIdentityRepairReceipt).where(
                    OracleIdentityRepairReceipt.repair_item_id == item.id
                )
            )
            if event is None or target is None or receipt is None:
                _hold_item(
                    item,
                    code="LOCAL_ACTIVATION_EVIDENCE_MISSING",
                    message="Local activation evidence is incomplete.",
                )
                continue
            try:
                desired_display_name = decrypt_text(
                    target.desired_display_name_encrypted
                )
            except Exception:
                desired_display_name = None
            if not desired_display_name:
                _hold_item(
                    item,
                    code="FROZEN_PII_UNREADABLE",
                    message="The approved protected identity cannot be decrypted for local activation.",
                )
                job.status = "NEEDS_ATTENTION"
                job.wait_reason = item.error_code
                continue
            if _immutable_digest(event) != item.immutable_facts_digest:
                _hold_item(
                    item,
                    code="IMMUTABLE_DRIFT_AFTER_ORACLE_COMMIT",
                    message="Physical punch facts changed after Oracle commit; manual investigation is required.",
                )
                job.status = "NEEDS_ATTENTION"
                job.wait_reason = item.error_code
                continue
            previous_revisions = list(
                session.scalars(
                    select(AttendanceIdentityRevision)
                    .where(AttendanceIdentityRevision.attendance_event_id == event.id)
                    .with_for_update()
                ).all()
            )
            existing = next(
                (row for row in previous_revisions if row.repair_item_id == item.id), None
            )
            if existing is None:
                for revision in previous_revisions:
                    if revision.state == "ACTIVE":
                        revision.state = "SUPERSEDED"
                        revision.superseded_at = utc_now()
                revision = AttendanceIdentityRevision(
                    attendance_event_id=event.id,
                    repair_item_id=item.id,
                    sequence=max([row.sequence for row in previous_revisions] or [0]) + 1,
                    effective_device_user_id=target.device_user_id,
                    display_name_encrypted=target.desired_display_name_encrypted,
                    cnic_encrypted=target.desired_cnic_encrypted,
                    cnic_lookup_hash=target.desired_cnic_lookup_hash,
                    cnic_last4=target.desired_cnic_last4,
                    identity_digest=target.desired_identity_digest,
                    state="ACTIVE",
                    activated_at=utc_now(),
                )
                session.add(revision)
                session.flush()
            else:
                revision = existing
                revision.state = "ACTIVE"
                revision.activated_at = revision.activated_at or utc_now()
            # This is the complete local writable surface. Source UID/user ID,
            # event UID, timestamps, punch, raw flag, raw event and manifests
            # are intentionally absent.
            event.device_user_id = target.device_user_id
            event.display_name = desired_display_name
            event.cnic_encrypted = target.desired_cnic_encrypted
            event.cnic_lookup_hash = target.desired_cnic_lookup_hash
            event.cnic_last4 = target.desired_cnic_last4
            event.effective_identity_revision_id = revision.id
            event.identity_resolution_status = "RESOLVED_ATTENDANCE_REPAIR"
            event.identity_resolved_at = utc_now()
            event.identity_repaired_at = utc_now()
            event.identity_repair_reason = "AUDITED_ATTENDANCE_REPAIR"
            event.identity_content_status = "ORACLE_VERIFIED_DOWNSTREAM_PENDING"
            event.identity_content_confirmed_at = receipt.raw_content_verified_at
            item.state = "DOWNSTREAM_VERIFY"
            item.lease_owner = None
            item.lease_expires_at = None
            item.next_attempt_at = utc_now()
            item.updated_at = utc_now()
            _repair_event(
                session,
                job,
                "ADD_IDENTITY_ACTIVATED",
                details={
                    "operation_id": item.operation_id,
                    "identity_revision": revision.sequence,
                },
                item_id=item.id,
                idempotency_key=f"activate-{item.operation_id}",
            )
        for job in jobs.values():
            _refresh_repair_totals(session, job)


async def _verify_downstream() -> None:
    from zk_add.db import session_scope

    now = utc_now()
    with session_scope() as session:
        items = list(
            session.scalars(
                select(AttendanceRepairItem)
                .where(
                    AttendanceRepairItem.state == "DOWNSTREAM_VERIFY",
                    or_(
                        AttendanceRepairItem.next_attempt_at == None,  # noqa: E711
                        AttendanceRepairItem.next_attempt_at <= now,
                    ),
                    or_(
                        AttendanceRepairItem.lease_expires_at == None,  # noqa: E711
                        AttendanceRepairItem.lease_expires_at <= now,
                    ),
                )
                .order_by(AttendanceRepairItem.id)
                .limit(settings.attendance_repair_oracle_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        if not items:
            return
        job = session.get(AttendanceRepairJob, items[0].job_id)
        same_job = [item for item in items if item.job_id == job.id]
        item_ids = [item.id for item in same_job]
        operation_ids = [item.operation_id for item in same_job]
        job_public_id = job.job_id
        for item in same_job:
            item.attempt_count += 1
            item.downstream_attempt_count += 1
            item.lease_owner = f"downstream-{uuid4()}"
            item.lease_expires_at = now + timedelta(
                seconds=settings.attendance_repair_lease_seconds
            )
    try:
        response = await _ords_request(
            "raw-captures/identity-repairs/status",
            payload={
                "contract_version": REPAIR_CONTRACT_VERSION,
                "mode": "DOWNSTREAM_VERIFY",
                "operation_ids": operation_ids,
            },
        )
        by_operation = _operation_results(
            response,
            expected_operation_ids=set(operation_ids),
            context="Oracle downstream status",
        )
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            for item in session.scalars(
                select(AttendanceRepairItem)
                .where(AttendanceRepairItem.id.in_(item_ids))
                .with_for_update()
            ).all():
                result = by_operation.get(item.operation_id)
                event = session.get(AttendanceEvent, item.attendance_event_id)
                receipt = session.scalar(
                    select(OracleIdentityRepairReceipt).where(
                        OracleIdentityRepairReceipt.repair_item_id == item.id
                    )
                )
                if result is None or event is None or receipt is None:
                    _hold_item(
                        item,
                        code="DOWNSTREAM_EVIDENCE_MISSING",
                        message="Downstream verification omitted a frozen operation.",
                    )
                    continue
                if result.get("identity_digest") != item.desired_identity_digest:
                    _hold_item(
                        item,
                        code="DOWNSTREAM_IDENTITY_MISMATCH",
                        message="Downstream identity digest did not match the approved identity.",
                    )
                    continue
                if result.get("raw_content_verified") is not True:
                    _hold_item(
                        item,
                        code="ORACLE_CONTENT_REGRESSED",
                        message="Oracle raw content no longer matches the approved identity.",
                    )
                    continue
                if (
                    result.get("downstream_verified") is True
                    and result.get("stale_old_identity_absent") is True
                ):
                    item.state = "COMPLETE"
                    item.outcome = f"{item.outcome or 'REPAIRED'}_DOWNSTREAM_VERIFIED"
                    item.error_code = None
                    item.error_message = None
                    item.lease_owner = None
                    item.lease_expires_at = None
                    item.next_attempt_at = None
                    item.completed_at = utc_now()
                    item.updated_at = utc_now()
                    event.identity_content_status = "VERIFIED"
                    event.identity_downstream_confirmed_at = utc_now()
                    receipt.downstream_status = "VERIFIED"
                    receipt.downstream_verified_at = utc_now()
                    receipt.updated_at = utc_now()
                    _repair_event(
                        session,
                        job,
                        "COMPLETE",
                        details={"operation_id": item.operation_id},
                        item_id=item.id,
                        idempotency_key=f"complete-{item.operation_id}",
                    )
                else:
                    receipt.downstream_status = "PENDING"
                    receipt.observation_count += 1
                    receipt.updated_at = utc_now()
                    _release_retry(
                        item,
                        code="DOWNSTREAM_PENDING",
                        message="Oracle raw row is corrected; downstream projection is still converging.",
                        status_code=None,
                    )
            _refresh_repair_totals(session, job)
    except OracleRepairError as exc:
        with session_scope() as session:
            job = session.scalar(
                select(AttendanceRepairJob)
                .where(AttendanceRepairJob.job_id == job_public_id)
                .with_for_update()
            )
            if job is None:
                return
            for item in session.scalars(
                select(AttendanceRepairItem)
                .where(AttendanceRepairItem.id.in_(item_ids))
                .with_for_update()
            ).all():
                if exc.retryable:
                    _release_retry(
                        item,
                        code=exc.code,
                        message=str(exc),
                        status_code=exc.status_code,
                    )
                else:
                    _hold_item(item, code=exc.code, message=str(exc))
            if not exc.retryable:
                job.status = "NEEDS_ATTENTION"
                job.wait_reason = exc.code
            job.error_code = exc.code
            job.error_message = str(exc)[:500]
            _refresh_repair_totals(session, job)


async def _advance_source_preparation() -> None:
    from zk_add.db import session_scope

    classify_ids: list[str] = []
    now = utc_now()
    with session_scope() as session:
        jobs = list(
            session.scalars(
                select(AttendanceRepairJob)
                .where(
                    AttendanceRepairJob.status == "PREPARING_SOURCE",
                    or_(
                        AttendanceRepairJob.phase != "ORACLE_CLASSIFICATION",
                        AttendanceRepairJob.next_attempt_at == None,  # noqa: E711
                        AttendanceRepairJob.next_attempt_at <= now,
                    ),
                )
                .order_by(AttendanceRepairJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for job in jobs:
            if job.phase == "ORACLE_CLASSIFICATION":
                classify_ids.append(job.job_id)
                continue
            connector = session.get(Connector, job.connector_id)
            dependency = (
                session.get(ReconciliationJob, job.source_reconciliation_job_id)
                if job.source_reconciliation_job_id
                else None
            )
            if connector is None:
                job.status = "NEEDS_ATTENTION"
                job.error_code = "CONNECTOR_DRIFT"
                job.error_message = "Connector disappeared during source preparation."
                continue
            source_current, certificate, _coverage = _source_certificate(session, connector)
            if source_current:
                try:
                    # Source reconciliation can discover a very large or newly
                    # ambiguous cohort. Freeze is all-or-nothing: a rejected
                    # target must never leave a partial preview behind.
                    with session.begin_nested():
                        job.source_certificate_digest = certificate["certificate_digest"]
                        _freeze_membership(session, job)
                    classify_ids.append(job.job_id)
                except RepairError as exc:
                    job.status = "NEEDS_ATTENTION"
                    job.phase = "SOURCE_REVIEW"
                    job.error_code = exc.code
                    job.error_message = str(exc)[:500]
                    job.wait_reason = exc.code
                    _repair_event(
                        session,
                        job,
                        "NEEDS_ATTENTION",
                        details={"error_code": exc.code},
                    )
            elif dependency and dependency.status in {
                "FAILED",
                "CANCELLED",
                "INVALIDATED",
                "NEEDS_ATTENTION",
            }:
                job.status = "NEEDS_ATTENTION"
                job.error_code = "SOURCE_DEPENDENCY_FAILED"
                job.error_message = "Full-device source certification needs operator attention."
                job.wait_reason = job.error_code
                _upsert_repair_alert(session, job, error_code=job.error_code)
    for job_id in classify_ids:
        await classify_repair_preview(job_id)


async def advance_attendance_repairs_once() -> None:
    """Advance source preparation and durable repair checkpoints once."""

    # Receipt/status recovery, ADD activation, and downstream proof are never
    # disabled after an Oracle operation may have committed.  The execution
    # flag gates only admission of new Oracle mutations.
    await _recover_oracle_operations()
    _activate_verified_items()
    await _verify_downstream()
    await _advance_source_preparation()
    if not settings.attendance_repair_execution_enabled:
        return
    claims = []
    for _ in range(settings.attendance_repair_oracle_concurrency):
        claim = _claim_apply_batch()
        if claim is None:
            break
        claims.append(claim)
    if claims:
        await asyncio.gather(
            *(
                _apply_claimed_batch(job_id, item_ids, payload, slot_id, slot_owner)
                for job_id, item_ids, payload, slot_id, slot_owner in claims
            )
        )


def record_repair_worker_heartbeat(state: str, error_code: str | None = None) -> None:
    """Persist a PII-free worker heartbeat without relying on process memory."""

    from zk_add.db import session_scope

    now = utc_now()
    with session_scope() as session:
        row = session.scalar(
            select(AttendanceRepairWorkerHeartbeat).where(
                AttendanceRepairWorkerHeartbeat.worker_id == _repair_worker_id
            )
        )
        if row is None:
            row = AttendanceRepairWorkerHeartbeat(
                worker_id=_repair_worker_id,
                state=state,
                updated_at=now,
            )
            session.add(row)
        row.worker_id = _repair_worker_id
        row.state = state
        row.updated_at = now
        if state == "RUNNING":
            row.last_started_at = now
        elif state == "IDLE":
            row.last_completed_at = now
            row.last_error_code = None
        elif state == "ERROR":
            row.last_error_at = now
            row.last_error_code = error_code or "UNHANDLED_EXCEPTION"


def repair_worker_metrics(session: Session) -> dict[str, Any]:
    now = utc_now()
    active = int(
        session.scalar(
            select(func.count(AttendanceRepairJob.id)).where(
                AttendanceRepairJob.status.not_in(JOB_TERMINAL_STATES)
            )
        )
        or 0
    )
    review = int(
        session.scalar(
            select(func.count(AttendanceRepairItem.id)).where(
                AttendanceRepairItem.state == "NEEDS_REVIEW"
            )
        )
        or 0
    )
    stale_leases = int(
        session.scalar(
            select(func.count(AttendanceRepairItem.id)).where(
                AttendanceRepairItem.lease_expires_at < now,
                AttendanceRepairItem.state.not_in(ITEM_TERMINAL_STATES),
            )
        )
        or 0
    )
    oldest = session.scalar(
        select(func.min(AttendanceRepairJob.created_at)).where(
            AttendanceRepairJob.status.not_in(JOB_TERMINAL_STATES)
        )
    )
    retrying = int(
        session.scalar(
            select(func.count(AttendanceRepairItem.id)).where(
                AttendanceRepairItem.state.not_in(ITEM_TERMINAL_STATES),
                AttendanceRepairItem.attempt_count > 0,
                AttendanceRepairItem.next_attempt_at != None,  # noqa: E711
            )
        )
        or 0
    )
    unknown_outcomes = int(
        session.scalar(
            select(func.count(AttendanceRepairItem.id)).where(
                AttendanceRepairItem.state == "ORACLE_VERIFY"
            )
        )
        or 0
    )
    waiting_downstream = int(
        session.scalar(
            select(func.count(AttendanceRepairItem.id)).where(
                AttendanceRepairItem.state == "DOWNSTREAM_VERIFY"
            )
        )
        or 0
    )
    oldest_downstream = session.scalar(
        select(func.min(AttendanceRepairItem.updated_at)).where(
            AttendanceRepairItem.state == "DOWNSTREAM_VERIFY"
        )
    )
    leased_oracle_slots = int(
        session.scalar(
            select(func.count(AttendanceRepairOracleSlot.id)).where(
                AttendanceRepairOracleSlot.lease_expires_at > now
            )
        )
        or 0
    )
    heartbeat = session.scalar(
        select(AttendanceRepairWorkerHeartbeat)
        .order_by(AttendanceRepairWorkerHeartbeat.updated_at.desc())
        .limit(1)
    )
    active_worker_count = int(
        session.scalar(
            select(func.count(AttendanceRepairWorkerHeartbeat.id)).where(
                AttendanceRepairWorkerHeartbeat.updated_at
                >= now - timedelta(seconds=30)
            )
        )
        or 0
    )
    return {
        "active_jobs": active,
        "review_items": review,
        "stale_leases": stale_leases,
        "oldest_job_age_seconds": (
            max(0, int((now - ensure_utc(oldest)).total_seconds())) if oldest else 0
        ),
        "retrying_items": retrying,
        "unknown_outcome_items": unknown_outcomes,
        "waiting_downstream_items": waiting_downstream,
        "oldest_downstream_lag_seconds": (
            max(0, int((now - ensure_utc(oldest_downstream)).total_seconds()))
            if oldest_downstream
            else 0
        ),
        "leased_oracle_slots": leased_oracle_slots,
        "active_worker_count": active_worker_count,
        "heartbeat": (
            {
                "state": heartbeat.state,
                "updated_at": heartbeat.updated_at,
                "last_started_at": heartbeat.last_started_at,
                "last_completed_at": heartbeat.last_completed_at,
                "last_error_at": heartbeat.last_error_at,
                "last_error_code": heartbeat.last_error_code,
            }
            if heartbeat
            else None
        ),
    }
