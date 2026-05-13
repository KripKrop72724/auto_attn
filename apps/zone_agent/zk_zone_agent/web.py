from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Body, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from zk_common.enums import ClockStatus
from zk_common.schemas import ZoneRegisterRequest
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
from zk_zone_agent.network_scanner import network_scanner
from zk_zone_agent.settings import settings
from zk_zone_agent.supervisor import zone_supervisor
from zk_zone_agent.sync import HeadOfficeClient, sync_queue_writer
from zk_zone_agent.trusted_time import trusted_time_service


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
app = FastAPI(title="ZK Zone Agent", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with session_scope() as session:
        if not config_manager.setup_completed(session):
            return RedirectResponse("/setup", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str | None = None):
    with session_scope() as session:
        config = config_manager.get(session)
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={"config": config, "error": error},
    )


@app.post("/setup")
def save_setup(
    zone_id: str = Form(...),
    zone_name: str = Form(...),
    head_office_url: str = Form(...),
    enrollment_key: str = Form(...),
    timezone: str = Form("Asia/Karachi"),
):
    try:
        client = HeadOfficeClient(head_office_url)
        response = client.register_zone(
            ZoneRegisterRequest(zone_id=zone_id, zone_name=zone_name, enrollment_key=enrollment_key)
        )
    except Exception as exc:
        return RedirectResponse(
            f"/setup?error={quote_plus(f'Head office registration failed: {exc}')}",
            status_code=303,
        )

    with session_scope() as session:
        config_manager.save_setup(
            session,
            zone_id=zone_id,
            zone_name=zone_name,
            timezone=timezone,
            head_office_url=head_office_url,
            zone_token=response.zone_token,
        )
        trusted_time_service.update_from_head_office(response.server_utc, session)
    zone_supervisor.refresh_device_workers()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as session:
        config = config_manager.get(session)
        counts = _counts(session)
        devices = session.scalars(select(Device).order_by(Device.label.asc())).all()
        latest_attendance = session.scalars(
            select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(15)
        ).all()
        incidents = session.scalars(select(FraudIncident).order_by(FraudIncident.created_at.desc()).limit(10)).all()
        pending = sync_queue_writer.pending_count(session)
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
        },
    )


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, error: str | None = None, success: str | None = None):
    with session_scope() as session:
        config = config_manager.get(session)
        devices = device_registry.list_devices(session)
    return templates.TemplateResponse(
        request=request,
        name="devices.html",
        context={
            "devices": devices,
            "config": config,
            "error": error,
            "success": success,
            "workers_disabled": settings.disable_workers,
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
        job_payloads = [
            comm_key_bruteforce_manager.serialize_job(job, include_secret=True)
            for job in jobs
        ]
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
        },
    )


@app.post("/devices/scan", response_class=HTMLResponse)
def run_scan(request: Request, subnet: str = Form("")):
    subnets = [subnet] if subnet.strip() else None
    discovery_service.run_scan(source="MANUAL", subnets=subnets)
    return RedirectResponse("/devices/scan", status_code=303)


@app.post("/devices")
def save_device(
    device_id: str = Form(...),
    label: str = Form(...),
    ip: str = Form(...),
    port: int = Form(4370),
    comm_key: str = Form(...),
    return_to: str = Form("/devices"),
):
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
        config = config_manager.get(session)
    zone_supervisor.refresh_device_workers()
    if settings.disable_workers:
        message = "Device validated and saved. Workers are disabled in local settings."
    elif config is None or not config.setup_completed:
        message = "Device validated and saved. Complete setup to start live monitoring."
    else:
        message = "Device validated and saved. Worker is connecting now for live capture and clock sync."
    return RedirectResponse(f"/devices?success={quote_plus(message)}", status_code=303)


@app.post("/devices/discovery/{candidate_id}/bruteforce")
def start_bruteforce_from_form(
    candidate_id: int,
    mode: str = Form("SAFE_FAST"),
    range_start: int = Form(0),
    range_end: int = Form(999999),
    worker_count: int | None = Form(None),
    timeout_seconds: float | None = Form(None),
    common_keys: str = Form(""),
    confirm_bruteforce: str | None = Form(None),
):
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
def toggle_device(device_id: str):
    with session_scope() as session:
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
    return templates.TemplateResponse(request=request, name="attendance.html", context={"rows": rows})


@app.get("/clock-guard", response_class=HTMLResponse)
def clock_guard_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(ClockCheck).order_by(ClockCheck.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(
        request=request, name="clock.html", context={"rows": rows, "ClockStatus": ClockStatus}
    )


@app.get("/outages", response_class=HTMLResponse)
def outages_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(OutagePeriod).order_by(OutagePeriod.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request=request, name="outages.html", context={"rows": rows})


@app.get("/sync-queue", response_class=HTMLResponse)
def sync_queue_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(SyncQueue).order_by(SyncQueue.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request=request, name="sync_queue.html", context={"rows": rows})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(ServiceEvent).order_by(ServiceEvent.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request=request, name="logs.html", context={"rows": rows})


@app.get("/api/status")
def api_status():
    with session_scope() as session:
        config = config_manager.get(session)
        devices = session.scalars(select(Device).order_by(Device.label.asc())).all()
        return {
            "setup_completed": bool(config and config.setup_completed),
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
def api_discovery_rescan(body: DiscoveryRescanBody | None = Body(default=None)):
    state = discovery_service.trigger_scan(source="MANUAL", subnets=None if body is None else body.subnets)
    return {
        "ok": True,
        "running": state.running,
        "current_scan_id": state.current_scan_id,
    }


@app.post("/api/discovery/candidates/{candidate_id}/ignore")
def api_ignore_candidate(candidate_id: int):
    with session_scope() as session:
        candidate = session.get(DeviceDiscoveryResult, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404)
        candidate.status = "IGNORED"
        candidate.updated_at = utc_now()
    return {"ok": True}


@app.post("/api/discovery/candidates/{candidate_id}/bruteforce/start")
def api_start_candidate_bruteforce(candidate_id: int, body: BruteForceStartBody):
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
def api_pause_bruteforce_job(job_id: int):
    try:
        comm_key_bruteforce_manager.pause(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


@app.post("/api/bruteforce/jobs/{job_id}/resume")
def api_resume_bruteforce_job(job_id: int):
    try:
        comm_key_bruteforce_manager.resume(job_id)
    except KeyError:
        raise HTTPException(status_code=404) from None
    return {"ok": True}


@app.post("/api/bruteforce/jobs/{job_id}/cancel")
def api_cancel_bruteforce_job(job_id: int):
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
