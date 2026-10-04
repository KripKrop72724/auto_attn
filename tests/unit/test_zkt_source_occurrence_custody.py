"""Canonical coordinates commit with raw custody, without interpretation."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from test_zkt_custody import custody as custody, observation, batch, count
from test_zkt_raw_source_custody import source_store as source_store, prepare, apply
from test_zkt_source_load import store as store
from zk_add import web, zkt_custody as custody_api, zkt_custody_work as work
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, ReconciliationCoverage,
    ReconciliationJob, SourceTailChunk, TerminalRecordManifest, TerminalSourceEpoch,
    ZktCustodyWork, ZktObservationLink, ZktObservationReceipt, ZktOccurrenceAlias)
from zk_add.schemas import Envelope
from zk_add.time_utils import utc_now


def manifests(db, connector, length=2):
    epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
    db.add(epoch)
    db.flush()
    rows = [TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
        terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=index,
        canonical_source=True, raw_record_digest=observation()["raw_digest"],
        terminal_record_key=observation()["raw_digest"], disposition="MALFORMED") for index in range(length)]
    db.add_all(rows)
    db.commit()
    return epoch, rows


def test_raw_ledger_and_later_journal_reuse_same_occurrence(source_store):
    with source_store() as db:
        connector, request = prepare(db, "tail")
        apply(db, connector, "tail", request)
        db.commit()
        original = {row.ordinal: (row.id, row.occurrence_id)
                    for row in db.scalars(select(ZktOccurrenceAlias))}
        raw = base64.b64decode(request.records[0].raw_record_b64)
        values = [observation(index + 1, terminal_serial="TEST-LOAD", raw_format="SOURCE_RECORD",
            observation_id=custody_api.observation_id("TEST-LOAD", "a" * 32, index + 1),
            raw_b64=base64.b64encode(raw).decode(), raw_digest=hashlib.sha256(raw).hexdigest(),
            occurrence={"source_epoch": request.source_epoch, "ordinal": index}) for index in range(2)]
        reply = custody_api.settle_observations(db, connector, batch(*values))
        db.commit()
        assert [row["occurrence_id"] for row in reply["items"]] == [original[index][1] for index in range(2)]
        assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 2
        assert {row.ordinal: (row.id, row.occurrence_id)
                for row in db.scalars(select(ZktOccurrenceAlias))} == original
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
        assert all(row.disposition == "RAW_PRESERVED" for row in db.scalars(select(TerminalRecordManifest)))


def test_alias_insert_failure_cannot_commit_range_or_cursor(source_store, monkeypatch):
    with source_store() as db:
        connector, request = prepare(db, "tail")
        db.commit()
        identifier = connector.id
    @contextmanager
    def scope():
        with source_store() as db, db.begin():
            yield db
    engine = source_store.kw["bind"]
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO add_zkt_occurrence_aliases"):
            raise SQLAlchemyError("synthetic alias persistence failure")
    monkeypatch.setattr(web, "session_scope", scope)
    event.listen(engine, "before_cursor_execute", fail)
    try:
        envelope = Envelope(connector_id="synthetic-load", message_id=str(uuid4()), boot_id="test",
            seq=1, sent_at=utc_now(), type="source_tail_chunk", payload=request.model_dump(mode="json"))
        with pytest.raises(SQLAlchemyError, match="alias persistence failure"):
            web.persist_envelope(identifier, envelope)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with source_store() as db:
        assert all(count(db, model) == 0 for model in (TerminalRecordManifest, ZktCustodyWork,
            ZktOccurrenceAlias, SourceTailChunk, AttendanceEvent, OrdsOutbox))
        assert db.scalar(select(ReconciliationCoverage)).source_committed_cursor == 0
        assert db.scalar(select(ReconciliationJob)).committed_next_ordinal == 0
        connector = db.scalar(select(Connector))
        assert apply(db, connector, "tail", request)[2] is False
        db.commit()
        assert count(db, ZktOccurrenceAlias) == 2


@pytest.mark.parametrize("field,value", [
    ("connector_id", 999), ("zkt_device_id", 999), ("terminal_serial", "REPLACED"),
    ("canonical_source", False), ("source_epoch_id", None), ("generation", 2),
    ("ordinal", -1), ("ordinal", 2**31), ("raw_record_digest", "not-a-digest"),
])
def test_changed_source_scope_refuses_entire_batch(custody, field, value):
    db, connector = custody
    _epoch, rows = manifests(db, connector)
    setattr(rows[-1], field, value)
    with db.no_autoflush, pytest.raises(custody_api.SourceAssociationError, match="BINDING"):
        custody_api.bind_manifest_occurrences(db, connector, rows)
    assert not any(isinstance(row, ZktOccurrenceAlias) for row in db.new)
    db.rollback()
    assert count(db, ZktOccurrenceAlias) == 0


@pytest.mark.parametrize("field,value", [
    ("occurrence_id", "d" * 64), ("raw_digest", "e" * 64), ("ordinal", 99),
    ("manifest_id", 999), ("zkt_device_id", 999), ("source_epoch_id", 999),
])
def test_corrupt_retained_alias_never_overwritten_or_partially_extended(custody, field, value):
    db, connector = custody
    _epoch, rows = manifests(db, connector)
    # A new earlier row cannot be inserted before a later retained conflict is checked.
    alias = custody_api.bind_manifest_occurrences(db, connector, [rows[1]])[rows[1].id]
    setattr(alias, field, value)
    db.commit()
    with pytest.raises(custody_api.SourceAssociationError, match="CONFLICT"):
        custody_api.bind_manifest_occurrences(db, connector, rows)
    assert not any(isinstance(row, ZktOccurrenceAlias) for row in db.new)
    assert count(db, ZktOccurrenceAlias) == 1 and getattr(alias, field) == value


def test_batch_queries_bounded_and_replay_keeps_existing_attendance_link(custody):
    db, connector = custody
    _epoch, rows = manifests(db, connector, 100)
    # A retained alias is never reassigned because its manifest was edited.
    now = utc_now()
    attendance = AttendanceEvent(event_uid="retained-original-oracle-key", connector_id=connector.id,
        zkt_device_id=connector.zkt_device.id, device_serial="TEST01", user_id="1007",
        device_event_time=now, captured_at=now, source="LIVE", status="1", punch="0",
        ords_status="ACKED_CHECK", oracle_confirmed_at=now, raw_event={"original": True})
    db.add(attendance)
    db.flush()
    db.add(OrdsOutbox(attendance_event_id=attendance.id, status="ACKED_CHECK", acknowledged_at=now))
    rows[0].attendance_event_id = attendance.id
    db.commit()
    queries = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            queries.append(statement)
    # Materialize ORM objects before measuring the two set-based reads.
    connector.zkt_device
    for row in rows:
        row.id
    event.listen(db.connection(), "before_cursor_execute", capture)
    try:
        first = custody_api.bind_manifest_occurrences(db, connector, rows)
    finally:
        event.remove(db.connection(), "before_cursor_execute", capture)
    assert len(queries) == 2
    db.commit()
    retained = first[rows[0].id]
    rows[0].attendance_event_id = None
    db.flush()
    second = custody_api.bind_manifest_occurrences(db, connector, rows)
    assert second[rows[0].id].id == retained.id and retained.attendance_event_id == attendance.id
    assert custody_api.source_occurrence_delivery_hold(db, connector, retained.occurrence_id) == "SOURCE_ATTENDANCE_LINK_CHANGED"
    assert count(db, ZktOccurrenceAlias) == 100
    assert attendance.event_uid == "retained-original-oracle-key" and attendance.ords_status == "ACKED_CHECK"
    assert db.scalar(select(OrdsOutbox)).status == "ACKED_CHECK"


@pytest.mark.parametrize("case", ["duplicate", "oversized", "unpersisted"])
def test_invalid_batch_has_no_partial_aliases(custody, case):
    db, connector = custody
    _epoch, rows = manifests(db, connector, 101 if case == "oversized" else 1)
    if case == "duplicate":
        rows *= 2
    elif case == "unpersisted":
        rows = [TerminalRecordManifest()]
    with pytest.raises(custody_api.SourceAssociationError):
        custody_api.bind_manifest_occurrences(db, connector, rows)
    assert count(db, ZktOccurrenceAlias) == 0


def test_changed_epoch_or_generation_isolated_from_later_observations(custody):
    db, connector = custody
    epoch, rows = manifests(db, connector)
    rows[0].generation = 2
    db.commit()
    values = [observation(index + 1, raw_format="SOURCE_RECORD",
        occurrence={"source_epoch": epoch.epoch_id, "ordinal": index}) for index in range(2)]
    reply = custody_api.settle_observations(db, connector, batch(*values))
    db.commit()
    assert reply["items"][0]["occurrence_id"] is None
    assert reply["items"][1]["occurrence_id"]
    assert count(db, ZktObservationReceipt) == 2 and count(db, ZktOccurrenceAlias) == 1
    assert db.scalar(select(ZktCustodyWork).order_by(ZktCustodyWork.id)).reason_code == "SOURCE_OCCURRENCE_BINDING"


def test_inspection_reconstructs_precontract_missing_alias_but_holds_changed_alias(source_store):
    with source_store() as db:
        connector, request = prepare(db, "tail", length=1)
        apply(db, connector, "tail", request)
        db.commit()
        alias = db.scalar(select(ZktOccurrenceAlias))
        identity = alias.occurrence_id
        db.delete(alias)  # Simulate a backup from before range aliases were introduced.
        db.commit()
        assert work.advance_work(db) == 1
        db.commit()
        alias = db.scalar(select(ZktOccurrenceAlias))
        assert alias.occurrence_id == identity
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
        alias.raw_digest = "e" * 64
        obligation = db.scalar(select(ZktCustodyWork))
        obligation.next_attempt_at = utc_now()
        db.commit()
        assert work.advance_work(db) == 1
        assert obligation.state == "HELD_EXCEPTION" and obligation.reason_code == "SOURCE_OCCURRENCE_CONFLICT"
        assert alias.raw_digest == "e" * 64


def test_concurrent_source_range_and_journal_share_one_occurrence(source_store):
    if source_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL connector serialization")
    with source_store() as db:
        _connector, request = prepare(db, "tail", length=1)
        db.commit()
    value = observation(1, terminal_serial="TEST-LOAD", raw_format="SOURCE_RECORD",
        observation_id=custody_api.observation_id("TEST-LOAD", "a" * 32, 1),
        raw_b64=request.records[0].raw_record_b64, raw_digest=request.records[0].raw_record_digest,
        occurrence={"source_epoch": request.source_epoch, "ordinal": 0})
    def submit(kind):
        with source_store() as db:
            connector = db.scalar(select(Connector).with_for_update())
            if kind == "source":
                apply(db, connector, "tail", request)
            else:
                custody_api.settle_observations(db, connector, batch(value))
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(submit, ("source", "journal")))
    with source_store() as db:
        assert work.advance_work(db) == 2
        db.commit()
        assert count(db, ZktOccurrenceAlias) == count(db, ZktObservationLink) == 1
        alias = db.scalar(select(ZktOccurrenceAlias))
        connector = db.scalar(select(Connector))
        reply = custody_api.settle_observations(db, connector, batch(value))
        assert reply["items"][0]["occurrence_id"] == alias.occurrence_id
        assert reply["items"][0]["replay"]
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
