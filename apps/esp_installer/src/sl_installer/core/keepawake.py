"""Keeps the computer awake while a board is set up and while its starts are watched.

A laptop on battery may sleep, or enter Modern Standby when its screen times out,
after a few idle minutes. USB is then suspended under esptool and the setup fails
halfway, or the replug is never seen. Serial traffic does not count as activity, so
the install (Installer.run) and each watched start (BootWatcher.wait_first_boot and
wait_restart) ask Windows to stay awake while they run.

The request belongs to the calling thread (SetThreadExecutionState), so it is made
and cleared on the same worker thread, also when the work fails. The screen is kept
on too: on Modern Standby laptops a screen that times out starts standby even while
the system is required, and the person is following the screen meanwhile.

On macOS the same request is a caffeinate process (-d display, -i idle, -s system on
power) that watches this app's own process id, so it ends with the app even after a
crash. Elsewhere this does nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

from .logging_setup import get_logger

log = get_logger("power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
RELEASED = ES_CONTINUOUS
CAFFEINATE = "/usr/bin/caffeinate"

_local = threading.local()


def _set_state(flags: int) -> bool:
    """SetThreadExecutionState(flags) for this thread; False where it does not apply."""
    if sys.platform == "darwin":
        return _caffeinate(flags == AWAKE)
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        call = kernel32.SetThreadExecutionState
        call.argtypes = [ctypes.c_uint32]
        call.restype = ctypes.c_uint32
        if call(flags):
            return True
        log.warning("Windows refused to stay awake (error %s)", ctypes.get_last_error())
    except (AttributeError, OSError) as error:
        log.warning("could not ask Windows to stay awake: %s", error)
    return False


def _caffeinate(awake: bool) -> bool:
    """Start (awake) or stop this thread's caffeinate; False when it cannot run."""
    running = getattr(_local, "caffeinate", None)
    if not awake:
        _local.caffeinate = None
        if running is not None and running.poll() is None:
            running.terminate()
            try:
                running.wait(timeout=2)
            except subprocess.TimeoutExpired:
                running.kill()
        return running is not None
    if running is not None and running.poll() is None:
        return True
    try:
        _local.caffeinate = subprocess.Popen(
            [CAFFEINATE, "-dis", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as error:
        log.warning("could not ask macOS to stay awake: %s", error)
        return False


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold the computer awake on this thread until the block ends; nested blocks are fine."""
    depth = getattr(_local, "depth", 0)
    if depth == 0:
        _set_state(AWAKE)
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth -= 1
        if _local.depth == 0:
            _set_state(RELEASED)
