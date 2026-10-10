"""Device health: alert lifecycle, derived tiers and their evidence rules."""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from test_add_backend import connector_fixture, db as db
from zk_add import device_health
from zk_add.device_health import POLICY, alert_currency, evaluate_health, gate_effect, shadow_report, terminal_link
from zk_add.models import AuditEvent, DeviceAlert
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.service import fleet_counts, resolve_alert, upsert_alert, utc_now
from zk_add.settings import settings
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
    def codes(queue):
        return sorted(r["code"] for r in client.get("/api/v1/alerts", params={"queue": queue}).json()["rows"])

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


def live(db, connector, *, state="ONLINE", firmware="2.6.15", lifecycle="ONLINE", transition_age=30,
         offline_age=None, heartbeat_age=5, family="zkt", poll_error=None):
    """A connected ESP whose last accepted heartbeat reported the given terminal state."""
    now = utc_now()
    connector.connected, connector.lifecycle_state = True, lifecycle
    connector.last_seen_at = now - timedelta(seconds=min(heartbeat_age, 5))
    connector.firmware_version, connector.firmware_family, connector.boot_id = firmware, family, "boot-b"
    zkt = connector.zkt_device
    zkt.connection_state = state
    zkt.last_seen_at = now - timedelta(seconds=heartbeat_age)
    zkt.last_transition_at = now - timedelta(seconds=transition_age)
    zkt.offline_since = None if offline_age is None else now - timedelta(seconds=offline_age)
    if poll_error is not None:
        zkt.capability_profile = {"hikvision_health": {"poll_error": poll_error}}
    db.commit()
    return connector


def second_connector(db, index=1):
    return connector_fixture(db, hardware_id=f"e0:72:a1:00:00:{index:02x}", expected_serial=f"SERIAL{index}")


@pytest.mark.parametrize("state,transition_age,offline_age,expected,reason", [
    ("ONLINE", 30, None, "CONNECTED", "LINK_UP"),
    ("RECOVERING", 599, None, "STABILIZING", "STABILIZING"),
    ("RECOVERING", 601, None, "STABILIZING", "STABILIZING_STALLED"),
    ("SESSION_REFRESH", 599, None, "MAINTENANCE", "SESSION_REFRESH"),
    ("RESTARTING", 601, 601, "DISCONNECTED", "MAINTENANCE_OVERRAN"),
    ("BOOTING", 10, 10, "STARTING", "BOOTING"),
    ("SUSPECT", 10, 299, "RECONNECTING", "SUSPECT"),
    ("CONNECTING", 10, 299, "RECONNECTING", "CONNECTING"),
    ("DISCOVERING", 10, 299, "RECONNECTING", "DISCOVERING"),
    ("RETRY_WAIT", 10, 301, "DISCONNECTED", "RETRY_WAIT"),
    ("OFFLINE", 10, 301, "DISCONNECTED", "OFFLINE"),
    ("FLAPPING", 10, 10, "FLAPPING", "FLAPPING"),
    ("SOMETHING_NEW", 10, 10, "UNKNOWN", "SOMETHING_NEW"),
])
def test_terminal_link_table_for_zkt(db, state, transition_age, offline_age, expected, reason):
    connector = live(db, connector_fixture(db), state=state, transition_age=transition_age,
                     offline_age=offline_age)
    link = terminal_link(connector)
    assert (link["state"], link["reason"], link["raw_state"]) == (expected, reason, state)
    assert link["message"]


@pytest.mark.parametrize("state,poll_error,offline_age,expected,reason", [
    ("ONLINE", 0, None, "CONNECTED", "LINK_UP"),
    ("ONLINE", 1, None, "ERROR", "HIK_CONFIGURATION"),
    ("ONLINE", 3, None, "ERROR", "HIK_AUTH"),
    ("ONLINE", 4, None, "ERROR", "HIK_HTTP_STATUS"),
    ("ONLINE", 5, None, "ERROR", "HIK_OVERSIZED"),
    ("ONLINE", 6, None, "ERROR", "HIK_SOURCE_CHANGED"),
    ("OFFLINE", 7, 30, "ERROR", "HIK_INVALID_RESPONSE"),
    ("OFFLINE", 2, 30, "RECONNECTING", "HIK_NETWORK"),
    ("OFFLINE", 2, 301, "DISCONNECTED", "HIK_NETWORK"),
    ("OFFLINE", 0, 301, "DISCONNECTED", "OFFLINE"),
    ("ONLINE", 8, None, "CONNECTED", "LINK_UP"),
    ("OFFLINE", 8, 30, "RECONNECTING", "OFFLINE"),
])
def test_terminal_link_table_for_hikvision(db, state, poll_error, offline_age, expected, reason):
    connector = live(db, connector_fixture(db), state=state, family="hikvision", firmware="3.1.0",
                     offline_age=offline_age, poll_error=poll_error)
    link = terminal_link(connector)
    assert (link["state"], link["reason"]) == (expected, reason)


def test_terminal_link_hikvision_starting_and_esp_disconnected(db):
    connector = live(db, connector_fixture(db), state="OFFLINE", family="hikvision", firmware="3.1.0",
                     offline_age=30, poll_error=0)
    assert terminal_link(connector, uptime_seconds=60)["state"] == "STARTING"
    connector.connected = False
    link = terminal_link(connector)
    assert (link["state"], link["reason"]) == ("UNKNOWN", "ESP_DISCONNECTED")


# upsert_alert calls whose code is computed; each name lists every code it can hold.
DYNAMIC_ALERT_CODES = {
    "clock_alert": {"ZKT_CLOCK_DRIFT", "HIK_CLOCK_DRIFT"},
    "code": {"ESP_FATAL", "ESP_LOCAL_FAILURE", "ESP_DURABILITY_FAULT", "ESP_DELIVERY_WORKER_FAULT"},
}


def test_policy_covers_every_upserted_code():
    literal, dynamic = set(), set()
    for path in sorted(Path(device_health.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            code = next((kw.value for kw in node.keywords if kw.arg == "code"), None)
            if name == "upsert_alert" and code is not None:
                if isinstance(code, ast.Constant):
                    literal.add(code.value)
                else:
                    dynamic.add(ast.unparse(code))
    assert dynamic <= set(DYNAMIC_ALERT_CODES), dynamic
    assert literal | set().union(*DYNAMIC_ALERT_CODES.values()) <= set(POLICY)
    for code, policy in POLICY.items():
        assert policy.tier in {"DEGRADED", "WARNING", "RULE", None}, code
        assert policy.currency in {"DIAGNOSTICS", "LED", "POSITIVE", "REJECTION", "TRANSPORT", "LATCHED"}
        assert policy.clear_condition.endswith("."), code
        # Codes without a health effect can never create a HIL or factory hold.
        assert not (policy.tier is None and policy.gating), code


@pytest.mark.parametrize("code,severity", [
    ("ESP_OFFLINE", "HIGH"), ("ATTENDANCE_TIMESTAMP_QUARANTINED", "HIGH"),
    ("TERMINAL_SOURCE_EXCEPTION", "HIGH"), ("ADD_SOURCE_COVERAGE_INVALIDATED", "CRITICAL"),
    ("ADMIN_REVOKE_OVERDUE", "CRITICAL"), ("SOMETHING_UNKNOWN", "CRITICAL"),
])
def test_data_codes_and_esp_offline_never_gate(db, code, severity):
    connector = live(db, connector_fixture(db))
    open_alert(db, connector, code, severity=severity)
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.last_error_code, health.reasons) == ("ONLINE", None, [])
    assert [reason.code for reason in health.other_active_alerts] == [code]


def test_gating_warnings_set_last_error(db):
    connector = live(db, connector_fixture(db), state="FLAPPING")
    open_alert(db, connector, "ZKT_CONNECTION_FLAPPING", severity="WARNING")
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", "ZKT_CONNECTION_FLAPPING")
    assert health.terminal_link["state"] == "FLAPPING"
    other = live(db, second_connector(db))
    open_alert(db, other, "OTA_DEVICE_REPORTED_FAILURE", details={"error_code": "DOWNLOAD_BEGIN_FAILED"})
    health = evaluate_health(db, other)
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", "OTA_DOWNLOAD_BEGIN_FAILED")
    assert health.reasons[0].currency == "LATCHED" and health.reasons[0].operator["resolvable"] is True


def test_quarantine_overrides_offline_and_other_reasons(db):
    connector = live(db, connector_fixture(db), state="FLAPPING")
    open_alert(db, connector, "ZKT_CONNECTION_FLAPPING", severity="WARNING")
    open_alert(db, connector, "QUARANTINED_DUPLICATE_SERIAL", severity="CRITICAL")
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.last_error_code) == (
        "QUARANTINED_DUPLICATE_SERIAL", "QUARANTINED_DUPLICATE_SERIAL")
    connector.connected = False
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.tier) == ("QUARANTINED_DUPLICATE_SERIAL", "OFFLINE")


@pytest.mark.parametrize("stored", ["OFFLINE", "ONBOARDING"])
def test_offline_and_onboarding_are_kept_until_a_heartbeat(db, stored):
    connector = live(db, connector_fixture(db), lifecycle=stored)
    assert evaluate_health(db, connector).derived_lifecycle == stored
    assert evaluate_health(db, connector, source="HEARTBEAT").derived_lifecycle == "ONLINE"


def test_stale_connector_derives_offline_but_keeps_its_gating_error(db):
    connector = live(db, connector_fixture(db))
    open_alert(db, connector, "ESP_DURABILITY_FAULT")
    connector.last_seen_at = utc_now() - timedelta(seconds=60)
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.last_error_code) == ("OFFLINE", "ESP_DURABILITY_FAULT")
    assert health.terminal_link["state"] == "UNKNOWN"


def diagnostics_alert(db, connector, code="ESP_DURABILITY_FAULT", **details):
    return open_alert(db, connector, code, details={"diagnostics_schema_version": 1, **details})


def test_diagnostics_currency_follows_boot_binding(db):
    connector = live(db, connector_fixture(db))
    unbound = diagnostics_alert(db, connector)
    assert alert_currency(unbound, connector) == "CURRENT"
    unbound.details = {"binding": "OBSERVED", "boot_id": "boot-b"}
    assert alert_currency(unbound, connector) == "CURRENT"
    unbound.last_seen_at = utc_now() - timedelta(minutes=5)
    assert alert_currency(unbound, connector) == "HELD"
    unbound.details = {"binding": "OBSERVED", "boot_id": "boot-a"}
    assert alert_currency(unbound, connector) == "PREVIOUS_BOOT"
    unbound.details = {"binding": "INFERRED_PREVIOUS"}
    assert alert_currency(unbound, connector) == "PREVIOUS_BOOT"


@pytest.mark.parametrize("code,firmware,tier,lifecycle", [
    ("ESP_DURABILITY_FAULT", "2.5.2", "WARNING", "ONLINE_WITH_WARNINGS"),
    ("ESP_DURABILITY_FAULT", "2.4.12", "WARNING", "ONLINE_WITH_WARNINGS"),
    ("ESP_DURABILITY_FAULT", "2.6.15", "DEGRADED", "DEGRADED"),
    ("ESP_DELIVERY_WORKER_FAULT", "2.5.2", "WARNING", "ONLINE_WITH_WARNINGS"),
    ("ESP_DELIVERY_WORKER_FAULT", "2.6.15", "WARNING", "ONLINE_WITH_WARNINGS"),
])
def test_previous_boot_diagnostics_tier_depends_on_reporting_capability(db, code, firmware, tier, lifecycle):
    connector = live(db, connector_fixture(db), firmware=firmware)
    diagnostics_alert(db, connector, code, binding="OBSERVED", boot_id="boot-a", firmware_version="2.6.27")
    health = evaluate_health(db, connector)
    reason = health.reasons[0]
    assert (reason.currency, reason.tier, reason.gating) == ("PREVIOUS_BOOT", tier, True)
    assert (health.derived_lifecycle, health.last_error_code) == (lifecycle, code)
    assert reason.operator["resolvable"] is True


def test_held_diagnostics_on_the_same_boot_stay_degraded_and_unresolvable(db):
    connector = live(db, connector_fixture(db))
    row = diagnostics_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", binding="OBSERVED", boot_id="boot-b")
    row.last_seen_at = utc_now() - timedelta(minutes=5)
    db.commit()
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.currency, reason.tier) == ("HELD", "DEGRADED")
    assert reason.operator["refusal_code"] == "ALERT_CONDITION_CURRENT"


@pytest.mark.parametrize("source,setting,tier", [
    ("add_connector.c:3764", "WARNING", "WARNING"),
    ("add_connector.c:3787", "WARNING", "WARNING"),
    ("add_connector.c:3764", "DEGRADED", "DEGRADED"),
    ("zone_lite.c:7389", "WARNING", "DEGRADED"),
])
def test_led_latch_tier_follows_owner_setting_and_allowlist(db, monkeypatch, source, setting, tier):
    monkeypatch.setattr(settings, "device_health_latched_led_tier", setting)
    connector = live(db, connector_fixture(db))
    latch = {"kind": "LED_LATCH_NO_IO_ERRORS", "source": source, "firmware_version": "2.6.15"}
    open_alert(db, connector, "ESP_LOCAL_FAILURE", details={"led_state": "LOCAL_FAILURE", "latch": latch})
    diagnostics_alert(db, connector, binding="OBSERVED", boot_id="boot-b", latch=latch)
    health = evaluate_health(db, connector)
    assert {reason.code: reason.tier for reason in health.reasons} == {
        "ESP_LOCAL_FAILURE": tier, "ESP_DURABILITY_FAULT": tier}
    assert health.last_error_code == "ESP_DURABILITY_FAULT"
    assert all(reason.gating and reason.alert.state == "OPEN" for reason in health.reasons)
    assert "latched by " + source in health.reasons[0].evidence_summary


def test_led_states_held_and_storage_verified_latches_are_warnings(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    row = open_alert(db, connector, "ESP_LOCAL_FAILURE", details={"led_clear_since": utc_now().isoformat()})
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.currency, reason.tier) == ("HELD", "WARNING")
    row.details = {"latch": {"kind": "LED_LATCH_STORAGE_VERIFIED", "source": "x", "firmware_version": "2.7.1"}}
    db.commit()
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.currency, reason.tier) == ("CURRENT", "WARNING")
    row.details = {}
    db.commit()
    assert evaluate_health(db, connector).reasons[0].tier == "DEGRADED"


@pytest.mark.parametrize("reason,offline_age,tier,lifecycle", [
    ("HIK_STORAGE", None, "DEGRADED", "DEGRADED"),
    ("HIK_AUTH", None, "WARNING", "ONLINE_WITH_WARNINGS"),
    ("HIK_NETWORK", 60, None, "ONLINE"),
    ("HIK_NETWORK", 400, "WARNING", "ONLINE_WITH_WARNINGS"),
])
def test_hikvision_capture_tiers(db, reason, offline_age, tier, lifecycle):
    poll_error = {"HIK_STORAGE": 8, "HIK_AUTH": 3, "HIK_NETWORK": 2}[reason]
    connector = live(db, connector_fixture(db), family="hikvision", firmware="3.1.0",
                     state="OFFLINE" if offline_age else "ONLINE", offline_age=offline_age,
                     poll_error=poll_error)
    open_alert(db, connector, "HIK_CAPTURE_UNHEALTHY", severity="WARNING",
               details={"reason": reason, "poll_error": poll_error})
    health = evaluate_health(db, connector)
    alert = next(r for r in health.reasons + health.other_active_alerts if r.code == "HIK_CAPTURE_UNHEALTHY")
    assert alert.tier == tier and health.derived_lifecycle == lifecycle
    assert health.last_error_code == (reason if tier else None)


def rejection(db, connector, code="DEVICE_MESSAGE_REJECTED", **types):
    now = utc_now()
    entries = {name: {"category": "SCHEMA_INVALID", "count": count, "first_at": (now - timedelta(seconds=first)).isoformat(),
                      "last_at": (now - timedelta(seconds=last)).isoformat()}
               for name, (count, first, last) in types.items()}
    return open_alert(db, connector, code, details={"types": entries})


@pytest.mark.parametrize("code,types,tier", [
    ("DEVICE_MESSAGE_REJECTED", {"heartbeat": (1, 10, 10)}, "DEGRADED"),
    ("DEVICE_MESSAGE_REJECTED", {"queue_evidence": (2, 60, 10)}, "DEGRADED"),
    ("DEVICE_MESSAGE_REJECTED", {"log": (1, 10, 10)}, "WARNING"),
    ("DEVICE_MESSAGE_REJECTED", {"heartbeat": (1, 3600, 3600)}, "WARNING"),
    ("ADD_MESSAGE_PROCESSING_FAILED", {"heartbeat": (9, 130, 5)}, "DEGRADED"),
    ("ADD_MESSAGE_PROCESSING_FAILED", {"heartbeat": (2, 60, 5)}, None),
    ("ADD_MESSAGE_PROCESSING_FAILED", {"log": (3, 400, 10)}, "WARNING"),
    ("ADD_MESSAGE_PROCESSING_FAILED", {"log": (1, 10, 10)}, None),
])
def test_rejection_tiers(db, code, types, tier):
    connector = live(db, connector_fixture(db))
    rejection(db, connector, code, **types)
    health = evaluate_health(db, connector)
    reasons = health.reasons + health.other_active_alerts
    assert reasons[0].tier == tier
    gating = code == "DEVICE_MESSAGE_REJECTED"
    assert health.last_error_code == (code if gating and tier else None)


def test_legacy_rejection_rows_use_their_message_type(db):
    connector = live(db, connector_fixture(db))
    open_alert(db, connector, "DEVICE_MESSAGE_REJECTED",
               details={"message_type": "heartbeat", "error_category": "SCHEMA_INVALID"})
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.currency, reason.tier) == ("CURRENT", "DEGRADED")
    assert reason.evidence_summary == "heartbeat x1 (SCHEMA_INVALID)"


def test_derived_transport_reasons_never_gate(db):
    connector = live(db, connector_fixture(db), heartbeat_age=100)
    health = evaluate_health(db, connector)
    assert [reason.code for reason in health.reasons] == ["HEARTBEAT_STALE"]
    assert (health.derived_lifecycle, health.last_error_code) == ("DEGRADED", None)
    other = live(db, second_connector(db), state="RETRY_WAIT", offline_age=301)
    health = evaluate_health(db, other)
    assert [reason.code for reason in health.reasons] == ["TERMINAL_DISCONNECTED"]
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", None)
    assert health.reasons[0].operator["refusal_code"] == "DERIVED_REASON"
    third = live(db, second_connector(db, 2), state="RECOVERING", transition_age=700)
    assert [reason.code for reason in evaluate_health(db, third).reasons] == ["TERMINAL_STABILIZING_STALLED"]
    # RECOVERING, RETRY_WAIT and SUSPECT within their windows have no health effect.
    for state in ("RECOVERING", "RETRY_WAIT", "SUSPECT"):
        live(db, third, state=state, offline_age=30, transition_age=30)
        assert evaluate_health(db, third).derived_lifecycle == "ONLINE"


def test_shadow_diff_reports_hold_effects(db):
    stale = live(db, connector_fixture(db), lifecycle="FLAPPING")
    stale.last_error_code = "ZKT_CONNECTION_FLAPPING"
    faulty = live(db, second_connector(db), lifecycle="DEGRADED")
    faulty.last_error_code = "ESP_DURABILITY_FAULT"
    open_alert(db, faulty, "ESP_DURABILITY_FAULT")
    report = shadow_report(db)
    rows = {row["connector_id"]: row for row in report["rows"]}
    assert set(rows) == {stale.connector_id}
    row = rows[stale.connector_id]
    assert row["gate_effect"] == "HOLD_LIFTED"
    assert row["stored"] == {"lifecycle_state": "FLAPPING", "last_error_code": "ZKT_CONNECTION_FLAPPING"}
    assert row["derived"] == {"lifecycle_state": "ONLINE", "last_error_code": None, "tier": "ONLINE"}
    assert report["counts"]["transitions"] == {"FLAPPING->ONLINE": 1}
    assert report["counts"]["gate_effects"] == {"HOLD_LIFTED": 1}
    assert (gate_effect("A", "A"), gate_effect(None, "A"), gate_effect("A", "B"), gate_effect(None, None)) == (
        "UNCHANGED", "HOLD_ADDED", "HOLD_CODE_CHANGED", "UNCHANGED")
    client, _headers = admin_client(db)
    body = client.get("/api/v1/device-health/shadow").json()
    assert [row["connector_id"] for row in body["rows"]] == [stale.connector_id]


def test_device_list_health_uses_one_alert_query(db):
    for index in range(3):
        connector = live(db, second_connector(db, index), state="FLAPPING")
        open_alert(db, connector, "ZKT_CONNECTION_FLAPPING", severity="WARNING")
    client, _headers = admin_client(db)
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get("/api/v1/devices")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 3
    for row in rows:
        assert row["terminal_link"]["state"] == "FLAPPING"
        assert row["health"]["tier"] == "ONLINE_WITH_WARNINGS"
        assert row["health"]["primary"]["code"] == "ZKT_CONNECTION_FLAPPING"
        assert (row["health"]["warning_count"], row["health"]["degraded_count"]) == (1, 0)
    assert sum("add_device_alerts" in statement for statement in statements) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_health_reads_never_write_lifecycle(db, monkeypatch, enabled):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = live(db, connector_fixture(db), lifecycle="FLAPPING")
    connector.last_error_code = "ZKT_CONNECTION_FLAPPING"
    db.commit()
    client, _headers = admin_client(db)
    for path in ("/api/v1/devices", f"/api/v1/devices/{connector.connector_id}", "/api/v1/device-health/shadow"):
        assert client.get(path).status_code == 200
    db.expire_all()
    assert (connector.lifecycle_state, connector.last_error_code) == ("FLAPPING", "ZKT_CONNECTION_FLAPPING")
    health = client.get(f"/api/v1/devices/{connector.connector_id}").json()["health"]
    assert health["mode"] == ("ENFORCED" if enabled else "SHADOW")


def test_device_detail_explains_health_coverage_and_device_error(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2", lifecycle="DEGRADED")
    connector.last_error_code = "ESP_DELIVERY_WORKER_FAULT"
    row = diagnostics_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", binding="INFERRED_PREVIOUS",
                            boot_id="boot-a", firmware_version="2.6.27")
    open_alert(db, connector, "TERMINAL_SOURCE_EXCEPTION")
    client, _headers = admin_client(db)
    body = client.get(f"/api/v1/devices/{connector.connector_id}").json()
    assert body["terminal_link"]["state"] == "CONNECTED"
    health = body["health"]
    assert (health["tier"], health["derived_lifecycle"], health["lifecycle_state"]) == (
        "ONLINE_WITH_WARNINGS", "ONLINE_WITH_WARNINGS", "DEGRADED")
    reason = health["reasons"][0]
    assert (reason["code"], reason["tier"], reason["currency"], reason["gating"]) == (
        "ESP_DELIVERY_WORKER_FAULT", "WARNING", "PREVIOUS_BOOT", True)
    assert (reason["alert_id"], reason["binding"], reason["firmware_version"]) == (row.id, "INFERRED_PREVIOUS", "2.6.27")
    assert reason["operator"] == {"resolvable": True, "refusal_code": None, "refusal": None}
    assert reason["clear_condition"].startswith("Clears when every required delivery worker")
    assert [item["code"] for item in health["other_active_alerts"]] == ["TERMINAL_SOURCE_EXCEPTION"]
    assert health["device_error"] == {
        "code": "ESP_DELIVERY_WORKER_FAULT", "message": None, "derived_code": "ESP_DELIVERY_WORKER_FAULT",
        "backed": True, "backing_alert_ids": [row.id]}
    coverage = {item["key"]: item for item in health["coverage"]}
    assert coverage["storage"]["status"] == "NOT_REPORTED_BY_FIRMWARE"
    assert coverage["storage"]["detail"] == "Not reported by firmware 2.5.2."
    assert coverage["led"]["status"] == "ACTIVE"


def test_quarantine_is_resolvable_only_without_another_claimant(db):
    connector = live(db, connector_fixture(db))
    connector.zkt_device.serial = "SHARED"
    open_alert(db, connector, "QUARANTINED_DUPLICATE_SERIAL", severity="CRITICAL")
    client, _headers = admin_client(db)
    path = f"/api/v1/devices/{connector.connector_id}"
    assert client.get(path).json()["health"]["reasons"][0]["operator"]["resolvable"] is True
    other = second_connector(db)
    other.zkt_device.serial = "SHARED"
    db.commit()
    operator = client.get(path).json()["health"]["reasons"][0]["operator"]
    assert (operator["resolvable"], operator["refusal_code"]) == (False, "ALERT_CONDITION_CURRENT")
