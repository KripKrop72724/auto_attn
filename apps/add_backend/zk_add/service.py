from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.crypto import (
    cnic_lookup,
    decrypt_cnic,
    decrypt_json,
    decrypt_text,
    encrypt_cnic,
    encrypt_json,
    encrypt_text,
    mask_cnic,
    normalize_cnic,
)
from zk_add.identity import build_machine_name, parse_machine_name
from zk_add.identity_conflicts import (
    valid_identity_resolutions,
    valid_resolution_for_user,
)
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
    DeviceUserSnapshot,
    HistoricalCurrentIdentityResolution,
    IdentityConflictResolution,
    IdentityTombstone,
    OracleReceipt,
    OrdsOutbox,
    Site,
    TemporaryAdminLease,
    UserDeletionItem,
    UserDeletionJob,
    ZKTDevice,
)
from zk_add.schemas import (
    AttendanceEventIn,
    HeartbeatPayload,
    OracleReceiptBatchRequest,
    UserSnapshotRequest,
)
from zk_add.security import connector_token_hash
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, parse_datetime, utc_now


ACTIVE_COMMAND_STATES = {
    "QUEUED",
    "WAITING_FOR_DEVICE",
    "WAITING_FOR_ZKT",
    "RETRYING",
    "DISPATCHED",
    "ACKNOWLEDGED",
    "RUNNING",
    "CANCEL_REQUESTED",
}
TERMINAL_COMMAND_STATES = {
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}
MUTATING_COMMANDS = {
    "CREATE_USER",
    "UPDATE_USER",
    "DELETE_USER",
    "GRANT_TEMP_ADMIN",
    "REVOKE_TEMP_ADMIN",
    "RESTART_ZKT",
    "APPLY_CONFIG",
}
ORACLE_ALLOWED_CAPTURE_TYPES = {
    "LIVE",
    "LIVE_POLL",
    "DUMP_RECONNECT",
    "DUMP_STARTUP",
    "MANUAL_REPROCESS",
}
IDENTITY_CONFLICT_DUPLICATE_CNIC = "DUPLICATE_CNIC"
ACTIVE_USER_DELETION_JOB_STATES = {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}
TERMINAL_USER_DELETION_ITEM_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"}
UNRESOLVED_HISTORICAL_IDENTITY_STATES = {
    "BLOCKED_IDENTITY",
    "QUARANTINED_IDENTITY_REUSE",
}
ORACLE_CONFIRMATION_PATH_PRIORITY = {
    "FIRMWARE_LIVE": 1,
    "FIRMWARE_BULK": 2,
    "FIRMWARE_RECONCILE": 3,
}
MIN_PLAUSIBLE_ATTENDANCE_TIME = datetime(2010, 1, 1, tzinfo=timezone.utc)
MAX_DEVICE_CLOCK_LEAD = timedelta(days=1)


def attendance_device_time_is_plausible(
    device_event_time: datetime,
    captured_at: datetime,
) -> bool:
    event_time = ensure_utc(device_event_time)
    capture_time = ensure_utc(captured_at)
    return (
        event_time >= MIN_PLAUSIBLE_ATTENDANCE_TIME
        and event_time <= capture_time + MAX_DEVICE_CLOCK_LEAD
    )


def user_snapshot_state_hash(snapshot: UserSnapshotRequest) -> str:
    """Hash every terminal field that can change attendance identity or authorization."""
    digest = hashlib.sha256()
    for row in snapshot.users:
        for value in (
            row.uid,
            row.user_id,
            row.terminal_identity_fingerprint or "",
            row.terminal_state_fingerprint or "",
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def ensure_site(session: Session, zone_id: str, zone_name: str) -> Site:
    site = session.scalar(select(Site).where(Site.site_id == zone_id))
    if site is None:
        site = Site(site_id=zone_id, name=zone_name, timezone="Asia/Karachi")
        session.add(site)
        session.flush()
    return site


def onboard_connector(
    session: Session,
    *,
    hardware_id: str,
    zone_id: str,
    zone_name: str,
    device_id: str,
    firmware_version: str,
    expected_serial: str | None,
    actor: str,
    ip_address: str | None,
) -> tuple[Connector, str, bool]:
    site = ensure_site(session, zone_id, zone_name)
    connector = session.scalar(select(Connector).where(Connector.hardware_id == hardware_id))
    created = connector is None
    if connector is None:
        connector = Connector(
            connector_id=str(uuid4()),
            hardware_id=hardware_id,
            site_id=site.id,
            zone_id=zone_id,
            zone_name=zone_name,
            device_id=device_id,
            display_name=zone_name,
            firmware_version=firmware_version,
            lifecycle_state="ONBOARDING",
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
                    "create_user": False,
                    "delete_user": False,
                    "admin_lease": False,
                    "protocol_restart": False,
                    "telnet_recovery": False,
                    "name_bytes": 24,
                },
            )
        )
    else:
        connector.site_id = site.id
        connector.zone_id = zone_id
        connector.zone_name = zone_name
        connector.device_id = device_id
        connector.display_name = zone_name
        connector.firmware_version = firmware_version
        if connector.zkt_device and expected_serial and not connector.zkt_device.expected_serial:
            connector.zkt_device.expected_serial = expected_serial

    now = utc_now()
    overlap_until = now + timedelta(seconds=settings.onboarding_token_overlap_seconds)
    for credential in session.scalars(
        select(ConnectorCredential).where(
            ConnectorCredential.connector_id == connector.id,
            ConnectorCredential.active == True,  # noqa: E712
            ConnectorCredential.revoked_at == None,  # noqa: E711
        )
    ).all():
        credential.valid_until = overlap_until
    raw_token = secrets.token_urlsafe(48)
    session.add(
        ConnectorCredential(
            connector_id=connector.id,
            token_hash=connector_token_hash(raw_token),
            token_last4=raw_token[-4:],
        )
    )
    connector.onboarding_generation = (connector.onboarding_generation or 0) + 1
    connector.last_onboarded_at = now
    connector.updated_at = now
    append_audit(
        session,
        actor=actor,
        action="CONNECTOR_AUTO_ONBOARDED" if created else "CONNECTOR_TOKEN_ROTATED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="SUCCESS",
        ip_address=ip_address,
        after={
            "hardware_id": hardware_id,
            "zone_id": zone_id,
            "device_id": device_id,
            "generation": connector.onboarding_generation,
        },
    )
    return connector, raw_token, created


def auto_certify_zkt(session: Session, connector: Connector, zkt: ZKTDevice) -> None:
    if not zkt.serial or not zkt.online:
        return
    if zkt.expected_serial and zkt.expected_serial != zkt.serial:
        zkt.certification_state = "READ_ONLY"
        zkt.writes_disabled_reason = "SERIAL_MISMATCH"
        return
    duplicates = session.scalars(
        select(ZKTDevice).where(
            ZKTDevice.serial == zkt.serial,
            ZKTDevice.id != zkt.id,
        )
    ).all()
    if duplicates:
        # A duplicate serial is ambiguous by definition. Quarantining only the
        # newest claimant would leave an older connector able to mutate the same
        # terminal, so every authenticated claimant is made read-only.
        for claimed in [zkt, *duplicates]:
            claimant = session.get(Connector, claimed.connector_id)
            if claimant is None:
                continue
            claimant.lifecycle_state = "QUARANTINED_DUPLICATE_SERIAL"
            claimant.last_error_code = "QUARANTINED_DUPLICATE_SERIAL"
            claimant.last_error_message = (
                "This ZKT serial is claimed by more than one authenticated connector."
            )
            claimed.certification_state = "QUARANTINED"
            claimed.writes_disabled_reason = "DUPLICATE_SERIAL"
            claimed.capability_profile = {
                **(claimed.capability_profile or {}),
                "user_write": False,
                "create_user": False,
                "delete_user": False,
                "admin_lease": False,
                "protocol_restart": False,
                "telnet_recovery": False,
            }
            upsert_alert(
                session,
                claimant,
                code="QUARANTINED_DUPLICATE_SERIAL",
                severity="CRITICAL",
                message=claimant.last_error_message,
                details={"serial": zkt.serial},
            )
        return

    record_size = int((zkt.capability_profile or {}).get("observed_user_record_bytes", 0))
    fingerprint = f"{zkt.serial}|{zkt.model or ''}|{zkt.platform or ''}|{record_size}"
    if zkt.certification_fingerprint != fingerprint:
        zkt.certification_fingerprint = fingerprint
        zkt.certification_observations = 1
    else:
        zkt.certification_observations = min(zkt.certification_observations + 1, 1000)
    if not zkt.stability_since:
        return
    stability_since = ensure_utc(zkt.stability_since)
    if utc_now() - stability_since < timedelta(minutes=2):
        return
    if zkt.certification_observations < 2:
        return

    writable = record_size == 72 and zkt.snapshot_complete
    zkt.certification_state = "CERTIFIED" if writable else "READ_ONLY"
    zkt.writes_disabled_reason = None if writable else (
        "LEGACY_28_BYTE_RECORD" if record_size == 28 else "FULL_USER_SNAPSHOT_REQUIRED"
    )
    zkt.capability_profile = {
        **(zkt.capability_profile or {}),
        "read_users": True,
        "read_attendance": True,
        "user_write": writable,
        "create_user": writable,
        "delete_user": writable,
        "admin_lease": writable,
        "protocol_restart": record_size in {28, 72},
        "telnet_recovery": False,
        "name_bytes": 24 if record_size == 72 else 8,
    }
    if writable:
        resolve_alert(session, connector, code="USER_SNAPSHOT_TRUNCATED")


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
    # A valid heartbeat is the authoritative recovery signal for the ESP
    # transport.  The maintenance loop opens this alert when heartbeats go
    # stale, so resolve it immediately when the connector reports again.
    resolve_alert(session, connector, code="ESP_OFFLINE")
    zkt = connector.zkt_device
    zkt_payload = payload.zkt
    if zkt:
        previous_terminal_serial = zkt.serial
        previous_attendance_count = zkt.attendance_count
        reported_state = str(
            zkt_payload.get("connection_state")
            or ("ONLINE" if zkt_payload.get("online", False) else "OFFLINE")
        ).upper()
        previous_state = zkt.connection_state
        # SESSION_REFRESH is a short, deliberate socket rotation used between
        # bounded truth reads. It remains command-gated by the connector while
        # the socket is closed, but must not be classified as a network flap.
        zkt.online = reported_state in {"ONLINE", "RECOVERING"}
        zkt.connection_state = reported_state
        zkt.consecutive_failures = int(
            zkt_payload.get("consecutive_failures", zkt.consecutive_failures)
        )
        zkt.consecutive_successes = int(
            zkt_payload.get("consecutive_successes", zkt.consecutive_successes)
        )
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
        capabilities = zkt_payload.get("reconciliation_capabilities")
        if isinstance(capabilities, dict):
            try:
                max_chunk_records = int(capabilities.get("max_chunk_records") or 1)
            except (TypeError, ValueError):
                max_chunk_records = 1
            try:
                source_coverage_cursor = int(
                    capabilities.get("source_coverage_cursor") or 0
                )
            except (TypeError, ValueError):
                source_coverage_cursor = 0
            try:
                max_credit_records = int(
                    capabilities.get("max_credit_records") or 100
                )
            except (TypeError, ValueError):
                max_credit_records = 100
            zkt.capability_profile = {
                **(zkt.capability_profile or {}),
                "history_stream_v1": bool(capabilities.get("history_stream_v1")),
                "history_stream_v2": bool(capabilities.get("history_stream_v2")),
                "partial_final_chunk_v1": bool(
                    capabilities.get("partial_final_chunk_v1")
                ),
                "source_divergence_probe_v1": bool(
                    capabilities.get("source_divergence_probe_v1")
                ),
                "source_tail_v1": bool(capabilities.get("source_tail_v1")),
                "history_range_resume_verified": bool(
                    capabilities.get("history_range_resume_verified")
                ),
                "history_chunk_max_records": max(
                    1, min(100, max_chunk_records)
                ),
                "history_credit_max_records": max(
                    100,
                    min(2000, max_credit_records),
                ),
                "source_coverage_certified": bool(
                    capabilities.get("source_coverage_certified")
                ),
                "source_coverage_cursor": max(
                    0, source_coverage_cursor
                ),
            }
        from zk_add.reconciliation import invalidate_coverage_for_terminal_change

        invalidate_coverage_for_terminal_change(
            session,
            zkt=zkt,
            previous_serial=previous_terminal_serial,
            previous_attendance_count=previous_attendance_count,
        )
        zkt.device_time_drift_seconds = zkt_payload.get(
            "drift_seconds", zkt.device_time_drift_seconds
        )
        zkt.last_seen_at = now
        zkt.updated_at = now
        if reported_state in {"ONLINE", "RECOVERING", "SESSION_REFRESH"}:
            zkt.last_online_at = now
            zkt.offline_since = None
        elif zkt.offline_since is None:
            zkt.offline_since = now
        # Null explicitly clears current-state timestamps. Omitted fields are
        # left alone so older firmware cannot erase newer backend state.
        for field_name in ("backoff_until", "stability_since", "next_restart_at"):
            if field_name in zkt_payload:
                value = zkt_payload[field_name]
                setattr(zkt, field_name, parse_datetime(value) if value else None)
        # A rebooted connector initially reports no completed reconcile. Keep
        # the last known successful one until a newer success is reported.
        last_reconcile = zkt_payload.get("last_reconcile_at")
        if last_reconcile:
            zkt.last_reconcile_at = parse_datetime(last_reconcile)
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
            connector.last_error_message = (
                "ZKT connectivity is unstable; connector is in protective backoff."
            )
            upsert_alert(
                session,
                connector,
                code="ZKT_CONNECTION_FLAPPING",
                severity="WARNING",
                message=connector.last_error_message,
                details={
                    "flaps_15m": zkt.flap_count_15m,
                    "backoff_until": zkt_payload.get("backoff_until"),
                },
            )
        elif reported_state in {"OFFLINE", "RETRY_WAIT", "DISCOVERING"}:
            connector.lifecycle_state = "DEGRADED"
        elif reported_state in {"ONLINE", "RECOVERING"}:
            connector.lifecycle_state = "ONLINE" if reported_state == "ONLINE" else "DEGRADED"
            if zkt.consecutive_successes >= 3:
                resolve_alert(session, connector, code="ZKT_CONNECTION_FLAPPING")
        elif reported_state == "SESSION_REFRESH":
            connector.lifecycle_state = "ONLINE"
        sample = zkt_payload.get("device_time")
        sampled_at = zkt_payload.get("device_time_sampled_at")
        if sample:
            zkt.sampled_device_time = parse_datetime(sample)
        if sampled_at:
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
            connector.last_error_message = (
                "Authenticated ZKT serial does not match the assigned device."
            )
            zkt.online = False
            upsert_alert(
                session,
                connector,
                code="ZKT_SERIAL_MISMATCH",
                severity="CRITICAL",
                message=connector.last_error_message,
            )
        if connector.lifecycle_state != "QUARANTINED_DUPLICATE_SERIAL":
            auto_certify_zkt(session, connector, zkt)
        history = zkt_payload.get("history_backfill")
        if isinstance(history, dict):
            history_state = str(history.get("state") or "NOT_STARTED").upper()
            allowed_history_states = {
                "NOT_STARTED",
                "RUNNING",
                "RETRYING",
                "BLOCKED",
                "COMPLETE",
            }
            if history_state not in allowed_history_states:
                history_state = "NOT_STARTED"
            try:
                history_failed_windows = max(
                    0, int(history.get("failed_windows") or 0)
                )
            except (TypeError, ValueError):
                history_failed_windows = 0
            zkt.capability_profile = {
                **(zkt.capability_profile or {}),
                "history_backfill_state": history_state,
                "history_coverage_start_month": str(
                    history.get("coverage_start_month") or ""
                )[:7],
                "history_cursor_month": str(history.get("cursor_month") or "")[:7],
                "history_last_sweep_at": str(history.get("last_sweep_at") or "")[:40],
                "history_failed_windows": history_failed_windows,
            }
            if history_state == "BLOCKED":
                upsert_alert(
                    session,
                    connector,
                    code="HISTORY_BACKFILL_BLOCKED",
                    severity="HIGH",
                    message=(
                        "Historical attendance truth contains unresolved identity windows; "
                        "Oracle replacement stayed fail-closed."
                    ),
                    details={
                        "coverage_start_month": history.get("coverage_start_month"),
                        "failed_windows": history_failed_windows,
                    },
                )
            elif history_state in {"RUNNING", "RETRYING", "COMPLETE"}:
                resolve_alert(session, connector, code="HISTORY_BACKFILL_BLOCKED")
    reported_led_state = (payload.led_state or "").strip().upper()
    if reported_led_state in {"FATAL", "LOCAL_FAILURE"}:
        code = "ESP_FATAL" if reported_led_state == "FATAL" else "ESP_LOCAL_FAILURE"
        message = (
            "The connector reports a fatal boot/security failure and may be inert."
            if reported_led_state == "FATAL"
            else "The connector reports a recoverable local storage/resource failure."
        )
        if connector.lifecycle_state != "QUARANTINED_DUPLICATE_SERIAL":
            connector.lifecycle_state = "DEGRADED"
        connector.last_error_code = code
        connector.last_error_message = message
        upsert_alert(
            session,
            connector,
            code=code,
            severity="CRITICAL" if reported_led_state == "FATAL" else "HIGH",
            message=message,
            details={"led_state": reported_led_state},
        )
    else:
        resolve_alert(session, connector, code="ESP_FATAL")
        resolve_alert(session, connector, code="ESP_LOCAL_FAILURE")
        if connector.last_error_code in {"ESP_FATAL", "ESP_LOCAL_FAILURE"}:
            connector.last_error_code = None
            connector.last_error_message = None
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
            payload=redact_context(payload.model_dump(mode="json")),
        )
    )
    return serialize_connector(connector)


def replace_user_snapshot(
    session: Session, *, connector: Connector, snapshot: UserSnapshotRequest
) -> int:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")

    computed_state_hash = user_snapshot_state_hash(snapshot)
    if snapshot.state_hash and snapshot.state_hash != computed_state_hash:
        raise ValueError("User snapshot state hash does not match its terminal rows.")
    stable_complete = snapshot.complete and snapshot.stable
    latest_recorded_revision = session.scalar(
        select(func.max(DeviceUserSnapshot.revision)).where(
            DeviceUserSnapshot.zkt_device_id == zkt.id
        )
    )
    next_revision = max(zkt.identity_snapshot_revision or 0, latest_recorded_revision or 0) + 1
    snapshot_record = DeviceUserSnapshot(
        zkt_device_id=zkt.id,
        snapshot_id=snapshot.snapshot_id,
        revision=next_revision,
        state_hash=computed_state_hash,
        complete=snapshot.complete,
        stable=snapshot.stable,
        reason=snapshot.reason,
        user_count=len(snapshot.users),
        started_at=ensure_utc(snapshot.started_at) if snapshot.started_at else None,
        observed_at=ensure_utc(snapshot.observed_at),
    )
    session.add(snapshot_record)
    session.flush()

    incoming_by_uid = {}
    incoming_user_ids: set[str] = set()
    for incoming in snapshot.users:
        if incoming.uid in incoming_by_uid:
            raise ValueError(f"Duplicate UID {incoming.uid} in user snapshot.")
        if incoming.user_id in incoming_user_ids:
            raise ValueError(f"Duplicate device user ID {incoming.user_id} in user snapshot.")
        incoming_by_uid[incoming.uid] = incoming
        incoming_user_ids.add(incoming.user_id)

    existing_rows = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all()
    )
    rows_by_uid = {row.uid: row for row in existing_rows}
    uid_claiming_user_id = {incoming.user_id: incoming.uid for incoming in snapshot.users}
    if not snapshot.complete:
        # A partial snapshot is evidence about rows that were observed, never
        # evidence that an unobserved identity disappeared.  Refuse ambiguous
        # UID/user-ID replacement until a complete table can resolve it.
        for row in existing_rows:
            claiming_uid = uid_claiming_user_id.get(row.user_id)
            if claiming_uid not in {None, row.uid} and row.uid not in incoming_by_uid:
                raise ValueError(
                    "A partial user snapshot cannot replace an unobserved identity."
                )
    for row in existing_rows:
        incoming = incoming_by_uid.get(row.uid)
        changes_user_id = incoming is not None and incoming.user_id != row.user_id
        claimed_by_other_uid = uid_claiming_user_id.get(row.user_id) not in {None, row.uid}
        if changes_user_id or claimed_by_other_uid:
            row.lifecycle_state = "STAGING"
    session.flush()
    seen: set[str] = set()
    identity_changed: list[DeviceUser] = []
    observed_at = ensure_utc(snapshot.observed_at)
    updated_at = utc_now()
    for incoming in snapshot.users:
        seen.add(incoming.uid)
        parsed = parse_machine_name(incoming.name)
        row = rows_by_uid.get(incoming.uid)
        previous_cnic_hash = row.cnic_lookup_hash if row is not None else None
        previous_state_fingerprint = row.terminal_state_fingerprint if row is not None else None
        terminal_state_changed = bool(
            row is not None
            and (
                row.user_id != incoming.user_id
                or (decrypt_text(row.machine_name_encrypted) or "") != incoming.name
                or row.terminal_identity_fingerprint
                != incoming.terminal_identity_fingerprint
                or row.terminal_state_fingerprint != incoming.terminal_state_fingerprint
                or row.privilege != incoming.privilege
                or row.card != incoming.card
                or not row.present
                or row.lifecycle_state != "ACTIVE"
            )
        )
        if row is None:
            conflicting = session.scalar(
                select(DeviceUser).where(
                    DeviceUser.zkt_device_id == zkt.id,
                    DeviceUser.user_id == incoming.user_id,
                    DeviceUser.lifecycle_state == "ACTIVE",
                )
            )
            if conflicting is not None:
                persist_identity_tombstone(session, zkt=zkt, user=conflicting)
                conflicting.present = False
                conflicting.lifecycle_state = "DELETED"
                conflicting.deleted_at = updated_at
                conflicting.deleted_by = "device:snapshot-replacement"
                conflicting.row_version += 1
                session.flush()
            row = DeviceUser(
                zkt_device_id=zkt.id,
                uid=incoming.uid,
                user_id=incoming.user_id,
                machine_name_encrypted=encrypt_text(incoming.name),
                terminal_identity_fingerprint=incoming.terminal_identity_fingerprint,
                terminal_state_fingerprint=incoming.terminal_state_fingerprint,
                display_name=parsed.display_name,
                row_version=1,
                lifecycle_state="ACTIVE",
                source="DEVICE_SNAPSHOT",
            )
            session.add(row)
            session.flush()
            rows_by_uid[incoming.uid] = row
        elif terminal_state_changed:
            row.row_version = (row.row_version or 0) + 1
        row.user_id = incoming.user_id
        row.machine_name_encrypted = encrypt_text(incoming.name)
        row.terminal_identity_fingerprint = incoming.terminal_identity_fingerprint
        row.terminal_state_fingerprint = incoming.terminal_state_fingerprint
        row.display_name = parsed.display_name
        if parsed.cnic:
            next_hash = cnic_lookup(parsed.cnic)
            # Exclude every participant from the partial unique index before
            # assigning a colliding hash. This also handles older terminals
            # that legitimately report the same CNIC on multiple user IDs.
            conflicting_rows = [
                candidate
                for candidate in rows_by_uid.values()
                if candidate is not row
                and candidate.lifecycle_state in {"ACTIVE", "STAGING"}
                and candidate.cnic_lookup_hash == next_hash
            ]
            if conflicting_rows:
                row.identity_conflict_code = IDENTITY_CONFLICT_DUPLICATE_CNIC
                for conflicting_row in conflicting_rows:
                    conflicting_row.identity_conflict_code = IDENTITY_CONFLICT_DUPLICATE_CNIC
                session.flush()
            row.cnic_encrypted = encrypt_cnic(parsed.cnic)
            row.cnic_lookup_hash = next_hash
            row.cnic_last4 = parsed.cnic[-4:]
            row.shift_worker = parsed.shift_worker
        else:
            row.cnic_encrypted = None
            row.cnic_lookup_hash = None
            row.cnic_last4 = None
            row.shift_worker = False
        if (
            previous_cnic_hash != row.cnic_lookup_hash
            or previous_state_fingerprint != incoming.terminal_state_fingerprint
        ):
            identity_changed.append(row)
        row.privilege = incoming.privilege
        row.card = incoming.card
        row.present = True
        row.lifecycle_state = "ACTIVE"
        row.deleted_at = None
        row.deleted_by = None
        row.snapshot_id = snapshot.snapshot_id
        row.snapshot_revision = next_revision
        row.observed_at = observed_at
        row.updated_at = updated_at
    if stable_complete:
        for row in existing_rows:
            if row.uid not in seen:
                persist_identity_tombstone(session, zkt=zkt, user=row)
                row.present = False
                row.lifecycle_state = "DELETED"
                row.deleted_at = updated_at
                row.deleted_by = "device:complete-snapshot"
                row.row_version += 1
                row.updated_at = updated_at
        zkt.user_count = len(snapshot.users)
        zkt.snapshot_complete = True
        zkt.identity_snapshot_revision = next_revision
        zkt.identity_snapshot_id = snapshot_record.id
        zkt.identity_snapshot_state_hash = computed_state_hash
        zkt.identity_snapshot_observed_at = observed_at
        zkt.identity_snapshot_received_at = updated_at
        zkt.identity_snapshot_stable = True
        # Reconstruct the start of the current uninterrupted identity state
        # from durable snapshot revisions. This safely recovers back-dated
        # missing-UID punches when several complete snapshots prove that the
        # terminal catalog did not change. A different, partial, or unstable
        # snapshot is an evidence boundary and prevents repair across it.
        continuity_boundary_revision = session.scalar(
            select(func.max(DeviceUserSnapshot.revision)).where(
                DeviceUserSnapshot.zkt_device_id == zkt.id,
                or_(
                    DeviceUserSnapshot.complete == False,  # noqa: E712
                    DeviceUserSnapshot.stable == False,  # noqa: E712
                    DeviceUserSnapshot.state_hash.is_(None),
                    DeviceUserSnapshot.state_hash != computed_state_hash,
                ),
            )
        )
        continuity_filters = [
            DeviceUserSnapshot.zkt_device_id == zkt.id,
            DeviceUserSnapshot.complete == True,  # noqa: E712
            DeviceUserSnapshot.stable == True,  # noqa: E712
            DeviceUserSnapshot.state_hash == computed_state_hash,
        ]
        if continuity_boundary_revision is not None:
            continuity_filters.append(
                DeviceUserSnapshot.revision > continuity_boundary_revision
            )
        continuity_started_at = session.scalar(
            select(func.min(DeviceUserSnapshot.observed_at)).where(*continuity_filters)
        )
        zkt.last_identity_change_at = continuity_started_at or observed_at
        if zkt.writes_disabled_reason == "USER_SNAPSHOT_TRUNCATED":
            zkt.writes_disabled_reason = None
    else:
        for row in existing_rows:
            if row.uid not in seen and row.lifecycle_state == "STAGING":
                row.lifecycle_state = "ACTIVE"
        zkt.snapshot_complete = False
        zkt.identity_snapshot_stable = False
        zkt.writes_disabled_reason = "USER_SNAPSHOT_TRUNCATED"
        zkt.certification_state = "READ_ONLY"
        zkt.capability_profile = {
            **(zkt.capability_profile or {}),
            "user_write": False,
            "create_user": False,
            "delete_user": False,
            "admin_lease": False,
        }
        upsert_alert(
            session,
            connector,
            code="USER_SNAPSHOT_TRUNCATED",
            severity="CRITICAL",
            message="The connector reported a partial user snapshot; terminal writes are disabled.",
            details={"snapshot_id": snapshot.snapshot_id, "rows_received": len(snapshot.users)},
        )
    resolved_conflicts = reconcile_device_user_identity_conflicts(
        session, connector=connector, zkt=zkt
    )
    affected = {
        row.id: row
        for row in [*identity_changed, *resolved_conflicts]
        if row.id is not None
        and row.lifecycle_state == "ACTIVE"
    }
    for row in session.scalars(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.identity_conflict_code.is_not(None),
        )
    ).all():
        if row.id is not None:
            affected[row.id] = row
    for row in affected.values():
        if row.cnic_lookup_hash and row.identity_conflict_code is None:
            enrich_undelivered_attendance(
                session, zkt=zkt, user=row, snapshot=snapshot_record
            )
        else:
            block_undelivered_attendance(
                session, zkt=zkt, user=row, snapshot=snapshot_record
            )
    zkt.updated_at = utc_now()
    return len(snapshot.users)


def reconcile_device_user_identity_conflicts(
    session: Session, *, connector: Connector, zkt: ZKTDevice
) -> list[DeviceUser]:
    """Quarantine duplicate active CNICs without deleting identity or punch data."""

    with session.no_autoflush:
        rows = list(
            session.scalars(
                select(DeviceUser).where(
                    DeviceUser.zkt_device_id == zkt.id,
                    DeviceUser.lifecycle_state == "ACTIVE",
                )
            ).all()
        )
    groups: dict[str, list[DeviceUser]] = {}
    for row in rows:
        if row.cnic_lookup_hash:
            groups.setdefault(row.cnic_lookup_hash, []).append(row)

    valid_resolutions = valid_identity_resolutions(
        session,
        zkt=zkt,
        groups=groups,
        mark_stale=True,
    )

    duplicate_rows = {
        row.id: row
        for group in groups.values()
        if len(group) > 1
        for row in group
        if row.id is not None
    }
    unresolved_groups = {
        lookup: group
        for lookup, group in groups.items()
        if len(group) > 1 and lookup not in valid_resolutions
    }
    unresolved_rows = {
        row.id: row
        for group in unresolved_groups.values()
        for row in group
        if row.id is not None
    }
    previously_conflicted = {
        row.id
        for row in rows
        if row.id is not None
        and row.identity_conflict_code == IDENTITY_CONFLICT_DUPLICATE_CNIC
    }

    # Phase one excludes every duplicate from the partial unique index. Only
    # after that flush is it safe to clear conflicts that became singletons.
    for row in duplicate_rows.values():
        row.identity_conflict_code = IDENTITY_CONFLICT_DUPLICATE_CNIC
    session.flush()
    for row in rows:
        if (
            row.id not in duplicate_rows
            and row.identity_conflict_code == IDENTITY_CONFLICT_DUPLICATE_CNIC
        ):
            row.identity_conflict_code = None
    session.flush()

    if unresolved_rows:
        upsert_alert(
            session,
            connector,
            code="DUPLICATE_USER_CNIC",
            severity="HIGH",
            message=(
                "Multiple active terminal users share a CNIC without a verified "
                "same-employee resolution."
            ),
            details={
                "affected_users": len(unresolved_rows),
                "duplicate_groups": len(unresolved_groups),
            },
        )
    else:
        resolve_alert(session, connector, code="DUPLICATE_USER_CNIC")

    return [
        row
        for row in rows
        if row.id in previously_conflicted and row.identity_conflict_code is None
    ]


def persist_identity_tombstone(
    session: Session, *, zkt: ZKTDevice, user: DeviceUser
) -> IdentityTombstone:
    existing = session.scalar(
        select(IdentityTombstone).where(
            IdentityTombstone.zkt_device_id == zkt.id,
            IdentityTombstone.user_id == user.user_id,
            IdentityTombstone.device_user_id == user.id,
        )
    )
    if existing is not None:
        return existing
    conflict_is_resolved = bool(
        user.identity_conflict_code
        and valid_resolution_for_user(session, zkt=zkt, user=user)
    )
    identity_is_usable = user.identity_conflict_code is None or conflict_is_resolved
    row = IdentityTombstone(
        zkt_device_id=zkt.id,
        device_user_id=user.id,
        device_serial=zkt.serial,
        uid=user.uid,
        user_id=user.user_id,
        display_name_encrypted=encrypt_text(user.display_name) or "",
        cnic_encrypted=(user.cnic_encrypted if identity_is_usable else None),
        cnic_lookup_hash=(user.cnic_lookup_hash if identity_is_usable else None),
        cnic_last4=user.cnic_last4 if identity_is_usable else None,
        shift_worker=user.shift_worker,
        privilege=user.privilege,
    )
    session.add(row)
    return row


def create_historical_identity_alias(
    session: Session,
    *,
    connector: Connector,
    source_user_id: str,
    source_cnic: str,
    target_user: DeviceUser,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> tuple[IdentityTombstone, int]:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    source_user_id = source_user_id.strip()
    if not source_user_id or source_user_id == target_user.user_id:
        raise ValueError("Historical user ID must differ from the active target user ID.")
    if target_user.zkt_device_id != zkt.id or not target_user.present or (
        target_user.lifecycle_state != "ACTIVE"
    ):
        raise ValueError("Historical aliases require a present active user on this terminal.")
    if target_user.identity_conflict_code is not None:
        raise ValueError("Resolve the target user's identity conflict before creating an alias.")
    cnic = decrypt_cnic(target_user.cnic_encrypted)
    if not cnic or not target_user.cnic_lookup_hash:
        raise ValueError("Historical aliases require a verified target CNIC.")
    if not secrets.compare_digest(source_cnic, cnic):
        raise ValueError(
            "Historical source evidence does not match the active target CNIC."
        )
    if session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_id == source_user_id,
            DeviceUser.present == True,  # noqa: E712
            DeviceUser.lifecycle_state == "ACTIVE",
        )
    ):
        raise ValueError("Historical user ID is currently active on the terminal.")
    existing = session.scalar(
        select(IdentityTombstone)
        .where(
            IdentityTombstone.zkt_device_id == zkt.id,
            IdentityTombstone.user_id == source_user_id,
        )
        .order_by(IdentityTombstone.id.desc())
    )
    if existing is not None:
        if existing.device_user_id != target_user.id:
            raise ValueError("Historical user ID is already bound to another identity.")
        return existing, 0

    eligible_statuses = {
        "BLOCKED_IDENTITY",
        "PENDING",
        "FAILED_RETRYABLE",
        "RETRYING",
    }
    blocked_rows = session.scalars(
        select(AttendanceEvent).where(
            AttendanceEvent.zkt_device_id == zkt.id,
            AttendanceEvent.user_id == source_user_id,
            AttendanceEvent.ords_status.in_(eligible_statuses),
            AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
        )
    ).all()
    if not blocked_rows:
        raise ValueError("No unresolved attendance exists for that historical user ID.")

    tombstone = IdentityTombstone(
        zkt_device_id=zkt.id,
        device_user_id=target_user.id,
        device_serial=zkt.serial,
        uid=target_user.uid,
        user_id=source_user_id,
        display_name_encrypted=encrypt_text(target_user.display_name) or "",
        cnic_encrypted=target_user.cnic_encrypted,
        cnic_lookup_hash=target_user.cnic_lookup_hash,
        cnic_last4=target_user.cnic_last4,
        shift_worker=target_user.shift_worker,
        privilege=target_user.privilege,
    )
    session.add(tombstone)
    session.flush()

    repaired = 0
    for row in blocked_rows:
        if row.device_user_id not in {None, target_user.id}:
            row.ords_status = "QUARANTINED_IDENTITY_REUSE"
            row.identity_resolution_status = "QUARANTINED_REUSE"
            continue
        row.device_user_id = target_user.id
        row.identity_snapshot_id = zkt.identity_snapshot_id
        row.identity_terminal_fingerprint = None
        row.identity_resolution_status = "RESOLVED_HISTORICAL_ALIAS"
        row.identity_resolved_at = utc_now()
        row.identity_repaired_at = utc_now()
        row.identity_repair_reason = "VERIFIED_HISTORICAL_ALIAS"
        row.display_name = target_user.display_name
        row.cnic_encrypted = target_user.cnic_encrypted
        row.cnic_lookup_hash = target_user.cnic_lookup_hash
        row.cnic_last4 = target_user.cnic_last4
        row.raw_punch = row.raw_punch or target_user.shift_worker
        row.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None
        repaired += 1

    append_audit(
        session,
        actor=actor,
        action="HISTORICAL_IDENTITY_ALIAS_VERIFIED",
        target_type="zkt_user_identity_alias",
        target_id=f"{zkt.id}:{source_user_id}",
        outcome="SUCCEEDED",
        before={
            "source_user_id": source_user_id,
            "source_cnic": mask_cnic(source_cnic),
            "blocked_events": len(blocked_rows),
        },
        after={
            "target_user_key": target_user.user_key,
            "target_user_id": target_user.user_id,
            "target_cnic": mask_cnic(cnic),
            "repaired_events": repaired,
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
    )
    return tombstone, repaired


def create_historical_directory_identity(
    session: Session,
    *,
    connector: Connector,
    source_user: DeviceUser,
    source_cnic: str,
    directory_employee_id: str,
    directory_service_number: str,
    directory_employee_name: str,
    directory_zone_code: str | None,
    expected_version: int,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> tuple[IdentityTombstone, int]:
    """Repair a deleted terminal identity from exact authoritative HR evidence."""

    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    if source_user.zkt_device_id != zkt.id:
        raise ValueError("Historical user does not belong to this terminal.")
    session.refresh(source_user)
    if source_user.row_version != expected_version:
        raise ValueError("Historical user changed since it was selected. Refresh and retry.")
    if source_user.present or source_user.lifecycle_state == "ACTIVE":
        raise ValueError("Directory evidence repair is only allowed for a deleted user.")
    if source_user.identity_conflict_code is not None:
        raise ValueError("Resolve the historical user's identity conflict first.")

    normalized_cnic = normalize_cnic(source_cnic)
    normalized_user_id = source_user.user_id.strip()
    normalized_service_number = directory_service_number.strip()
    if normalized_user_id.isdigit() and normalized_service_number.isdigit():
        service_number_matches = (
            (normalized_user_id.lstrip("0") or "0")
            == (normalized_service_number.lstrip("0") or "0")
        )
    else:
        service_number_matches = secrets.compare_digest(
            normalized_user_id.upper(),
            normalized_service_number.upper(),
        )
    if not service_number_matches:
        raise ValueError(
            "Terminal user ID does not exactly match the authoritative HR service number."
        )

    def normalized_name(value: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", value.upper())

    terminal_name = normalized_name(source_user.display_name)
    directory_name = normalized_name(directory_employee_name)
    if (
        len(terminal_name) < 5
        or len(directory_name) < 5
        or not (
            secrets.compare_digest(terminal_name, directory_name)
            or terminal_name.startswith(directory_name)
            or directory_name.startswith(terminal_name)
        )
    ):
        raise ValueError(
            "Terminal name does not match the authoritative HR employee name."
        )

    cnic_hash = cnic_lookup(normalized_cnic)
    conflicting_claim = session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.cnic_lookup_hash == cnic_hash,
            DeviceUser.id != source_user.id,
        )
    )
    if conflicting_claim is not None:
        raise ValueError(
            "That CNIC is already bound to another identity on this terminal."
        )

    existing = session.scalar(
        select(IdentityTombstone)
        .where(
            IdentityTombstone.zkt_device_id == zkt.id,
            IdentityTombstone.user_id == source_user.user_id,
            IdentityTombstone.device_user_id == source_user.id,
        )
        .order_by(IdentityTombstone.id.desc())
    )
    if existing is not None and existing.cnic_encrypted:
        existing_cnic = decrypt_cnic(existing.cnic_encrypted)
        if not existing_cnic or not secrets.compare_digest(existing_cnic, normalized_cnic):
            raise ValueError("Historical identity already has different verified evidence.")
        return existing, 0

    eligible_statuses = {
        "BLOCKED_IDENTITY",
        "PENDING",
        "FAILED_RETRYABLE",
        "RETRYING",
    }
    blocked_rows = session.scalars(
        select(AttendanceEvent).where(
            AttendanceEvent.zkt_device_id == zkt.id,
            AttendanceEvent.user_id == source_user.user_id,
            AttendanceEvent.ords_status.in_(eligible_statuses),
            AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
        )
    ).all()
    if not blocked_rows:
        raise ValueError("No unresolved attendance exists for that historical user ID.")

    encrypted_cnic = encrypt_cnic(normalized_cnic)
    encrypted_name = encrypt_text(directory_employee_name.strip()) or ""
    if existing is None:
        existing = IdentityTombstone(
            zkt_device_id=zkt.id,
            device_user_id=source_user.id,
            device_serial=zkt.serial,
            uid=source_user.uid,
            user_id=source_user.user_id,
            display_name_encrypted=encrypted_name,
            cnic_encrypted=encrypted_cnic,
            cnic_lookup_hash=cnic_hash,
            cnic_last4=normalized_cnic[-4:],
            shift_worker=source_user.shift_worker,
            privilege=source_user.privilege,
        )
        session.add(existing)
    else:
        existing.display_name_encrypted = encrypted_name
        existing.cnic_encrypted = encrypted_cnic
        existing.cnic_lookup_hash = cnic_hash
        existing.cnic_last4 = normalized_cnic[-4:]

    # Preserve verified evidence on both the deleted source and its tombstone
    # so future historical sweeps resolve without operator intervention.
    source_user.cnic_encrypted = encrypted_cnic
    source_user.cnic_lookup_hash = cnic_hash
    source_user.cnic_last4 = normalized_cnic[-4:]
    source_user.row_version += 1

    repaired = 0
    for row in blocked_rows:
        identity_reused = bool(
            (row.device_user_id is not None and row.device_user_id != source_user.id)
            or (
                row.device_user_id is None
                and row.uid
                and source_user.uid
                and row.uid != source_user.uid
            )
        )
        if identity_reused:
            row.ords_status = "QUARANTINED_IDENTITY_REUSE"
            row.identity_resolution_status = "QUARANTINED_REUSE"
            continue
        row.device_user_id = source_user.id
        row.identity_snapshot_id = zkt.identity_snapshot_id
        row.identity_terminal_fingerprint = source_user.terminal_identity_fingerprint
        row.identity_resolution_status = "RESOLVED_DIRECTORY_EVIDENCE"
        row.identity_resolved_at = utc_now()
        row.identity_repaired_at = utc_now()
        row.identity_repair_reason = "VERIFIED_HR_DIRECTORY_EVIDENCE"
        row.display_name = directory_employee_name.strip()
        row.cnic_encrypted = encrypted_cnic
        row.cnic_lookup_hash = cnic_hash
        row.cnic_last4 = normalized_cnic[-4:]
        row.raw_punch = row.raw_punch or source_user.shift_worker
        row.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None
        repaired += 1

    append_audit(
        session,
        actor=actor,
        action="HISTORICAL_DIRECTORY_IDENTITY_VERIFIED",
        target_type="zkt_historical_identity",
        target_id=f"{zkt.id}:{source_user.user_id}",
        outcome="SUCCEEDED",
        before={
            "source_user_key": source_user.user_key,
            "source_user_id": source_user.user_id,
            "blocked_events": len(blocked_rows),
        },
        after={
            "directory_employee_id": directory_employee_id,
            "directory_service_number": directory_service_number,
            "directory_employee_name": directory_employee_name.strip(),
            "directory_zone_code": directory_zone_code,
            "directory_cnic": mask_cnic(normalized_cnic),
            "repaired_events": repaired,
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
    )
    return existing, repaired


def _historical_identity_group_token(
    zkt: ZKTDevice,
    events: list[AttendanceEvent],
) -> str:
    """Version one exact unresolved terminal-identity cohort.

    The token includes every event and the fields that can change its identity
    disposition.  An operator can therefore never apply evidence to a cohort
    that changed after it was reviewed.
    """

    digest = hashlib.sha256()
    digest.update(str(zkt.id).encode("utf-8"))
    digest.update(b"\0")
    for event in sorted(events, key=lambda row: (row.id or 0, row.event_uid)):
        for value in (
            event.id,
            event.event_uid,
            event.user_id,
            event.uid or "",
            event.device_user_id,
            event.ords_status,
        ):
            digest.update(str(value if value is not None else "").encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _service_number_matches(user_id: str, service_number: str) -> bool:
    normalized_user_id = user_id.strip()
    normalized_service_number = service_number.strip()
    if normalized_user_id.isdigit() and normalized_service_number.isdigit():
        return (
            normalized_user_id.lstrip("0") or "0"
        ) == (normalized_service_number.lstrip("0") or "0")
    return secrets.compare_digest(
        normalized_user_id.upper(),
        normalized_service_number.upper(),
    )


def _normalized_identity_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _identity_names_match(left: str, right: str) -> bool:
    normalized_left = _normalized_identity_name(left)
    normalized_right = _normalized_identity_name(right)
    if not normalized_left or not normalized_right:
        return False
    return bool(
        secrets.compare_digest(normalized_left, normalized_right)
        or normalized_left.startswith(normalized_right)
        or normalized_right.startswith(normalized_left)
    )


def create_historical_event_group_identity(
    session: Session,
    *,
    connector: Connector,
    group_token: str,
    source_user_id: str,
    source_uid: str,
    source_cnic: str,
    directory_employee_id: str,
    directory_service_number: str,
    directory_employee_name: str,
    directory_zone_code: str | None,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> tuple[IdentityTombstone, int]:
    """Bind exact orphaned historical events to authoritative HR evidence.

    This path exists for old attendance whose terminal identity disappeared
    before ADD captured a stable user snapshot.  It never matches on a name or
    service number alone: the exact terminal user ID, non-empty terminal UID,
    complete unresolved event membership, and a versioned group token must all
    agree.  A synthetic *deleted* device-user record preserves that evidence
    without mutating the live terminal.
    """

    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    source_user_id = source_user_id.strip()
    source_uid = source_uid.strip()
    if not source_user_id:
        raise ValueError("Orphaned historical evidence requires a terminal user ID.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", source_user_id):
        raise ValueError(
            "Historical terminal user ID contains unsupported characters."
        )
    if not _service_number_matches(source_user_id, directory_service_number):
        raise ValueError(
            "Terminal user ID does not exactly match the authoritative HR service number."
        )

    normalized_cnic = normalize_cnic(source_cnic)
    normalized_directory_name = _normalized_identity_name(directory_employee_name)
    if len(normalized_directory_name) < 5:
        raise ValueError("Authoritative HR employee name is too short to verify.")

    uid_condition = (
        AttendanceEvent.uid == source_uid
        if source_uid
        else ((AttendanceEvent.uid == None) | (AttendanceEvent.uid == ""))  # noqa: E711
    )
    events = list(
        session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.user_id == source_user_id,
                uid_condition,
                AttendanceEvent.device_user_id == None,  # noqa: E711
                AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
                AttendanceEvent.ords_status.in_(
                    UNRESOLVED_HISTORICAL_IDENTITY_STATES
                ),
            )
            .order_by(AttendanceEvent.id.asc())
            .with_for_update()
        ).all()
    )
    if not events:
        existing = session.scalar(
            select(IdentityTombstone)
            .where(
                IdentityTombstone.zkt_device_id == zkt.id,
                IdentityTombstone.user_id == source_user_id,
                IdentityTombstone.uid == source_uid,
                IdentityTombstone.cnic_lookup_hash == cnic_lookup(normalized_cnic),
            )
            .order_by(IdentityTombstone.id.desc())
        )
        if existing is not None:
            return existing, 0
        raise ValueError("No unresolved exact historical event cohort remains.")
    if not secrets.compare_digest(
        _historical_identity_group_token(zkt, events),
        group_token,
    ):
        raise ValueError(
            "Historical event cohort changed since it was selected. Refresh and retry."
        )

    event_names = {
        normalized
        for normalized in (
            _normalized_identity_name(event.display_name or "") for event in events
        )
        if normalized
    }
    if len(event_names) > 1:
        raise ValueError(
            "Historical cohort contains multiple terminal names and remains fail-closed."
        )
    if not source_uid and not event_names:
        raise ValueError(
            "A legacy cohort without a UID also requires one stable terminal name."
        )
    if event_names:
        terminal_name = next(iter(event_names))
        if not (
            secrets.compare_digest(terminal_name, normalized_directory_name)
            or terminal_name.startswith(normalized_directory_name)
            or normalized_directory_name.startswith(terminal_name)
        ):
            raise ValueError(
                "Terminal name does not match the authoritative HR employee name."
            )

    current_identity = session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.lifecycle_state == "ACTIVE",
            (
                (DeviceUser.user_id == source_user_id)
                | (
                    (DeviceUser.uid == source_uid)
                    if source_uid
                    else (DeviceUser.id == -1)
                )
            ),
        )
    )
    if current_identity is not None:
        raise ValueError(
            "A current terminal user claims this user ID or UID. Enrich or resolve "
            "that live identity instead of creating historical evidence."
        )

    cnic_hash = cnic_lookup(normalized_cnic)
    conflicting_claim = session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.cnic_lookup_hash == cnic_hash,
            DeviceUser.lifecycle_state == "ACTIVE",
        )
    )
    if conflicting_claim is not None:
        raise ValueError(
            "That CNIC is already active on this terminal. Resolve the exact identity "
            "relationship before applying historical evidence."
        )

    encrypted_cnic = encrypt_cnic(normalized_cnic)
    source = DeviceUser(
        zkt_device_id=zkt.id,
        uid=source_uid,
        user_id=source_user_id,
        machine_name_encrypted=None,
        terminal_identity_fingerprint=None,
        terminal_state_fingerprint=None,
        display_name=directory_employee_name.strip(),
        cnic_encrypted=encrypted_cnic,
        cnic_lookup_hash=cnic_hash,
        cnic_last4=normalized_cnic[-4:],
        identity_conflict_code=None,
        shift_worker=any(event.raw_punch for event in events),
        privilege=0,
        card=None,
        present=False,
        lifecycle_state="DELETED",
        source="HR_DIRECTORY_EVIDENCE",
        deleted_at=max(event.device_event_time for event in events),
        deleted_by=actor,
        row_version=1,
        observed_at=min(event.device_event_time for event in events),
    )
    session.add(source)
    session.flush()

    tombstone = IdentityTombstone(
        zkt_device_id=zkt.id,
        device_user_id=source.id,
        device_serial=zkt.serial,
        uid=source_uid,
        user_id=source_user_id,
        display_name_encrypted=encrypt_text(directory_employee_name.strip()) or "",
        cnic_encrypted=encrypted_cnic,
        cnic_lookup_hash=cnic_hash,
        cnic_last4=normalized_cnic[-4:],
        shift_worker=source.shift_worker,
        privilege=source.privilege,
    )
    session.add(tombstone)
    session.flush()

    for event in events:
        event.device_user_id = source.id
        event.identity_snapshot_id = zkt.identity_snapshot_id
        event.identity_terminal_fingerprint = None
        event.identity_resolution_status = "RESOLVED_DIRECTORY_EVENT_GROUP"
        event.identity_resolved_at = utc_now()
        event.identity_repaired_at = utc_now()
        event.identity_repair_reason = "VERIFIED_HR_DIRECTORY_EVENT_GROUP"
        event.display_name = directory_employee_name.strip()
        event.cnic_encrypted = encrypted_cnic
        event.cnic_lookup_hash = cnic_hash
        event.cnic_last4 = normalized_cnic[-4:]
        event.raw_punch = event.raw_punch or source.shift_worker
        event.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=event.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None

    append_audit(
        session,
        actor=actor,
        action="HISTORICAL_EVENT_GROUP_IDENTITY_VERIFIED",
        target_type="zkt_historical_event_group",
        target_id=f"{zkt.id}:{source_user_id}:{source_uid}",
        outcome="SUCCEEDED",
        before={
            "group_token": group_token,
            "source_user_id": source_user_id,
            "source_uid": source_uid,
            "blocked_events": len(events),
        },
        after={
            "directory_employee_id": directory_employee_id,
            "directory_service_number": directory_service_number,
            "directory_employee_name": directory_employee_name.strip(),
            "directory_zone_code": directory_zone_code,
            "directory_cnic": mask_cnic(normalized_cnic),
            "repaired_events": len(events),
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
    )
    return tombstone, len(events)


def resolve_historical_event_group_to_current_identity(
    session: Session,
    *,
    connector: Connector,
    group_token: str,
    source_user_id: str,
    source_uid: str,
    target_user_key: str,
    expected_version: int,
    source_cnic: str,
    verified_employee_name: str,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> tuple[HistoricalCurrentIdentityResolution, DeviceUser, int]:
    """Attach one exact legacy cohort to its current, independently verified user.

    This is deliberately narrower than a historical alias. The terminal user ID
    must be unchanged, a non-empty historical UID must match, the active identity
    must already own the same verified CNIC, the terminal and evidence names must
    agree, and the reviewed cohort token and active row version must still be
    current. No terminal user or attendance event is deleted.
    """

    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    source_user_id = source_user_id.strip()
    source_uid = source_uid.strip()
    verified_employee_name = verified_employee_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", source_user_id):
        raise ValueError(
            "Historical terminal user ID contains unsupported characters."
        )
    if len(_normalized_identity_name(verified_employee_name)) < 5:
        raise ValueError("Authoritative employee name is too short to verify.")

    target_user = session.scalar(
        select(DeviceUser)
        .where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == target_user_key,
        )
        .with_for_update()
    )
    if target_user is None:
        raise ValueError("The selected current terminal user no longer exists.")
    if (
        not target_user.present
        or target_user.lifecycle_state != "ACTIVE"
        or target_user.user_id != source_user_id
    ):
        raise ValueError(
            "The selected target is not the active user with this exact terminal user ID."
        )
    if target_user.row_version != expected_version:
        raise ValueError(
            "Current terminal identity changed since it was selected. Refresh and retry."
        )
    if source_uid and target_user.uid != source_uid:
        raise ValueError(
            "Historical UID does not match the selected current terminal identity."
        )
    if target_user.identity_conflict_code is not None:
        raise ValueError(
            "Resolve the current terminal user's identity conflict before using it."
        )

    normalized_cnic = normalize_cnic(source_cnic)
    cnic_hash = cnic_lookup(normalized_cnic)
    current_cnic = decrypt_cnic(target_user.cnic_encrypted)
    if (
        not current_cnic
        or not target_user.cnic_lookup_hash
        or not secrets.compare_digest(current_cnic, normalized_cnic)
        or not secrets.compare_digest(target_user.cnic_lookup_hash, cnic_hash)
    ):
        raise ValueError(
            "Authoritative CNIC evidence does not match the selected current identity."
        )
    if not _identity_names_match(verified_employee_name, target_user.display_name):
        raise ValueError(
            "Authoritative employee name does not match the selected current identity."
        )
    conflicting_claim = session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.id != target_user.id,
            DeviceUser.lifecycle_state == "ACTIVE",
            DeviceUser.cnic_lookup_hash == cnic_hash,
        )
    )
    if conflicting_claim is not None:
        raise ValueError(
            "Another active terminal user claims this CNIC. Resolve that conflict first."
        )

    def validate_existing(
        existing: HistoricalCurrentIdentityResolution,
    ) -> tuple[HistoricalCurrentIdentityResolution, DeviceUser, int]:
        if (
            existing.group_token != group_token
            or existing.device_user_id != target_user.id
            or existing.source_user_id != source_user_id
            or existing.source_uid != source_uid
            or not secrets.compare_digest(
                existing.source_cnic_lookup_hash,
                cnic_hash,
            )
        ):
            raise ValueError(
                "That resolution key or cohort is already bound to different evidence."
            )
        return existing, target_user, 0

    existing = session.scalar(
        select(HistoricalCurrentIdentityResolution).where(
            HistoricalCurrentIdentityResolution.zkt_device_id == zkt.id,
            HistoricalCurrentIdentityResolution.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return validate_existing(existing)
    existing = session.scalar(
        select(HistoricalCurrentIdentityResolution).where(
            HistoricalCurrentIdentityResolution.zkt_device_id == zkt.id,
            HistoricalCurrentIdentityResolution.group_token == group_token,
        )
    )
    if existing is not None:
        return validate_existing(existing)

    uid_condition = (
        AttendanceEvent.uid == source_uid
        if source_uid
        else ((AttendanceEvent.uid == None) | (AttendanceEvent.uid == ""))  # noqa: E711
    )
    events = list(
        session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.user_id == source_user_id,
                uid_condition,
                AttendanceEvent.device_user_id == None,  # noqa: E711
                AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
                AttendanceEvent.ords_status.in_(
                    UNRESOLVED_HISTORICAL_IDENTITY_STATES
                ),
            )
            .order_by(AttendanceEvent.id.asc())
            .with_for_update()
        ).all()
    )
    if not events:
        raise ValueError("No unresolved exact historical event cohort remains.")
    if not secrets.compare_digest(
        _historical_identity_group_token(zkt, events),
        group_token,
    ):
        raise ValueError(
            "Historical event cohort changed since it was selected. Refresh and retry."
        )

    event_names = {
        (event.display_name or "").strip()
        for event in events
        if (event.display_name or "").strip()
    }
    normalized_event_names = {
        _normalized_identity_name(name) for name in event_names
    }
    if len(normalized_event_names) != 1:
        raise ValueError(
            "Current-identity resolution requires one stable historical terminal name."
        )
    terminal_name = next(iter(event_names))
    if (
        not _identity_names_match(terminal_name, verified_employee_name)
        or not _identity_names_match(terminal_name, target_user.display_name)
    ):
        raise ValueError(
            "Historical terminal name does not match the authoritative current identity."
        )

    resolution = HistoricalCurrentIdentityResolution(
        zkt_device_id=zkt.id,
        device_user_id=target_user.id,
        group_token=group_token,
        source_user_id=source_user_id,
        source_uid=source_uid,
        source_cnic_lookup_hash=cnic_hash,
        verified_employee_name=verified_employee_name,
        event_count=len(events),
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(resolution)
            session.flush()
    except IntegrityError:
        concurrent = session.scalar(
            select(HistoricalCurrentIdentityResolution).where(
                HistoricalCurrentIdentityResolution.zkt_device_id == zkt.id,
                (
                    (
                        HistoricalCurrentIdentityResolution.idempotency_key
                        == idempotency_key
                    )
                    | (HistoricalCurrentIdentityResolution.group_token == group_token)
                ),
            )
        )
        if concurrent is None:
            raise ValueError(
                "The exact historical resolution changed concurrently. Refresh and retry."
            )
        return validate_existing(concurrent)

    repaired_at = utc_now()
    for event in events:
        event.device_user_id = target_user.id
        event.identity_snapshot_id = zkt.identity_snapshot_id
        event.identity_terminal_fingerprint = None
        event.identity_resolution_status = "RESOLVED_CURRENT_IDENTITY_EVIDENCE"
        event.identity_resolved_at = repaired_at
        event.identity_repaired_at = repaired_at
        event.identity_repair_reason = "VERIFIED_CURRENT_IDENTITY_EVENT_GROUP"
        event.display_name = target_user.display_name
        event.cnic_encrypted = target_user.cnic_encrypted
        event.cnic_lookup_hash = target_user.cnic_lookup_hash
        event.cnic_last4 = target_user.cnic_last4
        event.raw_punch = event.raw_punch or target_user.shift_worker
        event.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=event.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None

    append_audit(
        session,
        actor=actor,
        action="HISTORICAL_CURRENT_IDENTITY_VERIFIED",
        target_type="zkt_historical_event_group",
        target_id=f"{zkt.id}:{source_user_id}:{source_uid}",
        outcome="SUCCEEDED",
        request_id=idempotency_key,
        before={
            "group_token": group_token,
            "source_user_id": source_user_id,
            "source_uid": source_uid,
            "blocked_events": len(events),
        },
        after={
            "resolution_id": resolution.resolution_id,
            "target_user_key": target_user.user_key,
            "target_user_id": target_user.user_id,
            "target_row_version": target_user.row_version,
            "verified_employee_name": verified_employee_name,
            "verified_cnic": mask_cnic(normalized_cnic),
            "repaired_events": len(events),
            "reason": reason,
        },
    )
    return resolution, target_user, len(events)


def build_historical_identity_report(
    session: Session,
    *,
    zkt: ZKTDevice,
) -> dict:
    """Describe unresolved history without exposing or mutating identity data.

    Rows are attributed to a deleted terminal user only when the preserved
    device-user foreign key matches, or when one unique deleted user has the
    exact same terminal user ID and a compatible UID. Ambiguous rows stay in
    the unassigned count and therefore remain fail-closed.
    """

    deleted_users = list(
        session.scalars(
            select(DeviceUser)
            .where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.present == False,  # noqa: E712
                DeviceUser.lifecycle_state == "DELETED",
            )
            .order_by(DeviceUser.id.asc())
        ).all()
    )
    active_users = list(
        session.scalars(
            select(DeviceUser)
            .where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.lifecycle_state == "ACTIVE",
            )
            .order_by(DeviceUser.id.asc())
        ).all()
    )
    active_users_by_user_id: dict[str, list[DeviceUser]] = {}
    for active_user in active_users:
        active_users_by_user_id.setdefault(active_user.user_id, []).append(active_user)
    unresolved_events = list(
        session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
                AttendanceEvent.ords_status.in_(
                    UNRESOLVED_HISTORICAL_IDENTITY_STATES
                ),
            )
            .order_by(AttendanceEvent.id.asc())
        ).all()
    )

    users_by_id = {row.id: row for row in deleted_users if row.id is not None}
    users_by_user_id: dict[str, list[DeviceUser]] = {}
    for row in deleted_users:
        users_by_user_id.setdefault(row.user_id, []).append(row)

    attributed: dict[int, list[AttendanceEvent]] = {}
    unassigned_events: list[AttendanceEvent] = []
    for event in unresolved_events:
        source = users_by_id.get(event.device_user_id)
        if source is None:
            candidates = [
                candidate
                for candidate in users_by_user_id.get(event.user_id, [])
                if not event.uid or not candidate.uid or event.uid == candidate.uid
            ]
            if len(candidates) == 1:
                source = candidates[0]
        if source is None or source.id is None:
            unassigned_events.append(event)
            continue
        attributed.setdefault(source.id, []).append(event)

    rows = []
    for source in deleted_users:
        events = attributed.get(source.id or -1, [])
        if not events:
            continue
        blocked_count = sum(
            event.ords_status == "BLOCKED_IDENTITY" for event in events
        )
        quarantined_count = sum(
            event.ords_status == "QUARANTINED_IDENTITY_REUSE"
            for event in events
        )
        event_times = [event.device_event_time for event in events]
        if quarantined_count:
            resolution_path = "IDENTITY_REUSE_REVIEW"
        elif source.cnic_lookup_hash:
            resolution_path = "VERIFIED_TOMBSTONE_REPAIR"
        elif source.identity_conflict_code:
            resolution_path = "IDENTITY_CONFLICT_REVIEW"
        else:
            resolution_path = "HR_DIRECTORY_EVIDENCE"
        rows.append(
            {
                "source_user_key": source.user_key,
                "source_kind": "DELETED_USER",
                "uid": source.uid,
                "user_id": source.user_id,
                "display_name": source.display_name,
                "row_version": source.row_version,
                "observed_at": source.observed_at,
                "deleted_at": source.deleted_at,
                "cnic_available": bool(source.cnic_lookup_hash),
                "identity_conflict_code": source.identity_conflict_code,
                "event_count": len(events),
                "blocked_count": blocked_count,
                "quarantined_count": quarantined_count,
                "first_event_at": min(event_times) if event_times else None,
                "last_event_at": max(event_times) if event_times else None,
                "resolution_path": resolution_path,
                "operator_actionable": resolution_path == "HR_DIRECTORY_EVIDENCE",
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["event_count"]),
            str(row["user_id"]),
            str(row["source_user_key"]),
        )
    )
    unassigned_groups: list[dict] = []
    grouped_unassigned: dict[tuple[str, str], list[AttendanceEvent]] = {}
    for event in unassigned_events:
        grouped_unassigned.setdefault((event.user_id, event.uid or ""), []).append(event)
    for (user_id, uid), events in grouped_unassigned.items():
        display_names: dict[str, str] = {}
        for event in events:
            display_name = (event.display_name or "").strip()
            normalized_display_name = _normalized_identity_name(display_name)
            if normalized_display_name:
                display_names.setdefault(normalized_display_name, display_name)
        linked_user_ids = {
            event.device_user_id for event in events if event.device_user_id is not None
        }
        linked_user = (
            session.get(DeviceUser, next(iter(linked_user_ids)))
            if len(linked_user_ids) == 1
            else None
        )
        active_enrichment = bool(
            linked_user
            and linked_user.present
            and linked_user.lifecycle_state == "ACTIVE"
            and linked_user.user_id == user_id
            and linked_user.cnic_lookup_hash is None
            and linked_user.identity_conflict_code is None
        )
        current_identity_candidates = [
            candidate
            for candidate in active_users_by_user_id.get(user_id, [])
            if candidate.cnic_lookup_hash
            and candidate.cnic_encrypted
            and candidate.identity_conflict_code is None
            and (not uid or candidate.uid == uid)
            and len(display_names) == 1
            and _identity_names_match(
                next(iter(display_names.values())),
                candidate.display_name,
            )
        ]
        current_identity = (
            current_identity_candidates[0]
            if not linked_user_ids and len(current_identity_candidates) == 1
            else None
        )
        exact_name = len(display_names) == 1
        service_number_shape = bool(re.fullmatch(r"[A-Za-z0-9._-]+", user_id))
        exact_orphan = (
            not linked_user_ids
            and len(display_names) <= 1
            and service_number_shape
            and (bool(uid) or exact_name)
        )
        event_times = [event.device_event_time for event in events]
        unassigned_groups.append(
            {
                "group_token": _historical_identity_group_token(zkt, events),
                "source_user_key": None,
                "source_kind": "EVENT_GROUP",
                "active_user_key": (
                    linked_user.user_key
                    if active_enrichment and linked_user
                    else (
                        current_identity.user_key
                        if current_identity is not None
                        else None
                    )
                ),
                "active_user_row_version": (
                    current_identity.row_version
                    if current_identity is not None
                    else (
                        linked_user.row_version
                        if active_enrichment and linked_user
                        else None
                    )
                ),
                "uid": uid,
                "user_id": user_id,
                "display_name": (
                    next(iter(display_names.values()))
                    if len(display_names) == 1
                    else (
                        "Unknown historical user"
                        if not display_names
                        else "Multiple terminal names"
                    )
                ),
                "row_version": None,
                "observed_at": min(event_times) if event_times else None,
                "deleted_at": max(event_times) if event_times else None,
                "cnic_available": False,
                "identity_conflict_code": None,
                "event_count": len(events),
                "blocked_count": sum(
                    event.ords_status == "BLOCKED_IDENTITY" for event in events
                ),
                "quarantined_count": sum(
                    event.ords_status == "QUARANTINED_IDENTITY_REUSE"
                    for event in events
                ),
                "first_event_at": min(event_times) if event_times else None,
                "last_event_at": max(event_times) if event_times else None,
                "resolution_path": (
                    "ACTIVE_USER_ENRICHMENT"
                    if active_enrichment
                    else (
                        "CURRENT_IDENTITY_EVIDENCE"
                        if current_identity is not None
                        else (
                            "HR_DIRECTORY_EVENT_GROUP"
                            if exact_orphan
                            else "IDENTITY_REUSE_REVIEW"
                        )
                    )
                ),
                "operator_actionable": (
                    exact_orphan
                    or active_enrichment
                    or current_identity is not None
                ),
            }
        )
    unassigned_groups.sort(
        key=lambda row: (
            -int(row["event_count"]),
            str(row["user_id"]),
            str(row["uid"]),
        )
    )

    return {
        "device_serial": zkt.serial,
        "snapshot_revision": zkt.identity_snapshot_revision,
        "totals": {
            "unresolved_events": len(unresolved_events),
            "blocked_identity": sum(
                event.ords_status == "BLOCKED_IDENTITY"
                for event in unresolved_events
            ),
            "quarantined_identity_reuse": sum(
                event.ords_status == "QUARANTINED_IDENTITY_REUSE"
                for event in unresolved_events
            ),
            "attributed_to_deleted_users": sum(
                len(events) for events in attributed.values()
            ),
            "unassigned_events": len(unassigned_events),
            "actionable_event_groups": sum(
                bool(group["operator_actionable"]) for group in unassigned_groups
            ),
            "candidate_users": len(rows),
        },
        "rows": rows,
        "unassigned_groups": unassigned_groups,
    }


def enrich_undelivered_attendance(
    session: Session,
    *,
    zkt: ZKTDevice,
    user: DeviceUser,
    snapshot: DeviceUserSnapshot | None = None,
) -> int:
    cnic = decrypt_cnic(user.cnic_encrypted)
    if not cnic:
        return 0
    eligible_statuses = {
        "BLOCKED_IDENTITY",
        "PENDING",
        "FAILED_RETRYABLE",
        "RETRYING",  # Compatibility with rows written before the durable outbox rename.
    }
    rows = session.scalars(
        select(AttendanceEvent).where(
            AttendanceEvent.zkt_device_id == zkt.id,
            AttendanceEvent.user_id == user.user_id,
            AttendanceEvent.ords_status.in_(eligible_statuses),
        )
    ).all()
    changed = 0
    for row in rows:
        if row.ords_status not in eligible_statuses:
            continue
        identity_reused = bool(
            (row.device_user_id is not None and row.device_user_id != user.id)
            or (
                row.device_user_id is None
                and row.uid
                and user.uid
                and row.uid != user.uid
            )
            or (
                row.identity_terminal_fingerprint
                and user.terminal_identity_fingerprint
                and row.identity_terminal_fingerprint != user.terminal_identity_fingerprint
            )
        )
        if identity_reused:
            row.ords_status = "QUARANTINED_IDENTITY_REUSE"
            row.identity_resolution_status = "QUARANTINED_REUSE"
            outbox = session.scalar(
                select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
            )
            if outbox is not None:
                outbox.status = "QUARANTINED_IDENTITY_REUSE"
                outbox.next_attempt_at = None
            continue
        row.device_user_id = user.id
        row.identity_snapshot_id = snapshot.id if snapshot else zkt.identity_snapshot_id
        row.identity_terminal_fingerprint = user.terminal_identity_fingerprint
        row.identity_resolution_status = "RESOLVED"
        row.identity_resolved_at = utc_now()
        row.identity_repaired_at = utc_now()
        row.identity_repair_reason = "VERIFIED_TERMINAL_SNAPSHOT"
        row.display_name = user.display_name
        row.cnic_encrypted = user.cnic_encrypted
        row.cnic_lookup_hash = user.cnic_lookup_hash
        row.cnic_last4 = user.cnic_last4
        row.raw_punch = row.raw_punch or user.shift_worker
        row.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None
        changed += 1
    return changed


def repair_verified_tombstone_backlog(
    session: Session,
    *,
    limit: int = 500,
) -> int:
    """Requeue blocked punches when a preserved terminal identity proves the CNIC.

    A user can disappear from a stable terminal snapshot after punches for that
    identity were already stored as ``BLOCKED_IDENTITY``.  Deletion correctly
    preserves an encrypted identity tombstone, but older blocked rows predate
    that tombstone and therefore need a bounded, durable repair pass.

    The repair fails closed on identity reuse: a row is eligible only when its
    preserved device-user identity (or, for older rows, its terminal UID)
    matches the tombstone.  No attendance row is deleted or replaced.
    """

    bounded_limit = max(1, min(int(limit), 500))
    eligible_tombstone = (
        select(IdentityTombstone.id)
        .where(
            IdentityTombstone.zkt_device_id == AttendanceEvent.zkt_device_id,
            IdentityTombstone.user_id == AttendanceEvent.user_id,
            IdentityTombstone.cnic_encrypted.is_not(None),
            IdentityTombstone.cnic_lookup_hash.is_not(None),
            (
                (
                    AttendanceEvent.device_user_id.is_not(None)
                    & (
                        IdentityTombstone.device_user_id
                        == AttendanceEvent.device_user_id
                    )
                )
                | (
                    AttendanceEvent.device_user_id.is_(None)
                    & AttendanceEvent.uid.is_not(None)
                    & (AttendanceEvent.uid != "")
                    & (IdentityTombstone.uid == AttendanceEvent.uid)
                )
            ),
        )
        .exists()
    )
    blocked_rows = session.scalars(
        select(AttendanceEvent)
        .where(
            AttendanceEvent.ords_status == "BLOCKED_IDENTITY",
            AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
            eligible_tombstone,
        )
        .order_by(AttendanceEvent.id.asc())
        .limit(bounded_limit)
        .with_for_update(skip_locked=True)
    ).all()
    if not blocked_rows:
        return 0

    repaired = 0
    tombstone_cache: dict[tuple[int, str, int | None, str | None], IdentityTombstone | None] = {}
    for row in blocked_rows:
        cache_key = (
            row.zkt_device_id,
            row.user_id,
            row.device_user_id,
            row.uid,
        )
        if cache_key not in tombstone_cache:
            statement = (
                select(IdentityTombstone)
                .where(
                    IdentityTombstone.zkt_device_id == row.zkt_device_id,
                    IdentityTombstone.user_id == row.user_id,
                    IdentityTombstone.cnic_encrypted.is_not(None),
                    IdentityTombstone.cnic_lookup_hash.is_not(None),
                )
                .order_by(IdentityTombstone.id.desc())
            )
            if row.device_user_id is not None:
                statement = statement.where(
                    IdentityTombstone.device_user_id == row.device_user_id
                )
            elif row.uid:
                statement = statement.where(IdentityTombstone.uid == row.uid)
            else:
                # A user ID without a preserved device-user or UID can have
                # been reused on the same terminal. Operator evidence is
                # required before such a row can be attributed.
                tombstone_cache[cache_key] = None
                continue
            tombstone_cache[cache_key] = session.scalar(statement.limit(1))

        tombstone = tombstone_cache[cache_key]
        if tombstone is None:
            continue

        cnic = decrypt_cnic(tombstone.cnic_encrypted)
        if not cnic:
            continue

        row.device_user_id = tombstone.device_user_id
        row.identity_resolution_status = "RESOLVED_TOMBSTONE"
        row.identity_resolved_at = utc_now()
        row.identity_repaired_at = utc_now()
        row.identity_repair_reason = "VERIFIED_IDENTITY_TOMBSTONE"
        row.display_name = (
            decrypt_text(tombstone.display_name_encrypted) or row.display_name
        )
        row.cnic_encrypted = tombstone.cnic_encrypted
        row.cnic_lookup_hash = tombstone.cnic_lookup_hash
        row.cnic_last4 = tombstone.cnic_last4
        row.raw_punch = row.raw_punch or tombstone.shift_worker
        row.ords_status = "PENDING"
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
        )
        if outbox is None:
            session.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING"))
        else:
            outbox.status = "PENDING"
            outbox.next_attempt_at = None
            outbox.last_http_status = None
            outbox.last_error = None
        repaired += 1
    return repaired


def repair_verified_active_identity_backlog(
    session: Session,
    *,
    limit: int = 500,
) -> int:
    """Requeue recent missing-UID punches proven by a later stable snapshot.

    Some ZKT attendance records contain a user ID and name but no terminal UID.
    A user ID alone is never sufficient identity evidence because terminals may
    reuse it.  This bounded repair therefore requires the punch to be newer than
    the terminal's last identity change and covered by a stable, complete
    snapshot.  The exact current name must match, and any supplied UID or
    terminal fingerprint must also match.  Historical or ambiguous rows remain
    blocked for operator evidence.
    """

    bounded_limit = max(1, min(int(limit), 500))
    tolerance = timedelta(
        seconds=max(0, settings.identity_snapshot_capture_tolerance_seconds)
    )
    repaired = 0

    zkts = session.scalars(
        select(ZKTDevice).where(
            ZKTDevice.snapshot_complete == True,  # noqa: E712
            ZKTDevice.identity_snapshot_stable == True,  # noqa: E712
            ZKTDevice.identity_snapshot_id.is_not(None),
            ZKTDevice.identity_snapshot_observed_at.is_not(None),
            ZKTDevice.last_identity_change_at.is_not(None),
        )
    ).all()
    for zkt in zkts:
        if repaired >= bounded_limit:
            break
        assert zkt.identity_snapshot_observed_at is not None
        assert zkt.last_identity_change_at is not None
        observed_at = ensure_utc(zkt.identity_snapshot_observed_at)
        identity_change_at = ensure_utc(zkt.last_identity_change_at)
        valid_resolutions = valid_identity_resolutions(session, zkt=zkt)
        resolved_conflict_hashes = tuple(valid_resolutions)

        candidates = session.execute(
            select(AttendanceEvent, DeviceUser)
            .join(
                DeviceUser,
                (DeviceUser.zkt_device_id == AttendanceEvent.zkt_device_id)
                & (DeviceUser.user_id == AttendanceEvent.user_id),
            )
            .where(
                AttendanceEvent.zkt_device_id == zkt.id,
                AttendanceEvent.ords_status == "BLOCKED_IDENTITY",
                AttendanceEvent.cnic_lookup_hash == None,  # noqa: E711
                AttendanceEvent.device_event_time >= identity_change_at,
                DeviceUser.lifecycle_state == "ACTIVE",
                DeviceUser.present == True,  # noqa: E712
                DeviceUser.cnic_encrypted.is_not(None),
                DeviceUser.cnic_lookup_hash.is_not(None),
                # Make the database select only rows that can actually be
                # repaired. Previously, newer bad-name rows consumed the
                # bounded LIMIT forever and starved older valid punches.
                func.lower(func.trim(AttendanceEvent.display_name))
                == func.lower(func.trim(DeviceUser.display_name)),
                AttendanceEvent.captured_at <= observed_at + tolerance,
                or_(
                    AttendanceEvent.identity_terminal_fingerprint.is_(None),
                    DeviceUser.terminal_identity_fingerprint.is_(None),
                    AttendanceEvent.identity_terminal_fingerprint
                    == DeviceUser.terminal_identity_fingerprint,
                ),
                or_(
                    DeviceUser.identity_conflict_code.is_(None),
                    DeviceUser.cnic_lookup_hash.in_(resolved_conflict_hashes)
                    if resolved_conflict_hashes
                    else DeviceUser.identity_conflict_code.is_(None),
                ),
                (
                    AttendanceEvent.device_user_id.is_(None)
                    | (AttendanceEvent.device_user_id == DeviceUser.id)
                ),
                (
                    AttendanceEvent.uid.is_(None)
                    | (AttendanceEvent.uid == "")
                    | (AttendanceEvent.uid == DeviceUser.uid)
                ),
            )
            .order_by(AttendanceEvent.id.desc())
            .limit(bounded_limit - repaired)
            .with_for_update(skip_locked=True)
        ).all()

        for row, user in candidates:
            if ensure_utc(row.captured_at) > observed_at + tolerance:
                continue
            if not _identity_names_match(row.display_name or "", user.display_name or ""):
                continue
            if (
                row.identity_terminal_fingerprint
                and user.terminal_identity_fingerprint
                and not secrets.compare_digest(
                    row.identity_terminal_fingerprint,
                    user.terminal_identity_fingerprint,
                )
            ):
                continue

            identity_resolution = None
            if user.identity_conflict_code:
                identity_resolution = valid_resolutions.get(
                    user.cnic_lookup_hash or ""
                )
                if identity_resolution is None:
                    continue

            cnic = decrypt_cnic(user.cnic_encrypted)
            if not cnic:
                continue

            now = utc_now()
            row.device_user_id = user.id
            row.identity_resolution_id = (
                identity_resolution.id if identity_resolution is not None else None
            )
            row.identity_snapshot_id = zkt.identity_snapshot_id
            row.identity_terminal_fingerprint = user.terminal_identity_fingerprint
            row.identity_resolution_status = "RESOLVED_CURRENT_SNAPSHOT"
            row.identity_resolved_at = now
            row.identity_repaired_at = now
            row.identity_repair_reason = "VERIFIED_CURRENT_TERMINAL_SNAPSHOT"
            row.display_name = user.display_name
            row.cnic_encrypted = user.cnic_encrypted
            row.cnic_lookup_hash = user.cnic_lookup_hash
            row.cnic_last4 = user.cnic_last4
            row.raw_punch = row.raw_punch or user.shift_worker
            row.ords_status = "PENDING"
            outbox = session.scalar(
                select(OrdsOutbox).where(
                    OrdsOutbox.attendance_event_id == row.id
                )
            )
            if outbox is None:
                session.add(
                    OrdsOutbox(attendance_event_id=row.id, status="PENDING")
                )
            else:
                outbox.status = "PENDING"
                outbox.next_attempt_at = None
                outbox.last_http_status = None
                outbox.last_error = None
            repaired += 1
    return repaired


def block_undelivered_attendance(
    session: Session,
    *,
    zkt: ZKTDevice,
    user: DeviceUser,
    snapshot: DeviceUserSnapshot | None = None,
) -> int:
    eligible_statuses = {"BLOCKED_IDENTITY", "PENDING", "FAILED_RETRYABLE", "RETRYING"}
    rows = session.scalars(
        select(AttendanceEvent).where(
            AttendanceEvent.zkt_device_id == zkt.id,
            AttendanceEvent.user_id == user.user_id,
            AttendanceEvent.ords_status.in_(eligible_statuses),
        )
    ).all()
    changed = 0
    for row in rows:
        if row.device_user_id not in {None, user.id}:
            row.ords_status = "QUARANTINED_IDENTITY_REUSE"
            row.identity_resolution_status = "QUARANTINED_REUSE"
        else:
            row.device_user_id = user.id
            row.identity_snapshot_id = snapshot.id if snapshot else zkt.identity_snapshot_id
            row.identity_terminal_fingerprint = user.terminal_identity_fingerprint
            row.cnic_encrypted = None
            row.cnic_lookup_hash = None
            row.cnic_last4 = None
            row.ords_status = "BLOCKED_IDENTITY"
            row.identity_resolution_status = (
                "BLOCKED_CONFLICT" if user.identity_conflict_code else "BLOCKED_MALFORMED_IDENTITY"
            )
        outbox = session.scalar(
            select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id)
        )
        if outbox is not None:
            outbox.status = row.ords_status
            outbox.next_attempt_at = None
        changed += 1
    return changed


def ingest_attendance(
    session: Session, *, connector: Connector, events: list[AttendanceEventIn]
) -> tuple[list[str], list[str]]:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("Connector has no assigned ZKT device.")
    accepted: list[str] = []
    duplicates: list[str] = []
    event_uids = [incoming.event_uid for incoming in events]
    existing_uids = set(
        session.scalars(
            select(AttendanceEvent.event_uid).where(
                AttendanceEvent.event_uid.in_(event_uids)
            )
        ).all()
    )
    seen_uids = set(existing_uids)
    pending_events: list[AttendanceEventIn] = []
    for incoming in events:
        if incoming.event_uid in seen_uids:
            duplicates.append(incoming.event_uid)
            continue
        seen_uids.add(incoming.event_uid)
        pending_events.append(incoming)
    if not pending_events:
        return accepted, duplicates

    user_ids = {incoming.user_id for incoming in pending_events}
    users_by_id = {
        row.user_id: row
        for row in session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.user_id.in_(user_ids),
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all()
    }
    tombstones_by_id: dict[str, list[IdentityTombstone]] = {}
    if user_ids:
        for row in session.scalars(
            select(IdentityTombstone)
            .where(
                IdentityTombstone.zkt_device_id == zkt.id,
                IdentityTombstone.user_id.in_(user_ids),
            )
            .order_by(IdentityTombstone.id.desc())
        ).all():
            tombstones_by_id.setdefault(row.user_id, []).append(row)
    receipts_by_uid = {
        row.event_uid: row
        for row in session.scalars(
            select(OracleReceipt).where(
                OracleReceipt.event_uid.in_(
                    [incoming.event_uid for incoming in pending_events]
                )
            )
        ).all()
    }

    identity_resolution_cache: dict[str, IdentityConflictResolution | None] = {}
    pending_rows: list[
        tuple[AttendanceEvent, OracleReceipt | None, bool, bool, bool]
    ] = []
    for incoming in pending_events:
        plausible_device_time = attendance_device_time_is_plausible(
            incoming.device_event_time,
            incoming.captured_at,
        )
        parsed = parse_machine_name(incoming.raw_name)
        active_user = users_by_id.get(incoming.user_id)
        user = active_user
        # A terminal user ID is not an identity by itself: ZKT terminals can
        # reuse it after deletion.  Only the exact current (user ID, UID) pair
        # may inherit a live identity.  A mismatch is handled as historical
        # identity evidence, never silently attached to the current employee.
        if (
            user is not None
            and (
                not incoming.uid
                or not user.uid
                or not secrets.compare_digest(incoming.uid, user.uid)
                or (
                    incoming.terminal_identity_fingerprint
                    and user.terminal_identity_fingerprint
                    and not secrets.compare_digest(
                        incoming.terminal_identity_fingerprint,
                        user.terminal_identity_fingerprint,
                    )
                )
            )
        ):
            user = None
        tombstone = None
        if user is None and incoming.uid:
            matching_tombstones = [
                candidate
                for candidate in tombstones_by_id.get(incoming.user_id, [])
                if candidate.uid
                and secrets.compare_digest(candidate.uid, incoming.uid)
            ]
            if len(matching_tombstones) == 1:
                tombstone = matching_tombstones[0]
        identity_resolution = None
        if user and user.identity_conflict_code and user.cnic_lookup_hash:
            lookup = user.cnic_lookup_hash
            if lookup not in identity_resolution_cache:
                identity_resolution_cache[lookup] = valid_resolution_for_user(
                    session, zkt=zkt, user=user
                )
            identity_resolution = identity_resolution_cache[lookup]
        usable_user_identity = bool(
            user and (user.identity_conflict_code is None or identity_resolution is not None)
        )
        snapshot_verified = bool(
            zkt.identity_snapshot_stable
            and zkt.identity_snapshot_id
            and zkt.identity_snapshot_observed_at
            and ensure_utc(zkt.identity_snapshot_observed_at)
            >= ensure_utc(incoming.captured_at)
            - timedelta(seconds=settings.identity_snapshot_capture_tolerance_seconds)
        )
        if settings.identity_snapshot_gate_enabled and not snapshot_verified:
            usable_user_identity = False
        cnic = (
            decrypt_cnic(user.cnic_encrypted)
            if usable_user_identity and user
            else (parsed.cnic if user is None and active_user is None else None)
        )
        if settings.identity_snapshot_gate_enabled and not snapshot_verified:
            cnic = None
        display_name = user.display_name if user else parsed.display_name or incoming.raw_name
        cnic_encrypted = user.cnic_encrypted if usable_user_identity and user else encrypt_cnic(cnic)
        cnic_hash = user.cnic_lookup_hash if usable_user_identity and user else cnic_lookup(cnic)
        cnic_last4 = (
            user.cnic_last4
            if usable_user_identity and user
            else (cnic[-4:] if cnic else None)
        )
        shift_worker = user.shift_worker if user else parsed.shift_worker
        if not cnic and tombstone is not None:
            cnic_encrypted = tombstone.cnic_encrypted
            cnic_hash = tombstone.cnic_lookup_hash
            cnic_last4 = tombstone.cnic_last4
            cnic = decrypt_cnic(tombstone.cnic_encrypted)
            display_name = decrypt_text(tombstone.display_name_encrypted) or display_name
            shift_worker = tombstone.shift_worker
        receipt = receipts_by_uid.get(incoming.event_uid)
        receipt_matches_connector = bool(
            receipt is not None and receipt.connector_id == connector.id
        )
        if receipt is not None and not receipt_matches_connector:
            upsert_alert(
                session,
                connector,
                code="ORACLE_RECEIPT_CONNECTOR_MISMATCH",
                severity="HIGH",
                message=(
                    "A device reported Oracle acceptance for an event owned by another "
                    "connector; the receipt was not applied."
                ),
                details={"event_uid_prefix": incoming.event_uid[:12]},
            )
        if not plausible_device_time:
            upsert_alert(
                session,
                connector,
                code="ATTENDANCE_TIMESTAMP_QUARANTINED",
                severity="HIGH",
                message=(
                    "ADD preserved a terminal record with an impossible attendance "
                    "timestamp and excluded it from normal Oracle delivery."
                ),
                details={"event_uid_prefix": incoming.event_uid[:12]},
            )
        row = AttendanceEvent(
            event_uid=incoming.event_uid,
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            device_user_id=user.id if user else (tombstone.device_user_id if tombstone else None),
            identity_resolution_id=(
                identity_resolution.id if identity_resolution is not None else None
            ),
            identity_snapshot_id=zkt.identity_snapshot_id if snapshot_verified else None,
            identity_terminal_fingerprint=(
                (
                    incoming.terminal_identity_fingerprint
                    or user.terminal_identity_fingerprint
                )
                if usable_user_identity and user
                else incoming.terminal_identity_fingerprint
            ),
            identity_resolution_status=(
                "RESOLVED"
                if cnic
                else (
                    "WAITING_FOR_SNAPSHOT"
                    if settings.identity_snapshot_gate_enabled and not snapshot_verified
                    else "BLOCKED_IDENTITY"
                )
            ),
            identity_resolved_at=utc_now() if cnic else None,
            device_serial=zkt.serial,
            uid=incoming.uid,
            user_id=incoming.user_id,
            display_name=display_name,
            cnic_encrypted=cnic_encrypted,
            cnic_lookup_hash=cnic_hash,
            cnic_last4=cnic_last4,
            device_event_time=ensure_utc(incoming.device_event_time),
            captured_at=ensure_utc(incoming.captured_at),
            source=incoming.source,
            status=None if incoming.status is None else str(incoming.status),
            punch=None if incoming.punch is None else str(incoming.punch),
            raw_punch=incoming.raw_punch or shift_worker,
            clock_drift_seconds=incoming.clock_drift_seconds,
            clock_quality=(
                incoming.clock_quality if plausible_device_time else "INVALID"
            ),
            boot_id=incoming.boot_id,
            sequence=incoming.sequence,
            raw_event=sanitize_raw_event(incoming.raw_event),
            ords_status=(
                "QUARANTINED_INVALID_DEVICE_TIME"
                if not plausible_device_time
                else (
                    "FIRMWARE_RECEIPT_UNVERIFIED"
                    if receipt_matches_connector
                    else ("PENDING" if cnic else "BLOCKED_IDENTITY")
                )
            ),
            oracle_confirmed_at=None,
            oracle_confirmation_path=None,
        )
        session.add(row)
        pending_rows.append(
            (row, receipt, receipt_matches_connector, bool(cnic), plausible_device_time)
        )
        accepted.append(incoming.event_uid)

    # Assign all attendance IDs in one flush. The previous per-event
    # SELECT/flush loop made a 100-event firmware batch exceed its ACK timeout
    # and stalled the device's durable outbox.
    session.flush()
    for row, receipt, receipt_matches_connector, has_cnic, plausible_device_time in pending_rows:
        if not plausible_device_time:
            if receipt_matches_connector:
                assert receipt is not None
                receipt.attendance_event_id = row.id
                row.oracle_confirmed_at = receipt.oracle_observed_at
                row.oracle_confirmation_path = receipt.confirmation_path
            continue
        if receipt_matches_connector:
            assert receipt is not None
            receipt.attendance_event_id = row.id
            session.add(
                OrdsOutbox(
                    attendance_event_id=row.id,
                    delivery_type=(
                        "FULL_HISTORY"
                        if row.source == "FULL_HISTORY"
                        else (
                            "CURRENT_RECONCILE"
                            if row.source in {"CURRENT_RECONCILE", "DUMP_RECONNECT", "DUMP_STARTUP", "RECONCILE_15M"}
                            else "LIVE"
                        )
                    ),
                    status="FIRMWARE_RECEIPT_UNVERIFIED",
                )
            )
        elif has_cnic:
            session.add(
                OrdsOutbox(
                    attendance_event_id=row.id,
                    delivery_type=(
                        "FULL_HISTORY"
                        if row.source == "FULL_HISTORY"
                        else (
                            "CURRENT_RECONCILE"
                            if row.source in {"CURRENT_RECONCILE", "DUMP_RECONNECT", "DUMP_STARTUP", "RECONCILE_15M"}
                            else "LIVE"
                        )
                    ),
                    status="PENDING",
                )
            )
    # Persist the corresponding Oracle outbox rows in one second flush so the
    # whole firmware message is durable before its websocket acknowledgement.
    session.flush()
    return accepted, duplicates


def record_oracle_receipts(
    session: Session,
    *,
    connector: Connector,
    batch: OracleReceiptBatchRequest,
) -> tuple[int, int, int]:
    """Persist device-observed Oracle acknowledgements before replying to firmware.

    Receipts are independent of attendance arrival order.  A receipt that reaches
    ADD before its attendance batch is retained and applied by ``ingest_attendance``
    when that immutable event later arrives.
    """

    observed_at = ensure_utc(batch.oracle_observed_at)
    now = utc_now()
    applied = 0
    awaiting_event = 0
    rejected = 0
    event_uids = list(batch.event_uids)
    receipts_by_uid = {
        row.event_uid: row
        for row in session.scalars(
            select(OracleReceipt).where(OracleReceipt.event_uid.in_(event_uids))
        ).all()
    }
    events_by_uid = {
        row.event_uid: row
        for row in session.scalars(
            select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(event_uids))
        ).all()
    }
    owned_event_ids = [
        row.id for row in events_by_uid.values() if row.connector_id == connector.id
    ]
    outboxes_by_event_id = (
        {
            row.attendance_event_id: row
            for row in session.scalars(
                select(OrdsOutbox).where(
                    OrdsOutbox.attendance_event_id.in_(owned_event_ids)
                )
            ).all()
        }
        if owned_event_ids
        else {}
    )

    for event_uid in batch.event_uids:
        receipt = receipts_by_uid.get(event_uid)
        event = events_by_uid.get(event_uid)
        if (
            receipt is not None
            and receipt.connector_id != connector.id
        ) or (
            event is not None
            and event.connector_id != connector.id
        ):
            rejected += 1
            upsert_alert(
                session,
                connector,
                code="ORACLE_RECEIPT_CONNECTOR_MISMATCH",
                severity="HIGH",
                message=(
                    "A device reported Oracle acceptance for an event owned by another "
                    "connector; the receipt was not applied."
                ),
                details={"event_uid_prefix": event_uid[:12]},
            )
            continue
        if receipt is None:
            receipt = OracleReceipt(
                event_uid=event_uid,
                connector_id=connector.id,
                confirmation_path=batch.confirmation_path,
                oracle_observed_at=observed_at,
                first_received_at=now,
                last_received_at=now,
                observation_count=1,
            )
            session.add(receipt)
            receipts_by_uid[event_uid] = receipt
        else:
            receipt.last_received_at = now
            receipt.observation_count += 1
            receipt.oracle_observed_at = max(
                ensure_utc(receipt.oracle_observed_at), observed_at
            )
            if ORACLE_CONFIRMATION_PATH_PRIORITY[batch.confirmation_path] >= (
                ORACLE_CONFIRMATION_PATH_PRIORITY.get(receipt.confirmation_path, 0)
            ):
                receipt.confirmation_path = batch.confirmation_path

        if event is None:
            awaiting_event += 1
            continue

        receipt.attendance_event_id = event.id
        outbox = outboxes_by_event_id.get(event.id)
        if outbox is None:
            outbox = OrdsOutbox(attendance_event_id=event.id)
            session.add(outbox)
            outboxes_by_event_id[event.id] = outbox
        if outbox.status not in {
            "ACKED",
            "ACKED_CHECK",
            "MEMBERSHIP_REVERIFYING",
            "MEMBERSHIP_REVERIFY_RETRY",
        }:
            event.ords_status = "FIRMWARE_RECEIPT_UNVERIFIED"
            event.oracle_confirmed_at = None
            event.oracle_confirmation_path = None
            outbox.status = "FIRMWARE_RECEIPT_UNVERIFIED"
            outbox.acknowledged_at = None
            outbox.next_attempt_at = None
            outbox.last_error = None
        applied += 1

    # One flush makes the complete receipt batch durable before the websocket
    # acknowledgement while avoiding the previous per-event flush/query loop.
    session.flush()
    remaining_failure = session.scalar(
        select(OrdsOutbox.id)
        .join(AttendanceEvent, AttendanceEvent.id == OrdsOutbox.attendance_event_id)
        .where(
            AttendanceEvent.connector_id == connector.id,
            OrdsOutbox.status.in_(
                [
                    "PENDING",
                    "FAILED_RETRYABLE",
                    "IN_FLIGHT",
                    "ACKED_FIRMWARE",
                    "FIRMWARE_RECEIPT_UNVERIFIED",
                    "FIRMWARE_RECEIPT_VERIFYING",
                ]
            ),
        )
        .limit(1)
    )
    if remaining_failure is None:
        resolve_alert(session, connector, code="ORDS_DELIVERY_FAILED")
    if rejected == 0:
        resolve_alert(session, connector, code="ORACLE_RECEIPT_CONNECTOR_MISMATCH")
    return applied, awaiting_event, rejected


def sanitize_raw_event(value: dict) -> dict:
    return redact_context(value, extra_blocked={"name", "raw_name"})


def oracle_payload(connector: Connector, zkt: ZKTDevice, row: AttendanceEvent, cnic: str) -> dict:
    capture_type = row.source
    if capture_type == "RECONCILE_15M":
        capture_type = "DUMP_RECONNECT"
    elif capture_type not in ORACLE_ALLOWED_CAPTURE_TYPES:
        capture_type = "MANUAL_REPROCESS"
    if row.clock_quality == "OK":
        trust_status = (
            "TRUSTED_LIVE"
            if capture_type in {"LIVE", "LIVE_POLL"}
            else "BACKFILL_ACCEPTED_CLOCK_OK"
        )
    else:
        trust_status = "SUSPECT_DEVICE_TIME"
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
        "capturetype": capture_type,
        "trust_status": trust_status,
        "clockdiff": row.clock_drift_seconds,
    }


def ingest_logs(session: Session, *, connector: Connector, logs: list) -> int:
    from zk_add.reconciliation import apply_reconciliation_device_fault

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
        apply_reconciliation_device_fault(
            session,
            connector=connector,
            code=incoming.code,
        )
        accepted += 1
    return accepted


def redact_context(value: dict, *, extra_blocked: set[str] | None = None) -> dict:
    blocked = {
        "password",
        "token",
        "secret",
        "comm_key",
        "cnic",
        "authorization",
        "wifi_password",
        "ords_password",
        "zkt_comm_key",
        "bootstrap_secret",
        "device_token",
    } | (extra_blocked or set())

    def sensitive(key: str) -> bool:
        normalized = key.lower()
        return normalized in blocked or normalized.endswith(
            ("_password", "_secret", "_token", "_comm_key")
        )

    def redact(item: object, key: str = "") -> object:
        if sensitive(key):
            return "[REDACTED]"
        if isinstance(item, dict):
            return {str(child): redact(value, str(child)) for child, value in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return {str(key): redact(item, str(key)) for key, item in value.items()}


def redact_text(value: str) -> str:
    import re

    value = re.sub(r"\b\d{5}-?\d{7}-?\d\b", "[CNIC-REDACTED]", value)
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
    owning_user_deletion_job_id: int | None = None,
) -> DeviceCommand:
    existing = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing
    if (
        command_type in MUTATING_COMMANDS
        and connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    ):
        raise ValueError("This connector is quarantined because its ZKT serial is duplicated.")
    if command_type in MUTATING_COMMANDS:
        active_job = session.scalar(
            select(UserDeletionJob).where(
                UserDeletionJob.connector_id == connector.id,
                UserDeletionJob.status.in_(ACTIVE_USER_DELETION_JOB_STATES),
            )
        )
        if active_job and active_job.id != owning_user_deletion_job_id:
            raise ValueError(
                f"Device already has active user deletion job {active_job.job_id}."
            )
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
    zkt = connector.zkt_device
    if not connector.connected:
        initial_status = "WAITING_FOR_DEVICE"
    elif command_type in MUTATING_COMMANDS and zkt and (
        not zkt.online or zkt.connection_state in {"FLAPPING", "RETRY_WAIT", "OFFLINE"}
    ):
        initial_status = "WAITING_FOR_ZKT"
    else:
        initial_status = "QUEUED"
    command = DeviceCommand(
        command_id=str(uuid4()),
        connector_id=connector.id,
        command_type=command_type,
        payload_encrypted=encrypt_json(payload),
        expected_state_encrypted=encrypt_json(expected_state),
        desired_state_encrypted=encrypt_json(desired_state),
        payload_summary=command_payload_summary(payload),
        idempotency_key=idempotency_key,
        actor=actor,
        status=initial_status,
        expires_at=None
        if expires_in_seconds is None
        else now + timedelta(seconds=expires_in_seconds),
    )
    session.add(command)
    session.flush()
    session.add(DeviceCommandEvent(command_id=command.id, status=initial_status, details={}))
    append_audit(
        session,
        actor=actor,
        action=f"COMMAND_{command_type}_QUEUED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=initial_status,
        after={"command_id": command.command_id},
    )
    return command


def serialize_command(command: DeviceCommand) -> dict:
    if command.status == "CANCEL_REQUESTED":
        return {
            "schema_version": "2",
            "type": "command_cancel",
            "command_id": command.command_id,
        }
    return {
        "schema_version": "2",
        "type": "command",
        "command_id": command.command_id,
        "command_type": command.command_type,
        "payload": decrypt_json(command.payload_encrypted),
        "expected_state": decrypt_json(command.expected_state_encrypted),
        "desired_state": decrypt_json(command.desired_state_encrypted),
        "created_at": command.created_at.isoformat(),
        "expires_at": command.expires_at.isoformat() if command.expires_at else None,
        "expires_epoch": int(command.expires_at.timestamp()) if command.expires_at else 0,
    }


def command_payload_summary(payload: dict) -> dict:
    allowed = {"user_key", "uid", "user_id", "lease_id", "duration_seconds", "reason"}
    return {key: value for key, value in payload.items() if key in allowed}


def require_writable_user_profile(connector: Connector, capability: str) -> ZKTDevice:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("No assigned ZKT device.")
    if connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL":
        raise ValueError("This connector is quarantined because its ZKT serial is duplicated.")
    if zkt.certification_state != "CERTIFIED" or not zkt.capability_profile.get(
        capability, False
    ):
        reason = zkt.writes_disabled_reason or "DEVICE_NOT_WRITE_CERTIFIED"
        raise ValueError(f"This terminal is read-only ({reason}).")
    if not zkt.snapshot_complete:
        raise ValueError("A complete fresh user snapshot is required before user mutation.")
    return zkt


def ensure_no_active_user_operation(
    session: Session, *, zkt: ZKTDevice, user: DeviceUser | None = None
) -> None:
    lease_statement = select(TemporaryAdminLease).where(
        TemporaryAdminLease.zkt_device_id == zkt.id,
        TemporaryAdminLease.state.in_(
            ["REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]
        ),
    )
    if user is not None:
        lease_statement = lease_statement.where(TemporaryAdminLease.device_user_id == user.id)
    if session.scalar(lease_statement):
        raise ValueError("An enrollment administrator lease is active for this user/device.")


def lock_zkt_user_registry(session: Session, *, zkt: ZKTDevice) -> None:
    """Serialize identity reservations for one terminal inside the transaction.

    PostgreSQL honors ``FOR UPDATE`` while SQLite safely ignores it in unit
    tests.  This closes the race where two concurrent ADD requests could both
    pass the CNIC/UID checks before either pending user became visible.
    """

    session.scalar(
        select(ZKTDevice.id).where(ZKTDevice.id == zkt.id).with_for_update()
    )


def find_terminal_cnic_claims(
    session: Session,
    *,
    zkt: ZKTDevice,
    lookup: str | None,
    exclude_user_id: int | None = None,
) -> list[DeviceUser]:
    if lookup is None:
        return []
    statement = select(DeviceUser).where(
        DeviceUser.zkt_device_id == zkt.id,
        DeviceUser.cnic_lookup_hash == lookup,
        DeviceUser.lifecycle_state.in_(["ACTIVE", "PENDING"]),
    )
    if exclude_user_id is not None:
        statement = statement.where(DeviceUser.id != exclude_user_id)
    return list(
        session.scalars(
            statement.order_by(DeviceUser.lifecycle_state.asc(), DeviceUser.user_id.asc())
        ).all()
    )


def duplicate_cnic_message(claims: list[DeviceUser]) -> str:
    """Return an actionable, non-PII explanation of an exact CNIC match."""

    active = [row for row in claims if row.lifecycle_state == "ACTIVE"]
    pending = [row for row in claims if row.lifecycle_state == "PENDING"]

    def user_ids(rows: list[DeviceUser]) -> str:
        shown = ", ".join(row.user_id for row in rows[:5])
        remaining = len(rows) - 5
        return f"{shown} (+{remaining} more)" if remaining > 0 else shown

    if active:
        noun = "record" if len(active) == 1 else "records"
        message = (
            "The terminal currently encodes this exact CNIC on active user "
            f"{noun} {user_ids(active)}."
        )
        if pending:
            message += f" It is also reserved by pending user {user_ids(pending)}."
        return message + " Correct the listed terminal record before reusing the CNIC."
    return (
        f"This exact CNIC is reserved by pending terminal user {user_ids(pending)}. "
        "Wait for that operation to finish or cancel it before retrying."
    )


def terminal_fingerprint_preconditions(user: DeviceUser) -> dict[str, str]:
    """Return opaque raw-record checks when the connector supplied them.

    Old ZKT models can contain non-UTF-8 bytes in a user ID. Their JSON-safe
    representation is intentionally lossy, so using that representation as a
    terminal precondition would reject the correct UID. New Zone Lite firmware
    supplies keyed fingerprints over the exact raw identifier and state bytes.
    """

    identity = user.terminal_identity_fingerprint
    state = user.terminal_state_fingerprint
    if ("?" in user.user_id or "\ufffd" in user.user_id) and not (identity and state):
        raise ValueError(
            "This legacy terminal user ID contains malformed bytes. Refresh users with "
            "the current Zone Lite firmware before editing this record."
        )
    result: dict[str, str] = {}
    if identity:
        result["terminal_identity_fingerprint"] = identity
    if state:
        result["terminal_state_fingerprint"] = state
    return result


def apply_verified_terminal_fingerprints(user: DeviceUser, result: dict) -> None:
    mappings = {
        "verified_terminal_identity_fingerprint": "terminal_identity_fingerprint",
        "verified_terminal_state_fingerprint": "terminal_state_fingerprint",
    }
    for result_key, attribute in mappings.items():
        value = result.get(result_key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            setattr(user, attribute, value)


def allocate_device_identifiers(
    session: Session, *, zkt: ZKTDevice, user_id_override: str | None
) -> tuple[str, str]:
    rows = session.scalars(
        select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id)
    ).all()
    used_uids = {int(row.uid) for row in rows if row.uid.isdigit()}
    uid = max(used_uids, default=0) + 1
    if uid > 65535:
        raise ValueError("The terminal has no never-used 16-bit UID remaining.")
    used_user_ids = {row.user_id for row in rows}
    if user_id_override:
        if user_id_override in used_user_ids:
            raise ValueError("That employee/user ID has already been used on this terminal.")
        user_id = user_id_override
    else:
        numeric_ids = [int(value) for value in used_user_ids if value.isdigit()]
        user_id = str(max(numeric_ids, default=0) + 1)
        while user_id in used_user_ids:
            user_id = str(int(user_id) + 1)
    if zkt.user_count is not None and zkt.user_count >= 65535:
        raise ValueError("The terminal user capacity has been reached.")
    return str(uid), user_id


def find_user_by_key(session: Session, *, zkt: ZKTDevice, user_key: str) -> DeviceUser | None:
    return session.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == user_key,
        )
    )


def find_idempotent_user_command(
    session: Session,
    *,
    connector: Connector,
    idempotency_key: str,
    command_type: str,
) -> tuple[DeviceUser | None, DeviceCommand] | None:
    command = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.idempotency_key == idempotency_key,
        )
    )
    if command is None:
        return None
    if command.command_type != command_type:
        raise ValueError("That idempotency key was already used for another operation.")
    payload = decrypt_json(command.payload_encrypted)
    user = session.scalar(
        select(DeviceUser).where(DeviceUser.user_key == payload.get("user_key"))
    )
    return user, command


def create_device_user_command(
    session: Session,
    *,
    connector: Connector,
    display_name: str,
    cnic: str,
    shift_worker: bool,
    user_id_override: str | None,
    idempotency_key: str,
    actor: str,
) -> tuple[DeviceUser, DeviceCommand]:
    replay = find_idempotent_user_command(
        session,
        connector=connector,
        idempotency_key=idempotency_key,
        command_type="CREATE_USER",
    )
    if replay is not None:
        user, existing = replay
        if user is None:
            raise ValueError("The idempotent create command no longer references a user.")
        return user, existing
    zkt = require_writable_user_profile(connector, "create_user")
    lock_zkt_user_registry(session, zkt=zkt)
    ensure_no_active_user_operation(session, zkt=zkt)
    normalized_cnic = normalize_cnic(cnic)
    if normalized_cnic is None:
        raise ValueError("CNIC must contain exactly 13 digits.")
    lookup = cnic_lookup(normalized_cnic)
    claims = find_terminal_cnic_claims(session, zkt=zkt, lookup=lookup)
    if claims:
        raise ValueError(duplicate_cnic_message(claims))
    uid, user_id = allocate_device_identifiers(
        session, zkt=zkt, user_id_override=user_id_override
    )
    name_limit = int(zkt.capability_profile.get("name_bytes", 24))
    machine_name = build_machine_name(
        display_name=display_name,
        cnic=normalized_cnic,
        shift_worker=shift_worker,
        byte_limit=name_limit,
    )
    now = utc_now()
    user = DeviceUser(
        zkt_device_id=zkt.id,
        uid=uid,
        user_id=user_id,
        machine_name_encrypted=encrypt_text(machine_name),
        display_name=" ".join(display_name.strip().split()),
        cnic_encrypted=encrypt_cnic(normalized_cnic),
        cnic_lookup_hash=lookup,
        cnic_last4=normalized_cnic[-4:],
        shift_worker=shift_worker,
        privilege=0,
        present=False,
        lifecycle_state="PENDING",
        source="ADD_MANAGED",
        observed_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()
    audit = append_audit(
        session,
        actor=actor,
        action="DEVICE_USER_CREATE_REQUESTED",
        target_type="device_user",
        target_id=user.user_key,
        outcome="PENDING",
        after={
            "display_name": user.display_name,
            "cnic": mask_cnic(normalized_cnic),
            "shift_worker": shift_worker,
            "privilege": 0,
            "uid": uid,
            "user_id": user_id,
        },
    )
    session.flush()
    user.create_audit_id = audit.id
    command = create_command(
        session,
        connector=connector,
        command_type="CREATE_USER",
        payload={
            "user_key": user.user_key,
            "uid": uid,
            "user_id": user_id,
            "name": machine_name,
            "privilege": 0,
        },
        expected_state={
            "serial": zkt.serial,
            "uid_absent": uid,
            "user_id_absent": user_id,
            "user_count": zkt.user_count,
        },
        desired_state={
            "user_key": user.user_key,
            "display_name": user.display_name,
            "cnic": normalized_cnic,
            "shift_worker": shift_worker,
            "privilege": 0,
            "machine_name": machine_name,
        },
        idempotency_key=idempotency_key,
        actor=actor,
        expires_in_seconds=settings.user_command_retry_seconds,
    )
    user.current_command_id = command.id
    return user, command


def update_device_user_command(
    session: Session,
    *,
    connector: Connector,
    user: DeviceUser,
    display_name: str | None,
    cnic: str | None,
    shift_worker: bool | None,
    privilege: int | None,
    expected_version: int,
    idempotency_key: str,
    actor: str,
    reason: str | None = None,
) -> DeviceCommand:
    replay = find_idempotent_user_command(
        session,
        connector=connector,
        idempotency_key=idempotency_key,
        command_type="UPDATE_USER",
    )
    if replay is not None:
        existing_user, existing_command = replay
        if existing_user is None or existing_user.id != user.id:
            raise ValueError("That idempotency key belongs to another user.")
        return existing_command
    zkt = require_writable_user_profile(connector, "user_write")
    lock_zkt_user_registry(session, zkt=zkt)
    session.refresh(user)
    if user.lifecycle_state != "ACTIVE" or not user.present:
        raise ValueError("Device user is not active.")
    if user.row_version != expected_version:
        raise ValueError("User changed since it was loaded. Refresh and retry.")
    ensure_no_active_user_operation(session, zkt=zkt, user=user)
    current_cnic = decrypt_cnic(user.cnic_encrypted)
    identity_resolution = (
        valid_resolution_for_user(session, zkt=zkt, user=user)
        if user.identity_conflict_code
        else None
    )
    if user.identity_conflict_code and identity_resolution is None and cnic is None:
        raise ValueError("A replacement CNIC is required to resolve this identity conflict.")
    next_cnic = normalize_cnic(cnic) if cnic is not None else current_cnic
    if next_cnic is None:
        raise ValueError("CNIC cannot be cleared.")
    next_lookup = cnic_lookup(next_cnic)
    claims = find_terminal_cnic_claims(
        session,
        zkt=zkt,
        lookup=next_lookup,
        exclude_user_id=user.id,
    )
    approved_member_ids = set(
        identity_resolution.member_device_user_ids if identity_resolution is not None else []
    )
    claims_are_approved_aliases = bool(
        identity_resolution is not None
        and next_lookup == user.cnic_lookup_hash
        and all(claim.id in approved_member_ids for claim in claims)
    )
    if claims and not claims_are_approved_aliases:
        raise ValueError(duplicate_cnic_message(claims))
    next_display = " ".join((display_name or user.display_name).strip().split())
    next_shift = user.shift_worker if shift_worker is None else shift_worker
    next_privilege = user.privilege if privilege is None else privilege
    fingerprint_preconditions = terminal_fingerprint_preconditions(user)
    machine_name = build_machine_name(
        display_name=next_display,
        cnic=next_cnic,
        shift_worker=next_shift,
        byte_limit=int(zkt.capability_profile.get("name_bytes", 24)),
    )
    desired = {
        "user_key": user.user_key,
        "display_name": next_display,
        "cnic": next_cnic,
        "shift_worker": next_shift,
        "privilege": next_privilege,
        "machine_name": machine_name,
    }
    current_machine_name = decrypt_text(user.machine_name_encrypted) or ""
    audit = append_audit(
        session,
        actor=actor,
        action="DEVICE_USER_UPDATE_REQUESTED",
        target_type="device_user",
        target_id=user.user_key,
        outcome="PENDING",
        before={
            "display_name": user.display_name,
            "cnic": mask_cnic(current_cnic),
            "shift_worker": user.shift_worker,
            "privilege": user.privilege,
            "version": user.row_version,
        },
        after={
            "display_name": next_display,
            "cnic": mask_cnic(next_cnic),
            "shift_worker": next_shift,
            "privilege": next_privilege,
            **({"privilege_change_reason": reason} if reason else {}),
        },
    )
    session.flush()
    user.update_audit_id = audit.id
    command = create_command(
        session,
        connector=connector,
        command_type="UPDATE_USER",
        payload={
            "user_key": user.user_key,
            "uid": user.uid,
            "user_id": user.user_id,
            "name": machine_name,
            "privilege": next_privilege,
        },
        expected_state={
            "serial": zkt.serial,
            "uid": user.uid,
            "user_id": user.user_id,
            "row_version": user.row_version,
            "name": current_machine_name,
            "privilege": user.privilege,
            **fingerprint_preconditions,
        },
        desired_state=desired,
        idempotency_key=idempotency_key,
        actor=actor,
        expires_in_seconds=settings.user_command_retry_seconds,
    )
    user.current_command_id = command.id
    return command


def delete_device_user_command(
    session: Session,
    *,
    connector: Connector,
    user: DeviceUser,
    expected_version: int,
    typed_confirmation: str,
    idempotency_key: str,
    actor: str,
    owning_user_deletion_job_id: int | None = None,
    expires_in_seconds: int | None = None,
) -> DeviceCommand:
    replay = find_idempotent_user_command(
        session,
        connector=connector,
        idempotency_key=idempotency_key,
        command_type="DELETE_USER",
    )
    if replay is not None:
        existing_user, existing_command = replay
        if existing_user is None or existing_user.id != user.id:
            raise ValueError("That idempotency key belongs to another user.")
        return existing_command
    zkt = require_writable_user_profile(connector, "delete_user")
    lock_zkt_user_registry(session, zkt=zkt)
    session.refresh(user)
    if user.lifecycle_state != "ACTIVE" or not user.present:
        raise ValueError("Device user is not active.")
    if user.row_version != expected_version:
        raise ValueError("User changed since it was loaded. Refresh and retry.")
    if typed_confirmation.strip() not in {user.display_name, user.user_id}:
        raise ValueError("Typed confirmation must exactly match the user name or user ID.")
    if user.privilege == 14:
        raise ValueError("Demote this permanent administrator before deletion.")
    ensure_no_active_user_operation(session, zkt=zkt, user=user)
    fingerprint_preconditions = terminal_fingerprint_preconditions(user)
    persist_identity_tombstone(session, zkt=zkt, user=user)
    cnic = decrypt_cnic(user.cnic_encrypted)
    current_machine_name = decrypt_text(user.machine_name_encrypted) or ""
    audit = append_audit(
        session,
        actor=actor,
        action="DEVICE_USER_DELETE_REQUESTED",
        target_type="device_user",
        target_id=user.user_key,
        outcome="PENDING",
        before={
            "display_name": user.display_name,
            "cnic": mask_cnic(cnic),
            "uid": user.uid,
            "user_id": user.user_id,
            "version": user.row_version,
        },
        after={"present": False, "lifecycle_state": "DELETE_PENDING"},
    )
    session.flush()
    user.delete_audit_id = audit.id
    command = create_command(
        session,
        connector=connector,
        command_type="DELETE_USER",
        payload={
            "user_key": user.user_key,
            "uid": user.uid,
            "user_id": user.user_id,
            "tombstone": {
                "display_name": user.display_name,
                "cnic": cnic,
                "shift_worker": user.shift_worker,
            },
        },
        expected_state={
            "serial": zkt.serial,
            "uid": user.uid,
            "user_id": user.user_id,
            "row_version": user.row_version,
            "name": current_machine_name,
            "privilege": user.privilege,
            "attendance_count": zkt.attendance_count,
            **fingerprint_preconditions,
        },
        desired_state={"user_key": user.user_key, "present": False},
        idempotency_key=idempotency_key,
        actor=actor,
        expires_in_seconds=(
            settings.user_command_retry_seconds
            if expires_in_seconds is None
            else expires_in_seconds
        ),
        owning_user_deletion_job_id=owning_user_deletion_job_id,
    )
    user.current_command_id = command.id
    return command


def _bulk_delete_request_digest(*, targets: list[tuple[str, int]], reason: str) -> str:
    canonical = {
        "reason": reason.strip(),
        "targets": [
            {"user_key": user_key, "expected_version": expected_version}
            for user_key, expected_version in sorted(targets)
        ],
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def create_user_deletion_job(
    session: Session,
    *,
    connector: Connector,
    targets: list[tuple[str, int]],
    reason: str,
    typed_confirmation: str,
    idempotency_key: str,
    actor: str,
) -> UserDeletionJob:
    digest = _bulk_delete_request_digest(targets=targets, reason=reason)
    existing = session.scalar(
        select(UserDeletionJob).where(
            UserDeletionJob.connector_id == connector.id,
            UserDeletionJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_digest != digest:
            raise ValueError("That idempotency key belongs to a different deletion request.")
        return existing

    zkt = require_writable_user_profile(connector, "delete_user")
    lock_zkt_user_registry(session, zkt=zkt)
    ensure_no_active_user_operation(session, zkt=zkt)
    expected_confirmation = f"DELETE {len(targets)} USERS FROM {connector.device_id}"
    if typed_confirmation.strip() != expected_confirmation:
        raise ValueError(f'Typed confirmation must exactly match "{expected_confirmation}".')
    if session.scalar(
        select(UserDeletionJob).where(
            UserDeletionJob.connector_id == connector.id,
            UserDeletionJob.status.in_(ACTIVE_USER_DELETION_JOB_STATES),
        )
    ):
        raise ValueError("This device already has an active bulk user deletion job.")
    if session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.command_type.in_(MUTATING_COMMANDS),
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
        )
    ):
        raise ValueError("This device already has an active mutating command.")

    requested = {
        user_key: expected_version for user_key, expected_version in targets
    }
    users = list(
        session.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.user_key.in_(requested),
            )
        ).all()
    )
    if len(users) != len(requested):
        found = {user.user_key for user in users}
        missing = sorted(set(requested) - found)
        raise ValueError(f"Selected terminal users no longer exist: {', '.join(missing[:5])}.")
    users_by_key = {user.user_key: user for user in users}
    for user_key, expected_version in targets:
        user = users_by_key[user_key]
        if user.lifecycle_state != "ACTIVE" or not user.present:
            raise ValueError(f"User {user.user_id} is no longer active.")
        if user.row_version != expected_version:
            raise ValueError(f"User {user.user_id} changed since it was selected.")
        if user.privilege == 14:
            raise ValueError(
                f"User {user.user_id} is a permanent administrator and cannot be deleted."
            )
        if user.current_command_id is not None:
            raise ValueError(f"User {user.user_id} already has an active command.")
        terminal_fingerprint_preconditions(user)

    now = utc_now()
    job = UserDeletionJob(
        connector_id=connector.id,
        zkt_device_id=zkt.id,
        actor=actor,
        reason=reason.strip(),
        idempotency_key=idempotency_key,
        request_digest=digest,
        status="QUEUED",
        requested_count=len(targets),
        expires_at=now + timedelta(hours=24),
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    for user_key, expected_version in targets:
        user = users_by_key[user_key]
        session.add(
            UserDeletionItem(
                job_id=job.id,
                device_user_id=user.id,
                user_key=user.user_key,
                uid=user.uid,
                user_id=user.user_id,
                display_name_encrypted=encrypt_text(user.display_name) or "",
                expected_row_version=expected_version,
                expected_identity_fingerprint=user.terminal_identity_fingerprint,
                expected_state_fingerprint=user.terminal_state_fingerprint,
                status="PENDING",
                result={},
                created_at=now,
                updated_at=now,
            )
        )
    append_audit(
        session,
        actor=actor,
        action="BULK_USER_DELETE_REQUESTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="PENDING",
        after={
            "job_id": job.job_id,
            "requested_count": job.requested_count,
            "reason": job.reason,
        },
    )
    return job


def _refresh_user_deletion_job_counts(
    session: Session, job: UserDeletionJob
) -> list[UserDeletionItem]:
    items = list(
        session.scalars(
            select(UserDeletionItem)
            .where(UserDeletionItem.job_id == job.id)
            .order_by(UserDeletionItem.id.asc())
        ).all()
    )
    job.succeeded_count = sum(item.status == "SUCCEEDED" for item in items)
    job.failed_count = sum(item.status == "FAILED" for item in items)
    job.canceled_count = sum(item.status == "CANCELED" for item in items)
    job.expired_count = sum(item.status == "EXPIRED" for item in items)
    job.updated_at = utc_now()
    return items


def _finalize_user_deletion_job(
    session: Session, *, job: UserDeletionJob, connector: Connector
) -> None:
    items = _refresh_user_deletion_job_counts(session, job)
    if any(item.status not in TERMINAL_USER_DELETION_ITEM_STATES for item in items):
        return
    if job.completed_at is not None:
        return
    if job.succeeded_count == job.requested_count:
        status = "SUCCEEDED"
    elif job.succeeded_count:
        status = "PARTIAL"
    elif job.failed_count:
        status = "FAILED"
    elif job.expired_count:
        status = "EXPIRED"
    else:
        status = "CANCELED"
    now = utc_now()
    job.status = status
    job.completed_at = now
    job.updated_at = now
    summary = {
        "job_id": job.job_id,
        "requested": job.requested_count,
        "succeeded": job.succeeded_count,
        "failed": job.failed_count,
        "canceled": job.canceled_count,
        "expired": job.expired_count,
    }
    append_audit(
        session,
        actor=job.actor,
        action="BULK_USER_DELETE_COMPLETED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=status,
        after=summary,
    )
    if status in {"PARTIAL", "FAILED", "EXPIRED"}:
        upsert_alert(
            session,
            connector,
            code="USER_DELETION_JOB_INCOMPLETE",
            severity="HIGH",
            message="A bulk user deletion job did not delete every selected user.",
            details=summary,
        )


def advance_user_deletion_jobs(session: Session) -> None:
    now = utc_now()
    jobs = list(
        session.scalars(
            select(UserDeletionJob)
            .where(UserDeletionJob.status.in_(ACTIVE_USER_DELETION_JOB_STATES))
            .order_by(UserDeletionJob.created_at.asc())
        ).all()
    )
    for job in jobs:
        connector = session.get(Connector, job.connector_id)
        if connector is None:
            continue
        items = _refresh_user_deletion_job_counts(session, job)

        for item in items:
            if item.status != "RUNNING" or item.current_command_id is None:
                continue
            command = session.get(DeviceCommand, item.current_command_id)
            if command is None or command.status in ACTIVE_COMMAND_STATES:
                continue
            item.result = command.result or {}
            item.error_code = command.error_code
            item.error_message = command.error_message
            item.completed_at = command.completed_at or now
            item.updated_at = now
            if command.status == "SUCCEEDED":
                item.status = "SUCCEEDED"
            elif command.status in {"CANCELLED", "CANCELED"}:
                item.status = "CANCELED"
            elif command.status == "EXPIRED":
                item.status = "EXPIRED"
            else:
                item.status = "FAILED"

        items = _refresh_user_deletion_job_counts(session, job)
        if job.expires_at <= now:
            for item in items:
                if item.status == "PENDING":
                    item.status = "EXPIRED"
                    item.error_code = "JOB_EXPIRED"
                    item.error_message = "The deletion job exceeded its 24-hour safety window."
                    item.completed_at = now
                    item.updated_at = now
            _finalize_user_deletion_job(session, job=job, connector=connector)
            continue

        if job.status == "CANCEL_REQUESTED":
            for item in items:
                if item.status == "PENDING":
                    item.status = "CANCELED"
                    item.error_code = "JOB_CANCELED"
                    item.error_message = "Canceled before dispatch."
                    item.completed_at = now
                    item.updated_at = now
            _finalize_user_deletion_job(session, job=job, connector=connector)
            continue

        if any(item.status == "RUNNING" for item in items):
            continue
        pending = next((item for item in items if item.status == "PENDING"), None)
        if pending is None:
            _finalize_user_deletion_job(session, job=job, connector=connector)
            continue

        user = session.get(DeviceUser, pending.device_user_id)
        mismatch = None
        if user is None or user.user_key != pending.user_key:
            mismatch = "The selected user record no longer exists."
        elif user.lifecycle_state != "ACTIVE" or not user.present:
            mismatch = "The selected user is no longer active."
        elif user.uid != pending.uid or user.user_id != pending.user_id:
            mismatch = "The selected terminal identity changed."
        elif user.privilege == 14:
            mismatch = "The selected user is now a permanent administrator."
        elif (
            pending.expected_identity_fingerprint
            and user.terminal_identity_fingerprint
            != pending.expected_identity_fingerprint
        ):
            mismatch = "The selected terminal identity fingerprint changed."
        elif (
            pending.expected_state_fingerprint
            and user.terminal_state_fingerprint != pending.expected_state_fingerprint
        ):
            mismatch = "The selected terminal state fingerprint changed."
        if mismatch:
            pending.status = "FAILED"
            pending.error_code = "USER_PRECONDITION_CHANGED"
            pending.error_message = mismatch
            pending.completed_at = now
            pending.updated_at = now
            continue

        try:
            remaining_seconds = max(1, int((job.expires_at - now).total_seconds()))
            command = delete_device_user_command(
                session,
                connector=connector,
                user=user,
                expected_version=user.row_version,
                typed_confirmation=user.user_id,
                idempotency_key=f"bulk-delete:{job.job_id}:{pending.id}",
                actor=job.actor,
                owning_user_deletion_job_id=job.id,
                expires_in_seconds=remaining_seconds,
            )
        except ValueError as exc:
            pending.status = "FAILED"
            pending.error_code = "DELETE_DISPATCH_REJECTED"
            pending.error_message = str(exc)
            pending.completed_at = now
            pending.updated_at = now
            continue
        pending.status = "RUNNING"
        pending.current_command_id = command.id
        pending.updated_at = now
        job.status = "RUNNING"
        job.started_at = job.started_at or now
        job.updated_at = now


def cancel_user_deletion_job(
    session: Session, *, job: UserDeletionJob, actor: str
) -> UserDeletionJob:
    if job.status not in ACTIVE_USER_DELETION_JOB_STATES:
        return job
    job.status = "CANCEL_REQUESTED"
    job.updated_at = utc_now()
    append_audit(
        session,
        actor=actor,
        action="BULK_USER_DELETE_CANCEL_REQUESTED",
        target_type="user_deletion_job",
        target_id=job.job_id,
        outcome="PENDING",
    )
    return job


def serialize_user_deletion_job(session: Session, job: UserDeletionJob) -> dict:
    items = _refresh_user_deletion_job_counts(session, job)
    return {
        "job_id": job.job_id,
        "connector_id": session.get(Connector, job.connector_id).connector_id,
        "status": job.status,
        "reason": job.reason,
        "counts": {
            "requested": job.requested_count,
            "succeeded": job.succeeded_count,
            "failed": job.failed_count,
            "canceled": job.canceled_count,
            "expired": job.expired_count,
            "pending": sum(
                item.status not in TERMINAL_USER_DELETION_ITEM_STATES for item in items
            ),
        },
        "created_at": job.created_at,
        "expires_at": job.expires_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "items": [
            {
                "user_key": item.user_key,
                "uid": item.uid,
                "user_id": item.user_id,
                "display_name": decrypt_text(item.display_name_encrypted) or "",
                "status": item.status,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "result": item.result or {},
            }
            for item in items
        ],
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
        "ota_capable": connector.ota_capable,
        "ota_state": connector.ota_state,
        "ota_partition_layout": connector.ota_partition_layout,
        "ota_running_partition": connector.ota_running_partition,
        "ota_image_sha256": connector.ota_image_sha256,
        "ota_signing_key_id": connector.ota_signing_key_id,
        "onboarding_generation": connector.onboarding_generation,
        "last_onboarded_at": connector.last_onboarded_at,
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
            "certification_observations": zkt.certification_observations,
            "capabilities": zkt.capability_profile,
            "snapshot_complete": zkt.snapshot_complete,
            "identity_snapshot_revision": zkt.identity_snapshot_revision,
            "identity_snapshot_state_hash": zkt.identity_snapshot_state_hash,
            "identity_snapshot_observed_at": zkt.identity_snapshot_observed_at,
            "identity_snapshot_received_at": zkt.identity_snapshot_received_at,
            "identity_snapshot_stable": zkt.identity_snapshot_stable,
            "last_identity_change_at": zkt.last_identity_change_at,
            "writes_disabled_reason": zkt.writes_disabled_reason,
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
    row = next(
        (
            candidate
            for candidate in session.new
            if isinstance(candidate, DeviceAlert)
            and candidate.connector_id == connector.id
            and candidate.code == code
            and candidate.state == "OPEN"
        ),
        None,
    )
    if row is None:
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
        select(Connector.lifecycle_state, func.count(Connector.id)).group_by(
            Connector.lifecycle_state
        )
    ).all()
    counts = {state.lower(): count for state, count in rows}
    counts["total"] = sum(counts.values())
    counts["open_alerts"] = (
        session.scalar(select(func.count(DeviceAlert.id)).where(DeviceAlert.state == "OPEN")) or 0
    )
    counts["active_leases"] = (
        session.scalar(
            select(func.count(TemporaryAdminLease.id)).where(
                TemporaryAdminLease.state.in_(
                    ["REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]
                )
            )
        )
        or 0
    )
    return counts


def reconcile_admin_lease_command(
    session: Session,
    *,
    command: DeviceCommand,
    now: datetime | None = None,
) -> TemporaryAdminLease | None:
    """Project a terminal grant/revoke command onto its lease state.

    Lease state is intentionally durable and separate from command state.  All
    command completion paths (connector result, local cancellation, expiry, and
    the maintenance repair sweep) must therefore use the same projection.
    """

    observed_at = now or utc_now()
    lease: TemporaryAdminLease | None = None
    if command.command_type == "GRANT_TEMP_ADMIN":
        lease = session.scalar(
            select(TemporaryAdminLease).where(
                TemporaryAdminLease.grant_command_id == command.id
            )
        )
        if lease is None or lease.state not in {"REQUESTED", "GRANTING"}:
            return lease
        if command.status == "SUCCEEDED":
            result = command.result or {}
            expires_epoch = result.get("expires_epoch")
            try:
                expires_at = (
                    datetime.fromtimestamp(int(expires_epoch), tz=timezone.utc)
                    if expires_epoch
                    else observed_at + timedelta(seconds=600)
                )
            except (TypeError, ValueError, OverflowError):
                expires_at = observed_at + timedelta(seconds=600)
            lease.state = "ACTIVE"
            lease.granted_at = command.completed_at or observed_at
            lease.expires_at = expires_at
            lease.last_error = None
        elif command.status in TERMINAL_COMMAND_STATES:
            lease.state = "FAILED"
            lease.last_error = (
                command.error_message
                or command.error_code
                or f"Enrollment access grant command {command.status.lower()}."
            )
    elif command.command_type == "REVOKE_TEMP_ADMIN":
        lease = session.scalar(
            select(TemporaryAdminLease).where(
                TemporaryAdminLease.revoke_command_id == command.id
            )
        )
        if lease is None or lease.state not in {"REVOKING", "OVERDUE"}:
            return lease
        zkt = session.get(ZKTDevice, lease.zkt_device_id)
        connector = session.get(Connector, zkt.connector_id) if zkt else None
        if command.status == "SUCCEEDED":
            lease.state = "REVOKED"
            lease.revoked_at = command.completed_at or observed_at
            lease.last_error = None
            if connector:
                resolve_alert(session, connector, code="ADMIN_REVOKE_OVERDUE")
        elif command.status in TERMINAL_COMMAND_STATES:
            lease.state = "OVERDUE"
            lease.last_error = (
                command.error_message
                or command.error_code
                or f"Enrollment access revoke command {command.status.lower()}."
            )
            if connector:
                upsert_alert(
                    session,
                    connector,
                    code="ADMIN_REVOKE_OVERDUE",
                    severity="CRITICAL",
                    message="Temporary administrator could not be verified as revoked.",
                    details={"lease_id": lease.lease_id},
                )
    if lease is not None:
        lease.updated_at = observed_at
    return lease


def reconcile_admin_lease_states(session: Session) -> int:
    """Repair leases left active by an older command completion path."""

    repaired = 0
    leases = session.scalars(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.state.in_(
                ["REQUESTED", "GRANTING", "REVOKING", "OVERDUE"]
            )
        )
    ).all()
    for lease in leases:
        command_id = (
            lease.grant_command_id
            if lease.state in {"REQUESTED", "GRANTING"}
            else lease.revoke_command_id
        )
        command = session.get(DeviceCommand, command_id) if command_id else None
        if command is None or command.status not in TERMINAL_COMMAND_STATES:
            continue
        previous_state = lease.state
        reconcile_admin_lease_command(session, command=command)
        if lease.state != previous_state:
            repaired += 1
    return repaired


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
    if command.status in TERMINAL_COMMAND_STATES:
        return command
    if status == "SUCCEEDED" and command.command_type == "DELETE_USER":
        before_count = result.get("attendance_count_before")
        after_count = result.get("attendance_count_after")
        if result.get("user_absent") is not True or before_count != after_count:
            status = "FAILED"
            error_code = "DELETE_POSTCONDITION_FAILED"
            error_message = (
                "Delete verification did not prove user absence with unchanged attendance count."
            )
    now = utc_now()
    command.status = status
    if status == "ACKNOWLEDGED":
        command.acknowledged_at = command.acknowledged_at or now
    elif status == "RUNNING":
        command.started_at = command.started_at or now
    elif status in {"WAITING_FOR_DEVICE", "WAITING_FOR_ZKT", "RETRYING"}:
        command.error_code = error_code
        command.error_message = error_message
    elif status in TERMINAL_COMMAND_STATES:
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
    if (
        command.command_type in {"CREATE_USER", "UPDATE_USER", "DELETE_USER"}
        and status in TERMINAL_COMMAND_STATES
    ):
        apply_user_command_terminal_state(session, command=command, status=status)
    lease = reconcile_admin_lease_command(session, command=command, now=now)
    if lease is not None and status == "SUCCEEDED":
        lease_user = session.get(DeviceUser, lease.device_user_id)
        if lease_user is not None:
            apply_verified_terminal_fingerprints(lease_user, result)
    append_audit(
        session,
        actor=f"connector:{connector.connector_id}",
        action=f"COMMAND_{command.command_type}_{status}",
        target_type="command",
        target_id=command.command_id,
        outcome=status,
        after={"error_code": error_code, "result": redact_context(result)},
    )
    return command


def apply_user_command_terminal_state(
    session: Session, *, command: DeviceCommand, status: str
) -> None:
    payload = decrypt_json(command.payload_encrypted)
    desired = decrypt_json(command.desired_state_encrypted)
    user_key = payload.get("user_key") or desired.get("user_key")
    if not user_key:
        return
    user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
    if user is None:
        return
    user.current_command_id = None
    user.updated_at = utc_now()
    if status != "SUCCEEDED":
        if command.command_type == "CREATE_USER" and user.lifecycle_state == "PENDING":
            user.lifecycle_state = "CREATE_FAILED"
        return
    zkt = session.get(ZKTDevice, user.zkt_device_id)
    if command.command_type == "CREATE_USER":
        user.present = True
        user.lifecycle_state = "ACTIVE"
        user.observed_at = utc_now()
        user.row_version += 1
    elif command.command_type == "UPDATE_USER":
        user.display_name = desired["display_name"]
        user.cnic_encrypted = encrypt_cnic(desired["cnic"])
        user.cnic_lookup_hash = cnic_lookup(desired["cnic"])
        user.cnic_last4 = desired["cnic"][-4:]
        user.shift_worker = bool(desired["shift_worker"])
        user.privilege = int(desired["privilege"])
        user.machine_name_encrypted = encrypt_text(desired["machine_name"])
        user.row_version += 1
    elif command.command_type == "DELETE_USER":
        user.present = False
        user.lifecycle_state = "DELETED"
        user.deleted_at = utc_now()
        user.deleted_by = command.actor
        user.row_version += 1
    if command.command_type in {"CREATE_USER", "UPDATE_USER"}:
        apply_verified_terminal_fingerprints(user, command.result or {})
    if zkt and command.command_type in {"CREATE_USER", "UPDATE_USER", "DELETE_USER"}:
        resolved_conflicts = reconcile_device_user_identity_conflicts(
            session, connector=zkt.connector, zkt=zkt
        )
        candidates = [*resolved_conflicts]
        if command.command_type in {"CREATE_USER", "UPDATE_USER"}:
            candidates.append(user)
        for candidate in {row.id: row for row in candidates if row.id is not None}.values():
            if (
                candidate.lifecycle_state == "ACTIVE"
                and candidate.identity_conflict_code is None
            ):
                enrich_undelivered_attendance(session, zkt=zkt, user=candidate)


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
    zkt = require_writable_user_profile(connector, "admin_lease")
    fingerprint_preconditions = terminal_fingerprint_preconditions(user)
    active = session.scalar(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.zkt_device_id == zkt.id,
            TemporaryAdminLease.state.in_(
                ["REQUESTED", "GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]
            ),
        )
    )
    if active:
        raise ValueError(f"Device already has active lease {active.lease_id}.")
    lease_id = str(uuid4())
    command = create_command(
        session,
        connector=connector,
        command_type="GRANT_TEMP_ADMIN",
        payload={
            "lease_id": lease_id,
            "uid": user.uid,
            "user_id": user.user_id,
            "duration_seconds": 600,
        },
        expected_state={
            "serial": zkt.serial,
            "uid": user.uid,
            "user_id": user.user_id,
            "privilege": 0,
            "row_version": user.row_version,
            **fingerprint_preconditions,
        },
        desired_state={"privilege": 14},
        idempotency_key=idempotency_key,
        actor=actor,
        # The grant request may wait for the device for up to ten minutes. The
        # enrollment lease itself starts only after the terminal elevation has
        # been reread and verified by firmware.
        expires_in_seconds=600,
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
            if existing and existing.status not in {
                "FAILED",
                "EXPIRED",
                "CANCELED",
                "CANCELLED",
            }:
                continue
        zkt = session.get(ZKTDevice, lease.zkt_device_id)
        connector = session.get(Connector, zkt.connector_id) if zkt else None
        user = session.get(DeviceUser, lease.device_user_id)
        if connector is None or user is None:
            continue
        try:
            command = create_command(
                session,
                connector=connector,
                command_type="REVOKE_TEMP_ADMIN",
                payload={"lease_id": lease.lease_id, "uid": user.uid, "user_id": user.user_id},
                expected_state={
                    "serial": zkt.serial,
                    "uid": user.uid,
                    "user_id": user.user_id,
                    "privilege": 14,
                    **terminal_fingerprint_preconditions(user),
                },
                desired_state={"privilege": 0},
                idempotency_key=f"revoke:{lease.lease_id}:{lease.revoke_command_id or 0}",
                actor="system:lease-watchdog",
                expires_in_seconds=None,
            )
        except ValueError as exc:
            lease.state = "OVERDUE"
            lease.last_error = str(exc)
            lease.updated_at = now
            upsert_alert(
                session,
                connector,
                code="ADMIN_REVOKE_OVERDUE",
                severity="CRITICAL",
                message="Temporary administrator is overdue and the connector cannot accept a revoke command.",
                details={"lease_id": lease.lease_id},
            )
            continue
        lease.revoke_command_id = command.id
        lease.state = "REVOKING"
        lease.updated_at = now
        commands.append(command)
    return commands
