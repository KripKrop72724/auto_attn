"""Previewed, signed and audited cleanup of stranded device-health alerts.

Nothing is deleted: a selected alert is resolved (kind CLEANUP) without moving
last_seen_at, and a condition that is still real is raised again from fresh
evidence as a new, traceable row. The preview is read-only. Apply recomputes
the plan under row locks and refuses any change in scope.

Classes: (A) STRANDED_DIAGNOSTICS, a diagnostics fault from a boot that has
ended; (B) CONDITION_CLEARED, a condition fresh evidence disproves; (C)
SUPERSEDED_ACK, a legacy acknowledged row with a newer open row; (D)
STALE_ERROR_CODE, a stored device error no active alert backs. REVIEW rows are
listed but never preselected. Current conditions, same-boot held diagnostics
and data or workflow-owned codes are never candidates.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add import storage_recovery
from zk_add.audit import append_audit
from zk_add.device_health import (
    CURRENT,
    DIAGNOSTICS_CODES,
    POLICY,
    PREVIOUS_BOOT,
    WORKFLOW_OWNED_CODES,
    _entry_current,
    _rejection_entries,
    alert_currency,
    connector_fresh,
    derive_health,
    duplicate_serial_claimed,
    gate_effect,
    plain_version,
)
from zk_add.device_health_actions import HealthActionError, _rederive
from zk_add.models import AuditEvent, Connector, DeviceAlert, DeviceTelemetry
from zk_add.service import close_alert_row, upsert_alert
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

STRANDED, CLEARED, SUPERSEDED, REVIEW = "STRANDED_DIAGNOSTICS", "CONDITION_CLEARED", "SUPERSEDED_ACK", "REVIEW"
APPLIED = "DEVICE_HEALTH_CLEANUP_APPLIED"
MAX_SCOPE, MAX_ALERTS, TELEMETRY_LOOKUPS = 50, 200, 50
DATA_CODES = WORKFLOW_OWNED_CODES | {
    "ORDS_EVENT_REJECTED", "ATTENDANCE_TIMESTAMP_QUARANTINED", "ADD_SOURCE_COVERAGE_INVALIDATED",
    "TERMINAL_SOURCE_EXCEPTION", "USER_DELETION_JOB_INCOMPLETE",
}
RECOVERY_NOTE = ("The 2.6.24-2.6.27 storage recovery images start no delivery workers by design, "
                 "so this fault describes the recovery run, not the device.")


class CleanupError(HealthActionError):
    pass


def _iso(value) -> str | None:
    return ensure_utc(value).isoformat() if value else None


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()).hexdigest()


def _scope_label(connector_ids) -> str:
    return ",".join(sorted(connector_ids)) if connector_ids else "ALL"


def sign_plan(*, digest: str, expires_at: datetime, actor: str, connector_ids) -> str:
    """Bind approval to the server-issued plan, actor, scope and expiry."""
    if not settings.pii_lookup_key:
        raise CleanupError("SIGNING_UNAVAILABLE", "The cleanup signing key is unavailable.")
    material = json.dumps(["device-health-cleanup-v1", digest, _iso(expires_at), actor, _scope_label(connector_ids)],
                          separators=(",", ":"))
    return hmac.new(settings.pii_lookup_key.encode(), material.encode(), hashlib.sha256).hexdigest()


def verify_plan(*, digest: str, expires_at: datetime, actor: str, connector_ids, signature: str) -> None:
    if not hmac.compare_digest(signature, sign_plan(digest=digest, expires_at=expires_at, actor=actor,
                                                    connector_ids=connector_ids)):
        raise CleanupError("PREVIEW_INVALID", "The cleanup preview is invalid; generate a new preview.")
    if ensure_utc(expires_at) <= utc_now():
        raise CleanupError("PREVIEW_EXPIRED", "The cleanup preview expired; generate a new preview.")


def _scope(session: Session, connector_ids, *, lock: bool) -> list[Connector]:
    statement = select(Connector).order_by(Connector.id)
    if connector_ids:
        statement = statement.where(Connector.connector_id.in_(list(connector_ids)))
    else:
        statement = statement.where(Connector.active == True, Connector.is_spare == False)  # noqa: E712
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    connectors = session.scalars(statement).all()
    missing = set(connector_ids or ()) - {connector.connector_id for connector in connectors}
    if missing:
        raise CleanupError("CONNECTOR_NOT_FOUND", "Unknown connector in the cleanup scope.", status=404,
                           connector_ids=sorted(missing))
    return list(connectors)


def _raising_telemetry(session: Session, row: DeviceAlert, budget: list[int]) -> DeviceTelemetry | None:
    """The heartbeat that last refreshed an alert written before boot binding existed."""
    if budget[0] <= 0 or row.last_seen_at is None:
        return None
    budget[0] -= 1
    seen = ensure_utc(row.last_seen_at)
    return session.scalar(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == row.connector_id, DeviceTelemetry.created_at <= seen,
        DeviceTelemetry.created_at >= seen - timedelta(seconds=300),
    ).order_by(DeviceTelemetry.created_at.desc()).limit(1))


def _diagnostics_excerpt(telemetry: DeviceTelemetry | None) -> str | None:
    diagnostics = ((telemetry.payload or {}).get("diagnostics") if telemetry is not None else None) or {}
    workers = [f"{row.get('name')} {row.get('state')}" for row in diagnostics.get("workers") or []
               if isinstance(row, dict)]
    storage = diagnostics.get("storage") if isinstance(diagnostics.get("storage"), dict) else {}
    parts = ([f"storage {storage.get('durability')}"] if storage.get("durability") else []) + workers[:6]
    return "; ".join(parts)[:300] or None


def _later_success(session: Session, connector: Connector, row: DeviceAlert):
    from zk_add.ota import FirmwareDeployment

    return session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.connector_id == connector.id, FirmwareDeployment.status == "SUCCEEDED",
        FirmwareDeployment.completed_at > row.first_seen_at,
    ).order_by(FirmwareDeployment.completed_at.desc()).limit(1))


def _latest_deployment(session: Session, connector: Connector) -> str:
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment

    latest = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == connector.id)
                            .order_by(FirmwareDeployment.id.desc()).limit(1))
    if latest is None:
        return "No deployment has run since."
    campaign = session.get(FirmwareCampaign, latest.campaign_id)
    return (f"No later deployment succeeded; the latest, {latest.deployment_id} ({latest.target_version}), is "
            f"{latest.status} in a {campaign.status if campaign else 'missing'} campaign.")


def _cleared(session: Session, connector: Connector, row: DeviceAlert, open_codes: set[str],
             now: datetime) -> tuple[str, bool, str] | None:
    """(class, preselected, rationale) when fresh evidence disproves the condition."""
    zkt = connector.zkt_device
    code = row.code
    if code == "ZKT_SERIAL_MISMATCH" and zkt is not None and zkt.serial and (
            not zkt.expected_serial or zkt.serial == zkt.expected_serial):
        return CLEARED, True, "The terminal now reports its assigned serial."
    if code == "QUARANTINED_DUPLICATE_SERIAL" and not duplicate_serial_claimed(session, connector):
        return CLEARED, True, "No other connector claims this terminal serial."
    if code == "USER_SNAPSHOT_TRUNCATED" and zkt is not None and zkt.snapshot_complete and zkt.identity_snapshot_stable:
        return CLEARED, True, "The latest user snapshot is complete and stable."
    if code == "ZKT_CONNECTION_FLAPPING" and zkt is not None and zkt.connection_state == "ONLINE" and (
            zkt.consecutive_successes or 0) >= 3:
        return CLEARED, True, f"The terminal link is online after {zkt.consecutive_successes} consecutive successes."
    if code in {"HIK_CAPTURE_UNHEALTHY", "HIK_LIGHT_RECONCILE_BLOCKED"} and zkt is not None:
        health = (zkt.capability_profile or {}).get("hikvision_health") or {}
        audit = (health.get("light_reconcile") or {}).get("state")
        if zkt.connection_state == "ONLINE" and not health.get("poll_error") and (
                code == "HIK_CAPTURE_UNHEALTHY" or audit in {"SCANNING", "COMPLETE"}):
            return CLEARED, True, "The Hikvision terminal is online and its checks succeed."
    if code == "ESP_OFFLINE":
        return CLEARED, True, "The ESP is connected and heartbeating."
    if code in {"ESP_LOCAL_FAILURE", "ESP_FATAL"} and row.state == "ACKNOWLEDGED" and code not in open_codes:
        return CLEARED, True, "The LED no longer reports this failure."
    if code in {"OTA_DEVICE_ROLLED_BACK", "OTA_DEVICE_REPORTED_FAILURE"}:
        success = _later_success(session, connector, row)
        if success is not None:
            return CLEARED, True, f"Deployment {success.deployment_id} ({success.target_version}) succeeded later."
        return REVIEW, False, _latest_deployment(session, connector)
    if code in {"DEVICE_MESSAGE_REJECTED", "ADD_MESSAGE_PROCESSING_FAILED"}:
        if alert_currency(row, connector, now=now) == CURRENT:
            return None
        entries = _rejection_entries(row).values()
        old = all(not _entry_current(entry, now) and (
            (entry.get("boot_id") and entry.get("boot_id") != connector.boot_id)
            or (ensure_utc(row.last_seen_at) < now - timedelta(hours=24))) for entry in entries)
        if old:
            return CLEARED, True, "Every rejected message type is latched and older than 24 hours or another boot."
        return REVIEW, False, "Some rejected message types are recent or from this boot."
    return None


def build_plan(session: Session, connector_ids=None, *, now: datetime | None = None, lock: bool = False) -> dict:
    now = now or utc_now()
    from zk_add.ota import FirmwareHilRun

    connectors = _scope(session, connector_ids, lock=lock)
    observing = set(session.scalars(select(FirmwareHilRun.connector_id).where(
        FirmwareHilRun.status == "OBSERVING",
        FirmwareHilRun.connector_id.in_([connector.id for connector in connectors]))).all()) if connectors else set()
    blocked = [{"connector_id": connector.connector_id, "reason": "HIL_OBSERVING"}
               for connector in connectors if connector.id in observing]
    active = [connector for connector in connectors if connector.id not in observing]
    statement = select(DeviceAlert).where(DeviceAlert.connector_id.in_([connector.id for connector in active]),
                                          DeviceAlert.state.in_(["OPEN", "ACKNOWLEDGED"])).order_by(DeviceAlert.id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    alerts = session.scalars(statement).all() if active else []
    by_connector: dict[int, list[DeviceAlert]] = {connector.id: [] for connector in active}
    for row in alerts:
        by_connector[row.connector_id].append(row)
    budget = [TELEMETRY_LOOKUPS]
    rows, skipped, summaries = [], [], []
    for connector in active:
        rows_here = by_connector[connector.id]
        open_rows = [row for row in rows_here if row.state == "OPEN"]
        open_codes = {row.code: row.id for row in open_rows}
        fresh = connector_fresh(connector, now)
        candidates = []
        for row in rows_here:
            if row.code in DATA_CODES or row.code not in POLICY:
                continue
            details = row.details or {}
            item = {
                "alert_id": row.id, "connector_id": connector.connector_id, "display_name": connector.display_name,
                "code": row.code, "state": row.state, "severity": row.severity, "message": row.message,
                "acknowledged_by": details.get("acknowledged_by"),
                "evidence": (details.get("evidence") or {}).get("summary") if isinstance(details.get("evidence"),
                                                                                         dict) else None,
                "raising_firmware": details.get("firmware_version"), "raising_boot_id": details.get("boot_id"),
                "current_firmware": connector.firmware_version, "current_boot_id": connector.boot_id,
                "last_seen_at": _iso(row.last_seen_at), "acknowledged_at": _iso(row.acknowledged_at),
            }
            if row.state == "ACKNOWLEDGED" and open_codes.get(row.code, 0) > row.id:
                candidates.append({**item, "class": SUPERSEDED, "default_selected": True,
                                   "rationale": f"Superseded by the newer open alert #{open_codes[row.code]}."})
                continue
            if row.code in DIAGNOSTICS_CODES:
                previous = alert_currency(row, connector, now=now) == PREVIOUS_BOOT
                if not previous and not details.get("binding"):
                    telemetry = _raising_telemetry(session, row, budget)
                    if telemetry is not None and telemetry.boot_id and telemetry.boot_id != connector.boot_id:
                        previous = True
                        item.update(raising_boot_id=telemetry.boot_id,
                                    raising_firmware=(telemetry.payload or {}).get("firmware_version"),
                                    raising_diagnostics=_diagnostics_excerpt(telemetry))
                if not previous:
                    continue  # current, or held on this boot: never a candidate
                durability = row.code == "ESP_DURABILITY_FAULT"
                rationale = (f"Raised on boot {item['raising_boot_id'] or 'unknown'} of firmware "
                             f"{item['raising_firmware'] or 'unknown'}; that boot has ended.")
                if plain_version(item["raising_firmware"]) in storage_recovery.VERSIONS and not durability:
                    rationale += " " + RECOVERY_NOTE
                if durability:
                    rationale += (" Storage evidence: resolving leaves ESP_PRESERVATION_UNVERIFIED, which keeps the "
                                  "HIL and factory holds until firmware that reports storage verifies it.")
                candidates.append({**item, "class": STRANDED, "default_selected": not durability,
                                   "rationale": rationale,
                                   **({"residual_code": "ESP_PRESERVATION_UNVERIFIED"} if durability else {})})
                continue
            cleared = _cleared(session, connector, row, set(open_codes), now)
            if cleared is None:
                continue
            if not fresh:
                skipped.append({"alert_id": row.id, "connector_id": connector.connector_id, "code": row.code,
                                "reason": "DEVICE_OFFLINE"})
                continue
            kind, preselected, rationale = cleared
            candidates.append({**item, "class": kind, "default_selected": preselected, "rationale": rationale})
        selected = {item["alert_id"] for item in candidates if item["default_selected"]}
        before = derive_health(connector, open_rows, now=now)
        after = derive_health(connector, [row for row in open_rows if row.id not in selected], now=now)
        stored = connector.last_error_code
        error_fix = stored != after.last_error_code
        effect = gate_effect(stored, after.last_error_code)
        holds = {"HOLD_LIFTED": "lifted", "HOLD_ADDED": "added"}.get(effect, "kept" if stored else None)
        rows.extend(candidates)
        if candidates or error_fix or any(item["connector_id"] == connector.connector_id for item in skipped):
            summaries.append({
                "connector_id": connector.connector_id, "display_name": connector.display_name,
                "connected": connector.connected, "lifecycle_state": connector.lifecycle_state,
                "derived_lifecycle_before": before.derived_lifecycle, "derived_lifecycle_after": after.derived_lifecycle,
                "last_error_before": stored, "last_error_after": after.last_error_code, "error_fix": error_fix,
                "gate_effect": effect,
                "hold_effects": [] if holds is None else [
                    f"HIL hold CONNECTOR_ERROR_REQUIRES_REVIEW {holds}",
                    f"Factory hold FACTORY_TERMINAL_NOT_READY {holds}"],
                "residuals": sorted({item["residual_code"] for item in candidates
                                     if item.get("residual_code") and item["default_selected"]}),
            })
    scope = sorted(connector.connector_id for connector in connectors)
    digest = _digest({
        "scope": scope,
        "rows": sorted([item["alert_id"], item["code"], item["state"], item["last_seen_at"], item["acknowledged_at"],
                        item["class"]] for item in rows),
        "connectors": sorted([connector.connector_id, connector.boot_id, connector.last_error_code,
                              connector.lifecycle_state] for connector in connectors),
        "blocked": sorted(item["connector_id"] for item in blocked),
        "skipped": sorted([item["alert_id"], item["reason"]] for item in skipped),
    })
    default_alerts = [item for item in rows if item["default_selected"]]
    fixes = [item["connector_id"] for item in summaries if item["error_fix"]]
    devices = {item["connector_id"] for item in default_alerts} | set(fixes)
    classes: dict[str, int] = {}
    for item in rows:
        classes[item["class"]] = classes.get(item["class"], 0) + 1
    return {
        "scope": scope, "digest": digest, "rows": rows, "connectors": summaries, "blocked": blocked,
        "skipped": skipped, "default_alert_ids": [item["alert_id"] for item in default_alerts],
        "default_error_fix_connector_ids": fixes,
        "typed_confirmation": confirmation(len(default_alerts), len(devices)),
        "counts": {"rows": len(rows), "default_selected": len(default_alerts), "error_fixes": len(fixes),
                   "blocked": len(blocked), "skipped": len(skipped), "by_class": classes},
    }


def confirmation(alerts: int, devices: int) -> str:
    return f"RESOLVE {alerts} ALERTS ON {devices} DEVICES"


def preview(session: Session, *, connector_ids, actor: str) -> dict:
    now = utc_now()
    plan = build_plan(session, connector_ids, now=now)
    expires_at = now + timedelta(seconds=settings.device_health_cleanup_preview_seconds)
    return {
        **plan, "expires_at": expires_at,
        "signature": sign_plan(digest=plan["digest"], expires_at=expires_at, actor=actor,
                               connector_ids=connector_ids),
        "suggested_first_scope": [target.connector_id for target in storage_recovery.TARGETS],
        "apply_enabled": settings.device_health_cleanup_apply_enabled,
    }


def _replayed(session: Session, idempotency_key: str, *, actor: str, digest: str) -> dict | None:
    row = session.scalar(select(AuditEvent).where(AuditEvent.action == APPLIED,
                                                  AuditEvent.request_id == idempotency_key))
    if row is None:
        return None
    if row.actor != actor or (row.after or {}).get("digest") != digest:
        raise CleanupError("IDEMPOTENCY_KEY_REUSED", "This idempotency key was already used for a different cleanup.")
    return {**(row.after or {}).get("result", {}), "replayed": True}


def apply_plan(session: Session, *, body, actor: str, ip_address: str | None) -> dict:
    if not settings.device_health_cleanup_apply_enabled:
        raise CleanupError("CLEANUP_DISABLED", "Stranded-alert cleanup is disabled on this server.")
    verify_plan(digest=body.digest, expires_at=body.expires_at, actor=actor, connector_ids=body.connector_ids,
                signature=body.signature)
    replay = _replayed(session, body.idempotency_key, actor=actor, digest=body.digest)
    if replay is not None:
        return replay
    now = utc_now()
    plan = build_plan(session, body.connector_ids, now=now, lock=True)  # connectors, then alerts, by id
    replay = _replayed(session, body.idempotency_key, actor=actor, digest=body.digest)
    if replay is not None:
        return replay
    if plan["digest"] != body.digest:
        raise CleanupError("SCOPE_CHANGED", "Device health changed since the preview; review a new preview.")
    candidates = {item["alert_id"]: item for item in plan["rows"]}
    alert_ids, fixes = sorted(set(body.alert_ids)), sorted(set(body.error_fix_connector_ids))
    fixable = {item["connector_id"] for item in plan["connectors"] if item["error_fix"]}
    if not set(alert_ids) <= set(candidates) or not set(fixes) <= fixable:
        raise CleanupError("SELECTION_OUT_OF_SCOPE", "The selection is outside the previewed plan.")
    devices = {candidates[alert_id]["connector_id"] for alert_id in alert_ids} | set(fixes)
    if body.typed_confirmation.strip() != confirmation(len(alert_ids), len(devices)):
        raise CleanupError("CONFIRMATION_MISMATCH", "The typed confirmation does not match the selection.",
                           expected=confirmation(len(alert_ids), len(devices)))
    connectors = {connector.connector_id: connector for connector in _scope(session, body.connector_ids, lock=False)}
    reason = body.reason.strip()
    resolved, residuals = [], []
    for alert_id in alert_ids:
        row = session.get(DeviceAlert, alert_id)
        item = candidates[alert_id]
        connector = connectors[item["connector_id"]]
        before = {"state": row.state, "last_seen_at": _iso(row.last_seen_at)}
        close_alert_row(row, touch_last_seen=False, now=now, resolution={
            "kind": "CLEANUP", "class": item["class"], "reason": reason, "actor": actor, "digest": body.digest})
        if row.code == "ESP_DURABILITY_FAULT":
            source = row.details or {}
            residual = upsert_alert(
                session, connector, code="ESP_PRESERVATION_UNVERIFIED", severity="WARNING",
                message=("A stranded storage durability fault was resolved by cleanup without verified storage; "
                         "attendance preservation is unverified."),
                details={"source_alert_id": row.id, "resolved_by": actor,
                         **{key: source[key] for key in ("boot_id", "firmware_version", "binding", "evidence")
                            if key in source}})
            session.flush()
            residuals.append(residual.id)
        resolved.append(alert_id)
        append_audit(session, actor=actor, action="ALERT_RESOLVED_BY_CLEANUP", target_type="alert",
                     target_id=str(alert_id), request_id=body.idempotency_key, outcome="RESOLVED",
                     ip_address=ip_address, before=before,
                     after={"state": "RESOLVED", "code": row.code, "class": item["class"], "digest": body.digest})
    rederived = []
    for connector_id in sorted(devices):
        connector = connectors[connector_id]
        before = {"lifecycle_state": connector.lifecycle_state, "last_error_code": connector.last_error_code}
        _rederive(session, connector, source="CLEANUP")
        rederived.append(connector_id)
        append_audit(session, actor=actor, action="DEVICE_HEALTH_REDERIVED_BY_CLEANUP", target_type="connector",
                     target_id=connector_id, request_id=body.idempotency_key, outcome="REDERIVED",
                     ip_address=ip_address, before=before,
                     after={"lifecycle_state": connector.lifecycle_state,
                            "last_error_code": connector.last_error_code, "digest": body.digest})
    result = {"request_id": body.idempotency_key, "digest": body.digest, "resolved_alert_ids": resolved,
              "residual_alert_ids": residuals, "rederived_connector_ids": rederived, "replayed": False}
    append_audit(session, actor=actor, action=APPLIED, target_type="device_health_cleanup",
                 target_id=_scope_label(body.connector_ids)[:120], request_id=body.idempotency_key,
                 outcome="APPLIED", ip_address=ip_address, before={"counts": plan["counts"]},
                 after={"digest": body.digest, "reason": reason, "result": result})
    return result
