"""One check-in's journey to the HR system (UI/UX revamp ATT-5)."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from test_add_backend import CNIC, _make_recovery_event, connector_fixture, db as db
from zk_add.models import OrdsOutbox
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db


def _client(db) -> TestClient:
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client


def test_journey_shows_outbox_times_and_attempts_without_secrets(db):
    connector = connector_fixture(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = _make_recovery_event(db, connector, event_number=7301, ords_status="FAILED_RETRYABLE")
    db.add(OrdsOutbox(
        attendance_event_id=event.id, delivery_type="LIVE", status="FAILED_RETRYABLE",
        payload_hash="f" * 64, attempt_count=3, last_attempt_at=now - timedelta(minutes=2),
        next_attempt_at=now + timedelta(minutes=3), last_error="HTTP 503 from ORDS",
        created_at=now - timedelta(minutes=10), updated_at=now,
    ))
    db.commit()
    client = _client(db)

    response = client.get(f"/api/v1/attendance/{event.id}/journey")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_group"] == "waiting"
    assert body["outbox"]["attempt_count"] == 3
    assert body["outbox"]["delivery_type"] == "LIVE"
    assert body["outbox"]["last_attempt_at"].startswith(
        (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"))
    assert body["outbox"]["next_attempt_at"] is not None
    assert [step["step"] for step in body["steps"]] == [
        "checked_in", "reached_dashboard", "queued_for_hr", "sent_to_hr"]
    assert [step["done"] for step in body["steps"]] == [True, True, True, False]
    text = response.text
    for secret in (CNIC, "f" * 64, "HTTP 503", "cnic", "payload"):
        assert secret not in text


def test_journey_without_an_outbox_row_and_unknown_id(db):
    connector = connector_fixture(db)
    event = _make_recovery_event(db, connector, event_number=7302, ords_status="BLOCKED_IDENTITY")
    db.commit()
    client = _client(db)
    body = client.get(f"/api/v1/attendance/{event.id}/journey").json()
    assert body["outbox"] is None
    assert body["delivery_group"] == "not_matched"
    assert body["steps"][2] == {"step": "queued_for_hr", "at": None, "done": False}
    assert client.get("/api/v1/attendance/999999/journey").status_code == 404
    # The summary route is not shadowed by the journey route.
    assert client.get("/api/v1/attendance/summary", params={
        "from_time": datetime.now(timezone.utc).isoformat()}).status_code == 200
