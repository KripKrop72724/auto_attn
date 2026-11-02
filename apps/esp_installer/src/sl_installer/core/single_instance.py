"""One window of the app at a time.

Two windows would share one log file (on Windows the second one breaks its rotation,
so records are lost) and could talk to the same board at once. The lock is an
operating-system lock on a small file in the data folder: it goes away by itself
when the app exits or crashes, so a stale file never keeps the app from starting.
The file itself stays; it holds nothing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Optional

from .logging_setup import get_logger

log = get_logger("instance")

LOCK_FILE_NAME = "app.lock"


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Optional[IO[bytes]] = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """True when this process now holds the lock; False when another copy does."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError as error:  # closing the file releases it anyway
            log.info("could not unlock %s: %s", self.path.name, error)
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _lock(handle: IO[bytes]) -> None:
    """Lock the file's first byte without waiting; OSError when someone else has it."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
