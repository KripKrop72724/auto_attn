from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.enums import ClockStatus, PayloadType, SourceType, SyncStatus
from zk_common.hashing import attendance_event_uid
from zk_common.schemas import AttendanceSyncEvent
from zk_common.time_utils import device_local_to_utc, ensure_utc, utc_now
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.db import AttendanceEvent, Device, DeviceUser, OutagePeriod
from zk_zone_agent.fraud import FraudEngine, fraud_engine
from zk_zone_agent.sync import sync_queue_writer
from zk_zone_agent.zk_client import ZKAttendance, ZKUser


@dataclass(frozen=True)
class AttendanceContext:
    zone_id: str
    timezone: str
    internet_online: bool
    current_clock_status: ClockStatus | str | None = None
    pc_clock_suspicious: bool = False
    reconnect_clock_ok: bool = True
    service_was_down: bool = False


class AttendanceProcessor:
    def __init__(self, engine: FraudEngine = fraud_engine) -> None:
        self.engine = engine

    def upsert_users(self, session: Session, device: Device, users: Iterable[ZKUser]) -> None:
        for user in users:
            row = session.scalar(
                select(DeviceUser).where(
                    DeviceUser.device_id == device.device_id,
                    DeviceUser.user_id == str(user.user_id),
                )
            )
            if row is None:
                row = DeviceUser(device_id=device.device_id, user_id=str(user.user_id))
                session.add(row)
            row.uid = str(user.uid)
            row.employee_name = user.name
            row.privilege = user.privilege
            row.raw_json = json.dumps(user.raw or {}, default=str, sort_keys=True)
        session.flush()

    def process(
        self,
        session: Session,
        *,
        device: Device,
        attendance: ZKAttendance,
        context: AttendanceContext,
        source_type: SourceType,
        zone_trusted_time: datetime,
    ) -> AttendanceEvent:
        event_uid = attendance_event_uid(
            zone_id=context.zone_id,
            device_serial=device.serial or device.device_id,
            user_id=str(attendance.user_id),
            device_event_time=attendance.timestamp,
            punch=attendance.punch,
            source_uid=attendance.uid,
        )
        existing = session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid))
        if existing is not None:
            return existing

        employee_name = self._employee_name(session, device.device_id, str(attendance.user_id))
        if source_type == SourceType.LIVE:
            classification = self.engine.classify_live_attendance(
                device_event_time=attendance.timestamp,
                zone_trusted_time=zone_trusted_time,
                timezone_name=context.timezone,
                internet_online=context.internet_online,
                current_clock_status=context.current_clock_status,
                pc_clock_suspicious=context.pc_clock_suspicious,
            )
        else:
            classification = self.engine.classify_backfill(
                source_type=source_type,
                device_event_time=attendance.timestamp,
                timezone_name=context.timezone,
                trusted_now=zone_trusted_time,
                in_lan_outage=self._is_inside_lan_outage(
                    session,
                    device.device_id,
                    device_local_to_utc(attendance.timestamp, context.timezone),
                ),
                reconnect_clock_ok=context.reconnect_clock_ok,
                service_was_down=context.service_was_down,
            )

        payload = AttendanceSyncEvent(
            event_uid=event_uid,
            device_id=device.device_id,
            device_serial=device.serial,
            user_id=str(attendance.user_id),
            employee_name=employee_name,
            device_event_time=attendance.timestamp,
            zone_received_wall_time=ensure_utc(utc_now()),
            zone_trusted_time=zone_trusted_time,
            source_type=source_type,
            trust_status=classification.trust_status,
            punch=None if attendance.punch is None else str(attendance.punch),
            raw_event=attendance.raw or {},
            device_drift_seconds=classification.drift_seconds,
            fraud_score=classification.fraud_score,
            fraud_reason=classification.fraud_reason,
        )
        row = AttendanceEvent(
            event_uid=event_uid,
            zone_id=context.zone_id,
            device_id=device.device_id,
            device_serial=device.serial,
            user_id=str(attendance.user_id),
            employee_name=employee_name,
            device_event_time=attendance.timestamp,
            zone_received_wall_time=payload.zone_received_wall_time,
            zone_trusted_time=zone_trusted_time,
            status=classification.trust_status.value,
            trust_status=classification.trust_status.value,
            punch=None if attendance.punch is None else str(attendance.punch),
            raw_event=json.dumps(attendance.raw or {}, default=str, sort_keys=True),
            device_drift_seconds=classification.drift_seconds,
            fraud_score=classification.fraud_score,
            fraud_reason=classification.fraud_reason,
            source_type=source_type.value,
            sync_status=SyncStatus.PENDING.value,
        )
        session.add(row)
        session.flush()
        audit_ledger.append(session, "attendance_event", row.event_uid, payload)
        sync_queue_writer.enqueue(
            session,
            payload_type=PayloadType.ATTENDANCE,
            payload=payload,
            event_uid=row.event_uid,
            record_id=row.id,
        )
        return row

    def _employee_name(self, session: Session, device_id: str, user_id: str) -> str | None:
        user = session.scalar(
            select(DeviceUser).where(DeviceUser.device_id == device_id, DeviceUser.user_id == user_id)
        )
        return user.employee_name if user else None

    def _is_inside_lan_outage(self, session: Session, device_id: str, event_utc: datetime) -> bool:
        outage = session.scalar(
            select(OutagePeriod)
            .where(
                OutagePeriod.device_id == device_id,
                OutagePeriod.outage_type == "DEVICE_LAN_OUTAGE",
                OutagePeriod.start_time <= event_utc,
                ((OutagePeriod.end_time == None) | (OutagePeriod.end_time >= event_utc)),  # noqa: E711
            )
            .limit(1)
        )
        return outage is not None


attendance_processor = AttendanceProcessor()
