from __future__ import annotations

import base64
import binascii
from datetime import timedelta
import hashlib
import hmac
import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import encrypt_text
from zk_add.models import (
    AttendanceEvent,
    Connector,
    DeviceCommand,
    OrdsOutbox,
    ReconciliationChunk,
    ReconciliationCoverage,
    ReconciliationEvent,
    ReconciliationJob,
    TemporaryAdminLease,
    TerminalRecordManifest,
    ZKTDevice,
)
from zk_add.schemas import (
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now


TERMINAL_JOB_STATES = {"COMPLETED", "CANCELLED", "FAILED", "INVALIDATED"}
PAUSED_JOB_STATES = {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}
ACTIVE_COMMAND_STATES = {
    "QUEUED",
    "WAITING_FOR_DEVICE",
    "WAITING_FOR_ZKT",
    "RETRYING",
    "DISPATCHED",
    "ACKNOWLEDGED",
    "RUNNING",
    "CANCEL_REQUESTED",
}
ACTIVE_LEASE_STATES = {"REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"}
ORDS_CONFIRMED_STATES = {"ACKED", "ACKED_CHECK"}
RECONCILIATION_CAPABILITY = "history_stream_v1"
RANGE_RESUME_CAPABILITY = "history_range_resume_verified"


def _version_tuple(value: str | None) -> tuple[int, int, int]:
    match = re.fullmatch(
        r"(?:zone-lite-)?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?",
        value or "",
    )
    return tuple(int(item) for item in match.groups()) if match else (0, 0, 0)


def _request_digest(*, connector: Connector, reason: str, confirmation: str) -> str:
    material = json.dumps(
        {
            "connector_id": connector.connector_id,
            "confirmation": confirmation,
            "mode": "FULL_HISTORY_BASELINE",
            "reason": reason.strip(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def preflight_reconciliation(session: Session, connector: Connector) -> dict:
    hard: list[dict[str, str]] = []
    waitable: list[dict[str, str]] = []
    zkt = connector.zkt_device
    if not settings.reconciliation_enabled:
        hard.append(
            {
                "code": "FEATURE_DISABLED",
                "message": "ADD-owned reconciliation has not been enabled for production.",
            }
        )
    if not connector.active:
        hard.append({"code": "CONNECTOR_INACTIVE", "message": "Connector is inactive."})
    if connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL":
        hard.append(
            {
                "code": "DUPLICATE_SERIAL_QUARANTINE",
                "message": "Duplicate terminal serial quarantine must be resolved first.",
            }
        )
    if zkt is None:
        hard.append({"code": "NO_ZKT", "message": "No ZKT terminal is assigned."})
    else:
        capabilities = zkt.capability_profile or {}
        if _version_tuple(connector.firmware_version) < (2, 3, 0):
            hard.append(
                {
                    "code": "FIRMWARE_UNSUPPORTED",
                    "message": "Zone Lite 2.3.0 or newer is required.",
                }
            )
        if not bool(capabilities.get(RECONCILIATION_CAPABILITY)):
            hard.append(
                {
                    "code": "HISTORY_STREAM_UNCERTIFIED",
                    "message": "The connector has not attested bounded history streaming.",
                }
            )
        if not bool(capabilities.get(RANGE_RESUME_CAPABILITY)):
            hard.append(
                {
                    "code": "RANGE_RESUME_UNCERTIFIED",
                    "message": "This terminal model has not passed nonzero-offset resume certification.",
                }
            )
        if not zkt.serial:
            hard.append({"code": "SERIAL_UNKNOWN", "message": "Terminal serial is unknown."})
        if not zkt.identity_snapshot_stable or not zkt.snapshot_complete:
            waitable.append(
                {
                    "code": "IDENTITY_SNAPSHOT_UNSTABLE",
                    "message": "Waiting for a complete stable terminal user snapshot.",
                }
            )
        if not zkt.online or zkt.connection_state not in {"ONLINE", "STABLE"}:
            waitable.append(
                {"code": "WAITING_FOR_ZKT", "message": "The ZKT terminal is not stable online."}
            )
    if not connector.connected:
        waitable.append(
            {"code": "WAITING_FOR_DEVICE", "message": "The ESP connector is offline."}
        )
    active_command = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
        )
    )
    if active_command:
        waitable.append(
            {
                "code": "COMMAND_ACTIVE",
                "message": f"Waiting for command {active_command.command_id} to finish.",
            }
        )
    if zkt is not None:
        active_lease = session.scalar(
            select(TemporaryAdminLease).where(
                TemporaryAdminLease.zkt_device_id == zkt.id,
                TemporaryAdminLease.state.in_(ACTIVE_LEASE_STATES),
            )
        )
        if active_lease:
            waitable.append(
                {
                    "code": "LEASE_ACTIVE",
                    "message": "Waiting for the temporary administrator lease to be safely revoked.",
                }
            )
    return {
        "eligible": not hard,
        "ready_now": not hard and not waitable,
        "hard_blockers": hard,
        "waitable_blockers": waitable,
        "connector": {
            "connector_id": connector.connector_id,
            "device_id": connector.device_id,
            "display_name": connector.display_name,
            "zone_id": connector.zone_id,
            "connected": connector.connected,
            "firmware_version": connector.firmware_version,
        },
        "terminal": None
        if zkt is None
        else {
            "serial": zkt.serial,
            "model": zkt.model,
            "attendance_count": zkt.attendance_count,
            "user_count": zkt.user_count,
            "connection_state": zkt.connection_state,
            "identity_snapshot_revision": zkt.identity_snapshot_revision,
            "range_resume_verified": bool(
                (zkt.capability_profile or {}).get(RANGE_RESUME_CAPABILITY)
            ),
        },
        "coverage": serialize_coverage(active_coverage(session, zkt)) if zkt else None,
    }


def create_reconciliation_job(
    session: Session,
    *,
    connector: Connector,
    actor: str,
    reason: str,
    confirmation: str,
    idempotency_key: str,
) -> ReconciliationJob:
    locked_connector = session.scalar(
        select(Connector).where(Connector.id == connector.id).with_for_update()
    )
    if locked_connector is None:
        raise ValueError("Connector no longer exists.")
    connector = locked_connector
    digest = _request_digest(
        connector=connector, reason=reason, confirmation=confirmation
    )
    existing = session.scalar(
        select(ReconciliationJob).where(
            ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise ValueError("That idempotency key was used for a different request.")
        return existing
    expected = f"RECONCILE {connector.device_id} FROM START"
    if confirmation != expected:
        raise ValueError(f"Type {expected} exactly to confirm this operation.")
    preflight = preflight_reconciliation(session, connector)
    if preflight["hard_blockers"]:
        raise ValueError(preflight["hard_blockers"][0]["message"])
    active = session.scalar(
        select(ReconciliationJob).where(
            ReconciliationJob.connector_id == connector.id,
            ReconciliationJob.status.not_in(TERMINAL_JOB_STATES),
        )
    )
    if active:
        raise ValueError(f"Device already has active reconciliation {active.job_id}.")
    zkt = connector.zkt_device
    assert zkt is not None
    waitable = preflight["waitable_blockers"]
    job = ReconciliationJob(
        connector_id=connector.id,
        zkt_device_id=zkt.id,
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        request_digest=digest,
        status="QUEUED",
        phase=("WAITING_FOR_SAFE_WINDOW" if waitable else "PREFLIGHT"),
        wait_reason=(waitable[0]["code"] if waitable else None),
        terminal_serial=zkt.serial,
        terminal_generation=max(1, connector.onboarding_generation),
        firmware_version=connector.firmware_version,
        identity_snapshot_id=zkt.identity_snapshot_id,
        latest_terminal_count=zkt.attendance_count,
    )
    session.add(job)
    session.flush()
    _event(session, job, "QUEUED", {"wait_reason": job.wait_reason})
    append_audit(
        session,
        actor=actor,
        action="FULL_HISTORY_RECONCILIATION_QUEUED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="QUEUED",
        after={"job_id": job.job_id, "reason": reason.strip()},
    )
    return job


def control_reconciliation_job(
    session: Session,
    *,
    job: ReconciliationJob,
    action: str,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> ReconciliationJob:
    locked = session.scalar(
        select(ReconciliationJob)
        .where(ReconciliationJob.id == job.id)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("Reconciliation job no longer exists.")
    job = locked
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise ValueError("Unknown reconciliation control action.")
    replay = session.scalar(
        select(ReconciliationEvent).where(
            ReconciliationEvent.job_id == job.id,
            ReconciliationEvent.idempotency_key == idempotency_key,
        )
    )
    if replay:
        return job
    if job.status in TERMINAL_JOB_STATES:
        if action == "retry" and job.status == "FAILED":
            raise ValueError("Failed jobs retain evidence; create a new audited job instead.")
        raise ValueError(f"Reconciliation is already {job.status.lower()}.")
    before = job.status
    if action == "pause":
        # A device source step advances only after ADD's atomic transaction is
        # acknowledged. Holding the job row lock therefore makes pause/cancel
        # immediate without a device-side durable job state.
        job.status = "PAUSED"
        job.wait_reason = reason.strip()
    elif action == "resume":
        job.status = "QUEUED"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = None
        job.error_code = None
        job.error_message = None
    elif action == "cancel":
        job.status = "CANCELLED"
        job.wait_reason = reason.strip()
        job.completed_at = utc_now()
    else:
        if job.status != "NEEDS_ATTENTION":
            raise ValueError("Only a safety-held job can retry from its checkpoint.")
        job.status = "QUEUED"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = None
        job.error_code = None
        job.error_message = None
        job.retry_count += 1
    job.updated_at = utc_now()
    _event(
        session,
        job,
        job.status,
        {"action": action, "reason": reason.strip()},
        idempotency_key=idempotency_key,
    )
    append_audit(
        session,
        actor=actor,
        action=f"FULL_HISTORY_RECONCILIATION_{action.upper()}",
        target_type="reconciliation_job",
        target_id=job.job_id,
        outcome=job.status,
        before={"status": before},
        after={"status": job.status, "reason": reason.strip()},
    )
    return job


def apply_reconciliation_anchor(
    session: Session,
    *,
    connector: Connector,
    payload: ReconciliationAnchorRequest,
) -> ReconciliationJob:
    job = _device_job(session, connector, payload.job_id)
    _require_runnable(job, payload.generation)
    if job.terminal_serial and job.terminal_serial != payload.terminal_serial:
        return _safety_hold(
            session, job, "TERMINAL_SERIAL_CHANGED", "Terminal serial changed while anchoring."
        )
    if payload.latest_terminal_count < payload.cutoff_count:
        return _safety_hold(
            session, job, "TERMINAL_COUNT_REGRESSION", "Terminal count is below the source cutoff."
        )
    expected_bytes = 4 + payload.cutoff_count * payload.record_size
    if payload.source_total_bytes < expected_bytes:
        return _safety_hold(
            session,
            job,
            "SOURCE_SIZE_MISMATCH",
            "Prepared source buffer is smaller than the advertised record count.",
        )
    if job.cutoff_count is not None and (
        job.cutoff_count != payload.cutoff_count
        or job.record_size != payload.record_size
        or job.first_anchor_digest != payload.first_anchor_digest
    ):
        return _safety_hold(
            session,
            job,
            "ANCHOR_DIVERGED",
            "A resumed prepared source no longer matches the committed anchor.",
        )
    now = utc_now()
    job.terminal_serial = payload.terminal_serial
    job.terminal_generation = payload.terminal_generation
    job.cutoff_count = payload.cutoff_count
    job.latest_terminal_count = payload.latest_terminal_count
    job.record_size = payload.record_size
    job.source_total_bytes = payload.source_total_bytes
    job.first_anchor_digest = payload.first_anchor_digest
    job.status = "RUNNING"
    job.phase = "SCANNING_TERMINAL"
    job.wait_reason = None
    job.started_at = job.started_at or now
    job.last_progress_at = now
    job.updated_at = now
    job.next_retry_at = None
    _event(
        session,
        job,
        "ANCHORED",
        {
            "cutoff_count": payload.cutoff_count,
            "record_size": payload.record_size,
            "source_total_bytes": payload.source_total_bytes,
        },
    )
    return job


def apply_reconciliation_chunk(
    session: Session,
    *,
    connector: Connector,
    payload: ReconciliationChunkRequest,
) -> tuple[ReconciliationJob, ReconciliationChunk, bool]:
    job = _device_job(session, connector, payload.job_id)
    _require_runnable(job, payload.generation)
    if job.cutoff_count is None or job.record_size is None:
        raise ValueError("Reconciliation must be anchored before accepting chunks.")
    existing = session.scalar(
        select(ReconciliationChunk).where(
            ReconciliationChunk.job_id == job.id,
            ReconciliationChunk.generation == payload.generation,
            ReconciliationChunk.start_ordinal == payload.start_ordinal,
        )
    )
    if existing:
        if (
            existing.chunk_digest != payload.chunk_digest
            or existing.end_ordinal != payload.end_ordinal
            or existing.resulting_chain_digest != payload.resulting_chain_digest
        ):
            _safety_hold(
                session,
                job,
                "COMMITTED_RANGE_DIVERGED",
                "A replayed source range had different immutable evidence.",
            )
            # Return the already-committed row so the transport can send an
            # explicit negative acknowledgement after this safety hold is
            # committed. Raising here would roll back the durable hold.
            return job, existing, False
        return job, existing, True
    if payload.start_ordinal != job.committed_next_ordinal:
        raise ValueError(
            f"Expected source ordinal {job.committed_next_ordinal}, got {payload.start_ordinal}."
        )
    if payload.end_ordinal > job.cutoff_count:
        raise ValueError("Chunk exceeds the anchored source cutoff.")
    for source in payload.records:
        try:
            raw_record = base64.b64decode(source.raw_record_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Source record contains invalid base64 evidence.") from exc
        if len(raw_record) != job.record_size:
            raise ValueError("Source record evidence does not match the anchored record size.")
        if hashlib.sha256(raw_record).hexdigest() != source.raw_record_digest:
            raise ValueError("Source record digest does not match its protected raw evidence.")
    if (
        payload.start_ordinal == 0
        and payload.records[0].raw_record_digest != job.first_anchor_digest
    ):
        raise ValueError("The first committed source row does not match the immutable anchor.")
    canonical_digest = reconciliation_chunk_digest(payload)
    if canonical_digest != payload.chunk_digest:
        raise ValueError("Chunk digest does not match its canonical source records.")
    expected_previous = job.last_chain_digest
    if payload.previous_chain_digest != expected_previous:
        raise ValueError("Chunk chain does not continue from the committed checkpoint.")
    expected_result = reconciliation_chain_digest(
        expected_previous,
        start_ordinal=payload.start_ordinal,
        end_ordinal=payload.end_ordinal,
        chunk_digest=payload.chunk_digest,
    )
    if payload.resulting_chain_digest != expected_result:
        raise ValueError("Resulting chunk chain digest is invalid.")

    from zk_add.service import ingest_attendance

    attendance = [row.event for row in payload.records if row.event is not None]
    accepted_uids: set[str] = set()
    duplicate_uids: set[str] = set()
    if attendance:
        accepted, duplicates = ingest_attendance(
            session, connector=connector, events=attendance
        )
        accepted_uids = set(accepted)
        duplicate_uids = set(duplicates)
    session.flush()
    event_uids = [row.event.event_uid for row in payload.records if row.event]
    events_by_uid = {
        row.event_uid: row
        for row in session.scalars(
            select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(event_uids))
        ).all()
    } if event_uids else {}
    chunk = ReconciliationChunk(
        job_id=job.id,
        generation=payload.generation,
        sequence=payload.sequence,
        start_ordinal=payload.start_ordinal,
        end_ordinal=payload.end_ordinal,
        record_count=len(payload.records),
        chunk_digest=payload.chunk_digest,
        previous_chain_digest=payload.previous_chain_digest,
        resulting_chain_digest=payload.resulting_chain_digest,
    )
    session.add(chunk)
    session.flush()
    blocked = 0
    quarantined = 0
    terminal_duplicates = 0
    for source in payload.records:
        event = events_by_uid.get(source.event.event_uid) if source.event else None
        disposition = source.disposition
        if event and event.ords_status == "BLOCKED_IDENTITY":
            disposition = "BLOCKED_IDENTITY"
        if disposition == "BLOCKED_IDENTITY":
            blocked += 1
        if disposition in {"INVALID_TIME", "MALFORMED"}:
            quarantined += 1
        if disposition == "TERMINAL_DUPLICATE":
            terminal_duplicates += 1
        session.add(
            TerminalRecordManifest(
                job_id=job.id,
                chunk_id=chunk.id,
                generation=payload.generation,
                ordinal=source.ordinal,
                raw_record_digest=source.raw_record_digest,
                terminal_record_key=source.terminal_record_key,
                occurrence_index=source.occurrence_index,
                attendance_event_id=event.id if event else None,
                disposition=disposition,
                protected_raw_record=encrypt_text(source.raw_record_b64),
                error_code=source.error_code,
            )
        )
    chunk.accepted_count = len(accepted_uids)
    chunk.already_present_count = len(duplicate_uids)
    chunk.blocked_identity_count = blocked
    chunk.quarantined_count = quarantined
    now = utc_now()
    job.committed_next_ordinal = payload.end_ordinal
    job.scanned_count = payload.end_ordinal
    job.add_durable_count += len(payload.records)
    job.already_present_count += len(duplicate_uids)
    job.terminal_duplicate_count += terminal_duplicates
    job.blocked_identity_count += blocked
    job.quarantined_count += quarantined
    job.last_chain_digest = payload.resulting_chain_digest
    job.last_progress_at = now
    job.updated_at = now
    job.next_retry_at = None
    job.status = "RUNNING"
    job.phase = "SCANNING_TERMINAL"
    _event(
        session,
        job,
        "CHUNK_COMMITTED",
        {
            "start_ordinal": payload.start_ordinal,
            "end_ordinal": payload.end_ordinal,
            "accepted": chunk.accepted_count,
            "already_present": chunk.already_present_count,
            "blocked_identity": blocked,
            "quarantined": quarantined,
        },
    )
    return job, chunk, False


def apply_reconciliation_manifest(
    session: Session,
    *,
    connector: Connector,
    payload: ReconciliationManifestRequest,
) -> ReconciliationJob:
    job = _device_job(session, connector, payload.job_id)
    _require_runnable(job, payload.generation)
    if (
        job.terminal_serial != payload.terminal_serial
        or job.terminal_generation != payload.terminal_generation
    ):
        return _safety_hold(
            session, job, "TERMINAL_GENERATION_CHANGED", "Terminal generation changed before sealing."
        )
    if payload.latest_terminal_count < payload.cutoff_count:
        return _safety_hold(
            session, job, "TERMINAL_COUNT_REGRESSION", "Terminal count regressed before sealing."
        )
    if (
        job.cutoff_count != payload.cutoff_count
        or job.committed_next_ordinal != payload.cutoff_count
        or (job.last_chain_digest or ("0" * 64)) != payload.final_chain_digest
    ):
        return _safety_hold(
            session,
            job,
            "SOURCE_MANIFEST_INCOMPLETE",
            "Final source manifest does not match the contiguous ADD checkpoint.",
        )
    manifest_count = session.scalar(
        select(func.count(TerminalRecordManifest.id)).where(
            TerminalRecordManifest.job_id == job.id,
            TerminalRecordManifest.generation == payload.generation,
        )
    ) or 0
    if manifest_count != payload.cutoff_count:
        return _safety_hold(
            session,
            job,
            "SOURCE_MANIFEST_GAP",
            "ADD does not hold one source-manifest row for every terminal ordinal.",
        )
    now = utc_now()
    evidence = _sealed_evidence({
        "job_id": job.job_id,
        "terminal_serial": job.terminal_serial,
        "terminal_generation": job.terminal_generation,
        "certified_source_cursor": payload.cutoff_count,
        "source_chain_digest": payload.final_chain_digest,
        "blocked_identity": job.blocked_identity_count,
        "quarantined": job.quarantined_count,
        "terminal_duplicates": job.terminal_duplicate_count,
        "firmware_version": job.firmware_version,
        "certified_at": now.isoformat(),
    })
    capture_state = (
        "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"
        if job.blocked_identity_count or job.quarantined_count
        else "SOURCE_CAPTURE_CERTIFIED"
    )
    for prior in session.scalars(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.zkt_device_id == job.zkt_device_id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
    ).all():
        prior.active = False
        prior.invalidated_reason = "SUPERSEDED_BY_NEW_SOURCE_CERTIFICATE"
        prior.invalidated_at = now
        prior.updated_at = now
    coverage = ReconciliationCoverage(
        zkt_device_id=job.zkt_device_id,
        job_id=job.id,
        terminal_serial=job.terminal_serial or "unknown",
        terminal_generation=job.terminal_generation,
        certified_source_cursor=payload.cutoff_count,
        source_chain_digest=payload.final_chain_digest,
        capture_state=capture_state,
        oracle_state="ORACLE_MEMBERSHIP_PENDING",
        capture_evidence=evidence,
    )
    session.add(coverage)
    job.latest_terminal_count = payload.latest_terminal_count
    job.capture_certificate = evidence
    job.capture_certified_at = now
    job.phase = (
        "WAITING_FOR_IDENTITY"
        if job.blocked_identity_count or job.quarantined_count
        else "DRAINING_ORDS"
    )
    job.status = "NEEDS_ATTENTION" if job.quarantined_count else "RUNNING"
    job.last_progress_at = now
    job.updated_at = now
    job.next_retry_at = None
    _event(session, job, capture_state, evidence)
    return refresh_reconciliation_assurance(session, job)


def refresh_reconciliation_assurance(
    session: Session, job: ReconciliationJob
) -> ReconciliationJob:
    manifest_events = select(TerminalRecordManifest.attendance_event_id).where(
        TerminalRecordManifest.job_id == job.id,
        TerminalRecordManifest.attendance_event_id.is_not(None),
    ).distinct()
    events = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(manifest_events))
    ).all()
    target = len(events)
    confirmed = sum(row.ords_status in ORDS_CONFIRMED_STATES for row in events)
    blocked = sum(row.ords_status == "BLOCKED_IDENTITY" for row in events)
    pending = target - confirmed - blocked
    job.ords_target_count = target
    job.ords_confirmed_count = confirmed
    job.ords_pending_count = max(0, pending)
    job.blocked_identity_count = blocked
    coverage = session.scalar(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.job_id == job.id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
    )
    if job.capture_certified_at is None:
        return job
    if blocked:
        job.phase = "WAITING_FOR_IDENTITY"
        if not job.quarantined_count:
            job.status = "RUNNING"
    elif job.quarantined_count:
        job.phase = "FINAL_ASSURANCE"
        job.status = "NEEDS_ATTENTION"
        job.wait_reason = "SOURCE_QUARANTINE_REQUIRES_REVIEW"
    elif confirmed < target:
        job.phase = "DRAINING_ORDS"
        job.status = "RUNNING"
    else:
        now = utc_now()
        evidence = _sealed_evidence({
            "job_id": job.job_id,
            "terminal_serial": job.terminal_serial,
            "certified_source_cursor": job.committed_next_ordinal,
            "oracle_membership_confirmed": confirmed,
            "source_chain_digest": job.last_chain_digest,
            "certified_at": now.isoformat(),
            "policy": "APPEND_ONLY_MEMBERSHIP",
        })
        job.oracle_certificate = evidence
        job.oracle_certified_at = now
        job.completed_at = now
        job.phase = "FINAL_ASSURANCE"
        job.status = "COMPLETED"
        job.wait_reason = None
        if coverage:
            coverage.oracle_state = "ORACLE_MEMBERSHIP_CERTIFIED"
            coverage.oracle_evidence = evidence
            coverage.oracle_certified_at = now
            coverage.updated_at = now
        _event(session, job, "ORACLE_MEMBERSHIP_CERTIFIED", evidence)
    job.updated_at = utc_now()
    return job


def refresh_all_reconciliation_assurance(session: Session) -> int:
    rows = session.scalars(
        select(ReconciliationJob).where(
            ReconciliationJob.capture_certified_at.is_not(None),
            ReconciliationJob.status.not_in(TERMINAL_JOB_STATES),
        )
    ).all()
    for row in rows:
        refresh_reconciliation_assurance(session, row)
    return len(rows)


def assignment_rows(session: Session) -> list[tuple[str, dict]]:
    if not settings.reconciliation_enabled:
        return []
    rows = session.scalars(
        select(ReconciliationJob)
        .where(
            ReconciliationJob.status.in_(["QUEUED", "RUNNING"]),
            ReconciliationJob.capture_certified_at.is_(None),
        )
        .order_by(ReconciliationJob.requested_at.asc())
    ).all()
    assignments: list[tuple[str, dict]] = []
    history_backlog = session.scalar(
        select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.delivery_type == "FULL_HISTORY",
            OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE", "IN_FLIGHT"]),
        )
    ) or 0
    # Each ready job owns one isolated device slot through its short assignment
    # cooldown. A disconnected or safety-blocked device releases its slot so it
    # cannot stall another zone; the durable checkpoint lets it resume later.
    # The global Oracle backlog gate still stops all new source intake before
    # ADD or ORDS can be overloaded.
    slot_limit = settings.reconciliation_device_concurrency
    slots_owned = 0
    now = utc_now()
    for job in rows:
        connector = session.get(Connector, job.connector_id)
        if connector is None or not connector.connected:
            job.phase = "WAITING_FOR_DEVICE"
            job.wait_reason = "WAITING_FOR_DEVICE"
            continue
        backlog_limit = (
            settings.reconciliation_history_backlog_resume
            if job.wait_reason == "HISTORY_BACKLOG_BACKPRESSURE"
            else settings.reconciliation_history_backlog_pause
        )
        if history_backlog >= backlog_limit:
            job.phase = "WAITING_FOR_SAFE_WINDOW"
            job.wait_reason = "HISTORY_BACKLOG_BACKPRESSURE"
            continue
        preflight = preflight_reconciliation(session, connector)
        if preflight["hard_blockers"]:
            _safety_hold(
                session,
                job,
                preflight["hard_blockers"][0]["code"],
                preflight["hard_blockers"][0]["message"],
            )
            continue
        if preflight["waitable_blockers"]:
            job.phase = "WAITING_FOR_SAFE_WINDOW"
            job.wait_reason = preflight["waitable_blockers"][0]["code"]
            continue
        if slots_owned >= slot_limit:
            job.phase = "WAITING_FOR_CAPACITY"
            job.wait_reason = "WAITING_FOR_SCAN_SLOT"
            continue
        slots_owned += 1
        job.wait_reason = None
        job.phase = "ANCHORING" if job.cutoff_count is None else "SCANNING_TERMINAL"
        if job.next_retry_at is not None and job.next_retry_at > now:
            continue
        zkt = connector.zkt_device
        try:
            device_chunk_limit = int(
                ((zkt.capability_profile if zkt else {}) or {}).get(
                    "history_chunk_max_records", 1
                )
            )
        except (TypeError, ValueError):
            device_chunk_limit = 1
        predecessor = None
        if job.committed_next_ordinal > 0:
            predecessor = session.scalar(
                select(TerminalRecordManifest).where(
                    TerminalRecordManifest.job_id == job.id,
                    TerminalRecordManifest.generation == job.terminal_generation,
                    TerminalRecordManifest.ordinal == job.committed_next_ordinal - 1,
                )
            )
        payload = {
            "schema_version": "1",
            "type": "reconcile_assignment",
            "job_id": job.job_id,
            "generation": job.terminal_generation,
            "mode": job.mode,
            "expected_terminal_serial": job.terminal_serial,
            "committed_next_ordinal": job.committed_next_ordinal,
            "cutoff_count": job.cutoff_count,
            "first_anchor_digest": job.first_anchor_digest,
            "preceding_chain_digest": job.last_chain_digest,
            "committed_predecessor_digest": (
                predecessor.raw_record_digest if predecessor is not None else None
            ),
            "chunk_records": min(
                100,
                max(1, settings.reconciliation_chunk_records),
                max(1, device_chunk_limit),
            ),
            "source_policy": "APPEND_ONLY_NO_DELETE",
        }
        assignments.append((connector.connector_id, payload))
        job.next_retry_at = now + timedelta(
            seconds=max(2, settings.reconciliation_assignment_seconds)
        )
    return assignments


def reconciliation_scheduler_state(session: Session) -> dict:
    rows = session.scalars(
        select(ReconciliationJob).where(
            ReconciliationJob.status.in_(["QUEUED", "RUNNING"]),
            ReconciliationJob.capture_certified_at.is_(None),
        )
    ).all()
    active = sum(
        row.phase in {"ANCHORING", "SCANNING_TERMINAL"}
        and row.wait_reason is None
        for row in rows
    )
    return {
        "policy": "BOUNDED_PARALLEL_PER_DEVICE",
        "device_concurrency": settings.reconciliation_device_concurrency,
        "active_scan_jobs": active,
        "waiting_scan_jobs": max(0, len(rows) - active),
        "available_scan_slots": max(
            0, settings.reconciliation_device_concurrency - active
        ),
    }


def serialize_job(session: Session, job: ReconciliationJob, *, include_events: bool = False) -> dict:
    connector = session.get(Connector, job.connector_id)
    chunks = session.scalar(
        select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id)
    ) or 0
    eta = _eta(job, chunks=chunks, connected=bool(connector and connector.connected))
    remaining = None if job.cutoff_count is None else max(0, job.cutoff_count - job.scanned_count)
    result = {
        "job_id": job.job_id,
        "mode": job.mode,
        "status": job.status,
        "phase": job.phase,
        "wait_reason": job.wait_reason,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "connector": None if connector is None else {
            "connector_id": connector.connector_id,
            "device_id": connector.device_id,
            "display_name": connector.display_name,
            "zone_id": connector.zone_id,
            "connected": connector.connected,
        },
        "terminal": {
            "serial": job.terminal_serial,
            "generation": job.terminal_generation,
            "cutoff_count": job.cutoff_count,
            "latest_count": job.latest_terminal_count,
            "record_size": job.record_size,
            "source_total_bytes": job.source_total_bytes,
        },
        "progress": {
            "scanned": job.scanned_count,
            "remaining": remaining,
            "add_durable": job.add_durable_count,
            "already_present": job.already_present_count,
            "terminal_duplicates": job.terminal_duplicate_count,
            "blocked_identity": job.blocked_identity_count,
            "quarantined": job.quarantined_count,
            "oracle_target": job.ords_target_count,
            "oracle_confirmed": job.ords_confirmed_count,
            "oracle_pending": job.ords_pending_count,
            "retry_count": job.retry_count,
        },
        "checkpoint": {
            "next_ordinal": job.committed_next_ordinal,
            "chain_digest": job.last_chain_digest,
            "last_progress_at": job.last_progress_at,
        },
        "eta": eta,
        "capture_certificate": job.capture_certificate or None,
        "oracle_certificate": job.oracle_certificate or None,
        "requested_at": job.requested_at,
        "started_at": job.started_at,
        "capture_certified_at": job.capture_certified_at,
        "oracle_certified_at": job.oracle_certified_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
    }
    if include_events:
        result["events"] = [
            {"state": row.state, "details": row.details, "created_at": row.created_at}
            for row in session.scalars(
                select(ReconciliationEvent)
                .where(ReconciliationEvent.job_id == job.id)
                .order_by(ReconciliationEvent.id.asc())
            ).all()
        ]
    return result


def serialize_coverage(row: ReconciliationCoverage | None) -> dict | None:
    if row is None:
        return None
    return {
        "coverage_id": row.coverage_id,
        "terminal_serial": row.terminal_serial,
        "terminal_generation": row.terminal_generation,
        "certified_source_cursor": row.certified_source_cursor,
        "source_chain_digest": row.source_chain_digest,
        "capture_state": row.capture_state,
        "oracle_state": row.oracle_state,
        "capture_evidence": row.capture_evidence,
        "oracle_evidence": row.oracle_evidence,
        "active": row.active,
        "invalidated_reason": row.invalidated_reason,
        "captured_at": row.captured_at,
        "oracle_certified_at": row.oracle_certified_at,
        "invalidated_at": row.invalidated_at,
    }


def active_coverage(
    session: Session, zkt: ZKTDevice | None
) -> ReconciliationCoverage | None:
    if zkt is None:
        return None
    return session.scalar(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.zkt_device_id == zkt.id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
    )


def invalidate_coverage_for_terminal_change(
    session: Session,
    *,
    zkt: ZKTDevice,
    previous_serial: str | None,
    previous_attendance_count: int | None,
) -> bool:
    coverage = active_coverage(session, zkt)
    if coverage is None:
        return False
    try:
        reported_coverage_cursor = int(
            (zkt.capability_profile or {}).get("source_coverage_cursor") or 0
        )
    except (TypeError, ValueError):
        reported_coverage_cursor = 0
    coverage_checkpoint_grace_elapsed = (
        coverage.captured_at is not None
        and utc_now() - ensure_utc(coverage.captured_at) >= timedelta(seconds=60)
    )
    reason = None
    if previous_serial and zkt.serial and previous_serial != zkt.serial:
        reason = "TERMINAL_SERIAL_CHANGED"
    elif (
        zkt.online
        and zkt.connection_state in {"ONLINE", "STABLE"}
        and coverage_checkpoint_grace_elapsed
        and bool((zkt.capability_profile or {}).get(RECONCILIATION_CAPABILITY))
        and not bool(
            (zkt.capability_profile or {}).get("source_coverage_certified")
        )
    ):
        reason = "FIRMWARE_COVERAGE_CHECKPOINT_LOST"
    elif (
        zkt.online
        and zkt.connection_state in {"ONLINE", "STABLE"}
        and coverage_checkpoint_grace_elapsed
        and reported_coverage_cursor < coverage.certified_source_cursor
    ):
        reason = "FIRMWARE_COVERAGE_CURSOR_REGRESSED"
    elif (
        zkt.online
        and zkt.connection_state in {"ONLINE", "STABLE"}
        and previous_attendance_count is not None
        and zkt.attendance_count is not None
        and zkt.attendance_count >= 0
        and zkt.attendance_count < previous_attendance_count
        and zkt.attendance_count < coverage.certified_source_cursor
    ):
        reason = "TERMINAL_ATTENDANCE_COUNT_REGRESSED"
    if reason is None:
        return False
    now = utc_now()
    coverage.active = False
    coverage.invalidated_reason = reason
    coverage.invalidated_at = now
    coverage.updated_at = now
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is not None:
        job.status = "INVALIDATED"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = reason
        job.error_code = reason
        job.error_message = "Certified terminal coverage changed and was invalidated fail-closed."
        job.updated_at = now
        _event(session, job, "COVERAGE_INVALIDATED", {"reason": reason})
    return True


def apply_reconciliation_device_fault(
    session: Session,
    *,
    connector: Connector,
    code: str | None,
) -> bool:
    """Persist fail-closed firmware source faults as ADD control-plane state."""

    if not code:
        return False
    scan_faults = {
        "SOURCE_RANGE_LAYOUT_INVALID",
        "SOURCE_RANGE_COUNT_REGRESSION",
        "SOURCE_FIRST_ANCHOR_DIVERGED",
        "SOURCE_COMMITTED_BOUNDARY_DIVERGED",
    }
    if code in scan_faults:
        job = session.scalar(
            select(ReconciliationJob)
            .where(
                ReconciliationJob.connector_id == connector.id,
                ReconciliationJob.capture_certified_at.is_(None),
                ReconciliationJob.status.in_(["QUEUED", "RUNNING"]),
            )
            .order_by(ReconciliationJob.requested_at.asc())
            .with_for_update()
        )
        if job is None:
            return False
        _safety_hold(
            session,
            job,
            code,
            "Firmware stopped source reconciliation after immutable terminal evidence diverged.",
        )
        return True
    if code != "ADD_SOURCE_COVERAGE_INVALIDATED" or connector.zkt_device is None:
        return False
    coverage = active_coverage(session, connector.zkt_device)
    if coverage is None:
        return False
    now = utc_now()
    coverage.active = False
    coverage.invalidated_reason = code
    coverage.invalidated_at = now
    coverage.updated_at = now
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is not None:
        job.status = "INVALIDATED"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = code
        job.error_code = code
        job.error_message = "Firmware invalidated its certified source checkpoint fail-closed."
        job.updated_at = now
        _event(session, job, "COVERAGE_INVALIDATED", {"reason": code})
    return True


def reconciliation_chunk_digest(payload: ReconciliationChunkRequest) -> str:
    records = [
        {
            "disposition": row.disposition,
            "event_uid": row.event.event_uid if row.event else None,
            "occurrence_index": row.occurrence_index,
            "ordinal": row.ordinal,
            "raw_record_digest": row.raw_record_digest,
            "terminal_record_key": row.terminal_record_key,
        }
        for row in payload.records
    ]
    material = json.dumps(records, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


def reconciliation_chain_digest(
    previous: str | None,
    *,
    start_ordinal: int,
    end_ordinal: int,
    chunk_digest: str,
) -> str:
    material = f"{previous or ('0' * 64)}:{start_ordinal}:{end_ordinal}:{chunk_digest}"
    return hashlib.sha256(material.encode()).hexdigest()


def _sealed_evidence(evidence: dict) -> dict:
    material = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(
        settings.effective_fleet_root_secret.encode(),
        material.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        **evidence,
        "evidence_algorithm": "HMAC-SHA256",
        "evidence_signature": signature,
    }


def _device_job(session: Session, connector: Connector, job_id: str) -> ReconciliationJob:
    job = session.scalar(
        select(ReconciliationJob)
        .where(
            ReconciliationJob.job_id == job_id,
            ReconciliationJob.connector_id == connector.id,
        )
        .with_for_update()
    )
    if job is None:
        raise ValueError("Unknown reconciliation assignment.")
    return job


def _require_runnable(job: ReconciliationJob, generation: int) -> None:
    if job.status in TERMINAL_JOB_STATES | PAUSED_JOB_STATES:
        raise ValueError(f"Reconciliation is not accepting source data while {job.status}.")
    if generation != job.terminal_generation:
        raise ValueError("Reconciliation generation does not match the assignment.")


def _safety_hold(
    session: Session, job: ReconciliationJob, code: str, message: str
) -> ReconciliationJob:
    job.status = "NEEDS_ATTENTION"
    job.phase = "WAITING_FOR_SAFE_WINDOW"
    job.wait_reason = code
    job.error_code = code
    job.error_message = message
    job.updated_at = utc_now()
    _event(session, job, "SAFETY_HOLD", {"code": code, "message": message})
    return job


def _event(
    session: Session,
    job: ReconciliationJob,
    state: str,
    details: dict,
    *,
    idempotency_key: str | None = None,
) -> None:
    session.add(
        ReconciliationEvent(
            job_id=job.id,
            state=state,
            details=details,
            idempotency_key=idempotency_key,
        )
    )


def _eta(job: ReconciliationJob, *, chunks: int, connected: bool) -> dict:
    unavailable = None
    if not connected:
        unavailable = "WAITING_FOR_DEVICE"
    elif job.status in {"PAUSED", "PAUSE_REQUESTED", "NEEDS_ATTENTION"}:
        unavailable = job.wait_reason or job.status
    elif chunks < 5 or job.started_at is None or job.last_progress_at is None:
        unavailable = "COLLECTING_THROUGHPUT"
    elapsed = 0.0
    if job.started_at and job.last_progress_at:
        elapsed = max(0.0, (job.last_progress_at - job.started_at).total_seconds())
    if elapsed < 60:
        unavailable = unavailable or "COLLECTING_THROUGHPUT"
    if unavailable or not job.cutoff_count or job.scanned_count <= 0:
        return {
            "low_seconds": None,
            "high_seconds": None,
            "confidence": "UNAVAILABLE",
            "unavailable_reason": unavailable or "SCOPE_UNKNOWN",
        }
    rate = job.scanned_count / elapsed
    scan_remaining = max(0, job.cutoff_count - job.scanned_count)
    scan_seconds = scan_remaining / max(rate, 0.001)
    point = max(scan_seconds, float(job.ords_pending_count) / max(rate, 0.001))
    return {
        "low_seconds": int(point * 0.8),
        "high_seconds": max(1, int(point * 1.3)),
        "confidence": "HIGH" if chunks >= 20 else "MEDIUM",
        "unavailable_reason": None,
    }
