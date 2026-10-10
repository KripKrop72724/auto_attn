"""Audited operator actions on device health.

A person may resolve an alert with a recorded reason, or re-evaluate a stale
device error. Both need a password step-up (checked by the endpoint before
any lock), are idempotent by key, lock the connector before its alerts (the
heartbeat's order) and never move an alert's last_seen_at. Neither can hide
a condition the latest evidence still asserts.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.device_health import (
    Health,
    alert_reason,
    apply_device_health,
    duplicate_serial_claimed,
    evaluate_health,
    operator_policy,
)
from zk_add.models import AuditEvent, Connector, DeviceAlert
from zk_add.service import close_alert_row, upsert_alert
from zk_add.time_utils import utc_now

RESOLVED_BY_OPERATOR = "ALERT_RESOLVED_BY_OPERATOR"
DEVICE_ERROR_CLEARED = "DEVICE_ERROR_CLEARED"


class HealthActionError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, **extra):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message, **extra}


def _replayed(session: Session, action: str, idempotency_key: str, *, target_id: str, actor: str) -> dict | None:
    row = session.scalar(select(AuditEvent).where(
        AuditEvent.action == action, AuditEvent.request_id == idempotency_key))
    if row is None:
        return None
    if row.target_id != target_id or row.actor != actor:
        raise HealthActionError("IDEMPOTENCY_KEY_REUSED",
                                "This idempotency key was already used for a different action.")
    return {**(row.after or {}).get("result", {}), "replayed": True}


def device_error(connector: Connector, health: Health) -> dict:
    stored = connector.last_error_code
    return {
        "code": stored, "message": connector.last_error_message, "derived_code": health.last_error_code,
        "backed": stored is not None and stored == health.last_error_code,
        "backing_alert_ids": [reason.alert_id for reason in health.reasons
                              if stored and reason.error_code == stored and reason.alert_id],
    }


def _rederive(session: Session, connector: Connector, *, source: str) -> Health:
    health = apply_device_health(session, connector, source=source)
    if health.mode == "SHADOW":
        # The legacy writers own lifecycle; only the device error is corrected.
        connector.last_error_code = health.last_error_code
        connector.last_error_message = health.last_error_message
    return health


def _lock_connector(session: Session, *, connector_pk: int | None = None, connector_id: str | None = None):
    clause = Connector.id == connector_pk if connector_pk is not None else Connector.connector_id == connector_id
    return session.scalar(select(Connector).where(clause).with_for_update())


def resolve_alert_by_operator(session: Session, *, alert_id: int, actor: str, reason: str, idempotency_key: str,
                              ip_address: str | None) -> dict:
    target = str(alert_id)
    replay = _replayed(session, RESOLVED_BY_OPERATOR, idempotency_key, target_id=target, actor=actor)
    if replay is not None:
        return replay
    row = session.get(DeviceAlert, alert_id)
    if row is None:
        raise HealthActionError("ALERT_NOT_FOUND", "Alert not found.", status=404)
    connector = _lock_connector(session, connector_pk=row.connector_id)
    replay = _replayed(session, RESOLVED_BY_OPERATOR, idempotency_key, target_id=target, actor=actor)
    if replay is not None:
        return replay
    row = session.scalar(select(DeviceAlert).where(DeviceAlert.id == alert_id).with_for_update()
                         .execution_options(populate_existing=True))
    if row.state not in {"OPEN", "ACKNOWLEDGED"}:
        raise HealthActionError("ALERT_NOT_ACTIVE", "This alert is already resolved.")
    now = utc_now()
    health = evaluate_health(session, connector, now=now)
    current = next((item for item in health.reasons + health.other_active_alerts if item.alert_id == row.id), None)
    current = current or alert_reason(row, connector, now=now, link=health.terminal_link)
    policy = operator_policy(current, duplicate_claimed=(
        duplicate_serial_claimed(session, connector) if row.code == "QUARANTINED_DUPLICATE_SERIAL" else None))
    if not policy["resolvable"]:
        raise HealthActionError(policy["refusal_code"], policy["refusal"], clear_condition=current.clear_condition)
    before = {"alert_state": row.state, "lifecycle_state": connector.lifecycle_state,
              "last_error_code": connector.last_error_code}
    close_alert_row(row, resolution={"kind": "OPERATOR", "actor": actor, "reason": reason.strip()},
                    touch_last_seen=False, now=now)
    residual = None
    if row.code == "ESP_DURABILITY_FAULT":
        # Storage that was never verified stays a gating warning (and a HIL
        # and factory hold) until firmware that reports storage verifies it.
        source = row.details or {}
        residual = upsert_alert(
            session, connector, code="ESP_PRESERVATION_UNVERIFIED", severity="WARNING",
            message=("An operator resolved a storage durability fault that firmware never verified; "
                     "attendance preservation is unverified."),
            details={"source_alert_id": row.id, "resolved_by": actor,
                     **{key: source[key] for key in ("boot_id", "firmware_version", "binding", "evidence")
                        if key in source}})
    health = _rederive(session, connector, source="OPERATOR")
    session.flush()
    result = {"alert_id": row.id, "device_state": connector.lifecycle_state,
              "device_error": device_error(connector, health),
              "residual_alert_id": residual.id if residual is not None else None}
    append_audit(
        session, actor=actor, action=RESOLVED_BY_OPERATOR, target_type="alert", target_id=target,
        request_id=idempotency_key, outcome="RESOLVED", ip_address=ip_address, before=before,
        after={"alert_state": "RESOLVED", "code": row.code, "reason": reason.strip(),
               "lifecycle_state": connector.lifecycle_state, "last_error_code": connector.last_error_code,
               "result": result},
    )
    return result


def clear_device_error(session: Session, *, connector_id: str, expected_code: str, actor: str, reason: str,
                       idempotency_key: str, ip_address: str | None) -> dict:
    replay = _replayed(session, DEVICE_ERROR_CLEARED, idempotency_key, target_id=connector_id, actor=actor)
    if replay is not None:
        return replay
    connector = _lock_connector(session, connector_id=connector_id)
    if connector is None:
        raise HealthActionError("CONNECTOR_NOT_FOUND", "Connector not found.", status=404)
    replay = _replayed(session, DEVICE_ERROR_CLEARED, idempotency_key, target_id=connector_id, actor=actor)
    if replay is not None:
        return replay
    if connector.last_error_code != expected_code:
        raise HealthActionError("DEVICE_ERROR_CHANGED", "The device error changed; review it again.",
                                current_code=connector.last_error_code)
    health = evaluate_health(session, connector)
    if health.last_error_code == expected_code:
        raise HealthActionError(
            "DEVICE_ERROR_STILL_BACKED", "An active alert still backs this device error.",
            alert_ids=[item.alert_id for item in health.reasons if item.error_code == expected_code and item.alert_id])
    before = {"lifecycle_state": connector.lifecycle_state, "last_error_code": connector.last_error_code}
    health = _rederive(session, connector, source="OPERATOR")
    result = {"device_state": connector.lifecycle_state, "device_error": device_error(connector, health)}
    append_audit(
        session, actor=actor, action=DEVICE_ERROR_CLEARED, target_type="connector", target_id=connector_id,
        request_id=idempotency_key, outcome="CLEARED", ip_address=ip_address, before=before,
        after={"lifecycle_state": connector.lifecycle_state, "last_error_code": connector.last_error_code,
               "reason": reason.strip(), "result": result},
    )
    return result
