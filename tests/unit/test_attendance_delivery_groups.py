"""Plain status groups on the attendance list (UI/UX revamp ATT-3)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_add_backend import _make_recovery_event, connector_fixture, db as db
from zk_add import attendance_groups
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
    ORDS_RECONCILIATION_PENDING_STATUSES,
    ORDS_TERMINAL_REVIEW_STATUSES,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ords_delivery_groups.json"
LIFE_MAC = "AA:BB:CC:DD:EE:02"


def _client(db) -> TestClient:
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client


def _life_connector(db):
    connector = connector_fixture(db, hardware_id=LIFE_MAC, expected_serial="LIFE-SERIAL-1")
    connector.display_name = "LF-ZONE-BLD9-03"
    connector.zone_id = "LF-ZONE-BLD9"
    db.commit()
    return connector


def test_every_known_status_and_unknown_values_map_to_exactly_one_group():
    every = (ORDS_ACKNOWLEDGED_STATUSES | ORDS_IDENTITY_HELD_STATUSES
             | ORDS_RECONCILIATION_PENDING_STATUSES | ORDS_TERMINAL_REVIEW_STATUSES
             | attendance_groups.SET_ASIDE_STATUSES)
    for status in every:
        assert attendance_groups.delivery_group(status) in attendance_groups.DELIVERY_GROUP_ORDER
        owners = [name for name, members in attendance_groups.DELIVERY_GROUPS.items() if status in members]
        assert len(owners) == 1, status
    for status in (None, "", "MISSING_STATUS", "SOMETHING_NEW"):
        assert attendance_groups.delivery_group(status) == "problem"
    assert attendance_groups.delivery_group("QUARANTINED_ORDS_REJECTED") == "problem"
    assert attendance_groups.delivery_group("ACKED_CHECK") == "sent"
    assert attendance_groups.delivery_group("RETRYING") == "waiting"
    assert attendance_groups.delivery_group("QUARANTINED_IDENTITY_REUSE") == "not_matched"
    assert attendance_groups.delivery_group("QUARANTINED_INVALID_DEVICE_TIME") == "set_aside"


def test_shared_fixture_matches_the_backend_groups():
    document = json.loads(FIXTURE.read_text())
    assert document["groups"] == list(attendance_groups.DELIVERY_GROUP_ORDER)
    assert document["trying_again"] == sorted(attendance_groups.TRYING_AGAIN_STATUSES)
    assert document["rows"] == attendance_groups.fixture_rows()
    for row in document["rows"]:
        assert attendance_groups.delivery_group(row["ords_status"]) == row["delivery_group"]


def test_parse_groups_accepts_lists_and_commas_and_rejects_unknown_names():
    assert attendance_groups.parse_groups(["sent,waiting", "sent", " PROBLEM "]) == ["sent", "waiting", "problem"]
    with pytest.raises(ValueError):
        attendance_groups.parse_groups(["finished"])


def test_totals_from_status_counts_fold_every_group_and_trying_again():
    totals = attendance_groups.totals_from_status_counts({
        "ACKED": 5, "PENDING": 2, "FAILED_RETRYABLE": 1, "BLOCKED_IDENTITY": 3,
        "QUARANTINED_INVALID_EVENT_UID": 1, "QUARANTINED_ORDS_REJECTED": 1, None: 1,
    })
    assert totals == {"received": 14, "sent": 5, "waiting": 3, "trying_again": 1,
                      "not_matched": 3, "set_aside": 1, "problem": 2}


def test_rows_carry_group_place_and_division_and_filters_work(db):
    health = connector_fixture(db)
    life = _life_connector(db)
    now = datetime.now(timezone.utc)
    sent = _make_recovery_event(db, health, event_number=9101, ords_status="ACKED")
    waiting = _make_recovery_event(db, health, event_number=9102, ords_status="FAILED_RETRYABLE")
    refused = _make_recovery_event(db, health, event_number=9103, ords_status="QUARANTINED_ORDS_REJECTED")
    unknown = _make_recovery_event(db, health, event_number=9104, ords_status="SOMETHING_NEW")
    life_held = _make_recovery_event(db, life, event_number=9105, ords_status="BLOCKED_IDENTITY")
    life_live = _make_recovery_event(db, life, event_number=9106, ords_status="PENDING")
    life_live.source = "LIVE_POLL"
    life_live.clock_quality = "DRIFTED"
    edge = _make_recovery_event(db, health, event_number=9107, ords_status="ACKED")
    edge.device_event_time = now.replace(microsecond=0) - timedelta(hours=1)
    edge.clock_quality = "INVALID"
    db.commit()
    client = _client(db)

    body = client.get("/api/v1/attendance").json()
    by_id = {row["id"]: row for row in body["rows"]}
    assert by_id[sent.id]["delivery_group"] == "sent"
    assert by_id[waiting.id]["delivery_group"] == "waiting"
    assert by_id[refused.id]["delivery_group"] == "problem"
    assert by_id[life_held.id]["delivery_group"] == "not_matched"
    assert by_id[life_held.id]["source_connector"]["division"] == "life"
    assert by_id[life_held.id]["source_connector"]["friendly_name"] == "Building 9 · Device 3"
    assert by_id[sent.id]["source_connector"]["division"] == "health"
    # Static options: no DISTINCT scan; the old test contract still holds.
    assert {"BLOCKED_IDENTITY", "FAILED_RETRYABLE", "ACKED", "CNIC_NOT_LINKED"} <= set(body["status_options"])

    def ids(**params):
        response = client.get("/api/v1/attendance", params=params)
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["rows"]}

    assert ids(delivery_group="problem") == {refused.id, unknown.id}
    assert ids(delivery_group="sent,waiting") == {sent.id, waiting.id, life_live.id, edge.id}
    # OR-ed with the old stored-code filter.
    assert ids(delivery_group="problem", ords_status="ACKED") == {refused.id, unknown.id, sent.id, edge.id}
    assert ids(division="life") == {life_held.id, life_live.id}
    assert ids(division="health") == {sent.id, waiting.id, refused.id, unknown.id, edge.id}
    assert ids(source_group="live") == {life_live.id}
    assert life_live.id not in ids(source_group="later")
    assert ids(clock_quality=["DRIFTED", "INVALID"]) == {life_live.id, edge.id}
    assert ids(clock_quality="DRIFTED") == {life_live.id}
    repeated = client.get("/api/v1/attendance", params=[
        ("device_id", life.connector_id), ("device_id", health.connector_id)])
    assert len(repeated.json()["rows"]) == 7
    assert ids(device_id=life.connector_id) == {life_held.id, life_live.id}
    upper = edge.device_event_time.isoformat()
    assert edge.id in ids(to_time=upper)
    assert edge.id not in ids(to_time=upper, to_exclusive="true")
    assert client.get("/api/v1/attendance", params={"delivery_group": "finished"}).status_code == 422
    assert client.get("/api/v1/attendance", params={"division": "both"}).status_code == 422


def test_notify_summary_false_skips_the_phone_message_summary(db):
    connector = connector_fixture(db)
    _make_recovery_event(db, connector, event_number=9201, ords_status="ACKED")
    db.commit()
    client = _client(db)
    assert client.get("/api/v1/attendance").json()["checkin_notifications"] is not None
    assert client.get("/api/v1/attendance", params={"notify_summary": "false"}).json()[
        "checkin_notifications"] is None
