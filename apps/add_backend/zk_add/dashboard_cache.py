"""Short-lived, single-flight cache for read-only dashboard figures.

The Fleet and Reconciliation pages poll the same heavy aggregate queries (the
outbox anti-join, status counts over every retained punch) on every realtime
event. Under a large force release or reconcile that turned into dozens of
identical scans a second. This cache lets one request compute a value while
other requests reuse it.

Rules, per key:

* A fresh value (younger than ``ttl_s``) is returned at once.
* Only one thread refreshes at a time. While it runs, other callers get the
  previous value if it is younger than ``stale_s`` (stale-while-revalidate);
  only a cold start (no usable value) waits, and for at most ``wait_s``.
* A failed refresh is never cached. With a usable previous value it is served
  and no new refresh starts for ``ttl_s`` (back-off), so a database that is
  timing out is not hit with the same heavy scans back to back. Callers that
  were already waiting when a refresh failed do not each retry it: they get the
  previous value, or :class:`DashboardCacheUnavailable`.
* :func:`invalidate` also discards a refresh that was already running when it
  was called, so a settings change cannot be overwritten by figures computed
  before it.
* :func:`mark_stale` is the cheap form for bulk operator actions (hundreds of
  releases a minute). It keeps the value and only marks it out of date: one
  later read refreshes it while the other readers keep getting the previous
  value (as for an expired TTL). Marks are coalesced: a value younger than
  ``min_age_s`` is still served, so a burst of marks costs at most one refresh
  per ``min_age_s``. A mark is never lost; a value whose compute started
  before the latest mark stays out of date, and a back-off after a failed
  refresh is kept.

Endpoints are synchronous FastAPI handlers, so they run in anyio's thread pool:
the guards are ``threading`` locks, not asyncio ones. The cache lives in the
process; ADD runs one uvicorn worker, so every browser shares it.

Only figures that tolerate being a few seconds old belong here. Operator
actions (Force release, recovery preview, repair) must query fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, TypeVar

from zk_add.time_utils import utc_now

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DashboardCacheUnavailable(RuntimeError):
    """No usable value: a cold-start refresh is still running or just failed."""


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    # monotonic clock, for TTL decisions
    stored_at: float
    # wall clock when the compute started, for reporting how old the figures are
    computed_at: datetime
    # monotonic time the compute started; a mark_stale() at or after it
    # means the value may predate the change (None: same as stored_at)
    started: float | None = None


@dataclass
class _KeyState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Bumped by invalidate(); a refresh stores its value only if unchanged.
    generation: int = 0
    # Bumped by every failed refresh, so waiters can tell one happened.
    failure_seq: int = 0
    last_error: BaseException | None = None
    # monotonic time before which a usable stale value is served without a refresh
    retry_after: float = 0.0
    # monotonic time of the latest mark_stale(), and how old a value must be
    # before that mark takes effect
    stale_mark: float | None = None
    stale_min_age: float = 0.0


# Indirection so tests can move time forward.
_clock: Callable[[], float] = time.monotonic

_entries: dict[str, CacheEntry] = {}
_states: dict[str, _KeyState] = {}
_registry_lock = threading.Lock()


def _state_for(key: str) -> _KeyState:
    with _registry_lock:
        state = _states.get(key)
        if state is None:
            state = _KeyState()
            _states[key] = state
        return state


def _fresh(entry: CacheEntry | None, ttl_s: float, state: _KeyState) -> bool:
    if entry is None:
        return False
    age = _clock() - entry.stored_at
    if age >= ttl_s:
        return False
    mark = state.stale_mark
    started = entry.stored_at if entry.started is None else entry.started
    # Marked after this value's compute began: out of date once old enough.
    return mark is None or mark < started or age < state.stale_min_age


def _usable(entry: CacheEntry | None, stale_s: float) -> bool:
    return entry is not None and _clock() - entry.stored_at < stale_s


def get_or_compute_entry(
    key: str,
    ttl_s: float,
    compute: Callable[[], T],
    stale_s: float = 120,
    wait_s: float = 30,
) -> CacheEntry:
    """Return the cached entry for ``key``, computing it at most once at a time.

    See the module docstring for the rules. Raises the ``compute`` error to the
    thread that ran it when there is no usable previous value, and
    :class:`DashboardCacheUnavailable` to a cold-start caller that waited more
    than ``wait_s`` or whose awaited refresh failed.
    """

    state = _state_for(key)
    entry = _entries.get(key)
    if _fresh(entry, ttl_s, state):
        return entry  # type: ignore[return-value]
    if _usable(entry, stale_s) and _clock() < state.retry_after:
        # A refresh failed moments ago; do not start another one yet.
        return entry  # type: ignore[return-value]

    lock = state.lock
    waited = False
    seen_failures = state.failure_seq
    if not lock.acquire(blocking=False):
        if _usable(entry, stale_s):
            # Someone else is refreshing: serve the previous value meanwhile.
            return entry  # type: ignore[return-value]
        waited = True
        if not lock.acquire(timeout=wait_s):
            raise DashboardCacheUnavailable(f"dashboard cache {key}: refresh still running")
    try:
        # Another thread may have refreshed the value while this one waited.
        entry = _entries.get(key)
        if _fresh(entry, ttl_s, state):
            return entry  # type: ignore[return-value]
        if waited and state.failure_seq != seen_failures:
            # The refresh this caller waited for failed; do not repeat it.
            if _usable(entry, stale_s):
                return entry  # type: ignore[return-value]
            raise DashboardCacheUnavailable(
                f"dashboard cache {key}: refresh failed"
            ) from state.last_error
        if _usable(entry, stale_s) and _clock() < state.retry_after:
            return entry  # type: ignore[return-value]

        with _registry_lock:
            generation = state.generation
        started_at = utc_now()
        started = _clock()
        try:
            value = compute()
        except Exception as error:
            state.failure_seq += 1
            state.last_error = error
            if _usable(entry, stale_s):
                state.retry_after = _clock() + ttl_s
                logger.warning(
                    "dashboard cache %s: refresh failed (%s); serving value computed at %s",
                    key,
                    type(error).__name__,
                    entry.computed_at.isoformat(),  # type: ignore[union-attr]
                )
                return entry  # type: ignore[return-value]
            raise
        state.last_error = None
        state.retry_after = 0.0
        new_entry = CacheEntry(
            value=value, stored_at=_clock(), computed_at=started_at, started=started
        )
        with _registry_lock:
            if _states.get(key) is state and state.generation == generation:
                _entries[key] = new_entry
            # Otherwise invalidate() ran during the compute: the value may
            # predate the change, so hand it to this caller only.
        return new_entry
    finally:
        lock.release()


def get_or_compute(
    key: str,
    ttl_s: float,
    compute: Callable[[], T],
    stale_s: float = 120,
    wait_s: float = 30,
) -> T:
    """Like :func:`get_or_compute_entry` but return only the value."""

    return get_or_compute_entry(key, ttl_s, compute, stale_s, wait_s).value


def invalidate(key: str | None = None) -> None:
    """Drop one key (or every key) so the next read recomputes.

    A refresh already running for that key keeps its result to itself.
    """

    with _registry_lock:
        keys = list(_states) if key is None else [key]
        if key is None:
            _entries.clear()
        else:
            _entries.pop(key, None)
        for name in keys:
            state = _states.get(name)
            if state is not None:
                state.generation += 1
                state.retry_after = 0.0


def mark_stale(key: str, min_age_s: float = 0.0) -> None:
    """Mark ``key`` out of date without dropping it (see the module docstring).

    O(1) and safe to call on every bulk mutation: readers keep the previous
    value while one of them refreshes it, at most once per ``min_age_s``.
    """

    state = _state_for(key)
    with _registry_lock:
        state.stale_mark = _clock()
        state.stale_min_age = min_age_s


def reset() -> None:
    """Test hook: forget every value and lock."""

    with _registry_lock:
        _entries.clear()
        _states.clear()
