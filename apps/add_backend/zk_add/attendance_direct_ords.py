"""Durable, explicitly approved delivery of selected saved punches to Oracle.

This operator action overrides ADD's delivery holds. It never invents a user,
CNIC or source fact, and the ordinary Oracle content check still decides ACKED.
"""

from uuid import uuid4

from sqlalchemy import func, select

from zk_add.attendance_force_release import _lock_scheduler
from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.attendance_repair import _immutable_facts
from zk_add.audit import append_audit
from zk_add.crypto import decrypt_cnic, decrypt_json, encrypt_json, encrypt_text, normalize_cnic
from zk_add.db import session_scope
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    Connector,
    DeviceUser,
    OrdsOutbox,
    ZKTDevice,
)
from zk_add.ords_states import ORDS_ACKNOWLEDGED_STATUSES
from zk_add.service import oracle_payload
from zk_add.time_utils import utc_now


ACTION = "MANUAL_DIRECT_ORDS"
POLICY = "manual-direct-ords-v1"
BATCH = 100
ACTIVE = {"RUNNING", "WAITING_ORACLE"}


def _source_digest(event: AttendanceEvent, connector: Connector) -> str:
    return _digest([
        _immutable_facts(event), event.raw_event, event.connector_id,
        event.zkt_device_id, event.device_user_id, event.cnic_encrypted,
        event.display_name, connector.hardware_id,
    ])


def _known_user_and_cnic(session, event):
    users = session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == event.zkt_device_id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
        ).limit(2)
    ).all()
    if len(users) != 1:
        return None, None, "UNKNOWN_USER"
    try:
        cnic = normalize_cnic(decrypt_cnic(event.cnic_encrypted))
        current_cnic = normalize_cnic(decrypt_cnic(users[0].cnic_encrypted))
    except Exception:
        cnic, current_cnic = None, None
    if not cnic or not current_cnic:
        return users[0], None, "CNIC_MISSING"
    return users[0], cnic, None


def _payload(connector, terminal, event, user, cnic):
    payload = oracle_payload(connector, terminal, event, cnic)
    payload["device_serial"] = event.device_serial or "unknown"
    payload["employee_name"] = event.display_name or user.display_name
    return payload


def create(session, *, actor: str, request):
    _lock_scheduler(session)
    event_ids = sorted(request.event_ids)
    request_digest = _digest([ACTION, actor, event_ids, request.reason.strip()])
    previous = session.scalar(select(Job).where(Job.idempotency_key == request.idempotency_key))
    if previous:
        if previous.action != ACTION or previous.actor != actor or previous.candidate_digest != request_digest:
            raise RecoveryError("This request key belongs to another selection.", "IDEMPOTENCY_CONFLICT")
        return previous
    rows = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(event_ids))
        .order_by(AttendanceEvent.id).with_for_update()
    ).all()
    if len(rows) != len(event_ids):
        raise RecoveryError("One of the selected punches is no longer saved.", "SOURCE_MISSING")
    now = utc_now()
    job = Job(
        action=ACTION, status="RUNNING", actor=actor,
        reason="Administrator-selected Oracle delivery",
        idempotency_key=request.idempotency_key,
        scope={
            "policy": POLICY,
            "selected_event_ids": event_ids,
            "approval": {"at": now.isoformat(), "reason_encrypted": encrypt_text(request.reason.strip())},
        },
        candidate_digest=request_digest,
        requested_count=len(rows), eligible_count=0, excluded_count=0,
        started_at=now, updated_at=now,
    )
    session.add(job)
    session.flush()
    accepted = 0
    for event in rows:
        connector = session.get(Connector, event.connector_id)
        terminal = session.get(ZKTDevice, event.zkt_device_id)
        user, cnic, error = _known_user_and_cnic(session, event)
        outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id))
        if event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
            error = "ALREADY_CONFIRMED"
        elif decision_for(session, event) or (outbox and outbox.status == "IN_FLIGHT"):
            error = "ALREADY_DELIVERING"
        elif not connector or not terminal:
            error = "SOURCE_MISSING"
        elif not error:
            accepted += 1
        payload = (
            _payload(connector, terminal, event, user, cnic)
            if error is None else None
        )
        session.add(Item(
            job_id=job.id, source_kind="ATTENDANCE_EVENT", source_ref=str(event.id),
            connector_id=event.connector_id, attendance_event_id=event.id,
            expected_state_digest=_source_digest(event, connector) if connector else _digest(event.id),
            lane="DIRECT_ORDS",
            status="READY" if payload else "SKIPPED",
            error_code=error,
            result={
                "payload_encrypted": encrypt_json(payload) if payload else None,
                "payload_digest": _digest(payload) if payload else None,
                "reason": error.replace("_", " ").title() if error else "Approved; waiting to prepare delivery.",
            },
            completed_at=now if error else None,
        ))
    job.eligible_count = accepted
    job.excluded_count = len(rows) - accepted
    if accepted == 0:
        job.status, job.completed_at = "COMPLETED_WITH_REVIEW", now
    append_audit(
        session, actor=actor, action="ATTENDANCE_DIRECT_ORDS_APPROVED",
        target_type="attendance_recovery_job", target_id=job.job_id,
        outcome="APPROVED", after={"selected": len(rows), "ready": accepted, "digest": request_digest},
    )
    return job


def approved_payload(session, event, connector, decision):
    if decision.proof.get("policy") != POLICY or connector is None:
        return None
    if _source_digest(event, connector) != decision.proof.get("source_digest"):
        return None
    _user, _cnic, error = _known_user_and_cnic(session, event)
    if error:
        return None
    try:
        payload = decrypt_json(decision.payload_encrypted)
    except Exception:
        return None
    return payload if _digest(payload) == decision.payload_digest else None


def _queue(session, job, item):
    outbox = session.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == item.attendance_event_id)
        .with_for_update()
    )
    event = session.scalar(
        select(AttendanceEvent).where(AttendanceEvent.id == item.attendance_event_id)
        .with_for_update()
    )
    connector = session.get(Connector, event.connector_id) if event else None
    if not event or not connector or item.expected_state_digest != _source_digest(event, connector):
        code = "SOURCE_CHANGED"
    elif event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
        code = "ALREADY_CONFIRMED"
    elif decision_for(session, event):
        code = "ALREADY_DELIVERING"
    elif outbox and outbox.status == "IN_FLIGHT":
        return  # Let the earlier claim settle; the next short tick will recheck.
    else:
        user, cnic, code = _known_user_and_cnic(session, event)
    if code:
        item.status, item.error_code, item.completed_at = "SKIPPED", code, utc_now()
        item.result = {"reason": code.replace("_", " ").title()}
        return
    try:
        payload = decrypt_json(item.result["payload_encrypted"])
    except Exception:
        item.status, item.error_code, item.completed_at = (
            "NEEDS_REVIEW", "APPROVAL_PAYLOAD_UNREADABLE", utc_now()
        )
        item.result = {"reason": "The saved approval cannot be read. No Oracle send was made."}
        return
    if payload != _payload(connector, session.get(ZKTDevice, event.zkt_device_id), event, user, cnic):
        item.status, item.error_code, item.completed_at = "SKIPPED", "SOURCE_CHANGED", utc_now()
        item.result = {"reason": "Saved punch or employee changed before delivery."}
        return
    decision = Decision(
        item_id=item.id, attendance_event_id=event.id, job_id=job.id,
        actor=job.actor, reason_encrypted=job.scope["approval"]["reason_encrypted"],
        proof={"policy": POLICY, "source_digest": item.expected_state_digest,
               "immutable_facts": _immutable_facts(event), "hardware_id": connector.hardware_id,
               "terminal": event.device_serial},
        prior_state_encrypted=encrypt_json({
            "ords_status": event.ords_status, "manual_release_required": event.manual_release_required,
        }),
        payload_encrypted=item.result["payload_encrypted"],
        payload_digest=item.result["payload_digest"], operation_id=str(uuid4()),
    )
    session.add(decision)
    event.manual_release_required = True
    event.ords_status = "PENDING"
    if outbox is None:
        outbox = OrdsOutbox(attendance_event_id=event.id)
        session.add(outbox)
    outbox.status, outbox.delivery_type = "PENDING", "FULL_HISTORY"
    outbox.next_attempt_at, outbox.last_error = None, None
    outbox.payload_hash = decision.payload_digest
    item.status, item.error_code = "WAITING_ORACLE", None
    item.result = {"reason": "Waiting for Oracle confirmation."}
    audit = append_audit(
        session, actor=job.actor, action="ATTENDANCE_DIRECT_ORDS_QUEUED",
        target_type="attendance_event", target_id=event.event_uid,
        outcome="WAITING_ORACLE", after={"job_id": job.job_id, "payload_digest": decision.payload_digest},
    )
    decision.audit_id = audit.id


def _observe(session, job):
    waiting = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        .order_by(Item.id).limit(BATCH)
    ).all()
    for item in waiting:
        event = session.get(AttendanceEvent, item.attendance_event_id)
        if not event:
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", "SOURCE_MISSING", utc_now()
        elif event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
            decision = decision_for(session, event)
            if decision and item.result.get("oracle_verified_payload_digest") == decision.payload_digest:
                item.status, item.completed_at = "CONFIRMED", utc_now()
        elif event.ords_status not in {"PENDING", "IN_FLIGHT", "FAILED_RETRYABLE", "RETRYING"}:
            item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", event.ords_status, utc_now()
        item.updated_at = utc_now()


def advance_once(session):
    _lock_scheduler(session)
    job = session.scalar(
        select(Job).where(Job.action == ACTION, Job.status.in_(ACTIVE))
        .order_by(Job.updated_at, Job.id).limit(1).with_for_update(skip_locked=True)
    )
    if not job:
        return
    ready = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.status == "READY")
        .order_by(Item.id).limit(BATCH)
    ).all()
    for item in ready:
        _queue(session, job, item)
    _observe(session, job)
    session.flush()
    counts = dict(session.execute(
        select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
    ).all())
    job.succeeded_count = counts.get("CONFIRMED", 0)
    job.skipped_count = counts.get("SKIPPED", 0)
    job.failed_count = counts.get("NEEDS_REVIEW", 0)
    if counts.get("READY", 0):
        job.status = "RUNNING"
    elif counts.get("WAITING_ORACLE", 0):
        job.status = "WAITING_ORACLE"
    else:
        job.status = "COMPLETED_WITH_REVIEW" if job.failed_count or job.skipped_count else "COMPLETED"
        job.completed_at = utc_now()
    job.updated_at = utc_now()


def tick():
    with session_scope() as session:
        advance_once(session)


def serialize(session, job):
    counts = dict(session.execute(
        select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
    ).all())
    waiting_attention = sum(
        bool(result.get("needs_attention"))
        for result in session.scalars(
            select(Item.result).where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        ).all()
    )
    return {
        "job_id": job.job_id, "status": job.status, "created_at": job.created_at,
        "approved_at": job.scope["approval"]["at"], "actor": job.actor,
        "selected": job.requested_count, "ready": counts.get("READY", 0),
        "waiting": counts.get("WAITING_ORACLE", 0), "confirmed": counts.get("CONFIRMED", 0),
        "skipped": counts.get("SKIPPED", 0),
        "attention": counts.get("NEEDS_REVIEW", 0) + waiting_attention,
        "completed_at": job.completed_at,
    }


def items_page(session, job, *, cursor: int = 0, limit: int = 50):
    rows = session.scalars(
        select(Item).where(Item.job_id == job.id, Item.id > cursor)
        .order_by(Item.id).limit(limit + 1)
    ).all()
    return {
        "rows": [
            {"item_id": row.item_id, "attendance_event_id": row.attendance_event_id,
             "status": "NEEDS_ATTENTION" if row.status == "WAITING_ORACLE" and row.result.get("needs_attention") else row.status,
             "reason": row.result.get("reason"),
             "error_code": row.error_code}
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }
