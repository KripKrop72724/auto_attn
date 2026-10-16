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
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

import zk_add.__main__ as add_main
from zk_add.device_session import WS_PING_INTERVAL_SECONDS, WS_PING_TIMEOUT_SECONDS, DeviceStreamSession

ROOT = Path(__file__).resolve().parents[2]
# Each message takes longer than uvicorn's whole ping interval plus timeout.
PROCESSING_SECONDS = 1.0
PING_INTERVAL = 0.2
PING_TIMEOUT = 0.5


async def reader_stream(websocket):
    await websocket.accept()
    session = DeviceStreamSession(websocket, "ZL-KEEPALIVE")
    session.start()
    try:
        while (raw := await session.next_message()) is not None:
            async def work(raw=raw):
                await asyncio.sleep(PROCESSING_SECONDS)
                await session.send_json({"type": "ack", "message": raw})
            await session.process("heartbeat", work())
    finally:
        await session.stop()


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
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
        ws="websockets-sansio", ws_ping_interval=PING_INTERVAL, ws_ping_timeout=PING_TIMEOUT,
        **overrides,
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
    _assert_keepalive(shlex.split(shell_command.split("exec ", 1)[1]))

    # The rollback override replaces the whole command for an older image, so
    # it must repeat the settings or a rollback silently drops them.
    rollback = _api_service(ROOT / "deploy/add/docker-compose.rollback.yml")["command"]
    assert rollback[:2] == ["uvicorn", "zk_add.web:app"]
    _assert_keepalive(rollback)

    dockerfile = (ROOT / "apps/add_backend/Dockerfile").read_text().splitlines()
    cmd = [line for line in dockerfile if line.startswith("CMD ")]
    assert len(cmd) == 1
    _assert_keepalive(json.loads(cmd[0].removeprefix("CMD ")))

    assert WS_PING_TIMEOUT_SECONDS >= 3 * WS_PING_INTERVAL_SECONDS


def test_development_entry_point_uses_the_same_keepalive(monkeypatch):
    calls = []
    monkeypatch.setattr(add_main.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("sys.argv", ["zk-add"])
    add_main.main()
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
