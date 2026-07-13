from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import select

from zk_add.db import session_scope
from zk_add.models import (
    AttendanceEvent,
    Connector,
    ConnectorNonce,
    DeviceCommand,
    DeviceLog,
    DeviceTelemetry,
    OrdsOutbox,
)
from zk_add.realtime import browser_events, connector_hub
from zk_add.service import queue_due_revokes, serialize_command, upsert_alert
from zk_add.settings import settings
from zk_common.time_utils import utc_now


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
            select(DeviceCommand).where(
                DeviceCommand.status.in_(["QUEUED", "DISPATCHED"])
            ).order_by(DeviceCommand.created_at.asc())
        ):
            if command.expires_at and command.expires_at <= now:
                command.status = "EXPIRED"
                command.completed_at = now
                continue
            connector = session.get(Connector, command.connector_id)
            if connector:
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
                if command and command.status == "QUEUED":
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
        row = session.scalar(
            select(OrdsOutbox).where(
                OrdsOutbox.status.in_(["PENDING", "FAILED_RETRYABLE"]),
                (OrdsOutbox.next_attempt_at == None) | (OrdsOutbox.next_attempt_at <= now),  # noqa: E711
            ).order_by(OrdsOutbox.id.asc()).limit(1)
        )
        if row:
            row.status = "IN_FLIGHT"
            row.attempt_count += 1
            row.last_attempt_at = now
            claimed_id = row.id
            payload = row.payload
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
