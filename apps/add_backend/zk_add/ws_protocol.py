"""uvicorn WebSocket protocol that reports the keepalive closes ADD never sees.

When a peer misses uvicorn's pong deadline, uvicorn closes the socket with
1011 and logs it only at TRACE; the application just receives the same 1005
"connection lost" as for any abrupt drop. This subclass of the protocol that
uvicorn selects by default (websockets-sansio) adds one warning per keepalive
close and shares payload-free transport facts with the endpoint through the
ASGI scope, so a device session's end line can say what really happened,
and a session stops starting queued messages as soon as uvicorn has closed
its socket for shutdown or keepalive.

Launch with ``--ws zk_add.ws_protocol:DiagnosticWebSocketProtocol``. Every
override calls the original implementation unchanged.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol

from zk_add.device_session import TRANSPORT_EXTENSION, WebSocketTransportFacts, safe_label

logger = logging.getLogger(__name__)


class DiagnosticWebSocketProtocol(WebSocketsSansIOProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.add_facts = WebSocketTransportFacts()
        self._bytes_at_ping = 0

    def handle_connect(self, event: Any) -> None:
        super().handle_connect(event)
        # The ASGI task that receives this scope has been created but has not
        # run yet, so the endpoint always finds the facts.
        scope = self.__dict__.get("scope")
        if isinstance(scope, dict):
            scope.setdefault("extensions", {})[TRANSPORT_EXTENSION] = self.add_facts

    def shutdown(self) -> None:
        self.add_facts.server_shutdown = True
        super().shutdown()

    def data_received(self, data: bytes) -> None:
        facts = self.add_facts
        facts.bytes_received += len(data)
        facts.last_data_at = time.monotonic()
        super().data_received(data)

    def send_keepalive_ping(self) -> None:
        super().send_keepalive_ping()
        if self.pending_ping_payload is not None:
            self.add_facts.pings_sent += 1
            self._bytes_at_ping = self.add_facts.bytes_received

    def handle_pong(self, event: Any) -> None:
        awaited = self.pending_ping_payload
        super().handle_pong(event)
        if awaited is not None and self.pending_ping_payload is None:
            self.add_facts.pongs_received += 1
            self.add_facts.last_ping_rtt = self.last_ping_rtt

    def keepalive_timeout(self) -> None:
        if not (self.close_sent or self.transport.is_closing()):
            facts = self.add_facts
            facts.keepalive_timed_out = True
            now = time.monotonic()
            scope = self.__dict__.get("scope") or {}
            # read_paused=true: ADD itself was not reading. Otherwise bytes
            # arriving since the ping mean the peer was busy sending and could
            # not answer it; none mean the path or the peer had gone quiet.
            logger.warning(
                "ADD WebSocket keepalive timeout connector_id=%s path=%s ping_timeout_seconds=%s "
                "seconds_since_ping=%.1f seconds_since_data=%s bytes_since_ping=%d read_paused=%s "
                "last_ping_rtt_seconds=%s pings_sent=%d pongs_received=%d",
                safe_label(facts.connector_id), safe_label(scope.get("path")), self.ping_timeout,
                self.loop.time() - self.ping_sent_at,
                "-" if facts.last_data_at is None else f"{now - facts.last_data_at:.1f}",
                facts.bytes_received - self._bytes_at_ping, str(self.read_paused).lower(),
                "-" if facts.last_ping_rtt is None else f"{facts.last_ping_rtt:.3f}",
                facts.pings_sent, facts.pongs_received,
            )
        super().keepalive_timeout()
