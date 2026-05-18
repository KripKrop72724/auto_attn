from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Body, FastAPI, Form, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from zk_common.enums import ClockStatus
from zk_common.time_utils import utc_now
from zk_zone_agent.bruteforce import BruteForceStart, comm_key_bruteforce_manager
from zk_zone_agent.config import config_manager
from zk_zone_agent.db import (
    AttendanceEvent,
    ClockCheck,
    CommKeyBruteforceJob,
    Device,
    DeviceDiscoveryResult,
    DiscoveryScanRun,
    FraudIncident,
    OutagePeriod,
    ServiceEvent,
    SyncQueue,
    init_db,
    session_scope,
)
from zk_zone_agent.device_registry import device_registry
from zk_zone_agent.device_validation import validate_device_connection
from zk_zone_agent.discovery import discovery_service
from zk_zone_agent.head_office_policy import normalize_head_office_url
from zk_zone_agent.local_security import (
    SESSION_COOKIE,
    admin_has_recovery_password,
    admin_exists,
    create_admin,
    login_rate_limiter,
    make_session,
    manual_action_rate_limiter,
    parse_session,
    record_security_event,
    set_recovery_password,
    setup_rate_limiter,
    valid_csrf,
    verify_admin_password,
)
from zk_zone_agent.network_scanner import network_scanner
from zk_zone_agent.settings import settings
from zk_zone_agent.supervisor import zone_supervisor
from zk_zone_agent.sync import HeadOfficeClient, sync_queue_writer
from zk_zone_agent.trusted_time import trusted_time_service
from zk_zone_agent.webauthn_security import (
    expected_webauthn_origin,
    webauthn_admin_security,
    webauthn_origin_for_request,
    webauthn_origin_is_canonical,
)


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class BruteForceStartBody(BaseModel):
    mode: str = Field("SAFE_FAST", pattern="^(SAFE_FAST|AGGRESSIVE|CUSTOM)$")
    range_start: int = Field(0, ge=0)
    range_end: int = Field(999999, ge=0)
    worker_count: int | None = Field(None, ge=1)
    timeout_seconds: float | None = Field(None, gt=0)
    common_keys: list[int] | None = None
    allow_configured: bool = False


class DiscoveryRescanBody(BaseModel):
    subnets: list[str] | None = None


class WebAuthnRegistrationOptionsBody(BaseModel):
    label: str | None = None


class WebAuthnRegistrationVerifyBody(BaseModel):
    challenge_id: str
    credential: dict
    label: str | None = None
    recovery_password: str | None = None
    recovery_password_confirm: str | None = None
    next: str = "/dashboard"


class WebAuthnAuthenticationVerifyBody(BaseModel):
    challenge_id: str
    credential: dict
    next: str = "/dashboard"


class LocalWebSocketHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.clients.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        for websocket in list(self.clients):
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(websocket)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    zone_supervisor.start()
    try:
        yield
    finally:
        zone_supervisor.stop()

ws_hub = LocalWebSocketHub()
app = FastAPI(title="ZK Zone Agent", version="0.1.2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _admin_context(request: Request, session) -> dict:
    admin_session = parse_session(session, request.cookies.get(SESSION_COOKIE))
    webauthn_credential_count = webauthn_admin_security.credential_count(session)
    return {
        "admin_configured": admin_exists(session),
        "admin_authenticated": admin_session is not None,
        "csrf_token": "" if admin_session is None else admin_session.csrf_token,
        "recovery_password_configured": admin_has_recovery_password(session),
        "webauthn_credential_count": webauthn_credential_count,
        "webauthn_enrolled": webauthn_credential_count > 0,
        "webauthn_origin_ok": webauthn_origin_is_canonical(request),
        "webauthn_canonical_url": expected_webauthn_origin(request.url.port, request.url.scheme),
    }


def _require_admin_form(request: Request, session, csrf_token: str | None) -> None:
    if not manual_action_rate_limiter.allow(_client_ip(request)):
        record_security_event(session, "LOCAL_RATE_LIMIT", "Too many local admin actions.")
        raise HTTPException(status_code=429, detail="Too many local admin actions.")
    if not valid_csrf(session, request.cookies.get(SESSION_COOKIE), csrf_token):
        record_security_event(session, "LOCAL_CSRF_REJECTED", "Form action failed CSRF validation.")
        raise HTTPException(status_code=403, detail="Admin unlock and CSRF token are required.")


def _require_admin_api(request: Request, session, csrf_token: str | None) -> None:
    _require_admin_form(request, session, csrf_token)


def _set_admin_cookie(response: RedirectResponse, admin) -> None:
    cookie_value, _session = make_session(admin)
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="strict",
        max_age=8 * 60 * 60,
    )


def _set_admin_cookie_on_json(response: JSONResponse, admin) -> None:
    cookie_value, _session = make_session(admin)
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="strict",
        max_age=8 * 60 * 60,
    )


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    return value


def _with_error(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?error={quote_plus(message)}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None, next: str | None = None):
    with session_scope() as session:
        context = _admin_context(request, session)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={**context, "error": error, "next": _safe_next(next)},
    )


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/dashboard")):
    if not login_rate_limiter.allow(_client_ip(request)):
        return _with_error("/login", "Too many login attempts. Try again shortly.")
    with session_scope() as session:
        admin = verify_admin_password(session, password)
        if admin is None:
            record_security_event(session, "LOCAL_LOGIN_FAILED", "Invalid local admin password.")
            return _with_error("/login", "Invalid admin password.")
        record_security_event(session, "LOCAL_LOGIN_SUCCEEDED", "Local admin unlocked the UI.")
        response = RedirectResponse(_safe_next(next), status_code=303)
        _set_admin_cookie(response, admin)
        return response


@app.post("/api/admin/webauthn/register/options")
def api_webauthn_register_options(
    request: Request,
    body: WebAuthnRegistrationOptionsBody | None = Body(default=None),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    if not setup_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many Windows Hello enrollment attempts.")
    with session_scope() as session:
        if admin_exists(session):
            _require_admin_api(request, session, x_csrf_token)
        try:
            options = webauthn_admin_security.registration_options(session, label=None if body is None else body.label)
        except Exception as exc:
            record_security_event(session, "LOCAL_WEBAUTHN_REGISTER_OPTIONS_FAILED", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return options


@app.post("/api/admin/webauthn/register/verify")
def api_webauthn_register_verify(
    request: Request,
    body: WebAuthnRegistrationVerifyBody,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    if not setup_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many Windows Hello enrollment attempts.")
    recovery_password = (body.recovery_password or "").strip()
    recovery_password_confirm = (body.recovery_password_confirm or "").strip()
    if recovery_password or recovery_password_confirm:
        if recovery_password != recovery_password_confirm:
            raise HTTPException(status_code=400, detail="Recovery password confirmation does not match.")
    else:
        recovery_password = None
    with session_scope() as session:
        existing_admin = admin_exists(session)
        if existing_admin:
            _require_admin_api(request, session, x_csrf_token)
            recovery_password = None
        try:
            admin, credential = webauthn_admin_security.verify_registration(
                session,
                challenge_id=body.challenge_id,
                credential=body.credential,
                expected_origin=webauthn_origin_for_request(request),
                label=body.label,
                recovery_password=recovery_password,
            )
        except Exception as exc:
            record_security_event(session, "LOCAL_WEBAUTHN_REGISTER_FAILED", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_security_event(
            session,
            "LOCAL_WEBAUTHN_REGISTERED",
            f"Windows Hello admin credential enrolled: {credential.label}.",
        )
        if recovery_password:
            record_security_event(
                session,
                "LOCAL_RECOVERY_PASSWORD_SET",
                "Local admin recovery password was configured during Windows Hello enrollment.",
            )
        response = JSONResponse({"ok": True, "redirect": _safe_next(body.next)})
        _set_admin_cookie_on_json(response, admin)
        return response


@app.post("/api/admin/webauthn/login/options")
def api_webauthn_login_options(request: Request):
    if not login_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again shortly.")
    with session_scope() as session:
        try:
            return webauthn_admin_security.authentication_options(session)
        except Exception as exc:
            record_security_event(session, "LOCAL_WEBAUTHN_LOGIN_OPTIONS_FAILED", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/webauthn/login/verify")
def api_webauthn_login_verify(request: Request, body: WebAuthnAuthenticationVerifyBody):
    if not login_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again shortly.")
    with session_scope() as session:
        try:
            admin = webauthn_admin_security.verify_authentication(
                session,
                challenge_id=body.challenge_id,
                credential=body.credential,
                expected_origin=webauthn_origin_for_request(request),
            )
        except Exception as exc:
            record_security_event(session, "LOCAL_WEBAUTHN_LOGIN_FAILED", str(exc))
            raise HTTPException(status_code=400, detail="Windows Hello unlock failed.") from exc
        record_security_event(session, "LOCAL_WEBAUTHN_LOGIN_SUCCEEDED", "Windows Hello unlocked the UI.")
        response = JSONResponse({"ok": True, "redirect": _safe_next(body.next)})
        _set_admin_cookie_on_json(response, admin)
        return response


@app.post("/admin/create")
def create_first_admin(
    request: Request,
    admin_password: str = Form(""),
    admin_password_confirm: str = Form(""),
):
    if not setup_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many setup attempts.")
    if not admin_password:
        return _with_error("/setup", "Recovery password is required for password unlock setup.")
    if admin_password != admin_password_confirm:
        return _with_error("/setup", "Admin password confirmation does not match.")
    with session_scope() as session:
        if admin_exists(session):
            raise HTTPException(status_code=409, detail="Local admin is already configured.")
        try:
            admin = create_admin(session, admin_password)
        except ValueError as exc:
            return _with_error("/setup", str(exc))
        record_security_event(session, "LOCAL_ADMIN_CREATED_UPGRADE", "Local admin was created after upgrade.")
        response = RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
        _set_admin_cookie(response, admin)
        return response


@app.post("/admin/recovery-password")
def update_recovery_password(
    request: Request,
    recovery_password: str = Form(...),
    recovery_password_confirm: str = Form(...),
    csrf_token: str = Form(""),
):
    if recovery_password != recovery_password_confirm:
        return _with_error("/setup", "Recovery password confirmation does not match.")
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        try:
            set_recovery_password(session, recovery_password)
        except ValueError as exc:
            return _with_error("/setup", str(exc))
    return RedirectResponse("/setup", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        record_security_event(session, "LOCAL_LOGOUT", "Local admin locked the UI.")
    response = RedirectResponse("/dashboard", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None):
    with session_scope() as session:
        config = config_manager.get(session)
        context = _admin_context(request, session)
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "config": config,
            "error": error,
            "default_head_office_url": settings.production_head_office_url,
            **context,
        },
    )


@app.post("/setup")
def save_setup(
    request: Request,
    zone_id: str = Form(...),
    zone_name: str = Form(...),
    head_office_url: str = Form(...),
    zone_token: str = Form(...),
    timezone: str = Form("Asia/Karachi"),
    admin_password: str = Form(""),
    admin_password_confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    if not setup_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many setup attempts.")
    login_cookie_value: str | None = None
    try:
        normalized_url = normalize_head_office_url(head_office_url)
    except ValueError as exc:
        return _with_error("/setup", str(exc))

    with session_scope() as session:
        if admin_exists(session):
            _require_admin_form(request, session, csrf_token)
        else:
            if not admin_password:
                return _with_error("/setup", "Enroll Windows Hello admin unlock before storing setup.")
            if admin_password != admin_password_confirm:
                return _with_error("/setup", "Admin password confirmation does not match.")
            try:
                admin = create_admin(session, admin_password)
                login_cookie_value, _admin_session = make_session(admin)
            except ValueError as exc:
                return _with_error("/setup", str(exc))
        record_security_event(session, "ZONE_SETUP_ATTEMPT", f"Setup attempted for {zone_id}.")

    try:
        client = HeadOfficeClient(normalized_url, zone_token.strip(), zone_id.strip())
        server_utc = client.get_time()
    except Exception as exc:
        with session_scope() as session:
            record_security_event(session, "ZONE_SETUP_FAILED", f"Head office token verification failed: {exc}")
        return _with_error("/setup", f"Head office token verification failed: {exc}")

    with session_scope() as session:
        try:
            config_manager.save_setup(
                session,
                zone_id=zone_id.strip(),
                zone_name=zone_name.strip(),
                timezone=timezone,
                head_office_url=normalized_url,
                zone_token=zone_token.strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        trusted_time_service.update_from_head_office(server_utc, session)
        record_security_event(session, "ZONE_SETUP_COMPLETED", f"Setup completed for {zone_id}.")
    zone_supervisor.refresh_device_workers()
    response = RedirectResponse("/dashboard", status_code=303)
    if login_cookie_value:
        response.set_cookie(
            SESSION_COOKIE,
            login_cookie_value,
            httponly=True,
            samesite="strict",
            max_age=8 * 60 * 60,
        )
    return response


@app.post("/setup/reset")
def reset_setup(request: Request, csrf_token: str = Form("")):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        config_manager.clear_setup(session)
        record_security_event(session, "ZONE_SETUP_RESET", "Local zone setup token was cleared.")
    return RedirectResponse("/setup", status_code=303)


@app.get("/api/head-office/health")
def api_head_office_health(request: Request, base_url: str = Query("")):
    if not setup_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many health checks.")
    with session_scope() as session:
        if admin_exists(session) and parse_session(session, request.cookies.get(SESSION_COOKIE)) is None:
            record_security_event(session, "HEAD_OFFICE_HEALTH_DENIED", "Unauthenticated health check denied.")
            raise HTTPException(status_code=403, detail="Admin unlock is required.")
    try:
        normalized_url = normalize_head_office_url(base_url)
        data = HeadOfficeClient(normalized_url).health()
    except Exception as exc:
        with session_scope() as session:
            record_security_event(session, "HEAD_OFFICE_HEALTH_FAILED", str(exc))
        return {"ok": False, "base_url": base_url, "error": str(exc)}
    with session_scope() as session:
        record_security_event(session, "HEAD_OFFICE_HEALTH_OK", f"Health check succeeded for {normalized_url}.")
    return {"ok": True, "base_url": normalized_url, "health": data}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, warning: str | None = None):
    with session_scope() as session:
        config = config_manager.get(session)
        counts = _counts(session)
        devices = session.scalars(select(Device).order_by(Device.label.asc())).all()
        latest_attendance = session.scalars(
            select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(15)
        ).all()
        incidents = session.scalars(select(FraudIncident).order_by(FraudIncident.created_at.desc()).limit(10)).all()
        pending = sync_queue_writer.pending_count(session)
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "config": config,
            "counts": counts,
            "devices": devices,
            "latest_attendance": latest_attendance,
            "incidents": incidents,
            "pending": pending,
            "trusted_time": trusted_time_service.now(),
            "warning": warning,
            **security_context,
        },
    )


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, error: str | None = None, success: str | None = None):
    with session_scope() as session:
        config = config_manager.get(session)
        devices = device_registry.list_devices(session)
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "devices": devices,
            "config": config,
            "error": error,
            "success": success,
            "workers_disabled": settings.disable_workers,
            **security_context,
        },
    )


@app.get("/devices/scan", response_class=HTMLResponse)
def scan_page(request: Request, error: str | None = None, success: str | None = None):
    subnets = [str(item) for item in network_scanner.discover_subnets()]
    with session_scope() as session:
        candidates = session.scalars(
            select(DeviceDiscoveryResult).order_by(
                DeviceDiscoveryResult.status.asc(),
                DeviceDiscoveryResult.last_seen.desc().nullslast(),
                DeviceDiscoveryResult.ip.asc(),
            )
        ).all()
        jobs = session.scalars(
            select(CommKeyBruteforceJob).order_by(CommKeyBruteforceJob.started_at.desc()).limit(25)
        ).all()
        last_scan = session.scalar(select(DiscoveryScanRun).order_by(DiscoveryScanRun.id.desc()).limit(1))
        security_context = _admin_context(request, session)
        is_admin = security_context["admin_authenticated"]
        job_payloads = [comm_key_bruteforce_manager.serialize_job(job, include_secret=is_admin) for job in jobs]
    return templates.TemplateResponse(
        request=request,
        name="scan.html",
        context={
            "subnets": subnets,
            "results": [],
            "candidates": candidates,
            "discovery_state": discovery_service.status(),
            "last_scan": last_scan,
            "bruteforce_enabled": settings.bruteforce_enabled,
            "error": error,
            "success": success,
            "jobs": job_payloads,
            "active_bruteforce_jobs": any(
                job["status"] in {"PENDING", "RUNNING", "PAUSED"} for job in job_payloads
            ),
            "found_keys_by_candidate": {
                job["device_candidate_id"]: job.get("found_key")
                for job in job_payloads
                if job["status"] == "SUCCEEDED" and job.get("found_key")
            },
            **security_context,
        },
    )


@app.post("/devices/scan", response_class=HTMLResponse)
def run_scan(request: Request, subnet: str = Form(""), csrf_token: str = Form("")):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
    subnets = [subnet] if subnet.strip() else None
    discovery_service.run_scan(source="MANUAL", subnets=subnets)
    return RedirectResponse("/devices/scan", status_code=303)


@app.post("/devices")
def save_device(
    request: Request,
    device_id: str = Form(...),
    label: str = Form(...),
    ip: str = Form(...),
    port: int = Form(4370),
    comm_key: str = Form(...),
    return_to: str = Form("/devices"),
    csrf_token: str = Form(""),
):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
    redirect_base = _safe_return_path(return_to)
    try:
        validation = validate_device_connection(ip=ip, port=port, comm_key=comm_key, timeout=5)
    except Exception as exc:
        return RedirectResponse(
            f"{redirect_base}?error={quote_plus(f'Device was not saved: {exc}')}",
            status_code=303,
        )

    serial = validation.info.serial
    platform = validation.info.platform
    device_name = validation.info.device_name
    with session_scope() as session:
        device = device_registry.save_device(
            session,
            device_id=device_id,
            label=label,
            ip=ip,
            port=port,
            comm_key=comm_key,
            serial=serial,
            platform=platform,
            device_name=device_name,
            enabled=True,
        )
        device.online = False
        device.last_error = "Validated. Worker is starting and will connect for live capture."
        device.last_clock_status = "PENDING"
        device.last_drift_seconds = None
        candidate = session.scalar(
            select(DeviceDiscoveryResult).where(
                DeviceDiscoveryResult.ip == ip,
                DeviceDiscoveryResult.port == port,
            )
        )
        if candidate:
            candidate.status = "CONFIGURED"
            candidate.configured_device_id = device_id
            candidate.serial = serial or candidate.serial
            candidate.platform = platform or candidate.platform
            candidate.device_name = device_name or candidate.device_name
            candidate.updated_at = utc_now()
    zone_supervisor.refresh_device_workers()
    if settings.disable_workers:
        message = "Device validated and saved. Workers are disabled in local settings."
    else:
        message = (
            "Device validated and saved. Worker is connecting now for live capture, "
            "clock sync, fraud checks, and local queueing."
        )
    return RedirectResponse(f"/devices?success={quote_plus(message)}", status_code=303)


@app.post("/devices/discovery/{candidate_id}/bruteforce")
def start_bruteforce_from_form(
    request: Request,
    candidate_id: int,
    mode: str = Form("SAFE_FAST"),
    range_start: int = Form(0),
    range_end: int = Form(999999),
    worker_count: int | None = Form(None),
    timeout_seconds: float | None = Form(None),
    common_keys: str = Form(""),
    confirm_bruteforce: str | None = Form(None),
    csrf_token: str = Form(""),
):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
    if confirm_bruteforce != "yes":
        raise HTTPException(status_code=400, detail="Operator confirmation is required.")
    with session_scope() as session:
        candidate = session.get(DeviceDiscoveryResult, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Discovery candidate not found.")
        request = BruteForceStart(
            candidate_id=candidate.id,
            ip=candidate.ip,
            port=candidate.port,
            mode=mode,
            range_start=range_start,
            range_end=range_end,
            worker_count=worker_count,
            timeout_seconds=timeout_seconds,
            common_keys=_parse_common_keys(common_keys),
        )
    try:
        comm_key_bruteforce_manager.start_job(request)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/devices/scan", status_code=303)


@app.post("/devices/{device_id}/toggle")
def toggle_device(request: Request, device_id: str, csrf_token: str = Form("")):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        device = session.scalar(select(Device).where(Device.device_id == device_id))
        if device is None:
            raise HTTPException(status_code=404)
        device.enabled = not device.enabled
    zone_supervisor.refresh_device_workers()
    return RedirectResponse("/devices", status_code=303)


@app.get("/attendance", response_class=HTMLResponse)
def attendance_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(200)).all()
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(request=request, name="attendance.html", context={"rows": rows, **security_context})


@app.get("/api/attendance/recent")
def api_recent_attendance(limit: int = Query(default=200, ge=1, le=500)):
    with session_scope() as session:
        rows = session.scalars(
            select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(limit)
        ).all()
        return {
            "server_time": utc_now().isoformat(),
            "rows": [_serialize_attendance(row) for row in rows],
        }


@app.get("/clock-guard", response_class=HTMLResponse)
def clock_guard_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(ClockCheck).order_by(ClockCheck.created_at.desc()).limit(200)).all()
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(
        request=request, name="clock.html", context={"rows": rows, "ClockStatus": ClockStatus, **security_context}
    )


@app.get("/outages", response_class=HTMLResponse)
def outages_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(OutagePeriod).order_by(OutagePeriod.created_at.desc()).limit(200)).all()
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(request=request, name="outages.html", context={"rows": rows, **security_context})


@app.get("/sync-queue", response_class=HTMLResponse)
def sync_queue_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(SyncQueue).order_by(SyncQueue.created_at.desc()).limit(200)).all()
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(request=request, name="sync_queue.html", context={"rows": rows, **security_context})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(ServiceEvent).order_by(ServiceEvent.created_at.desc()).limit(200)).all()
        security_context = _admin_context(request, session)
    return templates.TemplateResponse(request=request, name="logs.html", context={"rows": rows, **security_context})


@app.get("/api/status")
def api_status():
    with session_scope() as session:
        config = config_manager.get(session)
        devices = session.scalars(select(Device).order_by(Device.label.asc())).all()
        setup_completed = bool(config and config.setup_completed)
        return {
            "setup_completed": setup_completed,
            "registration_status": "REGISTERED" if setup_completed else "LOCAL_CAPTURE_ONLY",
            "capture_active": not settings.disable_workers,
            "sync_ready": bool(config and config.setup_completed and config.zone_token and config.head_office_url),
            "zone": None if not config else {"zone_id": config.zone_id, "zone_name": config.zone_name},
            "trusted_time": trusted_time_service.now().value.isoformat(),
            "trusted_time_source": trusted_time_service.now().source,
            "devices": [
                {
                    "device_id": device.device_id,
                    "label": device.label,
                    "online": device.online,
                    "last_clock_status": device.last_clock_status,
                    "last_drift_seconds": device.last_drift_seconds,
                    "last_error": device.last_error,
                }
                for device in devices
            ],
            "pending_queue_count": sync_queue_writer.pending_count(session),
        }


@app.get("/api/discovery/status")
def api_discovery_status():
    with session_scope() as session:
        candidates = session.scalars(
            select(DeviceDiscoveryResult).order_by(
                DeviceDiscoveryResult.status.asc(),
                DeviceDiscoveryResult.last_seen.desc().nullslast(),
                DeviceDiscoveryResult.ip.asc(),
            )
        ).all()
        last_scan = session.scalar(select(DiscoveryScanRun).order_by(DiscoveryScanRun.id.desc()).limit(1))
        jobs = session.scalars(
            select(CommKeyBruteforceJob).order_by(CommKeyBruteforceJob.started_at.desc()).limit(25)
        ).all()
        state = discovery_service.status()
        return {
            "running": state.running,
            "current_scan_id": state.current_scan_id,
            "last_started_at": state.last_started_at.isoformat() if state.last_started_at else None,
            "last_finished_at": state.last_finished_at.isoformat() if state.last_finished_at else None,
            "last_scan": None if last_scan is None else _serialize_scan_run(last_scan),
            "candidates": [_serialize_candidate(candidate) for candidate in candidates],
            "bruteforce_jobs": [comm_key_bruteforce_manager.serialize_job(job) for job in jobs],
        }


@app.post("/api/discovery/rescan")
def api_discovery_rescan(
    request: Request,
    body: DiscoveryRescanBody | None = Body(default=None),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    state = discovery_service.trigger_scan(source="MANUAL", subnets=None if body is None else body.subnets)
    return {
        "ok": True,
        "running": state.running,
        "current_scan_id": state.current_scan_id,
    }


@app.post("/api/discovery/candidates/{candidate_id}/ignore")
def api_ignore_candidate(
    request: Request,
    candidate_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
        candidate = session.get(DeviceDiscoveryResult, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404)
        candidate.status = "IGNORED"
        candidate.updated_at = utc_now()
    return {"ok": True}


@app.post("/api/discovery/candidates/{candidate_id}/bruteforce/start")
def api_start_candidate_bruteforce(
    request: Request,
    candidate_id: int,
    body: BruteForceStartBody,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    with session_scope() as session:
        candidate = session.get(DeviceDiscoveryResult, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Discovery candidate not found.")
        request = BruteForceStart(
            candidate_id=candidate.id,
            ip=candidate.ip,
            port=candidate.port,
            mode=body.mode,
            range_start=body.range_start,
            range_end=body.range_end,
            worker_count=body.worker_count,
            timeout_seconds=body.timeout_seconds,
            common_keys=body.common_keys,
            allow_configured=body.allow_configured,
        )
    try:
        job = comm_key_bruteforce_manager.start_job(request)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "job": comm_key_bruteforce_manager.serialize_job(job)}


@app.get("/api/bruteforce/jobs")
def api_list_bruteforce_jobs():
    with session_scope() as session:
        jobs = session.scalars(
            select(CommKeyBruteforceJob).order_by(CommKeyBruteforceJob.started_at.desc()).limit(100)
        ).all()
        return {"jobs": [comm_key_bruteforce_manager.serialize_job(job) for job in jobs]}


@app.get("/api/bruteforce/jobs/{job_id}")
def api_get_bruteforce_job(job_id: int):
    with session_scope() as session:
        job = session.get(CommKeyBruteforceJob, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return comm_key_bruteforce_manager.serialize_job(job)


@app.post("/api/bruteforce/jobs/{job_id}/pause")
def api_pause_bruteforce_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    try:
        comm_key_bruteforce_manager.pause(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


@app.post("/api/bruteforce/jobs/{job_id}/resume")
def api_resume_bruteforce_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    try:
        comm_key_bruteforce_manager.resume(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


@app.post("/api/bruteforce/jobs/{job_id}/cancel")
def api_cancel_bruteforce_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    try:
        comm_key_bruteforce_manager.cancel(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


@app.websocket("/ws/local-events")
async def local_events(websocket: WebSocket):
    await ws_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_json({"type": "status", "server_time": utc_now().isoformat()})
    except WebSocketDisconnect:
        ws_hub.disconnect(websocket)


def _counts(session):
    return {
        "devices": session.scalar(select(func.count(Device.id))) or 0,
        "attendance": session.scalar(select(func.count(AttendanceEvent.id))) or 0,
        "clock_checks": session.scalar(select(func.count(ClockCheck.id))) or 0,
        "outages": session.scalar(select(func.count(OutagePeriod.id))) or 0,
        "incidents": session.scalar(select(func.count(FraudIncident.id))) or 0,
        "sync_queue": session.scalar(select(func.count(SyncQueue.id))) or 0,
    }


def _parse_common_keys(value: str) -> list[int] | None:
    keys: list[int] = []
    for item in value.replace("\n", ",").replace(" ", ",").split(","):
        item = item.strip()
        if not item:
            continue
        keys.append(int(item))
    return keys or None


def _safe_return_path(value: str) -> str:
    if value in {"/devices", "/devices/scan"}:
        return value
    return "/devices"


def _serialize_candidate(candidate: DeviceDiscoveryResult) -> dict:
    return {
        "id": candidate.id,
        "ip": candidate.ip,
        "port": candidate.port,
        "subnet": candidate.subnet,
        "interface_name": candidate.interface_name,
        "status": candidate.status,
        "source": candidate.source,
        "first_seen": candidate.first_seen.isoformat(),
        "last_seen": candidate.last_seen.isoformat() if candidate.last_seen else None,
        "last_checked_at": candidate.last_checked_at.isoformat() if candidate.last_checked_at else None,
        "consecutive_failures": candidate.consecutive_failures,
        "last_error": candidate.last_error,
        "serial": candidate.serial,
        "platform": candidate.platform,
        "device_name": candidate.device_name,
        "configured_device_id": candidate.configured_device_id,
    }


def _serialize_attendance(row: AttendanceEvent) -> dict:
    return {
        "event_uid": row.event_uid,
        "device_event_time": row.device_event_time.isoformat() if row.device_event_time else None,
        "zone_trusted_time": row.zone_trusted_time.isoformat() if row.zone_trusted_time else None,
        "user": row.employee_name or row.user_id,
        "device_id": row.device_id,
        "source_type": row.source_type,
        "trust_status": row.trust_status,
        "fraud_score": row.fraud_score,
        "fraud_reason": row.fraud_reason or "",
    }


def _serialize_scan_run(scan_run: DiscoveryScanRun) -> dict:
    return {
        "id": scan_run.id,
        "source": scan_run.source,
        "status": scan_run.status,
        "started_at": scan_run.started_at.isoformat(),
        "ended_at": scan_run.ended_at.isoformat() if scan_run.ended_at else None,
        "target_count": scan_run.target_count,
        "found_count": scan_run.found_count,
        "error_count": scan_run.error_count,
        "message": scan_run.message,
    }
