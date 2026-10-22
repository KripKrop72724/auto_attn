"""Employee "attendance punched" phone notifications, sent after Oracle confirms.

Design (see docs/checkin-notifications.md for the operator view):

* Creation is transactional. A Session ``after_flush`` hook watches every
  attendance row whose ``ords_status`` moves into an Oracle-confirmed state
  (ACKED from ADD's own ORDS delivery, ACKED_CHECK from the "already in Oracle"
  membership/content checks, firmware receipt verification, journal content
  checks and administrator-approved deliveries). In the same transaction it
  inserts one ``add_attendance_checkin_notifications`` row with
  ``ON CONFLICT (attendance_event_id) DO NOTHING``: a punch can never get two
  rows, and the row exists if and only if the confirmation committed.
  Periodic re-verification of an already confirmed punch is not a new
  confirmation and never creates a row. A slow sweep re-creates any row a
  confirmation path might have missed (recent punches only).
* The CNIC stored on the row is the one Oracle received for that punch: the
  delivery paths pass it explicitly (``note_delivered_cnic``); otherwise the
  punch's own saved CNIC is used.
* A worker claims due rows with a lease (``FOR UPDATE SKIP LOCKED`` on
  PostgreSQL), applies the anti-spam policy (age window, invalid clock,
  OFF / PILOT / ALL, coalescing, first punch per day), sends with a bounded
  timeout, concurrency and request rate, retries network failures, timeouts,
  429 and 5xx with exponential backoff and jitter for a bounded period, and
  records a terminal FAILED for other client errors. A persisted circuit
  breaker pauses all sending after consecutive failures.
* Two hosts: the internal ORDS host (primary) and the public host (fallback).
  An attempt moves to the other host ONLY when it provably never reached a
  server (DNS failure, connection refused, connect timeout, TLS handshake
  failure). A read timeout, a reset after sending or any HTTP response is
  final for that attempt, so a fallback can never duplicate a notification.
  The host that last delivered is remembered; the primary is re-probed.
* Delivery is at-least-once: if the process dies after the HTTP request was
  sent but before the result commits, the lease expires and the punch is sent
  again. Every attempt is hard-bounded, and a job starts only while its whole
  budget fits inside its lease, so a live worker never overlaps a reclaim.
* Old punches are never backfilled: no row means "not sent".

The API key and full CNICs are never logged or stored in plain text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import random
import re
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import and_, event as sa_event, func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zk_add.crypto import cnic_lookup, decrypt_cnic, encrypt_cnic, normalize_cnic
from zk_add.db import session_scope
from zk_add.models import (
    AttendanceCheckinNotification as Notification,
    AttendanceEvent,
    CheckinNotifyControl,
    Connector,
    DeviceAlert,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now


logger = logging.getLogger(__name__)

CONFIRMED_STATUSES = frozenset({"ACKED", "ACKED_CHECK"})
# A prior state in this set means the punch was already confirmed before.
RECONFIRMATION_PRIOR_STATUSES = CONFIRMED_STATUSES | frozenset(
    {"MEMBERSHIP_REVERIFYING", "MEMBERSHIP_REVERIFY_RETRY"}
)
RECONFIRMATION_PATHS = frozenset({"PERIODIC_MEMBERSHIP_REVERIFY"})
STILL_CONFIRMED_STATUSES = RECONFIRMATION_PRIOR_STATUSES
MODES = ("OFF", "PILOT", "ALL")
PUNCH_POLICIES = ("ALL_PUNCHES", "FIRST_PER_DAY")
ACTIVE_STATUSES = ("PENDING", "SENDING", "RETRYING")
# A row in one of these states will (or did) notify the employee.
COVERING_STATUSES = ("SENDING", "RETRYING", "SENT")
PILL_BY_STATUS = {
    "SENT": "NOTIFIED",
    "PENDING": "PENDING",
    "SENDING": "PENDING",
    "RETRYING": "RETRYING",
    "FAILED": "FAILED",
    "SKIPPED": "NOT_SENT",
}
PILL_FILTERS = ("NOTIFIED", "PENDING", "RETRYING", "FAILED", "NOT_SENT")
# Pakistan Standard Time has no daylight saving; a fixed offset needs no tzdata.
PKT = timezone(timedelta(hours=5))
ALERT_CODE = "CHECKIN_NOTIFY_DEGRADED"
_HINT_KEY = "checkin_notify_delivered_cnic"
_DIGITS = re.compile(r"\d{5,}")
_SAFE_CATEGORY = re.compile(r"[^A-Z0-9_]")


# --------------------------------------------------------------------------
# Transactional creation
# --------------------------------------------------------------------------


def note_delivered_cnic(session: Session, attendance_event_id: int | None, cnic: object) -> None:
    """Remember the exact CNIC Oracle received for a punch in this transaction."""
    normalized = normalize_cnic(cnic if isinstance(cnic, str) else None)
    if attendance_event_id is None or normalized is None:
        return
    session.info.setdefault(_HINT_KEY, {})[attendance_event_id] = normalized


def masked_last4(last4: str | None) -> str | None:
    if not last4 or len(last4) != 4 or not last4.isdigit():
        return None
    return f"*****-****{last4[:3]}-{last4[3]}"


def _window() -> timedelta:
    return timedelta(hours=settings.checkin_notify_stale_window_hours)


def _initial_skip_reason(
    event: AttendanceEvent, punch: datetime, confirmed: datetime, has_cnic: bool
) -> str | None:
    """Reasons that can be decided from the punch alone, before queueing.

    Deciding these here keeps a large backlog release or history replay out of
    the send queue entirely; they can never become a notification later.
    """
    if not has_cnic:
        return "NO_CNIC"
    tolerance = timedelta(seconds=settings.checkin_notify_future_tolerance_seconds)
    if event.clock_quality == "INVALID" or punch > confirmed + tolerance:
        return "INVALID_CLOCK"
    if confirmed - punch > _window():
        return "OLD_PUNCH"
    return None


def _row_values(event: AttendanceEvent, delivered_cnic: str | None, now: datetime) -> dict:
    punch = ensure_utc(event.device_event_time)
    confirmed = ensure_utc(event.oracle_confirmed_at) if event.oracle_confirmed_at else now
    cnic_encrypted, lookup, last4 = event.cnic_encrypted, event.cnic_lookup_hash, event.cnic_last4
    if delivered_cnic:
        try:
            cnic_encrypted = encrypt_cnic(delivered_cnic)
            lookup = cnic_lookup(delivered_cnic)
            last4 = delivered_cnic[-4:]
        except Exception:
            # Keep the punch's own protected CNIC; never block the confirmation.
            cnic_encrypted, lookup, last4 = event.cnic_encrypted, event.cnic_lookup_hash, event.cnic_last4
    skip = _initial_skip_reason(event, punch, confirmed, bool(cnic_encrypted and lookup))
    return {
        "attendance_event_id": event.id,
        "connector_id": event.connector_id,
        "punch_time": punch,
        "confirmed_at": confirmed,
        "confirmation_path": (event.oracle_confirmation_path or None),
        "cnic_encrypted": cnic_encrypted,
        "cnic_lookup_hash": lookup,
        "cnic_last4": last4,
        "status": "SKIPPED" if skip else "PENDING",
        "reason": skip,
        "detail": None,
        "attempt_count": 0,
        "attempt_base": 0,
        "next_attempt_at": None if skip else now,
        "lease_expires_at": None,
        "last_attempt_at": None,
        "last_http_status": None,
        "sent_at": None,
        "grouped_into_id": None,
        "manual_retry": False,
        "retry_deadline_at": None
        if skip
        else now + timedelta(hours=settings.checkin_notify_retry_window_hours),
        "created_at": now,
        "updated_at": now,
    }


def _confirmation_transitions(session: Session) -> list[tuple[AttendanceEvent, str | None]]:
    hints = session.info.get(_HINT_KEY) or {}
    found: list[tuple[AttendanceEvent, str | None]] = []
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, AttendanceEvent) or obj.id is None:
            continue
        if obj.ords_status not in CONFIRMED_STATUSES:
            continue
        history = inspect(obj).attrs.ords_status.history
        if not history.added:
            continue  # Unchanged in this flush.
        if any(prior in RECONFIRMATION_PRIOR_STATUSES for prior in history.deleted):
            continue  # Re-verification of an already confirmed punch.
        if obj.oracle_confirmation_path in RECONFIRMATION_PATHS:
            continue
        found.append((obj, hints.pop(obj.id, None)))
    return found


def _insert_rows(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    connection = session.connection()
    table = Notification.__table__
    dialect = connection.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - ADD runs on PostgreSQL and tests on SQLite.
        raise RuntimeError("Unsupported database for check-in notifications")
    statement = dialect_insert(table).values(rows).on_conflict_do_nothing(
        index_elements=["attendance_event_id"]
    )
    # A savepoint keeps any failure here from aborting the Oracle confirmation.
    nested = connection.begin_nested()
    try:
        connection.execute(statement)
    except Exception:
        nested.rollback()
        raise
    nested.commit()


@sa_event.listens_for(Session, "after_flush")
def _record_oracle_confirmations(session: Session, _context) -> None:
    try:
        transitions = _confirmation_transitions(session)
        if not transitions:
            return
        now = utc_now()
        _insert_rows(session, [_row_values(event, cnic, now) for event, cnic in transitions])
    except Exception as exc:  # Attendance custody always wins over notification.
        logger.error(
            "Check-in notification row could not be recorded error=%s", type(exc).__name__
        )


# --------------------------------------------------------------------------
# Runtime control
# --------------------------------------------------------------------------


def _csv(value: str | None) -> list[str]:
    seen: list[str] = []
    for item in (value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


PRIMARY, FALLBACK = "PRIMARY", "FALLBACK"


def host_urls() -> dict[str, str]:
    """The configured hosts, primary (internal ORDS) first."""
    urls: dict[str, str] = {}
    if settings.resolved_checkin_notify_url:
        urls[PRIMARY] = settings.resolved_checkin_notify_url
    if settings.resolved_checkin_notify_fallback_url:
        urls[FALLBACK] = settings.resolved_checkin_notify_fallback_url
    return urls


def notify_configured() -> bool:
    return bool(settings.checkin_notify_api_key and host_urls())


def route_order(control: CheckinNotifyControl, now: datetime) -> tuple[str, ...]:
    """Sticky host order: the last host that delivered, re-probing the primary."""
    urls = host_urls()
    order = [PRIMARY, FALLBACK]
    reprobe_at = ensure_utc(control.primary_reprobe_at) if control.primary_reprobe_at else None
    if control.preferred_host == FALLBACK and reprobe_at is not None and reprobe_at > now:
        order = [FALLBACK, PRIMARY]
    return tuple(host for host in order if host in urls)


def ensure_control(session: Session, *, lock: bool = False) -> CheckinNotifyControl:
    statement = select(CheckinNotifyControl).where(CheckinNotifyControl.id == 1)
    row = session.scalar(statement.with_for_update() if lock else statement)
    if row is not None:
        return row
    mode = settings.checkin_notify_default_mode
    try:
        with session.begin_nested():
            session.add(
                CheckinNotifyControl(
                    id=1,
                    mode=mode if mode in MODES else "PILOT",
                    punch_policy=settings.checkin_notify_default_punch_policy,
                    pilot_zone_ids=_csv(settings.checkin_notify_default_pilot_zone_ids),
                    pilot_cnics=[],
                    revision=1,
                    consecutive_failures=0,
                    preferred_host=PRIMARY,
                )
            )
            session.flush()
    except IntegrityError:
        pass  # A concurrent process created the singleton first.
    return session.scalar(statement.with_for_update() if lock else statement)


def _pilot_hashes(control: CheckinNotifyControl) -> set[str]:
    return {
        str(item.get("hash"))
        for item in (control.pilot_cnics or [])
        if isinstance(item, dict) and item.get("hash")
    }


# --------------------------------------------------------------------------
# Worker: claim, send, record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SendJob:
    row_id: int
    token: int
    cnic: str = field(repr=False)
    masked: str | None


@dataclass(frozen=True)
class SendResult:
    # RELEASED: not attempted (the lease would not cover it); queue it again.
    outcome: Literal["SENT", "RETRY", "FAILED", "RELEASED"]
    category: str
    http_status: int | None = None
    circuit_failure: bool = False
    retry_after: float | None = None
    detail: str | None = None
    host: str | None = None
    # True only when the request provably never reached a server: DNS
    # failure, connection refused, connect timeout or TLS handshake failure.
    not_sent: bool = False


@dataclass
class Decision:
    action: Literal["SEND", "SKIP", "DEFER", "FAIL"]
    reason: str | None = None
    grouped_into_id: int | None = None
    defer_seconds: int = 0


def _advisory_lock(session: Session, cnic_hash: str) -> None:
    """Serialize coalescing decisions for one CNIC across worker processes."""
    if session.get_bind().dialect.name != "postgresql":
        return
    key = int(cnic_hash[:15], 16)  # 60 bits: always a valid signed bigint.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _pkt_day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    local = ensure_utc(moment).astimezone(PKT)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _covering_row(session: Session, row: Notification, start: datetime, end: datetime) -> int | None:
    return session.scalar(
        select(Notification.id)
        .where(
            Notification.cnic_lookup_hash == row.cnic_lookup_hash,
            Notification.id != row.id,
            Notification.status.in_(COVERING_STATUSES),
            Notification.punch_time >= start,
            Notification.punch_time < end,
        )
        .order_by(Notification.id)
        .limit(1)
    )


def _evaluate(
    session: Session,
    row: Notification,
    control: CheckinNotifyControl,
    now: datetime,
    *,
    configured: bool,
) -> Decision:
    event = session.get(AttendanceEvent, row.attendance_event_id)
    punch = ensure_utc(row.punch_time)
    if event is None:
        return Decision("SKIP", "EVENT_MISSING")
    if event.ords_status not in STILL_CONFIRMED_STATUSES:
        # Oracle confirmation was withdrawn (for example a later audit found
        # the punch missing). Wait for re-delivery within the age window.
        if now - punch < _window():
            return Decision("DEFER", defer_seconds=60)
        return Decision("SKIP", "CONFIRMATION_WITHDRAWN")
    if not configured:
        return Decision("SKIP", "NOT_CONFIGURED")
    if control.mode == "OFF":
        return Decision("SKIP", "DISABLED")
    if not row.cnic_encrypted or not row.cnic_lookup_hash:
        return Decision("SKIP", "NO_CNIC")
    if not row.manual_retry:
        tolerance = timedelta(seconds=settings.checkin_notify_future_tolerance_seconds)
        if event.clock_quality == "INVALID" or punch > now + tolerance:
            return Decision("SKIP", "INVALID_CLOCK")
        if now - punch > _window():
            return Decision("SKIP", "OLD_PUNCH")
    if row.retry_deadline_at and row.attempt_count > row.attempt_base and now > ensure_utc(
        row.retry_deadline_at
    ):
        return Decision("FAIL", "RETRY_WINDOW_EXHAUSTED")
    if control.mode == "PILOT":
        connector = session.get(Connector, row.connector_id)
        zone = connector.zone_id if connector is not None else None
        if zone not in set(control.pilot_zone_ids or []) and (
            row.cnic_lookup_hash not in _pilot_hashes(control)
        ):
            return Decision("SKIP", "NOT_IN_PILOT")
    if not row.manual_retry:
        window = timedelta(seconds=settings.checkin_notify_coalesce_seconds)
        if window:
            grouped = _covering_row(session, row, punch - window, punch + window + timedelta(microseconds=1))
            if grouped is not None:
                return Decision("SKIP", "GROUPED", grouped_into_id=grouped)
        if control.punch_policy == "FIRST_PER_DAY":
            start, end = _pkt_day_bounds(punch)
            earlier = _covering_row(session, row, start, end)
            if earlier is not None:
                return Decision("SKIP", "NOT_FIRST_OF_DAY", grouped_into_id=earlier)
    return Decision("SEND")


@dataclass
class Claim:
    jobs: list[SendJob]
    paused: bool = False
    processed: int = 0
    route: tuple[str, ...] = ()


def _host_budget_seconds() -> float:
    """Hard bound on one HTTP attempt: connect, write and read, plus slack."""
    return (
        settings.checkin_notify_connect_timeout_seconds
        + 2 * settings.checkin_notify_timeout_seconds
        + 1.0
    )


def _job_budget_seconds() -> float:
    return _host_budget_seconds() * max(1, len(host_urls()))


def _lease_seconds() -> float:
    # A job starts only while a full job budget still fits inside its lease, so
    # no request is in flight after its lease can expire (see deliver_once).
    return _job_budget_seconds() + 60.0


def _start_deadline_seconds() -> float:
    return max(1.0, _lease_seconds() - _job_budget_seconds() - 15.0)


def claim_batch(limit: int | None = None, *, now: datetime | None = None) -> Claim:
    """Claim due rows, decide each one, and lease the rows that will be sent."""
    limit = max(1, limit or settings.checkin_notify_batch_size)
    with session_scope() as session:
        now = now or utc_now()
        control = ensure_control(session)
        configured = notify_configured()
        if configured and control.paused_until and ensure_utc(control.paused_until) > now:
            return Claim([], paused=True)
        route = route_order(control, now)
        if configured and control.consecutive_failures >= settings.checkin_notify_circuit_failure_threshold:
            limit = 1  # Half-open: one probe decides whether sending resumes.
        due = or_(
            and_(
                Notification.status.in_(("PENDING", "RETRYING")),
                or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now),
            ),
            and_(Notification.status == "SENDING", Notification.lease_expires_at < now),
        )
        rows = session.scalars(
            select(Notification)
            .where(due)
            .order_by(Notification.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        # Coalescing reads other rows of the same CNIC. Lock each CNIC once,
        # in a fixed order, so concurrent claimers cannot deadlock.
        for cnic_hash in sorted({row.cnic_lookup_hash for row in rows if row.cnic_lookup_hash}):
            _advisory_lock(session, cnic_hash)
        jobs: list[SendJob] = []
        for row in rows:
            decision = _evaluate(session, row, control, now, configured=configured)
            row.updated_at = now
            if decision.action == "SEND":
                try:
                    cnic = normalize_cnic(decrypt_cnic(row.cnic_encrypted))
                except Exception:
                    cnic = None
                if cnic is None:
                    decision = Decision("SKIP", "NO_CNIC")
            if decision.action == "DEFER":
                row.status = "PENDING"
                row.next_attempt_at = now + timedelta(seconds=decision.defer_seconds)
                row.lease_expires_at = None
            elif decision.action in {"SKIP", "FAIL"}:
                row.status = "SKIPPED" if decision.action == "SKIP" else "FAILED"
                row.reason = decision.reason
                row.grouped_into_id = decision.grouped_into_id
                row.next_attempt_at = row.lease_expires_at = None
            else:
                row.status = "SENDING"
                row.attempt_count += 1
                row.last_attempt_at = now
                row.next_attempt_at = None
                row.lease_expires_at = now + timedelta(seconds=_lease_seconds())
                jobs.append(SendJob(row.id, row.attempt_count, cnic, masked_last4(row.cnic_last4)))
            # Later rows of this batch must see this decision when coalescing.
            session.flush()
        return Claim(jobs, processed=len(rows), route=route)


def _safe_detail(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _DIGITS.sub("#####", value.strip())
    key = settings.checkin_notify_api_key
    if key:
        cleaned = cleaned.replace(key, "***")
    return cleaned[:160]


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), float(settings.checkin_notify_backoff_max_seconds)))
    except ValueError:
        return None


def request_body(cnic: str) -> bytes:
    value: int | str = int(cnic) if settings.checkin_notify_cnic_json_type == "number" else cnic
    return json.dumps({"cnic": value}, separators=(",", ":")).encode()


async def post_notification(
    client: httpx.AsyncClient, url: str, job: SendJob, *, host: str | None = None
) -> SendResult:
    headers = {
        "X-Api-Key": settings.checkin_notify_api_key or "",
        "Content-Type": "application/json",
    }
    try:
        response = await asyncio.wait_for(
            client.post(url, content=request_body(job.cnic), headers=headers),
            timeout=_host_budget_seconds(),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # httpx raises these only while resolving, connecting or completing
        # the TLS handshake: no request byte was sent.
        name = "CONNECT_TIMEOUT" if isinstance(exc, httpx.ConnectTimeout) else "CONNECT_ERROR"
        return SendResult(
            "RETRY", f"TRANSPORT_{name}", circuit_failure=True, host=host, not_sent=True
        )
    except (httpx.TimeoutException, asyncio.TimeoutError):
        # A read or write timeout may follow a delivered request.
        return SendResult("RETRY", "TRANSPORT_TIMEOUT", circuit_failure=True, host=host)
    except httpx.HTTPError as exc:
        name = _SAFE_CATEGORY.sub("_", type(exc).__name__.upper())[:40]
        return SendResult("RETRY", f"TRANSPORT_{name}", circuit_failure=True, host=host)
    status = response.status_code
    body: object = None
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = None
    message = None
    if isinstance(body, dict):
        message = _safe_detail(body.get("message") or body.get("error") or body.get("status"))
    if 200 <= status < 300:
        if isinstance(body, dict) and (
            body.get("success") is False
            or str(body.get("status", "")).strip().lower() in {"error", "failed", "failure"}
        ):
            return SendResult("FAILED", "API_REPORTED_FAILURE", status, detail=message, host=host)
        return SendResult("SENT", f"HTTP_{status}", status, host=host)
    if status in {401, 403}:
        # A key or permission problem affects every punch: pause, keep the rows.
        return SendResult(
            "RETRY", f"HTTP_{status}", status, circuit_failure=True, detail=message, host=host
        )
    if status in {408, 425, 429} or status >= 500:
        return SendResult(
            "RETRY", f"HTTP_{status}", status, circuit_failure=True,
            retry_after=_retry_after(response), detail=message, host=host,
        )
    return SendResult("FAILED", f"HTTP_{status}", status, detail=message, host=host)


class Router:
    """Per-batch host order; a host that provably refused a connection moves last."""

    def __init__(self, order: tuple[str, ...]) -> None:
        self.order = list(order)

    def unreachable(self, host: str) -> None:
        if host in self.order and len(self.order) > 1:
            self.order.remove(host)
            self.order.append(host)


async def send_with_fallback(client: httpx.AsyncClient, job: SendJob, router: Router) -> SendResult:
    """Try hosts in order, moving on ONLY when an attempt never reached a server.

    Any other outcome (an HTTP response, a read timeout, a reset after the
    request was written) may mean the employee was notified, so it is final
    for this attempt and never repeated on another host.
    """
    urls = host_urls()
    tried: list[str] = []
    last: SendResult | None = None
    for host in list(router.order):
        url = urls.get(host)
        if url is None:
            continue
        result = await post_notification(client, url, job, host=host)
        tried.append(host)
        if not result.not_sent:
            if len(tried) > 1:
                result = replace(result, detail=_join(result.detail, f"after {tried[0].lower()} unreachable"))
            return result
        router.unreachable(host)
        last = result
    if last is None:
        return SendResult("RETRY", "NOT_CONFIGURED")
    category = "TRANSPORT_ALL_HOSTS_UNREACHABLE" if len(tried) > 1 else last.category
    return replace(last, category=category, detail=f"tried {', '.join(h.lower() for h in tried)}")


def _join(first: str | None, second: str) -> str:
    return f"{first}; {second}"[:200] if first else second


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with equal jitter: half fixed, half random."""
    rng = rng or random
    initial = settings.checkin_notify_backoff_initial_seconds
    ceiling = settings.checkin_notify_backoff_max_seconds
    delay = min(ceiling, initial * (2 ** max(0, min(attempt - 1, 20))))
    return delay / 2 + rng.uniform(0, delay / 2)


def apply_results(results: list[tuple[SendJob, SendResult]], *, now: datetime | None = None) -> None:
    with session_scope() as session:
        now = now or utc_now()
        control = ensure_control(session, lock=True)
        for job, result in results:
            row = session.scalar(
                select(Notification).where(Notification.id == job.row_id).with_for_update()
            )
            if row is None or row.status != "SENDING" or row.attempt_count != job.token:
                continue  # A newer lease owns this row; its own result will be recorded.
            row.updated_at = now
            row.lease_expires_at = None
            if result.outcome == "RELEASED":
                # Never attempted: give the attempt back and queue it again.
                row.status, row.attempt_count, row.next_attempt_at = "PENDING", job.token - 1, now
                continue
            row.last_http_status = result.http_status
            row.detail = result.detail
            row.host_used = result.host
            if result.outcome == "SENT":
                row.status, row.reason, row.sent_at = "SENT", None, now
                row.next_attempt_at = None
                logger.info("Check-in notification sent row=%s cnic=%s", row.id, job.masked)
            elif result.outcome == "RETRY":
                delay = backoff_seconds(row.attempt_count - row.attempt_base)
                if result.retry_after:
                    delay = max(delay, result.retry_after)
                next_at = now + timedelta(seconds=delay)
                deadline = ensure_utc(row.retry_deadline_at) if row.retry_deadline_at else now
                if next_at > deadline:
                    row.status, row.reason = "FAILED", "RETRY_WINDOW_EXHAUSTED"
                    row.detail = result.category
                    row.next_attempt_at = None
                else:
                    row.status, row.reason, row.next_attempt_at = "RETRYING", result.category, next_at
                logger.warning(
                    "Check-in notification attempt failed row=%s cnic=%s category=%s",
                    row.id, job.masked, result.category,
                )
            else:
                row.status, row.reason, row.next_attempt_at = "FAILED", result.category, None
                logger.warning(
                    "Check-in notification rejected row=%s cnic=%s category=%s",
                    row.id, job.masked, result.category,
                )
            if result.outcome == "SENT":
                control.consecutive_failures = 0
                control.paused_until = None
                control.last_success_at = now
                if result.host == FALLBACK:
                    if control.preferred_host != FALLBACK or control.primary_reprobe_at is None or (
                        ensure_utc(control.primary_reprobe_at) <= now
                    ):
                        control.primary_reprobe_at = now + timedelta(
                            seconds=settings.checkin_notify_primary_reprobe_seconds
                        )
                    control.preferred_host = FALLBACK
                elif result.host == PRIMARY:
                    control.preferred_host, control.primary_reprobe_at = PRIMARY, None
            elif result.circuit_failure:
                control.consecutive_failures += 1
                control.last_failure_at = now
                control.last_failure_category = result.category[:60]
        threshold = settings.checkin_notify_circuit_failure_threshold
        if control.consecutive_failures >= threshold and any(
            result.circuit_failure for _job, result in results
        ):
            exponent = min(control.consecutive_failures - threshold, 12)
            pause = min(
                settings.checkin_notify_circuit_max_pause_seconds,
                settings.checkin_notify_circuit_pause_seconds * (2**exponent),
            )
            control.paused_until = now + timedelta(seconds=pause)
            logger.warning(
                "Check-in notifications paused seconds=%s consecutive_failures=%s",
                pause, control.consecutive_failures,
            )
        control.updated_at = now


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / max(0.1, per_second)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


async def deliver_once(*, transport: httpx.AsyncBaseTransport | None = None) -> Claim:
    claim = await asyncio.to_thread(claim_batch)
    if not claim.jobs:
        return claim
    claimed_at = time.monotonic()
    start_deadline = _start_deadline_seconds()
    router = Router(claim.route or tuple(host_urls()))
    limiter = _RateLimiter(settings.checkin_notify_rate_per_second)
    semaphore = asyncio.Semaphore(settings.checkin_notify_concurrency)
    timeout = httpx.Timeout(
        settings.checkin_notify_timeout_seconds,
        connect=settings.checkin_notify_connect_timeout_seconds,
    )

    async with httpx.AsyncClient(
        timeout=timeout, transport=transport, follow_redirects=False
    ) as client:

        async def one(job: SendJob) -> tuple[SendJob, SendResult]:
            async with semaphore:
                await limiter.wait()
                if time.monotonic() - claimed_at > start_deadline:
                    return job, SendResult("RELEASED", "LEASE_BUDGET")
                try:
                    return job, await send_with_fallback(client, job, router)
                except Exception as exc:  # Never lose the lease bookkeeping.
                    name = _SAFE_CATEGORY.sub("_", type(exc).__name__.upper())[:40]
                    return job, SendResult("RETRY", f"CLIENT_{name}", circuit_failure=True)

        results = await asyncio.gather(*(one(job) for job in claim.jobs))
    await asyncio.to_thread(apply_results, list(results))
    return claim


def sweep_missing_rows(*, limit: int = 200, now: datetime | None = None) -> int:
    """Safety net: create rows for recent confirmed punches that have none."""
    with session_scope() as session:
        now = now or utc_now()
        control = ensure_control(session)
        start = ensure_utc(control.created_at)
        lower = max(start, now - _window())
        tolerance = timedelta(seconds=settings.checkin_notify_future_tolerance_seconds)
        events = session.scalars(
            select(AttendanceEvent)
            .outerjoin(Notification, Notification.attendance_event_id == AttendanceEvent.id)
            .where(
                Notification.id.is_(None),
                AttendanceEvent.device_event_time >= lower,
                AttendanceEvent.device_event_time <= now + tolerance,
                AttendanceEvent.ords_status.in_(tuple(CONFIRMED_STATUSES)),
                AttendanceEvent.oracle_confirmed_at >= start,
                or_(
                    AttendanceEvent.oracle_confirmation_path.is_(None),
                    AttendanceEvent.oracle_confirmation_path.not_in(tuple(RECONFIRMATION_PATHS)),
                ),
            )
            .order_by(AttendanceEvent.id)
            .limit(limit)
        ).all()
        _insert_rows(session, [_row_values(event, None, now) for event in events])
        return len(events)


def reconcile_alerts(*, now: datetime | None = None) -> int:
    """Raise or clear the per-device notification alert from durable state."""
    from zk_add.service import resolve_alert, upsert_alert

    with session_scope() as session:
        now = now or utc_now()
        control = ensure_control(session)
        backlog_before = now - timedelta(seconds=settings.checkin_notify_backlog_alert_seconds)
        recent = now - timedelta(hours=1)
        paused = bool(control.paused_until and ensure_utc(control.paused_until) > now)
        backlog = dict(
            session.execute(
                select(Notification.connector_id, func.count(Notification.id))
                .where(
                    Notification.status.in_(ACTIVE_STATUSES),
                    Notification.created_at < (now if paused else backlog_before),
                )
                .group_by(Notification.connector_id)
            ).all()
        )
        failed = dict(
            session.execute(
                select(Notification.connector_id, func.count(Notification.id))
                .where(Notification.status == "FAILED", Notification.updated_at >= recent)
                .group_by(Notification.connector_id)
            ).all()
        )
        affected = set(backlog) | {
            connector_id
            for connector_id, count in failed.items()
            if count >= settings.checkin_notify_failure_alert_count
        }
        for connector_id in affected:
            connector = session.get(Connector, connector_id)
            if connector is None:
                continue
            auth = (control.last_failure_category or "") in {"HTTP_401", "HTTP_403"}
            upsert_alert(
                session,
                connector,
                code="CHECKIN_NOTIFY_DEGRADED",
                severity="HIGH" if auth and paused else "WARNING",
                message=(
                    "Employee check-in phone notifications are paused after repeated failures; "
                    "punches and Oracle delivery are unaffected."
                    if paused
                    else "Employee check-in phone notifications are delayed or failing; "
                    "punches and Oracle delivery are unaffected."
                ),
                details={
                    "waiting": int(backlog.get(connector_id, 0)),
                    "failed_last_hour": int(failed.get(connector_id, 0)),
                    "paused_until": control.paused_until.isoformat() if paused else None,
                    "failure_category": control.last_failure_category,
                },
            )
        resolved = 0
        for alert in session.scalars(
            select(DeviceAlert).where(DeviceAlert.code == ALERT_CODE, DeviceAlert.state == "OPEN")
        ).all():
            if alert.connector_id in affected:
                continue
            connector = session.get(Connector, alert.connector_id)
            if connector is not None:
                resolved += resolve_alert(session, connector, code=ALERT_CODE)
        return resolved


async def notify_loop(stop: asyncio.Event, publish=None) -> None:
    tick = 0
    while not stop.is_set():
        claim = Claim([])
        try:
            claim = await deliver_once()
            if tick % 30 == 0:
                await asyncio.to_thread(sweep_missing_rows)
                await asyncio.to_thread(reconcile_alerts)
        except Exception as exc:
            logger.warning("Check-in notification worker tick failed error=%s", type(exc).__name__)
            if publish is not None:
                await publish(
                    "backend_error",
                    {
                        "code": "CHECKIN_NOTIFY_WORKER_ERROR",
                        "message": "Check-in notifications will retry. Attendance is unaffected.",
                    },
                )
        tick += 1
        busy = claim.processed >= settings.checkin_notify_batch_size and not claim.paused
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2 if busy else 1.0)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# Read models: pills, filter, summary, health
# --------------------------------------------------------------------------


def feature_started_at(session: Session) -> datetime | None:
    value = session.scalar(
        select(CheckinNotifyControl.created_at).where(CheckinNotifyControl.id == 1)
    )
    return ensure_utc(value) if value else None


def pill(event: AttendanceEvent, row: Notification | None, started_at: datetime | None) -> dict:
    if row is None:
        if event.ords_status not in STILL_CONFIRMED_STATUSES or not event.oracle_confirmed_at:
            reason = "AWAITING_ORACLE"
        elif started_at is None or ensure_utc(event.device_event_time) < started_at:
            reason = "BEFORE_FEATURE"
        elif utc_now() - ensure_utc(event.device_event_time) > _window():
            reason = "OLD_PUNCH"
        else:
            reason = "NOT_RECORDED"
        return {
            "status": "NOT_SENT", "reason": reason, "detail": None, "sent_at": None,
            "attempts": 0, "next_attempt_at": None, "last_http_status": None,
            "host": None, "updated_at": None, "can_retry": False, "manual_retry": False,
        }
    status = PILL_BY_STATUS.get(row.status, "PENDING")
    return {
        "status": status,
        "reason": row.reason,
        "detail": row.detail,
        "sent_at": row.sent_at,
        "attempts": row.attempt_count,
        "next_attempt_at": row.next_attempt_at if row.status == "RETRYING" else None,
        "last_http_status": row.last_http_status,
        "host": row.host_used,
        "updated_at": row.updated_at,
        "can_retry": row.status == "FAILED",
        "manual_retry": bool(row.manual_retry),
    }


def pills_for_page(session: Session, events: list[AttendanceEvent]) -> dict[int, dict]:
    if not events:
        return {}
    rows = {
        row.attendance_event_id: row
        for row in session.scalars(
            select(Notification).where(
                Notification.attendance_event_id.in_([event.id for event in events])
            )
        ).all()
    }
    started_at = feature_started_at(session)
    return {event.id: pill(event, rows.get(event.id), started_at) for event in events}


def filter_condition(values: list[str]):
    """SQL condition for an attendance query outer-joined to Notification."""
    conditions = []
    for value in values:
        if value == "NOTIFIED":
            conditions.append(Notification.status == "SENT")
        elif value == "PENDING":
            conditions.append(Notification.status.in_(("PENDING", "SENDING")))
        elif value == "RETRYING":
            conditions.append(Notification.status == "RETRYING")
        elif value == "FAILED":
            conditions.append(Notification.status == "FAILED")
        elif value == "NOT_SENT":
            conditions.append(or_(Notification.id.is_(None), Notification.status == "SKIPPED"))
    return or_(*conditions)


def summary(session: Session, *, now: datetime | None = None) -> dict:
    now = now or utc_now()
    since = now - timedelta(hours=24)
    counts = {key.lower(): 0 for key in PILL_FILTERS}
    reasons: dict[str, int] = {}
    for status, reason, count in session.execute(
        select(Notification.status, Notification.reason, func.count(Notification.id))
        .where(Notification.created_at >= since)
        .group_by(Notification.status, Notification.reason)
    ).all():
        key = PILL_BY_STATUS.get(status, "PENDING").lower()
        counts[key] += int(count)
        if status == "SKIPPED":
            reasons[reason or "UNKNOWN"] = reasons.get(reason or "UNKNOWN", 0) + int(count)
    return {**counts, "not_sent_reasons": reasons, "window_hours": 24}


def health(session: Session, *, now: datetime | None = None) -> dict:
    now = now or utc_now()
    control = session.scalar(select(CheckinNotifyControl).where(CheckinNotifyControl.id == 1))
    oldest = session.scalar(
        select(func.min(Notification.created_at)).where(Notification.status.in_(ACTIVE_STATUSES))
    )
    oldest_age = int((now - ensure_utc(oldest)).total_seconds()) if oldest else 0
    configured = notify_configured()
    mode = control.mode if control else settings.checkin_notify_default_mode
    paused = bool(control and control.paused_until and ensure_utc(control.paused_until) > now)
    if not configured:
        state = "NOT_CONFIGURED"
    elif mode == "OFF":
        state = "OFF"
    elif paused:
        state = "PAUSED"
    elif oldest_age > settings.checkin_notify_backlog_alert_seconds or (
        control and control.consecutive_failures
    ):
        state = "DEGRADED"
    else:
        state = "OK"
    return {
        "state": state,
        "configured": configured,
        "mode": mode,
        "punch_policy": control.punch_policy if control else settings.checkin_notify_default_punch_policy,
        "pilot_zone_count": len(control.pilot_zone_ids or []) if control else 0,
        "pilot_cnic_count": len(control.pilot_cnics or []) if control else 0,
        "last_success_at": control.last_success_at if control else None,
        "last_failure_at": control.last_failure_at if control else None,
        "last_failure_category": control.last_failure_category if control else None,
        "consecutive_failures": control.consecutive_failures if control else 0,
        "preferred_host": control.preferred_host if control else PRIMARY,
        "primary_reprobe_at": control.primary_reprobe_at if control else None,
        "paused_until": control.paused_until if paused else None,
        "oldest_pending_at": oldest,
        "oldest_pending_age_seconds": oldest_age,
        "started_at": control.created_at if control else None,
    }


def overview(session: Session) -> dict:
    return {**health(session), "last_24h": summary(session)}


def describe_control(session: Session) -> dict:
    control = ensure_control(session)
    url = settings.resolved_checkin_notify_url
    fallback = settings.resolved_checkin_notify_fallback_url
    return {
        **overview(session),
        "revision": control.revision,
        "pilot_zone_ids": list(control.pilot_zone_ids or []),
        "pilot_cnics": [
            {"id": str(item.get("hash", ""))[:16], "masked": item.get("masked")}
            for item in (control.pilot_cnics or [])
            if isinstance(item, dict)
        ],
        "updated_by": control.updated_by,
        "updated_at": control.updated_at,
        "endpoint_host": urlsplit(url).netloc if url else None,
        "fallback_host": urlsplit(fallback).netloc if fallback else None,
        "cnic_json_type": settings.checkin_notify_cnic_json_type,
        "stale_window_hours": settings.checkin_notify_stale_window_hours,
        "coalesce_seconds": settings.checkin_notify_coalesce_seconds,
        "retry_window_hours": settings.checkin_notify_retry_window_hours,
        "modes": list(MODES),
        "punch_policies": list(PUNCH_POLICIES),
    }


# --------------------------------------------------------------------------
# Administrator actions (step-up and audit are enforced by the API layer)
# --------------------------------------------------------------------------


class CheckinNotifySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["OFF", "PILOT", "ALL"]
    punch_policy: Literal["ALL_PUNCHES", "FIRST_PER_DAY"]
    pilot_zone_ids: list[str] = Field(default_factory=list, max_length=200)
    add_pilot_cnics: list[str] = Field(default_factory=list, max_length=50)
    remove_pilot_cnic_ids: list[str] = Field(default_factory=list, max_length=200)
    expected_revision: int | None = None
    reason: str = Field(min_length=3, max_length=300)
    password: SecretStr


class CheckinNotifyRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=300)
    password: SecretStr


class CheckinNotifyError(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _audit_view(control: CheckinNotifyControl) -> dict:
    return {
        "mode": control.mode,
        "punch_policy": control.punch_policy,
        "pilot_zone_ids": list(control.pilot_zone_ids or []),
        "pilot_cnics": [item.get("masked") for item in (control.pilot_cnics or [])],
        "revision": control.revision,
    }


def update_control(
    session: Session, body: CheckinNotifySettingsRequest, *, actor: str, ip_address: str | None
) -> dict:
    from zk_add.audit import append_audit

    control = ensure_control(session, lock=True)
    if body.expected_revision is not None and body.expected_revision != control.revision:
        raise CheckinNotifyError(409, "These settings changed. Reload and review them again.")
    zones: list[str] = []
    for zone in body.pilot_zone_ids:
        zone = zone.strip()
        if not zone or len(zone) > 80:
            raise CheckinNotifyError(422, "Each pilot zone ID must contain 1 to 80 characters.")
        if zone not in zones:
            zones.append(zone)
    remove = set(body.remove_pilot_cnic_ids)
    cnics = [
        item for item in (control.pilot_cnics or [])
        if isinstance(item, dict) and str(item.get("hash", ""))[:16] not in remove
    ]
    known = {item.get("hash") for item in cnics}
    for value in body.add_pilot_cnics:
        normalized = normalize_cnic(value)
        if normalized is None:
            raise CheckinNotifyError(422, "Each pilot CNIC must contain exactly 13 digits.")
        hashed = cnic_lookup(normalized)
        if hashed not in known:
            cnics.append({"hash": hashed, "masked": masked_last4(normalized[-4:])})
            known.add(hashed)
    if len(cnics) > 200:
        raise CheckinNotifyError(422, "The pilot list can contain at most 200 CNICs.")
    before = _audit_view(control)
    control.mode = body.mode
    control.punch_policy = body.punch_policy
    control.pilot_zone_ids = zones
    control.pilot_cnics = cnics
    control.revision += 1
    control.updated_by = actor
    control.update_reason = body.reason.strip()[:300]
    control.updated_at = utc_now()
    append_audit(
        session,
        actor=actor,
        action="CHECKIN_NOTIFY_SETTINGS_UPDATED",
        target_type="checkin_notify",
        target_id="1",
        outcome="SUCCESS",
        ip_address=ip_address,
        before=before,
        after={**_audit_view(control), "reason": control.update_reason},
    )
    session.flush()
    return describe_control(session)


def retry_notification(
    session: Session, *, event_id: int, actor: str, reason: str, ip_address: str | None
) -> dict:
    from zk_add.audit import append_audit

    event = session.get(AttendanceEvent, event_id)
    row = session.scalar(
        select(Notification).where(Notification.attendance_event_id == event_id).with_for_update()
    )
    if event is None or row is None:
        raise CheckinNotifyError(404, "This punch has no check-in notification to retry.")
    if row.status != "FAILED":
        raise CheckinNotifyError(409, "Only a failed check-in notification can be retried.")
    now = utc_now()
    before = {"status": row.status, "reason": row.reason, "attempts": row.attempt_count}
    row.status = "PENDING"
    row.reason = row.detail = None
    row.manual_retry = True
    row.attempt_base = row.attempt_count
    row.next_attempt_at = now
    row.lease_expires_at = None
    row.retry_deadline_at = now + timedelta(hours=settings.checkin_notify_retry_window_hours)
    row.updated_at = now
    append_audit(
        session,
        actor=actor,
        action="CHECKIN_NOTIFY_RETRY",
        target_type="attendance_event",
        target_id=str(event_id),
        outcome="SUCCESS",
        ip_address=ip_address,
        before=before,
        after={"status": "PENDING", "reason": reason.strip()[:300]},
    )
    session.flush()
    return pill(event, row, feature_started_at(session))
