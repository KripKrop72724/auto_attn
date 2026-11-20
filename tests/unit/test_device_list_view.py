"""Speed, server side: the slim device list, the fleet-only overview and slim live events."""
from __future__ import annotations

import json

import pytest

from test_add_backend import connector_fixture, db as db  # noqa: F401
from test_device_health import admin_client
from zk_add import device_names, web
from zk_add.models import Connector
from zk_add.schemas import DeviceLogIn, Envelope, LogBatchRequest
from zk_add.service import fleet_counts, serialize_connector
from zk_add.time_utils import utc_now

HIKVISION_HEALTH = {"qualification_state": "QUALIFIED", "last_successful_poll_epoch": 1_790_000_000,
                    "stage": "events", "failures": 0, "queued": 0}
CAPABILITIES = {
    "create_user": True, "user_write": True, "admin_lease": True, "delete_user": True, "name_bytes": 24,
    "delete_all_credentials": False, "source_coverage_certified": True, "user_role_write": True,
    "protocol_restart": True, "source_coverage_cursor": 18234, "history_backfill_state": "COMPLETE",
    "hikvision_health": HIKVISION_HEALTH,
}


@pytest.fixture(autouse=True)
def _no_remembered_fleet():
    device_names.forget_fleet()
    yield
    device_names.forget_fleet()


def _heavy(db) -> Connector:
    connector = connector_fixture(db)
    connector.display_name = connector.zone_id = connector.zone_name = "LF-ZONE-BLD9-03"
    connector.firmware_family = "hikvision"
    connector.firmware_diagnostics = {"memory": {"heap": [i for i in range(400)]}, "storage": "x" * 4000}
    connector.zkt_device.capability_profile = dict(CAPABILITIES)
    db.commit()
    return connector


def test_list_view_drops_only_the_heavy_fields(db):
    connector = _heavy(db)
    client, _headers = admin_client(db)
    full = client.get("/api/v1/devices").json()["rows"][0]
    slim = client.get("/api/v1/devices?view=list").json()["rows"][0]
    assert "payload" not in full and full["firmware_diagnostics"] is not None
    assert slim["payload"] == "list"
    assert slim["firmware_diagnostics"] is None
    assert "hikvision_health" not in slim["zkt"]["capabilities"]
    assert slim["hikvision"] == HIKVISION_HEALTH  # top-level copy kept
    # Every capability key read before the device page loads is kept.
    assert {key: value for key, value in CAPABILITIES.items() if key != "hikvision_health"} == slim["zkt"]["capabilities"]
    # Every other field passes through unchanged (snapshot_at is the serialize time).
    for key in set(full) - {"firmware_diagnostics", "zkt", "snapshot_at", "payload"}:
        assert slim[key] == full[key], key
    assert {key: value for key, value in slim["zkt"].items() if key != "capabilities"} == {
        key: value for key, value in full["zkt"].items() if key != "capabilities"}
    # The stored profile is untouched.
    db.refresh(connector.zkt_device)
    assert "hikvision_health" in connector.zkt_device.capability_profile


def test_list_view_rows_are_small(db):
    _heavy(db)
    client, _headers = admin_client(db)
    full = client.get("/api/v1/devices").json()["rows"][0]
    slim = client.get("/api/v1/devices?view=list").json()["rows"][0]
    size = len(json.dumps(slim, separators=(",", ":")))
    assert size < len(json.dumps(full, separators=(",", ":"))) / 2
    assert size <= 4_096, size


def test_list_view_is_the_default_shape_for_old_clients(db):
    _heavy(db)
    client, _headers = admin_client(db)
    assert client.get("/api/v1/devices?view=other").status_code == 422
    assert "payload" not in client.get("/api/v1/devices?view=full").json()["rows"][0]


def test_heartbeat_event_row_is_the_list_shape(db):
    connector = _heavy(db)
    from zk_add.schemas import HeartbeatPayload
    from zk_add.service import update_heartbeat

    connector.firmware_family = "zkt"
    db.commit()
    row = update_heartbeat(db, connector=connector, boot_id="slim-boot", sequence=1, payload=HeartbeatPayload(
        rssi=-50, free_heap=100_000, uptime_seconds=100, outbox_depth=0, current_activity="LIVE",
        led_state="GREEN"))
    assert row["payload"] == "list"
    assert row["firmware_diagnostics"] is None
    assert row["connector_id"] == connector.connector_id and "health" in row and "snapshot_at" in row
    connector.firmware_diagnostics = {"memory": "kept"}
    assert serialize_connector(connector)["firmware_diagnostics"] == {"memory": "kept"}


def test_overview_fleet_section_never_computes_the_cached_figures(db, monkeypatch):
    connector_fixture(db)
    client, _headers = admin_client(db)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("sections=fleet must not wait for the dashboard figures")

    monkeypatch.setattr(web, "dashboard_metrics", forbidden)
    monkeypatch.setattr(web, "_dashboard_metrics", forbidden)
    response = client.get("/api/v1/overview?sections=fleet")
    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == ["fleet"]
    assert {key: value for key, value in body.items() if key != "sections"} == fleet_counts(db)
    for key in ("total", "open_unacknowledged_alerts", "spares", "open_alerts", "active_leases"):
        assert key in body
    assert "ords_delivery" not in body and "metrics_computed_at" not in body
    assert client.get("/api/v1/overview?sections=other").status_code == 422


def test_overview_fleet_section_answers_while_the_figures_are_unavailable(db, monkeypatch):
    connector_fixture(db)
    client, _headers = admin_client(db)
    monkeypatch.setattr(web, "_dashboard_cache_applies", lambda _db: True)

    def unavailable(*_args, **_kwargs):
        raise web.dashboard_cache.DashboardCacheUnavailable("cold")

    monkeypatch.setattr(web.dashboard_cache, "get_or_compute_entry", unavailable)
    assert client.get("/api/v1/overview").status_code == 503
    assert client.get("/api/v1/overview?sections=fleet").status_code == 200


def _log(level="ERROR", code="ZKT_SERIAL_MISMATCH", sequence=1):
    return DeviceLogIn(boot_id="boot", sequence=sequence, level=level, subsystem="zkt", code=code,
                       message="CNIC 3520212345671 user 7 secret", context={"cnic": "3520212345671"},
                       device_time=utc_now())


def test_http_log_batch_events_carry_no_text(db):
    connector = connector_fixture(db)
    _response, events = web.persist_device_logs(LogBatchRequest(logs=[_log(), _log("INFO", None, 2)]),
                                                (db, connector))
    assert events == [
        ("log", {"connector_id": connector.connector_id, "level": "ERROR", "code": "ZKT_SERIAL_MISMATCH"}),
        ("log", {"connector_id": connector.connector_id, "level": "INFO", "code": None}),
    ]


def test_websocket_log_event_carries_no_text(db, monkeypatch):
    from contextlib import contextmanager

    connector = connector_fixture(db)

    @contextmanager
    def scope():
        yield db
        db.commit()

    monkeypatch.setattr(web, "session_scope", scope)
    envelope = Envelope(message_id="slim-log-1", connector_id=connector.connector_id, boot_id="ws-boot", seq=1,
                        sent_at=utc_now(), type="log",
                        payload={"level": "WARN", "subsystem": "zkt", "code": "ZKT_CONNECTION_LOST",
                                 "message": "CNIC 3520212345671", "context": {"secret": "x"}})
    outcome = web.persist_envelope(connector.id, envelope)
    assert outcome.event == {"connector_id": connector.connector_id, "level": "WARN",
                             "code": "ZKT_CONNECTION_LOST"}
