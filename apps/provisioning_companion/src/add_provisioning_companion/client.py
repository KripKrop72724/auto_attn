from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import queue
import tempfile
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from add_provisioning_companion import __version__
from add_provisioning_companion.identity import InstallationIdentity
from add_provisioning_companion.serial_worker import worker_main

PRODUCTION_API_ORIGIN = "https://autoattn.slichealth.com"
PRODUCTION_WS_URL = "wss://autoattn.slichealth.com/companion/v1/stream"


def platform_id() -> str:
    if platform.system() == "Windows" and platform.machine().lower() in {"amd64", "x86_64"}:
        return "windows-x64"
    if platform.system() == "Darwin" and platform.machine().lower() == "arm64":
        return "macos-arm64"
    raise RuntimeError("Supported platforms are Windows x64 and macOS Apple Silicon.")


class CompanionClient:
    def __init__(
        self,
        identity: InstallationIdentity,
        notify: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.identity = identity
        self.notify = notify
        self.base_url = os.environ.get("ADD_COMPANION_API_URL", PRODUCTION_API_ORIGIN).rstrip("/")
        self.ws_url = os.environ.get("ADD_COMPANION_WS_URL", PRODUCTION_WS_URL)
        if os.environ.get("ADD_COMPANION_DEVELOPMENT_MODE") != "1" and (
            self.base_url != PRODUCTION_API_ORIGIN or self.ws_url != PRODUCTION_WS_URL
        ):
            raise RuntimeError("Production companion endpoints cannot be overridden.")
        self.session_keys: dict[str, X25519PrivateKey] = {}
        self.session_ports: dict[str, str] = {}
        self.sequence: dict[str, int] = {}
        self.pending_events: list[dict[str, Any]] = []
        self.completed_sessions: set[str] = set()
        self.efuse_verified_sessions: set[str] = set()
        self.selection_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self.pairing_pending_until = 0.0
        self.retry_requested = threading.Event()

    def select_port(self, port: str) -> None:
        try:
            self.selection_queue.put_nowait(port)
        except queue.Full:
            pass

    def request_retry(self) -> None:
        self.retry_requested.set()

    async def create_pairing(self) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/companion/v1/pairings",
                json={
                    "installation_id": self.identity.installation_id,
                    "public_key": self.identity.public_key_b64,
                    "platform": platform_id(),
                    "application_version": __version__,
                },
            )
            if response.status_code == 403:
                raise RuntimeError("This companion installation was revoked.")
            response.raise_for_status()
            result = response.json()
            self.companion_id = result["companion_id"]
            self.pairing_pending_until = time.monotonic() + 280
            self.notify("pairing", result)

    def _next_sequence(self, session_id: str) -> int:
        value = self.sequence.get(session_id, 0) + 1
        self.sequence[session_id] = value
        return value

    async def _send_event(self, websocket, payload: dict[str, Any]) -> bool:
        try:
            await websocket.send(json.dumps(payload))
        except Exception:
            return False
        return True

    async def _flush_pending_events(self, websocket) -> None:
        for payload in list(self.pending_events):
            if not await self._send_event(websocket, payload):
                break
            self.pending_events.remove(payload)

    async def _heartbeat(self, websocket) -> None:
        while True:
            await websocket.send(
                json.dumps(
                    {
                        "type": "heartbeat",
                        "application_version": __version__,
                        "platform": platform_id(),
                    }
                )
            )
            await asyncio.sleep(30)

    async def _worker(self, request: dict, on_event=None) -> dict:
        context = get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=worker_main, args=(child,), daemon=True)
        process.start()
        parent.send(request)
        try:
            while True:
                message = await asyncio.to_thread(parent.recv)
                if message.get("event") and on_event:
                    await on_event(message)
                    continue
                if message.get("ok"):
                    return message["result"]
                raise RuntimeError(message.get("error", "USB worker failed"))
        finally:
            parent.close()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()

    async def _inspect(self, websocket, command: dict) -> None:
        session_id = command["session_id"]
        self.notify("stage", {"text": "Inspecting connected ESP32-S3…"})
        result = await self._worker({"operation": "probe"})
        if result.get("selection_required"):
            self.notify("device_selection", result)
            try:
                selected = await asyncio.wait_for(
                    asyncio.to_thread(self.selection_queue.get), timeout=120
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("DEVICE_SELECTION_TIMEOUT") from exc
            result = await self._worker({"operation": "probe", "port": selected})
        private = X25519PrivateKey.generate()
        self.session_keys[session_id] = private
        self.session_ports[session_id] = result.pop("port")
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        await websocket.send(
            json.dumps(
                {
                    "type": "inspection",
                    "session_id": session_id,
                    "sequence": self._next_sequence(session_id),
                    "inspection": {
                        **result,
                        "recipient_public_key": base64.b64encode(public).decode("ascii"),
                    },
                    "hmac_challenge_verified": False,
                }
            )
        )
        self.notify("device", result)

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = datetime.now(timezone.utc).isoformat()
        nonce = str(uuid4())
        body_hash = hashlib.sha256(b"").hexdigest()
        material = "\n".join([method, path, timestamp, nonce, body_hash]).encode()
        signature = self.identity.private_key.sign(material)
        return {
            "X-ADD-Companion-Id": self.companion_id,
            "X-ADD-Timestamp": timestamp,
            "X-ADD-Nonce": nonce,
            "X-ADD-Body-SHA256": body_hash,
            "X-ADD-Signature": base64.b64encode(signature).decode("ascii"),
        }

    async def _flash(self, websocket, command: dict) -> None:
        session_id = command["session_id"]
        if session_id in self.completed_sessions:
            return
        private = self.session_keys.get(session_id)
        port = self.session_ports.get(session_id)
        if private is None or not port:
            await self._inspect(websocket, command)
            private = self.session_keys[session_id]
            port = self.session_ports[session_id]
        resolved = await self._worker(
            {
                "operation": "resolve_port",
                "expected_mac": command["hardware_mac"],
            }
        )
        port = str(resolved["port"])
        self.session_ports[session_id] = port
        path = f"/companion/v1/sessions/{session_id}/artifact"
        headers = self._signed_headers("POST", path)
        with tempfile.TemporaryDirectory(prefix="add-companion-artifact-") as directory:
            artifact_path = Path(directory) / "artifact.json"
            artifact_path.touch(mode=0o600, exist_ok=False)
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", f"{self.base_url}{path}", headers=headers
                ) as response:
                    response.raise_for_status()
                    with artifact_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            handle.write(chunk)
            expected_digest = command["artifact"]["sha256"]
            if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != expected_digest:
                raise RuntimeError("ARTIFACT_HASH_MISMATCH")
            private_raw = private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )

            async def send_event(event: dict) -> None:
                if event["state"] == "EFUSE_VERIFIED":
                    self.efuse_verified_sessions.add(session_id)
                payload = {
                    "type": "event",
                    "session_id": session_id,
                    "sequence": self._next_sequence(session_id),
                    "state": event["state"],
                    "progress": event["progress"],
                    "details": event["details"],
                }
                self.pending_events.append(payload)
                if await self._send_event(websocket, payload):
                    self.pending_events.remove(payload)
                self.notify("progress", event)

            await self._worker(
                {
                    "operation": "flash",
                    "artifact_path": str(artifact_path),
                    "private_key_b64": base64.b64encode(private_raw).decode("ascii"),
                    "expected_mac": command["hardware_mac"],
                    "expected_session_id": session_id,
                    "expected_classification": command["classification"],
                    "factory_signing_public_key_pem_b64": command[
                        "factory_signing_public_key_pem_b64"
                    ],
                    "expected_factory_manifest_sha256": command["bundle"][
                        "manifest_sha256"
                    ],
                    "resume_after_efuse": bool(
                        command.get("resume_state")
                        in {"EFUSE_VERIFIED", "FLASHING", "READBACK_VERIFYING"}
                        and session_id in self.efuse_verified_sessions
                    ),
                    "port": port,
                },
                on_event=send_event,
            )
        self.completed_sessions.add(session_id)
        self.session_keys.pop(session_id, None)

    async def run(self) -> None:
        while True:
            try:
                parsed = urlparse(self.ws_url)
                if parsed.scheme != "wss" and os.environ.get("ADD_COMPANION_ALLOW_INSECURE_WS") != "1":
                    raise RuntimeError("Companion stream requires TLS.")
                uri = f"{self.ws_url}?installation_id={self.identity.installation_id}"
                async with websockets.connect(
                    uri,
                    subprotocols=["add-provisioning-v1"],
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    challenge = json.loads(await websocket.recv())
                    signature = self.identity.private_key.sign(
                        base64.b64decode(challenge["challenge"])
                    )
                    await websocket.send(
                        json.dumps({"signature": base64.b64encode(signature).decode("ascii")})
                    )
                    self.companion_id = challenge.get("companion_id", "")
                    if not self.companion_id:
                        self.companion_id = self.identity.installation_id
                    self.notify("connected", {"online": True})
                    heartbeat = asyncio.create_task(self._heartbeat(websocket))
                    try:
                        await self._flush_pending_events(websocket)
                        async for raw in websocket:
                            command = json.loads(raw)
                            kind = command.get("type")
                            if kind in {"inspect", "resume"}:
                                if kind == "resume":
                                    session = command["session"]
                                    if session["state"] not in {
                                        "WAITING_FOR_COMPANION",
                                        "WAITING_FOR_DEVICE",
                                        "INSPECTING",
                                    }:
                                        continue
                                    command = {"session_id": session["session_id"]}
                                await self._inspect(websocket, command)
                            elif kind == "flash":
                                await self._flash(websocket, command)
                            elif kind == "revoked":
                                raise RuntimeError("This companion was revoked in ADD.")
                    finally:
                        heartbeat.cancel()
                        with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                            await heartbeat
            except websockets.InvalidStatus as exc:
                if exc.response.status_code == 403:
                    if time.monotonic() >= self.pairing_pending_until:
                        await self.create_pairing()
                else:
                    self.notify("error", {"text": f"ADD connection failed ({exc.response.status_code})."})
            except Exception as exc:
                self.notify("error", {"text": str(exc)[:180]})
            self.notify("connected", {"online": False})
            await asyncio.to_thread(self.retry_requested.wait, 5)
            self.retry_requested.clear()
