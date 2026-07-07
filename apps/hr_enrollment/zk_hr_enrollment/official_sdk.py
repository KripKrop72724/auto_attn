from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from zk_hr_enrollment.zkt import FACE_TEMPLATE_ID


class OfficialSdkUnavailable(RuntimeError):
    """Raised when the Windows ZKTeco COM SDK cannot be used on this PC."""


class OfficialSdkEnrollmentError(RuntimeError):
    """Raised when the ZKTeco COM SDK rejects or fails a remote enrollment."""


@dataclass(frozen=True)
class OfficialFaceEnrollmentResult:
    started: bool
    completed: bool
    event_args: tuple[object, ...] = ()


def enroll_face_with_official_sdk(
    *,
    ip: str,
    port: int,
    comm_key: int,
    user_id: str,
    timeout: float,
) -> OfficialFaceEnrollmentResult:
    if sys.platform != "win32":
        raise OfficialSdkUnavailable("The official ZKTeco SDK face path is only available on Windows.")
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ModuleNotFoundError as exc:
        raise OfficialSdkUnavailable(
            "pywin32 is not available, so the official ZKTeco SDK cannot be loaded."
        ) from exc

    pythoncom.CoInitialize()
    zkem = None
    connected = False
    completed = threading.Event()
    event_args: list[object] = []

    class ZkemEvents:
        def OnEnrollFinger(self, *args):  # noqa: N802 - COM event name
            event_args.extend(args)
            completed.set()

        def OnEnrollFingerEx(self, *args):  # noqa: N802 - COM event name
            event_args.extend(args)
            completed.set()

    try:
        try:
            zkem = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
        except Exception as exc:
            raise OfficialSdkUnavailable(
                "zkemkeeper.dll is not registered on this Windows PC."
            ) from exc

        event_sink = None
        try:
            event_sink = win32com.client.WithEvents(zkem, ZkemEvents)
        except Exception:
            pass

        _optional_bool_call(zkem, "SetCommPassword", int(comm_key))
        connected = bool(zkem.Connect_Net(str(ip), int(port)))
        if not connected:
            raise OfficialSdkEnrollmentError(
                f"Official ZKTeco SDK could not connect to {ip}:{port}{_last_error_suffix(zkem)}."
            )

        _optional_bool_call(zkem, "RegEvent", 1, 65535)
        _optional_bool_call(zkem, "EnableDevice", 1, False)
        started = bool(zkem.StartEnrollEx(str(user_id), FACE_TEMPLATE_ID, 1))
        if not started:
            raise OfficialSdkEnrollmentError(
                "Official ZKTeco SDK rejected face enrollment start"
                f"{_last_error_suffix(zkem)}."
            )

        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline and not completed.is_set():
            pythoncom.PumpWaitingMessages()
            time.sleep(0.1)

        return OfficialFaceEnrollmentResult(
            started=True,
            completed=completed.is_set(),
            event_args=tuple(event_args),
        )
    finally:
        if zkem is not None:
            if connected and not completed.is_set():
                _optional_bool_call(zkem, "CancelOperation")
            if connected:
                _optional_bool_call(zkem, "StartIdentify")
                _optional_bool_call(zkem, "EnableDevice", 1, True)
                try:
                    zkem.Disconnect()
                except Exception:
                    pass
        pythoncom.CoUninitialize()


def _optional_bool_call(obj, method_name: str, *args) -> bool | None:
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    try:
        return bool(method(*args))
    except Exception:
        return None


def _last_error_suffix(zkem) -> str:
    method = getattr(zkem, "GetLastError", None)
    if not callable(method):
        return ""
    try:
        return f" (SDK error {method()})"
    except Exception:
        return ""
