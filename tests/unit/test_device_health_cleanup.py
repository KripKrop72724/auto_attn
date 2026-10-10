"""Stranded-alert cleanup: a read-only signed preview, then a locked, audited apply."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from test_add_backend import connector_fixture, db as db
from test_device_health import admin_client, deployment, heartbeat, live, open_alert, second_connector
from zk_add.device_health_cleanup import sign_plan
from zk_add.models import AuditEvent, DeviceAlert, DeviceTelemetry
from zk_add.ota import FirmwareHilRun
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_hil_schedule import _known_hold

P02 = ("bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", "e0:72:a1:d7:05:c4", "CJH9211060009")
P06 = ("233dac02-eb1b-4598-a876-e3a7b1ecfd54", "e0:72:a1:d5:08:a0", "CJH9211060002")
PASSWORD = "correct-password"


def peshawar(db, target, *, boot):
    connector = connector_fixture(db, hardware_id=target[1], expected_serial=target[2])
    connector.connector_id = target[0]
    live(db, connector, firmware="2.5.2")
    connector.boot_id = boot
    zkt = connector.zkt_device
    zkt.serial = zkt.confirmed_serial = target[2]
    connector.ota_capable = connector.ota_secure_boot = connector.ota_rollback_enabled = True
    connector.ota_partition_layout = "zone-lite-ota-v1"
    db.commit()
    return connector


@pytest.fixture()
def fleet(db):
    """P02 and P06 as they were on 10 Oct: back on 2.5.2 with stranded recovery-run alerts."""
    p02, p06 = peshawar(db, P02, boot="p02-252"), peshawar(db, P06, boot="p06-252")
    raised = utc_now() - timedelta(days=2)
    worker = open_alert(db, p02, "ESP_DELIVERY_WORKER_FAULT", seen=raised, details={"diagnostics_schema_version": 2})
    db.add(DeviceTelemetry(connector_id=p02.id, boot_id="recovery-boot", sequence=9, uptime_seconds=600,
                           created_at=raised - timedelta(seconds=10), payload={
                               "firmware_version": "2.6.27", "diagnostics": {"workers": [
                                   {"name": "add_delivery", "state": "STOPPED"},
                                   {"name": "ords_delivery", "state": "STOPPED"}]}}))
    durability = open_alert(db, p02, "ESP_DURABILITY_FAULT", seen=raised, details={
        "binding": "OBSERVED", "boot_id": "earlier-boot", "firmware_version": "2.6.15",
        "evidence": {"summary": "storage DEGRADED; write failures 3"}})
    rejection = open_alert(db, p02, "DEVICE_MESSAGE_REJECTED", seen=utc_now() - timedelta(days=3),
                           details={"message_type": "queue_evidence", "error_category": "EVIDENCE_INVALID"})
    data = open_alert(db, p02, "TERMINAL_SOURCE_EXCEPTION")
    p06_worker = open_alert(db, p06, "ESP_DELIVERY_WORKER_FAULT", seen=raised,
                            details={"binding": "INFERRED_PREVIOUS"})
    p02.last_error_code = p06.last_error_code = "ESP_DELIVERY_WORKER_FAULT"
    db.commit()
    return {"p02": p02, "p06": p06, "worker": worker, "durability": durability, "rejection": rejection,
            "data": data, "p06_worker": p06_worker}


def preview(client, connector_ids=(P02[0], P06[0])):
    response = client.post("/api/v1/device-health/cleanup/preview", json={"connector_ids": list(connector_ids)})
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, headers, plan, *, alert_ids=None, fixes=None, confirmation=None, key="cleanup-key-1",
          password=PASSWORD, **changes):
    body = {
        "connector_ids": [P02[0], P06[0]], "digest": plan["digest"], "expires_at": plan["expires_at"],
        "signature": plan["signature"],
        "alert_ids": plan["default_alert_ids"] if alert_ids is None else alert_ids,
        "error_fix_connector_ids": plan["default_error_fix_connector_ids"] if fixes is None else fixes,
        "reason": "Stranded recovery-run alerts on Peshawar", "password": password, "idempotency_key": key,
        "typed_confirmation": plan["typed_confirmation"] if confirmation is None else confirmation, **changes,
    }
    return client.post("/api/v1/device-health/cleanup/apply", headers=headers, json=body)


def snapshot(db):
    db.expire_all()
    return sorted((row.id, row.state, ensure_utc(row.last_seen_at)) for row in db.scalars(select(DeviceAlert)))


def test_preview_p02_p06_lists_stranded_rows_with_evidence(db, fleet):
    client, _headers = admin_client(db)
    before = snapshot(db)
    plan = preview(client)
    assert snapshot(db) == before
    rows = {row["alert_id"]: row for row in plan["rows"]}
    worker = rows[fleet["worker"].id]
    assert (worker["class"], worker["default_selected"]) == ("STRANDED_DIAGNOSTICS", True)
    assert (worker["raising_firmware"], worker["raising_boot_id"]) == ("2.6.27", "recovery-boot")
    assert worker["raising_diagnostics"] == "add_delivery STOPPED; ords_delivery STOPPED"
    assert "start no delivery workers by design" in worker["rationale"]
    durability = rows[fleet["durability"].id]
    assert (durability["class"], durability["default_selected"], durability["residual_code"]) == (
        "STRANDED_DIAGNOSTICS", False, "ESP_PRESERVATION_UNVERIFIED")
    assert durability["evidence"] == "storage DEGRADED; write failures 3"
    assert (rows[fleet["rejection"].id]["class"], rows[fleet["rejection"].id]["default_selected"]) == (
        "CONDITION_CLEARED", True)
    assert rows[fleet["p06_worker"].id]["default_selected"] is True
    assert fleet["data"].id not in rows
    connectors = {row["connector_id"]: row for row in plan["connectors"]}
    assert (connectors[P02[0]]["last_error_after"], connectors[P02[0]]["error_fix"]) == ("ESP_DURABILITY_FAULT", True)
    assert connectors[P02[0]]["hold_effects"] == ["HIL hold CONNECTOR_ERROR_REQUIRES_REVIEW kept",
                                                  "Factory hold FACTORY_TERMINAL_NOT_READY kept"]
    assert (connectors[P06[0]]["last_error_after"], connectors[P06[0]]["gate_effect"]) == (None, "HOLD_LIFTED")
    assert connectors[P06[0]]["derived_lifecycle_after"] == "ONLINE"
    assert plan["typed_confirmation"] == "RESOLVE 3 ALERTS ON 2 DEVICES"
    assert plan["suggested_first_scope"] == [P02[0], P06[0]]
    assert plan["blocked"] == plan["skipped"] == []


def test_preview_excludes_current_conditions_and_blocks_hil_observing(db, fleet):
    current = live(db, second_connector(db, 7))
    open_alert(db, current, "ESP_DURABILITY_FAULT")  # unbound and current on 2.6.15
    open_alert(db, current, "ZKT_CONNECTION_FLAPPING", severity="WARNING")  # the link is not stable
    observed = deployment(db, fleet["p06"], status="BOOTED_PENDING", index=5)
    db.add(FirmwareHilRun(run_id="hil-run-1", deployment_id=observed.id, connector_id=fleet["p06"].id,
                          release_id=observed.release_id, actor="test", idempotency_key="hil", target={},
                          release_identity={}, baseline={}, ends_at=utc_now() + timedelta(hours=1)))
    db.commit()
    client, _headers = admin_client(db)
    plan = preview(client, (P02[0], P06[0], current.connector_id))
    assert plan["blocked"] == [{"connector_id": P06[0], "reason": "HIL_OBSERVING"}]
    assert {row["connector_id"] for row in plan["rows"]} == {P02[0]}


def test_offline_device_condition_cleared_rows_are_skipped(db, fleet):
    fleet["p02"].connected = False
    db.commit()
    client, _headers = admin_client(db)
    plan = preview(client)
    assert plan["skipped"] == [{"alert_id": fleet["rejection"].id, "connector_id": P02[0],
                                "code": "DEVICE_MESSAGE_REJECTED", "reason": "DEVICE_OFFLINE"}]
    assert fleet["worker"].id in {row["alert_id"] for row in plan["rows"]}  # diagnostics need no fresh device


def test_apply_disabled_by_flag(db, fleet, monkeypatch):
    monkeypatch.setattr(settings, "device_health_cleanup_apply_enabled", False)
    client, headers = admin_client(db)
    response = apply(client, headers, preview(client))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "CLEANUP_DISABLED"


def test_apply_gates(db, fleet):
    client, headers = admin_client(db)
    plan = preview(client)
    assert apply(client, headers, plan, password="wrong-password").status_code == 403
    assert apply(client, {}, plan).status_code == 403
    forged = apply(client, headers, plan, signature="0" * 64)
    assert forged.json()["detail"]["code"] == "PREVIEW_INVALID"
    tampered = apply(client, headers, plan, expires_at=(utc_now() + timedelta(hours=1)).isoformat())
    assert tampered.json()["detail"]["code"] == "PREVIEW_INVALID"  # the signature binds the expiry
    past = utc_now() - timedelta(minutes=1)
    signed = sign_plan(digest=plan["digest"], expires_at=past, actor="StateHealthAdmin",
                       connector_ids=[P02[0], P06[0]])
    expired = apply(client, headers, plan, expires_at=past.isoformat(), signature=signed)
    assert expired.json()["detail"]["code"] == "PREVIEW_EXPIRED"
    mismatch = apply(client, headers, plan, confirmation="RESOLVE 3 ALERTS ON 1 DEVICES")
    assert mismatch.json()["detail"]["code"] == "CONFIRMATION_MISMATCH"
    outside = apply(client, headers, plan, alert_ids=[fleet["data"].id], confirmation="RESOLVE 1 ALERTS ON 2 DEVICES")
    assert outside.json()["detail"]["code"] == "SELECTION_OUT_OF_SCOPE"
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "DEVICE_HEALTH_CLEANUP_APPLIED")).all() == []


def test_apply_detects_scope_change(db, fleet):
    client, headers = admin_client(db)
    plan = preview(client)
    heartbeat(db, fleet["p02"], 1, boot="p02-252", firmware="2.5.2", led="LOCAL_FAILURE",
              zkt={"online": True, "connection_state": "ONLINE", "serial": P02[2]})
    db.commit()
    response = apply(client, headers, plan)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SCOPE_CHANGED"


def test_apply_resolves_audits_and_rederives(db, fleet):
    client, headers = admin_client(db)
    plan = preview(client)
    selected = plan["default_alert_ids"] + [fleet["durability"].id]
    seen = {row.id: ensure_utc(row.last_seen_at) for row in db.scalars(select(DeviceAlert))}
    response = apply(client, headers, plan, alert_ids=selected, confirmation="RESOLVE 4 ALERTS ON 2 DEVICES")
    assert response.status_code == 200, response.text
    result = response.json()
    assert sorted(result["resolved_alert_ids"]) == sorted(selected) and len(result["residual_alert_ids"]) == 1
    db.expire_all()
    for alert_id in selected:
        row = db.get(DeviceAlert, alert_id)
        assert row.state == "RESOLVED" and ensure_utc(row.last_seen_at) == seen[alert_id]
        assert row.details["resolution"]["kind"] == "CLEANUP"
    residual = db.get(DeviceAlert, result["residual_alert_ids"][0])
    assert (residual.code, residual.state, residual.details["source_alert_id"]) == (
        "ESP_PRESERVATION_UNVERIFIED", "OPEN", fleet["durability"].id)
    assert fleet["data"].state == "OPEN"
    p02, p06 = fleet["p02"], fleet["p06"]
    assert (p02.lifecycle_state, p02.last_error_code) == ("ONLINE_WITH_WARNINGS", "ESP_PRESERVATION_UNVERIFIED")
    assert (p06.lifecycle_state, p06.last_error_code) == ("ONLINE", None)
    assert _known_hold(db, p06) != "CONNECTOR_ERROR_REQUIRES_REVIEW"
    assert _known_hold(db, p02) == "CONNECTOR_ERROR_REQUIRES_REVIEW"
    actions = [row.action for row in db.scalars(select(AuditEvent).order_by(AuditEvent.id))
               if row.action.startswith(("ALERT_RESOLVED_BY_CLEANUP", "DEVICE_HEALTH_"))]
    assert actions == ["ALERT_RESOLVED_BY_CLEANUP"] * 4 + ["DEVICE_HEALTH_REDERIVED_BY_CLEANUP"] * 2 + [
        "DEVICE_HEALTH_CLEANUP_APPLIED"]
    summary = db.scalar(select(AuditEvent).where(AuditEvent.action == "DEVICE_HEALTH_CLEANUP_APPLIED"))
    assert summary.request_id == "cleanup-key-1" and summary.after["digest"] == plan["digest"]


def test_apply_replay_returns_stored_summary(db, fleet):
    client, headers = admin_client(db)
    plan = preview(client)
    first = apply(client, headers, plan).json()
    again = apply(client, headers, plan)
    assert again.status_code == 200 and again.json() == {**first, "replayed": True}
    assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "DEVICE_HEALTH_CLEANUP_APPLIED")).all()) == 1


def test_class_c_superseded_legacy_ack(db, fleet):
    old = open_alert(db, fleet["p06"], "ESP_LOCAL_FAILURE", state="ACKNOWLEDGED", acknowledged_at=utc_now())
    newer = open_alert(db, fleet["p06"], "ESP_LOCAL_FAILURE")
    client, _headers = admin_client(db)
    rows = {row["alert_id"]: row for row in preview(client)["rows"]}
    assert (rows[old.id]["class"], rows[old.id]["default_selected"]) == ("SUPERSEDED_ACK", True)
    assert str(newer.id) in rows[old.id]["rationale"] and newer.id not in rows


def test_class_d_stale_error_code_on_offline_device(db, fleet):
    stale = second_connector(db, 9)
    stale.lifecycle_state, stale.last_error_code = "OFFLINE", "ZKT_CONNECTION_FLAPPING"
    db.commit()
    client, headers = admin_client(db)
    scope = (P02[0], P06[0], stale.connector_id)
    plan = preview(client, scope)
    row = next(item for item in plan["connectors"] if item["connector_id"] == stale.connector_id)
    assert (row["error_fix"], row["last_error_before"], row["last_error_after"]) == (
        True, "ZKT_CONNECTION_FLAPPING", None)
    response = apply(client, headers, plan, connector_ids=list(scope), alert_ids=[], fixes=[stale.connector_id],
                     confirmation="RESOLVE 0 ALERTS ON 1 DEVICES")
    assert response.status_code == 200, response.text
    db.refresh(stale)
    assert (stale.last_error_code, stale.lifecycle_state) == (None, "OFFLINE")


def test_health_switches_are_deployable_repository_variables():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/add-deploy.yml").read_text(encoding="utf-8")
    deploy = (root / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    for flag in ("DERIVED", "CLEANUP_APPLY"):
        assert (f"ADD_DEPLOY_DEVICE_HEALTH_{flag}_ENABLED: "
                f"${{{{ vars.ADD_DEVICE_HEALTH_{flag}_ENABLED }}}}") in workflow
    assert 'foreach ($healthFlag in @("DERIVED", "CLEANUP_APPLY"))' in deploy
    assert '$environment["ADD_DEVICE_HEALTH_${healthFlag}_ENABLED"] = $deployValue' in deploy


def test_current_rejection_is_never_a_cleanup_candidate(db, fleet):
    current = open_alert(db, fleet["p06"], "DEVICE_MESSAGE_REJECTED",
                         details={"message_type": "attendance_batch", "error_category": "SCHEMA_INVALID"})
    client, _headers = admin_client(db)
    assert current.id not in {row["alert_id"] for row in preview(client)["rows"]}


def test_error_fix_for_a_blocked_device_is_out_of_scope(db, fleet):
    observed = deployment(db, fleet["p06"], status="BOOTED_PENDING", index=6)
    db.add(FirmwareHilRun(run_id="hil-run-2", deployment_id=observed.id, connector_id=fleet["p06"].id,
                          release_id=observed.release_id, actor="test", idempotency_key="hil", target={},
                          release_identity={}, baseline={}, ends_at=utc_now() + timedelta(hours=1)))
    db.commit()
    client, headers = admin_client(db)
    plan = preview(client)
    assert plan["blocked"] == [{"connector_id": P06[0], "reason": "HIL_OBSERVING"}]
    response = apply(client, headers, plan, alert_ids=[], fixes=[P06[0]], confirmation="RESOLVE 0 ALERTS ON 1 DEVICES")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SELECTION_OUT_OF_SCOPE"
    db.refresh(fleet["p06"])
    assert fleet["p06"].last_error_code == "ESP_DELIVERY_WORKER_FAULT"
