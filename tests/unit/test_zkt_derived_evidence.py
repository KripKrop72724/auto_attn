"""Synthetic provenance, crash, fairness and correction evidence; no field proof."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import SQLAlchemyError

from test_zkt_custody import custody as custody, observation, batch, count
from test_zkt_custody_work import packet_fragments, packet_observation
from test_zkt_decode import ENCODED, live, packet
from test_zkt_raw_source_custody import source_store as source_store, prepare, apply
from test_zkt_source_load import store as store
from zk_add import zkt_custody_work as work, zkt_derived_evidence as derived
from zk_add.crypto import decrypt_json
from zk_add.db import Base
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, TerminalRecordManifest,
    ZktCustodyWork, ZktDerivedEvidence, ZktObservationReceipt)
from zk_add.zkt_custody import observation_id, settle_observations
from zk_add.settings import settings


def steps(db):
    return db.scalars(select(ZktDerivedEvidence).order_by(ZktDerivedEvidence.id)).all()


def source_observation(size, **updates):
    raw = bytearray(size)
    if size == 8:
        struct.pack_into("<HBI", raw, 0, 456, 7, ENCODED)
        raw[7] = 2
    elif size == 16:
        struct.pack_into("<IIBB", raw, 0, 123, ENCODED, 7, 2)
    else:
        struct.pack_into("<H", raw, 0, 456)
        raw[2:5] = b"123"
        raw[26] = 7
        struct.pack_into("<I", raw, 27, ENCODED)
        raw[31] = 2
    return observation(raw_format="SOURCE_RECORD", raw_b64=base64.b64encode(raw).decode(),
                       raw_digest=hashlib.sha256(raw).hexdigest(), **updates)


def test_new_packet_history_is_read_once_per_locked_batch(custody):
    db, connector = custody
    db.autoflush = False
    raw = packet(live(32))
    for sequence in range(1, 17):
        settle_observations(db, connector, batch(packet_observation(sequence, raw)))
    db.commit()
    receipts = list(db.scalars(select(ZktObservationReceipt.protected_observation)))
    queries = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM add_zkt_derived_evidence" in statement:
            queries.append(statement)
    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert work.advance_work(db, limit=16) == 16
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(queries) == 1  # One bounded absence proof instead of 32 per-work queries.
    db.commit()
    assert count(db, ZktDerivedEvidence) == 16
    for obligation in db.scalars(select(ZktCustodyWork)):
        evidence = list(derived.verified_steps(db, obligation, raw))
        assert len(evidence) == 1 and evidence[0]["result"] == "UNQUALIFIED_FACTS"
        assert evidence[0]["records"][0]["facts"]["user_id"] == "123"
    assert list(db.scalars(select(ZktObservationReceipt.protected_observation))) == receipts
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


@pytest.mark.parametrize("invalidate", ["revision", "transaction", "context_exit"])
def test_empty_history_proof_cannot_outlive_its_input_or_transaction(custody, invalidate):
    db, connector = custody
    raw = packet(live(32))
    settle_observations(db, connector, batch(packet_observation(1, raw)))
    db.commit()
    obligation = db.scalar(select(ZktCustodyWork))
    queries = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM add_zkt_derived_evidence" in statement:
            queries.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        with derived.initial_evidence_batch(db, [obligation]):
            if invalidate == "revision":
                obligation.evidence_revision += 1
            elif invalidate == "transaction":
                db.rollback()
            if invalidate != "context_exit":
                derived.derive_step(db, obligation, raw)
        if invalidate == "context_exit":
            derived.derive_step(db, obligation, raw)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    # The initial proof is stale, so both normal chain/provenance reads run.
    assert len(queries) == 3
    assert count(db, ZktDerivedEvidence) == 1


def test_empty_history_context_cleans_up_after_failure_and_rejects_nesting(custody):
    db, connector = custody
    raw = packet(live(32))
    settle_observations(db, connector, batch(packet_observation(1, raw)))
    db.commit()
    obligation = db.scalar(select(ZktCustodyWork))
    with pytest.raises(RuntimeError, match="interrupted"):
        with derived.initial_evidence_batch(db, [obligation]):
            with pytest.raises(ValueError, match="PREFETCH_NESTING"):
                with derived.initial_evidence_batch(db, [obligation]):
                    pass
            raise RuntimeError("synthetic interrupted batch")
    with derived.initial_evidence_batch(db, [obligation]):
        row = derived.derive_step(db, obligation, raw)
    assert row.result == "UNQUALIFIED_FACTS" and count(db, ZktDerivedEvidence) == 1
    with pytest.raises(ValueError, match="PREFETCH_BOUNDS"):
        with derived.initial_evidence_batch(db, [obligation] * 1001):
            pass


@pytest.mark.parametrize("fail_write", [False, True])
def test_independent_steps_batch_writes_and_rollback_together(store, monkeypatch, fail_write):
    if store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL write batching")
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    with store() as db:
        db.autoflush = False  # Match the actual worker's session policy.
        connector = db.scalar(select(Connector))
        connector.zkt_custody_enabled = True
        for sequence in range(1, 17):
            value = packet_observation(sequence, packet(live(32)), terminal_serial="TEST-LOAD")
            value["observation_id"] = observation_id("TEST-LOAD", value["capture_epoch"], sequence)
            settle_observations(db, connector, batch(value))
        db.commit()
        receipts = list(db.scalars(select(ZktObservationReceipt.protected_observation)))
        writes = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO add_zkt_derived_evidence"):
                writes.append(statement)
                if fail_write:
                    raise SQLAlchemyError("synthetic interrupted batched evidence write")
        connection = db.connection()
        event.listen(connection, "after_cursor_execute", capture)
        try:
            if fail_write:
                with pytest.raises(SQLAlchemyError, match="interrupted batched"):
                    work.advance_work(db, limit=16)
            else:
                assert work.advance_work(db, limit=16) == 16
        finally:
            event.remove(connection, "after_cursor_execute", capture)
        assert len(writes) == 1
        if fail_write:
            db.rollback()
            assert count(db, ZktDerivedEvidence) == 0
            assert all(row.attempt_count == 0 and row.state == "PENDING"
                       for row in db.scalars(select(ZktCustodyWork)))
            assert work.advance_work(db, limit=16) == 16
        db.commit()
        assert count(db, ZktDerivedEvidence) == 16
        assert all(row.attempt_count == 1 and row.state == "WAIT_PROFILE"
                   for row in db.scalars(select(ZktCustodyWork)))
        assert list(db.scalars(select(ZktObservationReceipt.protected_observation))) == receipts
        assert work.advance_work(db) == 0
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


@pytest.mark.parametrize("size", [8, 16, 40])
def test_historical_uid_stays_separate_and_receipts_do_not_change(custody, size):
    db, connector = custody
    value = source_observation(size)
    result = settle_observations(db, connector, batch(value))
    db.commit()
    receipt = db.scalar(select(ZktObservationReceipt))
    original = receipt.protected_observation
    assert work.advance_work(db) == 1
    db.commit()
    row = steps(db)[0]
    evidence = decrypt_json(row.protected_evidence)
    assert row.result == "UNQUALIFIED_FACTS" and row.record_count == 1
    assert evidence["authority"] == "UNQUALIFIED"
    facts = evidence["records"][0]["facts"]
    assert facts["user_id"] == (None if size == 8 else "123")
    assert facts["attendance_uid"] == (None if size == 16 else 456)
    assert facts["local_time"] == "2026-10-03T09:14:22+05:00"
    assert facts["utc_time"] == "2026-10-03T04:14:22+00:00"
    assert receipt.protected_observation == original
    assert work.advance_work(db) == 0
    replay = settle_observations(db, connector, batch(value))
    assert replay["items"][0]["receipt_id"] == result["items"][0]["receipt_id"]
    assert count(db, ZktDerivedEvidence) == 1
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_bad_record_keeps_its_offset_and_later_valid_records(custody):
    db, connector = custody
    raw = packet(live(32, user=123) + live(32, month=0) + live(32, user=234))
    settle_observations(db, connector, batch(packet_observation(1, raw)))
    assert work.advance_work(db) == 1
    row = steps(db)[0]
    value = decrypt_json(row.protected_evidence)
    assert row.result == "DECODE_REJECTED"
    layout = [item for item in value["records"] if item["length"] == 32]
    assert [item["offset"] for item in layout] == [8, 40, 72]
    assert [item["error_code"] for item in layout] == [None, "INVALID_CALENDAR_DATE", None]
    assert [item["facts"]["user_id"] for item in (layout[0], layout[2])] == ["123", "234"]
    assert layout[1]["raw_digest"] == hashlib.sha256(raw[40:72]).hexdigest()
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_two_plausible_layouts_and_reported_profile_do_not_authorize_a_guess(custody):
    db, connector = custody
    body = bytearray(live(12, user=65) * 3)
    body[24:32] = bytes([7, 2, 26, 10, 3, 9, 14, 7])
    settle_observations(db, connector, batch(packet_observation(1, packet(body),
        decoder_profile="zkt-g3-v1", decoder_version="qualified-looking-claim")))
    assert work.advance_work(db) == 1
    row = steps(db)[0]
    evidence = decrypt_json(row.protected_evidence)
    assert row.result == "AMBIGUOUS_LAYOUT" and evidence["plausible_layouts"] == [12, 36]
    assert evidence["reported_profile"] == "zkt-g3-v1"
    assert not evidence["plan"]["header"]["checksum_verified"]
    assert not evidence["plan"]["header"]["session_binding_verified"]
    assert db.scalar(select(ZktCustodyWork)).state == "WAIT_PROFILE"


@pytest.mark.parametrize("raw,kind,error", [(b"x", "LIVE_PACKET", "LIVE_PACKET_BOUNDARY"),
    (packet(live(32), session=0), "LIVE_PACKET", "LIVE_PACKET_SESSION_OR_COMMAND"),
    (packet(live(32), command=1), "LIVE_PACKET", "LIVE_PACKET_SESSION_OR_COMMAND"),
    (packet(live(32)) + b"x", "LIVE_PACKET", "LIVE_RECORD_BOUNDARY"),
    (live(32), "LIVE_FRAME", "LIVE_FRAME_CONTRACT_UNSPECIFIED")])
def test_unknown_or_invalid_framing_retains_an_explicit_derived_reason(custody, raw, kind, error):
    db, connector = custody
    value = packet_observation(1, raw)
    value["raw_format"] = kind
    settle_observations(db, connector, batch(value))
    work.advance_work(db)
    row = steps(db)[0]
    assert row.record_count == 0 and row.result == "DECODE_REJECTED"
    assert decrypt_json(row.protected_evidence)["plan"]["framing_error"] == error


def test_maximum_packet_resumes_in_bounded_steps_without_losing_duplicate_occurrences(custody):
    db, connector = custody
    raw = packet(live(12) * 5460)  # 65,528 bytes, close to the maximum packet.
    values = packet_fragments(raw)
    for start in range(0, len(values), 50):
        settle_observations(db, connector, batch(*values[start:start + 50]))
        db.commit()
    obligation = db.scalar(select(ZktCustodyWork))
    initial_receipts = count(db, ZktObservationReceipt)
    calls = 0
    while work.advance_work(db, limit=1):
        calls += 1
        db.commit()
        assert calls <= derived.MAX_STEPS
        latest = db.scalar(select(ZktDerivedEvidence).order_by(ZktDerivedEvidence.id.desc()))
        assert latest.record_count <= 128 and len(latest.protected_evidence) <= derived.MAX_CIPHERTEXT
        # Reopen the transaction/identity map after every committed step.
        db.expire_all()
    assert calls > 1 and obligation.state == "WAIT_PROFILE"
    packets = list(derived.verified_steps(db, obligation, raw))
    records = [record for step in packets for record in step["records"] if record["length"] == 12]
    assert len(records) == 5460 and {row["offset"] for row in records} == set(range(8, len(raw), 12))
    assert len({row["raw_digest"] for row in records}) == 1  # Same bytes, separate occurrences.
    assert all(row["facts"]["user_id"] == "123" for row in records)
    assert packets[-1]["result"] == "UNQUALIFIED_FACTS"
    settle_observations(db, connector, batch(values[0]))
    assert work.advance_work(db) == 0
    assert count(db, ZktObservationReceipt) == initial_receipts
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_incomplete_fragments_have_no_derived_semantics(custody):
    db, connector = custody
    values = packet_fragments(packet(live(32) * 20))
    settle_observations(db, connector, batch(values[0]))
    assert work.advance_work(db) == 1
    assert count(db, ZktDerivedEvidence) == 0
    assert db.scalar(select(ZktCustodyWork)).state == "WAIT_FRAGMENTS"
    assert work.advance_work(db) == 0


def test_decoder_change_appends_correction_evidence_without_overwriting_old_facts(custody, monkeypatch):
    db, connector = custody
    raw = packet(live(32))
    settle_observations(db, connector, batch(packet_observation(1, raw)))
    assert work.advance_work(db) == 1
    db.commit()
    old = steps(db)[0]
    original = old.protected_evidence
    monkeypatch.setattr(derived, "INTERPRETATION_VERSION", "synthetic-correction-v2")
    assert not work.work_status(db, connector)["rows"][0]["decoding"]["current_decoder"]
    from dataclasses import replace
    decoder = derived.decode_live_record
    def changed(*args, **kwargs):
        return replace(decoder(*args, **kwargs), status=8)
    monkeypatch.setattr(derived, "decode_live_record", changed)
    assert work.advance_work(db) == 1
    db.commit()
    assert old.protected_evidence == original
    assert [decrypt_json(row.protected_evidence)["records"][0]["facts"]["status"] for row in steps(db)] == [7, 8]
    assert decrypt_json(steps(db)[1].protected_evidence)["prior_interpretation"] == dict(
        id=old.id, version=old.interpretation_version, evidence_digest=old.evidence_digest)
    assert work.advance_work(db) == 0
    assert db.scalar(select(ZktCustodyWork)).state == "WAIT_PROFILE"
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_transaction_failure_restarts_at_last_committed_step(custody, monkeypatch):
    db, connector = custody
    values = packet_fragments(packet(live(12) * 129))
    settle_observations(db, connector, batch(*values))
    db.commit()
    assert work.advance_work(db) == 1
    db.commit()
    old = steps(db)[0].protected_evidence
    original = derived.derive_step
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        args[0].flush()
        raise SQLAlchemyError("synthetic post-insert failure")
    monkeypatch.setattr(derived, "derive_step", fail)
    with pytest.raises(SQLAlchemyError):
        work.advance_work(db)
    db.rollback()
    assert len(steps(db)) == 1 and steps(db)[0].protected_evidence == old
    assert db.scalar(select(ZktCustodyWork)).state == "INTERPRETING"
    monkeypatch.setattr(derived, "derive_step", original)
    assert work.advance_work(db) == 1
    db.commit()
    assert len(steps(db)) == 2 and steps(db)[0].protected_evidence == old
    assert work.advance_work(db) == 0


@pytest.mark.parametrize("mutation", ["ciphertext", "digest", "missing-step", "previous-digest"])
def test_corrupt_derived_history_is_held_without_rewriting_it(custody, mutation):
    db, connector = custody
    values = packet_fragments(packet(live(12) * 300))
    settle_observations(db, connector, batch(*values))
    assert work.advance_work(db) == 1
    assert work.advance_work(db) == 1
    db.commit()
    rows = steps(db)
    if mutation == "ciphertext":
        rows[-1].protected_evidence = "corrupt synthetic ciphertext"
    elif mutation == "digest":
        rows[-1].evidence_digest = "f" * 64
    elif mutation == "missing-step":
        db.delete(rows[0])
    else:
        rows[-1].previous_digest = "f" * 64
    db.commit()
    assert work.advance_work(db) == 1
    assert db.scalar(select(ZktCustodyWork)).state == "HELD_EXCEPTION"
    assert db.scalar(select(ZktCustodyWork)).reason_code.startswith("DERIVED_")
    assert work.advance_work(db) == 0
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_earlier_corrupted_ciphertext_cannot_be_used_by_an_evidence_consumer(custody):
    db, connector = custody
    raw = packet(live(12) * 129)
    settle_observations(db, connector, batch(*packet_fragments(raw)))
    while work.advance_work(db):
        db.commit()
    rows = steps(db)
    rows[0].protected_evidence = "corrupt synthetic first step"
    db.commit()
    with pytest.raises(derived.DerivedEvidenceInvalid, match="CIPHERTEXT_CHANGED"):
        list(derived.verified_steps(db, db.scalar(select(ZktCustodyWork)), raw))


def test_status_reports_only_metadata_and_never_qualification(custody):
    db, connector = custody
    settle_observations(db, connector, batch(packet_observation(1, packet(live(32, user=123456)))))
    work.advance_work(db)
    result = work.work_status(db, connector)
    evidence = result["rows"][0]["decoding"]
    assert evidence["result"] == "UNQUALIFIED_FACTS" and evidence["authority"] == "UNQUALIFIED"
    rendered = json.dumps(result, default=str)
    for private in ("123456", "raw_digest", "protected_evidence", '"facts":', "local_time"):
        assert private not in rendered
    assert result["oracle_completion"] == "NOT_ASSERTED"


def test_disabled_connector_is_not_activated_by_a_decoder_revision(custody, monkeypatch):
    db, connector = custody
    settle_observations(db, connector, batch(packet_observation(1, packet(live(32)))))
    connector.zkt_custody_enabled = False
    db.commit()
    assert work.advance_work(db) == 0 and count(db, ZktDerivedEvidence) == 0
    monkeypatch.setattr(derived, "INTERPRETATION_VERSION", "synthetic-other-v2")
    assert work.advance_work(db) == 0 and count(db, ZktDerivedEvidence) == 0


def test_new_extent_marks_previous_decoding_historical_until_inspected(custody):
    db, connector = custody
    raw = packet(live(32) * 20)
    values = packet_fragments(raw)
    settle_observations(db, connector, batch(*values))
    work.advance_work(db)
    assert work.work_status(db, connector)["rows"][0]["decoding"]["current_input"]
    # A separately captured duplicate extent carries another custody receipt.
    repeated = packet_fragments(raw, first_sequence=20)[0]
    settle_observations(db, connector, batch(repeated))
    assert not work.work_status(db, connector)["rows"][0]["decoding"]["current_input"]
    work.advance_work(db)
    assert work.work_status(db, connector)["rows"][0]["decoding"]["current_input"]
    assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_canonical_source_keeps_original_bytes_and_classification(source_store):
    with source_store() as db:
        connector, request = prepare(db, "tail", length=2)
        apply(db, connector, "tail", request)
        db.commit()
        original = [row.protected_raw_record for row in db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.id))]
        assert work.advance_work(db) == 2
        db.commit()
        manifests = db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.id)).all()
        assert [row.protected_raw_record for row in manifests] == original
        assert all(row.disposition == "RAW_PRESERVED" and row.raw_timestamp is None for row in manifests)
        assert all(row.result == "DECODE_REJECTED" for row in steps(db))
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
        assert work.advance_work(db) == 0


def test_two_postgres_inspectors_commit_one_interpretation_per_occurrence(source_store):
    if source_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock qualification")
    with source_store() as db:
        connector, request = prepare(db, "tail", length=2)
        apply(db, connector, "tail", request)
        db.commit()
    def inspect(_):
        with source_store() as db:
            result = work.advance_work(db)
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(inspect, range(2))) == [0, 2]
    with source_store() as db:
        assert count(db, ZktDerivedEvidence) == 2
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_current_holds_do_not_redecode_and_one_old_revision_is_found_among_200000(store, monkeypatch):
    if store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL scheduler workload")
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    with store() as db:
        connector = db.scalar(select(Connector))
        connector.zkt_custody_enabled = True
        for first in range(0, 200000, 5000):
            db.execute(insert(ZktCustodyWork), [dict(work_key=f"{index:064x}", connector_id=connector.id,
                kind="SOURCE_LEDGER", terminal_serial="TEST-LOAD", state="WAIT_PROFILE",
                reason_code="PROFILE_QUALIFICATION_REQUIRED", owner="ADD_PROTOCOL",
                interpretation_version=derived.INTERPRETATION_VERSION,
                evidence_revision=1, processed_revision=1) for index in range(first, first + 5000)])
        db.commit()
        db.execute(text("ANALYZE add_zkt_custody_work"))
        db.execute(text("SET LOCAL statement_timeout = '2000ms'"))
        assert work.advance_work(db) == 0
        row = db.scalar(select(ZktCustodyWork).order_by(ZktCustodyWork.id.desc()).limit(1))
        row.interpretation_version = "synthetic-older-version"
        db.flush()
        result = work.advance_work_batch(db, limit=17, time_budget_ms=250)
        assert result.processed == 1
        # This synthetic obligation deliberately has no source row; only that
        # one group becomes an evidence hold, never an attendance row.
        assert row.state == "HELD_EXCEPTION" and row.reason_code == "SOURCE_WORK_EVIDENCE_CHANGED"
        assert work.advance_work(db) == 0
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == count(db, ZktDerivedEvidence) == 0


def test_additive_migration_is_idempotent_and_downgrade_keeps_evidence(source_store, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261004_0048_zkt_derived_evidence.py"
    spec = importlib.util.spec_from_file_location("derived_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = source_store.kw["bind"]
    with engine.begin() as connection:
        from zk_add.models import ZktSourceAttendance
        ops = Operations(MigrationContext.configure(connection))
        ZktSourceAttendance.__table__.drop(connection)
        ZktDerivedEvidence.__table__.drop(connection)
        ops.drop_index("ix_add_zkt_work_interpretation", table_name="add_zkt_custody_work")
        # The fixture starts at current metadata. Remove the later 0049
        # dependency before recreating the column introduced by 0048.
        future_index = next(index for index in ZktCustodyWork.__table__.indexes
                            if index.name == "ix_add_zkt_work_revision_hold")
        future_index.drop(connection)
        ops.drop_column("add_zkt_custody_work", "interpretation_version")
        monkeypatch.setattr(migration, "op", ops)
        migration.upgrade()
        migration.upgrade()
        ZktSourceAttendance.__table__.create(connection)
        future_index.create(connection)
        tables = {"add_zkt_custody_work", "add_zkt_derived_evidence"}
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare_to: kind != "table" or name in tables})
        assert compare_metadata(context, Base.metadata) == []
    with source_store() as db:
        connector, request = prepare(db, "tail", length=1)
        apply(db, connector, "tail", request)
        work.advance_work(db)
        db.commit()
        original = steps(db)[0].protected_evidence
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with source_store() as db:
        assert steps(db)[0].protected_evidence == original
