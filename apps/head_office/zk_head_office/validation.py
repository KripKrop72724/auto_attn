from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.enums import TrustStatus
from zk_common.schemas import AttendanceSyncEvent
from zk_common.time_utils import ensure_utc, utc_now
from zk_head_office.db import ClockCheck, FraudIncident, OutagePeriod


def final_trust_status(
    session: Session, event: AttendanceSyncEvent, *, zone_id: str
) -> tuple[TrustStatus, str | None, int]:
    now = utc_now()
    if ensure_utc(event.device_event_time) > now + timedelta(minutes=5):
        return TrustStatus.BACKFILL_SUSPECT_TIME, "Head office found attendance timestamp in the future.", 100

    nearby_clock = session.scalar(
        select(ClockCheck)
        .where(
            ClockCheck.device_id == event.device_id,
            ClockCheck.zone_id == zone_id,
            ClockCheck.trusted_time >= event.zone_trusted_time - timedelta(minutes=3),
            ClockCheck.trusted_time <= event.zone_trusted_time + timedelta(minutes=3),
        )
        .order_by(ClockCheck.trusted_time.desc())
        .limit(1)
    )
    if nearby_clock is None and event.source_type.value == "LIVE":
        return TrustStatus.SUSPECT_MISSING_CLOCK_CHECK, "No nearby clock check supports this live event.", 70
    if nearby_clock and nearby_clock.status == "SUSPICIOUS":
        return TrustStatus.SUSPECT_DEVICE_CLOCK_JUMP, nearby_clock.reason, max(event.fraud_score, 80)

    outage = session.scalar(
        select(OutagePeriod)
        .where(
            OutagePeriod.device_id == event.device_id,
            OutagePeriod.start_time <= event.zone_trusted_time,
            ((OutagePeriod.end_time == None) | (OutagePeriod.end_time >= event.zone_trusted_time)),  # noqa: E711
        )
        .limit(1)
    )
    if outage and outage.outage_type == "DEVICE_LAN_OUTAGE":
        return (
            TrustStatus.BACKFILL_UNVERIFIED_BLIND_PERIOD,
            "Head office found a LAN blind period covering this event.",
            max(event.fraud_score, 60),
        )

    pc_incident = session.scalar(
        select(FraudIncident)
        .where(
            FraudIncident.incident_type.in_(["PC_CLOCK_TAMPER", "ZONE_PC_CLOCK_TAMPER"]),
            FraudIncident.zone_id == zone_id,
            FraudIncident.created_at >= event.zone_trusted_time - timedelta(minutes=5),
            FraudIncident.created_at <= event.zone_trusted_time + timedelta(minutes=5),
        )
        .limit(1)
    )
    if pc_incident:
        return TrustStatus.SUSPECT_PC_TIME, "Head office found PC clock tamper near this event.", 90

    return event.trust_status, event.fraud_reason, event.fraud_score
