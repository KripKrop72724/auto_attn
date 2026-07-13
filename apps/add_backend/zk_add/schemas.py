from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class StepUpRequest(BaseModel):
    password: str


class ConnectorCreateRequest(BaseModel):
    hardware_id: str = Field(min_length=3, max_length=120)
    zone_id: str = Field(min_length=1, max_length=100)
    zone_name: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=255)
    expected_serial: str | None = Field(default=None, max_length=120)


class ConnectorActivateRequest(BaseModel):
    connector_id: str
    hardware_id: str
    activation_code: str


class Envelope(BaseModel):
    schema_version: str = "1"
    message_id: str
    connector_id: str
    boot_id: str
    seq: int = Field(ge=0)
    sent_at: datetime
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HeartbeatPayload(BaseModel):
    firmware_version: str | None = None
    config_version: int = 1
    uptime_seconds: int | None = None
    rssi: int | None = None
    free_heap: int | None = None
    outbox_depth: int = 0
    current_activity: str | None = None
    led_state: str | None = None
    zkt: dict[str, Any] = Field(default_factory=dict)


class UserSnapshotRow(BaseModel):
    uid: str
    user_id: str
    name: str
    privilege: int = 0
    card: int | None = None


class UserSnapshotRequest(BaseModel):
    snapshot_id: str
    complete: bool = True
    observed_at: datetime
    users: list[UserSnapshotRow]


class AttendanceEventIn(BaseModel):
    event_uid: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    uid: str | None = None
    user_id: str = Field(min_length=1, max_length=100)
    raw_name: str | None = None
    device_event_time: datetime
    captured_at: datetime
    source: Literal[
        "LIVE",
        "LIVE_POLL",
        "DUMP_STARTUP",
        "DUMP_RECONNECT",
        "MANUAL_REPROCESS",
        "RECONCILE_15M",
    ]
    status: str | int | None = None
    punch: str | int | None = None
    raw_punch: bool = False
    clock_drift_seconds: float | None = None
    clock_quality: str = "UNKNOWN"
    boot_id: str | None = None
    sequence: int | None = None
    raw_event: dict[str, Any] = Field(default_factory=dict)


class AttendanceBatchRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=120)
    payload_digest: str | None = None
    events: list[AttendanceEventIn] = Field(min_length=1, max_length=100)


class DeviceLogIn(BaseModel):
    boot_id: str
    sequence: int
    level: Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    subsystem: str
    code: str | None = None
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    device_time: datetime | None = None


class LogBatchRequest(BaseModel):
    logs: list[DeviceLogIn]


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    privilege: Literal[0, 14] | None = None
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)


class AdminLeaseRequest(BaseModel):
    uid: str
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str


class RestartRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str


class CommandUpdate(BaseModel):
    command_id: str
    status: Literal["ACKNOWLEDGED", "RUNNING", "SUCCEEDED", "FAILED"]
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class AlertAcknowledgeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
