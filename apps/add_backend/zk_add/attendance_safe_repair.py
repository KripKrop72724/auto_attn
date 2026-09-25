"""Restart-safe, bounded checks and repairs of attendance already preserved in ADD.

Each tick is one database transaction. Row locks fence competing workers; no
network call or lease survives a commit. Delivery uses the existing durable
outbox, and only its verified Oracle receipt completes an item.
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.attendance_identity_evidence import identity_evidence, valid_cnic
from zk_add.attendance_recovery import (
    IDENTITY_LANE,
    SAFE_LANE,
    RecoveryError,
    _digest,
    _event_lane,
    sign_recovery_preview,
    verify_recovery_preview,
)
from zk_add.audit import append_audit
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    AttendanceRepairJob,
    AttendanceSafeRepairDecision as Decision,
    AttendanceSafeRepairTask as Task,
    Connector,
    DeviceUser,
    IdentityConflictResolution,
    OrdsOutbox,
)
from zk_add.ords_states import ORDS_ACKNOWLEDGED_STATUSES
from zk_add.service import ensure_attendance_ords_outbox, upsert_alert
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

ACTION = "SAFE_REPAIR"
POLICY = "safe-attendance-repair-v1"
ACTIVE = {"RUNNING", "WAITING_ORACLE", "PAUSED", "STOPPING"}
TERMINAL = {"COMPLETED", "COMPLETED_WITH_REVIEW", "STOPPED", "EXPIRED"}


def allowed(connector: Connector) -> bool:
    ids = {
        s.strip()
        for s in settings.attendance_safe_repair_allowed_connectors.split(",")
        if s.strip()
    }
    return settings.attendance_safe_repair_execution_enabled and (
        not ids or connector.connector_id in ids
    )


def source_digest(event: AttendanceEvent) -> str:
    return _digest(
        [
            event.event_uid,
            event.connector_id,
            event.zkt_device_id,
            event.device_serial,
            event.user_id,
            event.uid,
            ensure_utc(event.device_event_time).isoformat(),
            event.raw_event,
            event.effective_identity_revision_id,
        ]
    )


def classify(
    session: Session,
    event: AttendanceEvent,
    connector: Connector,
    outbox: OrdsOutbox | None,
    *,
    identity_possible: bool = True,
):
    evidence = None

    def prove_identity():
        nonlocal evidence
        if connector.firmware_family == "hikvision":
            return bool(event.cnic_lookup_hash and event.identity_content_status == "VERIFIED")
        if identity_possible:
            evidence = identity_evidence(session, event, connector)
        return evidence is not None

    lane, reason = _event_lane(session, event, outbox, connector, identity_prover=prove_identity)
    if event.oracle_confirmed_at and event.ords_status in ORDS_ACKNOWLEDGED_STATUSES:
        return "CONFIRMED", "Oracle has confirmed this attendance.", None, {}
    if lane == "CONFIRMED":
        return (
            "NEEDS_REVIEW",
            "Delivery says complete, but its confirmation needs checking.",
            None,
            {},
        )
    if event.identity_resolution_id:
        resolution = session.get(IdentityConflictResolution, event.identity_resolution_id)
        if not resolution or resolution.status != "ACTIVE":
            return "NEEDS_REVIEW", "The previous identity approval is no longer valid.", None, {}
    if evidence and event.cnic_lookup_hash and event.cnic_lookup_hash != evidence.cnic_hash:
        return (
            "NEEDS_REVIEW",
            "The saved CNIC conflicts with the employee evidence. Review is required.",
            None,
            {},
        )
    if lane != SAFE_LANE:
        reasons = {
            "IDENTITY_HELD": "We need proof of who used this employee number when the punch was made.",
            "SOURCE_CORRECTION": "The punch date or time needs checking.",
            "PERMANENT_REVIEW": "The saved device or delivery evidence needs checking.",
        }
        return "NEEDS_REVIEW", reasons.get(lane, reason), None, {}
    if not evidence and not valid_cnic(event.cnic_encrypted, event.cnic_lookup_hash):
        return "NEEDS_REVIEW", "The saved CNIC needs checking.", None, {}
    proof = (
        evidence.proof
        if evidence
        else {
            "kind": "EXISTING_VERIFIED",
            "cnic_hash": event.cnic_lookup_hash,
            "identity_status": event.identity_resolution_status,
            "resolution_id": event.identity_resolution_id,
        }
    )
    return (
        "READY",
        reason,
        evidence,
        {**proof, "source_digest": source_digest(event), "policy": POLICY},
    )


def create_check(
    session: Session, *, actor: str, key: str, connector_ids: list[str], automatic: bool = False
) -> Job:
    if automatic:
        raise RecoveryError("Automatic attendance repair is retired.", "REPAIR_RETIRED")
    if not settings.attendance_safe_repair_preview_enabled:
        raise RecoveryError("Attendance repair is not enabled yet.", "REPAIR_DISABLED")
    ids = sorted(set(connector_ids))
    scope = {"connector_ids": ids, "all_history": True, "automatic": automatic, "policy": POLICY}
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing:
        if (
            existing.actor != actor
            or existing.action != ACTION
            or existing.scope.get("request") != scope
        ):
            raise RecoveryError(
                "This request key belongs to a different check.", "IDEMPOTENCY_CONFLICT"
            )
        return existing
    query = select(Connector).order_by(Connector.id)
    if ids:
        query = query.where(Connector.connector_id.in_(ids))
    connectors = session.scalars(query).all()
    if ids and len(connectors) != len(ids):
        raise RecoveryError(
            "A selected device no longer exists. Check the selection again.", "SCOPE_CHANGED"
        )
    job = Job(
        action=ACTION,
        status="CHECKING",
        actor=actor,
        reason="Check and repair saved attendance",
        idempotency_key=key,
        scope={"request": scope},
        candidate_digest="0" * 64,
    )
    from sqlalchemy.exc import IntegrityError

    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.idempotency_key == key))
        if (
            existing
            and existing.actor == actor
            and existing.action == ACTION
            and existing.scope.get("request") == scope
        ):
            return existing
        raise RecoveryError(
            "This request key belongs to a different check.", "IDEMPOTENCY_CONFLICT"
        ) from None
    # One grouped query freezes the upper boundary before scanning any records.
    boundaries = dict(
        session.execute(
            select(AttendanceEvent.connector_id, func.max(AttendanceEvent.id)).group_by(
                AttendanceEvent.connector_id
            )
        ).all()
    )
    for connector in connectors:
        session.add(
            Task(
                job_id=job.id,
                connector_id=connector.id,
                terminal_serial=connector.zkt_device.confirmed_serial
                if connector.zkt_device
                else None,
                hardware_id=connector.hardware_id,
                high_water_id=boundaries.get(connector.id, 0),
            )
        )
    session.flush()
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_REPAIR_CHECK",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="QUEUED",
        after={"devices": len(connectors), "policy": POLICY},
    )
    return job


def _task_matches(task: Task, connector: Connector) -> bool:
    zkt = connector.zkt_device
    return connector.hardware_id == task.hardware_id and bool(
        zkt and zkt.confirmed_serial == task.terminal_serial and zkt.serial == task.terminal_serial
    )


def _scan(session: Session, job: Job, limit: int) -> int:
    task = session.scalar(
        select(Task)
        .where(Task.job_id == job.id, Task.status == "CHECKING")
        .order_by(Task.updated_at, Task.id)
        .limit(1)
        .with_for_update()
    )
    if task:
        connector = session.get(Connector, task.connector_id)
        events = session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.connector_id == task.connector_id,
                AttendanceEvent.id > task.cursor,
                AttendanceEvent.id <= task.high_water_id,
                # Retain anomalous ACK rows lacking receipt for explicit review.
                (
                    AttendanceEvent.oracle_confirmed_at.is_(None)
                    | AttendanceEvent.ords_status.not_in(ORDS_ACKNOWLEDGED_STATUSES)
                ),
            )
            .order_by(AttendanceEvent.id)
            .limit(limit)
        ).all()
        from zk_add.models import AttendanceIdentityHistory, DeviceUser

        user_ids = {event.user_id for event in events}
        # Bulk negative lookup avoids thousands of individual queries for an
        # unknown employee number. Positive matches still use the full evaluator.
        known_users = (
            set(
                session.scalars(
                    select(DeviceUser.user_id).where(
                        DeviceUser.zkt_device_id == connector.zkt_device.id,
                        DeviceUser.user_id.in_(user_ids),
                    )
                ).all()
            )
            if connector.zkt_device
            else set()
        )
        known_users.update(
            session.scalars(
                select(AttendanceIdentityHistory.user_id)
                .where(
                    AttendanceIdentityHistory.zkt_device_id == connector.zkt_device.id,
                    AttendanceIdentityHistory.user_id.in_(user_ids),
                )
                .distinct()
            ).all()
            if connector.zkt_device
            else []
        )
        outboxes = {
            row.attendance_event_id: row
            for row in session.scalars(
                select(OrdsOutbox).where(
                    OrdsOutbox.attendance_event_id.in_([event.id for event in events])
                )
            ).all()
        }
        for event in events:
            outbox = outboxes.get(event.id)
            state, reason, _evidence, proof = classify(
                session, event, connector, outbox, identity_possible=event.user_id in known_users
            )
            if not _task_matches(task, connector):
                state, reason, proof = (
                    "NEEDS_REVIEW",
                    "The terminal attached to this device changed.",
                    {},
                )
            token = _digest([state, proof, source_digest(event)])
            if not job.scope["request"]["automatic"] or state == "READY":
                session.add(
                    Item(
                        job_id=job.id,
                        source_kind="ATTENDANCE_EVENT",
                        source_ref=str(event.id),
                        attendance_event_id=event.id,
                        connector_id=connector.id,
                        expected_state_digest=token,
                        status=state,
                        lane=SAFE_LANE if state == "READY" else IDENTITY_LANE,
                        result={
                            "reason": reason,
                            "proof": proof,
                            "source_digest": source_digest(event),
                        },
                    )
                )
            elif state != "CONFIRMED":
                job.review_count += 1
            job.requested_count += 1
            task.evidence_digest = _digest([task.evidence_digest, event.id, token])
            task.checked_count += 1
            task.cursor = event.id
        task.updated_at = utc_now()
        if len(events) < limit:
            task.status = "CHECKED"
        session.flush()
    pending = session.scalar(
        select(Task.id).where(Task.job_id == job.id, Task.status == "CHECKING").limit(1)
    )
    if not pending:
        tasks = session.scalars(select(Task).where(Task.job_id == job.id).order_by(Task.id)).all()
        job.candidate_digest = _digest(
            [
                POLICY,
                job.job_id,
                [
                    [
                        t.connector_id,
                        t.hardware_id,
                        t.terminal_serial,
                        t.high_water_id,
                        t.evidence_digest,
                    ]
                    for t in tasks
                ],
            ]
        )
        job.status = "CHECKED"
        job.last_error = None
        job.preview_expires_at = utc_now() + timedelta(minutes=15)
        job.requested_count = sum(t.checked_count for t in tasks)
        job.eligible_count = (
            session.scalar(
                select(func.count(Item.id)).where(Item.job_id == job.id, Item.status == "READY")
            )
            or 0
        )
        job.review_count = job.requested_count - job.eligible_count
    return len(events) if task else 0


def start_check(
    session: Session, job: Job, *, actor: str, signature: str, automatic: bool = False
) -> Job:
    if job.actor != actor or job.action != ACTION:
        raise RecoveryError("Open a check made with your admin account.", "CHECK_ACTOR_MISMATCH")
    if job.status in ACTIVE | TERMINAL:
        return job  # The check itself is the stable start idempotency key.
    if job.status != "CHECKED":
        raise RecoveryError("The check is still running.", "CHECK_INCOMPLETE")
    if not automatic:
        verify_recovery_preview(
            digest=job.candidate_digest,
            expires_at=job.preview_expires_at,
            actor=actor,
            action=ACTION,
            signature=signature,
        )
    else:
        raise RecoveryError("Automatic repair has been removed.", "REPAIR_RETIRED")
    tasks = session.scalars(
        select(Task).where(Task.job_id == job.id).order_by(Task.connector_id)
    ).all()
    actionable = set(
        session.scalars(
            select(Item.connector_id)
            .where(Item.job_id == job.id, Item.status.in_({"READY", "WAITING_ORACLE"}))
            .distinct()
        ).all()
    )
    # Stable connector locks serialize overlapping approvals.
    for task in tasks:
        if task.connector_id not in actionable:
            continue
        connector = session.scalar(
            select(Connector).where(Connector.id == task.connector_id).with_for_update()
        )
        if not allowed(connector) or not _task_matches(task, connector):
            raise RecoveryError(
                "This device is not enabled for repair, or its terminal changed. Run a new check.",
                "SCOPE_CHANGED",
            )
        overlapping = session.scalar(
            select(Job.id)
            .join(Item, Item.job_id == Job.id)
            .where(
                Item.connector_id == connector.id,
                Item.status.in_({"READY", "WAITING_ORACLE"}),
                Job.id != job.id,
                Job.action == ACTION,
                Job.status.in_(ACTIVE),
            )
            .limit(1)
        )
        legacy = session.scalar(
            select(AttendanceRepairJob.id)
            .where(
                AttendanceRepairJob.connector_id == connector.id,
                AttendanceRepairJob.status.not_in(
                    {"COMPLETED", "COMPLETED_WITH_ATTENTION", "CANCELLED"}
                ),
            )
            .limit(1)
        )
        if overlapping or legacy:
            raise RecoveryError(
                "Another repair is already working on a selected device. Open that run first.",
                "REPAIR_ALREADY_RUNNING",
            )
    job.status = "RUNNING"
    job.started_at = job.updated_at = utc_now()
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_SAFE_REPAIR_START",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="SUCCESS",
        after={"digest": job.candidate_digest, "eligible": job.eligible_count},
    )
    return job


def _apply(session: Session, job: Job, item: Item) -> None:
    event = session.scalar(
        select(AttendanceEvent)
        .where(AttendanceEvent.id == item.attendance_event_id)
        .with_for_update()
    )
    connector = session.get(Connector, item.connector_id)
    task = session.scalar(
        select(Task).where(Task.job_id == job.id, Task.connector_id == item.connector_id)
    )
    outbox = session.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id).with_for_update()
    )
    state, reason, evidence, proof = classify(session, event, connector, outbox)
    if state == "CONFIRMED":
        item.status, item.outcome = "CONFIRMED", "ORACLE_CONFIRMED"
        return
    if not allowed(connector) or not _task_matches(task, connector):
        item.status, item.result = (
            "NEEDS_REVIEW",
            {"reason": "Repair is paused for this device, or its terminal changed."},
        )
        return
    if _digest([state, proof, source_digest(event)]) != item.expected_state_digest:
        item.status, item.result = (
            "NEEDS_REVIEW",
            {"reason": "The evidence changed after the check. Run a new check."},
        )
        return
    # Never reset an active request or a retry's backoff; observe its receipt instead.
    if outbox and outbox.status == "IN_FLIGHT":
        item.status, item.outcome = "WAITING_ORACLE", "OBSERVING_EXISTING_DELIVERY"
        item.result = {
            "reason": "A delivery is already in progress. Waiting for its Oracle confirmation."
        }
        return
    prior = {
        "identity_status": event.identity_resolution_status,
        "device_user_id": event.device_user_id,
        "cnic_hash": event.cnic_lookup_hash,
        "ords_status": event.ords_status,
    }
    if evidence:
        event.device_user_id = evidence.user_id
        event.cnic_encrypted, event.cnic_lookup_hash = evidence.encrypted_cnic, evidence.cnic_hash
        event.cnic_last4 = None  # Derived safely from the validated ciphertext below.
        from zk_add.crypto import decrypt_cnic

        event.cnic_last4 = decrypt_cnic(evidence.encrypted_cnic)[-4:]
        event.display_name = evidence.display_name
        event.identity_resolution_status = "RESOLVED_RETAINED_IDENTITY"
        event.identity_repaired_at = event.identity_resolved_at = utc_now()
        event.identity_repair_reason = "SAFE_REPAIR_VERIFIED_EVIDENCE"
    if connector.firmware_family == "hikvision" and event.identity_content_status == "VERIFIED":
        event.identity_resolution_status = "RESOLVED_HIKVISION_NAME"
    session.add(
        Decision(
            item_id=item.id,
            attendance_event_id=event.id,
            actor=job.actor,
            proof=proof,
            prior_state=prior,
        )
    )
    if not outbox or outbox.status not in {"IN_FLIGHT", "PENDING", "FAILED_RETRYABLE"}:
        event.ords_status = "PENDING"
        if outbox:
            outbox.status, outbox.next_attempt_at, outbox.last_error = "PENDING", None, None
        else:
            outbox, _created = ensure_attendance_ords_outbox(session, event)
    if outbox and outbox.status != "IN_FLIGHT":
        outbox.delivery_type = "FULL_HISTORY"
    item.status, item.outcome = "WAITING_ORACLE", "QUEUED_FOR_DELIVERY"
    item.result = {"reason": "Saved for delivery. Waiting for Oracle confirmation.", "proof": proof}
    item.updated_at = utc_now()
    append_audit(
        session,
        actor=job.actor,
        action="ATTENDANCE_SAFE_REPAIR_EVENT",
        target_type="attendance_event",
        target_id=event.event_uid,
        outcome="QUEUED",
        after={"job_id": job.job_id, "proof_digest": _digest(proof)},
    )


def delivery_proof_valid(session: Session, event: AttendanceEvent, connector: Connector) -> bool:
    """Final pre-send check for repaired identity; changing evidence cannot silently pass."""
    if event.identity_resolution_status == "RESOLVED_SYNCED_CNIC":
        from zk_add.service import synced_cnic_identity_proven

        zkt = connector.zkt_device
        user = session.get(DeviceUser, event.device_user_id) if event.device_user_id else None
        return bool(
            zkt and user and event.identity_snapshot_id == zkt.identity_snapshot_id
            and event.cnic_lookup_hash == user.cnic_lookup_hash
            and synced_cnic_identity_proven(zkt, user, event)
        )
    if event.identity_resolution_status != "RESOLVED_RETAINED_IDENTITY":
        return True
    decision = session.scalar(
        select(Decision)
        .where(Decision.attendance_event_id == event.id)
        .order_by(Decision.id.desc())
        .limit(1)
    )
    evidence = identity_evidence(session, event, connector)
    return bool(
        decision
        and evidence
        and evidence.cnic_hash == event.cnic_lookup_hash
        and decision.proof
        == {**evidence.proof, "source_digest": source_digest(event), "policy": POLICY}
    )


def _observe(session: Session, job: Job, limit: int) -> int:
    progressed = 0
    waiting = session.scalars(
        select(Item)
        .where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        .order_by(Item.updated_at, Item.id)
        .limit(limit)
    ).all()
    for item in waiting:
        event = session.get(AttendanceEvent, item.attendance_event_id)
        if event.oracle_confirmed_at and event.ords_status in ORDS_ACKNOWLEDGED_STATUSES:
            progressed += 1
            item.status, item.outcome = "CONFIRMED", "ORACLE_CONFIRMED"
            item.completed_at = utc_now()
            item.result = {
                **item.result,
                "reason": "Oracle has confirmed this attendance.",
                "confirmed_at": ensure_utc(event.oracle_confirmed_at).isoformat(),
            }
            job.scope = {**job.scope, "last_progress_at": utc_now().isoformat()}
        elif event.ords_status not in {"PENDING", "IN_FLIGHT", "FAILED_RETRYABLE", "RETRYING"}:
            item.status = "NEEDS_REVIEW"
            item.result = {
                **item.result,
                "reason": "Delivery needs attention. The saved punch is preserved.",
            }
        item.updated_at = utc_now()
    session.flush()
    return progressed


def counts(session: Session, job: Job) -> dict:
    values = dict(
        session.execute(
            select(Item.status, func.count(Item.id))
            .where(Item.job_id == job.id)
            .group_by(Item.status)
        ).all()
    )
    return {
        "checked": job.requested_count
        if job.scope["request"]["automatic"]
        else sum(values.values()),
        "ready": values.get("READY", 0),
        "waiting": values.get("WAITING_ORACLE", 0),
        "confirmed": values.get("CONFIRMED", 0),
        "review": values.get("NEEDS_REVIEW", 0)
        + (job.review_count if job.scope["request"]["automatic"] else 0),
        "stopped": values.get("STOPPED", 0),
    }


def advance_once(session: Session) -> int:
    if not settings.attendance_safe_repair_preview_enabled:
        return 0
    job = session.scalar(
        select(Job)
        .where(
            Job.action == ACTION,
            Job.status.in_({"CHECKING", "RUNNING", "WAITING_ORACLE", "STOPPING", "PAUSED"}),
        )
        .order_by(Job.updated_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not job:
        return 0
    if job.scope.get("request", {}).get("automatic"):
        job.status = "STOPPING"
    limit = settings.attendance_safe_repair_batch_size
    progress = 0
    if job.status == "CHECKING":
        progress = _scan(session, job, limit)

    else:
        progress = _observe(session, job, limit)
        if job.status == "STOPPING":
            for item in session.scalars(
                select(Item)
                .where(Item.job_id == job.id, Item.status == "READY")
                .order_by(Item.id)
                .limit(limit)
            ).all():
                item.status = "STOPPED"
                item.result = {"reason": "Stopped before delivery. The saved punch is preserved."}
        if job.status == "RUNNING" and settings.attendance_safe_repair_execution_enabled:
            # Oldest updated device first, then event cursor: one busy zone cannot monopolize the run.
            tasks = (
                select(Task.connector_id)
                .where(Task.job_id == job.id)
                .order_by(Task.updated_at, Task.id)
            )
            for connector_id in session.scalars(tasks).all():
                items = session.scalars(
                    select(Item)
                    .where(
                        Item.job_id == job.id,
                        Item.connector_id == connector_id,
                        Item.status == "READY",
                    )
                    .order_by(Item.id)
                    .limit(limit)
                ).all()
                if items:
                    for item in items:
                        _apply(session, job, item)
                    task = session.scalar(
                        select(Task).where(Task.job_id == job.id, Task.connector_id == connector_id)
                    )
                    task.updated_at = utc_now()
                    progress += len(items)
                    job.scope = {**job.scope, "last_progress_at": utc_now().isoformat()}
                    break
        session.flush()
        totals = counts(session, job)
        if not totals["ready"]:
            if totals["waiting"]:
                job.status = (
                    job.status if job.status in {"STOPPING", "PAUSED"} else "WAITING_ORACLE"
                )
            else:
                job.status = (
                    "STOPPED"
                    if job.status == "STOPPING"
                    else ("COMPLETED_WITH_REVIEW" if totals["review"] else "COMPLETED")
                )
                job.completed_at = utc_now()
        last = job.scope.get("last_progress_at")
        from datetime import datetime

        last_at = datetime.fromisoformat(last) if last else job.started_at
        if (
            job.status not in TERMINAL
            and last_at
            and utc_now() - ensure_utc(last_at) > timedelta(minutes=10)
        ):
            job.last_error = "No repair progress for 10 minutes. Saved attendance is preserved; check delivery and worker health."
            for connector_id in session.scalars(
                select(Item.connector_id)
                .where(Item.job_id == job.id, Item.status.in_({"READY", "WAITING_ORACLE"}))
                .distinct()
            ).all():
                upsert_alert(
                    session,
                    session.get(Connector, connector_id),
                    code="ATTENDANCE_REPAIR_STALLED",
                    severity="WARNING",
                    message=job.last_error,
                    details={"job_id": job.job_id},
                )
    if progress or job.status in TERMINAL:
        job.last_error = None
        from zk_add.models import DeviceAlert

        for alert in session.scalars(
            select(DeviceAlert).where(
                DeviceAlert.code == "ATTENDANCE_REPAIR_STALLED", DeviceAlert.state == "OPEN"
            )
        ).all():
            if (alert.details or {}).get("job_id") == job.job_id:
                alert.state = "RESOLVED"
                alert.resolved_at = utc_now()
    job.updated_at = utc_now()
    return progress


def control(session: Session, job: Job, *, action: str, actor: str) -> Job:
    if job.action != ACTION:
        raise RecoveryError("Use the original controls for this older recovery run.")
    if (
        (action == "PAUSE" and job.status == "PAUSED")
        or (action == "RESUME" and job.status in {"RUNNING", "WAITING_ORACLE"})
        or (action == "STOP" and job.status in {"STOPPING", "STOPPED"})
    ):
        return job
    if action == "PAUSE" and job.status in {"RUNNING", "WAITING_ORACLE"}:
        job.status = "PAUSED"
    elif action == "RESUME" and job.status == "PAUSED":
        job.status = "RUNNING"
    elif action == "STOP" and job.status in ACTIVE:
        job.status = "STOPPING"
        # Bounded workers retire unsent items after stop; receipt observation continues.
    elif job.status not in TERMINAL:
        raise RecoveryError("This action is not available for the current run.")
    job.updated_at = utc_now()
    append_audit(
        session,
        actor=actor,
        action=f"ATTENDANCE_SAFE_REPAIR_{action}",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome=job.status,
    )
    return job


def serialize(session: Session, job: Job, actor: str) -> dict:
    tasks = session.execute(
        select(Task, Connector)
        .join(Connector, Connector.id == Task.connector_id)
        .where(Task.job_id == job.id)
        .order_by(Task.id)
    ).all()
    actionable = set(
        session.scalars(
            select(Item.connector_id)
            .where(Item.job_id == job.id, Item.status == "READY")
            .distinct()
        ).all()
    )
    result = {
        "job_id": job.job_id,
        "action": ACTION,
        "automatic": job.scope["request"]["automatic"],
        "status": job.status,
        "actor": job.actor,
        "counts": counts(session, job),
        "check_complete": job.status != "CHECKING",
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
        "last_error": job.last_error
        or (
            "No update has been received for 10 minutes. ADD will resume from its saved checkpoint when the worker recovers."
            if job.status in ACTIVE | {"CHECKING"}
            and utc_now() - ensure_utc(job.updated_at) > timedelta(minutes=10)
            else None
        ),
        "eligible_at_check": job.eligible_count,
        "expires_at": job.preview_expires_at,
        "execution_enabled": settings.attendance_safe_repair_execution_enabled
        and all(allowed(c) and _task_matches(t, c) for t, c in tasks if c.id in actionable),
        "devices": [
            {
                "connector_id": c.connector_id,
                "name": c.display_name,
                "serial": t.terminal_serial,
                "checked": t.checked_count,
                "status": t.status,
            }
            for t, c in tasks
        ],
        "signature": None,
        "candidate_digest": job.candidate_digest,
    }
    if (
        job.status == "CHECKED"
        and actor == job.actor
        and ensure_utc(job.preview_expires_at) > utc_now()
    ):
        result["signature"] = sign_recovery_preview(
            digest=job.candidate_digest,
            expires_at=job.preview_expires_at,
            actor=actor,
            action=ACTION,
        )
    return result


def items_page(
    session: Session, job: Job, *, after: int = 0, limit: int = 50, state: str | None = None
) -> dict:
    query = (
        select(Item, AttendanceEvent, Connector)
        .join(AttendanceEvent, AttendanceEvent.id == Item.attendance_event_id)
        .join(Connector, Connector.id == Item.connector_id)
        .where(Item.job_id == job.id, Item.id > after)
    )
    if state:
        query = query.where(Item.status == state)
    rows = session.execute(query.order_by(Item.id).limit(limit + 1)).all()
    return {
        "rows": [
            {
                "id": i.id,
                "event_id": e.id,
                "event_uid": e.event_uid,
                "status": i.status,
                "reason": i.result.get("reason"),
                "name": e.display_name,
                "user_id": e.user_id,
                "connector_id": c.connector_id,
                "device_name": c.display_name,
                "device_serial": e.device_serial,
                "time": e.device_event_time,
            }
            for i, e, c in rows[:limit]
        ],
        "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
    }




def tick() -> None:
    from zk_add.db import session_scope

    try:
        with session_scope() as session:
            advance_once(session)
    except Exception:
        # A failed transaction retains its cursor. Surface a durable error if
        # the database is available; never copy exception text containing PII.
        with session_scope() as session:
            job = session.scalar(
                select(Job)
                .where(Job.action == ACTION, Job.status.in_(ACTIVE | {"CHECKING"}))
                .order_by(Job.updated_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job:
                job.last_error = "The repair worker could not finish its last step. ADD will retry the saved checkpoint."
                job.updated_at = utc_now()
        raise


def delivery_coverage(session: Session, connector_id: str | None = None) -> dict:
    """Unique saved events, independent of historical reconciliation certificates."""
    from sqlalchemy import case
    from zk_add.ords_states import ORDS_ACTIVE_STATUSES, ORDS_IDENTITY_HELD_STATUSES

    confirmed = AttendanceEvent.oracle_confirmed_at.is_not(None) & AttendanceEvent.ords_status.in_(
        ORDS_ACKNOWLEDGED_STATUSES
    )
    query = select(
        func.count(AttendanceEvent.id),
        func.sum(case((confirmed, 1), else_=0)),
        func.sum(
            case(
                (AttendanceEvent.ords_status.in_(ORDS_IDENTITY_HELD_STATUSES) & ~confirmed, 1),
                else_=0,
            )
        ),
        func.sum(
            case((AttendanceEvent.ords_status.in_(ORDS_ACTIVE_STATUSES) & ~confirmed, 1), else_=0)
        ),
    )
    if connector_id:
        query = query.join(Connector, Connector.id == AttendanceEvent.connector_id).where(
            Connector.connector_id == connector_id
        )
    total, accepted, held, pending = (int(v or 0) for v in session.execute(query).one())
    return {
        "total": total,
        "confirmed": accepted,
        "identity_held": held,
        "pending": pending,
        "review": total - accepted - held - pending,
        "observed_at": utc_now(),
        "scope": "ALL_SAVED_ATTENDANCE",
        "connector_id": connector_id,
    }


def assert_no_active_safe_repair(session: Session, connector_id: int) -> None:
    # All operator entry points take this same connector lock before checking.
    session.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
    if session.scalar(
        select(Job.id)
        .join(Item, Item.job_id == Job.id)
        .where(
            Item.connector_id == connector_id,
            Item.status.in_({"READY", "WAITING_ORACLE"}),
            Job.action == ACTION,
            Job.status.in_(ACTIVE),
        )
        .limit(1)
    ):
        raise RecoveryError(
            "A saved attendance repair is running on this device. Stop that run before changing its identity decisions.",
            "REPAIR_ALREADY_RUNNING",
        )
