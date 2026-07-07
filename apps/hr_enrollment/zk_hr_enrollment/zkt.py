from __future__ import annotations

import socket
import struct
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType

from zk_zone_agent.network_scanner import NetworkScanner, ScanCandidate, network_scanner


class RuntimeDependencyError(RuntimeError):
    """Raised when the packaged EXE is missing a required runtime dependency."""


class ZKCommunicationError(RuntimeError):
    """Raised when a ZKT device connection drops or times out during an operation."""


FACE_TEMPLATE_ID = 111
FACE_EVENT_WAIT_SECONDS = 70


@dataclass(frozen=True)
class ScannedDevice:
    ip: str
    port: int
    serial: str | None
    platform: str | None
    device_name: str | None
    force_udp: bool | None = None
    subnet: str | None = None
    interface_name: str | None = None
    validated: bool = True
    validation_error: str | None = None

    @property
    def label(self) -> str:
        parts = [f"{self.ip}:{self.port}"]
        if self.device_name:
            parts.append(self.device_name)
        if self.platform:
            parts.append(self.platform)
        if self.serial:
            parts.append(self.serial)
        if not self.validated:
            parts.append("open ZKT port - not validated yet")
        elif self.force_udp is True:
            parts.append("UDP")
        return " | ".join(parts)


@dataclass(frozen=True)
class EnrollmentUser:
    uid: str
    user_id: str
    name: str | None
    privilege: str | None
    password: str | None = None
    group_id: str | None = None
    card: int | None = None


@dataclass(frozen=True)
class FingerTemplate:
    uid: int
    fid: int
    valid: int
    size: int


@dataclass(frozen=True)
class BiometricCounts:
    users: int | None
    fingers: int | None
    faces: int | None
    users_capacity: int | None
    fingers_capacity: int | None
    faces_capacity: int | None


@dataclass(frozen=True)
class RemoteEnrollmentResult:
    started: bool
    completed: bool
    event_count: int
    event_codes: tuple[int, ...]


class ZKDeviceSession:
    def __init__(
        self,
        *,
        ip: str,
        port: int,
        comm_key: int,
        timeout: float = 10,
        force_udp: bool = False,
        connect_attempts: int = 2,
        retry_delay: float = 0.75,
    ) -> None:
        self.ip = ip
        self.port = port
        self.comm_key = comm_key
        self.timeout = timeout
        self.force_udp = force_udp
        self.connect_attempts = max(1, int(connect_attempts))
        self.retry_delay = max(0, float(retry_delay))
        self.conn = None

    def __enter__(self) -> ZKDeviceSession:
        try:
            from zk import ZK  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeDependencyError(
                "The bundled ZKT device library is missing. Rebuild the Windows EXE from "
                "the latest shipping workflow so all runtime components are included."
            ) from exc

        last_error: Exception | None = None
        for attempt in range(1, self.connect_attempts + 1):
            zk = ZK(
                self.ip,
                port=self.port,
                timeout=self.timeout,
                password=self.comm_key,
                force_udp=self.force_udp,
                ommit_ping=True,
            )
            try:
                self.conn = zk.connect()
                return self
            except Exception as exc:
                if isinstance(exc, RuntimeDependencyError):
                    raise
                last_error = exc
                self.conn = None
                if attempt >= self.connect_attempts or not is_device_communication_error(exc):
                    break
                if self.retry_delay:
                    time.sleep(self.retry_delay)

        if last_error is not None and is_device_communication_error(last_error):
            raise ZKCommunicationError(
                f"Could not connect to ZKT device {self.ip}:{self.port}. "
                "The device timed out or closed the connection."
            ) from last_error
        if last_error is not None:
            raise last_error
        raise ZKCommunicationError(f"Could not connect to ZKT device {self.ip}:{self.port}.")

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self.conn is not None:
            try:
                self.conn.disconnect()
            except Exception:
                pass
            finally:
                self.conn = None

    def get_info(self) -> tuple[str | None, str | None, str | None]:
        conn = self._require_conn()
        return (
            _safe_call(conn.get_serialnumber),
            _safe_call(conn.get_platform),
            _safe_call(conn.get_device_name),
        )

    def get_users(self) -> list[EnrollmentUser]:
        return [
            _user_from_pyzk(user)
            for user in self._device_call("read users", lambda: self._require_conn().get_users() or [])
            if user is not None
        ]

    def get_templates(self) -> list[FingerTemplate]:
        templates: list[FingerTemplate] = []
        raw_templates = self._device_call(
            "read fingerprint templates",
            lambda: self._require_conn().get_templates() or [],
        )
        for template in raw_templates:
            try:
                templates.append(_template_from_pyzk(template))
            except (TypeError, ValueError):
                continue
        return templates

    def get_biometric_counts(self) -> BiometricCounts:
        conn = self._require_conn()
        self._device_call("read biometric counts", conn.read_sizes)
        return BiometricCounts(
            users=_optional_int(getattr(conn, "users", None)),
            fingers=_optional_int(getattr(conn, "fingers", None)),
            faces=_optional_int(getattr(conn, "faces", None)),
            users_capacity=_optional_int(getattr(conn, "users_cap", None)),
            fingers_capacity=_optional_int(getattr(conn, "fingers_cap", None)),
            faces_capacity=_optional_int(getattr(conn, "faces_cap", None)),
        )

    def create_user(self, *, uid: int, user_id: str, name: str) -> EnrollmentUser:
        conn = self._require_conn()
        self._device_call(
            "create user",
            lambda: conn.set_user(
                uid=uid,
                name=name,
                privilege=0,
                password="",
                group_id="",
                user_id=user_id,
                card=0,
            ),
        )
        users = self.get_users()
        created = next(
            (user for user in users if str(user.uid) == str(uid) or str(user.user_id) == str(user_id)),
            None,
        )
        if created is None:
            raise RuntimeError("Device accepted the user create command but the user could not be reloaded.")
        return created

    def enroll_finger(self, *, uid: str | int, user_id: str, finger_id: int):
        conn = self._require_conn()
        _safe_prepare_enrollment_state(conn)
        try:
            return self._device_call(
                "enroll fingerprint",
                lambda: conn.enroll_user(uid=int(uid), temp_id=int(finger_id), user_id=str(user_id)),
            )
        finally:
            _safe_recover_enrollment_state(conn)

    def enroll_face(self, *, uid: str | int, user_id: str):
        conn = self._require_conn()
        _safe_prepare_enrollment_state(conn)
        try:
            return self._device_call(
                "enroll face",
                lambda: self._start_remote_enrollment(
                    conn,
                    user_id=str(user_id),
                    template_id=FACE_TEMPLATE_ID,
                    wait_seconds=min(float(self.timeout), FACE_EVENT_WAIT_SECONDS),
                ),
            )
        finally:
            _safe_recover_enrollment_state(conn)

    def reset_enrollment_state(self) -> None:
        _safe_recover_enrollment_state(self._require_conn())

    def _require_conn(self):
        if self.conn is None:
            raise RuntimeError("ZKT device is not connected.")
        return self.conn

    def _device_call(self, operation: str, func):
        try:
            return func()
        except Exception as exc:
            if is_device_communication_error(exc):
                raise ZKCommunicationError(
                    f"Could not {operation} on ZKT device {self.ip}:{self.port}. "
                    "The device timed out or closed the connection."
                ) from exc
            raise

    def _start_remote_enrollment(
        self,
        conn,
        *,
        user_id: str,
        template_id: int,
        wait_seconds: float,
    ) -> RemoteEnrollmentResult | object:
        send_command = getattr(conn, "_ZK__send_command", None)
        sock = getattr(conn, "_ZK__sock", None)
        ack_ok = getattr(conn, "_ZK__ack_ok", None)
        if not callable(send_command) or sock is None or not callable(ack_ok):
            return conn.enroll_user(uid=int(user_id), temp_id=int(template_id), user_id=str(user_id))

        if bool(getattr(conn, "tcp", not self.force_udp)):
            command_string = struct.pack("<24sbb", str(user_id).encode(), int(template_id), 1)
            code_offset = 16
        else:
            command_string = struct.pack("<Ib", int(user_id), int(template_id))
            code_offset = 8

        cmd_response = send_command(self._cmd_startenroll(), command_string)
        if not cmd_response.get("status"):
            raise RuntimeError(f"Device rejected remote enrollment for template {template_id}.")

        event_codes: list[int] = []
        completed = False
        previous_timeout = getattr(sock, "gettimeout", lambda: None)()
        try:
            sock.settimeout(2)
            deadline = time.monotonic() + max(1, wait_seconds)
            while time.monotonic() < deadline:
                try:
                    data_recv = sock.recv(1032)
                except socket.timeout:
                    continue
                ack_ok()
                code = _event_result_code(data_recv, code_offset)
                if code is not None:
                    event_codes.append(code)
                if code == 0:
                    completed = True
                    break
                if code in {4, 5, 6}:
                    break
        finally:
            try:
                sock.settimeout(previous_timeout)
            except Exception:
                pass

        return RemoteEnrollmentResult(
            started=True,
            completed=completed,
            event_count=len(event_codes),
            event_codes=tuple(event_codes),
        )

    @staticmethod
    def _cmd_startenroll() -> int:
        try:
            from zk import const  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeDependencyError(
                "The bundled ZKT device library is missing. Rebuild the Windows EXE from "
                "the latest shipping workflow so all runtime components are included."
            ) from exc
        return int(const.CMD_STARTENROLL)


class AutoProtocolZKDeviceSession(AbstractContextManager):
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.session: ZKDeviceSession | None = None

    def __enter__(self) -> ZKDeviceSession:
        last_error: Exception | None = None
        for force_udp in (False, True):
            session = ZKDeviceSession(**{**self.kwargs, "force_udp": force_udp})
            try:
                self.session = session.__enter__()
                return self.session
            except Exception as exc:
                if isinstance(exc, RuntimeDependencyError):
                    raise
                last_error = exc
                try:
                    session.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
        if last_error is not None and is_device_communication_error(last_error):
            ip = self.kwargs.get("ip", "unknown")
            port = self.kwargs.get("port", "unknown")
            raise ZKCommunicationError(
                f"Could not connect to ZKT device {ip}:{port}. "
                "TCP and UDP both timed out or closed the connection."
            ) from last_error
        if last_error is not None:
            raise last_error
        raise ZKCommunicationError("Could not connect to ZKT device.")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.session is not None:
            try:
                self.session.__exit__(exc_type, exc, tb)
            finally:
                self.session = None


SessionOpener = Callable[..., AbstractContextManager]


def open_zkt_session(**kwargs) -> AbstractContextManager:
    if kwargs.get("force_udp") is None:
        return AutoProtocolZKDeviceSession(**kwargs)
    return ZKDeviceSession(**kwargs)


def probe_device(
    candidate: ScanCandidate,
    *,
    comm_key: int,
    timeout: float = 5,
    session_opener: SessionOpener = open_zkt_session,
) -> ScannedDevice:
    last_error: Exception | None = None
    for force_udp in (False, True):
        try:
            with session_opener(
                ip=candidate.ip,
                port=candidate.port,
                comm_key=comm_key,
                timeout=timeout,
                force_udp=force_udp,
            ) as session:
                serial, platform, device_name = session.get_info()
                return ScannedDevice(
                    ip=candidate.ip,
                    port=candidate.port,
                    serial=serial,
                    platform=platform,
                    device_name=device_name,
                    force_udp=force_udp,
                    subnet=candidate.subnet,
                    interface_name=candidate.interface_name,
                )
        except Exception as exc:
            if isinstance(exc, RuntimeDependencyError):
                raise
            last_error = exc
    raise RuntimeError(f"Could not validate ZKT device at {candidate.ip}:{candidate.port}: {last_error}")


def scan_zkt_devices(
    *,
    comm_key: int,
    scanner: NetworkScanner = network_scanner,
    timeout: float = 5,
    session_opener: SessionOpener = open_zkt_session,
) -> list[ScannedDevice]:
    candidates = scanner.scan()
    if not candidates:
        return []
    devices: list[ScannedDevice] = []
    workers = min(16, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                probe_device,
                candidate,
                comm_key=comm_key,
                timeout=timeout,
                session_opener=session_opener,
            ): candidate
            for candidate in candidates
        }
        for future in as_completed(future_map):
            candidate = future_map[future]
            try:
                devices.append(future.result())
            except RuntimeDependencyError:
                devices.append(_candidate_device(candidate, "Missing ZKT runtime dependency."))
            except Exception as exc:
                devices.append(_candidate_device(candidate, str(exc)))
    return sorted(devices, key=lambda device: tuple(int(part) for part in device.ip.split(".")))


def _candidate_device(candidate: ScanCandidate, validation_error: str | None = None) -> ScannedDevice:
    return ScannedDevice(
        ip=candidate.ip,
        port=candidate.port,
        serial=None,
        platform=None,
        device_name=None,
        force_udp=None,
        subnet=candidate.subnet,
        interface_name=candidate.interface_name,
        validated=False,
        validation_error=validation_error,
    )


def _safe_call(func):
    try:
        return func()
    except Exception:
        return None


def _optional_int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _event_result_code(data: bytes, offset: int) -> int | None:
    if len(data) <= offset + 1:
        return None
    try:
        return struct.unpack("H", data.ljust(offset + 2, b"\x00")[offset : offset + 2])[0]
    except struct.error:
        return None


def is_device_communication_error(exc: BaseException) -> bool:
    if isinstance(exc, ZKCommunicationError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__.lower()
    if name in {"zknetworkerror", "connectionreseterror", "timeout"}:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "forcibly closed",
            "connection reset",
            "connection aborted",
            "broken pipe",
        )
    )


def _safe_prepare_enrollment_state(conn) -> None:
    _safe_device_method(conn, "free_data")
    _safe_device_method(conn, "cancel_capture")
    _safe_device_method(conn, "cancel_enroll")
    _safe_device_method(conn, "verify_user")
    _safe_device_method(conn, "enable_device")


def _safe_recover_enrollment_state(conn) -> None:
    _safe_device_method(conn, "reg_event", 0)
    _safe_device_method(conn, "cancel_capture")
    _safe_device_method(conn, "cancel_enroll")
    _safe_device_method(conn, "verify_user")
    _safe_device_method(conn, "free_data")
    _safe_device_method(conn, "refresh_data")
    _safe_device_method(conn, "enable_device")


def _safe_device_method(conn, method_name: str, *args) -> None:
    method = getattr(conn, method_name, None)
    if not callable(method):
        return
    try:
        method(*args)
    except Exception:
        pass


def _user_from_pyzk(user) -> EnrollmentUser:
    card = getattr(user, "card", None)
    try:
        card = None if card is None else int(card)
    except (TypeError, ValueError):
        card = None
    return EnrollmentUser(
        uid=str(getattr(user, "uid", "")),
        user_id=str(getattr(user, "user_id", "")),
        name=getattr(user, "name", None),
        privilege=str(getattr(user, "privilege", "")),
        password=getattr(user, "password", None),
        group_id=getattr(user, "group_id", None),
        card=card,
    )


def _template_from_pyzk(template) -> FingerTemplate:
    return FingerTemplate(
        uid=int(getattr(template, "uid")),
        fid=int(getattr(template, "fid")),
        valid=int(getattr(template, "valid", 0)),
        size=int(getattr(template, "size", 0)),
    )
