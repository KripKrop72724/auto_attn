from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from zk_common.enums import ClockStatus
from zk_common.schemas import ZoneRegisterRequest
from zk_common.time_utils import utc_now
from zk_zone_agent.config import config_manager
from zk_zone_agent.db import (
    AttendanceEvent,
    ClockCheck,
    Device,
    FraudIncident,
    OutagePeriod,
    ServiceEvent,
    SyncQueue,
    init_db,
    session_scope,
)
from zk_zone_agent.device_registry import device_registry
from zk_zone_agent.network_scanner import network_scanner
from zk_zone_agent.settings import settings
from zk_zone_agent.supervisor import zone_supervisor
from zk_zone_agent.sync import HeadOfficeClient, sync_queue_writer
from zk_zone_agent.trusted_time import trusted_time_service
from zk_zone_agent.zk_client import PyZKClient


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
def devices_page(request: Request):
    with session_scope() as session:
        devices = device_registry.list_devices(session)
    return templates.TemplateResponse(request=request, name="devices.html", context={"devices": devices})


@app.get("/devices/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    subnets = [str(item) for item in network_scanner.discover_subnets()]
    return templates.TemplateResponse(request=request, name="scan.html", context={"subnets": subnets, "results": []})


@app.post("/devices/scan", response_class=HTMLResponse)
def run_scan(request: Request, subnet: str = Form("")):
    subnets = [subnet] if subnet.strip() else None
    results = network_scanner.scan(
        subnets=subnets,
        timeout=settings.scan_timeout_seconds,
        max_workers=settings.scan_concurrency,
    )
    return templates.TemplateResponse(
        request=request,
        name="scan.html",
        context={"subnets": [str(item) for item in network_scanner.discover_subnets()], "results": results},
    )


@app.post("/devices")
def save_device(
    device_id: str = Form(...),
    label: str = Form(...),
    ip: str = Form(...),
    port: int = Form(4370),
    comm_key: str = Form("0"),
):
    serial = platform = device_name = None
    try:
        client = PyZKClient(ip=ip, port=port, comm_key=int(comm_key or 0), timeout=5)
        client.connect()
        info = client.get_info()
        serial, platform, device_name = info.serial, info.platform, info.device_name
        client.disconnect()
    except Exception:
        pass
    with session_scope() as session:
        device_registry.save_device(
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
    zone_supervisor.refresh_device_workers()
    return RedirectResponse("/devices", status_code=303)


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
