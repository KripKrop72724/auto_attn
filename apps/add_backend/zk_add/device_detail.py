"""Read-side figures for one device's page: today's check-ins in Pakistan time.

Counts only; nothing here changes attendance, delivery or reconciliation. The
statuses come from the shared ORDS sets, so "sent", "waiting" and "not
matched" mean what the delivery worker means by them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from zk_add import dashboard_cache
from zk_add.models import AttendanceEvent, Connector
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_ACTIVE_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
)
from zk_add.time_utils import ensure_utc, utc_now

PKT = timezone(timedelta(hours=5))
TODAY_TTL_SECONDS = 15


def pakistan_day(now: datetime) -> tuple[str, datetime]:
    """Today's date in Pakistan and its midnight as UTC."""
    local = ensure_utc(now).astimezone(PKT)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local.date().isoformat(), midnight.astimezone(timezone.utc)


def _count(statuses: frozenset[str]):
    return func.coalesce(func.sum(case((AttendanceEvent.ords_status.in_(sorted(statuses)), 1), else_=0)), 0)


def compute_attendance_today(db: Session, connector: Connector, now: datetime | None = None) -> dict:
    now = now or utc_now()
    day, midnight = pakistan_day(now)
    zkt = connector.zkt_device
    result = {
        "day": day,
        "last_checkin_at": None,
        "checkins_today": 0,
        "sent_to_hr_today": 0,
        "waiting_to_send": 0,
        "not_matched_today": 0,
        "computed_at": ensure_utc(now),
    }
    if zkt is None:
        return result
    # Both read the (zkt_device_id, device_event_time) index.
    row = db.execute(
        select(
            func.count(AttendanceEvent.id),
            _count(ORDS_ACKNOWLEDGED_STATUSES),
            _count(ORDS_ACTIVE_STATUSES),
            _count(ORDS_IDENTITY_HELD_STATUSES),
        ).where(
            AttendanceEvent.zkt_device_id == zkt.id,
            AttendanceEvent.device_event_time >= midnight,
        )
    ).one()
    last = db.scalar(
        select(func.max(AttendanceEvent.device_event_time)).where(AttendanceEvent.zkt_device_id == zkt.id)
    )
    result.update({
        "last_checkin_at": ensure_utc(last) if last is not None else None,
        "checkins_today": int(row[0] or 0),
        "sent_to_hr_today": int(row[1] or 0),
        "waiting_to_send": int(row[2] or 0),
        "not_matched_today": int(row[3] or 0),
    })
    return result


def attendance_today(db: Session, connector: Connector, *, cached: bool) -> dict:
    """Today's figures, shared between viewers of the device for 15 s."""
    if not cached:
        return compute_attendance_today(db, connector)
    return dict(dashboard_cache.get_or_compute(
        f"device-today:{connector.connector_id}",
        TODAY_TTL_SECONDS,
        lambda: compute_attendance_today(db, connector),
        stale_s=60,
        wait_s=10,
    ))
