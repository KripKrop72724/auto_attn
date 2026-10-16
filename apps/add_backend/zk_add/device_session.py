"""Device WebSocket sessions that keep reading while a message is processed.

uvicorn stops reading a WebSocket when a message arrives while the application
is busy, and resumes only when the application asks for the next one. ADD used
to push its connect bootstrap before the first receive and to finish each
device message's database transaction before reading again, so pongs, the
device's own pings and the rest of a large upload stayed unread meanwhile.
uvicorn's keepalive then closed the socket as a dead peer, and a device whose
upload could not drain aborted its own session.

A session runs one reader task from the moment the socket is accepted. It
drains the socket into a bounded FIFO while the endpoint processes messages
one at a time, strictly in arrival order, like the former single receive loop.
The FIFO never drops a message: when it is full the reader simply stops asking
for more, which is the old behaviour, until the worker catches up.

When the socket closes, messages still queued are handed over only where the
former loop would also have gone on: after an abrupt drop, and only while no
newer session is registered for the connector. After an orderly close (this
server superseding, interrupting, shutting down or timing out the socket, or a
close frame from the device) the message in flight finishes and nothing else
starts; the device replays everything it has not had ACKed.

Each session also records why and how it ended (one log line per session,
plus one per slow message) so reconnect storms can be attributed to the
server, the device or the network. Nothing logged carries a payload, a token
or a peer-supplied string.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from starlette.websockets import WebSocketDisconnect

T = TypeVar("T")
logger = logging.getLogger(__name__)

# uvicorn keepalive for every ADD launch (Compose, rollback, image default and
# the development entry point). A device sending its full user snapshot holds
# its client lock for the whole message and cannot answer a ping until it is
# done, so the pong deadline must outlast a slow upload, not just a round trip.
WS_PING_INTERVAL_SECONDS = 20
WS_PING_TIMEOUT_SECONDS = 60
# The uvicorn protocol that reports keepalive closes (zk_add/ws_protocol.py).
# Only images that contain that module may name it; the rollback override runs
# an older image and therefore keeps uvicorn's default protocol.
WS_PROTOCOL = "zk_add.ws_protocol:DiagnosticWebSocketProtocol"

# One user snapshot can approach the 512 KiB envelope limit. The byte budget
# keeps several of them readable; a count bound alone would pause reads again.
INBOUND_QUEUE_MAX_MESSAGES = 32
INBOUND_QUEUE_MAX_BYTES = 2 * 1024 * 1024

# ASGI scope extension through which the uvicorn protocol shares transport
# facts (keepalive close, ping round trip, wire bytes) with the endpoint.
TRANSPORT_EXTENSION = "zk_add.ws_transport"

END_OF_STREAM: Any = object()
# Disconnect codes uvicorn reports when no close frame was exchanged.
ABRUPT_CLOSE_CODES = frozenset({1005, 1006})
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")


def message_size(data: str | bytes) -> int:
    if isinstance(data, bytes):
        return len(data)
    return len(data) if data.isascii() else len(data.encode("utf-8"))


def safe_label(value: object) -> str:
    """A log-safe identifier, or '-' for anything that is not one."""
    text = str(value) if value is not None else ""
    return text if _SAFE_LABEL.match(text) else "-"


def _seconds(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


class WebSocketTransportFacts:
    """Payload-free facts a uvicorn protocol records about one connection."""

    __slots__ = (
        "connector_id", "keepalive_timed_out", "server_shutdown", "last_ping_rtt", "pings_sent",
        "pongs_received", "bytes_received", "last_data_at",
    )

    def __init__(self) -> None:
        self.connector_id: str | None = None
        self.keepalive_timed_out = False
        self.server_shutdown = False
        self.last_ping_rtt: float | None = None
        self.pings_sent = 0
        self.pongs_received = 0
        self.bytes_received = 0
        self.last_data_at: float | None = None


def transport_facts(websocket: Any) -> WebSocketTransportFacts | None:
    scope = getattr(websocket, "scope", None)
    if not isinstance(scope, dict):
        return None
    facts = (scope.get("extensions") or {}).get(TRANSPORT_EXTENSION)
    return facts if isinstance(facts, WebSocketTransportFacts) else None


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
        self.peak_messages = 0
        self.peak_bytes = 0
        self.full_waits = 0

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
            if not waited:
                self.full_waits += 1
            waited = True
            self._writable.clear()
            await self._writable.wait()
        self._items.append((item, size))
        self._bytes += size
        self.peak_messages = max(self.peak_messages, len(self._items))
        self.peak_bytes = max(self.peak_bytes, self._bytes)
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


class _InFlight:
    __slots__ = ("message_type", "started", "thread_queued", "thread_started", "thread_finished")

    def __init__(self, message_type: str, started: float) -> None:
        self.message_type = message_type
        self.started = started
        self.thread_queued: float | None = None
        self.thread_started: float | None = None
        self.thread_finished: float | None = None


class DeviceStreamSession:
    """Reader task, ordered message intake, non-raising sends and diagnostics."""

    def __init__(
        self,
        websocket: Any,
        connector_id: str,
        *,
        max_messages: int | None = None,
        max_bytes: int | None = None,
        slow_message_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        is_current: Callable[[], bool] | None = None,
    ) -> None:
        self.websocket = websocket
        self.connector_id = connector_id
        # Whether this socket is still the one registered for the connector;
        # once a newer session exists this one must not start anything new.
        self._is_current = is_current
        self.queue = InboundQueue(
            max_messages=max_messages or INBOUND_QUEUE_MAX_MESSAGES,
            max_bytes=max_bytes or INBOUND_QUEUE_MAX_BYTES,
        )
        self.slow_message_seconds = slow_message_seconds
        self._clock = clock
        self.started_at = clock()
        self.startup_seconds: float | None = None
        self.disconnect_code: int | None = None
        self.reader_error: str | None = None
        self._reader: asyncio.Task[None] | None = None
        self._in_flight: _InFlight | None = None
        self._transport_closed = False
        # First close code exchanged on this socket: sent by this server (the
        # session or the connector hub) or received in the device's close frame.
        self._close_code: int | None = None
        self._send_failed = False
        self.messages_in = 0
        self.bytes_in = 0
        self.messages_out = 0
        self.bytes_out = 0
        self.handed_over = 0
        self.undelivered = 0
        self.last_message_at: float | None = None
        self.slowest: tuple[str, float] | None = None
        self.end: dict[str, Any] | None = None
        self.transport = transport_facts(websocket)
        if self.transport is not None:
            self.transport.connector_id = connector_id
        self._meter_sends()

    @property
    def transport_closed(self) -> bool:
        facts = self.transport
        return self._transport_closed or (
            facts is not None and (facts.keepalive_timed_out or facts.server_shutdown)
        )

    def is_current(self) -> bool:
        return self._is_current is None or bool(self._is_current())

    @property
    def attached(self) -> bool:
        """The socket is open and still the connector's registered socket."""
        return not self.transport_closed and self.is_current()

    def stop_reason(self) -> tuple[str, int | None] | None:
        """Why no further queued message may be handed over, or None.

        Every orderly close ends intake at once, as the former loop's failing
        receive or ACK did: a close sent by this server (the hub superseding or
        interrupting the socket, or the session itself), uvicorn's shutdown or
        keepalive close, a close frame from the device, or any send that found
        the socket closed. After an abrupt drop the queue is still drained, as
        the former loop drained uvicorn's buffer, but never once a newer
        session is registered: its messages would race the newer session's.
        """
        if self._close_code is not None:
            return self._disconnect_kind(self._close_code), self._close_code
        facts = self.transport
        if facts is not None and facts.server_shutdown:
            return "server_shutdown", 1012
        if facts is not None and facts.keepalive_timed_out:
            return "keepalive_timeout", None
        if self._send_failed:
            return "connection_lost", None
        if not self.is_current():
            return "superseded", None
        return None

    def should_stop(self) -> bool:
        """True once intake has ended; the first call records why the session ended."""
        reason = self.stop_reason()
        if reason is None:
            return False
        self.mark_end(*reason)
        return True

    def _meter_sends(self) -> None:
        # Every send to this socket (ACKs here, catalog, coverage and commands
        # through the connector hub, and the hub's own 4001 or 1013 close) goes
        # through the instance's send method.
        send = getattr(self.websocket, "send", None)
        if not callable(send):
            return

        async def metered_send(message: dict) -> None:
            kind = message.get("type")
            if kind == "websocket.close":
                # Whoever closes the socket, intake ends at once, before the
                # close is even written.
                self._transport_closed = True
                if self._close_code is None:
                    self._close_code = int(message.get("code") or 1000)
            try:
                await send(message)
            except Exception:
                if kind == "websocket.send":
                    # The pinned uvicorn (0.51) writes into an abruptly lost
                    # connection silently, so there a failed send means a close
                    # frame went one way or the other. Later uvicorn also fails
                    # sends after an abrupt loss; ending intake then only leaves
                    # more for the device to replay.
                    self._transport_closed = True
                    self._send_failed = True
                raise
            if kind == "websocket.send":
                data = message.get("text")
                self.messages_out += 1
                self.bytes_out += message_size(data if data is not None else message.get("bytes") or b"")

        self.websocket.send = metered_send

    def start(self) -> None:
        """Start reading. Call immediately after accept, before any bootstrap work."""
        if self._reader is None:
            self._reader = asyncio.create_task(self._read())

    def startup_complete(self) -> None:
        self.startup_seconds = self._clock() - self.started_at

    async def _read(self) -> None:
        try:
            while True:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    code = int(message.get("code") or 1005)
                    self.disconnect_code = code
                    self._transport_closed = True
                    if code not in ABRUPT_CLOSE_CODES and self._close_code is None:
                        self._close_code = code
                    self.mark_end(self._disconnect_kind(code), code)
                    return
                data = message.get("text")
                if data is None:
                    data = message.get("bytes") or b""
                size = message_size(data)
                self.messages_in += 1
                self.bytes_in += size
                self.last_message_at = self._clock()
                await self.queue.put(data, size)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A broken receive path ends the stream; it never interrupts the
            # message the worker is processing.
            self.reader_error = type(error).__name__
            self._transport_closed = True
            self.mark_end("reader_error")
        finally:
            self.queue.finish()

    def _disconnect_kind(self, code: int) -> str:
        if code in ABRUPT_CLOSE_CODES:
            # uvicorn reports its own keepalive close to the app as 1005 too;
            # only the protocol hook can tell the two apart.
            if self.transport is not None and self.transport.keepalive_timed_out:
                return "keepalive_timeout"
            return "connection_lost"
        if code == 1012:
            return "server_shutdown"
        if code == 4001:
            return "superseded"
        return "closed"

    async def next_message(self) -> str | bytes | None:
        """Next complete message in arrival order; None once intake has ended.

        A message refused because intake ended stays unACKed (it is counted as
        unprocessed) and the device replays it.
        """
        item = await self.queue.get()
        if item is END_OF_STREAM or self.should_stop():
            return None
        self.handed_over += 1
        return item

    async def run_in_thread(self, function: Callable[..., T], *args: Any) -> T:
        """asyncio.to_thread that also times the executor wait and the call."""
        record = self._in_flight
        if record is None:
            return await asyncio.to_thread(function, *args)
        clock = self._clock
        record.thread_queued = clock()

        def timed() -> T:
            record.thread_started = clock()
            try:
                return function(*args)
            finally:
                record.thread_finished = clock()

        return await asyncio.to_thread(timed)

    async def process(self, message_type: str, work: Awaitable[T]) -> T:
        """Run one message's work so that session teardown cannot abandon it.

        Database work already runs in a thread that cancellation cannot stop,
        so a cancelled session would otherwise commit the message but never
        send its ACK, and its cleanup could overtake the commit. The message
        finishes, ACK included, before the cancellation continues; a second
        cancellation stops only this wait, never the message itself.
        """
        record = _InFlight(safe_label(message_type), self._clock())
        task = asyncio.ensure_future(work)
        self._in_flight = record
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    raise
                self.mark_end("cancelled")
                await asyncio.wait({task})
                if not task.cancelled():
                    task.exception()
                raise
        finally:
            self._in_flight = None
            self._finish_message(record)

    def _finish_message(self, record: _InFlight) -> None:
        duration = self._clock() - record.started
        if self.slowest is None or duration > self.slowest[1]:
            self.slowest = (record.message_type, duration)
        if duration < self.slow_message_seconds:
            return
        executor_wait = transaction = None
        if record.thread_queued is not None and record.thread_started is not None:
            executor_wait = record.thread_started - record.thread_queued
            if record.thread_finished is not None:
                transaction = record.thread_finished - record.thread_started
        logger.warning(
            "ADD slow device message connector_id=%s type=%s duration_seconds=%.3f "
            "executor_wait_seconds=%s transaction_seconds=%s queued_messages=%d",
            safe_label(self.connector_id), record.message_type, duration,
            _seconds(executor_wait, 3), _seconds(transaction, 3), len(self.queue),
        )

    async def send_json(self, payload: dict) -> bool:
        """Send one message in order. A closed socket is reported, never raised.

        Encoding happens first, exactly as Starlette's send_json does it, so a
        payload that cannot be encoded still fails as a processing error.
        """
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if self.transport_closed:
            self.undelivered += 1
            return False
        try:
            await self.websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError):
            # Starlette and uvicorn report a socket closed by the peer, by
            # keepalive or by this server as one of these. The message being
            # processed still finishes, no queued one starts after it (see
            # stop_reason) and the device replays whatever was not ACKed.
            self._transport_closed = True
            self.undelivered += 1
            return False
        return True

    async def close(self, code: int, reason: str = "") -> None:
        self.mark_end("server_close", code, reason)
        self._transport_closed = True
        try:
            await self.websocket.close(code=code, reason=reason or None)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass

    def mark_end(self, kind: str, code: int | None = None, reason: str = "") -> None:
        """Record how the session ended. The first cause wins."""
        if self.end is not None:
            return
        now = self._clock()
        record = self._in_flight
        self.end = {
            "kind": kind,
            "code": code,
            # Only this server's own close reasons are logged, never a peer's.
            "reason": reason if kind in {"server_close", "error"} else "",
            "in_flight_type": record.message_type if record is not None else None,
            "in_flight_seconds": now - record.started if record is not None else None,
            "since_last_message": now - self.last_message_at if self.last_message_at is not None else None,
            "since_wire_data": (
                now - self.transport.last_data_at
                if self.transport is not None and self.transport.last_data_at is not None else None
            ),
            "queued": len(self.queue),
        }

    def end_summary(self) -> dict[str, Any]:
        end = self.end or {"kind": "unknown", "code": None, "reason": "", "in_flight_type": None,
                           "in_flight_seconds": None, "since_last_message": None,
                           "since_wire_data": None, "queued": len(self.queue)}
        transport = self.transport
        return {
            **end,
            "duration": self._clock() - self.started_at,
            "startup": self.startup_seconds,
            "messages_in": self.messages_in,
            "bytes_in": self.bytes_in,
            "messages_out": self.messages_out,
            "bytes_out": self.bytes_out,
            "processed": self.handed_over,
            "unprocessed": self.messages_in - self.handed_over,
            "undelivered": self.undelivered,
            "queue_peak_messages": self.queue.peak_messages,
            "queue_peak_bytes": self.queue.peak_bytes,
            "queue_full": self.queue.full_waits,
            "slowest_type": self.slowest[0] if self.slowest else None,
            "slowest_seconds": self.slowest[1] if self.slowest else None,
            "keepalive_timeout": None if transport is None else transport.keepalive_timed_out,
            "ping_rtt": None if transport is None else transport.last_ping_rtt,
            "reader_error": self.reader_error,
        }

    def log_end(self) -> None:
        summary = self.end_summary()
        logger.warning(
            "ADD device session ended connector_id=%s end=%s close_code=%s reason=%s "
            "duration_seconds=%.1f startup_seconds=%s in_flight_type=%s in_flight_seconds=%s "
            "since_last_message_seconds=%s since_wire_data_seconds=%s messages_in=%d bytes_in=%d "
            "messages_out=%d bytes_out=%d processed=%d unprocessed=%d undelivered=%d "
            "queue_peak_messages=%d queue_peak_bytes=%d queue_full=%d slowest_type=%s "
            "slowest_seconds=%s keepalive_timeout=%s ping_rtt_seconds=%s reader_error=%s",
            safe_label(self.connector_id), summary["kind"],
            "-" if summary["code"] is None else summary["code"],
            json.dumps(summary["reason"]) if summary["reason"] else "-",
            summary["duration"], _seconds(summary["startup"]),
            summary["in_flight_type"] or "-", _seconds(summary["in_flight_seconds"]),
            _seconds(summary["since_last_message"]), _seconds(summary["since_wire_data"]),
            summary["messages_in"], summary["bytes_in"], summary["messages_out"], summary["bytes_out"],
            summary["processed"], summary["unprocessed"], summary["undelivered"],
            summary["queue_peak_messages"], summary["queue_peak_bytes"], summary["queue_full"],
            summary["slowest_type"] or "-", _seconds(summary["slowest_seconds"]),
            "-" if summary["keepalive_timeout"] is None else str(summary["keepalive_timeout"]).lower(),
            _seconds(summary["ping_rtt"], 3), summary["reader_error"] or "-",
        )

    async def stop(self) -> None:
        """Stop the reader. Messages it has not handed over are abandoned unACKed."""
        reader = self._reader
        if reader is None:
            return
        if not reader.done():
            reader.cancel()
        await asyncio.wait({reader})
