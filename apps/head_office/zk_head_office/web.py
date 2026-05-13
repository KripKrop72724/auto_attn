from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import io
import json
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
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
from zk_common.security import token_hash, verify_token
from zk_common.time_utils import utc_now
from zk_head_office.db import (
    AttendanceEvent,
    ClockCheck,
    Device,
    FraudIncident,
    OutagePeriod,
    SyncBatch,
    Zone,
    ZoneHeartbeat,
    init_db,
    session_scope,
)
from zk_head_office.settings import settings
from zk_head_office.validation import final_trust_status


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="ZK Head Office", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def auth_zone(authorization: str | None = Header(default=None)) -> Zone:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()
    with session_scope() as session:
        zones = session.scalars(select(Zone).where(Zone.active == True)).all()  # noqa: E712
        for zone in zones:
            if verify_token(token, zone.token_hash):
                session.expunge(zone)
                return zone
    raise HTTPException(status_code=401, detail="Invalid zone token.")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with session_scope() as session:
        zones = session.scalars(select(Zone).order_by(Zone.zone_name.asc())).all()
        incidents = session.scalars(select(FraudIncident).order_by(FraudIncident.created_at.desc()).limit(12)).all()
        attendance = session.scalars(select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(20)).all()
        counts = _counts(session)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"zones": zones, "incidents": incidents, "attendance": attendance, "counts": counts},
    )


@app.get("/zones", response_class=HTMLResponse)
def zones_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(Zone).order_by(Zone.zone_name.asc())).all()
    return templates.TemplateResponse(request=request, name="zones.html", context={"rows": rows})


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(Device).order_by(Device.zone_id.asc(), Device.device_id.asc())).all()
    return templates.TemplateResponse(request=request, name="devices.html", context={"rows": rows})


@app.get("/attendance", response_class=HTMLResponse)
def attendance_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(AttendanceEvent).order_by(AttendanceEvent.created_at.desc()).limit(500)).all()
    return templates.TemplateResponse(request=request, name="attendance.html", context={"rows": rows})


@app.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(FraudIncident).order_by(FraudIncident.created_at.desc()).limit(500)).all()
    return templates.TemplateResponse(request=request, name="incidents.html", context={"rows": rows})


@app.get("/clock", response_class=HTMLResponse)
def clock_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(ClockCheck).order_by(ClockCheck.created_at.desc()).limit(500)).all()
    return templates.TemplateResponse(request=request, name="clock.html", context={"rows": rows})


@app.get("/outages", response_class=HTMLResponse)
def outages_page(request: Request):
    with session_scope() as session:
        rows = session.scalars(select(OutagePeriod).order_by(OutagePeriod.created_at.desc()).limit(500)).all()
    return templates.TemplateResponse(request=request, name="outages.html", context={"rows": rows})


@app.get("/reports/attendance.csv")
def attendance_csv():
    with session_scope() as session:
        rows = session.scalars(select(AttendanceEvent).order_by(AttendanceEvent.device_event_time.asc())).all()
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


@app.get("/api/time")
def api_time() -> TimeResponse:
    return TimeResponse(server_utc=utc_now())


@app.post("/api/zones/register")
def register_zone(request: ZoneRegisterRequest) -> ZoneRegisterResponse:
    if request.enrollment_key != settings.enrollment_key:
        raise HTTPException(status_code=403, detail="Invalid enrollment key.")
    zone_token = secrets.token_urlsafe(32)
    with session_scope() as session:
        zone = session.scalar(select(Zone).where(Zone.zone_id == request.zone_id))
        if zone is None:
            zone = Zone(
                zone_id=request.zone_id,
                zone_name=request.zone_name,
                token_hash=token_hash(zone_token),
                last_heartbeat_at=utc_now(),
            )
            session.add(zone)
        else:
            zone.zone_name = request.zone_name
            zone.token_hash = token_hash(zone_token)
            zone.active = True
            zone.updated_at = utc_now()
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


def _counts(session: Session):
    return {
        "zones": session.scalar(select(func.count(Zone.id))) or 0,
        "devices": session.scalar(select(func.count(Device.id))) or 0,
        "attendance": session.scalar(select(func.count(AttendanceEvent.id))) or 0,
        "clock_checks": session.scalar(select(func.count(ClockCheck.id))) or 0,
        "outages": session.scalar(select(func.count(OutagePeriod.id))) or 0,
        "incidents": session.scalar(select(func.count(FraudIncident.id))) or 0,
    }
