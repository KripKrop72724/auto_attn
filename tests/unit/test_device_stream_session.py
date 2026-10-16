"""Device WebSocket intake: keep reading while one message at a time is processed."""
from __future__ import annotations

import asyncio
import json
import shlex
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError

import zk_add.device_session as device_session
import zk_add.web as add_web
from zk_add.device_session import (
    END_OF_STREAM,
    TRANSPORT_EXTENSION,
    DeviceStreamSession,
    InboundQueue,
    WebSocketTransportFacts,
)
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
        self.closed_by_app = False

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = subprotocol

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send(self, message: dict) -> None:
        if self.closed_by_app:
            # Starlette refuses every send once the application sent a close.
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        if message["type"] == "websocket.close":
            self.closed_by_app = True
            self.closes.append((message["code"], message.get("reason")))
            # uvicorn queues the app's own close code as the final receive event.
            self.incoming.put_nowait({"type": "websocket.disconnect", "code": message["code"]})
            return
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(message["text"]))

    # Starlette routes both through send(), which the session meters.
    async def send_text(self, text: str) -> None:
        await self.send({"type": "websocket.send", "text": text})

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await self.send({"type": "websocket.close", "code": code, "reason": reason})

    def feed_text(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def feed(self, seq: int, *, message_type: str = "heartbeat", connector_id: str = CONNECTOR,
             payload: dict | None = None) -> str:
        text = json.dumps(envelope(seq, message_type=message_type, connector_id=connector_id,
                                   payload=payload))
        self.feed_text(text)
        return text

    def disconnect(self, code: int = 1005) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def sent_of(self, kind: str) -> list[dict]:
        return [message for message in self.sent if message.get("type") == kind]


def envelope(seq: int, *, message_type: str = "heartbeat", connector_id: str = CONNECTOR,
             payload: dict | None = None) -> dict:
    return {
        "schema_version": "2",
        "message_id": f"message-{seq}",
        "connector_id": connector_id,
        "boot_id": "boot-1",
        "seq": seq,
        "sent_at": datetime(2026, 10, 16, tzinfo=timezone.utc).isoformat(),
        "type": message_type,
        "payload": payload or {},
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
        self.bootstrap_errors: list[Exception] = []
        self.bootstrap_attempts = 0
        self.bootstrap_entered = 0
        self.commands: list[dict] = []
        self.events_for: set[int] = set()
        self.connected_error: Exception | None = None
        self.reject_error: Exception | None = None
        self.commands_error: Exception | None = None
        self.follow_up_for: set[int] = set()
        self.delays: dict[int, float] = {}

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
        if envelope_value.seq in self.delays:
            time.sleep(self.delays[envelope_value.seq])
        if envelope_value.seq in self.failures:
            raise self.failures[envelope_value.seq]
        with self.lock:
            self.persisted.append(envelope_value.seq)
        return add_web.EnvelopeOutcome(
            ack={"type": "ack", "message_id": envelope_value.message_id, "seq": envelope_value.seq},
            event={"seq": envelope_value.seq} if envelope_value.seq in self.events_for else None,
            commands_for=CONNECTOR if envelope_value.seq in self.follow_up_for else None,
        )

    def set_connected(self, connector_pk: int, connected: bool) -> str:
        if self.connected_error is not None:
            raise self.connected_error
        with self.lock:
            self.connected.append((connected, list(self.persisted)))
        return "ONLINE"

    def bootstrap(self, connector_pk: int):
        with self.lock:
            self.bootstrap_entered += 1
        if self.bootstrap_gate is not None:
            assert self.bootstrap_gate.wait(5), "test never released the bootstrap"
        self.bootstrap_attempts += 1
        if self.bootstrap_errors:
            raise self.bootstrap_errors.pop(0)
        catalog = {"schema_version": "2", "type": "identity_catalog", "rows": self.bootstrap_rows}
        return catalog, None

    def reject(self, connector_pk: int, envelope_value, error: Exception) -> None:
        with self.lock:
            self.rejections.append((envelope_value.seq, type(error).__name__))
        if self.reject_error is not None:
            raise self.reject_error

    def pending_commands(self, connector_id: str) -> list[dict]:
        if self.commands_error is not None:
            raise self.commands_error
        return list(self.commands)


def database_down() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


@pytest.fixture()
def backend(monkeypatch) -> Backend:
    fake = Backend()
    monkeypatch.setattr(add_web, "authenticate_stream", lambda connector_id, token: 7)
    monkeypatch.setattr(add_web, "set_stream_connected", fake.set_connected)
    monkeypatch.setattr(add_web, "stream_bootstrap", fake.bootstrap)
    monkeypatch.setattr(add_web, "pending_command_payloads", fake.pending_commands)
    monkeypatch.setattr(add_web, "STREAM_BOOTSTRAP_RETRY_SECONDS", (0.0, 0.0))
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
        # After an abrupt drop, and while no newer session exists, everything
        # fully received is processed, as the former loop drained uvicorn's
        # buffer; nothing is written to the closed socket and nothing rejected.
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
        await asyncio.wait_for(stream, 3)
        # The committed message is not reported as rejected, and, as the former
        # loop's failing ACK did, the failed send ends intake: message 2 stays
        # unACKed for the device to replay.
        assert backend.persisted == [1]
        assert backend.rejections == []
        assert websocket.sent_of("error") == []

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


# --- once a socket is closed or replaced, nothing new starts ----------------


def test_hub_send_only_to_never_reaches_a_newer_socket():
    async def scenario():
        hub = ConnectorHub()
        old, new = FakeWebSocket(), FakeWebSocket()
        await hub.connect(CONNECTOR, old)
        await hub.connect(CONNECTOR, new)
        assert old.closes == [(4001, "Superseded by a newer connector session")]
        assert hub.current(CONNECTOR) is new
        assert await hub.send(CONNECTOR, {"type": "command"}, only_to=old) is False
        assert await hub.send_many(CONNECTOR, [{"type": "command"}], only_to=old) is False
        assert new.sent == []
        assert await hub.send(CONNECTOR, {"type": "command"}, only_to=new) is True
        assert await hub.send(CONNECTOR, {"type": "command"}) is True
        assert len(new.sent) == 2
        await hub.disconnect(CONNECTOR, new)
        assert hub.current(CONNECTOR) is None

    asyncio.run(scenario())


def test_superseded_session_starts_nothing_after_4001(backend, caplog):
    async def scenario():
        old = FakeWebSocket()
        first = backend.gate(1)
        old_stream = asyncio.create_task(add_web.device_stream(old))
        old.feed(1)
        old.feed(2)
        old.feed(3)
        await until(lambda: backend.started == [1])
        await until(lambda: old.incoming.empty())
        # The device reconnects while message 1's transaction is still open.
        new = FakeWebSocket()
        new_stream = asyncio.create_task(add_web.device_stream(new))
        await until(lambda: old.closes == [(4001, "Superseded by a newer connector session")])
        new.feed(10)
        await until(lambda: acked(new) == [10])
        first.set()
        await asyncio.wait_for(old_stream, 3)
        # Message 1 was in flight and commits; 2 and 3 never start. They stay
        # unACKed on the old socket and the device replays them on the new one.
        assert backend.started == [1, 10]
        assert backend.persisted == [10, 1]
        assert acked(old) == []
        assert [message["type"] for message in new.sent] == [
            "identity_catalog_begin", "identity_catalog_commit", "ack",
        ]
        new.disconnect()
        await asyncio.wait_for(new_stream, 3)

    asyncio.run(scenario())
    old_end, new_end = session_ends(caplog)
    assert (old_end["end"], old_end["close_code"], old_end["in_flight_type"]) == (
        "superseded", "4001", "heartbeat",
    )
    assert (old_end["processed"], old_end["unprocessed"], old_end["undelivered"]) == ("1", "2", "1")
    assert (new_end["end"], new_end["processed"], new_end["unprocessed"]) == ("connection_lost", "1", "0")


def test_superseded_session_sends_no_follow_ups_to_the_newer_socket(backend, monkeypatch):
    command = {"type": "command", "command_id": "command-1", "command_type": "SYNC_TIME"}
    backend.commands = [command]
    backend.follow_up_for = {1}
    backend.events_for = {1}
    published = []
    publish = add_web.browser_events.publish

    async def record(event_type, data):
        published.append((event_type, data))
        return await publish(event_type, data)

    monkeypatch.setattr(add_web.browser_events, "publish", record)

    async def scenario():
        old = FakeWebSocket()
        first = backend.gate(1)
        old_stream = asyncio.create_task(add_web.device_stream(old))
        old.feed(1)
        await until(lambda: backend.started == [1])
        new = FakeWebSocket()
        new_stream = asyncio.create_task(add_web.device_stream(new))
        # The new session's own bootstrap delivers the pending command once.
        await until(lambda: new.sent_of("command") == [command])
        first.set()
        await asyncio.wait_for(old_stream, 3)
        await asyncio.sleep(0.05)
        # The old session's committed heartbeat flushes nothing through the hub.
        assert new.sent_of("command") == [command]
        assert backend.persisted == [1]
        new.disconnect()
        await asyncio.wait_for(new_stream, 3)

    asyncio.run(scenario())
    # The message is committed, so browsers still hear about it.
    assert ("heartbeat", {"seq": 1}) in published


def test_session_superseded_during_its_bootstrap_pushes_nothing(backend, caplog):
    async def scenario():
        old = FakeWebSocket()
        held = threading.Event()
        backend.bootstrap_gate = held
        old_stream = asyncio.create_task(add_web.device_stream(old))
        old.feed(1, message_type="user_snapshot")
        await until(lambda: backend.bootstrap_entered == 1)
        backend.bootstrap_gate = None
        new = FakeWebSocket()
        new_stream = asyncio.create_task(add_web.device_stream(new))
        await until(lambda: [message["type"] for message in new.sent] == [
            "identity_catalog_begin", "identity_catalog_commit",
        ])
        held.set()
        await asyncio.wait_for(old_stream, 3)
        await asyncio.sleep(0.05)
        # The old bootstrap finished after it was superseded: its catalog and
        # commands went nowhere, and its queued snapshot was never processed.
        assert [message["type"] for message in new.sent] == [
            "identity_catalog_begin", "identity_catalog_commit",
        ]
        assert old.sent == []
        assert backend.started == []
        new.disconnect()
        await asyncio.wait_for(new_stream, 3)

    asyncio.run(scenario())
    old_end = session_ends(caplog)[0]
    assert (old_end["end"], old_end["close_code"], old_end["startup_seconds"]) == ("superseded", "4001", "-")
    assert (old_end["processed"], old_end["unprocessed"]) == ("0", "1")


@pytest.mark.parametrize(
    ("close", "kind", "code"),
    [
        ("keepalive", "keepalive_timeout", "1005"),
        ("shutdown_frame", "server_shutdown", "1012"),
        ("shutdown_hook", "server_shutdown", "1012"),
        ("device_close_frame", "closed", "1000"),
        ("hil_interruption", "closed", "1013"),
    ],
)
def test_orderly_close_finishes_the_message_in_flight_and_starts_no_other(backend, caplog, close, kind, code):
    facts = WebSocketTransportFacts()

    async def scenario():
        websocket = FakeWebSocket()
        websocket.scope["extensions"][TRANSPORT_EXTENSION] = facts
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        websocket.feed(3)
        await until(lambda: backend.started == [1])
        await until(lambda: websocket.incoming.empty())
        if close == "keepalive":
            facts.keepalive_timed_out = True
            websocket.disconnect(1005)
        elif close == "shutdown_frame":
            websocket.disconnect(1012)
        elif close == "shutdown_hook":
            # uvicorn's shutdown, seen by the protocol before the reader gets it.
            facts.server_shutdown = True
        elif close == "device_close_frame":
            websocket.disconnect(1000)
        else:
            expires = add_web.utc_now() + timedelta(seconds=30)
            assert await add_web.connector_hub.interrupt_until(CONNECTOR, expires) is True
        await asyncio.sleep(0.05)
        first.set()
        await asyncio.wait_for(stream, 3)
        assert backend.persisted == [1]
        assert acked(websocket) == []

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"]) == (kind, code)
    assert (end["processed"], end["unprocessed"], end["undelivered"]) == ("1", "2", "1")


def test_abrupt_drop_drains_only_until_a_newer_session_registers(backend, caplog):
    async def scenario():
        old = FakeWebSocket()
        first, second = backend.gate(1), backend.gate(2)
        old_stream = asyncio.create_task(add_web.device_stream(old))
        old.feed(1)
        old.feed(2)
        old.feed(3)
        old.disconnect(1005)
        await until(lambda: backend.started == [1])
        first.set()
        # No newer session yet: the received backlog is still drained.
        await until(lambda: backend.started == [1, 2])
        new = FakeWebSocket()
        new_stream = asyncio.create_task(add_web.device_stream(new))
        await until(lambda: len(new.sent) == 2)
        second.set()
        await asyncio.wait_for(old_stream, 3)
        # Once the device is back, the old session starts nothing more.
        assert backend.persisted == [1, 2]
        new.disconnect()
        await asyncio.wait_for(new_stream, 3)

    asyncio.run(scenario())
    old_end = session_ends(caplog)[0]
    assert (old_end["end"], old_end["close_code"]) == ("connection_lost", "1005")
    assert (old_end["processed"], old_end["unprocessed"], old_end["undelivered"]) == ("2", "1", "2")


# --- ADD-side faults never drop the socket ----------------------------------


def test_database_fault_during_authentication_refuses_cleanly_without_accepting(backend, monkeypatch, caplog):
    def unavailable(connector_id, token):
        raise database_down()

    monkeypatch.setattr(add_web, "authenticate_stream", unavailable)

    async def scenario():
        websocket = FakeWebSocket()
        await asyncio.wait_for(add_web.device_stream(websocket), 3)
        assert websocket.accepted is None
        assert websocket.closes == [(1013, None)]

    asyncio.run(scenario())
    assert "authentication unavailable error_type=OperationalError" in caplog.text
    assert "device-token" not in caplog.text


def test_connection_bookkeeping_faults_do_not_cost_the_session(backend, caplog):
    backend.connected_error = database_down()
    backend.commands_error = database_down()

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: acked(websocket) == [1])
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)
        assert websocket.closes == []

    asyncio.run(scenario())
    assert "could not record its connection" in caplog.text
    assert "could not send pending commands" in caplog.text
    assert "could not record its disconnection" in caplog.text


def test_transient_bootstrap_database_fault_is_retried_within_the_session(backend):
    backend.bootstrap_errors = [database_down(), database_down()]

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: acked(websocket) == [1])
        assert backend.bootstrap_attempts == 3
        assert websocket.sent[0]["type"] == "identity_catalog_begin"
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("errors", "close"),
    [
        ([database_down(), database_down(), database_down()], (1013, "ADD database unavailable; retry")),
        ([KeyError("catalog")], (1011, "ADD bootstrap failed")),
    ],
)
def test_bootstrap_that_cannot_complete_closes_with_a_frame_and_processes_nothing(
    backend, errors, close
):
    backend.bootstrap_errors = list(errors)

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await asyncio.wait_for(stream, 3)
        # The session never runs without its identity catalog.
        assert websocket.closes == [close]
        assert websocket.sent == []
        assert backend.started == []
        assert backend.connected[-1] == (False, [])

    asyncio.run(scenario())


def test_rejection_bookkeeping_fault_still_answers_and_keeps_the_session(backend, caplog):
    backend.failures[1] = database_down()
    backend.reject_error = database_down()

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        await until(lambda: acked(websocket) == [2])
        assert [message["code"] for message in websocket.sent_of("error")] == ["MESSAGE_REJECTED"]
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)
        assert websocket.closes == []

    asyncio.run(scenario())
    assert "could not record a rejected envelope" in caplog.text
    assert "message_id=message-1 error_type=OperationalError" in caplog.text


def test_follow_up_fault_after_commit_is_not_reported_as_a_rejection(backend, caplog):
    backend.follow_up_for = {1}

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        await until(lambda: len(websocket.sent) == 2)  # bootstrap done
        backend.commands_error = database_down()
        websocket.feed(1)
        websocket.feed(2)
        await until(lambda: acked(websocket) == [1, 2])
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)
        assert websocket.sent_of("error") == []

    asyncio.run(scenario())
    assert backend.rejections == []
    assert "follow-up after a committed envelope failed" in caplog.text


def test_unexpected_session_fault_closes_with_a_frame(backend, monkeypatch):
    async def broken(*_args):
        raise LookupError("plumbing bug")

    monkeypatch.setattr(add_web, "process_device_message", broken)

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        with pytest.raises(LookupError):
            await asyncio.wait_for(stream, 3)
        assert websocket.closes == [(1011, "ADD internal error")]
        assert backend.connected[-1] == (False, [])

    asyncio.run(scenario())


# --- why a session ended ----------------------------------------------------


def session_ends(caplog) -> list[dict[str, str]]:
    return [
        dict(field.split("=", 1) for field in shlex.split(
            record.getMessage().removeprefix("ADD device session ended ")))
        for record in caplog.records if record.getMessage().startswith("ADD device session ended ")
    ]


def session_end(caplog) -> dict[str, str]:
    ends = session_ends(caplog)
    assert len(ends) == 1, ends
    return ends[0]


def test_session_end_reports_the_message_in_flight_when_the_peer_vanished(backend, caplog):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        sizes = [len(websocket.feed(1, message_type="user_snapshot", payload={"rows": ["x" * 2000]})),
                 len(websocket.feed(2))]
        await until(lambda: backend.started == [1])
        await until(lambda: websocket.incoming.empty())
        await asyncio.sleep(0.2)
        websocket.disconnect(1005)
        await asyncio.sleep(0.05)
        first.set()
        await asyncio.wait_for(stream, 3)
        return sizes, websocket

    sizes, websocket = asyncio.run(scenario())
    end = session_end(caplog)
    assert end["connector_id"] == CONNECTOR
    assert (end["end"], end["close_code"], end["reason"]) == ("connection_lost", "1005", "-")
    assert end["in_flight_type"] == "user_snapshot"
    assert float(end["in_flight_seconds"]) >= 0.2
    assert float(end["since_last_message_seconds"]) >= 0.2
    assert end["startup_seconds"] != "-"
    assert (end["messages_in"], end["bytes_in"]) == ("2", str(sum(sizes)))
    # The catalog begin/commit went out; both ACKs found the socket gone.
    assert end["messages_out"] == "2"
    assert int(end["bytes_out"]) == sum(len(json.dumps(message, separators=(",", ":")))
                                         for message in websocket.sent)
    assert (end["processed"], end["unprocessed"], end["undelivered"]) == ("2", "0", "2")
    # Both arrived while the bootstrap was still being pushed.
    assert end["queue_peak_messages"] == "2"
    assert end["slowest_type"] == "user_snapshot"
    # Without the uvicorn protocol hook the keepalive state is unknown, not false.
    assert (end["keepalive_timeout"], end["ping_rtt_seconds"]) == ("-", "-")


@pytest.mark.parametrize(
    ("code", "keepalive", "kind"),
    [
        (1005, True, "keepalive_timeout"),
        (1005, False, "connection_lost"),
        (4001, False, "superseded"),
        (1012, False, "server_shutdown"),
        (1000, False, "closed"),
    ],
)
def test_session_end_classifies_how_the_socket_closed(backend, caplog, code, keepalive, kind):
    facts = WebSocketTransportFacts()

    async def scenario():
        websocket = FakeWebSocket()
        websocket.scope["extensions"][TRANSPORT_EXTENSION] = facts
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: acked(websocket) == [1])
        facts.keepalive_timed_out = keepalive
        facts.last_ping_rtt = 0.25
        websocket.disconnect(code)
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert facts.connector_id == CONNECTOR
    assert (end["end"], end["close_code"]) == (kind, str(code))
    assert end["keepalive_timeout"] == str(keepalive).lower()
    assert end["ping_rtt_seconds"] == "0.250"
    assert end["in_flight_type"] == "-"


def test_session_duration_ends_at_the_disconnect_and_later_work_is_drain_time(backend, caplog):
    backend.delays = {1: 0.4, 2: 0.4}

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2)
        websocket.disconnect(1005)
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert end["end"] == "connection_lost"
    assert end["processed"] == "2"
    # The device left at once; draining its two messages took 0.8 s more.
    assert float(end["duration_seconds"]) < 0.3
    assert float(end["drain_seconds"]) >= 0.7


def test_session_duration_uses_the_transport_close_the_reader_learns_late(backend, caplog):
    facts = WebSocketTransportFacts()

    async def scenario():
        websocket = FakeWebSocket()
        websocket.scope["extensions"][TRANSPORT_EXTENSION] = facts
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: acked(websocket) == [1])
        facts.closed_at = time.monotonic()
        await asyncio.sleep(1.0)
        websocket.disconnect(1005)
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert float(end["duration_seconds"]) + 0.5 < float(end["drain_seconds"])
    assert float(end["drain_seconds"]) >= 0.9


def test_session_end_names_this_servers_own_close(backend, caplog):
    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: acked(websocket) == [1])
        websocket.feed_text("x" * (512 * 1024 + 1))
        websocket.feed(3)
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"], end["reason"]) == ("server_close", "4400", "Message too large")
    assert (end["messages_in"], end["processed"], end["unprocessed"]) == ("3", "2", "1")


def test_failed_bootstrap_has_no_startup_duration(backend, caplog):
    backend.bootstrap_errors = [database_down(), database_down(), database_down()]

    async def scenario():
        await asyncio.wait_for(add_web.device_stream(FakeWebSocket()), 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"], end["startup_seconds"]) == ("server_close", "1013", "-")


def test_cancelled_session_end_names_the_message_it_waited_for(backend, caplog):
    async def scenario():
        websocket = FakeWebSocket()
        first = backend.gate(1)
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        await until(lambda: backend.started == [1])
        stream.cancel()
        await asyncio.sleep(0.05)
        first.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["in_flight_type"]) == ("cancelled", "heartbeat")


def test_unexpected_fault_is_named_in_the_session_end(backend, monkeypatch, caplog):
    async def broken(*_args):
        raise LookupError("plumbing bug")

    monkeypatch.setattr(add_web, "process_device_message", broken)

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        with pytest.raises(LookupError):
            await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"], end["reason"]) == ("error", "1011", "LookupError")


def test_slow_message_log_splits_executor_wait_from_transaction_time(backend, monkeypatch, caplog):
    monkeypatch.setattr(add_web.settings, "slow_request_seconds", 0.1)
    backend.delays[2] = 0.2

    async def scenario():
        websocket = FakeWebSocket()
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1)
        websocket.feed(2, message_type="user_snapshot")
        await until(lambda: acked(websocket) == [1, 2])
        websocket.disconnect()
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    slow = [record.getMessage() for record in caplog.records
            if record.getMessage().startswith("ADD slow device message ")]
    assert len(slow) == 1
    fields = dict(field.split("=", 1) for field in slow[0].removeprefix("ADD slow device message ").split())
    assert (fields["connector_id"], fields["type"]) == (CONNECTOR, "user_snapshot")
    assert float(fields["transaction_seconds"]) >= 0.2
    assert float(fields["executor_wait_seconds"]) >= 0
    assert float(fields["duration_seconds"]) >= float(fields["transaction_seconds"])
    assert session_end(caplog)["slowest_type"] == "user_snapshot"


def test_session_logs_never_carry_tokens_payloads_or_peer_close_reasons(backend, caplog):
    async def scenario():
        websocket = FakeWebSocket()
        backend.failures[1] = ValueError("CNIC 35202-1234567-1 is not valid")
        stream = asyncio.create_task(add_web.device_stream(websocket))
        websocket.feed(1, message_type="attendance_batch", payload={"cnic": "35202-1234567-1"})
        websocket.feed(2, payload={"name": "PAYLOAD-NAME-MARKER"})
        await until(lambda: acked(websocket) == [2])
        websocket.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000,
                                       "reason": "PEER-REASON-MARKER"})
        await asyncio.wait_for(stream, 3)

    asyncio.run(scenario())
    text = caplog.text
    assert "ADD device session ended" in text
    for secret in ("device-token", "35202", "PAYLOAD-NAME-MARKER", "PEER-REASON-MARKER"):
        assert secret not in text
