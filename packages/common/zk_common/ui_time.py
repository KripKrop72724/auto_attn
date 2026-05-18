from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.sql import Select

from zk_common.time_utils import ensure_utc, iso_utc, parse_datetime, utc_now


DATE_PRESETS: tuple[tuple[str, str], ...] = (
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7_days", "Last 7 Days"),
    ("last_30_days", "Last 30 Days"),
    ("custom", "Custom"),
    ("all", "All"),
)
VALID_DATE_PRESETS = {value for value, _label in DATE_PRESETS}


@dataclass(frozen=True)
class TimelineDateFilter:
    date_preset: str
    from_date: str | None
    to_date: str | None
    start_utc: datetime | None
    end_utc: datetime | None
    display_timezone: str

    @property
    def is_bounded(self) -> bool:
        return self.start_utc is not None or self.end_utc is not None


def _first_query_value(params: Mapping[str, object], key: str) -> str | None:
    getter = getattr(params, "get", None)
    value = getter(key) if getter else params.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        value = value[0] if value else None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def selected_query_values(params: Mapping[str, object], keys: Sequence[str]) -> dict[str, str]:
    return {key: value for key in keys if (value := _first_query_value(params, key))}


def timeline_date_filter(
    params: Mapping[str, object],
    *,
    timezone_name: str,
    now: datetime | None = None,
    default_preset: str = "today",
) -> TimelineDateFilter:
    preset = _first_query_value(params, "date_preset") or default_preset
    if preset not in VALID_DATE_PRESETS:
        preset = default_preset

    from_date = _first_query_value(params, "from_date")
    to_date = _first_query_value(params, "to_date")
    zone = ZoneInfo(timezone_name)
    local_now = ensure_utc(now or utc_now()).astimezone(zone)
    today_start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    start_local: datetime | None = None
    end_local: datetime | None = None

    if preset == "today":
        start_local = today_start
        end_local = today_start + timedelta(days=1)
    elif preset == "yesterday":
        start_local = today_start - timedelta(days=1)
        end_local = today_start
    elif preset == "last_7_days":
        start_local = today_start - timedelta(days=6)
        end_local = today_start + timedelta(days=1)
    elif preset == "last_30_days":
        start_local = today_start - timedelta(days=29)
        end_local = today_start + timedelta(days=1)
    elif preset == "custom":
        if parsed_from := _parse_date(from_date):
            start_local = datetime.combine(parsed_from, time.min, tzinfo=zone)
        if parsed_to := _parse_date(to_date):
            end_local = datetime.combine(parsed_to + timedelta(days=1), time.min, tzinfo=zone)

    return TimelineDateFilter(
        date_preset=preset,
        from_date=from_date if preset == "custom" else None,
        to_date=to_date if preset == "custom" else None,
        start_utc=start_local.astimezone(timezone.utc) if start_local else None,
        end_utc=end_local.astimezone(timezone.utc) if end_local else None,
        display_timezone=timezone_name,
    )


def apply_timeline_date_filter(statement: Select, column, date_filter: TimelineDateFilter) -> Select:
    if date_filter.start_utc is not None:
        statement = statement.where(column >= date_filter.start_utc)
    if date_filter.end_utc is not None:
        statement = statement.where(column < date_filter.end_utc)
    return statement


def apply_selected_filters(statement: Select, filters: Mapping[str, tuple[object, str | None]]) -> Select:
    for column, value in filters.values():
        if value:
            statement = statement.where(column == value)
    return statement


def filter_context(date_filter: TimelineDateFilter, selected: Mapping[str, str] | None = None) -> dict:
    return {
        "date_preset": date_filter.date_preset,
        "from_date": date_filter.from_date or "",
        "to_date": date_filter.to_date or "",
        "display_timezone": date_filter.display_timezone,
        "presets": DATE_PRESETS,
        "selected": dict(selected or {}),
    }


def timestamp_view(value: datetime | str | None, timezone_name: str) -> dict[str, str] | None:
    parsed = _parse_datetime_value(value)
    if parsed is None:
        return None
    local_value = ensure_utc(parsed).astimezone(ZoneInfo(timezone_name))
    return {
        "iso": iso_utc(parsed),
        "date": local_value.strftime("%Y-%m-%d"),
        "time": local_value.strftime("%H:%M:%S"),
    }


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime_value(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return parse_datetime(value)
        except ValueError:
            return None
    return None
