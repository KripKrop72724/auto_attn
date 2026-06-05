from __future__ import annotations

import json
import secrets
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.enums import PayloadType, SyncStatus
from zk_common.hashing import canonical_json
from zk_common.schemas import (
    AttendanceSyncRequest,
    ClockChecksSyncRequest,
    IncidentSyncRequest,
    OutageSyncRequest,
    SyncResponse,
    ZoneRegisterRequest,
    ZoneRegisterResponse,
)
from zk_common.security import body_sha256, sign_request, signed_timestamp
from zk_common.time_utils import parse_datetime, utc_now
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.config import ActiveZoneConfig, config_manager
from zk_zone_agent.db import SyncQueue, run_session_with_retries, session_scope
from zk_zone_agent.trusted_time import trusted_time_service


class HeadOfficeClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        zone_id: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.zone_id = zone_id
        self.timeout = timeout

    def register_zone(self, request: ZoneRegisterRequest) -> ZoneRegisterResponse:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/api/zones/register", json=request.model_dump())
            response.raise_for_status()
            return ZoneRegisterResponse.model_validate(response.json())

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/api/health")
            response.raise_for_status()
            return response.json()

    def get_time(self) -> datetime:
        data = self._request_json("GET", "/api/time")
        return parse_datetime(data["server_utc"])

    def post_json(self, path: str, payload: dict[str, Any]) -> SyncResponse:
        data = self._request_json("POST", path, payload)
        return SyncResponse.model_validate(data)

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = b"" if payload is None else canonical_json(payload).encode("utf-8")
        headers = self._signed_headers(method, path, body)
        if body:
            headers["Content-Type"] = "application/json"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                content=body if body else None,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    def _signed_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        if not self.token or not self.zone_id:
            return {}
        timestamp = signed_timestamp()
        nonce = secrets.token_urlsafe(18)
        body_hash = body_sha256(body)
        signature = sign_request(
            token=self.token,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body_hash=body_hash,
        )
        return {
            "Authorization": f"Bearer {self.token}",
            "X-ZK-Zone-Id": self.zone_id,
            "X-ZK-Timestamp": timestamp,
            "X-ZK-Nonce": nonce,
            "X-ZK-Body-SHA256": body_hash,
            "X-ZK-Signature": signature,
        }


class SyncQueueWriter:
    def enqueue(
        self,
        session: Session,
        *,
        payload_type: PayloadType,
        payload: object,
        event_uid: str | None = None,
        record_id: str | int | None = None,
    ) -> SyncQueue:
        row = SyncQueue(
            payload_type=payload_type.value,
            payload_json=canonical_json(payload),
            event_uid=event_uid,
            record_id=None if record_id is None else str(record_id),
            status=SyncStatus.PENDING.value,
        )
        session.add(row)
        session.flush()
        audit_ledger.append(session, f"sync_queue:{payload_type.value}", row.id, payload)
        return row

    def pending_count(self, session: Session) -> int:
        return len(
            session.scalars(
                select(SyncQueue).where(SyncQueue.status.in_([SyncStatus.PENDING.value, SyncStatus.FAILED.value]))
            ).all()
        )


sync_queue_writer = SyncQueueWriter()


class SyncWorker(threading.Thread):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name="sync-worker", daemon=True)
        self.stop_event = stop_event
        self.backoff_seconds = 5

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                did_work = self.sync_once()
                self.backoff_seconds = 5 if did_work else min(self.backoff_seconds, 15)
            except Exception:
                self.backoff_seconds = min(max(self.backoff_seconds * 2, 15), 60)
            self.stop_event.wait(self.backoff_seconds)

    def sync_once(self) -> bool:
        with session_scope() as session:
            config = config_manager.get(session)
        if not config or not config.setup_completed or not config.zone_token or not config.head_office_url:
            return False

        client = HeadOfficeClient(config.head_office_url, config.zone_token, config.zone_id)
        server_utc = client.get_time()

        def update_trusted_time(session: Session) -> None:
            trusted_time_service.update_from_head_office(server_utc, session)

        run_session_with_retries(update_trusted_time, attempts=6, base_delay_seconds=0.1)

        with session_scope() as session:
            pending = session.scalars(
                select(SyncQueue)
                .where(SyncQueue.status.in_([SyncStatus.PENDING.value, SyncStatus.FAILED.value]))
                .order_by(SyncQueue.id.asc())
                .limit(500)
            ).all()
            pending_items = [
                {
                    "id": row.id,
                    "payload_type": row.payload_type,
                    "payload_json": row.payload_json,
                    "event_uid": row.event_uid,
                    "record_id": row.record_id,
                }
                for row in pending
            ]
        if not pending_items:
            return False
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in pending_items:
            grouped[str(row["payload_type"])].append(row)
        did_sync_work = False
        for payload_type, rows in grouped.items():
            if payload_type == PayloadType.ATTENDANCE.value and config.oracle_attendance_configured:
                continue
            self._sync_group(client, config, payload_type, rows)
            did_sync_work = True
        return did_sync_work

    def _sync_group(
        self,
        client: HeadOfficeClient,
        config: ActiveZoneConfig,
        payload_type: str,
        rows: list[dict[str, Any]],
    ) -> None:
        if payload_type == PayloadType.ATTENDANCE.value:
            chunks = _chunk(rows, 100)
            path = "/api/sync/attendance"
            key = "events"
            request_model = AttendanceSyncRequest
        elif payload_type == PayloadType.CLOCK_CHECK.value:
            chunks = _chunk(rows, 500)
            path = "/api/sync/clock-checks"
            key = "clock_checks"
            request_model = ClockChecksSyncRequest
        elif payload_type == PayloadType.OUTAGE.value:
            chunks = _chunk(rows, 100)
            path = "/api/sync/outages"
            key = "outages"
            request_model = OutageSyncRequest
        elif payload_type == PayloadType.INCIDENT.value:
            chunks = _chunk(rows, 100)
            path = "/api/sync/incidents"
            key = "incidents"
            request_model = IncidentSyncRequest
        else:
            return

        for chunk in chunks:
            payload_items = [self._payload_for_sync(row, config) for row in chunk]
            request = request_model(zone_id=config.zone_id, batch_id=str(uuid.uuid4()), **{key: payload_items})
            try:
                response = client.post_json(path, request.model_dump(mode="json"))
            except Exception as exc:
                self._mark_chunk_failed(chunk, str(exc))
                continue
            self._mark_chunk_response(chunk, response)

    def _mark_chunk_failed(self, rows: list[dict[str, Any]], error: str) -> None:
        row_ids = [int(row["id"]) for row in rows]

        def operation(session: Session) -> None:
            now = utc_now()
            for row_id in row_ids:
                row = session.get(SyncQueue, row_id)
                if row is None or row.status == SyncStatus.ACKED.value:
                    continue
                row.status = SyncStatus.FAILED.value
                row.attempt_count += 1
                row.last_attempt_at = now
                row.last_error = error[:2000]

        run_session_with_retries(operation, attempts=6, base_delay_seconds=0.1)

    def _mark_chunk_response(self, rows: list[dict[str, Any]], response: SyncResponse) -> None:
        row_ids = [int(row["id"]) for row in rows]
        row_meta = {int(row["id"]): row for row in rows}
        acked_uids = set(response.acked_event_uids)
        acked_ids = set(response.acked_ids)

        def operation(session: Session) -> None:
            now = utc_now()
            for row_id in row_ids:
                row = session.get(SyncQueue, row_id)
                if row is None or row.status == SyncStatus.ACKED.value:
                    continue
                meta = row_meta[row_id]
                row.attempt_count += 1
                row.last_attempt_at = now
                if meta["event_uid"] and meta["event_uid"] in acked_uids:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                elif meta["record_id"] and meta["record_id"] in acked_ids:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                elif response.ok:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                else:
                    row.status = SyncStatus.FAILED.value
                    row.last_error = "; ".join(response.errors)

        run_session_with_retries(operation, attempts=6, base_delay_seconds=0.1)

    def _payload_for_sync(self, row: dict[str, Any] | SyncQueue, config: ActiveZoneConfig) -> dict[str, Any]:
        payload_json = row["payload_json"] if isinstance(row, dict) else row.payload_json
        payload = json.loads(str(payload_json))
        if isinstance(payload, dict) and "zone_id" in payload:
            payload["zone_id"] = config.zone_id
        return payload


def _chunk(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
