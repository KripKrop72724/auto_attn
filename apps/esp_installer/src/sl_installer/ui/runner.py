"""Runs blocking engine calls on worker threads and brings results back.

The engine is blocking and may call our callbacks from its own threads. The
screens may only be touched from the GUI thread, so everything that comes
back from a worker goes through ``post``: a queued signal into a slot on this
object, which lives on the GUI thread. The GUI thread never waits for a
worker.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Optional, Set

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot

from ..core import api

log = logging.getLogger(__name__)

# Long watches (USB, console) hold a thread each for minutes, so the pool
# must be bigger than the number of watches that can run at once.
MAX_THREADS = 8


class Operation:
    """One piece of background work the screens care about.

    ``current`` turns False when the work is cancelled or replaced, so late
    results and callbacks from it are dropped instead of drawn.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.token = api.CancelToken()
        self.current = True

    def stop(self) -> None:
        self.current = False
        self.token.cancel()


class _Job(QRunnable):
    def __init__(
        self,
        runner: "Runner",
        work: Callable[[], Any],
        on_done: Optional[Callable[[Any], None]],
        on_error: Optional[Callable[[BaseException], None]],
        operation: Optional[Operation],
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._runner = runner
        self._work = work
        self._on_done = on_done
        self._on_error = on_error
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._work()
        except BaseException as error:  # handed to the screen, never lost in a thread
            callback, value = self._on_error, error
            if not isinstance(error, api.InstallerError):
                log.error(
                    "background work failed", exc_info=(type(error), error, error.__traceback__)
                )
        else:
            callback, value = self._on_done, result
        self._runner.post(
            functools.partial(self._runner._finish, self, callback, value, self._operation)
        )


class Runner(QObject):
    """Starts work on a private thread pool and hands results to the GUI thread."""

    _call = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_THREADS)
        self._jobs: Set[_Job] = set()
        self._call.connect(self._run_call, Qt.ConnectionType.QueuedConnection)

    # --------------------------------------------------------------- posting

    def post(self, function: Callable[[], None]) -> None:
        """Run ``function`` on the GUI thread soon. Safe from any thread."""
        self._call.emit(function)

    def relay(
        self, function: Callable[..., None], operation: Optional[Operation] = None
    ) -> Callable[..., None]:
        """Wrap a GUI callback so the engine can call it from any thread.

        Calls made after ``operation`` stopped are dropped.
        """

        def relayed(*args: Any) -> None:
            if operation is not None and not operation.current:
                return
            self.post(functools.partial(self._call_if_current, function, args, operation))

        return relayed

    @staticmethod
    def _call_if_current(
        function: Callable[..., None], args: tuple, operation: Optional[Operation]
    ) -> None:
        if operation is None or operation.current:
            function(*args)

    @Slot(object)
    def _run_call(self, function: Callable[[], None]) -> None:
        function()

    # ----------------------------------------------------------------- work

    def start(
        self,
        work: Callable[[], Any],
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        operation: Optional[Operation] = None,
    ) -> None:
        """Run ``work()`` on a worker, then ``on_done(result)`` or ``on_error(exc)`` on the
        GUI thread.

        When ``operation`` has stopped by then, neither is called.
        """
        job = _Job(self, work, on_done, on_error, operation)
        self._jobs.add(job)
        self._pool.start(job)

    def _finish(
        self,
        job: _Job,
        callback: Optional[Callable[[Any], None]],
        value: Any,
        operation: Optional[Operation],
    ) -> None:
        self._jobs.discard(job)
        if callback is None or (operation is not None and not operation.current):
            return
        callback(value)

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    def wait(self, msecs: int) -> bool:
        """Wait for workers to end. Only for shutdown, after the window is gone."""
        return self._pool.waitForDone(msecs)
