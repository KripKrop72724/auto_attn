"""Device page read-side fields: today's check-ins (Pakistan day) and the /status since and reason."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_add_backend import _make_recovery_event, connector_fixture, db as db  # noqa: F401
from test_device_health import admin_client
from zk_add import dashboard_cache, device_detail, device_names, public_status, web
from zk_add.public_status import StatusTracker
from zk_add.time_utils import iso_utc, utc_now

STARTED = utc_now() - timedelta(hours=1)
PKT = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def _clean():
    device_names.forget_fleet()
    public_status.remember_statuses([])
    dashboard_cache.invalidate()
    yield
    device_names.forget_fleet()
    public_status.remember_statuses([])
    dashboard_cache.invalidate()


def test_pakistan_day_starts_at_pakistan_midnight():
    # 23:59 PKT on 5 Nov is still 5 Nov; 00:01 PKT on 6 Nov is the next day.
    late = datetime(2026, 11, 5, 23, 59, tzinfo=PKT)
    early = datetime(2026, 11, 6, 0, 1, tzinfo=PKT)
    assert device_detail.pakistan_day(late) == ("2026-11-05", datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc))
    assert device_detail.pakistan_day(early) == ("2026-11-06", datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc))


def test_today_counts_by_delivery_status(db):
    connector = connector_fixture(db)
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PKT)
    midnight = datetime(2026, 11, 6, 0, 0, tzinfo=PKT).astimezone(timezone.utc)  # stored in UTC
    statuses = ["ACKED", "ACKED_CHECK", "PENDING", "FAILED_RETRYABLE", "BLOCKED_IDENTITY",
                "QUARANTINED_IDENTITY_REUSE", "QUARANTINED_ORDS_REJECTED"]
    for number, status in enumerate(statuses, start=1):
        _make_recovery_event(db, connector, event_number=number, ords_status=status,
                             device_event_time=midnight + timedelta(minutes=number))
    # 23:59 PKT yesterday: not today.
    _make_recovery_event(db, connector, event_number=50, ords_status="ACKED",
                         device_event_time=midnight - timedelta(minutes=1))
    db.commit()
    today = device_detail.compute_attendance_today(db, connector, now)
    assert today["day"] == "2026-11-06"
    assert (today["checkins_today"], today["sent_to_hr_today"], today["waiting_to_send"],
            today["not_matched_today"]) == (7, 2, 2, 2)
    assert today["last_checkin_at"] == midnight + timedelta(minutes=7)


def test_device_without_a_machine_has_zero_today(db):
    connector = connector_fixture(db)
    connector.zkt_device = None
    today = device_detail.compute_attendance_today(db, connector)
    assert (today["checkins_today"], today["last_checkin_at"]) == (0, None)


def test_today_is_shared_for_15_seconds(db, monkeypatch):
    connector = connector_fixture(db)
    calls = []
    real = device_detail.compute_attendance_today

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(device_detail, "compute_attendance_today", counted)
    first = device_detail.attendance_today(db, connector, cached=True)
    second = device_detail.attendance_today(db, connector, cached=True)
    assert first == second and len(calls) == 1
    device_detail.attendance_today(db, connector, cached=False)
    assert len(calls) == 2


def test_device_detail_carries_today_and_the_public_status(db):
    connector = connector_fixture(db)
    connector.display_name = connector.zone_id = "ZONE-MULTAN-02"
    db.commit()
    _make_recovery_event(db, connector, event_number=1, ords_status="ACKED", device_event_time=utc_now())
    db.commit()
    snapshot = StatusTracker(STARTED).refresh_in(db, utc_now())
    device = snapshot["devices"][0]
    client, _headers = admin_client(db)
    body = client.get(f"/api/v1/devices/{connector.connector_id}").json()
    assert body["attendance_today"]["checkins_today"] == 1
    assert body["attendance_today"]["sent_to_hr_today"] == 1
    assert set(body["attendance_today"]) == {"day", "last_checkin_at", "checkins_today", "sent_to_hr_today",
                                             "waiting_to_send", "not_matched_today", "computed_at"}
    assert (body["status_since"], body["offline_reason"]) == (device["since"], device["reason_code"])
    assert device["status"] == "offline" and body["offline_reason"] == "no_internet"


def test_device_detail_before_the_first_status_refresh(db):
    connector = connector_fixture(db)
    client, _headers = admin_client(db)
    body = client.get(f"/api/v1/devices/{connector.connector_id}").json()
    assert (body["public_status"], body["status_since"], body["offline_reason"]) == (None, None, None)


def test_device_list_rows_stay_free_of_today_figures(db):
    connector_fixture(db)
    client, _headers = admin_client(db)
    row = client.get("/api/v1/devices").json()["rows"][0]
    assert "attendance_today" not in row


def test_status_cache_matches_the_snapshot_since(db):
    connector = connector_fixture(db)
    now = utc_now()
    StatusTracker(STARTED).refresh_in(db, now)
    status = public_status.status_for(connector)
    assert status is not None and status["since"].endswith("Z")
    assert status["since"] == iso_utc(datetime.fromisoformat(status["since"].replace("Z", "+00:00")))
    connector.is_spare = True
    assert public_status.status_for(connector) is None
    assert web.device_attendance_today(db, connector)["checkins_today"] == 0
