"""Raw source custody has a receipt and an obligation, never inferred attendance."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import func, select

from test_reconciliation import (
    _certify_one_record_baseline, reconciliation_db as reconciliation_db,
)
from test_zkt_source_claim_guard import sealed
from test_zkt_source_load import store as store
from zk_add import reconciliation as reconcile, web, zkt_custody_work as work
from zk_add.attendance_recovery import RecoveryError, _correction_candidate
from zk_add.crypto import decrypt_text, encrypt_text
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent, Connector, OrdsOutbox, ReconciliationChunk, ReconciliationCoverage,
    ReconciliationJob, ReconciliationDivergence, SourceTailChunk, TerminalRecordManifest,
    TerminalRecordReview, TerminalSourceEpoch, ZktCustodyWork, ZktOccurrenceAlias,
)
from zk_add.schemas import (
    Envelope, ReconciliationAnchorRequest, ReconciliationChunkRequest,
    ReconciliationManifestRequest, ReconciliationSourceRecord, SourceTailChunkRequest,
)
from zk_add.settings import settings
from zk_add.source_exceptions import review_source_exception
from zk_add.time_utils import utc_now


@pytest.fixture
def source_store(store, monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "fleet_root_secret", "synthetic-raw-source-custody-key")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    with store() as db:
        connector = db.scalar(select(Connector))
        connector.zkt_custody_enabled = True
        connector.firmware_version = "zone-lite-2.7.0"
        db.commit()
    return store


def count(db, model):
    return db.scalar(select(func.count(model.id)))


def raw_row(ordinal, size=40):
    # Deliberately uninterpretable and identical at every ordinal. Transport
    # must preserve separate occurrences without pretending to decode them.
    raw = b"\xff" * size
    digest = hashlib.sha256(raw).hexdigest()
    return ReconciliationSourceRecord(ordinal=ordinal, raw_record_digest=digest,
        terminal_record_key=digest, occurrence_index=ordinal + 1, disposition="RAW_PRESERVED",
        raw_record_b64=base64.b64encode(raw).decode())


def prepare(db, path, size=40, length=2):
    connector = db.scalar(select(Connector))
    job = db.scalar(select(ReconciliationJob))
    epoch = reconcile.source_epoch_uuid(db, job)
    records = [raw_row(index, size) for index in range(length)]
    common = dict(source_epoch=epoch, start_ordinal=0, end_ordinal=length,
        chunk_digest="0" * 64, resulting_chain_digest="0" * 64, records=records)
    if path == "baseline":
        db.scalar(select(ReconciliationCoverage)).active = False
        reconcile.apply_reconciliation_anchor(db, connector=connector, payload=ReconciliationAnchorRequest(
            source_epoch=epoch, job_id=job.job_id, generation=1, terminal_serial="TEST-LOAD",
            terminal_generation=1, cutoff_count=length, latest_terminal_count=length,
            record_size=size, source_total_bytes=4 + size * length, first_anchor_digest=records[0].raw_record_digest))
        request = ReconciliationChunkRequest(**common, job_id=job.job_id, generation=1, sequence=0,
                                             previous_chain_digest=None)
    else:
        coverage = db.scalar(select(ReconciliationCoverage))
        request = SourceTailChunkRequest(**common, terminal_serial="TEST-LOAD", terminal_generation=1,
            record_size=size, latest_terminal_count=length,
            previous_chain_digest=coverage.source_committed_chain_digest)
    return connector, sealed(request)


def apply(db, connector, path, request):
    method = reconcile.apply_reconciliation_chunk if path == "baseline" else reconcile.apply_source_tail_chunk
    return method(db, connector=connector, payload=request)


def seal_baseline(db, connector, request):
    return reconcile.apply_reconciliation_manifest(db, connector=connector, payload=ReconciliationManifestRequest(
        source_epoch=request.source_epoch, job_id=request.job_id, generation=1,
        terminal_serial="TEST-LOAD", terminal_generation=1, cutoff_count=request.end_ordinal,
        latest_terminal_count=request.end_ordinal, final_chain_digest=request.resulting_chain_digest))


@pytest.mark.parametrize("path", ["baseline", "tail"])
@pytest.mark.parametrize("size", [8, 16, 40])
def test_atomic_custody_replays_without_attendance_or_polling_unchanged_holds(source_store, path, size):
    with source_store() as db:
        connector, request = prepare(db, path, size)
        result = apply(db, connector, path, request)
        db.commit()  # ACK is lost; replay from another transaction.
        assert not result[2]
        chunk_id = result[1].id
    with source_store() as db:
        connector = db.scalar(select(Connector))
        connector.zkt_custody_enabled = False  # Previously committed custody remains valid.
        result = apply(db, connector, path, request)
        assert result[2] and result[1].id == chunk_id
        assert count(db, TerminalRecordManifest) == count(db, ZktCustodyWork) == 2
        assert count(db, ZktOccurrenceAlias) == 2
        aliases = db.scalars(select(ZktOccurrenceAlias).order_by(ZktOccurrenceAlias.ordinal)).all()
        assert len({row.occurrence_id for row in aliases}) == 2
        assert [row.ordinal for row in aliases] == [0, 1]
        assert all(row.attendance_event_id is None for row in aliases)
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0
        manifests = db.scalars(select(TerminalRecordManifest).order_by(TerminalRecordManifest.ordinal)).all()
        assert [row.ordinal for row in manifests] == [0, 1]
        assert all(decrypt_text(row.protected_raw_record) == request.records[0].raw_record_b64 for row in manifests)
        assert all(row.raw_timestamp is None and row.observed_user_id is None and row.error_code is None for row in manifests)
        rows = db.scalars(select(ZktCustodyWork)).all()
        assert len({row.work_key for row in rows}) == 2
        assert all(row.state == "PENDING" and row.attempt_count == 0 and row.next_attempt_at is not None for row in rows)
        assert work.advance_work(db) == 0
        status = work.work_status(db, connector)
        assert not status["missing_processing_obligation"]
        assert status["oracle_completion"] == "NOT_ASSERTED"
        assert result[1].raw_preserved_count == 2
        if path == "tail":
            assert result[0].tail_exception_count == 0 and result[1].exception_count == 0
            assert result[0].raw_preserved_count == 2
        else:
            assert result[0].quarantined_count == 0 and result[1].quarantined_count == 0


@pytest.mark.parametrize("path", ["baseline", "tail"])
def test_missing_work_commit_rolls_back_raw_rows_receipt_and_cursor(source_store, path, monkeypatch):
    with source_store() as db:
        connector, request = prepare(db, path)
        db.commit()
        identifier = connector.id
    @contextmanager
    def scope():
        with source_store() as db:
            with db.begin():
                yield db
    original = work.attach_source_work
    def fail_after_insert(*args):
        original(*args)
        raise RuntimeError("Synthetic work commit failure")
    monkeypatch.setattr(web, "session_scope", scope)
    monkeypatch.setattr(work, "attach_source_work", fail_after_insert)
    envelope = Envelope(connector_id="synthetic-load", message_id=str(uuid4()), boot_id="test",
        seq=1, sent_at=utc_now(), type="reconcile_chunk" if path == "baseline" else "source_tail_chunk",
        payload=request.model_dump(mode="json"))
    with pytest.raises(RuntimeError, match="Synthetic work commit failure"):
        web.persist_envelope(identifier, envelope)
    with source_store() as db:
        assert count(db, TerminalRecordManifest) == count(db, ZktCustodyWork) == 0
        assert count(db, ZktOccurrenceAlias) == 0
        assert count(db, ReconciliationChunk) == count(db, SourceTailChunk) == 0
        assert db.scalar(select(ReconciliationJob)).committed_next_ordinal == 0
        assert db.scalar(select(ReconciliationCoverage)).source_committed_cursor == 0


@pytest.mark.parametrize("path", ["baseline", "tail"])
@pytest.mark.parametrize("failure", ["disabled", "wrong-family", "missing-key", "semantic-claim", "wrong-epoch"])
def test_new_raw_source_refuses_missing_authority_without_advancing(source_store, path, failure, monkeypatch):
    with source_store() as db:
        connector, request = prepare(db, path)
        if failure == "disabled":
            connector.zkt_custody_enabled = False
        elif failure == "wrong-family":
            connector.firmware_family = "hikvision"
        elif failure == "missing-key":
            monkeypatch.setattr(settings, "pii_fernet_key", None)
        elif failure == "wrong-epoch":
            request = request.model_copy(update={"source_epoch": str(uuid4())})
        else:
            request.records[0] = request.records[0].model_copy(update={"raw_timestamp": 1})
        with pytest.raises(ValueError):
            apply(db, connector, path, request)
        assert count(db, TerminalRecordManifest) == count(db, ZktCustodyWork) == 0
        assert db.scalar(select(ReconciliationJob)).committed_next_ordinal == 0
        assert db.scalar(select(ReconciliationCoverage)).source_committed_cursor == 0


@pytest.mark.parametrize("field,value", [("raw_timestamp", 1), ("observed_uid", "1"),
    ("observed_user_id", "1007"), ("error_code", "INVALID_TIME")])
def test_wire_schema_cannot_label_raw_bytes_with_interpretations(field, value):
    with pytest.raises(ValueError, match="Raw custody cannot claim"):
        ReconciliationSourceRecord.model_validate({**raw_row(0).model_dump(), field: value})


def test_source_capture_can_seal_but_review_notes_never_become_oracle_proof(source_store):
    with source_store() as db:
        connector, request = prepare(db, "baseline")
        apply(db, connector, "baseline", request)
        job = seal_baseline(db, connector, request)
        db.commit()
        assert job.capture_certified_at and job.capture_certificate["raw_preserved"] == 2
        assert job.capture_certificate["quarantined"] == 0
        assert job.oracle_certified_at is None and job.status == "NEEDS_ATTENTION"
        assert job.wait_reason == "SOURCE_INTERPRETATION_REQUIRED"
        manifest = db.scalar(select(TerminalRecordManifest))
        with pytest.raises(ValueError, match="qualified interpretation"):
            review_source_exception(db, row=manifest, actor="test", reason="Reviewed", idempotency_key="test")
        with pytest.raises(RecoveryError, match="not eligible for correction"):
            _correction_candidate(db, "ZKT_MANIFEST", f"ZKT_MANIFEST:{manifest.id}", utc_now())
        db.add(TerminalRecordReview(manifest_id=manifest.id, actor="test", reason="Legacy review note",
                                   idempotency_key="legacy", state="REVIEWED"))
        db.flush()
        assure = reconcile.source_exception_assurance(db, job)
        assert assure["raw_interpretation_open"] == assure["open"] == 2
        reconcile.refresh_reconciliation_assurance(db, job)
        assert job.oracle_certified_at is None
        assert reconcile.serialize_job(db, job)["progress"]["raw_preserved"] == 2
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_certifying_older_baseline_does_not_certify_new_raw_tail(reconciliation_db):
    db, connector = reconciliation_db
    coverage = _certify_one_record_baseline(db, connector, bind_epoch=True)
    connector.zkt_custody_enabled = True
    request = sealed(SourceTailChunkRequest(source_epoch=reconcile.source_epoch_uuid(db, coverage),
        terminal_serial=coverage.terminal_serial, terminal_generation=coverage.terminal_generation,
        record_size=40, start_ordinal=1, end_ordinal=2, latest_terminal_count=2,
        chunk_digest="0" * 64, resulting_chain_digest="0" * 64,
        previous_chain_digest=coverage.source_committed_chain_digest, records=[raw_row(1)]))
    reconcile.apply_source_tail_chunk(db, connector=connector, payload=request)
    event = db.scalar(select(AttendanceEvent))
    event.ords_status = "ACKED"
    job = db.get(ReconciliationJob, coverage.job_id)
    reconcile.refresh_reconciliation_assurance(db, job)
    assert job.status == "COMPLETED" and job.oracle_certificate["certified_source_cursor"] == 1
    assert coverage.oracle_state == "ORACLE_SOURCE_INTERPRETATION_PENDING"
    assert coverage.oracle_certified_at is None and coverage.source_committed_cursor == 2


@pytest.mark.parametrize("mutation,expected", [("ordinal", "HELD_EXCEPTION"),
    ("binding", "HELD_EXCEPTION"), ("bad-base64", "HELD_EXCEPTION"),
    ("ciphertext", "RETRY_SYSTEM"), ("none", "WAIT_PROFILE")])
def test_raw_work_checks_immutable_binding_without_reclassifying_crypto_errors(source_store, mutation, expected):
    with source_store() as db:
        connector, request = prepare(db, "tail", length=1)
        apply(db, connector, "tail", request)
        manifest = db.scalar(select(TerminalRecordManifest))
        row = db.scalar(select(ZktCustodyWork))
        if mutation == "ordinal":
            manifest.ordinal += 1
        elif mutation == "binding":
            connector.zkt_device.confirmed_serial = "OTHER-TERMINAL"
        elif mutation == "bad-base64":
            manifest.protected_raw_record = encrypt_text("not base64")
        elif mutation == "ciphertext":
            manifest.protected_raw_record = "corrupt ciphertext"
        work.inspect_work(db, row)
        assert row.state == expected and row.reason_code and row.owner
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_missing_source_obligation_is_visible(source_store):
    with source_store() as db:
        connector, request = prepare(db, "tail", length=1)
        apply(db, connector, "tail", request)
        db.delete(db.scalar(select(ZktCustodyWork)))
        db.flush()
        assert work.work_status(db, connector)["missing_processing_obligation"]


@pytest.mark.parametrize("state", ["NEEDS_ATTENTION", "PAUSED", "CANCELLED", "COMPLETED"])
def test_older_committed_receipt_survives_later_cursor_and_processing_state(source_store, monkeypatch, state):
    with source_store() as db:
        connector, whole = prepare(db, "baseline")
        first = sealed(whole.model_copy(update={"end_ordinal": 1, "records": whole.records[:1]}))
        apply(db, connector, "baseline", first)
        second = sealed(whole.model_copy(update={"sequence": 1, "start_ordinal": 1,
            "records": whole.records[1:], "previous_chain_digest": first.resulting_chain_digest}))
        apply(db, connector, "baseline", second)
        job = db.scalar(select(ReconciliationJob))
        job.status = state
        job.error_code = "SOURCE_INTERPRETATION_REQUIRED" if state == "NEEDS_ATTENTION" else None
        identifier = connector.id
        db.commit()
    @contextmanager
    def scope():
        with source_store() as db:
            with db.begin():
                yield db
    monkeypatch.setattr(web, "session_scope", scope)
    envelope = Envelope(connector_id="synthetic-load", message_id=str(uuid4()), boot_id="test",
        seq=1, sent_at=utc_now(), type="reconcile_chunk", payload=first.model_dump(mode="json"))
    result = web.persist_envelope(identifier, envelope)
    assert result.ack["type"] == "reconcile_chunk_ack" and result.ack["code"] is None
    assert result.ack["duplicate"] and result.ack["committed_next_ordinal"] == 1
    assert result.ack["resulting_chain_digest"] == first.resulting_chain_digest
    assert not result.ack["continue_allowed"]
    with source_store() as db:
        job = db.scalar(select(ReconciliationJob))
        assert job.status == state and job.committed_next_ordinal == 2
        assert count(db, ReconciliationChunk) == count(db, ZktCustodyWork) == 2
        if state in {"PAUSED", "CANCELLED", "COMPLETED"}:
            unknown = sealed(whole.model_copy(update={"sequence": 2, "start_ordinal": 2, "end_ordinal": 3,
                "records": [raw_row(2)], "previous_chain_digest": second.resulting_chain_digest}))
            with pytest.raises(ValueError, match="not accepting source data"):
                apply(db, db.get(Connector, identifier), "baseline", unknown)


@pytest.mark.parametrize("path", ["baseline", "tail"])
def test_replayed_raw_receipt_rechecks_canonical_content(source_store, path):
    with source_store() as db:
        connector, request = prepare(db, path, length=1)
        apply(db, connector, path, request)
        db.commit()
        changed = request.model_copy(update={"records": [request.records[0].model_copy(update={
            "terminal_record_key": "0" * 64})]})
        # Claiming the old digest cannot authenticate changed canonical content.
        result = apply(db, connector, path, changed)
        assert not result[2] and count(db, TerminalRecordManifest) == 1
        if path == "tail":
            assert result[3] == "SOURCE_TAIL_REPLAY_DIVERGED" and not result[0].active
        else:
            assert result[0].error_code == "COMMITTED_RANGE_DIVERGED"


@pytest.mark.parametrize("fail_second_page", [False, True])
def test_recovery_epoch_retains_bounded_raw_obligations_atomically(source_store, monkeypatch, fail_second_page):
    with source_store() as db:
        connector, first = prepare(db, "baseline", length=100)
        job = db.scalar(select(ReconciliationJob))
        job.cutoff_count = job.latest_terminal_count = 125
        job.source_total_bytes = 4 + 125 * 40
        apply(db, connector, "baseline", first)
        second = sealed(first.model_copy(update={"sequence": 1, "start_ordinal": 100, "end_ordinal": 125,
            "records": [raw_row(i) for i in range(100, 125)], "previous_chain_digest": first.resulting_chain_digest}))
        apply(db, connector, "baseline", second)
        old_epoch = job.source_epoch_id
        divergence = ReconciliationDivergence(job_id=job.id, source_epoch_id=old_epoch, ordinal=125,
            old_raw_digest="a" * 64, new_raw_digest="b" * 64)
        db.add(divergence)
        db.commit()
        batches = []
        original = work.attach_source_work
        def attach(session, connector, manifests):
            batches.append(len(manifests))
            original(session, connector, manifests)
            if fail_second_page and len(batches) == 2:
                raise RuntimeError("Synthetic recovery work failure")
        monkeypatch.setattr(work, "attach_source_work", attach)
        if fail_second_page:
            with pytest.raises(RuntimeError, match="Synthetic recovery work failure"):
                reconcile._activate_recovery_epoch(db, job=job, divergence=divergence, now=utc_now())
            db.rollback()
        else:
            reconcile._activate_recovery_epoch(db, job=job, divergence=divergence, now=utc_now())
            db.commit()
        assert batches == [100, 25]
    with source_store() as db:
        job = db.scalar(select(ReconciliationJob))
        assert count(db, TerminalRecordManifest) == count(db, ZktCustodyWork) == (125 if fail_second_page else 250)
        assert count(db, TerminalSourceEpoch) == (1 if fail_second_page else 2)
        assert (job.source_epoch_id == old_epoch) == fail_second_page
        connector = db.scalar(select(Connector))
        assert not work.work_status(db, connector)["missing_processing_obligation"]
        assert count(db, AttendanceEvent) == count(db, OrdsOutbox) == 0


def test_concurrent_tail_replay_commits_one_range_and_each_occurrence_once(source_store):
    if source_store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock qualification")
    with source_store() as db:
        _connector, request = prepare(db, "tail")
        db.commit()
    def submit(_index):
        with source_store() as db:
            connector = db.scalar(select(Connector).with_for_update())
            result = apply(db, connector, "tail", request)
            db.commit()
            return result[2]
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, range(2))) == [False, True]
    with source_store() as db:
        assert count(db, SourceTailChunk) == 1
        assert count(db, ZktCustodyWork) == count(db, TerminalRecordManifest) == 2
        assert count(db, ZktOccurrenceAlias) == 2
        assert db.scalar(select(ReconciliationCoverage)).raw_preserved_count == 2


def test_migration_upgrades_existing_install_and_retains_custody_on_downgrade(source_store, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261004_0047_zkt_source_work.py"
    spec = importlib.util.spec_from_file_location("raw_source_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = source_store.kw["bind"]
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("add_zkt_custody_work") as batch:
            batch.drop_constraint("fk_add_zkt_work_source_manifest", type_="foreignkey")
            batch.drop_constraint("uq_add_zkt_work_source_manifest", type_="unique")
            batch.drop_column("source_manifest_id")
        for table in ("add_reconciliation_jobs", "add_reconciliation_chunks",
                      "add_source_tail_chunks", "add_reconciliation_coverage"):
            operations.drop_column(table, "raw_preserved_count")
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        migration.upgrade()
        tables = {"add_reconciliation_jobs", "add_reconciliation_chunks", "add_source_tail_chunks",
                  "add_reconciliation_coverage", "add_zkt_custody_work"}
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare_to: kind != "table" or name in tables,
        })
        assert compare_metadata(context, Base.metadata) == []
    with source_store() as db:
        connector, request = prepare(db, "tail", length=1)
        apply(db, connector, "tail", request)
        db.commit()
        protected = db.scalar(select(TerminalRecordManifest)).protected_raw_record
        receipt = db.scalar(select(SourceTailChunk)).id
        obligation = db.scalar(select(ZktCustodyWork)).work_key
        alias = db.scalar(select(ZktOccurrenceAlias))
        original_occurrence = (alias.id, alias.occurrence_id, alias.manifest_id, alias.raw_digest)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with source_store() as db:
        assert db.scalar(select(TerminalRecordManifest)).protected_raw_record == protected
        assert db.scalar(select(SourceTailChunk)).id == receipt
        assert db.scalar(select(ZktCustodyWork)).work_key == obligation
        alias = db.scalar(select(ZktOccurrenceAlias))
        assert (alias.id, alias.occurrence_id, alias.manifest_id, alias.raw_digest) == original_occurrence
        assert db.scalar(select(ReconciliationCoverage)).raw_preserved_count == 1
