"""Device WebSocket sessions that keep reading while a message is processed.

uvicorn stops reading a WebSocket after every complete message and resumes
only when the application asks for the next one. ADD used to finish each
device message's database transaction before it read again, so pongs, the
device's own pings and the rest of a large upload stayed unread for the whole
transaction. uvicorn's keepalive then closed a slow transaction's socket as a
dead peer, and a device whose upload could not drain aborted its own session.

A session runs one reader task from the moment the socket is accepted. It
drains the socket into a bounded FIFO while the endpoint processes messages
one at a time, strictly in arrival order, exactly like the former single
receive loop. The FIFO never drops a message: when it is full the reader
simply stops asking for more, which is the old behaviour, until the worker
catches up.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable
from typing import Any, TypeVar

from starlette.websockets import WebSocketDisconnect

T = TypeVar("T")

# uvicorn keepalive for every ADD launch (Compose, rollback, image default and
# the development entry point). A device sending its full user snapshot holds
# its client lock for the whole message and cannot answer a ping until it is
# done, so the pong deadline must outlast a slow upload, not just a round trip.
WS_PING_INTERVAL_SECONDS = 20
WS_PING_TIMEOUT_SECONDS = 60

# One user snapshot can approach the 512 KiB envelope limit. The byte budget
# keeps several of them readable; a count bound alone would pause reads again.
INBOUND_QUEUE_MAX_MESSAGES = 32
INBOUND_QUEUE_MAX_BYTES = 2 * 1024 * 1024

END_OF_STREAM: Any = object()


def message_size(data: str | bytes) -> int:
    if isinstance(data, bytes):
        return len(data)
    return len(data) if data.isascii() else len(data.encode("utf-8"))


class InboundQueue:
    """FIFO for one producer and one consumer, bounded by count and bytes.

    An empty queue always admits the next message, so one large message can
    never wait for space that cannot appear. Nothing is ever dropped.
    """

    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        if max_messages < 1 or max_bytes < 1:
            raise ValueError("The inbound queue needs room for at least one message")
        self._max_messages = max_messages
        self._max_bytes = max_bytes
        self._items: deque[tuple[Any, int]] = deque()
        self._bytes = 0
        self._finished = False
        self._readable = asyncio.Event()
        self._writable = asyncio.Event()
        self._writable.set()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def bytes(self) -> int:
        return self._bytes

    def _admits(self, size: int) -> bool:
        if not self._items:
            return True
        return len(self._items) < self._max_messages and self._bytes + size <= self._max_bytes

    async def put(self, item: Any, size: int) -> bool:
        """Append in order, waiting while full. Returns whether it had to wait."""
        waited = False
        while not self._admits(size):
            waited = True
            self._writable.clear()
            await self._writable.wait()
        self._items.append((item, size))
        self._bytes += size
        self._readable.set()
        return waited

    async def get(self) -> Any:
        """Return the oldest item, or END_OF_STREAM once finished and drained."""
        while not self._items:
            if self._finished:
                return END_OF_STREAM
            self._readable.clear()
            await self._readable.wait()
        item, size = self._items.popleft()
        self._bytes -= size
        self._writable.set()
        return item

    def finish(self) -> None:
        self._finished = True
        self._readable.set()


class DeviceStreamSession:
    """Reader task, ordered message intake and non-raising sends for one socket."""

    def __init__(
        self,
        websocket: Any,
        connector_id: str,
        *,
        max_messages: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.websocket = websocket
        self.connector_id = connector_id
        self.queue = InboundQueue(
            max_messages=max_messages or INBOUND_QUEUE_MAX_MESSAGES,
            max_bytes=max_bytes or INBOUND_QUEUE_MAX_BYTES,
        )
        self.disconnect_code: int | None = None
        self.reader_error: str | None = None
        self._reader: asyncio.Task[None] | None = None
        self._in_flight: asyncio.Future[Any] | None = None
        self.in_flight_type: str | None = None
        self._transport_closed = False

    @property
    def transport_closed(self) -> bool:
        return self._transport_closed

    def start(self) -> None:
        """Start reading. Call immediately after accept, before any bootstrap work."""
        if self._reader is None:
            self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    self.disconnect_code = int(message.get("code") or 1005)
                    self._transport_closed = True
                    return
                data = message.get("text")
                if data is None:
                    data = message.get("bytes") or b""
                await self.queue.put(data, message_size(data))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A broken receive path ends the stream; it never interrupts the
            # message the worker is processing.
            self.reader_error = type(error).__name__
            self._transport_closed = True
        finally:
            self.queue.finish()

    async def next_message(self) -> str | bytes | None:
        """Next complete message in arrival order; None once the stream ended."""
        item = await self.queue.get()
        return None if item is END_OF_STREAM else item

    async def process(self, message_type: str, work: Awaitable[T]) -> T:
        """Run one message's work so that session teardown cannot abandon it.

        Database work already runs in a thread that cancellation cannot stop,
        so a cancelled session would otherwise commit the message but never
        send its ACK, and its cleanup could overtake the commit. The message
        finishes, ACK included, before the cancellation continues; a second
        cancellation stops only this wait, never the message itself.
        """
        task = asyncio.ensure_future(work)
        self._in_flight = task
        self.in_flight_type = message_type
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                await asyncio.wait({task})
                if not task.cancelled():
                    task.exception()
                raise
        finally:
            self._in_flight = None
            self.in_flight_type = None

    async def send_json(self, payload: dict) -> bool:
        """Send one message in order. A closed socket is reported, never raised.

        Encoding happens first, exactly as Starlette's send_json does it, so a
        payload that cannot be encoded still fails as a processing error.
        """
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if self._transport_closed:
            return False
        try:
            await self.websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # Starlette and uvicorn report a socket closed by the peer, by
            # keepalive or by this server as one of these. The worker still
            # finishes the queued messages; the device replays unACKed ones.
            self._transport_closed = True
            return False
        return True

    async def close(self, code: int, reason: str = "") -> None:
        self._transport_closed = True
        try:
            await self.websocket.close(code=code, reason=reason or None)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    async def stop(self) -> None:
        """Stop the reader. Messages it has not handed over are abandoned unACKed."""
        reader = self._reader
        if reader is None:
            return
        if not reader.done():
            reader.cancel()
        await asyncio.wait({reader})
