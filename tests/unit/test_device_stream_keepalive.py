"""uvicorn keepalive against slow device-message processing, on a real socket."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

import pytest
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from uvicorn.importer import import_from_string
from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

import zk_add.__main__ as add_main
from zk_add.device_session import (
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
    WS_PROTOCOL,
    DeviceStreamSession,
)
from zk_add.ws_protocol import DiagnosticWebSocketProtocol

ROOT = Path(__file__).resolve().parents[2]
# Each message takes longer than uvicorn's whole ping interval plus timeout.
PROCESSING_SECONDS = 1.0
PING_INTERVAL = 0.2
PING_TIMEOUT = 0.5


def make_reader_stream(processing_seconds: float):
    async def stream(websocket):
        await websocket.accept()
        session = DeviceStreamSession(websocket, "ZL-KEEPALIVE")
        session.start()
        try:
            while (raw := await session.next_message()) is not None:
                async def work(raw=raw):
                    await asyncio.sleep(processing_seconds)
                    await session.send_json({"type": "ack", "message": raw})
                await session.process("heartbeat", work())
        finally:
            await session.stop()
            session.log_end()

    return stream


reader_stream = make_reader_stream(PROCESSING_SECONDS)


async def receive_then_process_stream(websocket):
    """The former loop: the next receive waits for the previous transaction."""
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            await asyncio.sleep(PROCESSING_SECONDS)
            await websocket.send_json({"type": "ack", "message": raw})
    except Exception:
        pass


async def serve(app, **overrides):
    settings = {"ws": "websockets-sansio", **overrides}
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_config=None, log_level="warning",
        ws_ping_interval=PING_INTERVAL, ws_ping_timeout=PING_TIMEOUT, **settings,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


async def stop(server, task) -> None:
    server.should_exit = True
    await asyncio.wait_for(task, 10)


def test_reader_keeps_pongs_flowing_through_slow_message_processing():
    app = Starlette(routes=[
        WebSocketRoute("/reader", reader_stream),
        WebSocketRoute("/former", receive_then_process_stream),
    ])

    async def scenario():
        server, task, port = await serve(app)
        try:
            # Control: uvicorn stops reading when a message arrives while the
            # former loop is busy, leaves the pong unread and kills the socket.
            async with connect(f"ws://127.0.0.1:{port}/former", ping_interval=None) as client:
                await client.send("first")
                await asyncio.sleep(0.05)
                await client.send("second")
                with pytest.raises(ConnectionClosedError) as closed:
                    await asyncio.wait_for(client.recv(), 5)
                assert closed.value.rcvd.code == 1011
                assert closed.value.rcvd.reason == "keepalive ping timeout"

            async with connect(f"ws://127.0.0.1:{port}/reader", ping_interval=None) as client:
                await client.send("message-0")
                await asyncio.sleep(0.05)
                await client.send("message-1")
                acks = [json.loads(await asyncio.wait_for(client.recv(), 5)) for _ in range(2)]
                assert [ack["message"] for ack in acks] == ["message-0", "message-1"]
                assert client.state.name == "OPEN"
        finally:
            await stop(server, task)

    asyncio.run(scenario())


def session_end(caplog) -> dict[str, str]:
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("ADD device session ended ")]
    assert len(lines) == 1, lines
    return dict(field.split("=", 1) for field in shlex.split(lines[0].removeprefix("ADD device session ended ")))


def keepalive_lines(caplog) -> list[dict[str, str]]:
    return [
        dict(field.split("=", 1) for field in record.getMessage().removeprefix(
            "ADD WebSocket keepalive timeout ").split())
        for record in caplog.records if record.getMessage().startswith("ADD WebSocket keepalive timeout ")
    ]


def test_diagnostic_protocol_reports_a_keepalive_close_to_the_log_and_the_session(caplog):
    app = Starlette(routes=[WebSocketRoute("/reader", reader_stream)])

    async def scenario():
        server, task, port = await serve(app, ws=DiagnosticWebSocketProtocol)
        try:
            async with connect(f"ws://127.0.0.1:{port}/reader", ping_interval=None) as client:
                # A device busy sending cannot read pings; neither can this client.
                client.transport.pause_reading()
                deadline = asyncio.get_running_loop().time() + 5
                while not any(r.getMessage().startswith("ADD device session ended") for r in caplog.records):
                    assert asyncio.get_running_loop().time() < deadline, "keepalive never closed the socket"
                    await asyncio.sleep(0.02)
                client.transport.resume_reading()
        finally:
            await stop(server, task)

    asyncio.run(scenario())
    [kill] = keepalive_lines(caplog)
    assert (kill["connector_id"], kill["path"]) == ("ZL-KEEPALIVE", "/reader")
    assert float(kill["ping_timeout_seconds"]) == PING_TIMEOUT
    assert kill["read_paused"] == "false"
    assert kill["bytes_since_ping"] == "0"
    assert float(kill["seconds_since_ping"]) >= PING_TIMEOUT
    end = session_end(caplog)
    assert (end["end"], end["close_code"], end["keepalive_timeout"]) == ("keepalive_timeout", "1005", "true")
    assert end["since_wire_data_seconds"] != "-"


def test_diagnostic_protocol_measures_ping_round_trips_without_changing_traffic(caplog):
    app = Starlette(routes=[WebSocketRoute("/reader", reader_stream)])

    async def scenario():
        server, task, port = await serve(app, ws=WS_PROTOCOL)
        try:
            async with connect(f"ws://127.0.0.1:{port}/reader", ping_interval=None) as client:
                await client.send("message-0")
                await client.send("message-1")
                acks = [json.loads(await asyncio.wait_for(client.recv(), 5)) for _ in range(2)]
                assert [ack["message"] for ack in acks] == ["message-0", "message-1"]
            deadline = asyncio.get_running_loop().time() + 5
            while not any(r.getMessage().startswith("ADD device session ended") for r in caplog.records):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
        finally:
            await stop(server, task)

    asyncio.run(scenario())
    assert keepalive_lines(caplog) == []
    end = session_end(caplog)
    assert (end["end"], end["close_code"], end["keepalive_timeout"]) == ("closed", "1000", "false")
    assert float(end["ping_rtt_seconds"]) < PING_TIMEOUT
    assert (end["messages_in"], end["processed"], end["messages_out"]) == ("2", "2", "2")


def test_diagnostic_protocol_extends_the_protocol_uvicorn_selects_by_default():
    assert import_from_string(WS_PROTOCOL) is DiagnosticWebSocketProtocol
    assert AutoWebSocketsProtocol is WebSocketsSansIOProtocol
    assert issubclass(DiagnosticWebSocketProtocol, AutoWebSocketsProtocol)
    # A uvicorn upgrade that renames a hooked method must fail here, not
    # silently stop reporting keepalive closes.
    for name in ("handle_connect", "connection_lost", "shutdown", "data_received",
                 "send_keepalive_ping", "handle_pong", "keepalive_timeout"):
        assert name in vars(WebSocketsSansIOProtocol), name
        assert name in vars(DiagnosticWebSocketProtocol), name
    for attribute in ("pending_ping_payload", "last_ping_rtt", "ping_sent_at", "read_paused",
                      "close_sent", "ping_timeout"):
        assert attribute in WebSocketsSansIOProtocol.__init__.__code__.co_names, attribute


async def wait_for_session_end(caplog, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(r.getMessage().startswith("ADD device session ended") for r in caplog.records):
        assert asyncio.get_running_loop().time() < deadline, "the session never ended"
        await asyncio.sleep(0.02)


def test_server_shutdown_finishes_the_message_in_flight_and_starts_no_other(caplog):
    app = Starlette(routes=[WebSocketRoute("/reader", reader_stream)])

    async def scenario():
        server, task, port = await serve(app, ws=DiagnosticWebSocketProtocol)
        client = await connect(f"ws://127.0.0.1:{port}/reader", ping_interval=None)
        try:
            for index in range(3):
                await client.send(f"message-{index}")
            await asyncio.sleep(0.3)
            # Deploy: uvicorn closes every socket with 1012 and waits for the app.
            started = asyncio.get_running_loop().time()
            await stop(server, task)
            # Only the message in flight delays the shutdown, not the backlog.
            assert asyncio.get_running_loop().time() - started < 2 * PROCESSING_SECONDS
        finally:
            await client.close()

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"]) == ("server_shutdown", "1012")
    assert (end["processed"], end["unprocessed"], end["undelivered"]) == ("1", "2", "1")


def test_abrupt_drop_reports_duration_to_the_drop_and_the_drain_separately(caplog):
    app = Starlette(routes=[WebSocketRoute("/reader", make_reader_stream(0.3))])

    async def scenario():
        server, task, port = await serve(app, ws=DiagnosticWebSocketProtocol)
        try:
            client = await connect(f"ws://127.0.0.1:{port}/reader", ping_interval=None)
            for index in range(3):
                await client.send(f"message-{index}")
            await asyncio.sleep(0.1)
            client.transport.abort()
            await wait_for_session_end(caplog)
        finally:
            await stop(server, task)

    asyncio.run(scenario())
    end = session_end(caplog)
    assert (end["end"], end["close_code"]) == ("connection_lost", "1005")
    # No newer session exists, so the received backlog is still processed;
    # none of its ACKs can be delivered.
    assert (end["processed"], end["unprocessed"], end["undelivered"]) == ("3", "0", "3")
    assert float(end["duration_seconds"]) < 0.5
    assert float(end["drain_seconds"]) >= 0.5


def _api_service(compose_file: Path) -> dict:
    return yaml.safe_load(compose_file.read_text())["services"]["add-api"]


def _assert_keepalive(arguments: list[str]) -> None:
    def value(flag: str) -> str:
        assert arguments.count(flag) == 1, f"{flag} must be set exactly once: {arguments}"
        return arguments[arguments.index(flag) + 1]

    assert float(value("--ws-ping-interval")) == WS_PING_INTERVAL_SECONDS
    assert float(value("--ws-ping-timeout")) == WS_PING_TIMEOUT_SECONDS


def test_every_api_launch_carries_the_websocket_keepalive_settings():
    production = _api_service(ROOT / "docker-compose.add.yml")["command"]
    assert production[:2] == ["/bin/sh", "-c"]
    shell_command = production[2]
    production_arguments = shlex.split(shell_command.split("exec ", 1)[1])
    _assert_keepalive(production_arguments)
    assert production_arguments[production_arguments.index("--ws") + 1] == WS_PROTOCOL

    # The rollback override replaces the whole command for an older image, so
    # it must repeat the settings or a rollback silently drops them.
    rollback = _api_service(ROOT / "deploy/add/docker-compose.rollback.yml")["command"]
    assert rollback[:2] == ["uvicorn", "zk_add.web:app"]
    _assert_keepalive(rollback)
    # ...but never the protocol module, which an older image does not contain.
    assert "--ws" not in rollback
    assert not any("ws_protocol" in argument for argument in rollback)

    dockerfile = (ROOT / "apps/add_backend/Dockerfile").read_text().splitlines()
    cmd = [line for line in dockerfile if line.startswith("CMD ")]
    assert len(cmd) == 1
    image_arguments = json.loads(cmd[0].removeprefix("CMD "))
    _assert_keepalive(image_arguments)
    assert image_arguments[image_arguments.index("--ws") + 1] == WS_PROTOCOL

    assert WS_PING_TIMEOUT_SECONDS >= 3 * WS_PING_INTERVAL_SECONDS


def test_development_entry_point_uses_the_same_keepalive(monkeypatch):
    calls = []
    monkeypatch.setattr(add_main.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("sys.argv", ["zk-add"])
    add_main.main()
    assert calls[0]["ws"] == WS_PROTOCOL
    assert calls[0]["ws_ping_interval"] == WS_PING_INTERVAL_SECONDS
    assert calls[0]["ws_ping_timeout"] == WS_PING_TIMEOUT_SECONDS


def test_no_other_file_launches_the_api_without_the_keepalive_settings():
    skipped_directories = {".git", "node_modules", "__pycache__", "tests", "docs"}
    launches = []
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories
                             if name not in skipped_directories and not name.startswith(".venv")]
        for name in files:
            path = Path(directory, name)
            if path.suffix in {".py", ".md", ".pyc"}:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            relative = path.relative_to(ROOT).as_posix()
            for line in text.splitlines():
                if "uvicorn" in line and "zk_add.web:app" in line:
                    launches.append(relative)
                    assert "--ws-ping-timeout" in line, f"{relative} launches the API without the keepalive"
    assert sorted(launches) == [
        "apps/add_backend/Dockerfile",
        "deploy/add/docker-compose.rollback.yml",
        "docker-compose.add.yml",
    ]
