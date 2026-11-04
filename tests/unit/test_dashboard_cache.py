"""The overview's heavy figures are computed once and shared for a few seconds.

Production (4 Nov 2026) re-ran the outbox anti-join and the full status scan
on every realtime event from every open browser. These tests pin the
single-flight cache, its TTL and stale-on-error fallback, and that the overview
and recovery summary keep their response shape while sharing one computation.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from test_ords_outbox_coverage import _fleet, db  # noqa: F401  (db is a fixture)
from zk_add import dashboard_cache, web
from zk_add.attendance_recovery import recovery_summary
from zk_add.db import Base
from zk_add.models import AttendanceEvent
from zk_add.ords_outbox_coverage import event_ords_status_counts, ords_outbox_coverage
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.service import fleet_counts
from zk_add.settings import settings
from zk_add.web import app, get_db
from zk_add.worker import ords_delivery_metrics


@pytest.fixture(autouse=True)
def _fresh_cache():
    dashboard_cache.reset()
    yield
    dashboard_cache.reset()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    value = _Clock()
    monkeypatch.setattr(dashboard_cache, "_clock", value)
    return value


def _busy() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("canceling statement due to statement timeout"))


def test_concurrent_misses_run_one_compute_and_share_its_value():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def compute():
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return {"value": 42}

    results = []

    def reader():
        results.append(dashboard_cache.get_or_compute("k", 15, compute))

    first = threading.Thread(target=reader)
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=reader)
    second.start()
    # The second reader is now blocked on the key's lock, not computing.
    second.join(0.2)
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert len(calls) == 1
    assert results == [{"value": 42}, {"value": 42}]
    assert results[0] is results[1]


def test_value_is_reused_within_ttl_and_recomputed_after(clock):
    calls = []

    def compute():
        calls.append(clock.now)
        return len(calls)

    assert dashboard_cache.get_or_compute("k", 15, compute) == 1
    clock.now += 14.9
    assert dashboard_cache.get_or_compute("k", 15, compute) == 1
    clock.now += 0.2
    assert dashboard_cache.get_or_compute("k", 15, compute) == 2
    assert len(calls) == 2
    # Keys are independent.
    assert dashboard_cache.get_or_compute("other", 15, compute) == 3


def test_failed_refresh_serves_a_recent_value_and_then_fails(clock):
    assert dashboard_cache.get_or_compute("k", 15, lambda: "good", stale_s=120) == "good"
    computed_at = dashboard_cache.get_or_compute_entry("k", 15, lambda: "unused").computed_at

    def failing():
        raise _busy()

    clock.now += 60
    entry = dashboard_cache.get_or_compute_entry("k", 15, failing, stale_s=120)
    assert entry.value == "good"
    assert entry.computed_at == computed_at
    clock.now += 61  # now 121 s old: too stale to serve
    with pytest.raises(OperationalError):
        dashboard_cache.get_or_compute("k", 15, failing, stale_s=120)
    # A failure is not cached: the next caller computes again.
    assert dashboard_cache.get_or_compute("k", 15, lambda: "recovered") == "recovered"


def test_failure_without_a_previous_value_propagates():
    def failing():
        raise _busy()

    with pytest.raises(OperationalError):
        dashboard_cache.get_or_compute("k", 15, failing)
    assert dashboard_cache.get_or_compute("k", 15, lambda: 7) == 7


def test_invalidate_forces_a_recompute():
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    assert dashboard_cache.get_or_compute("k", 15, compute) == 1
    dashboard_cache.invalidate("k")
    assert dashboard_cache.get_or_compute("k", 15, compute) == 2
    dashboard_cache.invalidate()
    assert dashboard_cache.get_or_compute("k", 15, compute) == 3


def _prime_stale(clock: _Clock, key: str = "k") -> None:
    assert dashboard_cache.get_or_compute(key, 15, lambda: "good", stale_s=120) == "good"
    clock.now += 20  # past the TTL, well inside stale_s


def test_failing_refresh_with_a_stale_value_runs_once_for_many_callers(clock):
    """Five concurrent callers and a failing refresh: one compute, everyone served."""
    _prime_stale(clock)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def failing():
        calls.append(1)
        started.set()
        assert release.wait(5)
        raise _busy()

    results = []

    def reader():
        results.append(dashboard_cache.get_or_compute("k", 15, failing, stale_s=120))

    threads = [threading.Thread(target=reader) for _ in range(5)]
    threads[0].start()
    assert started.wait(5)
    for thread in threads[1:]:
        thread.start()
    # The other four do not wait for the refresh: they get the stale value now.
    for thread in threads[1:]:
        thread.join(2)
        assert not thread.is_alive()
    release.set()
    threads[0].join(5)
    assert results == ["good"] * 5
    assert len(calls) == 1
    # Back-off: within the TTL after the failure nobody refreshes again.
    clock.now += 14
    assert dashboard_cache.get_or_compute("k", 15, failing, stale_s=120) == "good"
    assert len(calls) == 1
    clock.now += 2
    assert dashboard_cache.get_or_compute("k", 15, lambda: "fresh", stale_s=120) == "fresh"


def test_a_stale_value_is_served_at_once_while_another_thread_refreshes(clock):
    _prime_stale(clock)
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
        return "new"

    refresher = threading.Thread(target=lambda: dashboard_cache.get_or_compute("k", 15, slow))
    refresher.start()
    assert started.wait(5)
    began = time.monotonic()
    assert dashboard_cache.get_or_compute("k", 15, slow) == "good"
    assert time.monotonic() - began < 1
    release.set()
    refresher.join(5)
    assert dashboard_cache.get_or_compute("k", 15, slow) == "new"


def test_cold_start_waiters_do_not_repeat_a_failed_refresh():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def failing():
        calls.append(1)
        started.set()
        assert release.wait(5)
        raise _busy()

    outcomes = []

    def reader():
        try:
            dashboard_cache.get_or_compute("k", 15, failing)
            outcomes.append("value")
        except OperationalError:
            outcomes.append("own-error")
        except dashboard_cache.DashboardCacheUnavailable as error:
            assert isinstance(error.__cause__, OperationalError)
            outcomes.append("unavailable")

    first = threading.Thread(target=reader)
    first.start()
    assert started.wait(5)
    waiters = [threading.Thread(target=reader) for _ in range(4)]
    for thread in waiters:
        thread.start()
    for thread in waiters:
        thread.join(0.1)
        assert thread.is_alive()  # no usable value: they wait
    release.set()
    for thread in [first, *waiters]:
        thread.join(5)
    assert len(calls) == 1
    assert sorted(outcomes) == ["own-error"] + ["unavailable"] * 4
    # A later caller (not one that waited) tries again.
    assert dashboard_cache.get_or_compute("k", 15, lambda: 3) == 3


def test_cold_start_wait_is_bounded():
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
        return 1

    refresher = threading.Thread(target=lambda: dashboard_cache.get_or_compute("k", 15, slow))
    refresher.start()
    assert started.wait(5)
    with pytest.raises(dashboard_cache.DashboardCacheUnavailable):
        dashboard_cache.get_or_compute("k", 15, slow, wait_s=0.1)
    release.set()
    refresher.join(5)
    assert dashboard_cache.get_or_compute("k", 15, slow) == 1


def test_invalidate_discards_a_refresh_that_was_already_running():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            return "before-change"
        return "after-change"

    results = []
    refresher = threading.Thread(target=lambda: results.append(dashboard_cache.get_or_compute("k", 15, compute)))
    refresher.start()
    assert started.wait(5)
    dashboard_cache.invalidate("k")
    release.set()
    refresher.join(5)
    # The caller that started it still gets its value, but it is not cached.
    assert results == ["before-change"]
    assert dashboard_cache.get_or_compute("k", 15, compute) == "after-change"
    assert len(calls) == 2


def test_shared_status_counts_match_each_summarys_own_queries(db: Session):  # noqa: F811
    _fleet(db)
    # A lower-case stored status is the same tile for the recovery summary
    # (normalised) but is not BLOCKED_IDENTITY for the delivery backlog, which
    # compares the raw stored value.
    rows = db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.id)).all()
    rows[-1].ords_status = "blocked_identity"
    db.flush()
    coverage = ords_outbox_coverage(db)
    counts = event_ords_status_counts(db)
    assert counts.get("blocked_identity") == 1
    shared = ords_delivery_metrics(db, outbox_coverage=coverage, event_status_counts=counts)
    assert shared == ords_delivery_metrics(db)
    assert shared["blocked_identity"] == counts.get("BLOCKED_IDENTITY", 0)
    summary = recovery_summary(db, outbox_coverage=coverage, event_status_counts=counts)
    assert summary == recovery_summary(db)
    assert summary["delivery"]["status_counts"]["BLOCKED_IDENTITY"] == (
        counts.get("BLOCKED_IDENTITY", 0) + 1
    )


@pytest.fixture
def shared_engine_client(tmp_path, monkeypatch):
    """A client whose request sessions use the application's own sessionmaker,

    so the overview takes the cached path exactly as in production. A file
    database gives the metrics session its own connection.
    """

    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'add.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    with sessions() as setup:
        _fleet(setup)
        raw_session, _admin = create_admin_session(
            setup, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
        )
        setup.commit()

    def override_db():
        session = sessions()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    try:
        yield client, sessions
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_overview_shares_one_cached_computation_and_keeps_its_shape(shared_engine_client, monkeypatch):
    client, sessions = shared_engine_client
    calls = []

    def counted(session):
        calls.append(session)
        return ords_outbox_coverage(session)

    monkeypatch.setattr(web, "ords_outbox_coverage", counted)
    first = client.get("/api/v1/overview")
    second = client.get("/api/v1/overview")
    summary = client.get("/api/v2/attendance-recovery/summary")
    assert first.status_code == second.status_code == summary.status_code == 200
    assert len(calls) == 1, "overview and recovery summary must share one computation"

    body = first.json()
    with sessions() as session:
        expected_keys = set(fleet_counts(session)) | set(web._dashboard_metrics(session))
    calls.clear()  # the live computation above was this test's own
    assert set(body) == expected_keys | {"metrics_computed_at", "metrics_max_age_seconds"}
    assert datetime.fromisoformat(body["metrics_computed_at"]).tzinfo is not None
    assert body["metrics_max_age_seconds"] == web.DASHBOARD_METRICS_TTL_SECONDS
    assert second.json()["metrics_computed_at"] == body["metrics_computed_at"]
    for section in (body["ords_delivery"], body["attendance_recovery"]["delivery"]):
        assert section["missing_outbox"] == 0
        assert section["held_without_outbox"] == 1
        assert section["review_without_outbox"] == 2

    recovery = summary.json()
    assert recovery["delivery"] == body["attendance_recovery"]["delivery"]
    assert recovery["schema_version"] == "1"
    assert recovery["metrics_computed_at"] == body["metrics_computed_at"]
    assert recovery["preview_enabled"] == settings.attendance_recovery_preview_enabled

    # Feature flags stay live while the heavy figures are cached.
    flipped_flag = not settings.attendance_recovery_preview_enabled
    monkeypatch.setattr(settings, "attendance_recovery_preview_enabled", flipped_flag)
    flipped = client.get("/api/v2/attendance-recovery/summary").json()
    assert flipped["preview_enabled"] is flipped_flag
    assert calls == [], "a cached summary must not recompute"


def test_overview_serves_recent_figures_when_a_refresh_fails(shared_engine_client, monkeypatch, clock):
    client, _sessions = shared_engine_client
    good = client.get("/api/v1/overview").json()

    def busy(_session):
        raise _busy()

    monkeypatch.setattr(web, "ords_outbox_coverage", busy)
    clock.now += web.DASHBOARD_METRICS_TTL_SECONDS + 1
    stale = client.get("/api/v1/overview")
    assert stale.status_code == 200
    assert stale.json()["metrics_computed_at"] == good["metrics_computed_at"]
    assert stale.json()["ords_delivery"] == good["ords_delivery"]


def test_request_sessions_on_another_engine_compute_live(db: Session, monkeypatch):  # noqa: F811
    """Tests and tools that bind a different engine never touch the process cache."""
    _fleet(db)
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()

    def override_db():
        yield db

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a foreign engine must not use the shared cache")

    monkeypatch.setattr(dashboard_cache, "get_or_compute_entry", forbidden)
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    body = client.get("/api/v1/overview").json()
    assert body["metrics_max_age_seconds"] == 0
    assert "ords_delivery" in body and "checkin_notifications" in body
    summary = client.get("/api/v2/attendance-recovery/summary").json()
    assert summary["metrics_max_age_seconds"] == 0
    assert summary["delivery"]["held_without_outbox"] == 1


def test_operator_actions_invalidate_the_figures_only_after_commit(shared_engine_client):
    client, sessions = shared_engine_client
    assert client.get("/api/v1/overview").status_code == 200
    assert web.DASHBOARD_METRICS_KEY in dashboard_cache._entries

    with sessions() as session:
        web.invalidate_dashboard_metrics_after_commit(session)
        session.rollback()
    assert web.DASHBOARD_METRICS_KEY in dashboard_cache._entries, "a rolled-back action keeps the figures"

    with sessions() as session:
        web.invalidate_dashboard_metrics_after_commit(session)
        session.commit()
    assert web.DASHBOARD_METRICS_KEY not in dashboard_cache._entries


def test_release_and_recovery_endpoints_register_the_invalidation():
    import inspect

    for handler in (
        web.quick_release_direct_ords,
        web.start_direct_ords_run,
        web.recheck_direct_ords_legacy,
        web.start_identity_backlog_release,
        web.stop_identity_backlog_release,
        web.create_force_check,
        web.start_force_run,
        web.control_force_run,
        web.control_attendance_recovery_job,
    ):
        assert "invalidate_dashboard_metrics_after_commit(db)" in inspect.getsource(handler), handler.__name__


def test_overview_answers_503_when_no_figures_are_available(shared_engine_client, monkeypatch):
    client, _sessions = shared_engine_client

    def unavailable(*_args, **_kwargs):
        raise dashboard_cache.DashboardCacheUnavailable("refresh still running")

    monkeypatch.setattr(dashboard_cache, "get_or_compute_entry", unavailable)
    for path in ("/api/v1/overview", "/api/v2/attendance-recovery/summary"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
