"""Attendance totals for a window and all-time totals (UI/UX revamp ATT-4 / HOME-5)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event

from test_add_backend import _make_recovery_event, connector_fixture, db as db
from zk_add import attendance_summary, dashboard_cache
from zk_add.ords_outbox_coverage import event_ords_status_counts
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import _dashboard_metrics, app, get_db

PKT = timezone(timedelta(hours=5))


def _client(db, *, signed_in: bool = True) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    if signed_in:
        raw_session, _admin = create_admin_session(
            db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
        )
        db.commit()
        client.cookies.set(ADMIN_COOKIE, raw_session)
    return client


def _pkt_midnight(now: datetime) -> datetime:
    local = now.astimezone(PKT)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


@pytest.fixture()
def seeded(db):
    now = datetime.now(timezone.utc)
    today = _pkt_midnight(now)
    health = connector_fixture(db)
    life = connector_fixture(db, hardware_id="AA:BB:CC:DD:EE:03", expected_serial="LIFE-SERIAL-2")
    life.display_name = "LF-ZONE-BLD9-03"
    life.zone_id = "LF-ZONE-BLD9"
    db.commit()
    times = iter(today + timedelta(minutes=minutes) for minutes in range(1, 100))
    rows = {}

    def add(number, connector, status, at=None):
        event = _make_recovery_event(db, connector, event_number=number, ords_status=status)
        event.device_event_time = at or min(next(times), now)
        rows[number] = event
        return event

    sent = add(1, health, "ACKED")
    sent.oracle_confirmed_at = now - timedelta(seconds=30)
    add(2, health, "PENDING")
    add(3, health, "FAILED_RETRYABLE")
    add(4, health, "BLOCKED_IDENTITY")
    add(5, life, "ACKED")
    add(6, life, "QUARANTINED_INVALID_DEVICE_TIME")
    add(7, life, "QUARANTINED_ORDS_REJECTED")
    add(8, life, "PENDING")
    # Yesterday (Pakistan time): never in today's totals.
    add(9, health, "ACKED", at=today - timedelta(minutes=5))
    db.commit()
    return {"now": now, "today": today, "health": health, "life": life, "rows": rows}


def test_today_totals_per_group_and_division(db, seeded):
    client = _client(db)
    response = client.get("/api/v1/attendance/summary", params={"from_time": seeded["today"].isoformat()})
    assert response.status_code == 200, response.text
    body = response.json()
    totals = body["totals"]
    assert {key: totals[key] for key in (
        "received", "sent", "waiting", "trying_again", "not_matched", "set_aside", "problem")} == {
        "received": 8, "sent": 2, "waiting": 3, "trying_again": 1,
        "not_matched": 1, "set_aside": 1, "problem": 1}
    oldest_waiting = min(seeded["rows"][n].device_event_time for n in (2, 3, 8))
    assert totals["oldest_waiting_at"] == oldest_waiting.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z")
    assert body["last_sent_at"] is not None
    assert body["to_time"] is None
    assert body["by_division"]["life"]["received"] == 4
    assert body["by_division"]["health"]["received"] == 4
    assert body["by_division"]["life"]["problem"] == 1

    life_only = client.get("/api/v1/attendance/summary", params={
        "from_time": seeded["today"].isoformat(), "division": "life"}).json()
    assert life_only["totals"]["received"] == 4
    assert life_only["totals"]["set_aside"] == 1
    # by_division always covers both divisions.
    assert life_only["by_division"]["health"]["received"] == 4

    one_place = client.get("/api/v1/attendance/summary", params={
        "from_time": seeded["today"].isoformat(), "device_id": seeded["health"].connector_id}).json()
    assert one_place["totals"]["received"] == 4
    assert one_place["by_division"]["life"]["received"] == 0


def test_yesterday_uses_an_exclusive_upper_bound(db, seeded):
    client = _client(db)
    body = client.get("/api/v1/attendance/summary", params={
        "from_time": (seeded["today"] - timedelta(days=1)).isoformat(),
        "to_time": seeded["today"].isoformat(),
    }).json()
    assert body["totals"]["received"] == 1
    assert body["totals"]["sent"] == 1


def test_range_rules_and_admin_only(db, seeded):
    client = _client(db)
    too_long = client.get("/api/v1/attendance/summary", params={
        "from_time": (seeded["now"] - timedelta(days=32)).isoformat()})
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == {"code": "RANGE_TOO_LONG", "message": "Choose 31 days or fewer."}
    assert client.get("/api/v1/attendance/summary").status_code == 422
    backwards = client.get("/api/v1/attendance/summary", params={
        "from_time": seeded["today"].isoformat(),
        "to_time": (seeded["today"] - timedelta(hours=1)).isoformat()})
    assert backwards.status_code == 422
    assert backwards.json()["detail"]["code"] == "RANGE_INVALID"
    anonymous = _client(db, signed_in=False)
    assert anonymous.get("/api/v1/attendance/summary", params={
        "from_time": seeded["today"].isoformat()}).status_code == 401


def _count_statements(db):
    engine = db.get_bind()
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: sa_event.remove(engine, "before_cursor_execute", record)


def test_preset_windows_are_cached_and_custom_ranges_are_not(db, seeded):
    dashboard_cache.reset()
    attendance_summary.forget_cached_keys()
    statements, stop = _count_statements(db)
    try:
        first = attendance_summary.summary(db, from_time=seeded["today"], use_cache=True)
        after_first = len(statements)
        assert after_first >= 1
        second = attendance_summary.summary(db, from_time=seeded["today"], use_cache=True)
        assert len(statements) == after_first  # served from the cache
        assert second["totals"] == first["totals"]
        assert second["max_age_seconds"] == attendance_summary.SUMMARY_TTL_SECONDS
        custom = seeded["today"] + timedelta(minutes=7)
        attendance_summary.summary(db, from_time=custom, use_cache=True)
        after_custom = len(statements)
        assert after_custom > after_first
        live = attendance_summary.summary(db, from_time=custom, use_cache=True)
        assert len(statements) > after_custom  # a custom range is computed every time
        assert live["max_age_seconds"] == 0
    finally:
        stop()
        dashboard_cache.reset()
        attendance_summary.forget_cached_keys()


def test_mark_summaries_stale_after_a_release_refreshes_the_cached_window(db, seeded, monkeypatch):
    dashboard_cache.reset()
    attendance_summary.forget_cached_keys()
    clock = [1000.0]
    monkeypatch.setattr(dashboard_cache, "_clock", lambda: clock[0])
    try:
        before = attendance_summary.summary(db, from_time=seeded["today"], use_cache=True)
        _make_recovery_event(db, seeded["health"], event_number=50, ords_status="ACKED").device_event_time = (
            seeded["today"] + timedelta(minutes=99))
        db.commit()
        clock[0] += 1
        assert attendance_summary.summary(db, from_time=seeded["today"], use_cache=True)["totals"] == before[
            "totals"]
        attendance_summary.mark_summaries_stale(0)
        clock[0] += 1
        after = attendance_summary.summary(db, from_time=seeded["today"], use_cache=True)
        assert after["totals"]["received"] == before["totals"]["received"] + 1
    finally:
        dashboard_cache.reset()
        attendance_summary.forget_cached_keys()


def test_release_invalidation_also_marks_the_summaries_stale(db, monkeypatch):
    from zk_add import web

    marked = []
    monkeypatch.setattr(attendance_summary, "mark_summaries_stale", lambda *args: marked.append(args))
    web.invalidate_dashboard_metrics_after_commit(db)
    db.commit()
    assert marked


def test_all_time_totals_come_from_the_overview_status_counts_with_no_extra_statement(db, seeded):
    counts = event_ords_status_counts(db)
    statements, stop = _count_statements(db)
    try:
        totals = attendance_summary.all_time_totals(counts)
    finally:
        stop()
    assert statements == []
    assert totals["received"] == 9
    assert totals["sent"] == 3
    assert totals["not_matched"] == 1
    assert totals["since"] is None
    metrics = _dashboard_metrics(db)
    assert metrics["attendance_all_time"] == totals
