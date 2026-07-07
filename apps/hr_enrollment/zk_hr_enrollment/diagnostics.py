from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from zk_hr_enrollment import BRAND_NAME


def diagnostic_log_path() -> Path:
    base = _diagnostic_base_dir()
    return base / BRAND_NAME / "HR Enrollment" / "logs" / "hr_enrollment.log"


def log_exception(
    context: str,
    exc: BaseException,
    tb: TracebackType | None = None,
) -> Path | None:
    try:
        path = diagnostic_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
        timestamp = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{timestamp}] {context}\n")
            handle.write(rendered)
        return path
    except Exception:
        return None


def friendly_exception_message(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError):
        module_name = getattr(exc, "name", None) or "unknown module"
        return (
            f"The application is missing a bundled runtime component: {module_name}. "
            "Use the latest HR Enrollment EXE built by the shipping workflow."
        )
    if isinstance(exc, ImportError):
        return (
            "The application could not load one of its runtime components. "
            "Use the latest HR Enrollment EXE built by the shipping workflow."
        )
    if isinstance(exc, ValueError):
        return str(exc) or "The entered employee information is invalid."
    if type(exc).__name__ == "ZKCommunicationError":
        return (
            "The selected ZKT device did not respond reliably. If an ESP32 or Zone Agent is "
            "attached to this device, pause it during HR enrollment, then search the employee again."
        )
    if isinstance(exc, TimeoutError):
        return (
            "The selected ZKT device did not respond in time. Check power/network, and pause any "
            "ESP32 or Zone Agent that is already connected to the device."
        )
    if isinstance(exc, OSError):
        return f"Windows or the network rejected the operation: {exc}"
    if isinstance(exc, RuntimeError):
        return str(exc) or "The operation could not be completed."
    return "The application hit an unexpected problem. Close and reopen it, then try the action again."


def message_with_log(message: str, log_path: Path | None) -> str:
    if not log_path:
        return message
    return f"{message}\n\nDiagnostic log:\n{log_path}"


def _diagnostic_base_dir() -> Path:
    for env_name in ("PROGRAMDATA", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            return Path(value)
    try:
        return Path.home()
    except Exception:
        return Path(sys.argv[0]).resolve().parent
