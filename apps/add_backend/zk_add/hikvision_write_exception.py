"""Operator-authorized BLD1 grants, bound to one connector and terminal."""
from datetime import datetime, timezone

from sqlalchemy import select

from zk_add.audit import append_audit
from zk_add.models import AuditEvent

CONNECTOR = "dacdee7c-f824-40cc-b4c0-3d873653f8db"
MAC = "ac:27:6e:a3:0a:58"
SERIAL = "G04600476"
ZONE = "LF-ZONE-BLD1-01"
PROFILE = "ds-k1t341amf-v3.2.30-20210406-unverified-v1"
EXPIRES = 1792080000  # 2026-10-15 16:00 UTC / 21:00 PKT


def now_epoch():
    return datetime.now(timezone.utc).timestamp()


def scope_matches(connector, terminal):
    health = (terminal.capability_profile or {}).get("hikvision_health") or {}
    return (
        connector.connector_id == CONNECTOR and connector.hardware_id.lower() == MAC
        and connector.zone_id == ZONE and connector.device_id == "1"
        and connector.firmware_family == "hikvision"
        and connector.lifecycle_state != "QUARANTINED_DUPLICATE_SERIAL"
        and terminal.serial == terminal.confirmed_serial == terminal.expected_serial == SERIAL
        and terminal.terminal_binding_state == "CONFIRMED"
        and health.get("capability_profile") == PROFILE
    )


def active(connector, terminal):
    profile = terminal.capability_profile or {}
    grant = profile.get("hikvision_write_exception") or {}
    health = profile.get("hikvision_health") or {}
    approved = (
        scope_matches(connector, terminal)
        and grant.get("enabled") is True
        and grant.get("terminal_serial") == SERIAL and grant.get("connector_id") == CONNECTOR
        and terminal.snapshot_complete and terminal.identity_snapshot_stable
    )
    if not approved:
        return False
    if permanent_active(connector, terminal):
        return True
    # While the new image is being installed, fix4 retains its original expiry.
    # A permanent approval must not claim to remove fix4's firmware time limit.
    legacy_grant = grant.get("expires_epoch") == EXPIRES or (
        grant.get("permanent") is True and grant.get("expires_epoch") is None
    )
    return (
        legacy_grant and 1791990000 <= now_epoch() < EXPIRES
        and health.get("profile_command_version") == 3
        and connector.firmware_version in {"3.1.1-bld1fix4", "zone-lite-3.1.1-bld1fix4"}
    )


def permanent_active(connector, terminal):
    profile = terminal.capability_profile or {}
    grant = profile.get("hikvision_write_exception") or {}
    health = profile.get("hikvision_health") or {}
    return (
        scope_matches(connector, terminal)
        and grant.get("enabled") is True and grant.get("permanent") is True
        and grant.get("expires_epoch") is None
        and grant.get("terminal_serial") == SERIAL and grant.get("connector_id") == CONNECTOR
        and health.get("profile_command_version") == 4
        and connector.firmware_version in {
            "3.1.1-bld1fix5", "zone-lite-3.1.1-bld1fix5",
            "3.1.1-bld1fix6", "zone-lite-3.1.1-bld1fix6",
        }
        and terminal.snapshot_complete and terminal.identity_snapshot_stable
    )


def configure(session, connector, *, terminal_serial, enabled, reason, idempotency_key, actor,
              permanent=False):
    terminal = connector.zkt_device
    if not terminal or terminal_serial != SERIAL or not scope_matches(connector, terminal):
        raise ValueError("BLD1_WRITE_EXCEPTION_IDENTITY_MISMATCH")
    if enabled and not permanent and not 1791990000 <= now_epoch() < EXPIRES:
        raise ValueError("BLD1_WRITE_EXCEPTION_EXPIRED")
    request = dict(enabled=enabled, terminal_serial=SERIAL, connector_id=CONNECTOR,
                   expires_epoch=None if permanent else EXPIRES, profile_id=PROFILE, reason=reason,
                   qualification_state="NOT_QUALIFIED", administrator_role_changes=False)
    if permanent:
        request["permanent"] = True
    prior = session.scalar(select(AuditEvent).where(
        AuditEvent.action == "HIKVISION_WRITE_EXCEPTION_CONFIGURED",
        AuditEvent.target_id == CONNECTOR, AuditEvent.request_id == idempotency_key))
    if prior:
        if prior.after != request:
            raise ValueError("IDEMPOTENCY_CONFLICT")
        return request
    terminal.capability_profile = {**(terminal.capability_profile or {}),
                                   "hikvision_write_exception": request}
    append_audit(session, actor=actor, action="HIKVISION_WRITE_EXCEPTION_CONFIGURED",
                 target_type="connector", target_id=CONNECTOR, request_id=idempotency_key,
                 outcome="ENABLED" if enabled else "DISABLED", after=request)
    session.flush()
    return request
