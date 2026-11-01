"""Talks to head office (ADD) over HTTPS: sign-in, bundles, boards and packages.

Design section 5. Sign-in uses the dashboard's admin username and password
(owner decision, 1 Nov 2026): the ``add_admin`` cookie, the CSRF token from the
login or session response on every change, and the password re-sent ("step-up")
in the body of POST /packages. The password lives only in this object's memory;
it is used again for step-up and to sign in silently when ADD expires the session
(30 minutes idle).

ADD locks sign-in for 15 minutes after 5 wrong passwords, which would also lock
the dashboard, so the app stops after 3 wrong tries within 10 minutes and waits
10 minutes itself (``LoginThrottle``), even across restarts.
"""
from __future__ import annotations

import base64
import json
import math
import re
import ssl
import threading
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

from . import texts
from .api import (Advice, BundleInfo, BytesProgressFn, CancelToken, Capabilities,
                  DeviceRecord, EncryptedPackage, Family, InstallConfig, InstallerError,
                  InstallMode, LocalBundle, PackageRequest, ZoneSummary, ZktSettings)
from .bundles import INSTALLABLE_STATES, BundleStore
from .clock import Clock
from .logging_setup import get_logger, redact, register_secret

log = get_logger("add")

DEFAULT_SERVER = "https://autoattn.slichealth.com"
API = "/api/v1/installer"
LOGIN_PATH = "/api/v1/auth/login"
SESSION_PATH = "/api/v1/auth/session"
LOGOUT_PATH = "/api/v1/auth/logout"
MAX_FAILURES = 3
FAILURE_WINDOW_S = 600.0
PAUSE_S = 600.0
SERVER_LOCK_S = 900.0
PACKAGE_TIMEOUT_S = 150.0
EVENT_TIMEOUT_S = 8.0
_DNS_FAILURE = re.compile(r"getaddrinfo|Name or service not known|nodename nor servname|"
                          r"No address associated|Temporary failure in name resolution|"
                          r"\[Errno 11001\]|\[Errno -2\]|\[Errno 8\]", re.I)
_TLS_FAILURE = re.compile(r"CERTIFICATE_VERIFY_FAILED|SSL|certificate|TLS", re.I)
# Event detail names never sent to ADD: secret-looking names (as in installer_api
# EVENT_SECRET_PARTS and provisioning SECRET_KEYS) and any name that is a key itself
# ("key", "hmac_key", "recipient_public_key"). Flags such as "lock_burned" are kept:
# head office needs them to tell that a board is now locked to State Life. ADD stores
# every other name containing "key" as [REDACTED], so no flag the app sends uses one.
_SECRET_DETAIL = re.compile(r"pass|token|secret|cookie|csrf|hmac|private|comm_?key|nvs|"
                            r"plaintext|ciphertext|(?:^|_)key$", re.I)
# ADD's wait after a 429 is a sliding window; say at most this many minutes.
MAX_WAIT_MINUTES = 60


def tls_context() -> ssl.SSLContext:
    """The operating system's trust store when truststore is bundled, else certifi.

    The Windows store also knows any corporate proxy certificate a site uses.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class LoginThrottle:
    """Pauses sign-in after 3 wrong passwords in 10 minutes, for 10 minutes."""

    def __init__(self, clock: Optional[Clock] = None, state_file: Optional[Path] = None,
                 max_failures: int = MAX_FAILURES, window_s: float = FAILURE_WINDOW_S,
                 pause_s: float = PAUSE_S) -> None:
        self._clock = clock or Clock()
        self._file = state_file
        self._max = max_failures
        self._window = window_s
        self._pause = pause_s
        self._failures: List[float] = []
        self._paused_until = 0.0
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._file is None:
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._failures = [float(x) for x in data.get("failures", [])][-10:]
            self._paused_until = float(data.get("paused_until", 0.0))
        except (OSError, ValueError, TypeError, AttributeError):
            self._failures, self._paused_until = [], 0.0

    def _save(self) -> None:
        if self._file is None:
            return
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"failures": self._failures,
                                              "paused_until": self._paused_until}),
                                  encoding="utf-8")
        except OSError as error:
            log.warning("could not save sign-in pause: %s", error)

    def seconds_left(self) -> float:
        with self._lock:
            return max(0.0, self._paused_until - self._clock.wall())

    def check(self) -> None:
        left = self.seconds_left()
        if left > 0:
            raise InstallerError(texts.login_paused(int((left + 59) // 60)),
                                 cause="sign-in paused by the app")

    def failed(self) -> bool:
        """Record a wrong password; True when this one started a pause."""
        with self._lock:
            now = self._clock.wall()
            self._failures = [t for t in self._failures if now - t < self._window] + [now]
            started = len(self._failures) >= self._max
            if started:
                self._paused_until = now + self._pause
                self._failures = []
            self._save()
            return started

    def succeeded(self) -> None:
        with self._lock:
            self._failures, self._paused_until = [], 0.0
            self._save()

    def server_locked(self) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, self._clock.wall() + SERVER_LOCK_S)
            self._failures = []
            self._save()


class AddError(InstallerError):
    """An error response from ADD; keeps the status and body for callers that need them."""

    def __init__(self, advice: Advice, *, status: int, body: Dict[str, object],
                 cause: str) -> None:
        super().__init__(advice, cause=cause)
        self.status = status
        self.body = body


def _error_body(response: httpx.Response) -> Dict[str, object]:
    """``{code, message, field, ...}`` from an ADD error.

    Installer routes send those keys at the top level and repeat them in
    ``detail``; ADD's shared checks send ``detail`` as a sentence such as
    "Invalid CSRF token.", kept here as ``detail_text``.
    """
    try:
        data = response.json()
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    body = dict(data)
    detail = data.get("detail")
    if isinstance(detail, dict):
        body.update(detail)
    elif isinstance(detail, str):
        body["detail_text"] = detail
        body.setdefault("message", detail)
    return body


def _error_words(body: Mapping[str, object]) -> str:
    return " ".join(str(body.get(key) or "") for key in ("code", "detail_text", "message")).lower()


def network_advice(error: Exception) -> Advice:
    text = str(error)
    if isinstance(error, httpx.ConnectError) and _TLS_FAILURE.search(text):
        return texts.SECURE_CONNECTION_FAILED
    if isinstance(error, httpx.ConnectError) and _DNS_FAILURE.search(text):
        return texts.NO_INTERNET
    return texts.SERVER_UNREACHABLE


def _wait_minutes(response: httpx.Response) -> int:
    """Whole minutes ADD asked the app to wait (Retry-After, in seconds); 1 if unsaid."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:  # missing, or an HTTP date ADD never sends
        return 1
    if not math.isfinite(seconds):
        return 1
    return max(1, min(MAX_WAIT_MINUTES, math.ceil(seconds / 60.0)))


def response_advice(response: httpx.Response) -> Advice:
    """Plain words for an ADD error response (never echoing what was sent)."""
    body = _error_body(response)
    code = str(body.get("code") or "")
    message = body.get("message") if isinstance(body.get("message"), str) else None
    status = response.status_code
    if code == "PASSWORD_REJECTED" or (status == 403 and "password" in _error_words(body)):
        return texts.STEP_UP_FAILED
    if code in ("INVALID_FIELD", "INVALID_REQUEST") or status == 422:
        field = body.get("field")
        return texts.invalid_field(str(field) if field else None,
                                   message if code == "INVALID_FIELD" else None)
    if code == "SETTINGS_CHANGE_NOT_SUPPORTED":
        return texts.SETTINGS_CHANGE_NOT_SUPPORTED
    if code == "BUNDLE_FAMILY_MISMATCH":
        return texts.BUNDLE_WRONG_FAMILY
    if code in ("BUNDLE_UNAVAILABLE", "FILE_NOT_FOUND") or status == 404:
        return texts.NOT_FOUND
    if code == "PROVISIONER_UNAVAILABLE":
        return texts.PROVISIONER_UNAVAILABLE
    if code in ("INSTALLER_DISABLED", "DISABLED"):
        return texts.SERVER_DISABLED
    if status == 426 or code == "APP_UPDATE_REQUIRED":
        return texts.APP_TOO_OLD
    if status == 429 or code == "RATE_LIMITED":
        return texts.rate_limited(_wait_minutes(response))
    if status in (401, 403):
        return texts.SESSION_EXPIRED
    if status in (502, 503, 504):
        # A gateway answer without an installer code: ADD itself is down or restarting
        # (a deploy), which is what the top bar's "Head office not answering" says too.
        return texts.SERVER_UNREACHABLE
    return texts.SERVER_ERROR


def configuration_payload(config: InstallConfig) -> Dict[str, object]:
    """The ``configuration`` object of POST /packages (names as the package builder's)."""
    payload: Dict[str, object] = {
        "firmware_family": config.family.value,
        "zone_id": config.zone_id,
        "zone_name": config.zone_name,
        "device_id": config.device_id,
        "wifi_ssid": config.wifi_ssid,
        "wifi_password": config.wifi_password,
    }
    if config.family is Family.ZKT:
        zkt = config.zkt or ZktSettings()
        payload.update(zkt_port=zkt.port, zkt_comm_key=zkt.comm_key,
                       zkt_preferred_ip=zkt.preferred_ip)
    else:
        hik = config.hik
        if hik is None:
            raise InstallerError(texts.invalid_field("hik_host"), cause="no Hikvision settings")
        # Hikvision boards do not use a ZKT comm key; ADD refuses anything but 0.
        payload.update(zkt_port=4370, zkt_comm_key=0, zkt_preferred_ip="0.0.0.0",
                       hik_host=hik.host, hik_port=hik.port, hik_transport=hik.transport,
                       hik_username=hik.username, hik_password=hik.password,
                       hik_expected_serial=hik.expected_serial, hik_profile=hik.profile,
                       hik_source_epoch=hik.source_epoch, hik_ca_pem=hik.ca_pem)
    return payload


def _package_error(error: AddError, request: PackageRequest) -> InstallerError:
    """Package refusals that need the request to be explained."""
    code = str(error.body.get("code") or "")
    config = request.config
    if code == "FAMILY_MISMATCH":
        known = _family(error.body.get("known_family")) or (
            Family.HIKVISION if config.family is Family.ZKT else Family.ZKT)
        return InstallerError(texts.family_mismatch(known), cause=error.cause)
    if code == "DEVICE_ID_IN_USE":
        suggested = error.body.get("suggested_device_id")
        return InstallerError(texts.device_id_in_use(
            config.device_id, config.zone_id, str(suggested) if suggested else None),
            cause=error.cause)
    return error


def _family(value: object) -> Optional[Family]:
    try:
        return Family(str(value)) if value else None
    except ValueError:
        return None


def _bundle_from(item: Mapping[str, object]) -> Optional[Tuple[BundleInfo, str]]:
    family = _family(item.get("family") or item.get("firmware_family"))
    if family is None or not item.get("bundle_id"):
        return None
    info = BundleInfo(
        bundle_id=str(item["bundle_id"]), family=family,
        label=str(item.get("label") or item["bundle_id"]), version=str(item.get("version", "")),
        recommended=bool(item.get("recommended")),
        release_state=str(item.get("release_state", "")), notes=str(item.get("notes") or ""),
        download_bytes=int(item.get("download_bytes") or 0))
    return info, str(item.get("manifest_sha256") or "")


class AddClient:
    """ServerService, and the BundleSource the bundle store downloads through."""

    def __init__(self, base_url: str = DEFAULT_SERVER, *, app_version: str,
                 store: BundleStore, clock: Optional[Clock] = None,
                 throttle: Optional[LoginThrottle] = None,
                 transport: Optional[httpx.BaseTransport] = None,
                 timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._app_version = app_version
        self._store = store
        self._clock = clock or Clock()
        self._throttle = throttle or LoginThrottle(self._clock)
        self._timeout = timeout_s
        self._http = httpx.Client(
            base_url=self._base, timeout=httpx.Timeout(timeout_s),
            verify=True if transport is not None else tls_context(),
            transport=transport, follow_redirects=False,
            headers={"User-Agent": f"StateLifeDeviceInstaller/{app_version}",
                     "Accept": "application/json"})
        self._lock = threading.RLock()
        self._username: Optional[str] = None
        self._password: Optional[str] = None
        self._user: Optional[str] = None
        self._csrf: Optional[str] = None
        self._catalogue: Dict[str, str] = {}

    # ---------------------------------------------------------- sign-in

    @property
    def signed_in_user(self) -> Optional[str]:
        return self._user

    @property
    def throttle(self) -> LoginThrottle:
        return self._throttle

    def login(self, username: str, password: str) -> str:
        with self._lock:
            self._throttle.check()
            register_secret(password)
            response = self._send("POST", LOGIN_PATH,
                                  json_body={"username": username, "password": password})
            if response.status_code == 200:
                data = response.json()
                self._csrf = str(data.get("csrf_token") or "")
                self._user = str(data.get("username") or username)
                self._username, self._password = username, password
                self._throttle.succeeded()
                log.info("signed in as %s", self._user)
                return self._user
            self._clear()
            if response.status_code == 401:
                if self._throttle.failed():
                    raise InstallerError(texts.login_paused(int(PAUSE_S // 60)),
                                         cause="third wrong password")
                raise InstallerError(texts.LOGIN_FAILED, cause="401 at sign-in")
            if response.status_code == 429:
                self._throttle.server_locked()
                raise InstallerError(texts.ACCOUNT_LOCKED, cause="429 at sign-in")
            raise InstallerError(response_advice(response),
                                 cause=f"sign-in HTTP {response.status_code}")

    def logout(self) -> None:
        with self._lock:
            if self._csrf:
                try:
                    self._http.post(LOGOUT_PATH, headers={"X-CSRF-Token": self._csrf},
                                    timeout=EVENT_TIMEOUT_S)
                except httpx.HTTPError as error:
                    log.info("sign-out request failed: %s", error)
            self._clear()

    def _clear(self) -> None:
        self._username = self._password = self._user = self._csrf = None
        self._http.cookies.clear()

    def _relogin(self) -> bool:
        if not (self._username and self._password):
            return False
        if self._throttle.seconds_left() > 0:
            return False
        response = self._send("POST", LOGIN_PATH,
                              json_body={"username": self._username, "password": self._password})
        if response.status_code == 200:
            data = response.json()
            self._csrf = str(data.get("csrf_token") or "")
            self._user = str(data.get("username") or self._username)
            log.info("session renewed")
            return True
        if response.status_code == 401:
            self._throttle.failed()
        elif response.status_code == 429:
            self._throttle.server_locked()
        self._clear()
        return False

    def _refresh_csrf(self) -> bool:
        response = self._send("GET", SESSION_PATH)
        if response.status_code != 200:
            return False
        data = response.json()
        if not data.get("authenticated"):
            return False
        self._csrf = str(data.get("csrf_token") or "")
        return bool(self._csrf)

    # ---------------------------------------------------------- transport

    def _send(self, method: str, path: str, *, json_body: Optional[Mapping[str, object]] = None,
              headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None
              ) -> httpx.Response:
        try:
            response = self._http.request(method, path, json=json_body, headers=headers,
                                          timeout=timeout or self._timeout)
        except httpx.HTTPError as error:
            log.warning("%s %s failed: %s", method, path, redact(str(error)))
            raise InstallerError(network_advice(error),
                                 cause=f"{method} {path}: {type(error).__name__}") from error
        log.info("%s %s -> %s", method, path, response.status_code)
        return response

    def _call(self, method: str, path: str, *, json_body: Optional[Mapping[str, object]] = None,
              mutate: bool = False, step_up: bool = False, timeout: Optional[float] = None,
              expect: Sequence[int] = (200,)) -> httpx.Response:
        with self._lock:
            if self._user is None:
                raise InstallerError(texts.SESSION_EXPIRED, cause="not signed in")
        relogged = refreshed = False
        while True:
            with self._lock:
                body = dict(json_body or {}) if (json_body is not None or step_up) else None
                if step_up:
                    if not self._password:
                        raise InstallerError(texts.SESSION_EXPIRED, cause="no password")
                    assert body is not None
                    body["password"] = self._password
                headers = {"X-CSRF-Token": self._csrf or ""} if mutate else None
            response = self._send(method, path, json_body=body, headers=headers,
                                  timeout=timeout)
            if response.status_code in expect:
                return response
            if response.status_code == 401 and not relogged:
                relogged = True
                with self._lock:
                    if self._relogin():
                        continue
                raise InstallerError(texts.SESSION_EXPIRED, cause=f"{path}: 401")
            error_body = _error_body(response)
            if response.status_code == 403 and "csrf" in _error_words(error_body) and not refreshed:
                refreshed = True
                with self._lock:
                    if self._refresh_csrf():
                        continue
            raise AddError(response_advice(response), status=response.status_code,
                           body=error_body,
                           cause=f"{method} {path}: HTTP {response.status_code} "
                                 f"{error_body.get('code') or ''}".strip())

    def _json(self, response: httpx.Response) -> Dict[str, object]:
        try:
            data = response.json()
        except ValueError as error:
            raise InstallerError(texts.SERVER_ERROR, cause="response is not JSON") from error
        if not isinstance(data, dict):
            raise InstallerError(texts.SERVER_ERROR, cause="response is not an object")
        return data

    # ------------------------------------------------------------ queries

    def capabilities(self) -> Capabilities:
        data = self._json(self._call("GET", f"{API}/capabilities"))
        return Capabilities(enabled=bool(data.get("enabled")),
                            provisioner_ready=bool(data.get("provisioner_ready")),
                            min_app_version=str(data.get("min_app_version") or "0.0.0"),
                            server_time=str(data["server_time"]) if data.get("server_time")
                            else None)

    def bundles(self, family: Optional[Family] = None) -> List[BundleInfo]:
        data = self._json(self._call("GET", f"{API}/bundles"))
        found: List[BundleInfo] = []
        for item in data.get("bundles") or []:
            parsed = _bundle_from(item) if isinstance(item, dict) else None
            if parsed is None:
                continue
            info, manifest_hash = parsed
            if info.release_state not in INSTALLABLE_STATES[info.family]:
                continue
            if family is not None and info.family is not family:
                continue
            self._catalogue[info.bundle_id] = manifest_hash
            found.append(info)
        found.sort(key=lambda info: (not info.recommended, info.label))
        return found

    def fetch_bundle(self, bundle_id: str, on_bytes: Optional[BytesProgressFn],
                     cancel: CancelToken) -> LocalBundle:
        return self._store.fetch(self, bundle_id, on_bytes, cancel,
                                 expected_manifest_sha256=self._catalogue.get(bundle_id) or None)

    def device(self, mac: str) -> DeviceRecord:
        data = self._json(self._call("GET", f"{API}/devices/{quote(mac, safe=':')}"))
        # A server without the field allows it; anything but true or nothing refuses.
        allowed = data.get("settings_change_allowed")
        reason = data.get("settings_change_reason")
        return DeviceRecord(
            known=bool(data.get("known")), mac=str(data.get("mac") or mac),
            zone_id=data.get("zone_id") or None, zone_name=data.get("zone_name") or None,
            device_id=str(data["device_id"]) if data.get("device_id") is not None else None,
            family=_family(data.get("family")),
            firmware_version=data.get("firmware_version") or None,
            online=data.get("online") if isinstance(data.get("online"), bool) else None,
            last_seen_at=data.get("last_seen_at") or None,
            ever_online=bool(data.get("ever_online")),
            public_name=data.get("public_name") or None, group=data.get("group") or None,
            status_text=data.get("status_text") or None,
            hik_source_epoch=data.get("hik_source_epoch") or None,
            settings_change_allowed=allowed is None or allowed is True,
            settings_change_reason=(reason.strip() or None) if isinstance(reason, str) else None)

    def zones(self) -> List[ZoneSummary]:
        data = self._json(self._call("GET", f"{API}/zones"))
        zones = []
        for item in data.get("zones") or []:
            if not isinstance(item, dict) or not item.get("zone_id"):
                continue
            families = tuple(f for f in (_family(v) for v in item.get("families") or []) if f)
            zones.append(ZoneSummary(
                zone_id=str(item["zone_id"]), zone_name=str(item.get("zone_name") or ""),
                device_ids=tuple(str(v) for v in item.get("device_ids") or []),
                families=families))
        return zones

    def create_package(self, request: PackageRequest) -> EncryptedPackage:
        body: Dict[str, object] = {
            "request_id": request.request_id,
            "mac": request.mac,
            "mode": request.mode.value,
            "app_version": request.app_version,
            "recipient_public_key": request.recipient_public_key_b64,
            "configuration": configuration_payload(request.config),
        }
        # A settings change writes no board software, so it names one only if one was
        # chosen; ADD then needs no bundle for it.
        if request.config.bundle_id or request.mode is not InstallMode.CHANGE_SETTINGS:
            body["bundle_id"] = request.config.bundle_id
        try:
            response = self._call("POST", f"{API}/packages", json_body=body, mutate=True,
                                  step_up=True, timeout=PACKAGE_TIMEOUT_S, expect=(200, 201))
        except AddError as error:
            raise _package_error(error, request) from None
        data = self._json(response)
        try:
            manifest = data["provisioning_manifest"]
            if not isinstance(manifest, dict):
                raise ValueError("manifest")
            nvs = base64.b64decode(str(data["provisioning_ciphertext_b64"]), validate=True)
            key = base64.b64decode(str(data.get("hmac_ciphertext_b64") or ""), validate=True)
        except (KeyError, ValueError) as error:
            raise InstallerError(texts.PACKAGE_BAD, cause="package response") from error
        return EncryptedPackage(str(data.get("request_id") or ""), manifest, nvs, key)

    def report(self, request_id: str, mac: str, kind: str, detail: Dict[str, object]) -> None:
        """Best effort: an audit event must never stop an install."""
        safe = {key: (redact(value) if isinstance(value, str) else value)
                for key, value in (detail or {}).items() if not _SECRET_DETAIL.search(str(key))}
        try:
            self._call("POST", f"{API}/events", mutate=True, timeout=EVENT_TIMEOUT_S,
                       json_body={"request_id": request_id, "mac": mac, "kind": kind,
                                  "detail": safe}, expect=(200, 201, 202, 204))
        except InstallerError as failure:
            log.warning("event %s not recorded: %s", kind, failure.cause or failure.advice.code)

    # --------------------------------------------------- bundle source

    def bundle_manifest(self, bundle_id: str) -> Tuple[Dict[str, object], str]:
        data = self._json(self._call("GET", f"{API}/bundles/{quote(bundle_id)}/manifest"))
        manifest, signature = data.get("manifest"), data.get("signature")
        if not isinstance(manifest, dict) or not isinstance(signature, str):
            raise InstallerError(texts.BUNDLE_NOT_TRUSTED, cause="manifest response")
        return manifest, signature

    def download_bundle_file(self, bundle_id: str, name: str, destination: Path,
                             on_chunk: Callable[[int], None], cancel: CancelToken) -> None:
        path = f"{API}/bundles/{quote(bundle_id)}/files/{quote(name)}"
        for attempt in (1, 2):
            try:
                with self._http.stream("GET", path, timeout=httpx.Timeout(
                        self._timeout, read=60.0)) as response:
                    if response.status_code == 401:
                        with self._lock:
                            if attempt == 1 and self._relogin():
                                continue
                        raise InstallerError(texts.SESSION_EXPIRED, cause=f"{name}: 401")
                    if response.status_code != 200:
                        raise InstallerError(texts.BUNDLE_DOWNLOAD_FAILED,
                                             cause=f"{name}: HTTP {response.status_code}")
                    with destination.open("wb") as out:
                        for chunk in response.iter_bytes(64 * 1024):
                            if cancel.cancelled:
                                raise InstallerError(texts.CANCELLED, cause="download cancelled")
                            out.write(chunk)
                            on_chunk(len(chunk))
                return
            except httpx.HTTPError as error:
                raise InstallerError(network_advice(error) if not isinstance(
                    error, httpx.ReadTimeout) else texts.BUNDLE_DOWNLOAD_FAILED,
                    cause=f"{name}: {type(error).__name__}") from error
