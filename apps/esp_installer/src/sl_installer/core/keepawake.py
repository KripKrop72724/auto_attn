"""Keeps Windows awake while a board is set up and while its first start is watched.

A laptop on battery may sleep, or enter Modern Standby when its screen times out,
after a few idle minutes. USB is then suspended under esptool and the setup fails
halfway, or the replug is never seen. Serial traffic does not count as activity, so
the install (Installer.run) and the first-start watch (BootWatcher.wait_first_boot)
each ask Windows to stay awake while they run.

The request belongs to the calling thread (SetThreadExecutionState), so it is made
and cleared on the same worker thread, also when the work fails. The screen is kept
on too: on Modern Standby laptops a screen that times out starts standby even while
the system is required, and the person is following the screen meanwhile. On other
systems this does nothing.
"""
from __future__ import annotations

import os
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

_local = threading.local()


def _set_state(flags: int) -> bool:
    """SetThreadExecutionState(flags) for this thread; False where it does not apply."""
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


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold Windows awake on this thread until the block ends; nested blocks are fine."""
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
