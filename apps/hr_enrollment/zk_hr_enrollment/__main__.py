from __future__ import annotations

import ctypes
import importlib
import platform
import sys

from zk_hr_enrollment import BRAND_NAME
from zk_hr_enrollment.diagnostics import (
    friendly_exception_message,
    log_exception,
    message_with_log,
)


CRITICAL_MODULES = (
    "tkinter",
    "psutil",
    "zk",
    "zk_common",
    "zk_zone_agent.network_scanner",
    "zk_hr_enrollment.app",
    "zk_hr_enrollment.zkt",
)

WINDOWS_CRITICAL_MODULES = (
    "psutil._psutil_common",
    "psutil._psutil_windows",
    "win32timezone",
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--health-check" in args:
        return _run_health_check()
    try:
        from zk_hr_enrollment.app import run_app

        run_app()
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        return 1
    except Exception as exc:
        log_path = log_exception("startup", exc)
        _show_startup_error(message_with_log(friendly_exception_message(exc), log_path))
        return 1


def _run_health_check() -> int:
    modules = list(CRITICAL_MODULES)
    if platform.system() == "Windows":
        modules.extend(WINDOWS_CRITICAL_MODULES)
    try:
        for module_name in modules:
            importlib.import_module(module_name)
        return 0
    except Exception as exc:
        log_exception("health-check", exc)
        return 1


def _show_startup_error(message: str) -> None:
    title = f"{BRAND_NAME} - Startup problem"
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        return


if __name__ == "__main__":
    raise SystemExit(main())
