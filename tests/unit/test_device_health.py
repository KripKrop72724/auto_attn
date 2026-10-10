"""Device health: alert lifecycle, derived tiers and their evidence rules."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_add_backend import connector_fixture, db as db
from zk_add.models import AuditEvent, DeviceAlert
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.service import fleet_counts, resolve_alert, upsert_alert, utc_now
from zk_add.web import app, get_db


def admin_client(db):
    raw_session, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client, {"X-CSRF-Token": admin.csrf_token}


def open_alert(db, connector, code="ESP_LOCAL_FAILURE", *, severity="HIGH", seen=None, **fields):
    now = seen or utc_now()
    row = DeviceAlert(
        connector_id=connector.id, code=code, severity=severity, state=fields.pop("state", "OPEN"),
        message=fields.pop("message", code), details=fields.pop("details", {}),
        first_seen_at=now, last_seen_at=now, **fields,
    )
    db.add(row)
    db.commit()
    return row


def test_acknowledge_keeps_alert_open_and_records_actor(db):
    connector = connector_fixture(db)
    row = open_alert(db, connector)
    client, headers = admin_client(db)
    response = client.post(f"/api/v1/alerts/{row.id}/acknowledge", json={"note": "seen"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "OPEN"
    assert body["acknowledged_at"] is not None
    assert body["acknowledged_by"] == "StateHealthAdmin"
    db.refresh(row)
    assert row.state == "OPEN" and row.details["acknowledgement_note"] == "seen"
    audits = db.scalars(select(AuditEvent).where(AuditEvent.action == "ALERT_ACKNOWLEDGED")).all()
    assert len(audits) == 1


def test_upsert_after_acknowledge_refreshes_the_same_row(db):
    connector = connector_fixture(db)
    row = open_alert(db, connector, seen=utc_now() - timedelta(minutes=5))
    client, headers = admin_client(db)
    assert client.post(f"/api/v1/alerts/{row.id}/acknowledge", json={}, headers=headers).status_code == 200
    before = row.last_seen_at
    upsert_alert(db, connector, code="ESP_LOCAL_FAILURE", severity="HIGH", message="again",
                 details={"led_state": "LOCAL_FAILURE"})
    db.commit()
    rows = db.scalars(select(DeviceAlert).where(DeviceAlert.code == "ESP_LOCAL_FAILURE")).all()
    assert len(rows) == 1 and rows[0].state == "OPEN"
    assert rows[0].details["acknowledged_by"] == "StateHealthAdmin"
    assert rows[0].details["led_state"] == "LOCAL_FAILURE"
    assert rows[0].last_seen_at > before


def test_resolve_alert_closes_acknowledged_open_rows_and_records_resolution(db):
    connector = connector_fixture(db)
    row = open_alert(db, connector, acknowledged_at=utc_now(), details={"acknowledged_by": "x"})
    assert resolve_alert(db, connector, code="ESP_LOCAL_FAILURE",
                         resolution={"kind": "CONDITION_CLEARED"}) == 1
    db.commit()
    db.refresh(row)
    assert row.state == "RESOLVED"
    assert row.details["resolution"]["kind"] == "CONDITION_CLEARED"
    assert row.details["resolution"]["at"]
    assert resolve_alert(db, connector, code="ESP_LOCAL_FAILURE") == 0


def test_acknowledge_rejects_resolved_and_is_idempotent_for_legacy_acknowledged(db):
    connector = connector_fixture(db)
    resolved = open_alert(db, connector, "A", state="RESOLVED", resolved_at=utc_now())
    legacy = open_alert(db, connector, "B", state="ACKNOWLEDGED", acknowledged_at=utc_now())
    client, headers = admin_client(db)
    response = client.post(f"/api/v1/alerts/{resolved.id}/acknowledge", json={}, headers=headers)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ALERT_NOT_ACTIVE"
    response = client.post(f"/api/v1/alerts/{legacy.id}/acknowledge", json={}, headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "ACKNOWLEDGED"
    assert client.post("/api/v1/alerts/999999/acknowledge", json={}, headers=headers).status_code == 404


def test_resolve_without_touch_keeps_alert_out_of_later_hil_window(db):
    connector = connector_fixture(db)
    old = utc_now() - timedelta(hours=3)
    row = open_alert(db, connector, seen=old)
    resolve_alert(db, connector, code="ESP_LOCAL_FAILURE", resolution={"kind": "BOOT_ENDED"},
                  touch_last_seen=False)
    db.commit()
    window_start = utc_now() - timedelta(minutes=15)
    # The HIL evidence window selects alerts by last_seen_at (hil_observation.py).
    in_window = db.scalars(select(DeviceAlert).where(
        DeviceAlert.connector_id == connector.id, DeviceAlert.last_seen_at >= window_start)).all()
    assert row not in in_window


def test_alert_queue_filters_and_queue_totals(db):
    connector = connector_fixture(db)
    open_alert(db, connector, "NEEDS", severity="CRITICAL")
    open_alert(db, connector, "ACKED", acknowledged_at=utc_now())
    open_alert(db, connector, "LEGACY", state="ACKNOWLEDGED", acknowledged_at=utc_now())
    open_alert(db, connector, "DONE", state="RESOLVED", resolved_at=utc_now())
    client, _headers = admin_client(db)
    codes = lambda queue: sorted(r["code"] for r in client.get("/api/v1/alerts", params={"queue": queue}).json()["rows"])
    assert codes("NEEDS_ACTION") == ["NEEDS"]
    assert codes("ACKNOWLEDGED") == ["ACKED", "LEGACY"]
    assert codes("ACTIVE") == ["ACKED", "LEGACY", "NEEDS"]
    assert codes("RESOLVED") == ["DONE"]
    assert codes("ALL") == ["ACKED", "DONE", "LEGACY", "NEEDS"]
    body = client.get("/api/v1/alerts").json()
    assert body["totals"] == {"all": 4, "open": 2, "acknowledged": 1, "resolved": 1}
    assert body["queue_totals"] == {"needs_action": 1, "acknowledged": 2, "resolved": 1, "all": 4}
    assert client.get("/api/v1/alerts", params={"queue": "BOGUS"}).status_code == 422
    device = client.get(f"/api/v1/devices/{connector.connector_id}/alerts", params={"queue": "NEEDS_ACTION"})
    assert [r["code"] for r in device.json()["rows"]] == ["NEEDS"]


def test_fleet_counts_open_unacknowledged_alerts(db):
    connector = connector_fixture(db)
    open_alert(db, connector, "ONE")
    open_alert(db, connector, "TWO", acknowledged_at=utc_now())
    counts = fleet_counts(db)
    assert counts["open_alerts"] == 2
    assert counts["open_unacknowledged_alerts"] == 1
