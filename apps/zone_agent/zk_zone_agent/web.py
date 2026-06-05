from __future__ import annotations

from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from zk_zone_agent import APP_VERSION
from zk_common.enums import ClockStatus
from zk_common.time_utils import parse_datetime, utc_now
from zk_common.ui_time import (
    apply_timeline_date_filter,
    filter_context,
    selected_query_values,
    timeline_date_filter,
    timestamp_view,
)
from zk_zone_agent.bruteforce import BruteForceStart, comm_key_bruteforce_manager
from zk_zone_agent.bulk_user_update import (
    ExportUserRow,
    export_users_xlsx,
    parse_bulk_update_xlsx,
    split_machine_name_cnic,
)
from zk_zone_agent.config import config_manager
from zk_zone_agent.db import (
    AttendanceEvent,
    BulkUserUpdateItem,
    BulkUserUpdateJob,
    ClockCheck,
    CommKeyBruteforceJob,
    Device,
    DeviceDiscoveryResult,
    DeviceUser,
    DiscoveryScanRun,
    FraudIncident,
    OracleAttendanceOutbox,
    OutagePeriod,
    ServiceEvent,
    SyncQueue,
    init_db,
    is_sqlite_lock_error,
    run_session_with_retries,
    session_scope,
)
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.device_registry import device_registry
from zk_zone_agent.device_users import PRIVILEGE_CHOICES, normalize_device_user_update
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
from zk_zone_agent.oracle_sync import oracle_sync_wakeup
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
templates.env.globals["timestamp_view"] = timestamp_view


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
app = FastAPI(title="ZK Zone Agent", version=APP_VERSION, lifespan=lifespan)
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
    config = config_manager.get(session)
    return {
        "admin_configured": admin_exists(session),
        "admin_authenticated": admin_session is not None,
        "csrf_token": "" if admin_session is None else admin_session.csrf_token,
        "recovery_password_configured": admin_has_recovery_password(session),
        "webauthn_credential_count": webauthn_credential_count,
        "webauthn_enrolled": webauthn_credential_count > 0,
        "webauthn_origin_ok": webauthn_origin_is_canonical(request),
        "webauthn_canonical_url": expected_webauthn_origin(request.url.port, request.url.scheme),
        "display_timezone": config.timezone if config else settings.default_timezone,
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


def _require_admin_read_api(request: Request, session, csrf_token: str | None) -> None:
    if not valid_csrf(session, request.cookies.get(SESSION_COOKIE), csrf_token):
        raise HTTPException(status_code=403, detail="Admin unlock and CSRF token are required.")


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


def _display_timezone(session) -> str:
    config = config_manager.get(session)
    return config.timezone if config else settings.default_timezone


def _timeline_filter(request: Request, session):
    return timeline_date_filter(request.query_params, timezone_name=_display_timezone(session))


def _filters_context(date_filter, selected: dict[str, str], choices: dict[str, list[dict[str, str]]] | None = None):
    return {
        "filters": filter_context(date_filter, selected),
        "filter_choices": choices or {},
        "display_timezone": date_filter.display_timezone,
    }


def _apply_selected(statement, selected: dict[str, str], columns: dict[str, object]):
    for key, column in columns.items():
        if value := selected.get(key):
            statement = statement.where(column == value)
    return statement


def _option(value: str, label: str | None = None) -> dict[str, str]:
    return {"value": value, "label": label or value}


def _distinct_options(session, column) -> list[dict[str, str]]:
    values = session.scalars(
        select(column).where(column.is_not(None)).distinct().order_by(column.asc())
    ).all()
    return [_option(str(value)) for value in values if str(value).strip()]


def _device_options(session) -> list[dict[str, str]]:
    rows = session.scalars(select(Device).order_by(Device.label.asc(), Device.device_id.asc())).all()
    return [_option(row.device_id, f"{row.label} ({row.device_id})") for row in rows]


def _with_error(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?error={quote_plus(message)}", status_code=303)


def _users_redirect(
    *,
    device_id: str | None = None,
    uid: str | None = None,
    q: str | None = None,
    success: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    params = {}
    if device_id:
        params["device_id"] = device_id
    if uid:
        params["uid"] = uid
    if q:
        params["q"] = q
    if success:
        params["success"] = success
    if error:
        params["error"] = error
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/users{suffix}", status_code=303)


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
        oracle_summary = _oracle_summary(session)
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "config": config,
            "error": error,
            "default_head_office_url": settings.production_head_office_url,
            "default_ords_base_url": settings.default_ords_base_url,
            "oracle_summary": oracle_summary,
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


@app.post("/setup/oracle")
def save_oracle_setup(
    request: Request,
    ords_base_url: str = Form(...),
    ords_api_username: str = Form(...),
    ords_api_password: str = Form(...),
    oracle_cutover_utc: str = Form(...),
    csrf_token: str = Form(""),
):
    if not ords_base_url.strip().startswith(("https://", "http://")):
        return _with_error("/setup", "Oracle ORDS base URL must start with http:// or https://.")
    try:
        cutover = parse_datetime(oracle_cutover_utc.strip())
    except Exception:
        return _with_error("/setup", "Oracle cutover must be ISO UTC like 2026-06-03T13:59:00Z.")
    if not ords_api_username.strip() or not ords_api_password.strip():
        return _with_error("/setup", "Oracle API username and password are required.")

    def _save(session):
        _require_admin_form(request, session, csrf_token)
        config_manager.save_oracle_attendance(
            session,
            ords_base_url=ords_base_url,
            ords_api_username=ords_api_username,
            ords_api_password=ords_api_password,
            oracle_cutover_utc=cutover,
        )
        record_security_event(session, "ORACLE_SETUP_SAVED", "Oracle attendance sync configuration was saved.")
        return None

    try:
        error_response = run_session_with_retries(_save)
    except Exception as exc:
        if is_sqlite_lock_error(exc):
            error_response = _with_error(
                "/setup",
                "Local database was busy while saving Oracle settings. Please retry in a few seconds.",
            )
        else:
            raise
    if error_response is not None:
        return error_response

    oracle_sync_wakeup.set()
    return RedirectResponse("/setup", status_code=303)


@app.post("/setup/oracle/clear")
def clear_oracle_setup(request: Request, csrf_token: str = Form("")):
    def _clear(session):
        _require_admin_form(request, session, csrf_token)
        config_manager.clear_oracle_attendance(session)
        record_security_event(session, "ORACLE_SETUP_CLEARED", "Oracle attendance sync configuration was cleared.")

    run_session_with_retries(_clear)
    oracle_sync_wakeup.set()
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
    selected = selected_query_values(request.query_params, ["device_id"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        config = config_manager.get(session)
        counts = _counts(session)
        devices = session.scalars(select(Device).order_by(Device.label.asc())).all()
        latest_stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.desc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        latest_stmt = _apply_selected(latest_stmt, selected, {"device_id": AttendanceEvent.device_id})
        latest_attendance = session.scalars(
            latest_stmt.limit(15)
        ).all()
        incidents_stmt = apply_timeline_date_filter(
            select(FraudIncident).order_by(FraudIncident.created_at.desc()),
            FraudIncident.created_at,
            date_filter,
        )
        incidents_stmt = _apply_selected(incidents_stmt, selected, {"device_id": FraudIncident.device_id})
        incidents = session.scalars(incidents_stmt.limit(10)).all()
        pending = sync_queue_writer.pending_count(session)
        security_context = _admin_context(request, session)
        choices = {"devices": _device_options(session)}
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
            **_filters_context(date_filter, selected, choices),
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


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    device_id: str = Query(""),
    uid: str = Query(""),
    q: str = Query(""),
    error: str | None = None,
    success: str | None = None,
):
    q = q.strip()
    with session_scope() as session:
        devices = device_registry.list_devices(session)
        selected_device_id = device_id.strip()
        if selected_device_id and not any(
            device.device_id == selected_device_id for device in devices
        ):
            selected_device_id = ""
        stmt = select(DeviceUser, Device).join(Device, DeviceUser.device_id == Device.device_id)
        if selected_device_id:
            stmt = stmt.where(DeviceUser.device_id == selected_device_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    DeviceUser.uid.ilike(like),
                    DeviceUser.user_id.ilike(like),
                    DeviceUser.employee_name.ilike(like),
                    DeviceUser.privilege.ilike(like),
                )
            )
        rows = session.execute(
            stmt.order_by(Device.label.asc(), DeviceUser.user_id.asc()).limit(500)
        ).all()
        selected_pair = None
        if uid:
            selected_pair = next(
                (
                    (user, device)
                    for user, device in rows
                    if user.device_id == selected_device_id and str(user.uid or "") == uid
                ),
                None,
            )
        if selected_pair is None and rows:
            selected_pair = rows[0]
        selected_user = selected_pair[0] if selected_pair is not None else None
        selected_device = selected_pair[1] if selected_pair is not None else None
        bulk_jobs = []
        security_context = _admin_context(request, session)
        if selected_device_id and security_context["admin_authenticated"]:
            bulk_jobs = session.scalars(
                select(BulkUserUpdateJob)
                .where(BulkUserUpdateJob.device_id == selected_device_id)
                .order_by(BulkUserUpdateJob.created_at.desc())
                .limit(5)
            ).all()
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "devices": devices,
            "rows": rows,
            "selected_device_id": selected_device_id,
            "selected_uid": "" if selected_user is None else str(selected_user.uid or ""),
            "selected_user": selected_user,
            "selected_device": selected_device,
            "q": q,
            "error": error,
            "success": success,
            "privilege_choices": PRIVILEGE_CHOICES,
            "bulk_jobs": bulk_jobs,
            **security_context,
        },
    )


@app.post("/users/{device_id}/refresh")
def refresh_device_users(
    request: Request,
    device_id: str,
    csrf_token: str = Form(""),
):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        device = session.scalar(select(Device).where(Device.device_id == device_id))
        if device is None:
            raise HTTPException(status_code=404)
        record_security_event(
            session,
            "DEVICE_USERS_REFRESH_ATTEMPT",
            f"Refreshing users for {device_id}.",
        )
    try:
        users = zone_supervisor.refresh_device_users(device_id)
    except Exception as exc:
        with session_scope() as session:
            record_security_event(
                session,
                "DEVICE_USERS_REFRESH_FAILED",
                f"Refreshing users for {device_id} failed: {exc}",
            )
        return _users_redirect(device_id=device_id, error=str(exc))
    with session_scope() as session:
        record_security_event(
            session,
            "DEVICE_USERS_REFRESH_SUCCEEDED",
            f"Loaded {len(users)} user(s) from {device_id}.",
        )
        audit_ledger.append(
            session,
            "device_users_refresh",
            device_id,
            {
                "device_id": device_id,
                "user_count": len(users),
                "refreshed_at": utc_now(),
            },
        )
    return _users_redirect(device_id=device_id, success=f"Loaded {len(users)} user(s) from device.")


@app.post("/users/{device_id}/{uid}/update")
def update_device_user(
    request: Request,
    device_id: str,
    uid: str,
    csrf_token: str = Form(""),
    user_id: str = Form(...),
    employee_name: str = Form(...),
    privilege: str = Form(...),
    card: str = Form(""),
):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        device = session.scalar(select(Device).where(Device.device_id == device_id))
        if device is None:
            raise HTTPException(status_code=404)
        current = session.scalar(
            select(DeviceUser).where(DeviceUser.device_id == device_id, DeviceUser.uid == uid)
        )
        if current is None:
            return _users_redirect(
                device_id=device_id,
                error="Refresh this device before editing that user.",
            )
        old_snapshot = {
            "uid": current.uid,
            "user_id": current.user_id,
            "name": current.employee_name,
            "privilege": current.privilege,
            "card": current.card,
        }
        record_security_event(
            session,
            "DEVICE_USER_UPDATE_ATTEMPT",
            f"Updating user UID {uid} on {device_id}.",
        )
    try:
        update = normalize_device_user_update(
            uid=uid,
            user_id=user_id,
            name=employee_name,
            privilege=privilege,
            card=card,
        )
        updated = zone_supervisor.update_device_user(device_id, update)
    except Exception as exc:
        with session_scope() as session:
            record_security_event(
                session,
                "DEVICE_USER_UPDATE_FAILED",
                f"Updating user UID {uid} on {device_id} failed: {exc}",
            )
        return _users_redirect(device_id=device_id, uid=uid, error=str(exc))

    with session_scope() as session:
        record_security_event(
            session,
            "DEVICE_USER_UPDATE_SUCCEEDED",
            (
                f"Updated user UID {uid} on {device_id}: "
                f"{old_snapshot['user_id']} -> {updated.user_id}."
            ),
        )
        audit_ledger.append(
            session,
            "device_user_update",
            f"{device_id}:{uid}",
            {
                "device_id": device_id,
                "uid": uid,
                "old": old_snapshot,
                "new": {
                    "user_id": updated.user_id,
                    "name": updated.name,
                    "privilege": updated.privilege,
                    "card": updated.card,
                },
                "updated_at": utc_now(),
                "history_policy": "attendance_history_preserved",
            },
        )
    return _users_redirect(
        device_id=device_id,
        uid=uid,
        success=f"Updated user {updated.name or updated.user_id} on device.",
    )


@app.get("/users/{device_id}/bulk.xlsx")
def download_bulk_user_update_xlsx(request: Request, device_id: str):
    with session_scope() as session:
        _require_admin_read_api(request, session, request.query_params.get("csrf_token"))
        device = session.scalar(select(Device).where(Device.device_id == device_id))
        if device is None:
            raise HTTPException(status_code=404)
    _record_security_event_best_effort(
        "BULK_USER_UPDATE_DOWNLOAD_ATTEMPT",
        f"Downloading bulk update workbook for {device_id}.",
    )
    try:
        zone_supervisor.refresh_device_users(device_id)
    except Exception as exc:
        return _users_redirect(device_id=device_id, error=f"Could not refresh users before export: {exc}")
    with session_scope() as session:
        users = session.scalars(
            select(DeviceUser)
            .where(DeviceUser.device_id == device_id)
            .order_by(DeviceUser.user_id.asc())
        ).all()
        rows: list[ExportUserRow] = []
        for user in users:
            name, cnic = split_machine_name_cnic(user.employee_name)
            rows.append(ExportUserRow(user_id=user.user_id, name=name, cnic=cnic))
        content = export_users_xlsx(rows)
    _record_bulk_download_success_best_effort(device_id=device_id, user_count=len(rows))
    filename = f"{device_id}-users.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _record_security_event_best_effort(event_type: str, description: str | None = None) -> None:
    try:
        run_session_with_retries(
            lambda session: record_security_event(session, event_type, description),
            attempts=6,
        )
    except Exception:
        pass


def _record_bulk_download_success_best_effort(*, device_id: str, user_count: int) -> None:
    try:
        run_session_with_retries(
            lambda session: _record_bulk_download_success(session, device_id, user_count),
            attempts=6,
        )
    except Exception:
        pass


def _record_bulk_download_success(session, device_id: str, user_count: int) -> None:
    record_security_event(
        session,
        "BULK_USER_UPDATE_DOWNLOAD_SUCCEEDED",
        f"Downloaded bulk update workbook for {device_id} with {user_count} user(s).",
    )
    audit_ledger.append(
        session,
        "bulk_user_update_download",
        device_id,
        {"device_id": device_id, "user_count": user_count, "downloaded_at": utc_now()},
    )


@app.post("/users/{device_id}/bulk-upload")
def upload_bulk_user_update_xlsx(
    request: Request,
    device_id: str,
    csrf_token: str = Form(""),
    file: UploadFile = File(...),
):
    with session_scope() as session:
        _require_admin_form(request, session, csrf_token)
        device = session.scalar(select(Device).where(Device.device_id == device_id))
        if device is None:
            raise HTTPException(status_code=404)
        record_security_event(
            session,
            "BULK_USER_UPDATE_UPLOAD_ATTEMPT",
            f"Uploading bulk update workbook for {device_id}.",
        )
    if not (file.filename or "").lower().endswith(".xlsx"):
        return _users_redirect(device_id=device_id, error="Upload a .xlsx workbook.")
    content = file.file.read()
    try:
        parsed_rows = parse_bulk_update_xlsx(content)
    except Exception as exc:
        return _users_redirect(device_id=device_id, error=str(exc))
    try:
        zone_supervisor.refresh_device_users(device_id)
    except Exception as exc:
        return _users_redirect(device_id=device_id, error=f"Could not refresh users before upload: {exc}")

    with session_scope() as session:
        device_users = {
            user.user_id: user
            for user in session.scalars(select(DeviceUser).where(DeviceUser.device_id == device_id))
        }
        job = BulkUserUpdateJob(
            device_id=device_id,
            status="PENDING",
            uploaded_filename=file.filename,
        )
        session.add(job)
        session.flush()
        for parsed in parsed_rows:
            cached = device_users.get(parsed.user_id)
            status = parsed.status
            message = parsed.message
            expected_name = parsed.expected_name
            if cached is None and parsed.status != "SKIPPED":
                status = "FAILED"
                message = f"ID {parsed.user_id} is not present on the current device snapshot."
                expected_name = None
            item = BulkUserUpdateItem(
                job_id=job.id,
                device_id=device_id,
                uid=None if cached is None else cached.uid,
                user_id=parsed.user_id,
                old_name=None if cached is None else cached.employee_name,
                sheet_name=parsed.sheet_name,
                expected_name=expected_name,
                cnic=parsed.cnic,
                privilege=None if cached is None else cached.privilege,
                card=None if cached is None else cached.card,
                status=status,
                message=message,
            )
            session.add(item)
        session.flush()
        _refresh_bulk_job_counts_for_web(session, job)
        record_security_event(
            session,
            "BULK_USER_UPDATE_UPLOAD_SUCCEEDED",
            f"Created bulk update job {job.id} for {device_id}.",
        )
        audit_ledger.append(
            session,
            "bulk_user_update_job",
            job.id,
            {
                "device_id": device_id,
                "status": job.status,
                "total_count": job.total_count,
                "pending_count": job.pending_count,
                "skipped_count": job.skipped_count,
                "failed_count": job.failed_count,
                "uploaded_at": utc_now(),
            },
        )
        job_id = job.id
    zone_supervisor.start_bulk_user_update_job(job_id)
    return _users_redirect(
        device_id=device_id,
        success=f"Bulk update job {job_id} started. Progress is shown below.",
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
    selected = selected_query_values(request.query_params, ["device_id", "source_type", "trust_status"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.desc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "device_id": AttendanceEvent.device_id,
                "source_type": AttendanceEvent.source_type,
                "trust_status": AttendanceEvent.trust_status,
            },
        )
        rows = session.scalars(stmt.limit(200)).all()
        security_context = _admin_context(request, session)
        choices = {
            "devices": _device_options(session),
            "source_types": _distinct_options(session, AttendanceEvent.source_type),
            "trust_statuses": _distinct_options(session, AttendanceEvent.trust_status),
        }
    return templates.TemplateResponse(
        request=request,
        name="attendance.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **security_context},
    )


@app.get("/api/attendance/recent")
def api_recent_attendance(request: Request, limit: int = Query(default=200, ge=1, le=500)):
    selected = selected_query_values(request.query_params, ["device_id", "source_type", "trust_status"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.desc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "device_id": AttendanceEvent.device_id,
                "source_type": AttendanceEvent.source_type,
                "trust_status": AttendanceEvent.trust_status,
            },
        )
        rows = session.scalars(stmt.limit(limit)).all()
        return {
            "server_time": utc_now().isoformat(),
            "display_timezone": date_filter.display_timezone,
            "rows": [_serialize_attendance(row) for row in rows],
        }


@app.get("/clock-guard", response_class=HTMLResponse)
def clock_guard_page(request: Request):
    selected = selected_query_values(request.query_params, ["device_id", "status"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(ClockCheck).order_by(ClockCheck.trusted_time.desc()),
            ClockCheck.trusted_time,
            date_filter,
        )
        stmt = _apply_selected(stmt, selected, {"device_id": ClockCheck.device_id, "status": ClockCheck.status})
        rows = session.scalars(stmt.limit(200)).all()
        security_context = _admin_context(request, session)
        choices = {
            "devices": _device_options(session),
            "statuses": _distinct_options(session, ClockCheck.status),
        }
    return templates.TemplateResponse(
        request=request,
        name="clock.html",
        context={
            "rows": rows,
            "ClockStatus": ClockStatus,
            **_filters_context(date_filter, selected, choices),
            **security_context,
        },
    )


@app.get("/outages", response_class=HTMLResponse)
def outages_page(request: Request):
    selected = selected_query_values(request.query_params, ["device_id", "outage_type"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(OutagePeriod).order_by(OutagePeriod.start_time.desc()),
            OutagePeriod.start_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {"device_id": OutagePeriod.device_id, "outage_type": OutagePeriod.outage_type},
        )
        rows = session.scalars(stmt.limit(200)).all()
        security_context = _admin_context(request, session)
        choices = {
            "devices": _device_options(session),
            "outage_types": _distinct_options(session, OutagePeriod.outage_type),
        }
    return templates.TemplateResponse(
        request=request,
        name="outages.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **security_context},
    )


@app.get("/sync-queue", response_class=HTMLResponse)
def sync_queue_page(request: Request):
    selected = selected_query_values(request.query_params, ["payload_type", "status"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(SyncQueue).order_by(SyncQueue.created_at.desc()),
            SyncQueue.created_at,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {"payload_type": SyncQueue.payload_type, "status": SyncQueue.status},
        )
        rows = session.scalars(stmt.limit(200)).all()
        security_context = _admin_context(request, session)
        choices = {
            "payload_types": _distinct_options(session, SyncQueue.payload_type),
            "statuses": _distinct_options(session, SyncQueue.status),
        }
    return templates.TemplateResponse(
        request=request,
        name="sync_queue.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **security_context},
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    selected = selected_query_values(request.query_params, ["event_type"])
    with session_scope() as session:
        date_filter = _timeline_filter(request, session)
        stmt = apply_timeline_date_filter(
            select(ServiceEvent).order_by(ServiceEvent.created_at.desc()),
            ServiceEvent.created_at,
            date_filter,
        )
        stmt = _apply_selected(stmt, selected, {"event_type": ServiceEvent.event_type})
        rows = session.scalars(stmt.limit(200)).all()
        security_context = _admin_context(request, session)
        choices = {"event_types": _distinct_options(session, ServiceEvent.event_type)}
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **security_context},
    )


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


@app.get("/api/users/bulk-jobs/{job_id}")
def api_bulk_user_update_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_read_api(request, session, x_csrf_token)
        job = session.get(BulkUserUpdateJob, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        items = session.scalars(
            select(BulkUserUpdateItem)
            .where(BulkUserUpdateItem.job_id == job_id)
            .order_by(BulkUserUpdateItem.id.asc())
        ).all()
        return _serialize_bulk_job(job, items)


@app.post("/api/users/bulk-jobs/{job_id}/resume")
def api_resume_bulk_user_update_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
        job = session.get(BulkUserUpdateJob, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        if job.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"}:
            raise HTTPException(status_code=400, detail="This bulk update job cannot be resumed.")
        job.status = "PENDING"
        job.last_error = None
        job.updated_at = utc_now()
        for item in session.scalars(
            select(BulkUserUpdateItem).where(
                BulkUserUpdateItem.job_id == job_id,
                BulkUserUpdateItem.status == "UPDATING",
            )
        ):
            item.status = "PENDING"
            item.message = "Retrying after resume."
    zone_supervisor.start_bulk_user_update_job(job_id)
    return {"ok": True}


@app.post("/api/users/bulk-jobs/{job_id}/cancel")
def api_cancel_bulk_user_update_job(
    request: Request,
    job_id: int,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    with session_scope() as session:
        _require_admin_api(request, session, x_csrf_token)
    try:
        zone_supervisor.cancel_bulk_user_update_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


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


def _oracle_summary(session) -> dict:
    counts = {
        str(status): count
        for status, count in session.execute(
            select(OracleAttendanceOutbox.status, func.count(OracleAttendanceOutbox.id)).group_by(
                OracleAttendanceOutbox.status
            )
        ).all()
    }
    total = session.scalar(select(func.count(OracleAttendanceOutbox.id))) or 0
    return {"counts": counts, "total": total}


def _refresh_bulk_job_counts_for_web(session, job: BulkUserUpdateJob) -> None:
    items = list(session.scalars(select(BulkUserUpdateItem).where(BulkUserUpdateItem.job_id == job.id)))
    job.total_count = len(items)
    job.pending_count = sum(1 for item in items if item.status == "PENDING")
    job.updating_count = sum(1 for item in items if item.status == "UPDATING")
    job.verified_count = sum(1 for item in items if item.status == "VERIFIED")
    job.skipped_count = sum(1 for item in items if item.status == "SKIPPED")
    job.failed_count = sum(1 for item in items if item.status == "FAILED")
    job.updated_at = utc_now()


def _serialize_bulk_job(job: BulkUserUpdateJob, items: list[BulkUserUpdateItem]) -> dict:
    return {
        "id": job.id,
        "device_id": job.device_id,
        "status": job.status,
        "total_count": job.total_count,
        "pending_count": job.pending_count,
        "updating_count": job.updating_count,
        "verified_count": job.verified_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
        "items": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "uid": item.uid,
                "old_name": item.old_name,
                "sheet_name": item.sheet_name,
                "expected_name": item.expected_name,
                "cnic": item.cnic,
                "status": item.status,
                "message": item.message,
                "attempt_count": item.attempt_count,
            }
            for item in items
        ],
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
