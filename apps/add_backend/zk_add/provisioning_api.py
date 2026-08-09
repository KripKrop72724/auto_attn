from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.db import SessionLocal
from zk_add.models import Connector, ZKTDevice
from zk_add.provisioning import (
    HARDWARE_PROFILE,
    TERMINAL_STATES,
    FactoryFirmwareBundle,
    HardwareInspection,
    ProvisionedDeviceRecord,
    ProvisioningCompanion,
    ProvisioningCompanionNonce,
    ProvisioningConfiguration,
    ProvisioningSession,
    ProvisioningState,
    active_session_for_mac,
    append_provisioning_event,
    ensure_assignment_available,
    latest_factory_bundle,
    pairing_code_hash,
    sanitize_event_details,
    semver_key,
    serialize_bundle,
    serialize_session,
)
from zk_add.realtime import browser_events
from zk_add.security import AdminContext, admin_context, require_csrf, require_step_up
from zk_add.service import create_command
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

logger = logging.getLogger(__name__)
router = APIRouter()


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_admin(
    request: Request, db: Session = Depends(get_db)
) -> tuple[Session, AdminContext]:
    return db, admin_context(request, db)


def require_admin_mutation(
    request: Request,
    csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> tuple[Session, AdminContext]:
    context = admin_context(request, db)
    require_csrf(context, csrf)
    return db, context


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class FixedWindowLimiter:
    def __init__(self, limit: int, seconds: int) -> None:
        self.limit = limit
        self.seconds = seconds
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = [value for value in self._events.get(key, []) if value > now - self.seconds]
            if len(events) >= self.limit:
                return False
            events.append(now)
            self._events[key] = events
            return True


pairing_limiter = FixedWindowLimiter(10, 15 * 60)


@dataclass
class SecretEntry:
    configuration: ProvisioningConfiguration
    expires_at: float


class ProvisioningSecretVault:
    """Worker-local, bounded secret handoff; nothing is serialized to durable state."""

    def __init__(self) -> None:
        self._entries: dict[str, SecretEntry] = {}
        self._lock = threading.Lock()

    def put(self, session_id: str, configuration: ProvisioningConfiguration) -> None:
        with self._lock:
            self._purge()
            self._entries[session_id] = SecretEntry(
                configuration,
                time.monotonic() + settings.provisioning_session_seconds,
            )

    def pop(self, session_id: str) -> ProvisioningConfiguration | None:
        with self._lock:
            self._purge()
            entry = self._entries.pop(session_id, None)
            return entry.configuration if entry else None

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [key for key, value in self._entries.items() if value.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


secret_vault = ProvisioningSecretVault()


class CompanionHub:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, companion_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            previous = self._connections.get(companion_id)
            self._connections[companion_id] = websocket
        if previous and previous is not websocket:
            try:
                await previous.close(code=4001, reason="Superseded")
            except Exception:
                pass

    async def disconnect(self, companion_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(companion_id) is websocket:
                self._connections.pop(companion_id, None)

    async def send(self, companion_id: str, payload: dict[str, Any]) -> bool:
        async with self._lock:
            websocket = self._connections.get(companion_id)
        if not websocket:
            return False
        try:
            await websocket.send_json(payload)
            return True
        except Exception:
            await self.disconnect(companion_id, websocket)
            return False

    async def online(self, companion_id: str) -> bool:
        async with self._lock:
            return companion_id in self._connections


companion_hub = CompanionHub()


class PairingCreate(BaseModel):
    installation_id: str = Field(min_length=16, max_length=100)
    public_key: str = Field(min_length=40, max_length=200)
    platform: Literal["windows-x64", "macos-arm64"]
    application_version: str = Field(min_length=5, max_length=40)

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        try:
            key = base64.b64decode(value, validate=True)
            Ed25519PublicKey.from_public_bytes(key)
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("Installation Ed25519 public key is invalid.") from exc
        return value


class PairingApprove(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    password: str = Field(min_length=1, max_length=512)


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class SessionCreate(BaseModel):
    companion_id: str
    idempotency_key: str = Field(min_length=8, max_length=120)


class PreflightRequest(ProvisioningConfiguration):
    pass


class AuthorizationRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)
    typed_mac: str | None = None
    physical_label_acknowledged: bool = False


class TerminalBindingRequest(BaseModel):
    observed_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    password: str = Field(min_length=1, max_length=512)


class CompanionInspectionMessage(BaseModel):
    type: Literal["inspection"]
    session_id: str
    sequence: int = Field(ge=1)
    inspection: HardwareInspection
    hmac_challenge_verified: bool = False


class CompanionEventMessage(BaseModel):
    type: Literal["event"]
    session_id: str
    sequence: int = Field(ge=1)
    state: ProvisioningState
    progress: int = Field(ge=0, le=100)
    details: dict[str, Any] = Field(default_factory=dict)


def _feature_required() -> None:
    if not settings.provisioning_enabled:
        raise HTTPException(status_code=409, detail="Physical provisioning is disabled.")


def _session_or_404(db: Session, session_id: str) -> ProvisioningSession:
    row = db.scalar(
        select(ProvisioningSession).where(ProvisioningSession.session_id == session_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Provisioning session not found.")
    return row


def _companion_or_404(db: Session, companion_id: str) -> ProvisioningCompanion:
    row = db.scalar(
        select(ProvisioningCompanion).where(
            ProvisioningCompanion.companion_id == companion_id
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="Provisioning companion not found.")
    return row


def _serialize_companion(row: ProvisioningCompanion, *, online: bool) -> dict[str, Any]:
    return {
        "companion_id": row.companion_id,
        "platform": row.platform,
        "application_version": row.application_version,
        "paired": row.paired,
        "revoked": row.revoked,
        "online": online,
        "update_required": semver_key(row.application_version)
        < semver_key(settings.provisioning_companion_min_version),
        "paired_operator": row.paired_operator,
        "paired_at": row.paired_at,
        "last_contact_at": row.last_contact_at,
    }


@router.get("/api/v1/provisioning/capabilities")
def provisioning_capabilities(
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    bundle = latest_factory_bundle(db) if settings.provisioning_enabled else None
    return {
        "enabled": settings.provisioning_enabled,
        "supported_platforms": ["windows-x64", "macos-arm64"],
        "hardware_profile": HARDWARE_PROFILE,
        "companion_min_version": settings.provisioning_companion_min_version,
        "latest_bundle": serialize_bundle(bundle),
        "can_start": bool(settings.provisioning_enabled and bundle),
    }


@router.post("/companion/v1/pairings", status_code=201)
def create_pairing(request: Request, body: PairingCreate, db: Session = Depends(get_db)):
    _feature_required()
    if not pairing_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many pairing requests.")
    if semver_key(body.application_version) < semver_key(
        settings.provisioning_companion_min_version
    ):
        raise HTTPException(status_code=426, detail="Provisioning companion update required.")
    code = ""
    for _attempt in range(10):
        candidate = f"{secrets.randbelow(1_000_000):06d}"
        if not db.scalar(
            select(ProvisioningCompanion.id).where(
                ProvisioningCompanion.pairing_code_hash == pairing_code_hash(candidate),
                ProvisioningCompanion.pairing_expires_at > utc_now(),
            )
        ):
            code = candidate
            break
    if not code:
        raise HTTPException(status_code=503, detail="A unique pairing code is unavailable.")
    row = db.scalar(
        select(ProvisioningCompanion).where(
            ProvisioningCompanion.installation_id == body.installation_id
        )
    )
    now = utc_now()
    if row and row.revoked:
        raise HTTPException(status_code=403, detail="This companion installation was revoked.")
    if row is None:
        row = ProvisioningCompanion(
            installation_id=body.installation_id,
            public_key=body.public_key,
            platform=body.platform,
            application_version=body.application_version,
        )
        db.add(row)
        db.flush()
    elif row.public_key != body.public_key:
        raise HTTPException(
            status_code=409,
            detail="Installation identity changed; revoke it before pairing a replacement.",
        )
    row.platform = body.platform
    row.application_version = body.application_version
    row.pairing_code_hash = pairing_code_hash(code)
    row.pairing_expires_at = now + timedelta(seconds=settings.provisioning_pairing_seconds)
    row.updated_at = now
    return {
        "companion_id": row.companion_id,
        "pairing_code": code,
        "expires_at": row.pairing_expires_at,
        "dashboard_url": "https://attendancedevices.slichealth.com/firmware?tab=prepare",
    }


@router.post("/api/v1/provisioning/pairings/approve")
async def approve_pairing(
    request: Request,
    body: PairingApprove,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    _feature_required()
    db, context = auth
    require_step_up(body.password, db, context)
    digest = pairing_code_hash(body.code)
    now = utc_now()
    row = db.scalar(
        select(ProvisioningCompanion).where(
            ProvisioningCompanion.pairing_code_hash == digest,
            ProvisioningCompanion.pairing_expires_at > now,
            ProvisioningCompanion.revoked.is_(False),
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="Pairing code is invalid or expired.")
    row.paired = True
    row.paired_operator = context.username
    row.paired_at = now
    row.pairing_code_hash = None
    row.pairing_expires_at = None
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_COMPANION_PAIRED",
        target_type="provisioning_companion",
        target_id=row.companion_id,
        outcome="SUCCESS",
        ip_address=client_ip(request),
        after={"platform": row.platform, "application_version": row.application_version},
    )
    db.commit()
    await browser_events.publish(
        "provisioning", {"companion_id": row.companion_id, "paired": True}
    )
    return _serialize_companion(row, online=await companion_hub.online(row.companion_id))


@router.get("/api/v1/provisioning/companions")
async def list_companions(
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    rows = db.scalars(
        select(ProvisioningCompanion).order_by(ProvisioningCompanion.created_at.desc())
    ).all()
    return {
        "rows": [
            _serialize_companion(row, online=await companion_hub.online(row.companion_id))
            for row in rows
        ]
    }


@router.post("/api/v1/provisioning/companions/{companion_id}/revoke")
async def revoke_companion(
    request: Request,
    companion_id: str,
    body: PasswordBody,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    row = _companion_or_404(db, companion_id)
    row.revoked = True
    row.paired = False
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_COMPANION_REVOKED",
        target_type="provisioning_companion",
        target_id=companion_id,
        outcome="SUCCESS",
        ip_address=client_ip(request),
        after={"platform": row.platform},
    )
    db.commit()
    await companion_hub.send(companion_id, {"type": "revoked"})
    await browser_events.publish(
        "provisioning", {"companion_id": companion_id, "revoked": True}
    )
    return {"companion_id": companion_id, "revoked": True}


@router.post("/api/v1/provisioning/factory-bundles/{bundle_id}/revoke")
async def revoke_factory_bundle(
    request: Request,
    bundle_id: str,
    body: PasswordBody,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    _feature_required()
    db, context = auth
    require_step_up(body.password, db, context)
    row = db.scalar(
        select(FactoryFirmwareBundle).where(FactoryFirmwareBundle.bundle_id == bundle_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Factory firmware bundle not found.")
    row.state = "REVOKED"
    row.revoked_at = utc_now()
    row.revoked_by = context.username
    append_audit(
        db,
        actor=context.username,
        action="FACTORY_FIRMWARE_BUNDLE_REVOKED",
        target_type="factory_firmware_bundle",
        target_id=bundle_id,
        outcome="REVOKED",
        ip_address=client_ip(request),
        after={
            "hardware_profile": row.hardware_profile,
            "version": row.version,
            "manifest_sha256": row.manifest_sha256,
        },
    )
    db.commit()
    await browser_events.publish(
        "provisioning", {"bundle_id": bundle_id, "state": "REVOKED"}
    )
    return {"bundle_id": bundle_id, "state": "REVOKED"}


@router.post("/api/v1/provisioning/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreate,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    _feature_required()
    db, context = auth
    companion = _companion_or_404(db, body.companion_id)
    if not companion.paired or companion.revoked:
        raise HTTPException(status_code=409, detail="Select a paired companion.")
    if semver_key(companion.application_version) < semver_key(
        settings.provisioning_companion_min_version
    ):
        raise HTTPException(status_code=426, detail="Provisioning companion update required.")
    existing = db.scalar(
        select(ProvisioningSession).where(
            ProvisioningSession.operator == context.username,
            ProvisioningSession.idempotency_key == body.idempotency_key,
        )
    )
    if existing:
        if existing.companion_id != companion.id:
            raise HTTPException(status_code=409, detail="Idempotency key belongs to another session.")
        return serialize_session(db, existing, include_events=True)
    operator_active = db.scalar(
        select(ProvisioningSession).where(
            ProvisioningSession.operator == context.username,
            ProvisioningSession.state.not_in([item.value for item in TERMINAL_STATES]),
        )
    )
    if operator_active:
        raise HTTPException(
            status_code=409,
            detail=f"You already have active session {operator_active.session_id}.",
        )
    active = db.scalar(
        select(ProvisioningSession).where(
            ProvisioningSession.companion_id == companion.id,
            ProvisioningSession.state.not_in([item.value for item in TERMINAL_STATES]),
        )
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Companion already has active session {active.session_id}.",
        )
    bundle = latest_factory_bundle(db)
    if not bundle:
        raise HTTPException(status_code=409, detail="No approved factory bundle is available.")
    now = utc_now()
    row = ProvisioningSession(
        operator=context.username,
        companion_id=companion.id,
        bundle_id=bundle.id,
        state=ProvisioningState.WAITING_FOR_DEVICE.value,
        idempotency_key=body.idempotency_key,
        expires_at=now + timedelta(seconds=settings.provisioning_session_seconds),
    )
    db.add(row)
    db.flush()
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.WAITING_FOR_DEVICE.value,
        progress=0,
        source="SERVER",
        details={"hardware_profile": HARDWARE_PROFILE, "bundle_id": bundle.bundle_id},
    )
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_SESSION_CREATED",
        target_type="provisioning_session",
        target_id=row.session_id,
        outcome=row.state,
        ip_address=client_ip(request),
        after={"companion_id": companion.companion_id, "bundle_id": bundle.bundle_id},
    )
    db.commit()
    online = await companion_hub.send(
        companion.companion_id,
        {
            "type": "inspect",
            "session_id": row.session_id,
            "hardware_profile": HARDWARE_PROFILE,
            "bundle": serialize_bundle(bundle),
        },
    )
    if not online:
        logger.info("Companion disconnected after session %s was created", row.session_id)
    await browser_events.publish(
        "provisioning", {"session_id": row.session_id, "state": row.state}
    )
    return serialize_session(db, row, include_events=True)


@router.get("/api/v1/provisioning/sessions")
def list_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    rows = db.scalars(
        select(ProvisioningSession)
        .order_by(ProvisioningSession.created_at.desc())
        .limit(limit)
    ).all()
    return {"rows": [serialize_session(db, row) for row in rows]}


@router.get("/api/v1/provisioning/sessions/{session_id}")
def get_session(
    session_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    return serialize_session(db, _session_or_404(db, session_id), include_events=True)


@router.post("/api/v1/provisioning/sessions/{session_id}/preflight")
async def preflight_session(
    request: Request,
    session_id: str,
    body: PreflightRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = _session_or_404(db, session_id)
    if row.operator != context.username:
        raise HTTPException(status_code=403, detail="This session belongs to another operator.")
    if row.state not in {
        ProvisioningState.CONFIGURING.value,
        ProvisioningState.PREFLIGHT_READY.value,
        ProvisioningState.AWAITING_AUTHORIZATION.value,
    }:
        raise HTTPException(status_code=409, detail="Device inspection must finish first.")
    if not row.hardware_mac:
        raise HTTPException(status_code=409, detail="Detected ESP identity is missing.")
    bundle = db.get(FactoryFirmwareBundle, row.bundle_id)
    if not bundle or bundle.state != "AVAILABLE":
        raise HTTPException(status_code=409, detail="Selected factory bundle is unavailable.")
    try:
        existing = ensure_assignment_available(
            db,
            hardware_mac=row.hardware_mac,
            zone_id=body.zone_id,
            device_id=body.device_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row.zone_id = body.zone_id
    row.zone_name = body.zone_name
    row.device_id = body.device_id
    row.preferred_ip = body.preferred_ip
    row.zkt_port = body.zkt_port
    row.config_digest = body.digest()
    secret_vault.put(session_id, ProvisioningConfiguration.model_validate(body.model_dump()))
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.PREFLIGHT_READY.value,
        progress=8,
        source="SERVER",
        details={
            **body.public_metadata(),
            "assignment_mode": "TRANSFER" if existing else "NEW_ASSIGNMENT",
        },
    )
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.AWAITING_AUTHORIZATION.value,
        progress=10,
        source="SERVER",
        details={"authorization_required": True},
    )
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_PREFLIGHT_COMPLETED",
        target_type="provisioning_session",
        target_id=session_id,
        outcome="SUCCESS",
        ip_address=client_ip(request),
        after={
            "hardware_mac": row.hardware_mac,
            "zone_id": body.zone_id,
            "device_id": body.device_id,
            "bundle_id": bundle.bundle_id,
        },
    )
    db.commit()
    await browser_events.publish(
        "provisioning", {"session_id": session_id, "state": row.state}
    )
    return serialize_session(db, row, include_events=True)


async def _prepare_package(
    row: ProvisioningSession,
    bundle: FactoryFirmwareBundle,
    configuration: ProvisioningConfiguration,
) -> dict[str, Any]:
    token = settings.provisioning_internal_token
    if not token:
        raise RuntimeError("Protected provisioner token is unavailable.")
    payload = {
        "session_id": row.session_id,
        "hardware_mac": row.hardware_mac,
        "hardware_classification": row.hardware_classification,
        "recipient_public_key": row.recipient_public_key,
        "bundle_id": bundle.bundle_id,
        "bundle_storage_prefix": bundle.storage_prefix,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "configuration": configuration.model_dump(),
        "managed_defaults": {
            "zkt_recovery_enabled": False,
            "hardware_profile": HARDWARE_PROFILE,
        },
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{settings.provisioning_worker_url.rstrip('/')}/internal/v1/packages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
    allowed = {"artifact_id", "artifact_sha256", "expires_at", "manifest"}
    return {key: result[key] for key in allowed if key in result}


def _flash_command(row: ProvisioningSession, bundle: FactoryFirmwareBundle) -> dict[str, Any]:
    if not row.artifact_sha256 or not row.artifact_expires_at or not row.hardware_mac:
        raise ValueError("Provisioning artifact is incomplete.")
    public_key = settings.firmware_signing_public_key_pem_b64
    if not public_key:
        raise ValueError("Factory manifest verification key is unavailable.")
    return {
        "type": "flash",
        "session_id": row.session_id,
        "hardware_mac": row.hardware_mac,
        "classification": row.hardware_classification,
        "resume_state": row.state,
        "bundle": serialize_bundle(bundle),
        "factory_signing_public_key_pem_b64": public_key,
        "artifact": {
            "sha256": row.artifact_sha256,
            "expires_at": row.artifact_expires_at.isoformat(),
        },
        "preserve_partitions": ["storage"] if row.mode == "MANAGED_REFLASH" else [],
    }


@router.post("/api/v1/provisioning/sessions/{session_id}/authorize")
async def authorize_session(
    request: Request,
    session_id: str,
    body: AuthorizationRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = _session_or_404(db, session_id)
    if row.operator != context.username:
        raise HTTPException(status_code=403, detail="This session belongs to another operator.")
    if row.state != ProvisioningState.AWAITING_AUTHORIZATION.value:
        raise HTTPException(status_code=409, detail="Session is not awaiting authorization.")
    require_step_up(body.password, db, context)
    irreversible = row.hardware_classification in {"BLANK_NEW", "KNOWN_LEGACY"}
    if irreversible:
        normalized = (body.typed_mac or "").strip().lower().replace("-", ":")
        if not body.physical_label_acknowledged or normalized != row.hardware_mac:
            raise HTTPException(
                status_code=409,
                detail="Confirm the physical label and type the exact detected MAC.",
            )
    configuration = secret_vault.pop(session_id)
    if not configuration or configuration.digest() != row.config_digest:
        raise HTTPException(
            status_code=409,
            detail="Protected configuration expired; review the configuration again.",
        )
    bundle = db.get(FactoryFirmwareBundle, row.bundle_id)
    if not bundle or bundle.state != "AVAILABLE":
        raise HTTPException(status_code=409, detail="Selected factory bundle was revoked.")
    row.authorized_at = utc_now()
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.PACKAGE_PREPARING.value,
        progress=12,
        source="SERVER",
        details={"bundle_id": bundle.bundle_id},
    )
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_IRREVERSIBLE_AUTHORIZED"
        if irreversible
        else "PROVISIONING_REFLASH_AUTHORIZED",
        target_type="provisioning_session",
        target_id=session_id,
        outcome="AUTHORIZED",
        ip_address=client_ip(request),
        after={
            "hardware_mac": row.hardware_mac,
            "classification": row.hardware_classification,
            "bundle_id": bundle.bundle_id,
            "physical_label_acknowledged": body.physical_label_acknowledged,
        },
    )
    db.commit()
    try:
        artifact = await _prepare_package(row, bundle, configuration)
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        logger.warning("Protected package preparation failed for session %s", session_id)
        append_provisioning_event(
            db,
            row,
            state=ProvisioningState.FAILED.value,
            progress=row.progress,
            source="SERVER",
            details={"error_code": "PACKAGE_PREPARATION_FAILED"},
        )
        row.result = {"error_code": "PACKAGE_PREPARATION_FAILED"}
        db.commit()
        await browser_events.publish(
            "provisioning", {"session_id": session_id, "state": row.state}
        )
        raise HTTPException(
            status_code=503,
            detail="Protected package preparation failed; no device write was started.",
        ) from exc
    row.artifact_id = str(artifact["artifact_id"])
    row.artifact_sha256 = str(artifact["artifact_sha256"])
    row.artifact_expires_at = datetime.fromisoformat(str(artifact["expires_at"]))
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.PACKAGE_READY.value,
        progress=18,
        source="SERVER",
        details={
            "artifact_sha256": row.artifact_sha256,
            "manifest": sanitize_event_details(artifact.get("manifest", {})),
        },
    )
    db.commit()
    companion = db.get(ProvisioningCompanion, row.companion_id)
    if not companion or not await companion_hub.send(
        companion.companion_id,
        _flash_command(row, bundle),
    ):
        raise HTTPException(
            status_code=409,
            detail="Package is ready, but the companion disconnected. Reconnect to resume.",
        )
    await browser_events.publish(
        "provisioning", {"session_id": session_id, "state": row.state}
    )
    return serialize_session(db, row, include_events=True)


@router.post("/api/v1/provisioning/sessions/{session_id}/cancel")
async def cancel_session(
    request: Request,
    session_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = _session_or_404(db, session_id)
    if row.irreversible_started_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Cancellation is unavailable after an irreversible operation starts.",
        )
    append_provisioning_event(
        db,
        row,
        state=ProvisioningState.CANCELLED.value,
        progress=row.progress,
        source="OPERATOR",
    )
    secret_vault.clear(session_id)
    append_audit(
        db,
        actor=context.username,
        action="PROVISIONING_SESSION_CANCELLED",
        target_type="provisioning_session",
        target_id=session_id,
        outcome="CANCELLED",
        ip_address=client_ip(request),
        after={"hardware_mac": row.hardware_mac},
    )
    db.commit()
    companion = db.get(ProvisioningCompanion, row.companion_id)
    if companion:
        await companion_hub.send(
            companion.companion_id, {"type": "cancel", "session_id": session_id}
        )
    await browser_events.publish(
        "provisioning", {"session_id": session_id, "state": row.state}
    )
    return serialize_session(db, row, include_events=True)


@router.post("/api/v1/provisioning/sessions/{session_id}/terminal-binding/confirm")
async def confirm_terminal_binding(
    request: Request,
    session_id: str,
    body: TerminalBindingRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = _session_or_404(db, session_id)
    require_step_up(body.password, db, context)
    if row.state != ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION.value:
        raise HTTPException(status_code=409, detail="Session is not waiting for terminal confirmation.")
    connector = db.get(Connector, row.connector_id) if row.connector_id else None
    zkt = connector.zkt_device if connector else None
    if not connector or not zkt or body.observed_serial != zkt.serial:
        raise HTTPException(
            status_code=409,
            detail="Observed terminal serial changed; refresh the authenticated terminal evidence.",
        )
    collision = db.scalar(
        select(ZKTDevice).where(
            ZKTDevice.confirmed_serial == body.observed_serial,
            ZKTDevice.id != zkt.id,
        )
    )
    if collision:
        raise HTTPException(status_code=409, detail="That terminal serial is already pinned.")
    zkt.expected_serial = body.observed_serial
    zkt.confirmed_serial = body.observed_serial
    zkt.terminal_binding_state = "PENDING_DEVICE_ACK"
    zkt.serial_confirmed_by = context.username
    zkt.serial_confirmed_at = utc_now()
    zkt.certification_state = "READ_ONLY"
    zkt.writes_disabled_reason = "TERMINAL_SERIAL_PENDING_DEVICE_ACK"
    command = create_command(
        db,
        connector=connector,
        command_type="PIN_TERMINAL_SERIAL",
        payload={"serial": body.observed_serial},
        expected_state={"serial": body.observed_serial},
        desired_state={"expected_serial": body.observed_serial},
        idempotency_key=f"pin-terminal:{session_id}:{body.observed_serial}",
        actor=context.username,
        expires_in_seconds=10 * 60,
    )
    append_audit(
        db,
        actor=context.username,
        action="TERMINAL_SERIAL_CONFIRMATION_REQUESTED",
        target_type="provisioning_session",
        target_id=session_id,
        outcome=command.status,
        ip_address=client_ip(request),
        after={
            "hardware_mac": row.hardware_mac,
            "terminal_serial": body.observed_serial,
            "command_id": command.command_id,
        },
    )
    db.commit()
    await browser_events.publish(
        "provisioning",
        {"session_id": session_id, "terminal_binding_state": zkt.terminal_binding_state},
    )
    return {"session": serialize_session(db, row), "command_id": command.command_id}


@router.get("/api/v1/provisioning/sessions/{session_id}/receipt")
def provisioning_receipt(
    session_id: str,
    response: Response,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    row = _session_or_404(db, session_id)
    response.headers["Cache-Control"] = "no-store"
    payload = serialize_session(db, row, include_events=True)
    payload.pop("hardware_evidence", None)
    return {
        "schema_version": 1,
        "receipt_id": f"provisioning:{row.session_id}",
        "session": payload,
    }


def _verify_companion_release_manifest(platform: str) -> tuple[dict[str, Any], Path]:
    root = Path(settings.provisioning_companion_release_path).resolve()
    platform_directory = (root / platform).resolve()
    if root not in platform_directory.parents or not platform_directory.is_dir():
        raise HTTPException(status_code=404, detail="Companion release not found.")
    manifest_paths = sorted(
        path
        for path in platform_directory.glob("*/manifest.json")
        if not path.parent.name.startswith(".")
    )
    if not manifest_paths:
        raise HTTPException(status_code=404, detail="Companion release not found.")
    public_b64 = settings.provisioning_companion_release_public_key_b64
    if not public_b64:
        raise HTTPException(status_code=503, detail="Companion release verification is unavailable.")
    candidates: list[tuple[dict[str, Any], Path]] = []
    try:
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_b64, validate=True)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503, detail="Companion release verification key is invalid."
        ) from exc
    expected_suffix = ".exe" if platform == "windows-x64" else ".zip"
    for manifest_path in manifest_paths:
        directory = manifest_path.parent
        signature_path = directory / "manifest.sig"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        filename = Path(str(manifest.get("filename", ""))).name
        artifact = directory / filename
        try:
            key.verify(base64.b64decode(signature_path.read_text().strip()), canonical)
        except (OSError, ValueError, InvalidSignature) as exc:
            raise HTTPException(
                status_code=503, detail="Companion release signature is invalid."
            ) from exc
        if (
            manifest.get("platform") != platform
            or str(manifest.get("version", "")) != directory.name
            or not filename.endswith(expected_suffix)
            or semver_key(str(manifest.get("version", "")))[0] < 0
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_sha", "")))
            or not artifact.is_file()
        ):
            raise HTTPException(status_code=503, detail="Companion release manifest is invalid.")
        candidates.append((manifest, artifact))
    manifest, artifact = max(
        candidates, key=lambda item: semver_key(str(item[0]["version"]))
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, str(manifest.get("sha256", ""))):
        raise HTTPException(
            status_code=503, detail="Companion release failed SHA-256 verification."
        )
    if artifact.stat().st_size != int(manifest.get("size", -1)):
        raise HTTPException(status_code=503, detail="Companion release size is invalid.")
    return manifest, artifact


@router.get("/api/v1/provisioning/companion-releases/latest")
def latest_companion_release(
    platform: Literal["windows-x64", "macos-arm64"],
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    _db, _context = auth
    manifest, _artifact = _verify_companion_release_manifest(platform)
    return {
        **manifest,
        "download_url": f"/api/v1/provisioning/companion-releases/{platform}/download",
        "os_signed": False,
    }


@router.get("/api/v1/provisioning/companion-releases/{platform}/download")
def download_companion_release(
    platform: Literal["windows-x64", "macos-arm64"],
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    _db, _context = auth
    manifest, artifact = _verify_companion_release_manifest(platform)
    return FileResponse(
        artifact,
        filename=str(manifest["filename"]),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _decode_companion_key(row: ProvisioningCompanion) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(row.public_key, validate=True))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Companion installation key is invalid.") from exc


def _parse_request_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid companion timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if abs((utc_now() - parsed.astimezone(timezone.utc)).total_seconds()) > 300:
        raise HTTPException(status_code=401, detail="Companion timestamp is outside the allowed skew.")
    return parsed


def authenticate_companion_request(
    request: Request,
    db: Session,
    companion_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    body_hash: str | None,
    signature: str | None,
) -> ProvisioningCompanion:
    if not all((companion_id, timestamp, nonce, body_hash, signature)):
        raise HTTPException(status_code=401, detail="Companion signature headers are required.")
    row = _companion_or_404(db, str(companion_id))
    if not row.paired or row.revoked:
        raise HTTPException(status_code=403, detail="Companion is not paired.")
    parsed = _parse_request_timestamp(str(timestamp))
    if db.scalar(
        select(ProvisioningCompanionNonce).where(
            ProvisioningCompanionNonce.companion_id == row.id,
            ProvisioningCompanionNonce.nonce == nonce,
        )
    ):
        raise HTTPException(status_code=409, detail="Companion nonce was already used.")
    supplied_body = hashlib.sha256(b"").hexdigest()
    if not hmac.compare_digest(supplied_body, str(body_hash)):
        raise HTTPException(status_code=401, detail="Companion body digest is invalid.")
    material = "\n".join(
        [request.method.upper(), request.url.path, str(timestamp), str(nonce), str(body_hash)]
    ).encode()
    try:
        _decode_companion_key(row).verify(base64.b64decode(str(signature)), material)
    except (ValueError, InvalidSignature) as exc:
        raise HTTPException(status_code=401, detail="Companion signature is invalid.") from exc
    db.add(
        ProvisioningCompanionNonce(
            companion_id=row.id,
            nonce=str(nonce),
            request_timestamp=parsed,
        )
    )
    row.last_contact_at = utc_now()
    return row


@router.post("/companion/v1/sessions/{session_id}/artifact")
def companion_artifact(
    request: Request,
    session_id: str,
    companion_id: str | None = Header(default=None, alias="X-ADD-Companion-Id"),
    timestamp: str | None = Header(default=None, alias="X-ADD-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-ADD-Nonce"),
    body_hash: str | None = Header(default=None, alias="X-ADD-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-ADD-Signature"),
    db: Session = Depends(get_db),
):
    companion = authenticate_companion_request(
        request, db, companion_id, timestamp, nonce, body_hash, signature
    )
    row = _session_or_404(db, session_id)
    if row.companion_id != companion.id or not row.artifact_id:
        raise HTTPException(status_code=403, detail="Artifact is not assigned to this companion.")
    if not row.artifact_expires_at or ensure_utc(row.artifact_expires_at) <= utc_now():
        raise HTTPException(status_code=410, detail="Provisioning artifact expired.")
    root = Path(settings.provisioning_artifact_path).resolve()
    artifact = (root / f"{row.artifact_id}.json").resolve()
    if root not in artifact.parents or not artifact.is_file():
        raise HTTPException(status_code=404, detail="Provisioning artifact is unavailable.")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if not row.artifact_sha256 or not hmac.compare_digest(digest, row.artifact_sha256):
        raise HTTPException(status_code=503, detail="Provisioning artifact integrity failed.")
    return FileResponse(
        artifact,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _classify_inspection(
    db: Session, inspection: HardwareInspection, _hmac_challenge_verified: bool
) -> tuple[str, str]:
    record = db.scalar(
        select(ProvisionedDeviceRecord).where(
            ProvisionedDeviceRecord.hardware_mac == inspection.hardware_mac
        )
    )
    classification = inspection.efuse_classification.upper()
    if classification == "BLANK" and record is None:
        return "BLANK_NEW", "FACTORY_NEW"
    if classification == "TRUSTED_SECURE" and record:
        if record.hardware_profile != HARDWARE_PROFILE:
            return "UNKNOWN_FOREIGN", "RECOVERY"
        expected = sorted(record.secure_boot_digests)
        if expected == sorted(inspection.secure_boot_digests):
            return "KNOWN_SECURE_MANAGED", "MANAGED_REFLASH"
    # Legacy HMAC_UP devices remain fail-closed until the server, not the
    # companion, verifies the RAM-only challenge response against a protected
    # historical root. A boolean supplied by a paired workstation is never
    # sufficient evidence of root ownership.
    return "UNKNOWN_FOREIGN", "RECOVERY"


async def _handle_inspection(
    companion: ProvisioningCompanion, message: CompanionInspectionMessage
) -> None:
    with SessionLocal() as db:
        row = _session_or_404(db, message.session_id)
        if row.companion_id != companion.id:
            raise ValueError("Session belongs to another companion.")
        inspection = message.inspection
        active = active_session_for_mac(db, inspection.hardware_mac)
        if active and active.id != row.id:
            raise ValueError(f"Detected MAC already has active session {active.session_id}.")
        classification, mode = _classify_inspection(
            db, inspection, message.hmac_challenge_verified
        )
        row.hardware_mac = inspection.hardware_mac
        row.hardware_classification = classification
        row.mode = mode
        row.recipient_public_key = inspection.recipient_public_key
        existing_connector = db.scalar(
            select(Connector).where(Connector.hardware_id == inspection.hardware_mac)
        )
        evidence = inspection.model_dump(exclude={"recipient_public_key"})
        if existing_connector:
            evidence["current_assignment"] = {
                "connector_id": existing_connector.connector_id,
                "zone_id": existing_connector.zone_id,
                "zone_name": existing_connector.zone_name,
                "device_id": existing_connector.device_id,
            }
        row.hardware_evidence = sanitize_event_details(evidence)
        target = (
            ProvisioningState.RECOVERY_REQUIRED
            if classification == "UNKNOWN_FOREIGN"
            else ProvisioningState.CONFIGURING
        )
        append_provisioning_event(
            db,
            row,
            state=target.value,
            progress=5,
            source="COMPANION",
            source_sequence=message.sequence,
            details={"classification": classification, "mode": mode},
        )
        db.commit()
        await browser_events.publish(
            "provisioning",
            {
                "session_id": row.session_id,
                "state": row.state,
                "hardware_mac": row.hardware_mac,
            },
        )


def _upsert_verified_device(
    db: Session, row: ProvisioningSession, evidence: dict[str, Any]
) -> None:
    if not row.hardware_mac or not row.bundle_id:
        return
    bundle = db.get(FactoryFirmwareBundle, row.bundle_id)
    if not bundle:
        return
    record = db.scalar(
        select(ProvisionedDeviceRecord).where(
            ProvisionedDeviceRecord.hardware_mac == row.hardware_mac
        )
    )
    secure_boot_digests = list(evidence.get("secure_boot_digests") or [])
    if not secure_boot_digests or evidence.get("efuse_classification") != "TRUSTED_SECURE":
        raise ValueError("Post-boot Secure Boot and HMAC_UP evidence is incomplete.")
    if record is None:
        record = ProvisionedDeviceRecord(
            hardware_mac=row.hardware_mac,
            derivation_version="hkdf-sha256-v1",
            root_label="ADD_FLEET_ROOT_SECRET",
            efuse_purpose="HMAC_UP",
            secure_boot_digests=secure_boot_digests,
            hardware_profile=HARDWARE_PROFILE,
            bundle_hashes={},
            last_session_id=row.id,
        )
        db.add(record)
    record.secure_boot_digests = secure_boot_digests
    record.bundle_hashes = {
        "manifest_sha256": bundle.manifest_sha256,
        "images": {
            item.get("name"): item.get("sha256")
            for item in bundle.manifest.get("images", [])
            if isinstance(item, dict)
        },
    }
    record.last_session_id = row.id
    record.verified_at = utc_now()
    record.updated_at = utc_now()


async def _handle_progress(
    companion: ProvisioningCompanion, message: CompanionEventMessage
) -> None:
    with SessionLocal() as db:
        row = _session_or_404(db, message.session_id)
        if row.companion_id != companion.id:
            raise ValueError("Session belongs to another companion.")
        bundle = db.get(FactoryFirmwareBundle, row.bundle_id)
        if (
            message.state
            in {
                ProvisioningState.EFUSE_BURNING,
                ProvisioningState.FLASHING,
                ProvisioningState.READBACK_VERIFYING,
            }
            and (not bundle or bundle.state != "AVAILABLE")
            and row.irreversible_started_at is None
        ):
            raise ValueError("Factory bundle was revoked before the destructive boundary.")
        details = sanitize_event_details(message.details)
        if details.get("hardware_mac") not in {None, row.hardware_mac}:
            raise ValueError("Companion reported a different ESP MAC.")
        append_provisioning_event(
            db,
            row,
            state=message.state.value,
            progress=message.progress,
            source="COMPANION",
            source_sequence=message.sequence,
            details=details,
        )
        if message.state == ProvisioningState.WAITING_FOR_ONBOARDING:
            _upsert_verified_device(db, row, details)
        if message.state in TERMINAL_STATES:
            row.result = details
        db.commit()
        await browser_events.publish(
            "provisioning",
            {
                "session_id": row.session_id,
                "state": row.state,
                "progress": row.progress,
            },
        )


@router.websocket("/companion/v1/stream")
async def companion_stream(websocket: WebSocket):
    if not settings.provisioning_enabled:
        await websocket.close(code=4403, reason="Provisioning disabled")
        return
    installation_id = websocket.query_params.get("installation_id", "")
    with SessionLocal() as db:
        companion = db.scalar(
            select(ProvisioningCompanion).where(
                ProvisioningCompanion.installation_id == installation_id,
                ProvisioningCompanion.paired.is_(True),
                ProvisioningCompanion.revoked.is_(False),
            )
        )
        if not companion:
            await websocket.close(code=4403, reason="Companion is not paired")
            return
        companion_id = companion.companion_id
        public_key = _decode_companion_key(companion)
    await websocket.accept(subprotocol="add-provisioning-v1")
    challenge = secrets.token_bytes(32)
    await websocket.send_json(
        {
            "type": "challenge",
            "challenge": base64.b64encode(challenge).decode(),
            "companion_id": companion_id,
        }
    )
    try:
        answer = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        signature = base64.b64decode(str(answer.get("signature", "")))
        public_key.verify(signature, challenge)
    except (asyncio.TimeoutError, ValueError, InvalidSignature, WebSocketDisconnect):
        await websocket.close(code=4401, reason="Challenge signature failed")
        return
    await companion_hub.connect(companion_id, websocket)
    pending_command: dict[str, Any] | None = None
    with SessionLocal() as db:
        row = _companion_or_404(db, companion_id)
        row.last_contact_at = utc_now()
        pending = db.scalar(
            select(ProvisioningSession)
            .where(
                ProvisioningSession.companion_id == row.id,
                ProvisioningSession.state.not_in([item.value for item in TERMINAL_STATES]),
            )
            .order_by(ProvisioningSession.created_at.desc())
        )
        pending_payload = serialize_session(db, pending) if pending else None
        if pending and pending.state in {
            ProvisioningState.PACKAGE_READY.value,
            ProvisioningState.EFUSE_VERIFIED.value,
            ProvisioningState.FLASHING.value,
            ProvisioningState.READBACK_VERIFYING.value,
        }:
            bundle = db.get(FactoryFirmwareBundle, pending.bundle_id)
            if bundle and (
                bundle.state == "AVAILABLE" or pending.irreversible_started_at is not None
            ):
                pending_command = _flash_command(pending, bundle)
        db.commit()
    await browser_events.publish(
        "provisioning", {"companion_id": companion_id, "online": True}
    )
    if pending:
        if pending_command:
            await companion_hub.send(companion_id, pending_command)
        else:
            await companion_hub.send(
                companion_id,
                {
                    "type": "resume",
                    "session": pending_payload,
                },
            )
    try:
        while True:
            payload = await websocket.receive_json()
            kind = payload.get("type")
            if kind == "heartbeat":
                with SessionLocal() as db:
                    row = _companion_or_404(db, companion_id)
                    row.last_contact_at = utc_now()
                    row.application_version = str(payload.get("application_version", ""))[:40]
                    db.commit()
                await websocket.send_json({"type": "heartbeat_ack", "server_time": str(utc_now())})
            elif kind == "inspection":
                await _handle_inspection(companion, CompanionInspectionMessage.model_validate(payload))
            elif kind == "event":
                await _handle_progress(companion, CompanionEventMessage.model_validate(payload))
            else:
                await websocket.send_json({"type": "error", "code": "UNSUPPORTED_MESSAGE"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Provisioning companion stream failed")
        try:
            await websocket.close(code=1011, reason="Protocol error")
        except Exception:
            pass
    finally:
        await companion_hub.disconnect(companion_id, websocket)
        await browser_events.publish(
            "provisioning", {"companion_id": companion_id, "online": False}
        )


def correlate_onboarding(db: Session, connector: Connector) -> ProvisioningSession | None:
    row = db.scalar(
        select(ProvisioningSession)
        .where(
            ProvisioningSession.hardware_mac == connector.hardware_id.lower(),
            ProvisioningSession.state.in_(
                [
                    ProvisioningState.LOCAL_VERIFIED.value,
                    ProvisioningState.BOOT_VERIFYING.value,
                    ProvisioningState.WAITING_FOR_ONBOARDING.value,
                    ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION.value,
                    ProvisioningState.VERIFYING_SITE.value,
                    ProvisioningState.SITE_VALIDATION_PENDING.value,
                ]
            ),
        )
        .order_by(ProvisioningSession.created_at.desc())
    )
    if not row:
        return None
    if row.zone_id != connector.zone_id or row.device_id != connector.device_id:
        append_provisioning_event(
            db,
            row,
            state=ProvisioningState.RECOVERY_REQUIRED.value,
            progress=row.progress,
            source="SERVER",
            details={"error_code": "ONBOARDING_IDENTITY_MISMATCH"},
        )
        return row
    row.connector_id = connector.id
    zkt = connector.zkt_device
    session_result = dict(row.result or {})
    if zkt and not session_result.get("terminal_binding_initialized"):
        # Every package intentionally starts without an expected terminal
        # serial. Do not let a prior assignment's backend pin silently certify
        # the newly flashed configuration, including during a managed transfer.
        zkt.expected_serial = None
        zkt.confirmed_serial = None
        zkt.terminal_binding_state = "SERIAL_CONFIRMATION_REQUIRED"
        zkt.serial_confirmed_by = None
        zkt.serial_confirmed_at = None
        zkt.certification_state = "READ_ONLY"
        zkt.writes_disabled_reason = "TERMINAL_SERIAL_CONFIRMATION_REQUIRED"
        session_result["terminal_binding_initialized"] = True
        row.result = session_result
    if zkt and zkt.terminal_binding_state == "CONFIRMED" and zkt.certification_state == "CERTIFIED":
        target = ProvisioningState.VERIFIED_ONLINE
        progress = 100
    elif zkt and zkt.terminal_binding_state == "CONFIRMED":
        target = ProvisioningState.VERIFYING_SITE
        progress = 96
    else:
        target = ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION
        progress = 92
    if row.state != target.value:
        append_provisioning_event(
            db,
            row,
            state=target.value,
            progress=max(row.progress, progress),
            source="SERVER",
            details={"connector_id": connector.connector_id},
        )
    if target == ProvisioningState.VERIFIED_ONLINE:
        row.result = {
            "connector_id": connector.connector_id,
            "hardware_mac": row.hardware_mac,
            "site_validation": "VERIFIED",
        }
    return row
