from __future__ import annotations

import base64
import binascii
from datetime import timedelta
import hashlib
import hmac
import json
import re
from uuid import uuid4

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
    ReconciliationDivergence,
    ReconciliationEvent,
    ReconciliationJob,
    SourceTailChunk,
    TemporaryAdminLease,
    TerminalRecordManifest,
    TerminalSourceEpoch,
    ZKTDevice,
)
from zk_add.ords_states import (
    ORDS_IDENTITY_HELD_STATUSES,
    classify_ords_assurance_status,
    normalize_ords_status,
)
from zk_add.schemas import (
    ReconciliationAssignmentReleaseRequest,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationManifestRequest,
    SourceProbeResultRequest,
    SourceTailChunkRequest,
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
RECONCILIATION_CAPABILITY = "history_stream_v1"
RECONCILIATION_V2_CAPABILITY = "history_stream_v2"
RANGE_RESUME_CAPABILITY = "history_range_resume_verified"
SOURCE_PROBE_CAPABILITY = "source_divergence_probe_v1"


def _release_assignment(job: ReconciliationJob) -> None:
    job.active_assignment_id = None
    job.credit_start_ordinal = None
    job.credit_end_ordinal = None
    job.credit_committed_through = job.committed_next_ordinal
    job.assignment_granted_at = None
    job.assignment_expires_at = None
    job.assignment_accepted_at = None
    job.assignment_heartbeat_at = None


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


def _active_or_new_source_epoch(
    session: Session, *, zkt_device_id: int, terminal_generation: int
) -> TerminalSourceEpoch:
    epoch = session.scalar(
        select(TerminalSourceEpoch)
        .where(
            TerminalSourceEpoch.zkt_device_id == zkt_device_id,
            TerminalSourceEpoch.terminal_generation == terminal_generation,
            TerminalSourceEpoch.state.in_(["ACTIVE", "CANDIDATE"]),
        )
        .order_by(TerminalSourceEpoch.sequence.desc())
    )
    if epoch is not None:
        return epoch
    latest = session.scalar(
        select(func.max(TerminalSourceEpoch.sequence)).where(
            TerminalSourceEpoch.zkt_device_id == zkt_device_id,
            TerminalSourceEpoch.terminal_generation == terminal_generation,
        )
    ) or 0
    now = utc_now()
    epoch = TerminalSourceEpoch(
        zkt_device_id=zkt_device_id,
        terminal_generation=terminal_generation,
        sequence=latest + 1,
        state="ACTIVE",
        activated_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(epoch)
    session.flush()
    return epoch


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
    coverage = active_coverage(session, zkt) if zkt else None
    coverage_payload = serialize_coverage(coverage)
    if coverage is not None and coverage_payload is not None:
        source_count, first_ordinal, last_ordinal = session.execute(
            select(
                func.count(TerminalRecordManifest.id),
                func.min(TerminalRecordManifest.ordinal),
                func.max(TerminalRecordManifest.ordinal),
            ).where(
                TerminalRecordManifest.zkt_device_id == coverage.zkt_device_id,
                TerminalRecordManifest.generation == coverage.terminal_generation,
                TerminalRecordManifest.source_epoch_id == coverage.source_epoch_id,
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal < coverage.source_committed_cursor,
            )
        ).one()
        cursor = coverage.source_committed_cursor
        ledger_complete = (
            int(source_count or 0) == cursor
            and (
                cursor == 0
                or (first_ordinal == 0 and last_ordinal == cursor - 1)
            )
        )
        coverage_payload.update(
            {
                "source_ledger_count": int(source_count or 0),
                "source_ledger_complete": ledger_complete,
                "terminal_source_parity": bool(
                    zkt is not None
                    and zkt.attendance_count is not None
                    and cursor == zkt.attendance_count
                ),
                "chain_continuous": bool(
                    coverage.active
                    and len(coverage.source_committed_chain_digest or "") == 64
                ),
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
        "coverage": coverage_payload,
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
    generation = max(1, connector.onboarding_generation)
    source_epoch = _active_or_new_source_epoch(
        session,
        zkt_device_id=zkt.id,
        terminal_generation=generation,
    )
    operation_id = str(uuid4())
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
        terminal_generation=generation,
        source_epoch_id=source_epoch.id,
        operation_id=operation_id,
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
    _release_assignment(job)
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
    _release_assignment(job)
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
) -> tuple[ReconciliationJob, ReconciliationChunk | None, bool]:
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
    if payload.assignment_id is not None:
        if job.active_assignment_id != payload.assignment_id:
            raise ValueError("Reconciliation assignment lease is no longer active.")
        if (
            job.credit_start_ordinal is None
            or job.credit_end_ordinal is None
            or payload.start_ordinal < job.credit_start_ordinal
            or payload.end_ordinal > job.credit_end_ordinal
        ):
            raise ValueError("Chunk is outside its durable reconciliation credit.")
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

    existing_source_rows = {
        row.ordinal: row
        for row in session.scalars(
            select(TerminalRecordManifest).where(
                TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
                TerminalRecordManifest.generation == payload.generation,
                TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal >= payload.start_ordinal,
                TerminalRecordManifest.ordinal < payload.end_ordinal,
            )
        ).all()
    }
    interpretation_drift_ordinals: set[int] = set()
    for source in payload.records:
        prior = existing_source_rows.get(source.ordinal)
        if prior is None:
            continue
        prior_parsed = prior.disposition in {
            "EVENT",
            "BLOCKED_IDENTITY",
            "TERMINAL_DUPLICATE",
        }
        source_parsed = source.disposition in {"EVENT", "BLOCKED_IDENTITY"}
        if prior.raw_record_digest != source.raw_record_digest:
            _begin_source_divergence(session, job=job, prior=prior, source=source)
            return job, None, False
        if (
            prior.terminal_record_key != source.terminal_record_key
            or prior.occurrence_index != source.occurrence_index
        ):
            _safety_hold(
                session,
                job,
                "SOURCE_DERIVED_KEY_DIVERGED",
                "Raw evidence matched but its derived source key changed; scanning stopped fail-closed.",
            )
            return job, None, False
        if prior_parsed != source_parsed or (
            not prior_parsed and prior.disposition != source.disposition
        ):
            interpretation_drift_ordinals.add(source.ordinal)
            _event(
                session,
                job,
                "SOURCE_INTERPRETATION_DRIFT",
                {
                    "ordinal": source.ordinal,
                    "preserved_disposition": prior.disposition,
                    "observed_disposition": source.disposition,
                    "raw_record_digest": source.raw_record_digest,
                },
            )

    from zk_add.service import ingest_attendance

    attendance = [
        row.event
        for row in payload.records
        if row.event is not None and row.ordinal not in interpretation_drift_ordinals
    ]
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
    current_terminal_keys = [row.terminal_record_key for row in payload.records]
    seen_terminal_keys = set(
        session.scalars(
            select(TerminalRecordManifest.terminal_record_key).where(
                TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
                TerminalRecordManifest.generation == payload.generation,
                TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal < payload.start_ordinal,
                TerminalRecordManifest.terminal_record_key.in_(current_terminal_keys),
            )
        ).all()
    )
    for source in payload.records:
        event = events_by_uid.get(source.event.event_uid) if source.event else None
        disposition = source.disposition
        if event and source.terminal_record_key in seen_terminal_keys:
            disposition = "TERMINAL_DUPLICATE"
        elif event and event.ords_status == "BLOCKED_IDENTITY":
            disposition = "BLOCKED_IDENTITY"
        seen_terminal_keys.add(source.terminal_record_key)
        if disposition == "BLOCKED_IDENTITY":
            blocked += 1
        if disposition in {"INVALID_TIME", "MALFORMED"}:
            quarantined += 1
        if disposition == "TERMINAL_DUPLICATE":
            terminal_duplicates += 1
        prior = existing_source_rows.get(source.ordinal)
        if prior is None:
            session.add(TerminalRecordManifest(
                job_id=job.id,
                chunk_id=chunk.id,
                connector_id=connector.id,
                zkt_device_id=job.zkt_device_id,
                terminal_serial=job.terminal_serial or "unknown",
                generation=payload.generation,
                source_epoch_id=job.source_epoch_id,
                ordinal=source.ordinal,
                source_kind="BASELINE",
                canonical_source=True,
                record_size=job.record_size,
                raw_record_digest=source.raw_record_digest,
                terminal_record_key=source.terminal_record_key,
                occurrence_index=source.occurrence_index,
                attendance_event_id=event.id if event else None,
                disposition=disposition,
                protected_raw_record=encrypt_text(source.raw_record_b64),
                error_code=source.error_code,
                raw_timestamp=source.raw_timestamp,
                observed_uid=source.observed_uid,
                observed_user_id=source.observed_user_id,
            ))
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
    if payload.assignment_id is not None:
        job.assignment_accepted_at = job.assignment_accepted_at or now
        job.assignment_heartbeat_at = now
        job.credit_committed_through = payload.end_ordinal
        job.assignment_expires_at = now + timedelta(
            seconds=settings.reconciliation_v2_assignment_seconds
        )
        if job.credit_end_ordinal is not None and payload.end_ordinal >= job.credit_end_ordinal:
            _release_assignment(job)
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


def apply_reconciliation_assignment_release(
    session: Session,
    *,
    connector: Connector,
    payload: ReconciliationAssignmentReleaseRequest,
) -> ReconciliationJob:
    """Release only transport credit; the committed source checkpoint is immutable."""

    job = _device_job(session, connector, payload.job_id)
    _require_runnable(job, payload.generation)
    if payload.committed_next_ordinal != job.committed_next_ordinal:
        raise ValueError("Released reconciliation credit did not match ADD's durable cursor.")
    if job.active_assignment_id == payload.assignment_id:
        _release_assignment(job)
        now = utc_now()
        if payload.reason == "TRANSIENT_STEP_FAILED":
            delays = (5, 15, 30, 60)
            delay = delays[min(job.auto_retry_count, len(delays) - 1)]
            job.auto_retry_count += 1
            job.phase = "RECOVERING_AFTER_INTERRUPTION"
            job.wait_reason = "TRANSIENT_STEP_RETRY"
            job.next_retry_at = now + timedelta(seconds=delay)
        else:
            job.next_retry_at = now
        job.updated_at = now
        _event(
            session,
            job,
            "ASSIGNMENT_RELEASED",
            {
                "assignment_id": payload.assignment_id,
                "committed_next_ordinal": payload.committed_next_ordinal,
                "reason": payload.reason,
            },
        )
    return job


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
            TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
            TerminalRecordManifest.generation == payload.generation,
            TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
            TerminalRecordManifest.ordinal < payload.cutoff_count,
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
        source_epoch_id=job.source_epoch_id,
        terminal_serial=job.terminal_serial or "unknown",
        terminal_generation=job.terminal_generation,
        certified_source_cursor=payload.cutoff_count,
        source_chain_digest=payload.final_chain_digest,
        source_committed_cursor=payload.cutoff_count,
        source_committed_chain_digest=payload.final_chain_digest,
        tail_exception_count=0,
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
    _release_assignment(job)
    _event(session, job, capture_state, evidence)
    return refresh_reconciliation_assurance(session, job)


def _invalidate_tail_coverage(
    session: Session,
    *,
    connector: Connector,
    coverage: ReconciliationCoverage,
    code: str,
    message: str,
) -> None:
    """Persist a source-evidence divergence instead of losing it to rollback."""

    now = utc_now()
    coverage.active = False
    coverage.capture_state = "SOURCE_COVERAGE_INVALIDATED"
    coverage.invalidated_reason = code
    coverage.invalidated_at = now
    coverage.updated_at = now
    job = session.get(ReconciliationJob, coverage.job_id)
    if job is not None:
        job.status = "INVALIDATED"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = code
        job.error_code = code
        job.error_message = message
        job.updated_at = now
        _event(session, job, "COVERAGE_INVALIDATED", {"reason": code})
    from zk_add.service import upsert_alert

    upsert_alert(
        session,
        connector,
        code="ADD_SOURCE_COVERAGE_INVALIDATED",
        severity="CRITICAL",
        message=message,
        details={"failure_category": code},
    )


def apply_source_tail_chunk(
    session: Session,
    *,
    connector: Connector,
    payload: SourceTailChunkRequest,
) -> tuple[ReconciliationCoverage, SourceTailChunk | None, bool, str | None]:
    """Atomically commit a contiguous terminal tail, including poison rows.

    The caller must send an ACK only after the surrounding transaction commits.
    Content dispositions never fail the batch; only evidence/cursor divergence
    invalidates source coverage.
    """

    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT terminal.")
    coverage = session.scalar(
        select(ReconciliationCoverage)
        .where(
            ReconciliationCoverage.zkt_device_id == zkt.id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
        .with_for_update()
    )
    if coverage is None:
        raise ValueError("Terminal has no active certified source coverage.")

    def invalidate(code: str, message: str):
        _invalidate_tail_coverage(
            session,
            connector=connector,
            coverage=coverage,
            code=code,
            message=message,
        )
        return coverage, None, False, code

    if (
        payload.terminal_serial != coverage.terminal_serial
        or payload.terminal_generation != coverage.terminal_generation
    ):
        return invalidate(
            "SOURCE_TAIL_GENERATION_DIVERGED",
            "Terminal identity changed while extending certified source coverage.",
        )
    committed_cursor = coverage.source_committed_cursor
    committed_chain = coverage.source_committed_chain_digest
    existing = session.scalar(
        select(SourceTailChunk).where(
            SourceTailChunk.coverage_id == coverage.id,
            SourceTailChunk.generation == payload.terminal_generation,
            SourceTailChunk.start_ordinal == payload.start_ordinal,
        )
    )
    if existing is not None:
        if (
            existing.end_ordinal != payload.end_ordinal
            or existing.chunk_digest != payload.chunk_digest
            or existing.resulting_chain_digest != payload.resulting_chain_digest
        ):
            return invalidate(
                "SOURCE_TAIL_REPLAY_DIVERGED",
                "A replayed terminal tail range no longer matched its committed evidence.",
            )
        return coverage, existing, True, None
    if payload.latest_terminal_count < committed_cursor:
        return invalidate(
            "SOURCE_TAIL_COUNT_REGRESSION",
            "Terminal attendance count regressed below ADD's committed source cursor.",
        )
    if payload.start_ordinal != committed_cursor:
        return invalidate(
            "SOURCE_TAIL_CURSOR_DIVERGED",
            "Terminal tail did not begin at ADD's exact committed source cursor.",
        )
    for source in payload.records:
        try:
            raw_record = base64.b64decode(source.raw_record_b64, validate=True)
        except (binascii.Error, ValueError):
            return invalidate(
                "SOURCE_TAIL_EVIDENCE_INVALID",
                "Terminal tail contained invalid protected source evidence.",
            )
        if (
            len(raw_record) != payload.record_size
            or hashlib.sha256(raw_record).hexdigest() != source.raw_record_digest
        ):
            return invalidate(
                "SOURCE_TAIL_EVIDENCE_INVALID",
                "Terminal tail raw evidence did not match its declared record layout or digest.",
            )
    canonical_digest = reconciliation_chunk_digest(payload)
    if canonical_digest != payload.chunk_digest:
        return invalidate(
            "SOURCE_TAIL_CHUNK_DIGEST_DIVERGED",
            "Terminal tail canonical digest did not match its source records.",
        )
    if payload.previous_chain_digest != committed_chain:
        return invalidate(
            "SOURCE_TAIL_CHAIN_DIVERGED",
            "Terminal tail did not continue from ADD's committed source chain.",
        )
    expected_result = reconciliation_chain_digest(
        committed_chain,
        start_ordinal=payload.start_ordinal,
        end_ordinal=payload.end_ordinal,
        chunk_digest=payload.chunk_digest,
    )
    if payload.resulting_chain_digest != expected_result:
        return invalidate(
            "SOURCE_TAIL_CHAIN_DIVERGED",
            "Terminal tail resulting chain digest was invalid.",
        )

    from zk_add.service import ingest_attendance, upsert_alert

    attendance = [row.event for row in payload.records if row.event is not None]
    if attendance:
        ingest_attendance(session, connector=connector, events=attendance)
    session.flush()
    event_uids = [row.event.event_uid for row in payload.records if row.event]
    events_by_uid = (
        {
            row.event_uid: row
            for row in session.scalars(
                select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(event_uids))
            ).all()
        }
        if event_uids
        else {}
    )
    chunk = SourceTailChunk(
        coverage_id=coverage.id,
        connector_id=connector.id,
        zkt_device_id=zkt.id,
        generation=payload.terminal_generation,
        start_ordinal=payload.start_ordinal,
        end_ordinal=payload.end_ordinal,
        latest_terminal_count=payload.latest_terminal_count,
        record_count=len(payload.records),
        chunk_digest=payload.chunk_digest,
        previous_chain_digest=payload.previous_chain_digest,
        resulting_chain_digest=payload.resulting_chain_digest,
    )
    session.add(chunk)
    session.flush()
    blocked = 0
    exceptions = 0
    event_count = 0
    current_terminal_keys = [row.terminal_record_key for row in payload.records]
    seen_terminal_keys = set(
        session.scalars(
            select(TerminalRecordManifest.terminal_record_key).where(
                TerminalRecordManifest.zkt_device_id == zkt.id,
                TerminalRecordManifest.generation == payload.terminal_generation,
                TerminalRecordManifest.source_epoch_id == coverage.source_epoch_id,
                TerminalRecordManifest.canonical_source == True,  # noqa: E712
                TerminalRecordManifest.ordinal < payload.start_ordinal,
                TerminalRecordManifest.terminal_record_key.in_(current_terminal_keys),
            )
        ).all()
    )
    for source in payload.records:
        event = events_by_uid.get(source.event.event_uid) if source.event else None
        disposition = source.disposition
        if event is not None:
            event_count += 1
            if source.terminal_record_key in seen_terminal_keys:
                disposition = "TERMINAL_DUPLICATE"
            elif event.ords_status == "BLOCKED_IDENTITY":
                disposition = "BLOCKED_IDENTITY"
        seen_terminal_keys.add(source.terminal_record_key)
        if disposition == "BLOCKED_IDENTITY":
            blocked += 1
        if disposition in {"INVALID_TIME", "MALFORMED"}:
            exceptions += 1
        session.add(
            TerminalRecordManifest(
                job_id=None,
                chunk_id=None,
                connector_id=connector.id,
                zkt_device_id=zkt.id,
                terminal_serial=coverage.terminal_serial,
                generation=payload.terminal_generation,
                source_epoch_id=coverage.source_epoch_id,
                ordinal=source.ordinal,
                source_kind="TAIL",
                canonical_source=True,
                record_size=payload.record_size,
                raw_record_digest=source.raw_record_digest,
                terminal_record_key=source.terminal_record_key,
                occurrence_index=source.occurrence_index,
                attendance_event_id=event.id if event else None,
                disposition=disposition,
                protected_raw_record=encrypt_text(source.raw_record_b64),
                error_code=source.error_code,
                raw_timestamp=source.raw_timestamp,
                observed_uid=source.observed_uid,
                observed_user_id=source.observed_user_id,
            )
        )
    chunk.event_count = event_count
    chunk.blocked_identity_count = blocked
    chunk.exception_count = exceptions
    now = utc_now()
    coverage.source_committed_cursor = payload.end_ordinal
    coverage.source_committed_chain_digest = payload.resulting_chain_digest
    coverage.tail_exception_count += exceptions
    coverage.tail_last_committed_at = now
    coverage.updated_at = now
    if exceptions:
        coverage.capture_state = "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"
        upsert_alert(
            session,
            connector,
            code="TERMINAL_SOURCE_EXCEPTION",
            severity="HIGH",
            message=(
                "ADD preserved invalid or malformed terminal source rows and safely "
                "continued reconciliation."
            ),
            details={
                "failure_category": "TERMINAL_SOURCE_EXCEPTION",
                "exception_count": coverage.tail_exception_count,
                "last_start_ordinal": payload.start_ordinal,
                "last_end_ordinal": payload.end_ordinal,
                "inspector_path": (
                    "/reconciliation?tab=source-exceptions&device_id="
                    f"{connector.connector_id}"
                ),
            },
        )
    return coverage, chunk, False, None


def refresh_reconciliation_assurance(
    session: Session, job: ReconciliationJob
) -> ReconciliationJob:
    if job.status in TERMINAL_JOB_STATES:
        return job
    manifest_events = select(TerminalRecordManifest.attendance_event_id).where(
        TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
        TerminalRecordManifest.generation == job.terminal_generation,
        TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
        TerminalRecordManifest.canonical_source == True,  # noqa: E712
        TerminalRecordManifest.ordinal < (job.cutoff_count or 0),
        TerminalRecordManifest.attendance_event_id.is_not(None),
    ).distinct()
    status_rows = session.execute(
        select(AttendanceEvent.ords_status, func.count(AttendanceEvent.id))
        .where(AttendanceEvent.id.in_(manifest_events))
        .group_by(AttendanceEvent.ords_status)
    ).all()
    status_counts: dict[str, int] = {}
    for status, count in status_rows:
        normalized = normalize_ords_status(status)
        status_counts[normalized] = status_counts.get(normalized, 0) + int(count)
    outcome_counts = {
        "CONFIRMED": 0,
        "IDENTITY_HELD": 0,
        "PENDING": 0,
        "REVIEW_REQUIRED": 0,
    }
    for status, count in status_counts.items():
        outcome_counts[classify_ords_assurance_status(status)] += count
    target = sum(status_counts.values())
    confirmed = outcome_counts["CONFIRMED"]
    blocked = outcome_counts["IDENTITY_HELD"]
    pending = outcome_counts["PENDING"]
    review = outcome_counts["REVIEW_REQUIRED"]
    review_state_counts = {
        status: count
        for status, count in sorted(status_counts.items())
        if classify_ords_assurance_status(status) == "REVIEW_REQUIRED"
    }
    identity_state_counts = {
        status: count
        for status, count in sorted(status_counts.items())
        if status in ORDS_IDENTITY_HELD_STATUSES
    }
    previous_counts = (
        job.ords_target_count,
        job.ords_confirmed_count,
        job.ords_pending_count,
        job.ords_review_count,
        job.blocked_identity_count,
    )
    job.ords_target_count = target
    job.ords_confirmed_count = confirmed
    job.ords_pending_count = pending
    job.ords_review_count = review
    job.blocked_identity_count = blocked
    now = utc_now()
    counts_changed = previous_counts != (
        target,
        confirmed,
        pending,
        review,
        blocked,
    )
    if counts_changed:
        job.last_progress_at = now
        job.updated_at = now
    coverage = session.scalar(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.job_id == job.id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
    )
    if job.capture_certified_at is None:
        return job
    if job.status in PAUSED_JOB_STATES:
        return job
    managed_oracle_hold = (
        job.error_code == "ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW"
    )
    if job.status == "NEEDS_ATTENTION" and not managed_oracle_hold:
        # Reconciliation assurance must never clear an unrelated source,
        # identity, device, or operator safety hold.
        return job
    if job.quarantined_count:
        job.phase = "FINAL_ASSURANCE"
        job.status = "NEEDS_ATTENTION"
        job.wait_reason = "SOURCE_QUARANTINE_REQUIRES_REVIEW"
    elif review > 0:
        details = _sealed_evidence({
            "job_id": job.job_id,
            "oracle_target": target,
            "oracle_confirmed": confirmed,
            "identity_held": blocked,
            "oracle_pending": pending,
            "oracle_review_required": review,
            "review_state_counts": review_state_counts,
            "observed_at": now.isoformat(),
            "policy": "APPEND_ONLY_MEMBERSHIP",
        })
        state_summary = ", ".join(
            f"{status}={count}" for status, count in review_state_counts.items()
        )
        entering_hold = not managed_oracle_hold or job.status != "NEEDS_ATTENTION"
        job.phase = "FINAL_ASSURANCE"
        job.status = "NEEDS_ATTENTION"
        job.wait_reason = "ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW"
        job.error_code = "ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW"
        job.error_message = (
            f"{review:,} preserved attendance event(s) reached a terminal Oracle "
            f"outcome requiring review ({state_summary}). No attendance or Oracle "
            "rows were deleted; resolve the recorded outcome before retrying assurance."
        )
        if coverage:
            coverage.oracle_state = "ORACLE_MEMBERSHIP_REVIEW_REQUIRED"
            if entering_hold or counts_changed or not coverage.oracle_evidence:
                coverage.oracle_evidence = details
                coverage.updated_at = now
        if entering_hold or counts_changed:
            _event(session, job, "ORACLE_MEMBERSHIP_REVIEW_REQUIRED", details)
    elif pending > 0:
        job.phase = "DRAINING_ORDS"
        job.status = "RUNNING"
        job.wait_reason = None
        if managed_oracle_hold:
            job.error_code = None
            job.error_message = None
            _event(
                session,
                job,
                "ORACLE_MEMBERSHIP_RETRYING",
                {
                    "oracle_pending": pending,
                    "oracle_confirmed": confirmed,
                    "identity_held": blocked,
                },
            )
        if coverage:
            coverage.oracle_state = "ORACLE_MEMBERSHIP_PENDING"
            coverage.updated_at = now
    else:
        evidence = _sealed_evidence({
            "job_id": job.job_id,
            "terminal_serial": job.terminal_serial,
            "certified_source_cursor": job.committed_next_ordinal,
            "oracle_membership_confirmed": confirmed,
            "blocked_identity": blocked,
            "identity_held_by_status": identity_state_counts,
            "oracle_review_required": 0,
            "resolvable_event_count": target - blocked,
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
        if managed_oracle_hold:
            job.error_code = None
            job.error_message = None
        if coverage:
            coverage.oracle_state = "ORACLE_MEMBERSHIP_CERTIFIED"
            coverage.oracle_evidence = evidence
            coverage.oracle_certified_at = now
            coverage.updated_at = now
        _event(session, job, "ORACLE_MEMBERSHIP_CERTIFIED", evidence)
    job.updated_at = now
    return job


def refresh_all_reconciliation_assurance(session: Session) -> int:
    rows = session.scalars(
        select(ReconciliationJob).where(
            ReconciliationJob.capture_certified_at.is_not(None),
            ReconciliationJob.status.not_in(TERMINAL_JOB_STATES),
            ReconciliationJob.status.not_in(PAUSED_JOB_STATES),
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
    now = utc_now()
    reserved_credit = 0
    for leased in rows:
        if (
            leased.active_assignment_id
            and leased.assignment_expires_at
            and ensure_utc(leased.assignment_expires_at) > now
            and leased.credit_end_ordinal is not None
        ):
            reserved_credit += max(
                0,
                leased.credit_end_ordinal - leased.committed_next_ordinal,
            )
    # Each ready job owns one isolated device slot through its short assignment
    # cooldown. A disconnected or safety-blocked device releases its slot so it
    # cannot stall another zone; the durable checkpoint lets it resume later.
    # The global Oracle backlog gate still stops all new source intake before
    # ADD or ORDS can be overloaded.
    slot_limit = settings.reconciliation_device_concurrency
    slots_owned = 0
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
        if (
            job.active_assignment_id is None
            and job.next_retry_at is not None
            and ensure_utc(job.next_retry_at) > now
        ):
            if job.phase != "VERIFYING_SOURCE_CHANGE":
                job.phase = "RECOVERING_AFTER_INTERRUPTION"
                job.wait_reason = job.wait_reason or "TRANSIENT_STEP_RETRY"
            continue
        if slots_owned >= slot_limit:
            job.phase = "WAITING_FOR_CAPACITY"
            job.wait_reason = "WAITING_FOR_SCAN_SLOT"
            continue
        slots_owned += 1
        job.wait_reason = None
        if job.phase != "VERIFYING_SOURCE_CHANGE":
            job.phase = "ANCHORING" if job.cutoff_count is None else "SCANNING_TERMINAL"
        zkt = connector.zkt_device
        capabilities = (zkt.capability_profile if zkt else {}) or {}
        supports_v2 = bool(capabilities.get(RECONCILIATION_V2_CAPABILITY))
        assignment_v2 = supports_v2 and job.cutoff_count is not None
        if job.phase == "VERIFYING_SOURCE_CHANGE":
            divergence = session.scalar(
                select(ReconciliationDivergence)
                .where(
                    ReconciliationDivergence.job_id == job.id,
                    ReconciliationDivergence.state.in_(["OBSERVED", "PROBING"]),
                )
                .order_by(ReconciliationDivergence.id.desc())
            )
            if divergence is None:
                _safety_hold(
                    session,
                    job,
                    "SOURCE_DIVERGENCE_EVIDENCE_MISSING",
                    "The source-recovery state had no immutable divergence evidence.",
                )
                continue
            if not bool(capabilities.get(SOURCE_PROBE_CAPABILITY)):
                _safety_hold(
                    session,
                    job,
                    "SOURCE_PROBE_UNSUPPORTED",
                    "Zone Lite 2.4.4 source-probe capability is required for automatic recovery.",
                )
                continue
            if divergence.next_probe_at and ensure_utc(divergence.next_probe_at) > now:
                job.wait_reason = "SOURCE_DIVERGENCE_PROBE_PENDING"
                continue
            assignments.append(
                (
                    connector.connector_id,
                    {
                        "schema_version": "1",
                        "type": "source_probe_assignment",
                        "job_id": job.job_id,
                        "generation": job.terminal_generation,
                        "expected_terminal_serial": job.terminal_serial,
                        "ordinal": divergence.ordinal,
                        "probe_attempt": len(divergence.observations or []),
                    },
                )
            )
            job.wait_reason = None
            job.next_retry_at = now + timedelta(seconds=30)
            continue
        if supports_v2 and job.active_assignment_id:
            lease_live = bool(
                job.assignment_expires_at
                and ensure_utc(job.assignment_expires_at) > now
            )
            credit_matches = bool(
                job.credit_start_ordinal is not None
                and job.credit_end_ordinal is not None
                and job.credit_start_ordinal <= job.committed_next_ordinal
                and job.committed_next_ordinal < job.credit_end_ordinal
            )
            if lease_live and credit_matches:
                continue
            reserved_credit -= max(
                0,
                (job.credit_end_ordinal or job.committed_next_ordinal)
                - job.committed_next_ordinal,
            )
            _release_assignment(job)
            job.auto_retry_count += 1
            job.phase = "RECOVERING_AFTER_INTERRUPTION"
            _event(
                session,
                job,
                "STALE_ASSIGNMENT_RECOVERED",
                {"committed_next_ordinal": job.committed_next_ordinal},
            )
        if job.next_retry_at is not None and job.next_retry_at > now:
            continue
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
                    TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
                    TerminalRecordManifest.generation == job.terminal_generation,
                    TerminalRecordManifest.source_epoch_id == job.source_epoch_id,
                    TerminalRecordManifest.canonical_source == True,  # noqa: E712
                    TerminalRecordManifest.ordinal == job.committed_next_ordinal - 1,
                )
            )
        chunk_records = min(
            100,
            max(1, settings.reconciliation_chunk_records),
            max(1, device_chunk_limit),
        )
        assignment_id = None
        credit_end = None
        max_chunks = 1
        lease_expires = None
        if assignment_v2:
            try:
                device_credit_limit = int(
                    capabilities.get("history_credit_max_records") or chunk_records
                )
            except (TypeError, ValueError):
                device_credit_limit = chunk_records
            remaining_records = max(
                0, job.cutoff_count - job.committed_next_ordinal
            )
            available_credit = max(
                0,
                settings.reconciliation_history_backlog_pause
                - history_backlog
                - reserved_credit,
            )
            requested_credit = min(
                settings.reconciliation_v2_credit_records,
                max(chunk_records, device_credit_limit),
                chunk_records * settings.reconciliation_v2_max_chunks,
                remaining_records,
                available_credit,
            )
            minimum_grant = min(chunk_records, remaining_records)
            if requested_credit < minimum_grant and remaining_records > 0:
                job.phase = "WAITING_FOR_SAFE_WINDOW"
                job.wait_reason = "HISTORY_BACKLOG_BACKPRESSURE"
                continue
            assignment_id = str(uuid4())
            credit_end = job.committed_next_ordinal + requested_credit
            max_chunks = max(1, (requested_credit + chunk_records - 1) // chunk_records)
            lease_expires = now + timedelta(
                seconds=settings.reconciliation_v2_assignment_seconds
            )
            job.active_assignment_id = assignment_id
            job.credit_start_ordinal = job.committed_next_ordinal
            job.credit_end_ordinal = credit_end
            job.credit_committed_through = job.committed_next_ordinal
            job.assignment_granted_at = now
            job.assignment_expires_at = lease_expires
            job.assignment_accepted_at = None
            job.assignment_heartbeat_at = None
            reserved_credit += requested_credit
        payload = {
            "schema_version": "2" if assignment_v2 else "1",
            "type": "reconcile_assignment",
            "protocol": "history_stream_v2" if assignment_v2 else "history_stream_v1",
            "assignment_id": assignment_id,
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
            "chunk_records": chunk_records,
            "credit_end_ordinal": credit_end,
            "max_chunks": max_chunks,
            "lease_expires_at": lease_expires.isoformat() if lease_expires else None,
            "lease_expires_epoch": int(lease_expires.timestamp()) if lease_expires else None,
            "source_policy": "APPEND_ONLY_NO_DELETE",
        }
        assignments.append((connector.connector_id, payload))
        job.next_retry_at = now + timedelta(
            seconds=max(
                2,
                settings.reconciliation_v2_assignment_seconds
                if assignment_v2
                else settings.reconciliation_assignment_seconds,
            )
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
        row.phase in {"ANCHORING", "SCANNING_TERMINAL", "VERIFYING_SOURCE_CHANGE"}
        and row.wait_reason is None
        for row in rows
    )
    history_backlog = session.scalar(
        select(func.count(OrdsOutbox.id)).where(
            OrdsOutbox.delivery_type == "FULL_HISTORY",
            OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE", "IN_FLIGHT"]),
        )
    ) or 0
    now = utc_now()
    reserved_credit = sum(
        max(0, (row.credit_end_ordinal or row.committed_next_ordinal) - row.committed_next_ordinal)
        for row in rows
        if row.active_assignment_id
        and row.assignment_expires_at
        and ensure_utc(row.assignment_expires_at) > now
    )
    return {
        "policy": "BOUNDED_PARALLEL_PER_DEVICE",
        "device_concurrency": settings.reconciliation_device_concurrency,
        "active_scan_jobs": active,
        "waiting_scan_jobs": max(0, len(rows) - active),
        "available_scan_slots": max(
            0, settings.reconciliation_device_concurrency - active
        ),
        "history_backlog": history_backlog,
        "history_backlog_limit": settings.reconciliation_history_backlog_pause,
        "reserved_credit": reserved_credit,
        "available_credit": max(
            0,
            settings.reconciliation_history_backlog_pause
            - history_backlog
            - reserved_credit,
        ),
    }


def serialize_job(session: Session, job: ReconciliationJob, *, include_events: bool = False) -> dict:
    connector = session.get(Connector, job.connector_id)
    chunks = session.scalar(
        select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id)
    ) or 0
    eta = _eta(job, chunks=chunks, connected=bool(connector and connector.connected))
    remaining = None if job.cutoff_count is None else max(0, job.cutoff_count - job.scanned_count)
    divergence = session.scalar(
        select(ReconciliationDivergence)
        .where(ReconciliationDivergence.job_id == job.id)
        .order_by(ReconciliationDivergence.id.desc())
    )
    source_epoch = session.get(TerminalSourceEpoch, job.source_epoch_id)
    operator_state, operator_message = _operator_status(job, connected=bool(connector and connector.connected))
    result = {
        "job_id": job.job_id,
        "mode": job.mode,
        "status": job.status,
        "phase": job.phase,
        "wait_reason": job.wait_reason,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "operator_state": operator_state,
        "operator_message": operator_message,
        "completion_outcome": job.completion_outcome,
        "review_required": job.review_required,
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
            "oracle_review_required": job.ords_review_count,
            "retry_count": job.retry_count,
            "auto_retry_count": job.auto_retry_count,
        },
        "checkpoint": {
            "next_ordinal": job.committed_next_ordinal,
            "chain_digest": job.last_chain_digest,
            "last_progress_at": job.last_progress_at,
        },
        "assignment": {
            "assignment_id": job.active_assignment_id,
            "credit_start_ordinal": job.credit_start_ordinal,
            "credit_end_ordinal": job.credit_end_ordinal,
            "credit_committed_through": job.credit_committed_through,
            "granted_at": job.assignment_granted_at,
            "expires_at": job.assignment_expires_at,
            "accepted_at": job.assignment_accepted_at,
            "heartbeat_at": job.assignment_heartbeat_at,
        },
        "eta": eta,
        "recovery": {
            "operation_id": job.operation_id or job.job_id,
            "source_epoch": source_epoch.sequence if source_epoch else 1,
            "source_epoch_id": source_epoch.epoch_id if source_epoch else None,
            "divergence": None if divergence is None else {
                "divergence_id": divergence.divergence_id,
                "ordinal": divergence.ordinal,
                "state": divergence.state,
                "old_raw_digest": divergence.old_raw_digest,
                "new_raw_digest": divergence.new_raw_digest,
                "observation_count": len(divergence.observations or []),
                "next_probe_at": divergence.next_probe_at,
            },
        },
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
        "source_epoch_id": row.source_epoch_id,
        "terminal_serial": row.terminal_serial,
        "terminal_generation": row.terminal_generation,
        "certified_source_cursor": row.certified_source_cursor,
        "source_chain_digest": row.source_chain_digest,
        "source_committed_cursor": row.source_committed_cursor,
        "source_committed_chain_digest": row.source_committed_chain_digest,
        "tail_exception_count": row.tail_exception_count,
        "tail_last_committed_at": row.tail_last_committed_at,
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


def _begin_source_divergence(
    session: Session,
    *,
    job: ReconciliationJob,
    prior: TerminalRecordManifest,
    source,
) -> ReconciliationDivergence:
    """Persist the first mismatch and switch to bounded fresh-source probes."""

    now = utc_now()
    row = session.scalar(
        select(ReconciliationDivergence).where(
            ReconciliationDivergence.job_id == job.id,
            ReconciliationDivergence.ordinal == source.ordinal,
            ReconciliationDivergence.state.in_(["OBSERVED", "PROBING"]),
        )
    )
    observation = {
        "raw_record_digest": source.raw_record_digest,
        "disposition": source.disposition,
        "observed_at": now.isoformat(),
        "kind": "RECONCILIATION_CHUNK",
    }
    if row is None:
        row = ReconciliationDivergence(
            job_id=job.id,
            source_epoch_id=job.source_epoch_id,
            ordinal=source.ordinal,
            state="OBSERVED",
            old_raw_digest=prior.raw_record_digest,
            new_raw_digest=source.raw_record_digest,
            old_disposition=prior.disposition,
            new_disposition=source.disposition,
            protected_new_raw_record=encrypt_text(source.raw_record_b64),
            observations=[observation],
            next_probe_at=now + timedelta(seconds=5),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        row.observations = [*(row.observations or []), observation]
        row.updated_at = now
    _release_assignment(job)
    if not settings.reconciliation_self_healing_enabled:
        job.status = "NEEDS_ATTENTION"
        job.phase = "WAITING_FOR_SAFE_WINDOW"
        job.wait_reason = "SELF_HEALING_RECOVERY_DISABLED"
        job.error_code = "SELF_HEALING_RECOVERY_DISABLED"
        job.error_message = (
            "A raw terminal-source change was preserved while automatic recovery is gated."
        )
        job.updated_at = now
        _event(
            session,
            job,
            "SOURCE_DIVERGENCE_OBSERVED",
            {
                "divergence_id": row.divergence_id,
                "ordinal": row.ordinal,
                "recovery_gated": True,
            },
        )
        return row
    job.status = "RUNNING"
    job.phase = "VERIFYING_SOURCE_CHANGE"
    job.wait_reason = "SOURCE_DIVERGENCE_PROBE_PENDING"
    job.error_code = None
    job.error_message = None
    job.next_retry_at = row.next_probe_at
    job.updated_at = now
    _event(
        session,
        job,
        "SOURCE_DIVERGENCE_OBSERVED",
        {
            "divergence_id": row.divergence_id,
            "ordinal": row.ordinal,
            "old_raw_digest": row.old_raw_digest,
            "new_raw_digest": row.new_raw_digest,
        },
    )
    return row


def apply_source_probe_result(
    session: Session,
    *,
    connector: Connector,
    payload: SourceProbeResultRequest,
) -> ReconciliationJob:
    """Resolve a mismatch only after independent fresh terminal preparations."""

    job = _device_job(session, connector, payload.job_id)
    _require_runnable(job, payload.generation)
    if job.terminal_serial != payload.terminal_serial:
        return _safety_hold(
            session,
            job,
            "TERMINAL_SERIAL_CHANGED",
            "Terminal identity changed while confirming a source divergence.",
        )
    divergence = session.scalar(
        select(ReconciliationDivergence)
        .where(
            ReconciliationDivergence.job_id == job.id,
            ReconciliationDivergence.ordinal == payload.ordinal,
            ReconciliationDivergence.state.in_(["OBSERVED", "PROBING"]),
        )
        .with_for_update()
    )
    if divergence is None:
        raise ValueError("Source probe has no active divergence to verify.")
    if payload.record.ordinal != payload.ordinal:
        raise ValueError("Source probe record did not match its requested ordinal.")
    try:
        raw = base64.b64decode(payload.record.raw_record_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Source probe contains invalid base64 evidence.") from exc
    if (
        len(raw) != payload.record_size
        or hashlib.sha256(raw).hexdigest() != payload.record.raw_record_digest
    ):
        raise ValueError("Source probe raw evidence did not match its digest or layout.")

    now = utc_now()
    observations = [*(divergence.observations or [])]
    observations.append(
        {
            "raw_record_digest": payload.record.raw_record_digest,
            "disposition": payload.record.disposition,
            "observed_at": now.isoformat(),
            "kind": "FRESH_SOURCE_PROBE",
        }
    )
    divergence.observations = observations
    divergence.protected_new_raw_record = encrypt_text(payload.record.raw_record_b64)
    divergence.updated_at = now
    recent_probe_digests = [
        item.get("raw_record_digest")
        for item in observations
        if item.get("kind") == "FRESH_SOURCE_PROBE"
    ]

    if len(recent_probe_digests) >= 2 and recent_probe_digests[-2:] == [
        divergence.old_raw_digest,
        divergence.old_raw_digest,
    ]:
        divergence.state = "TRANSIENT_RECOVERED"
        divergence.resolved_at = now
        divergence.next_probe_at = None
        job.status = "QUEUED"
        job.phase = "RECOVERING_AFTER_INTERRUPTION"
        job.wait_reason = None
        job.next_retry_at = now
        job.auto_retry_count += 1
        _event(
            session,
            job,
            "SOURCE_DIVERGENCE_TRANSIENT_RECOVERED",
            {"divergence_id": divergence.divergence_id, "ordinal": divergence.ordinal},
        )
    elif (
        len(observations) >= 3
        and all(
            item.get("raw_record_digest") == divergence.new_raw_digest
            for item in observations[-3:]
        )
    ):
        _activate_recovery_epoch(
            session,
            job=job,
            divergence=divergence,
            now=now,
        )
    else:
        elapsed = (now - ensure_utc(divergence.created_at)).total_seconds()
        if elapsed >= 15 * 60:
            divergence.state = "UNSTABLE_REVIEW_REQUIRED"
            divergence.next_probe_at = None
            job.status = "NEEDS_ATTENTION"
            job.phase = "FINAL_ASSURANCE"
            job.wait_reason = "SOURCE_DIVERGENCE_UNSTABLE"
            job.error_code = "SOURCE_DIVERGENCE_UNSTABLE"
            job.error_message = (
                "Fresh terminal reads did not converge within the protected verification window."
            )
        else:
            probe_count = len(recent_probe_digests)
            delay = (5, 15, 30, 60)[min(probe_count, 3)]
            divergence.state = "PROBING"
            divergence.next_probe_at = now + timedelta(seconds=delay)
            job.status = "RUNNING"
            job.phase = "VERIFYING_SOURCE_CHANGE"
            job.wait_reason = "SOURCE_DIVERGENCE_PROBE_PENDING"
            job.next_retry_at = divergence.next_probe_at
    job.updated_at = now
    return job


def _activate_recovery_epoch(
    session: Session,
    *,
    job: ReconciliationJob,
    divergence: ReconciliationDivergence,
    now,
) -> None:
    old_epoch = session.get(TerminalSourceEpoch, job.source_epoch_id)
    recent_epoch_count = session.scalar(
        select(func.count(TerminalSourceEpoch.id)).where(
            TerminalSourceEpoch.zkt_device_id == job.zkt_device_id,
            TerminalSourceEpoch.created_at >= now - timedelta(hours=24),
            TerminalSourceEpoch.sequence > 1,
        )
    ) or 0
    if recent_epoch_count >= 3:
        divergence.state = "MUTATION_LIMIT_REVIEW_REQUIRED"
        divergence.resolved_at = now
        job.status = "NEEDS_ATTENTION"
        job.phase = "FINAL_ASSURANCE"
        job.wait_reason = "SOURCE_EPOCH_RECOVERY_LIMIT"
        job.error_code = "SOURCE_EPOCH_RECOVERY_LIMIT"
        job.error_message = "Terminal history changed repeatedly; automatic epoch recovery stopped."
        return
    sequence = (old_epoch.sequence if old_epoch else 0) + 1
    new_epoch = TerminalSourceEpoch(
        zkt_device_id=job.zkt_device_id,
        terminal_generation=job.terminal_generation,
        sequence=sequence,
        state="ACTIVE",
        parent_epoch_id=old_epoch.id if old_epoch else None,
        activated_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(new_epoch)
    session.flush()
    if old_epoch is not None:
        old_epoch.state = "SUPERSEDED"
        old_epoch.superseded_at = now
        old_epoch.updated_at = now
    prefix = session.scalars(
        select(TerminalRecordManifest).where(
            TerminalRecordManifest.zkt_device_id == job.zkt_device_id,
            TerminalRecordManifest.generation == job.terminal_generation,
            TerminalRecordManifest.source_epoch_id == divergence.source_epoch_id,
            TerminalRecordManifest.canonical_source == True,  # noqa: E712
            TerminalRecordManifest.ordinal < job.committed_next_ordinal,
        )
    ).all()
    for prior in prefix:
        session.add(
            TerminalRecordManifest(
                job_id=None,
                chunk_id=None,
                connector_id=prior.connector_id,
                zkt_device_id=prior.zkt_device_id,
                terminal_serial=prior.terminal_serial,
                generation=prior.generation,
                source_epoch_id=new_epoch.id,
                ordinal=prior.ordinal,
                source_kind="RECOVERY_PREFIX",
                canonical_source=True,
                record_size=prior.record_size,
                raw_record_digest=prior.raw_record_digest,
                terminal_record_key=prior.terminal_record_key,
                occurrence_index=prior.occurrence_index,
                attendance_event_id=prior.attendance_event_id,
                disposition=prior.disposition,
                protected_raw_record=prior.protected_raw_record,
                error_code=prior.error_code,
                raw_timestamp=prior.raw_timestamp,
                observed_uid=prior.observed_uid,
                observed_user_id=prior.observed_user_id,
            )
        )
    for coverage in session.scalars(
        select(ReconciliationCoverage).where(
            ReconciliationCoverage.zkt_device_id == job.zkt_device_id,
            ReconciliationCoverage.active == True,  # noqa: E712
        )
    ).all():
        coverage.active = False
        coverage.invalidated_reason = "SUPERSEDED_BY_CONFIRMED_SOURCE_MUTATION"
        coverage.invalidated_at = now
        coverage.updated_at = now
    job.source_epoch_id = new_epoch.id
    job.status = "QUEUED"
    job.phase = "RECOVERING_AFTER_SOURCE_CHANGE"
    job.wait_reason = None
    job.error_code = None
    job.error_message = None
    job.next_retry_at = now
    job.auto_retry_count += 1
    job.review_required = True
    job.completion_outcome = "CURRENT_TRUTH_CERTIFIED_WITH_SOURCE_CHANGE"
    divergence.state = "CONFIRMED_NEW_EPOCH"
    divergence.resolved_at = now
    divergence.next_probe_at = None
    _event(
        session,
        job,
        "SOURCE_RECOVERY_EPOCH_ACTIVATED",
        {
            "divergence_id": divergence.divergence_id,
            "source_epoch_id": new_epoch.epoch_id,
            "reused_prefix_through": job.committed_next_ordinal,
        },
    )


def _safety_hold(
    session: Session, job: ReconciliationJob, code: str, message: str
) -> ReconciliationJob:
    _release_assignment(job)
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


def _operator_status(job: ReconciliationJob, *, connected: bool) -> tuple[str, str]:
    if job.status in {"PAUSED", "PAUSE_REQUESTED"}:
        return (
            "PAUSED",
            "Reconciliation is durably paused. Its committed checkpoint is safe and will be used when an administrator resumes it.",
        )
    if job.status == "CANCELLED":
        return (
            "CANCELLED",
            "Reconciliation was cancelled without deleting its committed source evidence.",
        )
    if not connected or job.wait_reason == "WAITING_FOR_DEVICE":
        return (
            "WAITING_FOR_DEVICE",
            "Waiting for the device to reconnect. Committed progress is safe and will resume automatically.",
        )
    if job.phase == "VERIFYING_SOURCE_CHANGE":
        return (
            "VERIFYING_SOURCE_CHANGE",
            "ADD is confirming a terminal-history change with independent fresh reads.",
        )
    if job.phase in {"RECOVERING_AFTER_INTERRUPTION", "RECOVERING_AFTER_SOURCE_CHANGE"}:
        return (
            "RECOVERING",
            "Reconciliation is recovering automatically from its last durable checkpoint.",
        )
    if job.wait_reason in {"HISTORY_BACKLOG_BACKPRESSURE", "HISTORY_BACKLOG_CREDIT_EXHAUSTED"}:
        return (
            "WAITING_FOR_ORACLE_CAPACITY",
            "Terminal capture is temporarily paused while previously committed Oracle work drains.",
        )
    if job.status == "NEEDS_ATTENTION":
        return (
            "REVIEW_REQUIRED",
            job.error_message or "A correctness check needs administrator review; committed progress remains safe.",
        )
    if job.status == "COMPLETED" and job.review_required:
        return (
            "COMPLETED_WITH_REVIEW",
            "Current terminal truth is certified. A preserved historical source change remains available for review.",
        )
    if job.status == "COMPLETED":
        return ("COMPLETED", "Terminal source coverage and Oracle membership are certified.")
    if job.capture_certified_at is not None:
        return ("VERIFYING_ORACLE", "Terminal source capture is complete; ADD is proving Oracle membership.")
    return (
        "CAPTURING_SOURCE",
        "ADD is committing terminal source records in bounded restart-safe chunks.",
    )


def _eta(job: ReconciliationJob, *, chunks: int, connected: bool) -> dict:
    unavailable = None
    if job.status in TERMINAL_JOB_STATES:
        unavailable = job.status
    elif job.status in {"PAUSED", "PAUSE_REQUESTED", "NEEDS_ATTENTION"}:
        unavailable = job.wait_reason or job.status
    elif job.capture_certified_at is not None:
        # Source scan throughput does not measure the independent Oracle
        # delivery/check path, so projecting it as an Oracle ETA is misleading.
        unavailable = (
            "ORACLE_OUTCOME_REVIEW_REQUIRED"
            if job.ords_review_count
            else "ORACLE_PROGRESS_RATE_UNAVAILABLE"
        )
    elif not connected:
        unavailable = "WAITING_FOR_DEVICE"
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
