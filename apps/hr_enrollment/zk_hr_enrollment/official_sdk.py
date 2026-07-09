from __future__ import annotations

import os
import shutil
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
    """Raised when the ZKTeco SDK rejects or fails a remote enrollment."""


ZKEMKEEPER_PROG_ID = "zkemkeeper.ZKEM.1"
ZKEMKEEPER_DLL_NAME = "zkemkeeper.dll"
ZKEMKEEPER_DEPENDENCY_DLL_NAMES = (
    "commpro.dll",
    "comms.dll",
    "plcommpro.dll",
    "plcomms.dll",
    "plrscagent.dll",
    "plrscomm.dll",
    "pltcpcomm.dll",
    "rscagent.dll",
    "rscomm.dll",
    "tcpcomm.dll",
    "usbcomm.dll",
    "zkemsdk.dll",
)
ZKEMKEEPER_PAYLOAD_DLL_NAMES = (ZKEMKEEPER_DLL_NAME, *ZKEMKEEPER_DEPENDENCY_DLL_NAMES)


@dataclass(frozen=True)
class OfficialFaceEnrollmentResult:
    started: bool
    completed: bool
    event_args: tuple[object, ...] = ()


def find_zkemkeeper_payload() -> Path | None:
    for dll_path in _candidate_zkemkeeper_paths():
        if dll_path.exists():
            return dll_path
    return None


def find_zkemkeeper_payloads() -> dict[str, Path]:
    payloads: dict[str, Path] = {}
    main_dll = find_zkemkeeper_payload()
    if main_dll is not None:
        payloads[ZKEMKEEPER_DLL_NAME] = main_dll

    search_dirs = _candidate_zkemkeeper_dirs(main_dll)
    for dll_name in ZKEMKEEPER_DEPENDENCY_DLL_NAMES:
        for directory in search_dirs:
            dll_path = directory / dll_name
            if dll_path.exists():
                payloads[dll_name] = dll_path
                break
    return payloads


def find_missing_zkemkeeper_payloads() -> tuple[str, ...]:
    payloads = find_zkemkeeper_payloads()
    return tuple(dll_name for dll_name in ZKEMKEEPER_PAYLOAD_DLL_NAMES if dll_name not in payloads)


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
        missing_payloads = find_missing_zkemkeeper_payloads()
        if ZKEMKEEPER_DLL_NAME not in missing_payloads and missing_payloads:
            raise OfficialSdkUnavailable(
                "zkemkeeper.dll was found, but the ZKTeco SDK DLL set is incomplete. Missing: "
                f"{', '.join(missing_payloads)}. Bundle the official 32-bit SDK DLL folder with "
                "StateLifeHREnrollment.exe."
            ) from first_error
        raise OfficialSdkUnavailable(
            "zkemkeeper.dll is not registered on this Windows PC. Install the official ZKTeco "
            "Standalone SDK, or place the full official SDK DLL folder beside "
            "StateLifeHREnrollment.exe and run the app once as Administrator so the COM class can "
            "be registered."
        ) from first_error


def _try_register_nearby_zkemkeeper() -> Path | None:
    dll_path = find_zkemkeeper_payload()
    if dll_path is None:
        return None
    if find_missing_zkemkeeper_payloads():
        return None
    for candidate in _registration_candidates(dll_path):
        if _register_zkemkeeper(candidate):
            return candidate
    return None


def _registration_candidates(dll_path: Path) -> tuple[Path, ...]:
    stable_path = _copy_payload_to_stable_sdk_dir(dll_path)
    candidates = [path for path in (stable_path, dll_path) if path is not None]
    return tuple(dict.fromkeys(candidates))


def _copy_payload_to_stable_sdk_dir(dll_path: Path) -> Path | None:
    stable_dir = _stable_sdk_dir()
    if stable_dir is None:
        return None
    try:
        stable_dir.mkdir(parents=True, exist_ok=True)
        payloads = find_zkemkeeper_payloads()
        payloads.setdefault(ZKEMKEEPER_DLL_NAME, dll_path)
        for source_path in payloads.values():
            shutil.copy2(source_path, stable_dir / source_path.name)
        return stable_dir / ZKEMKEEPER_DLL_NAME
    except Exception:
        return None


def _stable_sdk_dir() -> Path | None:
    for env_name in ("ProgramData", "LOCALAPPDATA"):
        base_dir = os.environ.get(env_name)
        if base_dir:
            return Path(base_dir) / "State Life Insurance Corporation" / "HR Enrollment" / "sdk"
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


def _candidate_zkemkeeper_dirs(main_dll: Path | None = None) -> tuple[Path, ...]:
    dirs: list[Path] = []
    if main_dll is not None:
        dirs.append(main_dll.parent)
    dirs.extend(path.parent for path in _candidate_zkemkeeper_paths())
    return tuple(dict.fromkeys(path.resolve() for path in dirs))


def _register_zkemkeeper(dll_path: Path) -> bool:
    try:
        completed = subprocess.run(
            [_regsvr32_executable(), "/s", str(dll_path)],
            check=False,
            cwd=str(dll_path.parent),
            timeout=20,
        )
    except Exception:
        return False
    return completed.returncode == 0


def _regsvr32_executable() -> str:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    syswow64_regsvr32 = windir / "SysWOW64" / "regsvr32.exe"
    if syswow64_regsvr32.exists():
        return str(syswow64_regsvr32)
    return "regsvr32"


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
