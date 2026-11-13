"""The owner's switch keeps unknown-identity punches releasing, paced and bounded.

Runs on SQLite and, when ADD_SAFE_REPAIR_TEST_DATABASE_URL is set, on
PostgreSQL. Batch selection, pacing, backpressure and the retry window are
pinned with a stand-in for quick_release that records which punches it was
handed; one test runs the real quick_release end to end.
"""

from __future__ import annotations

from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
import inspect
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import MetaData, create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.pool import StaticPool

import test_attendance_repair
from test_attendance_force_release import store as store  # noqa: F401 - fixture
from test_attendance_repair import repair_store as repair_store  # noqa: F401 - fixture
from zk_add import attendance_direct_ords as direct
from zk_add import attendance_force_release as force_release
from zk_add import attendance_safe_repair as safe_repair
from zk_add import attendance_unknown_auto_release as auto
from zk_add import worker
from zk_add.attendance_recovery import RecoveryError, sign_recovery_preview
from zk_add.crypto import decrypt_text
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    AuditEvent,
    UnknownIdentityAutoRelease as State,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.settings import AddSettings, settings
from zk_add.time_utils import utc_now
from zk_add.web import app, get_db

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "apps/add_backend/migrations/versions"
MIGRATION_FILE = VERSIONS / "20261105_0062_unknown_identity_auto_release.py"
PASSWORD = "auto-release-test-password"


def _postgres_url() -> str | None:
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    return url if url and url.startswith("postgresql") else None


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request, monkeypatch):
    """The direct-send test store on SQLite or in a fresh PostgreSQL schema."""
    if request.param == "postgresql":
        url = _postgres_url()
        if url is None:
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "unknown_auto_release_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engines = []

        def pg_engine(*_args, **_kwargs):
            engine = create_engine(url, connect_args={
                "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
            engines.append(engine)
            return engine

        def drop():
            for engine in engines:
                engine.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

        request.addfinalizer(drop)
        monkeypatch.setattr(test_attendance_repair, "create_engine", pg_engine)
        # PostgreSQL DDL marks cyclic foreign keys use_alter on the metadata it
        # creates; build a copy so later SQLite create_all calls keep them.
        copy = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(copy)
        monkeypatch.setattr(Base.metadata, "create_all", lambda bind, **_kwargs: copy.create_all(bind))
    sessions, _connector_id, _uid = request.getfixturevalue("store")
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password(PASSWORD))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_allowed", True)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_batch_size", 50)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_min_interval_seconds", 5)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_in_flight_limit", 1500)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_retry_hours", 24)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_count_interval_seconds", 60)
    yield sessions
    app.dependency_overrides.clear()


def unknown_punches(sessions, count: int) -> list[int]:
    """The store's punch plus copies: held, no name or CNIC saved, one synced user with a CNIC."""
    with sessions() as db:
        first = db.scalar(select(AttendanceEvent).order_by(AttendanceEvent.id))
        first.cnic_encrypted = first.cnic_lookup_hash = None
        first.display_name = None
        first.ords_status = "BLOCKED_IDENTITY"
        columns = [
            column.key for column in AttendanceEvent.__table__.columns
            if column.key not in {"id", "event_uid"}
        ]
        for number in range(1, count):
            copy = AttendanceEvent(**{key: getattr(first, key) for key in columns})
            copy.event_uid = f"{number:064x}"
            copy.device_event_time = first.device_event_time + timedelta(minutes=number)
            db.add(copy)
        db.commit()
        return list(db.scalars(select(AttendanceEvent.id).order_by(AttendanceEvent.id)).all())


class FakeRelease:
    """Stands in for quick_release: one run whose items get the planned status."""

    def __init__(self, monkeypatch, *, statuses=None, nothing=False, during=None):
        self.calls: list[list[int]] = []
        self.statuses = statuses or {}
        self.nothing = nothing
        self.during = during
        monkeypatch.setattr(direct, "quick_release", self)

    def __call__(self, session, *, actor, event_ids, idempotency_key):
        self.calls.append(list(event_ids))
        if self.during is not None:
            self.during(session)
        if self.nothing:
            raise RecoveryError("gone", "NOTHING_TO_RELEASE")
        statuses = [self.statuses.get(event_id, "READY") for event_id in event_ids]
        job = Job(
            action=direct.ACTION, actor=actor, reason="test", idempotency_key=idempotency_key,
            status="RUNNING" if "READY" in statuses else "COMPLETED_WITH_REVIEW",
            scope={"selected_event_ids": sorted(event_ids)}, candidate_digest="d" * 64,
            requested_count=len(event_ids),
        )
        session.add(job)
        session.flush()
        for event_id, status in zip(event_ids, statuses):
            session.add(Item(
                job_id=job.id, source_kind="ATTENDANCE_EVENT", source_ref=str(event_id),
                attendance_event_id=event_id, expected_state_digest="e" * 64, lane="DIRECT_ORDS",
                status=status, error_code="CNIC_CONFLICT" if status == "SKIPPED" else None, result={},
            ))
        session.flush()
        return job, 0


def step(sessions, now=None) -> str:
    with sessions() as db:
        outcome = auto.run_once(db, now=now or utc_now())
        db.commit()
        return outcome


def state(sessions) -> State:
    with sessions() as db:
        return db.get(State, 1)


def client_for(db):
    raw_session, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client, {"X-CSRF-Token": admin.csrf_token}


# Switch ------------------------------------------------------------------------


def test_missing_row_means_on_and_status_has_the_panel_shape(backend):
    sessions = backend
    with sessions() as db:
        assert db.get(State, 1) is None
        client, _headers = client_for(db)
        body = client.get("/api/v2/unknown-identity-auto-release").json()
    assert body["allowed"] is True and body["enabled"] is True and body["active"] is True
    assert body["drain_active"] is False and body["changed_by"] is None and body["reason"] is None
    assert body["backlog"] == {
        "count": None, "counted_at": None, "remaining_estimate": None, "held_by_other_approval": None,
    }
    assert set(body["progress"]) == {
        "ready", "waiting_oracle", "in_flight", "queued_total", "confirmed_total", "confirmed_last_hour",
        "skipped_total", "skipped_window_hours", "skipped_by_reason", "needs_review", "checked_total",
        "counted_at",
    }
    assert body["progress"]["skipped_by_reason"] == []
    assert body["in_flight_limit"] == 1500 and body["batch_size"] == 50
    assert body["rate_per_min"] is None and body["eta_minutes"] is None
    assert body["server_time"].endswith("Z")


def test_switch_needs_the_password_and_a_reason_and_is_audited(backend):
    sessions = backend
    with sessions() as db:
        client, headers = client_for(db)
        url = "/api/v2/unknown-identity-auto-release/control"
        off = {"enabled": False, "reason": "HR asked to pause", "password": PASSWORD,
               "idempotency_key": "switch-off-0001"}
        assert client.post(url, json=off).status_code == 403  # no CSRF token
        assert client.post(url, json={**off, "password": "wrong"}, headers=headers).status_code == 403
        assert client.post(url, json={**off, "reason": "no"}, headers=headers).status_code == 422
        missing = {key: value for key, value in off.items() if key != "password"}
        assert client.post(url, json=missing, headers=headers).status_code == 422
        db.rollback()
        assert db.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "ATTENDANCE_UNKNOWN_AUTO_RELEASE_SWITCHED")) == 0

        response = client.post(url, json=off, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is False and body["active"] is False
        assert body["changed_by"] == "StateHealthAdmin" and body["reason"] == "HR asked to pause"
        db.commit()
        replay = client.post(url, json=off, headers=headers)
        assert replay.status_code == 200 and replay.json()["enabled"] is False
        conflict = client.post(url, json={**off, "enabled": True}, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
        db.rollback()
        audits = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "ATTENDANCE_UNKNOWN_AUTO_RELEASE_SWITCHED")).all()
        assert [(row.outcome, row.before["enabled"], row.after["enabled"]) for row in audits] == [
            ("OFF", True, False)]
        assert "HR asked to pause" not in str(audits[0].after)
        row = db.get(State, 1)
        assert row.reason_encrypted != "HR asked to pause"
        assert decrypt_text(row.reason_encrypted) == "HR asked to pause"

        # "Release all now" while OFF: one click, no password, audited once.
        drain_url = "/api/v2/unknown-identity-auto-release/release-all"
        started = client.post(drain_url, json={"idempotency_key": "release-all-0001"}, headers=headers)
        assert started.status_code == 202, started.text
        assert started.json()["outcome"] == "STARTED" and started.json()["drain_active"] is True
        assert started.json()["active"] is True and started.json()["drain_requested_by"] == "StateHealthAdmin"
        db.commit()
        again = client.post(drain_url, json={"idempotency_key": "release-all-0002"}, headers=headers)
        assert again.json()["outcome"] == "ALREADY_RUNNING"
        db.commit()
        assert db.scalar(select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "ATTENDANCE_UNKNOWN_RELEASE_ALL_REQUESTED")) == 1

        # Turning it back ON needs the password and a reason too; it ends the one-shot.
        on = {"enabled": True, "reason": "Owner wants it running", "password": PASSWORD,
              "idempotency_key": "switch-on-0001"}
        assert client.post(url, json={**on, "password": "nope"}, headers=headers).status_code == 403
        db.rollback()
        body = client.post(url, json=on, headers=headers).json()
        assert body["enabled"] is True and body["drain_active"] is False
        db.commit()
        already = client.post(drain_url, json={"idempotency_key": "release-all-0003"}, headers=headers)
        assert already.json()["outcome"] == "ALREADY_AUTOMATIC"


def test_release_all_is_refused_when_the_deployment_disallows_it(backend, monkeypatch):
    sessions = backend
    monkeypatch.setattr(settings, "unknown_identity_auto_release_allowed", False)
    with sessions() as db:
        client, headers = client_for(db)
        response = client.post("/api/v2/unknown-identity-auto-release/release-all",
                               json={"idempotency_key": "release-all-0001"}, headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "AUTO_RELEASE_NOT_ALLOWED"
        assert client.get("/api/v2/unknown-identity-auto-release").json()["active"] is False


# Worker ------------------------------------------------------------------------


def test_tick_obeys_the_deployment_switch_the_owner_switch_and_release_all(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 3)
    fake = FakeRelease(monkeypatch)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_allowed", False)
    assert step(sessions) == "NOT_ALLOWED" and auto.tick() == "NOT_ALLOWED"
    monkeypatch.setattr(settings, "unknown_identity_auto_release_allowed", True)
    with sessions() as db:
        auto._ensure(db).enabled = False
        db.commit()
    assert step(sessions) == "OFF" and fake.calls == []
    with sessions() as db:
        auto.request_drain(db, actor="owner", request=auto.AutoReleaseDrainRequest(idempotency_key="drain-0001"))
        db.commit()
    now = utc_now() + timedelta(seconds=1)
    assert step(sessions, now) == "BATCH"
    assert fake.calls == [sorted(ids, reverse=True)]  # newest first
    row = state(sessions)
    assert row.drain_requested_at is None and row.drain_completed_at is not None  # nothing left untried
    assert row.batches_created == 1 and row.punches_submitted == 3 and row.last_batch_size == 3
    assert step(sessions, now + timedelta(seconds=10)) == "OFF"
    with sessions() as db:
        jobs = db.scalars(select(Job)).all()
        assert [job.actor for job in jobs] == [auto.AUTO_ACTOR]
        assert jobs[0].scope[auto.SCOPE_FLAG] is True


def test_batches_are_small_paced_and_held_back_by_in_flight_punches(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 5)
    fake = FakeRelease(monkeypatch)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_batch_size", 2)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_in_flight_limit", 4)
    now = utc_now()
    assert step(sessions, now) == "BATCH"
    assert step(sessions, now + timedelta(seconds=1)) == "INTERVAL"
    assert step(sessions, now + timedelta(seconds=6)) == "BATCH"
    # Four approved punches still wait: no third batch until Oracle answers.
    assert step(sessions, now + timedelta(seconds=12)) == "BACKPRESSURE"
    newest = sorted(ids, reverse=True)
    assert fake.calls == [newest[:2], newest[2:4]]
    with sessions() as db:
        assert auto.status(db)["backpressure"] is True
        for item in db.scalars(select(Item)).all():
            item.status = "CONFIRMED"
            item.completed_at = now
        db.commit()
    assert step(sessions, now + timedelta(seconds=18)) == "BATCH"
    assert fake.calls[-1] == newest[4:]
    assert state(sessions).scan_head_id is None  # the pass reached the oldest punch


def test_skipped_punches_are_not_picked_again_until_the_retry_window(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 2)
    fake = FakeRelease(monkeypatch, statuses={event_id: "SKIPPED" for event_id in ids})
    now = utc_now()
    assert step(sessions, now) == "BATCH"
    # The pass reached the oldest punch. They are still in the unknown-identity
    # filter, but tried: the releaser rests (reading only newer punches) for
    # SCAN_REST_SECONDS, not one count interval, instead of rescanning them.
    rest = auto.ensure_utc(state(sessions).scan_idle_until)
    assert rest >= now + timedelta(seconds=auto.SCAN_REST_SECONDS)
    assert step(sessions, now + timedelta(seconds=6)) == "IDLE"
    assert step(sessions, now + timedelta(seconds=12)) == "IDLE"
    assert len(fake.calls) == 1
    with sessions() as db:
        assert direct.unknown_synced_event_ids(db) == sorted(ids, reverse=True)
        assert auto.status(db, now=now + timedelta(seconds=12))["resting_until"] is not None
        job = db.scalar(select(Job))
        job.created_at = utc_now() - timedelta(hours=25)
        db.commit()
    assert step(sessions, now + timedelta(seconds=80)) == "IDLE"
    assert len(fake.calls) == 1
    assert step(sessions, rest + timedelta(seconds=1)) == "BATCH"
    assert fake.calls[-1] == sorted(ids, reverse=True)


def test_the_rest_ends_when_the_first_tried_punch_may_be_retried(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 3)
    monkeypatch.setattr(auto, "SCAN_REST_SECONDS", 10 * 3600)
    now = utc_now()
    tried = FakeRelease(monkeypatch, statuses={event_id: "SKIPPED" for event_id in ids})
    with sessions() as db:
        tried(db, actor=auto.AUTO_ACTOR, event_ids=ids[1:], idempotency_key="earlier-run-0001")
        db.scalar(select(Job)).created_at = now - timedelta(hours=24) + timedelta(seconds=100)
        db.commit()
    fake = FakeRelease(monkeypatch)
    assert step(sessions, now) == "BATCH"
    assert fake.calls == [[ids[0]]]
    rest = auto.ensure_utc(state(sessions).scan_idle_until)
    assert now + timedelta(seconds=99) <= rest <= now + timedelta(seconds=101)
    assert step(sessions, now + timedelta(seconds=50)) == "IDLE"
    assert step(sessions, rest + timedelta(seconds=1)) == "BATCH"
    assert fake.calls[-1] == sorted(ids[1:], reverse=True)


def test_while_resting_only_new_punches_are_read_and_released(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 2)
    fake = FakeRelease(monkeypatch, statuses={event_id: "SKIPPED" for event_id in ids})
    now = utc_now()
    assert step(sessions, now) == "BATCH"
    head = state(sessions).idle_head_id
    assert head == max(ids)
    with sessions() as db:
        newcomer = db.get(AttendanceEvent, ids[0])
        columns = [c.key for c in AttendanceEvent.__table__.columns if c.key not in {"id", "event_uid"}]
        copy = AttendanceEvent(**{key: getattr(newcomer, key) for key in columns})
        copy.event_uid = "e" * 64
        db.add(copy)
        db.commit()
        new_id = copy.id
    fake.statuses = {}
    assert step(sessions, now + timedelta(seconds=2)) == "INTERVAL"  # reads are paced while resting too
    assert step(sessions, now + timedelta(seconds=6)) == "BATCH"
    assert fake.calls[-1] == [new_id]
    assert state(sessions).scan_head_id is None  # still resting; no full pass started
    assert step(sessions, now + timedelta(seconds=12)) == "IDLE"
    assert state(sessions).idle_head_id == new_id
    assert len(fake.calls) == 2


def test_each_batch_reads_a_bounded_window_of_held_punches(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 5)
    tried = FakeRelease(monkeypatch, statuses={event_id: "SKIPPED" for event_id in ids})
    with sessions() as db:
        tried(db, actor=auto.AUTO_ACTOR, event_ids=ids[1:], idempotency_key="earlier-run-0001")
        db.commit()
    fake = FakeRelease(monkeypatch)
    monkeypatch.setattr(auto, "SCAN_WINDOW", 2)
    monkeypatch.setattr(auto, "SCAN_BUDGET", 2)
    now = utc_now()
    # Two held punches per batch are read; the four newest were tried today.
    assert step(sessions, now) == "SCANNING"
    assert state(sessions).scan_cursor_id == ids[3]
    # A read that found nothing is paced like a batch.
    assert step(sessions, now + timedelta(seconds=1)) == "INTERVAL"
    assert state(sessions).scan_cursor_id == ids[3]
    assert step(sessions, now + timedelta(seconds=6)) == "SCANNING"
    assert state(sessions).scan_cursor_id == ids[1]
    assert step(sessions, now + timedelta(seconds=12)) == "BATCH"
    assert fake.calls == [[ids[0]]]
    assert state(sessions).scan_head_id is None


def test_new_punches_are_picked_before_the_rest_of_a_pass(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 4)
    fake = FakeRelease(monkeypatch)
    monkeypatch.setattr(settings, "unknown_identity_auto_release_batch_size", 1)
    now = utc_now()
    assert step(sessions, now) == "BATCH"
    with sessions() as db:
        newcomer = db.get(AttendanceEvent, ids[0])
        columns = [c.key for c in AttendanceEvent.__table__.columns if c.key not in {"id", "event_uid"}]
        copy = AttendanceEvent(**{key: getattr(newcomer, key) for key in columns})
        copy.event_uid = "f" * 64
        db.add(copy)
        db.commit()
        new_id = copy.id
    assert step(sessions, now + timedelta(seconds=6)) == "BATCH"
    assert fake.calls == [[ids[3]], [new_id]]
    assert step(sessions, now + timedelta(seconds=12)) == "BATCH"
    assert fake.calls[-1] == [ids[2]]


def test_nothing_to_release_moves_on_without_an_error(backend, monkeypatch):
    sessions = backend
    unknown_punches(sessions, 1)
    fake = FakeRelease(monkeypatch, nothing=True)
    now = utc_now()
    assert step(sessions, now) == "NOTHING_TO_RELEASE"
    row = state(sessions)
    assert row.last_error_code is None and row.last_batch_size == 1
    assert row.scan_head_id is None  # the pass ended; the punch is not retried in a loop
    assert step(sessions, now + timedelta(seconds=6)) == "IDLE"
    assert len(fake.calls) == 1


def test_switching_off_while_a_batch_is_built_discards_the_batch(backend, monkeypatch):
    sessions = backend
    unknown_punches(sessions, 3)
    with sessions() as db:
        auto._ensure(db)
        db.commit()
        postgres = db.get_bind().dialect.name == "postgresql"

    def switch_off(session):
        if postgres:
            # A real concurrent OFF, committed while the batch is being built.
            with sessions() as other:
                auto.control(other, actor="owner", request=auto.AutoReleaseControlRequest(
                    enabled=False, reason="Oracle maintenance", password="unused-here",
                    idempotency_key="switch-off-race-1"))
                other.commit()
        else:
            # SQLite tests share one connection: the same change inside the transaction.
            session.execute(auto.update(State).where(State.id == 1).values(enabled=False))

    fake = FakeRelease(monkeypatch, during=switch_off)
    assert step(sessions) == "OFF"
    assert len(fake.calls) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(Job.id))) == 0
        assert db.scalar(select(func.count(Item.id))) == 0
        row = db.get(State, 1)
        assert row.batches_created == 0 and row.last_batch_at is None
    if postgres:
        # The administrator's committed OFF stands; the next tick stays OFF.
        assert state(sessions).enabled is False
        assert step(sessions, utc_now() + timedelta(seconds=10)) == "OFF"
        assert len(fake.calls) == 1


def test_stopping_release_all_while_a_batch_is_built_discards_the_batch(backend, monkeypatch):
    sessions = backend
    unknown_punches(sessions, 2)
    with sessions() as db:
        auto._ensure(db).enabled = False
        db.commit()
        auto.request_drain(db, actor="owner", request=auto.AutoReleaseDrainRequest(idempotency_key="drain-race-1"))
        db.commit()

    def stop(session):
        session.execute(auto.update(State).where(State.id == 1).values(
            drain_requested_at=None, drain_requested_by=None))

    FakeRelease(monkeypatch, during=stop)
    assert step(sessions, utc_now() + timedelta(seconds=1)) == "OFF"
    with sessions() as db:
        assert db.scalar(select(func.count(Job.id))) == 0


def test_a_count_that_times_out_waits_its_interval_and_backs_off(backend, monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Canceled(Exception):
        sqlstate = "57014"

    sessions = backend
    with sessions() as db:
        auto._ensure(db).enabled = False  # counts run while OFF too; no batch in the way
        db.commit()
    real = auto.refresh_counts
    calls = []

    def timeout(*_args, **_kwargs):
        calls.append(1)
        raise OperationalError("SELECT count(...)", {}, Canceled("canceling statement due to statement timeout"))

    monkeypatch.setattr(auto, "refresh_counts", timeout)
    assert auto.tick() == "OFF"
    row = state(sessions)
    assert len(calls) == 1 and row.last_error_code == "COUNT_BUSY" and row.count_failures == 1
    assert row.count_attempted_at is not None and row.backlog_counted_at is None
    # The next worker loop does not start the heavy count again.
    assert auto.tick() == "OFF" and len(calls) == 1

    def age(seconds):
        with sessions() as db:
            db.get(State, 1).count_attempted_at = utc_now() - timedelta(seconds=seconds)
            db.commit()

    age(61)  # one failure: twice the count interval
    auto.tick()
    assert len(calls) == 1
    age(121)
    auto.tick()
    assert len(calls) == 2 and state(sessions).count_failures == 2
    age(121)  # two failures: four intervals
    auto.tick()
    assert len(calls) == 2

    monkeypatch.setattr(auto, "refresh_counts", real)
    age(241)
    auto.tick()
    row = state(sessions)
    assert row.count_failures == 0 and row.backlog_counted_at is not None
    assert row.backlog_count is not None


def test_real_quick_release_queues_and_the_counts_feed_the_status(backend):
    sessions = backend
    (event_id,) = unknown_punches(sessions, 1)
    assert auto.tick() == "BATCH"
    with sessions() as db:
        job = db.scalar(select(Job))
        assert job.actor == auto.AUTO_ACTOR and job.action == direct.ACTION
        assert job.scope[auto.SCOPE_FLAG] is True
        item = db.scalar(select(Item))
        assert (item.attendance_event_id, item.status) == (event_id, "READY")
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "PENDING"
        assert auto.refresh_counts(db, force=True) is True
        db.commit()
        body = auto.status(db)
    assert body["backlog"]["count"] == 0 and body["backlog"]["counted_at"]
    assert body["progress"]["waiting_oracle"] == 1 and body["progress"]["in_flight"] == 1
    assert body["progress"]["queued_total"] == 1 and body["progress"]["checked_total"] == 1
    assert body["rate_per_min"] == 0 and body["eta_minutes"] is None
    assert body["last_batch_job_id"] == job.job_id
    with sessions() as db:
        # Within the count interval nothing is recounted.
        assert auto.refresh_counts(db) is False
        item = db.scalar(select(Item))
        item.status, item.completed_at = "CONFIRMED", utc_now()
        db.commit()
        assert auto.refresh_counts(db, force=True) is True
        db.commit()
        body = auto.status(db)
    assert body["progress"]["confirmed_total"] == 1 and body["progress"]["confirmed_last_hour"] == 1
    assert body["rate_per_min"] == round(1 / 60, 2) and body["eta_minutes"] == 0


def test_skips_are_counted_once_per_punch_with_reasons(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, 2)
    FakeRelease(monkeypatch, statuses={ids[0]: "SKIPPED"})
    step(sessions)
    with sessions() as db:
        auto.refresh_counts(db, force=True)
        db.commit()
        body = auto.status(db)
    assert body["backlog"]["count"] == 2  # READY items and skipped punches are still held
    assert body["progress"]["skipped_total"] == 1
    assert body["progress"]["skipped_by_reason"] == [{
        "code": "CNIC_CONFLICT", "reason": direct.REASONS["CNIC_CONFLICT"], "count": 1}]
    assert body["progress"]["ready"] == 1
    assert body["backlog"]["held_by_other_approval"] == 0  # its own queue is not another approval
    assert body["backlog"]["remaining_estimate"] == 1


def other_run(sessions, event_ids, *, action, status, preview_expires_at=None) -> int:
    """An administrator's run (not this releaser's) holding these punches READY."""
    with sessions() as db:
        job = Job(
            action=action, actor="StateHealthAdmin", reason="test", status=status,
            idempotency_key=f"other-run-{uuid4().hex}", scope={}, candidate_digest="c" * 64,
            preview_expires_at=preview_expires_at,
        )
        db.add(job)
        db.flush()
        for event_id in event_ids:
            db.add(Item(
                job_id=job.id, source_kind="ATTENDANCE", source_ref=str(event_id),
                attendance_event_id=event_id, expected_state_digest="e" * 64, lane=action,
                status="READY", result={},
            ))
        db.commit()
        return job.id


# (action, run status, preview expiry from now, whether its READY item still holds the punch)
OTHER_RUNS = [
    # Checks never approved: their start refuses an expired preview.
    (force_release.ACTION, "CHECKED", timedelta(days=-9), False),
    (force_release.ACTION, "CHECKED", timedelta(minutes=10), True),
    (force_release.ACTION, "CHECKED", None, True),
    (safe_repair.ACTION, "CHECKED", timedelta(0), False),
    (safe_repair.ACTION, "CHECKED", timedelta(minutes=10), True),
    # Still checking, sending, waiting, paused or stopping: may still act.
    (force_release.ACTION, "CHECKING", None, True),
    (force_release.ACTION, "RUNNING", timedelta(days=-9), True),
    (force_release.ACTION, "WAITING_ORACLE", timedelta(days=-9), True),
    (force_release.ACTION, "PAUSED", timedelta(days=-9), True),
    (force_release.ACTION, "STOPPING", timedelta(days=-9), True),
    (safe_repair.ACTION, "CHECKING", None, True),
    (safe_repair.ACTION, "RUNNING", timedelta(days=-9), True),
    (safe_repair.ACTION, "PAUSED", timedelta(days=-9), True),
    (direct.ACTION, "RUNNING", None, True),
    (direct.ACTION, "WAITING_ORACLE", None, True),
    # Unknown to this rule: kept holding.
    (direct.ACTION, "CHECKED", timedelta(days=-9), True),
    ("SOME_NEWER_ACTION", "COMPLETED", None, True),
    # Ended.
    (force_release.ACTION, "COMPLETED", None, False),
    (force_release.ACTION, "COMPLETED_WITH_REVIEW", None, False),
    (force_release.ACTION, "STOPPED", None, False),
    (force_release.ACTION, "EXPIRED", None, False),
    (safe_repair.ACTION, "COMPLETED", None, False),
    (safe_repair.ACTION, "STOPPED", None, False),
    (direct.ACTION, "COMPLETED", None, False),
    (direct.ACTION, "COMPLETED_WITH_REVIEW", None, False),
]


def test_only_runs_that_may_still_send_hold_their_ready_punches(backend, monkeypatch):
    sessions = backend
    ids = unknown_punches(sessions, len(OTHER_RUNS))
    now = utc_now()
    for event_id, (action, status, expires_in, _holds) in zip(ids, OTHER_RUNS):
        other_run(sessions, [event_id], action=action, status=status,
                  preview_expires_at=None if expires_in is None else now + expires_in)
    held = [event_id for event_id, run in zip(ids, OTHER_RUNS) if run[3]]
    free = [event_id for event_id, run in zip(ids, OTHER_RUNS) if not run[3]]
    # "Still to release" leaves out what other approvals still hold.
    with sessions() as db:
        assert auto.refresh_counts(db, now=now, force=True) is True
        db.commit()
        backlog = auto.status(db, now=now)["backlog"]
    assert backlog["count"] == len(ids)
    assert backlog["held_by_other_approval"] == len(held)
    assert backlog["remaining_estimate"] == len(free)

    fake = FakeRelease(monkeypatch)
    assert step(sessions, now) == "BATCH"
    assert fake.calls == [sorted(free, reverse=True)]
    with sessions() as db:
        auto.refresh_counts(db, now=now, force=True)
        db.commit()
        body = auto.status(db, now=now)
    # This releaser's own READY items are its queue, counted once.
    assert body["progress"]["ready"] == len(free)
    assert body["backlog"]["held_by_other_approval"] == len(held)
    assert body["backlog"]["remaining_estimate"] == len(free)


def test_a_punch_held_elsewhere_and_already_counted_here_is_subtracted_once(backend, monkeypatch):
    """Skipped here, or in this releaser's own queue, and later held READY by an administrator."""
    sessions = backend
    monkeypatch.setattr(settings, "unknown_identity_auto_release_batch_size", 10)
    ids = unknown_punches(sessions, 20)
    now = utc_now()
    newest = sorted(ids, reverse=True)[:10]
    skipped, queued = newest[:6], newest[6:]
    fake = FakeRelease(monkeypatch, statuses={event_id: "SKIPPED" for event_id in skipped})
    assert step(sessions, now) == "BATCH"
    assert fake.calls == [newest]
    # A paused administrator's run holds the six skipped punches and two of the queued ones.
    other_run(sessions, skipped + queued[:2], action=force_release.ACTION, status="PAUSED")
    with sessions() as db:
        auto.refresh_counts(db, now=now, force=True)
        db.commit()
        body = auto.status(db, now=now)
    assert body["backlog"]["count"] == 20
    assert body["progress"]["skipped_total"] == 6 and body["progress"]["ready"] == 4
    assert body["backlog"]["held_by_other_approval"] == 0  # all eight already counted here
    assert body["backlog"]["remaining_estimate"] == 10 + 4  # never picked, plus its own queue

    # A held punch this releaser never touched still counts as held elsewhere.
    unpicked = sorted(set(ids) - set(newest))
    other_run(sessions, unpicked[:3], action=force_release.ACTION, status="PAUSED")
    with sessions() as db:
        auto.refresh_counts(db, now=now, force=True)
        db.commit()
        backlog = auto.status(db, now=now)["backlog"]
    assert backlog["held_by_other_approval"] == 3
    assert backlog["remaining_estimate"] == 7 + 4


def test_a_check_left_unapproved_past_its_preview_releases_its_punches(backend, monkeypatch):
    """13 Nov 2026: 7,462 punches stayed READY in force release checks never started."""
    sessions = backend
    stale, fresh = unknown_punches(sessions, 2)
    now = utc_now()
    expired = other_run(sessions, [stale], action=force_release.ACTION, status="CHECKED",
                        preview_expires_at=now - timedelta(days=9))
    other_run(sessions, [fresh], action=force_release.ACTION, status="CHECKED",
              preview_expires_at=now + timedelta(minutes=10))
    repair = other_run(sessions, [], action=safe_repair.ACTION, status="CHECKED",
                       preview_expires_at=now - timedelta(minutes=1))
    # The rule rests on these refusals: neither check can be approved any more.
    with sessions() as db:
        check = db.get(Job, expired)
        signature = sign_recovery_preview(digest=check.candidate_digest, expires_at=check.preview_expires_at,
                                          actor=check.actor, action=check.action)
        with pytest.raises(RecoveryError) as refused:
            force_release.start(db, check, actor=check.actor, signature=signature,
                                reason="late approval", key="late-approval-0001")
        assert refused.value.code == "PREVIEW_EXPIRED"
        db.rollback()
        check = db.get(Job, repair)
        signature = sign_recovery_preview(digest=check.candidate_digest, expires_at=check.preview_expires_at,
                                          actor=check.actor, action=check.action)
        with pytest.raises(RecoveryError) as refused:
            safe_repair.start_check(db, check, actor=check.actor, signature=signature)
        assert refused.value.code == "PREVIEW_EXPIRED"
        db.rollback()

    fake = FakeRelease(monkeypatch)
    assert step(sessions, now) == "BATCH"
    assert fake.calls == [[stale]]
    # The other check holds its punch until its own preview has expired.
    assert step(sessions, now + timedelta(minutes=20)) == "BATCH"
    assert fake.calls[-1] == [fresh]


def test_a_busy_database_is_recorded_not_raised(backend, monkeypatch):
    from sqlalchemy.exc import OperationalError

    class LockTimeout(Exception):
        sqlstate = "55P03"

    sessions = backend
    unknown_punches(sessions, 1)

    def busy(*_args, **_kwargs):
        raise OperationalError("SELECT ... FOR UPDATE", {}, LockTimeout("lock timeout"))

    monkeypatch.setattr(direct, "quick_release", busy)
    assert auto.tick() == "ADD_BUSY"
    with sessions() as db:
        assert db.get(State, 1).last_error_code == "ADD_BUSY"
        assert db.scalar(select(func.count(Job.id))) == 0


# Wiring, settings, migration ------------------------------------------------------


def test_worker_runs_the_releaser_with_its_own_error_code():
    source = inspect.getsource(worker._safe_attendance_repair_loop)
    assert "attendance_unknown_auto_release import tick as unknown_release_tick" in source
    assert '(unknown_release_tick, "UNKNOWN_IDENTITY_AUTO_RELEASE_WORKER_ERROR")' in source


def test_settings_defaults_match_the_owner_request():
    defaults = AddSettings(_env_file=None)
    assert defaults.unknown_identity_auto_release_allowed is True
    assert defaults.unknown_identity_auto_release_batch_size == 50
    assert defaults.unknown_identity_auto_release_min_interval_seconds == 5
    assert defaults.unknown_identity_auto_release_in_flight_limit == 1500
    assert defaults.unknown_identity_auto_release_retry_hours == 24
    assert defaults.unknown_identity_auto_release_count_interval_seconds == 60
    example = (ROOT / ".env.add.example").read_text(encoding="utf-8")
    assert "# ADD_UNKNOWN_IDENTITY_AUTO_RELEASE_ALLOWED=true" in example


def _migration():
    spec = spec_from_file_location("unknown_auto_release_migration", MIGRATION_FILE)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_follows_0061_and_fits_alembic_version_column():
    migration = _migration()
    previous = (VERSIONS / "20261105_0061_dashboard_indexes.py").read_text(encoding="utf-8")
    assert f'revision = "{migration.down_revision}"' in previous
    assert len(migration.revision) <= 32
    assert migration.TABLE == State.__tablename__


def _run(engine, step_name):
    migration = _migration()
    with engine.connect() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, step_name)()
        connection.commit()


def _check_migration(engine):
    _run(engine, "upgrade")
    _run(engine, "upgrade")  # a retried deploy keeps the seeded row
    columns = {column["name"] for column in sa_inspect(engine).get_columns(State.__tablename__)}
    assert columns == set(State.__table__.columns.keys())
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT id, enabled FROM {State.__tablename__}")).all()
    assert [(row[0], bool(row[1])) for row in rows] == [(1, True)]
    _run(engine, "downgrade")
    assert State.__tablename__ not in sa_inspect(engine).get_table_names()


def test_migration_up_and_down_on_sqlite():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    try:
        _check_migration(engine)
    finally:
        engine.dispose()


def test_migration_up_and_down_on_postgresql():
    url = _postgres_url()
    if url is None:
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "unknown_auto_release_migration_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        _check_migration(engine)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
