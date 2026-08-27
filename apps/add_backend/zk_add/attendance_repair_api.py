"""Administrator API for terminal-scoped employee attendance repair."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from zk_add.attendance_repair import (
    JOB_TERMINAL_STATES,
    OracleRepairError,
    RepairError,
    approve_repair_job,
    build_repair_candidates,
    control_repair_job,
    create_repair_job,
    oracle_repair_capabilities,
    repair_preflight,
    repair_worker_metrics,
    serialize_repair_job,
    stream_repair_evidence,
)
from zk_add.db import SessionLocal
from zk_add.models import AttendanceRepairJob, Connector
from zk_add.realtime import browser_events
from zk_add.security import AdminContext, admin_context, require_csrf, require_step_up
from zk_add.settings import settings


router = APIRouter(tags=["attendance-repair"])


class RepairCandidateQuery(BaseModel):
    user_keys: list[str] = Field(min_length=1, max_length=500)
    date_from: date | None = None
    date_to: date | None = None

    @field_validator("user_keys")
    @classmethod
    def unique_user_keys(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Each employee may be selected once")
        return values

    @model_validator(mode="after")
    def complete_date_scope(self):
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("Provide both date bounds or neither")
        return self


class RepairTargetSelection(BaseModel):
    user_key: str = Field(min_length=36, max_length=36)
    expected_row_version: int = Field(ge=1)
    all_provable_history: bool = True
    cohort_tokens: list[str] = Field(default_factory=list, max_length=5000)

    @field_validator("cohort_tokens")
    @classmethod
    def valid_tokens(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("A cohort may be selected once")
        if any(len(value) != 64 for value in values):
            raise ValueError("Invalid server-issued cohort token")
        return values


class RepairPrepareRequest(BaseModel):
    targets: list[RepairTargetSelection] = Field(min_length=1, max_length=500)
    date_from: date | None = None
    date_to: date | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_scope(self):
        keys = [target.user_key for target in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("Each employee may be selected once")
        if sum(len(target.cohort_tokens) for target in self.targets) > 25_000:
            raise ValueError("Select no more than 25,000 historical cohorts per request")
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("Provide both date bounds or neither")
        return self


class RepairApproveRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: SecretStr
    typed_confirmation: str = Field(min_length=10, max_length=240)
    preview_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=120)


class RepairControlRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: SecretStr
    idempotency_key: str = Field(min_length=8, max_length=120)


def _db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _admin(request: Request, db: Session = Depends(_db)) -> tuple[Session, AdminContext]:
    return db, admin_context(request, db)


def _admin_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    db: Session = Depends(_db),
) -> tuple[Session, AdminContext]:
    context = admin_context(request, db)
    require_csrf(context, x_csrf_token)
    return db, context


def _connector(session: Session, connector_id: str) -> Connector:
    connector = session.scalar(select(Connector).where(Connector.connector_id == connector_id))
    if connector is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    return connector


def _job(session: Session, job_id: str) -> AttendanceRepairJob:
    row = session.scalar(select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Attendance repair job not found.")
    return row


def _raise_repair(error: Exception) -> None:
    code = getattr(error, "code", "ATTENDANCE_REPAIR_REJECTED")
    status = 503 if code.startswith("ORDS_") else 409
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": str(error)},
    ) from error


@router.get("/api/v1/devices/{connector_id}/attendance-repairs/preflight")
async def attendance_repair_preflight(
    connector_id: str,
    auth: tuple[Session, AdminContext] = Depends(_admin),
):
    db, _context = auth
    result = repair_preflight(db, _connector(db, connector_id))
    try:
        capabilities = await oracle_repair_capabilities()
        result["oracle"] = {"available": True, "capabilities": capabilities}
    except OracleRepairError as error:
        result["oracle"] = {
            "available": False,
            "error_code": error.code,
            "message": str(error),
        }
        result["ready_now"] = False
    result["worker"] = repair_worker_metrics(db)
    return result


@router.post("/api/v1/devices/{connector_id}/attendance-repair-candidates/query")
def query_attendance_repair_candidates(
    connector_id: str,
    body: RepairCandidateQuery,
    auth: tuple[Session, AdminContext] = Depends(_admin),
):
    db, _context = auth
    try:
        return build_repair_candidates(
            db,
            connector=_connector(db, connector_id),
            user_keys=body.user_keys,
            date_from=body.date_from,
            date_to=body.date_to,
        )
    except RepairError as error:
        _raise_repair(error)


@router.post(
    "/api/v1/devices/{connector_id}/attendance-repairs/prepare",
    status_code=201,
)
async def prepare_attendance_repair(
    connector_id: str,
    body: RepairPrepareRequest,
    auth: tuple[Session, AdminContext] = Depends(_admin_mutation),
):
    db, context = auth
    try:
        job = create_repair_job(
            db,
            connector=_connector(db, connector_id),
            actor=context.username,
            selections=[target.model_dump() for target in body.targets],
            date_from=body.date_from,
            date_to=body.date_to,
            idempotency_key=body.idempotency_key,
        )
        job_id = job.job_id
        db.commit()
    except RepairError as error:
        db.rollback()
        _raise_repair(error)
    row = _job(db, job_id)
    await browser_events.publish(
        "attendance_repair",
        {"job_id": row.job_id, "status": row.status, "phase": row.phase},
    )
    return serialize_repair_job(db, row, include_items=True)


@router.get("/api/v1/attendance-repairs")
def list_attendance_repairs(
    connector_id: str | None = None,
    status: str | None = None,
    q: str | None = Query(default=None, max_length=120),
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(_admin),
):
    db, _context = auth
    clauses = []
    if connector_id:
        clauses.append(AttendanceRepairJob.connector_id == _connector(db, connector_id).id)
    if status:
        clauses.append(AttendanceRepairJob.status == status.upper())
    if q and q.strip():
        term = f"%{q.strip()}%"
        clauses.append(
            or_(
                AttendanceRepairJob.job_id.ilike(term),
                Connector.connector_id.ilike(term),
                Connector.device_id.ilike(term),
                Connector.display_name.ilike(term),
            )
        )
    statement = (
        select(AttendanceRepairJob)
        .join(Connector, AttendanceRepairJob.connector_id == Connector.id)
        .where(*clauses)
    )
    if cursor:
        statement = statement.where(AttendanceRepairJob.id < cursor)
    fetched = list(
        db.scalars(statement.order_by(AttendanceRepairJob.id.desc()).limit(limit + 1)).all()
    )
    rows = fetched[:limit]
    return {
        "preview_enabled": settings.attendance_repair_preview_enabled,
        "execution_enabled": settings.attendance_repair_execution_enabled,
        "rows": [serialize_repair_job(db, row, include_targets=False) for row in rows],
        "next_cursor": rows[-1].id if len(fetched) > limit and rows else None,
        "totals": {
            "all": int(db.scalar(select(func.count(AttendanceRepairJob.id))) or 0),
            "active": int(
                db.scalar(
                    select(func.count(AttendanceRepairJob.id)).where(
                        AttendanceRepairJob.status.not_in(JOB_TERMINAL_STATES)
                    )
                )
                or 0
            ),
            "attention": int(
                db.scalar(
                    select(func.count(AttendanceRepairJob.id)).where(
                        AttendanceRepairJob.status.in_(
                            ["NEEDS_ATTENTION", "COMPLETED_WITH_ATTENTION"]
                        )
                    )
                )
                or 0
            ),
        },
        "worker": repair_worker_metrics(db),
    }


@router.get("/api/v1/attendance-repairs/{job_id}")
def get_attendance_repair(
    job_id: str,
    include_items: bool = True,
    item_cursor: int | None = Query(default=None, ge=1),
    item_limit: int = Query(default=500, ge=1, le=500),
    auth: tuple[Session, AdminContext] = Depends(_admin),
):
    db, _context = auth
    return serialize_repair_job(
        db,
        _job(db, job_id),
        include_items=include_items,
        item_cursor=item_cursor,
        item_limit=item_limit,
    )


@router.post("/api/v1/attendance-repairs/{job_id}/approve")
async def approve_attendance_repair(
    job_id: str,
    body: RepairApproveRequest,
    auth: tuple[Session, AdminContext] = Depends(_admin_mutation),
):
    db, context = auth
    require_step_up(body.password.get_secret_value(), db, context)
    try:
        job = approve_repair_job(
            db,
            job=_job(db, job_id),
            actor=context.username,
            reason=body.reason,
            typed_confirmation=body.typed_confirmation,
            preview_digest=body.preview_digest,
            idempotency_key=body.idempotency_key,
        )
        db.commit()
    except RepairError as error:
        db.rollback()
        _raise_repair(error)
    await browser_events.publish("attendance_repair", {"job_id": job.job_id, "status": job.status})
    return serialize_repair_job(db, job, include_items=True)


@router.post("/api/v1/attendance-repairs/{job_id}/{action}")
async def control_attendance_repair(
    job_id: str,
    action: Literal["pause", "resume", "cancel", "retry"],
    body: RepairControlRequest,
    auth: tuple[Session, AdminContext] = Depends(_admin_mutation),
):
    db, context = auth
    require_step_up(body.password.get_secret_value(), db, context)
    try:
        job = control_repair_job(
            db,
            job=_job(db, job_id),
            action=action,
            actor=context.username,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
        db.commit()
    except RepairError as error:
        db.rollback()
        _raise_repair(error)
    await browser_events.publish("attendance_repair", {"job_id": job.job_id, "status": job.status})
    return serialize_repair_job(db, job, include_items=True)


@router.get("/api/v1/attendance-repairs/{job_id}/evidence")
def download_attendance_repair_evidence(
    job_id: str,
    auth: tuple[Session, AdminContext] = Depends(_admin),
):
    db, _context = auth
    job = _job(db, job_id)
    response = StreamingResponse(
        stream_repair_evidence(job.job_id),
        media_type="application/json",
    )
    response.headers["Content-Disposition"] = (
        f'attachment; filename="attendance-repair-{job.job_id}.json"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response
