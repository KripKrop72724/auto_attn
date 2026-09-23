from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import hashlib
import json
import re
import time

import httpx
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from zk_add.db import session_scope
from zk_add.crypto import decrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    AttendanceDeliverySweep,
    Connector,
    ConnectorCredential,
    ConnectorNonce,
    OnboardingNonce,
    DeviceAlert,
    DeviceCommand,
    DeviceLog,
    DeviceTelemetry,
    OrdsOutbox,
    ZKTDevice,
)
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_ACTIVE_STATUSES,
    ORDS_FIRMWARE_UNVERIFIED_STATUSES,
    ORDS_MEMBERSHIP_REVERIFY_STATUSES,
)
from zk_add.identity_states import VERIFIED_IDENTITY_RESOLUTION_STATUSES
from zk_add.realtime import browser_events, connector_hub
from zk_add.provisioning import (
    TERMINAL_STATES as PROVISIONING_TERMINAL_STATES,
    ProvisioningCompanionNonce,
    ProvisioningSession,
    ProvisioningState,
    append_provisioning_event,
)
from zk_add.service import (
    attendance_device_time_is_plausible,
    ACTIVE_COMMAND_STATES,
    MUTATING_COMMANDS,
    advance_user_deletion_jobs,
    apply_user_command_terminal_state,
    oracle_payload,
    queue_due_revokes,
    reconcile_admin_lease_command,
    reconcile_admin_lease_states,
    repair_missing_terminal_provenance,
    repair_attendance_delivery_backlog,
    resolve_alert,
    serialize_command,
    upsert_alert,
)
from zk_add.attendance_recovery import advance_attendance_recovery_jobs
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.reconciliation import (
    assignment_rows,
    refresh_all_reconciliation_assurance,
)


EVENT_UID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ORDS_DELIVERY_BATCH_SIZE = max(1, min(settings.ords_delivery_batch_size, 500))
ORDS_DELIVERY_CONCURRENCY = max(
    1,
    min(settings.ords_delivery_concurrency, ORDS_DELIVERY_BATCH_SIZE),
)
ORDS_FIRMWARE_AUDIT_BATCH_SIZE = max(
    1,
    min(settings.ords_firmware_audit_batch_size, 500),
)
ORDS_PERMANENT_REJECTION_STATUSES = {400, 413, 422}
ORDS_SAFE_TRANSPORT_ERRORS = {
    "ConnectError",
    "ConnectTimeout",
    "NetworkError",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}
ORDS_CIRCUIT_BACKOFF_INITIAL_SECONDS = 15
ORDS_CIRCUIT_BACKOFF_MAX_SECONDS = 300

_ords_request_lock = asyncio.Lock()
_ords_circuit_failures = 0
_ords_circuit_open_until = 0.0


def ords_circuit_is_open(*, now: float | None = None) -> bool:
    """Return whether Oracle transport attempts are temporarily paused."""
    observed_at = time.monotonic() if now is None else now
    return observed_at < _ords_circuit_open_until


def ords_circuit_retry_after_seconds(*, now: float | None = None) -> int:
    """Return the bounded number of seconds before the next route probe."""
    observed_at = time.monotonic() if now is None else now
    return max(0, int(_ords_circuit_open_until - observed_at + 0.999))


def record_ords_route_result(
    *,
    status: int | None,
    transport_error: str | None,
    now: float | None = None,
) -> None:
    """Open the shared circuit only for failures before an HTTP response."""
    global _ords_circuit_failures, _ords_circuit_open_until

    observed_at = time.monotonic() if now is None else now
    if status is not None:
        _ords_circuit_failures = 0
        _ords_circuit_open_until = 0.0
        return
    if not transport_error:
        return

    _ords_circuit_failures += 1
    exponent = min(_ords_circuit_failures - 1, 20)
    delay = min(
        ORDS_CIRCUIT_BACKOFF_MAX_SECONDS,
        ORDS_CIRCUIT_BACKOFF_INITIAL_SECONDS * (2**exponent),
    )
    _ords_circuit_open_until = max(_ords_circuit_open_until, observed_at + delay)


def event_uid_is_valid(value: object) -> bool:
    return isinstance(value, str) and EVENT_UID_PATTERN.fullmatch(value) is not None


def ords_delivery_succeeded(status: int | None, body: object) -> bool:
    """Classify the documented idempotent responses from the live endpoint."""
    if status == 409:
        return True
    return (
        status in {200, 201}
        and isinstance(body, dict)
        and body.get("success") is True
    )


def ords_failure_is_permanent(status: int | None) -> bool:
    """Payload-level client rejections cannot improve by retrying the same event."""
    return status in ORDS_PERMANENT_REJECTION_STATUSES


def ords_failure_category(
    status: int | None, *, transport_error: str | None = None, response_parsed: bool = True
) -> str:
    """Return a bounded non-PII failure category suitable for storage and alerts."""
    if transport_error:
        safe_error = transport_error if transport_error in ORDS_SAFE_TRANSPORT_ERRORS else "Error"
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", safe_error).upper()[:60]
        return f"TRANSPORT_{normalized}"
    if status is None:
        return "TRANSPORT_UNKNOWN"
    if not response_parsed:
        return f"HTTP_{status}_INVALID_JSON"
    return f"HTTP_{status}"


def ords_delivery_metrics(session: Session) -> dict:
    rows = session.execute(
        select(OrdsOutbox.status, func.count(OrdsOutbox.id)).group_by(OrdsOutbox.status)
    ).all()
    counts = {status.lower(): int(count) for status, count in rows}
    active_outbox = sum(
        count for status, count in counts.items() if status.upper() in ORDS_ACTIVE_STATUSES
    )
    firmware_unverified = sum(
        count
        for status, count in counts.items()
        if status.upper() in ORDS_FIRMWARE_UNVERIFIED_STATUSES
    )
    membership_reverify = sum(
        count
        for status, count in counts.items()
        if status.upper() in ORDS_MEMBERSHIP_REVERIFY_STATUSES
    )
    blocked_identity = int(
        session.scalar(
            select(func.count(AttendanceEvent.id)).where(
                AttendanceEvent.ords_status == "BLOCKED_IDENTITY"
            )
        )
        or 0
    )
    quarantined = sum(
        count for status, count in counts.items() if status.upper().startswith("QUARANTINED")
    )
    oldest_outbox_at = session.scalar(
        select(func.min(OrdsOutbox.created_at)).where(
            OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES)
        )
    )
    oldest_blocked_at = session.scalar(
        select(func.min(AttendanceEvent.received_at)).where(
            AttendanceEvent.ords_status == "BLOCKED_IDENTITY"
        )
    )
    oldest_backlog_at = min(
        (value for value in (oldest_outbox_at, oldest_blocked_at) if value is not None),
        default=None,
    )
    last_attempt_at = session.scalar(select(func.max(OrdsOutbox.last_attempt_at)))
    acknowledged = sum(
        count
        for status, count in counts.items()
        if status.upper() in ORDS_ACKNOWLEDGED_STATUSES
    )
    sweep = session.get(AttendanceDeliverySweep, 1)
    missing_outbox = int(
        session.scalar(
            select(func.count(AttendanceEvent.id))
            .outerjoin(OrdsOutbox, OrdsOutbox.attendance_event_id == AttendanceEvent.id)
            .where(
                OrdsOutbox.id.is_(None),
                ~AttendanceEvent.ords_status.in_(
                    tuple(ORDS_ACKNOWLEDGED_STATUSES)
                ),
            )
        )
        or 0
    )
    return {
        "backlog": active_outbox + blocked_identity,
        "pending": counts.get("pending", 0),
        "retrying": counts.get("failed_retryable", 0),
        "in_flight": counts.get("in_flight", 0),
        "blocked_identity": blocked_identity,
        "quarantined": quarantined,
        "acknowledged": acknowledged,
        "acknowledged_add": counts.get("acked", 0),
        "acknowledged_check": counts.get("acked_check", 0),
        "acknowledged_firmware": counts.get("acked_firmware", 0),
        "firmware_unverified": firmware_unverified,
        "membership_reverify": membership_reverify,
        "oldest_backlog_at": oldest_backlog_at,
        "last_attempt_at": last_attempt_at,
        "missing_outbox": missing_outbox,
        "historical_sweep": {
            "state": sweep.state if sweep else "NOT_STARTED",
            "last_event_id": sweep.last_event_id if sweep else None,
            "pages": sweep.pages if sweep else 0,
            "scanned": sweep.scanned_count if sweep else 0,
            "repaired": sweep.repaired_count if sweep else 0,
            "outbox_created": sweep.outbox_created_count if sweep else 0,
            "unresolved": sweep.unresolved_count if sweep else 0,
            "quarantined": sweep.quarantined_count if sweep else 0,
            "updated_at": sweep.updated_at if sweep else None,
        },
    }


async def _control_plane_loop(stop: asyncio.Event) -> None:
    retention_counter = 0
    while not stop.is_set():
        try:
            await maintenance_tick()
            retention_counter += 1
            if retention_counter >= 300:
                retention_counter = 0
                await asyncio.to_thread(retention_tick)
        except Exception as exc:
            await browser_events.publish(
                "backend_error", {"code": "MAINTENANCE_LOOP_ERROR", "message": str(exc)[:500]}
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def _ords_delivery_loop(stop: asyncio.Event) -> None:
    """Drain Oracle independently so route latency never stalls source credits."""

    while not stop.is_set():
        try:
            await deliver_ords_batch()
        except Exception as exc:
            await browser_events.publish(
                "backend_error",
                {"code": "ORDS_DELIVERY_LOOP_ERROR", "message": str(exc)[:500]},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


async def _ords_audit_loop(stop: asyncio.Event) -> None:
    """Run lower-priority Oracle evidence audits outside source scheduling."""

    while not stop.is_set():
        try:
            await asyncio.gather(
                audit_firmware_receipts_batch(),
                audit_confirmed_membership_batch(),
            )
        except Exception as exc:
            await browser_events.publish(
                "backend_error",
                {"code": "ORDS_AUDIT_LOOP_ERROR", "message": str(exc)[:500]},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass


async def _attendance_repair_loop(stop: asyncio.Event) -> None:
    """Advance durable repair checkpoints independently of live delivery."""

    from zk_add.attendance_repair import (
        advance_attendance_repairs_once,
        record_repair_worker_heartbeat,
    )

    while not stop.is_set():
        try:
            await asyncio.to_thread(record_repair_worker_heartbeat, "RUNNING")
            await asyncio.to_thread(lambda: asyncio.run(advance_attendance_repairs_once()))
            await asyncio.to_thread(record_repair_worker_heartbeat, "IDLE")
        except Exception:
            try:
                await asyncio.to_thread(
                    record_repair_worker_heartbeat,
                    "ERROR",
                    "UNHANDLED_EXCEPTION",
                )
            except Exception:
                pass
            await browser_events.publish(
                "backend_error",
                {
                    "code": "ATTENDANCE_REPAIR_LOOP_ERROR",
                    "message": "The durable attendance repair worker encountered an internal error.",
                },
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def _safe_attendance_repair_loop(stop: asyncio.Event) -> None:
    from zk_add.attendance_safe_repair import tick
    from zk_add.attendance_force_release import tick as force_tick
    from zk_add.attendance_direct_ords import tick as direct_tick

    while not stop.is_set():
        for work, code in (
            (tick, "SAFE_ATTENDANCE_REPAIR_WORKER_ERROR"),
            (force_tick, "MANUAL_FORCE_RELEASE_WORKER_ERROR"),
            (direct_tick, "DIRECT_ORDS_WORKER_ERROR"),
        ):
            try:
                await asyncio.to_thread(work)
            except Exception:
                await browser_events.publish(
                    "backend_error",
                    {
                        "code": code,
                        "message": "Attendance delivery will retry its saved checkpoint. Saved punches are preserved.",
                    },
                )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def maintenance_loop(stop: asyncio.Event) -> None:
    tasks = [
        asyncio.create_task(_control_plane_loop(stop)),
        asyncio.create_task(_ords_delivery_loop(stop)),
        asyncio.create_task(_ords_audit_loop(stop)),
        asyncio.create_task(_attendance_repair_loop(stop)),
        asyncio.create_task(_safe_attendance_repair_loop(stop)),
    ]
    try:
        await stop.wait()
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


def reconcile_ords_delivery_alerts(session: Session) -> int:
    """Resolve stale delivery alerts after a connector's durable queue drains.

    Concurrent batch results are applied one row at a time.  A connector's
    final successful row can therefore be processed while another row from the
    same batch still appears active, leaving its alert open even though the
    completed transaction drains the queue moments later.  Maintenance closes
    only that stale alert; permanent rejection and identity alerts are not
    affected.
    """

    active_connector_ids = set(
        session.scalars(
            select(AttendanceEvent.connector_id)
            .join(
                OrdsOutbox,
                OrdsOutbox.attendance_event_id == AttendanceEvent.id,
            )
            .where(OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES))
            .distinct()
        ).all()
    )
    alerts = session.scalars(
        select(DeviceAlert).where(
            DeviceAlert.code == "ORDS_DELIVERY_FAILED",
            DeviceAlert.state == "OPEN",
        )
    ).all()
    resolved = 0
    for alert in alerts:
        if alert.connector_id in active_connector_ids:
            continue
        connector = session.get(Connector, alert.connector_id)
        if connector is None:
            continue
        resolve_alert(session, connector, code="ORDS_DELIVERY_FAILED")
        resolved += 1
    return resolved


async def dispatch_reconciliation_assignments(
    assignments: list[tuple[str, dict]],
) -> None:
    async def offer(connector_id: str, assignment: dict) -> None:
        if await connector_hub.send(connector_id, assignment):
            await browser_events.publish(
                "reconciliation",
                {
                    "job_id": assignment["job_id"],
                    "phase": "ASSIGNMENT_OFFERED",
                    "connector_id": connector_id,
                },
            )

    await asyncio.gather(
        *(offer(connector_id, assignment) for connector_id, assignment in assignments)
    )


def prepare_maintenance_tick(
    now: datetime,
) -> tuple[list[tuple[str, dict]], list[dict], list[dict], list[tuple[str, dict]], list[dict]]:
    """Run the control-plane database work outside the server event loop."""

    dispatch: list[tuple[str, dict]] = []
    connector_updates: list[dict] = []
    reconciliation_updates: list[dict] = []
    reconciliation_dispatch: list[tuple[str, dict]] = []
    provisioning_updates: list[dict] = []
    with session_scope() as session:
        for connector in session.scalars(select(Connector).where(Connector.active == True)):  # noqa: E712
            if connector.last_seen_at and connector.last_seen_at + timedelta(seconds=settings.offline_after_seconds) < now:
                if connector.connected or connector.lifecycle_state != "OFFLINE":
                    connector.connected = False
                    connector.lifecycle_state = "OFFLINE"
                    connector.last_disconnect_at = now
                    upsert_alert(
                        session,
                        connector,
                        code="ESP_OFFLINE",
                        severity="HIGH",
                        message="ESP heartbeat is stale.",
                    )
                    connector_updates.append({"connector_id": connector.connector_id, "state": "OFFLINE"})
        advance_user_deletion_jobs(session)
        repair_missing_terminal_provenance(session)
        repair_attendance_delivery_backlog(session, limit=ORDS_DELIVERY_BATCH_SIZE)
        advance_attendance_recovery_jobs(
            session,
            limit=settings.attendance_recovery_batch_size,
            owner="maintenance",
        )
        reconcile_ords_delivery_alerts(session)
        reconcile_admin_lease_states(session)
        from zk_add.comm_keys import expire_staged_comm_key_operations

        expire_staged_comm_key_operations(session)
        for provisioning in session.scalars(
            select(ProvisioningSession).where(
                ProvisioningSession.expires_at <= now,
                ProvisioningSession.state.not_in(
                    [state.value for state in PROVISIONING_TERMINAL_STATES]
                ),
            )
        ).all():
            locally_safe = provisioning.state in {
                ProvisioningState.LOCAL_VERIFIED.value,
                ProvisioningState.BOOT_VERIFYING.value,
                ProvisioningState.WAITING_FOR_ONBOARDING.value,
                ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION.value,
                ProvisioningState.VERIFYING_SITE.value,
            }
            target = (
                ProvisioningState.SITE_VALIDATION_PENDING
                if locally_safe
                else ProvisioningState.EXPIRED
            )
            append_provisioning_event(
                session,
                provisioning,
                state=target.value,
                progress=provisioning.progress,
                source="SERVER",
                details={
                    "reason": "Destination validation did not complete before session expiry."
                    if locally_safe
                    else "Provisioning authorization expired before local verification."
                },
            )
            provisioning_updates.append(
                {"session_id": provisioning.session_id, "state": provisioning.state}
            )
        for command in session.scalars(
            select(DeviceCommand)
            .where(DeviceCommand.status.in_(ACTIVE_COMMAND_STATES))
            .order_by(DeviceCommand.created_at.asc())
        ):
            connector = session.get(Connector, command.connector_id)
            if command.expires_at and command.expires_at <= now:
                command.status = "EXPIRED"
                command.completed_at = now
                command.error_code = "COMMAND_EXPIRED"
                command.error_message = "Command expired before verified completion."
                if command.command_type == "APPLY_CONFIG" and connector is not None:
                    from zk_add.comm_keys import reconcile_comm_key_command

                    reconcile_comm_key_command(
                        session,
                        connector=connector,
                        command=command,
                        status="EXPIRED",
                        result={},
                        error_code=command.error_code,
                    )
                apply_user_command_terminal_state(session, command=command, status="EXPIRED")
                reconcile_admin_lease_command(session, command=command, now=now)
                continue
            if connector is None:
                continue
            if command.status == "CANCEL_REQUESTED":
                retry_due = not command.dispatched_at or (
                    command.dispatched_at
                    + timedelta(seconds=settings.command_redispatch_seconds)
                    <= now
                )
                if connector.connected and retry_due:
                    dispatch.append((connector.connector_id, serialize_command(command)))
                continue
            if not connector.connected:
                command.status = "WAITING_FOR_DEVICE"
                continue
            zkt = connector.zkt_device
            if (
                command.command_type in MUTATING_COMMANDS
                and connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
            ):
                command.status = "WAITING_FOR_ZKT"
                command.error_code = "QUARANTINED_DUPLICATE_SERIAL"
                command.error_message = "All writes are blocked while this serial has multiple claimants."
                continue
            if command.command_type in MUTATING_COMMANDS and zkt and (
                not zkt.online
                or zkt.connection_state in {"OFFLINE", "FLAPPING", "RETRY_WAIT", "DISCOVERING"}
            ):
                command.status = "WAITING_FOR_ZKT"
                continue
            if command.dispatched_at and (
                command.dispatched_at
                + timedelta(seconds=settings.command_redispatch_seconds)
                > now
            ):
                continue
            if command.attempt_count > 0:
                command.status = "RETRYING"
            dispatch.append((connector.connector_id, serialize_command(command)))
        for command in queue_due_revokes(session):
            connector = session.get(Connector, command.connector_id)
            if connector:
                dispatch.append((connector.connector_id, serialize_command(command)))
        # Grant terminal-history assignments in the short control-plane
        # transaction.  Assurance refresh can scan large immutable manifests
        # and must never hold this transaction open; otherwise a new job can
        # remain QUEUED/PREFLIGHT with no assignment even while its terminal is
        # online.  The assignment and checkpoint are committed before the
        # slower assurance pass below starts.
        reconciliation_dispatch = assignment_rows(session)

    # Oracle assurance is deliberately isolated from assignment dispatch.  A
    # large historical job can require indexed manifest/status aggregation;
    # it must not starve new terminal captures or make the scheduler appear
    # idle.  A fresh session also prevents assurance locks from delaying the
    # connector dispatch transaction above.
    with session_scope() as assurance_session:
        reconciliation_updates = refresh_all_reconciliation_assurance(assurance_session)
    return (
        dispatch,
        connector_updates,
        reconciliation_updates,
        reconciliation_dispatch,
        provisioning_updates,
    )


def prepare_reconciliation_dispatch() -> list[tuple[str, dict]]:
    """Grant pending terminal scans in an isolated recovery transaction.

    The general maintenance tick owns several independent repairers.  A
    failure in any one of those repairers must not prevent a queued
    reconciliation from receiving its terminal assignment.  This fallback is
    deliberately idempotent: an already leased job is ignored by
    ``assignment_rows`` and no source or delivery rows are changed here.
    """

    with session_scope() as session:
        return assignment_rows(session)


def mark_command_dispatched(command_id: str) -> None:
    with session_scope() as session:
        command = session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_id == command_id)
        )
        if command and command.status in {
            "QUEUED",
            "WAITING_FOR_DEVICE",
            "WAITING_FOR_ZKT",
            "RETRYING",
            "DISPATCHED",
            "ACKNOWLEDGED",
            "RUNNING",
            "CANCEL_REQUESTED",
        }:
            if command.status != "CANCEL_REQUESTED":
                command.status = "DISPATCHED"
            command.dispatched_at = utc_now()
            command.attempt_count += 1


async def maintenance_tick() -> None:
    try:
        (
            dispatch,
            connector_updates,
            reconciliation_updates,
            reconciliation_dispatch,
            provisioning_updates,
        ) = await asyncio.to_thread(prepare_maintenance_tick, utc_now())
    except Exception as exc:
        # Keep the control-plane error visible, but still give queued
        # reconciliation jobs an independent chance to acquire a scan lease.
        # Without this guard, an unrelated repair failure can leave a job in
        # QUEUED/PREFLIGHT with no assignment indefinitely.
        await browser_events.publish(
            "backend_error", {"code": "MAINTENANCE_LOOP_ERROR", "message": str(exc)[:500]}
        )
        dispatch = []
        connector_updates = []
        reconciliation_updates = []
        provisioning_updates = []
        reconciliation_dispatch = []
        try:
            reconciliation_dispatch = await asyncio.to_thread(
                prepare_reconciliation_dispatch
            )
        except Exception as dispatch_exc:
            await browser_events.publish(
                "backend_error",
                {
                    "code": "RECONCILIATION_DISPATCH_ERROR",
                    "message": str(dispatch_exc)[:500],
                },
            )
    for connector_id, update in dispatch:
        if await connector_hub.send(connector_id, update):
            await asyncio.to_thread(mark_command_dispatched, update["command_id"])
    for update in connector_updates:
        await browser_events.publish("device", update)
    for update in provisioning_updates:
        await browser_events.publish("provisioning", update)
    for update in reconciliation_updates:
        await browser_events.publish("reconciliation", update)
    await dispatch_reconciliation_assignments(reconciliation_dispatch)


def fair_ords_candidate_order(
    session: Session, candidates: list[OrdsOutbox]
) -> list[OrdsOutbox]:
    priority = [row for row in candidates if row.delivery_type != "FULL_HISTORY"]
    buckets: dict[int, deque[OrdsOutbox]] = {}
    for row in candidates:
        if row.delivery_type != "FULL_HISTORY":
            continue
        event = (
            session.get(AttendanceEvent, row.attendance_event_id)
            if row.attendance_event_id
            else None
        )
        # Invalid/missing event rows still get a stable bucket so validation
        # can quarantine them without stalling valid connector histories.
        connector_id = event.connector_id if event is not None else -row.id
        buckets.setdefault(connector_id, deque()).append(row)
    history: list[OrdsOutbox] = []
    while buckets:
        for connector_id in list(buckets):
            bucket = buckets[connector_id]
            history.append(bucket.popleft())
            if not bucket:
                del buckets[connector_id]
    return [*priority, *history]


def claim_ords_batch(limit: int) -> list[tuple[int, dict, int, bool]]:
    claims: list[tuple[int, dict, int, bool]] = []
    with session_scope() as session:
        now = utc_now()
        # A process interruption after claiming a row must not strand it in
        # IN_FLIGHT forever.  No normal request remains active this long.
        stale_before = now - timedelta(seconds=max(60, int(settings.ords_timeout_seconds * 3)))
        for stale in session.scalars(
            select(OrdsOutbox)
            .where(
                OrdsOutbox.status == "IN_FLIGHT",
                OrdsOutbox.last_attempt_at < stale_before,
            )
            .order_by(OrdsOutbox.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all():
            stale.status = "FAILED_RETRYABLE"
            stale.next_attempt_at = now
            stale.last_error = "RECOVERED_STALE_IN_FLIGHT"
            event = (
                session.get(AttendanceEvent, stale.attendance_event_id)
                if stale.attendance_event_id
                else None
            )
            if event:
                event.ords_status = "FAILED_RETRYABLE"

        from zk_add.models import AttendanceDeliverySchedule
        from sqlalchemy.exc import IntegrityError

        schedule = session.scalar(
            select(AttendanceDeliverySchedule)
            .where(AttendanceDeliverySchedule.id == 1)
            .with_for_update()
        )
        if schedule is None:
            try:
                with session.begin_nested():
                    session.add(AttendanceDeliverySchedule(id=1))
                    session.flush()
            except IntegrityError:
                pass  # A concurrent first claimant created the singleton.
            schedule = session.scalar(
                select(AttendanceDeliverySchedule)
                .where(AttendanceDeliverySchedule.id == 1)
                .with_for_update()
            )
        eligible = [
            OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE"]),
            OrdsOutbox.next_attempt_at.is_(None) | (OrdsOutbox.next_attempt_at <= now),
        ]
        priority = deque(
            session.scalars(
                select(OrdsOutbox)
                .where(*eligible, OrdsOutbox.delivery_type != "FULL_HISTORY")
                .order_by(OrdsOutbox.id)
                .limit(max(100, limit * 4))
                .with_for_update(skip_locked=True)
            ).all()
        )
        # Select background independently; a large live queue cannot hide it
        # behind a LIMIT. Rank one connector at a time, rotating after service.
        ranked = (
            select(
                OrdsOutbox.id.label("row_id"),
                AttendanceEvent.connector_id.label("connector_id"),
                func.row_number()
                .over(partition_by=AttendanceEvent.connector_id, order_by=OrdsOutbox.id)
                .label("position"),
            )
            .join(AttendanceEvent, AttendanceEvent.id == OrdsOutbox.attendance_event_id)
            .where(*eligible, OrdsOutbox.delivery_type == "FULL_HISTORY")
            .subquery()
        )
        background_ids = (
            select(ranked.c.row_id)
            .order_by(
                ranked.c.position,
                case((ranked.c.connector_id > schedule.last_connector_id, 0), else_=1),
                ranked.c.connector_id,
            )
            .limit(max(100, limit))
        )
        background_rows = session.scalars(
            select(OrdsOutbox)
            .where(OrdsOutbox.id.in_(background_ids))
            .with_for_update(skip_locked=True)
        ).all()
        indexed = {row.id: row for row in background_rows}
        background = deque(
            indexed[row_id] for row_id in session.scalars(background_ids).all() if row_id in indexed
        )
        while priority or background:
            use_background = bool(background) and (schedule.priority_served >= 4 or not priority)
            row = background.popleft() if use_background else priority.popleft()
            # Count validation attempts too: corrupt rows must not monopolize a slice.
            if use_background:
                schedule.priority_served = 0
                source = session.get(AttendanceEvent, row.attendance_event_id)
                schedule.last_connector_id = (
                    source.connector_id if source else schedule.last_connector_id
                )
            else:
                schedule.priority_served = min(4, schedule.priority_served + 1)
            was_retry = row.status == "FAILED_RETRYABLE"
            event = (
                session.get(AttendanceEvent, row.attendance_event_id)
                if row.attendance_event_id
                else None
            )
            from zk_add.attendance_manual_guard import delivery_authorized, decision_for
            decision = decision_for(session, event) if event else None
            if decision and decision.proof.get("policy") == "manual-direct-ords-v1":
                connector = session.get(Connector, event.connector_id)
                from zk_add.attendance_force_release import delivery_payload

                candidate_payload = delivery_payload(session, event, connector)
                if candidate_payload is None:
                    row.status = event.ords_status = "BLOCKED_IDENTITY"
                    row.last_error = "The approved punch changed or its user/CNIC is unavailable."
                    row.next_attempt_at = None
                    continue
                row.status = "IN_FLIGHT"
                row.attempt_count += 1
                row.last_attempt_at = now
                row.next_attempt_at = None
                row.last_error = None
                row.payload_hash = hashlib.sha256(
                    json.dumps(candidate_payload, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest()
                claims.append((row.id, candidate_payload, connector.id, was_retry))
                if len(claims) >= limit:
                    break
                continue
            event_uid = event.event_uid if event else None
            if not event_uid_is_valid(event_uid):
                row.status = "QUARANTINED_INVALID_EVENT_UID"
                row.last_error = "Rejected before ORDS delivery: event_uid is not a 64-character lowercase hex digest."
                if event:
                    event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                continue
            connector = session.get(Connector, event.connector_id)
            zkt = session.get(ZKTDevice, event.zkt_device_id)
            if not delivery_authorized(session, event):
                row.status = event.ords_status = "BLOCKED_IDENTITY"
                row.last_error = "Administrator approval is required. Open Force release attendance."
                row.next_attempt_at = None
                continue
            from zk_add.attendance_recovery import _terminal_provenance_verified

            if (
                connector is None
                or zkt is None
                or not _terminal_provenance_verified(event, connector)
            ):
                row.status = event.ords_status = "QUARANTINED_MISSING_TERMINAL_PROVENANCE"
                row.last_error = "Verified terminal provenance is required before Oracle delivery."
                row.next_attempt_at = None
                continue
            try:
                cnic = decrypt_cnic(event.cnic_encrypted)
            except Exception:
                # A malformed protected identity must remain held instead of
                # crashing the whole delivery claim transaction.
                cnic = None
            identity_unverified = (
                settings.identity_snapshot_gate_enabled
                and event.identity_resolution_status not in VERIFIED_IDENTITY_RESOLUTION_STATUSES
            )
            if connector is None or zkt is None or not cnic or identity_unverified:
                row.status = "BLOCKED_IDENTITY"
                event.ords_status = "BLOCKED_IDENTITY"
                continue
            if event.clock_quality == "INVALID" or not attendance_device_time_is_plausible(
                event.device_event_time, event.captured_at
            ):
                row.status = event.ords_status = "QUARANTINED_INVALID_DEVICE_TIME"
                row.next_attempt_at = None
                continue
            from zk_add.attendance_safe_repair import delivery_proof_valid

            if not delivery_proof_valid(session, event, connector):
                row.status = event.ords_status = "BLOCKED_IDENTITY"
                row.last_error = (
                    "The saved identity evidence changed. A new repair check is required."
                )
                continue
            candidate_payload = oracle_payload(connector, zkt, event, cnic)
            if decision_for(session, event):
                from zk_add.attendance_force_release import delivery_payload
                candidate_payload = delivery_payload(session, event, connector)
                if candidate_payload is None:
                    row.status = event.ords_status = "BLOCKED_IDENTITY"
                    row.last_error = "The approved identity or source changed. Review this forced release."
                    row.next_attempt_at = None
                    continue
            row.status = "IN_FLIGHT"
            row.attempt_count += 1
            row.last_attempt_at = now
            row.next_attempt_at = None
            row.last_error = None
            row.payload_hash = hashlib.sha256(
                json.dumps(candidate_payload, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            claims.append((row.id, candidate_payload, connector.id, was_retry))
            if len(claims) >= limit:
                break
    return claims


async def post_ords_claim(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    claim: tuple[int, dict, int, bool],
) -> tuple[int, int, int | None, object, str | None, bool]:
    row_id, payload, connector_id, _was_retry = claim
    status: int | None = None
    body: object = None
    transport_error: str | None = None
    response_parsed = True
    try:
        async with semaphore:
            response = await client.post(url, json=payload)
        status = response.status_code
        if response.content:
            try:
                body = response.json()
            except ValueError:
                response_parsed = False
        elif status != 409:
            response_parsed = False
    except Exception as exc:
        transport_error = type(exc).__name__
        response_parsed = False
    return row_id, connector_id, status, body, transport_error, response_parsed


async def post_ords_membership_check(
    client: httpx.AsyncClient,
    url: str,
    claims: list[tuple[int, dict, int, bool]],
) -> tuple[int | None, object, str | None, bool]:
    status: int | None = None
    body: object = None
    transport_error: str | None = None
    response_parsed = True
    try:
        response = await client.post(
            url,
            json={"event_uids": [claim[1]["event_uid"] for claim in claims]},
        )
        status = response.status_code
        if response.content:
            try:
                body = response.json()
            except ValueError:
                response_parsed = False
        else:
            response_parsed = False
    except Exception as exc:
        transport_error = type(exc).__name__
        response_parsed = False
    return status, body, transport_error, response_parsed


def ords_membership_missing(
    status: int | None,
    body: object,
    requested: set[str],
) -> set[str] | None:
    if status != 200 or not isinstance(body, dict) or body.get("success") is not True:
        return None
    missing = body.get("missing_event_uids")
    if not isinstance(missing, list) or any(not event_uid_is_valid(item) for item in missing):
        return None
    missing_set = set(missing)
    if len(missing_set) != len(missing) or not missing_set.issubset(requested):
        return None
    if body.get("received_count") != len(requested):
        return None
    if body.get("missing_count") != len(missing_set):
        return None
    if body.get("existing_count") != len(requested) - len(missing_set):
        return None
    return missing_set


def claim_firmware_receipt_audit_batch(
    limit: int = ORDS_FIRMWARE_AUDIT_BATCH_SIZE,
) -> list[tuple[int, str, int]]:
    claims: list[tuple[int, str, int]] = []
    with session_scope() as session:
        now = utc_now()
        stale_before = now - timedelta(
            seconds=max(60, int(settings.ords_timeout_seconds * 3))
        )
        for stale in session.scalars(
            select(OrdsOutbox)
            .where(
                OrdsOutbox.status == "FIRMWARE_RECEIPT_VERIFYING",
                (OrdsOutbox.last_attempt_at == None)  # noqa: E711
                | (OrdsOutbox.last_attempt_at < stale_before),
            )
            .limit(max(1, limit))
        ).all():
            stale.status = "FIRMWARE_RECEIPT_UNVERIFIED"
            stale.next_attempt_at = now
            stale.acknowledged_at = None
            stale.last_error = "RECOVERED_STALE_FIRMWARE_RECEIPT_CHECK"
            event = (
                session.get(AttendanceEvent, stale.attendance_event_id)
                if stale.attendance_event_id
                else None
            )
            if event is not None:
                event.ords_status = "FIRMWARE_RECEIPT_UNVERIFIED"
                event.oracle_confirmed_at = None
                event.oracle_confirmation_path = None

        candidates = session.scalars(
            select(OrdsOutbox)
            .where(
                OrdsOutbox.status.in_(
                    ["ACKED_FIRMWARE", "FIRMWARE_RECEIPT_UNVERIFIED"]
                ),
                (OrdsOutbox.next_attempt_at == None)  # noqa: E711
                | (OrdsOutbox.next_attempt_at <= now),
            )
            .order_by(OrdsOutbox.id.asc())
            .limit(max(1, min(limit, 500)))
            .with_for_update(skip_locked=True)
        ).all()
        for row in candidates:
            event = (
                session.get(AttendanceEvent, row.attendance_event_id)
                if row.attendance_event_id
                else None
            )
            if event is None or not event_uid_is_valid(event.event_uid):
                row.status = "QUARANTINED_INVALID_EVENT_UID"
                row.next_attempt_at = None
                row.last_error = (
                    "Firmware receipt cannot be verified because its attendance "
                    "event_uid is invalid or missing."
                )
                if event is not None:
                    event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                continue
            row.status = "FIRMWARE_RECEIPT_VERIFYING"
            from zk_add.attendance_manual_guard import generic_confirmation_allowed, decision_for
            if not generic_confirmation_allowed(session, event):
                row.status = event.ords_status = "FAILED_RETRYABLE" if decision_for(session, event) else "BLOCKED_IDENTITY"
                row.next_attempt_at = None
                row.last_error = "CONTENT_VERIFICATION_REQUIRED" if decision_for(session, event) else "MANUAL_APPROVAL_REQUIRED"
                continue
            row.attempt_count += 1
            row.last_attempt_at = now
            row.next_attempt_at = None
            row.acknowledged_at = None
            row.last_error = None
            event.ords_status = "FIRMWARE_RECEIPT_VERIFYING"
            event.oracle_confirmed_at = None
            event.oracle_confirmation_path = None
            claims.append((row.id, event.event_uid, event.connector_id))
    return claims


def apply_firmware_receipt_audit_failure(
    session: Session,
    *,
    claimed_id: int,
    status: int | None,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = (
        session.get(AttendanceEvent, row.attendance_event_id)
        if row.attendance_event_id
        else None
    )
    connector = session.get(Connector, event.connector_id) if event else None
    category = ords_failure_category(
        status,
        transport_error=transport_error,
        response_parsed=response_parsed,
    )
    row.status = "FIRMWARE_RECEIPT_UNVERIFIED"
    row.acknowledged_at = None
    row.last_http_status = status
    row.last_error = f"FIRMWARE_CHECK_{category}"
    row.next_attempt_at = utc_now() + timedelta(
        seconds=min(600, 2 ** min(row.attempt_count, 9))
    )
    if event is not None:
        event.ords_status = "FIRMWARE_RECEIPT_UNVERIFIED"
        event.oracle_confirmed_at = None
        event.oracle_confirmation_path = None
    if connector is not None:
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity="HIGH" if status in {401, 403} else "WARNING",
            message=(
                "A firmware receipt is awaiting direct Oracle membership proof; "
                "the preserved event remains auditable in ADD."
            ),
            details={
                "failure_category": f"FIRMWARE_CHECK_{category}",
                "http_status": status,
                "attempt_count": row.attempt_count,
            },
        )


def apply_firmware_receipt_missing(
    session: Session,
    *,
    claimed_id: int,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = (
        session.get(AttendanceEvent, row.attendance_event_id)
        if row.attendance_event_id
        else None
    )
    connector = session.get(Connector, event.connector_id) if event else None
    now = utc_now()
    row.status = "PENDING"
    row.acknowledged_at = None
    row.last_http_status = 200
    row.last_error = "FIRMWARE_RECEIPT_NOT_IN_ORACLE"
    row.next_attempt_at = now
    if event is not None:
        event.ords_status = "PENDING"
        event.oracle_confirmed_at = None
        event.oracle_confirmation_path = None
    if connector is not None:
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity="HIGH",
            message=(
                "Oracle did not contain a firmware-confirmed attendance event; "
                "ADD automatically requeued the preserved event."
            ),
            details={
                "failure_category": "FIRMWARE_RECEIPT_NOT_IN_ORACLE",
                "http_status": 200,
            },
        )


def apply_firmware_receipt_audit_results(
    claims: list[tuple[int, str, int]],
    missing: set[str] | None,
    *,
    status: int | None,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    with session_scope() as session:
        if missing is None:
            for row_id, _event_uid, _connector_id in claims:
                apply_firmware_receipt_audit_failure(
                    session,
                    claimed_id=row_id,
                    status=status,
                    transport_error=transport_error,
                    response_parsed=response_parsed,
                )
            return
        for row_id, event_uid, _connector_id in claims:
            if event_uid in missing:
                apply_firmware_receipt_missing(session, claimed_id=row_id)
            else:
                apply_ords_confirmation(
                    session,
                    claimed_id=row_id,
                    path="FIRMWARE_RECEIPT_MEMBERSHIP_CHECK",
                )


async def audit_firmware_receipts_batch(
    *,
    limit: int = ORDS_FIRMWARE_AUDIT_BATCH_SIZE,
) -> None:
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return
    async with _ords_request_lock:
        if ords_circuit_is_open():
            return
        claims = await asyncio.to_thread(
            claim_firmware_receipt_audit_batch,
            max(1, min(limit, 500)),
        )
        if not claims:
            return
        check_url = settings.ords_base_url.rstrip("/") + "/raw-captures/check"
        async with httpx.AsyncClient(
            timeout=settings.ords_timeout_seconds,
            headers={
                "X-API-Username": settings.ords_username,
                "X-API-Password": settings.ords_password,
            },
        ) as client:
            check_status, check_body, check_error, check_parsed = (
                await post_ords_membership_check(
                    client,
                    check_url,
                    [
                        (row_id, {"event_uid": event_uid}, connector_id, True)
                        for row_id, event_uid, connector_id in claims
                    ],
                )
            )
        record_ords_route_result(
            status=check_status,
            transport_error=check_error,
        )
    requested = {event_uid for _row_id, event_uid, _connector_id in claims}
    missing = ords_membership_missing(check_status, check_body, requested)
    await asyncio.to_thread(
        apply_firmware_receipt_audit_results,
        claims,
        missing,
        status=check_status,
        transport_error=check_error,
        response_parsed=check_parsed,
    )


def claim_confirmed_membership_audit_batch(
    limit: int = ORDS_FIRMWARE_AUDIT_BATCH_SIZE,
) -> list[tuple[int, str, int]]:
    from zk_add.models import AttendanceForceReleaseDecision
    claims: list[tuple[int, str, int]] = []
    with session_scope() as session:
        now = utc_now()
        stale_before = now - timedelta(
            seconds=max(60, int(settings.ords_timeout_seconds * 3))
        )
        for stale in session.scalars(
            select(OrdsOutbox)
            .where(
                OrdsOutbox.status == "MEMBERSHIP_REVERIFYING",
                (OrdsOutbox.last_attempt_at == None)  # noqa: E711
                | (OrdsOutbox.last_attempt_at < stale_before),
            )
            .limit(max(1, limit))
        ).all():
            stale.status = "MEMBERSHIP_REVERIFY_RETRY"
            stale.next_attempt_at = now
            stale.last_error = "RECOVERED_STALE_MEMBERSHIP_REVERIFY"
            event = (
                session.get(AttendanceEvent, stale.attendance_event_id)
                if stale.attendance_event_id
                else None
            )
            if event is not None:
                event.ords_status = "MEMBERSHIP_REVERIFY_RETRY"

        due_before = now - timedelta(
            seconds=max(60, settings.ords_membership_reverify_seconds)
        )
        proof_at = func.coalesce(
            AttendanceEvent.oracle_confirmed_at,
            OrdsOutbox.acknowledged_at,
            OrdsOutbox.created_at,
        )
        candidates = session.scalars(
            select(OrdsOutbox)
            .join(
                AttendanceEvent,
                AttendanceEvent.id == OrdsOutbox.attendance_event_id,
            )
            .where(
                AttendanceEvent.id.not_in(select(AttendanceForceReleaseDecision.attendance_event_id)),
                or_(
                    (
                        OrdsOutbox.status.in_(["ACKED", "ACKED_CHECK"])
                        & (proof_at <= due_before)
                        & (
                            (OrdsOutbox.last_attempt_at == None)  # noqa: E711
                            | (OrdsOutbox.last_attempt_at <= proof_at)
                        )
                    ),
                    (
                        (OrdsOutbox.status == "MEMBERSHIP_REVERIFY_RETRY")
                        & (
                            (OrdsOutbox.next_attempt_at == None)  # noqa: E711
                            | (OrdsOutbox.next_attempt_at <= now)
                        )
                    ),
                )
            )
            .order_by(
                case(
                    (OrdsOutbox.status == "MEMBERSHIP_REVERIFY_RETRY", 0),
                    else_=1,
                ),
                OrdsOutbox.id.asc(),
            )
            .limit(max(1, min(limit, 500)))
            .with_for_update(skip_locked=True)
        ).all()
        for row in candidates:
            event = (
                session.get(AttendanceEvent, row.attendance_event_id)
                if row.attendance_event_id
                else None
            )
            if event is None or not event_uid_is_valid(event.event_uid):
                row.status = "QUARANTINED_INVALID_EVENT_UID"
                row.next_attempt_at = None
                row.last_error = (
                    "Oracle membership cannot be reverified because the "
                    "attendance event_uid is invalid or missing."
                )
                if event is not None:
                    event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                    event.oracle_confirmed_at = None
                    event.oracle_confirmation_path = None
                continue
            row.status = "MEMBERSHIP_REVERIFYING"
            row.attempt_count += 1
            row.last_attempt_at = now
            row.next_attempt_at = None
            row.last_error = None
            event.ords_status = "MEMBERSHIP_REVERIFYING"
            claims.append((row.id, event.event_uid, event.connector_id))
    return claims


def apply_confirmed_membership_audit_failure(
    session: Session,
    *,
    claimed_id: int,
    status: int | None,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = (
        session.get(AttendanceEvent, row.attendance_event_id)
        if row.attendance_event_id
        else None
    )
    connector = session.get(Connector, event.connector_id) if event else None
    category = ords_failure_category(
        status,
        transport_error=transport_error,
        response_parsed=response_parsed,
    )
    row.status = "MEMBERSHIP_REVERIFY_RETRY"
    row.last_http_status = status
    row.last_error = f"REVERIFY_{category}"
    row.next_attempt_at = utc_now() + timedelta(
        seconds=min(600, 2 ** min(row.attempt_count, 9))
    )
    if event is not None:
        event.ords_status = "MEMBERSHIP_REVERIFY_RETRY"
    if connector is not None:
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity="HIGH" if status in {401, 403} else "WARNING",
            message=(
                "Periodic Oracle membership verification is retrying; "
                "the prior confirmation and preserved ADD event remain auditable."
            ),
            details={
                "failure_category": f"REVERIFY_{category}",
                "http_status": status,
                "attempt_count": row.attempt_count,
            },
        )


def apply_confirmed_membership_missing(
    session: Session,
    *,
    claimed_id: int,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = (
        session.get(AttendanceEvent, row.attendance_event_id)
        if row.attendance_event_id
        else None
    )
    connector = session.get(Connector, event.connector_id) if event else None
    now = utc_now()
    row.status = "PENDING"
    row.acknowledged_at = None
    row.last_http_status = 200
    row.last_error = "CONFIRMED_EVENT_MISSING_FROM_ORACLE"
    row.next_attempt_at = now
    if event is not None:
        event.ords_status = "PENDING"
        event.oracle_confirmed_at = None
        event.oracle_confirmation_path = None
    if connector is not None:
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity="HIGH",
            message=(
                "A previously confirmed attendance event is missing from Oracle; "
                "ADD automatically requeued its preserved copy."
            ),
            details={
                "failure_category": "CONFIRMED_EVENT_MISSING_FROM_ORACLE",
                "http_status": 200,
            },
        )


def apply_confirmed_membership_audit_results(
    claims: list[tuple[int, str, int]],
    missing: set[str] | None,
    *,
    status: int | None,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    with session_scope() as session:
        if missing is None:
            for row_id, _event_uid, _connector_id in claims:
                apply_confirmed_membership_audit_failure(
                    session,
                    claimed_id=row_id,
                    status=status,
                    transport_error=transport_error,
                    response_parsed=response_parsed,
                )
            return
        for row_id, event_uid, _connector_id in claims:
            if event_uid in missing:
                apply_confirmed_membership_missing(session, claimed_id=row_id)
            else:
                apply_ords_confirmation(
                    session,
                    claimed_id=row_id,
                    path="PERIODIC_MEMBERSHIP_REVERIFY",
                )


async def audit_confirmed_membership_batch(
    *,
    limit: int = ORDS_FIRMWARE_AUDIT_BATCH_SIZE,
) -> None:
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return
    async with _ords_request_lock:
        if ords_circuit_is_open():
            return
        claims = await asyncio.to_thread(
            claim_confirmed_membership_audit_batch,
            max(1, min(limit, 500)),
        )
        if not claims:
            return
        check_url = settings.ords_base_url.rstrip("/") + "/raw-captures/check"
        async with httpx.AsyncClient(
            timeout=settings.ords_timeout_seconds,
            headers={
                "X-API-Username": settings.ords_username,
                "X-API-Password": settings.ords_password,
            },
        ) as client:
            check_status, check_body, check_error, check_parsed = (
                await post_ords_membership_check(
                    client,
                    check_url,
                    [
                        (row_id, {"event_uid": event_uid}, connector_id, True)
                        for row_id, event_uid, connector_id in claims
                    ],
                )
            )
        record_ords_route_result(
            status=check_status,
            transport_error=check_error,
        )
    requested = {event_uid for _row_id, event_uid, _connector_id in claims}
    missing = ords_membership_missing(check_status, check_body, requested)
    await asyncio.to_thread(
        apply_confirmed_membership_audit_results,
        claims,
        missing,
        status=check_status,
        transport_error=check_error,
        response_parsed=check_parsed,
    )


def apply_ords_confirmation(
    session: Session,
    *,
    claimed_id: int,
    path: str,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    from zk_add.attendance_manual_guard import generic_confirmation_allowed
    event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
    if event and not generic_confirmation_allowed(session, event):
        return
    confirmed_at = utc_now()
    row.status = "ACKED_CHECK"
    row.acknowledged_at = confirmed_at
    row.next_attempt_at = None
    row.last_error = None
    row.last_http_status = 200
    event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
    if event is not None:
        event.ords_status = "ACKED_CHECK"
        event.oracle_confirmed_at = confirmed_at
        event.oracle_confirmation_path = path
        connector = session.get(Connector, event.connector_id)
        remaining_failure = session.scalar(
            select(OrdsOutbox.id)
            .join(AttendanceEvent, AttendanceEvent.id == OrdsOutbox.attendance_event_id)
            .where(
                AttendanceEvent.connector_id == event.connector_id,
                OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if connector is not None and remaining_failure is None:
            resolve_alert(session, connector, code="ORDS_DELIVERY_FAILED")


def apply_ords_membership_failure(
    session: Session,
    *,
    claimed_id: int,
    status: int | None,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
    connector = session.get(Connector, event.connector_id) if event else None
    category = ords_failure_category(
        status,
        transport_error=transport_error,
        response_parsed=response_parsed,
    )
    row.status = "FAILED_RETRYABLE"
    row.last_http_status = status
    row.last_error = f"CHECK_{category}"
    row.next_attempt_at = utc_now() + timedelta(
        seconds=min(600, 2 ** min(row.attempt_count, 9))
    )
    if event is not None:
        event.ords_status = "FAILED_RETRYABLE"
    if connector is not None:
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity="HIGH" if status in {401, 403} else "WARNING",
            message=(
                "Oracle attendance membership verification is retrying; "
                "preserved events remain queued."
            ),
            details={
                "failure_category": f"CHECK_{category}",
                "http_status": status,
                "attempt_count": row.attempt_count,
            },
        )


def apply_ords_delivery_result(
    session: Session,
    *,
    claimed_id: int,
    status: int | None,
    body: object,
    transport_error: str | None,
    response_parsed: bool,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
        return
    event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
    from zk_add.attendance_manual_guard import generic_confirmation_allowed
    if event and not generic_confirmation_allowed(session, event):
        return
    connector = session.get(Connector, event.connector_id) if event else None
    row.last_http_status = status
    category = ords_failure_category(
        status,
        transport_error=transport_error,
        response_parsed=response_parsed,
    )
    if ords_delivery_succeeded(status, body):
        confirmed_at = utc_now()
        row.status = "ACKED"
        row.acknowledged_at = confirmed_at
        row.next_attempt_at = None
        row.last_error = None
        if event:
            event.ords_status = "ACKED"
            event.oracle_confirmed_at = confirmed_at
            event.oracle_confirmation_path = "ADD_DELIVERY"
        if connector is not None:
            remaining_failure = session.scalar(
                select(OrdsOutbox.id)
                .join(
                    AttendanceEvent,
                    AttendanceEvent.id == OrdsOutbox.attendance_event_id,
                )
                .where(
                    AttendanceEvent.connector_id == connector.id,
                    OrdsOutbox.status.in_(ORDS_ACTIVE_STATUSES),
                )
                .limit(1)
            )
            if remaining_failure is None:
                resolve_alert(session, connector, code="ORDS_DELIVERY_FAILED")
        return

    if ords_failure_is_permanent(status):
        row.status = "QUARANTINED_ORDS_REJECTED"
        row.next_attempt_at = None
        row.last_error = category
        if event:
            event.ords_status = "QUARANTINED_ORDS_REJECTED"
        if connector is not None:
            upsert_alert(
                session,
                connector,
                code="ORDS_EVENT_REJECTED",
                severity="HIGH",
                message=(
                    "Oracle permanently rejected a preserved attendance event; "
                    "later queued events continue."
                ),
                details={
                    "failure_category": category,
                    "http_status": status,
                },
            )
        return

    row.status = "FAILED_RETRYABLE"
    row.last_error = category
    delay = min(600, 2 ** min(row.attempt_count, 9))
    row.next_attempt_at = utc_now() + timedelta(seconds=delay)
    if event:
        event.ords_status = "FAILED_RETRYABLE"
    if connector is not None:
        severity = "HIGH" if status in {401, 403, 404, 405} else "WARNING"
        upsert_alert(
            session,
            connector,
            code="ORDS_DELIVERY_FAILED",
            severity=severity,
            message="Oracle attendance delivery is retrying; preserved events remain queued.",
            details={
                "failure_category": category,
                "http_status": status,
                "attempt_count": row.attempt_count,
            },
        )


def apply_ords_batch_results(
    claims: list[tuple[int, dict, int, bool]],
    confirmed_claim_ids: list[int],
    membership_failure: tuple[int | None, str | None, bool] | None,
    results: list[tuple[int, int, int | None, object, str | None, bool]],
) -> None:
    with session_scope() as session:
        for claimed_id in confirmed_claim_ids:
            apply_ords_confirmation(
                session,
                claimed_id=claimed_id,
                path="ORDS_MEMBERSHIP_CHECK",
            )
        if membership_failure is not None:
            failure_status, failure_error, failure_parsed = membership_failure
            for claimed_id, _payload, _connector_id, _was_retry in claims:
                apply_ords_membership_failure(
                    session,
                    claimed_id=claimed_id,
                    status=failure_status,
                    transport_error=failure_error,
                    response_parsed=failure_parsed,
                )
        for row_id, _connector_id, status, body, error, parsed in results:
            apply_ords_delivery_result(
                session,
                claimed_id=row_id,
                status=status,
                body=body,
                transport_error=error,
                response_parsed=parsed,
            )


async def deliver_ords_batch(
    *,
    limit: int = ORDS_DELIVERY_BATCH_SIZE,
    concurrency: int = ORDS_DELIVERY_CONCURRENCY,
) -> None:
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return
    async with _ords_request_lock:
        if ords_circuit_is_open():
            return
        claims = await asyncio.to_thread(claim_ords_batch, max(1, limit))
        if not claims:
            return
        from zk_add.attendance_force_delivery import split_claims, deliver_forced
        claims, forced = await asyncio.to_thread(split_claims, claims)
        # Reserve service for live claims while slow content verification runs.
        # Each lane commits its receipts independently of the other's network wait.
        if forced and claims and concurrency > 1:
            background_slots = min(4, max(1, concurrency // 5))
            await asyncio.gather(
                _deliver_ordinary_claims(claims, concurrency=concurrency - background_slots, limit=limit),
                deliver_forced(forced, concurrency=background_slots),
            )
        else:
            if claims:
                await _deliver_ordinary_claims(claims, concurrency=concurrency, limit=limit)
            if forced:
                await deliver_forced(forced, concurrency=concurrency)


async def _deliver_ordinary_claims(claims, *, concurrency, limit):
    base_url = settings.ords_base_url.rstrip("/")
    url = base_url + "/raw-captures"
    check_url = base_url + "/raw-captures/check"
    semaphore = asyncio.Semaphore(max(1, min(concurrency, limit)))
    route_results: list[tuple[int | None, str | None]] = []
    async with httpx.AsyncClient(
        timeout=settings.ords_timeout_seconds,
        headers={
            "X-API-Username": settings.ords_username,
            "X-API-Password": settings.ords_password,
        },
    ) as client:
        send_claims: list[tuple[int, dict, int, bool]] = []
        confirmed_claim_ids: list[int] = []
        membership_failure: tuple[int | None, str | None, bool] | None = None
        check_status, check_body, check_error, check_parsed = (
            await post_ords_membership_check(client, check_url, claims)
        )
        route_results.append((check_status, check_error))
        requested = {claim[1]["event_uid"] for claim in claims}
        missing = ords_membership_missing(check_status, check_body, requested)
        if missing is not None:
            for claim in claims:
                if claim[1]["event_uid"] in missing:
                    send_claims.append(claim)
                else:
                    confirmed_claim_ids.append(claim[0])
        elif check_status in {404, 405}:
            # Backward compatibility while Oracle environments roll out the
            # membership endpoint.
            send_claims.extend(claims)
        else:
            membership_failure = (check_status, check_error, check_parsed)
        results = await asyncio.gather(
            *(post_ords_claim(client, semaphore, url, claim) for claim in send_claims)
        )
        route_results.extend((status, error) for _, _, status, _, error, _ in results)

    responded_status = next(
        (status for status, _error in route_results if status is not None),
        None,
    )
    transport_error = next(
        (
            error
            for status, error in route_results
            if status is None and error is not None
        ),
        None,
    )
    record_ords_route_result(
        status=responded_status,
        transport_error=transport_error,
    )
    await asyncio.to_thread(
        apply_ords_batch_results,
        claims,
        confirmed_claim_ids,
        membership_failure,
        results,
    )


async def deliver_ords_once() -> None:
    """Compatibility wrapper for targeted tests and operational callers."""
    await deliver_ords_batch(limit=1, concurrency=1)


def retention_tick() -> None:
    now = utc_now()
    with session_scope() as session:
        session.query(DeviceLog).filter(
            DeviceLog.received_at < now - timedelta(days=settings.log_retention_days)
        ).delete(synchronize_session=False)
        session.query(DeviceTelemetry).filter(
            DeviceTelemetry.created_at < now - timedelta(days=settings.telemetry_retention_days)
        ).delete(synchronize_session=False)
        session.query(ConnectorNonce).filter(
            ConnectorNonce.created_at < now - timedelta(minutes=10)
        ).delete(synchronize_session=False)
        session.query(OnboardingNonce).filter(
            OnboardingNonce.created_at < now - timedelta(minutes=10)
        ).delete(synchronize_session=False)
        session.query(ProvisioningCompanionNonce).filter(
            ProvisioningCompanionNonce.created_at < now - timedelta(minutes=10)
        ).delete(synchronize_session=False)
        session.query(ConnectorCredential).filter(
            ConnectorCredential.active == True,  # noqa: E712
            ConnectorCredential.valid_until != None,  # noqa: E711
            ConnectorCredential.valid_until <= now,
        ).update(
            {ConnectorCredential.active: False, ConnectorCredential.revoked_at: now},
            synchronize_session=False,
        )
