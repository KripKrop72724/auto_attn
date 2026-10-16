"""Device WebSocket intake: keep reading while one message at a time is processed."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

import pytest

import zk_add.device_session as device_session
import zk_add.web as add_web
from zk_add.device_session import END_OF_STREAM, DeviceStreamSession, InboundQueue
from zk_add.realtime import BrowserEventHub, ConnectorHub

CONNECTOR = "ZL-STREAM-TEST"


async def until(condition, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.005)


class FakeWebSocket:
    """The parts of Starlette's WebSocket the device endpoint uses, over uvicorn semantics."""

    def __init__(self) -> None:
        self.headers = {"X-ADD-Connector-Id": CONNECTOR, "Authorization": "Bearer device-token"}
        self.query_params: dict[str, str] = {}
        self.scope: dict = {"type": "websocket", "extensions": {}}
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closes: list[tuple[int, str | None]] = []
        self.send_error: Exception | None = None
        self.accepted: str | None = None

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = subprotocol

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_text(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))
        # uvicorn queues the app's own close code as the final receive event.
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def feed_text(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def feed(self, seq: int, *, message_type: str = "heartbeat", connector_id: str = CONNECTOR) -> None:
        self.feed_text(json.dumps(envelope(seq, message_type=message_type, connector_id=connector_id)))

    def disconnect(self, code: int = 1005) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def sent_of(self, kind: str) -> list[dict]:
        return [message for message in self.sent if message.get("type") == kind]


def envelope(seq: int, *, message_type: str = "heartbeat", connector_id: str = CONNECTOR) -> dict:
    return {
        "schema_version": "2",
        "message_id": f"message-{seq}",
        "connector_id": connector_id,
        "boot_id": "boot-1",
        "seq": seq,
        "sent_at": datetime(2026, 10, 16, tzinfo=timezone.utc).isoformat(),
        "type": message_type,
        "payload": {},
    }


class Backend:
    """Thread-side stand-ins for the database work, with per-message gates."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started: list[int] = []
        self.persisted: list[int] = []
        self.connected: list[tuple[bool, list[int]]] = []
        self.rejections: list[tuple[int, str]] = []
        self.gates: dict[int, threading.Event] = {}
        self.failures: dict[int, Exception] = {}
        self.bootstrap_gate: threading.Event | None = None
        self.bootstrap_rows: list[dict] = []

    def gate(self, seq: int) -> threading.Event:
        self.gates[seq] = threading.Event()
        return self.gates[seq]

    def persist(self, connector_pk: int, envelope_value) -> add_web.EnvelopeOutcome:
        assert connector_pk == 7
        with self.lock:
            self.started.append(envelope_value.seq)
        gate = self.gates.get(envelope_value.seq)
        if gate is not None:
            assert gate.wait(5), "test never released the transaction"
        if envelope_value.seq in self.failures:
            raise self.failures[envelope_value.seq]
        with self.lock:
            self.persisted.append(envelope_value.seq)
        return add_web.EnvelopeOutcome(
            ack={"type": "ack", "message_id": envelope_value.message_id, "seq": envelope_value.seq}
        )

    def set_connected(self, connector_pk: int, connected: bool) -> str:
        with self.lock:
            self.connected.append((connected, list(self.persisted)))
        return "ONLINE"

    def bootstrap(self, connector_pk: int):
        if self.bootstrap_gate is not None:
            assert self.bootstrap_gate.wait(5), "test never released the bootstrap"
        catalog = {"schema_version": "2", "type": "identity_catalog", "rows": self.bootstrap_rows}
        return catalog, None

    def reject(self, connector_pk: int, envelope_value, error: Exception) -> None:
        with self.lock:
            self.rejections.append((envelope_value.seq, type(error).__name__))


@pytest.fixture()
def backend(monkeypatch) -> Backend:
    fake = Backend()
    monkeypatch.setattr(add_web, "authenticate_stream", lambda connector_id, token: 7)
    monkeypatch.setattr(add_web, "set_stream_connected", fake.set_connected)
    monkeypatch.setattr(add_web, "stream_bootstrap", fake.bootstrap)
    monkeypatch.setattr(add_web, "pending_command_payloads", lambda connector_id: [])
    monkeypatch.setattr(add_web, "persist_envelope", fake.persist)
    monkeypatch.setattr(add_web, "record_envelope_rejection", fake.reject)
    monkeypatch.setattr(add_web, "connector_hub", ConnectorHub())
    monkeypatch.setattr(add_web, "browser_events", BrowserEventHub())
    return fake


def acked(websocket: FakeWebSocket) -> list[int]:
    return [message["seq"] for message in websocket.sent_of("ack")]


# --- the bounded FIFO -------------------------------------------------------


def test_inbound_queue_is_fifo_and_bounded_by_count_without_dropping():
    async def scenario():
        queue = InboundQueue(max_messages=2, max_bytes=10_000)
        assert await queue.put("a", 1) is False
        assert await queue.put("b", 1) is False
        third = asyncio.create_task(queue.put("c", 1))
        await asyncio.sleep(0.01)
        assert not third.done(), "a full queue must wait, not drop or overtake"
        assert await queue.get() == "a"
        assert await third is True
        queue.finish()
        assert [await queue.get(), await queue.get()] == ["b", "c"]
        assert await queue.get() is END_OF_STREAM
        assert await queue.get() is END_OF_STREAM

    asyncio.run(scenario())


def test_inbound_queue_byte_bound_still_admits_one_oversized_message():
    async def scenario():
        queue = InboundQueue(max_messages=10, max_bytes=100)
        assert await queue.put("huge", 1_000) is False  # an empty queue always admits
        blocked = asyncio.create_task(queue.put("small", 1))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        assert queue.bytes == 1_000
        assert await queue.get() == "huge"
        await blocked
        assert (len(queue), queue.bytes) == (1, 1)

    asyncio.run(scenario())


def test_inbound_queue_cancellation_never_loses_or_duplicates_items():
    async def scenario():
        queue = InboundQueue(max_messages=1, max_bytes=100)
        getter = asyncio.create_task(queue.get())
        await asyncio.sleep(0.01)
        getter.cancel()
        await asyncio.gather(getter, return_exceptions=True)
        await queue.put("kept", 1)
        blocked = asyncio.create_task(queue.put("abandoned", 1))
        await asyncio.sleep(0.01)
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        queue.finish()
        assert await queue.get() == "kept"
        assert await queue.get() is END_OF_STREAM

    asyncio.run(scenario())


# --- the session ------------------------------------------------------------


def test_session_send_reports_a_closed_socket_instead_of_raising():
    async def scenario():
        websocket = FakeWebSocket()
        session = DeviceStreamSession(websocket, CONNECTOR)
        assert await session.send_json({"type": "ack", "seq": 1}) is True
        websocket.send_error = RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'.")
        assert await session.send_json({"type": "ack", "seq": 2}) is False
        websocket.send_error = None
        # Once the socket is known to be gone nothing more is written to it.
        assert await session.send_json({"type": "ack", "seq": 3}) is False
        assert acked(websocket) == [1]
        with pytest.raises(TypeError):
            await session.send_json({"type": "ack", "value": object()})

    asyncio.run(scenario())


def test_session_stop_cancels_a_reader_blocked_on_a_full_queue():
    async def scenario():
        websocket = FakeWebSocket()
        session = DeviceStreamSession(websocket, CONNECTOR, max_messages=1)
        session.start()
        websocket.feed(1)
        websocket.feed(2)
        websocket.feed(3)
        # Message 1 is queued, the reader holds message 2 and never read 3.
        await until(lambda: websocket.incoming.qsize() == 1)
        await session.stop()
        assert json.loads(await session.next_message())["seq"] == 1
        assert await session.next_message() is None

    asyncio.run(scenario())


# --- the device endpoint ----------------------------------------------------


def test_stream_reads_while_a_transaction_runs_and_processes_strictly_in_order(backend):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2, message_type="user_snapshot")
        websocket.feed(3)
        await until(lambda: backend.started == [1])
        # The device's later messages leave the socket while message 1's
        # transaction is still open, but none starts before it finishes.
        await until(lambda: websocket.incoming.empty())
        await asyncio.sleep(0.05)
        assert backend.started == [1]
        assert acked(websocket) == []
        first.set()
        await until(lambda: acked(websocket) == [1, 2, 3])
        assert backend.persisted == [1, 2, 3]
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)
        assert backend.connected == [(True, []), (False, [1, 2, 3])]

    asyncio.run(scenario())


def test_stream_reads_during_bootstrap_and_processes_after_it_in_order(backend):
    async def scenario():
        websocket = FakeWebSocket()
        backend.bootstrap_gate = threading.Event()
        backend.bootstrap_rows = [{"uid": 1, "user_id": "1", "display_name": "A", "cnic": None,
                                   "shift_worker": False}]
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1, message_type="user_snapshot")
        websocket.feed(2)
        await until(lambda: websocket.incoming.empty())
        assert backend.started == []
        backend.bootstrap_gate.set()
        await until(lambda: acked(websocket) == [1, 2])
        kinds = [message["type"] for message in websocket.sent]
        assert kinds == [
            "identity_catalog_begin", "identity_catalog_chunk", "identity_catalog_commit", "ack", "ack",
        ]
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


def test_full_queue_applies_backpressure_and_drops_nothing(backend, monkeypatch):
    monkeypatch.setattr(device_session, "INBOUND_QUEUE_MAX_MESSAGES", 2)

    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        for seq in range(1, 7):
            websocket.feed(seq)
        await until(lambda: backend.started == [1])
        # Message 1 is in flight, 2 and 3 fill the queue and the reader holds
        # 4. It stops asking the socket for more until there is room.
        await until(lambda: websocket.incoming.qsize() == 2)
        await asyncio.sleep(0.05)
        assert websocket.incoming.qsize() == 2
        first.set()
        await until(lambda: acked(websocket) == [1, 2, 3, 4, 5, 6])
        assert backend.persisted == [1, 2, 3, 4, 5, 6]
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


def test_cancelled_session_finishes_the_in_flight_message_and_its_ack_first(backend):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        await until(lambda: backend.started == [1])
        await until(lambda: websocket.incoming.empty())
        stream.cancel()
        await asyncio.sleep(0.05)
        assert not stream.done(), "teardown must wait for the committed message"
        first.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stream, 3)
        assert acked(websocket) == [1]
        # Message 2 was never started: it stays unACKed for the device to replay.
        assert backend.persisted == [1]
        # The disconnect bookkeeping ran after message 1 committed.
        assert backend.connected[-1] == (False, [1])

    asyncio.run(scenario())


def test_peer_disconnect_mid_transaction_still_commits_received_messages_in_order(backend):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        websocket.disconnect(1005)
        await until(lambda: backend.started == [1])
        await until(lambda: websocket.incoming.empty())
        first.set()
        await asyncio.wait_for(stream, 3)
        # Everything fully received is processed, as the single receive loop
        # did; nothing is written to the closed socket and nothing is rejected.
        assert backend.persisted == [1, 2]
        assert acked(websocket) == []
        assert backend.rejections == []
        assert backend.connected[-1] == (False, [1, 2])

    asyncio.run(scenario())


def test_ack_to_a_socket_closed_by_keepalive_is_not_reported_as_a_rejected_message(backend):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        await until(lambda: backend.started == [1])
        await until(lambda: websocket.incoming.empty())
        # uvicorn 0.51 raises this for any send after a keepalive close.
        websocket.send_error = RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'.")
        first.set()
        await until(lambda: backend.persisted == [1, 2])
        assert backend.rejections == []
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


def test_invalid_and_binary_frames_are_answered_in_order_without_closing(backend):
    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed_text("{not json")
        websocket.incoming.put_nowait({"type": "websocket.receive", "bytes": b"\x00\x01"})
        websocket.feed(2)
        await until(lambda: len(websocket.sent) == 6)
        assert [message.get("seq", message.get("code")) for message in websocket.sent[2:]] == [
            1, "INVALID_ENVELOPE", "INVALID_ENVELOPE", 2,
        ]
        assert websocket.closes == []
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("make_frame", "close"),
    [
        (lambda: "x" * (512 * 1024 + 1), (4400, "Message too large")),
        (lambda: json.dumps(envelope(1, connector_id="ZL-SOMEONE-ELSE")), (4403, None)),
    ],
)
def test_stream_closing_frames_end_processing_of_later_messages(backend, make_frame, close):
    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed_text(make_frame())
        websocket.feed(2)
        await asyncio.wait_for(stream, 3)
        assert websocket.closes == [close]
        assert backend.started == []

    asyncio.run(scenario())


def test_rejected_message_is_recorded_answered_and_followed_by_the_next(backend):
    async def scenario():
        websocket = FakeWebSocket()
        backend.failures[1] = ValueError("evidence is not valid")
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1, message_type="attendance_batch")
        websocket.feed(2)
        await until(lambda: acked(websocket) == [2])
        assert backend.rejections == [(1, "ValueError")]
        rejected = websocket.sent_of("error")
        assert rejected == [{
            "type": "error", "code": "MESSAGE_REJECTED", "message_id": "message-1",
            "message_type": "attendance_batch",
        }]
        assert websocket.sent.index(rejected[0]) < websocket.sent.index(websocket.sent_of("ack")[0])
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


def test_controlled_interruption_closes_with_1013_and_stops(backend):
    async def scenario():
        websocket = FakeWebSocket()
        backend.failures[1] = add_web.TransportInterrupted(30)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        await asyncio.wait_for(stream, 3)
        assert websocket.closes == [(1013, "Controlled ADD interruption")]
        assert backend.started == [1]
        assert backend.rejections == []

    asyncio.run(scenario())
