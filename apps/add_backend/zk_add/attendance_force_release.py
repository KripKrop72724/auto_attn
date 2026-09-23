"""Manual, frozen attendance approvals backed by fresh terminal reads.

The scheduler only advances explicitly created jobs. Its transactions are bounded;
device and Oracle network operations are handled by the existing transports.
"""

from datetime import datetime, timedelta
import re
from uuid import uuid4

from sqlalchemy import func, or_, select

from zk_add.attendance_manual_guard import decision_for
from zk_add.attendance_recovery import (
    RecoveryError,
    _digest,
    _terminal_provenance_verified,
    sign_recovery_preview,
    verify_recovery_preview,
)
from zk_add.audit import append_audit
from zk_add.crypto import (
    cnic_lookup,
    decrypt_cnic,
    decrypt_json,
    decrypt_text,
    encrypt_json,
    encrypt_text,
)
from zk_add.identity import parse_machine_name
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceForceReleaseScheduler as Scheduler,
    AttendanceForceReleaseTask as Task,
    AttendanceForceReleaseUser as BaselineUser,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    AttendanceRepairJob,
    Connector,
    DeviceCommand,
    DeviceUser,
    DeviceUserSnapshot,
    IdentityTombstone,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.ords_states import ORDS_ACKNOWLEDGED_STATUSES
from zk_add.service import attendance_device_time_is_plausible, create_command, oracle_payload
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

ACTION = "MANUAL_FORCE_RELEASE"
POLICY = "manual-current-terminal-v1"
BATCH = 100
ACTIVE = {"CHECKING", "RUNNING", "WAITING_ORACLE", "PAUSED", "STOPPING"}
TERMINAL = {"COMPLETED", "COMPLETED_WITH_REVIEW", "STOPPED", "EXPIRED"}
MESSAGES = {
    "READY": "Current terminal user and CNIC match. Administrator approval is required.",
    "CNIC_MISSING": "CNIC missing from ADD or the terminal.",
    "CNIC_CONFLICT": "CNIC does not match.",
    "USER_MISSING": "User no longer on terminal, or the user cannot be identified.",
    "IDENTITY_CONFLICT": "The employee identity changed or has a known conflict.",
    "USER_ID_INVALID": "The saved user ID is damaged.",
    "UID_INVALID": "The terminal user UID is damaged or does not match.",
    "UID_UNPROVEN": "This record does not explain its missing terminal user UID.",
    "EVENT_ID_INVALID": "Punch ID is damaged.",
    "SOURCE_INVALID": "The original punch data needs checking.",
    "TIME_INVALID": "The punch date or time needs checking.",
    "TERMINAL_CHANGED": "The terminal identity changed or is not confirmed.",
    "SNAPSHOT_CHANGED": "The terminal user list changed. Sync and check again.",
    "SYNC_FAILED": "Couldn’t sync the latest complete terminal user list.",
    "SYNC_EXPIRED": "Terminal sync timed out. Try this device again later.",
    "DEVICE_OFFLINE": "The device is unavailable for a fresh terminal sync.",
    "DEVICE_BUSY": "Another user operation is using this terminal. Try again after it finishes.",
    "ALREADY_CONFIRMED": "Oracle has already confirmed this attendance.",
    "ALREADY_DELIVERING": "A delivery is already in progress. It will not be submitted again.",
    "NOT_HELD": "This record needs a different investigation; it is not an identity hold.",
    "PERMANENT_REVIEW": "The saved record has a conflict that force release cannot override.",
    "EVIDENCE_CHANGED": "The approved employee or punch changed. This record was not released.",
    "STOPPED": "Stopped before delivery. The attendance remains saved.",
    "ROLLOUT_RESTRICTED": "Force release is not enabled for this device yet.",
    "ORACLE_UNAVAILABLE": "Oracle verification is unavailable. The approved attendance is saved.",
    "ORACLE_CONFLICT": "Oracle contains different attendance or employee details. Review is required.",
}


def explain(code):
    return MESSAGES.get(code, "The record needs review before it can be released.")


def enabled(connector):
    ids = set(
        filter(
            None,
            (v.strip() for v in settings.attendance_force_release_allowed_connectors.split(",")),
        )
    )
    families = set(settings.attendance_force_release_allowed_families.split(","))
    return bool(
        settings.attendance_force_release_execution_enabled
        and connector.firmware_family in families
        and (not ids or connector.connector_id in ids)
    )


def _lock_scheduler(session):
    row = session.scalar(select(Scheduler).where(Scheduler.id == 1).with_for_update())
    if row is None:
        # Production migrations seed this singleton. create_all test stores use it too.
        row = Scheduler(id=1)
        session.add(row)
        session.flush()
    return row


def _binding(task, connector):
    terminal = connector.zkt_device if connector else None
    return bool(
        connector
        and connector.active
        and not connector.is_spare
        and terminal
        and terminal.terminal_binding_state == "CONFIRMED"
        and task.hardware_id == connector.hardware_id
        and task.terminal_serial
        and task.terminal_serial == terminal.serial == terminal.confirmed_serial
    )


def _in_scope(job, query):
    request = job.scope["request"]
    if request.get("from_time"):
        query = query.where(
            AttendanceEvent.device_event_time >= datetime.fromisoformat(request["from_time"])
        )
    if request.get("to_time"):
        query = query.where(
            AttendanceEvent.device_event_time < datetime.fromisoformat(request["to_time"])
        )
    return query


def create_check(session, *, actor, request):
    if not settings.attendance_force_release_preview_enabled:
        raise RecoveryError("Force release checks are not enabled yet.", "FORCE_DISABLED")
    _lock_scheduler(session)
    scope = request.model_dump(mode="json", exclude={"idempotency_key"})
    scope["connector_ids"] = sorted(set(scope["connector_ids"]))
    old = session.scalar(select(Job).where(Job.idempotency_key == request.idempotency_key))
    if old:
        if old.actor != actor or old.action != ACTION or old.scope.get("request") != scope:
            raise RecoveryError(
                "This request key belongs to a different check.", "IDEMPOTENCY_CONFLICT"
            )
        return old
    query = select(Connector).order_by(Connector.id)
    if request.scope == "ALL_PAKISTAN":
        query = query.where(Connector.active.is_(True), Connector.is_spare.is_(False))
    else:
        query = query.where(Connector.connector_id.in_(scope["connector_ids"]))
    connectors = session.scalars(query).all()
    if not connectors or (
        request.scope == "SELECTED" and len(connectors) != len(scope["connector_ids"])
    ):
        raise RecoveryError("Select existing devices to check.", "SCOPE_CHANGED")
    job = Job(
        action=ACTION,
        status="CHECKING",
        actor=actor,
        reason="Manual force release check",
        idempotency_key=request.idempotency_key,
        candidate_digest="0" * 64,
        scope={"request": scope, "policy": POLICY},
    )
    session.add(job)
    session.flush()
    boundaries = dict(
        session.execute(
            select(AttendanceEvent.connector_id, func.max(AttendanceEvent.id))
            .where(AttendanceEvent.connector_id.in_([c.id for c in connectors]))
            .group_by(AttendanceEvent.connector_id)
        ).all()
    )
    for connector in connectors:
        terminal = connector.zkt_device
        session.add(
            Task(
                job_id=job.id,
                connector_id=connector.id,
                terminal_serial=terminal.confirmed_serial if terminal else None,
                hardware_id=connector.hardware_id,
                high_water_id=boundaries.get(connector.id, 0),
                baseline_revision=(terminal.identity_snapshot_revision or 0) if terminal else 0,
            )
        )
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_FORCE_CHECK",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="QUEUED",
        after={"devices": len(connectors), "policy": POLICY},
    )
    return job


def _unavailable(task, code):
    task.status, task.error_code, task.updated_at = "UNAVAILABLE", code, utc_now()


def _baseline(session, task, connector):
    terminal = connector.zkt_device
    if (terminal.identity_snapshot_revision or 0) != task.baseline_revision:
        _unavailable(task, "SNAPSHOT_CHANGED")
        return
    users = session.scalars(
        select(DeviceUser)
        .where(
            DeviceUser.zkt_device_id == terminal.id,
            DeviceUser.id > task.user_cursor,
            DeviceUser.lifecycle_state == "ACTIVE",
        )
        .order_by(DeviceUser.id)
        .limit(BATCH)
    ).all()
    for user in users:
        session.add(
            BaselineUser(
                task_id=task.id,
                device_user_id=user.id,
                user_id=user.user_id,
                uid=user.uid,
                cnic_hash=user.cnic_lookup_hash,
                identity_fingerprint=user.terminal_identity_fingerprint,
            )
        )
        task.user_cursor = user.id
    if len(users) < BATCH:
        task.status = "SYNC_PENDING"


def _request_sync(session, job, task, connector):
    from zk_add.service import ACTIVE_COMMAND_STATES, MUTATING_COMMANDS

    concurrent = (
        session.scalar(
            select(func.count())
            .select_from(Task)
            .join(DeviceCommand, Task.sync_command_id == DeviceCommand.id)
            .where(DeviceCommand.status.in_(ACTIVE_COMMAND_STATES))
        )
        or 0
    )
    if concurrent >= 2:
        return
    active_command = session.scalar(
        select(DeviceCommand.id)
        .where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.command_type.in_([*MUTATING_COMMANDS, "REFRESH_USERS"]),
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
        )
        .limit(1)
    )
    if active_command is not None and active_command == task.sync_command_id:
        return  # Resume waits for its prior command, then requests a new sync.
    if active_command:
        _unavailable(task, "DEVICE_BUSY")
        return
    task.sync_round += 1
    command = create_command(
        session,
        connector=connector,
        command_type="REFRESH_USERS",
        payload={},
        expected_state={},
        desired_state={},
        actor=job.actor,
        idempotency_key=f"force-sync:{job.job_id}:{task.id}:{task.sync_round}",
        expires_in_seconds=600,
    )
    task.sync_command_id = command.id
    task.sync_boot_id = connector.boot_id
    task.sync_revision = connector.zkt_device.identity_snapshot_revision or 0
    task.sync_requested_at = utc_now()
    task.sync_deadline = task.sync_requested_at + timedelta(minutes=10)
    task.status = "SYNCING"
    # The existing durable command dispatcher sends this after our transaction commits.


def _accept_sync(session, task, connector, *, releasing):
    command = session.get(DeviceCommand, task.sync_command_id)
    if (
        not command
        or connector.boot_id != task.sync_boot_id
        or command.status in {"FAILED", "EXPIRED", "CANCELLED"}
    ):
        _unavailable(task, "SYNC_FAILED")
        return
    if utc_now() >= ensure_utc(task.sync_deadline):
        _unavailable(task, "SYNC_EXPIRED")
        return
    terminal = connector.zkt_device
    snapshot = (
        session.get(DeviceUserSnapshot, terminal.identity_snapshot_id)
        if terminal.identity_snapshot_id
        else None
    )
    started = command.started_at or command.acknowledged_at
    if not (command.status == "SUCCEEDED" and command.completed_at and started and snapshot):
        return
    from zk_add.attendance_sync_evidence import ordered_refresh_proven

    clock_fresh = (
        ensure_utc(snapshot.observed_at) >= ensure_utc(started) - timedelta(seconds=5)
        and abs((ensure_utc(snapshot.received_at) - ensure_utc(snapshot.observed_at)).total_seconds()) <= 300
    )
    ordered_fresh = ordered_refresh_proven(
        session, command, snapshot, boot_id=task.sync_boot_id,
        requested_at=task.sync_requested_at,
    )
    # A command acknowledgment is not a roster. The newly committed full roster
    # must be received after execution started, on the same connector boot.
    if not (
        snapshot.revision > task.sync_revision
        and snapshot.complete
        and snapshot.stable
        and terminal.snapshot_complete
        and terminal.identity_snapshot_stable
        and snapshot.connector_boot_id == task.sync_boot_id
        and ensure_utc(snapshot.received_at) >= ensure_utc(started)
        and ensure_utc(snapshot.received_at) >= ensure_utc(task.sync_requested_at)
        and (clock_fresh or ordered_fresh)
    ):
        return
    task.snapshot_id, task.synced_at = snapshot.id, utc_now()
    task.status = "RELEASING" if releasing else "CHECKING"
    task.error_code = None


def _cnic(user):
    try:
        saved = decrypt_cnic(user.cnic_encrypted)
        terminal = parse_machine_name(decrypt_text(user.machine_name_encrypted)).cnic
        if not saved or not terminal:
            return None, "CNIC_MISSING"
        if not re.fullmatch(r"[0-9]{13}", saved) or len(set(saved)) == 1:
            return None, "CNIC_MISSING"
        if saved != terminal or cnic_lookup(saved) != user.cnic_lookup_hash:
            return None, "CNIC_CONFLICT"
        return saved, None
    except Exception:
        return None, "CNIC_MISSING"


def _snapshot_matches(session, task, terminal):
    saved = session.get(DeviceUserSnapshot, task.snapshot_id) if task.snapshot_id else None
    current = (
        session.get(DeviceUserSnapshot, terminal.identity_snapshot_id)
        if terminal.identity_snapshot_id
        else None
    )
    return bool(
        saved
        and current
        and current.complete
        and current.stable
        and saved.state_hash == current.state_hash
        and saved.connector_boot_id == current.connector_boot_id == task.sync_boot_id
    )


def _uid_optional(session, event, connector):
    if connector.firmware_family == "hikvision":
        return True  # employeeNo is the protocol's sole user identifier.
    if event.source in {"LIVE", "LIVE_POLL"}:
        return True  # Supported ZKT realtime records include user-ID-only frames.
    return (
        session.scalar(
            select(TerminalRecordManifest.id)
            .where(
                TerminalRecordManifest.attendance_event_id == event.id,
                TerminalRecordManifest.canonical_source.is_(True),
                TerminalRecordManifest.connector_id == connector.id,
                TerminalRecordManifest.terminal_serial == event.device_serial,
                TerminalRecordManifest.disposition == "EVENT",
                TerminalRecordManifest.record_size.in_([16, 40]),
                TerminalRecordManifest.observed_user_id == event.user_id,
            )
            .limit(1)
        )
        is not None
    )


def _hikvision_source_valid(session, event, connector):
    """Use the preserved ISAPI observation, whose status is not a ZKT byte."""
    import hashlib
    from zk_add.hikvision_delivery import HikvisionPolicy
    from zk_add.hikvision_evidence import HikvisionEvidence
    from zk_add.hikvision_protocol import normalize_observation
    from zk_add.hikvision_probe import decode_body, ProbeError

    policy = session.get(HikvisionPolicy, connector.id)
    if not policy or not policy.enabled or policy.terminal_serial != event.device_serial:
        return False
    evidence = session.scalar(
        select(HikvisionEvidence)
        .where(
            HikvisionEvidence.connector_id == connector.id,
            HikvisionEvidence.event_uid == event.event_uid,
            HikvisionEvidence.terminal_serial == event.device_serial,
            HikvisionEvidence.source_epoch == policy.source_epoch,
            HikvisionEvidence.disposition.in_(
                {"ATTENDANCE", "IDENTITY_BLOCKED", "DUPLICATE_OBSERVATION"}
            ),
        )
        .order_by(HikvisionEvidence.id)
        .limit(1)
    )
    if not evidence or session.scalar(
        select(HikvisionEvidence.id)
        .where(
            HikvisionEvidence.connector_id == connector.id,
            HikvisionEvidence.event_uid == event.event_uid,
            HikvisionEvidence.disposition.in_({"SOURCE_FACT_CONFLICT", "IDENTITY_FACT_CONFLICT"}),
        )
        .limit(1)
    ):
        return False
    try:
        raw = decrypt_text(evidence.raw_encrypted)
        if hashlib.sha256(raw.encode()).hexdigest() != evidence.observation_sha256:
            return False
        observed = normalize_observation(
            decode_body(raw.encode()),
            terminal_serial=event.device_serial,
            source_epoch=evidence.source_epoch,
        )
        code = [observed.major, observed.minor]
        return bool(
            observed.event_uid == event.event_uid
            and observed.immutable_facts_digest == evidence.immutable_digest
            and observed.employee_no == event.user_id
            and ensure_utc(datetime.fromisoformat(observed.event_time_utc))
            == ensure_utc(event.device_event_time)
            and observed.attendance_status == event.status
            and event.punch is None
            and observed.serial_no == event.sequence
            and code in policy.success_codes
            and code not in policy.excluded_codes
        )
    except (ValueError, TypeError, AttributeError, ProbeError):
        return False


def identity_proof(session, event, connector, task=None):
    """Only historical continuity is overridable; contradictory evidence never is."""
    if (
        not _terminal_provenance_verified(event, connector)
        or connector.zkt_device.terminal_binding_state != "CONFIRMED"
    ):
        return None, "TERMINAL_CHANGED"
    if not re.fullmatch(r"[0-9a-f]{64}", event.event_uid or ""):
        return None, "EVENT_ID_INVALID"
    if event.clock_quality == "INVALID" or not attendance_device_time_is_plausible(
        event.device_event_time, event.captured_at
    ):
        return None, "TIME_INVALID"
    if connector.firmware_family == "hikvision":
        if not _hikvision_source_valid(session, event, connector):
            return None, "SOURCE_INVALID"
    elif any(
        value is not None and (not re.fullmatch(r"[0-9]{1,3}", value) or int(value) > 255)
        for value in (event.status, event.punch)
    ):
        return None, "SOURCE_INVALID"
    terminal = connector.zkt_device
    identifier = (
        r"[0-9]{1,32}" if connector.firmware_family == "hikvision" else r"[A-Za-z0-9_.-]{1,24}"
    )
    if not re.fullmatch(identifier, event.user_id or "") or event.user_id.lower() in {
        "0",
        "unknown",
        "null",
        "none",
    }:
        return None, "USER_ID_INVALID"
    users = session.scalars(
        select(DeviceUser)
        .where(
            DeviceUser.zkt_device_id == terminal.id,
            DeviceUser.user_id == event.user_id,
            DeviceUser.present.is_(True),
            DeviceUser.lifecycle_state == "ACTIVE",
        )
        .limit(2)
    ).all()
    if len(users) != 1:
        return None, "USER_MISSING"
    user = users[0]
    if (
        not terminal.snapshot_complete
        or not terminal.identity_snapshot_stable
        or user.snapshot_revision != terminal.identity_snapshot_revision
        or (task and not _snapshot_matches(session, task, terminal))
    ):
        return None, "SNAPSHOT_CHANGED"
    if (
        user.identity_conflict_code
        or (event.device_user_id and event.device_user_id != user.id)
        or event.identity_resolution_status in {"QUARANTINED_REUSE", "BLOCKED_SOURCE_CONFLICT"}
        or (
            event.identity_terminal_fingerprint
            and event.identity_terminal_fingerprint != user.terminal_identity_fingerprint
        )
    ):
        return None, "IDENTITY_CONFLICT"
    if event.uid:
        if (
            not re.fullmatch(r"[0-9]{1,32}", event.uid)
            or event.uid != user.uid
            or int(event.uid) == 0
        ):
            return None, "UID_INVALID"
    elif not _uid_optional(session, event, connector):
        return None, "UID_UNPROVEN"
    cnic, error = _cnic(user)
    if error:
        return None, error
    if event.cnic_lookup_hash and event.cnic_lookup_hash != user.cnic_lookup_hash:
        return None, "CNIC_CONFLICT"
    if event.captured_cnic_lookup_hash and event.captured_cnic_lookup_hash != user.cnic_lookup_hash:
        return None, "CNIC_CONFLICT"
    if event.cnic_encrypted:
        try:
            if decrypt_cnic(event.cnic_encrypted) != cnic:
                return None, "CNIC_CONFLICT"
        except Exception:
            return None, "CNIC_CONFLICT"
    identifiers = [DeviceUser.user_id == user.user_id]
    if user.uid:
        identifiers.append(DeviceUser.uid == user.uid)
    if session.scalar(
        select(DeviceUser.id)
        .where(DeviceUser.zkt_device_id == terminal.id, DeviceUser.id != user.id, or_(*identifiers))
        .limit(1)
    ):
        return None, "IDENTITY_CONFLICT"
    if session.scalar(
        select(IdentityTombstone.id)
        .where(
            IdentityTombstone.zkt_device_id == terminal.id,
            or_(IdentityTombstone.user_id == user.user_id, IdentityTombstone.uid == user.uid),
            or_(
                IdentityTombstone.device_user_id != user.id,
                IdentityTombstone.cnic_lookup_hash != user.cnic_lookup_hash,
            ),
        )
        .limit(1)
    ):
        return None, "IDENTITY_CONFLICT"
    if session.scalar(
        select(AttendanceIdentityHistory.id)
        .where(
            AttendanceIdentityHistory.zkt_device_id == terminal.id,
            AttendanceIdentityHistory.user_id == user.user_id,
            or_(
                AttendanceIdentityHistory.device_user_id != user.id,
                AttendanceIdentityHistory.cnic_lookup_hash != user.cnic_lookup_hash,
            ),
        )
        .limit(1)
    ):
        return None, "IDENTITY_CONFLICT"
    if task:
        before = session.scalars(
            select(BaselineUser).where(
                BaselineUser.task_id == task.id,
                or_(BaselineUser.user_id == user.user_id, BaselineUser.uid == user.uid),
            )
        ).all()
        if any(
            b.device_user_id != user.id
            or (b.cnic_hash and b.cnic_hash != user.cnic_lookup_hash)
            or (
                b.identity_fingerprint
                and b.identity_fingerprint != user.terminal_identity_fingerprint
            )
            for b in before
        ):
            return None, "IDENTITY_CONFLICT"
    from zk_add.attendance_repair import _protected_digest, _immutable_facts

    key = _protected_digest(
        [
            user.id,
            user.user_id,
            user.uid,
            user.cnic_lookup_hash,
            user.terminal_identity_fingerprint,
            user.display_name,
        ]
    )
    source = _digest(
        [
            _immutable_facts(event),
            event.raw_event,
            event.connector_id,
            event.zkt_device_id,
            event.effective_identity_revision_id,
            event.captured_cnic_lookup_hash,
        ]
    )
    return {
        "policy": POLICY,
        "source_digest": source,
        "identity_key": key,
        "device_user_id": user.id,
        "cnic_hash": user.cnic_lookup_hash,
        "terminal": event.device_serial,
        "hardware_id": connector.hardware_id,
    }, None


def classify(session, event, connector, task):
    if event.ords_status in ORDS_ACKNOWLEDGED_STATUSES and event.oracle_confirmed_at:
        return "EXCLUDED", "ALREADY_CONFIRMED", {}
    outbox = session.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id))
    if outbox and outbox.status in {"IN_FLIGHT", "PENDING", "FAILED_RETRYABLE", "RETRYING"}:
        return "EXCLUDED", "ALREADY_DELIVERING", {}
    if event.ords_status not in {"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"}:
        code = {
            "QUARANTINED_INVALID_EVENT_UID": "EVENT_ID_INVALID",
            "QUARANTINED_INVALID_DEVICE_TIME": "TIME_INVALID",
            "QUARANTINED_IDENTITY_REUSE": "IDENTITY_CONFLICT",
            "QUARANTINED_SOURCE_CONFLICT": "SOURCE_INVALID",
        }.get(event.ords_status, "PERMANENT_REVIEW")
        return "NEEDS_REVIEW", code, {}
    if outbox and outbox.status not in {"BLOCKED_IDENTITY", "WAITING_FOR_SNAPSHOT"}:
        return "NEEDS_REVIEW", "PERMANENT_REVIEW", {}
    proof, error = identity_proof(session, event, connector, task)
    if error:
        return "NEEDS_REVIEW", error, {}
    return "READY", "READY", proof


def _scan(session, job, task, connector):
    query = select(AttendanceEvent).where(
        AttendanceEvent.connector_id == task.connector_id,
        AttendanceEvent.id > task.cursor,
        AttendanceEvent.id <= task.high_water_id,
    )
    events = session.scalars(_in_scope(job, query).order_by(AttendanceEvent.id).limit(BATCH)).all()
    for event in events:
        state, code, proof = classify(session, event, connector, task)
        session.add(
            Item(
                job_id=job.id,
                source_kind="ATTENDANCE",
                source_ref=str(event.id),
                connector_id=connector.id,
                attendance_event_id=event.id,
                status=state,
                lane=ACTION,
                expected_state_digest=_digest(proof),
                error_code=code,
                result={
                    "reason": explain(code),
                    "proof": proof,
                    "check_snapshot_id": task.snapshot_id,
                },
            )
        )
        task.evidence_digest = _digest([task.evidence_digest, event.id, state, proof])
        task.cursor = event.id
        task.checked_count += 1
    if len(events) < BATCH:
        task.status = "CHECKED"


def counts(session, job):
    rows = dict(
        session.execute(
            select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
        ).all()
    )
    excluded = dict(
        session.execute(
            select(Item.error_code, func.count())
            .where(Item.job_id == job.id, Item.status == "EXCLUDED")
            .group_by(Item.error_code)
        ).all()
    )
    return {
        "checked": sum(rows.values()),
        "ready": rows.get("READY", 0),
        "review": rows.get("NEEDS_REVIEW", 0),
        "waiting": rows.get("WAITING_ORACLE", 0),
        "attention": rows.get("NEEDS_REVIEW", 0)
        + (
            session.scalar(
                select(func.count())
                .select_from(Item)
                .where(
                    Item.job_id == job.id,
                    Item.status == "WAITING_ORACLE",
                    Item.result["needs_attention"].as_boolean().is_(True),
                )
            )
            or 0
        ),
        "confirmed": rows.get("CONFIRMED", 0),
        "stopped": rows.get("STOPPED", 0),
        "skipped": rows.get("SKIPPED", 0),
        "already_confirmed": excluded.get("ALREADY_CONFIRMED", 0),
        "already_delivering": excluded.get("ALREADY_DELIVERING", 0),
        "unavailable_devices": session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.job_id == job.id, Task.status == "UNAVAILABLE")
        )
        or 0,
    }


def _finish_check(session, job):
    tasks = session.scalars(select(Task).where(Task.job_id == job.id).order_by(Task.id)).all()
    if not all(t.status in {"CHECKED", "UNAVAILABLE"} for t in tasks):
        return
    totals = counts(session, job)
    job.candidate_digest = _digest(
        [
            POLICY,
            job.scope["request"],
            [
                [
                    t.connector_id,
                    t.terminal_serial,
                    t.hardware_id,
                    t.high_water_id,
                    t.evidence_digest,
                    t.status,
                ]
                for t in tasks
            ],
        ]
    )
    job.status, job.preview_expires_at = "CHECKED", utc_now() + timedelta(minutes=15)
    job.requested_count, job.eligible_count, job.review_count = (
        totals["checked"],
        totals["ready"],
        totals["review"],
    )


def start(session, job, *, actor, signature, reason, key):
    _lock_scheduler(session)
    session.refresh(job, with_for_update=True)
    if job.actor != actor or job.action != ACTION:
        raise RecoveryError("Open a check made with your administrator account.", "ACTOR_MISMATCH")
    approval = job.scope.get("approval")
    from zk_add.attendance_repair import _protected_digest

    reason = reason.strip()
    if len(reason) < 3:
        raise RecoveryError("Enter a short release reason.", "REASON_REQUIRED")
    if approval:
        if approval["key"] != key or approval["reason_hash"] != _protected_digest(reason):
            raise RecoveryError(
                "This check was already approved with different details.", "IDEMPOTENCY_CONFLICT"
            )
        return job
    if job.status != "CHECKED":
        raise RecoveryError("The terminal sync and check must finish first.", "CHECK_INCOMPLETE")
    verify_recovery_preview(
        digest=job.candidate_digest,
        expires_at=job.preview_expires_at,
        actor=actor,
        action=ACTION,
        signature=signature,
    )
    tasks = session.scalars(
        select(Task).where(Task.job_id == job.id).order_by(Task.connector_id)
    ).all()
    actionable = set(
        session.scalars(
            select(Item.connector_id)
            .where(Item.job_id == job.id, Item.status == "READY")
            .distinct()
        )
    )
    if not actionable:
        raise RecoveryError(
            "No punches in this check can be force released.", "NO_ELIGIBLE_RECORDS"
        )
    for task in tasks:
        if task.connector_id not in actionable:
            continue
        connector = session.scalar(
            select(Connector).where(Connector.id == task.connector_id).with_for_update()
        )
        if not enabled(connector) or not _binding(task, connector):
            raise RecoveryError(explain("ROLLOUT_RESTRICTED"), "ROLLOUT_RESTRICTED")
        active = session.scalar(
            select(Job.id)
            .join(Item, Item.job_id == Job.id)
            .where(
                Item.connector_id == connector.id,
                Job.id != job.id,
                Job.status.in_({"RUNNING", "WAITING_ORACLE", "PAUSED", "STOPPING"}),
                Item.status.in_({"READY", "WAITING_ORACLE"}),
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
        if active or legacy:
            raise RecoveryError(
                "Another release is active for this device. Open that run first.",
                "RELEASE_ALREADY_RUNNING",
            )
        task.status = "SYNC_PENDING"
    job.scope = {
        **job.scope,
        "approval": {
            "key": key,
            "reason_hash": _protected_digest(reason),
            "reason_encrypted": encrypt_text(reason),
            "at": utc_now().isoformat(),
            "digest": job.candidate_digest,
        },
    }
    job.status, job.started_at, job.updated_at = "RUNNING", utc_now(), utc_now()
    append_audit(
        session,
        actor=actor,
        action="ATTENDANCE_FORCE_APPROVED",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome="APPROVED",
        after={
            "count": job.eligible_count,
            "digest": job.candidate_digest,
            "reason_hash": _protected_digest(reason),
        },
    )
    return job


def _skip(item, code, *, skipped=False):
    item.status, item.error_code = "SKIPPED" if skipped else "NEEDS_REVIEW", code
    item.result = {**item.result, "reason": explain(code)}
    item.updated_at = utc_now()


def _apply(session, job, task, connector, item):
    # Match the normal delivery worker's lock order: outbox before attendance.
    outbox = session.scalar(
        select(OrdsOutbox)
        .where(OrdsOutbox.attendance_event_id == item.attendance_event_id)
        .with_for_update()
    )
    event = session.scalar(
        select(AttendanceEvent)
        .where(AttendanceEvent.id == item.attendance_event_id)
        .with_for_update()
    )
    if not event:
        _skip(item, "SOURCE_INVALID")
        return
    state, code, proof = classify(session, event, connector, task)
    if state != "READY" or _digest(proof) != item.expected_state_digest:
        _skip(item, code if state != "READY" else "EVIDENCE_CHANGED", skipped=True)
        return
    user = session.get(DeviceUser, proof["device_user_id"])
    cnic, error = _cnic(user)
    if error:
        _skip(item, error)
        return
    payload = oracle_payload(connector, connector.zkt_device, event, cnic)
    payload["employee_name"] = user.display_name
    from zk_add.attendance_repair import _immutable_facts

    prior = {
        "device_user_id": event.device_user_id,
        "cnic_encrypted": event.cnic_encrypted,
        "display_name": event.display_name,
        "identity_resolution_status": event.identity_resolution_status,
        "ords_status": event.ords_status,
        "identity_snapshot_id": event.identity_snapshot_id,
    }
    decision = Decision(
        item_id=item.id,
        attendance_event_id=event.id,
        job_id=job.id,
        actor=job.actor,
        reason_encrypted=job.scope["approval"]["reason_encrypted"],
        proof={
            **proof,
            "snapshot_id": task.snapshot_id,
            "sync_command_id": task.sync_command_id,
            "immutable_facts": _immutable_facts(event),
            "approval_digest": job.candidate_digest,
        },
        prior_state_encrypted=encrypt_json(prior),
        payload_encrypted=encrypt_json(payload),
        payload_digest=_digest(payload),
        operation_id=str(uuid4()),
    )
    session.add(decision)
    event.manual_release_required = True
    event.device_user_id = user.id
    event.display_name, event.cnic_encrypted = user.display_name, user.cnic_encrypted
    event.cnic_lookup_hash, event.cnic_last4 = user.cnic_lookup_hash, cnic[-4:]
    event.identity_resolution_status = "MANUALLY_APPROVED_CURRENT_TERMINAL"
    event.identity_repaired_at = event.identity_resolved_at = utc_now()
    event.identity_repair_reason = POLICY
    event.ords_status = "PENDING"
    if outbox is None:
        outbox = OrdsOutbox(attendance_event_id=event.id)
        session.add(outbox)
    outbox.status, outbox.delivery_type, outbox.next_attempt_at = "PENDING", "FULL_HISTORY", None
    outbox.last_error, outbox.payload_hash = None, decision.payload_digest
    item.status, item.error_code = "WAITING_ORACLE", None
    item.result = {**item.result, "reason": "Saved for delivery. Waiting for Oracle confirmation."}
    audit = append_audit(
        session,
        actor=job.actor,
        action="ATTENDANCE_FORCE_QUEUED",
        target_type="attendance_event",
        target_id=event.event_uid,
        outcome="WAITING_ORACLE",
        after={"job_id": job.job_id, "policy": POLICY, "payload_digest": decision.payload_digest},
    )
    decision.audit_id = audit.id


def delivery_payload(session, event, connector):
    decision = decision_for(session, event)
    if decision is None:
        return None
    proof, error = identity_proof(session, event, connector)
    if error or any(
        proof.get(k) != decision.proof.get(k)
        for k in ("policy", "source_digest", "identity_key", "hardware_id", "terminal")
    ):
        return None
    try:
        payload = decrypt_json(decision.payload_encrypted)
        return payload if _digest(payload) == decision.payload_digest else None
    except Exception:
        return None


def _release_batch(session, job, task, connector):
    if not enabled(connector):
        _unavailable(task, "ROLLOUT_RESTRICTED")
        return
    if (
        not task.synced_at
        or utc_now() - ensure_utc(task.synced_at) > timedelta(minutes=5)
        or not _snapshot_matches(session, task, connector.zkt_device)
    ):
        task.status = "SYNC_PENDING"
        return
    items = session.scalars(
        select(Item)
        .where(Item.job_id == job.id, Item.connector_id == connector.id, Item.status == "READY")
        .order_by(Item.id)
        .limit(BATCH)
    ).all()
    for item in items:
        _apply(session, job, task, connector, item)
    if len(items) < BATCH:
        task.status = "DONE"


def _observe(session, job):
    items = session.scalars(
        select(Item)
        .where(Item.job_id == job.id, Item.status == "WAITING_ORACLE")
        .order_by(Item.updated_at, Item.id)
        .limit(BATCH)
    ).all()
    for item in items:
        event = session.get(AttendanceEvent, item.attendance_event_id)
        if event.oracle_confirmed_at and event.ords_status in ORDS_ACKNOWLEDGED_STATUSES:
            decision = decision_for(session, event)
            if (
                decision
                and item.result.get("oracle_verified_payload_digest") == decision.payload_digest
            ):
                item.status, item.completed_at = "CONFIRMED", utc_now()
                item.result = {**item.result, "reason": "Oracle confirmed the approved attendance."}
        elif event.ords_status not in {"PENDING", "IN_FLIGHT", "FAILED_RETRYABLE", "RETRYING"}:
            _skip(
                item,
                "ORACLE_CONFLICT"
                if event.ords_status == "QUARANTINED_IDENTITY_CONFLICT"
                else "EVIDENCE_CHANGED",
            )
        item.updated_at = utc_now()


def control(session, job, *, actor, action, key):
    from zk_add.models import AttendanceForceReleaseControl as Control

    _lock_scheduler(session)
    session.refresh(job, with_for_update=True)
    previous = session.scalar(
        select(Control).where(Control.job_id == job.id, Control.request_key == key)
    )
    if previous:
        if previous.actor != actor or previous.action != action:
            raise RecoveryError(
                "The request key belongs to another action.", "IDEMPOTENCY_CONFLICT"
            )
        return job
    if action == "STOP" and job.status not in TERMINAL:
        job.status = "STOPPING"
    elif action == "PAUSE" and job.status in {"RUNNING", "WAITING_ORACLE"}:
        job.status = "PAUSED"
    elif action == "RESUME" and job.status == "PAUSED":
        job.status = "RUNNING"
        for task in session.scalars(
            select(Task).where(
                Task.job_id == job.id,
                Task.status != "UNAVAILABLE",
                Task.connector_id.in_(
                    select(Item.connector_id).where(Item.job_id == job.id, Item.status == "READY")
                ),
            )
        ):
            task.status = "SYNC_PENDING"
    else:
        raise RecoveryError(
            "This run cannot accept that action in its current state.", "CONTROL_CONFLICT"
        )
    session.add(Control(job_id=job.id, request_key=key, actor=actor, action=action))
    job.updated_at = utc_now()
    append_audit(
        session,
        actor=actor,
        action=f"ATTENDANCE_FORCE_{action}",
        target_type="attendance_recovery_job",
        target_id=job.job_id,
        outcome=job.status,
    )
    return job


def advance_once(session):
    _lock_scheduler(session)
    job = session.scalar(
        select(Job)
        .where(Job.action == ACTION, Job.status.in_(ACTIVE))
        .order_by(Job.updated_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not job:
        return
    has_ready = session.scalar(
        select(Item.id).where(Item.job_id == job.id, Item.status == "READY").limit(1)
    )
    if job.status != "CHECKING" and not has_ready:
        _observe(session, job)
    if job.status == "STOPPING":
        for item in session.scalars(
            select(Item)
            .where(Item.job_id == job.id, Item.status == "READY")
            .order_by(Item.id)
            .limit(BATCH)
        ):
            item.status, item.error_code = "STOPPED", "STOPPED"
            item.result = {**item.result, "reason": explain("STOPPED")}
        for task in session.scalars(
            select(Task).where(Task.job_id == job.id, Task.status.not_in({"DONE", "UNAVAILABLE"}))
        ):
            task.status = "DONE"
    elif job.status in {"CHECKING", "RUNNING"}:
        task = session.scalar(
            select(Task)
            .where(
                Task.job_id == job.id,
                Task.status.in_(
                    {"BASELINE", "SYNC_PENDING", "SYNCING", "CHECKING", "RELEASING", "UNAVAILABLE"}
                ),
            )
            .order_by(Task.updated_at, Task.id)
            .limit(1)
        )
        if task:
            connector = session.scalar(
                select(Connector).where(Connector.id == task.connector_id).with_for_update()
            )
            if not _binding(task, connector):
                _unavailable(task, "TERMINAL_CHANGED")
            elif not connector.connected:
                _unavailable(task, "DEVICE_OFFLINE")
            elif settings.attendance_force_release_execution_enabled and not enabled(connector):
                _unavailable(task, "ROLLOUT_RESTRICTED")
            elif task.status == "BASELINE":
                _baseline(session, task, connector)
            elif task.status == "SYNC_PENDING":
                _request_sync(session, job, task, connector)
            elif task.status == "SYNCING":
                _accept_sync(session, task, connector, releasing=job.status == "RUNNING")
            elif task.status == "CHECKING":
                _scan(session, job, task, connector)
            elif task.status == "RELEASING":
                _release_batch(session, job, task, connector)
            if task.status == "UNAVAILABLE" and job.status == "RUNNING":
                for item in session.scalars(
                    select(Item)
                    .where(
                        Item.job_id == job.id,
                        Item.connector_id == task.connector_id,
                        Item.status == "READY",
                    )
                    .order_by(Item.id)
                    .limit(BATCH)
                ):
                    _skip(item, task.error_code, skipped=True)
            task.updated_at = utc_now()
    session.flush()
    if job.status == "CHECKING":
        _finish_check(session, job)
    elif job.status != "PAUSED":
        totals = counts(session, job)
        if not totals["ready"]:
            if totals["waiting"]:
                if job.status != "STOPPING":
                    job.status = "WAITING_ORACLE"
            else:
                job.status = (
                    "STOPPED"
                    if job.status == "STOPPING"
                    else (
                        "COMPLETED_WITH_REVIEW"
                        if totals["review"] or totals["skipped"] or totals["unavailable_devices"]
                        else "COMPLETED"
                    )
                )
                job.completed_at = utc_now()
    job.updated_at = utc_now()


def tick():
    from zk_add.db import session_scope

    try:
        with session_scope() as session:
            advance_once(session)
    except Exception:
        with session_scope() as session:
            job = session.scalar(
                select(Job)
                .where(Job.action == ACTION, Job.status.in_(ACTIVE))
                .order_by(Job.updated_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job:
                job.last_error = (
                    "The last step could not be saved. ADD will retry its committed checkpoint."
                )
                job.updated_at = utc_now()
        raise


def serialize(session, job, actor):
    devices = session.execute(
        select(Task, Connector)
        .join(Connector, Task.connector_id == Connector.id)
        .where(Task.job_id == job.id)
        .order_by(Task.id)
    ).all()
    ready_by_device = dict(
        session.execute(
            select(Item.connector_id, func.count())
            .where(Item.job_id == job.id, Item.status == "READY")
            .group_by(Item.connector_id)
        ).all()
    )
    actionable = set(ready_by_device)
    signature = None
    if (
        job.status == "CHECKED"
        and job.actor == actor
        and job.preview_expires_at
        and ensure_utc(job.preview_expires_at) > utc_now()
    ):
        signature = sign_recovery_preview(
            digest=job.candidate_digest,
            expires_at=job.preview_expires_at,
            actor=actor,
            action=ACTION,
        )
    return {
        "job_id": job.job_id,
        "workflow": ACTION,
        "status": job.status,
        "actor": job.actor,
        "counts": counts(session, job),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "expires_at": job.preview_expires_at,
        "signature": signature,
        "last_error": job.last_error,
        "scope": job.scope["request"],
        "check_complete": job.status != "CHECKING",
        "execution_enabled": bool(actionable)
        and all(enabled(c) for t, c in devices if c.id in actionable),
        "devices": [
            {
                "connector_id": c.connector_id,
                "name": c.display_name,
                "serial": t.terminal_serial,
                "status": t.status,
                "checked": t.checked_count,
                "ready": ready_by_device.get(c.id, 0),
                "synced_at": t.synced_at,
                "error": explain(t.error_code) if t.error_code else None,
            }
            for t, c in devices
        ],
    }


def items_page(session, job, *, cursor=0, limit=50, state=None, search=None):
    query = select(Item, AttendanceEvent, Connector).join(
        AttendanceEvent, Item.attendance_event_id == AttendanceEvent.id
    )
    query = query.join(Connector, Item.connector_id == Connector.id).where(
        Item.job_id == job.id, Item.id > cursor
    )
    if state:
        query = query.where(Item.status == state)
    if search:
        query = query.where(
            or_(
                AttendanceEvent.user_id == search,
                AttendanceEvent.display_name.ilike(f"%{search.replace('%', '').replace('_', '')}%"),
            )
        )
    rows = session.execute(query.order_by(Item.id).limit(limit + 1)).all()
    return {
        "rows": [
            {
                "id": i.id,
                "event_id": e.id,
                "event_uid": e.event_uid,
                "status": i.status,
                "needs_attention": bool(i.result.get("needs_attention")),
                "reason": i.result.get("reason", explain(i.error_code)),
                "name": e.display_name,
                "user_id": e.user_id,
                "device_name": c.display_name,
                "device_serial": e.device_serial,
                "time": e.device_event_time,
                "connector_id": c.connector_id,
            }
            for i, e, c in rows[:limit]
        ],
        "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
    }


def metadata(session, event):
    decision = decision_for(session, event)
    if not decision:
        return None
    job = session.get(Job, decision.job_id)
    item = session.get(Item, decision.item_id)
    return {
        "run_id": job.job_id,
        "administrator": decision.actor,
        "approved_at": job.scope["approval"]["at"],
        "audit_id": decision.audit_id,
        "reason": decrypt_text(decision.reason_encrypted),
        "snapshot_id": decision.proof.get("snapshot_id"),
        "sync_command_id": decision.proof.get("sync_command_id"),
        "needs_attention": bool(
            item.status == "NEEDS_REVIEW" or item.result.get("needs_attention")
        ),
    }
