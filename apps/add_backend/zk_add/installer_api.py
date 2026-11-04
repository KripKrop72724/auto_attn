"""State Life Device Installer API: /api/v1/installer/* (docs/esp-installer-server.md).

The Windows app signs in as the ADD administrator: the admin cookie on every
route, X-CSRF-Token on mutations and a password step-up for each package.
Every secret stays on the server. The protected provisioner builds a board's
encrypted settings (NVS) and HMAC eFuse key and encrypts both to the app's
one-time X25519 key. Firmware comes from signed installer bundles in a
read-only store. The dashboard offers the app itself for download from
another read-only store (installer_apps.py).

Errors on these routes are {"code", "message", "field" (optional)}. "detail"
stays what ADD's usual handler sends: the same object for installer refusals,
the original sentence for the shared sign-in checks. Messages are plain words
and never repeat a submitted value.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from zk_add import public_status
from zk_add.audit import append_audit
from zk_add.device_health import evaluate_health
from zk_add.hikvision_delivery import HikvisionPolicy
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.installer_apps import app_store
from zk_add.models import AuditEvent, Connector, PublicDeviceStatus
from zk_add.ota import FirmwareRelease
from zk_add.provisioning import (
    HEX_PSK_PATTERN,
    ID_PATTERN,
    RFC1918_NETWORKS,
    ensure_assignment_available,
    sanitize_event_details,
    semver_key,
)
from zk_add.provisioning_api import (
    FixedWindowLimiter,
    client_ip,
    require_admin,
    require_admin_mutation,
)
from zk_add.security import AdminContext, require_step_up
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, iso_utc, utc_now

logger = logging.getLogger(__name__)

PREFIX = "/api/v1/installer"
router = APIRouter(prefix=PREFIX)

# Bundle ids, image names and other path parts taken from a request or a
# manifest. Nothing else is ever joined to a store path.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{7,95}$"
MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(?:([:-]?)[0-9A-Fa-f]{2})(?:\1[0-9A-Fa-f]{2}){4}$")
ROLE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
EVENT_KIND_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,47}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

BUNDLE_SCHEMA = "zone-lite-installer-bundle-v1"
BUNDLE_LAYOUT = "zone-lite-ota-v1"
BUNDLE_RELEASE_STATES = frozenset({"AVAILABLE", "FIELD_BASELINE", "HIL_ONLY"})
# The app descriptor project name of each family's image.
APP_PROJECTS = {"zkt": "zone_lite", "hikvision": "zone_lite_hikvision"}
FLASH_BYTES = 16 * 1024 * 1024
MANIFEST_MAX_BYTES = 1024 * 1024
SIGNATURE_MAX_BYTES = 8 * 1024
EVENT_DETAIL_MAX_BYTES = 8 * 1024
RESERVED_EVENT_KINDS = frozenset({"PACKAGE_ISSUED"})
# Only a new_board package issued for a MAC means this app set that board up, so
# its key is the fleet root's (settings_change_refusal). A resume burns no key:
# the board may have been locked by the flow before this app, with an older root.
KEY_PACKAGE_MODES = frozenset({"new_board"})
SETTINGS_CHANGE_REFUSED = (
    "This board was set up before this app. Only head office can change its settings."
)

# Hikvision text settings with their NVS byte limits; all but the CA are required.
HIK_TEXT_LIMITS = (
    ("hik_username", 63),
    ("hik_password", 127),
    ("hik_expected_serial", 119),
    ("hik_profile", 79),
    ("hik_source_epoch", 64),
)
HIK_FIELDS = (
    "hik_host", "hik_port", "hik_transport", "hik_username", "hik_password",
    "hik_expected_serial", "hik_profile", "hik_source_epoch", "hik_ca_pem",
)
# A ZKT board must not carry Hikvision settings (ProvisioningConfiguration);
# the port and transport have defaults and are ignored for ZKT.
HIK_SETTINGS_FOR_ZKT = (
    "hik_host", "hik_username", "hik_password", "hik_expected_serial",
    "hik_profile", "hik_source_epoch", "hik_ca_pem",
)
# Extra event detail keys never stored, beyond sanitize_event_details. "pass",
# "pwd", "psk" and "key" also catch the firmware's own NVS names (wifi_pass,
# hik_pass, tel_pass, zkt_key) and names such as api_key and comm-key.
EVENT_SECRET_PARTS = (
    "secret", "token", "cookie", "csrf", "pass", "pwd", "psk", "key", "hmac", "nvs",
    "plaintext", "ciphertext", "private", "ords",
)

# Pakistan Standard Time has no daylight saving; a fixed offset needs no tzdata.
PKT = timezone(timedelta(hours=5))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

DEFAULT_FIELD_MESSAGE = "Check this setting and try again."
FIELD_MESSAGES = {
    "password": "Type your ADD password to confirm.",
    "request_id": "The app sent a bad request number. Close the app, open it again and retry.",
    "mac": "The board ID is not valid. Unplug the board, plug it in again and retry.",
    "mode": "The app asked for an unknown kind of setup. Update the app and try again.",
    "bundle_id": "Choose a firmware from the list.",
    "app_version": "This copy of the app reports a bad version. Install the app again.",
    "recipient_public_key": (
        "The app's one-time security key is not valid. Close the app, open it again and retry."
    ),
    "configuration": "The board settings are missing. Fill them in and try again.",
    "family": "Choose ZKTeco or Hikvision.",
    "kind": "The app sent an unknown kind of report. Update the app.",
    "detail": "The app sent a report head office could not store. Update the app.",
    "firmware_family": "Choose ZKTeco or Hikvision.",
    "zone_id": (
        "ZONE ID can use letters, numbers, dots, dashes and underscores, must start with "
        "a letter or number, and can be up to 64 long."
    ),
    "zone_name": (
        "ZONE NAME must be 1 to 120 characters, with no space at the start or the end."
    ),
    "device_id": (
        "The device number can use letters, numbers, dots, dashes and underscores, must "
        "start with a letter or number, and can be up to 31 long."
    ),
    "wifi_ssid": "The Wi-Fi name must be 1 to 32 characters.",
    "wifi_password": "The Wi-Fi password must be 8 to 63 characters, or exactly 64 hex digits.",
    "zkt_port": "The attendance machine port must be a number from 1 to 65535.",
    "zkt_comm_key": "The comm key must be a whole number from 0 to 4294967295.",
    "zkt_preferred_ip": (
        "The machine address must be 0.0.0.0, or a local address such as 192.168.1.20."
    ),
    "hik_host": "The machine address must be a local address such as 192.168.1.20.",
    "hik_port": "The attendance machine port must be a number from 1 to 65535.",
    "hik_transport": "Choose HTTP digest or HTTPS for the attendance machine.",
    "hik_username": "The attendance machine user name must be 1 to 63 characters.",
    "hik_password": "The attendance machine password must be 1 to 127 characters.",
    "hik_expected_serial": "The attendance machine serial number must be 1 to 119 characters.",
    "hik_profile": "Choose the attendance machine model.",
    "hik_source_epoch": "The record series name must be 1 to 64 characters.",
    "hik_ca_pem": "The machine certificate is too long (up to 3967 characters).",
}
# Fixed details raised by the shared admin dependencies (zk_add.security).
STRING_DETAILS = {
    "Authentication required.": ("AUTH_REQUIRED", "Please sign in again.", None),
    "Session expired.": (
        "SESSION_EXPIRED", "You were signed out after a long pause. Please sign in again.", None,
    ),
    "Invalid CSRF token.": (
        "CSRF_INVALID", "Your sign-in is out of date. Please sign in again.", None,
    ),
    "Password confirmation failed.": (
        "PASSWORD_REJECTED", "The password was not accepted. Type your ADD password again.",
        "password",
    ),
}
STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "TOO_LARGE",
    415: "BAD_REQUEST",
    422: "INVALID_FIELD",
    429: "RATE_LIMITED",
}

package_limiter = FixedWindowLimiter(
    settings.installer_package_limit, settings.installer_rate_window_seconds
)
event_limiter = FixedWindowLimiter(
    settings.installer_event_limit, settings.installer_rate_window_seconds
)


class InstallerError(HTTPException):
    """An installer refusal; install_error_handlers gives it the installer shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        field: str | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        detail: dict[str, Any] = {"code": code, "message": message}
        if field:
            detail["field"] = field
        detail.update(extra)
        super().__init__(status_code=status_code, detail=detail, headers=headers)


def invalid_field(field: str | None, message: str | None = None) -> InstallerError:
    known = field if field in FIELD_MESSAGES else None
    return InstallerError(
        422,
        "INVALID_FIELD",
        message or FIELD_MESSAGES.get(known or "", DEFAULT_FIELD_MESSAGE),
        field=known,
    )


def _provisioner_unavailable() -> InstallerError:
    return InstallerError(
        502,
        "PROVISIONER_UNAVAILABLE",
        "Head office could not prepare the board's settings just now. Wait a minute, "
        "then try again. Nothing was written to the board.",
    )


def _rate_limited(seconds: int) -> InstallerError:
    """The wait the limiter really imposes: until its oldest counted request expires."""
    minutes = max(1, math.ceil(seconds / 60))
    return InstallerError(
        429,
        "RATE_LIMITED",
        f"Too many requests in a short time. Wait {minutes} minute{'s' if minutes > 1 else ''}, "
        "then try again.",
        headers={"Retry-After": str(seconds)},
    )


# ------------------------------------------------------------- error shape


def is_installer_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _error_response(
    status_code: int,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    detail: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={**body, "detail": dict(body) if detail is None else detail},
        headers=headers,
    )


def _http_error_response(error: StarletteHTTPException) -> JSONResponse:
    detail = error.detail
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return _error_response(error.status_code, dict(detail), getattr(error, "headers", None))
    code, message, field = STRING_DETAILS.get(
        detail if isinstance(detail, str) else "",
        (
            STATUS_CODES.get(error.status_code, "SERVER_ERROR"),
            "Head office could not do this. Try again, or call head office.",
            None,
        ),
    )
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    # "detail" keeps the shared check's own sentence, e.g. "Invalid CSRF token.".
    return _error_response(
        error.status_code, body, getattr(error, "headers", None),
        detail=detail if isinstance(detail, str) else message,
    )


def _validation_error_response(error: RequestValidationError) -> JSONResponse:
    # Name the first field the request got wrong, from its location only: the
    # rejected input and pydantic's message stay out of the response.
    for item in error.errors():
        names = [
            part for part in item.get("loc", ())
            if isinstance(part, str) and part not in {"body", "query", "path", "header", "cookie"}
        ]
        if names:
            raised = invalid_field(names[-1])
            return _error_response(422, dict(raised.detail))
    return _error_response(
        422,
        {
            "code": "INVALID_REQUEST",
            "message": "The app sent a request head office could not read. Update the app.",
        },
    )


def install_error_handlers(app) -> None:
    """Give installer routes their error shape; every other path keeps its handlers.

    Application exception handlers run after the request's database session has
    been rolled back, exactly as for every other route.
    """
    validation_fallback = app.exception_handlers[RequestValidationError]
    http_fallback = app.exception_handlers[StarletteHTTPException]

    async def _delegate(handler, request, error):
        result = handler(request, error)
        return await result if inspect.isawaitable(result) else result

    async def installer_validation_error(request: Request, error: RequestValidationError):
        if not is_installer_path(request.url.path):
            return await _delegate(validation_fallback, request, error)
        return _validation_error_response(error)

    async def installer_http_error(request: Request, error: StarletteHTTPException):
        if not is_installer_path(request.url.path):
            return await _delegate(http_fallback, request, error)
        return _http_error_response(error)

    app.add_exception_handler(RequestValidationError, installer_validation_error)
    app.add_exception_handler(StarletteHTTPException, installer_http_error)


# ------------------------------------------------------------ dependencies


def installer_enabled() -> bool:
    return bool(settings.installer_enabled and settings.provisioning_enabled)


def _require_enabled() -> None:
    if not installer_enabled():
        raise InstallerError(
            409,
            "INSTALLER_DISABLED",
            "Head office is not accepting board setups right now. Try again later, "
            "or call head office.",
        )


def installer_admin(
    auth: tuple[Session, AdminContext] = Depends(require_admin),
) -> tuple[Session, AdminContext]:
    _require_enabled()
    return auth


def installer_admin_mutation(
    auth: tuple[Session, AdminContext] = Depends(require_admin_mutation),
) -> tuple[Session, AdminContext]:
    _require_enabled()
    return auth


def normalize_installer_mac(value: str) -> str:
    """aa:bb:cc:dd:ee:ff, the form onboarding stores in Connector.hardware_id."""
    text = value.strip() if isinstance(value, str) else ""
    if not MAC_PATTERN.fullmatch(text):
        raise ValueError("Invalid board MAC address.")
    compact = re.sub(r"[:-]", "", text).lower()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


# ---------------------------------------------------------- bundle store


class BundleInvalid(Exception):
    """A skipped bundle; the reason is a fixed phrase, never file content."""


@dataclass(frozen=True)
class InstallerBundle:
    bundle_id: str
    family: str
    label: str
    version: str
    release_state: str
    recommended: bool  # the manifest flag; the listing keeps the newest per family
    notes: str
    created_at: datetime
    manifest: dict
    signature: str
    manifest_sha256: str
    files: dict  # image name -> (size, sha256)
    directory: Path

    @property
    def download_bytes(self) -> int:
        return sum(size for size, _digest in self.files.values())


@lru_cache(maxsize=4)
def _signing_key(pem_b64: str) -> rsa.RSAPublicKey:
    # Decoded exactly as the OTA release store does (ota._verify_manifest).
    key = serialization.load_pem_public_key(base64.b64decode(pem_b64))
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("The firmware signing key is not an RSA key.")
    return key


def _verify_signature(canonical: bytes, signature_text: str) -> None:
    pem_b64 = settings.firmware_signing_public_key_pem_b64
    if not pem_b64:
        raise BundleInvalid("no firmware signing public key is configured")
    try:
        key = _signing_key(pem_b64)
    except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm):
        raise BundleInvalid("the firmware signing public key is invalid") from None
    try:
        signature = base64.b64decode("".join(signature_text.split()), validate=True)
        key.verify(
            signature,
            canonical,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError, binascii.Error):
        raise BundleInvalid("the manifest signature does not verify") from None


def _contained_file(directory: Path, name: str) -> Path:
    """A regular file directly inside the bundle directory, never outside it."""
    if not NAME_PATTERN.fullmatch(name):
        raise BundleInvalid("a file name is not allowed")
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        raise BundleInvalid(f"{name} is missing")
    return path


def _read_limited(directory: Path, name: str, limit: int) -> bytes:
    path = _contained_file(directory, name)
    if path.stat().st_size > limit:
        raise BundleInvalid(f"{name} is too large")
    return path.read_bytes()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_text(manifest: dict, key: str, limit: int, *, required: bool = True) -> str:
    value = manifest.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise BundleInvalid(f"{key} is invalid")
    return value


def _verify_bundle(directory: Path, bundle_id: str) -> InstallerBundle:
    manifest_bytes = _read_limited(directory, "manifest.json", MANIFEST_MAX_BYTES)
    try:
        signature_text = _read_limited(directory, "manifest.sig", SIGNATURE_MAX_BYTES).decode("ascii")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BundleInvalid("the manifest or its signature is unreadable") from None
    if not isinstance(manifest, dict):
        raise BundleInvalid("the manifest is not an object")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    _verify_signature(canonical, signature_text)
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleInvalid("the manifest schema is unknown")
    if manifest.get("bundle_id") != bundle_id:
        raise BundleInvalid("bundle_id does not match its directory")
    family = manifest.get("firmware_family")
    if family not in APP_PROJECTS:
        raise BundleInvalid("the firmware family is unknown")
    label = _manifest_text(manifest, "label", 80)
    version = _manifest_text(manifest, "version", 40)
    if semver_key(version)[0] < 0:
        raise BundleInvalid("version is not a semantic version")
    release_state = manifest.get("release_state")
    if release_state not in BUNDLE_RELEASE_STATES:
        raise BundleInvalid("release_state is not offered by the installer")
    recommended = manifest.get("recommended", False)
    if type(recommended) is not bool:
        raise BundleInvalid("recommended is not true or false")
    notes = _manifest_text(manifest, "notes", 2000, required=False)
    if not GIT_SHA_PATTERN.fullmatch(str(manifest.get("git_sha", ""))):
        raise BundleInvalid("git_sha is not a full commit")
    try:
        created_at = datetime.fromisoformat(str(manifest.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError:
        raise BundleInvalid("created_at is not a date") from None
    if created_at.tzinfo is None:
        raise BundleInvalid("created_at has no time zone")
    if manifest.get("partition_layout") != BUNDLE_LAYOUT:
        raise BundleInvalid("the partition layout is not zone-lite-ota-v1")
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        raise BundleInvalid("the manifest lists no images")
    roles: set[str] = set()
    files: dict[str, tuple[int, str]] = {}
    for image in images:
        if not isinstance(image, dict):
            raise BundleInvalid("an image entry is invalid")
        role, name = image.get("role"), image.get("name")
        offset, size, digest = image.get("offset"), image.get("size"), image.get("sha256")
        if not isinstance(role, str) or not ROLE_PATTERN.fullmatch(role) or role in roles:
            raise BundleInvalid("an image role is invalid or repeated")
        roles.add(role)
        if (not isinstance(name, str) or not NAME_PATTERN.fullmatch(name)
                or name in {"manifest.json", "manifest.sig"}):
            raise BundleInvalid("an image name is not allowed")
        if (type(offset) is not int or type(size) is not int or offset < 0 or size < 0
                or offset + size > FLASH_BYTES):
            raise BundleInvalid(f"{name} does not fit the 16 MB flash")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise BundleInvalid(f"{name} has no SHA-256")
        if name in files:
            # factory and ota_0 share the production image file.
            if files[name] != (size, digest):
                raise BundleInvalid(f"{name} is listed with two sizes or digests")
            continue
        path = _contained_file(directory, name)
        if path.stat().st_size != size or not hmac.compare_digest(_sha256_file(path), digest):
            raise BundleInvalid(f"{name} does not match the signed manifest")
        files[name] = (size, digest)
    factory = next((image for image in images if image.get("role") == "factory"), None)
    if factory is None:
        raise BundleInvalid("the bundle has no factory image")
    project = factory.get("app_project")
    if project is not None and project != APP_PROJECTS[family]:
        raise BundleInvalid("the factory image belongs to the other family")
    return InstallerBundle(
        bundle_id=bundle_id,
        family=family,
        label=label,
        version=version,
        release_state=release_state,
        recommended=recommended,
        notes=notes,
        created_at=created_at,
        manifest=manifest,
        signature="".join(signature_text.split()),
        manifest_sha256=hashlib.sha256(canonical).hexdigest(),
        files=files,
        directory=directory,
    )


class BundleStore:
    """Installer bundles under settings.installer_bundle_store_path.

    A bundle is verified in full (signature, manifest and every image hash) when
    it is first seen and again whenever a file in its directory changes, so the
    catalogue never re-hashes megabytes per request. A bad bundle is logged once
    and skipped; it never fails the other bundles.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple, InstallerBundle | None]] = {}
        self._unreadable: set[str] = set()  # entries already logged as unreadable

    @staticmethod
    def _root() -> Path | None:
        try:
            root = Path(settings.installer_bundle_store_path).resolve()
        except (OSError, RuntimeError):
            return None
        return root if root.is_dir() else None

    @staticmethod
    def _fingerprint(root: Path, name: str) -> tuple:
        directory = (root / name).resolve()
        if directory.parent != root or not directory.is_dir():
            raise BundleInvalid("the bundle is not a directory inside the store")
        entries = []
        for entry in sorted(directory.iterdir()):
            status = entry.stat()
            entries.append((entry.name, entry.is_symlink(), status.st_size, status.st_mtime_ns))
        key = hashlib.sha256((settings.firmware_signing_public_key_pem_b64 or "").encode()).hexdigest()
        return (str(directory), tuple(entries), key)

    def _load_locked(self, root: Path, name: str) -> InstallerBundle | None:
        try:
            fingerprint = self._fingerprint(root, name)
        except BundleInvalid:
            self._cache.pop(name, None)
            return None
        except (OSError, RuntimeError):
            # Python 3.12's Path.resolve raises RuntimeError on a symlink loop.
            self._cache.pop(name, None)
            if name not in self._unreadable:
                self._unreadable.add(name)
                logger.warning("Installer bundle %s skipped: its directory cannot be read", name)
            return None
        self._unreadable.discard(name)
        cached = self._cache.get(name)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        try:
            bundle = _verify_bundle(Path(fingerprint[0]), name)
        except BundleInvalid as error:
            logger.warning("Installer bundle %s skipped: %s", name, error)
            bundle = None
        except Exception as error:  # one unreadable bundle never fails the catalogue
            logger.warning("Installer bundle %s skipped: %s", name, type(error).__name__)
            bundle = None
        self._cache[name] = (fingerprint, bundle)
        return bundle

    def bundles(self) -> list[InstallerBundle]:
        root = self._root()
        if root is None:
            return []
        with self._lock:
            try:
                names = sorted(entry.name for entry in root.iterdir())
            except OSError:
                logger.warning("Installer bundle store is unreadable")
                return []
            names = [name for name in names if NAME_PATTERN.fullmatch(name)]
            for stale in set(self._cache) - set(names):
                del self._cache[stale]
            self._unreadable &= set(names)
            found = [self._load_locked(root, name) for name in names]
        return [bundle for bundle in found if bundle is not None]

    def get(self, bundle_id: str) -> InstallerBundle | None:
        if not isinstance(bundle_id, str) or not NAME_PATTERN.fullmatch(bundle_id):
            return None
        root = self._root()
        if root is None:
            return None
        with self._lock:
            return self._load_locked(root, bundle_id)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._unreadable.clear()


bundle_store = BundleStore()


def _recommended_ids(bundles: list[InstallerBundle]) -> set[str]:
    newest: dict[str, InstallerBundle] = {}
    for bundle in bundles:
        current = newest.get(bundle.family)
        if bundle.recommended and (
            current is None
            or (bundle.created_at, bundle.bundle_id) > (current.created_at, current.bundle_id)
        ):
            newest[bundle.family] = bundle
    return {bundle.bundle_id for bundle in newest.values()}


APP_IMAGE_ROLES = frozenset({"factory", "ota_0", "ota_1"})


def _revoked_bundle_ids(db: Session, bundles: list[InstallerBundle]) -> set[str]:
    """Bundles carrying an application image ADD has revoked since publishing.

    The publisher signs only releases ADD lists as AVAILABLE, but a release can
    be revoked afterwards. Images match ADD's releases by exact SHA-256, so an
    image ADD never published (a Hikvision build) is never affected.
    """
    holders: dict[str, set[str]] = {}
    for bundle in bundles:
        for image in bundle.manifest["images"]:
            if image["role"] in APP_IMAGE_ROLES:
                holders.setdefault(image["sha256"], set()).add(bundle.bundle_id)
    if not holders:
        return set()
    revoked = db.scalars(
        select(FirmwareRelease.image_sha256).where(
            FirmwareRelease.image_sha256.in_(sorted(holders)),
            or_(FirmwareRelease.state == "REVOKED", FirmwareRelease.revoked_at.is_not(None)),
        )
    )
    return {bundle_id for digest in revoked for bundle_id in holders[digest]}


def _offered_bundles(db: Session) -> list[InstallerBundle]:
    bundles = bundle_store.bundles()
    revoked = _revoked_bundle_ids(db, bundles)
    return [bundle for bundle in bundles if bundle.bundle_id not in revoked]


def _offered_bundle(db: Session, bundle_id: str) -> InstallerBundle | None:
    bundle = bundle_store.get(bundle_id)
    if bundle is None or _revoked_bundle_ids(db, [bundle]):
        return None
    return bundle


def _bundle_or_404(db: Session, bundle_id: str) -> InstallerBundle:
    bundle = _offered_bundle(db, bundle_id)
    if bundle is None:
        raise InstallerError(
            404, "BUNDLE_NOT_FOUND", "This firmware is not available on the head office server."
        )
    return bundle


# ------------------------------------------------------------ provisioner


def _provisioner_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(settings.installer_provisioner_timeout_seconds, connect=5.0)
    )


def _provisioner_url(path: str) -> str:
    return f"{settings.provisioning_worker_url.rstrip('/')}{path}"


def _provisioner_ready() -> bool:
    if not settings.provisioning_internal_token:
        return False
    try:
        with _provisioner_client() as client:
            answer = client.get(_provisioner_url("/health/ready"), timeout=2.0)
    except httpx.HTTPError:
        return False
    return answer.status_code == 200


# ---------------------------------------------------------- configuration


def _text_limit(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) > limit or "\x00" in value:
        raise ValueError("too long or contains NUL")
    return value


class InstallerConfiguration(BaseModel):
    """One board's settings: the rules of ProvisioningConfiguration and the
    provisioner's validate_request. The provisioner checks them again."""

    model_config = ConfigDict(extra="ignore")

    firmware_family: Literal["zkt", "hikvision"]
    zone_id: StrictStr
    zone_name: StrictStr
    device_id: StrictStr
    wifi_ssid: StrictStr
    wifi_password: StrictStr = Field(repr=False)
    zkt_port: StrictInt = 4370
    zkt_comm_key: StrictInt | None = Field(default=None, repr=False)
    zkt_preferred_ip: StrictStr | None = None
    hik_host: StrictStr = ""
    hik_port: StrictInt | None = None
    hik_transport: Literal["https", "http_digest"] | None = None
    hik_username: StrictStr = ""
    hik_password: StrictStr = Field(default="", repr=False)
    hik_expected_serial: StrictStr = ""
    hik_profile: StrictStr = ""
    hik_source_epoch: StrictStr = ""
    hik_ca_pem: StrictStr = Field(default="", repr=False)

    @field_validator("zone_id")
    @classmethod
    def validate_zone_id(cls, value: str) -> str:
        if not 1 <= len(value) <= 64 or not ID_PATTERN.fullmatch(value):
            raise ValueError("invalid zone ID")
        return value

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not 1 <= len(value) <= 31 or not ID_PATTERN.fullmatch(value):
            raise ValueError("invalid device ID")
        return value

    @field_validator("zone_name")
    @classmethod
    def validate_zone_name(cls, value: str) -> str:
        if (value.strip() != value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or not 1 <= len(value.encode("utf-8")) <= 120):
            raise ValueError("invalid zone name")
        return value

    @field_validator("wifi_ssid")
    @classmethod
    def validate_ssid(cls, value: str) -> str:
        if not 1 <= len(value.encode("utf-8")) <= 32 or "\x00" in value:
            raise ValueError("invalid Wi-Fi name")
        return value

    @field_validator("wifi_password")
    @classmethod
    def validate_wifi_password(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if "\x00" in value or not (HEX_PSK_PATTERN.fullmatch(value) or 8 <= size <= 63):
            raise ValueError("invalid Wi-Fi password")
        return value

    @field_validator("zkt_port", "hik_port")
    @classmethod
    def validate_port(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65_535:
            raise ValueError("invalid port")
        return value

    @field_validator("zkt_comm_key")
    @classmethod
    def validate_comm_key(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= 0xFFFF_FFFF:
            raise ValueError("invalid comm key")
        return value

    @field_validator("zkt_preferred_ip")
    @classmethod
    def validate_preferred_ip(cls, value: str | None) -> str | None:
        if value is None or value == "0.0.0.0":
            return value
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            raise ValueError("invalid preferred IP") from None
        if not any(address in network for network in RFC1918_NETWORKS):
            raise ValueError("preferred IP is not RFC1918")
        return str(address)

    @field_validator("hik_host")
    @classmethod
    def validate_hik_host(cls, value: str) -> str:
        return _text_limit(value, 63)

    @field_validator("hik_username")
    @classmethod
    def validate_hik_username(cls, value: str) -> str:
        return _text_limit(value, 63)

    @field_validator("hik_password")
    @classmethod
    def validate_hik_password(cls, value: str) -> str:
        return _text_limit(value, 127)

    @field_validator("hik_expected_serial")
    @classmethod
    def validate_hik_serial(cls, value: str) -> str:
        return _text_limit(value, 119)

    @field_validator("hik_profile")
    @classmethod
    def validate_hik_profile(cls, value: str) -> str:
        return _text_limit(value, 79)

    @field_validator("hik_source_epoch")
    @classmethod
    def validate_hik_epoch(cls, value: str) -> str:
        return _text_limit(value, 64)

    @field_validator("hik_ca_pem")
    @classmethod
    def validate_hik_ca(cls, value: str) -> str:
        # The NVS generator (ESP-IDF 5.5.3) fits one string and its NUL on one
        # page: at most 3967 bytes. A longer CA could never be built.
        return _text_limit(value, 3967)

    def provisioner_payload(self) -> dict[str, Any]:
        keys = (
            "firmware_family", "zone_id", "zone_name", "device_id", "wifi_ssid",
            "wifi_password", "zkt_port", "zkt_comm_key", "zkt_preferred_ip",
        )
        if self.firmware_family == "hikvision":
            keys += HIK_FIELDS
        return {key: getattr(self, key) for key in keys}


def validated_configuration(values: dict[str, Any]) -> InstallerConfiguration:
    """Field rules, then the family rules, each failure naming one field."""
    try:
        config = InstallerConfiguration.model_validate(values)
    except ValidationError as error:
        first = next(
            (item["loc"][0] for item in error.errors() if item.get("loc") and isinstance(item["loc"][0], str)),
            "configuration",
        )
        raise invalid_field(first) from None
    if config.firmware_family == "zkt":
        # The app always sends the comm key and the preferred IP explicitly.
        if config.zkt_comm_key is None:
            raise invalid_field("zkt_comm_key")
        if config.zkt_preferred_ip is None:
            raise invalid_field("zkt_preferred_ip")
        for key in HIK_SETTINGS_FOR_ZKT:
            if getattr(config, key):
                raise invalid_field(key, "Hikvision settings are only for a Hikvision machine.")
        return config
    if config.zkt_comm_key not in (None, 0):
        raise invalid_field("zkt_comm_key", "A Hikvision machine does not use a comm key.")
    try:
        host = ipaddress.IPv4Address(config.hik_host)
    except ipaddress.AddressValueError:
        raise invalid_field("hik_host") from None
    if not any(host in network for network in RFC1918_NETWORKS):
        raise invalid_field("hik_host")
    for key, _limit in HIK_TEXT_LIMITS:
        if not getattr(config, key):
            raise invalid_field(key)
    transport = config.hik_transport or "https"
    return config.model_copy(update={
        "zkt_comm_key": 0,
        "zkt_preferred_ip": config.zkt_preferred_ip or "0.0.0.0",
        "hik_host": str(host),
        "hik_transport": transport,
        "hik_port": config.hik_port if config.hik_port is not None
        else (443 if transport == "https" else 80),
    })


def _mac_validator(value: str) -> str:
    return normalize_installer_mac(value)


class InstallerPackageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password: SecretStr = Field(min_length=1, max_length=512)
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    mac: str = Field(max_length=32)
    mode: Literal["new_board", "resume", "change_settings"]
    # Required for new_board and resume (installer_package). A settings-only
    # change writes no firmware, so it may send none and any it sends is unused.
    bundle_id: str | None = Field(default=None, pattern=NAME_PATTERN.pattern)
    app_version: str = Field(min_length=5, max_length=40)
    recipient_public_key: str = Field(min_length=40, max_length=64)
    configuration: dict[str, Any]

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        return _mac_validator(value)

    @field_validator("bundle_id", mode="before")
    @classmethod
    def validate_bundle_id(cls, value: Any) -> Any:
        # An app with no firmware to name may send an empty string.
        return None if value == "" else value

    @field_validator("app_version")
    @classmethod
    def validate_app_version(cls, value: str) -> str:
        if semver_key(value)[0] < 0:
            raise ValueError("invalid app version")
        return value

    @field_validator("recipient_public_key")
    @classmethod
    def validate_recipient_key(cls, value: str) -> str:
        # A raw 32-byte X25519 key in standard base64, as provisioning_envelope reads it.
        try:
            raw = base64.b64decode(value, validate=True)
            if len(raw) != 32:
                raise ValueError("wrong key size")
            X25519PublicKey.from_public_bytes(raw)
        except (ValueError, binascii.Error):
            raise ValueError("invalid recipient key") from None
        return value


class InstallerEventRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    mac: str = Field(max_length=32)
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        return _mac_validator(value)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if not EVENT_KIND_PATTERN.fullmatch(value) or value.upper() in RESERVED_EVENT_KINDS:
            raise ValueError("invalid event kind")
        return value.upper()

    @field_validator("detail")
    @classmethod
    def validate_detail(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str).encode("utf-8")) > EVENT_DETAIL_MAX_BYTES:
            raise ValueError("event detail is too large")
        return value


def _scrub_event_detail(value: Any) -> Any:
    """sanitize_event_details, which also hides the installer's extra secret-looking
    keys. It judges each key whole, before cutting it to 80 characters (review SV3)."""
    return sanitize_event_details(value, extra_secret_parts=EVENT_SECRET_PARTS)


# ------------------------------------------------------------- device view


def _pakistan_time(value: datetime, now: datetime) -> str:
    """The status page's wording: "10:42 AM", "yesterday, 10:42 AM", "24 Oct, 10:42 AM"."""
    local = ensure_utc(value).astimezone(PKT)
    today = ensure_utc(now).astimezone(PKT).date()
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    if local.date() == today:
        return clock
    if local.date() == today - timedelta(days=1):
        return f"yesterday, {clock}"
    day = f"{local.day} {MONTHS[local.month - 1]}"
    if local.year != today.year:
        day = f"{day} {local.year}"
    return f"{day}, {clock}"


def _public_identity(db: Session, connector: Connector) -> tuple[str, str]:
    """The /status page's name and Life/Health group (public_status.build_snapshot)."""
    name = public_status.device_name(connector.display_name or connector.zone_id)
    group = dict(public_status.ZONES)[name.zone]
    if not name.unit:
        return name.location, group
    if name.number is not None:
        # A numbered unit is named only where its place has several numbered
        # devices among the listed (active, non-spare) connectors.
        numbered, listed = 0, False
        for row in db.execute(
            select(Connector.id, Connector.display_name, Connector.zone_id).where(
                Connector.active.is_(True), Connector.is_spare.is_(False)
            )
        ):
            other = public_status.device_name(row.display_name or row.zone_id)
            if other.number is not None and (other.zone, other.location) == (name.zone, name.location):
                numbered += 1
                listed = listed or row.id == connector.id
        if numbered + (0 if listed else 1) <= 1:
            return name.location, group
    return f"{name.location} · {name.unit}", group


def _hik_source_epoch(db: Session, connector: Connector) -> str | None:
    """ADD's policy epoch for the board, else the epoch its records arrive with."""
    if (connector.firmware_family or "zkt") != "hikvision":
        return None
    policy = db.get(HikvisionPolicy, connector.id)
    if policy is not None and policy.source_epoch:
        return policy.source_epoch
    return db.scalar(
        select(HikvisionEvidence.source_epoch)
        .where(HikvisionEvidence.connector_id == connector.id, HikvisionEvidence.source_epoch != "")
        .order_by(HikvisionEvidence.id.desc())
        .limit(1)
    )


def _ever_online(connector: Connector) -> bool:
    # Connector.last_seen_at is written only when the board itself opens its
    # authenticated device stream or sends on it (set_stream_connected and the
    # stream handler). Onboarding, ADD's own jobs and this API never set it. A
    # board that only reached the onboarding URL (last_onboarded_at) has not run
    # with a working site network, so it cannot hold punches yet.
    return connector.last_seen_at is not None


def _prepared_by_installer(db: Session, mac: str) -> bool:
    """This app issued the board a new_board package, so the key it burned is the
    one the fleet root gives that MAC. A resume package proves nothing: it burns
    no key, and the board may have been locked before this app with an older
    root (head office's later setup with that root leaves no record in ADD)."""
    issued = db.scalars(
        select(AuditEvent.after).where(
            AuditEvent.action == "INSTALLER_PACKAGE_ISSUED",
            AuditEvent.target_type == "esp",
            AuditEvent.target_id == mac,
            AuditEvent.outcome == "SUCCESS",
        )
    )
    return any(isinstance(after, dict) and after.get("mode") in KEY_PACKAGE_MODES
               for after in issued)


def settings_change_refusal(
    db: Session, mac: str, connector: Connector | None, mode: str = "change_settings"
) -> str | None:
    """Why ADD builds no resume or change_settings package for the board, or None.

    The provisioner takes every board's settings key from the fleet root. A
    board set up before this app may hold an HMAC key from an older root: new
    settings would leave it unable to start, with its unsent punches already
    erased with the storage, and only head office could recover it (review
    B1/F1). Nobody can read a locked key, so ADD goes by its own records. A
    board ADD does not know, or has never seen online, has no punches to lose.
    One that has been online gets new settings only when this app set it up as
    a new board, and is never resumed: a board whose setup stopped has never
    started.
    """
    if connector is None or not _ever_online(connector):
        return None
    if mode == "change_settings" and _prepared_by_installer(db, mac):
        return None
    return SETTINGS_CHANGE_REFUSED


def device_record(db: Session, connector: Connector) -> dict[str, Any]:
    now = utc_now()
    ever_online = _ever_online(connector)
    health = evaluate_health(db, connector, now=now)
    status, since = public_status.observed_status(connector, health, now)
    stored = db.scalar(
        select(PublicDeviceStatus).where(PublicDeviceStatus.connector_id == connector.id)
    )
    if stored is not None and stored.status == status:
        since = stored.since  # the date the /status page shows
    online = bool(connector.active) and ever_online and status == public_status.ONLINE
    if not connector.active:
        status_text = "Not in use: head office has switched this board off."
    elif not ever_online:
        status_text = "Set up, but it has not connected to head office yet."
    else:
        when = _pakistan_time(since, now) if since else None
        if status == public_status.ONLINE:
            status_text = f"Online since {when}" if when else "Online"
        else:
            status_text = f"Offline since {when}" if when else "Offline"
            heading, explanation = public_status.REASONS.get(
                public_status.offline_reason(health), (None, None)
            )
            if heading:
                status_text = f"{status_text}: {heading}. {explanation}"
    if connector.is_spare:
        status_text = f"{status_text} It is marked as a spare board."
    public_name, group = _public_identity(db, connector)
    refusal = settings_change_refusal(db, connector.hardware_id, connector)
    return {
        "known": True,
        "mac": connector.hardware_id,
        "connector_id": connector.connector_id,
        "zone_id": connector.zone_id,
        "zone_name": connector.zone_name,
        "device_id": connector.device_id,
        "family": connector.firmware_family or "zkt",
        "firmware_version": connector.firmware_version,
        "online": online,
        "last_seen_at": iso_utc(connector.last_seen_at) if connector.last_seen_at else None,
        "ever_online": ever_online,
        "public_name": public_name,
        "group": group,
        "status_text": status_text,
        "hik_source_epoch": _hik_source_epoch(db, connector),
        "settings_change_allowed": refusal is None,
        "settings_change_reason": refusal,
    }


def unknown_device_record(mac: str) -> dict[str, Any]:
    return {
        "known": False,
        "mac": mac,
        "connector_id": None,
        "zone_id": None,
        "zone_name": None,
        "device_id": None,
        "family": None,
        "firmware_version": None,
        "online": None,
        "last_seen_at": None,
        "ever_online": False,
        "public_name": None,
        "group": None,
        "status_text": None,
        "hik_source_epoch": None,
        "settings_change_allowed": True,
        "settings_change_reason": None,
    }


def _natural_key(value: str) -> list:
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.lower())
            for part in re.split(r"(\d+)", value) if part]


def free_device_id(db: Session, zone_id: str, mac: str) -> str:
    """The smallest positive whole number no other active board uses in the zone."""
    used = set(db.scalars(
        select(Connector.device_id).where(
            Connector.zone_id == zone_id,
            Connector.active.is_(True),
            Connector.hardware_id != mac,
        )
    ))
    number = 1
    while str(number) in used:
        number += 1
    return str(number)


# ------------------------------------------------------------------ routes


@router.get("/capabilities")
def installer_capabilities(auth: tuple[Session, AdminContext] = Depends(require_admin)):
    db, _context = auth
    enabled = installer_enabled()
    return {
        "enabled": enabled,
        "provisioner_ready": enabled and _provisioner_ready(),
        "min_app_version": settings.installer_min_app_version,
        "server_time": iso_utc(utc_now()),
        "bundle_count": len(_offered_bundles(db)) if enabled else 0,
    }


@router.get("/bundles")
def installer_bundles(
    family: Literal["zkt", "hikvision"] | None = None,
    auth: tuple[Session, AdminContext] = Depends(installer_admin),
):
    db, _context = auth
    bundles = _offered_bundles(db)
    recommended = _recommended_ids(bundles)
    rows = sorted(
        (bundle for bundle in bundles if family is None or bundle.family == family),
        key=lambda bundle: (
            bundle.family, bundle.bundle_id not in recommended,
            -bundle.created_at.timestamp(), bundle.bundle_id,
        ),
    )
    return {
        "bundles": [
            {
                "bundle_id": bundle.bundle_id,
                "family": bundle.family,
                "label": bundle.label,
                "version": bundle.version,
                "recommended": bundle.bundle_id in recommended,
                "release_state": bundle.release_state,
                "notes": bundle.notes,
                "download_bytes": bundle.download_bytes,
                "manifest_sha256": bundle.manifest_sha256,
            }
            for bundle in rows
        ]
    }


@router.get("/bundles/{bundle_id}/manifest")
def installer_bundle_manifest(
    bundle_id: str, auth: tuple[Session, AdminContext] = Depends(installer_admin)
):
    db, _context = auth
    bundle = _bundle_or_404(db, bundle_id)
    return {"manifest": bundle.manifest, "signature": bundle.signature}


@router.get("/bundles/{bundle_id}/files/{name}")
def installer_bundle_file(
    bundle_id: str, name: str, auth: tuple[Session, AdminContext] = Depends(installer_admin)
):
    db, _context = auth
    bundle = _bundle_or_404(db, bundle_id)
    if name not in bundle.files:
        raise InstallerError(404, "FILE_NOT_FOUND", "This firmware file is not in the bundle.")
    # The request's session closes only after the whole file is sent. Commit
    # the admin session touch now, as the OTA download does, so a slow link
    # never holds a transaction past PostgreSQL's idle-in-transaction limit.
    db.commit()
    # The name is in the signed manifest and was verified as a regular file
    # directly inside the bundle directory; the store has not changed since.
    return FileResponse(
        bundle.directory / name,
        media_type="application/octet-stream",
        filename=name,
        headers={"Cache-Control": "private, no-store", "X-Content-SHA256": bundle.files[name][1]},
    )


@router.get("/apps")
def installer_apps(auth: tuple[Session, AdminContext] = Depends(installer_admin)):
    """The newest published app for the dashboard's download card."""
    release = app_store.latest()
    latest = None
    if release is not None:
        latest = {
            "version": release.version,
            "git_sha": release.git_sha,
            "published_at": release.published_at,
            "files": [
                {
                    "platform": item.platform,
                    "arch": item.arch,
                    "kind": item.kind,
                    "filename": item.filename,
                    "size": item.size,
                    "sha256": item.sha256,
                    "download_path": f"{PREFIX}/apps/{release.version}/{item.filename}",
                }
                for item in release.files
            ],
        }
    return {"latest": latest, "min_app_version": settings.installer_min_app_version}


@router.get("/apps/{version}/{filename}")
def installer_app_file(
    request: Request,
    version: str,
    filename: str,
    auth: tuple[Session, AdminContext] = Depends(installer_admin),
):
    db, context = auth
    release = app_store.get(version)
    item = release.file(filename) if release is not None else None
    if item is None:
        raise InstallerError(
            404, "FILE_NOT_FOUND", "This copy of the app is not on the head office server."
        )
    append_audit(
        db,
        actor=context.username,
        action="INSTALLER_APP_DOWNLOADED",
        target_type="installer_app",
        target_id=release.version,
        outcome="SUCCESS",
        ip_address=client_ip(request),
        after={
            "version": release.version,
            "platform": item.platform,
            "arch": item.arch,
            "filename": item.filename,
            "size": item.size,
            "sha256": item.sha256,
        },
    )
    # The download is audited durably before it starts; the request's session
    # closes only after the whole file is sent, so nothing stays open meanwhile.
    db.commit()
    # The file was verified (size and SHA-256) as a regular file directly in
    # its version directory, and has not changed since.
    return FileResponse(
        item.path,
        media_type=item.media_type,
        filename=item.filename,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-SHA256": item.sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/devices/{mac}")
def installer_device(mac: str, auth: tuple[Session, AdminContext] = Depends(installer_admin)):
    db, _context = auth
    try:
        normalized = normalize_installer_mac(mac)
    except ValueError:
        raise invalid_field("mac") from None
    connector = db.scalar(select(Connector).where(Connector.hardware_id == normalized))
    if connector is None:
        return unknown_device_record(normalized)
    return device_record(db, connector)


@router.get("/zones")
def installer_zones(auth: tuple[Session, AdminContext] = Depends(installer_admin)):
    db, _context = auth
    zones: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        select(
            Connector.id, Connector.zone_id, Connector.zone_name, Connector.device_id,
            Connector.firmware_family, Connector.last_onboarded_at,
        ).where(Connector.active.is_(True))
    ).all()
    for row in rows:
        zone = zones.setdefault(row.zone_id, {"names": [], "device_ids": set(), "families": set()})
        onboarded = ensure_utc(row.last_onboarded_at) if row.last_onboarded_at else datetime.min.replace(
            tzinfo=timezone.utc
        )
        zone["names"].append((onboarded, row.id, row.zone_name))
        zone["device_ids"].add(row.device_id)
        zone["families"].add(row.firmware_family or "zkt")
    return {
        "zones": [
            {
                "zone_id": zone_id,
                # The name the zone's most recently onboarded board reported.
                "zone_name": max(zone["names"])[2],
                "device_ids": sorted(zone["device_ids"], key=_natural_key),
                "families": sorted(zone["families"]),
            }
            for zone_id, zone in sorted(zones.items(), key=lambda item: _natural_key(item[0]))
        ]
    }


def _checked_package(
    result: Any, body: InstallerPackageRequest, config: InstallerConfiguration
) -> dict[str, Any]:
    """The provisioner's envelopes, bound to this request (provisioning_envelope checks)."""
    manifest = result["provisioning_manifest"]
    provisioning_b64 = result["provisioning_ciphertext_b64"]
    hmac_b64 = result["hmac_ciphertext_b64"]
    if not (isinstance(manifest, dict) and isinstance(provisioning_b64, str)
            and isinstance(hmac_b64, str)):
        raise ValueError("unexpected package shape")
    aad, hmac_manifest = manifest["aad"], manifest["hmac_key"]
    expected = {
        "request_id": body.request_id,
        "target_mac": body.mac,
        "firmware_family": config.firmware_family,
        "zone_id": config.zone_id,
        "zone_device_id": config.device_id,
    }
    if any(aad.get(key) != value for key, value in expected.items()):
        raise ValueError("package is bound to another request")
    if (hmac_manifest["aad"].get("request_id") != body.request_id
            or hmac_manifest["aad"].get("target_mac") != body.mac):
        raise ValueError("key package is bound to another request")
    for encoded, digest in (
        (provisioning_b64, manifest["ciphertext_sha256"]),
        (hmac_b64, hmac_manifest["ciphertext_sha256"]),
    ):
        actual = hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
        if not hmac.compare_digest(actual, str(digest)):
            raise ValueError("ciphertext digest mismatch")
    return {
        "provisioning_manifest": manifest,
        "provisioning_ciphertext_b64": provisioning_b64,
        "hmac_ciphertext_b64": hmac_b64,
    }


def _build_package(body: InstallerPackageRequest, config: InstallerConfiguration) -> dict[str, Any]:
    token = settings.provisioning_internal_token
    if not token:
        logger.warning("Installer package refused: the provisioner token is not configured")
        raise _provisioner_unavailable()
    payload = {
        "request_id": body.request_id,
        "target_mac": body.mac,
        "recipient_public_key": body.recipient_public_key,
        "configuration": config.provisioner_payload(),
    }
    try:
        with _provisioner_client() as client:
            answer = client.post(
                _provisioner_url("/internal/v2/nvs-packages"),
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as error:
        logger.warning(
            "Installer package build unreachable request_id=%s error=%s",
            body.request_id, type(error).__name__,
        )
        raise _provisioner_unavailable() from None
    if answer.status_code == 422:
        try:
            refusal = answer.json()
        except ValueError:
            refusal = None
        if isinstance(refusal, dict) and refusal.get("code") == "INVALID_FIELD":
            field = refusal.get("field")
            field = "mac" if field == "target_mac" else field
            if isinstance(field, str) and field in FIELD_MESSAGES:
                raise invalid_field(field)
    if answer.status_code != 201:
        logger.warning(
            "Installer package build failed request_id=%s status=%s",
            body.request_id, answer.status_code,
        )
        raise _provisioner_unavailable()
    try:
        return _checked_package(answer.json(), body, config)
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
        logger.warning("Installer package build returned an unusable package request_id=%s",
                       body.request_id)
        raise _provisioner_unavailable() from None


@router.post("/packages", status_code=201)
def installer_package(
    request: Request,
    response: Response,
    body: InstallerPackageRequest,
    auth: tuple[Session, AdminContext] = Depends(installer_admin_mutation),
):
    db, context = auth
    # Counted before the password check, so the limit also bounds guessing.
    if not package_limiter.allow(context.username):
        raise _rate_limited(package_limiter.retry_after(context.username))
    require_step_up(body.password.get_secret_value(), db, context)
    if semver_key(body.app_version) < semver_key(settings.installer_min_app_version):
        raise InstallerError(
            426,
            "APP_UPDATE_REQUIRED",
            "This copy of the app is too old. Install the latest app from head office.",
        )
    config = validated_configuration(body.configuration)
    # Only new_board and resume write firmware. A settings-only change needs no
    # published bundle, and the family rules below still apply to it.
    bundle = None
    if body.mode != "change_settings":
        if body.bundle_id is None:
            raise invalid_field("bundle_id")
        bundle = _offered_bundle(db, body.bundle_id)
        if bundle is None:
            raise InstallerError(
                409,
                "BUNDLE_UNAVAILABLE",
                "This firmware is no longer available. Go back and choose the firmware again.",
                field="bundle_id",
            )
        if bundle.family != config.firmware_family:
            raise InstallerError(
                409,
                "BUNDLE_FAMILY_MISMATCH",
                "This firmware is for the other kind of attendance machine. "
                "Choose the firmware again.",
                field="bundle_id",
            )
    # The same lookup as /device/v2/onboard: a board ADD knows keeps its family,
    # or the firmware would abort and onboarding would refuse it.
    existing = db.scalar(select(Connector).where(Connector.hardware_id == body.mac))
    if existing is not None and (existing.firmware_family or "zkt") != config.firmware_family:
        raise InstallerError(
            409,
            "FAMILY_MISMATCH",
            "Head office already knows this board for the other kind of attendance machine, "
            "so it cannot be set up for this one. Use another board.",
            field="firmware_family",
            known_family=existing.firmware_family or "zkt",
        )
    # Resume and change_settings write fleet-root settings to a key that is
    # already locked; they must not reach a board that may hold another root's.
    if body.mode in ("resume", "change_settings"):
        refusal = settings_change_refusal(db, body.mac, existing, body.mode)
        if refusal:
            raise InstallerError(409, "SETTINGS_CHANGE_NOT_SUPPORTED", refusal)
    try:
        ensure_assignment_available(
            db, hardware_mac=body.mac, zone_id=config.zone_id, device_id=config.device_id
        )
    except ValueError:
        raise InstallerError(
            409,
            "DEVICE_ID_IN_USE",
            "Another board already uses this device number in this zone. Choose another device number.",
            field="device_id",
            suggested_device_id=free_device_id(db, config.zone_id, body.mac),
        ) from None
    # Nothing is reserved: end the transaction (and its advisory locks) before the
    # build, which can outlast the database's idle-transaction limit.
    db.commit()
    package = _build_package(body, config)
    append_audit(
        db,
        actor=context.username,
        action="INSTALLER_PACKAGE_ISSUED",
        target_type="esp",
        target_id=body.mac,
        outcome="SUCCESS",
        ip_address=client_ip(request),
        request_id=body.request_id,
        after={
            "mode": body.mode,
            "bundle_id": bundle.bundle_id if bundle is not None else None,
            "zone_id": config.zone_id,
            "device_id": config.device_id,
            "firmware_family": config.firmware_family,
            "app_version": body.app_version,
        },
    )
    # The issue is audited durably before any envelope leaves the server.
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"request_id": body.request_id, **package}


@router.post("/events", status_code=204)
def installer_event(
    request: Request,
    body: InstallerEventRequest,
    auth: tuple[Session, AdminContext] = Depends(installer_admin_mutation),
):
    db, context = auth
    if not event_limiter.allow(context.username):
        raise _rate_limited(event_limiter.retry_after(context.username))
    append_audit(
        db,
        actor=context.username,
        action=f"INSTALLER_{body.kind}",
        target_type="esp",
        target_id=body.mac,
        outcome="REPORTED",
        ip_address=client_ip(request),
        request_id=body.request_id,
        after=_scrub_event_detail(body.detail),
    )
    db.commit()
    return Response(status_code=204)
