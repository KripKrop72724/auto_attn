from __future__ import annotations

import json
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
from zk_common.time_utils import parse_datetime, utc_now
from zk_zone_agent.audit import audit_ledger
from zk_zone_agent.config import ActiveZoneConfig, config_manager
from zk_zone_agent.db import SyncQueue, session_scope
from zk_zone_agent.trusted_time import trusted_time_service


class HeadOfficeClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def register_zone(self, request: ZoneRegisterRequest) -> ZoneRegisterResponse:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/api/zones/register", json=request.model_dump())
            response.raise_for_status()
            return ZoneRegisterResponse.model_validate(response.json())

    def get_time(self) -> datetime:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/api/time", headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return parse_datetime(data["server_utc"])

    def post_json(self, path: str, payload: dict[str, Any]) -> SyncResponse:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}{path}", json=payload, headers=self.headers)
            response.raise_for_status()
            return SyncResponse.model_validate(response.json())


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
            client = HeadOfficeClient(config.head_office_url, config.zone_token)
            server_utc = client.get_time()
            trusted_time_service.update_from_head_office(server_utc, session)
            pending = session.scalars(
                select(SyncQueue)
                .where(SyncQueue.status.in_([SyncStatus.PENDING.value, SyncStatus.FAILED.value]))
                .order_by(SyncQueue.id.asc())
                .limit(500)
            ).all()
            if not pending:
                return False
            grouped: dict[str, list[SyncQueue]] = defaultdict(list)
            for row in pending:
                grouped[row.payload_type].append(row)
            for payload_type, rows in grouped.items():
                self._sync_group(session, client, config, payload_type, rows)
            return True

    def _sync_group(
        self,
        session: Session,
        client: HeadOfficeClient,
        config: ActiveZoneConfig,
        payload_type: str,
        rows: list[SyncQueue],
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
                now = utc_now()
                for row in chunk:
                    row.status = SyncStatus.FAILED.value
                    row.attempt_count += 1
                    row.last_attempt_at = now
                    row.last_error = str(exc)
                continue
            now = utc_now()
            acked_uids = set(response.acked_event_uids)
            acked_ids = set(response.acked_ids)
            for row in chunk:
                row.attempt_count += 1
                row.last_attempt_at = now
                if row.event_uid and row.event_uid in acked_uids:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                elif row.record_id and row.record_id in acked_ids:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                elif response.ok:
                    row.status = SyncStatus.ACKED.value
                    row.acked_at = now
                else:
                    row.status = SyncStatus.FAILED.value
                    row.last_error = "; ".join(response.errors)

    def _payload_for_sync(self, row: SyncQueue, config: ActiveZoneConfig) -> dict[str, Any]:
        payload = json.loads(row.payload_json)
        if isinstance(payload, dict) and "zone_id" in payload:
            payload["zone_id"] = config.zone_id
        return payload


def _chunk(items: list[SyncQueue], size: int) -> list[list[SyncQueue]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
