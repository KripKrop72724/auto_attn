from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import re

import httpx
from sqlalchemy import select

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
    apply_user_command_terminal_state,
    oracle_payload,
    queue_due_revokes,
    serialize_command,
    upsert_alert,
)
from zk_add.settings import settings
from zk_add.time_utils import utc_now


EVENT_UID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
    await deliver_ords_once()


async def deliver_ords_once() -> None:
    if not settings.ords_base_url or not settings.ords_username or not settings.ords_password:
        return
    claimed_id = None
    payload = None
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
            stale.last_error = "Recovered a stale in-flight delivery after backend interruption."
            event = session.get(AttendanceEvent, stale.attendance_event_id) if stale.attendance_event_id else None
            if event:
                event.ords_status = "FAILED_RETRYABLE"

        candidates = session.scalars(
            select(OrdsOutbox).where(
                OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE"]),
                (OrdsOutbox.next_attempt_at == None) | (OrdsOutbox.next_attempt_at <= now),  # noqa: E711
            ).order_by(OrdsOutbox.id.asc()).limit(100)
        ).all()
        for row in candidates:
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
            if connector is None or zkt is None or not cnic:
                row.status = "BLOCKED_IDENTITY"
                event.ords_status = "BLOCKED_IDENTITY"
                continue
            candidate_payload = oracle_payload(connector, zkt, event, cnic)
            row.status = "IN_FLIGHT"
            row.attempt_count += 1
            row.last_attempt_at = now
            row.payload_hash = hashlib.sha256(
                json.dumps(candidate_payload, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            claimed_id = row.id
            payload = candidate_payload
            break
    if claimed_id is None or payload is None:
        return
    url = settings.ords_base_url.rstrip("/") + "/raw-captures"
    status = None
    body = None
    error = None
    try:
        async with httpx.AsyncClient(timeout=settings.ords_timeout_seconds) as client:
            response = await client.post(
                url,
                headers={
                    "X-API-Username": settings.ords_username,
                    "X-API-Password": settings.ords_password,
                },
                json=payload,
            )
            status = response.status_code
            body = response.json() if response.content else None
    except Exception as exc:
        error = str(exc)
    with session_scope() as session:
        row = session.get(OrdsOutbox, claimed_id)
        if row is None:
            return
        event = session.get(AttendanceEvent, row.attendance_event_id) if row.attendance_event_id else None
        if ords_delivery_succeeded(status, body):
            row.status = "ACKED"
            row.acknowledged_at = utc_now()
            row.last_http_status = status
            row.last_error = None
            if event:
                event.ords_status = "ACKED"
        else:
            row.status = "FAILED_RETRYABLE"
            row.last_http_status = status
            row.last_error = error or str(body)[:1000]
            delay = min(600, 2 ** min(row.attempt_count, 9))
            row.next_attempt_at = utc_now() + timedelta(seconds=delay)
            if event:
                event.ords_status = "FAILED_RETRYABLE"


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
