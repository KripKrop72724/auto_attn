from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zk_hr_enrollment.zkt import FACE_TEMPLATE_ID


class OfficialSdkUnavailable(RuntimeError):
    """Raised when the Windows ZKTeco COM SDK cannot be used on this PC."""


class OfficialSdkEnrollmentError(RuntimeError):
    """Raised when the ZKTeco COM SDK rejects or fails a remote enrollment."""


ZKEMKEEPER_PROG_ID = "zkemkeeper.ZKEM.1"
ZKEMKEEPER_DLL_NAME = "zkemkeeper.dll"


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
        zkem = _dispatch_zkem(win32com.client)

        _event_sink = None
        try:
            _event_sink = win32com.client.WithEvents(zkem, ZkemEvents)
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


def _dispatch_zkem(win32_client):
    try:
        return win32_client.Dispatch(ZKEMKEEPER_PROG_ID)
    except Exception as first_error:
        registered_path = _try_register_nearby_zkemkeeper()
        if registered_path is not None:
            try:
                return win32_client.Dispatch(ZKEMKEEPER_PROG_ID)
            except Exception as second_error:
                raise OfficialSdkUnavailable(
                    "zkemkeeper.dll was found and registration was attempted, but Windows still "
                    f"could not create {ZKEMKEEPER_PROG_ID}. Run StateLifeHREnrollment.exe once "
                    "as Administrator, or install the official ZKTeco Standalone SDK on this PC."
                ) from second_error
        raise OfficialSdkUnavailable(
            "zkemkeeper.dll is not registered on this Windows PC. Install the official ZKTeco "
            "Standalone SDK, or place zkemkeeper.dll beside StateLifeHREnrollment.exe and run the "
            "app once as Administrator so the COM class can be registered."
        ) from first_error


def _try_register_nearby_zkemkeeper() -> Path | None:
    for dll_path in _candidate_zkemkeeper_paths():
        if not dll_path.exists():
            continue
        if _register_zkemkeeper(dll_path):
            return dll_path
    return None


def _candidate_zkemkeeper_paths() -> tuple[Path, ...]:
    candidates: list[Path] = []
    env_path = os.environ.get("ZKEMKEEPER_DLL")
    if env_path:
        candidates.append(Path(env_path))

    executable_dir = Path(sys.executable).resolve().parent
    candidates.append(executable_dir / ZKEMKEEPER_DLL_NAME)

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / ZKEMKEEPER_DLL_NAME)

    candidates.extend(
        [
            Path.cwd() / ZKEMKEEPER_DLL_NAME,
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "SysWOW64" / ZKEMKEEPER_DLL_NAME,
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / ZKEMKEEPER_DLL_NAME,
        ]
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _register_zkemkeeper(dll_path: Path) -> bool:
    try:
        completed = subprocess.run(
            ["regsvr32", "/s", str(dll_path)],
            check=False,
            timeout=20,
        )
    except Exception:
        return False
    return completed.returncode == 0


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
