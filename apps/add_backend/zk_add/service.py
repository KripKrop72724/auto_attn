from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import cnic_lookup, encrypt_cnic
from zk_add.identity import parse_machine_name
from zk_add.models import (
    AttendanceEvent,
    Connector,
    ConnectorCredential,
    DeviceAlert,
    DeviceCommand,
    DeviceCommandEvent,
    DeviceConnectionEvent,
    DeviceLog,
    DeviceTelemetry,
    DeviceUser,
    OrdsOutbox,
    Site,
    TemporaryAdminLease,
    ZKTDevice,
)
from zk_add.schemas import AttendanceEventIn, HeartbeatPayload, UserSnapshotRequest
from zk_add.security import connector_token_hash
from zk_add.settings import settings
from zk_common.time_utils import ensure_utc, utc_now


ACTIVE_COMMAND_STATES = {"QUEUED", "DISPATCHED", "ACKNOWLEDGED", "RUNNING"}
MUTATING_COMMANDS = {
    "UPDATE_USER",
    "GRANT_TEMP_ADMIN",
    "REVOKE_TEMP_ADMIN",
    "RESTART_ZKT",
    "APPLY_CONFIG",
}


def ensure_site(session: Session, zone_id: str, zone_name: str) -> Site:
    site = session.scalar(select(Site).where(Site.site_id == zone_id))
    if site is None:
        site = Site(site_id=zone_id, name=zone_name, timezone="Asia/Karachi")
        session.add(site)
        session.flush()
    return site


def create_connector(
    session: Session,
    *,
    hardware_id: str,
    zone_id: str,
    zone_name: str,
    device_id: str,
    display_name: str,
    expected_serial: str | None,
    actor: str,
    ip_address: str | None,
) -> tuple[Connector, str]:
    if session.scalar(select(Connector).where(Connector.hardware_id == hardware_id)):
        raise ValueError("A connector with this hardware ID already exists.")
    site = ensure_site(session, zone_id, zone_name)
    connector_id = str(uuid4())
    activation_code = secrets.token_urlsafe(32)
    connector = Connector(
        connector_id=connector_id,
        hardware_id=hardware_id,
        site_id=site.id,
        zone_id=zone_id,
        zone_name=zone_name,
        device_id=device_id,
        display_name=display_name,
        activation_hash=connector_token_hash(activation_code),
    )
    session.add(connector)
    session.flush()
    session.add(
        ZKTDevice(
            connector_id=connector.id,
            expected_serial=expected_serial,
            serial=expected_serial,
            certification_state="READ_ONLY",
            capability_profile={
                "read_users": True,
                "read_attendance": True,
                "user_write": False,
                "admin_lease": False,
                "protocol_restart": False,
                "telnet_recovery": False,
            },
        )
    )
    append_audit(
        session,
        actor=actor,
        action="CONNECTOR_CREATED",
        target_type="connector",
        target_id=connector_id,
        outcome="SUCCESS",
        ip_address=ip_address,
        after={"hardware_id": hardware_id, "zone_id": zone_id, "device_id": device_id},
    )
    return connector, activation_code


def activate_connector(
    session: Session, *, connector_id: str, hardware_id: str, activation_code: str
) -> tuple[Connector, str]:
    connector = session.scalar(
        select(Connector).where(
            Connector.connector_id == connector_id,
            Connector.hardware_id == hardware_id,
            Connector.active == True,  # noqa: E712
        )
    )
    if connector is None or not connector.activation_hash:
        raise ValueError("Activation is invalid or has already been used.")
    if not secrets.compare_digest(connector.activation_hash, connector_token_hash(activation_code)):
        raise ValueError("Activation is invalid or has already been used.")
    raw_token = secrets.token_urlsafe(48)
    session.add(
        ConnectorCredential(
            connector_id=connector.id,
            token_hash=connector_token_hash(raw_token),
            token_last4=raw_token[-4:],
        )
    )
    connector.activation_hash = None
    connector.lifecycle_state = "ONLINE"
    connector.updated_at = utc_now()
    append_audit(
        session,
        actor=f"connector:{connector.connector_id}",
        action="CONNECTOR_ACTIVATED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="SUCCESS",
    )
    return connector, raw_token


def seed_bootstrap_connector(session: Session) -> None:
    if not settings.bootstrap_connector_id or not settings.bootstrap_connector_token:
        return
    connector = session.scalar(
        select(Connector).where(Connector.connector_id == settings.bootstrap_connector_id)
    )
    if connector is None:
        site = ensure_site(session, settings.bootstrap_zone_id, settings.bootstrap_zone_name)
        connector = Connector(
            connector_id=settings.bootstrap_connector_id,
            hardware_id=settings.bootstrap_hardware_id or settings.bootstrap_connector_id,
            site_id=site.id,
            zone_id=settings.bootstrap_zone_id,
            zone_name=settings.bootstrap_zone_name,
            device_id=settings.bootstrap_device_id,
            display_name=settings.bootstrap_zone_name,
            lifecycle_state="ONBOARDING",
        )
        session.add(connector)
        session.flush()
        session.add(
            ZKTDevice(
                connector_id=connector.id,
                expected_serial=settings.bootstrap_expected_serial,
                serial=settings.bootstrap_expected_serial,
                certification_state="READ_ONLY",
            )
        )
    elif connector.zkt_device and settings.bootstrap_expected_serial:
        connector.zkt_device.expected_serial = settings.bootstrap_expected_serial
        if connector.zkt_device.serial is None:
            connector.zkt_device.serial = settings.bootstrap_expected_serial
    hashed = connector_token_hash(settings.bootstrap_connector_token)
    credential = session.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.token_hash == hashed,
        )
    )
    if credential is None:
        session.add(
            ConnectorCredential(
                connector_id=connector.id,
                token_hash=hashed,
                token_last4=settings.bootstrap_connector_token[-4:],
            )
        )


def update_heartbeat(
    session: Session,
    *,
    connector: Connector,
    boot_id: str,
    sequence: int,
    payload: HeartbeatPayload,
) -> dict:
    now = utc_now()
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    connector.boot_id = boot_id
    connector.last_sequence = max(connector.last_sequence, sequence)
    connector.last_seen_at = now
    connector.updated_at = now
    connector.firmware_version = payload.firmware_version or connector.firmware_version
    connector.config_version = payload.config_version
    connector.current_activity = payload.current_activity
    zkt = connector.zkt_device
    zkt_payload = payload.zkt
    if zkt:
        reported_state = str(
            zkt_payload.get("connection_state")
            or ("ONLINE" if zkt_payload.get("online", False) else "OFFLINE")
        ).upper()
        previous_state = zkt.connection_state
        zkt.online = reported_state in {"ONLINE", "RECOVERING"}
        zkt.connection_state = reported_state
        zkt.consecutive_failures = int(zkt_payload.get("consecutive_failures", zkt.consecutive_failures))
        zkt.consecutive_successes = int(zkt_payload.get("consecutive_successes", zkt.consecutive_successes))
        zkt.flap_count_15m = int(zkt_payload.get("flap_count_15m", zkt.flap_count_15m))
        zkt.probe_latency_ms = zkt_payload.get("probe_latency_ms", zkt.probe_latency_ms)
        observed_record_size = zkt_payload.get("user_record_size")
        if observed_record_size:
            zkt.capability_profile = {
                **(zkt.capability_profile or {}),
                "observed_user_record_bytes": int(observed_record_size),
                "read_users": True,
                "read_attendance": True,
            }
        zkt.serial = zkt_payload.get("serial") or zkt.serial
        zkt.ip_address = zkt_payload.get("ip_address") or zkt.ip_address
        zkt.model = zkt_payload.get("model") or zkt.model
        zkt.platform = zkt_payload.get("platform") or zkt.platform
        zkt.firmware_version = zkt_payload.get("firmware_version") or zkt.firmware_version
        zkt.user_count = zkt_payload.get("user_count", zkt.user_count)
        zkt.attendance_count = zkt_payload.get("attendance_count", zkt.attendance_count)
        zkt.device_time_drift_seconds = zkt_payload.get("drift_seconds", zkt.device_time_drift_seconds)
        zkt.last_seen_at = now
        zkt.updated_at = now
        if reported_state in {"ONLINE", "RECOVERING"}:
            zkt.last_online_at = now
            zkt.offline_since = None
        elif zkt.offline_since is None:
            zkt.offline_since = now
        for field_name in ("backoff_until", "stability_since"):
            value = zkt_payload.get(field_name)
            if value:
                from zk_common.time_utils import parse_datetime

                setattr(zkt, field_name, parse_datetime(value))
        if previous_state != reported_state:
            zkt.last_transition_at = now
            session.add(
                DeviceConnectionEvent(
                    connector_id=connector.id,
                    zkt_device_id=zkt.id,
                    from_state=previous_state,
                    to_state=reported_state,
                    reason=(zkt_payload.get("transition_reason") or "connector heartbeat")[:160],
                    consecutive_failures=zkt.consecutive_failures,
                    consecutive_successes=zkt.consecutive_successes,
                    flap_count_15m=zkt.flap_count_15m,
                )
            )
        if reported_state == "FLAPPING":
            connector.lifecycle_state = "FLAPPING"
            connector.last_error_code = "ZKT_CONNECTION_FLAPPING"
            connector.last_error_message = "ZKT connectivity is unstable; connector is in protective backoff."
            upsert_alert(
                session,
                connector,
                code="ZKT_CONNECTION_FLAPPING",
                severity="WARNING",
                message=connector.last_error_message,
                details={"flaps_15m": zkt.flap_count_15m, "backoff_until": zkt_payload.get("backoff_until")},
            )
        elif reported_state in {"OFFLINE", "RETRY_WAIT", "DISCOVERING"}:
            connector.lifecycle_state = "DEGRADED"
        elif reported_state in {"ONLINE", "RECOVERING"}:
            connector.lifecycle_state = "ONLINE" if reported_state == "ONLINE" else "DEGRADED"
            if zkt.consecutive_successes >= 3:
                resolve_alert(session, connector, code="ZKT_CONNECTION_FLAPPING")
        sample = zkt_payload.get("device_time")
        sampled_at = zkt_payload.get("device_time_sampled_at")
        if sample:
            from zk_common.time_utils import parse_datetime

            zkt.sampled_device_time = parse_datetime(sample)
        if sampled_at:
            from zk_common.time_utils import parse_datetime

            zkt.device_time_sampled_at = parse_datetime(sampled_at)
        if zkt.sampled_device_time and zkt.device_time_sampled_at:
            zkt.device_time_drift_seconds = (
                zkt.sampled_device_time - zkt.device_time_sampled_at
            ).total_seconds()
            if abs(zkt.device_time_drift_seconds) > 120:
                upsert_alert(
                    session,
                    connector,
                    code="ZKT_CLOCK_DRIFT",
                    severity="WARNING",
                    message="ZKT terminal clock differs from trusted connector time by more than two minutes.",
                    details={"drift_seconds": zkt.device_time_drift_seconds},
                )
            else:
                resolve_alert(session, connector, code="ZKT_CLOCK_DRIFT")
        if zkt.expected_serial and zkt.serial and zkt.expected_serial != zkt.serial:
            connector.lifecycle_state = "DEGRADED"
            connector.last_error_code = "ZKT_SERIAL_MISMATCH"
            connector.last_error_message = "Authenticated ZKT serial does not match the assigned device."
            zkt.online = False
            upsert_alert(
                session,
                connector,
                code="ZKT_SERIAL_MISMATCH",
                severity="CRITICAL",
                message=connector.last_error_message,
            )
    session.add(
        DeviceTelemetry(
            connector_id=connector.id,
            boot_id=boot_id,
            sequence=sequence,
            rssi=payload.rssi,
            free_heap=payload.free_heap,
            uptime_seconds=payload.uptime_seconds,
            outbox_depth=payload.outbox_depth,
            current_activity=payload.current_activity,
            led_state=payload.led_state,
            payload=payload.model_dump(mode="json"),
        )
    )
    return serialize_connector(connector)


def replace_user_snapshot(
    session: Session, *, connector: Connector, snapshot: UserSnapshotRequest
) -> int:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    seen: set[str] = set()
    for incoming in snapshot.users:
        seen.add(incoming.uid)
        parsed = parse_machine_name(incoming.name)
        row = session.scalar(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id, DeviceUser.uid == incoming.uid
            )
        )
        if row is None:
            row = DeviceUser(
                zkt_device_id=zkt.id,
                uid=incoming.uid,
                user_id=incoming.user_id,
                raw_name=incoming.name,
                display_name=parsed.display_name,
                row_version=1,
            )
            session.add(row)
        else:
            if row.user_id != incoming.user_id:
                duplicate = session.scalar(
                    select(DeviceUser).where(
                        DeviceUser.zkt_device_id == zkt.id,
                        DeviceUser.user_id == incoming.user_id,
                        DeviceUser.id != row.id,
                    )
                )
                if duplicate:
                    raise ValueError(f"Duplicate device user ID {incoming.user_id} in snapshot.")
                row.user_id = incoming.user_id
            row.row_version = (row.row_version or 0) + 1
        row.raw_name = incoming.name
        row.display_name = parsed.display_name
        row.cnic_encrypted = encrypt_cnic(parsed.cnic)
        row.cnic_lookup_hash = cnic_lookup(parsed.cnic)
        row.cnic_last4 = parsed.cnic[-4:] if parsed.cnic else None
        row.shift_worker = parsed.shift_worker
        row.privilege = incoming.privilege
        row.card = incoming.card
        row.present = True
        row.snapshot_id = snapshot.snapshot_id
        row.observed_at = ensure_utc(snapshot.observed_at)
        row.updated_at = utc_now()
    if snapshot.complete:
        for row in session.scalars(
            select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id, DeviceUser.present == True)  # noqa: E712
        ):
            if row.uid not in seen:
                row.present = False
                row.row_version += 1
                row.updated_at = utc_now()
    zkt.user_count = len(snapshot.users)
    zkt.updated_at = utc_now()
    return len(snapshot.users)


def ingest_attendance(
    session: Session, *, connector: Connector, events: list[AttendanceEventIn]
) -> tuple[list[str], list[str]]:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    accepted: list[str] = []
    duplicates: list[str] = []
    for incoming in events:
        existing = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid)
        )
        if existing:
            duplicates.append(incoming.event_uid)
            continue
        parsed = parse_machine_name(incoming.raw_name)
        if not parsed.cnic:
            user = session.scalar(
                select(DeviceUser).where(
                    DeviceUser.zkt_device_id == zkt.id,
                    DeviceUser.user_id == incoming.user_id,
                    DeviceUser.present == True,  # noqa: E712
                )
            )
            if user:
                parsed = parse_machine_name(user.raw_name)
        row = AttendanceEvent(
            event_uid=incoming.event_uid,
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            device_serial=zkt.serial,
            uid=incoming.uid,
            user_id=incoming.user_id,
            raw_name=incoming.raw_name,
            display_name=parsed.display_name or incoming.raw_name,
            cnic_encrypted=encrypt_cnic(parsed.cnic),
            cnic_lookup_hash=cnic_lookup(parsed.cnic),
            cnic_last4=parsed.cnic[-4:] if parsed.cnic else None,
            device_event_time=ensure_utc(incoming.device_event_time),
            captured_at=ensure_utc(incoming.captured_at),
            source=incoming.source,
            status=None if incoming.status is None else str(incoming.status),
            punch=None if incoming.punch is None else str(incoming.punch),
            raw_punch=incoming.raw_punch or parsed.shift_worker,
            clock_drift_seconds=incoming.clock_drift_seconds,
            clock_quality=incoming.clock_quality,
            boot_id=incoming.boot_id,
            sequence=incoming.sequence,
            raw_event=incoming.raw_event,
            ords_status="PENDING" if parsed.cnic else "BLOCKED_IDENTITY",
        )
        session.add(row)
        session.flush()
        if parsed.cnic:
            payload = oracle_payload(connector, zkt, row, parsed.cnic)
            session.add(
                OrdsOutbox(
                    attendance_event_id=row.id,
                    payload=payload,
                    payload_hash=hashlib.sha256(
                        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
                    ).hexdigest(),
                )
            )
        accepted.append(incoming.event_uid)
    return accepted, duplicates


def oracle_payload(connector: Connector, zkt: ZKTDevice, row: AttendanceEvent, cnic: str) -> dict:
    return {
        "event_uid": row.event_uid,
        "zone_id": connector.zone_id,
        "zone_name": connector.zone_name,
        "device_id": connector.device_id,
        "device_serial": zkt.serial or "unknown",
        "user_id": row.user_id,
        "employee_name": row.display_name,
        "cnic": cnic,
        "timestamp": row.device_event_time.isoformat().replace("+00:00", "Z"),
        "status": row.status,
        "punch": row.punch,
        "raw_punch": "T" if row.raw_punch else "F",
        "capturetype": row.source,
        "trust_status": "TRUSTED_LIVE" if row.clock_quality == "OK" else "SUSPECT_DEVICE_TIME",
        "clockdiff": row.clock_drift_seconds,
    }


def ingest_logs(session: Session, *, connector: Connector, logs: list) -> int:
    accepted = 0
    for incoming in logs[:500]:
        if session.scalar(
            select(DeviceLog).where(
                DeviceLog.connector_id == connector.id,
                DeviceLog.boot_id == incoming.boot_id,
                DeviceLog.sequence == incoming.sequence,
            )
        ):
            continue
        context = redact_context(incoming.context)
        message = redact_text(incoming.message)
        session.add(
            DeviceLog(
                connector_id=connector.id,
                boot_id=incoming.boot_id,
                sequence=incoming.sequence,
                level=incoming.level,
                subsystem=incoming.subsystem[:80],
                code=(incoming.code or "")[:120] or None,
                message=message[:4000],
                context=context,
                device_time=incoming.device_time,
            )
        )
        accepted += 1
    return accepted


def redact_context(value: dict) -> dict:
    blocked = {"password", "token", "secret", "comm_key", "cnic", "authorization"}
    return {
        key: "[REDACTED]" if key.lower() in blocked else item
        for key, item in value.items()
    }


def redact_text(value: str) -> str:
    import re

    value = re.sub(r"\b\d{13}\b", "[CNIC-REDACTED]", value)
    return value


def create_command(
    session: Session,
    *,
    connector: Connector,
    command_type: str,
    payload: dict,
    expected_state: dict,
    desired_state: dict,
    idempotency_key: str,
    actor: str,
    expires_in_seconds: int | None = 300,
) -> DeviceCommand:
    existing = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    if command_type in MUTATING_COMMANDS:
        active = session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.connector_id == connector.id,
                DeviceCommand.command_type.in_(MUTATING_COMMANDS),
                DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
            )
        )
        if active:
            raise ValueError(f"Device already has active command {active.command_id}.")
    now = utc_now()
    command = DeviceCommand(
        command_id=str(uuid4()),
        connector_id=connector.id,
        command_type=command_type,
        payload=payload,
        expected_state=expected_state,
        desired_state=desired_state,
        idempotency_key=idempotency_key,
        actor=actor,
        expires_at=None if expires_in_seconds is None else now + timedelta(seconds=expires_in_seconds),
    )
    session.add(command)
    session.flush()
    session.add(DeviceCommandEvent(command_id=command.id, status="QUEUED", details={}))
    append_audit(
        session,
        actor=actor,
        action=f"COMMAND_{command_type}_QUEUED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="QUEUED",
        after={"command_id": command.command_id},
    )
    return command


def serialize_command(command: DeviceCommand) -> dict:
    return {
        "schema_version": "1",
        "type": "command",
        "command_id": command.command_id,
        "command_type": command.command_type,
        "payload": command.payload,
        "expected_state": command.expected_state,
        "desired_state": command.desired_state,
        "created_at": command.created_at.isoformat(),
        "expires_at": command.expires_at.isoformat() if command.expires_at else None,
    }


def serialize_connector(connector: Connector) -> dict:
    zkt = connector.zkt_device
    return {
        "connector_id": connector.connector_id,
        "hardware_id": connector.hardware_id,
        "zone_id": connector.zone_id,
        "zone_name": connector.zone_name,
        "device_id": connector.device_id,
        "display_name": connector.display_name,
        "state": connector.lifecycle_state,
        "connected": connector.connected,
        "firmware_version": connector.firmware_version,
        "last_seen_at": connector.last_seen_at,
        "current_activity": connector.current_activity,
        "last_error_code": connector.last_error_code,
        "zkt": None
        if zkt is None
        else {
            "id": zkt.id,
            "serial": zkt.serial,
            "expected_serial": zkt.expected_serial,
            "ip_address": zkt.ip_address,
            "model": zkt.model,
            "platform": zkt.platform,
            "online": zkt.online,
            "connection_state": zkt.connection_state,
            "consecutive_failures": zkt.consecutive_failures,
            "consecutive_successes": zkt.consecutive_successes,
            "flap_count_15m": zkt.flap_count_15m,
            "last_transition_at": zkt.last_transition_at,
            "last_online_at": zkt.last_online_at,
            "offline_since": zkt.offline_since,
            "stability_since": zkt.stability_since,
            "backoff_until": zkt.backoff_until,
            "probe_latency_ms": zkt.probe_latency_ms,
            "certification_state": zkt.certification_state,
            "capabilities": zkt.capability_profile,
            "user_count": zkt.user_count,
            "attendance_count": zkt.attendance_count,
            "device_time": zkt.sampled_device_time,
            "device_time_sampled_at": zkt.device_time_sampled_at,
            "drift_seconds": zkt.device_time_drift_seconds,
            "last_reconcile_at": zkt.last_reconcile_at,
            "next_restart_at": zkt.next_restart_at,
        },
    }


def upsert_alert(
    session: Session,
    connector: Connector,
    *,
    code: str,
    severity: str,
    message: str,
    details: dict | None = None,
) -> DeviceAlert:
    row = session.scalar(
        select(DeviceAlert).where(
            DeviceAlert.connector_id == connector.id,
            DeviceAlert.code == code,
            DeviceAlert.state == "OPEN",
        )
    )
    if row is None:
        row = DeviceAlert(
            connector_id=connector.id,
            code=code,
            severity=severity,
            state="OPEN",
            message=message,
            details=details or {},
        )
        session.add(row)
    else:
        row.last_seen_at = utc_now()
        row.message = message
        row.details = details or row.details
    return row


def resolve_alert(session: Session, connector: Connector, *, code: str) -> None:
    now = utc_now()
    for row in session.scalars(
        select(DeviceAlert).where(
            DeviceAlert.connector_id == connector.id,
            DeviceAlert.code == code,
            DeviceAlert.state == "OPEN",
        )
    ).all():
        row.state = "RESOLVED"
        row.resolved_at = now
        row.last_seen_at = now


def fleet_counts(session: Session) -> dict:
    rows = session.execute(
        select(Connector.lifecycle_state, func.count(Connector.id)).group_by(Connector.lifecycle_state)
    ).all()
    counts = {state.lower(): count for state, count in rows}
    counts["total"] = sum(counts.values())
    counts["open_alerts"] = session.scalar(
        select(func.count(DeviceAlert.id)).where(DeviceAlert.state == "OPEN")
    ) or 0
    counts["active_leases"] = session.scalar(
        select(func.count(TemporaryAdminLease.id)).where(
            TemporaryAdminLease.state.in_(["REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"])
        )
    ) or 0
    return counts


def apply_command_update(
    session: Session,
    *,
    connector: Connector,
    command_id: str,
    status: str,
    result: dict,
    error_code: str | None,
    error_message: str | None,
) -> DeviceCommand:
    command = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.command_id == command_id,
            DeviceCommand.connector_id == connector.id,
        )
    )
    if command is None:
        raise ValueError("Unknown command ID.")
    if command.status in {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}:
        return command
    now = utc_now()
    command.status = status
    if status == "ACKNOWLEDGED":
        command.acknowledged_at = command.acknowledged_at or now
    elif status == "RUNNING":
        command.started_at = command.started_at or now
    elif status in {"SUCCEEDED", "FAILED"}:
        command.completed_at = now
        command.result = result
        command.error_code = error_code
        command.error_message = error_message
    session.add(
        DeviceCommandEvent(
            command_id=command.id,
            status=status,
            details={"result": result, "error_code": error_code, "error_message": error_message},
        )
    )
    lease = None
    if command.command_type == "GRANT_TEMP_ADMIN":
        lease = session.scalar(
            select(TemporaryAdminLease).where(TemporaryAdminLease.grant_command_id == command.id)
        )
        if lease:
            if status == "SUCCEEDED":
                lease.state = "ACTIVE"
                lease.granted_at = now
                expires_epoch = result.get("expires_epoch")
                lease.expires_at = (
                    datetime.fromtimestamp(int(expires_epoch), tz=timezone.utc)
                    if expires_epoch
                    else now + timedelta(seconds=600)
                )
            elif status == "FAILED":
                lease.state = "FAILED"
                lease.last_error = error_message or error_code
            lease.updated_at = now
    elif command.command_type == "REVOKE_TEMP_ADMIN":
        lease = session.scalar(
            select(TemporaryAdminLease).where(TemporaryAdminLease.revoke_command_id == command.id)
        )
        if lease:
            if status == "SUCCEEDED":
                lease.state = "REVOKED"
                lease.revoked_at = now
            elif status == "FAILED":
                lease.state = "OVERDUE"
                lease.last_error = error_message or error_code
                parent_connector = session.get(Connector, connector.id)
                if parent_connector:
                    upsert_alert(
                        session,
                        parent_connector,
                        code="ADMIN_REVOKE_OVERDUE",
                        severity="CRITICAL",
                        message="Temporary administrator could not be verified as revoked.",
                        details={"lease_id": lease.lease_id},
                    )
            lease.updated_at = now
    append_audit(
        session,
        actor=f"connector:{connector.connector_id}",
        action=f"COMMAND_{command.command_type}_{status}",
        target_type="command",
        target_id=command.command_id,
        outcome=status,
        after={"error_code": error_code, "result": result},
    )
    return command


def create_admin_lease(
    session: Session,
    *,
    connector: Connector,
    user: DeviceUser,
    idempotency_key: str,
    actor: str,
) -> tuple[TemporaryAdminLease, DeviceCommand]:
    if user.privilege != 0 or not user.present:
        raise ValueError("Only a present regular user can receive a temporary admin lease.")
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    if not zkt.capability_profile.get("admin_lease", False):
        raise ValueError("This ZKT model is not certified for temporary administrator leases.")
    active = session.scalar(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.zkt_device_id == zkt.id,
            TemporaryAdminLease.state.in_(["REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]),
        )
    )
    if active:
        raise ValueError(f"Device already has active lease {active.lease_id}.")
    lease_id = str(uuid4())
    command = create_command(
        session,
        connector=connector,
        command_type="GRANT_TEMP_ADMIN",
        payload={"lease_id": lease_id, "uid": user.uid, "duration_seconds": 600},
        expected_state={"uid": user.uid, "privilege": 0, "row_version": user.row_version},
        desired_state={"privilege": 14},
        idempotency_key=idempotency_key,
        actor=actor,
        expires_in_seconds=120,
    )
    lease = TemporaryAdminLease(
        lease_id=lease_id,
        zkt_device_id=zkt.id,
        device_user_id=user.id,
        state="GRANTING",
        original_privilege=0,
        grant_command_id=command.id,
    )
    session.add(lease)
    session.flush()
    return lease, command


def queue_due_revokes(session: Session) -> list[DeviceCommand]:
    now = utc_now()
    commands: list[DeviceCommand] = []
    leases = session.scalars(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.state.in_(["ACTIVE", "OVERDUE"]),
            TemporaryAdminLease.expires_at != None,  # noqa: E711
            TemporaryAdminLease.expires_at <= now,
        )
    ).all()
    for lease in leases:
        if lease.revoke_command_id:
            existing = session.get(DeviceCommand, lease.revoke_command_id)
            if existing and existing.status not in {"FAILED", "EXPIRED", "CANCELED"}:
                continue
        zkt = session.get(ZKTDevice, lease.zkt_device_id)
        connector = session.get(Connector, zkt.connector_id) if zkt else None
        user = session.get(DeviceUser, lease.device_user_id)
        if connector is None or user is None:
            continue
        command = create_command(
            session,
            connector=connector,
            command_type="REVOKE_TEMP_ADMIN",
            payload={"lease_id": lease.lease_id, "uid": user.uid},
            expected_state={"uid": user.uid},
            desired_state={"privilege": 0},
            idempotency_key=f"revoke:{lease.lease_id}:{lease.revoke_command_id or 0}",
            actor="system:lease-watchdog",
            expires_in_seconds=None,
        )
        lease.revoke_command_id = command.id
        lease.state = "REVOKING"
        lease.updated_at = now
        commands.append(command)
    return commands
