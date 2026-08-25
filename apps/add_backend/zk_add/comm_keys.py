from __future__ import annotations

import base64
import json
import os
from datetime import timedelta
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.models import (
    CommKeyOperation,
    Connector,
    ConnectorCommKeyState,
    DeviceCommand,
    DeviceCommandEvent,
    ZKTDevice,
)
from zk_add.onboarding import derive_bootstrap_secret, normalize_mac
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now


COMM_KEY_ACTIVE_OPERATION_STATES = {
    "PENDING_CAPABILITY",
    "QUEUED",
    "WAITING_FOR_DEVICE",
    "DISPATCHED",
    "ACKNOWLEDGED",
    "RUNNING",
    "RETRYING",
    "CANCEL_REQUESTED",
    "RECONCILIATION_REQUIRED",
}
COMM_KEY_TERMINAL_STATES = {
    "APPLIED", "FAILED", "CANCELLED", "CANCELED", "EXPIRED", "INDETERMINATE"
}
CONFIG_HKDF_SALT = b"state-life-zone-lite-config-v1"
CONFIG_HKDF_INFO_PREFIX = "zone-lite-config:"


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _secret_fernet() -> Fernet:
    if not settings.comm_key_secret_fernet_key:
        raise RuntimeError("ADD_COMM_KEY_SECRET_FERNET_KEY is required.")
    return Fernet(settings.comm_key_secret_fernet_key.encode("ascii"))


def encrypt_managed_key(value: str) -> str:
    return _secret_fernet().encrypt(value.encode("ascii")).decode("ascii")


def decrypt_managed_key(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _secret_fernet().decrypt(value.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise RuntimeError("Stored COMM Key cannot be decrypted with the active key version.") from exc


def _envelope_key(connector: Connector) -> bytes:
    mac = normalize_mac(connector.hardware_id)
    bootstrap = derive_bootstrap_secret(mac).encode("ascii")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=CONFIG_HKDF_SALT,
        info=f"{CONFIG_HKDF_INFO_PREFIX}{mac}".encode("ascii"),
    ).derive(bootstrap)


def envelope_aad(
    *,
    connector: Connector,
    operation_id: str,
    revision: int,
    mode: str,
    expected_serial: str,
    expires_epoch: int,
) -> str:
    return "\n".join(
        (
            "zone-lite-config-v1",
            connector.connector_id,
            normalize_mac(connector.hardware_id),
            operation_id,
            str(revision),
            mode,
            expected_serial,
            str(expires_epoch),
        )
    )


def seal_comm_key(
    *,
    connector: Connector,
    operation_id: str,
    revision: int,
    mode: str,
    expected_serial: str,
    expires_epoch: int,
    comm_key: str,
) -> dict:
    aad = envelope_aad(
        connector=connector,
        operation_id=operation_id,
        revision=revision,
        mode=mode,
        expected_serial=expected_serial,
        expires_epoch=expires_epoch,
    ).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_envelope_key(connector)).encrypt(
        nonce, comm_key.encode("ascii"), aad
    )
    return {
        "version": 1,
        "algorithm": "HKDF-SHA256-AES-256-GCM",
        "nonce": _urlsafe(nonce),
        "ciphertext": _urlsafe(ciphertext),
    }


def comm_key_state(session: Session, connector: Connector, *, create: bool = False):
    row = session.scalar(
        select(ConnectorCommKeyState).where(
            ConnectorCommKeyState.connector_id == connector.id
        )
    )
    if row is None and create:
        row = ConnectorCommKeyState(connector_id=connector.id)
        session.add(row)
        session.flush()
    return row


def active_operation(session: Session, connector: Connector) -> CommKeyOperation | None:
    return session.scalar(
        select(CommKeyOperation)
        .where(
            CommKeyOperation.connector_id == connector.id,
            CommKeyOperation.status.in_(COMM_KEY_ACTIVE_OPERATION_STATES),
        )
        .order_by(CommKeyOperation.created_at.desc())
        .limit(1)
    )


def serialize_operation(operation: CommKeyOperation | None) -> dict | None:
    if operation is None:
        return None
    return {
        "operation_id": operation.operation_id,
        "mode": operation.mode,
        "requested_revision": operation.requested_revision,
        "expected_terminal_serial": operation.expected_terminal_serial,
        "status": operation.status,
        "error_code": operation.error_code,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "expires_at": operation.expires_at,
        "completed_at": operation.completed_at,
    }


def serialize_comm_key_state(session: Session, connector: Connector) -> dict:
    state = comm_key_state(session, connector)
    operation = active_operation(session, connector)
    zkt = connector.zkt_device
    terminal_write = bool(
        connector.comm_key_capable
        and zkt
        and zkt.online
        and zkt.connection_state == "ONLINE"
        and zkt.capability_profile.get("comm_key_write_v1", False)
        and (zkt.confirmed_serial or zkt.expected_serial)
    )
    terminal_write_reason = None
    if not connector.comm_key_capable:
        terminal_write_reason = "FIRMWARE_UPDATE_REQUIRED"
    elif not zkt or not (zkt.confirmed_serial or zkt.expected_serial):
        terminal_write_reason = "TERMINAL_SERIAL_REQUIRED"
    elif not zkt.online or zkt.connection_state != "ONLINE":
        terminal_write_reason = "CURRENT_KEY_AUTHENTICATION_REQUIRED"
    elif not zkt.capability_profile.get("comm_key_write_v1", False):
        terminal_write_reason = "TERMINAL_MODEL_NOT_CERTIFIED"
    return {
        "enabled": settings.comm_key_management_enabled,
        "reveal_enabled": settings.comm_key_reveal_enabled,
        "management_state": state.status if state else "UNKNOWN",
        "applied_revision": state.applied_revision if state else 0,
        "desired_revision": state.desired_revision if state else 0,
        "last_verified_at": state.last_verified_at if state else None,
        "verified_terminal_serial": state.expected_terminal_serial if state else None,
        "last_error_code": state.last_error_code if state else None,
        "managed": bool(state and state.applied_secret_encrypted),
        "capabilities": {
            "esp_only": bool(connector.comm_key_capable),
            "esp_and_terminal": terminal_write,
            "esp_and_terminal_block_reason": None if terminal_write else terminal_write_reason,
            "recovery_staging": not connector.comm_key_capable,
        },
        "active_operation": serialize_operation(operation),
    }


def _validate_expected_terminal(
    session: Session, connector: Connector, expected_serial: str
) -> tuple[ZKTDevice, bool]:
    zkt = connector.zkt_device
    if zkt is None:
        raise ValueError("No assigned ZKT terminal.")
    known = zkt.confirmed_serial or zkt.expected_serial or zkt.serial
    if known and expected_serial != known:
        raise ValueError("Expected terminal serial does not match the connector binding evidence.")
    collision = session.scalar(
        select(ZKTDevice).where(
            ZKTDevice.id != zkt.id,
            or_(
                ZKTDevice.serial == expected_serial,
                ZKTDevice.expected_serial == expected_serial,
                ZKTDevice.confirmed_serial == expected_serial,
            ),
        )
    )
    if collision:
        raise ValueError("Expected terminal serial is already associated with another connector.")
    return zkt, known is None


def materialize_comm_key_command(
    session: Session,
    *,
    connector: Connector,
    operation: CommKeyOperation,
    state: ConnectorCommKeyState,
) -> DeviceCommand:
    if operation.command_id is not None:
        command = session.get(DeviceCommand, operation.command_id)
        if command is None:
            raise RuntimeError("COMM Key operation references a missing command.")
        return command
    if not connector.comm_key_capable:
        raise ValueError("Connector has not advertised COMM Key management capability.")
    if ensure_utc(operation.expires_at) <= utc_now():
        operation.status = "EXPIRED"
        operation.error_code = "COMM_KEY_OPERATION_EXPIRED"
        operation.completed_at = utc_now()
        state.status = "EXPIRED"
        state.pending_secret_encrypted = None
        state.last_error_code = operation.error_code
        raise ValueError("COMM Key operation expired before capable firmware connected.")
    comm_key = decrypt_managed_key(state.pending_secret_encrypted)
    if not comm_key:
        raise RuntimeError("Pending COMM Key material is unavailable.")
    expires_epoch = int(ensure_utc(operation.expires_at).timestamp())
    command_uuid = str(uuid4())
    envelope = seal_comm_key(
        connector=connector,
        operation_id=operation.operation_id,
        revision=operation.requested_revision,
        mode=operation.mode,
        expected_serial=operation.expected_terminal_serial,
        expires_epoch=expires_epoch,
        comm_key=comm_key,
    )
    from zk_add.service import create_command

    command = create_command(
        session,
        connector=connector,
        command_type="APPLY_CONFIG",
        payload={
            "config_field": "zkt_comm_key",
            "operation_id": operation.operation_id,
            "revision": operation.requested_revision,
            "mode": operation.mode,
            "expected_serial": operation.expected_terminal_serial,
            "expires_epoch": expires_epoch,
            "sealed_value": envelope,
        },
        expected_state={
            "serial": operation.expected_terminal_serial,
            "comm_key_revision": state.applied_revision,
        },
        desired_state={"comm_key_revision": operation.requested_revision},
        idempotency_key=f"comm-key:{operation.operation_id}",
        actor=operation.actor,
        expires_in_seconds=max(1, expires_epoch - int(utc_now().timestamp())),
        command_id=command_uuid,
    )
    operation.command_id = command.id
    operation.status = command.status
    operation.updated_at = utc_now()
    state.status = command.status
    state.updated_at = utc_now()
    return command


def create_comm_key_change(
    session: Session,
    *,
    connector: Connector,
    new_key: str,
    mode: str,
    expected_revision: int,
    expected_terminal_serial: str,
    reason: str,
    typed_confirmation: str,
    idempotency_key: str,
    actor: str,
) -> tuple[CommKeyOperation, DeviceCommand | None]:
    if not settings.comm_key_management_enabled:
        raise ValueError("COMM Key management is disabled.")
    duplicate = session.scalar(
        select(CommKeyOperation).where(
            CommKeyOperation.connector_id == connector.id,
            CommKeyOperation.idempotency_key == idempotency_key,
        )
    )
    if duplicate:
        command = session.get(DeviceCommand, duplicate.command_id) if duplicate.command_id else None
        return duplicate, command
    expected_confirmation = f"CHANGE {connector.connector_id} {expected_terminal_serial}"
    if typed_confirmation.strip() != expected_confirmation:
        raise ValueError(f"Type '{expected_confirmation}' to confirm this key operation.")
    zkt, requires_serial_attestation = _validate_expected_terminal(
        session, connector, expected_terminal_serial
    )
    existing_active = active_operation(session, connector)
    if existing_active:
        raise ValueError(f"COMM Key operation {existing_active.operation_id} is still active.")
    command_active = session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.status.in_(
                {
                    "WAITING_FOR_DEVICE", "WAITING_FOR_ZKT", "QUEUED", "RETRYING",
                    "DISPATCHED", "ACKNOWLEDGED", "RUNNING", "CANCEL_REQUESTED",
                }
            ),
        )
    )
    if command_active:
        raise ValueError(f"Device already has active command {command_active.command_id}.")
    if mode == "ESP_AND_TERMINAL":
        if not connector.comm_key_capable:
            raise ValueError("ESP and terminal rotation requires capable firmware first.")
        if not zkt or not zkt.online or zkt.connection_state != "ONLINE":
            raise ValueError("Current-key ZKT authentication is required for terminal rotation.")
        if not zkt.capability_profile.get("comm_key_write_v1", False):
            raise ValueError("This terminal model/firmware is not certified for remote key writes.")
    state = comm_key_state(session, connector, create=True)
    assert state is not None
    if expected_revision != state.applied_revision or state.desired_revision != state.applied_revision:
        raise ValueError("COMM Key revision changed; refresh before submitting another operation.")
    now = utc_now()
    if requires_serial_attestation:
        zkt.expected_serial = expected_terminal_serial
        zkt.terminal_binding_state = "RECOVERY_EXPECTED_SERIAL"
        zkt.certification_state = "READ_ONLY"
        zkt.writes_disabled_reason = "RECOVERY_SERIAL_PENDING_PROOF"
    revision = state.applied_revision + 1
    operation = CommKeyOperation(
        connector_id=connector.id,
        mode=mode,
        requested_revision=revision,
        expected_terminal_serial=expected_terminal_serial,
        status="QUEUED" if connector.comm_key_capable else "PENDING_CAPABILITY",
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
        expires_at=now + timedelta(seconds=settings.comm_key_operation_seconds),
    )
    session.add(operation)
    session.flush()
    state.pending_secret_encrypted = encrypt_managed_key(new_key)
    state.desired_revision = revision
    state.status = operation.status
    state.mode = mode
    state.expected_terminal_serial = expected_terminal_serial
    state.last_error_code = None
    state.updated_by = actor
    state.updated_at = now
    command = None
    if connector.comm_key_capable:
        command = materialize_comm_key_command(
            session, connector=connector, operation=operation, state=state
        )
    append_audit(
        session,
        actor=actor,
        action="COMM_KEY_CHANGE_REQUESTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=operation.status,
        after={
            "operation_id": operation.operation_id,
            "mode": mode,
            "revision": revision,
            "expected_terminal_serial": expected_terminal_serial,
        },
    )
    if requires_serial_attestation:
        append_audit(
            session,
            actor=actor,
            action="COMM_KEY_RECOVERY_SERIAL_ATTESTED",
            target_type="connector",
            target_id=connector.connector_id,
            outcome="PENDING_FIRMWARE_PROOF",
            after={
                "operation_id": operation.operation_id,
                "expected_terminal_serial": expected_terminal_serial,
                "reason": reason,
                "terminal_binding_state": zkt.terminal_binding_state,
            },
        )
    return operation, command


def dispatch_pending_comm_key_operation(
    session: Session, *, connector: Connector
) -> DeviceCommand | None:
    if not connector.comm_key_capable:
        return None
    operation = session.scalar(
        select(CommKeyOperation)
        .where(
            CommKeyOperation.connector_id == connector.id,
            CommKeyOperation.status == "PENDING_CAPABILITY",
        )
        .order_by(CommKeyOperation.created_at.asc())
        .limit(1)
    )
    if operation is None:
        return None
    state = comm_key_state(session, connector)
    if state is None:
        raise RuntimeError("Pending COMM Key operation has no durable state.")
    return materialize_comm_key_command(
        session, connector=connector, operation=operation, state=state
    )


def reconcile_comm_key_heartbeat(session: Session, *, connector: Connector) -> None:
    """Detect firmware rollback or NVS loss without treating a heartbeat as key proof."""
    state = comm_key_state(session, connector)
    if (
        state is None
        or state.status != "APPLIED"
        or active_operation(session, connector) is not None
        or connector.comm_key_revision == state.applied_revision
    ):
        return
    state.status = "RECONCILIATION_REQUIRED"
    state.last_error_code = "COMM_KEY_REVISION_DRIFT"
    state.updated_at = utc_now()
    append_audit(
        session,
        actor=f"connector:{connector.connector_id}",
        action="COMM_KEY_REVISION_DRIFT_DETECTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="RECONCILIATION_REQUIRED",
        after={
            "managed_revision": state.applied_revision,
            "reported_revision": connector.comm_key_revision,
        },
    )


def expire_staged_comm_key_operations(session: Session) -> int:
    now = utc_now()
    expired = 0
    for operation in session.scalars(
        select(CommKeyOperation).where(
            CommKeyOperation.status == "PENDING_CAPABILITY",
            CommKeyOperation.expires_at <= now,
        )
    ).all():
        connector = session.get(Connector, operation.connector_id)
        state = comm_key_state(session, connector) if connector is not None else None
        operation.status = "EXPIRED"
        operation.error_code = "COMM_KEY_OPERATION_EXPIRED"
        operation.updated_at = now
        operation.completed_at = now
        if state is not None:
            state.pending_secret_encrypted = None
            state.desired_revision = state.applied_revision
            state.status = "APPLIED" if state.applied_secret_encrypted else "EXPIRED"
            state.last_error_code = operation.error_code
            state.updated_at = now
        if connector is not None:
            append_audit(
                session,
                actor="maintenance",
                action="COMM_KEY_OPERATION_EXPIRED",
                target_type="connector",
                target_id=connector.connector_id,
                outcome="EXPIRED",
                after={"operation_id": operation.operation_id},
            )
        expired += 1
    return expired


def reconcile_comm_key_command(
    session: Session,
    *,
    connector: Connector,
    command: DeviceCommand,
    status: str,
    result: dict,
    error_code: str | None,
) -> tuple[str, str | None, str | None]:
    operation = session.scalar(
        select(CommKeyOperation).where(CommKeyOperation.command_id == command.id)
    )
    if operation is None:
        return "FAILED", "COMM_KEY_OPERATION_MISSING", "Configuration operation is unavailable."
    state = comm_key_state(session, connector)
    if state is None:
        return "FAILED", "COMM_KEY_STATE_MISSING", "Configuration state is unavailable."
    now = utc_now()
    operation.updated_at = now
    if status not in {"SUCCEEDED", "FAILED", "CANCELLED", "CANCELED", "EXPIRED"}:
        operation.status = status
        state.status = status
        state.updated_at = now
        return status, error_code, None
    if status == "SUCCEEDED":
        valid = (
            result.get("config_field") == "zkt_comm_key"
            and result.get("applied_revision") == operation.requested_revision
            and result.get("verified_serial") == operation.expected_terminal_serial
            and result.get("authentication_verified") is True
        )
        if not valid:
            status = "FAILED"
            error_code = "COMM_KEY_POSTCONDITION_FAILED"
        else:
            state.applied_secret_encrypted = state.pending_secret_encrypted
            state.pending_secret_encrypted = None
            state.applied_revision = operation.requested_revision
            state.desired_revision = operation.requested_revision
            state.status = "APPLIED"
            state.last_verified_at = now
            state.last_error_code = None
            connector.comm_key_revision = operation.requested_revision
            operation.status = "APPLIED"
            operation.completed_at = now
            append_audit(
                session,
                actor=f"connector:{connector.connector_id}",
                action="COMM_KEY_CHANGE_APPLIED",
                target_type="connector",
                target_id=connector.connector_id,
                outcome="APPLIED",
                after={
                    "operation_id": operation.operation_id,
                    "mode": operation.mode,
                    "revision": operation.requested_revision,
                    "verified_terminal_serial": operation.expected_terminal_serial,
                },
            )
            return "SUCCEEDED", None, None
    indeterminate = error_code == "COMM_KEY_INDETERMINATE"
    operation.status = "INDETERMINATE" if indeterminate else status
    operation.error_code = error_code
    operation.completed_at = now
    state.status = operation.status
    state.last_error_code = error_code
    state.updated_at = now
    if not indeterminate:
        state.pending_secret_encrypted = None
        state.desired_revision = state.applied_revision
    return status, error_code, None


def sanitize_comm_key_result(result: dict) -> dict:
    """Allow only non-secret postcondition evidence from an untrusted connector."""
    allowed = {
        "config_field",
        "applied_revision",
        "verified_serial",
        "authentication_verified",
        "terminal_updated",
        "duplicate",
        "recovery_resolution",
    }
    sanitized = {key: result[key] for key in allowed if key in result}
    if "applied_revision" in sanitized:
        try:
            sanitized["applied_revision"] = int(sanitized["applied_revision"])
        except (TypeError, ValueError):
            sanitized.pop("applied_revision", None)
    for key in ("authentication_verified", "terminal_updated", "duplicate"):
        if key in sanitized:
            sanitized[key] = sanitized[key] is True
    return sanitized


def reveal_comm_key(session: Session, *, connector: Connector) -> tuple[str, ConnectorCommKeyState]:
    if not settings.comm_key_reveal_enabled:
        raise ValueError("COMM Key reveal is disabled.")
    state = comm_key_state(session, connector)
    if state is None or state.status != "APPLIED" or not state.applied_secret_encrypted:
        raise ValueError("No successfully applied ADD-managed COMM Key is available.")
    value = decrypt_managed_key(state.applied_secret_encrypted)
    if not value:
        raise ValueError("No successfully applied ADD-managed COMM Key is available.")
    return value, state


def cancel_comm_key_operation(
    session: Session, *, operation: CommKeyOperation, actor: str
) -> tuple[Connector, DeviceCommand | None, bool]:
    if operation.status in COMM_KEY_TERMINAL_STATES:
        raise ValueError("This COMM Key operation is already complete.")
    if operation.status in {"RUNNING", "ACKNOWLEDGED"}:
        raise ValueError("A running COMM Key mutation cannot be cancelled safely.")
    connector = session.get(Connector, operation.connector_id)
    if connector is None:
        raise ValueError("COMM Key operation connector is unavailable.")
    state = comm_key_state(session, connector)
    command = session.get(DeviceCommand, operation.command_id) if operation.command_id else None
    remote_cancel = False
    now = utc_now()
    if command is not None:
        if command.status in {"RUNNING", "ACKNOWLEDGED", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
            raise ValueError("The device command can no longer be cancelled safely.")
        remote_cancel = command.attempt_count > 0 or command.status == "DISPATCHED"
        command.status = "CANCEL_REQUESTED" if remote_cancel else "CANCELLED"
        if not remote_cancel:
            command.completed_at = now
        session.add(
            DeviceCommandEvent(
                command_id=command.id,
                status=command.status,
                details={"requested_by": actor},
            )
        )
    operation.status = "CANCEL_REQUESTED" if remote_cancel else "CANCELLED"
    operation.updated_at = now
    if not remote_cancel:
        operation.completed_at = now
        if state:
            state.pending_secret_encrypted = None
            state.desired_revision = state.applied_revision
            state.status = "APPLIED" if state.applied_secret_encrypted else "UNKNOWN"
            state.last_error_code = None
            state.updated_at = now
    append_audit(
        session,
        actor=actor,
        action=f"COMM_KEY_OPERATION_{operation.status}",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=operation.status,
        after={"operation_id": operation.operation_id},
    )
    return connector, command, remote_cancel


def canonical_envelope_json(value: dict) -> str:
    """Stable helper used by contract tests without exposing key material."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
