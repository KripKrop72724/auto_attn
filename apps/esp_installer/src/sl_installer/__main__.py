"""Start the State Life Device Installer.

    State Life Device Installer.exe                the app
    ... --tool esptool|espefuse ARGS...            run a bundled tool and exit with its code
    ... --self-test                                check the engine and the window, print
                                                   any problems, exit 0 (fine) or 1
    ... --screenshots DIR                          draw every screen into DIR as PNG files
    ... --server URL                               use another ADD server (tests only)

The frozen app is one executable, so it also stands in for esptool and
espefuse: the engine starts ``<exe> --tool esptool ...`` exactly where a
source checkout would start ``python -m esptool ...``. Everything heavy is
imported only by the mode that needs it, so a tool run never loads Qt.

Only one window runs at a time: a second start says the app is already open and
stops (core/single_instance.py). Tool runs and the self-test are not limited.
While the window is open on Windows the app also holds the named mutex
``APP_MUTEX``: Setup and the uninstaller (packaging/installer.iss, AppMutex) then
ask for the app to be closed instead of deleting files under a running setup.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Sequence

# How long the self-test lets the window run before closing it.
SMOKE_SECONDS = 2.0
# packaging/installer.iss names the same mutex in AppMutex.
APP_MUTEX = "StateLifeDeviceInstaller"


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Must come first: a frozen multiprocessing child has to stop here.
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    _give_missing_streams()
    if args[:1] == ["--tool"]:
        return _run_tool(args[1:])
    parser = _parser()
    options = parser.parse_args(args)
    if options.server is not None:
        from sl_installer.config import load_config

        try:
            load_config(server_url=options.server)
        except ValueError as error:
            parser.error(f"--server: {error}")
    if options.self_test:
        return _self_test(options.server)
    if options.screenshots is not None:
        return _screenshots(options.screenshots)
    return _run_gui(options.server)


def _parser() -> argparse.ArgumentParser:
    from sl_installer import __version__

    parser = argparse.ArgumentParser(
        prog="State Life Device Installer",
        description="Prepares and checks Zone Lite boards for State Life attendance machines.",
        epilog="--tool esptool|espefuse ARGS... runs a bundled tool (it must come first).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--self-test", action="store_true",
                      help="check the engine and the window, then exit 0 (fine) or 1")
    mode.add_argument("--screenshots", metavar="DIR", type=Path,
                      help="save a picture of every screen in DIR, then exit")
    parser.add_argument("--server", metavar="URL",
                        help="use this ADD server instead of head office (tests only)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _give_missing_streams() -> None:
    """A windowed Windows program has no stdout/stderr unless its parent passed
    some; libraries that write to them would crash, so they write nowhere."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _run_tool(args: List[str]) -> int:
    try:
        from sl_installer.core.esptool_runner import dispatch_tool

        return dispatch_tool(args)
    except Exception:
        # Report and exit: a windowed child must never stop at an error dialog.
        traceback.print_exc()
        return 1


def _self_test(server: Optional[str]) -> int:
    problems: List[str] = []
    try:
        from sl_installer.config import load_config
        from sl_installer.core import selftest

        problems.extend(selftest.run(load_config(server_url=server)))
    except Exception:
        problems.append("the engine self-test crashed:\n" + traceback.format_exc())
    try:
        _smoke_test_window()
    except Exception:
        problems.append("the window could not be opened and closed:\n" + traceback.format_exc())
    try:
        _smoke_test_services(server)
    except Exception:
        problems.append("the app's own services could not be built:\n" + traceback.format_exc())
    for problem in problems:
        print(f"PROBLEM: {problem}")
    if problems:
        print(f"Self-test failed: {len(problems)} problem(s).", flush=True)
        return 1
    print("Self-test passed.", flush=True)
    return 0


def _smoke_test_window() -> None:
    """Open the real window on scripted services, let it run, then close it."""
    from sl_installer.core.fakes import make_fake_services
    from sl_installer.ui.app import ensure_app
    from sl_installer.ui.window import MainWindow

    app = ensure_app()
    window = MainWindow(make_fake_services("happy_zkt"))
    window.show()
    window.controller.start()
    deadline = time.monotonic() + SMOKE_SECONDS
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    window.controller.shutdown()
    window.close()
    window.controller.runner.wait(3000)
    app.processEvents()
    print("Window opened and closed.", flush=True)


def _smoke_test_services(server: Optional[str]) -> None:
    """Build the services the app really runs on, and a window on them.

    The window is never started: starting checks the internet and head office,
    which the scripted window above covers, so CI never contacts production.
    This catches a module or file the bundle lacks only on the real path.
    """
    from sl_installer.config import load_config
    from sl_installer.core.factory import make_services
    from sl_installer.ui.app import ensure_app
    from sl_installer.ui.window import MainWindow

    app = ensure_app()
    window = MainWindow(make_services(load_config(server_url=server)))
    window.close()
    window.controller.runner.wait(3000)
    app.processEvents()
    print("Real services built.", flush=True)


def _screenshots(directory: Path) -> int:
    try:
        from sl_installer.ui.screenshots import capture_all

        saved = capture_all(str(directory))
    except Exception:
        traceback.print_exc()
        return 1
    print(f"Saved {len(saved)} screenshots in {directory}", flush=True)
    return 0 if saved else 1


def _run_gui(server: Optional[str]) -> int:
    from sl_installer.config import load_config
    from sl_installer.core.factory import make_services
    from sl_installer.core.installer import cleanup_stale_work
    from sl_installer.core.single_instance import LOCK_FILE_NAME, InstanceLock
    from sl_installer.ui.app import run_gui

    cfg = load_config(server_url=server)
    # Taken before make_services, which opens the log file a second window must not share.
    lock = InstanceLock(cfg.state_dir / LOCK_FILE_NAME)
    if not lock.acquire():
        return _already_open()
    mutex: Optional[int] = None
    try:
        services = make_services(cfg)
        # Every install runs in a holder of this lock, so none is running now: wipe the
        # settings and key files of any setup a crash or power cut stopped, however new.
        cleanup_stale_work(cfg.work_dir, max_age_s=0)
        mutex = _hold_app_mutex()
        return run_gui(services)
    finally:
        _release_app_mutex(mutex)
        lock.release()


def _hold_app_mutex() -> Optional[int]:
    """Create APP_MUTEX for as long as the window is open; Windows only, else None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        handle = kernel32.CreateMutexW(None, False, APP_MUTEX)
    except Exception as error:  # Setup then only has its own way of closing the app
        import logging

        logging.getLogger("sl_installer").warning("could not create the app mutex: %s", error)
        return None
    return handle or None


def _release_app_mutex(handle: Optional[int]) -> None:
    if not handle:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle(handle)


def _already_open() -> int:
    """What a second copy of the app shows before it stops."""
    from PySide6.QtWidgets import QMessageBox

    from sl_installer.ui.app import ensure_app
    from sl_installer.ui.window import TITLE

    ensure_app()
    QMessageBox.information(
        None, TITLE,
        "The app is already open.\n\nUse the window that is open. If you cannot see it, "
        "click its icon on the taskbar at the bottom of the screen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
