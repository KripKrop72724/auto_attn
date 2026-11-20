"""Problems list: queues, division, filters, counts, names and the close rule (PRB-1, PRB-2)."""

import json
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy import event as sa_event

from test_add_backend import connector_fixture, db as db
from test_device_health import admin_client, open_alert
from test_device_health_actions import resolve
from zk_add import device_health, problem_words, public_status
from zk_add.time_utils import utc_now

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "problem_types.json"
HEALTH_ONLY = {"HEARTBEAT_STALE", "TERMINAL_DISCONNECTED", "TERMINAL_STABILIZING_STALLED"}


def _life(db):
    connector = connector_fixture(db, hardware_id="aa:bb:cc:dd:ee:21", expected_serial="LIFE-SERIAL-21")
    connector.display_name = "LF-ZONE-BLD9-03"
    connector.zone_id = "LF-ZONE-BLD9"
    db.commit()
    return connector


def _health(db):
    connector = connector_fixture(db)
    connector.display_name = "ZONE-PESHAWAR-02"
    connector.zone_id = "ZONE-PESHAWAR"
    db.commit()
    return connector


def _codes(client, **params):
    response = client.get("/api/v1/alerts", params=params)
    assert response.status_code == 200, response.text
    return sorted(row["code"] for row in response.json()["rows"])


def test_seen_and_legacy_queues_counts_and_legacy_by_code(db):
    connector = _health(db)
    open_alert(db, connector, "NEEDS", severity="CRITICAL")
    open_alert(db, connector, "ACKED", acknowledged_at=utc_now())
    open_alert(db, connector, "LEGACY", state="ACKNOWLEDGED", acknowledged_at=utc_now())
    open_alert(db, connector, "LEGACY2", state="ACKNOWLEDGED", acknowledged_at=utc_now())
    open_alert(db, connector, "DONE", state="RESOLVED", resolved_at=utc_now())
    client, _headers = admin_client(db)
    assert _codes(client, queue="SEEN") == ["ACKED"]
    assert _codes(client, queue="LEGACY_SEEN") == ["LEGACY", "LEGACY2"]
    body = client.get("/api/v1/alerts").json()
    # The pinned dicts stay as they are.
    assert body["totals"] == {"all": 5, "open": 2, "acknowledged": 2, "resolved": 1}
    assert body["queue_totals"] == {"needs_action": 1, "acknowledged": 3, "resolved": 1, "all": 5}
    counts = body["counts"]
    assert (counts["open"], counts["seen"], counts["legacy_seen"], counts["fixed"]) == (1, 1, 2, 1)
    assert counts["legacy_by_code"] == {"LEGACY": 1, "LEGACY2": 1}
    # Tab counts ignore the queue.
    assert client.get("/api/v1/alerts", params={"queue": "SEEN"}).json()["counts"]["open"] == 1


def test_division_filter_and_by_division_with_spares_never_counted(db):
    life = _life(db)
    health = _health(db)
    spare = connector_fixture(db, hardware_id="aa:bb:cc:dd:ee:22", expected_serial="SPARE-22")
    spare.display_name = "LF-ZONE-BLD1-01"
    spare.is_spare = True
    db.commit()
    open_alert(db, life, "ESP_OFFLINE", severity="HIGH")
    open_alert(db, health, "TERMINAL_LINK_DOWN", severity="WARNING")
    open_alert(db, spare, "ESP_FATAL", severity="CRITICAL")
    client, _headers = admin_client(db)
    body = client.get("/api/v1/alerts", params={"division": "life"}).json()
    assert [row["code"] for row in body["rows"]] == ["ESP_OFFLINE"]
    assert body["counts"]["open"] == 1
    assert body["counts"]["by_division"]["life"]["open"] == 1
    assert body["counts"]["by_division"]["health"]["open"] == 1
    assert _codes(client, division="health") == ["TERMINAL_LINK_DOWN"]
    assert client.get("/api/v1/alerts", params={"division": "nope"}).status_code == 422
    # Device names equal /status.
    status_names = {
        device["code"]: (device["zone"], device["name"])
        for device in public_status.build_snapshot(
            [(life, "ONLINE", utc_now(), None), (health, "ONLINE", utc_now(), None)], utc_now())["devices"]
    }
    for row in client.get("/api/v1/alerts").json()["rows"]:
        device = row["device"]
        zone, name = status_names[device["display_name"]]
        assert device["division"] == zone
        assert name in device["friendly_name"]


def test_severity_list_dates_and_orders_with_their_own_cursor(db):
    connector = _health(db)
    now = utc_now()
    open_alert(db, connector, "W", severity="WARNING", seen=now - timedelta(hours=3))
    open_alert(db, connector, "M", severity="MEDIUM", seen=now - timedelta(hours=2))
    open_alert(db, connector, "I", severity="INFO", seen=now - timedelta(hours=1))
    open_alert(db, connector, "H", severity="HIGH", seen=now - timedelta(days=3))
    for index, code in enumerate(("F1", "F2", "F3")):
        open_alert(db, connector, code, state="RESOLVED", seen=now - timedelta(days=2),
                   resolved_at=now - timedelta(hours=index + 1))
    client, _headers = admin_client(db)
    assert _codes(client, severity="WARNING,MEDIUM,INFO", queue="NEEDS_ACTION") == ["I", "M", "W"]
    assert _codes(client, severity="HIGH", queue="NEEDS_ACTION") == ["H"]
    assert _codes(client, severity="CRITICAL,HIGH", queue="NEEDS_ACTION") == ["H"]
    assert _codes(client, severity="BOGUS") == []
    day_ago = (now - timedelta(days=1)).isoformat()
    assert _codes(client, queue="NEEDS_ACTION", from_time=day_ago) == ["I", "M", "W"]
    # Fixed filters on when it was fixed, not when it started.
    assert _codes(client, queue="RESOLVED", from_time=(now - timedelta(minutes=150)).isoformat()) == ["F1", "F2"]
    newest = client.get("/api/v1/alerts", params={"queue": "NEEDS_ACTION", "order": "newest"}).json()
    assert [row["code"] for row in newest["rows"]] == ["I", "M", "W", "H"]
    oldest = client.get("/api/v1/alerts", params={"queue": "NEEDS_ACTION", "order": "oldest"}).json()
    assert [row["code"] for row in oldest["rows"]] == ["H", "W", "M", "I"]
    seen, cursor = [], None
    for _ in range(5):
        params = {"queue": "RESOLVED", "order": "fixed_newest", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/alerts", params=params).json()
        seen += [row["code"] for row in page["rows"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ["F1", "F2", "F3"]
    serious_cursor = client.get("/api/v1/alerts", params={"limit": 1}).json()["next_cursor"]
    assert client.get("/api/v1/alerts", params={
        "order": "fixed_newest", "cursor": serious_cursor}).status_code == 422
    counts = client.get("/api/v1/alerts", params={"queue": "NEEDS_ACTION", "severity": "HIGH"}).json()["counts"]
    # Severity chip counts ignore the severity filter.
    assert counts["by_severity"] == {"CRITICAL": 0, "HIGH": 1, "WARNING": 1, "MEDIUM": 1, "INFO": 1}


def test_each_open_row_carries_the_same_close_rule_as_the_resolve_action(db):
    connector = _health(db)
    workflow = open_alert(db, connector, "ORDS_DELIVERY_FAILED", severity="HIGH")
    latched = open_alert(db, connector, "ESP_PRESERVATION_UNVERIFIED", severity="WARNING")
    fixed = open_alert(db, connector, "TERMINAL_SOURCE_EXCEPTION", state="RESOLVED", resolved_at=utc_now())
    client, headers = admin_client(db)
    rows = {row["id"]: row for row in client.get("/api/v1/alerts").json()["rows"]}
    assert rows[workflow.id]["operator"] == {
        "resolvable": False, "refusal_code": "ALERT_WORKFLOW_OWNED",
        "refusal": "Its own review workflow resolves this alert."}
    assert rows[latched.id]["operator"]["refusal_code"] == "ALERT_CONDITION_CURRENT"
    assert "operator" not in rows[fixed.id]
    for alert in (workflow, latched):
        response = resolve(client, headers, alert.id, key=f"resolve-key-{alert.id}")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == rows[alert.id]["operator"]["refusal_code"]
    device_rows = client.get(f"/api/v1/devices/{connector.connector_id}/alerts").json()["rows"]
    assert {row["id"]: row.get("operator") for row in device_rows}[workflow.id] == rows[workflow.id]["operator"]


def test_type_attention_and_search_filters(db):
    life = _life(db)
    health = _health(db)
    open_alert(db, life, "ESP_DURABILITY_FAULT", severity="CRITICAL")
    open_alert(db, health, "TERMINAL_LINK_DOWN", severity="WARNING")
    open_alert(db, health, "ESP_CLOCK_SKEW", severity="WARNING")
    open_alert(db, health, "CHECKIN_NOTIFY_DEGRADED", severity="WARNING")
    client, _headers = admin_client(db)
    assert _codes(client, type="machine") == ["TERMINAL_LINK_DOWN"]
    assert _codes(client, type="link_box,phone") == ["CHECKIN_NOTIFY_DEGRADED", "ESP_CLOCK_SKEW"]
    assert client.get("/api/v1/alerts", params={"type": "bogus"}).status_code == 422
    assert _codes(client, affects_attendance="true") == ["ESP_DURABILITY_FAULT", "TERMINAL_LINK_DOWN"]
    assert _codes(client, q="bld9") == ["ESP_DURABILITY_FAULT"]
    assert _codes(client, q="Building 9") == ["ESP_DURABILITY_FAULT"]
    assert _codes(client, q="memory") == ["ESP_DURABILITY_FAULT"]
    assert _codes(client, q="not responding") == ["TERMINAL_LINK_DOWN"]
    by_type = client.get("/api/v1/alerts", params={"type": "machine"}).json()["counts"]["by_type"]
    # Type counts ignore the type filter.
    assert by_type["machine"] == 1 and by_type["link_box_memory"] == 1 and by_type["phone"] == 1


def test_rows_carry_plain_words_type_and_attendance_flag(db):
    connector = _health(db)
    open_alert(db, connector, "ZKT_CLOCK_DRIFT", severity="WARNING", details={"drift_seconds": -420})
    open_alert(db, connector, "SOMETHING_NEW", severity="INFO")
    client, _headers = admin_client(db)
    rows = {row["code"]: row for row in client.get("/api/v1/alerts").json()["rows"]}
    drift = rows["ZKT_CLOCK_DRIFT"]
    assert drift["plain_title"] == "Machine clock is 7 min slow"
    assert drift["type"] == "machine" and drift["affects_attendance"] is True
    assert drift["message"] == "ZKT_CLOCK_DRIFT"  # the stored message is unchanged
    unknown = rows["SOMETHING_NEW"]
    assert unknown["plain_title"] == "Link box problem"
    assert unknown["type"] is None and unknown["affects_attendance"] is False


def test_every_policy_code_has_words_a_type_and_the_shared_fixture_matches():
    for code in device_health.POLICY:
        assert code in problem_words.GROUP, code
        if code not in HEALTH_ONLY:
            assert problem_words.PROBLEM_TYPE[code] in problem_words.PROBLEM_TYPES, code
        words = problem_words.plain(code, {})
        for value in words.values():
            assert value and "_" not in value, (code, value)
            assert not re.search(r"\b[0-9a-f]{8,}\b", value)
        assert words["plain_detail"].endswith(".") and words["next_step"].endswith(".")
        assert not words["plain_title"].endswith(".")
    assert set(problem_words.GROUP) <= set(device_health.POLICY)
    document = json.loads(FIXTURE.read_text())
    assert document["types"] == list(problem_words.PROBLEM_TYPES)
    assert document["problem_type"] == problem_words.PROBLEM_TYPE
    assert document["affects_attendance"] == sorted(problem_words.AFFECTS_ATTENDANCE)


def test_one_health_evaluation_per_request_not_per_row(db, monkeypatch):
    from zk_add import alert_list

    connector = _health(db)
    for code in ("ESP_OFFLINE", "TERMINAL_LINK_DOWN", "ESP_CLOCK_SKEW", "ZKT_CLOCK_DRIFT"):
        open_alert(db, connector, code, severity="WARNING")
    calls = []
    original = alert_list.evaluate_health_batch
    monkeypatch.setattr(alert_list, "evaluate_health_batch",
                        lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    client, _headers = admin_client(db)
    statements = []
    engine = db.get_bind()

    def record(*_args):
        statements.append(1)

    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get("/api/v1/alerts").status_code == 200
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
    assert calls == [1]
    assert len(statements) < 25
