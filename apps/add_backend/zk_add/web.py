from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add import APP_VERSION
from zk_add.audit import append_audit
from zk_add.attendance_batches import (
    attendance_quarantine_item,
    attendance_quarantine_summary,
    reveal_attendance_quarantine,
    review_attendance_quarantine,
    settle_attendance_batch,
)
from zk_add.crypto import cnic_lookup, decrypt_cnic, decrypt_text, mask_cnic, normalize_cnic
from zk_add.identity_conflicts import (
    build_identity_conflict_report,
    create_same_employee_resolution,
    revoke_identity_resolution,
    valid_identity_resolutions,
)
from zk_add.identity_catalog import identity_catalog_messages
from zk_add.db import SessionLocal, init_db, session_scope
from zk_add.models import (
    AdminSession,
    AttendanceEvent,
    Connector,
    CommKeyOperation,
    DeviceAlert,
    DeviceCommand,
    DeviceCommandEvent,
    DeviceConnectionEvent,
    DeviceLog,
    DeviceUser,
    IdentityConflictResolution,
    IdentityTombstone,
    OnboardingNonce,
    ReconciliationJob,
    ReconciliationDivergence,
    TemporaryAdminLease,
    UserDeletionJob,
    ZKTDevice,
)
from zk_add.realtime import browser_events, connector_hub, sse_encode
from zk_add.schemas import (
    AdminLeaseRequest,
    AlertAcknowledgeRequest,
    BulkUserDeleteCancelRequest,
    BulkUserDeleteRequest,
    CommandUpdate,
    CommKeyChangeRequest,
    CommKeyRevealRequest,
    Envelope,
    HeartbeatPayload,
    HistoricalCurrentIdentityRequest,
    HistoricalDirectoryIdentityRequest,
    HistoricalEventGroupIdentityRequest,
    HistoricalIdentityAliasRequest,
    IdentityConflictResolveRequest,
    IdentityConflictRevokeRequest,
    LoginRequest,
    OnboardRequest,
    DeviceLogIn,
    LogBatchRequest,
    OracleReceiptBatchRequest,
    RestartRequest,
    ReconciliationAssignmentReleaseRequest,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationControlRequest,
    ReconciliationCreateRequest,
    ReconciliationManifestRequest,
    SourceExceptionActionRequest,
    SourceProbeResultRequest,
    SourceTailChunkRequest,
    TerminalSerialConfirmRequest,
    TerminalSerialReplaceRequest,
    UserCreateRequest,
    UserDeleteRequest,
    UserSelectionValidationRequest,
    UserSnapshotRequest,
    UserUpdateRequest,
)
from zk_add.security import (
    ADMIN_COOKIE,
    AdminContext,
    LoginRateLimiter,
    admin_context,
    authenticate_connector_request,
    authenticate_websocket_token,
    create_admin_session,
    login_rate_limiter,
    require_csrf,
    require_step_up,
    verify_admin_password,
)
from zk_add.onboarding import normalize_mac, verify_onboarding_signature
from zk_add.service import (
    ACTIVE_COMMAND_STATES,
    apply_command_update,
    apply_user_command_terminal_state,
    cancel_user_deletion_job,
    build_historical_identity_report,
    create_admin_lease,
    create_command,
    create_device_user_command,
    create_historical_directory_identity,
    create_historical_event_group_identity,
    create_historical_identity_alias,
    create_user_deletion_job,
    delete_device_user_command,
    fleet_counts,
    ingest_logs,
    replace_user_snapshot,
    record_oracle_receipts,
    reconcile_device_user_identity_conflicts,
    reconcile_admin_lease_command,
    resolve_historical_event_group_to_current_identity,
    resolve_message_rejection,
    onboard_connector,
    serialize_command,
    serialize_connector,
    serialize_user_deletion_job,
    terminal_fingerprint_preconditions,
    update_heartbeat,
    update_device_user_command,
    upsert_alert,
)
from zk_add.settings import settings
from zk_add.comm_keys import (
    cancel_comm_key_operation,
    create_comm_key_change,
    reveal_comm_key,
    serialize_comm_key_state,
    serialize_operation as serialize_comm_key_operation,
)
from zk_add.worker import maintenance_loop, ords_delivery_metrics
from zk_add.attendance_repair import repair_worker_metrics
from zk_add.time_utils import parse_datetime, utc_now
from zk_add.reconciliation import (
    apply_reconciliation_assignment_release,
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    apply_reconciliation_manifest,
    apply_source_tail_chunk,
    apply_source_probe_result,
    active_coverage,
    control_reconciliation_job,
    create_reconciliation_job,
    preflight_reconciliation,
    reconciliation_scheduler_state,
    refresh_reconciliations_for_source_exception,
    serialize_job,
    serialize_coverage,
    source_exception_assurance,
)
from zk_add.source_exceptions import (
    exception_or_404_row,
    list_source_exceptions,
    reveal_source_exception,
    review_source_exception,
    source_exception_detail,
)


logger = logging.getLogger(__name__)
comm_key_reveal_rate_limiter = LoginRateLimiter(
    attempts=3, window_seconds=5 * 60, lock_seconds=15 * 60
)


def get_db():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def identity_catalog_payload(db: Session, connector: Connector) -> dict:
    rows = []
    if connector.zkt_device:
        for item in db.scalars(
            select(IdentityTombstone)
            .where(IdentityTombstone.zkt_device_id == connector.zkt_device.id)
            .order_by(IdentityTombstone.id.asc())
        ).all():
            rows.append(
                {
                    "uid": item.uid,
                    "user_id": item.user_id,
                    "display_name": decrypt_text(item.display_name_encrypted),
                    "cnic": decrypt_cnic(item.cnic_encrypted),
                    "shift_worker": item.shift_worker,
                }
            )
    return {"schema_version": "2", "type": "identity_catalog", "rows": rows}


async def send_identity_catalog(connector_id: str, catalog: dict) -> bool:
    return await connector_hub.send_many(
        connector_id,
        identity_catalog_messages(catalog),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.require_production_secrets()
    if settings.auto_create_schema:
        init_db()
    stop = asyncio.Event()
    task = asyncio.create_task(maintenance_loop(stop))
    yield
    stop.set()
    await task


app = FastAPI(
    title="Attendance Device Dashboard API",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-Id"],
)


@app.exception_handler(RequestValidationError)
async def sanitized_validation_error(_request: Request, error: RequestValidationError):
    # Pydantic's default response includes the rejected input value. That is
    # useful for ordinary APIs but would reflect Wi-Fi/terminal credentials on
    # a failed provisioning request. Preserve field/type/message without ever
    # echoing request values or validation context.
    detail = [
        {
            "type": item.get("type", "value_error"),
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg", "Invalid value"),
        }
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if settings.admin_cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_admin(request: Request, db: Session = Depends(get_db)) -> tuple[Session, AdminContext]:
    return db, admin_context(request, db)


def require_admin_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(get_db),
) -> tuple[Session, AdminContext]:
    context = admin_context(request, db)
    require_csrf(context, x_csrf_token)
    return db, context


async def require_connector(
    request: Request,
    authorization: str | None = Header(default=None),
    connector_id: str | None = Header(default=None, alias="X-ADD-Connector-Id"),
    timestamp: str | None = Header(default=None, alias="X-ADD-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-ADD-Nonce"),
    supplied_body_hash: str | None = Header(default=None, alias="X-ADD-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-ADD-Signature"),
    db: Session = Depends(get_db),
) -> tuple[Session, Connector]:
    connector = await authenticate_connector_request(
        request,
        db,
        authorization,
        connector_id,
        timestamp,
        nonce,
        supplied_body_hash,
        signature,
    )
    return db, connector


@app.get("/health/live")
def health_live():
    return {"ok": True, "app": "attendance-device-dashboard", "version": APP_VERSION}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.exception("ADD database readiness check failed")
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc
    provisioner_ready = not settings.provisioning_enabled
    if settings.provisioning_enabled:
        try:
            response = httpx.get(
                f"{settings.provisioning_worker_url.rstrip('/')}/health/ready",
                timeout=2,
            )
            response.raise_for_status()
            provisioner_ready = True
        except httpx.HTTPError as exc:
            logger.warning("Protected provisioner readiness check failed")
            raise HTTPException(
                status_code=503, detail="Protected provisioner unavailable."
            ) from exc
    return {
        "ok": True,
        "database": True,
        "provisioner": provisioner_ready,
        "server_utc": utc_now(),
    }


@app.post("/api/v1/auth/login")
def login(request: Request, body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    key = f"{body.username}:{client_ip(request)}"
    if not login_rate_limiter.allow(key):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    if not verify_admin_password(body.username, body.password):
        login_rate_limiter.fail(key)
        append_audit(
            db,
            actor=body.username,
            action="ADMIN_LOGIN",
            target_type="session",
            target_id=None,
            outcome="FAILED",
            ip_address=client_ip(request),
        )
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    login_rate_limiter.success(key)
    raw, row = create_admin_session(
        db,
        username=body.username,
        ip_address=client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    response.set_cookie(
        ADMIN_COOKIE,
        raw,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        max_age=settings.admin_session_absolute_seconds,
        path="/",
    )
    append_audit(
        db,
        actor=body.username,
        action="ADMIN_LOGIN",
        target_type="session",
        target_id=str(row.id),
        outcome="SUCCESS",
        ip_address=client_ip(request),
    )
    return {"ok": True, "username": row.username, "csrf_token": row.csrf_token}


@app.post("/api/v1/auth/logout")
def logout(
    request: Request,
    response: Response,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = db.get(AdminSession, context.row_id)
    if row:
        row.revoked_at = utc_now()
    response.delete_cookie(ADMIN_COOKIE, path="/")
    append_audit(
        db,
        actor=context.username,
        action="ADMIN_LOGOUT",
        target_type="session",
        target_id=str(context.row_id),
        outcome="SUCCESS",
        ip_address=client_ip(request),
    )
    return {"ok": True}


@app.get("/api/v1/auth/session")
def session_info(auth: tuple[Session, AdminContext] = Depends(require_admin)):
    _db, context = auth
    return {"authenticated": True, "username": context.username, "csrf_token": context.csrf_token}


@app.get("/api/v1/overview")
def overview(auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    result = fleet_counts(db)
    result["ords_delivery"] = ords_delivery_metrics(db)
    result["attendance_repair"] = repair_worker_metrics(db)
    return result


@app.post("/device/v2/onboard")
async def onboard(
    request: Request,
    body: OnboardRequest,
    x_zone_mac: str | None = Header(default=None, alias="X-Zone-MAC"),
    timestamp: str | None = Header(default=None, alias="X-ADD-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-ADD-Nonce"),
    supplied_body_hash: str | None = Header(default=None, alias="X-ADD-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-ADD-Signature"),
    db: Session = Depends(get_db),
):
    if not all([x_zone_mac, timestamp, nonce, supplied_body_hash, signature]):
        raise HTTPException(status_code=401, detail="Missing signed onboarding headers.")
    try:
        header_mac = normalize_mac(x_zone_mac or "")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if header_mac != body.hardware_id:
        raise HTTPException(status_code=401, detail="Onboarding MAC does not match the body.")
    if db.scalar(
        select(OnboardingNonce).where(
            OnboardingNonce.hardware_id == header_mac,
            OnboardingNonce.nonce == nonce,
        )
    ):
        raise HTTPException(status_code=409, detail="Onboarding nonce replay rejected.")
    raw_body = await request.body()
    try:
        verified = verify_onboarding_signature(
            mac=header_mac,
            method=request.method,
            path=request.url.path,
            timestamp=timestamp or "",
            nonce=nonce or "",
            supplied_body_hash=supplied_body_hash or "",
            signature=signature or "",
            body=raw_body,
        )
    except Exception:
        verified = False
    if not verified:
        raise HTTPException(status_code=401, detail="Invalid or expired onboarding signature.")
    # Serialize onboarding for one MAC. This closes both the nonce-check race
    # and the first-onboard unique-hardware race without locking the fleet.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:hardware_id, 0))"),
            {"hardware_id": header_mac},
        )
    if db.scalar(
        select(OnboardingNonce).where(
            OnboardingNonce.hardware_id == header_mac,
            OnboardingNonce.nonce == nonce,
        )
    ):
        raise HTTPException(status_code=409, detail="Onboarding nonce replay rejected.")

    db.add(
        OnboardingNonce(
            hardware_id=header_mac,
            nonce=nonce or "",
            request_timestamp=parse_datetime(timestamp or ""),
        )
    )
    connector, token, created = onboard_connector(
        db,
        hardware_id=header_mac,
        zone_id=body.zone_id,
        zone_name=body.zone_name,
        device_id=body.device_id,
        firmware_version=body.firmware_version,
        expected_serial=body.expected_serial,
        actor=f"esp:{header_mac}",
        ip_address=client_ip(request),
    )
    from zk_add.provisioning_api import correlate_onboarding

    provisioning_session = correlate_onboarding(db, connector)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Onboarding replay or conflict rejected.") from exc
    ws_url = f"{settings.public_device_ws_url}?connector_id={connector.connector_id}"
    if provisioning_session:
        await browser_events.publish(
            "provisioning",
            {"session_id": provisioning_session.session_id, "state": provisioning_session.state},
        )
    return {
        "ok": True,
        "created": created,
        "connector_id": connector.connector_id,
        "device_token": token,
        "ws_url": ws_url,
        "schema_version": "2",
        "token_overlap_seconds": settings.onboarding_token_overlap_seconds,
    }


@app.get("/api/v1/devices")
def list_devices(
    state: str | None = None,
    q: str | None = None,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    statement = select(Connector).order_by(Connector.display_name.asc())
    if state:
        statement = statement.where(Connector.lifecycle_state == state.upper())
    if q:
        like = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                Connector.display_name.ilike(like),
                Connector.zone_name.ilike(like),
                Connector.connector_id.ilike(like),
                Connector.hardware_id.ilike(like),
            )
        )
    return {"rows": [serialize_connector(row) for row in db.scalars(statement).all()]}


def connector_or_404(db: Session, connector_id: str) -> Connector:
    row = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    return row


@app.get("/api/v1/devices/{connector_id}")
def get_device(connector_id: str, auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    active_command = db.scalar(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
        ).order_by(DeviceCommand.created_at.asc()).limit(1)
    )
    active_lease = None
    if connector.zkt_device:
        active_lease = db.scalar(
            select(TemporaryAdminLease).where(
                TemporaryAdminLease.zkt_device_id == connector.zkt_device.id,
                TemporaryAdminLease.state.in_(["GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]),
            ).order_by(TemporaryAdminLease.requested_at.desc()).limit(1)
        )
    return {
        **serialize_connector(connector),
        "active_command": command_response(active_command) if active_command else None,
        "active_lease": serialize_lease(active_lease) if active_lease else None,
    }


@app.get("/api/v1/devices/{connector_id}/comm-key")
def get_device_comm_key_state(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    return serialize_comm_key_state(db, connector)


@app.post("/api/v1/devices/{connector_id}/comm-key/changes", status_code=202)
async def change_device_comm_key(
    request: Request,
    connector_id: str,
    body: CommKeyChangeRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password.get_secret_value(), db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt:
        active_lease = db.scalar(
            select(TemporaryAdminLease).where(
                TemporaryAdminLease.zkt_device_id == zkt.id,
                TemporaryAdminLease.state.in_(["GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]),
            )
        )
        if active_lease:
            raise HTTPException(
                status_code=409,
                detail="COMM Key changes are blocked by an active enrollment lease.",
            )
    try:
        operation, command = create_comm_key_change(
            db,
            connector=connector,
            new_key=body.new_key.get_secret_value(),
            mode=body.mode,
            expected_revision=body.expected_revision,
            expected_terminal_serial=body.expected_terminal_serial,
            reason=body.reason,
            typed_confirmation=body.typed_confirmation,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="COMM Key protection is unavailable.") from exc
    append_audit(
        db,
        actor=context.username,
        action="COMM_KEY_CHANGE_STEP_UP_CONFIRMED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=operation.status,
        ip_address=client_ip(request),
        after={
            "operation_id": operation.operation_id,
            "mode": operation.mode,
            "expected_terminal_serial": operation.expected_terminal_serial,
            "reason": operation.reason,
            "provisional_serial_attestation": bool(
                zkt and zkt.terminal_binding_state == "RECOVERY_EXPECTED_SERIAL"
            ),
        },
    )
    db.commit()
    if command is not None:
        await dispatch_command(connector, command)
    response = {
        "state": serialize_comm_key_state(db, connector),
        "operation": serialize_comm_key_operation(operation),
        "command": command_response(command) if command else None,
    }
    await browser_events.publish(
        "device",
        {
            "connector_id": connector.connector_id,
            "comm_key_state": response["state"]["management_state"],
        },
    )
    return response


@app.post("/api/v1/devices/{connector_id}/comm-key/reveal")
def reveal_device_comm_key(
    request: Request,
    connector_id: str,
    body: CommKeyRevealRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    rate_limit_key = f"{context.username}:{client_ip(request)}:{connector_id}"
    if not comm_key_reveal_rate_limiter.allow(rate_limit_key):
        raise HTTPException(
            status_code=429,
            detail="Too many COMM Key reveal attempts. Try again later.",
            headers={"Retry-After": "900"},
        )
    # Count every admitted attempt, including an incorrect password. The API
    # runs one production worker, matching the existing login limiter model.
    comm_key_reveal_rate_limiter.fail(rate_limit_key)
    require_step_up(body.password.get_secret_value(), db, context)
    connector = connector_or_404(db, connector_id)
    expected_confirmation = f"REVEAL {connector.connector_id}"
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Type '{expected_confirmation}' to reveal the managed key.",
        )
    try:
        comm_key, state = reveal_comm_key(db, connector=connector)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="COMM Key protection is unavailable.") from exc
    append_audit(
        db,
        actor=context.username,
        action="COMM_KEY_REVEALED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome="SUCCEEDED",
        ip_address=client_ip(request),
        after={
            "revision": state.applied_revision,
            "verified_terminal_serial": state.expected_terminal_serial,
            "reason": body.reason,
        },
    )
    db.commit()
    return JSONResponse(
        content={
            "comm_key": comm_key,
            "applied_revision": state.applied_revision,
            "verified_terminal_serial": state.expected_terminal_serial,
            "last_verified_at": (
                state.last_verified_at.isoformat() if state.last_verified_at else None
            ),
        },
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/v1/comm-key-operations/{operation_id}/cancel")
async def cancel_device_comm_key_operation(
    operation_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    operation = db.scalar(
        select(CommKeyOperation).where(CommKeyOperation.operation_id == operation_id)
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="COMM Key operation not found.")
    try:
        connector, command, remote_cancel = cancel_comm_key_operation(
            db, operation=operation, actor=context.username
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    if remote_cancel and command is not None:
        await connector_hub.send(
            connector.connector_id,
            {
                "schema_version": "2",
                "type": "command_cancel",
                "command_id": command.command_id,
            },
        )
    return {
        "state": serialize_comm_key_state(db, connector),
        "operation": serialize_comm_key_operation(operation),
    }


@app.post(
    "/api/v1/devices/{connector_id}/terminal-binding/confirm",
    status_code=202,
)
async def confirm_device_terminal_binding(
    request: Request,
    connector_id: str,
    body: TerminalSerialConfirmRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None or not zkt.serial:
        raise HTTPException(
            status_code=409,
            detail="The authenticated connector has not reported a terminal serial.",
        )
    if zkt.terminal_binding_state != "SERIAL_CONFIRMATION_REQUIRED":
        raise HTTPException(
            status_code=409,
            detail="This terminal is not awaiting initial serial confirmation.",
        )
    if body.observed_serial != zkt.serial:
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
    try:
        command = create_command(
            db,
            connector=connector,
            command_type="PIN_TERMINAL_SERIAL",
            payload={"serial": body.observed_serial},
            expected_state={"serial": body.observed_serial},
            desired_state={"expected_serial": body.observed_serial},
            idempotency_key=body.idempotency_key,
            actor=context.username,
            expires_in_seconds=10 * 60,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    append_audit(
        db,
        actor=context.username,
        action="TERMINAL_SERIAL_CONFIRMATION_REQUESTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=command.status,
        ip_address=client_ip(request),
        after={
            "hardware_mac": connector.hardware_id,
            "terminal_serial": body.observed_serial,
            "command_id": command.command_id,
        },
    )
    db.commit()
    await dispatch_command(connector, command)
    await browser_events.publish(
        "device",
        {
            "connector_id": connector.connector_id,
            "terminal_binding_state": zkt.terminal_binding_state,
        },
    )
    return {
        "device": serialize_connector(connector),
        "command": command_response(command),
    }


@app.post(
    "/api/v1/devices/{connector_id}/terminal-binding/replace",
    status_code=202,
)
async def replace_device_terminal_binding(
    request: Request,
    connector_id: str,
    body: TerminalSerialReplaceRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None or not zkt.serial:
        raise HTTPException(
            status_code=409,
            detail="The authenticated connector has not reported a replacement terminal serial.",
        )
    current_serial = zkt.confirmed_serial or zkt.expected_serial
    if not current_serial or body.current_serial != current_serial:
        raise HTTPException(
            status_code=409,
            detail="The current terminal binding changed; refresh before replacing it.",
        )
    if body.observed_serial != zkt.serial:
        raise HTTPException(
            status_code=409,
            detail="Observed replacement serial changed; refresh the authenticated terminal evidence.",
        )
    if body.observed_serial == current_serial:
        raise HTTPException(status_code=409, detail="The replacement serial matches the current binding.")
    expected_confirmation = (
        f"REPLACE {connector.connector_id} {current_serial} {body.observed_serial}"
    )
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Type '{expected_confirmation}' to confirm this terminal replacement.",
        )
    if not connector.connected or zkt.connection_state not in {"RECOVERING", "ONLINE"}:
        raise HTTPException(
            status_code=409,
            detail="A current authenticated replacement-terminal observation is required.",
        )
    collision = db.scalar(
        select(ZKTDevice).where(
            ZKTDevice.id != zkt.id,
            or_(
                ZKTDevice.serial == body.observed_serial,
                ZKTDevice.expected_serial == body.observed_serial,
                ZKTDevice.confirmed_serial == body.observed_serial,
            ),
        )
    )
    if collision:
        raise HTTPException(
            status_code=409,
            detail="That replacement terminal serial is already associated with another connector.",
        )

    zkt.expected_serial = body.observed_serial
    zkt.confirmed_serial = None
    zkt.terminal_binding_state = "PENDING_DEVICE_ACK"
    zkt.serial_confirmed_by = context.username
    zkt.serial_confirmed_at = utc_now()
    zkt.certification_state = "READ_ONLY"
    zkt.certification_fingerprint = None
    zkt.certification_observations = 0
    zkt.writes_disabled_reason = "TERMINAL_SERIAL_PENDING_DEVICE_ACK"
    try:
        command = create_command(
            db,
            connector=connector,
            command_type="PIN_TERMINAL_SERIAL",
            payload={"serial": body.observed_serial},
            expected_state={"serial": body.observed_serial},
            desired_state={"expected_serial": body.observed_serial},
            idempotency_key=body.idempotency_key,
            actor=context.username,
            expires_in_seconds=10 * 60,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    append_audit(
        db,
        actor=context.username,
        action="TERMINAL_BINDING_REPLACEMENT_REQUESTED",
        target_type="connector",
        target_id=connector.connector_id,
        outcome=command.status,
        ip_address=client_ip(request),
        before={"terminal_serial": current_serial},
        after={
            "hardware_mac": connector.hardware_id,
            "terminal_serial": body.observed_serial,
            "command_id": command.command_id,
            "reason": body.reason,
        },
    )
    db.commit()
    await dispatch_command(connector, command)
    await browser_events.publish(
        "device",
        {
            "connector_id": connector.connector_id,
            "terminal_binding_state": zkt.terminal_binding_state,
        },
    )
    return {
        "device": serialize_connector(connector),
        "command": command_response(command),
    }


def reconciliation_or_404(db: Session, job_id: str) -> ReconciliationJob:
    row = db.scalar(
        select(ReconciliationJob).where(ReconciliationJob.job_id == job_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reconciliation job not found.")
    return row


@app.get("/api/v1/devices/{connector_id}/reconciliations/preflight")
def reconciliation_preflight(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    return preflight_reconciliation(db, connector_or_404(db, connector_id))


@app.post(
    "/api/v1/devices/{connector_id}/reconciliations/full-history",
    status_code=201,
)
async def start_full_history_reconciliation(
    connector_id: str,
    body: ReconciliationCreateRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    try:
        job = create_reconciliation_job(
            db,
            connector=connector,
            actor=context.username,
            reason=body.reason,
            confirmation=body.confirmation,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "reconciliation", {"job_id": job.job_id, "status": job.status}
    )
    return serialize_job(db, job, include_events=True)


@app.get("/api/v1/reconciliations")
def list_reconciliations(
    connector_id: str | None = None,
    status: str | None = None,
    status_group: str | None = None,
    zone_id: str | None = None,
    q: str | None = Query(default=None, max_length=120),
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    clauses = []
    if connector_id:
        connector = connector_or_404(db, connector_id)
        clauses.append(ReconciliationJob.connector_id == connector.id)
    if status:
        clauses.append(ReconciliationJob.status == status.upper())
    if zone_id:
        clauses.append(Connector.zone_id == zone_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        clauses.append(
            or_(
                ReconciliationJob.job_id.ilike(term),
                Connector.connector_id.ilike(term),
                Connector.device_id.ilike(term),
                Connector.display_name.ilike(term),
                Connector.zone_id.ilike(term),
                Connector.hardware_id.ilike(term),
            )
        )
    status_groups = {
        "ACTIVE": ReconciliationJob.status == "RUNNING",
        "QUEUED_WAITING": ReconciliationJob.status == "QUEUED",
        "PAUSED": ReconciliationJob.status.in_(
            ["PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"]
        ),
        "ATTENTION": ReconciliationJob.status.in_(
            ["NEEDS_ATTENTION", "FAILED", "INVALIDATED"]
        ),
        "COMPLETED": ReconciliationJob.status == "COMPLETED",
        "CANCELLED": ReconciliationJob.status == "CANCELLED",
    }
    if status_group:
        normalized_group = status_group.upper()
        if normalized_group not in status_groups:
            raise HTTPException(status_code=422, detail="Unknown reconciliation status group.")
        clauses.append(status_groups[normalized_group])

    filtered_total = db.scalar(
        select(func.count(ReconciliationJob.id))
        .join(Connector, ReconciliationJob.connector_id == Connector.id)
        .where(*clauses)
    ) or 0
    statement = (
        select(ReconciliationJob)
        .join(Connector, ReconciliationJob.connector_id == Connector.id)
        .where(*clauses)
    )
    if cursor is not None:
        statement = statement.where(ReconciliationJob.id < cursor)
    fetched = list(
        db.scalars(
            statement.order_by(ReconciliationJob.id.desc()).limit(limit + 1)
        ).all()
    )
    rows = fetched[:limit]
    next_cursor = rows[-1].id if len(fetched) > limit and rows else None
    totals = {
        "all": int(db.scalar(select(func.count(ReconciliationJob.id))) or 0),
        "active": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(status_groups["ACTIVE"])
            )
            or 0
        ),
        "queued_waiting": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(
                    status_groups["QUEUED_WAITING"]
                )
            )
            or 0
        ),
        "paused": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(status_groups["PAUSED"])
            )
            or 0
        ),
        "attention": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(
                    status_groups["ATTENTION"]
                )
            )
            or 0
        ),
        "completed": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(
                    status_groups["COMPLETED"]
                )
            )
            or 0
        ),
        "cancelled": int(
            db.scalar(
                select(func.count(ReconciliationJob.id)).where(
                    status_groups["CANCELLED"]
                )
            )
            or 0
        ),
    }
    return {
        "enabled": settings.reconciliation_enabled,
        "scheduler": reconciliation_scheduler_state(db),
        "rows": [serialize_job(db, row) for row in rows],
        "next_cursor": next_cursor,
        "filtered_total": int(filtered_total),
        "totals": totals,
    }


@app.get("/api/v1/reconciliations/{job_id}")
def get_reconciliation(
    job_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    return serialize_job(db, reconciliation_or_404(db, job_id), include_events=True)


@app.get("/api/v1/reconciliations/{job_id}/evidence")
def get_reconciliation_evidence(
    job_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    job = reconciliation_or_404(db, job_id)
    return {
        "schema_version": "1",
        "policy": "APPEND_ONLY_MEMBERSHIP",
        "job": serialize_job(db, job, include_events=True),
    }


@app.get("/api/v1/source-exceptions")
def source_exceptions(
    job_id: str | None = None,
    device_id: str | None = None,
    disposition: str | None = None,
    error_code: str | None = None,
    review_state: str | None = None,
    ordinal: int | None = Query(default=None, ge=0),
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    job = reconciliation_or_404(db, job_id) if job_id else None
    connector_pk = None
    if device_id:
        connector_pk = connector_or_404(db, device_id).id
    if job is not None and connector_pk is not None and job.connector_id != connector_pk:
        raise HTTPException(
            status_code=422,
            detail="The selected device does not belong to the reconciliation job.",
        )
    if disposition and disposition not in {"INVALID_TIME", "MALFORMED"}:
        raise HTTPException(status_code=422, detail="Unknown source exception disposition.")
    if review_state and review_state not in {"OPEN", "REVIEWED"}:
        raise HTTPException(status_code=422, detail="Unknown source exception review state.")
    result = list_source_exceptions(
        db,
        job=job,
        connector_id=connector_pk,
        disposition=disposition,
        error_code=error_code,
        review_state=review_state,
        ordinal=ordinal,
        cursor=cursor,
        limit=limit,
    )
    if job is not None:
        assurance = source_exception_assurance(db, job)
        job_connector = db.get(Connector, job.connector_id)
        result["scope"] = {
            "job_id": job.job_id,
            "device_id": job_connector.connector_id if job_connector else None,
            "terminal_serial": job.terminal_serial,
            "cutoff_count": job.cutoff_count,
            "source_exception_assurance": {
                key: assurance[key]
                for key in (
                    "total",
                    "reviewed",
                    "open",
                    "invalid_time",
                    "malformed",
                    "state",
                    "cohort_digest",
                )
            },
        }
    return result


@app.get("/api/v1/attendance-quarantine")
def attendance_quarantine(
    device_id: str | None = None,
    review_state: str | None = None,
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector_pk = connector_or_404(db, device_id).id if device_id else None
    if review_state and review_state not in {"OPEN", "REVIEWED"}:
        raise HTTPException(
            status_code=422, detail="Unknown attendance quarantine review state."
        )
    return attendance_quarantine_summary(
        db,
        connector_id=connector_pk,
        review_state=review_state,
        cursor=cursor,
        limit=limit,
    )


@app.post("/api/v1/attendance-quarantine/{item_id}/review")
def review_attendance_quarantine_endpoint(
    item_id: int,
    body: SourceExceptionActionRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    item = attendance_quarantine_item(db, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Attendance quarantine item not found.")
    try:
        review_attendance_quarantine(
            db,
            item=item,
            actor=context.username,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {
        "id": item.id,
        "review_state": item.review_state,
        "reviewed_by": item.reviewed_by,
        "review_reason": item.review_reason,
        "reviewed_at": item.reviewed_at,
    }


@app.post("/api/v1/attendance-quarantine/{item_id}/reveal")
def reveal_attendance_quarantine_endpoint(
    item_id: int,
    body: SourceExceptionActionRequest,
    response: Response,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    item = attendance_quarantine_item(db, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Attendance quarantine item not found.")
    result = reveal_attendance_quarantine(
        db,
        item=item,
        actor=context.username,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
    )
    db.commit()
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return result


@app.get("/api/v1/source-exceptions/{exception_id}")
def source_exception(
    exception_id: int,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    row = exception_or_404_row(db, exception_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source exception not found.")
    return source_exception_detail(db, row)


@app.post("/api/v1/source-exceptions/{exception_id}/review")
async def review_source_exception_endpoint(
    exception_id: int,
    body: SourceExceptionActionRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    row = exception_or_404_row(db, exception_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source exception not found.")
    review_source_exception(
        db,
        row=row,
        actor=context.username,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
    )
    db.flush()
    affected_jobs = refresh_reconciliations_for_source_exception(db, row)
    result = source_exception_detail(db, row)
    db.commit()
    for job in affected_jobs:
        await browser_events.publish(
            "reconciliation",
            {
                "job_id": job.job_id,
                "status": job.status,
                "source_exception_state": source_exception_assurance(db, job)[
                    "state"
                ],
            },
        )
    return result


@app.post("/api/v1/source-exceptions/{exception_id}/reveal")
def reveal_source_exception_endpoint(
    exception_id: int,
    body: SourceExceptionActionRequest,
    response: Response,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    row = exception_or_404_row(db, exception_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source exception not found.")
    try:
        result = reveal_source_exception(
            db,
            row=row,
            actor=context.username,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return result


@app.get("/api/v1/reconciliation-divergences/{divergence_id}")
def reconciliation_divergence_detail(
    divergence_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    row = db.scalar(
        select(ReconciliationDivergence).where(
            ReconciliationDivergence.divergence_id == divergence_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reconciliation divergence not found.")
    job = db.get(ReconciliationJob, row.job_id)
    return {
        "divergence_id": row.divergence_id,
        "job_id": job.job_id if job else None,
        "ordinal": row.ordinal,
        "state": row.state,
        "old_raw_digest": row.old_raw_digest,
        "new_raw_digest": row.new_raw_digest,
        "old_disposition": row.old_disposition,
        "new_disposition": row.new_disposition,
        "observations": row.observations or [],
        "evidence_available": bool(row.protected_new_raw_record),
        "created_at": row.created_at,
        "resolved_at": row.resolved_at,
    }


@app.post("/api/v1/reconciliation-divergences/{divergence_id}/reveal")
def reveal_reconciliation_divergence(
    divergence_id: str,
    body: SourceExceptionActionRequest,
    response: Response,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    row = db.scalar(
        select(ReconciliationDivergence).where(
            ReconciliationDivergence.divergence_id == divergence_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reconciliation divergence not found.")
    if not row.protected_new_raw_record:
        raise HTTPException(status_code=409, detail="Protected divergence evidence is unavailable.")
    raw_b64 = decrypt_text(row.protected_new_raw_record)
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=409, detail="Protected divergence evidence is invalid.") from exc
    append_audit(
        db,
        actor=context.username,
        action="RECONCILIATION_DIVERGENCE_EVIDENCE_REVEALED",
        target_type="reconciliation_divergence",
        target_id=row.divergence_id,
        outcome="SUCCESS",
        after={"reason": body.reason.strip(), "ordinal": row.ordinal},
        request_id=body.idempotency_key,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return {
        "divergence_id": row.divergence_id,
        "raw_record_b64": raw_b64,
        "raw_record_hex": raw.hex(),
        "new_raw_digest": row.new_raw_digest,
    }


@app.post("/api/v1/reconciliations/{job_id}/{action}")
async def control_reconciliation(
    job_id: str,
    action: str,
    body: ReconciliationControlRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise HTTPException(status_code=404, detail="Unknown reconciliation action.")
    db, context = auth
    require_step_up(body.password, db, context)
    job = reconciliation_or_404(db, job_id)
    try:
        job = control_reconciliation_job(
            db,
            job=job,
            action=action,
            actor=context.username,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "reconciliation", {"job_id": job.job_id, "status": job.status}
    )
    return serialize_job(db, job, include_events=True)


@app.get("/api/v1/users")
def list_users(
    device_id: str,
    q: str | None = None,
    cnic: str | None = None,
    privilege: int | None = None,
    present: bool = True,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, device_id)
    if not connector.zkt_device:
        return {"rows": [], "next_cursor": None}
    statement = select(DeviceUser).where(
        DeviceUser.zkt_device_id == connector.zkt_device.id,
        DeviceUser.present == present,
    )
    if cursor:
        statement = statement.where(DeviceUser.id > cursor)
    if q:
        like = f"%{q.strip()}%"
        statement = statement.where(
            or_(DeviceUser.display_name.ilike(like), DeviceUser.user_id.ilike(like), DeviceUser.uid.ilike(like))
        )
    if cnic:
        normalized = normalize_cnic(cnic)
        if normalized is None:
            raise HTTPException(status_code=422, detail="CNIC must contain exactly 13 digits.")
        try:
            lookup = cnic_lookup(normalized)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        statement = statement.where(DeviceUser.cnic_lookup_hash == lookup)
    if privilege is not None:
        statement = statement.where(DeviceUser.privilege == privilege)
    rows = db.scalars(statement.order_by(DeviceUser.id.asc()).limit(limit + 1)).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    resolutions = valid_identity_resolutions(db, zkt=connector.zkt_device)
    return {
        "rows": [
            serialize_user(
                row,
                identity_resolution=resolutions.get(row.cnic_lookup_hash or ""),
            )
            for row in rows[:limit]
        ],
        "next_cursor": next_cursor,
    }


@app.get("/api/v2/devices/{connector_id}/users")
def list_device_users_v2(
    connector_id: str,
    q: str | None = None,
    cnic: str | None = None,
    privilege: int | None = None,
    identity: str | None = None,
    present: bool = True,
    limit: int = Query(default=200, ge=1, le=500),
    cursor: int | None = None,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        return {"rows": [], "next_cursor": None, "device": serialize_connector(connector)}
    statement = select(DeviceUser).where(
        DeviceUser.zkt_device_id == zkt.id,
        DeviceUser.present == present,
        DeviceUser.lifecycle_state == ("ACTIVE" if present else "DELETED"),
    )
    if cursor:
        statement = statement.where(DeviceUser.id > cursor)
    if q:
        like = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                DeviceUser.display_name.ilike(like),
                DeviceUser.user_id.ilike(like),
                DeviceUser.uid.ilike(like),
            )
        )
    if cnic:
        normalized = normalize_cnic(cnic)
        if normalized is None:
            raise HTTPException(status_code=422, detail="CNIC must contain exactly 13 digits.")
        statement = statement.where(DeviceUser.cnic_lookup_hash == cnic_lookup(normalized))
    if privilege is not None:
        statement = statement.where(DeviceUser.privilege == privilege)
    rows = list(db.scalars(statement.order_by(DeviceUser.id.asc())).all())
    integrity, conflict_members, resolutions_by_user = device_user_identity_integrity(
        db, zkt=zkt
    )
    if identity == "COMPLETE":
        rows = [
            row
            for row in rows
            if row.cnic_lookup_hash
            and (row.identity_conflict_code is None or row.id in resolutions_by_user)
        ]
    elif identity == "MISSING":
        rows = [
            row
            for row in rows
            if not row.cnic_lookup_hash
            or (row.identity_conflict_code is not None and row.id not in resolutions_by_user)
        ]
    elif identity == "CONFLICT":
        rows = [
            row
            for row in rows
            if row.identity_conflict_code is not None and row.id not in resolutions_by_user
        ]
    elif identity == "RESOLVED_ALIAS":
        rows = [row for row in rows if row.id in resolutions_by_user]
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return {
        "rows": [
            serialize_user(
                row,
                zkt=zkt,
                identity_conflict_members=conflict_members.get(row.id, []),
                identity_resolution=resolutions_by_user.get(row.id),
            )
            for row in rows[:limit]
        ],
        "next_cursor": next_cursor,
        "device": serialize_connector(connector),
        "identity_integrity": integrity,
    }


@app.post("/api/v2/devices/{connector_id}/users/validate-selection")
def validate_device_user_selection_v2(
    connector_id: str,
    body: UserSelectionValidationRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    """Return current authoritative rows for an exact, bounded selection."""
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        return {"rows": [], "missing_user_keys": body.user_keys}
    rows = list(
        db.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.user_key.in_(body.user_keys),
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
            )
        ).all()
    )
    rows_by_key = {row.user_key: row for row in rows}
    return {
        "rows": [
            serialize_user(rows_by_key[user_key], zkt=zkt)
            for user_key in body.user_keys
            if user_key in rows_by_key
        ],
        "missing_user_keys": [
            user_key for user_key in body.user_keys if user_key not in rows_by_key
        ],
    }


@app.get("/api/v2/devices/{connector_id}/identity-conflicts")
def identity_conflict_report(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    if connector.zkt_device is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    return build_identity_conflict_report(db, zkt=connector.zkt_device)


@app.post("/api/v2/devices/{connector_id}/identity-conflicts/resolve", status_code=201)
def resolve_identity_conflict(
    connector_id: str,
    body: IdentityConflictResolveRequest,
    request: Request,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    try:
        resolution = create_same_employee_resolution(
            db,
            zkt=zkt,
            group_token=body.group_token,
            members=[(member.user_key, member.expected_version) for member in body.members],
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            actor=context.username,
            ip_address=client_ip(request),
        )
        reconcile_device_user_identity_conflicts(db, connector=connector, zkt=zkt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {
        "resolution": serialize_identity_resolution(resolution),
        "report": build_identity_conflict_report(db, zkt=zkt),
    }


@app.post(
    "/api/v2/devices/{connector_id}/identity-conflicts/{resolution_id}/revoke"
)
def revoke_resolved_identity_conflict(
    connector_id: str,
    resolution_id: str,
    body: IdentityConflictRevokeRequest,
    request: Request,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    try:
        resolution = revoke_identity_resolution(
            db,
            zkt=zkt,
            resolution_id=resolution_id,
            reason=body.reason,
            actor=context.username,
            ip_address=client_ip(request),
        )
        reconcile_device_user_identity_conflicts(db, connector=connector, zkt=zkt)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {
        "resolution": serialize_identity_resolution(resolution),
        "report": build_identity_conflict_report(db, zkt=zkt),
    }


@app.post("/api/v2/devices/{connector_id}/identity-aliases", status_code=201)
async def create_identity_alias(
    connector_id: str,
    body: HistoricalIdentityAliasRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    target_user = db.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == body.target_user_key,
        )
    )
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target terminal user not found.")
    if target_user.row_version != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail="Target user changed since it was selected. Refresh and retry.",
        )
    expected_confirmation = f"{body.source_user_id} -> {target_user.user_id}"
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Typed confirmation must exactly match {expected_confirmation}.",
        )
    try:
        tombstone, repaired = create_historical_identity_alias(
            db,
            connector=connector,
            source_user_id=body.source_user_id,
            source_cnic=body.source_cnic,
            target_user=target_user,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(connector)
    payload = identity_catalog_payload(db, connector)
    delivered = await send_identity_catalog(connector.connector_id, payload)
    await browser_events.publish(
        "identity_alias",
        {
            "connector_id": connector.connector_id,
            "source_user_id": body.source_user_id,
            "repaired_events": repaired,
        },
    )
    return {
        "ok": True,
        "alias_id": tombstone.id,
        "source_user_id": tombstone.user_id,
        "target_user_key": target_user.user_key,
        "target_user_id": target_user.user_id,
        "repaired_events": repaired,
        "catalog_delivered": delivered,
    }


@app.get("/api/v2/devices/{connector_id}/historical-identities")
def historical_identity_report(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    return build_historical_identity_report(db, zkt=zkt)


@app.post(
    "/api/v2/devices/{connector_id}/historical-identities/resolve",
    status_code=201,
)
async def resolve_historical_directory_identity(
    connector_id: str,
    body: HistoricalDirectoryIdentityRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    source_user = db.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == body.source_user_key,
        )
    )
    if source_user is None:
        raise HTTPException(status_code=404, detail="Historical terminal user not found.")
    expected_confirmation = (
        f"{source_user.user_id} -> HR {body.directory_employee_id}"
    )
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Typed confirmation must exactly match {expected_confirmation}.",
        )
    try:
        tombstone, repaired = create_historical_directory_identity(
            db,
            connector=connector,
            source_user=source_user,
            source_cnic=body.source_cnic,
            directory_employee_id=body.directory_employee_id,
            directory_service_number=body.directory_service_number,
            directory_employee_name=body.directory_employee_name,
            directory_zone_code=body.directory_zone_code,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(connector)
    payload = identity_catalog_payload(db, connector)
    delivered = await send_identity_catalog(connector.connector_id, payload)
    await browser_events.publish(
        "historical_directory_identity",
        {
            "connector_id": connector.connector_id,
            "source_user_id": source_user.user_id,
            "repaired_events": repaired,
        },
    )
    return {
        "ok": True,
        "tombstone_id": tombstone.id,
        "source_user_key": source_user.user_key,
        "source_user_id": source_user.user_id,
        "repaired_events": repaired,
        "catalog_delivered": delivered,
    }


@app.post(
    "/api/v2/devices/{connector_id}/historical-identities/resolve-event-group",
    status_code=201,
)
async def resolve_historical_event_group_identity(
    connector_id: str,
    body: HistoricalEventGroupIdentityRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    expected_confirmation = (
        f"{body.source_user_id} -> HR {body.directory_employee_id}"
    )
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Typed confirmation must exactly match {expected_confirmation}.",
        )
    try:
        tombstone, repaired = create_historical_event_group_identity(
            db,
            connector=connector,
            group_token=body.group_token,
            source_user_id=body.source_user_id,
            source_uid=body.source_uid,
            source_cnic=body.source_cnic,
            directory_employee_id=body.directory_employee_id,
            directory_service_number=body.directory_service_number,
            directory_employee_name=body.directory_employee_name,
            directory_zone_code=body.directory_zone_code,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(connector)
    payload = identity_catalog_payload(db, connector)
    delivered = await send_identity_catalog(connector.connector_id, payload)
    await browser_events.publish(
        "historical_event_group_identity",
        {
            "connector_id": connector.connector_id,
            "source_user_id": body.source_user_id,
            "source_uid": body.source_uid,
            "repaired_events": repaired,
        },
    )
    return {
        "ok": True,
        "tombstone_id": tombstone.id,
        "source_user_id": tombstone.user_id,
        "source_uid": tombstone.uid,
        "repaired_events": repaired,
        "catalog_delivered": delivered,
    }


@app.post(
    "/api/v2/devices/{connector_id}/historical-identities/resolve-current-identity",
    status_code=201,
)
async def resolve_historical_current_identity(
    connector_id: str,
    body: HistoricalCurrentIdentityRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    expected_confirmation = (
        f"{body.source_user_id} -> CURRENT {body.source_user_id}"
    )
    if body.typed_confirmation.strip() != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Typed confirmation must exactly match {expected_confirmation}.",
        )
    try:
        resolution, target_user, repaired = (
            resolve_historical_event_group_to_current_identity(
                db,
                connector=connector,
                group_token=body.group_token,
                source_user_id=body.source_user_id,
                source_uid=body.source_uid,
                target_user_key=body.target_user_key,
                expected_version=body.expected_version,
                source_cnic=body.source_cnic,
                verified_employee_name=body.verified_employee_name,
                reason=body.reason,
                idempotency_key=body.idempotency_key,
                actor=context.username,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "historical_current_identity",
        {
            "connector_id": connector.connector_id,
            "source_user_id": body.source_user_id,
            "source_uid": body.source_uid,
            "target_user_key": target_user.user_key,
            "repaired_events": repaired,
        },
    )
    return {
        "ok": True,
        "resolution_id": resolution.resolution_id,
        "source_user_id": body.source_user_id,
        "source_uid": body.source_uid,
        "target_user_key": target_user.user_key,
        "target_user_id": target_user.user_id,
        "repaired_events": repaired,
    }


@app.post("/api/v1/devices/{connector_id}/users/refresh", status_code=202)
async def refresh_users(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    connector = connector_or_404(db, connector_id)
    try:
        command = create_command(
            db,
            connector=connector,
            command_type="REFRESH_USERS",
            payload={},
            expected_state={},
            desired_state={},
            idempotency_key=f"refresh:{connector_id}:{int(utc_now().timestamp() // 300)}",
            actor=context.username,
            expires_in_seconds=120,
        )
        db.commit()
        await dispatch_command(connector, command)
        return {
            **command_response(command),
            "identity_snapshot": {
                "revision": connector.zkt_device.identity_snapshot_revision,
                "observed_at": connector.zkt_device.identity_snapshot_observed_at,
                "stable": connector.zkt_device.identity_snapshot_stable,
            } if connector.zkt_device else None,
        }
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v2/devices/{connector_id}/users", status_code=202)
async def create_user_v2(
    connector_id: str,
    body: UserCreateRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    try:
        user, command = create_device_user_command(
            db,
            connector=connector,
            display_name=body.display_name,
            cnic=body.cnic,
            shift_worker=body.shift_worker,
            user_id_override=body.user_id_override,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await dispatch_command(connector, command)
    return {"user": serialize_user(user, zkt=connector.zkt_device), "command": command_response(command)}


@app.patch("/api/v2/devices/{connector_id}/users/{user_key}", status_code=202)
async def update_user_v2(
    connector_id: str,
    user_key: str,
    body: UserUpdateRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    user = db.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == user_key,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail="Device user not found.")
    if body.privilege == 14 and user.privilege != 14:
        expected_confirmation = f"ELEVATE {user.user_id} ON {connector.device_id}"
        if body.typed_confirmation != expected_confirmation:
            raise HTTPException(
                status_code=409,
                detail=f"Typed confirmation must exactly match: {expected_confirmation}",
            )
        if body.reason is None:
            raise HTTPException(
                status_code=422,
                detail="A reason is required for permanent administrator elevation.",
            )
    try:
        command = update_device_user_command(
            db,
            connector=connector,
            user=user,
            display_name=body.display_name,
            cnic=body.cnic,
            shift_worker=body.shift_worker,
            privilege=body.privilege,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
            actor=context.username,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await dispatch_command(connector, command)
    return {"user": serialize_user(user, zkt=zkt), "command": command_response(command)}


@app.delete("/api/v2/devices/{connector_id}/users/{user_key}", status_code=202)
async def delete_user_v2(
    connector_id: str,
    user_key: str,
    body: UserDeleteRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    user = db.scalar(
        select(DeviceUser).where(
            DeviceUser.zkt_device_id == zkt.id,
            DeviceUser.user_key == user_key,
        )
    )
    if user is None:
        raise HTTPException(status_code=404, detail="Device user not found.")
    try:
        command = delete_device_user_command(
            db,
            connector=connector,
            user=user,
            expected_version=body.expected_version,
            typed_confirmation=body.typed_confirmation,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await dispatch_command(connector, command)
    return {"user": serialize_user(user, zkt=zkt), "command": command_response(command)}


@app.post("/api/v2/devices/{connector_id}/user-deletion-jobs", status_code=202)
def create_user_deletion_job_v2(
    connector_id: str,
    body: BulkUserDeleteRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    try:
        job = create_user_deletion_job(
            db,
            connector=connector,
            targets=[
                (target.user_key, target.expected_version) for target in body.targets
            ],
            reason=body.reason,
            typed_confirmation=body.typed_confirmation,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return {"job": serialize_user_deletion_job(db, job)}


@app.get("/api/v2/devices/{connector_id}/user-deletion-jobs/latest")
def latest_user_deletion_job_v2(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    job = db.scalar(
        select(UserDeletionJob)
        .where(UserDeletionJob.connector_id == connector.id)
        .order_by(UserDeletionJob.created_at.desc())
    )
    return {"job": serialize_user_deletion_job(db, job) if job else None}


@app.get("/api/v2/user-deletion-jobs/{job_id}")
def get_user_deletion_job_v2(
    job_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    job = db.scalar(select(UserDeletionJob).where(UserDeletionJob.job_id == job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="User deletion job not found.")
    return {"job": serialize_user_deletion_job(db, job)}


@app.post("/api/v2/user-deletion-jobs/{job_id}/cancel", status_code=202)
def cancel_user_deletion_job_v2(
    job_id: str,
    body: BulkUserDeleteCancelRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    job = db.scalar(select(UserDeletionJob).where(UserDeletionJob.job_id == job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="User deletion job not found.")
    cancel_user_deletion_job(db, job=job, actor=context.username)
    db.commit()
    return {"job": serialize_user_deletion_job(db, job)}


@app.post("/api/v1/devices/{connector_id}/admin-leases", status_code=202)
async def grant_admin_lease(
    connector_id: str,
    body: AdminLeaseRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    user = db.scalar(select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id, DeviceUser.uid == body.uid))
    if user is None:
        raise HTTPException(status_code=404, detail="Device user not found.")
    try:
        lease, command = create_admin_lease(
            db,
            connector=connector,
            user=user,
            idempotency_key=body.idempotency_key,
            actor=context.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await dispatch_command(connector, command)
    return {"lease": serialize_lease(lease), "command": command_response(command)}


@app.post("/api/v1/admin-leases/{lease_id}/revoke", status_code=202)
async def revoke_admin_lease(
    lease_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    lease = db.scalar(select(TemporaryAdminLease).where(TemporaryAdminLease.lease_id == lease_id))
    if lease is None:
        raise HTTPException(status_code=404, detail="Lease not found.")
    zkt = db.get(ZKTDevice, lease.zkt_device_id)
    connector = db.get(Connector, zkt.connector_id) if zkt else None
    user = db.get(DeviceUser, lease.device_user_id)
    if connector is None or user is None:
        raise HTTPException(status_code=409, detail="Lease target is unavailable.")
    command = create_command(
        db,
        connector=connector,
        command_type="REVOKE_TEMP_ADMIN",
        payload={"lease_id": lease.lease_id, "uid": user.uid, "user_id": user.user_id},
        expected_state={
            "serial": zkt.serial if zkt else None,
            "uid": user.uid,
            "user_id": user.user_id,
            "privilege": 14,
            **terminal_fingerprint_preconditions(user),
        },
        desired_state={"privilege": 0},
        idempotency_key=f"manual-revoke:{lease.lease_id}",
        actor=context.username,
        expires_in_seconds=None,
    )
    lease.revoke_command_id = command.id
    lease.state = "REVOKING"
    lease.updated_at = utc_now()
    db.commit()
    await dispatch_command(connector, command)
    return {"lease": serialize_lease(lease), "command": command_response(command)}


@app.post("/api/v1/devices/{connector_id}/restart", status_code=202)
async def restart_device(
    connector_id: str,
    body: RestartRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None or not zkt.capability_profile.get("protocol_restart", False):
        raise HTTPException(status_code=409, detail="This ZKT profile is not certified for restart.")
    active_lease = db.scalar(
        select(TemporaryAdminLease).where(
            TemporaryAdminLease.zkt_device_id == zkt.id,
            TemporaryAdminLease.state.in_(["GRANTING", "ACTIVE", "REVOKING", "OVERDUE"]),
        )
    )
    if active_lease:
        raise HTTPException(status_code=409, detail="Restart is blocked by an active enrollment lease.")
    command = create_command(
        db,
        connector=connector,
        command_type="RESTART_ZKT",
        payload={"reason": body.reason, "mode": "protocol"},
        expected_state={"serial": zkt.serial or zkt.expected_serial},
        desired_state={"restart": True},
        idempotency_key=body.idempotency_key,
        actor=context.username,
        expires_in_seconds=120,
    )
    db.commit()
    await dispatch_command(connector, command)
    return command_response(command)


@app.get("/api/v1/attendance")
def attendance(
    device_id: str | None = None,
    q: str | None = None,
    cnic: str | None = None,
    punch: str | None = None,
    source: str | None = None,
    clock_quality: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    cursor: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    statement = select(AttendanceEvent).where(
        AttendanceEvent.ords_status != "QUARANTINED_INVALID_EVENT_UID"
    )
    if device_id:
        connector = connector_or_404(db, device_id)
        statement = statement.where(AttendanceEvent.connector_id == connector.id)
    if cursor:
        statement = statement.where(AttendanceEvent.id < cursor)
    if q:
        like = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                AttendanceEvent.display_name.ilike(like),
                AttendanceEvent.user_id.ilike(like),
                AttendanceEvent.uid.ilike(like),
            )
        )
    if cnic:
        normalized = normalize_cnic(cnic)
        if normalized is None:
            raise HTTPException(status_code=422, detail="CNIC must contain exactly 13 digits.")
        statement = statement.where(AttendanceEvent.cnic_lookup_hash == cnic_lookup(normalized))
    if punch:
        statement = statement.where(AttendanceEvent.punch == punch)
    if source:
        statement = statement.where(AttendanceEvent.source == source)
    if clock_quality:
        statement = statement.where(AttendanceEvent.clock_quality == clock_quality)
    if from_time:
        statement = statement.where(AttendanceEvent.device_event_time >= parse_datetime(from_time))
    if to_time:
        statement = statement.where(AttendanceEvent.device_event_time <= parse_datetime(to_time))
    rows = db.scalars(statement.order_by(AttendanceEvent.id.desc()).limit(limit + 1)).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return {"rows": [serialize_attendance(row) for row in rows[:limit]], "next_cursor": next_cursor}


@app.get("/api/v1/devices/{connector_id}/logs")
def logs(
    connector_id: str,
    level: str | None = None,
    subsystem: str | None = None,
    cursor: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    statement = select(DeviceLog).where(DeviceLog.connector_id == connector.id)
    if cursor:
        statement = statement.where(DeviceLog.id < cursor)
    if level:
        statement = statement.where(DeviceLog.level == level.upper())
    if subsystem:
        statement = statement.where(DeviceLog.subsystem == subsystem)
    rows = db.scalars(statement.order_by(DeviceLog.id.desc()).limit(limit + 1)).all()
    return {
        "rows": [serialize_log(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@app.get("/api/v1/devices/{connector_id}/connectivity")
def connectivity_history(
    connector_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    rows = db.scalars(
        select(DeviceConnectionEvent)
        .where(DeviceConnectionEvent.connector_id == connector.id)
        .order_by(DeviceConnectionEvent.id.desc())
        .limit(limit)
    ).all()
    return {
        "rows": [
            {
                "id": row.id,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "reason": row.reason,
                "consecutive_failures": row.consecutive_failures,
                "consecutive_successes": row.consecutive_successes,
                "flap_count_15m": row.flap_count_15m,
                "observed_at": row.observed_at,
            }
            for row in rows
        ]
    }


@app.get("/api/v1/devices/{connector_id}/alerts")
def alerts(connector_id: str, auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    connector = connector_or_404(db, connector_id)
    rows = db.scalars(
        select(DeviceAlert).where(DeviceAlert.connector_id == connector.id).order_by(DeviceAlert.last_seen_at.desc())
    ).all()
    return {"rows": [serialize_alert(row) for row in rows]}


@app.get("/api/v1/alerts")
def global_alerts(
    state: str | None = None,
    severity: str | None = None,
    connector_id: str | None = None,
    zone_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    scope_filters = []
    state_filter = []
    if state:
        state_filter.append(DeviceAlert.state == state.strip().upper())
    if severity:
        scope_filters.append(DeviceAlert.severity == severity.strip().upper())
    if connector_id:
        scope_filters.append(Connector.connector_id == connector_id)
    if zone_id:
        scope_filters.append(Connector.zone_id == zone_id)
    severity_order = case(
        (DeviceAlert.severity == "CRITICAL", 0),
        (DeviceAlert.severity == "HIGH", 1),
        (DeviceAlert.severity == "WARNING", 2),
        (DeviceAlert.severity == "MEDIUM", 3),
        (DeviceAlert.severity == "INFO", 4),
        else_=5,
    )
    page_filters = [*scope_filters, *state_filter]
    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            cursor_payload = json.loads(raw)
            cursor_rank = int(cursor_payload["severity_rank"])
            cursor_seen_at = parse_datetime(str(cursor_payload["last_seen_at"]))
            cursor_id = int(cursor_payload["id"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as error:
            raise HTTPException(status_code=422, detail="Alert cursor is invalid or expired.") from error
        page_filters.append(
            or_(
                severity_order > cursor_rank,
                and_(severity_order == cursor_rank, DeviceAlert.last_seen_at < cursor_seen_at),
                and_(
                    severity_order == cursor_rank,
                    DeviceAlert.last_seen_at == cursor_seen_at,
                    DeviceAlert.id < cursor_id,
                ),
            )
        )
    rows = db.execute(
        select(DeviceAlert, Connector)
        .join(Connector, Connector.id == DeviceAlert.connector_id)
        .where(*page_filters)
        .order_by(severity_order.asc(), DeviceAlert.last_seen_at.desc(), DeviceAlert.id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    def count_for(alert_state: str | None = None) -> int:
        state_filter = [DeviceAlert.state == alert_state] if alert_state else []
        return int(
            db.scalar(
                select(func.count(DeviceAlert.id))
                .select_from(DeviceAlert)
                .join(Connector, Connector.id == DeviceAlert.connector_id)
                .where(*scope_filters, *state_filter)
            )
            or 0
        )

    next_cursor = None
    if len(rows) > limit and page:
        last_row = page[-1][0]
        rank = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "MEDIUM": 3, "INFO": 4}.get(
            last_row.severity,
            5,
        )
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "severity_rank": rank,
                    "last_seen_at": last_row.last_seen_at.isoformat(),
                    "id": last_row.id,
                },
                separators=(",", ":"),
            ).encode()
        ).rstrip(b"=").decode()

    return {
        "rows": [
            {
                **serialize_alert(row),
                "device": {
                    "connector_id": connector.connector_id,
                    "display_name": connector.display_name,
                    "zone_id": connector.zone_id,
                    "hardware_id": connector.hardware_id,
                },
            }
            for row, connector in page
        ],
        "next_cursor": next_cursor,
        "totals": {
            "all": count_for(),
            "open": count_for("OPEN"),
            "acknowledged": count_for("ACKNOWLEDGED"),
            "resolved": count_for("RESOLVED"),
        },
    }


@app.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    request: Request,
    alert_id: int,
    body: AlertAcknowledgeRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = db.get(DeviceAlert, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    row.state = "ACKNOWLEDGED"
    row.acknowledged_at = utc_now()
    append_audit(
        db,
        actor=context.username,
        action="ALERT_ACKNOWLEDGED",
        target_type="alert",
        target_id=str(alert_id),
        outcome="SUCCESS",
        ip_address=client_ip(request),
        after={"note": body.note},
    )
    return serialize_alert(row)


@app.get("/api/v1/commands/{command_id}")
@app.get("/api/v2/commands/{command_id}")
def command(command_id: str, auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    row = db.scalar(select(DeviceCommand).where(DeviceCommand.command_id == command_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Command not found.")
    return command_response(row)


@app.post("/api/v2/commands/{command_id}/cancel")
async def cancel_command(
    command_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    row = db.scalar(select(DeviceCommand).where(DeviceCommand.command_id == command_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Command not found.")
    if row.status == "CANCEL_REQUESTED":
        return command_response(row)
    if row.status in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
        raise HTTPException(status_code=409, detail="This command can no longer be cancelled.")
    connector = db.get(Connector, row.connector_id)
    can_cancel_locally = row.attempt_count == 0 and row.status in {
        "QUEUED",
        "WAITING_FOR_DEVICE",
        "WAITING_FOR_ZKT",
    }
    row.status = "CANCELLED" if can_cancel_locally else "CANCEL_REQUESTED"
    if can_cancel_locally:
        row.completed_at = utc_now()
        apply_user_command_terminal_state(db, command=row, status="CANCELLED")
        reconcile_admin_lease_command(db, command=row, now=row.completed_at)
    db.add(
        DeviceCommandEvent(
            command_id=row.id,
            status=row.status,
            details={"requested_by": context.username},
        )
    )
    append_audit(
        db,
        actor=context.username,
        action=f"COMMAND_{row.command_type}_{row.status}",
        target_type="command",
        target_id=row.command_id,
        outcome=row.status,
    )
    db.commit()
    if connector and not can_cancel_locally:
        await connector_hub.send(
            connector.connector_id,
            {"schema_version": "2", "type": "command_cancel", "command_id": row.command_id},
        )
    await browser_events.publish("command", command_response(row))
    return command_response(row)


@app.get("/events/v1/stream")
async def browser_stream(
    request: Request,
    last_event_id: int | None = Query(default=None),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    _db, _context = auth
    header_id = request.headers.get("Last-Event-ID")
    if header_id and header_id.isdigit():
        last_event_id = int(header_id)

    async def stream():
        async for event in browser_events.subscribe(last_event_id):
            if await request.is_disconnected():
                break
            yield sse_encode(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/device/v1/attendance/batches")
async def device_attendance(
    body: object = Body(...),
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    settlement = settle_attendance_batch(db, connector=connector, payload=body)
    db.commit()
    for event_uid in settlement.accepted_event_uids:
        await browser_events.publish("attendance", {"connector_id": connector.connector_id, "event_uid": event_uid})
    return settlement.response()


@app.post("/device/v2/reconciliations/anchor")
async def device_reconciliation_anchor(
    body: ReconciliationAnchorRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    try:
        job = apply_reconciliation_anchor(db, connector=connector, payload=body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "reconciliation", {"job_id": job.job_id, "phase": job.phase}
    )
    return {
        "ok": job.status != "NEEDS_ATTENTION",
        "job_id": job.job_id,
        "status": job.status,
        "phase": job.phase,
        "committed_next_ordinal": job.committed_next_ordinal,
    }


@app.post("/device/v2/reconciliations/chunks")
async def device_reconciliation_chunk(
    body: ReconciliationChunkRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    try:
        job, chunk, duplicate = apply_reconciliation_chunk(
            db, connector=connector, payload=body
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "reconciliation",
        {
            "job_id": job.job_id,
            "phase": job.phase,
            "committed_next_ordinal": job.committed_next_ordinal,
        },
    )
    return {
        "ok": job.status != "NEEDS_ATTENTION",
        "job_id": job.job_id,
        "generation": chunk.generation,
        "sequence": chunk.sequence,
        "committed_next_ordinal": job.committed_next_ordinal,
        "resulting_chain_digest": chunk.resulting_chain_digest,
        "duplicate": duplicate,
    }


@app.post("/device/v2/reconciliations/manifest")
async def device_reconciliation_manifest(
    body: ReconciliationManifestRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    try:
        job = apply_reconciliation_manifest(db, connector=connector, payload=body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish(
        "reconciliation", {"job_id": job.job_id, "status": job.status, "phase": job.phase}
    )
    return {
        "ok": job.capture_certified_at is not None,
        "job_id": job.job_id,
        "status": job.status,
        "phase": job.phase,
        "capture_certificate": job.capture_certificate or None,
        "oracle_certificate": job.oracle_certificate or None,
    }


@app.post("/device/v1/user-snapshots")
async def device_users(
    body: UserSnapshotRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    try:
        count = replace_user_snapshot(db, connector=connector, snapshot=body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish("users", {"connector_id": connector.connector_id, "count": count})
    return {"ok": True, "snapshot_id": body.snapshot_id, "count": count}


@app.post("/device/v1/logs/batches")
async def device_logs(
    body: LogBatchRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    count = ingest_logs(db, connector=connector, logs=body.logs)
    db.commit()
    for item in body.logs[-min(count, 50):]:
        await browser_events.publish(
            "log",
            {
                "connector_id": connector.connector_id,
                "level": item.level,
                "subsystem": item.subsystem,
                "code": item.code,
                "message": item.message,
                "device_time": item.device_time,
            },
        )
    return {"ok": True, "accepted": count}


@app.get("/device/v1/commands")
async def poll_commands(auth: tuple[Session, Connector] = Depends(require_connector)):
    db, connector = auth
    rows = db.scalars(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
            or_(DeviceCommand.expires_at == None, DeviceCommand.expires_at > utc_now()),  # noqa: E711
        ).order_by(DeviceCommand.created_at.asc()).limit(10)
    ).all()
    for row in rows:
        if row.status in {"QUEUED", "WAITING_FOR_DEVICE", "WAITING_FOR_ZKT", "RETRYING"}:
            row.status = "DISPATCHED"
            row.dispatched_at = utc_now()
            row.attempt_count += 1
    return {"commands": [serialize_command(row) for row in rows]}


@app.post("/device/v1/commands/result")
async def command_result(
    body: CommandUpdate,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    try:
        row = apply_command_update(
            db,
            connector=connector,
            command_id=body.command_id,
            status=body.status,
            result=body.result,
            error_code=body.error_code,
            error_message=body.error_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    await browser_events.publish("command", command_response(row))
    return {"ok": True, "command_id": row.command_id, "status": row.status}


@app.get("/device/v1/config")
@app.get("/device/v2/config")
async def device_config(auth: tuple[Session, Connector] = Depends(require_connector)):
    db, connector = auth
    tombstones = []
    if connector.zkt_device:
        for row in db.scalars(
            select(IdentityTombstone)
            .where(IdentityTombstone.zkt_device_id == connector.zkt_device.id)
            .order_by(IdentityTombstone.id.asc())
        ).all():
            tombstones.append(
                {
                    "uid": row.uid,
                    "user_id": row.user_id,
                    "display_name": decrypt_text(row.display_name_encrypted),
                    "cnic": decrypt_cnic(row.cnic_encrypted),
                    "shift_worker": row.shift_worker,
                }
            )
    return {
        "config_version": connector.config_version,
        "heartbeat_seconds": settings.heartbeat_interval_seconds,
        "reconcile_seconds": settings.reconcile_interval_seconds,
        "user_integrity_seconds": settings.user_integrity_interval_seconds,
        "timezone": "Asia/Karachi",
        "restart_slots": ["02:00", "12:00", "22:00"],
        "restart_window_minutes": 30,
        "restart_jitter_minutes": 10,
        "led_fault_latch_seconds": 120,
        "identity_tombstones": tombstones,
    }


@app.websocket("/device/v1/stream")
@app.websocket("/device/v2/stream")
async def device_stream(websocket: WebSocket):
    connector_id = websocket.query_params.get("connector_id") or websocket.headers.get("X-ADD-Connector-Id")
    authorization = websocket.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not connector_id or not token:
        await websocket.close(code=4401)
        return
    with session_scope() as db:
        try:
            connector = authenticate_websocket_token(db, connector_id, token)
        except ValueError:
            await websocket.close(code=4401)
            return
        connector_pk = connector.id
    await connector_hub.connect(connector_id, websocket)
    with session_scope() as db:
        connector = db.get(Connector, connector_pk)
        if connector:
            connector.connected = True
            connector.lifecycle_state = "ONLINE"
            connector.last_seen_at = utc_now()
    await browser_events.publish("device", {"connector_id": connector_id, "state": "ONLINE"})
    with session_scope() as db:
        connector = db.get(Connector, connector_pk)
        catalog = identity_catalog_payload(db, connector) if connector else {
            "schema_version": "2",
            "type": "identity_catalog",
            "rows": [],
        }
    if not await send_identity_catalog(connector_id, catalog):
        await websocket.close(code=1011, reason="Identity catalog delivery failed")
        return
    with session_scope() as db:
        connector = db.get(Connector, connector_pk)
        coverage = (
            active_coverage(db, connector.zkt_device)
            if connector is not None and connector.zkt_device is not None
            else None
        )
        coverage_payload = serialize_coverage(coverage)
        if coverage_payload is None and connector is not None and connector.zkt_device is not None:
            terminal_serial = (
                connector.zkt_device.serial or connector.zkt_device.expected_serial
            )
            if terminal_serial:
                coverage_payload = {
                    "terminal_serial": terminal_serial,
                    "terminal_generation": max(1, connector.onboarding_generation),
                    "source_committed_cursor": 0,
                    "source_committed_chain_digest": "0" * 64,
                    "active": False,
                }
    if coverage_payload is not None:
        if not await connector_hub.send(
            connector_id,
            {"type": "source_coverage", **coverage_payload},
        ):
            await websocket.close(code=1011, reason="Source coverage delivery failed")
            return
    await send_pending_commands(connector_id)
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 512 * 1024:
                await websocket.close(code=4400, reason="Message too large")
                break
            try:
                envelope = Envelope.model_validate_json(raw)
            except Exception:
                await websocket.send_json({"type": "error", "code": "INVALID_ENVELOPE"})
                continue
            if envelope.connector_id != connector_id:
                await websocket.close(code=4403)
                break
            try:
                await handle_envelope(connector_pk, envelope, websocket)
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                if envelope.type == "source_tail_chunk":
                    # Pydantic validation traces may echo rejected field input.
                    # Source-tail records contain encrypted-at-rest raw terminal
                    # evidence, so this path records metadata only.
                    logger.error(
                        "Rejected protected source envelope connector_id=%s "
                        "type=%s message_id=%s error_type=%s",
                        connector_id,
                        envelope.type,
                        envelope.message_id,
                        type(exc).__name__,
                    )
                else:
                    logger.exception(
                        "Rejected connector envelope connector_id=%s type=%s message_id=%s",
                        connector_id,
                        envelope.type,
                        envelope.message_id,
                    )
                record_envelope_rejection(connector_pk, envelope, exc)
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "MESSAGE_REJECTED",
                        "message_id": envelope.message_id,
                        "message_type": envelope.type,
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        await connector_hub.disconnect(connector_id, websocket)
        with session_scope() as db:
            connector = db.get(Connector, connector_pk)
            if connector:
                connector.connected = False
                connector.last_disconnect_at = utc_now()
        await browser_events.publish("device", {"connector_id": connector_id, "connected": False})


def record_envelope_rejection(connector_pk: int, envelope: Envelope, error: Exception) -> None:
    error_type = type(error).__name__[:80]
    safe_detail = None
    if isinstance(error, ValueError):
        detail = str(error)
        if detail.startswith(("Duplicate UID ", "Duplicate device user ID ", "Connector has no ")):
            safe_detail = detail[:300]
    message = safe_detail or f"{envelope.type} message was rejected ({error_type})."
    with session_scope() as db:
        connector = db.get(Connector, connector_pk)
        if connector is None:
            return
        connector.lifecycle_state = "DEGRADED"
        connector.last_error_code = "DEVICE_MESSAGE_REJECTED"
        connector.last_error_message = message
        ingest_logs(
            db,
            connector=connector,
            logs=[
                DeviceLogIn(
                    boot_id=envelope.boot_id,
                    sequence=envelope.seq,
                    level="ERROR",
                    subsystem="add_backend",
                    code="DEVICE_MESSAGE_REJECTED",
                    message=message,
                    context={"message_type": envelope.type, "error_type": error_type},
                    device_time=envelope.sent_at,
                )
            ],
        )
        upsert_alert(
            db,
            connector,
            code="DEVICE_MESSAGE_REJECTED",
            severity="HIGH",
            message=message,
            details={"message_type": envelope.type, "error_type": error_type},
        )


async def handle_envelope(connector_pk: int, envelope: Envelope, websocket: WebSocket) -> None:
    event_payload = None
    ack_payload = None
    authoritative_coverage_payload = None
    send_commands_after_ack = False
    connector_id_to_flush = None
    with session_scope() as db:
        connector = db.get(Connector, connector_pk)
        if connector is None:
            return
        if connector.boot_id == envelope.boot_id and envelope.seq <= connector.last_sequence:
            await websocket.send_json({"type": "ack", "message_id": envelope.message_id, "duplicate": True})
            return
        connector.boot_id = envelope.boot_id
        connector.last_sequence = envelope.seq
        connector.last_seen_at = utc_now()
        connector.connected = True
        if envelope.type == "heartbeat":
            payload = HeartbeatPayload.model_validate(envelope.payload)
            event_payload = update_heartbeat(
                db,
                connector=connector,
                boot_id=envelope.boot_id,
                sequence=envelope.seq,
                payload=payload,
            )
            # A firmware capability heartbeat can materialize a previously staged
            # recovery command. The initial WebSocket backlog was sent before this
            # heartbeat, so flush the active queue again after its ACK.
            send_commands_after_ack = True
            connector_id_to_flush = connector.connector_id
            from zk_add.provisioning_api import correlate_onboarding

            provisioning_session = correlate_onboarding(db, connector)
            if provisioning_session:
                await browser_events.publish(
                    "provisioning",
                    {
                        "session_id": provisioning_session.session_id,
                        "state": provisioning_session.state,
                    },
                )
        elif envelope.type == "command_update":
            update = CommandUpdate.model_validate(envelope.payload)
            command = apply_command_update(
                db,
                connector=connector,
                command_id=update.command_id,
                status=update.status,
                result=update.result,
                error_code=update.error_code,
                error_message=update.error_message,
            )
            event_payload = command_response(command)
        elif envelope.type == "log":
            log = DeviceLogIn(
                boot_id=envelope.boot_id,
                sequence=envelope.seq,
                level=envelope.payload.get("level", "INFO"),
                subsystem=envelope.payload.get("subsystem", "firmware"),
                code=envelope.payload.get("code"),
                message=envelope.payload.get("message", ""),
                context=envelope.payload.get("context", {}),
                device_time=envelope.sent_at,
            )
            ingest_logs(db, connector=connector, logs=[log])
            event_payload = {"connector_id": connector.connector_id, **envelope.payload}
        elif envelope.type == "user_snapshot":
            snapshot = UserSnapshotRequest.model_validate(envelope.payload)
            count = replace_user_snapshot(db, connector=connector, snapshot=snapshot)
            resolve_message_rejection(
                db, connector, message_type="user_snapshot"
            )
            event_payload = {"connector_id": connector.connector_id, "count": count}
        elif envelope.type == "attendance_batch":
            settlement = settle_attendance_batch(
                db, connector=connector, payload=envelope.payload
            )
            event_payload = {
                "connector_id": connector.connector_id,
                "receipt_id": settlement.receipt_id,
                "outcome": settlement.outcome,
                "accepted": settlement.accepted_count,
                "duplicates": settlement.duplicate_count,
                "quarantined": settlement.quarantined_count,
            }
            ack_payload = settlement.ack(
                message_id=envelope.message_id, sequence=envelope.seq
            )
        elif envelope.type == "oracle_receipt_batch":
            receipt_batch = OracleReceiptBatchRequest.model_validate(envelope.payload)
            applied, awaiting_event, rejected = record_oracle_receipts(
                db,
                connector=connector,
                batch=receipt_batch,
            )
            event_payload = {
                "connector_id": connector.connector_id,
                "applied": applied,
                "awaiting_event": awaiting_event,
                "rejected": rejected,
                "confirmation_path": receipt_batch.confirmation_path,
            }
        elif envelope.type == "reconcile_anchor":
            anchor = ReconciliationAnchorRequest.model_validate(envelope.payload)
            job = apply_reconciliation_anchor(db, connector=connector, payload=anchor)
            event_payload = {
                "connector_id": connector.connector_id,
                "job_id": job.job_id,
                "status": job.status,
                "phase": job.phase,
            }
            ack_payload = {
                "type": (
                    "error" if job.status == "NEEDS_ATTENTION" else "reconcile_anchor_ack"
                ),
                "message_id": envelope.message_id,
                "code": job.error_code,
                "message_type": envelope.type,
                "job_id": job.job_id,
                "status": job.status,
                "committed_next_ordinal": job.committed_next_ordinal,
            }
        elif envelope.type == "reconcile_assignment_release":
            release = ReconciliationAssignmentReleaseRequest.model_validate(
                envelope.payload
            )
            job = apply_reconciliation_assignment_release(
                db,
                connector=connector,
                payload=release,
            )
            event_payload = {
                "connector_id": connector.connector_id,
                "job_id": job.job_id,
                "phase": job.phase,
                "committed_next_ordinal": job.committed_next_ordinal,
            }
        elif envelope.type == "reconcile_chunk":
            source_chunk = ReconciliationChunkRequest.model_validate(envelope.payload)
            job, chunk, duplicate = apply_reconciliation_chunk(
                db, connector=connector, payload=source_chunk
            )
            event_payload = {
                "connector_id": connector.connector_id,
                "job_id": job.job_id,
                "phase": job.phase,
                "committed_next_ordinal": job.committed_next_ordinal,
            }
            ack_payload = {
                "type": (
                    "error" if job.status == "NEEDS_ATTENTION" else "reconcile_chunk_ack"
                ),
                "message_id": envelope.message_id,
                "code": job.error_code,
                "message_type": envelope.type,
                "job_id": job.job_id,
                "assignment_id": source_chunk.assignment_id,
                "generation": chunk.generation if chunk is not None else source_chunk.generation,
                "sequence": chunk.sequence if chunk is not None else source_chunk.sequence,
                "committed_next_ordinal": job.committed_next_ordinal,
                "resulting_chain_digest": (
                    chunk.resulting_chain_digest
                    if chunk is not None
                    else source_chunk.resulting_chain_digest
                ),
                "duplicate": duplicate,
                "continue_allowed": bool(
                    source_chunk.assignment_id
                    and job.active_assignment_id == source_chunk.assignment_id
                    and job.credit_end_ordinal is not None
                    and job.committed_next_ordinal < job.credit_end_ordinal
                ),
                "credit_end_ordinal": job.credit_end_ordinal,
                # WebSocket.send_json uses the standard JSON encoder directly,
                # unlike FastAPI HTTP responses. Keep this value JSON-native so
                # a durable chunk commit can always receive its ACK.
                "lease_expires_at": (
                    job.assignment_expires_at.isoformat()
                    if job.assignment_expires_at is not None
                    else None
                ),
            }
        elif envelope.type == "reconcile_source_manifest":
            manifest = ReconciliationManifestRequest.model_validate(envelope.payload)
            job = apply_reconciliation_manifest(db, connector=connector, payload=manifest)
            event_payload = {
                "connector_id": connector.connector_id,
                "job_id": job.job_id,
                "status": job.status,
                "phase": job.phase,
            }
            ack_payload = {
                "type": (
                    "error"
                    if job.capture_certified_at is None
                    else "reconcile_manifest_ack"
                ),
                "message_id": envelope.message_id,
                "code": job.error_code,
                "message_type": envelope.type,
                "job_id": job.job_id,
                "status": job.status,
                "capture_certificate": job.capture_certificate or None,
            }
        elif envelope.type == "source_probe_result":
            probe = SourceProbeResultRequest.model_validate(envelope.payload)
            job = apply_source_probe_result(db, connector=connector, payload=probe)
            event_payload = {
                "connector_id": connector.connector_id,
                "job_id": job.job_id,
                "status": job.status,
                "phase": job.phase,
            }
            ack_payload = {
                "type": "source_probe_ack",
                "message_id": envelope.message_id,
                "message_type": envelope.type,
                "job_id": job.job_id,
                "status": job.status,
                "phase": job.phase,
                "committed_next_ordinal": job.committed_next_ordinal,
            }
            if job.phase == "RECOVERING_AFTER_SOURCE_CHANGE":
                # A confirmed mutation supersedes the old coverage epoch. Tell a
                # still-connected ESP immediately so it cannot continue tail work
                # with a locally cached certificate while the replacement epoch is
                # rebuilt from ADD's preserved checkpoint.
                authoritative_coverage_payload = {
                    "type": "source_coverage",
                    "source_epoch_id": job.source_epoch_id,
                    "terminal_serial": job.terminal_serial,
                    "terminal_generation": job.terminal_generation,
                    "source_committed_cursor": 0,
                    "source_committed_chain_digest": "0" * 64,
                    "active": False,
                }
        elif envelope.type == "source_tail_chunk":
            tail = SourceTailChunkRequest.model_validate(envelope.payload)
            coverage, chunk, duplicate, error_code = apply_source_tail_chunk(
                db,
                connector=connector,
                payload=tail,
            )
            event_payload = {
                "connector_id": connector.connector_id,
                "coverage_id": coverage.coverage_id,
                "source_committed_cursor": coverage.source_committed_cursor,
                "tail_exception_count": coverage.tail_exception_count,
                "error_code": error_code,
            }
            ack_payload = {
                "type": "error" if error_code else "source_tail_ack",
                "message_id": envelope.message_id,
                "message_type": envelope.type,
                "code": error_code,
                "terminal_serial": coverage.terminal_serial,
                "terminal_generation": coverage.terminal_generation,
                "committed_next_ordinal": (
                    chunk.end_ordinal
                    if chunk is not None
                    else coverage.source_committed_cursor
                ),
                "resulting_chain_digest": (
                    chunk.resulting_chain_digest
                    if chunk is not None
                    else coverage.source_committed_chain_digest
                ),
                "duplicate": duplicate,
                "exception_count": chunk.exception_count if chunk is not None else 0,
            }
        else:
            event_payload = {"connector_id": connector.connector_id, "type": envelope.type}
    await websocket.send_json(
        ack_payload
        or {"type": "ack", "message_id": envelope.message_id, "seq": envelope.seq}
    )
    if send_commands_after_ack and connector_id_to_flush:
        await send_pending_commands(connector_id_to_flush)
    if authoritative_coverage_payload is not None:
        await websocket.send_json(authoritative_coverage_payload)
    await browser_events.publish(envelope.type, event_payload or {})


async def send_pending_commands(connector_id: str) -> None:
    with session_scope() as db:
        connector = connector_or_404(db, connector_id)
        rows = db.scalars(
            select(DeviceCommand).where(
                DeviceCommand.connector_id == connector.id,
                DeviceCommand.status.in_(ACTIVE_COMMAND_STATES),
                or_(DeviceCommand.expires_at == None, DeviceCommand.expires_at > utc_now()),  # noqa: E711
            ).order_by(DeviceCommand.created_at.asc())
        ).all()
        payloads = [serialize_command(row) for row in rows]
    for payload in payloads:
        await connector_hub.send(connector_id, payload)


async def dispatch_command(connector: Connector, command: DeviceCommand) -> None:
    if await connector_hub.send(connector.connector_id, serialize_command(command)):
        with session_scope() as db:
            row = db.scalar(select(DeviceCommand).where(DeviceCommand.command_id == command.command_id))
            if row and row.status in {
                "QUEUED",
                "WAITING_FOR_DEVICE",
                "WAITING_FOR_ZKT",
                "RETRYING",
            }:
                row.status = "DISPATCHED"
                row.dispatched_at = utc_now()
                row.attempt_count += 1


def command_response(row: DeviceCommand) -> dict:
    return {
        "command_id": row.command_id,
        "type": row.command_type,
        "status": row.status,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "dispatched_at": row.dispatched_at,
        "acknowledged_at": row.acknowledged_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "result": row.result,
        "error_code": row.error_code,
        "error_message": row.error_message,
    }


def device_user_identity_integrity(
    db: Session, *, zkt: ZKTDevice
) -> tuple[
    dict,
    dict[int, list[dict[str, str]]],
    dict[int, IdentityConflictResolution],
]:
    """Build exact-match evidence without returning CNICs or lookup hashes."""

    active = list(
        db.scalars(
            select(DeviceUser).where(
                DeviceUser.zkt_device_id == zkt.id,
                DeviceUser.lifecycle_state == "ACTIVE",
                DeviceUser.present == True,  # noqa: E712
            )
        ).all()
    )
    groups: dict[str, list[DeviceUser]] = {}
    for user in active:
        if user.cnic_lookup_hash:
            groups.setdefault(user.cnic_lookup_hash, []).append(user)
    duplicate_groups = [group for group in groups.values() if len(group) > 1]
    valid_resolutions = valid_identity_resolutions(db, zkt=zkt, groups=groups)
    conflict_members: dict[int, list[dict[str, str]]] = {}
    resolutions_by_user: dict[int, IdentityConflictResolution] = {}
    for group in duplicate_groups:
        ordered = sorted(group, key=lambda user: (user.user_id, user.uid))
        resolution = valid_resolutions.get(ordered[0].cnic_lookup_hash or "")
        for user in ordered:
            if user.id is None:
                continue
            if resolution is not None:
                resolutions_by_user[user.id] = resolution
            conflict_members[user.id] = [
                {"user_id": member.user_id, "uid": member.uid}
                for member in ordered
                if member.id != user.id
            ]
    with_cnic = sum(bool(user.cnic_lookup_hash) for user in active)
    unresolved_groups = [
        group
        for group in duplicate_groups
        if (group[0].cnic_lookup_hash or "") not in valid_resolutions
    ]
    return (
        {
            "source": (
                "CURRENT_COMPLETE_ZKT_SNAPSHOT"
                if zkt.snapshot_complete
                else "PARTIAL_ZKT_SNAPSHOT"
            ),
            "total_users": len(active),
            "with_cnic": with_cnic,
            "missing_cnic": len(active) - with_cnic,
            "duplicate_groups": len(duplicate_groups),
            "duplicate_users": sum(len(group) for group in duplicate_groups),
            "resolved_duplicate_groups": len(duplicate_groups) - len(unresolved_groups),
            "unresolved_duplicate_groups": len(unresolved_groups),
            "unresolved_duplicate_users": sum(len(group) for group in unresolved_groups),
        },
        conflict_members,
        resolutions_by_user,
    )


def serialize_user(
    row: DeviceUser,
    *,
    zkt: ZKTDevice | None = None,
    identity_conflict_members: list[dict[str, str]] | None = None,
    identity_resolution: IdentityConflictResolution | None = None,
) -> dict:
    cnic = decrypt_cnic(row.cnic_encrypted)
    machine_name = decrypt_text(row.machine_name_encrypted) or ""
    if cnic and machine_name:
        machine_name = machine_name.replace(cnic, mask_cnic(cnic) or "[CNIC]")
    return {
        "id": row.id,
        "user_key": row.user_key,
        "uid": row.uid,
        "user_id": row.user_id,
        "display_name": row.display_name,
        "cnic_masked": mask_cnic(cnic),
        "cnic_available": bool(cnic),
        "identity_complete": bool(
            cnic
            and row.display_name
            and (row.identity_conflict_code is None or identity_resolution is not None)
        ),
        "identity_conflict_code": row.identity_conflict_code,
        "identity_conflict_members": identity_conflict_members or [],
        "identity_conflict_resolved": identity_resolution is not None,
        "identity_resolution_id": (
            identity_resolution.resolution_id if identity_resolution is not None else None
        ),
        "shift_worker": row.shift_worker,
        "privilege": row.privilege,
        "present": row.present,
        "lifecycle_state": row.lifecycle_state,
        "row_version": row.row_version,
        "observed_at": row.observed_at,
        "machine_name_preview": machine_name or None,
        "current_command_state": "PENDING" if row.current_command_id else None,
        "read_only": bool(zkt and zkt.certification_state != "CERTIFIED"),
    }


def serialize_identity_resolution(row: IdentityConflictResolution) -> dict:
    return {
        "resolution_id": row.resolution_id,
        "resolution_type": row.resolution_type,
        "classification": row.classification,
        "status": row.status,
        "reason": row.reason,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "revoked_by": row.revoked_by,
        "revoked_at": row.revoked_at,
    }


def serialize_attendance(row: AttendanceEvent) -> dict:
    return {
        "id": row.id,
        "event_uid": row.event_uid,
        "device_serial": row.device_serial,
        "uid": row.uid,
        "user_id": row.user_id,
        "display_name": row.display_name,
        "cnic_masked": mask_cnic(decrypt_cnic(row.cnic_encrypted)),
        "device_event_time": row.device_event_time,
        "captured_at": row.captured_at,
        "received_at": row.received_at,
        "source": row.source,
        "status": row.status,
        "punch": row.punch,
        "clock_quality": row.clock_quality,
        "clock_drift_seconds": row.clock_drift_seconds,
        "ords_status": row.ords_status,
        "oracle_confirmed_at": row.oracle_confirmed_at,
        "oracle_confirmation_path": row.oracle_confirmation_path,
        "identity_resolution_id": row.identity_resolution_id,
    }


def serialize_log(row: DeviceLog) -> dict:
    return {
        "id": row.id,
        "boot_id": row.boot_id,
        "sequence": row.sequence,
        "level": row.level,
        "subsystem": row.subsystem,
        "code": row.code,
        "message": row.message,
        "context": row.context,
        "device_time": row.device_time,
        "received_at": row.received_at,
    }


def serialize_alert(row: DeviceAlert) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "severity": row.severity,
        "state": row.state,
        "message": row.message,
        "details": row.details,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
    }


def serialize_lease(row: TemporaryAdminLease | None) -> dict | None:
    if row is None:
        return None
    return {
        "lease_id": row.lease_id,
        "state": row.state,
        "requested_at": row.requested_at,
        "granted_at": row.granted_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "last_error": row.last_error,
    }

# Firmware OTA routes are intentionally additive. Legacy connectors never call
# these endpoints and remain supported by the existing device protocol.
from pathlib import Path as _Path  # noqa: E402
from typing import Literal as _Literal  # noqa: E402

from fastapi.responses import StreamingResponse as _StreamingResponse  # noqa: E402
from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402
from sqlalchemy import select as _select  # noqa: E402

from zk_add.audit import append_audit as _append_audit  # noqa: E402
from zk_add.ota import (  # noqa: E402
    FirmwareCampaign as _FirmwareCampaign,
    FirmwareDeployment as _FirmwareDeployment,
    FirmwareRelease as _FirmwareRelease,
    assignment_for_connector as _assignment_for_connector,
    campaign_detail as _campaign_detail,
    campaign_page as _campaign_page,
    create_campaign as _create_firmware_campaign,
    parse_single_range as _parse_single_range,
    preview_campaign_scope as _preview_firmware_campaign_scope,
    record_progress as _record_firmware_progress,
    release_page as _release_page,
    resolve_download as _resolve_firmware_download,
    version_at_least as _firmware_version_at_least,
)
from zk_add.time_utils import utc_now as _ota_utc_now  # noqa: E402


class _FirmwareCapabilityIn(_BaseModel):
    capable: bool
    secure_boot: bool
    rollback_enabled: bool
    partition_layout: str = _Field(max_length=80)
    running_version: str = _Field(max_length=80)
    running_partition: str = _Field(max_length=40)
    image_sha256: str | None = _Field(default=None, pattern=r"^[0-9a-f]{64}$")
    signing_key_id: str | None = _Field(default=None, max_length=80)


class _FirmwareProgressIn(_BaseModel):
    state: _Literal[
        "OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING",
        "RECONCILING", "SUCCEEDED", "FAILED", "ROLLED_BACK", "CANCELLED",
        "SUPERSEDED", "RELEASE_REVOKED",
    ]
    bytes_written: int = _Field(default=0, ge=0)
    running_version: str | None = _Field(default=None, max_length=80)
    running_partition: str | None = _Field(default=None, max_length=40)
    image_sha256: str | None = _Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = _Field(default=None, max_length=120)
    error_message: str | None = _Field(default=None, max_length=500)


class _FirmwareCampaignIn(_BaseModel):
    release_id: str = _Field(min_length=1, max_length=100)
    zone_id: str = _Field(min_length=1, max_length=100)
    reason: str = _Field(min_length=10, max_length=500)
    typed_confirmation: str = _Field(min_length=1, max_length=80)
    password: str = _Field(min_length=1, max_length=512)
    scope_token: str = _Field(min_length=32, max_length=4096)
    idempotency_key: str = _Field(min_length=8, max_length=120)


class _FirmwarePreflightIn(_BaseModel):
    release_id: str = _Field(min_length=1, max_length=100)
    zone_id: str = _Field(min_length=1, max_length=100)


class _FirmwareControlIn(_BaseModel):
    # FirmwareCampaign.pause_reason is deliberately bounded to 200 characters.
    # Validate at the API boundary so an overlong audit reason is a clear 422,
    # not a database error after the operator has already stepped up.
    reason: str = _Field(min_length=10, max_length=200)
    password: str = _Field(min_length=1, max_length=512)


async def _require_ota_connector(
    request: Request,
    authorization: str | None = Header(default=None),
    connector_id: str | None = Header(default=None, alias="X-ADD-Connector-Id"),
    timestamp: str | None = Header(default=None, alias="X-ADD-Timestamp"),
    nonce: str | None = Header(default=None, alias="X-ADD-Nonce"),
    body_hash: str | None = Header(default=None, alias="X-ADD-Body-SHA256"),
    signature: str | None = Header(default=None, alias="X-ADD-Signature"),
    db: Session = Depends(get_db),
) -> tuple[Session, Connector]:
    connector = await authenticate_connector_request(
        request,
        db,
        authorization=authorization,
        connector_id=connector_id,
        timestamp=timestamp,
        nonce=nonce,
        supplied_body_hash=body_hash,
        signature=signature,
    )
    return db, connector


@app.post("/device/v2/firmware/capability")
async def report_firmware_capability(
    body: _FirmwareCapabilityIn,
    auth: tuple[Session, Connector] = Depends(_require_ota_connector),
):
    db, connector = auth
    eligible = bool(
        body.capable
        and body.secure_boot
        and body.rollback_enabled
        and body.partition_layout == "zone-lite-ota-v1"
        and _firmware_version_at_least(body.running_version, "2.2.0")
    )
    connector.ota_capable = eligible
    connector.ota_secure_boot = body.secure_boot
    connector.ota_rollback_enabled = body.rollback_enabled
    connector.ota_partition_layout = body.partition_layout
    connector.ota_running_partition = body.running_partition
    connector.ota_image_sha256 = body.image_sha256
    connector.ota_signing_key_id = body.signing_key_id or "fleet-key-0"
    connector.ota_state = "OTA_READY" if eligible else "OTA_BLOCKED"
    connector.firmware_version = body.running_version
    db.commit()
    return {"accepted": eligible, "ota_state": connector.ota_state}


@app.get("/device/v2/firmware/assignment")
async def firmware_assignment(
    auth: tuple[Session, Connector] = Depends(_require_ota_connector),
):
    db, connector = auth
    assignment = _assignment_for_connector(
        db,
        connector=connector,
        public_base=settings.firmware_public_base_url,
    )
    db.commit()
    if assignment is None:
        return Response(status_code=204)
    return assignment


@app.post("/device/v2/firmware/deployments/{deployment_id}/progress")
async def firmware_progress(
    deployment_id: str,
    body: _FirmwareProgressIn,
    auth: tuple[Session, Connector] = Depends(_require_ota_connector),
):
    db, connector = auth
    try:
        deployment = _record_firmware_progress(
            db,
            connector=connector,
            deployment_public_id=deployment_id,
            state=body.state,
            bytes_written=body.bytes_written,
            running_version=body.running_version,
            running_partition=body.running_partition,
            image_sha256=body.image_sha256,
            error_code=body.error_code,
            error_message=body.error_message,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    db.commit()
    return {"deployment_id": deployment.deployment_id, "state": deployment.status, "confirm": body.state == "BOOTED_PENDING"}


def _firmware_chunks(path: _Path, start: int, end: int):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.head("/device/v2/firmware/download/{token}")
@app.get("/device/v2/firmware/download/{token}")
def download_firmware(token: str, request: Request, db: Session = Depends(get_db)):
    try:
        release, image = _resolve_firmware_download(db, token)
        requested = _parse_single_range(request.headers.get("range"), release.image_size)
    except ValueError as error:
        raise HTTPException(status_code=416 if "range" in str(error).lower() else 404, detail=str(error)) from error
    db.commit()
    start, end = requested or (0, release.image_size - 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-transform, immutable",
        "ETag": f'"{release.image_sha256}"',
        "Content-Length": str(end - start + 1),
        "Content-Disposition": f'attachment; filename="zone-lite-{release.version}.bin"',
    }
    status_code = 206 if requested else 200
    if requested:
        headers["Content-Range"] = f"bytes {start}-{end}/{release.image_size}"
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type="application/octet-stream")
    return _StreamingResponse(
        _firmware_chunks(image, start, end), status_code=status_code,
        headers=headers, media_type="application/octet-stream"
    )


@app.get("/api/v1/firmware/releases")
def list_firmware_releases(
    q: str | None = Query(default=None, max_length=120),
    state: str | None = Query(default=None, max_length=30),
    cursor: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _ = auth
    result = _release_page(db, query=q, state=state, cursor=cursor, limit=limit)
    return {
        **result,
        "enabled": settings.firmware_ota_enabled,
        "hil_enabled": settings.firmware_hil_enabled,
    }


@app.get("/api/v1/firmware/campaigns")
def list_firmware_campaigns(
    view: _Literal["full", "summary"] = "full",
    q: str | None = Query(default=None, max_length=120),
    status: str | None = Query(default=None, max_length=30),
    zone_id: str | None = Query(default=None, max_length=100),
    release_id: str | None = Query(default=None, max_length=100),
    cursor: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=200),
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _ = auth
    result = _campaign_page(
        db,
        query=q,
        status=status,
        zone_id=zone_id,
        release_id=release_id,
        cursor=cursor,
        limit=limit,
        include_deployments=view == "full",
    )
    return {
        **result,
        "enabled": settings.firmware_ota_enabled,
        "hil_enabled": settings.firmware_hil_enabled,
    }


@app.get("/api/v1/firmware/campaigns/{campaign_id}")
def get_firmware_campaign(
    campaign_id: str,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _ = auth
    row = _campaign_detail(db, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Firmware campaign not found.")
    return row


@app.post("/api/v1/firmware/campaigns/preflight")
def preflight_firmware_campaign(
    body: _FirmwarePreflightIn,
    auth: tuple[Session, AdminContext] = Depends(require_admin),
):
    db, _context = auth
    try:
        return _preview_firmware_campaign_scope(
            db,
            release_public_id=body.release_id,
            zone_id=body.zone_id,
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/v1/firmware/campaigns", status_code=201)
async def start_firmware_campaign(
    body: _FirmwareCampaignIn,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    if not (settings.firmware_ota_enabled or settings.firmware_hil_enabled):
        raise HTTPException(status_code=409, detail="Firmware OTA remains disabled until pilot acceptance.")
    require_step_up(body.password, db, context)
    try:
        campaign = _create_firmware_campaign(
            db, release_public_id=body.release_id, zone_id=body.zone_id, reason=body.reason,
            typed_confirmation=body.typed_confirmation, actor=context.username,
            scope_token=body.scope_token, idempotency_key=body.idempotency_key,
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _append_audit(db, actor=context.username, action="FIRMWARE_CAMPAIGN_CREATED", target_type="zone",
                  target_id=body.zone_id, outcome="ACTIVE", after={"campaign_id": campaign.campaign_id, "release_id": body.release_id})
    db.commit()
    await browser_events.publish(
        "firmware",
        {"campaign_id": campaign.campaign_id, "status": campaign.status, "zone_id": body.zone_id},
    )
    return {"campaign_id": campaign.campaign_id, "status": campaign.status,
            "eligible": campaign.eligible_count, "legacy_skipped": campaign.legacy_skipped_count}


@app.post("/api/v1/firmware/campaigns/{campaign_id}/{action}")
async def control_firmware_campaign(
    campaign_id: str,
    action: _Literal["pause", "resume", "cancel"],
    body: _FirmwareControlIn,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    campaign = db.scalar(_select(_FirmwareCampaign).where(_FirmwareCampaign.campaign_id == campaign_id))
    if campaign is None:
        raise HTTPException(status_code=404, detail="Firmware campaign not found.")
    if action == "resume":
        release = db.get(_FirmwareRelease, campaign.release_id)
        if release is None or release.state not in {"AVAILABLE", "HIL_ONLY"}:
            raise HTTPException(
                status_code=409,
                detail="This campaign cannot resume because its signed release is unavailable.",
            )
    campaign.status = {"pause": "PAUSED", "resume": "ACTIVE", "cancel": "CANCELLED"}[action]
    campaign.pause_reason = body.reason if action != "resume" else None
    if action == "cancel":
        for deployment in db.scalars(_select(_FirmwareDeployment).where(
            _FirmwareDeployment.campaign_id == campaign.id,
            _FirmwareDeployment.status.in_(["PENDING", "OFFERED", "DOWNLOADING"]))):
            deployment.status = "CANCELLED"
    _append_audit(db, actor=context.username, action=f"FIRMWARE_CAMPAIGN_{action.upper()}",
                  target_type="firmware_campaign", target_id=campaign_id,
                  outcome=campaign.status, after={"reason": body.reason})
    db.commit()
    await browser_events.publish(
        "firmware", {"campaign_id": campaign_id, "status": campaign.status}
    )
    return {"campaign_id": campaign_id, "status": campaign.status}


@app.post("/api/v1/firmware/releases/{release_id}/revoke")
async def revoke_firmware_release(
    release_id: str,
    body: _FirmwareControlIn,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    require_step_up(body.password, db, context)
    release = db.scalar(_select(_FirmwareRelease).where(_FirmwareRelease.release_id == release_id))
    if release is None:
        raise HTTPException(status_code=404, detail="Firmware release not found.")
    release.state = "REVOKED"
    release.revoked_at = _ota_utc_now()
    release.revoked_by = context.username
    for campaign in db.scalars(_select(_FirmwareCampaign).where(
        _FirmwareCampaign.release_id == release.id,
        _FirmwareCampaign.status.in_(["ACTIVE", "PAUSED"]))):
        campaign.status = "PAUSED"
        campaign.pause_reason = f"Release revoked: {body.reason}"
    _append_audit(db, actor=context.username, action="FIRMWARE_RELEASE_REVOKED",
                  target_type="firmware_release", target_id=release_id,
                  outcome="REVOKED", after={"reason": body.reason})
    db.commit()
    await browser_events.publish(
        "firmware", {"release_id": release_id, "state": release.state}
    )
    return {"release_id": release_id, "state": release.state}


# Physical provisioning is isolated in an additive router so the existing
# device, attendance and OTA paths remain unchanged when the feature is dark.
from zk_add.provisioning_api import router as provisioning_router  # noqa: E402
from zk_add.attendance_repair_api import router as attendance_repair_router  # noqa: E402

app.router.routes.extend(provisioning_router.routes)
app.router.routes.extend(attendance_repair_router.routes)
