"""Attendance totals for a time window, all-time totals, and one check-in's journey.

Read side only (UI/UX revamp ATT-4, ATT-5, HOME-5). One endpoint serves the
Home page's "Attendance today" and the Attendance page's tiles, so the two
never disagree:

* one grouped query over the indexed ``device_event_time``
  (``connector_id, ords_status -> count, min(device_event_time),
  max(oracle_confirmed_at)``); grouping into the five plain status groups and
  into Life / Health happens in Python;
* preset windows (whole Pakistan days in the last week, open-ended or ending
  at a Pakistan midnight) are cached for 15 s through ``dashboard_cache``;
  custom ranges are computed live and are bounded to 31 days.

Nothing here writes, and nothing changes custody, delivery or release.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add import dashboard_cache
from zk_add.attendance_groups import (
    DELIVERY_GROUPS,
    add_to_totals,
    delivery_group,
    empty_totals,
    totals_from_status_counts,
)
from zk_add.connector_naming import DIVISIONS, code_division
from zk_add.models import AttendanceEvent, Connector, OrdsOutbox
from zk_add.time_utils import ensure_utc, iso_utc, utc_now

LIVE_SOURCES = ("LIVE", "LIVE_POLL")
MAX_RANGE = timedelta(days=31)
PKT = timezone(timedelta(hours=5))
# The same figures as the overview's cached sections (web.py DASHBOARD_METRICS_*).
SUMMARY_TTL_SECONDS = 15
SUMMARY_STALE_SECONDS = 120
SUMMARY_WAIT_SECONDS = 30
SUMMARY_MARK_MIN_AGE_SECONDS = 5
SUMMARY_KEY_PREFIX = "attendance-summary:"
# Preset windows start at a Pakistan midnight at most this many days back.
PRESET_MAX_DAYS_BACK = 8

_cached_keys: set[str] = set()
_cached_keys_lock = threading.Lock()


class SummaryRangeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_window(from_time: datetime, to_time: datetime | None, *, now: datetime) -> None:
    from_time = ensure_utc(from_time)
    upper = ensure_utc(to_time) if to_time is not None else max(now, from_time)
    if to_time is not None and upper <= from_time:
        raise SummaryRangeError("RANGE_INVALID", "The start must be before the end.")
    if upper - from_time > MAX_RANGE:
        raise SummaryRangeError("RANGE_TOO_LONG", "Choose 31 days or fewer.")


def _is_pkt_midnight(value: datetime) -> bool:
    local = ensure_utc(value).astimezone(PKT)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def is_preset_window(from_time: datetime, to_time: datetime | None, *, now: datetime) -> bool:
    """Today, Yesterday, Last 7 days and the like: whole recent Pakistan days."""

    if not _is_pkt_midnight(from_time):
        return False
    if to_time is not None and not _is_pkt_midnight(to_time):
        return False
    return now - ensure_utc(from_time) <= timedelta(days=PRESET_MAX_DAYS_BACK)


def _empty_division_totals() -> dict:
    return {**empty_totals(), "oldest_waiting_at": None}


def _fold(target: dict, status: str | None, count: int, oldest: datetime | None) -> None:
    add_to_totals(target, status, count)
    if oldest is not None and delivery_group(status) == "waiting":
        current = target["oldest_waiting_at"]
        oldest = ensure_utc(oldest)
        if current is None or oldest < current:
            target["oldest_waiting_at"] = oldest


def compute_summary(
    session: Session,
    *,
    from_time: datetime,
    to_time: datetime | None,
    division: str | None,
    connector_ids: Iterable[int] | None,
) -> dict:
    """The totals for one window. ``connector_ids`` None means every device."""

    statement = (
        select(
            AttendanceEvent.connector_id,
            AttendanceEvent.ords_status,
            func.count(AttendanceEvent.id),
            func.min(AttendanceEvent.device_event_time),
            func.max(AttendanceEvent.oracle_confirmed_at),
        )
        .where(AttendanceEvent.device_event_time >= ensure_utc(from_time))
        .group_by(AttendanceEvent.connector_id, AttendanceEvent.ords_status)
    )
    if to_time is not None:
        statement = statement.where(AttendanceEvent.device_event_time < ensure_utc(to_time))
    if connector_ids is not None:
        statement = statement.where(AttendanceEvent.connector_id.in_(sorted(set(connector_ids))))
    rows = session.execute(statement).all()
    divisions = {
        row.id: code_division(row.display_name or row.zone_id)
        for row in session.execute(
            select(Connector.id, Connector.display_name, Connector.zone_id).where(
                Connector.id.in_({row[0] for row in rows})
            )
        ).all()
    } if rows else {}
    totals = _empty_division_totals()
    by_division = {name: _empty_division_totals() for name in DIVISIONS}
    last_sent_at: datetime | None = None
    for connector_pk, status, count, oldest, confirmed in rows:
        row_division = divisions.get(connector_pk, "health")
        _fold(by_division[row_division], status, int(count), oldest)
        if division is not None and row_division != division:
            continue
        _fold(totals, status, int(count), oldest)
        if confirmed is not None:
            confirmed = ensure_utc(confirmed)
            last_sent_at = confirmed if last_sent_at is None or confirmed > last_sent_at else last_sent_at
    for bucket in (totals, *by_division.values()):
        if bucket["oldest_waiting_at"] is not None:
            bucket["oldest_waiting_at"] = iso_utc(bucket["oldest_waiting_at"])
    return {
        "from_time": iso_utc(from_time),
        "to_time": iso_utc(to_time) if to_time is not None else None,
        "division": division,
        "totals": totals,
        "last_sent_at": iso_utc(last_sent_at) if last_sent_at is not None else None,
        "by_division": by_division,
    }


def _cache_key(from_time: datetime, to_time: datetime | None, division: str | None,
               connector_ids: Iterable[int] | None) -> str:
    ids = "all" if connector_ids is None else ",".join(str(value) for value in sorted(set(connector_ids)))
    upper = iso_utc(to_time) if to_time is not None else "open"
    return f"{SUMMARY_KEY_PREFIX}{iso_utc(from_time)}|{upper}|{division or 'all'}|{ids}"


def summary(
    session: Session,
    *,
    from_time: datetime,
    to_time: datetime | None = None,
    division: str | None = None,
    connector_ids: Iterable[int] | None = None,
    use_cache: bool = False,
    compute_session=None,
    now: datetime | None = None,
) -> dict:
    """Validated, optionally cached totals with ``computed_at`` / ``max_age_seconds``.

    ``compute_session`` (a factory) gives the cached computation a session of
    its own, as the overview does; without it the request's session is used.
    """

    now = now or utc_now()
    validate_window(from_time, to_time, now=now)
    connector_ids = None if connector_ids is None else sorted(set(connector_ids))
    # A single place or every place: bounded key space, so the cache cannot grow.
    cacheable = use_cache and is_preset_window(from_time, to_time, now=now) and (
        connector_ids is None or len(connector_ids) <= 1
    )

    def compute() -> dict:
        if compute_session is None:
            return compute_summary(session, from_time=from_time, to_time=to_time,
                                   division=division, connector_ids=connector_ids)
        own = compute_session()
        try:
            return compute_summary(own, from_time=from_time, to_time=to_time,
                                   division=division, connector_ids=connector_ids)
        finally:
            own.close()

    if not cacheable:
        return {**compute_summary(session, from_time=from_time, to_time=to_time,
                                  division=division, connector_ids=connector_ids),
                "computed_at": iso_utc(now), "max_age_seconds": 0}
    key = _cache_key(from_time, to_time, division, connector_ids)
    with _cached_keys_lock:
        _cached_keys.add(key)
    entry = dashboard_cache.get_or_compute_entry(
        key, SUMMARY_TTL_SECONDS, compute,
        stale_s=SUMMARY_STALE_SECONDS, wait_s=SUMMARY_WAIT_SECONDS,
    )
    return {**entry.value, "computed_at": iso_utc(entry.computed_at),
            "max_age_seconds": SUMMARY_TTL_SECONDS}


def mark_summaries_stale(min_age_s: float = SUMMARY_MARK_MIN_AGE_SECONDS) -> None:
    """After a release or recovery action: refresh every cached window soon."""

    with _cached_keys_lock:
        keys = list(_cached_keys)
    for key in keys:
        dashboard_cache.mark_stale(key, min_age_s)


def forget_cached_keys() -> None:
    """Test hook."""

    with _cached_keys_lock:
        _cached_keys.clear()


def all_time_totals(event_status_counts: dict[str | None, int]) -> dict:
    """The overview's all-time figures, from the status counts it already has.

    No extra statement. ``since`` is None: the figures cover every retained day.
    """

    totals = totals_from_status_counts(event_status_counts)
    return {
        "received": totals["received"],
        "sent": totals["sent"],
        "waiting": totals["waiting"],
        "trying_again": totals["trying_again"],
        "not_matched": totals["not_matched"],
        "set_aside": totals["set_aside"],
        "problem": totals["problem"],
        "since": None,
        "range": "all_days",
    }


def journey(session: Session, event: AttendanceEvent) -> dict:
    """One check-in from the machine to the HR system, as plain times.

    Never returns the CNIC, the payload or any secret: only times, counts and
    the stored status codes (for Technical details).
    """

    outbox = session.scalar(
        select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == event.id).limit(1)
    )

    def when(value: datetime | None) -> str | None:
        return iso_utc(value) if value is not None else None

    group = delivery_group(event.ords_status)
    sent = group == "sent"
    steps = [
        {"step": "checked_in", "at": when(event.device_event_time), "done": True},
        {"step": "reached_dashboard", "at": when(event.received_at or event.captured_at), "done": True},
        {"step": "queued_for_hr", "at": when(outbox.created_at) if outbox else None,
         "done": outbox is not None or sent},
        {"step": "sent_to_hr", "at": when(event.oracle_confirmed_at), "done": sent},
    ]
    return {
        "id": event.id,
        "delivery_group": group,
        "ords_status": event.ords_status,
        "source": event.source,
        "live": event.source in LIVE_SOURCES,
        "device_event_time": when(event.device_event_time),
        "captured_at": when(event.captured_at),
        "received_at": when(event.received_at),
        "oracle_confirmed_at": when(event.oracle_confirmed_at),
        "outbox": {
            "status": outbox.status,
            "delivery_type": outbox.delivery_type,
            "created_at": when(outbox.created_at),
            "last_attempt_at": when(outbox.last_attempt_at),
            "attempt_count": int(outbox.attempt_count or 0),
            "next_attempt_at": when(outbox.next_attempt_at),
            "acknowledged_at": when(outbox.acknowledged_at),
        } if outbox is not None else None,
        "steps": steps,
    }


__all__ = [
    "DELIVERY_GROUPS", "LIVE_SOURCES", "MAX_RANGE", "SummaryRangeError", "all_time_totals",
    "compute_summary", "is_preset_window", "journey", "mark_summaries_stale", "summary",
    "validate_window",
]
