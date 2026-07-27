from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import re

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from zk_add.db import session_scope
from zk_add.crypto import decrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    Connector,
    ConnectorCredential,
    ConnectorNonce,
    OnboardingNonce,
    DeviceCommand,
    DeviceLog,
    DeviceTelemetry,
    OrdsOutbox,
    ZKTDevice,
)
from zk_add.realtime import browser_events, connector_hub
from zk_add.service import (
    ACTIVE_COMMAND_STATES,
    MUTATING_COMMANDS,
    advance_user_deletion_jobs,
    apply_user_command_terminal_state,
    oracle_payload,
    queue_due_revokes,
    resolve_alert,
    serialize_command,
    upsert_alert,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now


EVENT_UID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ORDS_DELIVERY_BATCH_SIZE = 8
ORDS_DELIVERY_CONCURRENCY = 4
ORDS_PERMANENT_REJECTION_STATUSES = {400, 413, 422}
ORDS_ACTIVE_STATUSES = {"PENDING", "FAILED_RETRYABLE", "IN_FLIGHT"}
ORDS_ACKNOWLEDGED_STATUSES = {"ACKED", "ACKED_CHECK", "ACKED_FIRMWARE"}
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
        "oldest_backlog_at": oldest_backlog_at,
        "last_attempt_at": last_attempt_at,
    }


async def maintenance_loop(stop: asyncio.Event) -> None:
    retention_counter = 0
    while not stop.is_set():
        try:
            await maintenance_tick()
            retention_counter += 1
            if retention_counter >= 300:
                retention_counter = 0
                retention_tick()
        except Exception as exc:
            await browser_events.publish(
                "backend_error", {"code": "MAINTENANCE_LOOP_ERROR", "message": str(exc)[:500]}
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def maintenance_tick() -> None:
    now = utc_now()
    dispatch: list[tuple[str, dict]] = []
    connector_updates: list[dict] = []
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
        for command in session.scalars(
            select(DeviceCommand)
            .where(DeviceCommand.status.in_(ACTIVE_COMMAND_STATES))
            .order_by(DeviceCommand.created_at.asc())
        ):
            if command.expires_at and command.expires_at <= now:
                command.status = "EXPIRED"
                command.completed_at = now
                apply_user_command_terminal_state(session, command=command, status="EXPIRED")
                continue
            connector = session.get(Connector, command.connector_id)
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
    for connector_id, update in dispatch:
        if await connector_hub.send(connector_id, update):
            with session_scope() as session:
                command = session.scalar(
                    select(DeviceCommand).where(DeviceCommand.command_id == update["command_id"])
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
    for update in connector_updates:
        await browser_events.publish("device", update)
    await deliver_ords_batch()


def claim_ords_batch(limit: int) -> list[tuple[int, dict, int, bool]]:
    claims: list[tuple[int, dict, int, bool]] = []
    with session_scope() as session:
        now = utc_now()
        # A process interruption after claiming a row must not strand it in
        # IN_FLIGHT forever.  No normal request remains active this long.
        stale_before = now - timedelta(seconds=max(60, int(settings.ords_timeout_seconds * 3)))
        for stale in session.scalars(
            select(OrdsOutbox).where(
                OrdsOutbox.status == "IN_FLIGHT",
                OrdsOutbox.last_attempt_at < stale_before,
            ).limit(100)
        ).all():
            stale.status = "FAILED_RETRYABLE"
            stale.next_attempt_at = now
            stale.last_error = "RECOVERED_STALE_IN_FLIGHT"
            event = session.get(AttendanceEvent, stale.attendance_event_id) if stale.attendance_event_id else None
            if event:
                event.ords_status = "FAILED_RETRYABLE"

        candidates = session.scalars(
            select(OrdsOutbox)
            .where(
                OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE"]),
                (OrdsOutbox.next_attempt_at == None)  # noqa: E711
                | (OrdsOutbox.next_attempt_at <= now),
            )
            .order_by(
                case((OrdsOutbox.status == "PENDING", 0), else_=1),
                OrdsOutbox.id.asc(),
            )
            .limit(max(200, limit * 20))
            .with_for_update(skip_locked=True)
        ).all()
        for row in candidates:
            was_retry = row.status == "FAILED_RETRYABLE"
            event = (
                session.get(AttendanceEvent, row.attendance_event_id)
                if row.attendance_event_id
                else None
            )
            event_uid = event.event_uid if event else None
            if not event_uid_is_valid(event_uid):
                row.status = "QUARANTINED_INVALID_EVENT_UID"
                row.last_error = "Rejected before ORDS delivery: event_uid is not a 64-character lowercase hex digest."
                if event:
                    event.ords_status = "QUARANTINED_INVALID_EVENT_UID"
                continue
            connector = session.get(Connector, event.connector_id)
            zkt = session.get(ZKTDevice, event.zkt_device_id)
            cnic = decrypt_cnic(event.cnic_encrypted)
            identity_unverified = (
                settings.identity_snapshot_gate_enabled
                and event.identity_resolution_status != "RESOLVED"
            )
            if connector is None or zkt is None or not cnic or identity_unverified:
                row.status = "BLOCKED_IDENTITY"
                event.ords_status = "BLOCKED_IDENTITY"
                continue
            candidate_payload = oracle_payload(connector, zkt, event, cnic)
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


def apply_ords_confirmation(
    session: Session,
    *,
    claimed_id: int,
    path: str,
) -> None:
    row = session.get(OrdsOutbox, claimed_id)
    if row is None:
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
                    OrdsOutbox.status == "FAILED_RETRYABLE",
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


async def deliver_ords_batch(
    *,
    limit: int = ORDS_DELIVERY_BATCH_SIZE,
    concurrency: int = ORDS_DELIVERY_CONCURRENCY,
) -> None:
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return
    claims = claim_ords_batch(max(1, limit))
    if not claims:
        return
    base_url = settings.ords_base_url.rstrip("/")
    url = base_url + "/raw-captures"
    check_url = base_url + "/raw-captures/check"
    semaphore = asyncio.Semaphore(max(1, min(concurrency, limit)))
    async with httpx.AsyncClient(
        timeout=settings.ords_timeout_seconds,
        headers={
            "X-API-Username": settings.ords_username,
            "X-API-Password": settings.ords_password,
        },
    ) as client:
        retry_claims = [claim for claim in claims if claim[3]]
        send_claims = [claim for claim in claims if not claim[3]]
        confirmed_claim_ids: list[int] = []
        membership_failure: tuple[int | None, str | None, bool] | None = None
        if retry_claims:
            check_status, check_body, check_error, check_parsed = (
                await post_ords_membership_check(client, check_url, retry_claims)
            )
            requested = {claim[1]["event_uid"] for claim in retry_claims}
            missing = ords_membership_missing(check_status, check_body, requested)
            if missing is not None:
                for claim in retry_claims:
                    if claim[1]["event_uid"] in missing:
                        send_claims.append(claim)
                    else:
                        confirmed_claim_ids.append(claim[0])
            elif check_status in {404, 405}:
                # Backward compatibility while Oracle environments roll out the
                # membership endpoint.
                send_claims.extend(retry_claims)
            else:
                membership_failure = (check_status, check_error, check_parsed)
        results = await asyncio.gather(
            *(post_ords_claim(client, semaphore, url, claim) for claim in send_claims)
        )
    with session_scope() as session:
        for claimed_id in confirmed_claim_ids:
            apply_ords_confirmation(
                session,
                claimed_id=claimed_id,
                path="ORDS_MEMBERSHIP_CHECK",
            )
        if membership_failure is not None:
            failure_status, failure_error, failure_parsed = membership_failure
            for claimed_id, _payload, _connector_id, _was_retry in retry_claims:
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
        session.query(ConnectorCredential).filter(
            ConnectorCredential.active == True,  # noqa: E712
            ConnectorCredential.valid_until != None,  # noqa: E711
            ConnectorCredential.valid_until <= now,
        ).update(
            {ConnectorCredential.active: False, ConnectorCredential.revoked_at: now},
            synchronize_session=False,
        )
