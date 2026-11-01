"""Is there internet, and can the head office server be reached? (design section 8)

Internet: Windows' own probe, http://www.msftconnecttest.com/connecttest.txt, which
answers exactly "Microsoft Connect Test" unless a captive portal intercepts it.
Server: GET /health/live, which needs no sign-in (/health/ready can fail for
reasons inside the server that do not matter here). The screens call ``check``
every 15 seconds.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .add_client import DEFAULT_SERVER, tls_context
from .api import ConnectivityStatus
from .logging_setup import get_logger

log = get_logger("connectivity")

INTERNET_PROBE = "http://www.msftconnecttest.com/connecttest.txt"
INTERNET_ANSWER = "Microsoft Connect Test"
HEALTH_PATH = "/health/live"


class Connectivity:
    """ConnectivityService."""

    def __init__(self, server_url: str = DEFAULT_SERVER, *, timeout_s: float = 6.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self._server = server_url.rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s), follow_redirects=False,
                                  verify=True if transport is not None else tls_context(),
                                  transport=transport,
                                  headers={"User-Agent": "StateLifeDeviceInstaller"})

    def _internet(self) -> Optional[bool]:
        try:
            response = self._http.get(INTERNET_PROBE)
        except httpx.HTTPError:
            return False
        if response.status_code == 200 and response.text.strip() == INTERNET_ANSWER:
            return True
        # Some other page answered: a hotel or office sign-in page is in the way.
        return None

    def _server_up(self) -> bool:
        try:
            response = self._http.get(self._server + HEALTH_PATH)
        except httpx.HTTPError as error:
            log.info("server check failed: %s", type(error).__name__)
            return False
        if response.status_code != 200:
            return False
        try:
            return bool(response.json().get("ok"))
        except ValueError:
            return False

    def check(self) -> ConnectivityStatus:
        internet = self._internet()
        server = self._server_up()
        if server:
            # The server answering proves there is internet even if the probe is blocked.
            return ConnectivityStatus(True, True, "Connected to head office.")
        if internet is None:
            return ConnectivityStatus(False, False,
                                      "This network needs you to sign in to it first. Open a "
                                      "web browser and follow its sign-in page.")
        if internet:
            return ConnectivityStatus(True, False, "The internet works, but head office is "
                                                   "not answering.")
        return ConnectivityStatus(False, False, "This computer is not connected to the internet.")
