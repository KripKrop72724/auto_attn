from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.orm import Session

from zk_zone_agent.db import session_scope

T = TypeVar("T")


class DatabaseWriteQueue:
    """Single-writer queue for device workers and sync side effects."""

    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.items: queue.Queue[tuple[Callable[[Session], object], queue.Queue[object]]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="sqlite-write-queue", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, fn: Callable[[Session], T], timeout: float | None = 10) -> T:
        result_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self.items.put((fn, result_queue))
        result = result_queue.get(timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                fn, result_queue = self.items.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with session_scope() as session:
                    result_queue.put(fn(session))
            except Exception as exc:
                result_queue.put(exc)
