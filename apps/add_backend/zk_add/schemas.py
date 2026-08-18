from __future__ import annotations

from datetime import datetime
import re
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
    terminal_identity_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
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
        "FULL_HISTORY",
        "CURRENT_RECONCILE",
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


class ReconciliationCreateRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    confirmation: str = Field(min_length=10, max_length=180)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReconciliationControlRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReconciliationAnchorRequest(BaseModel):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    cutoff_count: int = Field(ge=0)
    latest_terminal_count: int = Field(ge=0)
    record_size: Literal[8, 16, 40]
    source_total_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    first_anchor_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    identity_snapshot_id: str | None = Field(default=None, max_length=120)


class ReconciliationSourceRecord(BaseModel):
    ordinal: int = Field(ge=0)
    raw_record_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    terminal_record_key: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    occurrence_index: int = Field(default=1, ge=1)
    disposition: Literal[
        "EVENT",
        "BLOCKED_IDENTITY",
        "INVALID_TIME",
        "MALFORMED",
        "TERMINAL_DUPLICATE",
    ]
    event: AttendanceEventIn | None = None
    raw_record_b64: str = Field(min_length=4, max_length=512)
    error_code: str | None = Field(default=None, max_length=120)
    raw_timestamp: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    observed_uid: str | None = Field(default=None, max_length=40)
    observed_user_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_record_evidence(self):
        if self.disposition in {"EVENT", "BLOCKED_IDENTITY"} and self.event is None:
            raise ValueError("Parsed reconciliation rows require an attendance event")
        return self


class SourceProbeResultRequest(BaseModel):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    latest_terminal_count: int = Field(ge=0)
    record_size: Literal[8, 16, 40]
    ordinal: int = Field(ge=0)
    record: ReconciliationSourceRecord


class ReconciliationChunkRequest(BaseModel):
    assignment_id: str | None = Field(default=None, min_length=36, max_length=36)
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    sequence: int = Field(ge=0)
    start_ordinal: int = Field(ge=0)
    end_ordinal: int = Field(ge=1)
    chunk_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    previous_chain_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resulting_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    records: list[ReconciliationSourceRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_contiguous_chunk(self):
        if self.end_ordinal - self.start_ordinal != len(self.records):
            raise ValueError("Chunk range must exactly match its record count")
        expected = list(range(self.start_ordinal, self.end_ordinal))
        if [row.ordinal for row in self.records] != expected:
            raise ValueError("Chunk records must be ordered and contiguous")
        return self


class ReconciliationManifestRequest(BaseModel):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    cutoff_count: int = Field(ge=0)
    latest_terminal_count: int = Field(ge=0)
    final_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )


class SourceTailChunkRequest(BaseModel):
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    record_size: Literal[8, 16, 40]
    start_ordinal: int = Field(ge=0)
    end_ordinal: int = Field(ge=1)
    latest_terminal_count: int = Field(ge=1)
    chunk_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    previous_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resulting_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    records: list[ReconciliationSourceRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_contiguous_tail(self):
        if self.end_ordinal - self.start_ordinal != len(self.records):
            raise ValueError("Tail range must exactly match its record count")
        if self.end_ordinal > self.latest_terminal_count:
            raise ValueError("Tail range cannot exceed the observed terminal count")
        expected = list(range(self.start_ordinal, self.end_ordinal))
        if [row.ordinal for row in self.records] != expected:
            raise ValueError("Tail records must be ordered and contiguous")
        return self


class SourceExceptionActionRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReconciliationAssignmentReleaseRequest(BaseModel):
    assignment_id: str = Field(min_length=36, max_length=36)
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    committed_next_ordinal: int = Field(ge=0)
    reason: Literal[
        "COMMAND_PENDING",
        "LEASE_EXPIRING",
        "HEAP_PRESSURE",
        "DISCONNECTING",
        "TRANSIENT_STEP_FAILED",
    ]


class OracleReceiptBatchRequest(BaseModel):
    confirmation_path: Literal[
        "FIRMWARE_LIVE",
        "FIRMWARE_BULK",
        "FIRMWARE_RECONCILE",
    ]
    oracle_observed_at: datetime
    event_uids: list[
        str
    ] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("event_uids")
    @classmethod
    def validate_event_uids(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) != 64 or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(
                    "Each event_uid must be a 64-character lowercase hex digest"
                )
        if len(values) != len(set(values)):
            raise ValueError("Each event_uid may be confirmed only once per batch")
        return values


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
    reason: str | None = Field(default=None, min_length=10, max_length=500)
    typed_confirmation: str | None = Field(default=None, max_length=300)

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


class TerminalSerialConfirmRequest(BaseModel):
    observed_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


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


class HistoricalIdentityAliasRequest(BaseModel):
    source_user_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    source_cnic: str = Field(min_length=13, max_length=15)
    target_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_source_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Historical source CNIC must contain exactly 13 digits")
        return digits


class HistoricalDirectoryIdentityRequest(BaseModel):
    source_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    source_cnic: str = Field(min_length=13, max_length=15)
    directory_employee_id: str = Field(min_length=1, max_length=40)
    directory_service_number: str = Field(min_length=1, max_length=40)
    directory_employee_name: str = Field(min_length=2, max_length=255)
    directory_zone_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_directory_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Directory CNIC must contain exactly 13 digits")
        return digits

    @field_validator("directory_employee_id")
    @classmethod
    def validate_directory_employee_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("Directory employee ID must contain only digits")
        return normalized

    @field_validator("directory_service_number")
    @classmethod
    def validate_directory_service_number(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValueError(
                "Directory service number contains unsupported characters"
            )
        return normalized


class HistoricalEventGroupIdentityRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_user_id: str = Field(min_length=1, max_length=100)
    source_uid: str = Field(default="", max_length=40)
    source_cnic: str = Field(min_length=13, max_length=15)
    directory_employee_id: str = Field(min_length=1, max_length=40)
    directory_service_number: str = Field(min_length=1, max_length=40)
    directory_employee_name: str = Field(min_length=2, max_length=255)
    directory_zone_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_event_group_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Directory CNIC must contain exactly 13 digits")
        return digits

    @field_validator("directory_employee_id")
    @classmethod
    def validate_event_group_employee_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("Directory employee ID must contain only digits")
        return normalized

    @field_validator("directory_service_number")
    @classmethod
    def validate_event_group_service_number(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValueError(
                "Directory service number contains unsupported characters"
            )
        return normalized


class HistoricalCurrentIdentityRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_user_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    source_uid: str = Field(default="", max_length=40)
    target_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    source_cnic: str = Field(min_length=13, max_length=15)
    verified_employee_name: str = Field(min_length=2, max_length=255)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_current_identity_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Authoritative CNIC must contain exactly 13 digits")
        return digits


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
