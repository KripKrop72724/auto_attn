"""Intake priority is bounded and transactional; it grants no decoding authority."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
from threading import Event

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import event, insert, select, text

from test_zkt_source_load import store as store
from zk_add import zkt_custody_work as work, zkt_derived_evidence as derived
from zk_add.db import Base
from zk_add.models import Connector, ZktCustodySchedule, ZktCustodyWork

NOW = datetime(2026, 10, 4, 3, tzinfo=timezone.utc)


@pytest.fixture
def priority_store(store, monkeypatch):
    monkeypatch.setattr(work, "utc_now", lambda: NOW)
    with store() as db:
        db.scalar(select(Connector)).zkt_custody_enabled = True
        db.commit()
    return store


def add_work(db, number, *, kind="SOURCE_RECORD", age=3600, due=True, version=None):
    row = ZktCustodyWork(work_key=f"{number:064x}", connector_id=db.scalar(select(Connector.id)),
        kind=kind, terminal_serial="TEST-LOAD", state="PENDING", reason_code="PENDING",
        owner="ADD_PROTOCOL", created_at=NOW - timedelta(seconds=age),
        next_attempt_at=NOW - timedelta(seconds=age) if due else None,
        interpretation_version=version)
    db.add(row)
    db.flush()
    return row.id


def inspector(monkeypatch, seen, *, pending=False):
    # Isolate selection from raw decoding, which has its own evidence tests.
    # Keeping pending=True simulates one bounded step of a large packet.
    def inspect(db, row):
        seen.append(row.id)
        row.attempt_count += 1
        row.state = "INTERPRETING" if pending else "WAIT_PROFILE"
        row.interpretation_version = derived.INTERPRETATION_VERSION
        row.next_attempt_at = NOW if pending else None
    monkeypatch.setattr(work, "inspect_work", inspect)
    return inspect


def test_recent_intake_precedes_history_with_bounded_bursts(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        old = [add_work(db, i) for i in range(5)]
        recent = [add_work(db, 100 + i, kind="LIVE_PACKET", age=i) for i in range(10)]
        db.commit()
        assert work.advance_work(db, limit=9) == 9
        db.commit()
        assert seen == recent[:8] + old[:1]
        assert db.scalar(select(ZktCustodySchedule.priority_burst)) == 0
        assert work.advance_work(db, limit=9) == 6
        assert seen[9:] == recent[8:] + old[1:]


def test_new_arrivals_cannot_starve_old_packets_across_restarts(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        old = [add_work(db, i, kind="LIVE_PACKET", age=3600-i) for i in range(3)]
        db.commit()
    # Every tick has new priority work and a new session/worker instance.
    for tick in range(27):
        with priority_store() as db:
            add_work(db, 100 + tick, kind="PACKET_FRAGMENT", age=0)
            db.commit()
            assert work.advance_work(db, limit=1) == 1
            db.commit()
    assert [seen[index] for index in (8, 17, 26)] == old
    assert len(set(seen)) == 27


def test_old_decoder_hold_gets_a_turn_despite_null_retry_time(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        old = add_work(db, 1, due=False, version="older-decoder")
        add_work(db, 2, age=100)
        add_work(db, 3, kind="LIVE_PACKET", age=0)
        db.add(ZktCustodySchedule(connector_id=db.scalar(select(Connector.id)), priority_burst=8))
        db.commit()
        assert work.advance_work(db, limit=1) == 1
        assert seen == [old]


def test_decoder_revision_preserves_explicit_backoff_and_excludes_invalid_holds(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        delayed = db.get(ZktCustodyWork, add_work(db, 1, kind="LIVE_PACKET", age=-30))
        delayed.created_at = NOW - timedelta(seconds=1)
        invalid = db.get(ZktCustodyWork, add_work(db, 2, due=False))
        invalid.state = "HELD_EXCEPTION"
        add_work(db, 3, kind="UNKNOWN", due=False)
        add_work(db, 4, due=False, version=derived.INTERPRETATION_VERSION)
        db.commit()
        assert work.advance_work(db) == 0
        delayed.next_attempt_at = NOW
        db.commit()
        assert work.advance_work(db) == 1
        assert seen == [delayed.id]


def test_revision_ranges_make_progress_once_without_reading_current_holds(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        expected = [add_work(db, i, due=False, version=version)
                    for i, version in enumerate((None, "a", "b", "zz", "zzz"))]
        add_work(db, 100, due=False, version=derived.INTERPRETATION_VERSION)
        db.commit()
        for _ in expected:
            assert work.advance_work(db, limit=1) == 1
            db.commit()
        assert set(seen) == set(expected)
        assert work.advance_work(db) == 0


def test_priority_uses_bounded_server_intake_window(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        expired = add_work(db, 1, kind="LIVE_PACKET", age=61)
        add_work(db, 2, kind="LIVE_PACKET", age=-1)
        boundary = add_work(db, 3, kind="LIVE_PACKET", age=60)
        add_work(db, 4, kind="SOURCE_RECORD", age=0)
        db.commit()
        assert work.advance_work(db, limit=2) == 2
        assert seen == [boundary, expired]


def test_same_pending_packet_is_inspected_once_per_batch(priority_store, monkeypatch):
    seen = []
    inspector(monkeypatch, seen, pending=True)
    with priority_store() as db:
        packet = add_work(db, 1, kind="LIVE_PACKET", age=0)
        db.commit()
        assert work.advance_work(db, limit=100) == 1
        assert seen == [packet]
        db.commit()
        assert work.advance_work(db, limit=100) == 1
        assert seen == [packet, packet]


def test_failed_transaction_preserves_fairness_and_work_state(priority_store, monkeypatch):
    seen = []
    inspect = inspector(monkeypatch, seen)
    with priority_store() as db:
        old = add_work(db, 1)
        recent = add_work(db, 2, kind="LIVE_PACKET", age=0)
        db.add(ZktCustodySchedule(connector_id=db.scalar(select(Connector.id)), priority_burst=7))
        db.commit()
        def fail(db, row):
            inspect(db, row)
            if row.id == old:
                raise RuntimeError("synthetic interrupted transaction")
        monkeypatch.setattr(work, "inspect_work", fail)
        with pytest.raises(RuntimeError, match="interrupted"):
            work.advance_work(db, limit=2)
        db.rollback()
    with priority_store() as db:
        assert db.scalar(select(ZktCustodySchedule.priority_burst)) == 7
        assert all(row.attempt_count == 0 for row in db.scalars(select(ZktCustodyWork)))
        monkeypatch.setattr(work, "inspect_work", inspect)
        seen.clear()
        assert work.advance_work(db, limit=2) == 2
        db.commit()
        assert seen == [recent, old]
        assert db.scalar(select(ZktCustodySchedule.priority_burst)) == 0


def test_concurrent_workers_share_fairness_without_waiting_under_connector_lock(priority_store, monkeypatch):
    if priority_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock qualification")
    seen, started, release = [], Event(), Event()
    inspect = inspector(monkeypatch, seen)
    with priority_store() as db:
        old = add_work(db, 1)
        recent = add_work(db, 2, kind="LIVE_PACKET", age=0)
        db.add(ZktCustodySchedule(connector_id=db.scalar(select(Connector.id)), priority_burst=7))
        db.commit()
    def blocked(db, row):
        inspect(db, row)
        started.set()
        assert release.wait(5)
    monkeypatch.setattr(work, "inspect_work", blocked)
    def tick():
        with priority_store() as db:
            result = work.advance_work_batch(db, limit=1, time_budget_ms=None)
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(tick)
        try:
            assert started.wait(5)
            skipped = tick()
            assert skipped.processed == 0 and skipped.locked_connectors == 1
        finally:
            release.set()
        assert first.result(timeout=5).processed == 1
    assert seen == [recent]
    monkeypatch.setattr(work, "inspect_work", inspect)
    assert tick().processed == 1
    assert seen == [recent, old]


def test_large_due_history_does_not_hide_recent_intake(priority_store, monkeypatch):
    if priority_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL scheduler workload")
    seen = []
    inspector(monkeypatch, seen)
    with priority_store() as db:
        connector_id = db.scalar(select(Connector.id))
        for start in range(0, 200000, 5000):
            db.execute(insert(ZktCustodyWork), [dict(work_key=f"{index:064x}", connector_id=connector_id,
                kind="SOURCE_RECORD", terminal_serial="TEST-LOAD", state="PENDING", reason_code="PENDING",
                owner="ADD_PROTOCOL", created_at=NOW-timedelta(days=1), next_attempt_at=NOW-timedelta(days=1))
                for index in range(start, start + 5000)])
        recent = add_work(db, 200001, kind="LIVE_PACKET", age=0)
        db.commit()
        db.execute(text("ANALYZE add_zkt_custody_work"))
        db.execute(text("SET LOCAL statement_timeout = '2000ms'"))
        assert work.advance_work(db, limit=1) == 1
        assert seen == [recent]


def test_generic_plans_skip_retained_holds_without_full_scan_or_sort(priority_store, monkeypatch):
    if priority_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL prepared-plan workload")
    from psycopg import sql
    inspector(monkeypatch, [])
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "add_zkt_custody_work" in statement:
            statements.append((statement, parameters))
    with priority_store() as db:
        connector_id = db.scalar(select(Connector.id))
        for start in range(0, 200000, 5000):
            db.execute(insert(ZktCustodyWork), [dict(work_key=f"{index:064x}", connector_id=connector_id,
                kind="LIVE_PACKET", terminal_serial="TEST-LOAD", state="WAIT_PROFILE", reason_code="PENDING",
                owner="ADD_PROTOCOL", created_at=NOW-timedelta(seconds=5),
                interpretation_version=derived.INTERPRETATION_VERSION) for index in range(start, start + 5000)])
        for index, version in enumerate((None, "a", "zz"), 200000):
            add_work(db, index, due=False, version=version)
        add_work(db, 200003, kind="LIVE_PACKET", age=0)
        db.commit()
        db.execute(text("ANALYZE add_zkt_custody_work"))
        connection = db.connection()
        event.listen(connection, "before_cursor_execute", capture)
        try:
            assert work.advance_work(db, limit=1) == 1
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        # EXPLAIN the actual emitted statements with server-side generic plans,
        # including safely rendered fixed predicates, rather than a toy query.
        cursor = connection.connection.driver_connection.cursor()
        cursor.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor.execute("SET LOCAL statement_timeout = '2000ms'")
        indexes = set()
        def visit(node):
            if node.get("Relation Name") == "add_zkt_custody_work" and node.get("Actual Loops", 0):
                assert node["Node Type"] != "Seq Scan", json.dumps(node)
                assert node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0) <= 20, json.dumps(node)
                assert node.get("Rows Removed by Index Recheck", 0) <= 20
            if "Index Name" in node:
                indexes.add(node["Index Name"])
            for child in node.get("Plans", []):
                visit(child)
        for number, (statement, parameters) in enumerate(statements):
            keys = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", statement)))
            prepared = re.sub(r"%\((\w+)\)s", lambda match: "$" + str(keys.index(match[1]) + 1), statement)
            name = sql.Identifier(f"custody_plan_{number}")
            cursor.execute(sql.SQL("PREPARE {} AS {}").format(name, sql.SQL(prepared)))
            values = sql.SQL(",").join(sql.Literal(parameters[key]) for key in keys)
            cursor.execute(sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE {} ({})").format(name, values))
            visit(cursor.fetchone()[0][0]["Plan"])
            cursor.execute(sql.SQL("DEALLOCATE {}").format(name))
        cursor.close()
        assert {"ix_add_zkt_work_due", "ix_add_zkt_work_revision_hold", "ix_add_zkt_work_recent_live"} <= indexes


def test_additive_migration_retains_counter_on_backend_rollback(priority_store, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261004_0049_zkt_custody_priority.py"
    spec = importlib.util.spec_from_file_location("priority_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = priority_store.kw["bind"]
    with engine.begin() as connection:
        ops = Operations(MigrationContext.configure(connection))
        ZktCustodySchedule.__table__.drop(connection)
        ops.drop_index("ix_add_zkt_work_recent_live", table_name="add_zkt_custody_work")
        ops.drop_index("ix_add_zkt_work_revision_hold", table_name="add_zkt_custody_work")
        monkeypatch.setattr(migration, "op", ops)
        migration.upgrade()
        migration.upgrade()
        tables = {"add_zkt_custody_schedule", "add_zkt_custody_work"}
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare_to: kind != "table" or name in tables})
        assert compare_metadata(context, Base.metadata) == []
    with priority_store() as db:
        db.add(ZktCustodySchedule(connector_id=db.scalar(select(Connector.id)), priority_burst=7))
        db.commit()
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with priority_store() as db:
        assert db.scalar(select(ZktCustodySchedule.priority_burst)) == 7
