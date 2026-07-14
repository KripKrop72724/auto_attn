from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
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
    IdentityTombstone,
    OrdsOutbox,
    Site,
    TemporaryAdminLease,
    ZKTDevice,
)
from zk_add.schemas import AttendanceEventIn, HeartbeatPayload, UserSnapshotRequest
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
        reported_state = str(
            zkt_payload.get("connection_state")
            or ("ONLINE" if zkt_payload.get("online", False) else "OFFLINE")
        ).upper()
        previous_state = zkt.connection_state
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
        zkt.device_time_drift_seconds = zkt_payload.get(
            "drift_seconds", zkt.device_time_drift_seconds
        )
        zkt.last_seen_at = now
        zkt.updated_at = now
        if reported_state in {"ONLINE", "RECOVERING"}:
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
                display_name=parsed.display_name,
                row_version=1,
                lifecycle_state="ACTIVE",
                source="DEVICE_SNAPSHOT",
            )
            session.add(row)
            session.flush()
            rows_by_uid[incoming.uid] = row
        else:
            row.row_version = (row.row_version or 0) + 1
        row.user_id = incoming.user_id
        row.machine_name_encrypted = encrypt_text(incoming.name)
        if row.source != "ADD_MANAGED" or not row.display_name:
            row.display_name = parsed.display_name
        if parsed.cnic:
            previous_hash = row.cnic_lookup_hash
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
            if previous_hash != next_hash:
                identity_changed.append(row)
        row.privilege = incoming.privilege
        row.card = incoming.card
        row.present = True
        row.lifecycle_state = "ACTIVE"
        row.deleted_at = None
        row.deleted_by = None
        row.snapshot_id = snapshot.snapshot_id
        row.observed_at = observed_at
        row.updated_at = updated_at
    if snapshot.complete:
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
        if zkt.writes_disabled_reason == "USER_SNAPSHOT_TRUNCATED":
            zkt.writes_disabled_reason = None
    else:
        for row in existing_rows:
            if row.uid not in seen and row.lifecycle_state == "STAGING":
                row.lifecycle_state = "ACTIVE"
        zkt.snapshot_complete = False
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
    enrichable = {
        row.id: row
        for row in [*identity_changed, *resolved_conflicts]
        if row.id is not None
        and row.lifecycle_state == "ACTIVE"
        and row.identity_conflict_code is None
    }
    for row in enrichable.values():
        enrich_undelivered_attendance(session, zkt=zkt, user=row)
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

    duplicate_rows = {
        row.id: row
        for group in groups.values()
        if len(group) > 1
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

    if duplicate_rows:
        duplicate_group_count = sum(len(group) > 1 for group in groups.values())
        upsert_alert(
            session,
            connector,
            code="DUPLICATE_USER_CNIC",
            severity="HIGH",
            message="Multiple active terminal users share a CNIC; correction is required.",
            details={
                "affected_users": len(duplicate_rows),
                "duplicate_groups": duplicate_group_count,
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
    row = IdentityTombstone(
        zkt_device_id=zkt.id,
        device_user_id=user.id,
        device_serial=zkt.serial,
        uid=user.uid,
        user_id=user.user_id,
        display_name_encrypted=encrypt_text(user.display_name) or "",
        cnic_encrypted=(user.cnic_encrypted if user.identity_conflict_code is None else None),
        cnic_lookup_hash=(
            user.cnic_lookup_hash if user.identity_conflict_code is None else None
        ),
        cnic_last4=user.cnic_last4 if user.identity_conflict_code is None else None,
        shift_worker=user.shift_worker,
        privilege=user.privilege,
    )
    session.add(row)
    return row


def enrich_undelivered_attendance(
    session: Session, *, zkt: ZKTDevice, user: DeviceUser
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
        row.device_user_id = user.id
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
        user = session.scalar(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.user_id == incoming.user_id,
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        )
        tombstone = None
        if user is None:
            tombstone = session.scalar(
                select(IdentityTombstone)
                .where(
                    IdentityTombstone.zkt_device_id == zkt.id,
                    IdentityTombstone.user_id == incoming.user_id,
                )
                .order_by(IdentityTombstone.id.desc())
            )
        usable_user_identity = bool(user and user.identity_conflict_code is None)
        cnic = (
            decrypt_cnic(user.cnic_encrypted)
            if usable_user_identity and user
            else (parsed.cnic if user is None else None)
        )
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
        row = AttendanceEvent(
            event_uid=incoming.event_uid,
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            device_user_id=user.id if user else (tombstone.device_user_id if tombstone else None),
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
            clock_quality=incoming.clock_quality,
            boot_id=incoming.boot_id,
            sequence=incoming.sequence,
            raw_event=sanitize_raw_event(incoming.raw_event),
            ords_status="PENDING" if cnic else "BLOCKED_IDENTITY",
        )
        session.add(row)
        session.flush()
        if cnic:
            session.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING"))
        accepted.append(incoming.event_uid)
    return accepted, duplicates


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
    if user.identity_conflict_code and cnic is None:
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
    if claims:
        raise ValueError(duplicate_cnic_message(claims))
    next_display = " ".join((display_name or user.display_name).strip().split())
    next_shift = user.shift_worker if shift_worker is None else shift_worker
    next_privilege = user.privilege if privilege is None else privilege
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
        },
        desired_state={"user_key": user.user_key, "present": False},
        idempotency_key=idempotency_key,
        actor=actor,
        expires_in_seconds=settings.user_command_retry_seconds,
    )
    user.current_command_id = command.id
    return command


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
    if command.status in {"SUCCEEDED", "FAILED", "CANCELLED", "CANCELED", "EXPIRED"}:
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
    elif status in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
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
    if command.command_type in {"CREATE_USER", "UPDATE_USER", "DELETE_USER"} and status in {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    }:
        apply_user_command_terminal_state(session, command=command, status=status)
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
    lease_expires_at = utc_now() + timedelta(seconds=600)
    command = create_command(
        session,
        connector=connector,
        command_type="GRANT_TEMP_ADMIN",
        payload={
            "lease_id": lease_id,
            "uid": user.uid,
            "user_id": user.user_id,
            "duration_seconds": 600,
            "lease_expires_epoch": int(lease_expires_at.timestamp()),
        },
        expected_state={
            "serial": zkt.serial,
            "uid": user.uid,
            "user_id": user.user_id,
            "privilege": 0,
            "row_version": user.row_version,
        },
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
