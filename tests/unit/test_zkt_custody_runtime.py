import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from test_zkt_custody import custody as custody, observation, batch
from zk_add import zkt_custody_runtime as runtime, zkt_custody_work as work
from zk_add.models import Connector, ZKTDevice, ZktCustodyWork
from zk_add.zkt_custody import settle_observations, observation_id


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 2)


def add_sites(db, connector, size, observations=3):
    sites = [connector]
    for index in range(1, size):
        site = Connector(connector_id=f"fair-site-{index}", hardware_id=f"fair-hardware-{index}",
                         zone_id=f"TEST-{index}", zone_name="TEST", device_id=f"TEST-{index}",
                         display_name="TEST", zkt_custody_enabled=True)
        db.add(site)
        db.flush()
        site.zkt_device = ZKTDevice(connector_id=site.id, serial=f"TEST-{index}", confirmed_serial=f"TEST-{index}")
        sites.append(site)
    db.flush()
    for site in sites:
        for sequence in range(1, observations + 1):
            value = observation(sequence, terminal_serial=site.zkt_device.serial)
            value["observation_id"] = observation_id(value["terminal_serial"], value["capture_epoch"], sequence)
            settle_observations(db, site, batch(value))
    db.commit()
    return sites


@pytest.mark.parametrize("sites", [16, 17])
def test_saturated_sites_rotate_under_the_time_budget(custody, monkeypatch, sites):
    db, connector = custody
    add_sites(db, connector, sites)
    now = [0.0]
    inspected = []
    original = work.inspect_work
    def inspect(session, row, **kwargs):
        inspected.append(row.connector_id)
        original(session, row, **kwargs)
        now[0] += 0.010  # One expensive group consumes this tick's budget.
    monkeypatch.setattr(work, "inspect_work", inspect)
    cursor = 0
    for _ in range(sites * 2):
        result = work.advance_work_batch(db, limit=100, after_connector=cursor,
                                         time_budget_ms=1, clock=lambda: now[0])
        db.commit()
        assert result.processed == 1
        cursor = result.after_connector
    assert len(set(inspected[:sites])) == len(set(inspected[sites:])) == sites
    assert inspected[:sites] == inspected[sites:]


def test_connector_is_not_skipped_when_its_query_exhausts_the_budget(custody):
    db, connector = custody
    sites = add_sites(db, connector, 2, observations=1)
    times = iter([0.0, 0.0004, 0.0012])
    result = work.advance_work_batch(db, limit=2, time_budget_ms=1, clock=lambda: next(times))
    assert result.processed == 1 and result.after_connector == sites[0].id
    result = work.advance_work_batch(db, limit=2, after_connector=result.after_connector,
                                     time_budget_ms=1, clock=lambda: 0)
    assert result.processed == 1 and result.after_connector == sites[1].id


def test_database_rollback_cannot_advance_cursor_or_progress(custody, monkeypatch):
    db, connector = custody
    settle_observations(db, connector, batch(observation()))
    db.commit()
    @contextmanager
    def scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
    monkeypatch.setattr("zk_add.db.session_scope", scope)
    original = work.inspect_work
    def fail(session, row, **kwargs):
        original(session, row, **kwargs)
        raise SQLAlchemyError("synthetic private error payload")
    monkeypatch.setattr(work, "inspect_work", fail)
    processor = runtime.CustodyProcessor()
    assert processor._one_tick() == (0, "CUSTODY_DATABASE_UNAVAILABLE")
    row = db.scalar(select(ZktCustodyWork))
    assert row.state == "PENDING" and row.attempt_count == 0
    assert processor._cursor == 0
    snapshot = processor.snapshot()
    assert snapshot["inspected_groups_total"] == 0 and snapshot["last_progress_at"] is None
    assert "private" not in json.dumps(snapshot, default=str)
    monkeypatch.setattr(work, "inspect_work", original)
    assert processor._one_tick() == (1, None)
    assert processor._cursor == connector.id
    assert processor.snapshot()["inspected_groups_total"] == 1
    assert db.scalar(select(ZktCustodyWork)).state == "WAIT_PROFILE"


def test_device_status_exposes_separate_global_runtime_evidence(custody):
    from fastapi import Response
    from zk_add.web import zkt_custody_status
    db, connector = custody
    response = Response()
    result = zkt_custody_status(connector.connector_id, response, before=None, limit=10, auth=(db, None))
    assert result["connector_id"] == connector.connector_id
    assert result["oracle_completion"] == "NOT_ASSERTED"
    assert result["processor"]["schema_version"] == 1
    assert result["processor"]["instance_id"] and result["processor"]["sampled_at"]
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


def test_stalled_transaction_is_observable_and_repeated_cancel_never_replaces_it():
    async def run():
        started, release = Event(), Event()
        now, calls = [0.0], []
        def tick(cursor):
            calls.append(cursor)
            started.set()
            assert release.wait(2)
            return work.InspectionBatch(2, 4, 1)
        processor = runtime.CustodyProcessor(tick, clock=lambda: now[0])
        assert processor.snapshot()["state"] == "NOT_STARTED"
        task = asyncio.create_task(processor.run(asyncio.Event()))
        try:
            await eventually(started.is_set)
            now[0] = 6.0
            status = processor.snapshot()
            assert status["state"] == "STALLED" and status["current_tick_elapsed_ms"] == 6000
            with pytest.raises(RuntimeError, match="already running"):
                await processor.run(asyncio.Event())
            task.cancel()
            await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.005)
            assert not task.done() and calls == [0]
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        status = processor.snapshot()
        assert status["state"] == "STOPPED" and status["current_tick_elapsed_ms"] is None
        assert status["successful_ticks"] == 1 and status["inspected_groups_total"] == 2
        assert status["starts"] == 1 and status["deadline_overruns"] == 1
    asyncio.run(run())


def test_default_executor_saturation_does_not_block_custody():
    async def run():
        release, blocked = Event(), Event()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        def unrelated():
            blocked.set()
            assert release.wait(2)
        existing = asyncio.create_task(asyncio.to_thread(unrelated))
        stop = asyncio.Event()
        def tick(cursor):
            loop.call_soon_threadsafe(stop.set)
            return work.InspectionBatch(1, 1, 1)
        processor = runtime.CustodyProcessor(tick)
        try:
            await eventually(blocked.is_set)
            await asyncio.wait_for(processor.run(stop), 1)
            assert processor.snapshot()["inspected_groups_total"] == 1
            assert not existing.done()
        finally:
            release.set()
            await existing
    asyncio.run(run())


def test_failures_retry_with_safe_categories_and_idle_is_not_delivery(monkeypatch):
    monkeypatch.setattr(runtime, "IDLE_SECONDS", 0.001)
    async def run():
        stop, calls, notices = asyncio.Event(), [], []
        loop = asyncio.get_running_loop()
        def tick(cursor):
            calls.append(cursor)
            if len(calls) == 1:
                raise RuntimeError("private packet content")
            if len(calls) == 2:
                raise SQLAlchemyError("private database content")
            loop.call_soon_threadsafe(stop.set)
            return work.InspectionBatch(0, 0, 0)
        async def publish(topic, value):
            notices.append((topic, value))
            raise RuntimeError("browser transport unavailable")
        processor = runtime.CustodyProcessor(tick)
        await asyncio.wait_for(processor.run(stop, publish=publish), 1)
        status = processor.snapshot()
        assert calls == [0, 0, 0] and len(notices) == 3
        assert status["failed_ticks"] == 2 and status["successful_ticks"] == 1
        assert status["consecutive_failures"] == 0 and status["active_error_code"] is None
        assert status["last_failure_code"] == "CUSTODY_DATABASE_UNAVAILABLE"
        assert status["inspected_groups_total"] == 0 and status["last_progress_at"] is None
        assert "private" not in json.dumps([status, notices], default=str)
    asyncio.run(run())


def test_thread_start_failure_is_not_counted_as_success(monkeypatch):
    def unavailable(**_):
        raise RuntimeError("synthetic private thread creation failure")
    monkeypatch.setattr(runtime, "ThreadPoolExecutor", unavailable)
    processor = runtime.CustodyProcessor()
    with pytest.raises(RuntimeError):
        asyncio.run(processor.run(asyncio.Event()))
    status = processor.snapshot()
    assert status["state"] == "STOPPED" and status["starts"] == 0
    assert status["start_attempts"] == 1 and status["successful_ticks"] == 0
    assert status["active_error_code"] == "CUSTODY_WORKER_STOPPED_UNEXPECTEDLY"
    assert "private" not in json.dumps(status, default=str)


def test_locked_connectors_are_not_reported_as_idle_successful_processing():
    processor = runtime.CustodyProcessor(lambda _: work.InspectionBatch(0, 3, 3, 3))
    assert processor._one_tick() == (0, None)
    status = processor.snapshot()
    assert status["state"] == "WAITING_FOR_LOCK" and status["last_locked_connectors"] == 3
    assert status["inspected_groups_total"] == 0 and status["last_progress_at"] is None


def test_startup_recovers_after_thread_resources_return(monkeypatch):
    original = runtime.ThreadPoolExecutor
    attempts = []
    def transient(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("synthetic thread resource exhaustion")
        return original(**kwargs)
    monkeypatch.setattr(runtime, "ThreadPoolExecutor", transient)
    monkeypatch.setattr(runtime, "IDLE_SECONDS", 0.001)
    async def run():
        loop, stop = asyncio.get_running_loop(), asyncio.Event()
        def tick(_):
            loop.call_soon_threadsafe(stop.set)
            return work.InspectionBatch(1, 1, 1)
        processor = runtime.CustodyProcessor(tick)
        await asyncio.wait_for(runtime.run_custody_processor(stop, processor=processor), 1)
        status = processor.snapshot()
        assert status["start_attempts"] == 2 and status["starts"] == 1
        assert status["successful_ticks"] == 1 and status["active_error_code"] is None
    asyncio.run(run())


def test_stalled_dispatch_is_distinct_from_a_healthy_idle_worker():
    async def run():
        now, publishing = [0.0], asyncio.Event()
        def tick(_):
            return work.InspectionBatch(1, 1, 1)
        async def publish(*_):
            publishing.set()
            await asyncio.Event().wait()
        processor = runtime.CustodyProcessor(tick, clock=lambda: now[0])
        task = asyncio.create_task(processor.run(asyncio.Event(), publish=publish))
        await publishing.wait()
        now[0] = 6.0
        status = processor.snapshot()
        assert status["state"] == "STALLED" and status["current_tick_elapsed_ms"] is None
        assert status["last_completion_age_ms"] == 6000
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
