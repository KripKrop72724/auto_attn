"""Automatic, paced release of "Unknown identity · CNIC on synced user" punches.

The owner's switch (ON unless an administrator turned it OFF with their
password and a reason) keeps ADD's one-click quick release running from the
worker, so these punches never pile up as a separate chore. A one-shot
"Release all now" does the same while the switch is OFF, until no untried
punch remains.

Every batch is an ordinary ``attendance_direct_ords.quick_release``: the same
audited Direct ORDS approval, the same per-punch checks, the same skips. This
module only decides when and which punches to hand it:

* Paced and bounded. One small batch (``batch_size``) at most every
  ``min_interval_seconds``, and none while this releaser's approved punches
  still waiting for Oracle reach ``in_flight_limit``, so the forced Oracle
  lane stays bounded and live delivery keeps its priority.
* No re-picking. A punch the checks skipped stays in the unknown-identity
  filter; it is tried again at most every ``retry_hours`` (a roster or CNIC
  may be fixed later). A punch another approval holds READY is left to that
  approval while the approval can still send it; a READY item of a run that
  ended, or of a check whose preview expired before anyone approved it, no
  longer holds the punch (``held_by_live_approval``).
* Bounded reads. Each batch reads the next newest punches below a saved
  cursor, plus any punch that arrived since the pass started, so a pass over
  the whole backlog reads it once. Reads are paced like batches (at most one
  every ``min_interval_seconds``, whether or not they found anything). When a
  pass reaches the oldest punch the releaser rests until the first punch it
  tried becomes eligible again, at most ``SCAN_REST_SECONDS``; while resting
  it reads only punches saved since (a short index range), so new punches are
  not delayed.
* The backlog count and the progress totals are computed here at most every
  ``count_interval_seconds`` and saved with a timestamp; the status endpoint
  reads them and the few active runs, never the whole table. The attempt is
  saved before the count runs, so a count that times out waits its interval
  too, and failures in a row back off further. The punches other approvals
  hold are counted separately, so a timeout there loses only that figure; the
  figure is dropped when a check it covered expires, and counted again then.

Turning the switch OFF only stops new batches: a batch being built when the
switch turns OFF is discarded (the switch is re-read under its row lock before
the batch is saved). Punches already approved stay in their Direct ORDS runs
and follow through to Oracle, which cannot be undone.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.exc import OperationalError

from zk_add import attendance_direct_ords as direct
from zk_add import attendance_force_release as force_release
from zk_add import attendance_safe_repair as safe_repair
from zk_add.attendance_cnic_link import UNKNOWN_SYNCED_HELD_STATUSES, unknown_synced_cnic_expression
from zk_add.attendance_recovery import RecoveryError, _digest
from zk_add.audit import append_audit
from zk_add.crypto import decrypt_text, encrypt_text
from zk_add.db import database_busy, session_scope
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    UnknownIdentityAutoRelease as State,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, iso_utc, parse_datetime, utc_now


# Runs this releaser creates carry this actor (indexed) and a scope flag.
AUTO_ACTOR = "ADD automatic release"
SCOPE_FLAG = "automatic_unknown_release"
# One automatic tick at a time across ADD, whichever process runs it.
TICK_LOCK_KEY = 0x41_44_44_55_4E_4B_52
IN_FLIGHT_STATUSES = ("READY", "WAITING_ORACLE")
# Held punch IDs read per filter query, and at most per batch (head and tail each).
SCAN_WINDOW = 500
SCAN_BUDGET = 5_000
# Longest rest after a pass found nothing more to release (new punches are still read).
SCAN_REST_SECONDS = 15 * 60
# A count that failed this many times in a row waits 2**n count intervals (n capped).
COUNT_BACKOFF_STEPS = 4
AUDIT_TARGET = "unknown_identity_auto_release"
# The runs that save READY items, and the statuses in which each never acts on
# them again: a Direct ORDS send completes only once no item is READY; a force
# release or repair run ends in its TERMINAL statuses. Any other action or
# status (running, waiting, paused, stopping, a check still in progress) may
# still send, so its READY items keep holding their punches.
ENDED_RUN_STATUSES = {
    direct.ACTION: ("COMPLETED", "COMPLETED_WITH_REVIEW"),
    force_release.ACTION: tuple(sorted(force_release.TERMINAL)),
    safe_repair.ACTION: tuple(sorted(safe_repair.TERMINAL)),
}
# Checks never approved stay CHECKED; their start refuses an expired preview
# (verify_recovery_preview: PREVIEW_EXPIRED), so they can never send either.
PREVIEW_CHECK_ACTIONS = (force_release.ACTION, safe_repair.ACTION)


class AutoReleaseControlRequest(BaseModel):
    """Turn the automatic release ON or OFF: the administrator's password and a reason."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool
    reason: str = Field(min_length=3, max_length=500)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class AutoReleaseDrainRequest(BaseModel):
    """One click, like the quick release: release every such punch now while OFF."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=120)


class AutoReleaseError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# State ---------------------------------------------------------------------


def _ensure(session) -> State:
    """The singleton row; a missing row means ON (create_all stores, first start)."""
    row = session.get(State, 1)
    if row is None:
        # Production migrations seed the row; create_all stores create it here.
        row = State(id=1, enabled=True, stats={})
        session.add(row)
        session.flush()
    return row


def _lock(session) -> State:
    _ensure(session)
    return session.scalar(
        select(State).where(State.id == 1).with_for_update()
        .execution_options(populate_existing=True)
    )


def _auto_jobs():
    return and_(Job.action == direct.ACTION, Job.actor == AUTO_ACTOR)


def _in_flight(session) -> dict[str, int]:
    """This releaser's approved punches not yet confirmed or refused, from its active runs only."""
    counts = dict(session.execute(
        select(Item.status, func.count(Item.id))
        .join(Job, Job.id == Item.job_id)
        .where(_auto_jobs(), Job.status.in_(direct.ACTIVE), Item.status.in_(IN_FLIGHT_STATUSES))
        .group_by(Item.status)
    ).all())
    return {status: int(counts.get(status, 0)) for status in IN_FLIGHT_STATUSES}


def _run_never_acts_again(now):
    """Runs (``Job``) that will never act on their READY items again.

    Ended runs, and force release or repair checks still CHECKED whose preview
    expired by ``now``: their start refuses an expired preview. Nothing else
    counts as dead, so a punch is never handed to two approvals that may send.
    """
    expired_check = and_(
        Job.action.in_(PREVIEW_CHECK_ACTIONS),
        Job.status == "CHECKED",
        Job.preview_expires_at.is_not(None),
        Job.preview_expires_at <= now,
    )
    ended = [
        and_(Job.action == action, Job.status.in_(statuses))
        for action, statuses in ENDED_RUN_STATUSES.items()
    ]
    return or_(expired_check, *ended)


def held_by_live_approval(now):
    """A READY item (``Item`` joined to its ``Job``) whose run may still send its punch."""
    return and_(Item.status == "READY", ~_run_never_acts_again(now))


def _tried_recently(cutoff, now):
    """A punch this releaser checked since ``cutoff``, or one a live approval holds READY."""
    return (
        select(Item.id).join(Job, Job.id == Item.job_id).where(
            Item.attendance_event_id == AttendanceEvent.id,
            or_(and_(_auto_jobs(), Job.created_at >= cutoff), held_by_live_approval(now)),
        ).exists()
    )


def _recent_skips(cutoff):
    """This releaser's SKIPPED items from runs since ``cutoff`` (the panel's skipped count)."""
    return and_(_auto_jobs(), Job.created_at >= cutoff, Item.status == "SKIPPED")


def _own_ready():
    """This releaser's READY items in its active runs (the panel's ready count)."""
    return and_(_auto_jobs(), Job.status.in_(direct.ACTIVE), Item.status == "READY")


def _held_by_other_approval(now, cutoff):
    """A punch READY in a live approval that is not this releaser's own run.

    Leaves out a punch the panel already subtracts as this releaser's recent
    skip or its own READY item, so "Still to release" never subtracts it twice.
    """
    def item_of_punch(*where):
        return select(Item.id).join(Job, Job.id == Item.job_id).where(
            Item.attendance_event_id == AttendanceEvent.id, *where,
        ).exists()

    return and_(
        item_of_punch(held_by_live_approval(now), ~_auto_jobs()),
        ~item_of_punch(or_(_recent_skips(cutoff), _own_ready())),
    )


def _due(stamp, now, seconds) -> bool:
    return stamp is None or now >= ensure_utc(stamp) + timedelta(seconds=seconds)


def _held_until(stats) -> datetime | None:
    """When the saved count of punches other approvals hold goes stale, if ever.

    The earliest preview expiry of a check still unapproved when counted: from
    then the punches it held are this releaser's to release.
    """
    value = (stats or {}).get("held_until")
    try:
        return parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _count_due(state: State, now) -> bool:
    """Gated on the last attempt, not the last success, with back-off after failures.

    Also due once when a check the held count covered has expired, so its
    punches are not left out of "Still to release" for a whole interval.
    """
    failures = min(int(state.count_failures or 0), COUNT_BACKOFF_STEPS)
    attempted = state.count_attempted_at or state.backlog_counted_at
    until = _held_until(state.stats)
    if failures == 0 and until is not None and now >= until and (
        attempted is None or ensure_utc(attempted) < until
    ):
        return True
    interval = settings.unknown_identity_auto_release_count_interval_seconds * (2 ** failures)
    return _due(attempted, now, interval)


# Worker ----------------------------------------------------------------------


def _held_window(session, *, below, above, size):
    """The next held, unconfirmed punch IDs below ``below``, newest first: a cheap index range."""
    query = select(AttendanceEvent.id).where(
        AttendanceEvent.ords_status.in_(UNKNOWN_SYNCED_HELD_STATUSES),
        AttendanceEvent.oracle_confirmed_at.is_(None),
    )
    if below is not None:
        query = query.where(AttendanceEvent.id < below)
    if above is not None:
        query = query.where(AttendanceEvent.id > above)
    return list(session.scalars(query.order_by(AttendanceEvent.id.desc()).limit(size)).all())


def _scan(session, *, below, above, need, budget, cutoff, now):
    """Matching punches, newest first, reading at most ``budget`` held rows.

    The full filter (two correlated lookups per punch) runs only on a window
    of held IDs, never on the whole table: PostgreSQL cannot estimate it and
    would otherwise evaluate every held punch and sort them for each batch.
    Returns (found, lowest ID fully examined, whether the range is exhausted).
    """
    found: list[int] = []
    examined_to = below
    while need > len(found) and budget > 0:
        size = min(SCAN_WINDOW, budget)
        window = _held_window(session, below=examined_to, above=above, size=size)
        budget -= size
        if window:
            matches = list(session.scalars(
                select(AttendanceEvent.id).where(
                    AttendanceEvent.id.in_(window),
                    unknown_synced_cnic_expression(), ~_tried_recently(cutoff, now),
                ).order_by(AttendanceEvent.id.desc())
            ).all())
            wanted = need - len(found)
            if len(matches) > wanted:
                # Stop at the last punch taken; the rest of the window is read next time.
                found += matches[:wanted]
                return found, found[-1], False
            found += matches
            examined_to = window[-1]
        if len(window) < size:
            return found, examined_to, True
    return found, examined_to, False


def _pick(session, state: State, now, limit: int):
    """The next batch, newest first, and the scan position after it.

    A pass reads down from the newest punch saved when it started; punches
    saved since then are read first on every batch (they are few).
    """
    cutoff = now - timedelta(hours=settings.unknown_identity_auto_release_retry_hours)
    drain_at = state.drain_requested_at
    restart = state.scan_head_id is None or (
        drain_at is not None and not state.enabled and (
            state.scan_started_at is None or ensure_utc(state.scan_started_at) < ensure_utc(drain_at)
        )
    )
    if restart:
        head = session.scalar(select(func.max(AttendanceEvent.id))) or 0
        cursor, started = head + 1, now
    else:
        head, cursor, started = state.scan_head_id, state.scan_cursor_id, ensure_utc(state.scan_started_at)
    budget = SCAN_BUDGET
    picked, _lowest, _done = _scan(
        session, below=None, above=head, need=limit, budget=budget, cutoff=cutoff, now=now,
    )
    pass_done = False
    if len(picked) < limit:
        tail, lowest, pass_done = _scan(
            session, below=cursor, above=None, need=limit - len(picked), budget=budget,
            cutoff=cutoff, now=now,
        )
        if lowest is not None:
            cursor = lowest
        picked += tail
    scan = (
        # Resting after the pass reads only punches above the pass head.
        {"scan_head_id": None, "scan_cursor_id": None, "scan_started_at": started, "idle_head_id": head}
        if pass_done else
        {"scan_head_id": head, "scan_cursor_id": cursor, "scan_started_at": started}
    )
    return picked, scan, pass_done, started


def _pick_new(session, state: State, now, limit: int):
    """While resting: only punches saved since the last read, a short index range above ``idle_head_id``."""
    cutoff = now - timedelta(hours=settings.unknown_identity_auto_release_retry_hours)
    top = session.scalar(select(func.max(AttendanceEvent.id))) or 0
    above = state.idle_head_id if state.idle_head_id is not None else top
    picked, _lowest, done = _scan(
        session, below=None, above=above, need=limit, budget=SCAN_BUDGET, cutoff=cutoff, now=now,
    )
    # Every held punch above ``above`` was read: next time start from the newest seen now.
    scan = {"idle_head_id": max(top, above)} if done and len(picked) < limit else {}
    return picked, scan


def _rest_until(session, now):
    """After a full pass: until the first punch this releaser tried may be retried, within bounds.

    Nothing below the pass head can become eligible earlier, except a punch
    whose roster or CNIC changed or whose other approval ended (or whose check
    expired unapproved); those wait at most ``SCAN_REST_SECONDS``.
    """
    retry = timedelta(hours=settings.unknown_identity_auto_release_retry_hours)
    rest = now + timedelta(seconds=SCAN_REST_SECONDS)
    oldest = session.scalar(
        select(func.min(Job.created_at)).where(_auto_jobs(), Job.created_at >= now - retry)
    )
    if oldest is not None:
        rest = min(rest, ensure_utc(oldest) + retry)
    return max(rest, now + timedelta(seconds=settings.unknown_identity_auto_release_count_interval_seconds))


def _still_releasing(session) -> bool:
    """The switch as committed now, read under its row lock.

    ``control`` changes the row under the same lock, so an OFF committed while
    this batch was being built is seen here, and an OFF that arrives after this
    read waits until the batch is saved (it was created before the OFF).
    """
    row = session.execute(
        select(State.enabled, State.drain_requested_at).where(State.id == 1).with_for_update()
    ).one()
    return bool(row.enabled) or row.drain_requested_at is not None


def run_once(session, *, now=None) -> str:
    """One paced step: at most one batch. Returns what it did (for logs and tests)."""
    now = now or utc_now()
    if not settings.unknown_identity_auto_release_allowed:
        return "NOT_ALLOWED"
    if session.get_bind().dialect.name == "postgresql" and not session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": TICK_LOCK_KEY}
    ):
        return "LOCKED"
    state = _ensure(session)
    drain_at = state.drain_requested_at
    if not (state.enabled or drain_at):
        return "OFF"
    # Every read of held punches is paced, whether or not it finds a batch.
    if not _due(
        state.last_scan_at or state.last_batch_at, now,
        settings.unknown_identity_auto_release_min_interval_seconds,
    ):
        return "INTERVAL"
    in_flight = sum(_in_flight(session).values())
    if in_flight >= settings.unknown_identity_auto_release_in_flight_limit:
        return "BACKPRESSURE"
    limit = settings.unknown_identity_auto_release_batch_size
    batch_number = (state.batches_created or 0) + 1
    if state.scan_idle_until is not None and now < ensure_utc(state.scan_idle_until):
        ids, scan = _pick_new(session, state, now, limit)
        pass_done, started, outcome = False, None, "IDLE"
    else:
        ids, scan, pass_done, started = _pick(session, state, now, limit)
        outcome = "NOTHING_LEFT" if pass_done else "SCANNING"
    values: dict = dict(scan)
    if ids:
        outcome = "BATCH"
        key = f"auto-unknown-release-{batch_number}-{_digest(sorted(ids))[:16]}"
        try:
            job, _not_matching = direct.quick_release(
                session, actor=AUTO_ACTOR, event_ids=ids, idempotency_key=key,
            )
        except RecoveryError as exc:
            # NOTHING_TO_RELEASE: they left the filter meanwhile. Anything
            # else is recorded; the cursor moves on so a batch never loops.
            job = None
            outcome = exc.code
            if exc.code != "NOTHING_TO_RELEASE":
                values.update(last_error_code=exc.code[:80], last_error_at=now)
        if not _still_releasing(session):
            # Switched OFF (or Release all stopped) while this batch was being
            # built: discard it; nothing of it is saved or sent.
            session.rollback()
            return "OFF"
        if job is not None:
            job.scope = {**(job.scope or {}), SCOPE_FLAG: True, "batch_number": batch_number}
            values.update(last_batch_job_id=job.job_id)
        values.update(
            last_batch_at=max(now, utc_now()), last_batch_size=len(ids),
            batches_created=State.batches_created + 1,
            punches_submitted=State.punches_submitted + len(ids),
        )
    if pass_done:
        values["scan_idle_until"] = _rest_until(session, now)
    if outcome in {"BATCH", "NOTHING_LEFT", "NOTHING_TO_RELEASE", "SCANNING", "IDLE"}:
        values.update(last_error_code=None, last_error_at=None)
    # The pause runs from the end of this read: a slow batch or scan is
    # followed by the full pause, never by the next read at once.
    values["last_scan_at"] = max(now, utc_now())
    values["updated_at"] = now
    session.execute(update(State).where(State.id == 1).values(**values))
    if pass_done and drain_at is not None and started >= ensure_utc(drain_at):
        # Cleared only if no newer "Release all" arrived during this batch.
        session.execute(
            update(State).where(State.id == 1, State.drain_requested_at == drain_at)
            .values(drain_requested_at=None, drain_requested_by=None, drain_completed_at=now)
        )
    return outcome


def claim_count(session, *, now=None) -> bool:
    """Whether a count is due; if so, save the attempt first (commit before counting).

    A count that then times out still waits its interval (doubled for every
    failure in a row), so it never turns into a retry loop on a busy database.
    """
    now = now or utc_now()
    state = _lock(session)
    if not _count_due(state, now):
        return False
    state.count_attempted_at = now
    return True


def refresh_counts(session, *, now=None, force: bool = False) -> bool:
    """Backlog count and progress totals, at most every count interval."""
    now = now or utc_now()
    state = _ensure(session)
    if not force and not _count_due(state, now):
        return False
    if session.get_bind().dialect.name == "postgresql":
        # Bounded like the dashboard's scans; SET LOCAL ends with the transaction.
        session.execute(text("SET LOCAL statement_timeout = '20s'"))
    backlog = session.scalar(
        select(func.count(AttendanceEvent.id)).where(unknown_synced_cnic_expression())
    )
    cutoff = now - timedelta(hours=settings.unknown_identity_auto_release_retry_hours)
    held_elsewhere, held_until = _count_held_elsewhere(session, now, cutoff)
    hour_ago = now - timedelta(hours=1)
    by_status, confirmed_last_hour = {}, 0
    for status, count, recent in session.execute(
        select(
            Item.status, func.count(Item.id),
            func.sum(case((and_(Item.status == "CONFIRMED", Item.completed_at >= hour_ago), 1), else_=0)),
        ).join(Job, Job.id == Item.job_id).where(_auto_jobs()).group_by(Item.status)
    ).all():
        by_status[status] = int(count)
        confirmed_last_hour += int(recent or 0)
    recent_skips = _recent_skips(cutoff)
    skipped = {
        code or "UNKNOWN": int(count)
        for code, count in session.execute(
            select(Item.error_code, func.count(func.distinct(Item.attendance_event_id)))
            .join(Job, Job.id == Item.job_id).where(recent_skips).group_by(Item.error_code)
        ).all()
    }
    skipped_total = session.scalar(
        select(func.count(func.distinct(Item.attendance_event_id)))
        .join(Job, Job.id == Item.job_id).where(recent_skips)
    ) or 0
    session.execute(update(State).where(State.id == 1).values(
        backlog_count=int(backlog or 0), backlog_counted_at=now,
        count_attempted_at=now, count_failures=0,
        stats={
            "items_by_status": by_status,
            "confirmed_last_hour": confirmed_last_hour,
            "skipped_recent": int(skipped_total),
            "skipped_by_reason": skipped,
            "held_by_other_approval": held_elsewhere,
            "held_until": _iso(held_until),
        },
        stats_counted_at=now,
    ))
    return True


def _count_held_elsewhere(session, now, cutoff):
    """Held punches a live approval other than this releaser holds READY, and until when.

    Its own statements in a savepoint, after the backlog count: if they run
    out of time only this figure is lost (None: nothing is left out of "Still
    to release"), never the backlog count.
    """
    try:
        with session.begin_nested():
            held = session.scalar(
                select(func.count(AttendanceEvent.id))
                .where(unknown_synced_cnic_expression(), _held_by_other_approval(now, cutoff))
            )
            until = session.scalar(
                select(func.min(Job.preview_expires_at)).where(
                    Job.action.in_(PREVIEW_CHECK_ACTIONS), Job.status == "CHECKED",
                    Job.preview_expires_at > now,
                )
            )
    except OperationalError as exc:
        if not database_busy(exc):
            raise
        return None, None
    held = int(held or 0)
    return held, until if held else None


def _record_error(code: str, *, count_failed: bool = False) -> None:
    try:
        with session_scope() as session:
            _ensure(session)
            now = utc_now()
            values = {"last_error_code": code, "last_error_at": now, "updated_at": now}
            if count_failed:
                values["count_failures"] = State.count_failures + 1
            session.execute(update(State).where(State.id == 1).values(**values))
    except Exception:  # noqa: BLE001 - telemetry only; the worker loop reports the original error.
        pass


def tick() -> str:
    """Worker entry point (worker._safe_attendance_repair_loop, in a thread)."""
    if not settings.unknown_identity_auto_release_allowed:
        return "NOT_ALLOWED"
    try:
        with session_scope() as session:
            outcome = run_once(session)
    except OperationalError as exc:
        if not database_busy(exc):
            _record_error("BATCH_FAILED")
            raise
        # A lock or statement ran out of time: nothing was saved; retry later.
        _record_error("ADD_BUSY")
        outcome = "ADD_BUSY"
    except Exception:
        _record_error("BATCH_FAILED")
        raise
    counting = False
    try:
        with session_scope() as session:
            counting = claim_count(session)
        if counting:
            # The attempt is committed above, so a timeout here is not retried at once.
            with session_scope() as session:
                refresh_counts(session, force=True)
    except OperationalError as exc:
        if not database_busy(exc):
            raise
        _record_error("COUNT_BUSY", count_failed=counting)
    return outcome


# Owner actions ----------------------------------------------------------------


def control(session, *, actor: str, request: AutoReleaseControlRequest, ip_address: str | None = None) -> State:
    """Switch ON or OFF (the caller has already checked the password).

    OFF also ends a running "Release all now". A repeat of the same request
    key changes nothing and writes no second audit row.
    """
    state = _lock(session)
    if state.control_key == request.idempotency_key:
        if bool(state.enabled) != request.enabled:
            raise AutoReleaseError("This request key belongs to another change.", "IDEMPOTENCY_CONFLICT")
        return state
    reason = request.reason.strip()
    if len(reason) < 3:
        raise AutoReleaseError("Write a reason of at least 3 characters.", "REASON_REQUIRED")
    now = utc_now()
    before = {"enabled": bool(state.enabled), "drain_active": state.drain_requested_at is not None}
    state.enabled = request.enabled
    state.changed_by, state.changed_at = actor, now
    state.reason_encrypted = encrypt_text(reason)
    state.control_key = request.idempotency_key
    state.drain_requested_at = state.drain_requested_by = None
    state.scan_idle_until = None
    state.updated_at = now
    append_audit(
        session, actor=actor, action="ATTENDANCE_UNKNOWN_AUTO_RELEASE_SWITCHED",
        target_type=AUDIT_TARGET, target_id="1", outcome="ON" if request.enabled else "OFF",
        ip_address=ip_address, before=before,
        after={"enabled": request.enabled, "reason_digest": _digest(reason)},
    )
    return state


def request_drain(session, *, actor: str, request: AutoReleaseDrainRequest, ip_address: str | None = None) -> str:
    """"Release all now" while OFF. Returns ALREADY_AUTOMATIC, ALREADY_RUNNING or STARTED."""
    if not settings.unknown_identity_auto_release_allowed:
        raise AutoReleaseError(
            "Automatic release is turned off for this ADD deployment.", "AUTO_RELEASE_NOT_ALLOWED",
        )
    state = _lock(session)
    if state.enabled:
        return "ALREADY_AUTOMATIC"
    if state.drain_requested_at is not None:
        return "ALREADY_RUNNING"
    now = utc_now()
    state.drain_requested_at, state.drain_requested_by = now, actor
    state.drain_key = request.idempotency_key
    state.scan_idle_until = None
    state.updated_at = now
    append_audit(
        session, actor=actor, action="ATTENDANCE_UNKNOWN_RELEASE_ALL_REQUESTED",
        target_type=AUDIT_TARGET, target_id="1", outcome="STARTED", ip_address=ip_address,
        after={"backlog_count": state.backlog_count, "request_key": request.idempotency_key},
    )
    return "STARTED"


# Status ------------------------------------------------------------------------


def _skip_reason(code: str) -> str:
    return "Skipped without a recorded reason." if code == "UNKNOWN" else direct._reason(code)


def _iso(value):
    return iso_utc(value) if value is not None else None


def _reason_text(state: State) -> str | None:
    try:
        return decrypt_text(state.reason_encrypted)
    except Exception:  # noqa: BLE001 - an unreadable reason is shown as missing, never fails the page.
        return None


def status(session, *, now=None) -> dict:
    """The panel's poll: the saved row plus the few active runs. No backlog scan."""
    now = now or utc_now()
    state = session.get(State, 1)
    stats = (state.stats if state is not None else None) or {}
    by_status = stats.get("items_by_status") or {}
    live = _in_flight(session)
    ready, waiting = live["READY"], live["WAITING_ORACLE"]
    confirmed_last_hour = int(stats.get("confirmed_last_hour") or 0)
    skipped_recent = int(stats.get("skipped_recent") or 0)
    rate = round(confirmed_last_hour / 60, 2) if state is not None and state.stats_counted_at else None
    backlog = state.backlog_count if state is not None else None
    # Held punches another live approval holds READY are its to send, not this
    # releaser's; once a check counted there has expired they are its own again.
    until = _held_until(stats)
    held_elsewhere = int(stats.get("held_by_other_approval") or 0)
    if until is not None and now >= until:
        held_elsewhere = 0
    remaining = (
        None if backlog is None
        else max(0, backlog - skipped_recent - ready - held_elsewhere) + ready + waiting
    )
    allowed = settings.unknown_identity_auto_release_allowed
    enabled = True if state is None else bool(state.enabled)
    drain_active = state is not None and state.drain_requested_at is not None
    limit = settings.unknown_identity_auto_release_in_flight_limit
    return {
        "allowed": allowed,
        "enabled": enabled,
        "active": allowed and (enabled or drain_active),
        "changed_by": state.changed_by if state is not None else None,
        "changed_at": _iso(state.changed_at) if state is not None else None,
        "reason": _reason_text(state) if state is not None else None,
        "drain_active": drain_active,
        "drain_requested_at": _iso(state.drain_requested_at) if state is not None else None,
        "drain_requested_by": state.drain_requested_by if state is not None else None,
        "drain_completed_at": _iso(state.drain_completed_at) if state is not None else None,
        # After a full pass: older punches are read again from then; new ones meanwhile.
        "resting_until": (
            _iso(state.scan_idle_until)
            if state is not None and state.scan_idle_until is not None and now < ensure_utc(state.scan_idle_until)
            else None
        ),
        "backlog": {
            "count": backlog,
            "counted_at": _iso(state.backlog_counted_at) if state is not None else None,
            "remaining_estimate": remaining,
            "held_by_other_approval": None if backlog is None else held_elsewhere,
        },
        "progress": {
            "ready": ready,
            "waiting_oracle": waiting,
            "in_flight": ready + waiting,
            "queued_total": sum(int(by_status.get(key, 0)) for key in ("READY", "WAITING_ORACLE", "CONFIRMED")),
            "confirmed_total": int(by_status.get("CONFIRMED", 0)),
            "confirmed_last_hour": confirmed_last_hour,
            "skipped_total": skipped_recent,
            "skipped_window_hours": settings.unknown_identity_auto_release_retry_hours,
            "skipped_by_reason": [
                {"code": code, "reason": _skip_reason(code), "count": count}
                for code, count in sorted(
                    (stats.get("skipped_by_reason") or {}).items(), key=lambda pair: (-pair[1], pair[0])
                )
            ],
            "needs_review": int(by_status.get("NEEDS_REVIEW", 0)),
            "checked_total": sum(int(value) for value in by_status.values()),
            "counted_at": _iso(state.stats_counted_at) if state is not None else None,
        },
        "rate_per_min": rate,
        "eta_minutes": math.ceil(remaining / rate) if rate and remaining else (0 if remaining == 0 else None),
        "last_batch_at": _iso(state.last_batch_at) if state is not None else None,
        "last_batch_size": state.last_batch_size if state is not None else 0,
        "last_batch_job_id": state.last_batch_job_id if state is not None else None,
        "batches_created": state.batches_created if state is not None else 0,
        "punches_submitted": state.punches_submitted if state is not None else 0,
        "batch_size": settings.unknown_identity_auto_release_batch_size,
        "min_interval_seconds": settings.unknown_identity_auto_release_min_interval_seconds,
        "in_flight_limit": limit,
        "backpressure": ready + waiting >= limit,
        "last_error_code": state.last_error_code if state is not None else None,
        "last_error_at": _iso(state.last_error_at) if state is not None else None,
        "server_time": iso_utc(now),
    }
