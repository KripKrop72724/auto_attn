"""GET /devices/{id}/activity (plain, folded) and the /logs filters."""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_add_backend import connector_fixture, db as db  # noqa: F401
from test_device_health import admin_client
from zk_add import device_names, web
from zk_add.models import DeviceAlert, DeviceCommand, DeviceLog
from zk_add.time_utils import ensure_utc, utc_now

NOW = utc_now().replace(microsecond=0)


@pytest.fixture(autouse=True)
def _clean():
    device_names.forget_fleet()
    yield
    device_names.forget_fleet()


class _Logs:
    def __init__(self, db, connector):
        self.db, self.connector, self.sequence = db, connector, 0

    def add(self, code, level="INFO", *, minutes_ago, message="", subsystem="zkt", context=None):
        self.sequence += 1
        row = DeviceLog(connector_id=self.connector.id, boot_id="activity-boot", sequence=self.sequence,
                        level=level, subsystem=subsystem, code=code, message=message, context=context or {},
                        device_time=NOW - timedelta(minutes=minutes_ago),
                        received_at=NOW - timedelta(minutes=minutes_ago))
        self.db.add(row)
        self.db.flush()
        return row


def _setup(db):
    connector = connector_fixture(db)
    return connector, _Logs(db, connector)


def _activity(client, connector, query=""):
    response = client.get(f"/api/v1/devices/{connector.connector_id}/activity{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_routine_runs_fold_with_exact_counts(db):
    connector, logs = _setup(db)
    for minute in range(60, 0, -1):
        logs.add("ZKT_TIME_SYNC_OK", minutes_ago=minute, message="verified_pst=2026-11-05T10:00:00")
    db.commit()
    client, _headers = admin_client(db)
    body = _activity(client, connector)
    assert len(body["items"]) == 1
    group = body["items"][0]
    assert (group["type"], group["count"], group["kind"], group["topic"]) == ("group", 60, "routine", "machine")
    assert group["title"] == "Set the machine's clock to Pakistan time 60 times. All fine."
    assert body["counts"]["routine"] == 60 and body["counts"]["problems"] == 0
    assert body["retention_days"] == 14 and body["newest_id"] == group["last_id"]


def test_six_lost_connections_in_30_minutes_escalate(db):
    connector, logs = _setup(db)
    for minute in (30, 25, 20, 15, 10, 5):
        logs.add("ZKT_CONNECTION_LOST", "WARN", minutes_ago=minute, message="established ZKT session disconnected")
        logs.add("ZKT_TIME_SYNC_OK", minutes_ago=minute - 1)
    db.commit()
    client, _headers = admin_client(db)
    items = _activity(client, connector)["items"]
    escalations = [item for item in items if item["type"] == "escalation"]
    assert len(escalations) == 1
    assert escalations[0]["count"] == 6 and escalations[0]["kind"] == "problem"
    assert escalations[0]["title"] == "Lost the connection to the machine 6 times in 30 minutes."
    assert not [item for item in items if item.get("code") == "ZKT_CONNECTION_LOST" and item["type"] != "escalation"]
    problems = _activity(client, connector, "?show=problems")["items"]
    assert [item["type"] for item in problems] == ["escalation"]


def test_a_fix_line_marks_the_failure_fixed(db):
    connector, logs = _setup(db)
    lost = logs.add("ZKT_CONNECTION_LOST", "WARN", minutes_ago=10)
    stable = logs.add("ZKT_STABLE", minutes_ago=8)
    db.commit()
    client, _headers = admin_client(db)
    items = {item["id"]: item for item in _activity(client, connector)["items"] if item["type"] == "event"}
    assert items[lost.id]["fixed_at"] is not None
    assert items[lost.id]["kind"] == "retried" and items[stable.id]["kind"] == "good"
    assert items[lost.id]["raw"] == web.serialize_log(lost) | {
        "device_time": items[lost.id]["raw"]["device_time"], "received_at": items[lost.id]["raw"]["received_at"]}


def test_show_problems_and_topics(db):
    connector, logs = _setup(db)
    logs.add("ZKT_TIME_SYNC_OK", minutes_ago=20)
    serial = logs.add("ZKT_SERIAL_MISMATCH", "CRITICAL", minutes_ago=15)
    logs.add("ORACLE_RECONCILE_DELEGATED_TO_ADD", minutes_ago=10, subsystem="ords")
    review = logs.add("ATTENDANCE_TIMESTAMP_QUARANTINED", "WARN", minutes_ago=5)
    db.commit()
    client, _headers = admin_client(db)
    problems = _activity(client, connector, "?show=problems")["items"]
    assert {item["id"] for item in problems} == {serial.id, review.id}
    assert next(item for item in problems if item["id"] == serial.id)["serious"] is True
    hr = _activity(client, connector, "?topic=hr_system")["items"]
    assert [item["code"] for item in hr] == ["ORACLE_RECONCILE_DELEGATED_TO_ADD"]
    both = _activity(client, connector, "?topic=hr_system&topic=machine")["items"]
    assert len(both) == 4
    counts = _activity(client, connector)["counts"]
    assert counts["problems"] == 2 and counts["by_topic"]["machine"] == 2


def test_since_until_and_paging_have_no_gaps_or_repeats(db):
    connector, logs = _setup(db)
    rows = []
    for minute in range(90, 0, -1):
        code = "ZKT_TIME_SYNC_OK" if minute % 3 else "ZKT_SERIAL_MISMATCH"
        rows.append(logs.add(code, "CRITICAL" if code == "ZKT_SERIAL_MISMATCH" else "INFO", minutes_ago=minute))
    db.commit()
    client, _headers = admin_client(db)
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        query = "?limit=7" + (f"&before={cursor}" if cursor else "")
        body = _activity(client, connector, query)
        for item in body["items"]:
            if item["type"] == "event":
                seen.append(item["id"])
            else:
                seen.extend(range(item["first_id"], item["last_id"] + 1))
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages > 1
    assert sorted(seen) == sorted(row.id for row in rows) and len(seen) == len(set(seen))

    since = (NOW - timedelta(minutes=30)).isoformat()
    until = (NOW - timedelta(minutes=10)).isoformat()
    window = _activity(client, connector, f"?since={since.replace('+', '%2B')}&until={until.replace('+', '%2B')}")
    ids = {item["id"] for item in window["items"] if item["type"] == "event"}
    ids |= {i for item in window["items"] if item["type"] == "group" for i in range(item["first_id"],
                                                                                   item["last_id"] + 1)}
    assert ids == {row.id for row in rows
                   if NOW - timedelta(minutes=30) <= ensure_utc(row.received_at) < NOW - timedelta(minutes=10)}


# limit 3: the burst's newest row starts a page, so the cut moves on past it;
# limit 4: the cut would fall just after it, so the cut moves back before it.
@pytest.mark.parametrize("limit", [3, 4])
def test_a_page_cut_never_splits_an_escalation_burst(db, limit):
    connector, logs = _setup(db)
    lost, other = [], []
    for minute in (29, 23, 17, 11, 5):  # five lost connections in 30 minutes, each followed by two problems
        lost.append(logs.add("ZKT_CONNECTION_LOST", "WARN", minutes_ago=minute))
        other.append(logs.add("ZKT_SERIAL_MISMATCH", "CRITICAL", minutes_ago=minute - 1))
        other.append(logs.add("ZKT_SERIAL_MISMATCH", "CRITICAL", minutes_ago=minute - 2))
    other.append(logs.add("ZKT_SERIAL_MISMATCH", "CRITICAL", minutes_ago=1))
    db.commit()
    client, _headers = admin_client(db)
    lost_ids = {row.id for row in lost}
    shown_lost: list[int] = []
    shown_other: list[int] = []
    cursor, pages = None, 0
    while True:
        body = _activity(client, connector, f"?limit={limit}" + (f"&before={cursor}" if cursor else ""))
        for item in body["items"]:
            if item["type"] == "escalation":
                shown_lost.extend(i for i in range(item["first_id"], item["last_id"] + 1) if i in lost_ids)
                assert item["count"] == 5
            elif item["type"] == "event" and item["id"] in lost_ids:
                shown_lost.append(item["id"])
            elif item["type"] == "event":
                shown_other.append(item["id"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages > 1
    assert sorted(shown_lost) == sorted(lost_ids)  # each lost connection once, none twice, none lost
    assert sorted(shown_other) == sorted(row.id for row in other)


def test_after_returns_only_newer_rows(db):
    connector, logs = _setup(db)
    old = logs.add("ZKT_SERIAL_MISMATCH", "CRITICAL", minutes_ago=10)
    new = logs.add("ZKT_SERIAL_CHANGED", "CRITICAL", minutes_ago=5)
    db.commit()
    client, _headers = admin_client(db)
    items = _activity(client, connector, f"?after={old.id}")["items"]
    assert [item["id"] for item in items if item["type"] == "event"] == [new.id]


def test_requests_and_problem_events_are_merged_without_payloads(db):
    connector, logs = _setup(db)
    logs.add("ZKT_TIME_SYNC_OK", minutes_ago=50)
    db.add(DeviceCommand(command_id="activity-cmd-1", connector_id=connector.id, command_type="REFRESH_USERS",
                         payload_encrypted="secret-payload", expected_state_encrypted="x", desired_state_encrypted="y",
                         idempotency_key="activity-1", status="SUCCEEDED", actor="StateHealthAdmin",
                         created_at=NOW - timedelta(minutes=30)))
    db.add(DeviceAlert(connector_id=connector.id, code="ESP_DURABILITY_FAULT", severity="CRITICAL", state="OPEN",
                       message="internal text", details={}, first_seen_at=NOW - timedelta(minutes=20),
                       last_seen_at=NOW - timedelta(minutes=1)))
    db.commit()
    client, _headers = admin_client(db)
    response = client.get(f"/api/v1/devices/{connector.connector_id}/activity")
    assert "secret-payload" not in response.text and "internal text" not in response.text
    items = response.json()["items"]
    action = next(item for item in items if item["type"] == "action")
    assert action["title"] == "StateHealthAdmin refreshed the people list. Confirmed."
    problem = next(item for item in items if item["type"] == "problem_event")
    assert problem["title"] == "Problem started: Link box cannot save attendance safely."
    assert problem["topic"] == "storage"
    assert [item["type"] for item in items] == ["problem_event", "action", "event"]


def test_after_only_adds_requests_and_problem_events_newer_than_the_shown_row(db):
    connector, logs = _setup(db)
    db.add(DeviceCommand(command_id="activity-cmd-old", connector_id=connector.id, command_type="REFRESH_USERS",
                         payload_encrypted="p", expected_state_encrypted="x", desired_state_encrypted="y",
                         idempotency_key="activity-old", status="SUCCEEDED", actor="StateHealthAdmin",
                         created_at=NOW - timedelta(minutes=30)))
    shown = logs.add("ZKT_TIME_SYNC_OK", minutes_ago=10)
    db.add(DeviceCommand(command_id="activity-cmd-new", connector_id=connector.id, command_type="REFRESH_USERS",
                         payload_encrypted="p", expected_state_encrypted="x", desired_state_encrypted="y",
                         idempotency_key="activity-new", status="SUCCEEDED", actor="StateHealthAdmin",
                         created_at=NOW - timedelta(minutes=5)))
    db.commit()
    client, _headers = admin_client(db)
    everything = _activity(client, connector)["items"]
    assert {item["command_id"] for item in everything if item["type"] == "action"} == {
        "activity-cmd-old", "activity-cmd-new"}
    live = _activity(client, connector, f"?after={shown.id}")["items"]
    assert [item["command_id"] for item in live if item["type"] == "action"] == ["activity-cmd-new"]
    # An unknown row adds no requests at all (nothing to anchor them to).
    assert not [item for item in _activity(client, connector, "?after=999999999")["items"] if item["type"] == "action"]


def test_activity_needs_a_known_device_and_a_valid_cursor(db):
    connector, _logs = _setup(db)
    client, _headers = admin_client(db)
    assert client.get("/api/v1/devices/nope/activity").status_code == 404
    assert client.get(f"/api/v1/devices/{connector.connector_id}/activity?before=bad|x").status_code == 422
    assert client.get(f"/api/v1/devices/{connector.connector_id}/activity?limit=101").status_code == 422


def test_activity_is_admin_only(db):
    connector, _logs = _setup(db)
    from fastapi.testclient import TestClient

    from zk_add.web import app, get_db

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    assert TestClient(app).get(f"/api/v1/devices/{connector.connector_id}/activity").status_code == 401


def test_logs_default_answer_is_unchanged(db):
    connector, logs = _setup(db)
    for minute in range(5, 0, -1):
        logs.add("ZKT_TIME_SYNC_OK", "INFO", minutes_ago=minute)
    db.commit()
    client, _headers = admin_client(db)
    body = client.get(f"/api/v1/devices/{connector.connector_id}/logs?limit=2").json()
    assert set(body) == {"rows", "next_cursor"}
    assert list(body["rows"][0]) == ["id", "boot_id", "sequence", "level", "subsystem", "code", "message",
                                     "context", "device_time", "received_at"]
    assert body["next_cursor"] == body["rows"][1]["id"]
    assert client.get(f"/api/v1/devices/{connector.connector_id}/logs?limit=1000").status_code == 200


def test_logs_filters(db):
    connector, logs = _setup(db)
    warn = logs.add("ZKT_CONNECTION_LOST", "WARN", minutes_ago=30)
    error = logs.add("ZKT_SERIAL_MISMATCH", "ERROR", minutes_ago=20)
    logs.add("ZKT_TIME_SYNC_OK", "INFO", minutes_ago=10)
    db.commit()
    client, _headers = admin_client(db)
    base = f"/api/v1/devices/{connector.connector_id}/logs"

    def ids(query):
        return [row["id"] for row in client.get(base + query).json()["rows"]]

    assert ids("?level=WARN") == [warn.id]
    assert ids("?level=warn&level=ERROR") == [error.id, warn.id]
    assert ids("?level=WARN,ERROR") == [error.id, warn.id]
    assert ids("?code=ZKT_SERIAL_MISMATCH") == [error.id]
    since = (NOW - timedelta(minutes=25)).isoformat().replace("+", "%2B")
    until = (NOW - timedelta(minutes=15)).isoformat().replace("+", "%2B")
    assert ids(f"?since={since}&until={until}") == [error.id]
