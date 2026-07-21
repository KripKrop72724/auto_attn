from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from zk_add.db import Base
from zk_add.time_utils import utc_now


def utc_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Site(Base):
    __tablename__ = "add_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    province: Mapped[str | None] = mapped_column(String(100), index=True)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Karachi")
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class Connector(Base):
    __tablename__ = "add_connectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    hardware_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("add_sites.id"), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True)
    zone_name: Mapped[str] = mapped_column(String(255))
    device_id: Mapped[str] = mapped_column(String(120), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    firmware_version: Mapped[str | None] = mapped_column(String(80))
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    lifecycle_state: Mapped[str] = mapped_column(String(40), default="ONBOARDING", index=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    boot_id: Mapped[str | None] = mapped_column(String(100), index=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_disconnect_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_activity: Mapped[str | None] = mapped_column(String(80))
    last_error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    onboarding_generation: Mapped[int] = mapped_column(Integer, default=0)
    last_onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    site: Mapped[Site | None] = relationship()
    zkt_device: Mapped["ZKTDevice | None"] = relationship(back_populates="connector", uselist=False)


class ConnectorCredential(Base):
    __tablename__ = "add_connector_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_last4: Mapped[str] = mapped_column(String(8))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    issued_at: Mapped[datetime] = utc_column()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OnboardingNonce(Base):
    __tablename__ = "add_onboarding_nonces"
    __table_args__ = (UniqueConstraint("hardware_id", "nonce", name="uq_add_onboard_nonce"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hardware_id: Mapped[str] = mapped_column(String(120), index=True)
    nonce: Mapped[str] = mapped_column(String(120))
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_column()


class ConnectorNonce(Base):
    __tablename__ = "add_connector_nonces"
    __table_args__ = (UniqueConstraint("connector_id", "nonce", name="uq_add_connector_nonce"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    nonce: Mapped[str] = mapped_column(String(120))
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_column()


class ZKTDevice(Base):
    __tablename__ = "add_zkt_devices"
    __table_args__ = (
        UniqueConstraint("connector_id", name="uq_add_zkt_connector"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    serial: Mapped[str | None] = mapped_column(String(120), index=True)
    expected_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    mac_address: Mapped[str | None] = mapped_column(String(32))
    port: Mapped[int] = mapped_column(Integer, default=4370)
    model: Mapped[str | None] = mapped_column(String(120), index=True)
    platform: Mapped[str | None] = mapped_column(String(120), index=True)
    firmware_version: Mapped[str | None] = mapped_column(String(120))
    transport: Mapped[str] = mapped_column(String(16), default="TCP")
    capability_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    certification_state: Mapped[str] = mapped_column(String(40), default="READ_ONLY", index=True)
    certification_fingerprint: Mapped[str | None] = mapped_column(String(255), index=True)
    certification_observations: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_snapshot_revision: Mapped[int] = mapped_column(Integer, default=0)
    identity_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("add_device_user_snapshots.id"), index=True
    )
    identity_snapshot_state_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    identity_snapshot_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    identity_snapshot_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_snapshot_stable: Mapped[bool] = mapped_column(Boolean, default=False)
    last_identity_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    writes_disabled_reason: Mapped[str | None] = mapped_column(String(160), index=True)
    online: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    connection_state: Mapped[str] = mapped_column(String(40), default="UNKNOWN", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    flap_count_15m: Mapped[int] = mapped_column(Integer, default=0)
    last_transition_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offline_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stability_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_latency_ms: Mapped[int | None] = mapped_column(Integer)
    user_count: Mapped[int | None] = mapped_column(Integer)
    attendance_count: Mapped[int | None] = mapped_column(Integer)
    sampled_device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_time_sampled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_time_drift_seconds: Mapped[float | None] = mapped_column(Float)
    last_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_reconcile_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_restart_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_restart_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()

    connector: Mapped[Connector] = relationship(back_populates="zkt_device")


class ConnectorSession(Base):
    __tablename__ = "add_connector_sessions"
    __table_args__ = (UniqueConstraint("connector_id", "boot_id", name="uq_add_session_boot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    boot_id: Mapped[str] = mapped_column(String(100), index=True)
    connected_at: Mapped[datetime] = utc_column()
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnect_reason: Mapped[str | None] = mapped_column(Text)
    remote_address: Mapped[str | None] = mapped_column(String(100))
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)


class DeviceUser(Base):
    __tablename__ = "add_device_users"
    __table_args__ = (
        Index(
            "uq_add_user_device_uid_active",
            "zkt_device_id",
            "uid",
            unique=True,
            postgresql_where=text("lifecycle_state = 'ACTIVE'"),
            sqlite_where=text("lifecycle_state = 'ACTIVE'"),
        ),
        Index(
            "uq_add_user_device_user_id_active",
            "zkt_device_id",
            "user_id",
            unique=True,
            postgresql_where=text("lifecycle_state = 'ACTIVE'"),
            sqlite_where=text("lifecycle_state = 'ACTIVE'"),
        ),
        Index(
            "uq_add_user_device_cnic_active",
            "zkt_device_id",
            "cnic_lookup_hash",
            unique=True,
            postgresql_where=text(
                "lifecycle_state = 'ACTIVE' AND cnic_lookup_hash IS NOT NULL "
                "AND identity_conflict_code IS NULL"
            ),
            sqlite_where=text(
                "lifecycle_state = 'ACTIVE' AND cnic_lookup_hash IS NOT NULL "
                "AND identity_conflict_code IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_key: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    uid: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    machine_name_encrypted: Mapped[str | None] = mapped_column(Text)
    terminal_identity_fingerprint: Mapped[str | None] = mapped_column(String(64))
    terminal_state_fingerprint: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(255), index=True)
    cnic_encrypted: Mapped[str | None] = mapped_column(Text)
    cnic_lookup_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    cnic_last4: Mapped[str | None] = mapped_column(String(4))
    identity_conflict_code: Mapped[str | None] = mapped_column(String(50), index=True)
    shift_worker: Mapped[bool] = mapped_column(Boolean, default=False)
    privilege: Mapped[int] = mapped_column(Integer, default=0, index=True)
    card: Mapped[int | None] = mapped_column(BigInteger)
    present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    lifecycle_state: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    source: Mapped[str] = mapped_column(String(40), default="DEVICE_SNAPSHOT", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(120))
    create_audit_id: Mapped[int | None] = mapped_column(ForeignKey("add_audit_events.id"))
    update_audit_id: Mapped[int | None] = mapped_column(ForeignKey("add_audit_events.id"))
    delete_audit_id: Mapped[int | None] = mapped_column(ForeignKey("add_audit_events.id"))
    current_command_id: Mapped[int | None] = mapped_column(ForeignKey("add_device_commands.id"))
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot_id: Mapped[str | None] = mapped_column(String(100), index=True)
    snapshot_revision: Mapped[int | None] = mapped_column(Integer, index=True)
    observed_at: Mapped[datetime] = utc_column()
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class DeviceUserSnapshot(Base):
    __tablename__ = "add_device_user_snapshots"
    __table_args__ = (
        UniqueConstraint("zkt_device_id", "revision", name="uq_add_user_snapshot_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(100), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    state_hash: Mapped[str] = mapped_column(String(64), index=True)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    stable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str] = mapped_column(String(80), default="PERIODIC")
    user_count: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = utc_column()


class IdentityConflictResolution(Base):
    """Audited approval for one exact-CNIC terminal group.

    The terminal records remain independent.  This row only records that an
    administrator verified that the current, exact set of records belongs to
    one employee.  A deterministic group token makes the approval stale as
    soon as the ZKT membership changes.
    """

    __tablename__ = "add_identity_conflict_resolutions"
    __table_args__ = (
        Index(
            "uq_add_active_identity_resolution",
            "zkt_device_id",
            "cnic_lookup_hash",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resolution_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    cnic_lookup_hash: Mapped[str] = mapped_column(String(64), index=True)
    group_token: Mapped[str] = mapped_column(String(64), index=True)
    member_device_user_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    resolution_type: Mapped[str] = mapped_column(String(80), index=True)
    classification: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    reason: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    created_by: Mapped[str] = mapped_column(String(120), index=True)
    revoked_by: Mapped[str | None] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AttendanceEvent(Base):
    __tablename__ = "add_attendance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    device_user_id: Mapped[int | None] = mapped_column(ForeignKey("add_device_users.id"), index=True)
    identity_resolution_id: Mapped[int | None] = mapped_column(
        ForeignKey("add_identity_conflict_resolutions.id"), index=True
    )
    identity_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("add_device_user_snapshots.id"), index=True
    )
    identity_terminal_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    identity_resolution_status: Mapped[str] = mapped_column(
        String(50), default="WAITING_FOR_SNAPSHOT", index=True
    )
    identity_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_repaired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_repair_reason: Mapped[str | None] = mapped_column(String(120))
    device_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    uid: Mapped[str | None] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), index=True)
    cnic_encrypted: Mapped[str | None] = mapped_column(Text)
    cnic_lookup_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    cnic_last4: Mapped[str | None] = mapped_column(String(4))
    device_event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = utc_column()
    source: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str | None] = mapped_column(String(40), index=True)
    punch: Mapped[str | None] = mapped_column(String(40), index=True)
    raw_punch: Mapped[bool] = mapped_column(Boolean, default=False)
    clock_drift_seconds: Mapped[float | None] = mapped_column(Float)
    clock_quality: Mapped[str] = mapped_column(String(40), default="UNKNOWN", index=True)
    boot_id: Mapped[str | None] = mapped_column(String(100), index=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    raw_event: Mapped[dict] = mapped_column(JSON, default=dict)
    ords_status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)


Index("ix_add_attendance_device_time_id", AttendanceEvent.zkt_device_id, AttendanceEvent.device_event_time, AttendanceEvent.id)


class DeviceCommand(Base):
    __tablename__ = "add_device_commands"
    __table_args__ = (
        UniqueConstraint("connector_id", "idempotency_key", name="uq_add_command_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    command_type: Mapped[str] = mapped_column(String(80), index=True)
    payload_encrypted: Mapped[str] = mapped_column(Text)
    expected_state_encrypted: Mapped[str] = mapped_column(Text)
    desired_state_encrypted: Mapped[str] = mapped_column(Text)
    payload_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    actor: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = utc_column()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)


class DeviceCommandEvent(Base):
    __tablename__ = "add_device_command_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_id: Mapped[int] = mapped_column(ForeignKey("add_device_commands.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = utc_column()


class TemporaryAdminLease(Base):
    __tablename__ = "add_temporary_admin_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    device_user_id: Mapped[int] = mapped_column(ForeignKey("add_device_users.id"), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    original_privilege: Mapped[int] = mapped_column(Integer, default=0)
    grant_command_id: Mapped[int | None] = mapped_column(ForeignKey("add_device_commands.id"))
    revoke_command_id: Mapped[int | None] = mapped_column(ForeignKey("add_device_commands.id"))
    requested_at: Mapped[datetime] = utc_column()
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = utc_column()


class DeviceTelemetry(Base):
    __tablename__ = "add_device_telemetry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    boot_id: Mapped[str | None] = mapped_column(String(100), index=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    rssi: Mapped[int | None] = mapped_column(Integer)
    free_heap: Mapped[int | None] = mapped_column(Integer)
    uptime_seconds: Mapped[int | None] = mapped_column(BigInteger)
    outbox_depth: Mapped[int] = mapped_column(Integer, default=0)
    current_activity: Mapped[str | None] = mapped_column(String(80))
    led_state: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = utc_column()


class DeviceLog(Base):
    __tablename__ = "add_device_logs"
    __table_args__ = (UniqueConstraint("connector_id", "boot_id", "sequence", name="uq_add_log_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    boot_id: Mapped[str] = mapped_column(String(100), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    level: Mapped[str] = mapped_column(String(20), index=True)
    subsystem: Mapped[str] = mapped_column(String(80), index=True)
    code: Mapped[str | None] = mapped_column(String(120), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = utc_column()


class DeviceAlert(Base):
    __tablename__ = "add_device_alerts"
    __table_args__ = (
        Index(
            "uq_add_open_alert_connector_code",
            "connector_id",
            "code",
            unique=True,
            postgresql_where=text("state = 'OPEN'"),
            sqlite_where=text("state = 'OPEN'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    code: Mapped[str] = mapped_column(String(120), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    state: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = utc_column()
    last_seen_at: Mapped[datetime] = utc_column()
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceConnectionEvent(Base):
    __tablename__ = "add_device_connection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    zkt_device_id: Mapped[int | None] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(40), index=True)
    to_state: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str | None] = mapped_column(String(160))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    flap_count_15m: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime] = utc_column()


class AdminSession(Base):
    __tablename__ = "add_admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(120), index=True)
    csrf_token: Mapped[str] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = utc_column()
    last_seen_at: Mapped[datetime] = utc_column()
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_step_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Base):
    __tablename__ = "add_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(80), index=True)
    target_id: Mapped[str | None] = mapped_column(String(120), index=True)
    request_id: Mapped[str | None] = mapped_column(String(120), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(100))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(40), index=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = utc_column()


class OrdsOutbox(Base):
    __tablename__ = "add_ords_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_event_id: Mapped[int | None] = mapped_column(ForeignKey("add_attendance_events.id"), unique=True, index=True)
    delivery_type: Mapped[str] = mapped_column(String(30), default="LIVE", index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class IdentityTombstone(Base):
    __tablename__ = "add_identity_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "zkt_device_id", "user_id", "device_user_id", name="uq_add_identity_tombstone"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zkt_device_id: Mapped[int] = mapped_column(ForeignKey("add_zkt_devices.id"), index=True)
    device_user_id: Mapped[int] = mapped_column(ForeignKey("add_device_users.id"), index=True)
    device_serial: Mapped[str | None] = mapped_column(String(120), index=True)
    uid: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    display_name_encrypted: Mapped[str] = mapped_column(Text)
    cnic_encrypted: Mapped[str | None] = mapped_column(Text)
    cnic_lookup_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    cnic_last4: Mapped[str | None] = mapped_column(String(4))
    shift_worker: Mapped[bool] = mapped_column(Boolean, default=False)
    privilege: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = utc_column()
