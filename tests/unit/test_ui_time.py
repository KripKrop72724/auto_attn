from datetime import datetime, timezone

from zk_common.ui_time import timeline_date_filter, timestamp_view


def test_timeline_date_presets_use_display_timezone_boundaries():
    now = datetime(2026, 5, 18, 6, 30, tzinfo=timezone.utc)

    today = timeline_date_filter({"date_preset": "today"}, timezone_name="Asia/Karachi", now=now)
    assert today.start_utc == datetime(2026, 5, 17, 19, 0, tzinfo=timezone.utc)
    assert today.end_utc == datetime(2026, 5, 18, 19, 0, tzinfo=timezone.utc)

    yesterday = timeline_date_filter({"date_preset": "yesterday"}, timezone_name="Asia/Karachi", now=now)
    assert yesterday.start_utc == datetime(2026, 5, 16, 19, 0, tzinfo=timezone.utc)
    assert yesterday.end_utc == datetime(2026, 5, 17, 19, 0, tzinfo=timezone.utc)

    last_7_days = timeline_date_filter({"date_preset": "last_7_days"}, timezone_name="Asia/Karachi", now=now)
    assert last_7_days.start_utc == datetime(2026, 5, 11, 19, 0, tzinfo=timezone.utc)
    assert last_7_days.end_utc == datetime(2026, 5, 18, 19, 0, tzinfo=timezone.utc)

    last_30_days = timeline_date_filter(
        {"date_preset": "last_30_days"}, timezone_name="Asia/Karachi", now=now
    )
    assert last_30_days.start_utc == datetime(2026, 4, 18, 19, 0, tzinfo=timezone.utc)
    assert last_30_days.end_utc == datetime(2026, 5, 18, 19, 0, tzinfo=timezone.utc)


def test_custom_date_range_is_inclusive_of_to_date():
    date_filter = timeline_date_filter(
        {"date_preset": "custom", "from_date": "2026-05-10", "to_date": "2026-05-12"},
        timezone_name="Asia/Karachi",
        now=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )

    assert date_filter.start_utc == datetime(2026, 5, 9, 19, 0, tzinfo=timezone.utc)
    assert date_filter.end_utc == datetime(2026, 5, 12, 19, 0, tzinfo=timezone.utc)


def test_all_date_preset_has_no_bounds_and_timestamp_view_has_seconds():
    date_filter = timeline_date_filter(
        {"date_preset": "all"},
        timezone_name="Asia/Karachi",
        now=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    assert date_filter.start_utc is None
    assert date_filter.end_utc is None

    view = timestamp_view(datetime(2026, 5, 18, 6, 30, 5, tzinfo=timezone.utc), "Asia/Karachi")
    assert view == {
        "iso": "2026-05-18T06:30:05Z",
        "date": "2026-05-18",
        "time": "11:30:05",
    }
