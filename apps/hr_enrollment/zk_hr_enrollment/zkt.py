from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from types import TracebackType

from zk_zone_agent.network_scanner import NetworkScanner, ScanCandidate, network_scanner


class RuntimeDependencyError(RuntimeError):
    """Raised when the packaged EXE is missing a required runtime dependency."""


@dataclass(frozen=True)
class ScannedDevice:
    ip: str
    port: int
    serial: str | None
    platform: str | None
    device_name: str | None
    force_udp: bool
    subnet: str | None = None
    interface_name: str | None = None

    @property
    def label(self) -> str:
        parts = [self.ip]
        if self.device_name:
            parts.append(self.device_name)
        if self.platform:
            parts.append(self.platform)
        if self.serial:
            parts.append(self.serial)
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


class ZKDeviceSession:
    def __init__(
        self,
        *,
        ip: str,
        port: int,
        comm_key: int,
        timeout: float = 10,
        force_udp: bool = False,
    ) -> None:
        self.ip = ip
        self.port = port
        self.comm_key = comm_key
        self.timeout = timeout
        self.force_udp = force_udp
        self.conn = None

    def __enter__(self) -> ZKDeviceSession:
        try:
            from zk import ZK  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeDependencyError(
                "The bundled ZKT device library is missing. Rebuild the Windows EXE from "
                "the latest shipping workflow so all runtime components are included."
            ) from exc

        zk = ZK(
            self.ip,
            port=self.port,
            timeout=self.timeout,
            password=self.comm_key,
            force_udp=self.force_udp,
            ommit_ping=True,
        )
        self.conn = zk.connect()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self.conn is not None:
            try:
                self.conn.disconnect()
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
            for user in (self._require_conn().get_users() or [])
            if user is not None
        ]

    def get_templates(self) -> list[FingerTemplate]:
        templates: list[FingerTemplate] = []
        for template in self._require_conn().get_templates() or []:
            try:
                templates.append(_template_from_pyzk(template))
            except (TypeError, ValueError):
                continue
        return templates

    def create_user(self, *, uid: int, user_id: str, name: str) -> EnrollmentUser:
        conn = self._require_conn()
        conn.set_user(
            uid=uid,
            name=name,
            privilege=0,
            password="",
            group_id="",
            user_id=user_id,
            card=0,
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
        return self._require_conn().enroll_user(uid=int(uid), temp_id=int(finger_id), user_id=str(user_id))

    def _require_conn(self):
        if self.conn is None:
            raise RuntimeError("ZKT device is not connected.")
        return self.conn


SessionOpener = Callable[..., ZKDeviceSession]


def open_zkt_session(**kwargs) -> ZKDeviceSession:
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
            try:
                devices.append(future.result())
            except RuntimeDependencyError:
                raise
            except Exception:
                continue
    return sorted(devices, key=lambda device: tuple(int(part) for part in device.ip.split(".")))


def _safe_call(func):
    try:
        return func()
    except Exception:
        return None


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
