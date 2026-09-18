"""ADD-owned retained-source jobs using serial seeks, never ZKT ordinals."""

from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, JSON, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.hikvision_delivery import HikvisionPolicy
from zk_add.hikvision_evidence import HikvisionEvidence, ObservationIn, preserve_observation
from zk_add.hikvision_history import (
    SerialCheckpoint,
    CoverageError,
    record_digest,
    coverage_certificate,
)
from zk_add.models import ReconciliationJob, AttendanceEvent, DeviceUser, DeviceUserSnapshot
from zk_add.time_utils import utc_now, ensure_utc

MODE = "HIKVISION_SERIAL_HISTORY"
ACTIVE_SCOPE = "ACTIVE_USERS"
# This exact profile must pass the live employee-filter/serial-seek qualification.
EMPLOYEE_FILTER_PROFILE = "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1"


def active_user_snapshot(session, connector):
    terminal = connector.zkt_device
    policy = session.get(HikvisionPolicy, connector.id)
    if not policy or policy.profile_id != EMPLOYEE_FILTER_PROFILE:
        raise ValueError("Active-user history is not qualified for this terminal profile.")
    snapshot = (
        session.get(DeviceUserSnapshot, terminal.identity_snapshot_id)
        if terminal and terminal.identity_snapshot_id
        else None
    )
    if (
        not terminal
        or not terminal.snapshot_complete
        or not terminal.identity_snapshot_stable
        or not snapshot
        or not snapshot.complete
        or not snapshot.stable
        or snapshot.zkt_device_id != terminal.id
        or snapshot.revision != terminal.identity_snapshot_revision
        or utc_now() - ensure_utc(snapshot.received_at) > timedelta(minutes=15)
    ):
        raise ValueError(
            "Refresh terminal users: a complete, stable snapshot from the last 15 minutes is required."
        )
    users = list(
        session.scalars(
            select(DeviceUser)
            .where(
                DeviceUser.zkt_device_id == terminal.id,
                DeviceUser.present.is_(True),
                DeviceUser.lifecycle_state == "ACTIVE",
                DeviceUser.snapshot_revision == snapshot.revision,
            )
            .order_by(DeviceUser.user_id)
        )
    )
    employees = [u.user_id for u in users]
    if (
        not employees
        or len(employees) != snapshot.user_count
        or len(set(employees)) != len(employees)
        or any(not e.isascii() or not e.isdigit() or len(e) > 32 for e in employees)
    ):
        raise ValueError("The active-user snapshot is empty or incomplete; refresh terminal users.")
    return {
        "scope": ACTIVE_SCOPE,
        "employee_numbers": employees,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_revision": snapshot.revision,
        "snapshot_received_at": ensure_utc(snapshot.received_at).isoformat(),
        "snapshot_digest": record_digest({"employee_numbers": employees}),
        "user_index": 0,
        "completed_records": 0,
        "user_certificates": [],
        "phase": "SCOPE_FIRST",
    }


def _finish_active_user(job, data, certificate):
    employee = data["employee_numbers"][data["user_index"]]
    certificates = list(data["user_certificates"])
    certificates.append({"employee_number": employee, **certificate})
    data["user_certificates"] = certificates
    data["completed_records"] += certificate["record_count"]
    data["user_index"] += 1
    job.scanned_count = job.add_durable_count = data["completed_records"]
    if data["user_index"] == len(data["employee_numbers"]):
        job.cutoff_count = data["completed_records"]
        job.capture_certificate = {
            "source_protocol": "hikvision-isapi-v1",
            "scope": ACTIVE_SCOPE,
            "search_strategy": "employee-filtered-serial-seek-v1",
            "snapshot_id": data["snapshot_id"],
            "snapshot_digest": data["snapshot_digest"],
            "employee_numbers": data["employee_numbers"],
            "user_count": data["user_index"],
            "cutoff_serial": data["scope_cutoff"],
            "record_count": data["completed_records"],
            "user_certificates": certificates,
            "oracle_assurance": "NOT_EVALUATED",
            "excluded_history": "Employees outside the frozen active-user snapshot were not scanned.",
        }
        data["phase"] = "SCOPE_VERIFY"
    else:
        for key in (
            "first_serial",
            "last_serial",
            "initial_count",
            "first_anchor",
            "last_anchor",
            "checkpoint",
            "first_pass",
            "second_pass",
        ):
            data.pop(key, None)
        data["phase"] = "FIRST"


class HikvisionReconciliationState(Base):
    __tablename__ = "add_hikvision_reconciliation_states"
    job_id: Mapped[int] = mapped_column(ForeignKey("add_reconciliation_jobs.id"), primary_key=True)
    source_epoch: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class HikvisionReconciliationPage(Base):
    __tablename__ = "add_hikvision_reconciliation_pages"
    __table_args__ = (UniqueConstraint("job_id", "token", name="uq_hikvision_job_page_token"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("add_reconciliation_jobs.id"), index=True)
    token: Mapped[str] = mapped_column(String(36))
    response_digest: Mapped[str] = mapped_column(String(64))
    evidence_ids: Mapped[list] = mapped_column(JSON)
    phase: Mapped[str] = mapped_column(String(32))


def preflight(session, connector):
    from zk_add.settings import settings

    hard, waiting = [], []
    terminal = connector.zkt_device
    policy = session.get(HikvisionPolicy, connector.id)
    if not settings.reconciliation_enabled or not connector.active:
        hard.append(
            {"code": "FEATURE_DISABLED", "message": "Reconciliation or connector is disabled."}
        )
    if (
        not terminal
        or not terminal.confirmed_serial
        or terminal.serial != terminal.confirmed_serial
    ):
        hard.append(
            {
                "code": "TERMINAL_BINDING_REQUIRED",
                "message": "A verified terminal binding is required.",
            }
        )
    if connector.lifecycle_state == "QUARANTINED_DUPLICATE_SERIAL":
        hard.append(
            {
                "code": "DUPLICATE_SERIAL_QUARANTINE",
                "message": "Resolve the duplicate terminal binding.",
            }
        )
    if (
        not policy
        or not policy.enabled
        or not policy.source_epoch
        or not terminal
        or policy.terminal_serial != terminal.confirmed_serial
    ):
        hard.append(
            {
                "code": "HIKVISION_POLICY_REQUIRED",
                "message": "Approve the Hikvision source profile first.",
            }
        )
    if not connector.connected or not terminal or not terminal.online:
        waiting.append(
            {"code": "WAITING_FOR_DEVICE", "message": "Waiting for the terminal and ESP."}
        )
    try:
        snapshot = active_user_snapshot(session, connector)
        active_scope = {
            "eligible": not hard,
            "user_count": len(snapshot["employee_numbers"]),
            "snapshot_received_at": snapshot["snapshot_received_at"],
            "reason": None,
        }
    except ValueError as exc:
        active_scope = {"eligible": False, "user_count": None, "reason": str(exc)}
    return {
        "active_user_history": active_scope,
        "eligible": not hard,
        "ready_now": not hard and not waiting,
        "hard_blockers": hard,
        "waitable_blockers": waiting,
        "connector": {"connector_id": connector.connector_id, "device_id": connector.device_id},
        "terminal": {
            "serial": terminal.serial,
            "model": terminal.model,
            "connection_state": terminal.connection_state,
            "attendance_count": terminal.attendance_count,
            "user_count": terminal.user_count,
            "range_resume_verified": False,
            "serial_search_enabled": bool(policy and policy.enabled),
        }
        if terminal
        else None,
        "coverage": None,
        "source_protocol": "hikvision-isapi-v1",
    }


def initialize(session, job, connector, scope="ALL_RECORDS"):
    data = active_user_snapshot(session, connector) if scope == ACTIVE_SCOPE else {"phase": "FIRST"}
    policy = session.get(HikvisionPolicy, connector.id)
    job.mode = MODE
    session.add(
        HikvisionReconciliationState(job_id=job.id, source_epoch=policy.source_epoch, data=data)
    )


def assignment(session, job, connector):
    state = session.get(HikvisionReconciliationState, job.id)
    data = dict(state.data)
    if data.get("phase") == "SEALED":
        return None
    if not data.get("request"):
        phase = data["phase"]
        request = {
            "AcsEventCond": {
                "searchID": uuid4().hex,
                "searchResultPosition": 0,
                "maxResults": 1,
                "major": 0,
                "minor": 0,
                "picEnable": False,
            }
        }
        cond = request["AcsEventCond"]
        if phase == "SCOPE_LAST":
            cond["searchResultPosition"] = data["scope_initial_count"] - 1
        elif phase == "LAST":
            cond["searchResultPosition"] = data["initial_count"] - 1
        elif phase in {"SCAN1", "SCAN2"}:
            request = SerialCheckpoint(**data["checkpoint"]).request(20)
        elif phase == "COUNT":
            cond.update(beginSerialNo=data["first_serial"], endSerialNo=data["last_serial"])
        elif phase == "VERIFY_FIRST":
            # Verify the retained lower boundary, not merely the old serial's existence.
            pass
        elif phase == "VERIFY_LAST":
            cond.update(beginSerialNo=data["last_serial"], endSerialNo=data["last_serial"])
        if data.get("scope") == ACTIVE_SCOPE and phase not in {
            "SCOPE_FIRST",
            "SCOPE_LAST",
            "SCOPE_VERIFY",
            "SCOPE_EMPTY_VERIFY",
        }:
            cond = request["AcsEventCond"]
            cond["employeeNoString"] = data["employee_numbers"][data["user_index"]]
            cond.setdefault("beginSerialNo", 1)
            cond.setdefault("endSerialNo", data["scope_cutoff"])
        data.update(request=request, token=str(uuid4()))
        state.data = data
    job.status = "RUNNING"
    job.phase = "SCANNING_TERMINAL"
    job.started_at = job.started_at or utc_now()
    job.next_retry_at = utc_now() + timedelta(seconds=5)
    return {
        "type": "hikvision_history_assignment",
        "job_id": job.job_id,
        "token": data["token"],
        "terminal_serial": job.terminal_serial,
        "source_epoch": state.source_epoch,
        "request": data["request"],
    }


def apply_page(session: Session, connector, payload: dict):
    job = session.scalar(
        select(ReconciliationJob)
        .where(
            ReconciliationJob.job_id == payload.get("job_id"),
            ReconciliationJob.connector_id == connector.id,
        )
        .with_for_update()
    )
    if not job or job.mode != MODE or connector.firmware_family != "hikvision":
        raise ValueError("HIKVISION_JOB_BINDING_MISMATCH")
    state = session.get(HikvisionReconciliationState, job.id)
    if (
        payload.get("terminal_serial") != job.terminal_serial
        or connector.zkt_device.confirmed_serial != job.terminal_serial
        or payload.get("source_epoch") != state.source_epoch
    ):
        raise ValueError("HIKVISION_SOURCE_BINDING_MISMATCH")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise ValueError("HIKVISION_INVALID_PAGE")
    digest = record_digest(response)
    token = payload.get("token")
    old = session.scalar(
        select(HikvisionReconciliationPage).where(
            HikvisionReconciliationPage.job_id == job.id, HikvisionReconciliationPage.token == token
        )
    )
    if old:
        if old.response_digest != digest:
            raise ValueError("HIKVISION_PAGE_REPLAY_CONFLICT")
        return {"job_id": job.job_id, "token": token, "durable": True}
    if job.status not in {"RUNNING", "QUEUED"}:
        raise ValueError("HIKVISION_JOB_NOT_RUNNABLE")
    data = dict(state.data)
    if not token or token != data.get("token"):
        raise ValueError("HIKVISION_ASSIGNMENT_EXPIRED")
    page = response.get("AcsEvent", {})
    request = data["request"]["AcsEventCond"]
    rows = page.get("InfoList", []) if isinstance(page, dict) else None
    count = page.get("numOfMatches") if isinstance(page, dict) else None
    total = page.get("totalMatches") if isinstance(page, dict) else None
    if (
        not isinstance(rows, list)
        or type(count) is not int
        or count != len(rows)
        or count > request["maxResults"]
        or type(total) is not int
        or not 0 <= count <= total <= 150000
        or page.get("searchID") != request["searchID"]
    ):
        raise CoverageError("INVALID_SOURCE_PAGE")
    phase = data["phase"]
    scoped = data.get("scope") == ACTIVE_SCOPE
    employee = request.get("employeeNoString")
    evidence_ids = []
    if employee is not None and any(
        not isinstance(row, dict)
        or row.get("employeeNoString") != employee
        or type(row.get("serialNo")) is not int
        or not request["beginSerialNo"] <= row["serialNo"] <= request["endSerialNo"]
        for row in rows
    ):
        job.status, job.phase = "NEEDS_ATTENTION", "SAFETY_HOLD"
        job.review_required, job.error_code = True, "EMPLOYEE_FILTER_OR_SCOPE_MISMATCH"
        job.wait_reason = "SOURCE_COVERAGE_REVIEW"
        data.pop("request", None)
        data.pop("token", None)
        state.data = data
        # Retain the response digest but never release an out-of-scope observation.
        session.add(
            HikvisionReconciliationPage(
                job_id=job.id, token=token, response_digest=digest, evidence_ids=[], phase=phase
            )
        )
        session.flush()
        return {"job_id": job.job_id, "token": token, "durable": True}
    # Preserve raw observations before interpreting record fields. Invalid-time or
    # identity records remain evidenced and do not stop later valid source records.
    for raw in (
        [] if phase in {"SCOPE_FIRST", "SCOPE_LAST", "SCOPE_VERIFY", "SCOPE_EMPTY_VERIFY"} else rows
    ):
        text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        sha = hashlib.sha256(text.encode()).hexdigest()
        preserve_observation(
            session,
            connector,
            ObservationIn(
                schema_version=1,
                source_protocol="hikvision-isapi-v1",
                terminal_serial=job.terminal_serial,
                source_epoch=state.source_epoch,
                observation_sha256=sha,
                channel="HISTORY",
                raw=text,
                captured_epoch=int(utc_now().timestamp()),
            ),
        )
        evidence_ids.append(
            session.scalar(
                select(HikvisionEvidence.id).where(
                    HikvisionEvidence.connector_id == connector.id,
                    HikvisionEvidence.observation_sha256 == sha,
                )
            )
        )
    phase = data["phase"]
    try:
        if (
            phase in {"SCOPE_FIRST", "SCOPE_EMPTY_VERIFY"}
            and count == 0
            and total == 0
            and page.get("responseStatusStrg") == "NO MATCH"
        ):
            if phase == "SCOPE_FIRST":
                data["phase"] = "SCOPE_EMPTY_VERIFY"
            else:
                data["scope_cutoff"] = 0
                for _ in data["employee_numbers"]:
                    _finish_active_user(
                        job, data, {"record_count": 0, "matching_empty_observations": 2}
                    )
                job.capture_certified_at = utc_now()
                data["phase"] = "SEALED"
        elif phase == "SCOPE_VERIFY":
            if count != 1 or record_digest(rows[0]) != data["scope_first_anchor"]:
                raise CoverageError("RETAINED_BOUNDARY_CHANGED")
            job.capture_certified_at = utc_now()
            data["phase"] = "SEALED"
        elif phase == "SCOPE_FIRST":
            if count != 1 or type(rows[0].get("serialNo")) is not int:
                raise CoverageError("GLOBAL_BOUNDARY_NOT_AVAILABLE")
            data.update(
                scope_first=rows[0]["serialNo"],
                scope_first_anchor=record_digest(rows[0]),
                scope_initial_count=total,
                phase="SCOPE_LAST",
            )
        elif phase == "SCOPE_LAST":
            if (
                count != 1
                or type(rows[0].get("serialNo")) is not int
                or total < data["scope_initial_count"]
                or rows[0]["serialNo"] < data["scope_first"]
            ):
                raise CoverageError("GLOBAL_BOUNDARY_CHANGED")
            data.update(scope_cutoff=rows[0]["serialNo"], phase="FIRST")
        elif phase in {"SCAN1", "SCAN2"}:
            _, checkpoint = SerialCheckpoint(**data["checkpoint"]).stage_page(
                data["request"], response
            )
            data["checkpoint"] = asdict(checkpoint)
            base = data.get("completed_records", 0)
            job.scanned_count = base + (
                checkpoint.committed_count if phase == "SCAN1" else checkpoint.retained_count
            )
            if phase == "SCAN1":
                job.add_durable_count = base + checkpoint.committed_count
            if checkpoint.enumeration_complete:
                if phase == "SCAN1":
                    data["first_pass"] = asdict(checkpoint)
                    data["checkpoint"] = asdict(
                        SerialCheckpoint(
                            checkpoint.first_serial,
                            checkpoint.cutoff_serial,
                            checkpoint.retained_count,
                        )
                    )
                    data["phase"] = "SCAN2"
                else:
                    data["second_pass"] = asdict(checkpoint)
                    data["phase"] = "VERIFY_FIRST"
        elif (
            phase in {"FIRST", "VERIFY_EMPTY"}
            and count == 0
            and total == 0
            and page.get("responseStatusStrg") == "NO MATCH"
        ):
            if phase == "FIRST":
                data["phase"] = "VERIFY_EMPTY"
                if not scoped:
                    job.cutoff_count = 0
            else:
                certificate = {
                    "source_protocol": "hikvision-isapi-v1",
                    "record_count": 0,
                    "matching_empty_observations": 2,
                    "oracle_assurance": "NOT_EVALUATED",
                }
                if scoped:
                    _finish_active_user(job, data, certificate)
                else:
                    job.capture_certificate = certificate
                    job.capture_certified_at = utc_now()
                    data["phase"] = "SEALED"
        else:
            if (
                count != 1
                or not isinstance(rows[0], dict)
                or type(rows[0].get("serialNo")) is not int
            ):
                raise CoverageError("BOUNDARY_NOT_AVAILABLE")
            serial = rows[0]["serialNo"]
            if phase == "FIRST":
                data.update(
                    first_serial=serial,
                    initial_count=total,
                    first_anchor=record_digest(rows[0]),
                    phase="LAST",
                )
            elif phase == "LAST":
                if total < data["initial_count"] or serial < data["first_serial"]:
                    raise CoverageError("BOUNDARY_CHANGED")
                data.update(last_serial=serial, last_anchor=record_digest(rows[0]), phase="COUNT")
            elif phase == "COUNT":
                if serial != data["first_serial"] or total != data["initial_count"]:
                    raise CoverageError("BOUNDARY_CHANGED")
                data.update(
                    checkpoint=asdict(
                        SerialCheckpoint(data["first_serial"], data["last_serial"], total)
                    ),
                    phase="SCAN1",
                )
                if not scoped:
                    job.cutoff_count = total
            elif phase == "VERIFY_FIRST":
                if serial != data["first_serial"] or record_digest(rows[0]) != data["first_anchor"]:
                    raise CoverageError("RETAINED_BOUNDARY_CHANGED")
                data["phase"] = "VERIFY_LAST"
            elif phase == "VERIFY_LAST":
                certificate = coverage_certificate(
                    SerialCheckpoint(**data["first_pass"]),
                    SerialCheckpoint(**data["second_pass"]),
                    first_anchor_before=data["first_anchor"],
                    first_anchor_after=data["first_anchor"],
                    last_anchor_before=data["last_anchor"],
                    last_anchor_after=record_digest(rows[0]),
                )
                if scoped:
                    _finish_active_user(job, data, certificate)
                else:
                    job.capture_certificate = certificate
                    job.capture_certified_at = utc_now()
                    data["phase"] = "SEALED"
            else:
                raise CoverageError("UNKNOWN_SOURCE_PHASE")
    except CoverageError as exc:
        job.status = "NEEDS_ATTENTION"
        job.phase = "SAFETY_HOLD"
        job.review_required = True
        job.error_code = str(exc)
        job.wait_reason = "SOURCE_COVERAGE_REVIEW"
        data.pop("request", None)
        data.pop("token", None)
        state.data = data
        # Evidence and the failed page commit, but the source checkpoint never advances.
    else:
        data.pop("request", None)
        data.pop("token", None)
        state.data = data
        job.next_retry_at = None
        job.last_progress_at = utc_now()
    session.add(
        HikvisionReconciliationPage(
            job_id=job.id,
            token=token,
            response_digest=digest,
            evidence_ids=evidence_ids,
            phase=phase,
        )
    )
    session.flush()
    refresh_assurance(session, job)
    return {"job_id": job.job_id, "token": token, "durable": True}


def refresh_assurance(session, job):
    if not job.capture_certified_at or job.status in {
        "PAUSED",
        "NEEDS_ATTENTION",
        "CANCELLED",
        "INVALIDATED",
    }:
        return job
    # Membership comes from committed pages, never a serial gap or event-count guess.
    ids = set()
    for page in session.scalars(
        select(HikvisionReconciliationPage).where(
            HikvisionReconciliationPage.job_id == job.id,
            HikvisionReconciliationPage.phase == "SCAN1",
        )
    ):
        ids.update(page.evidence_ids)
    # Bounded SQL batches; do not issue a 150k-ID IN expression.
    confirmed = pending = holds = targets = 0
    ordered = sorted(ids)
    for offset in range(0, len(ordered), 500):
        evidence_batch = list(
            session.scalars(
                select(HikvisionEvidence).where(
                    HikvisionEvidence.id.in_(ordered[offset : offset + 500])
                )
            )
        )
        uids = [e.event_uid for e in evidence_batch if e.event_uid]
        attendance = {
            r.event_uid: r
            for r in session.scalars(
                select(AttendanceEvent).where(AttendanceEvent.event_uid.in_(uids))
            )
        }
        for evidence in evidence_batch:
            if evidence.disposition == "NON_ATTENDANCE":
                continue
            row = attendance.get(evidence.event_uid)
            if not row or row.ords_status.startswith(("BLOCKED", "QUARANTINED")):
                holds += 1
            else:
                targets += 1
                if row.oracle_confirmed_at:
                    confirmed += 1
                else:
                    pending += 1
    job.ords_target_count = targets
    job.ords_confirmed_count = confirmed
    job.ords_pending_count = pending
    job.ords_review_count = holds
    job.review_required = bool(holds)
    job.phase = "ORACLE_ASSURANCE"
    job.status = "RUNNING"
    if not holds and not pending and len(ids) == job.cutoff_count:
        job.oracle_certificate = {
            "source_protocol": "hikvision-isapi-v1",
            "scope": (job.capture_certificate or {}).get("scope", "ALL_RECORDS"),
            "confirmed": confirmed,
            "non_attendance": len(ids) - targets,
        }
        job.oracle_certified_at = job.completed_at = utc_now()
        job.status = "COMPLETED"
        job.phase = "COMPLETED"
        job.completion_outcome = "ALL_CONFIRMED"
    return job
