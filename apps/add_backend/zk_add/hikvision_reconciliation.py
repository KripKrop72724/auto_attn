"""ADD-owned retained-source jobs using serial seeks, never ZKT ordinals."""

from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import (BigInteger, ForeignKey, Integer, JSON, String, UniqueConstraint, cast, exists, func,
                        select)
from sqlalchemy.orm import Mapped, Session, aliased, mapped_column

from zk_add.db import Base
from zk_add.hikvision_delivery import HikvisionPolicy
from zk_add.hikvision_evidence import HikvisionEvidence, ObservationIn, preserve_observation
from zk_add.hikvision_history import (
    SerialCheckpoint,
    CoverageError,
    record_digest,
    coverage_certificate,
)
from zk_add.models import (
    AttendanceEvent,
    AttendanceSourceCorrection,
    ReconciliationJob,
    DeviceUser,
    DeviceUserSnapshot,
)
from zk_add.ords_states import classify_ords_assurance_status
from zk_add.time_utils import utc_now, ensure_utc, parse_datetime

MODE = "HIKVISION_SERIAL_HISTORY"
ACTIVE_SCOPE = "ACTIVE_USERS"
# Recent-tail recovery: resume from the last serial ADD holds through gap-free
# custody (the ESP's checkpointed poll, or an earlier tail job), then keep
# following the terminal tail. It preserves records; it never certifies coverage.
TAIL_SCOPE = "RECENT_TAIL"
TAIL_PHASE = "TAIL"
# Re-read records ADD already holds. The evidence event_uid/facts check then
# proves, or flags for review, whether the terminal kept those serials.
TAIL_OVERLAP_RECORDS = 20
TAIL_PAGE_RECORDS = 20
TAIL_FOLLOW_SECONDS = 15
# A first page whose earliest record ADD did not hold with the same facts steps
# back one page at a time, at most this often, to find where continuity holds.
TAIL_MAX_STEPBACKS = 10
TAIL_LISTED_BREAKS = 5
TAIL_RECENT_REQUESTS = 4
MAX_SOURCE_SERIAL = 3_000_000_000
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


def terminal_answering(terminal, now=None) -> bool:
    """The ESP's latest check reached the terminal's event search with HTTP 200.

    Firmware reports the terminal OFFLINE whenever a poll fails, including a
    saved-anchor mismatch at the events stage. That failure needs an
    authenticated, serial-verified response, so the terminal itself answers.
    Every history page is identity-checked again by the ESP before it runs.
    """
    from zk_add.settings import settings

    if terminal is None or terminal.last_seen_at is None:
        return False
    age = ((now or utc_now()) - ensure_utc(terminal.last_seen_at)).total_seconds()
    health = (terminal.capability_profile or {}).get("hikvision_health") or {}
    return (0 <= age <= settings.offline_after_seconds and health.get("poll_stage") == "events"
            and health.get("poll_http_status") == 200 and health.get("poll_error") in {0, 6})


def _poll_custody(model, connector, policy) -> tuple:
    return (
        model.connector_id == connector.id,
        model.terminal_serial == policy.terminal_serial,
        model.source_epoch == policy.source_epoch,
        model.channel == "POLL",
        model.event_uid.is_not(None),
    )


def _poll_breaks(session, connector, policy) -> tuple[int | None, list[int]]:
    """ADD's newest POLL serial and the serials after which its next one is missing.

    The poll reads every event serial in order, so a missing next serial below
    its newest record means its checkpoint restarted further on (a reinstalled
    or reset ESP starts at the terminal's newest records). Newest break first.
    """
    serial = cast(HikvisionEvidence.source_event_id, BigInteger)
    top = session.scalar(select(func.max(serial)).where(*_poll_custody(HikvisionEvidence, connector, policy)))
    if not top:
        return None, []
    following = aliased(HikvisionEvidence)
    breaks = session.scalars(select(serial).distinct().where(
        *_poll_custody(HikvisionEvidence, connector, policy),
        serial < top,
        ~exists().where(*_poll_custody(following, connector, policy),
                        cast(following.source_event_id, BigInteger) == serial + 1),
    ).order_by(serial.desc()).limit(TAIL_LISTED_BREAKS + 1)).all()
    return int(top), [int(value) for value in breaks]


def _tail_resume(session, connector, policy) -> tuple[int | None, list[int], int | None]:
    """The resume point, the earlier POLL breaks it leaves behind, and ADD's newest POLL serial.

    Only the ESP's checkpointed POLL channel and earlier tail jobs qualify. A
    live stream can drop events while disconnected, and employee-filtered
    history is not contiguous, so neither may move the resume point forward.
    Resume at the latest POLL break: anything after it may never have been
    read. Earlier breaks are listed for the operator; only a full terminal audit
    covers them.
    """
    top, breaks = _poll_breaks(session, connector, policy)
    if not top:
        return None, [], None
    best = breaks[0] if breaks else top
    spans = []
    for data, epoch, serial in session.execute(
        select(HikvisionReconciliationState.data, HikvisionReconciliationState.source_epoch,
               ReconciliationJob.terminal_serial)
        .join(HikvisionReconciliationState, HikvisionReconciliationState.job_id == ReconciliationJob.id)
        .where(ReconciliationJob.connector_id == connector.id, ReconciliationJob.mode == MODE)
    ).all():
        data = data or {}
        begin, last = data.get("begin_serial"), data.get("last_serial")
        if (data.get("scope") == TAIL_SCOPE and epoch == policy.source_epoch and serial == policy.terminal_serial
                and type(begin) is int and type(last) is int and 0 < begin <= last):
            spans.append((begin, last))
    # An earlier tail job read every serial from its begin to its last, so it
    # extends custody only when it began at or before the current resume point.
    for begin, last in sorted(spans):
        if begin <= best + 1 and last > best:
            best = last
    return best, [value for value in breaks[1:] if value < best][:TAIL_LISTED_BREAKS], top


def recent_tail_scope(session, connector) -> dict:
    policy = session.get(HikvisionPolicy, connector.id)
    terminal = connector.zkt_device
    if (not policy or not policy.enabled or not policy.source_epoch or not terminal
            or policy.terminal_serial != terminal.confirmed_serial):
        raise ValueError("Approve the Hikvision source profile first.")
    anchor, earlier_breaks, top = _tail_resume(session, connector, policy)
    if not anchor:
        raise ValueError(
            "ADD holds no checkpointed live-poll record for this terminal; use a full terminal audit instead."
        )
    cursor = ((terminal.capability_profile or {}).get("hikvision_health") or {}).get("durable_poll_cursor")
    return {
        "scope": TAIL_SCOPE,
        "phase": TAIL_PHASE,
        "anchor_serial": anchor,
        "begin_serial": max(1, anchor - TAIL_OVERLAP_RECORDS + 1),
        "earlier_breaks": earlier_breaks,
        # Proof that the ESP poll works again needs a POLL record beyond
        # everything it (or its queue) could have read before it stalled.
        "poll_top_serial": max(top or 0, cursor if type(cursor) is int else 0, anchor),
        "boundary_serial": None,
        "stepbacks": 0,
        "last_serial": None,
        "last_facts": None,
        "pages": 0,
        "records": 0,
        "overlap_records": 0,
        "fact_conflicts": 0,
        "confirmed_conflicts": 0,
        "continuity_conflicts": 0,
        "anchor_returned": None,
        "caught_up": False,
        "caught_up_at": None,
        "follow_after": None,
        "last_page_at": None,
        "poll_ok_epoch": None,
        "recent_requests": [],
    }


def _immutable_facts(row: dict) -> list:
    return [row.get("serialNo"), row.get("major"), row.get("minor"),
            row.get("employeeNoString") or None, row.get("time")]


def _held_before(session, evidence_id, fresh: bool) -> str:
    """CONFLICT, MATCHED (ADD already held these exact facts) or NEW."""
    evidence = session.get(HikvisionEvidence, evidence_id) if evidence_id else None
    if evidence is None or evidence.event_uid is None:
        return "NEW"
    if evidence.disposition == "SOURCE_FACT_CONFLICT":
        return "CONFLICT"
    if not fresh:
        return "MATCHED"
    earlier = session.scalar(select(HikvisionEvidence.id).where(
        HikvisionEvidence.connector_id == evidence.connector_id,
        HikvisionEvidence.event_uid == evidence.event_uid,
        HikvisionEvidence.id != evidence.id,
        HikvisionEvidence.immutable_digest == evidence.immutable_digest,
    ).limit(1))
    return "MATCHED" if earlier else "NEW"


def _live_poll_restored(session, connector, data, now) -> bool:
    """Positive proof that the ESP's own checkpointed poll captures again.

    A heartbeat sent before the first poll after an ESP boot reports
    poll_error 0 (its zero-initialised value) with the loaded, still
    mismatching cursor, so poll_error alone proves nothing. This needs a fresh
    heartbeat showing a successful events-stage poll with no failures, a
    successful-poll time that advanced since an earlier check (two separate
    successful polls), a durable cursor this job already covers, and a POLL
    record beyond anything the stalled poll could have read.
    """
    from zk_add.settings import settings

    terminal = connector.zkt_device
    health = ((terminal.capability_profile or {}) if terminal else {}).get("hikvision_health") or {}
    seen_at = terminal.last_seen_at if terminal else None
    epoch = health.get("last_successful_poll_epoch")
    healthy = (
        seen_at is not None
        and 0 <= (now - ensure_utc(seen_at)).total_seconds() <= settings.offline_after_seconds
        and health.get("poll_error") == 0
        and health.get("poll_stage") in {"events", "checkpoint_commit"}
        and health.get("poll_http_status") == 200
        and health.get("consecutive_poll_failures") == 0
        and type(epoch) is int and epoch > 0
    )
    if not healthy:
        data["poll_ok_epoch"] = None
        return False
    previous = data.get("poll_ok_epoch")
    if type(previous) is not int or epoch < previous:
        data["poll_ok_epoch"] = epoch
        return False
    if epoch == previous:
        return False
    cursor = health.get("durable_poll_cursor")
    if not (type(cursor) is int and data.get("last_serial") and 0 < cursor <= data["last_serial"]):
        return False
    policy = session.get(HikvisionPolicy, connector.id)
    if not policy:
        return False
    serial = cast(HikvisionEvidence.source_event_id, BigInteger)
    return session.scalar(select(HikvisionEvidence.id).where(
        *_poll_custody(HikvisionEvidence, connector, policy),
        serial > (data.get("poll_top_serial") or data["anchor_serial"]),
    ).limit(1)) is not None


def _apply_tail_page(session, connector, job, data, request, page, rows, count, total, evidence_ids, fresh):
    """One open-ended serial page. Validation failures raise CoverageError.

    Returns True for an idle follow page (caught up, nothing new), which needs
    no page row and is not progress.
    """
    begin, end = request["beginSerialNo"], request["endSerialNo"]
    status = page.get("responseStatusStrg")
    complete = count == total
    if (complete and status not in ({"OK", "NO MATCH"} if count == 0 else {"OK"})) or (
            not complete and (status != "MORE" or count == 0)):
        raise CoverageError("INCONSISTENT_SEARCH_COMPLETION")
    previous = begin - 1
    for row in rows:
        serial = row.get("serialNo") if isinstance(row, dict) else None
        if type(serial) is not int or not begin <= serial <= end or serial <= previous:
            raise CoverageError("SERIAL_REUSE_ORDER_OR_SCOPE_CONFLICT")
        previous = serial
    now = utc_now()
    data["pages"] = data.get("pages", 0) + 1
    data["last_page_at"] = now.isoformat()
    last = data.get("last_serial")
    anchor = data["anchor_serial"]
    if last is None:
        # The first page must still show the serials ADD resumed from. An empty
        # or later-starting answer means the terminal no longer retains them
        # (cleared, replaced or renumbered): continuity cannot be shown.
        if not rows or rows[0]["serialNo"] > anchor:
            raise CoverageError("TAIL_RESUME_POINT_NOT_RETAINED")
        # Its earliest record must be one ADD already held with the same facts.
        # Otherwise the terminal may have reused serials from below the resume
        # point, and new punches there would never be read: step back a page.
        if _held_before(session, evidence_ids[0], fresh[0]) != "MATCHED":
            if data.get("stepbacks", 0) >= TAIL_MAX_STEPBACKS or data["begin_serial"] <= 1:
                raise CoverageError("TAIL_REUSE_BEFORE_RESUME_POINT")
            data["stepbacks"] = data.get("stepbacks", 0) + 1
            data["begin_serial"] = max(1, data["begin_serial"] - TAIL_PAGE_RECORDS)
            data["follow_after"] = None
            return False
        data["boundary_serial"] = rows[0]["serialNo"]
        data["anchor_returned"] = any(row["serialNo"] == anchor for row in rows)
    elif rows and rows[0]["serialNo"] == last and _immutable_facts(rows[0]) != data.get("last_facts"):
        # Inclusive terminals return the predecessor again. Changed facts mean
        # the serial was reused; the evidence layer already holds both records.
        data["continuity_conflicts"] = data.get("continuity_conflicts", 0) + 1
    # Count each serial once: an inclusive terminal returns the previous page's
    # last record again. Re-read serials up to the anchor are not new punches.
    high_water = last if last is not None else -1
    first_read = [(row, evidence_id) for row, evidence_id in zip(rows, evidence_ids)
                  if row["serialNo"] > high_water]
    if not complete and not first_read:
        raise CoverageError("TAIL_PAGE_NO_PROGRESS")
    overlap = sum(1 for row, _ in first_read if row["serialNo"] <= anchor)
    new_ids = [evidence_id for _, evidence_id in first_read if evidence_id]
    conflicted = session.scalars(select(HikvisionEvidence.event_uid).where(
        HikvisionEvidence.id.in_(new_ids),
        HikvisionEvidence.disposition == "SOURCE_FACT_CONFLICT",
    )).all() if new_ids else []
    if conflicted:
        data["fact_conflicts"] = data.get("fact_conflicts", 0) + len(conflicted)
        data["confirmed_conflicts"] = data.get("confirmed_conflicts", 0) + (session.scalar(
            select(func.count(AttendanceEvent.id)).where(
                AttendanceEvent.event_uid.in_([uid for uid in conflicted if uid]),
                AttendanceEvent.oracle_confirmed_at.is_not(None),
            )) or 0)
    if data.get("fact_conflicts") or data.get("continuity_conflicts"):
        job.review_required = True
        confirmed = data.get("confirmed_conflicts", 0)
        job.error_message = (
            f"{data.get('fact_conflicts', 0) + data.get('continuity_conflicts', 0):,} terminal serial(s) "
            "now carry different facts than the record ADD already held; both versions are preserved "
            "and held for source review."
            + (f" {confirmed:,} earlier version(s) were already confirmed in Oracle and keep that status."
               if confirmed else "")
        )
    if rows:
        data["last_serial"] = max(rows[-1]["serialNo"], high_water)
        data["last_facts"] = _immutable_facts(rows[-1])
    data["overlap_records"] = data.get("overlap_records", 0) + overlap
    data["records"] = data.get("records", 0) + len(first_read) - overlap
    job.scanned_count = job.add_durable_count = data["overlap_records"] + data["records"]
    data["caught_up"] = complete
    if complete:
        data["caught_up_at"] = now.isoformat()
        data["follow_after"] = (now + timedelta(seconds=TAIL_FOLLOW_SECONDS)).isoformat()
    else:
        data["follow_after"] = None
    if complete and _live_poll_restored(session, connector, data, now):
        # The ESP's own checkpointed poll works again and resumes at or below
        # what this job already holds, so ending here leaves no gap.
        data["phase"] = "SEALED"
        job.status = job.phase = "COMPLETED"
        job.completion_outcome = "LIVE_POLL_RESTORED"
        job.completed_at = now
    return last is not None and not first_read


def tail_waiting_to_follow(session, job, *, paced: bool = False) -> bool:
    """A tail job between checks asks nothing of the ESP, so it holds no scan slot.

    Following one terminal's tail for days therefore never delays another
    device's reconciliation. While the ORDS history backlog is over its pause
    limit (paced), a catch-up also reads at most one page per follow interval.
    """
    state = session.get(HikvisionReconciliationState, job.id)
    data = (state.data or {}) if state else {}
    if data.get("scope") != TAIL_SCOPE or data.get("phase") == "SEALED" or data.get("request"):
        return False
    now = utc_now()
    follow_after = data.get("follow_after")
    if follow_after and parse_datetime(follow_after) > now:
        job.status = "RUNNING"
        job.phase = "FOLLOWING_TERMINAL_TAIL"
        job.wait_reason = None
        return True
    last_page_at = data.get("last_page_at")
    if (paced and not data.get("caught_up") and last_page_at
            and parse_datetime(last_page_at) + timedelta(seconds=TAIL_FOLLOW_SECONDS) > now):
        job.status = "RUNNING"
        job.phase = "SCANNING_TERMINAL"
        job.wait_reason = None
        return True
    return False


def tail_summary(data: dict) -> dict:
    return {
        "anchor_serial": data.get("anchor_serial"),
        "begin_serial": data.get("begin_serial"),
        "boundary_serial": data.get("boundary_serial"),
        "earlier_breaks": data.get("earlier_breaks") or [],
        "last_serial": data.get("last_serial"),
        "anchor_returned": data.get("anchor_returned"),
        "pages": data.get("pages", 0),
        "records": data.get("records", 0),
        "overlap_records": data.get("overlap_records", 0),
        "fact_conflicts": data.get("fact_conflicts", 0),
        "confirmed_conflicts": data.get("confirmed_conflicts", 0),
        "continuity_conflicts": data.get("continuity_conflicts", 0),
        "caught_up": bool(data.get("caught_up")),
        "caught_up_at": data.get("caught_up_at"),
        "last_page_at": data.get("last_page_at"),
        "coverage_certified": False,
    }


def tail_operator_status(job, data: dict) -> tuple[str, str] | None:
    """Tail jobs never certify coverage; never show the certified wording."""
    held = data.get("fact_conflicts", 0) + data.get("continuity_conflicts", 0)
    review = (f" {held:,} changed terminal serial(s) remain held for source review." if held else "")
    if job.status == "COMPLETED":
        return ("TAIL_RECOVERY_ENDED",
                "Recent-tail recovery ended because the ESP's own checkpointed poll works again. "
                "No full-history coverage was certified." + review)
    if job.status in {"RUNNING", "QUEUED"} and data.get("caught_up"):
        return ("FOLLOWING_TERMINAL_TAIL",
                f"Caught up to terminal serial {data.get('last_serial')} with {data.get('records', 0):,} new "
                f"record(s) after serial {data.get('anchor_serial')}; ADD checks for new punches every "
                f"{TAIL_FOLLOW_SECONDS} seconds until it is cancelled or the ESP poll recovers. "
                "No full-history coverage is certified." + review)
    return None


def preflight(session, connector, scope=None):
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
    # Only a recent tail may run against a terminal that answers the ESP but
    # fails its saved-anchor check; every certifying scope needs it ONLINE.
    answering = bool(terminal and (terminal.online or terminal_answering(terminal)))
    if not connector.connected or not terminal or not (
            terminal.online or (scope == TAIL_SCOPE and answering)):
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
    # The scheduler re-runs this preflight every cycle for the device's active
    # job; only a device without one can start a tail, so skip the evidence scan.
    active_job = session.scalar(select(ReconciliationJob.id).where(
        ReconciliationJob.connector_id == connector.id,
        ReconciliationJob.status.not_in(["COMPLETED", "CANCELLED", "FAILED", "INVALIDATED"]),
    ).limit(1))
    try:
        if active_job is not None:
            raise ValueError("Finish or cancel this device's active reconciliation first.")
        tail = recent_tail_scope(session, connector)
        tail_scope = {"eligible": not hard, "ready_now": not hard and connector.connected and answering,
                      "anchor_serial": tail["anchor_serial"], "begin_serial": tail["begin_serial"],
                      "earlier_breaks": tail["earlier_breaks"], "reason": None}
    except ValueError as exc:
        tail_scope = {"eligible": False, "ready_now": False, "anchor_serial": None, "begin_serial": None,
                      "earlier_breaks": [], "reason": str(exc)}
    return {
        "active_user_history": active_scope,
        "recent_tail": tail_scope,
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
    if scope == TAIL_SCOPE:
        data = recent_tail_scope(session, connector)
    else:
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
    tail = data.get("scope") == TAIL_SCOPE
    if tail and not data.get("request"):
        follow_after = data.get("follow_after")
        if follow_after and parse_datetime(follow_after) > utc_now():
            job.status = "RUNNING"
            job.phase = "FOLLOWING_TERMINAL_TAIL"
            return None
        # Begin at the last record already seen: an inclusive terminal returns
        # it again and an exclusive one starts after it, so neither skips one.
        data.update(request={"AcsEventCond": {
            "searchID": uuid4().hex, "searchResultPosition": 0, "maxResults": TAIL_PAGE_RECORDS,
            "major": 0, "minor": 0, "picEnable": False,
            "beginSerialNo": data["last_serial"] or data["begin_serial"], "endSerialNo": MAX_SOURCE_SERIAL,
        }}, token=str(uuid4()))
        state.data = data
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
    job.phase = "FOLLOWING_TERMINAL_TAIL" if tail and data.get("caught_up") else "SCANNING_TERMINAL"
    job.started_at = job.started_at or utc_now()
    # A busy ESP takes an assignment only between its own polls; re-sending an
    # open-ended tail request too early just makes it run twice.
    job.next_retry_at = utc_now() + timedelta(seconds=20 if tail else 5)
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
    current = state.data or {}
    if current.get("scope") == TAIL_SCOPE and token and token != current.get("token"):
        recent = next((item for item in current.get("recent_requests") or [] if item.get("token") == token),
                      None)
        if old or recent:
            # An open-ended tail request executed twice (the ESP keeps one
            # queued copy) legitimately differs once new punches arrive. The
            # first answer already moved the job on; keep the second answer's
            # records as evidence without moving the job again.
            if recent and (old is None or old.response_digest != digest):
                _preserve_tail_replay(session, connector, job, state, recent["cond"], response)
            return {"job_id": job.job_id, "token": token, "durable": True}
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
    evidence_ids, fresh = _preserve_rows(
        session, connector, job, state,
        [] if phase in {"SCOPE_FIRST", "SCOPE_LAST", "SCOPE_VERIFY", "SCOPE_EMPTY_VERIFY"} else rows,
        current_after=data.get("anchor_serial") if phase == TAIL_PHASE else None,
    )
    phase = data["phase"]
    idle = False
    if phase == TAIL_PHASE:
        # A late second execution of this request is recognised by its token.
        data["recent_requests"] = [*(data.get("recent_requests") or [])[-(TAIL_RECENT_REQUESTS - 1):],
                                   {"token": token, "cond": request}]
    try:
        if phase == TAIL_PHASE:
            idle = _apply_tail_page(session, connector, job, data, request, page, rows, count, total,
                                    evidence_ids, fresh)
        elif (
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
        if not idle:
            job.last_progress_at = utc_now()
    if not idle:
        # An idle follow page (caught up, nothing new) adds no page row: a
        # following tail would otherwise write one every 15 seconds for days.
        # A late replay of it is still recognised through recent_requests.
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


def _preserve_rows(session, connector, job, state, rows, *, current_after=None):
    """Preserve each row as HISTORY evidence; returns evidence ids and whether each was new.

    Tail records after the resume point are the terminal's current punches,
    missed only because its own poll stalled; they use the current-reconcile
    ORDS lane instead of queueing behind retained-history backfill.
    """
    evidence_ids, fresh = [], []
    for raw in rows:
        text = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        sha = hashlib.sha256(text.encode()).hexdigest()
        lookup = select(HikvisionEvidence.id).where(
            HikvisionEvidence.connector_id == connector.id,
            HikvisionEvidence.observation_sha256 == sha,
        )
        existed = session.scalar(lookup)
        serial = raw.get("serialNo") if isinstance(raw, dict) else None
        current = current_after is not None and type(serial) is int and serial > current_after
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
            delivery_type="CURRENT_RECONCILE" if current else None,
        )
        evidence_ids.append(existed or session.scalar(lookup))
        fresh.append(existed is None)
    return evidence_ids, fresh


def _preserve_tail_replay(session, connector, job, state, cond, response):
    """Keep a second execution's records as evidence when they fit the original request."""
    page = response.get("AcsEvent")
    rows = page.get("InfoList") if isinstance(page, dict) else None
    count = page.get("numOfMatches") if isinstance(page, dict) else None
    if (not isinstance(rows, list) or type(count) is not int or count != len(rows)
            or count > cond.get("maxResults", 0) or page.get("searchID") != cond.get("searchID")):
        return
    previous = cond["beginSerialNo"] - 1
    for row in rows:
        serial = row.get("serialNo") if isinstance(row, dict) else None
        if type(serial) is not int or not cond["beginSerialNo"] <= serial <= cond["endSerialNo"] or serial <= previous:
            return
        previous = serial
    _preserve_rows(session, connector, job, state, rows,
                   current_after=(state.data or {}).get("anchor_serial"))
    session.flush()


def refresh_assurance(session, job):
    if not job.capture_certified_at or job.status in {
        "PAUSED",
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
    confirmed = pending = holds = identity_holds = review_holds = targets = corrected = 0
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
        corrections = {
            correction.hikvision_evidence_id: correction
            for correction in session.scalars(
                select(AttendanceSourceCorrection).where(
                    AttendanceSourceCorrection.hikvision_evidence_id.in_(
                        [evidence.id for evidence in evidence_batch]
                    ),
                    AttendanceSourceCorrection.status == "CREATED",
                    AttendanceSourceCorrection.derived_attendance_event_id.is_not(None),
                )
            ).all()
        }
        derived = {
            correction.derived_attendance_event_id: correction
            for correction in corrections.values()
        }
        if derived:
            for event in session.scalars(
                select(AttendanceEvent).where(
                    AttendanceEvent.id.in_(list(derived))
                )
            ).all():
                attendance[event.event_uid] = event
        for evidence in evidence_batch:
            if evidence.disposition == "NON_ATTENDANCE":
                continue
            correction = corrections.get(evidence.id)
            row = (
                session.get(AttendanceEvent, correction.derived_attendance_event_id)
                if correction
                else attendance.get(evidence.event_uid)
            )
            if correction:
                corrected += 1
            outcome = classify_ords_assurance_status(row.ords_status if row else None)
            if not row:
                holds += 1
                review_holds += 1
            elif outcome == "IDENTITY_HELD":
                holds += 1
                identity_holds += 1
            elif outcome == "REVIEW_REQUIRED":
                holds += 1
                review_holds += 1
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
    job.blocked_identity_count = identity_holds
    job.review_required = bool(review_holds)
    job.error_code = "ORACLE_TERMINAL_OUTCOME_REQUIRES_REVIEW" if holds else None
    job.error_message = (
        f"{holds:,} preserved Hikvision source event(s) remain held for identity or review."
        if holds
        else None
    )
    job.phase = "ORACLE_ASSURANCE"
    job.status = "NEEDS_ATTENTION" if holds else "RUNNING"
    if not review_holds and not pending and len(ids) == job.cutoff_count:
        job.oracle_certificate = {
            "source_protocol": "hikvision-isapi-v1",
            "scope": (job.capture_certificate or {}).get("scope", "ALL_RECORDS"),
            "confirmed": confirmed,
            "non_attendance": len(ids) - targets,
            "corrected_source_events": corrected,
        }
        job.oracle_certified_at = job.completed_at = utc_now()
        job.status = "COMPLETED"
        job.phase = "COMPLETED"
        job.error_code = None
        job.error_message = None
        job.completion_outcome = (
            "CERTIFIED_WITH_CORRECTED_SOURCE_EXCEPTIONS"
            if corrected
            else "COMPLETED_WITH_IDENTITY_HOLDS"
            if identity_holds
            else "ALL_CONFIRMED"
        )
    return job
