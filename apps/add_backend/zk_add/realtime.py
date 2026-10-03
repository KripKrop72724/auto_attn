from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator
from uuid import uuid4

from fastapi import WebSocket

from zk_add.time_utils import utc_now


# Device wire types are not browser topics. Keep this boundary in one place so
# HTTP and WebSocket producers invalidate the same browser data.
BROWSER_TOPICS = {
    "heartbeat": "device",
    "command_update": "command",
    "user_snapshot": "users",
    "attendance_batch": "attendance",
    "zkt_observation_batch": "attendance",
    "oracle_receipt_batch": "attendance",
    "queue_evidence": "reconciliation",
    "reconcile_anchor": "reconciliation",
    "reconcile_assignment_release": "reconciliation",
    "reconcile_chunk": "reconciliation",
    "reconcile_source_manifest": "reconciliation",
    "source_probe_result": "reconciliation",
    "source_tail_chunk": "reconciliation",
    "hikvision_profile_page": "users",
    "hikvision_history_page": "reconciliation",
    "hikvision_observation": "attendance",
}


@dataclass(frozen=True)
class LiveEvent:
    event_id: int
    event_type: str
    data: dict
    created_at: datetime
    generation: str | None = None


class BrowserEventHub:
    def __init__(self, history_size: int = 1000) -> None:
        self._next_id = 1
        self._generation = uuid4().hex
        self._history: deque[LiveEvent] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[LiveEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: dict) -> LiveEvent:
        async with self._lock:
            event = LiveEvent(self._next_id, BROWSER_TOPICS.get(event_type, event_type),
                              data, utc_now(), self._generation)
            self._next_id += 1
            self._history.append(event)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                # Losing any invalidation requires a full snapshot. A later
                # unrelated event cannot stand in for the discarded update.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(LiveEvent(0, "resync", {"reason": "overflow"}, utc_now()))
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def subscribe(self, last_event_id: str | int | None = None) -> AsyncIterator[LiveEvent]:
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=500)
        generation, _, sequence = str(last_event_id or "").rpartition(":")
        cursor = int(sequence) if sequence.isdigit() and generation == self._generation else None
        async with self._lock:
            can_replay = (cursor is not None and self._history
                          and self._history[0].event_id - 1 <= cursor < self._next_id)
            backlog = [item for item in self._history if item.event_id > cursor] if can_replay else []
            self._subscribers.add(queue)
        try:
            # Always fetch current snapshots on connection, including a server
            # restart, a legacy cursor or history eviction. SSE is a hint, not
            # the authoritative device cache.
            yield LiveEvent(0, "resync", {"generation": self._generation}, utc_now())
            for item in backlog:
                yield item
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield LiveEvent(0, "keepalive", {"generation": self._generation}, utc_now())
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


class ConnectorHub:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def connect(self, connector_id: str, websocket: WebSocket) -> None:
        offered = websocket.headers.get("sec-websocket-protocol", "")
        protocol = "add-device-v2" if "add-device-v2" in offered else "add-device-v1"
        await websocket.accept(subprotocol=protocol)
        async with self._lock:
            previous = self._connections.get(connector_id)
            self._connections[connector_id] = websocket
            self._send_locks.setdefault(connector_id, asyncio.Lock())
        if previous and previous is not websocket:
            try:
                await previous.close(code=4001, reason="Superseded by a newer connector session")
            except Exception:
                pass

    async def disconnect(self, connector_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(connector_id) is websocket:
                self._connections.pop(connector_id, None)

    async def send(self, connector_id: str, payload: dict) -> bool:
        return await self.send_many(connector_id, [payload])

    async def send_many(self, connector_id: str, payloads: list[dict]) -> bool:
        async with self._lock:
            websocket = self._connections.get(connector_id)
            send_lock = self._send_locks.setdefault(connector_id, asyncio.Lock())
        if websocket is None:
            return False
        async with send_lock:
            try:
                for payload in payloads:
                    await websocket.send_text(
                        json.dumps(payload, separators=(",", ":"), default=str)
                    )
                return True
            except Exception:
                await self.disconnect(connector_id, websocket)
                return False

    async def is_connected(self, connector_id: str) -> bool:
        async with self._lock:
            return connector_id in self._connections


browser_events = BrowserEventHub()
connector_hub = ConnectorHub()


def sse_encode(event: LiveEvent) -> str:
    data = json.dumps(event.data, separators=(",", ":"), default=str)
    cursor = f"{event.generation}:{event.event_id}" if event.generation else str(event.event_id)
    id_line = f"id: {cursor}\n" if event.event_id else ""
    return f"{id_line}event: {event.event_type}\ndata: {data}\n\n"
