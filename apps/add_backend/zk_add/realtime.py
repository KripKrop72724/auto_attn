from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

from fastapi import WebSocket

from zk_add.time_utils import utc_now


@dataclass(frozen=True)
class LiveEvent:
    event_id: int
    event_type: str
    data: dict
    created_at: datetime


class BrowserEventHub:
    def __init__(self, history_size: int = 1000) -> None:
        self._next_id = 1
        self._history: deque[LiveEvent] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[LiveEvent]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: dict) -> LiveEvent:
        async with self._lock:
            event = LiveEvent(self._next_id, event_type, data, utc_now())
            self._next_id += 1
            self._history.append(event)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def subscribe(self, last_event_id: int | None = None) -> AsyncIterator[LiveEvent]:
        queue: asyncio.Queue[LiveEvent] = asyncio.Queue(maxsize=500)
        async with self._lock:
            backlog = [item for item in self._history if not last_event_id or item.event_id > last_event_id]
            self._subscribers.add(queue)
        try:
            for item in backlog:
                yield item
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield LiveEvent(0, "keepalive", {}, utc_now())
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
    if event.event_type == "keepalive":
        return ": keepalive\n\n"
    data = json.dumps(event.data, separators=(",", ":"), default=str)
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {data}\n\n"
