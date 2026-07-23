from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class StepUpRequest(BaseModel):
    password: str


class OnboardRequest(BaseModel):
    hardware_id: str = Field(min_length=17, max_length=17)
    zone_id: str = Field(min_length=1, max_length=100)
    zone_name: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=120)
    firmware_version: str = Field(min_length=1, max_length=80)
    expected_serial: str | None = Field(default=None, max_length=120)

    @field_validator("hardware_id")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        compact = "".join(character for character in value.lower() if character in "0123456789abcdef")
        if len(compact) != 12:
            raise ValueError("hardware_id must be a 6-byte Wi-Fi MAC address")
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


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
    terminal_identity_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    terminal_state_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class UserSnapshotRequest(BaseModel):
    snapshot_id: str
    complete: bool = True
    stable: bool = True
    state_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reason: str = Field(default="PERIODIC", min_length=1, max_length=80)
    started_at: datetime | None = None
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


class UserCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    cnic: str = Field(min_length=13, max_length=15)
    shift_worker: bool = False
    user_id_override: str | None = Field(default=None, min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("cnic")
    @classmethod
    def validate_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("CNIC must contain exactly 13 digits")
        return digits

    @field_validator("user_id_override")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("Employee/user ID override must be numeric")
        return value


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    cnic: str | None = Field(default=None, min_length=13, max_length=15)
    shift_worker: bool | None = None
    privilege: Literal[0, 14] | None = None
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str = Field(min_length=1, max_length=512)

    @field_validator("cnic")
    @classmethod
    def validate_optional_cnic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("CNIC must contain exactly 13 digits")
        return digits

    @model_validator(mode="after")
    def require_change(self):
        if all(
            value is None
            for value in (self.display_name, self.cnic, self.shift_worker, self.privilege)
        ):
            raise ValueError("At least one user field must be changed")
        return self


class UserDeleteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    typed_confirmation: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=512)


class BulkUserDeleteTarget(BaseModel):
    user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class BulkUserDeleteRequest(BaseModel):
    targets: list[BulkUserDeleteTarget] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [target.user_key for target in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Each terminal user may be selected only once")
        return self


class BulkUserDeleteCancelRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class IdentityConflictMemberConfirmation(BaseModel):
    user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class IdentityConflictResolveRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    members: list[IdentityConflictMemberConfirmation] = Field(min_length=2, max_length=20)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: Literal["SAME EMPLOYEE"]
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def unique_members(self):
        keys = [member.user_key for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError("Each conflict member must be confirmed exactly once")
        return self


class IdentityConflictRevokeRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: Literal["REVOKE RESOLUTION"]
    password: str = Field(min_length=1, max_length=512)


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
    status: Literal[
        "ACKNOWLEDGED",
        "WAITING_FOR_DEVICE",
        "WAITING_FOR_ZKT",
        "RETRYING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    ]
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class AlertAcknowledgeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
