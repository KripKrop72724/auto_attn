from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.enums import SourceType, TrustStatus
from zk_common.hashing import payload_hash
from zk_common.time_utils import ensure_utc, iso_utc, utc_now
from zk_zone_agent.config import ActiveZoneConfig, config_manager
from zk_zone_agent.db import AttendanceEvent, OracleAttendanceOutbox, ServiceEvent, session_scope


ORACLE_LIVE_PATH = "/raw-captures"
ORACLE_BULK_PATH = "/raw-captures/bulk"
ORACLE_BULK_CHUNK_SIZE = 5000
ORACLE_REQUEST_TIMEOUT_SECONDS = 20.0
ORACLE_STALE_IN_FLIGHT_SECONDS = 300

ORACLE_STATUS_PENDING = "PENDING"
ORACLE_STATUS_IN_FLIGHT = "IN_FLIGHT"
ORACLE_STATUS_ACKED = "ACKED"
ORACLE_STATUS_FAILED_RETRYABLE = "FAILED_RETRYABLE"
ORACLE_STATUS_FAILED_PERMANENT = "FAILED_PERMANENT"
ORACLE_STATUS_BLOCKED_IDENTITY = "BLOCKED_IDENTITY"

DELIVERY_LIVE = "LIVE"
DELIVERY_BULK = "BULK"

ORACLE_ALLOWED_CAPTURE_TYPES = {
    SourceType.LIVE.value,
    SourceType.LIVE_POLL.value,
    SourceType.DUMP_RECONNECT.value,
    SourceType.DUMP_STARTUP.value,
    SourceType.MANUAL_REPROCESS.value,
}


def oracle_trust_status(value: str | TrustStatus | None) -> str:
    normalized = str(value or "")
    if normalized == TrustStatus.TRUSTED_LIVE.value:
        return "TRUSTED_LIVE"
    if normalized == TrustStatus.INTERNET_OFFLINE_TRUSTED_LOCAL.value:
        return "INTERNET_OFFLINE_TRUSTED_LOCAL"
    if normalized == TrustStatus.BACKFILL_ACCEPTED_CLOCK_OK.value:
        return "BACKFILL_ACCEPTED_CLOCK_OK"
    if normalized in {
        TrustStatus.BACKFILL_UNVERIFIED_BLIND_PERIOD.value,
        TrustStatus.BACKFILL_UNVERIFIED_AGENT_DOWN.value,
    }:
        return "BACKFILL_UNVERIFIED_BLIND_PERIOD"
    return "SUSPECT_DEVICE_TIME"


def oracle_capture_type(value: str | SourceType | None) -> str:
    normalized = str(value or "")
    if normalized in ORACLE_ALLOWED_CAPTURE_TYPES:
        return normalized
    return SourceType.MANUAL_REPROCESS.value


def build_oracle_event_payload(event: AttendanceEvent) -> dict[str, Any]:
    if not event.cnic or len(event.cnic) != 13 or not event.cnic.isdigit():
        raise ValueError("CNIC is required for Oracle attendance delivery.")
    clockdiff = "0"
    if event.device_drift_seconds is not None:
        clockdiff = f"{float(event.device_drift_seconds):.3f}".rstrip("0").rstrip(".")
    return {
        "event_uid": event.event_uid,
        "zone_id": event.zone_id,
        "device_id": event.device_id,
        "device_serial": event.device_serial,
        "user_id": event.user_id,
        "employee_name": event.employee_name or "",
        "cnic": event.cnic,
        "timestamp": iso_utc(event.device_event_time),
        "clockdiff": clockdiff,
        "capturetype": oracle_capture_type(event.source_type),
        "trust_status": oracle_trust_status(event.trust_status),
        "raw_punch": "T" if event.raw_punch else "F",
    }


class OracleAttendanceOutboxWriter:
    def enqueue_for_event(
        self,
        session: Session,
        event: AttendanceEvent,
        *,
        preferred_delivery: str,
    ) -> OracleAttendanceOutbox | None:
        existing = session.scalar(
            select(OracleAttendanceOutbox).where(OracleAttendanceOutbox.event_uid == event.event_uid)
        )
        if existing is not None:
            return existing
        status = (
            ORACLE_STATUS_PENDING
            if event.cnic and len(event.cnic) == 13 and event.cnic.isdigit()
            else ORACLE_STATUS_BLOCKED_IDENTITY
        )
        row = OracleAttendanceOutbox(
            attendance_event_id=event.id,
            event_uid=event.event_uid,
            status=status,
            delivery_mode=preferred_delivery,
            next_attempt_at=utc_now() if status == ORACLE_STATUS_PENDING else None,
            last_error=None if status == ORACLE_STATUS_PENDING else "CNIC is missing or invalid.",
        )
        session.add(row)
        session.flush()
        return row


oracle_outbox_writer = OracleAttendanceOutboxWriter()
oracle_sync_wakeup = threading.Event()


class OracleAttendanceClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_username: str,
        api_password: str,
        timeout: float = ORACLE_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_username = api_username
        self.api_password = api_password
        self.timeout = timeout

    def post_live(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
        return self._post_json(ORACLE_LIVE_PATH, payload)

    def post_bulk(
        self, *, batch_uid: str, events: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        return self._post_json(ORACLE_BULK_PATH, {"batch_uid": batch_uid, "events": events})

    def _post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
        headers = {
            "Content-Type": "application/json",
            "X-API-Username": self.api_username,
            "X-API-Password": self.api_password,
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}{path}", json=payload, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data, response.text[:1000]


class OracleSyncWorker(threading.Thread):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="oracle-attendance-sync-worker", daemon=True)
        self.stop_event = stop_event
        self.backoff_seconds = 2

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                did_work = self.sync_once()
                self.backoff_seconds = 2 if did_work else min(self.backoff_seconds + 1, 15)
            except Exception as exc:
                self._record_service_event("ORACLE_SYNC_WORKER_ERROR", str(exc))
                self.backoff_seconds = min(max(self.backoff_seconds * 2, 15), 120)
            oracle_sync_wakeup.wait(self.backoff_seconds)
            oracle_sync_wakeup.clear()

    def sync_once(self) -> bool:
        with session_scope() as session:
            config = config_manager.get(session)
            if not config or not config.oracle_attendance_configured:
                return False
            self._reset_stale_in_flight(session)
            self._sweep_missing_outbox_rows(session, config)
            client = OracleAttendanceClient(
                base_url=config.ords_base_url,
                api_username=config.ords_api_username,
                api_password=config.ords_api_password,
            )
            did_live = self._sync_live_once(session, client)
            did_bulk = self._sync_bulk_once(session, client)
            return did_live or did_bulk

    def _reset_stale_in_flight(self, session: Session) -> None:
        cutoff = utc_now() - timedelta(seconds=ORACLE_STALE_IN_FLIGHT_SECONDS)
        for row in session.scalars(
            select(OracleAttendanceOutbox).where(
                OracleAttendanceOutbox.status == ORACLE_STATUS_IN_FLIGHT,
                OracleAttendanceOutbox.last_attempt_at < cutoff,
            )
        ):
            row.status = ORACLE_STATUS_FAILED_RETRYABLE
            row.next_attempt_at = utc_now()
            row.updated_at = utc_now()

    def _sweep_missing_outbox_rows(self, session: Session, config: ActiveZoneConfig) -> int:
        cutover = ensure_utc(config.oracle_cutover_utc) if config.oracle_cutover_utc else None
        stmt = (
            select(AttendanceEvent)
            .outerjoin(
                OracleAttendanceOutbox,
                OracleAttendanceOutbox.attendance_event_id == AttendanceEvent.id,
            )
            .where(OracleAttendanceOutbox.id == None)  # noqa: E711
            .order_by(AttendanceEvent.id.asc())
            .limit(500)
        )
        if cutover is not None:
            stmt = stmt.where(AttendanceEvent.device_event_time >= cutover)
        events = session.scalars(stmt).all()
        for event in events:
            mode = DELIVERY_LIVE if event.source_type == SourceType.LIVE.value else DELIVERY_BULK
            oracle_outbox_writer.enqueue_for_event(session, event, preferred_delivery=mode)
        return len(events)

    def _sync_live_once(self, session: Session, client: OracleAttendanceClient) -> bool:
        now = utc_now()
        row = session.scalar(
            select(OracleAttendanceOutbox)
            .where(
                OracleAttendanceOutbox.status == ORACLE_STATUS_PENDING,
                OracleAttendanceOutbox.delivery_mode == DELIVERY_LIVE,
                OracleAttendanceOutbox.attempt_count == 0,
                (
                    (OracleAttendanceOutbox.next_attempt_at == None)  # noqa: E711
                    | (OracleAttendanceOutbox.next_attempt_at <= now)
                ),
            )
            .order_by(OracleAttendanceOutbox.id.asc())
            .limit(1)
        )
        if row is None:
            return False
        event = session.get(AttendanceEvent, row.attendance_event_id)
        if event is None:
            self._mark_permanent(row, 0, "Local attendance event was deleted.")
            return True
        try:
            payload = build_oracle_event_payload(event)
        except ValueError as exc:
            self._mark_blocked_identity(row, str(exc))
            return True

        self._mark_in_flight(row, payload, DELIVERY_LIVE, None)
        try:
            status, data, response_text = client.post_live(payload)
        except Exception as exc:
            self._mark_retryable(row, None, str(exc))
            self._record_service_event("ORACLE_ENDPOINT_OUTAGE", str(exc))
            return True

        if status in {200, 201} and data and data.get("success") is True:
            self._mark_acked(row, status)
        elif status == 409:
            self._mark_acked(row, status)
        elif status in {400, 422}:
            self._mark_permanent(row, status, _response_error(data, response_text))
        elif status in {401, 403}:
            self._mark_retryable(row, status, _response_error(data, response_text), delay_seconds=900)
            self._record_service_event("ORACLE_AUTH_FAILED", f"HTTP {status}: {_response_error(data, response_text)}")
        else:
            self._mark_retryable(row, status, _response_error(data, response_text))
        return True

    def _sync_bulk_once(self, session: Session, client: OracleAttendanceClient) -> bool:
        now = utc_now()
        rows = session.scalars(
            select(OracleAttendanceOutbox)
            .where(
                OracleAttendanceOutbox.status.in_(
                    [ORACLE_STATUS_PENDING, ORACLE_STATUS_FAILED_RETRYABLE]
                ),
                ~(
                    (OracleAttendanceOutbox.delivery_mode == DELIVERY_LIVE)
                    & (OracleAttendanceOutbox.attempt_count == 0)
                ),
                (
                    (OracleAttendanceOutbox.next_attempt_at == None)  # noqa: E711
                    | (OracleAttendanceOutbox.next_attempt_at <= now)
                ),
            )
            .order_by(OracleAttendanceOutbox.id.asc())
            .limit(ORACLE_BULK_CHUNK_SIZE)
        ).all()
        if not rows:
            return False

        payload_events: list[dict[str, Any]] = []
        send_rows: list[OracleAttendanceOutbox] = []
        batch_uid = f"ZONE-ORDS-{utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        for row in rows:
            event = session.get(AttendanceEvent, row.attendance_event_id)
            if event is None:
                self._mark_permanent(row, 0, "Local attendance event was deleted.")
                continue
            try:
                payload = build_oracle_event_payload(event)
            except ValueError as exc:
                self._mark_blocked_identity(row, str(exc))
                continue
            payload_events.append(payload)
            send_rows.append(row)
            self._mark_in_flight(row, payload, DELIVERY_BULK, batch_uid)

        if not send_rows:
            return True

        try:
            status, data, response_text = client.post_bulk(batch_uid=batch_uid, events=payload_events)
        except Exception as exc:
            for row in send_rows:
                self._mark_retryable(row, None, str(exc))
            self._record_service_event("ORACLE_ENDPOINT_OUTAGE", str(exc))
            return True

        if status in {200, 201} and _bulk_response_clean(data, len(send_rows)):
            for row in send_rows:
                self._mark_acked(row, status)
            return True

        if status in {401, 403}:
            error = _response_error(data, response_text)
            for row in send_rows:
                self._mark_retryable(row, status, error, delay_seconds=900)
            self._record_service_event("ORACLE_AUTH_FAILED", f"HTTP {status}: {error}")
            return True

        details = _bulk_result_status_by_event_uid(data)
        for row in send_rows:
            detail = details.get(row.event_uid)
            if detail == "DUPLICATE_EXISTING":
                self._mark_acked(row, status)
            elif detail == "INVALID":
                self._mark_permanent(row, status, _response_error(data, response_text))
            else:
                self._mark_retryable(row, status, _response_error(data, response_text))
        return True

    def _mark_in_flight(
        self,
        row: OracleAttendanceOutbox,
        payload: dict[str, Any],
        mode: str,
        batch_uid: str | None,
    ) -> None:
        now = utc_now()
        row.status = ORACLE_STATUS_IN_FLIGHT
        row.delivery_mode = mode
        row.attempt_count += 1
        row.last_attempt_at = now
        row.next_attempt_at = None
        row.last_payload_hash = payload_hash(payload)
        row.batch_uid = batch_uid
        row.updated_at = now

    def _mark_acked(self, row: OracleAttendanceOutbox, http_status: int | None) -> None:
        now = utc_now()
        row.status = ORACLE_STATUS_ACKED
        row.last_http_status = http_status
        row.last_error = None
        row.acked_at = now
        row.updated_at = now

    def _mark_retryable(
        self,
        row: OracleAttendanceOutbox,
        http_status: int | None,
        error: str,
        *,
        delay_seconds: int | None = None,
    ) -> None:
        now = utc_now()
        row.status = ORACLE_STATUS_FAILED_RETRYABLE
        row.last_http_status = http_status
        row.last_error = error[:2000]
        row.next_attempt_at = now + timedelta(seconds=delay_seconds or _retry_delay(row.attempt_count))
        row.updated_at = now

    def _mark_permanent(
        self,
        row: OracleAttendanceOutbox,
        http_status: int | None,
        error: str,
    ) -> None:
        now = utc_now()
        row.status = ORACLE_STATUS_FAILED_PERMANENT
        row.last_http_status = http_status
        row.last_error = error[:2000]
        row.next_attempt_at = None
        row.updated_at = now
        self._record_service_event("ORACLE_PERMANENT_VALIDATION_FAILED", row.last_error)

    def _mark_blocked_identity(self, row: OracleAttendanceOutbox, error: str) -> None:
        now = utc_now()
        row.status = ORACLE_STATUS_BLOCKED_IDENTITY
        row.last_error = error[:2000]
        row.next_attempt_at = None
        row.updated_at = now
        self._record_service_event("ORACLE_BLOCKED_IDENTITY", row.last_error)

    def _record_service_event(self, event_type: str, description: str) -> None:
        try:
            with session_scope() as session:
                session.add(ServiceEvent(event_type=event_type, description=description[:1000]))
        except Exception:
            pass


def _retry_delay(attempt_count: int) -> int:
    return min(300, max(5, 5 * (2 ** min(attempt_count, 6))))


def _response_error(data: dict[str, Any] | None, text: str | None) -> str:
    if data:
        if message := data.get("message"):
            return str(message)
        if errors := data.get("errors"):
            return "; ".join(str(item) for item in errors)
        return json.dumps(data, default=str, sort_keys=True)[:1000]
    return (text or "Oracle endpoint returned no JSON response.")[:1000]


def _bulk_response_clean(data: dict[str, Any] | None, expected_count: int) -> bool:
    if not data or data.get("success") is not True:
        return False
    inserted = int(data.get("inserted_count") or 0)
    duplicates = int(data.get("duplicate_existing_count") or 0)
    invalid = int(data.get("invalid_count") or 0)
    failed = int(data.get("failed_count") or 0)
    duplicate_in_request = int(data.get("duplicate_in_request_count") or 0)
    return (
        inserted + duplicates == expected_count
        and invalid == 0
        and failed == 0
        and duplicate_in_request == 0
    )


def _bulk_result_status_by_event_uid(data: dict[str, Any] | None) -> dict[str, str]:
    if not data:
        return {}
    result: dict[str, str] = {}
    for item in data.get("results") or []:
        event_uid = str(item.get("event_uid") or "")
        status = str(item.get("status") or "")
        if event_uid:
            result[event_uid] = status
    return result
