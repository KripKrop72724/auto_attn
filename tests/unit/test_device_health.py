"""Device health: alert lifecycle, derived tiers and their evidence rules."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from test_add_backend import SERIAL, connector_fixture, db as db
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from test_add_backend import queue_evidence_payload
from test_hil_scope import hil_session  # noqa: F401
from test_zkt_factory_trial import factory  # noqa: F401
from zk_add import device_health, web as add_web, worker as add_worker
from zk_add import zkt_factory_trial
from zk_add.device_health import (
    POLICY,
    alert_currency,
    apply_device_health,
    evaluate_health,
    gate_effect,
    shadow_report,
    terminal_link,
)
from zk_add.models import AuditEvent, DeviceAlert, DeviceConnectionEvent, DeviceLog, DeviceTelemetry
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease
from zk_add.schemas import Envelope, HeartbeatPayload, UserSnapshotRequest, UserSnapshotRow
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.service import (
    apply_firmware_diagnostics,
    evaluate_terminal_link_down,
    fleet_counts,
    replace_user_snapshot,
    resolve_alert,
    resolve_message_rejection,
    update_heartbeat,
    upsert_alert,
    utc_now,
)
from zk_add.time_utils import ensure_utc
from zk_add.zkt_hil_schedule import _known_hold
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


P02 = ("bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", "e0:72:a1:d7:05:c4")
RECOVERY_DIGEST = "d" * 64


def alert_row(db, connector, code):
    return db.scalar(select(DeviceAlert).where(DeviceAlert.connector_id == connector.id, DeviceAlert.code == code)
                     .order_by(DeviceAlert.id.desc()))


def worker(name, state="RUNNING", tick=99_000, **fields):
    return {"name": name, "state": state, "last_activity_uptime_ms": tick, **fields}


def apply(db, connector, diagnostics=None, *, uptime=100, firmware=None, image=None, led=None):
    connector.firmware_version = firmware or connector.firmware_version
    payload = {"firmware_version": connector.firmware_version, "uptime_seconds": uptime, "led_state": led}
    if diagnostics is not None:
        payload["diagnostics"] = diagnostics
    if image:
        payload["ota"] = {"image_sha256": image}
    apply_firmware_diagnostics(db, connector=connector, payload=HeartbeatPayload(**payload))
    db.flush()


def recovery_release(db, version="2.6.27"):
    db.add(FirmwareRelease(
        release_id=f"zone-lite-{version}", version=version, git_sha="a" * 40, image_sha256="e" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout="zone-lite-ota-v1",
        minimum_bootstrap_version="2.5.2", storage_name=f"recovery/{version}.bin",
        manifest={"application_sha256": RECOVERY_DIGEST}, manifest_signature="test-signature", state="HIL_ONLY"))
    db.commit()


def p02(db):
    connector = live(db, connector_fixture(db, hardware_id=P02[1], expected_serial="CJH9211060009"))
    connector.connector_id = P02[0]
    db.commit()
    return connector


def test_recovery_image_worker_contract_not_applicable(db):
    recovery_release(db)
    stopped = {"workers": [worker("add_delivery", "STOPPED"), worker("ords_delivery", "STOPPED")]}
    connector = p02(db)
    apply(db, connector, stopped, firmware="2.6.27", image=RECOVERY_DIGEST)
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT") is None
    assert connector.firmware_diagnostics["worker_contract"] == "NOT_APPLICABLE_STORAGE_RECOVERY"
    # A different application digest is evaluated like any other image.
    apply(db, connector, stopped, firmware="2.6.27", image="f" * 64)
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT").state == "OPEN"
    assert "worker_contract" not in connector.firmware_diagnostics
    # So is the exact image on a connector outside the reviewed Peshawar scope.
    other = live(db, second_connector(db))
    apply(db, other, stopped, firmware="2.6.27", image=RECOVERY_DIGEST)
    assert alert_row(db, other, "ESP_DELIVERY_WORKER_FAULT").state == "OPEN"


def test_p02_shaped_legacy_worker_row_binds_inferred_previous_on_252(db, monkeypatch):
    monkeypatch.setattr(settings, "device_health_derived_enabled", False)
    connector = connector_fixture(db)
    row = open_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", details={"diagnostics_schema_version": 2},
                     seen=utc_now() - timedelta(days=1))
    seen = ensure_utc(row.last_seen_at)
    update_heartbeat(db, connector=connector, boot_id="p02-252", sequence=1, payload=HeartbeatPayload(
        firmware_version="2.5.2", uptime_seconds=600, led_state="HEALTHY",
        zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL}))
    db.flush()
    assert row.state == "OPEN" and row.details["binding"] == "INFERRED_PREVIOUS"
    assert ensure_utc(row.last_seen_at) == seen
    # The legacy writer still holds the device DEGRADED in SHADOW mode.
    assert connector.lifecycle_state == "DEGRADED"
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.code, reason.tier, reason.currency, reason.gating) == (
        "ESP_DELIVERY_WORKER_FAULT", "WARNING", "PREVIOUS_BOOT", True)
    assert evaluate_health(db, connector).derived_lifecycle == "ONLINE_WITH_WARNINGS"


def test_row_raised_on_this_boot_binds_inferred_current(db):
    connector = live(db, connector_fixture(db), firmware="2.5.2")
    row = open_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", seen=utc_now() - timedelta(seconds=60))
    apply(db, connector, uptime=600)
    assert (row.details["binding"], row.details["boot_id"]) == ("INFERRED_CURRENT", "boot-b")
    assert alert_currency(row, connector) == "HELD"


def test_worker_fault_still_failing_on_new_boot_is_rebound(db):
    connector = live(db, connector_fixture(db))
    connector.boot_id = "boot-a"
    apply(db, connector, {"workers": [worker("add_delivery", "STOPPED"), worker("ords_delivery")]})
    row = alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT")
    first = row.details["boot_first_seen_at"]
    assert (row.details["binding"], row.details["boot_id"]) == ("OBSERVED", "boot-a")
    apply(db, connector, {"workers": [worker("add_delivery", "STOPPED"), worker("ords_delivery")]})
    assert row.details["boot_first_seen_at"] == first  # carried within the boot
    connector.boot_id = "boot-b"
    apply(db, connector, {"workers": [worker("add_delivery", "FAULT"), worker("ords_delivery")]})
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT").id == row.id and row.state == "OPEN"
    assert row.details["boot_id"] == "boot-b" and row.details["boot_first_seen_at"] != first
    assert row.message == "Attendance delivery worker fault: add_delivery FAULT."


def heartbeat(db, connector, sequence, *, boot="boot-b", firmware="2.6.15", uptime=1000, led="HEALTHY",
              diagnostics=None, zkt=None, activity=None):
    result = update_heartbeat(db, connector=connector, boot_id=boot, sequence=sequence, payload=HeartbeatPayload(
        firmware_version=firmware, uptime_seconds=uptime, led_state=led, current_activity=activity,
        zkt=zkt if zkt is not None else {"online": True, "connection_state": "ONLINE", "serial": SERIAL},
        **({"diagnostics": diagnostics} if diagnostics is not None else {})))
    db.flush()
    return result


def legacy_sample(uptime=1000, **storage):
    return {"storage": {"durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True,
                        "write_failures": 0, "read_failures": 0, **storage},
            "workers": [worker("add_delivery", tick=uptime * 1000 - 1000),
                        worker("ords_delivery", tick=uptime * 1000 - 1000)]}


def test_durability_on_capable_firmware_is_held_until_verified(db):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, diagnostics=legacy_sample(durability="DEGRADED", write_failures=3))
    row = alert_row(db, connector, "ESP_DURABILITY_FAULT")
    residual = open_alert(db, connector, "ESP_PRESERVATION_UNVERIFIED", severity="WARNING",
                          seen=utc_now() - timedelta(days=2))
    residual_seen = ensure_utc(residual.last_seen_at)
    assert row.details["evidence"]["storage"]["write_failures"] == 3
    assert evaluate_health(db, connector).reasons[0].currency == "CURRENT"
    heartbeat(db, connector, 2)  # no diagnostics: indeterminate on the same boot
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.code, reason.currency, reason.tier) == ("ESP_DURABILITY_FAULT", "HELD", "DEGRADED")
    heartbeat(db, connector, 3, diagnostics=legacy_sample())
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "VERIFIED"
    assert residual.state == "RESOLVED" and residual.details["resolution"]["kind"] == "VERIFIED"
    assert ensure_utc(residual.last_seen_at) == residual_seen


def journal_sample(authority="ADD", *, uptime=100, phase="READY", owner=None, workers=None, storage=None):
    tick = uptime * 1000 - 1000
    return {
        "schema_version": 2, "runtime_profile": "ZKT_JOURNAL_V1", "journal_format": 1,
        "delivery_authority": authority,
        "journal_runtime": {"observed": True, "phase": phase, "reader_ready": True,
                            "writer_ready": authority == "ADD", "start_attempts": 1, "storage_starts": 1,
                            "delivery_starts": 1, "capture_starts": 1, "proof_attempts": 0, "failures": 0},
        "journal_storage": {"observed": True, "fresh": True, "ready": True, "durability": "HEALTHY",
                            "checkpoint_recovery_pending": False, "mailbox_capacity": 8,
                            "mailbox_high_watermark": 1, "pending_appends": 0, "sampled_uptime_ms": tick,
                            **(owner or {})},
        "storage": storage or {"durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True},
        "workers": workers if workers is not None else [
            worker("storage_owner", tick=tick), worker("add_delivery", tick=tick),
            {"name": "capture", "state": "RUNNING", "execution_model": "ON_DEMAND", "sampled_uptime_ms": tick,
             "last_activity_uptime_ms": 0, "pending_requests": 0}],
    }


def test_unknown_authority_rules(db):
    connector = live(db, connector_fixture(db), firmware="2.7.1")
    early = [worker("storage_owner", "STOPPED"), worker("add_delivery", tick=1000)]
    apply(db, connector, journal_sample("UNKNOWN", uptime=100, workers=early), uptime=100)
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT") is None
    assert alert_row(db, connector, "DELIVERY_AUTHORITY_UNKNOWN") is None
    stale = [worker("storage_owner", tick=1000), worker("add_delivery", tick=1000)]
    apply(db, connector, journal_sample("UNKNOWN", uptime=700, workers=stale), uptime=700)
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT") is None
    unknown = alert_row(db, connector, "DELIVERY_AUTHORITY_UNKNOWN")
    assert unknown.state == "OPEN" and unknown.severity == "WARNING"
    reason = next(r for r in evaluate_health(db, connector).reasons if r.code == "DELIVERY_AUTHORITY_UNKNOWN")
    assert (reason.tier, reason.gating) == ("WARNING", False)
    faulted = [worker("storage_owner", "FAULT"), worker("add_delivery")]
    apply(db, connector, journal_sample("UNKNOWN", uptime=700, workers=faulted), uptime=700)
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT").message == (
        "Attendance delivery worker fault: storage_owner FAULT.")
    unknown_seen = ensure_utc(unknown.last_seen_at)
    apply(db, connector, journal_sample("ADD", uptime=800), uptime=800)
    assert unknown.state == "RESOLVED" and ensure_utc(unknown.last_seen_at) == unknown_seen
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT").state == "RESOLVED"


@pytest.mark.parametrize("uptime,owner,storage,raised", [
    (60, {"ready": False}, None, False),
    (60, {"ready": False, "last_append_result": "IO"}, None, True),
    (60, {"ready": False}, {"durability": "HEALTHY", "error_code": 5, "error_operation": "journal_append"}, True),
    (700, {"ready": False}, None, True),
])
def test_journal_soft_owner_not_ready_is_starting_but_hard_evidence_raises(db, uptime, owner, storage, raised):
    connector = live(db, connector_fixture(db), firmware="2.7.1")
    apply(db, connector, journal_sample(uptime=uptime, phase="OWNER_START", owner=owner, storage=storage),
          uptime=uptime)
    row = alert_row(db, connector, "ESP_DURABILITY_FAULT")
    assert (row is not None) is raised
    if raised:
        assert row.details["evidence"]["summary"].startswith("storage ")


def latch_storage(source="add_connector.c:3764", **fields):
    return {"durability": "DEGRADED", "persistence_verified": True, "recovery_complete": True, "write_failures": 0,
            "read_failures": 0, "persistence_probe_failures": 0, "local_failure_source": source,
            "used_bytes": 148, "total_bytes": 1000, **fields}


@pytest.mark.parametrize("setting,source,fields,latch,tier", [
    ("WARNING", "add_connector.c:3764", {}, "LED_LATCH_NO_IO_ERRORS", "WARNING"),
    ("WARNING", "add_connector.c:3787", {}, "LED_LATCH_NO_IO_ERRORS", "WARNING"),
    ("DEGRADED", "add_connector.c:3764", {}, "LED_LATCH_NO_IO_ERRORS", "DEGRADED"),
    ("WARNING", "zone_lite.c:7389", {}, "LED_LATCH_NO_IO_ERRORS", "DEGRADED"),
    ("WARNING", "add_connector.c:3764", {"write_failures": 207}, None, "DEGRADED"),
    ("WARNING", "add_connector.c:3764", {"persistence_verified": False}, None, "DEGRADED"),
])
def test_led_latch_classification_on_2615(db, monkeypatch, setting, source, fields, latch, tier):
    monkeypatch.setattr(settings, "device_health_latched_led_tier", setting)
    connector = connector_fixture(db)
    sample = legacy_sample()
    sample["storage"] = latch_storage(source, **fields)
    heartbeat(db, connector, 1, led="LOCAL_FAILURE", diagnostics=sample)
    led, durability = alert_row(db, connector, "ESP_LOCAL_FAILURE"), alert_row(db, connector, "ESP_DURABILITY_FAULT")
    for row in (led, durability):
        assert row.state == "OPEN" and row.severity == "HIGH"
        assert (row.details.get("latch") or {}).get("kind") == latch
    if latch:
        assert led.details["latch"] == {"kind": latch, "source": source, "firmware_version": "2.6.15"}
    health = evaluate_health(db, connector)
    assert {reason.code: reason.tier for reason in health.reasons} == {
        "ESP_DURABILITY_FAULT": tier, "ESP_LOCAL_FAILURE": tier}
    assert health.last_error_code == "ESP_DURABILITY_FAULT"
    assert health.derived_lifecycle == ("ONLINE_WITH_WARNINGS" if tier == "WARNING" else "DEGRADED")


def test_led_latch_is_removed_when_the_predicate_fails(db):
    connector = connector_fixture(db)
    sample = legacy_sample()
    sample["storage"] = latch_storage()
    heartbeat(db, connector, 1, led="LOCAL_FAILURE", diagnostics=sample)
    assert alert_row(db, connector, "ESP_LOCAL_FAILURE").details["latch"]["kind"] == "LED_LATCH_NO_IO_ERRORS"
    sample["storage"] = latch_storage(read_failures=1)
    heartbeat(db, connector, 2, led="LOCAL_FAILURE", diagnostics=sample)
    assert "latch" not in alert_row(db, connector, "ESP_LOCAL_FAILURE").details
    assert "latch" not in alert_row(db, connector, "ESP_DURABILITY_FAULT").details
    assert evaluate_health(db, connector).derived_lifecycle == "DEGRADED"


def test_led_latch_with_verified_storage_on_current_firmware_is_a_warning(db):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, firmware="2.7.1", led="LOCAL_FAILURE", diagnostics=legacy_sample())
    led = alert_row(db, connector, "ESP_LOCAL_FAILURE")
    assert led.details["latch"]["kind"] == "LED_LATCH_STORAGE_VERIFIED"
    assert alert_row(db, connector, "ESP_DURABILITY_FAULT") is None
    health = evaluate_health(db, connector)
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", "ESP_LOCAL_FAILURE")


def test_alert_details_carry_bounded_evidence_summary(db):
    connector = live(db, connector_fixture(db))
    workers = [worker(f"worker_{index}_" + "x" * 30, "STOPPED", operation="o" * 80, restart_count=index)
               for index in range(8)]
    apply(db, connector, {"workers": workers}, uptime=200)
    details = alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT").details
    assert set(details) >= {"diagnostics_schema_version", "boot_id", "firmware_version", "binding",
                            "boot_first_seen_at", "evidence"}
    evidence = details["evidence"]
    assert len(evidence["summary"]) <= 300 and len(evidence["workers"]) == 8
    assert evidence["workers"][0] == {"name": workers[0]["name"], "state": "STOPPED", "operation": "o" * 80,
                                      "tick_age_ms": 101_000, "restart_count": 0}
    apply(db, connector, {"storage": {"durability": "FULL", "error_operation": "queue_append", "error_code": 28,
                                      "used_bytes": 99, "total_bytes": 100}}, uptime=200)
    row = alert_row(db, connector, "ESP_DURABILITY_FAULT")
    assert row.details["evidence"]["storage"]["used_percent"] == 99.0
    assert row.message.startswith("Attendance preservation needs recovery: storage FULL; queue_append error 28")


def test_state_lock_busy_sample_is_ignored(db):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL,
                                     "user_count": 10, "attendance_count": 100})
    zkt = connector.zkt_device
    events = db.scalar(select(func.count(DeviceConnectionEvent.id)))
    marker = zkt.last_seen_at
    heartbeat(db, connector, 2, activity="STATE_LOCK_BUSY", led="STATE_LOCK_BUSY",
              zkt={"online": False, "connection_state": "", "user_count": 0, "attendance_count": 0})
    assert db.scalar(select(func.count(DeviceConnectionEvent.id))) == events
    assert (zkt.user_count, zkt.attendance_count, zkt.connection_state, zkt.offline_since) == (
        10, 100, "ONLINE", None)
    assert zkt.last_seen_at > marker  # the heartbeat itself was accepted
    assert evaluate_health(db, connector).derived_lifecycle == "ONLINE"


def test_led_pseudo_states_neither_raise_nor_resolve(db):
    connector = connector_fixture(db)
    for sequence, state in enumerate(("STATE_LOCK_BUSY", "UNAVAILABLE", ""), start=1):
        heartbeat(db, connector, sequence, led=state)
    assert alert_row(db, connector, "ESP_LOCAL_FAILURE") is None
    heartbeat(db, connector, 4, led="LOCAL_FAILURE")
    row = alert_row(db, connector, "ESP_LOCAL_FAILURE")
    seen = ensure_utc(row.last_seen_at)
    for sequence, state in enumerate(("STATE_LOCK_BUSY", "UNAVAILABLE", ""), start=5):
        heartbeat(db, connector, sequence, led=state)
        assert row.state == "OPEN" and ensure_utc(row.last_seen_at) == seen
    heartbeat(db, connector, 8, led="HEALTHY")
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "CONDITION_CLEARED"


def test_252_boot_time_local_failure_hold(db):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, boot="boot-1", firmware="2.5.2", uptime=60, led="LOCAL_FAILURE")
    row = alert_row(db, connector, "ESP_LOCAL_FAILURE")
    assert (row.details["boot_time"], row.details["first_uptime_seconds"], row.details["boot_id"]) == (
        True, 60, "boot-1")
    heartbeat(db, connector, 2, boot="boot-1", firmware="2.5.2", uptime=200, led="LOCAL_FAILURE")
    assert row.details["first_uptime_seconds"] == 60  # carried within the boot
    heartbeat(db, connector, 3, boot="boot-1", firmware="2.5.2", uptime=330, led="HEALTHY")
    assert row.state == "OPEN" and row.details["led_clear_since"]
    held_since, seen = row.details["led_clear_since"], ensure_utc(row.last_seen_at)
    heartbeat(db, connector, 4, boot="boot-1", firmware="2.5.2", uptime=345, led="HEALTHY")
    assert row.details["led_clear_since"] == held_since and ensure_utc(row.last_seen_at) == seen
    health = evaluate_health(db, connector)
    reason = health.reasons[0]
    assert (reason.code, reason.currency, reason.tier, reason.gating) == ("ESP_LOCAL_FAILURE", "HELD", "WARNING", True)
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", "ESP_LOCAL_FAILURE")
    heartbeat(db, connector, 1, boot="boot-2", firmware="2.5.2", uptime=20, led="HEALTHY")
    assert row.state == "RESOLVED"
    # Firmware that reports diagnostics clears on the next healthy LED.
    other = second_connector(db)
    heartbeat(db, other, 1, boot="boot-1", firmware="2.6.15", uptime=60, led="LOCAL_FAILURE")
    assert alert_row(db, other, "ESP_LOCAL_FAILURE").details["boot_time"] is False
    heartbeat(db, other, 2, boot="boot-1", firmware="2.6.15", uptime=75, led="HEALTHY")
    assert alert_row(db, other, "ESP_LOCAL_FAILURE").state == "RESOLVED"


def test_serial_mismatch_resolves_on_matching_serial_only(db):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, zkt={"online": True, "connection_state": "ONLINE", "serial": "WRONG"})
    row = alert_row(db, connector, "ZKT_SERIAL_MISMATCH")
    assert row.state == "OPEN" and connector.last_error_code == "ZKT_SERIAL_MISMATCH"
    heartbeat(db, connector, 2, zkt={"online": True, "connection_state": "ONLINE"})
    assert row.state == "OPEN"
    seen = ensure_utc(row.last_seen_at)
    heartbeat(db, connector, 3)
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "CONDITION_CLEARED"
    assert ensure_utc(row.last_seen_at) == seen
    assert connector.last_error_code != "ZKT_SERIAL_MISMATCH"


def test_quarantine_resolves_when_duplicate_claim_disappears(db):
    first = connector_fixture(db)
    first.zkt_device.serial, first.zkt_device.online = SERIAL, True
    second = connector_fixture(db, hardware_id="e0:72:a1:d6:f3:29", expected_serial=SERIAL)
    heartbeat(db, second, 1)
    row = alert_row(db, second, "QUARANTINED_DUPLICATE_SERIAL")
    assert row.state == "OPEN" and second.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    seen = ensure_utc(row.last_seen_at)
    first.zkt_device.serial = "MOVED-ELSEWHERE"
    db.flush()
    heartbeat(db, second, 2)
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "CONDITION_CLEARED"
    assert ensure_utc(row.last_seen_at) == seen
    assert second.last_error_code is None
    assert alert_row(db, first, "QUARANTINED_DUPLICATE_SERIAL").state == "OPEN"


def test_user_snapshot_truncated_resolves_on_complete_stable_28_byte_snapshot(db):
    connector = connector_fixture(db)
    connector.zkt_device.capability_profile = {"observed_user_record_bytes": 28}

    def snapshot(snapshot_id, complete):
        replace_user_snapshot(db, connector=connector, snapshot=UserSnapshotRequest(
            snapshot_id=snapshot_id, complete=complete, observed_at=utc_now(),
            users=[UserSnapshotRow(uid="1", user_id="1001", name="One")]))
        db.flush()

    snapshot("partial", False)
    row = alert_row(db, connector, "USER_SNAPSHOT_TRUNCATED")
    seen = ensure_utc(row.last_seen_at)
    snapshot("complete", True)
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "CONDITION_CLEARED"
    assert ensure_utc(row.last_seen_at) == seen


def telemetry(db, connector, boot, minutes_ago):
    db.add(DeviceTelemetry(connector_id=connector.id, boot_id=boot, sequence=1, uptime_seconds=30,
                           payload={}, created_at=utc_now() - timedelta(minutes=minutes_ago)))
    db.flush()


@pytest.mark.parametrize("earlier,raised", [(["b1", "b2", "b3"], True), (["b1", "b2"], False),
                                            (["old", "b2", "b3"], False)])
def test_restart_loop(db, earlier, raised):
    connector = connector_fixture(db)
    for index, boot in enumerate(earlier):
        telemetry(db, connector, boot, 45 if boot == "old" else 20 - index * 5)
    heartbeat(db, connector, 1, boot="b4", uptime=30)
    row = alert_row(db, connector, "ESP_RESTART_LOOP")
    assert (row is not None) is raised
    if raised:
        assert (row.severity, row.details["boots"]) == ("HIGH", ["b1", "b2", "b3", "b4"])
        reason = evaluate_health(db, connector).reasons[0]
        assert (reason.code, reason.tier, reason.gating) == ("ESP_RESTART_LOOP", "DEGRADED", True)
        seen = ensure_utc(row.last_seen_at)
        heartbeat(db, connector, 2, boot="b4", uptime=1800)
        assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen


def test_terminal_link_down_after_15_minutes_and_resolves_on_online(db):
    connector = connector_fixture(db)
    down = {"online": False, "connection_state": "RETRY_WAIT", "serial": SERIAL,
            "transition_reason": "terminal unreachable"}
    heartbeat(db, connector, 1, zkt=down)
    connector.zkt_device.offline_since = utc_now() - timedelta(minutes=14)
    heartbeat(db, connector, 2, zkt=down)
    assert alert_row(db, connector, "TERMINAL_LINK_DOWN") is None
    connector.zkt_device.offline_since = utc_now() - timedelta(minutes=16)
    heartbeat(db, connector, 3, zkt=down)
    row = alert_row(db, connector, "TERMINAL_LINK_DOWN")
    assert (row.state, row.severity, row.details["raw_state"], row.details["transition_reason"]) == (
        "OPEN", "WARNING", "RETRY_WAIT", "terminal unreachable")
    health = evaluate_health(db, connector)
    assert [reason.code for reason in health.reasons] == ["TERMINAL_DISCONNECTED"]
    assert [reason.code for reason in health.other_active_alerts] == ["TERMINAL_LINK_DOWN"]
    assert health.last_error_code is None
    seen = ensure_utc(row.last_seen_at)
    heartbeat(db, connector, 4)
    assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen


def test_no_terminal_link_down_for_hikvision(db):
    connector = live(db, connector_fixture(db), family="hikvision", firmware="3.1.0", state="OFFLINE",
                     offline_age=3600, poll_error=2)
    assert terminal_link(connector)["state"] == "DISCONNECTED"
    evaluate_terminal_link_down(db, connector, now=utc_now())
    db.flush()
    assert alert_row(db, connector, "TERMINAL_LINK_DOWN") is None


@pytest.fixture()
def envelopes(db, monkeypatch):
    """Run the WebSocket envelope paths against the test session."""
    @contextmanager
    def scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(add_web, "session_scope", scope)
    sequence = iter(range(1, 1000))

    def envelope(connector, message_type, payload, *, boot="boot-e"):
        return Envelope(message_id=f"message-{next(sequence)}", connector_id=connector.connector_id, boot_id=boot,
                        seq=next(sequence), sent_at=utc_now(), type=message_type, payload=payload)
    return envelope


def reject(connector, envelope, error):
    add_web.record_envelope_rejection(connector.id, envelope, error)


def test_server_side_rejection_uses_add_message_processing_failed(db, envelopes):
    connector = live(db, connector_fixture(db))
    seen = ensure_utc(connector.last_seen_at)
    reject(connector, envelopes(connector, "heartbeat", {}), OperationalError("SELECT 1", {}, Exception("down")))
    db.refresh(connector)
    row = alert_row(db, connector, "ADD_MESSAGE_PROCESSING_FAILED")
    assert (row.state, row.severity) == ("OPEN", "WARNING")
    assert row.message == "ADD could not process a heartbeat message (DATABASE_UNAVAILABLE)."
    assert alert_row(db, connector, "DEVICE_MESSAGE_REJECTED") is None
    assert ensure_utc(connector.last_seen_at) == seen
    log = db.scalar(select(DeviceLog).where(DeviceLog.connector_id == connector.id))
    assert log.code == "ADD_MESSAGE_PROCESSING_FAILED"
    # One transient failure has no health effect and never gates.
    health = evaluate_health(db, connector)
    assert (health.reasons, health.last_error_code) == ([], None)


def test_device_rejection_tracks_types_and_field_paths_without_values(db, envelopes):
    connector = live(db, connector_fixture(db))
    secret = "35202-1234567-1"
    try:
        HeartbeatPayload.model_validate({"uptime_seconds": secret, "zkt": {}})
    except ValidationError as exc:
        invalid = exc
    for _ in range(2):
        reject(connector, envelopes(connector, "heartbeat", {}), invalid)
    reject(connector, envelopes(connector, "log", {}), ValueError("LOG_EVIDENCE_INVALID"))
    row = alert_row(db, connector, "DEVICE_MESSAGE_REJECTED")
    types = row.details["types"]
    assert set(types) == {"heartbeat", "log"}
    assert (types["heartbeat"]["count"], types["heartbeat"]["category"], types["heartbeat"]["error_paths"]) == (
        2, "SCHEMA_INVALID", ["uptime_seconds"])
    assert (types["log"]["category"], types["log"]["error_paths"]) == ("EVIDENCE_INVALID", [])
    assert row.details["message_type"] == "log"  # legacy top-level keys follow the latest
    stored = str(row.details) + str([log.context for log in db.scalars(select(DeviceLog)).all()])
    assert secret not in stored
    reason = evaluate_health(db, connector).reasons[0]
    assert (reason.code, reason.tier, reason.currency) == ("DEVICE_MESSAGE_REJECTED", "DEGRADED", "CURRENT")


def test_rejection_types_are_bounded(db, envelopes):
    connector = live(db, connector_fixture(db))
    for index in range(18):
        reject(connector, envelopes(connector, f"type_{index:02d}", {}), ValueError("bad"))
    types = alert_row(db, connector, "DEVICE_MESSAGE_REJECTED").details["types"]
    assert len(types) == 16 and "type_00" not in types and "type_17" in types


def test_accepted_message_resolves_only_its_type(db, envelopes):
    connector = live(db, connector_fixture(db))
    reject(connector, envelopes(connector, "heartbeat", {}), ValueError("bad"))
    reject(connector, envelopes(connector, "log", {}), ValueError("bad"))
    row = alert_row(db, connector, "DEVICE_MESSAGE_REJECTED")
    seen = ensure_utc(row.last_seen_at)
    assert resolve_message_rejection(db, connector, message_type="user_snapshot") is False
    assert resolve_message_rejection(db, connector, message_type="log") is True
    assert row.state == "OPEN" and set(row.details["types"]) == {"heartbeat"}
    assert resolve_message_rejection(db, connector, message_type="heartbeat") is True
    assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "MESSAGE_ACCEPTED"
    assert ensure_utc(row.last_seen_at) == seen


@pytest.mark.parametrize("message_type", ["heartbeat", "log", "queue_evidence", "unrecognized_probe"])
def test_each_processed_envelope_type_resolves_its_rejection(db, envelopes, message_type):
    connector = live(db, connector_fixture(db))
    payloads = {
        "heartbeat": {"zkt": {"online": True, "connection_state": "ONLINE", "serial": SERIAL}},
        "log": {"level": "INFO", "message": "ok"},
        "queue_evidence": queue_evidence_payload(connector),
        "unrecognized_probe": {},
    }
    reject(connector, envelopes(connector, message_type, {}), ValueError("bad"))
    reject(connector, envelopes(connector, "other_type", {}), ValueError("bad"))
    add_web.persist_envelope(connector.id, envelopes(connector, message_type, payloads[message_type]))
    row = alert_row(db, connector, "DEVICE_MESSAGE_REJECTED")
    assert row.state == "OPEN" and set(row.details["types"]) == {"other_type"}


def test_duplicate_replay_does_not_resolve_a_rejection(db, envelopes):
    connector = live(db, connector_fixture(db))
    accepted = envelopes(connector, "log", {"level": "INFO", "message": "ok"})
    add_web.persist_envelope(connector.id, accepted)
    reject(connector, envelopes(connector, "log", {}), ValueError("bad"))
    add_web.persist_envelope(connector.id, accepted)  # same boot and sequence: a replay
    assert alert_row(db, connector, "DEVICE_MESSAGE_REJECTED").state == "OPEN"


def test_rejected_heartbeat_never_refreshes_last_seen(db, envelopes):
    connector = live(db, connector_fixture(db))
    connector.last_seen_at = utc_now() - timedelta(seconds=40)
    db.commit()
    seen = ensure_utc(connector.last_seen_at)
    reject(connector, envelopes(connector, "heartbeat", {}), ValueError("DIAGNOSTICS_SAMPLE_MISMATCH"))
    db.refresh(connector)
    assert ensure_utc(connector.last_seen_at) == seen and connector.connected


def deployment(db, connector, *, status="SUCCEEDED", completed_minutes_ago=10, index=1):
    release = FirmwareRelease(
        release_id=f"release-{index}", version=f"2.9.{index}", git_sha="a" * 40, image_sha256=f"{index:x}" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout="zone-lite-ota-v1",
        storage_name=f"health/{index}.bin", manifest={}, manifest_signature="test-signature", state="AVAILABLE")
    db.add(release)
    db.flush()
    campaign = FirmwareCampaign(
        campaign_id=f"campaign-{index}", release_id=release.id, zone_id=f"zone-{index}", status="COMPLETED",
        actor="StateHealthAdmin", idempotency_key=f"key-{index}", reason="Device health test",
        typed_confirmation=release.version, eligible_count=1, legacy_skipped_count=0)
    db.add(campaign)
    db.flush()
    row = FirmwareDeployment(
        deployment_id=f"deployment-{index}", campaign_id=campaign.id, release_id=release.id,
        connector_id=connector.id, status=status, target_version=release.version,
        completed_at=utc_now() - timedelta(minutes=completed_minutes_ago))
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("code,details,error", [
    ("OTA_DEVICE_REPORTED_FAILURE", {"error_code": "DOWNLOAD_BEGIN_FAILED"}, "OTA_DOWNLOAD_BEGIN_FAILED"),
    ("OTA_DEVICE_ROLLED_BACK", {}, "OTA_PREVIOUS_FIRMWARE_OBSERVED"),
])
def test_ota_alerts_resolve_on_later_successful_deployment(db, code, details, error):
    connector = connector_fixture(db)
    connector.last_error_code = error
    row = open_alert(db, connector, code, details=details, seen=utc_now() - timedelta(hours=2))
    seen = ensure_utc(row.last_seen_at)
    deployment(db, connector, completed_minutes_ago=180, index=1)  # finished before the failure
    heartbeat(db, connector, 1)
    assert row.state == "OPEN" and connector.last_error_code == error
    later = deployment(db, connector, completed_minutes_ago=10, index=2)
    deployment(db, connector, status="FAILED", completed_minutes_ago=5, index=3)
    heartbeat(db, connector, 2)
    assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen
    assert row.details["resolution"]["kind"] == "DEPLOYMENT_SUCCEEDED"
    assert row.details["resolution"]["deployment_id"] == later.deployment_id
    assert connector.last_error_code is None


def explained(health):
    """Every non-ONLINE tier and every device error is backed by a visible reason."""
    tiers = {reason.tier for reason in health.reasons}
    if health.tier == "DEGRADED":
        assert "DEGRADED" in tiers
    if health.tier == "ONLINE_WITH_WARNINGS":
        assert tiers == {"WARNING"}
    if health.last_error_code:
        assert any(reason.gating and reason.error_code == health.last_error_code for reason in health.reasons)
    assert all(reason.message and reason.clear_condition for reason in health.reasons)
    return health


def test_p02_scenario_enforced(db):
    recovery_release(db)
    connector = p02(db)
    terminal = {"online": True, "connection_state": "ONLINE", "serial": "CJH9211060009"}
    update_heartbeat(db, connector=connector, boot_id="recovery-boot", sequence=1, payload=HeartbeatPayload(
        firmware_version="2.6.27", uptime_seconds=60, led_state="HEALTHY", zkt=terminal,
        ota={"image_sha256": RECOVERY_DIGEST},
        diagnostics={"workers": [worker("add_delivery", "STOPPED"), worker("ords_delivery", "STOPPED")]}))
    db.flush()
    assert alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT") is None
    assert connector.lifecycle_state == "ONLINE"
    # A worker row an earlier recovery run raised before this release, then 2.5.2 boots.
    row = open_alert(db, connector, "ESP_DELIVERY_WORKER_FAULT", details={"diagnostics_schema_version": 2},
                     seen=utc_now() - timedelta(hours=20))
    result = heartbeat(db, connector, 1, boot="p02-252", firmware="2.5.2", uptime=600, zkt=terminal)
    assert row.state == "OPEN" and row.details["binding"] == "INFERRED_PREVIOUS"
    assert (connector.lifecycle_state, result["state"]) == ("ONLINE_WITH_WARNINGS", "ONLINE_WITH_WARNINGS")
    assert connector.last_error_code == "ESP_DELIVERY_WORKER_FAULT"
    assert result["health"]["primary"]["code"] == "ESP_DELIVERY_WORKER_FAULT"
    reason = explained(evaluate_health(db, connector)).reasons[0]
    assert (reason.tier, reason.currency) == ("WARNING", "PREVIOUS_BOOT")
    assert reason.message and reason.clear_condition.startswith("Clears when every required delivery worker")


@pytest.mark.parametrize("enabled", [False, True])
def test_bound_worker_fault_boot_ended_only_when_enforced(db, monkeypatch, enabled):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, boot="boot-a", diagnostics={
        "workers": [worker("add_delivery", "STOPPED", tick=999_000), worker("ords_delivery", tick=999_000)]})
    row = alert_row(db, connector, "ESP_DELIVERY_WORKER_FAULT")
    seen = ensure_utc(row.last_seen_at)
    heartbeat(db, connector, 1, boot="boot-b", uptime=30)
    if enabled:
        assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen
        assert row.details["resolution"] | {"at": None} == {
            "kind": "BOOT_ENDED", "previous_boot_id": "boot-a", "current_boot_id": "boot-b",
            "current_firmware": "2.6.15", "at": None}
        assert (connector.lifecycle_state, connector.last_error_code) == ("ONLINE", None)
    else:
        assert row.state == "OPEN" and connector.lifecycle_state == "DEGRADED"


@pytest.mark.parametrize("state,offline_minutes,lifecycle,link", [
    ("RECOVERING", None, "ONLINE", "STABILIZING"),
    ("SUSPECT", 1, "ONLINE", "RECONNECTING"),
    ("CONNECTING", 1, "ONLINE", "RECONNECTING"),
    ("RETRY_WAIT", 1, "ONLINE", "RECONNECTING"),
    ("DISCOVERING", 1, "ONLINE", "RECONNECTING"),
    ("BOOTING", 1, "ONLINE", "STARTING"),
    ("RETRY_WAIT", 6, "ONLINE_WITH_WARNINGS", "DISCONNECTED"),
    ("SESSION_REFRESH", None, "ONLINE", "MAINTENANCE"),
    ("RESTARTING", 1, "ONLINE", "MAINTENANCE"),
])
def test_zkt_link_states_enforced(db, state, offline_minutes, lifecycle, link):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1)
    if offline_minutes:
        connector.zkt_device.offline_since = utc_now() - timedelta(minutes=offline_minutes)
    result = heartbeat(db, connector, 2, zkt={"online": state == "RECOVERING", "connection_state": state,
                                              "serial": SERIAL})
    assert (result["state"], result["terminal_link"]["state"], connector.last_error_code) == (lifecycle, link, None)
    explained(evaluate_health(db, connector))


def test_flapping_is_a_gating_warning_until_the_link_proves_stable(db):
    connector = connector_fixture(db)
    result = heartbeat(db, connector, 1, zkt={"online": False, "connection_state": "FLAPPING", "serial": SERIAL,
                                              "flap_count_15m": 4})
    assert (result["state"], connector.last_error_code) == ("ONLINE_WITH_WARNINGS", "ZKT_CONNECTION_FLAPPING")
    result = heartbeat(db, connector, 2, zkt={"online": True, "connection_state": "ONLINE", "serial": SERIAL,
                                              "consecutive_successes": 3})
    assert (result["state"], connector.last_error_code) == ("ONLINE", None)


def hik_heartbeat(db, connector, sequence, *, state="ONLINE", poll_error=0, uptime=1000):
    result = update_heartbeat(db, connector=connector, boot_id="hik-boot", sequence=sequence, payload=HeartbeatPayload(
        firmware_family="hikvision", firmware_version="3.1.0", uptime_seconds=uptime, led_state="HEALTHY",
        terminal=dict(schema_version=2, vendor="hikvision", protocol="isapi", serial=SERIAL, ip_address="192.0.2.1",
                      capability_profile="pilot", qualification_state="NOT_QUALIFIED", online=state == "ONLINE",
                      connection_state=state, stream_open=False, stream_error=0, current_event_count=0,
                      replay_event_count=0, last_stream_message_epoch=0, source_storage_failures=0,
                      source_queue_depth=0, capture_mode="poll", poll_interval_seconds=2, poll_error=poll_error)))
    db.flush()
    return result


def test_hikvision_enforced(db):
    connector = connector_fixture(db)
    connector.firmware_family = "hikvision"
    db.commit()
    result = hik_heartbeat(db, connector, 1, poll_error=8)
    assert (result["state"], connector.last_error_code) == ("DEGRADED", "HIK_STORAGE")
    result = hik_heartbeat(db, connector, 2, state="OFFLINE", poll_error=2)
    assert (result["state"], connector.last_error_code) == ("ONLINE", None)
    connector.zkt_device.offline_since = utc_now() - timedelta(minutes=6)
    result = hik_heartbeat(db, connector, 3, state="OFFLINE", poll_error=2)
    assert (result["state"], connector.last_error_code) == ("ONLINE_WITH_WARNINGS", "HIK_NETWORK")
    explained(evaluate_health(db, connector))
    result = hik_heartbeat(db, connector, 4, poll_error=3)
    assert (result["state"], connector.last_error_code) == ("ONLINE_WITH_WARNINGS", "HIK_AUTH")
    result = hik_heartbeat(db, connector, 5)
    assert (result["state"], connector.last_error_code) == ("ONLINE", None)


def test_quarantine_persists_across_terminal_offline_heartbeat_and_stream_connect(db, envelopes):
    first = connector_fixture(db)
    first.zkt_device.serial, first.zkt_device.online = SERIAL, True
    second = connector_fixture(db, hardware_id="e0:72:a1:d6:f3:29", expected_serial=SERIAL)
    heartbeat(db, second, 1)
    assert second.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    heartbeat(db, second, 2, zkt={"online": False, "connection_state": "RETRY_WAIT", "serial": SERIAL})
    assert second.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"
    assert second.last_error_code == "QUARANTINED_DUPLICATE_SERIAL"
    db.commit()
    assert add_web.set_stream_connected(second.id, True) == "QUARANTINED_DUPLICATE_SERIAL"
    db.refresh(second)
    assert second.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL"


@pytest.mark.parametrize("enabled,state", [(True, "OFFLINE"), (False, "ONLINE")])
def test_stream_connect_keeps_lifecycle_until_a_heartbeat(db, envelopes, monkeypatch, enabled, state):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = connector_fixture(db)
    connector.lifecycle_state, connector.connected = "OFFLINE", False
    db.commit()
    assert add_web.set_stream_connected(connector.id, True) == state
    db.refresh(connector)
    assert connector.connected and connector.lifecycle_state == state
    assert evaluate_health(db, connector).derived_lifecycle == state
    assert heartbeat(db, connector, 1)["state"] == "ONLINE"


def test_rejection_derivation_enforced(db, envelopes):
    custody = live(db, connector_fixture(db))
    reject(custody, envelopes(custody, "queue_evidence", {}), ValueError("bad"))
    db.refresh(custody)
    assert (custody.lifecycle_state, custody.last_error_code) == ("DEGRADED", "DEVICE_MESSAGE_REJECTED")
    log = live(db, second_connector(db))
    reject(log, envelopes(log, "log", {}), ValueError("bad"))
    db.refresh(log)
    assert (log.lifecycle_state, log.last_error_code) == ("ONLINE_WITH_WARNINGS", "DEVICE_MESSAGE_REJECTED")
    server = live(db, second_connector(db, 2))
    outage = OperationalError("SELECT 1", {}, Exception("down"))
    reject(server, envelopes(server, "heartbeat", {}), outage)
    db.refresh(server)
    assert (server.lifecycle_state, server.last_error_code) == ("ONLINE", None)
    row = alert_row(db, server, "ADD_MESSAGE_PROCESSING_FAILED")
    details = deepcopy(row.details)
    details["types"]["heartbeat"]["first_at"] = (utc_now() - timedelta(seconds=130)).isoformat()
    row.details = details
    db.commit()
    reject(server, envelopes(server, "heartbeat", {}), outage)
    db.refresh(server)
    assert (server.lifecycle_state, server.last_error_code) == ("DEGRADED", None)
    for connector in (custody, log, server):
        explained(evaluate_health(db, connector))


@pytest.mark.parametrize("enabled", [False, True])
def test_telemetry_carries_add_health_in_both_modes(db, monkeypatch, enabled):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = connector_fixture(db)
    heartbeat(db, connector, 1, led="LOCAL_FAILURE")
    row = db.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == connector.id))
    assert row.payload["_add_health"] == {
        "v": 1, "mode": "ENFORCED" if enabled else "SHADOW", "lifecycle": "DEGRADED",
        "derived_lifecycle": "DEGRADED", "tier": "DEGRADED", "reasons": ["ESP_LOCAL_FAILURE:DEGRADED:CURRENT"],
        "terminal": "CONNECTED"}


@pytest.mark.parametrize("enabled", [False, True])
def test_hil_known_hold_unchanged_for_current_faults(factory, monkeypatch, enabled):  # noqa: F811
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    session, _release, devices, _ = factory
    device = devices[0]
    serial = device.zkt_device.serial
    update_heartbeat(session, connector=device, boot_id="hold-boot", sequence=1, payload=HeartbeatPayload(
        firmware_version="2.6.15", uptime_seconds=1000, led_state="LOCAL_FAILURE",
        zkt={"online": True, "connection_state": "ONLINE", "serial": serial},
        diagnostics={"storage": latch_storage("zone_lite.c:7389"), "workers": legacy_sample()["workers"]}))
    session.flush()
    assert device.last_error_code == "ESP_DURABILITY_FAULT"
    assert _known_hold(session, device) == "CONNECTOR_ERROR_REQUIRES_REVIEW"


def test_factory_previous_boot_durability_on_252(factory):  # noqa: F811
    session, release, devices, _ = factory
    device = devices[0]
    open_alert(session, device, "ESP_DURABILITY_FAULT", details={
        "binding": "OBSERVED", "boot_id": "earlier-2615-boot", "firmware_version": "2.6.15"},
        seen=utc_now() - timedelta(days=1))
    health = explained(apply_device_health(session, device, source="READ"))
    assert (health.reasons[0].tier, health.reasons[0].currency) == ("WARNING", "PREVIOUS_BOOT")
    assert device.last_error_code == "ESP_DURABILITY_FAULT"
    with pytest.raises(ValueError, match="FACTORY_TERMINAL_NOT_READY"):
        zkt_factory_trial.predecessor_snapshot(session, release, device)
    device.last_error_code = None
    with pytest.raises(ValueError, match="FACTORY_EXISTING_SAFETY_HOLD"):
        zkt_factory_trial.predecessor_snapshot(session, release, device)


@pytest.fixture()
def sweep(db, monkeypatch):
    @contextmanager
    def scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(add_worker, "session_scope", scope)
    monkeypatch.setattr(add_worker, "PROCESS_STARTED_AT", utc_now() - timedelta(hours=1))

    def run(seconds_from_now=0):
        updates = add_worker.mark_stale_connectors(utc_now() + timedelta(seconds=seconds_from_now))
        db.expire_all()
        return updates
    return run


def silent(db, connector, seconds, *, connected=True, lifecycle="ONLINE"):
    connector.last_seen_at = utc_now() - timedelta(seconds=seconds)
    connector.connected, connector.lifecycle_state = connected, lifecycle
    db.commit()
    return connector


def test_sweep_respects_startup_grace(db, sweep, monkeypatch):
    connector = silent(db, live(db, connector_fixture(db)), 60)
    monkeypatch.setattr(add_worker, "PROCESS_STARTED_AT", utc_now() - timedelta(seconds=30))
    assert sweep() == []
    assert connector.connected and connector.lifecycle_state == "ONLINE"
    assert sweep(61)[0]["connector_id"] == connector.connector_id
    assert (connector.connected, connector.lifecycle_state) == (False, "OFFLINE")


def test_sweep_marks_offline_at_45s_alerts_at_120s_once_and_does_not_realert_long_offline_devices(db, sweep):
    recent = silent(db, live(db, connector_fixture(db)), 50)
    long_gone = silent(db, live(db, second_connector(db)), 7200, connected=False, lifecycle="OFFLINE")
    assert [update["connector_id"] for update in sweep()] == [recent.connector_id]
    assert (recent.connected, recent.lifecycle_state) == (False, "OFFLINE")
    assert alert_row(db, recent, "ESP_OFFLINE") is None  # silent for 50 s: no alert yet
    sweep(80)
    sweep(90)
    rows = db.scalars(select(DeviceAlert).where(DeviceAlert.code == "ESP_OFFLINE")).all()
    assert [(row.connector_id, row.state) for row in rows] == [(recent.id, "OPEN")]
    assert alert_row(db, long_gone, "ESP_OFFLINE") is None
    assert heartbeat(db, recent, 1)["state"] == "ONLINE"
    assert alert_row(db, recent, "ESP_OFFLINE").state == "RESOLVED"


@pytest.mark.parametrize("enabled,state", [(True, "QUARANTINED_DUPLICATE_SERIAL"), (False, "OFFLINE")])
def test_sweep_keeps_quarantine_when_enforced(db, sweep, monkeypatch, enabled, state):
    monkeypatch.setattr(settings, "device_health_derived_enabled", enabled)
    connector = silent(db, live(db, connector_fixture(db)), 60, lifecycle="QUARANTINED_DUPLICATE_SERIAL")
    sweep()
    assert (connector.connected, connector.lifecycle_state) == (False, state)
    assert sweep() == []  # nothing left to mark


def test_heartbeat_stale_enforced(db, sweep):
    connector = connector_fixture(db)
    heartbeat(db, connector, 1)
    db.commit()
    connector.zkt_device.last_seen_at = utc_now() - timedelta(seconds=100)
    connector.last_seen_at = utc_now()  # a log message keeps the transport fresh
    db.commit()
    assert sweep() == [{"connector_id": connector.connector_id, "state": "DEGRADED"}]
    assert (connector.lifecycle_state, connector.last_error_code) == ("DEGRADED", None)
    assert [reason.code for reason in evaluate_health(db, connector).reasons] == ["HEARTBEAT_STALE"]
    assert heartbeat(db, connector, 2)["state"] == "ONLINE"


def test_rejected_heartbeat_lets_sweep_mark_offline(db, envelopes, sweep):
    connector = silent(db, live(db, connector_fixture(db)), 40)
    reject(connector, envelopes(connector, "heartbeat", {}), ValueError("DIAGNOSTICS_SAMPLE_MISMATCH"))
    sweep(10)
    assert (connector.connected, connector.lifecycle_state) == (False, "OFFLINE")


def test_hikvision_huge_poll_epoch_never_breaks_terminal_link(db):
    connector = live(db, connector_fixture(db), state="ONLINE", family="hikvision", firmware="3.1.0", poll_error=2)
    connector.zkt_device.capability_profile = {"hikvision_health": {
        "poll_error": 2, "last_successful_poll_epoch": 10 ** 18}}
    assert terminal_link(connector)["state"] in {"RECONNECTING", "STARTING", "DISCONNECTED"}


def test_2615_latch_survives_a_heartbeat_without_diagnostics(db):
    connector = connector_fixture(db)
    sample = legacy_sample()
    sample["storage"] = latch_storage()
    heartbeat(db, connector, 1, led="LOCAL_FAILURE", diagnostics=sample)
    assert evaluate_health(db, connector).derived_lifecycle == "ONLINE_WITH_WARNINGS"
    # 2.6.15 drops the whole diagnostics object when an allocation fails.
    heartbeat(db, connector, 2, led="LOCAL_FAILURE")
    health = evaluate_health(db, connector)
    assert {reason.code: reason.tier for reason in health.reasons} == {
        "ESP_DURABILITY_FAULT": "WARNING", "ESP_LOCAL_FAILURE": "WARNING"}
    assert connector.lifecycle_state == "ONLINE_WITH_WARNINGS"
    # A new boot starts without the old classification.
    heartbeat(db, connector, 1, boot="boot-c", led="LOCAL_FAILURE")
    assert "latch" not in alert_row(db, connector, "ESP_LOCAL_FAILURE").details


def test_sweep_alerts_a_device_that_died_during_a_long_backend_outage(db, sweep):
    connector = silent(db, live(db, connector_fixture(db)), 1800)  # still marked connected
    sweep()
    sweep(5)  # the next tick
    assert (connector.connected, connector.lifecycle_state) == (False, "OFFLINE")
    assert alert_row(db, connector, "ESP_OFFLINE").state == "OPEN"


def test_fleet_counts_terminal_attention(db):
    live(db, connector_fixture(db), state="RETRY_WAIT", offline_age=400)
    live(db, second_connector(db), state="FLAPPING")
    live(db, second_connector(db, 2), state="RECOVERING")
    counts = fleet_counts(db)
    assert counts["terminal_attention"] == 2
    assert counts["total"] == 3


# Firmware reports its version as "zone-lite-X.Y.Z"; every rule compares the release.
def test_reported_zone_lite_versions_match_release_rules(db, monkeypatch):
    monkeypatch.setattr(settings, "device_health_latched_led_tier", "WARNING")
    latched = connector_fixture(db)
    sample = legacy_sample()
    sample["storage"] = latch_storage()
    heartbeat(db, latched, 1, firmware="zone-lite-2.6.15", led="LOCAL_FAILURE", diagnostics=sample)
    assert alert_row(db, latched, "ESP_LOCAL_FAILURE").details["latch"]["firmware_version"] == "2.6.15"
    assert latched.lifecycle_state == "ONLINE_WITH_WARNINGS"
    recovery_release(db)
    p02_connector = p02(db)
    apply(db, p02_connector, {"workers": [worker("add_delivery", "STOPPED")]}, firmware="zone-lite-2.6.27",
          image=RECOVERY_DIGEST)
    assert alert_row(db, p02_connector, "ESP_DELIVERY_WORKER_FAULT") is None
    legacy = live(db, second_connector(db, 3), firmware="zone-lite-2.5.2")
    coverage = {item["key"]: item for item in device_health.coverage(legacy)}
    assert coverage["storage"]["detail"] == "Not reported by firmware 2.5.2."
    open_alert(db, legacy, "ESP_DURABILITY_FAULT", details={"binding": "INFERRED_PREVIOUS"})
    assert evaluate_health(db, legacy).reasons[0].tier == "WARNING"
