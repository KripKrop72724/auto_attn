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
from zk_add.models import ReconciliationJob, AttendanceEvent
from zk_add.time_utils import utc_now

MODE = "HIKVISION_SERIAL_HISTORY"


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
    return {
        "eligible": not hard,
        "ready_now": not hard and not waiting,
        "hard_blockers": hard,
        "waitable_blockers": waiting,
        "connector": {"connector_id": connector.connector_id, "device_id": connector.device_id},
        "terminal": {"serial": terminal.serial, "model": terminal.model,
                     "connection_state": terminal.connection_state,
                     "attendance_count": terminal.attendance_count, "user_count": terminal.user_count,
                     "range_resume_verified": False, "serial_search_enabled": bool(policy and policy.enabled)} if terminal else None,
        "coverage": None,
        "source_protocol": "hikvision-isapi-v1",
    }


def initialize(session, job, connector):
    policy = session.get(HikvisionPolicy, connector.id)
    job.mode = MODE
    session.add(
        HikvisionReconciliationState(
            job_id=job.id, source_epoch=policy.source_epoch, data={"phase": "FIRST"}
        )
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
        if phase == "LAST":
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
    evidence_ids = []
    # Preserve raw observations before interpreting record fields. Invalid-time or
    # identity records remain evidenced and do not stop later valid source records.
    for raw in rows:
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
        if phase in {"SCAN1", "SCAN2"}:
            _, checkpoint = SerialCheckpoint(**data["checkpoint"]).stage_page(
                data["request"], response
            )
            data["checkpoint"] = asdict(checkpoint)
            job.scanned_count = checkpoint.committed_count
            if phase == "SCAN1":
                job.add_durable_count = checkpoint.committed_count
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
                job.cutoff_count = 0
            else:
                job.capture_certificate = {
                    "source_protocol": "hikvision-isapi-v1",
                    "record_count": 0,
                    "matching_empty_observations": 2,
                    "oracle_assurance": "NOT_EVALUATED",
                }
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
    if not job.capture_certified_at or job.status in {"PAUSED", "NEEDS_ATTENTION", "CANCELLED", "INVALIDATED"}:
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
            "confirmed": confirmed,
            "non_attendance": len(ids) - targets,
        }
        job.oracle_certified_at = job.completed_at = utc_now()
        job.status = "COMPLETED"
        job.phase = "COMPLETED"
        job.completion_outcome = "ALL_CONFIRMED"
    return job
