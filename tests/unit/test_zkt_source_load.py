"""Workload grain, incomplete evidence, query budgets and the actual admin API."""
from datetime import datetime, timedelta, timezone
import json
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, create_engine, event, insert, select, text
from sqlalchemy.orm import sessionmaker

from zk_add.db import Base
from zk_add.models import Connector, ReconciliationCoverage, ReconciliationJob, TerminalRecordManifest, TerminalSourceEpoch, ZKTDevice
from zk_add.zkt_decode import PAKISTAN_TIME, encode_time
from zk_add import zkt_source_load as load

NOW = datetime(2026, 10, 4, 2, tzinfo=PAKISTAN_TIME)
END = NOW.replace(hour=0)
START = END - timedelta(days=30)


@pytest.fixture(params=["sqlite", "postgres"])
def store(tmp_path, request):
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "zkt_load_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'load.db'}", connect_args={"check_same_thread": False})
    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    request.addfinalizer(cleanup)
    # PostgreSQL defers cyclic constraints by mutating their DDL rules. Keep
    # that dialect-specific state out of later SQLite fixtures in this process.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        connector = Connector(connector_id="synthetic-load", hardware_id="aa:bb:cc:dd:ee:02",
            zone_id="test", zone_name="test", device_id="test", display_name="test", onboarding_generation=1)
        db.add(connector)
        db.flush()
        connector.zkt_device = ZKTDevice(connector_id=connector.id, serial="TEST-LOAD", confirmed_serial="TEST-LOAD",
            attendance_count=0)
        db.flush()
        epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
        db.add(epoch)
        db.flush()
        job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
            reason="Synthetic workload measurement", idempotency_key="load-test", request_digest="a" * 64,
            source_epoch_id=epoch.id, terminal_serial="TEST-LOAD", terminal_generation=1)
        db.add(job)
        db.flush()
        db.add(ReconciliationCoverage(zkt_device_id=connector.zkt_device.id, job_id=job.id,
            source_epoch_id=epoch.id, terminal_serial="TEST-LOAD", terminal_generation=1,
            certified_source_cursor=0, source_chain_digest="a" * 64, source_committed_cursor=0,
            source_committed_chain_digest="a" * 64, capture_state="SOURCE_CAPTURE_CERTIFIED", oracle_state="PENDING"))
        db.commit()
    yield sessions


def rows(db, dates, **changes):
    connector = db.scalar(select(Connector))
    coverage = db.scalar(select(ReconciliationCoverage))
    for ordinal, date in enumerate(dates):
        values = dict(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST-LOAD", generation=1, source_epoch_id=coverage.source_epoch_id, ordinal=ordinal,
            record_size=40, raw_record_digest="b" * 64, terminal_record_key="c" * 64,
            disposition="EVENT", protected_raw_record="protected-synthetic-byte-evidence",
            observed_user_id="PRIVATE-EMPLOYEE", raw_timestamp=encode_time(date), canonical_source=True)
        db.add(TerminalRecordManifest(**{**values, **changes}))
    coverage.source_committed_cursor = coverage.certified_source_cursor = len(dates)
    connector.zkt_device.attendance_count = len(dates)
    db.commit()
    return connector, coverage


def test_source_occurrences_keep_same_second_multiplicity_and_ignore_old_epochs(store):
    with store() as db:
        connector, coverage = rows(db, [START-timedelta(seconds=1), START, START, START+timedelta(seconds=59),
                                       END-timedelta(seconds=1), END])
        old = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=2, state="SUPERSEDED")
        db.add(old)
        db.flush()
        for canonical, epoch in [(False, coverage.source_epoch_id), (True, old.id)]:
            db.add(TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
                terminal_serial="TEST-LOAD", generation=1, source_epoch_id=epoch, ordinal=1,
                record_size=40, raw_record_digest="b"*64, terminal_record_key="c"*64, disposition="EVENT",
                protected_raw_record="protected", raw_timestamp=encode_time(START), canonical_source=canonical))
        db.commit()
        result = load.source_load_baseline(db, connector, now=NOW)
        assert result["source_inventory_complete"] and result["coherent_snapshot"]
        assert result["observed_occurrences"] == 4
        assert result["peak_daily_observed_occurrences"] == result["peak_minute_observed_occurrences"] == 3
        assert len(result["daily"]) == 30 and result["daily"][1]["observed_occurrences"] == 0
        assert result["daily"][-1]["observed_occurrences"] == 1
        assert not result["baseline_complete"] and result["qualification"] == "NOT_ASSERTED"
        serialized = json.dumps(result, default=str)
        assert "PRIVATE-EMPLOYEE" not in serialized and "protected-synthetic" not in serialized
        assert load.source_load_baseline(db, connector, now=NOW+timedelta(hours=1))["measurement_digest"] == result["measurement_digest"]


@pytest.mark.parametrize("fault,reason", [("binding", "SOURCE_BINDING_OR_EPOCH_UNVERIFIED"),
    ("epoch", "SOURCE_BINDING_OR_EPOCH_UNVERIFIED"), ("generation", "SOURCE_BINDING_OR_EPOCH_UNVERIFIED"),
    ("chain", "SOURCE_CURSOR_OR_CHAIN_INVALID"), ("inactive", "ACTIVE_SOURCE_COVERAGE_UNAVAILABLE")])
def test_wrong_or_missing_source_scope_does_not_return_zero_as_a_measurement(store, fault, reason):
    with store() as db:
        connector, coverage = rows(db, [START])
        if fault == "binding":
            connector.zkt_device.confirmed_serial = "REPLACED"
        elif fault == "epoch":
            db.get(TerminalSourceEpoch, coverage.source_epoch_id).state = "SUPERSEDED"
        elif fault == "generation":
            connector.onboarding_generation = 2
        elif fault == "chain":
            coverage.source_committed_chain_digest = "bad"
        else:
            coverage.active = False
        db.commit()
        result = load.source_load_baseline(db, connector, now=NOW)
        assert result["observed_occurrences"] is None and result["daily"] == []
        assert reason in result["reasons"] and not result["baseline_complete"]


def test_missing_ordinals_unknown_clock_and_missing_raw_evidence_remain_explicit(store):
    with store() as db:
        connector, coverage = rows(db, [START]*5)
        values = db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.ordinal)).all()
        db.delete(values[1])
        values[2].raw_timestamp = None
        values[3].disposition = "INVALID_TIME"
        values[4].protected_raw_record = None
        connector.zkt_device.attendance_count = 9
        coverage.capture_state = "INVALIDATED"
        db.commit()
        result = load.source_load_baseline(db, connector, now=NOW)
        assert result["observed_occurrences"] == 2 and result["inventoried_occurrences"] == 4
        assert result["unusable_source_records"] == 2 and result["missing_raw_evidence_records"] == 1
        assert not result["source_inventory_complete"] and not result["baseline_complete"]
        assert {"SOURCE_ORDINALS_MISSING", "TERMINAL_COUNT_NOT_COVERED", "SOURCE_CUSTODY_UNCERTIFIED",
            "UNUSABLE_SOURCE_TIMESTAMPS_OR_LAYOUTS", "RAW_SOURCE_EVIDENCE_MISSING"} <= set(result["reasons"])


def test_invalid_calendar_is_not_folded_into_another_day(store):
    now = datetime(2026, 3, 5, 1, tzinfo=PAKISTAN_TIME)
    with store() as db:
        connector, _ = rows(db, [datetime(2026, 2, 28, 1, tzinfo=PAKISTAN_TIME)])
        row = db.scalar(select(TerminalRecordManifest))
        row.raw_timestamp += 86400  # February 29, which does not exist in 2026.
        db.commit()
        result = load.source_load_baseline(db, connector, now=now)
        assert result["invalid_calendar_records_in_window"] == 1 and result["observed_occurrences"] == 0
        assert "INVALID_CALENDAR_RECORDS_IN_WINDOW" in result["reasons"]


def test_changed_snapshot_cannot_qualify_the_previous_count(store, monkeypatch):
    with store() as db:
        connector, _ = rows(db, [START])
        original = load._snapshot
        calls = 0
        def changed(*args):
            nonlocal calls
            calls += 1
            value = original(*args)
            if calls == 2:
                value["chain"] = "d" * 64
            return value
        monkeypatch.setattr(load, "_snapshot", changed)
        result = load.source_load_baseline(db, connector, now=NOW)
        assert result["observed_occurrences"] == 1 and not result["coherent_snapshot"]
        assert "SOURCE_CHANGED_DURING_MEASUREMENT" in result["reasons"]


def test_postgres_lock_timeout_is_safe_and_restores_the_callers_transaction(store):
    if store.kw["bind"].dialect.name != "postgresql":
        return
    with store() as db, store() as holder:
        connector, _ = rows(db, [START])
        previous = db.execute(text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one()
        holder.execute(text("LOCK TABLE add_terminal_record_manifest IN ACCESS EXCLUSIVE MODE"))
        started = time.monotonic()
        with pytest.raises(ValueError, match="^BASELINE_QUERY_DEADLINE$"):
            load.source_load_baseline(db, connector, now=NOW)
        assert time.monotonic() - started < 2
        assert db.execute(text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one() == previous
        holder.rollback()
        assert load.source_load_baseline(db, connector, now=NOW)["observed_occurrences"] == 1


def test_200000_source_records_remain_bounded_and_same_second_punches_are_counted(store):
    if store.kw["bind"].dialect.name != "postgresql":
        return
    with store() as db:
        connector, coverage = rows(db, [])
        encoded_minutes = [encode_time(START + timedelta(minutes=minute)) for minute in range(30 * 24 * 60)]
        for begin in range(0, 200000, 10000):
            db.execute(insert(TerminalRecordManifest), [dict(connector_id=connector.id,
                zkt_device_id=connector.zkt_device.id, terminal_serial="TEST-LOAD", generation=1,
                source_epoch_id=coverage.source_epoch_id, ordinal=i, record_size=40,
                raw_record_digest="b"*64, terminal_record_key="c"*64, disposition="EVENT",
                protected_raw_record="protected", raw_timestamp=encoded_minutes[i % len(encoded_minutes)],
                canonical_source=True) for i in range(begin, begin+10000)])
        coverage.source_committed_cursor = connector.zkt_device.attendance_count = 200000
        db.commit()
        started = time.monotonic()
        result = load.source_load_baseline(db, connector, now=NOW)
        assert time.monotonic() - started < 5
        assert result["source_inventory_complete"] and result["observed_occurrences"] == 200000
        assert result["peak_minute_observed_occurrences"] == 5 and len(result["daily"]) == 30


def test_postgres_inventory_and_minutes_share_one_statement_snapshot(store):
    engine = store.kw["bind"]
    if engine.dialect.name != "postgresql":
        return
    with store() as db:
        connector, _ = rows(db, [START])
        changed = False
        def change_after_read(connection, cursor, statement, parameters, context, executemany):
            nonlocal changed
            if not changed and statement.startswith("SELECT") and "add_terminal_record_manifest" in statement:
                changed = True
                with store() as concurrent:
                    concurrent.execute(text("DELETE FROM add_terminal_record_manifest"))
                    concurrent.commit()
        event.listen(engine, "after_cursor_execute", change_after_read)
        try:
            result = load.source_load_baseline(db, connector, now=NOW)
        finally:
            event.remove(engine, "after_cursor_execute", change_after_read)
        assert changed and result["source_inventory_complete"]
        assert result["inventoried_occurrences"] == result["observed_occurrences"] == 1
        later = load.source_load_baseline(db, connector, now=NOW)
        assert later["observed_occurrences"] == 0 and not later["source_inventory_complete"]


def test_admin_api_requires_authentication_and_does_not_cache_or_reveal_records(store, monkeypatch):
    from fastapi.testclient import TestClient
    from zk_add import web
    from zk_add.security import ADMIN_COOKIE, create_admin_session
    with store() as db:
        connector, _ = rows(db, [START])
        previous = web.app.dependency_overrides.copy()
        def override_db():
            yield db
        web.app.dependency_overrides[web.get_db] = override_db
        monkeypatch.setattr(load, "utc_now", lambda: NOW.astimezone(timezone.utc))
        try:
            client = TestClient(web.app)
            path = f"/api/v1/devices/{connector.connector_id}/source-load-baseline"
            assert client.get(path).status_code == 401
            token, _ = create_admin_session(db, username="test", ip_address="127.0.0.1", user_agent="pytest")
            db.commit()
            client.cookies.set(ADMIN_COOKIE, token)
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.json()["observed_occurrences"] == 1
            assert "no-store" in response.headers["Cache-Control"]
            assert "PRIVATE-EMPLOYEE" not in response.text and "protected-synthetic" not in response.text
            connector.firmware_family = "hikvision"
            db.commit()
            assert client.get(path).status_code == 409
        finally:
            web.app.dependency_overrides.clear()
            web.app.dependency_overrides.update(previous)
