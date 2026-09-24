"""Durable, explicitly approved delivery of selected saved punches to Oracle.

This operator action overrides ADD's delivery holds. It never invents a user,
CNIC or source fact, and the ordinary Oracle content check still decides ACKED.
"""

from uuid import uuid4

from sqlalchemy import func, select, tuple_

from zk_add.attendance_force_release import _lock_scheduler
from zk_add.attendance_legacy_uid import potentially_recoverable
from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.attendance_repair import _immutable_facts
from zk_add.audit import append_audit
from zk_add.crypto import cnic_lookup, decrypt_cnic, decrypt_json, encrypt_json, encrypt_text, normalize_cnic
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


def _identity_from_users(event, users):
    if len(users) != 1:
        return None, None, None, None, "UNKNOWN_USER"
    try:
        current_cnic = normalize_cnic(decrypt_cnic(users[0].cnic_encrypted))
    except Exception:
        current_cnic = None
    if not current_cnic:
        return users[0], None, None, None, "CNIC_MISSING"
    try:
        saved_cnic = normalize_cnic(decrypt_cnic(event.cnic_encrypted))
    except Exception:
        saved_cnic = None
    return (
        users[0], saved_cnic or current_cnic,
        "SAVED_PUNCH" if saved_cnic else "SYNCED_USER", current_cnic, None,
    )


def _known_user_and_cnic(session, event):
    users = session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == event.zkt_device_id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
        ).limit(2)
    ).all()
    return _identity_from_users(event, users)


def identity_hints_for_page(session, events):
    """Tell the ledger which rows have a usable synced identity in bounded queries."""
    from zk_add.worker import event_uid_is_valid

    if not events:
        return {}
    users_by_key = {}
    keys = sorted({(row.zkt_device_id, row.user_id) for row in events})
    for start in range(0, len(keys), 200):
        for user in session.scalars(
            select(DeviceUser).where(
                tuple_(DeviceUser.zkt_device_id, DeviceUser.user_id).in_(keys[start:start + 200]),
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all():
            users_by_key.setdefault((user.zkt_device_id, user.user_id), []).append(user)
    hints = {}
    for event in events:
        _user, _cnic, source, _current_cnic, error = _identity_from_users(
            event, users_by_key.get((event.zkt_device_id, event.user_id), []),
        )
        if not error and not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            error = "INVALID_EVENT_UID"
        hints[event.id] = {
            "eligible": error is None,
            "cnic_source": source,
            "exclusion": error,
        }
    return hints


def _payload(connector, terminal, event, user, cnic, source):
    payload = oracle_payload(connector, terminal, event, cnic)
    payload["device_serial"] = event.device_serial or "unknown"
    payload["employee_name"] = (
        user.display_name or event.display_name
        if source == "SYNCED_USER"
        else event.display_name or user.display_name
    )
    return payload


def create(session, *, actor: str, request):
    from zk_add.worker import event_uid_is_valid

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
        user, cnic, source, current_cnic, error = _known_user_and_cnic(session, event)
        outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id))
        if event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
            error = "ALREADY_CONFIRMED"
        elif decision_for(session, event) or (outbox and outbox.status == "IN_FLIGHT"):
            error = "ALREADY_DELIVERING"
        elif not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            error = "INVALID_EVENT_UID"
        elif not connector or not terminal:
            error = "SOURCE_MISSING"
        elif not error:
            accepted += 1
        payload = (
            _payload(connector, terminal, event, user, cnic, source)
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
                "cnic_source": source if payload else None,
                "current_user_key": user.user_key if payload else None,
                "current_cnic_hash": cnic_lookup(current_cnic) if payload else None,
                "reason": (
                    "Oracle cannot accept this older punch ID. The punch remains saved for ID repair."
                    if error == "INVALID_EVENT_UID" else
                    error.replace("_", " ").title() if error else
                    "Approved; waiting to prepare delivery."
                ),
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
    user, cnic, source, current_cnic, error = _known_user_and_cnic(session, event)
    if error:
        return None
    try:
        payload = decrypt_json(decision.payload_encrypted)
    except Exception:
        return None
    if _digest(payload) != decision.payload_digest:
        return None
    if decision.proof.get("cnic_source") is None:
        return payload  # Preserve approvals queued before the stricter identity proof.
    terminal = session.get(ZKTDevice, event.zkt_device_id)
    if (
        terminal is None
        or decision.proof.get("cnic_source") != source
        or decision.proof.get("current_user_key") != user.user_key
        or decision.proof.get("current_cnic_hash") != cnic_lookup(current_cnic)
        or payload != _payload(connector, terminal, event, user, cnic, source)
    ):
        return None
    return payload


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
        user, cnic, source, current_cnic, code = _known_user_and_cnic(session, event)
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
    if (
        payload != _payload(connector, session.get(ZKTDevice, event.zkt_device_id), event, user, cnic, source)
        or item.result.get("cnic_source") != source
        or item.result.get("current_user_key") != user.user_key
        or item.result.get("current_cnic_hash") != cnic_lookup(current_cnic)
    ):
        item.status, item.error_code, item.completed_at = "SKIPPED", "SOURCE_CHANGED", utc_now()
        item.result = {"reason": "Saved punch or employee changed before delivery."}
        return
    decision = Decision(
        item_id=item.id, attendance_event_id=event.id, job_id=job.id,
        actor=job.actor, reason_encrypted=job.scope["approval"]["reason_encrypted"],
        proof={"policy": POLICY, "source_digest": item.expected_state_digest,
               "immutable_facts": _immutable_facts(event), "hardware_id": connector.hardware_id,
               "terminal": event.device_serial, "cnic_source": source,
               "current_user_key": user.user_key,
               "current_cnic_hash": cnic_lookup(current_cnic)},
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
    item.result = {"reason": "Waiting for Oracle confirmation.", "cnic_source": source}
    audit = append_audit(
        session, actor=job.actor, action="ATTENDANCE_DIRECT_ORDS_QUEUED",
        target_type="attendance_event", target_id=event.event_uid,
        outcome="WAITING_ORACLE", after={"job_id": job.job_id, "payload_digest": decision.payload_digest},
    )
    decision.audit_id = audit.id


def _observe(session, job):
    from zk_add.worker import event_uid_is_valid

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
        elif not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            outbox = session.scalar(
                select(OrdsOutbox)
                .where(OrdsOutbox.attendance_event_id == event.id)
                .with_for_update()
            )
            # An in-flight worker owns the row and will apply the same guard.
            if outbox and outbox.status != "IN_FLIGHT":
                now = utc_now()
                outbox.status = event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                outbox.next_attempt_at, outbox.last_error = None, "INVALID_EVENT_UID"
                item.status, item.error_code, item.completed_at = "NEEDS_REVIEW", "INVALID_EVENT_UID", now
                item.result = {
                    **item.result,
                    "reason": "Oracle cannot accept this older punch ID. The punch and approval remain saved for ID repair.",
                    "needs_attention": True,
                }
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


def recheck_legacy(session, job, *, actor: str) -> int:
    """Reopen only saved direct approvals for read-only Oracle source verification."""
    from zk_add.attendance_force_release import delivery_payload

    _lock_scheduler(session)
    if job.action != ACTION:
        raise RecoveryError("This is not a saved Oracle send run.", "RUN_MISMATCH")
    count = 0
    items = session.scalars(
        select(Item).where(
            Item.job_id == job.id,
            Item.status == "NEEDS_REVIEW",
            Item.error_code == "INVALID_EVENT_UID",
        ).order_by(Item.id).with_for_update()
    ).all()
    for item in items:
        event = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.id == item.attendance_event_id).with_for_update()
        )
        if not event or not potentially_recoverable(event.event_uid):
            continue
        decision = decision_for(session, event)
        connector = session.get(Connector, event.connector_id)
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id).with_for_update()
        )
        try:
            approved_payload = decrypt_json(decision.payload_encrypted) if decision else None
        except Exception:
            approved_payload = None
        if (
            not decision or decision.job_id != job.id or decision.item_id != item.id
            or decision.proof.get("policy") != POLICY
            or not connector or _source_digest(event, connector) != decision.proof.get("source_digest")
            or not outbox or outbox.status != "QUARANTINED_INVALID_EVENT_UID"
            or event.ords_status != "QUARANTINED_INVALID_EVENT_UID"
            or event.oracle_confirmed_at is not None
            or approved_payload is None
            or delivery_payload(session, event, connector) != approved_payload
        ):
            continue
        outbox.status = event.ords_status = "PENDING"
        outbox.next_attempt_at = outbox.last_error = None
        item.status, item.error_code, item.completed_at = "WAITING_ORACLE", None, None
        item.result = {**item.result, "reason": "Checking the already saved Oracle punch.", "needs_attention": False}
        item.updated_at = utc_now()
        count += 1
    if items and not count:
        raise RecoveryError(
            "These saved approvals or punch details changed. No Oracle check was started.",
            "RECHECK_UNAVAILABLE",
        )
    if count:
        job.status, job.completed_at, job.updated_at = "WAITING_ORACLE", None, utc_now()
        append_audit(
            session, actor=actor, action="ATTENDANCE_DIRECT_ORDS_LEGACY_RECHECK",
            target_type="attendance_recovery_job", target_id=job.job_id,
            outcome="WAITING_ORACLE", after={"rechecked_count": count},
        )
    return count


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
    recheckable = sum(
        potentially_recoverable(uid)
        for (uid,) in session.execute(
            select(AttendanceEvent.event_uid)
            .join(Item, Item.attendance_event_id == AttendanceEvent.id)
            .where(Item.job_id == job.id, Item.status == "NEEDS_REVIEW", Item.error_code == "INVALID_EVENT_UID")
        ).all()
    )
    return {
        "job_id": job.job_id, "status": job.status, "created_at": job.created_at,
        "approved_at": job.scope["approval"]["at"], "actor": job.actor,
        "selected": job.requested_count, "ready": counts.get("READY", 0),
        "waiting": counts.get("WAITING_ORACLE", 0), "confirmed": counts.get("CONFIRMED", 0),
        "skipped": counts.get("SKIPPED", 0),
        "attention": counts.get("NEEDS_REVIEW", 0) + waiting_attention,
        "legacy_recheckable": recheckable,
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
