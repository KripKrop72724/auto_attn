"""Read-only ISAPI qualification. This module never certifies a terminal.

POST is used only for the two documented search resources. No terminal configuration,
person mutation, enrollment, restart, or firmware endpoint is reachable through this client.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import ssl
import stat
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx


MAX_BODY = 256 * 1024
MAX_HEADERS = 8192
MAX_PART = 64 * 1024
SEARCH_PATHS = {
    "users": "/ISAPI/AccessControl/UserInfo/Search?format=json",
    "events": "/ISAPI/AccessControl/AcsEvent?format=json",
}
READ_PATHS = (
    "/ISAPI/System/deviceInfo",
    "/ISAPI/System/time",
    "/ISAPI/System/capabilities",
    "/ISAPI/AccessControl/capabilities",
    "/ISAPI/AccessControl/UserInfo/capabilities?format=json",
    "/ISAPI/AccessControl/UserInfo/Count?format=json",
    "/ISAPI/AccessControl/AcsEvent/capabilities?format=json",
    "/ISAPI/AccessControl/AcsEventTotalNum/capabilities?format=json",
    "/ISAPI/Event/notification/subscribeEventCap",
)
STREAM_PATH = "/ISAPI/Event/notification/alertStream"
SAFE_VALUES = {
    "model", "firmwareVersion", "firmwareReleasedDate", "major", "minor",
    "majorEventType", "subEventType", "eventType", "eventState", "attendanceStatus",
    "currentVerifyMode", "userType", "statusCode", "statusString", "subStatusCode",
    "responseStatusStrg", "numOfMatches", "totalMatches", "userNumber", "serialNo",
    "frontSerialNo", "maxResults", "searchResultPosition", "timeMode", "timeZone",
}
IDENTIFIERS = {"employeeNo", "employeeNoString", "cardNo", "serialNumber", "deviceID"}
REQUIRED_HARDWARE_CHECKS = (
    "live_history_identity_parity", "final_authentication_event_mapping",
    "leading_zero_employee_numbers", "bounded_history_coverage",
    "history_resume_during_new_punches", "profile_create_readback",
    "profile_edit_preserves_credentials", "profile_delete_completion",
    "profile_delete_preserves_history", "power_loss_recovery",
    "independent_add_ords_delivery", "signed_family_ota_rollback", "soak_72_hours",
)


class ProbeError(Exception):
    """A deliberately non-sensitive diagnostic code, never a raw device message."""


@dataclass(frozen=True)
class ProbeConfig:
    base_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    expected_serial: str = field(repr=False)
    allow_http_digest: bool = False
    ca_file: str | None = None
    timeout_seconds: float = 5

    def __post_init__(self):
        if (not isinstance(self.base_url, str) or type(self.allow_http_digest) is not bool
                or type(self.timeout_seconds) not in (int, float)
                or (self.ca_file is not None and not isinstance(self.ca_file, str))):
            raise ProbeError("CONFIG_INVALID_TYPES")
        parsed = urlsplit(self.base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise ProbeError("CONFIG_REQUIRES_LAN_IP") from None
        if (not address.is_private or address.is_loopback or address.is_unspecified
                or address.is_multicast or address.is_link_local):
            raise ProbeError("CONFIG_REQUIRES_LAN_IP")
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ProbeError("CONFIG_INVALID_ORIGIN")
        if parsed.scheme == "http" and not self.allow_http_digest:
            raise ProbeError("HTTP_DIGEST_REQUIRES_EXPLICIT_OPT_IN")
        if not all(isinstance(value, str) and value for value in (
            self.username, self.password, self.expected_serial,
        )):
            raise ProbeError("CONFIG_MISSING_CREDENTIAL_OR_SERIAL")
        if not 1 <= self.timeout_seconds <= 30:
            raise ProbeError("CONFIG_TIMEOUT_OUT_OF_RANGE")

    @classmethod
    def load(cls, path: Path) -> ProbeConfig:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024:
                raise ProbeError("CONFIG_NOT_SMALL_REGULAR_FILE")
            if os.name == "posix" and (
                metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ProbeError("CONFIG_REQUIRES_OWNER_ONLY_PERMISSIONS")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ProbeError("CONFIG_INVALID")
            try:
                return cls(**value)
            except TypeError:
                raise ProbeError("CONFIG_INVALID_FIELDS") from None
        finally:
            os.close(fd)


class Sanitizer:
    """One report's aliases are stable, but cannot be linked between reports."""

    def __init__(self, sensitive_values: tuple[str, ...] = ()):
        self.key = secrets.token_bytes(32)
        self.sensitive_values = sensitive_values

    def alias(self, value: Any) -> str:
        return "anon-" + hmac.new(
            self.key, str(value).encode(), hashlib.sha256,
        ).hexdigest()[:24]

    def clean(self, value: Any, field: str = "", depth: int = 0,
              *, capabilities: bool = False) -> Any:
        if depth > 24:
            return "[depth-limit]"
        if isinstance(value, dict):
            return {
                (self.alias(key) if self.contains_secret(str(key)) else str(key)[:100]):
                    self.clean(item, str(key), depth + 1, capabilities=capabilities)
                for key, item in list(value.items())[:200]
            }
        if isinstance(value, list):
            return [self.clean(item, field, depth + 1, capabilities=capabilities)
                    for item in value[:30]]
        if value is None:
            return None
        if field in IDENTIFIERS:
            return self.alias(value)
        if isinstance(value, str) and self.contains_secret(value):
            return self.alias(value)
        if capabilities:
            if field in {"@min", "@max", "@opt", "@size", "@minSize", "@maxSize"}:
                return value if not isinstance(value, str) else value[:2048]
            if value in ("true", "false"):
                return value == "true"
        if field in SAFE_VALUES and isinstance(value, (str, int, float, bool)):
            # No unbounded device-controlled values are exported.
            return value if not isinstance(value, str) else value[:120]
        if isinstance(value, bool):
            return value
        # Names, URLs, addresses, PINs, timestamps, passwords, and unknown fields
        # are all opaque aliases; never rely on a finite sensitive-field blacklist.
        return self.alias(value)

    def contains_secret(self, value: str) -> bool:
        return any(secret and secret in value for secret in self.sensitive_values)


def decode_body(body: bytes) -> Any:
    if len(body) > MAX_BODY:
        raise ProbeError("BODY_TOO_LARGE")
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeError:
        raise ProbeError("UNSUPPORTED_ENCODING") from None
    if decoded.lstrip().startswith(("{", "[")):
        try:
            result = json.loads(decoded)
            if not isinstance(result, dict):
                raise ProbeError("EXPECTED_JSON_OBJECT")
            return result
        except (ValueError, RecursionError):
            raise ProbeError("INVALID_JSON") from None
    if "<!DOCTYPE" in decoded.upper() or "<!ENTITY" in decoded.upper():
        raise ProbeError("XML_DECLARATION_REJECTED")
    try:
        root = ElementTree.fromstring(decoded)
    except (ElementTree.ParseError, ValueError):
        raise ProbeError("INVALID_XML") from None

    def convert(node, depth=0):
        if depth > 24:
            raise ProbeError("XML_TOO_DEEP")
        if not len(node) and not node.attrib:
            return node.text or ""
        result: dict[str, Any] = {"@" + key: value for key, value in node.attrib.items()}
        if node.text and node.text.strip():
            result["#text"] = node.text.strip()
        for child in node:
            key = child.tag.rsplit("}", 1)[-1]
            value = convert(child, depth + 1)
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result

    return {root.tag.rsplit("}", 1)[-1]: convert(root)}


class MultipartDecoder:
    """Incremental bounded multipart reader; image bodies are discarded as they arrive."""

    def __init__(self, boundary: bytes):
        if not boundary or len(boundary) > 200 or b"\r" in boundary or b"\n" in boundary:
            raise ProbeError("INVALID_MULTIPART_BOUNDARY")
        self.marker = b"--" + boundary
        self.buffer = bytearray()
        self.state = "boundary"
        self.capture = False
        self.body = bytearray()
        self.discarded_parts = 0
        self.oversized_parts = 0

    def feed(self, data: bytes) -> Iterator[bytes]:
        # Limit memory even if a caller passes a large transport chunk.
        for start in range(0, len(data), 4096):
            self.buffer.extend(data[start:start + 4096])
            yield from self._drain()

    def _append(self, data: bytes):
        if not self.capture:
            return
        if len(self.body) + len(data) > MAX_PART:
            self.capture = False
            self.body.clear()
            self.oversized_parts += 1
        else:
            self.body.extend(data)

    def _drain(self) -> Iterator[bytes]:
        while True:
            if self.state == "boundary":
                index = self.buffer.find(self.marker)
                if index < 0:
                    if len(self.buffer) > MAX_HEADERS:
                        raise ProbeError("MULTIPART_BOUNDARY_MISSING")
                    return
                end = index + len(self.marker)
                if len(self.buffer) < end + 2:
                    return
                suffix = self.buffer[end:end + 2]
                if suffix == b"--":
                    self.buffer.clear()
                    self.state = "done"
                    return
                if suffix != b"\r\n":
                    raise ProbeError("INVALID_MULTIPART_DELIMITER")
                del self.buffer[:end + 2]
                self.state = "headers"
            elif self.state == "headers":
                end = self.buffer.find(b"\r\n\r\n")
                if end < 0:
                    if len(self.buffer) > MAX_HEADERS:
                        raise ProbeError("MULTIPART_HEADERS_TOO_LARGE")
                    return
                if end > MAX_HEADERS:
                    raise ProbeError("MULTIPART_HEADERS_TOO_LARGE")
                headers = bytes(self.buffer[:end]).lower()
                self.capture = any(kind in headers for kind in (
                    b"application/json", b"application/xml", b"text/xml",
                ))
                # Observed V3.3.5 push format: JSON event_log form part with no
                # Content-Type. Images and all other untyped parts stay skipped.
                lines = headers.split(b"\r\n")
                if (not any(line.startswith(b"content-type:") for line in lines)
                        and b'content-disposition: form-data; name="event_log"' in lines):
                    self.capture = True
                if not self.capture:
                    self.discarded_parts += 1
                self.body.clear()
                del self.buffer[:end + 4]
                self.state = "body"
            elif self.state == "body":
                delimiter = b"\r\n" + self.marker
                index = self.buffer.find(delimiter)
                if index < 0:
                    safe = len(self.buffer) - len(delimiter) - 2
                    if safe > 0:
                        self._append(bytes(self.buffer[:safe]))
                        del self.buffer[:safe]
                    return
                # Wait for the suffix so boundary-like bytes inside a binary part
                # cannot be mistaken for an actual MIME delimiter.
                end = index + len(delimiter)
                if len(self.buffer) < end + 2:
                    return
                if self.buffer[end:end + 2] not in (b"\r\n", b"--"):
                    self._append(bytes(self.buffer[:index + 2]))
                    del self.buffer[:index + 2]
                    continue
                self._append(bytes(self.buffer[:index]))
                if self.capture:
                    yield bytes(self.body)
                self.body.clear()
                del self.buffer[:index + 2]
                self.state = "boundary"
            else:
                self.buffer.clear()
                return


def multipart_boundary(content_type: str) -> bytes:
    from email.message import Message

    message = Message()
    message["content-type"] = content_type
    boundary = message.get_param("boundary")
    if message.get_content_maintype() != "multipart" or not boundary:
        raise ProbeError("UNSUPPORTED_STREAM_CONTENT_TYPE")
    try:
        return str(boundary).encode("ascii")
    except UnicodeError:
        raise ProbeError("INVALID_MULTIPART_BOUNDARY") from None


def search_pages(
    fetch: Callable[[dict], dict], *, kind: str, page_size: int = 20,
    max_pages: int = 2, max_records: int = 150_000,
    serial_range: tuple[int, int] | None = None,
) -> Iterator[tuple[list[dict], dict]]:
    """Enumerate one search session. Completion here is NOT a coverage certificate."""
    if kind not in SEARCH_PATHS or not 1 <= page_size <= 100 or not 1 <= max_pages <= 150_001:
        raise ProbeError("INVALID_SEARCH_LIMIT")
    if not 1 <= max_records <= 150_000:
        raise ProbeError("INVALID_SEARCH_LIMIT")
    if serial_range is not None and (
        kind != "events" or len(serial_range) != 2
        or any(type(value) is not int for value in serial_range)
        or not 1 <= serial_range[0] <= serial_range[1] <= 3_000_000_000
    ):
        raise ProbeError("INVALID_SERIAL_RANGE")
    search_id = uuid.uuid4().hex
    position = 0
    original_total = None
    last_digest = None
    last_serial = None
    root_name = "UserInfoSearch" if kind == "users" else "AcsEvent"
    condition_name = "UserInfoSearchCond" if kind == "users" else "AcsEventCond"
    row_name = "UserInfo" if kind == "users" else "InfoList"
    for _ in range(max_pages):
        condition = {"searchID": search_id, "searchResultPosition": position,
                     "maxResults": min(page_size, max_records - position)}
        if condition["maxResults"] <= 0:
            raise ProbeError("SEARCH_RECORD_LIMIT")
        if kind == "events":
            condition.update(major=0, minor=0, picEnable=False)
            if serial_range:
                condition.update(beginSerialNo=serial_range[0], endSerialNo=serial_range[1])
        payload = fetch({condition_name: condition})
        result = payload.get(root_name) if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise ProbeError("SEARCH_RESULT_MISSING")
        if result.get("searchID") not in (None, search_id):
            raise ProbeError("SEARCH_SESSION_MISMATCH")
        rows = result.get(row_name, [])
        count, total = result.get("numOfMatches"), result.get("totalMatches")
        status = result.get("responseStatusStrg")
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or type(count) is not int or type(total) is not int
                or count != len(rows) or count > condition["maxResults"]
                or total < 0 or position + count > total
                or status not in {"OK", "MORE", "NO MATCH"}):
            raise ProbeError("SEARCH_INVALID_PAGE")
        if original_total is not None and original_total != total:
            raise ProbeError("SEARCH_TOTAL_CHANGED")
        original_total = total
        if serial_range:
            for row in rows:
                serial = row.get("serialNo")
                if (type(serial) is not int or not serial_range[0] <= serial <= serial_range[1]
                        or (last_serial is not None and serial <= last_serial)):
                    raise ProbeError("SEARCH_SERIAL_BOUNDARY_OR_ORDER_CHANGED")
                last_serial = serial
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
        if rows and digest == last_digest:
            raise ProbeError("SEARCH_REPEATED_PAGE")
        last_digest = digest
        next_position = position + count
        complete = next_position == total and status in {"OK", "NO MATCH"}
        if status == "MORE" and next_position >= total:
            raise ProbeError("SEARCH_INCONSISTENT_END")
        if (not rows and not complete) or (status == "NO MATCH" and total != 0):
            raise ProbeError("SEARCH_STALLED")
        if status == "OK" and not complete:
            raise ProbeError("SEARCH_PREMATURE_END")
        yield rows, {"position": position, "next_position": next_position,
                     "reported_total": total, "enumeration_complete": complete}
        position = next_position
        if complete:
            return


class ProbeClient:
    def __init__(self, config: ProbeConfig, *, transport=None):
        self.config = config
        verify: ssl.SSLContext | bool = True
        if config.ca_file:
            verify = ssl.create_default_context(cafile=config.ca_file)
        self.client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            auth=httpx.DigestAuth(config.username, config.password),
            verify=verify, timeout=config.timeout_seconds, trust_env=False,
            follow_redirects=False, transport=transport,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, payload=None) -> Any:
        if not ((method == "GET" and path in READ_PATHS)
                or (method == "POST" and path in SEARCH_PATHS.values())):
            raise ProbeError("READ_ONLY_ENDPOINT_REJECTED")
        started = time.monotonic()
        for attempt in range(2):
            # Only read-only resources reach here. A fresh Digest exchange recovers
            # devices that reject a renewed nonce on the first authenticated retry.
            options = {} if attempt == 0 else {
                "auth": httpx.DigestAuth(self.config.username, self.config.password),
            }
            if time.monotonic() - started > self.config.timeout_seconds:
                raise ProbeError("REQUEST_DEADLINE")
            with self.client.stream(method, path, json=payload, **options) as response:
                if response.status_code == 401 and attempt == 0:
                    continue
                if response.status_code != 200:
                    raise ProbeError(f"HTTP_{response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > self.config.timeout_seconds:
                        raise ProbeError("REQUEST_DEADLINE")
                    if len(body) + len(chunk) > MAX_BODY:
                        raise ProbeError("BODY_TOO_LARGE")
                    body.extend(chunk)
                return decode_body(bytes(body))
        raise ProbeError("HTTP_401")

    def observe_stream(self, seconds: int, sanitizer: Sanitizer) -> dict:
        started = time.monotonic()
        result: dict[str, Any] = {
            "status": "NOT_STARTED", "messages": 0, "samples": [], "current_samples": [],
            "current_event_count": 0, "replayed_event_count": 0, "unknown_age_event_count": 0,
        }
        try:
            with self.client.stream("GET", STREAM_PATH) as response:
                if response.status_code != 200:
                    raise ProbeError(f"HTTP_{response.status_code}")
                decoder = MultipartDecoder(multipart_boundary(
                    response.headers.get("content-type", ""),
                ))
                result["status"] = "OPEN"
                # iter_bytes(chunk_size=N) can wait to fill N bytes on a sparse
                # stream. Natural transport chunks preserve live latency.
                for chunk in response.iter_bytes():
                    for part in decoder.feed(chunk):
                        result["messages"] += 1
                        try:
                            value = decode_body(part)
                        except ProbeError:
                            result["malformed_parts"] = result.get("malformed_parts", 0) + 1
                            continue
                        if len(result["samples"]) < 10:
                            result["samples"].append(sanitizer.clean(value))
                        root = value.get("EventNotificationAlert", value)
                        event = root.get("AccessControllerEvent") if isinstance(root, dict) else None
                        if isinstance(event, dict):
                            current = event.get("currentEvent")
                            # XML scalars retain text; only accept explicit boolean
                            # spellings rather than treating any nonempty text as true.
                            if current == "true":
                                current = True
                            elif current == "false":
                                current = False
                            if current is True:
                                result["current_event_count"] += 1
                                if len(result["current_samples"]) < 10:
                                    result["current_samples"].append(sanitizer.clean(value))
                            elif current is False:
                                result["replayed_event_count"] += 1
                            else:
                                result["unknown_age_event_count"] += 1
                    if time.monotonic() - started >= seconds:
                        result["status"] = "OBSERVATION_WINDOW_ENDED"
                        break
                else:
                    result["status"] = "STREAM_ENDED"
                result["discarded_parts"] = decoder.discarded_parts
                result["oversized_parts"] = decoder.oversized_parts
                result["partial_part_at_stop"] = decoder.state in {"headers", "body"}
        except (ProbeError, httpx.HTTPError) as error:
            result["status"] = safe_error(error)
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result


def safe_error(error: Exception) -> str:
    if isinstance(error, ProbeError):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "NETWORK_TIMEOUT"
    return "NETWORK_OR_TLS_ERROR"


def qualify(config: ProbeConfig, *, page_size=20, max_pages=2, stream_seconds=20,
            transport=None) -> dict:
    if (not 1 <= stream_seconds <= 120 or not 1 <= page_size <= 100
            or not 1 <= max_pages <= 150_001):
        raise ProbeError("INVALID_PROBE_LIMITS")
    sanitizer = Sanitizer((config.username, config.password, config.expected_serial,
                           urlsplit(config.base_url).hostname or ""))
    report: dict[str, Any] = {
        "schema_version": 1, "firmware_family": "hikvision", "qualified": False,
        "qualification_state": "HARDWARE_ACCEPTANCE_REQUIRED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "transport": urlsplit(config.base_url).scheme,
        "identity_verified": False, "endpoints": {}, "searches": {},
        "required_hardware_checks": {name: "NOT_RUN" for name in REQUIRED_HARDWARE_CHECKS},
        "limitations": [
            "Read-only observations do not certify event mapping, CRUD, or full history coverage.",
            "Search offsets are session cursors, not stable source record identities.",
            "No credentials, images, names, CNICs, or exact device timestamps are exported.",
        ],
    }
    client = ProbeClient(config, transport=transport)
    try:
        try:
            identity = client.request("GET", READ_PATHS[0])
            info = identity.get("DeviceInfo", {})
            if not isinstance(info, dict) or info.get("serialNumber") != config.expected_serial:
                raise ProbeError("TERMINAL_SERIAL_MISMATCH")
            report["identity_verified"] = True
            report["endpoints"][READ_PATHS[0]] = {
                "status": "OK", "sample": sanitizer.clean(identity),
            }
        except (ProbeError, httpx.HTTPError) as error:
            report["qualification_state"] = safe_error(error)
            return report
        for path in READ_PATHS[1:]:
            try:
                report["endpoints"][path] = {
                    "status": "OK", "sample": sanitizer.clean(
                        client.request("GET", path),
                        capabilities="capabilities" in path or path.endswith("EventCap"),
                    ),
                }
            except (ProbeError, httpx.HTTPError) as error:
                report["endpoints"][path] = {"status": safe_error(error)}
                if str(error) in {"HTTP_401", "HTTP_403"}:
                    report["qualification_state"] = "AUTHORIZATION_FAILED"
                    return report

        # Independent Digest contexts for the long-lived stream and request worker.
        stream_client = ProbeClient(config, transport=transport)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(stream_client.observe_stream, stream_seconds, sanitizer)
                for kind in SEARCH_PATHS:
                    summary: dict[str, Any] = {
                        "status": "OK", "pages": 0, "records": 0,
                        "enumeration_complete": False, "coverage_certified": False,
                        "samples": [],
                    }
                    report["searches"][kind] = summary
                    try:
                        for rows, cursor in search_pages(
                            lambda payload: client.request("POST", SEARCH_PATHS[kind], payload),
                            kind=kind, page_size=page_size, max_pages=max_pages,
                        ):
                            summary.update(cursor)
                            summary["pages"] += 1
                            summary["records"] += len(rows)
                            if len(summary["samples"]) < 3:
                                summary["samples"].extend(sanitizer.clean(rows[:1]))
                    except (ProbeError, httpx.HTTPError) as error:
                        summary["status"] = safe_error(error)
                    if summary["status"] in {"HTTP_401", "HTTP_403"}:
                        report["qualification_state"] = "AUTHORIZATION_FAILED"
                        break
                report["live_stream"] = future.result()
        finally:
            stream_client.close()
        return report
    finally:
        client.close()


def initialize_config(path: Path) -> None:
    """Interactive local setup: passwords never appear in shell arguments/history."""
    import sys

    if not sys.stdin.isatty():
        raise ProbeError("INTERACTIVE_TERMINAL_REQUIRED")
    origin = input("Terminal origin (for example https://192.168.1.50): ").strip()
    username = input("Web/ISAPI username: ").strip()
    password = getpass.getpass("Web/ISAPI password (not the ISUP key): ")
    serial = input("Exact full terminal serial from its web interface or label: ").strip()
    allow_http = False
    ca_file = None
    if urlsplit(origin).scheme == "http":
        allow_http = input("Allow HTTP Digest on this trusted LAN? Type yes: ").strip() == "yes"
    else:
        ca_file = input("Trusted device CA certificate path (blank for system trust): ").strip() or None
    value = {"base_url": origin, "username": username, "password": password,
             "expected_serial": serial, "allow_http_digest": allow_http, "ca_file": ca_file}
    ProbeConfig(**value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    print("Owner-only configuration saved. No terminal requests were made.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", type=Path)
    mode.add_argument("--init-config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--stream-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if args.config and not args.output:
        parser.error("--output is required with --config")
    try:
        if args.init_config:
            initialize_config(args.init_config)
            return 0
        if args.output.exists() or args.output.is_symlink():
            raise ProbeError("OUTPUT_ALREADY_EXISTS")
        config = ProbeConfig.load(args.config)
        report = qualify(config, page_size=args.page_size, max_pages=args.max_pages,
                         stream_seconds=args.stream_seconds)
        # Do not overwrite an earlier report or follow a user-created symlink.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print("Read-only report saved. Terminal remains UNQUALIFIED.")
        return 0 if report["identity_verified"] else 2
    except (OSError, ValueError, TypeError, EOFError, ProbeError):
        # Exception text may contain a URL, local credential content, or path.
        print("Qualification failed. Check config permissions, fields, LAN IP, and output path.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
