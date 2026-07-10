from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import (
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
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from zk_add import APP_VERSION
from zk_add.audit import append_audit
from zk_add.crypto import cnic_lookup, decrypt_cnic, mask_cnic
from zk_add.db import SessionLocal, init_db, session_scope
from zk_add.identity import build_machine_name
from zk_add.models import (
    AdminSession,
    AttendanceEvent,
    Connector,
    DeviceAlert,
    DeviceCommand,
    DeviceConnectionEvent,
    DeviceLog,
    DeviceUser,
    TemporaryAdminLease,
    ZKTDevice,
)
from zk_add.realtime import browser_events, connector_hub, sse_encode
from zk_add.schemas import (
    AdminLeaseRequest,
    AlertAcknowledgeRequest,
    AttendanceBatchRequest,
    CommandUpdate,
    ConnectorActivateRequest,
    ConnectorCreateRequest,
    Envelope,
    HeartbeatPayload,
    LoginRequest,
    DeviceLogIn,
    LogBatchRequest,
    RestartRequest,
    UserSnapshotRequest,
    UserUpdateRequest,
)
from zk_add.security import (
    ADMIN_COOKIE,
    AdminContext,
    admin_context,
    authenticate_connector_request,
    authenticate_websocket_token,
    create_admin_session,
    login_rate_limiter,
    require_csrf,
    require_step_up,
    verify_admin_password,
)
from zk_add.service import (
    apply_command_update,
    create_admin_lease,
    create_command,
    create_connector,
    fleet_counts,
    ingest_attendance,
    ingest_logs,
    replace_user_snapshot,
    seed_bootstrap_connector,
    serialize_command,
    serialize_connector,
    update_heartbeat,
)
from zk_add.settings import settings
from zk_add.worker import maintenance_loop
from zk_common.time_utils import utc_now


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.require_production_secrets()
    if settings.auto_create_schema:
        init_db()
    with session_scope() as session:
        seed_bootstrap_connector(session)
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
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-Id"],
)


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
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc
    return {"ok": True, "database": True, "server_utc": utc_now()}


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
    return fleet_counts(db)


@app.post("/api/v1/connectors")
def create_connector_route(
    request: Request,
    body: ConnectorCreateRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    try:
        connector, activation_code = create_connector(
            db,
            hardware_id=body.hardware_id,
            zone_id=body.zone_id,
            zone_name=body.zone_name,
            device_id=body.device_id,
            display_name=body.display_name,
            expected_serial=body.expected_serial,
            actor=context.username,
            ip_address=client_ip(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"connector": serialize_connector(connector), "activation_code": activation_code}


@app.post("/device/v1/activate")
def activate(body: ConnectorActivateRequest, db: Session = Depends(get_db)):
    from zk_add.service import activate_connector

    try:
        connector, token = activate_connector(
            db,
            connector_id=body.connector_id,
            hardware_id=body.hardware_id,
            activation_code=body.activation_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"ok": True, "connector_id": connector.connector_id, "device_token": token}


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
            DeviceCommand.status.in_(["QUEUED", "DISPATCHED", "ACKNOWLEDGED", "RUNNING"]),
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
        "active_command": serialize_command(active_command) if active_command else None,
        "active_lease": serialize_lease(active_lease) if active_lease else None,
    }


@app.post("/api/v1/devices/{connector_id}/certify")
def certify_device(
    request: Request,
    connector_id: str,
    capabilities: dict,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    connector = connector_or_404(db, connector_id)
    if connector.zkt_device is None:
        raise HTTPException(status_code=409, detail="No ZKT device has been observed.")
    zkt = connector.zkt_device
    if zkt.expected_serial and zkt.serial != zkt.expected_serial:
        raise HTTPException(status_code=409, detail="ZKT serial assignment has not been verified.")
    observed_record_size = int((zkt.capability_profile or {}).get("observed_user_record_bytes", 0))
    requests_writes = any(
        bool(capabilities.get(name))
        for name in ("user_write", "admin_lease", "protocol_restart")
    )
    if requests_writes and observed_record_size != 72:
        raise HTTPException(
            status_code=409,
            detail="Write capabilities require an observed 72-byte ZKT user record.",
        )
    allowed = {
        "read_users",
        "read_attendance",
        "user_write",
        "admin_lease",
        "protocol_restart",
        "telnet_recovery",
        "name_bytes",
    }
    capabilities = {key: value for key, value in capabilities.items() if key in allowed}
    capabilities["observed_user_record_bytes"] = observed_record_size
    before = connector.zkt_device.capability_profile
    connector.zkt_device.capability_profile = capabilities
    connector.zkt_device.certification_state = "CERTIFIED"
    append_audit(
        db,
        actor=context.username,
        action="DEVICE_CERTIFIED",
        target_type="zkt_device",
        target_id=str(connector.zkt_device.id),
        outcome="SUCCESS",
        ip_address=client_ip(request),
        before=before,
        after=capabilities,
    )
    return serialize_connector(connector)


@app.get("/api/v1/users")
def list_users(
    device_id: str,
    q: str | None = None,
    cnic: str | None = None,
    privilege: int | None = None,
    present: bool = True,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: int | None = None,
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
        try:
            lookup = cnic_lookup(cnic)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        statement = statement.where(DeviceUser.cnic_lookup_hash == lookup)
    if privilege is not None:
        statement = statement.where(DeviceUser.privilege == privilege)
    rows = db.scalars(statement.order_by(DeviceUser.id.asc()).limit(limit + 1)).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return {"rows": [serialize_user(row) for row in rows[:limit]], "next_cursor": next_cursor}


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
        return command_response(command)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/v1/devices/{connector_id}/users/{uid}", status_code=202)
async def update_user(
    connector_id: str,
    uid: str,
    body: UserUpdateRequest,
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
):
    db, context = auth
    connector = connector_or_404(db, connector_id)
    zkt = connector.zkt_device
    if zkt is None:
        raise HTTPException(status_code=409, detail="No assigned ZKT device.")
    if not zkt.capability_profile.get("user_write", False):
        raise HTTPException(status_code=409, detail="This ZKT profile is read-only until certified.")
    user = db.scalar(select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id, DeviceUser.uid == uid))
    if user is None or not user.present:
        raise HTTPException(status_code=404, detail="Device user not found.")
    if user.row_version != body.expected_version:
        raise HTTPException(status_code=409, detail="User changed since it was loaded. Refresh and retry.")
    desired: dict = {}
    if body.display_name is not None:
        name_limit = int(zkt.capability_profile.get("name_bytes", 24))
        try:
            desired["name"] = build_machine_name(
                display_name=body.display_name,
                current_raw_name=user.raw_name,
                byte_limit=name_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.privilege is not None:
        desired["privilege"] = body.privilege
    if not desired:
        raise HTTPException(status_code=422, detail="No user changes supplied.")
    command = create_command(
        db,
        connector=connector,
        command_type="UPDATE_USER",
        payload={"uid": user.uid, **desired},
        expected_state={"uid": user.uid, "user_id": user.user_id, "row_version": user.row_version},
        desired_state=desired,
        idempotency_key=body.idempotency_key,
        actor=context.username,
    )
    db.commit()
    await dispatch_command(connector, command)
    return command_response(command)


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
        payload={"lease_id": lease.lease_id, "uid": user.uid},
        expected_state={"uid": user.uid},
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
    statement = select(AttendanceEvent)
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
        statement = statement.where(AttendanceEvent.cnic_lookup_hash == cnic_lookup(cnic))
    if punch:
        statement = statement.where(AttendanceEvent.punch == punch)
    if source:
        statement = statement.where(AttendanceEvent.source == source)
    if clock_quality:
        statement = statement.where(AttendanceEvent.clock_quality == clock_quality)
    if from_time:
        from zk_common.time_utils import parse_datetime

        statement = statement.where(AttendanceEvent.device_event_time >= parse_datetime(from_time))
    if to_time:
        from zk_common.time_utils import parse_datetime

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
def command(command_id: str, auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    row = db.scalar(select(DeviceCommand).where(DeviceCommand.command_id == command_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Command not found.")
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
    body: AttendanceBatchRequest,
    auth: tuple[Session, Connector] = Depends(require_connector),
):
    db, connector = auth
    digest = hashlib.sha256(
        json.dumps([row.model_dump(mode="json") for row in body.events], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    if body.payload_digest and not secrets.compare_digest(digest, body.payload_digest):
        raise HTTPException(status_code=400, detail="Attendance batch digest mismatch.")
    accepted, duplicates = ingest_attendance(db, connector=connector, events=body.events)
    db.commit()
    for event_uid in accepted:
        await browser_events.publish("attendance", {"connector_id": connector.connector_id, "event_uid": event_uid})
    return {
        "ok": True,
        "batch_id": body.batch_id,
        "payload_digest": digest,
        "accepted_event_uids": accepted,
        "duplicate_event_uids": duplicates,
        "rejected": [],
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
            DeviceCommand.status.in_(["QUEUED", "DISPATCHED"]),
            or_(DeviceCommand.expires_at == None, DeviceCommand.expires_at > utc_now()),  # noqa: E711
        ).order_by(DeviceCommand.created_at.asc()).limit(10)
    ).all()
    for row in rows:
        if row.status == "QUEUED":
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
async def device_config(auth: tuple[Session, Connector] = Depends(require_connector)):
    _db, connector = auth
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
    }


@app.websocket("/device/v1/stream")
async def device_stream(websocket: WebSocket):
    connector_id = websocket.query_params.get("connector_id") or websocket.headers.get("X-ADD-Connector-Id")
    authorization = websocket.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        token = websocket.query_params.get("token", "")
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
            await handle_envelope(connector_pk, envelope, websocket)
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


async def handle_envelope(connector_pk: int, envelope: Envelope, websocket: WebSocket) -> None:
    event_payload = None
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
            event_payload = {"connector_id": connector.connector_id, "count": count}
        elif envelope.type == "attendance_batch":
            batch = AttendanceBatchRequest.model_validate(envelope.payload)
            accepted, duplicates = ingest_attendance(db, connector=connector, events=batch.events)
            event_payload = {
                "connector_id": connector.connector_id,
                "accepted": len(accepted),
                "duplicates": len(duplicates),
            }
        else:
            event_payload = {"connector_id": connector.connector_id, "type": envelope.type}
    await websocket.send_json({"type": "ack", "message_id": envelope.message_id, "seq": envelope.seq})
    await browser_events.publish(envelope.type, event_payload or {})


async def send_pending_commands(connector_id: str) -> None:
    with session_scope() as db:
        connector = connector_or_404(db, connector_id)
        rows = db.scalars(
            select(DeviceCommand).where(
                DeviceCommand.connector_id == connector.id,
                DeviceCommand.status.in_(["QUEUED", "DISPATCHED"]),
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
            if row and row.status == "QUEUED":
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


def serialize_user(row: DeviceUser) -> dict:
    cnic = decrypt_cnic(row.cnic_encrypted)
    return {
        "id": row.id,
        "uid": row.uid,
        "user_id": row.user_id,
        "raw_name": row.raw_name,
        "display_name": row.display_name,
        "cnic_masked": mask_cnic(cnic),
        "cnic_available": bool(cnic),
        "shift_worker": row.shift_worker,
        "privilege": row.privilege,
        "present": row.present,
        "row_version": row.row_version,
        "observed_at": row.observed_at,
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
