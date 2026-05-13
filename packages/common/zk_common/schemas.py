from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from zk_common.enums import (
    ClockStatus,
    IncidentSeverity,
    IncidentType,
    OutageType,
    SourceType,
    TrustStatus,
)


class ZoneRegisterRequest(BaseModel):
    zone_id: str
    zone_name: str
    enrollment_key: str


class ZoneRegisterResponse(BaseModel):
    ok: bool
    zone_token: str
    server_utc: datetime


class TimeResponse(BaseModel):
    server_utc: datetime


class DeviceHeartbeat(BaseModel):
    device_id: str
    serial: str | None = None
    online: bool = False
    last_clock_status: str | None = None
    last_drift_seconds: float | None = None


class HeartbeatRequest(BaseModel):
    zone_id: str
    zone_name: str
    agent_version: str = "0.1.0"
    server_time_estimate: datetime | None = None
    devices: list[DeviceHeartbeat] = Field(default_factory=list)
    pending_queue_count: int = 0


class AttendanceSyncEvent(BaseModel):
    event_uid: str
    device_id: str
    device_serial: str | None = None
    user_id: str
    employee_name: str | None = None
    device_event_time: datetime
    zone_received_wall_time: datetime | None = None
    zone_trusted_time: datetime
    head_office_received_time: datetime | None = None
    source_type: SourceType
    trust_status: TrustStatus
    punch: str | None = None
    raw_event: dict[str, Any] = Field(default_factory=dict)
    device_drift_seconds: float | None = None
    fraud_score: int = 0
    fraud_reason: str | None = None


class AttendanceSyncRequest(BaseModel):
    zone_id: str
    batch_id: str
    events: list[AttendanceSyncEvent]


class ClockCheckSyncItem(BaseModel):
    id: str | int | None = None
    zone_id: str
    device_id: str
    device_serial: str | None = None
    device_time: datetime | None = None
    trusted_time: datetime
    windows_wall_time: datetime
    monotonic_ns: int
    drift_seconds: float | None = None
    expected_device_time: datetime | None = None
    jump_seconds: float | None = None
    status: ClockStatus
    reason: str | None = None
    created_at: datetime


class ClockChecksSyncRequest(BaseModel):
    zone_id: str
    batch_id: str
    clock_checks: list[ClockCheckSyncItem]


class OutageSyncItem(BaseModel):
    id: str | int | None = None
    zone_id: str
    device_id: str | None = None
    outage_type: OutageType
    start_time: datetime
    end_time: datetime | None = None
    duration_seconds: float | None = None
    start_reason: str | None = None
    end_reason: str | None = None
    classification: str | None = None
    created_at: datetime


class OutageSyncRequest(BaseModel):
    zone_id: str
    batch_id: str
    outages: list[OutageSyncItem]


class IncidentSyncItem(BaseModel):
    id: str | int | None = None
    zone_id: str
    device_id: str | None = None
    incident_type: IncidentType
    severity: IncidentSeverity
    description: str
    related_event_uid: str | None = None
    related_outage_id: str | int | None = None
    created_at: datetime


class IncidentSyncRequest(BaseModel):
    zone_id: str
    batch_id: str
    incidents: list[IncidentSyncItem]


class SyncResponse(BaseModel):
    ok: bool
    acked_event_uids: list[str] = Field(default_factory=list)
    acked_ids: list[str] = Field(default_factory=list)
    server_utc: datetime
    errors: list[str] = Field(default_factory=list)
