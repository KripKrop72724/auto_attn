from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import io
import json
import secrets
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from zk_common.enums import PayloadType
from zk_common.schemas import (
    AttendanceSyncRequest,
    ClockChecksSyncRequest,
    HeartbeatRequest,
    IncidentSyncRequest,
    OutageSyncRequest,
    SyncResponse,
    TimeResponse,
    ZoneRegisterRequest,
    ZoneRegisterResponse,
)
from zk_common.security import (
    body_sha256,
    timestamp_within_skew,
    token_hash,
    verify_request_signature,
    verify_token,
)
from zk_common.time_utils import parse_datetime, utc_now
from zk_common.ui_time import (
    apply_timeline_date_filter,
    filter_context,
    selected_query_values,
    timeline_date_filter,
    timestamp_view,
)
from zk_head_office import APP_VERSION
from zk_head_office.admin_auth import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    admin_auth_enabled,
    login_rate_limiter,
    make_session,
    manual_action_rate_limiter,
    parse_session,
    require_auth_config,
    valid_csrf,
    verify_admin_login,
)
from zk_head_office.db import (
    AttendanceEvent,
    ClockCheck,
    Device,
    FraudIncident,
    OutagePeriod,
    SecurityEvent,
    SyncBatch,
    SyncNonce,
    Zone,
    ZoneHeartbeat,
    init_db,
    session_scope,
)
from zk_head_office.settings import settings
from zk_head_office.validation import final_trust_status


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["timestamp_view"] = timestamp_view

@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_auth_config()
    init_db()
    yield


app = FastAPI(title="ZK Head Office", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


async def auth_zone(
    request: Request,
    authorization: str | None = Header(default=None),
    x_zk_zone_id: str | None = Header(default=None, alias="X-ZK-Zone-Id"),
    x_zk_timestamp: str | None = Header(default=None, alias="X-ZK-Timestamp"),
    x_zk_nonce: str | None = Header(default=None, alias="X-ZK-Nonce"),
    x_zk_body_sha256: str | None = Header(default=None, alias="X-ZK-Body-SHA256"),
    x_zk_signature: str | None = Header(default=None, alias="X-ZK-Signature"),
) -> Zone:
    if not authorization or not authorization.startswith("Bearer "):
        _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Missing bearer token.")
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    if not all([x_zk_zone_id, x_zk_timestamp, x_zk_nonce, x_zk_body_sha256, x_zk_signature]):
        _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Missing signed sync headers.")
        raise HTTPException(status_code=401, detail="Missing signed sync headers.")
    token = authorization.removeprefix("Bearer ").strip()
    body = await request.body()
    actual_body_hash = body_sha256(body)
    if not secrets.compare_digest(actual_body_hash, x_zk_body_sha256):
        _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Body hash mismatch.")
        raise HTTPException(status_code=401, detail="Body hash mismatch.")
    try:
        request_timestamp = parse_datetime(x_zk_timestamp)
        if not timestamp_within_skew(x_zk_timestamp):
            raise ValueError("Timestamp outside allowed skew.")
    except Exception:
        _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Invalid request timestamp.")
        raise HTTPException(status_code=401, detail="Invalid request timestamp.") from None

    with session_scope() as session:
        zones = session.scalars(
            select(Zone).where(Zone.active == True, Zone.token_revoked_at == None)  # noqa: E711,E712
        ).all()
        matched: Zone | None = None
        for zone in zones:
            if verify_token(token, zone.token_hash):
                matched = zone
                break
        if matched is None:
            _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Invalid zone token.")
            raise HTTPException(status_code=401, detail="Invalid zone token.")
        if x_zk_zone_id != matched.zone_id:
            _record_security_event(
                request,
                "SYNC_AUTH_FAILED",
                x_zk_zone_id,
                "Signed zone id does not match token owner.",
            )
            raise HTTPException(status_code=403, detail="Token does not match zone id.")
        if not verify_request_signature(
            token=token,
            method=request.method,
            path=request.url.path,
            timestamp=x_zk_timestamp,
            nonce=x_zk_nonce,
            body_hash=x_zk_body_sha256,
            signature=x_zk_signature,
        ):
            _record_security_event(request, "SYNC_AUTH_FAILED", x_zk_zone_id, "Invalid sync signature.")
            raise HTTPException(status_code=401, detail="Invalid sync signature.")
        if session.scalar(
            select(SyncNonce).where(SyncNonce.zone_id == matched.zone_id, SyncNonce.nonce == x_zk_nonce)
        ):
            _record_security_event(request, "SYNC_AUTH_REPLAY", matched.zone_id, "Replay nonce rejected.")
            raise HTTPException(status_code=409, detail="Replay nonce rejected.")
        session.add(
            SyncNonce(
                zone_id=matched.zone_id,
                nonce=x_zk_nonce,
                request_timestamp=request_timestamp,
            )
        )
        matched.token_last_used_at = utc_now()
        matched.updated_at = utc_now()
        session.flush()
        session.expunge(matched)
        return matched


def _record_security_event(
    request: Request | None,
    event_type: str,
    zone_id: str | None,
    description: str,
) -> None:
    try:
        ip_address = request.client.host if request and request.client else None
        with session_scope() as session:
            session.add(
                SecurityEvent(
                    event_type=event_type,
                    zone_id=zone_id,
                    ip_address=ip_address,
                    description=description,
                )
            )
    except Exception:
        pass


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _request_path(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return path


def _admin_context(request: Request) -> dict:
    admin_session = parse_session(request.cookies.get(SESSION_COOKIE))
    return {
        "admin_auth_required": admin_auth_enabled(),
        "admin_authenticated": not admin_auth_enabled() or admin_session is not None,
        "csrf_token": "" if admin_session is None else admin_session.csrf_token,
        "display_timezone": settings.display_timezone,
    }


def _admin_redirect(request: Request) -> RedirectResponse | None:
    if not admin_auth_enabled() or parse_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return None
    return RedirectResponse(f"/login?next={quote_plus(_request_path(request))}", status_code=303)


def _timeline_filter(request: Request):
    return timeline_date_filter(request.query_params, timezone_name=settings.display_timezone)


def _filters_context(date_filter, selected: dict[str, str], choices: dict[str, list[dict[str, str]]] | None = None):
    return {
        "filters": filter_context(date_filter, selected),
        "filter_choices": choices or {},
        "display_timezone": date_filter.display_timezone,
    }


def _current_query_path(request: Request, path: str) -> str:
    return f"{path}?{request.url.query}" if request.url.query else path


def _apply_selected(statement, selected: dict[str, str], columns: dict[str, object]):
    for key, column in columns.items():
        if value := selected.get(key):
            statement = statement.where(column == value)
    return statement


def _option(value: str, label: str | None = None) -> dict[str, str]:
    return {"value": value, "label": label or value}


def _distinct_options(session: Session, column) -> list[dict[str, str]]:
    values = session.scalars(
        select(column).where(column.is_not(None)).distinct().order_by(column.asc())
    ).all()
    return [_option(str(value)) for value in values if str(value).strip()]


def _zone_options(session: Session) -> list[dict[str, str]]:
    rows = session.scalars(select(Zone).order_by(Zone.zone_name.asc(), Zone.zone_id.asc())).all()
    return [_option(row.zone_id, f"{row.zone_name} ({row.zone_id})") for row in rows]


def _require_admin_form(request: Request, csrf_token: str | None) -> None:
    if not admin_auth_enabled():
        return
    if not manual_action_rate_limiter.allow(_client_ip(request)):
        _record_security_event(request, "HEAD_ADMIN_RATE_LIMIT", None, "Too many admin actions.")
        raise HTTPException(status_code=429, detail="Too many admin actions.")
    if not valid_csrf(request.cookies.get(SESSION_COOKIE), csrf_token):
        _record_security_event(request, "HEAD_ADMIN_CSRF_REJECTED", None, "Admin CSRF validation failed.")
        raise HTTPException(status_code=403, detail="Admin unlock and CSRF token are required.")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None, next: str | None = None):
    if not admin_auth_enabled():
        return RedirectResponse("/", status_code=303)
    if parse_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error, "next": _safe_next(next), **_admin_context(request)},
    )


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if not admin_auth_enabled():
        return RedirectResponse("/", status_code=303)
    if not login_rate_limiter.allow(_client_ip(request)):
        _record_security_event(request, "HEAD_ADMIN_LOGIN_RATE_LIMIT", None, "Too many login attempts.")
        return RedirectResponse(
            f"/login?error={quote_plus('Too many login attempts. Try again shortly.')}&next={quote_plus(_safe_next(next))}",
            status_code=303,
        )
    if not verify_admin_login(password):
        _record_security_event(request, "HEAD_ADMIN_LOGIN_FAILED", None, "Invalid admin password.")
        return RedirectResponse(
            f"/login?error={quote_plus('Invalid admin password.')}&next={quote_plus(_safe_next(next))}",
            status_code=303,
        )
    cookie_value, _session = make_session()
    _record_security_event(request, "HEAD_ADMIN_LOGIN_SUCCEEDED", None, "Head office admin unlocked the UI.")
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="strict",
        secure=settings.admin_cookie_secure,
        max_age=SESSION_SECONDS,
    )
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    _require_admin_form(request, csrf_token)
    _record_security_event(request, "HEAD_ADMIN_LOGOUT", None, "Head office admin locked the UI.")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(request.query_params, ["zone_id"])
    with session_scope() as session:
        zones_stmt = select(Zone).order_by(Zone.zone_name.asc())
        if selected.get("zone_id"):
            zones_stmt = zones_stmt.where(Zone.zone_id == selected["zone_id"])
        zones = session.scalars(zones_stmt).all()
        incidents_stmt = apply_timeline_date_filter(
            select(FraudIncident).order_by(FraudIncident.created_at.desc()),
            FraudIncident.created_at,
            date_filter,
        )
        incidents_stmt = _apply_selected(incidents_stmt, selected, {"zone_id": FraudIncident.zone_id})
        incidents = session.scalars(incidents_stmt.limit(12)).all()
        attendance_stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.desc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        attendance_stmt = _apply_selected(attendance_stmt, selected, {"zone_id": AttendanceEvent.zone_id})
        attendance = session.scalars(attendance_stmt.limit(20)).all()
        counts = _counts(session)
        choices = {"zones": _zone_options(session)}
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "zones": zones,
            "incidents": incidents,
            "attendance": attendance,
            "counts": counts,
            **_filters_context(date_filter, selected, choices),
            **_admin_context(request),
        },
    )


@app.get("/zones", response_class=HTMLResponse)
def zones_page(request: Request, generated_token: str | None = None, token_zone_id: str | None = None):
    if redirect := _admin_redirect(request):
        return redirect
    with session_scope() as session:
        rows = session.scalars(select(Zone).order_by(Zone.zone_name.asc())).all()
    return templates.TemplateResponse(
        request=request,
        name="zones.html",
        context={
            "rows": rows,
            "generated_token": generated_token,
            "token_zone_id": token_zone_id,
            **_admin_context(request),
        },
    )


@app.post("/zones/token", response_class=HTMLResponse)
def issue_zone_token_page(
    request: Request,
    zone_id: str = Form(...),
    zone_name: str = Form(...),
    csrf_token: str = Form(""),
):
    _require_admin_form(request, csrf_token)
    zone_token = _issue_zone_token(zone_id=zone_id.strip(), zone_name=zone_name.strip())
    with session_scope() as session:
        rows = session.scalars(select(Zone).order_by(Zone.zone_name.asc())).all()
    return templates.TemplateResponse(
        request=request,
        name="zones.html",
        context={
            "rows": rows,
            "generated_token": zone_token,
            "token_zone_id": zone_id.strip(),
            **_admin_context(request),
        },
    )


@app.post("/zones/{zone_id}/revoke")
def revoke_zone_token(request: Request, zone_id: str, csrf_token: str = Form("")):
    _require_admin_form(request, csrf_token)
    with session_scope() as session:
        zone = session.scalar(select(Zone).where(Zone.zone_id == zone_id))
        if zone is None:
            raise HTTPException(status_code=404)
        zone.active = False
        zone.token_revoked_at = utc_now()
        zone.updated_at = utc_now()
    return zones_page_redirect()


def zones_page_redirect():
    return RedirectResponse("/zones", status_code=303)


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    with session_scope() as session:
        rows = session.scalars(select(Device).order_by(Device.zone_id.asc(), Device.device_id.asc())).all()
    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={"rows": rows, **_admin_context(request)},
    )


@app.get("/attendance", response_class=HTMLResponse)
def attendance_page(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(
        request.query_params,
        ["zone_id", "device_id", "source_type", "trust_status"],
    )
    with session_scope() as session:
        stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.desc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "zone_id": AttendanceEvent.zone_id,
                "device_id": AttendanceEvent.device_id,
                "source_type": AttendanceEvent.source_type,
                "trust_status": AttendanceEvent.head_office_final_trust_status,
            },
        )
        rows = session.scalars(stmt.limit(500)).all()
        choices = {
            "zones": _zone_options(session),
            "devices": _distinct_options(session, AttendanceEvent.device_id),
            "source_types": _distinct_options(session, AttendanceEvent.source_type),
            "trust_statuses": _distinct_options(session, AttendanceEvent.head_office_final_trust_status),
        }
    return templates.TemplateResponse(
        request=request,
        name="attendance.html",
        context={
            "rows": rows,
            "csv_url": _current_query_path(request, "/reports/attendance.csv"),
            **_filters_context(date_filter, selected, choices),
            **_admin_context(request),
        },
    )


@app.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(
        request.query_params,
        ["zone_id", "device_id", "severity", "incident_type"],
    )
    with session_scope() as session:
        stmt = apply_timeline_date_filter(
            select(FraudIncident).order_by(FraudIncident.created_at.desc()),
            FraudIncident.created_at,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "zone_id": FraudIncident.zone_id,
                "device_id": FraudIncident.device_id,
                "severity": FraudIncident.severity,
                "incident_type": FraudIncident.incident_type,
            },
        )
        rows = session.scalars(stmt.limit(500)).all()
        choices = {
            "zones": _zone_options(session),
            "devices": _distinct_options(session, FraudIncident.device_id),
            "severities": _distinct_options(session, FraudIncident.severity),
            "incident_types": _distinct_options(session, FraudIncident.incident_type),
        }
    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **_admin_context(request)},
    )


@app.get("/clock", response_class=HTMLResponse)
def clock_page(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(request.query_params, ["zone_id", "device_id", "status"])
    with session_scope() as session:
        stmt = apply_timeline_date_filter(
            select(ClockCheck).order_by(ClockCheck.trusted_time.desc()),
            ClockCheck.trusted_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {"zone_id": ClockCheck.zone_id, "device_id": ClockCheck.device_id, "status": ClockCheck.status},
        )
        rows = session.scalars(stmt.limit(500)).all()
        choices = {
            "zones": _zone_options(session),
            "devices": _distinct_options(session, ClockCheck.device_id),
            "statuses": _distinct_options(session, ClockCheck.status),
        }
    return templates.TemplateResponse(
        request=request,
        name="clock.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **_admin_context(request)},
    )


@app.get("/outages", response_class=HTMLResponse)
def outages_page(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(request.query_params, ["zone_id", "device_id", "outage_type"])
    with session_scope() as session:
        stmt = apply_timeline_date_filter(
            select(OutagePeriod).order_by(OutagePeriod.start_time.desc()),
            OutagePeriod.start_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "zone_id": OutagePeriod.zone_id,
                "device_id": OutagePeriod.device_id,
                "outage_type": OutagePeriod.outage_type,
            },
        )
        rows = session.scalars(stmt.limit(500)).all()
        choices = {
            "zones": _zone_options(session),
            "devices": _distinct_options(session, OutagePeriod.device_id),
            "outage_types": _distinct_options(session, OutagePeriod.outage_type),
        }
    return templates.TemplateResponse(
        request=request,
        name="outages.html",
        context={"rows": rows, **_filters_context(date_filter, selected, choices), **_admin_context(request)},
    )


@app.get("/reports/attendance.csv")
def attendance_csv(request: Request):
    if redirect := _admin_redirect(request):
        return redirect
    date_filter = _timeline_filter(request)
    selected = selected_query_values(
        request.query_params,
        ["zone_id", "device_id", "source_type", "trust_status"],
    )
    with session_scope() as session:
        stmt = apply_timeline_date_filter(
            select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.asc()),
            AttendanceEvent.device_event_time,
            date_filter,
        )
        stmt = _apply_selected(
            stmt,
            selected,
            {
                "zone_id": AttendanceEvent.zone_id,
                "device_id": AttendanceEvent.device_id,
                "source_type": AttendanceEvent.source_type,
                "trust_status": AttendanceEvent.head_office_final_trust_status,
            },
        )
        rows = session.scalars(stmt).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "event_uid",
                "zone_id",
                "device_id",
                "user_id",
                "employee_name",
                "device_event_time",
                "zone_trusted_time",
                "head_office_received_time",
                "zone_claimed_trust_status",
                "head_office_final_trust_status",
                "fraud_score",
                "fraud_reason",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.event_uid,
                    row.zone_id,
                    row.device_id,
                    row.user_id,
                    row.employee_name or "",
                    row.device_event_time,
                    row.zone_trusted_time,
                    row.head_office_received_time,
                    row.zone_claimed_trust_status,
                    row.head_office_final_trust_status,
                    row.fraud_score,
                    row.fraud_reason or "",
                ]
            )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=attendance.csv"},
    )


@app.get("/api/health")
def api_health():
    database_ok = True
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
    return {
        "ok": database_ok,
        "app": "zk-head-office",
        "version": APP_VERSION,
        "server_utc": utc_now(),
        "database_ok": database_ok,
    }


@app.get("/api/time")
def api_time(_zone: Zone = Depends(auth_zone)) -> TimeResponse:
    return TimeResponse(server_utc=utc_now())


@app.post("/api/zones/register")
def register_zone(request: ZoneRegisterRequest) -> ZoneRegisterResponse:
    if not settings.allow_legacy_registration:
        raise HTTPException(status_code=410, detail="Enrollment-key registration is disabled.")
    if request.enrollment_key != settings.enrollment_key:
        raise HTTPException(status_code=403, detail="Invalid enrollment key.")
    zone_token = _issue_zone_token(zone_id=request.zone_id, zone_name=request.zone_name)
    return ZoneRegisterResponse(ok=True, zone_token=zone_token, server_utc=utc_now())


@app.post("/api/zones/heartbeat")
def heartbeat(request: HeartbeatRequest, zone: Zone = Depends(auth_zone)):
    if request.zone_id != zone.zone_id:
        raise HTTPException(status_code=403, detail="Token does not match zone_id.")
    now = utc_now()
    with session_scope() as session:
        db_zone = session.scalar(select(Zone).where(Zone.zone_id == zone.zone_id))
        if db_zone is None:
            raise HTTPException(status_code=404)
        db_zone.zone_name = request.zone_name
        db_zone.last_heartbeat_at = now
        db_zone.last_seen_server_time_estimate = request.server_time_estimate
        db_zone.pending_queue_count = request.pending_queue_count
        db_zone.updated_at = now
        session.add(
            ZoneHeartbeat(
                zone_id=request.zone_id,
                zone_name=request.zone_name,
                agent_version=request.agent_version,
                server_time_estimate=request.server_time_estimate,
                devices_json=json.dumps([item.model_dump(mode="json") for item in request.devices]),
                pending_queue_count=request.pending_queue_count,
            )
        )
        for item in request.devices:
            _upsert_device(session, request.zone_id, item.device_id, item.serial, item.online, item.last_clock_status, item.last_drift_seconds)
    return {"ok": True, "server_utc": utc_now()}


@app.post("/api/sync/attendance")
def sync_attendance(request: AttendanceSyncRequest, zone: Zone = Depends(auth_zone)) -> SyncResponse:
    _assert_zone(request.zone_id, zone)
    acked: list[str] = []
    errors: list[str] = []
    with session_scope() as session:
        for event in request.events:
            if session.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == event.event_uid)):
                acked.append(event.event_uid)
                continue
            if event.device_serial:
                conflict = session.scalar(
                    select(Device).where(Device.serial == event.device_serial, Device.zone_id != zone.zone_id).limit(1)
                )
                if conflict:
                    errors.append(f"Device serial {event.device_serial} belongs to a different zone.")
                    continue
            _upsert_device(session, zone.zone_id, event.device_id, event.device_serial, True, None, None)
            final_status, reason, score = final_trust_status(session, event, zone_id=zone.zone_id)
            row = AttendanceEvent(
                event_uid=event.event_uid,
                zone_id=zone.zone_id,
                device_id=event.device_id,
                device_serial=event.device_serial,
                user_id=event.user_id,
                employee_name=event.employee_name,
                device_event_time=event.device_event_time,
                zone_received_wall_time=event.zone_received_wall_time,
                zone_trusted_time=event.zone_trusted_time,
                head_office_received_time=utc_now(),
                zone_claimed_trust_status=event.trust_status.value,
                head_office_final_trust_status=final_status.value,
                source_type=event.source_type.value,
                punch=event.punch,
                raw_event=json.dumps(event.raw_event, default=str, sort_keys=True),
                device_drift_seconds=event.device_drift_seconds,
                fraud_score=max(event.fraud_score, score),
                fraud_reason=reason or event.fraud_reason,
            )
            session.add(row)
            acked.append(event.event_uid)
        _record_batch(session, request.batch_id, zone.zone_id, PayloadType.ATTENDANCE.value, len(request.events), not errors, errors)
    return SyncResponse(ok=not errors, acked_event_uids=acked, server_utc=utc_now(), errors=errors)


@app.post("/api/sync/clock-checks")
def sync_clock_checks(request: ClockChecksSyncRequest, zone: Zone = Depends(auth_zone)) -> SyncResponse:
    _assert_zone(request.zone_id, zone)
    acked: list[str] = []
    with session_scope() as session:
        for item in request.clock_checks:
            external_id = str(item.id) if item.id is not None else None
            row = ClockCheck(
                external_id=external_id,
                zone_id=zone.zone_id,
                device_id=item.device_id,
                device_serial=item.device_serial,
                device_time=item.device_time,
                trusted_time=item.trusted_time,
                windows_wall_time=item.windows_wall_time,
                monotonic_ns=item.monotonic_ns,
                drift_seconds=item.drift_seconds,
                expected_device_time=item.expected_device_time,
                jump_seconds=item.jump_seconds,
                status=item.status.value,
                reason=item.reason,
            )
            session.add(row)
            if external_id:
                acked.append(external_id)
            _upsert_device(session, zone.zone_id, item.device_id, item.device_serial, True, item.status.value, item.drift_seconds)
        _record_batch(session, request.batch_id, zone.zone_id, PayloadType.CLOCK_CHECK.value, len(request.clock_checks), True, [])
    return SyncResponse(ok=True, acked_ids=acked, server_utc=utc_now())


@app.post("/api/sync/outages")
def sync_outages(request: OutageSyncRequest, zone: Zone = Depends(auth_zone)) -> SyncResponse:
    _assert_zone(request.zone_id, zone)
    acked: list[str] = []
    with session_scope() as session:
        for item in request.outages:
            external_id = str(item.id) if item.id is not None else None
            session.add(
                OutagePeriod(
                    external_id=external_id,
                    zone_id=zone.zone_id,
                    device_id=item.device_id,
                    outage_type=item.outage_type.value,
                    start_time=item.start_time,
                    end_time=item.end_time,
                    duration_seconds=item.duration_seconds,
                    start_reason=item.start_reason,
                    end_reason=item.end_reason,
                    classification=item.classification,
                )
            )
            if external_id:
                acked.append(external_id)
        _record_batch(session, request.batch_id, zone.zone_id, PayloadType.OUTAGE.value, len(request.outages), True, [])
    return SyncResponse(ok=True, acked_ids=acked, server_utc=utc_now())


@app.post("/api/sync/incidents")
def sync_incidents(request: IncidentSyncRequest, zone: Zone = Depends(auth_zone)) -> SyncResponse:
    _assert_zone(request.zone_id, zone)
    acked: list[str] = []
    with session_scope() as session:
        for item in request.incidents:
            external_id = str(item.id) if item.id is not None else None
            session.add(
                FraudIncident(
                    external_id=external_id,
                    zone_id=zone.zone_id,
                    device_id=item.device_id,
                    incident_type=item.incident_type.value,
                    severity=item.severity.value,
                    description=item.description,
                    related_event_uid=item.related_event_uid,
                    related_outage_id=None if item.related_outage_id is None else str(item.related_outage_id),
                    created_at=item.created_at,
                )
            )
            if external_id:
                acked.append(external_id)
        _record_batch(session, request.batch_id, zone.zone_id, PayloadType.INCIDENT.value, len(request.incidents), True, [])
    return SyncResponse(ok=True, acked_ids=acked, server_utc=utc_now())


def _assert_zone(request_zone_id: str, zone: Zone) -> None:
    if request_zone_id != zone.zone_id:
        raise HTTPException(status_code=403, detail="Token does not match zone_id.")


def _upsert_device(
    session: Session,
    zone_id: str,
    device_id: str,
    serial: str | None,
    online: bool,
    last_clock_status: str | None,
    last_drift_seconds: float | None,
) -> Device:
    device = session.scalar(select(Device).where(Device.zone_id == zone_id, Device.device_id == device_id))
    if device is None:
        device = Device(zone_id=zone_id, device_id=device_id)
        session.add(device)
    device.serial = serial or device.serial
    device.online = online
    device.last_clock_status = last_clock_status or device.last_clock_status
    device.last_drift_seconds = last_drift_seconds if last_drift_seconds is not None else device.last_drift_seconds
    device.last_seen_at = utc_now()
    device.updated_at = utc_now()
    return device


def _record_batch(
    session: Session,
    batch_id: str,
    zone_id: str,
    payload_type: str,
    item_count: int,
    ok: bool,
    errors: list[str],
) -> None:
    if session.scalar(select(SyncBatch).where(SyncBatch.batch_id == batch_id)):
        return
    session.add(
        SyncBatch(
            batch_id=batch_id,
            zone_id=zone_id,
            payload_type=payload_type,
            item_count=item_count,
            ok=ok,
            errors_json=json.dumps(errors),
        )
    )


def _issue_zone_token(*, zone_id: str, zone_name: str) -> str:
    if not zone_id:
        raise HTTPException(status_code=400, detail="Zone ID is required.")
    if not zone_name:
        raise HTTPException(status_code=400, detail="Zone name is required.")
    zone_token = secrets.token_urlsafe(32)
    now = utc_now()
    with session_scope() as session:
        zone = session.scalar(select(Zone).where(Zone.zone_id == zone_id))
        if zone is None:
            session.add(
                Zone(
                    zone_id=zone_id,
                    zone_name=zone_name,
                    token_hash=token_hash(zone_token),
                    token_last4=zone_token[-4:],
                    token_issued_at=now,
                    token_revoked_at=None,
                    token_last_used_at=None,
                    last_heartbeat_at=None,
                    active=True,
                )
            )
        else:
            zone.zone_name = zone_name
            zone.token_hash = token_hash(zone_token)
            zone.token_last4 = zone_token[-4:]
            zone.token_issued_at = now
            zone.token_revoked_at = None
            zone.token_last_used_at = None
            zone.active = True
            zone.updated_at = now
    return zone_token


def _counts(session: Session):
    return {
        "zones": session.scalar(select(func.count(Zone.id))) or 0,
        "devices": session.scalar(select(func.count(Device.id))) or 0,
        "attendance": session.scalar(select(func.count(AttendanceEvent.id))) or 0,
        "clock_checks": session.scalar(select(func.count(ClockCheck.id))) or 0,
        "outages": session.scalar(select(func.count(OutagePeriod.id))) or 0,
        "incidents": session.scalar(select(func.count(FraudIncident.id))) or 0,
    }
