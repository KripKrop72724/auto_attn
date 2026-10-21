"""Durable, explicitly approved delivery of selected saved punches to Oracle.

This operator action overrides ADD's delivery holds. It never invents a user,
CNIC or source fact, and the ordinary Oracle content check still decides ACKED.
It refuses impossible punch times, and when the CNIC the terminal held at the
punch time is not today's CNIC (or ADD has no record of it), the administrator
must choose which CNIC to send. Punches ADD's own proof is about to release
need an explicit "send now" as well.
"""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select, tuple_

from zk_add.attendance_force_release import _lock_scheduler
from zk_add.attendance_legacy_uid import potentially_recoverable
from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.attendance_release_safety import (
    DIFFERS,
    MATCHES_CURRENT,
    NONE,
    PunchTimeIdentity,
    auto_release_preview,
    classify_punch_time,
    intervals_for_page,
    punch_time_identity,
    time_refusal,
)
from zk_add.attendance_repair import _immutable_facts
from zk_add.audit import append_audit
from zk_add.crypto import (
    cnic_lookup, decrypt_cnic, decrypt_json, encrypt_json, encrypt_text, mask_cnic, normalize_cnic,
)
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

PUNCH_TIME = "PUNCH_TIME"
CURRENT = "CURRENT"
PUNCH_TIME_SOURCE = "PUNCH_TIME_HISTORY"

REASONS = {
    "CLOCK_INVALID": "The terminal clock was invalid for this punch. It is never sent to Oracle.",
    "TIME_IMPOSSIBLE": (
        "The punch time is impossible (before 2010 or later than its capture). "
        "It is never sent to Oracle."
    ),
    "CNIC_CHOICE_REQUIRED": (
        "The CNIC at punch time is not today's CNIC, or ADD has no record of it. "
        "Choose which CNIC to send."
    ),
    "PUNCH_TIME_CNIC_DIFFERS": (
        "The terminal held another CNIC for this user ID at punch time. "
        "This older approval used today's CNIC and was stopped."
    ),
    "AUTO_RELEASE_PENDING": (
        "ADD releases this punch automatically at the next user snapshot. "
        "Confirm sending it now to override that."
    ),
}


@dataclass(frozen=True)
class Resolution:
    """The identity a direct approval would send, and why."""

    user: DeviceUser | None = None
    cnic: str | None = None
    source: str | None = None  # SAVED_PUNCH, SYNCED_USER or PUNCH_TIME_HISTORY
    current_cnic: str | None = None
    error: str | None = None
    punch_time: PunchTimeIdentity | None = None
    choice: str | None = None
    choices: tuple[str, ...] = ()


def _source_digest(event: AttendanceEvent, connector: Connector) -> str:
    return _digest([
        _immutable_facts(event), event.raw_event, event.connector_id,
        event.zkt_device_id, event.device_user_id, event.cnic_encrypted,
        event.display_name, connector.hardware_id,
    ])


def _current_and_saved(event, users):
    if len(users) != 1:
        return None, None, None, "UNKNOWN_USER"
    try:
        current_cnic = normalize_cnic(decrypt_cnic(users[0].cnic_encrypted))
    except Exception:
        current_cnic = None
    if not current_cnic:
        return users[0], None, None, "CNIC_MISSING"
    try:
        saved_cnic = normalize_cnic(decrypt_cnic(event.cnic_encrypted))
    except Exception:
        saved_cnic = None
    return users[0], saved_cnic, current_cnic, None


def _resolve(event, users, punch_time_for, *, choice=None, legacy=False) -> Resolution:
    """Pick the CNIC to send. Never today's CNIC by user ID alone.

    A CNIC saved with the punch is what the terminal captured. Otherwise the
    retained identity interval at punch time decides: today's CNIC only when
    it matches, or when the administrator explicitly chose it. ``legacy``
    re-checks an approval saved before this rule: it stays valid unless a
    retained interval now shows another CNIC at punch time.
    """
    user, saved_cnic, current_cnic, error = _current_and_saved(event, users)
    if error:
        return Resolution(user=user, error=error)
    if saved_cnic:
        return Resolution(user, saved_cnic, "SAVED_PUNCH", current_cnic)
    punch = punch_time_for(cnic_lookup(current_cnic))
    if punch.state == MATCHES_CURRENT:
        return Resolution(user, current_cnic, "SYNCED_USER", current_cnic, punch_time=punch)
    if legacy:
        if punch.state == NONE:
            return Resolution(user, current_cnic, "SYNCED_USER", current_cnic, punch_time=punch)
        return Resolution(
            user, None, None, current_cnic, "PUNCH_TIME_CNIC_DIFFERS", punch_time=punch,
        )
    choices = (PUNCH_TIME, CURRENT) if punch.state == DIFFERS and punch.cnic else (CURRENT,)
    if choice == PUNCH_TIME and PUNCH_TIME in choices:
        return Resolution(
            user, punch.cnic, PUNCH_TIME_SOURCE, current_cnic,
            punch_time=punch, choice=choice, choices=choices,
        )
    if choice == CURRENT:
        return Resolution(
            user, current_cnic, "SYNCED_USER", current_cnic,
            punch_time=punch, choice=choice, choices=choices,
        )
    return Resolution(
        user, None, None, current_cnic, "CNIC_CHOICE_REQUIRED",
        punch_time=punch, choices=choices,
    )


def _active_users(session, event):
    return session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == event.zkt_device_id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
        ).limit(2)
    ).all()


def _known_identity(session, event, *, choice=None, legacy=False) -> Resolution:
    return _resolve(
        event, _active_users(session, event),
        lambda current_hash: punch_time_identity(session, event, current_hash),
        choice=choice, legacy=legacy,
    )


def _choice_in(record: dict) -> tuple[str | None, bool]:
    """The saved operator choice, and whether the record predates choices."""
    return record.get("cnic_choice"), "cnic_choice" not in record


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
    intervals = intervals_for_page(session, [
        row for row in events
        if len(users_by_key.get((row.zkt_device_id, row.user_id), [])) == 1
    ])
    hints = {}
    for event in events:
        resolution = _resolve(
            event, users_by_key.get((event.zkt_device_id, event.user_id), []),
            lambda current_hash, event=event: classify_punch_time(
                event, intervals.get((event.zkt_device_id, event.device_serial, event.user_id), []),
                current_hash, verify=False,
            ),
        )
        error = resolution.error
        if error == "CNIC_CHOICE_REQUIRED":
            error = None  # The administrator chooses in the send dialog.
        if not error:
            error = time_refusal(event)
        if not error and not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
            error = "INVALID_EVENT_UID"
        punch = resolution.punch_time
        hints[event.id] = {
            "eligible": error is None,
            "cnic_source": resolution.source or (
                "SYNCED_USER" if resolution.error == "CNIC_CHOICE_REQUIRED" else None
            ),
            "exclusion": error,
            "punch_time_cnic": punch.state if punch else None,
            "cnic_choice_required": resolution.error == "CNIC_CHOICE_REQUIRED",
        }
    return hints


def _payload(connector, terminal, event, resolution: Resolution):
    payload = oracle_payload(connector, terminal, event, resolution.cnic)
    payload["device_serial"] = event.device_serial or "unknown"
    user = resolution.user
    if resolution.source == PUNCH_TIME_SOURCE:
        payload["employee_name"] = (
            resolution.punch_time.display_name or user.display_name or event.display_name
        )
    elif resolution.source == "SYNCED_USER":
        payload["employee_name"] = user.display_name or event.display_name
    else:
        payload["employee_name"] = event.display_name or user.display_name
    return payload


def _identity_proof(resolution: Resolution, *, legacy: bool = False) -> dict:
    """Saved with each item and decision; delivery re-derives and compares it.

    An approval saved before per-punch choices keeps its original keys, so it
    is re-checked under the rule it was approved with (see ``_resolve``).
    """
    proof = {
        "cnic_source": resolution.source,
        "current_user_key": resolution.user.user_key,
        "current_cnic_hash": cnic_lookup(resolution.current_cnic),
    }
    if legacy:
        return proof
    punch = resolution.punch_time
    return {
        **proof,
        "cnic_choice": resolution.choice,
        "punch_time_state": punch.state if punch else None,
        "punch_time_cnic_hash": (
            punch.cnic_hash if resolution.source == PUNCH_TIME_SOURCE else None
        ),
    }


def _same_identity(saved: dict, resolution: Resolution) -> bool:
    current = _identity_proof(resolution)
    keys = ["cnic_source", "current_user_key", "current_cnic_hash"]
    if "cnic_choice" in saved:
        keys += ["cnic_choice", "punch_time_cnic_hash"]
    return all(saved.get(key) == current[key] for key in keys)


@dataclass(frozen=True)
class Assessment:
    event: AttendanceEvent
    connector: Connector | None
    terminal: ZKTDevice | None
    resolution: Resolution
    error: str | None
    auto_release: dict


def assess(session, event, *, choice=None, send_now=False, include_guard=True) -> Assessment:
    """Everything a direct approval checks for one punch, without writing."""
    from zk_add.worker import event_uid_is_valid

    connector = session.get(Connector, event.connector_id)
    terminal = session.get(ZKTDevice, event.zkt_device_id)
    resolution = _known_identity(session, event, choice=choice)
    outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id))
    auto_release = {"at_next_snapshot": False, "awaiting_snapshot": False, "database_guard": "NOT_CHECKED"}
    if event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
        error = "ALREADY_CONFIRMED"
    elif decision_for(session, event) or (outbox and outbox.status == "IN_FLIGHT"):
        error = "ALREADY_DELIVERING"
    elif not (event_uid_is_valid(event.event_uid) or potentially_recoverable(event.event_uid)):
        error = "INVALID_EVENT_UID"
    elif not connector or not terminal:
        error = "SOURCE_MISSING"
    else:
        error = time_refusal(event) or resolution.error
    if error is None or error == "CNIC_CHOICE_REQUIRED":
        auto_release = auto_release_preview(session, event, include_guard=include_guard)
        if error is None and not send_now and (
            auto_release["at_next_snapshot"] or auto_release["awaiting_snapshot"]
        ):
            error = "AUTO_RELEASE_PENDING"
    return Assessment(event, connector, terminal, resolution, error, auto_release)


def _reason(error: str | None) -> str:
    if error == "INVALID_EVENT_UID":
        return "Oracle cannot accept this older punch ID. The punch remains saved for ID repair."
    if error in REASONS:
        return REASONS[error]
    return error.replace("_", " ").title() if error else "Approved; waiting to prepare delivery."


def preview(session, event_ids) -> dict:
    """Read-only checks the send dialog shows before an administrator approves."""
    rows = session.scalars(
        select(AttendanceEvent).where(AttendanceEvent.id.in_(sorted(set(event_ids))))
        .order_by(AttendanceEvent.id)
    ).all()
    found = {row.id for row in rows}
    result = []
    for event in rows:
        check = assess(session, event)
        resolution, punch = check.resolution, check.resolution.punch_time
        user = resolution.user
        result.append({
            "event_id": event.id,
            "user_id": event.user_id,
            "display_name": user.display_name if user else event.display_name,
            "device_event_time": event.device_event_time,
            "exclusion": None if check.error in {"CNIC_CHOICE_REQUIRED", "AUTO_RELEASE_PENDING"} else check.error,
            "exclusion_reason": (
                None if check.error in {None, "CNIC_CHOICE_REQUIRED", "AUTO_RELEASE_PENDING"}
                else _reason(check.error)
            ),
            "cnic_source": resolution.source,
            "punch_time_cnic": punch.state if punch else None,
            "punch_time_cnic_masked": mask_cnic(punch.cnic) if punch and punch.cnic else None,
            "punch_time_display_name": punch.display_name if punch else None,
            "current_cnic_masked": mask_cnic(resolution.current_cnic),
            "uid_matches_current": bool(user and (not event.uid or user.uid == event.uid)),
            "cnic_choice_required": check.error == "CNIC_CHOICE_REQUIRED",
            "cnic_choices": list(resolution.choices),
            "default_cnic_choice": PUNCH_TIME if PUNCH_TIME in resolution.choices else None,
            "auto_release_at_next_snapshot": check.auto_release["at_next_snapshot"],
            "awaiting_next_snapshot": check.auto_release["awaiting_snapshot"],
            "database_guard": check.auto_release["database_guard"],
        })
    return {
        "rows": result,
        "missing_event_ids": sorted(set(event_ids) - found),
        "counts": {
            "selected": len(set(event_ids)),
            "refused": sum(row["exclusion"] is not None for row in result),
            "cnic_choice_required": sum(row["cnic_choice_required"] for row in result),
            "auto_release_soon": sum(
                row["auto_release_at_next_snapshot"] or row["awaiting_next_snapshot"]
                for row in result
            ),
        },
    }


def _request_decisions(request) -> tuple[dict[int, str], set[int]]:
    choices = {row.event_id: row.choice for row in getattr(request, "cnic_choices", None) or []}
    send_now = set(getattr(request, "send_now_event_ids", None) or [])
    return choices, send_now


def create(session, *, actor: str, request):
    _lock_scheduler(session)
    event_ids = sorted(request.event_ids)
    choices, send_now = _request_decisions(request)
    request_digest = _digest(
        [ACTION, actor, event_ids, request.reason.strip()]
        # Approvals saved before per-punch decisions keep their digest.
        + ([sorted(choices.items()), sorted(send_now)] if choices or send_now else [])
    )
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
    # Every check reads before the run is added, so the read-only database
    # guard probe never flushes a half-built approval.
    checks = [
        assess(session, event, choice=choices.get(event.id), send_now=event.id in send_now)
        for event in rows
    ]
    now = utc_now()
    job = Job(
        action=ACTION, status="RUNNING", actor=actor,
        reason="Administrator-selected Oracle delivery",
        idempotency_key=request.idempotency_key,
        scope={
            "policy": POLICY,
            "selected_event_ids": event_ids,
            "approval": {"at": now.isoformat(), "reason_encrypted": encrypt_text(request.reason.strip())},
            "cnic_choices": {str(key): value for key, value in sorted(choices.items())},
            "send_now_event_ids": sorted(send_now),
        },
        candidate_digest=request_digest,
        requested_count=len(rows), eligible_count=0, excluded_count=0,
        started_at=now, updated_at=now,
    )
    session.add(job)
    session.flush()
    accepted = 0
    excluded_by_code: dict[str, int] = {}
    for check in checks:
        event, error = check.event, check.error
        payload = (
            _payload(check.connector, check.terminal, event, check.resolution)
            if error is None else None
        )
        if payload:
            accepted += 1
        else:
            excluded_by_code[error] = excluded_by_code.get(error, 0) + 1
        session.add(Item(
            job_id=job.id, source_kind="ATTENDANCE_EVENT", source_ref=str(event.id),
            connector_id=event.connector_id, attendance_event_id=event.id,
            expected_state_digest=(
                _source_digest(event, check.connector) if check.connector else _digest(event.id)
            ),
            lane="DIRECT_ORDS",
            status="READY" if payload else "SKIPPED",
            error_code=error,
            result={
                "payload_encrypted": encrypt_json(payload) if payload else None,
                "payload_digest": _digest(payload) if payload else None,
                **(_identity_proof(check.resolution) if payload else {
                    "cnic_source": None, "current_user_key": None, "current_cnic_hash": None,
                    "cnic_choice": None, "punch_time_state": None, "punch_time_cnic_hash": None,
                }),
                "auto_release_override": bool(
                    payload and (
                        check.auto_release["at_next_snapshot"]
                        or check.auto_release["awaiting_snapshot"]
                    )
                ),
                "reason": _reason(error),
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
        outcome="APPROVED", after={
            "selected": len(rows), "ready": accepted, "digest": request_digest,
            "excluded": dict(sorted(excluded_by_code.items())),
            "cnic_choices": {
                choice: sum(value == choice for value in choices.values())
                for choice in sorted(set(choices.values()))
            },
            "send_now": len(send_now),
        },
    )
    return job


def approved_payload(session, event, connector, decision):
    if decision.proof.get("policy") != POLICY or connector is None:
        return None
    if time_refusal(event):
        return None
    if _source_digest(event, connector) != decision.proof.get("source_digest"):
        return None
    choice, legacy = _choice_in(decision.proof)
    resolution = _known_identity(session, event, choice=choice, legacy=legacy)
    if resolution.error:
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
        or not _same_identity(decision.proof, resolution)
        or payload != _payload(connector, terminal, event, resolution)
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
    resolution, legacy = None, False
    if not event or not connector or item.expected_state_digest != _source_digest(event, connector):
        code = "SOURCE_CHANGED"
    elif event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
        code = "ALREADY_CONFIRMED"
    elif decision_for(session, event):
        code = "ALREADY_DELIVERING"
    elif outbox and outbox.status == "IN_FLIGHT":
        return  # Let the earlier claim settle; the next short tick will recheck.
    else:
        choice, legacy = _choice_in(item.result)
        resolution = _known_identity(session, event, choice=choice, legacy=legacy)
        code = time_refusal(event) or resolution.error
    if code:
        item.status, item.error_code, item.completed_at = "SKIPPED", code, utc_now()
        item.result = {"reason": _reason(code)}
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
        payload != _payload(connector, session.get(ZKTDevice, event.zkt_device_id), event, resolution)
        or not _same_identity(item.result, resolution)
    ):
        item.status, item.error_code, item.completed_at = "SKIPPED", "SOURCE_CHANGED", utc_now()
        item.result = {"reason": "Saved punch or employee changed before delivery."}
        return
    decision = Decision(
        item_id=item.id, attendance_event_id=event.id, job_id=job.id,
        actor=job.actor, reason_encrypted=job.scope["approval"]["reason_encrypted"],
        proof={"policy": POLICY, "source_digest": item.expected_state_digest,
               "immutable_facts": _immutable_facts(event), "hardware_id": connector.hardware_id,
               "terminal": event.device_serial,
               **_identity_proof(resolution, legacy=legacy),
               "auto_release_override": bool(item.result.get("auto_release_override"))},
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
    item.result = {"reason": "Waiting for Oracle confirmation.", "cnic_source": resolution.source}
    audit = append_audit(
        session, actor=job.actor, action="ATTENDANCE_DIRECT_ORDS_QUEUED",
        target_type="attendance_event", target_id=event.event_uid,
        outcome="WAITING_ORACLE", after={
            "job_id": job.job_id, "payload_digest": decision.payload_digest,
            "cnic_source": resolution.source, "cnic_choice": resolution.choice,
            "auto_release_override": decision.proof["auto_release_override"],
        },
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
