"""Operator actions on device health: resolve with a reason, clear a stale device error."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from test_add_backend import SERIAL, connector_fixture, db as db
from test_device_health import (
    admin_client,
    alert_row,
    heartbeat,
    latch_storage,
    legacy_sample,
    live,
    open_alert,
)
from test_hil_scope import hil_session  # noqa: F401
from test_zkt_factory_trial import factory  # noqa: F401
from zk_add import zkt_factory_trial
from zk_add.device_health import apply_device_health
from zk_add.models import AuditEvent, DeviceAlert
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_hil_schedule import _post_verdict_hold

PASSWORD = "correct-password"


def resolve(client, headers, alert_id, *, reason="Raised by the 2.6.27 recovery image", key="resolve-key-1",
            password=PASSWORD):
    return client.post(f"/api/v1/alerts/{alert_id}/resolve", headers=headers,
                       json={"reason": reason, "password": password, "idempotency_key": key})


def previous_boot_worker_fault(db, connector):
    return open_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", seen=utc_now() - timedelta(hours=6), details={
        "binding": "OBSERVED", "boot_id": "recovery-boot", "firmware_version": "2.6.27",
        "evidence": {"summary": "add_delivery STOPPED"}})


def audits(db, action):
    return db.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()


def test_resolve_requires_csrf_step_up_and_reason(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    row = previous_boot_worker_fault(db, connector)
    client, headers = admin_client(db)
    assert resolve(client, {}, row.id).status_code == 403
    assert resolve(client, headers, row.id, password="wrong-password").status_code == 403
    assert resolve(client, headers, row.id, reason="too short").status_code == 422
    assert resolve(client, headers, row.id, key="short").status_code == 422
    db.refresh(row)
    assert row.state == "OPEN" and audits(db, "ALERT_RESOLVED_BY_OPERATOR") == []


def test_resolve_refuses_current_conditions(db):
    connector = connector_fixture(db)
    sample = legacy_sample()
    sample["storage"] = latch_storage("zone_lite.c:7389")
    heartbeat(db, connector, 1, led="LOCAL_FAILURE", diagnostics=sample)
    db.commit()
    durability = alert_row(db, connector, "ESP_DURABILITY_FAULT")
    client, headers = admin_client(db)
    response = resolve(client, headers, durability.id)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "ALERT_CONDITION_CURRENT"
    assert detail["clear_condition"].startswith("Clears when firmware that reports storage diagnostics")
    heartbeat(db, connector, 2, zkt={"online": True, "connection_state": "ONLINE", "serial": "WRONG"})
    db.commit()
    mismatch = alert_row(db, connector, "ZKT_SERIAL_MISMATCH")
    assert resolve(client, headers, mismatch.id, key="resolve-key-2").json()["detail"]["code"] == (
        "ALERT_CONDITION_CURRENT")
    revoke = open_alert(db, connector, "ADMIN_REVOKE_OVERDUE", severity="CRITICAL")
    assert resolve(client, headers, revoke.id, key="resolve-key-3").json()["detail"]["code"] == "ALERT_WORKFLOW_OWNED"
    done = open_alert(db, connector, "TERMINAL_SOURCE_EXCEPTION", state="RESOLVED", resolved_at=utc_now())
    assert resolve(client, headers, done.id, key="resolve-key-4").json()["detail"]["code"] == "ALERT_NOT_ACTIVE"
    assert resolve(client, headers, 999_999, key="resolve-key-5").status_code == 404
    assert audits(db, "ALERT_RESOLVED_BY_OPERATOR") == []


@pytest.mark.parametrize("enabled", [True, False])
def test_resolve_previous_boot_worker_fault_audited(db, monkeypatch, enabled):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = live(db, connector_fixture(db), firmware="2.5.2", lifecycle="DEGRADED")
    connector.last_error_code = "ESP_DELIVERY_WORKER_FAULT"
    row = previous_boot_worker_fault(db, connector)
    seen = ensure_utc(row.last_seen_at)
    client, headers = admin_client(db)
    response = resolve(client, headers, row.id)
    assert response.status_code == 200, response.text
    body = response.json()
    db.refresh(row)
    db.refresh(connector)
    assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen
    assert row.details["resolution"]["kind"] == "OPERATOR"
    assert row.details["resolution"]["actor"] == "StateHealthAdmin"
    assert body["alert"]["state"] == "RESOLVED" and body["residual_alert_id"] is None
    assert body["device_error"]["code"] is None and connector.last_error_code is None
    # ENFORCED re-derives the lifecycle; SHADOW corrects only the device error.
    assert connector.lifecycle_state == ("ONLINE" if enabled else "DEGRADED")
    audit, = audits(db, "ALERT_RESOLVED_BY_OPERATOR")
    assert audit.request_id == "resolve-key-1" and audit.target_id == str(row.id)
    assert audit.before == {"alert_state": "OPEN", "lifecycle_state": "DEGRADED",
                            "last_error_code": "ESP_DELIVERY_WORKER_FAULT"}
    assert audit.after["reason"] == "Raised by the 2.6.27 recovery image"
    assert audit.after["last_error_code"] is None


def test_resolve_idempotent_by_key_and_key_reuse_rejected(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    row = previous_boot_worker_fault(db, connector)
    other = open_alert(db, connector, "TERMINAL_SOURCE_EXCEPTION")
    client, headers = admin_client(db)
    first = resolve(client, headers, row.id)
    again = resolve(client, headers, row.id)
    assert first.status_code == again.status_code == 200
    assert again.json()["replayed"] is True and again.json()["alert_id"] == row.id
    assert len(audits(db, "ALERT_RESOLVED_BY_OPERATOR")) == 1
    reused = resolve(client, headers, other.id)
    assert reused.status_code == 409 and reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_resolve_durability_leaves_residual_preservation_unverified(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    row = open_alert(db, connector, "ESP_DURABILITY_FAULT", seen=utc_now() - timedelta(days=1), details={
        "binding": "OBSERVED", "boot_id": "earlier-boot", "firmware_version": "2.6.15",
        "evidence": {"summary": "storage DEGRADED; write failures 3"}})
    client, headers = admin_client(db)
    body = resolve(client, headers, row.id, reason="Peshawar storage custody receipts reviewed").json()
    residual = db.get(DeviceAlert, body["residual_alert_id"])
    assert (residual.code, residual.state, residual.severity) == ("ESP_PRESERVATION_UNVERIFIED", "OPEN", "WARNING")
    assert residual.details["source_alert_id"] == row.id
    assert residual.details["evidence"]["summary"] == "storage DEGRADED; write failures 3"
    assert body["device_error"]["code"] == "ESP_PRESERVATION_UNVERIFIED"
    assert body["device_state"] == "ONLINE_WITH_WARNINGS"
    connector.ota_secure_boot = connector.ota_rollback_enabled = True
    hold = _post_verdict_hold(db, connector, run_completed_at=(utc_now() - timedelta(hours=1)).isoformat())
    assert hold == "POST_VERDICT_PERSISTENCE_FAULT"
    # Verified storage from capable firmware clears the residual.
    heartbeat(db, connector, 1, firmware="2.6.15", diagnostics=legacy_sample())
    assert residual.state == "RESOLVED" and residual.details["resolution"]["kind"] == "VERIFIED"


def test_resolved_durability_residual_still_holds_the_factory_trial(factory):  # noqa: F811
    session, release, devices, _ = factory
    device = devices[0]
    open_alert(session, device, "ESP_PRESERVATION_UNVERIFIED", severity="WARNING")
    apply_device_health(session, device, source="READ")
    assert device.last_error_code == "ESP_PRESERVATION_UNVERIFIED"
    with pytest.raises(ValueError, match="FACTORY_TERMINAL_NOT_READY"):
        zkt_factory_trial.predecessor_snapshot(session, release, device)
    device.last_error_code = None
    with pytest.raises(ValueError, match="FACTORY_EXISTING_SAFETY_HOLD"):
        zkt_factory_trial.predecessor_snapshot(session, release, device)


def clear(client, headers, connector, expected, *, key="clear-key-1"):
    return client.post(f"/api/v1/devices/{connector.connector_id}/clear-error", headers=headers, json={
        "expected_code": expected, "reason": "Flapping ended before the device went offline",
        "password": PASSWORD, "idempotency_key": key})


def test_clear_error(db):
    connector = live(db, connector_fixture(db), lifecycle="FLAPPING")
    connector.last_error_code = "ZKT_CONNECTION_FLAPPING"
    flapping = open_alert(db, connector, "ZKT_CONNECTION_FLAPPING", severity="WARNING")
    client, headers = admin_client(db)
    backed = clear(client, headers, connector, "ZKT_CONNECTION_FLAPPING")
    assert backed.status_code == 409
    assert backed.json()["detail"] == {"code": "DEVICE_ERROR_STILL_BACKED",
                                       "message": "An active alert still backs this device error.",
                                       "alert_ids": [flapping.id]}
    changed = clear(client, headers, connector, "ESP_LOCAL_FAILURE", key="clear-key-2")
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "DEVICE_ERROR_CHANGED"
    flapping.state, flapping.resolved_at = "RESOLVED", utc_now()
    db.commit()
    response = clear(client, headers, connector, "ZKT_CONNECTION_FLAPPING", key="clear-key-3")
    assert response.status_code == 200, response.text
    assert response.json()["device_error"]["code"] is None and response.json()["device_state"] == "ONLINE"
    db.refresh(connector)
    assert connector.last_error_code is None
    audit, = audits(db, "DEVICE_ERROR_CLEARED")
    assert audit.before["last_error_code"] == "ZKT_CONNECTION_FLAPPING" and audit.after["last_error_code"] is None
    assert clear(client, headers, connector, "ZKT_CONNECTION_FLAPPING", key="clear-key-3").json()["replayed"]


def test_detail_reports_operator_eligibility_consistently(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    # A legacy row inferred to be from an ended boot is not retired automatically.
    row = open_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", seen=utc_now() - timedelta(hours=6),
                     details={"binding": "INFERRED_PREVIOUS"})
    heartbeat(db, connector, 1, firmware="2.5.2", zkt={"online": True, "connection_state": "ONLINE",
                                                      "serial": SERIAL})
    db.commit()
    client, headers = admin_client(db)
    reason = client.get(f"/api/v1/devices/{connector.connector_id}").json()["health"]["reasons"][0]
    assert (reason["alert_id"], reason["operator"]["resolvable"]) == (row.id, True)
    assert resolve(client, headers, row.id).status_code == 200
