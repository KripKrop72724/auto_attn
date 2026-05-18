from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol


@dataclass(frozen=True)
class ZKUser:
    uid: str
    user_id: str
    name: str | None = None
    privilege: str | None = None
    password: str | None = None
    group_id: str | None = None
    card: int | None = None
    raw: dict | None = None


@dataclass(frozen=True)
class ZKAttendance:
    user_id: str
    timestamp: datetime
    status: str | int | None = None
    punch: str | int | None = None
    uid: str | int | None = None
    raw: dict | None = None


@dataclass(frozen=True)
class ZKDeviceInfo:
    serial: str | None
    platform: str | None
    device_name: str | None


class ZKClient(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_info(self) -> ZKDeviceInfo: ...

    def get_users(self) -> list[ZKUser]: ...

    def update_user(
        self,
        *,
        uid: str | int,
        user_id: str,
        name: str,
        privilege: int,
        card: int,
    ) -> ZKUser: ...

    def stop_live_capture(self) -> None: ...

    def get_time(self) -> datetime: ...

    def get_attendance(self) -> list[ZKAttendance]: ...

    def live_capture(self, new_timeout: int = 5) -> Iterator[ZKAttendance | None]: ...


class PyZKClient:
    def __init__(
        self,
        *,
        ip: str,
        port: int = 4370,
        comm_key: int = 0,
        timeout: float = 5,
        force_udp: bool = False,
    ) -> None:
        self.ip = ip
        self.port = port
        self.comm_key = comm_key
        self.timeout = timeout
        self.force_udp = force_udp
        self.zk = None
        self.conn = None

    def connect(self) -> None:
        from zk import ZK  # type: ignore

        self.zk = ZK(
            self.ip,
            port=self.port,
            timeout=self.timeout,
            password=self.comm_key,
            force_udp=self.force_udp,
            ommit_ping=True,
        )
        self.conn = self.zk.connect()

    def disconnect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.end_live_capture = True
                self.conn.disconnect()
            finally:
                self.conn = None

    def _require_conn(self):
        if self.conn is None:
            raise RuntimeError("ZKT device is not connected.")
        return self.conn

    def get_info(self) -> ZKDeviceInfo:
        conn = self._require_conn()
        return ZKDeviceInfo(
            serial=_safe_call(conn.get_serialnumber),
            platform=_safe_call(conn.get_platform),
            device_name=_safe_call(conn.get_device_name),
        )

    def get_users(self) -> list[ZKUser]:
        conn = self._require_conn()
        return [_user_from_pyzk(user) for user in conn.get_users()]

    def update_user(
        self,
        *,
        uid: str | int,
        user_id: str,
        name: str,
        privilege: int,
        card: int,
    ) -> ZKUser:
        conn = self._require_conn()
        uid_text = str(uid)
        existing = next((user for user in self.get_users() if str(user.uid) == uid_text), None)
        if existing is None:
            raise ValueError(f"User UID {uid_text} was not found on the device.")
        password = existing.password or ""
        group_id = existing.group_id or ""
        conn.set_user(
            uid=int(uid_text),
            name=name,
            privilege=int(privilege),
            password=password,
            group_id=group_id,
            user_id=user_id,
            card=int(card),
        )
        refreshed = next((user for user in self.get_users() if str(user.uid) == uid_text), None)
        if refreshed is None:
            raise RuntimeError("Device accepted the update but the user could not be reloaded.")
        return refreshed

    def get_time(self) -> datetime:
        return self._require_conn().get_time()

    def get_attendance(self) -> list[ZKAttendance]:
        conn = self._require_conn()
        return [_attendance_from_pyzk(item) for item in conn.get_attendance()]

    def live_capture(self, new_timeout: int = 5) -> Iterator[ZKAttendance | None]:
        conn = self._require_conn()
        for item in conn.live_capture(new_timeout=new_timeout):
            yield None if item is None else _attendance_from_pyzk(item)

    def stop_live_capture(self) -> None:
        conn = self._require_conn()
        conn.end_live_capture = True


def _safe_call(func):
    try:
        return func()
    except Exception:
        return None


def _user_from_pyzk(user) -> ZKUser:
    card = getattr(user, "card", None)
    try:
        card = None if card is None else int(card)
    except (TypeError, ValueError):
        card = None
    return ZKUser(
        uid=str(getattr(user, "uid", "")),
        user_id=str(getattr(user, "user_id", "")),
        name=getattr(user, "name", None),
        privilege=str(getattr(user, "privilege", "")),
        password=getattr(user, "password", None),
        group_id=getattr(user, "group_id", None),
        card=card,
        raw={
            "uid": getattr(user, "uid", None),
            "user_id": getattr(user, "user_id", None),
            "name": getattr(user, "name", None),
            "privilege": getattr(user, "privilege", None),
            "password": getattr(user, "password", None),
            "group_id": getattr(user, "group_id", None),
            "card": card,
        },
    )


def _attendance_from_pyzk(item) -> ZKAttendance:
    return ZKAttendance(
        user_id=str(getattr(item, "user_id", "")),
        timestamp=getattr(item, "timestamp"),
        status=getattr(item, "status", None),
        punch=getattr(item, "punch", None),
        uid=getattr(item, "uid", None),
        raw={
            "user_id": getattr(item, "user_id", None),
            "timestamp": getattr(item, "timestamp", None).isoformat()
            if getattr(item, "timestamp", None)
            else None,
            "status": getattr(item, "status", None),
            "punch": getattr(item, "punch", None),
            "uid": getattr(item, "uid", None),
        },
    )


class FakeZKClient:
    def __init__(
        self,
        *,
        info: ZKDeviceInfo | None = None,
        users: list[ZKUser] | None = None,
        attendances: list[ZKAttendance] | None = None,
        current_time: datetime | None = None,
    ) -> None:
        self.info = info or ZKDeviceInfo("FAKE-SERIAL", "FAKE", "Fake Device")
        self.users = users or []
        self.attendances = attendances or []
        self.current_time = current_time or datetime.utcnow()
        self.connected = False
        self.live_capture_stopped = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_info(self) -> ZKDeviceInfo:
        return self.info

    def get_users(self) -> list[ZKUser]:
        return self.users

    def update_user(
        self,
        *,
        uid: str | int,
        user_id: str,
        name: str,
        privilege: int,
        card: int,
    ) -> ZKUser:
        uid_text = str(uid)
        for index, user in enumerate(self.users):
            if str(user.uid) == uid_text:
                updated = ZKUser(
                    uid=user.uid,
                    user_id=str(user_id),
                    name=name,
                    privilege=str(privilege),
                    password=user.password,
                    group_id=user.group_id,
                    card=int(card),
                    raw={
                        **(user.raw or {}),
                        "uid": user.uid,
                        "user_id": str(user_id),
                        "name": name,
                        "privilege": int(privilege),
                        "card": int(card),
                    },
                )
                self.users[index] = updated
                return updated
        raise ValueError(f"User UID {uid_text} was not found on the device.")

    def get_time(self) -> datetime:
        return self.current_time

    def get_attendance(self) -> list[ZKAttendance]:
        return self.attendances

    def live_capture(self, new_timeout: int = 5) -> Iterator[ZKAttendance | None]:
        self.live_capture_stopped = False
        for attendance in self.attendances:
            if self.live_capture_stopped:
                return
            yield attendance
        while not self.live_capture_stopped:
            yield None

    def stop_live_capture(self) -> None:
        self.live_capture_stopped = True
