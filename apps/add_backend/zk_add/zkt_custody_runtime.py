"""One independently scheduled custody inspector with observable useful progress.

An elapsed deadline never starts a replacement while the original transaction
may still own locks. PostgreSQL limits each statement and lock wait; the record
and time budgets stop between work groups. No network call belongs in a tick.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from zk_add.time_utils import utc_now
from zk_add.zkt_custody_work import InspectionBatch, advance_work_batch

IDLE_SECONDS = 2.0
BUSY_SECONDS = 0.05
STALLED_SECONDS = 5.0
BATCH_BUDGET_MS = 250


def inspection_tick(after_connector: int) -> InspectionBatch:
    from zk_add.db import session_scope

    with session_scope() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '5000ms'"))
        result = advance_work_batch(session, limit=100, after_connector=after_connector,
                                    time_budget_ms=BATCH_BUDGET_MS)
    return result  # Only a committed tick can advance the runtime cursor/progress.


class CustodyProcessor:
    def __init__(self, tick=None, *, clock=None):
        self._tick = tick or inspection_tick
        self._clock = clock or time.monotonic
        self._lock = Lock()
        self._running = False
        self._thread_started = False
        self._active_since: float | None = None
        self._last_completed: float | None = None
        self._cursor = 0
        self._failures = 0
        self._evidence = {
            "schema_version": 1, "instance_id": str(uuid4()), "state": "NOT_STARTED",
            "start_attempts": 0, "starts": 0, "started_at": None, "last_started_at": None,
            "last_completed_at": None, "last_progress_at": None,
            "last_failure_at": None, "last_failure_code": None, "active_error_code": None,
            "successful_ticks": 0, "failed_ticks": 0, "inspected_groups_total": 0,
            "last_batch_groups": None, "last_tick_ms": None, "max_tick_ms": 0,
            "last_attempted_connectors": None, "last_locked_connectors": None,
            "deadline_overruns": 0,
        }

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            result = dict(self._evidence)
            elapsed = None if self._active_since is None else max(0, int((now - self._active_since) * 1000))
            result.update(sampled_at=utc_now(), current_tick_elapsed_ms=elapsed,
                last_completion_age_ms=None if self._last_completed is None else max(0, int((now - self._last_completed) * 1000)),
                consecutive_failures=self._failures)
            if ((elapsed is not None and elapsed >= STALLED_SECONDS * 1000)
                    or (self._running and result["state"] in {"IDLE", "WAITING_FOR_LOCK"}
                        and result["last_completion_age_ms"] is not None
                        and result["last_completion_age_ms"] >= STALLED_SECONDS * 1000)):
                result["state"] = "STALLED"
            return result

    def _one_tick(self) -> tuple[int, str | None]:
        started = self._clock()
        with self._lock:
            if not self._thread_started:
                self._thread_started = True
                self._evidence.update(starts=self._evidence["starts"] + 1, started_at=utc_now())
            self._active_since = started
            self._evidence.update(state="RUNNING", last_started_at=utc_now())
            cursor = self._cursor
        error = None
        result = None
        try:
            result = self._tick(cursor)
        except SQLAlchemyError:
            error = "CUSTODY_DATABASE_UNAVAILABLE"
        except Exception:
            error = "CUSTODY_PROCESSING_FAILED"
        finished = self._clock()
        milliseconds = max(0, int((finished - started) * 1000))
        now = utc_now()
        with self._lock:
            self._active_since = None
            self._last_completed = finished
            self._evidence.update(last_completed_at=now, last_tick_ms=milliseconds,
                max_tick_ms=max(self._evidence["max_tick_ms"], milliseconds), active_error_code=error)
            if milliseconds > BATCH_BUDGET_MS:
                self._evidence["deadline_overruns"] += 1
            if error:
                self._failures += 1
                self._evidence.update(state="RETRYING", last_failure_at=now, last_failure_code=error,
                    failed_ticks=self._evidence["failed_ticks"] + 1, last_batch_groups=None,
                    last_attempted_connectors=None, last_locked_connectors=None)
                return 0, error
            self._cursor = result.after_connector
            self._failures = 0
            state = "WAITING_FOR_LOCK" if result.locked_connectors and not result.processed else "IDLE"
            self._evidence.update(state=state, successful_ticks=self._evidence["successful_ticks"] + 1,
                inspected_groups_total=self._evidence["inspected_groups_total"] + result.processed,
                last_batch_groups=result.processed, last_attempted_connectors=result.attempted_connectors,
                last_locked_connectors=result.locked_connectors)
            if result.processed:
                self._evidence["last_progress_at"] = now
            return result.processed, None

    async def run(self, stop: asyncio.Event, *, publish=None) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("Custody inspector is already running")
            self._running = True
            self._thread_started = False
            self._evidence.update(state="STARTING", start_attempts=self._evidence["start_attempts"] + 1)
        executor = None
        last_notice = float("-inf")
        previous_error = None
        try:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="add-zkt-custody")
            while not stop.is_set():
                pending = asyncio.get_running_loop().run_in_executor(executor, self._one_tick)
                cancelled = False
                while True:
                    try:
                        groups, error = await asyncio.shield(pending)
                        break
                    except asyncio.CancelledError:
                        # Defer even repeated cancellation until the transaction
                        # releases its locks. Keep the event loop responsive.
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
                now = self._clock()
                if publish and (error != previous_error or (groups and now - last_notice >= 1)):
                    try:
                        await publish("reconciliation", {
                            "code": error or "ZKT_CUSTODY_PROCESSOR_PROGRESS",
                            "message": "Custody processing evidence changed; refresh its independent status.",
                        })
                    except Exception:
                        pass  # Browser transport failure never rewinds a committed tick.
                    last_notice = now
                previous_error = error
                if error:
                    delay = min(60.0, IDLE_SECONDS * 2 ** min(self._failures - 1, 5))
                else:
                    delay = BUSY_SECONDS if groups else IDLE_SECONDS
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            with self._lock:
                self._evidence.update(active_error_code="CUSTODY_WORKER_STOPPED_UNEXPECTEDLY",
                    last_failure_code="CUSTODY_WORKER_STOPPED_UNEXPECTEDLY", last_failure_at=utc_now())
            raise
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._running = False
                self._active_since = None
                self._evidence["state"] = "STOPPED"


custody_processor = CustodyProcessor()


async def run_custody_processor(stop: asyncio.Event, *, publish=None, processor=None) -> None:
    """Retry an exited runtime only after it has joined its original thread."""
    processor = processor or custody_processor
    failures = 0
    while not stop.is_set():
        try:
            await processor.run(stop, publish=publish)
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            if publish:
                try:
                    await publish("reconciliation", {
                        "code": "ZKT_CUSTODY_WORKER_RESTART_PENDING",
                        "message": "The custody worker stopped and will retry after its transaction has exited.",
                    })
                except Exception:
                    pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60.0, IDLE_SECONDS * 2 ** min(failures - 1, 5)))
            except asyncio.TimeoutError:
                pass
        else:
            return
