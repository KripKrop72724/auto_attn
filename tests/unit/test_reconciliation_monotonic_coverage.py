"""An older baseline can finish without replacing a longer live source chain."""
from copy import deepcopy
import base64
import hashlib

import pytest
from sqlalchemy import func, select

from test_reconciliation import reconciliation_db, SERIAL  # noqa: F401
from zk_add.models import (ReconciliationChunk, ReconciliationCoverage, ReconciliationEvent,
                           ReconciliationJob, SourceTailChunk, TerminalRecordManifest, TerminalSourceEpoch)
from zk_add import reconciliation as service
from zk_add.schemas import (ReconciliationAnchorRequest, ReconciliationChunkRequest,
                           ReconciliationManifestRequest, ReconciliationSourceRecord, SourceTailChunkRequest)


def source(ordinal, *, disposition="TERMINAL_DUPLICATE"):
    raw = ordinal.to_bytes(4, "little") + b"\0" * 36
    return ReconciliationSourceRecord(ordinal=ordinal, raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(b"key" + raw).hexdigest(), occurrence_index=1,
        disposition=disposition, raw_record_b64=base64.b64encode(raw).decode())


def create(session, connector, cutoff, key):
    job = service.create_reconciliation_job(session, connector=connector, actor="test", reason="Verify preserved source custody.",
        confirmation="RECONCILE 1 FROM START", idempotency_key=key)
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_generation=job.terminal_generation,
        terminal_serial=SERIAL, cutoff_count=cutoff, latest_terminal_count=connector.zkt_device.attendance_count,
        record_size=40, source_total_bytes=4 + cutoff * 40, first_anchor_digest=source(0).raw_record_digest))
    return job


def append(session, connector, job, end, *, width=100, exceptions=()):
    sequence = session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id))
    while job.committed_next_ordinal < end:
        start, stop = job.committed_next_ordinal, min(job.committed_next_ordinal + width, end)
        draft = ReconciliationChunkRequest(job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
            generation=job.terminal_generation, sequence=sequence, start_ordinal=start, end_ordinal=stop,
            previous_chain_digest=job.last_chain_digest, chunk_digest="0" * 64, resulting_chain_digest="0" * 64,
            records=[source(i, disposition="INVALID_TIME" if i in exceptions else "TERMINAL_DUPLICATE") for i in range(start, stop)])
        digest = service.reconciliation_chunk_digest(draft)
        chain = service.reconciliation_chain_digest(job.last_chain_digest, start_ordinal=start, end_ordinal=stop, chunk_digest=digest)
        _, chunk, _ = service.apply_reconciliation_chunk(session, connector=connector,
            payload=draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain}))
        assert chunk is not None
        sequence += 1


def tail(session, connector, coverage, end, *, exceptions=()):
    while coverage.source_committed_cursor < end:
        start, stop = coverage.source_committed_cursor, min(coverage.source_committed_cursor + 100, end)
        draft = SourceTailChunkRequest(source_epoch=service.source_epoch_uuid(session, coverage), terminal_serial=SERIAL,
            terminal_generation=coverage.terminal_generation, record_size=40, start_ordinal=start, end_ordinal=stop,
            latest_terminal_count=end, previous_chain_digest=coverage.source_committed_chain_digest,
            chunk_digest="0" * 64, resulting_chain_digest="0" * 64,
            records=[source(i, disposition="INVALID_TIME" if i in exceptions else "TERMINAL_DUPLICATE") for i in range(start, stop)])
        digest = service.reconciliation_chunk_digest(draft)
        chain = service.reconciliation_chain_digest(coverage.source_committed_chain_digest,
            start_ordinal=start, end_ordinal=stop, chunk_digest=digest)
        _, chunk, _, error = service.apply_source_tail_chunk(session, connector=connector,
            payload=draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain}))
        assert error is None and chunk is not None
    connector.zkt_device.attendance_count = end


def manifest(connector, job, epoch):
    return ReconciliationManifestRequest(job_id=job.job_id, source_epoch=epoch,
        generation=job.terminal_generation, terminal_generation=job.terminal_generation, terminal_serial=SERIAL,
        cutoff_count=job.cutoff_count, latest_terminal_count=connector.zkt_device.attendance_count,
        final_chain_digest=job.last_chain_digest or "0" * 64)


def scenario(db, *, baseline=5, checkpoint=4, cutoff=7, current=11, exceptions=()):
    session, connector = db
    connector.firmware_version = "zone-lite-2.6.20"
    connector.zkt_device.attendance_count = current
    prior = create(session, connector, baseline, "retained-baseline")
    append(session, connector, prior, baseline)
    service.apply_reconciliation_manifest(session, connector=connector,
        payload=manifest(connector, prior, service.source_epoch_uuid(session, prior)))
    coverage = session.scalar(select(ReconciliationCoverage).where(ReconciliationCoverage.active == True))  # noqa: E712
    tail(session, connector, coverage, current, exceptions=exceptions)
    job = create(session, connector, cutoff, "retry-old-checkpoint")
    append(session, connector, job, checkpoint, width=97, exceptions=exceptions)
    job.status, job.phase = "NEEDS_ATTENTION", "FINAL_ASSURANCE"
    job.wait_reason = job.error_code = "SOURCE_EPOCH_RECOVERY_LIMIT"
    service.control_reconciliation_job(session, job=job, action="retry", actor="test",
        reason="Single audited retry with existing source evidence.", idempotency_key="one-retry-only")
    append(session, connector, job, cutoff, width=97, exceptions=exceptions)
    session.commit()
    return session, connector, job, coverage, manifest(connector, job, service.source_epoch_uuid(session, job))


def finish(data):
    session, connector, job, coverage, payload = data
    service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
    session.commit()
    return job


def snapshot(coverage):
    return {key: deepcopy(getattr(coverage, key)) for key in ("id", "active", "source_committed_cursor",
        "source_committed_chain_digest", "tail_exception_count", "capture_evidence", "oracle_evidence", "oracle_state")}


def test_production_sized_old_checkpoint_retains_longer_chain_and_exact_lost_ack_replays(reconciliation_db):  # noqa: F811
    data = scenario(reconciliation_db, baseline=3086, checkpoint=3000, cutoff=3172, current=4512)
    session, connector, job, coverage, payload = data
    before = snapshot(coverage)
    original_count = session.scalar(select(func.count(TerminalRecordManifest.id)))
    finish(data)
    assert job.capture_certified_at and job.status == "COMPLETED"
    assert snapshot(coverage) == before
    assert session.scalar(select(func.count(TerminalRecordManifest.id))) == original_count == 4512
    own = session.scalar(select(ReconciliationCoverage).where(ReconciliationCoverage.job_id == job.id))
    assert own.active is False and own.source_committed_cursor == 3172
    assert own.capture_evidence["retained_coverage"]["coverage_id"] == coverage.coverage_id
    assert own.oracle_state == "ORACLE_MEMBERSHIP_CERTIFIED"
    saved = deepcopy(job.capture_certificate)
    events = session.scalar(select(func.count(ReconciliationEvent.id)).where(ReconciliationEvent.job_id == job.id))
    session.expire_all()  # The next request cannot rely on the previous transaction's identity map.
    tail(session, connector, coverage, 4513)
    session.commit()
    service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
    session.commit()
    assert job.capture_certificate == saved and coverage.source_committed_cursor == 4513
    assert service._assurance_coverage(session, job).id == own.id
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 2
    assert session.scalar(select(func.count(ReconciliationEvent.id)).where(ReconciliationEvent.job_id == job.id)) == events


@pytest.mark.parametrize("fault", ["short_chain_gap", "short_chain_corrupt", "tail_gap", "tail_chain_corrupt", "hmac",
    "epoch", "superseded_epoch", "size", "negative_ordinal", "missing_raw", "malformed_digest", "latest_count", "limit"])
def test_missing_or_conflicting_proof_holds_without_replacing_current_coverage(reconciliation_db, monkeypatch, fault):  # noqa: F811
    data = scenario(reconciliation_db)
    session, connector, job, coverage, payload = data
    if fault == "short_chain_gap":
        session.delete(session.scalar(select(ReconciliationChunk).where(ReconciliationChunk.job_id == job.id)))
    elif fault == "short_chain_corrupt":
        session.scalar(select(ReconciliationChunk).where(ReconciliationChunk.job_id == job.id)).chunk_digest = "a" * 64
    elif fault == "tail_gap":
        session.delete(session.scalar(select(SourceTailChunk)))
    elif fault == "tail_chain_corrupt":
        coverage.source_committed_chain_digest = "a" * 64
    elif fault == "hmac":
        coverage.capture_evidence = {**coverage.capture_evidence, "evidence_signature": "a" * 64}
    elif fault == "epoch":
        coverage.source_epoch_id = None
    elif fault == "superseded_epoch":
        session.get(TerminalSourceEpoch, job.source_epoch_id).state = "SUPERSEDED"
    elif fault == "size":
        session.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 8)).record_size = 16
    elif fault == "negative_ordinal":
        session.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 8)).ordinal = -1
    elif fault == "missing_raw":
        session.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 8)).protected_raw_record = None
    elif fault == "malformed_digest":
        session.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 8)).raw_record_digest = "z" * 64
    elif fault == "latest_count":
        payload = payload.model_copy(update={"latest_terminal_count": 6})
    elif fault == "limit":
        monkeypatch.setattr(service, "COVERAGE_PROOF_MAX_RECORDS", 10)
    session.flush()
    before = snapshot(coverage)
    if fault == "superseded_epoch":
        with pytest.raises(ValueError, match="epoch"):
            service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
    else:
        service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
        assert job.status == "NEEDS_ATTENTION" and job.capture_certified_at is None
    assert snapshot(coverage) == before
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 1


def test_short_exception_assurance_uses_own_inactive_certificate_and_keeps_long_tail_pending(reconciliation_db):  # noqa: F811
    data = scenario(reconciliation_db, exceptions={6, 9})
    session, connector, job, coverage, _ = data
    before = snapshot(coverage)
    finish(data)
    assert job.capture_certified_at and job.status == "NEEDS_ATTENTION"
    assurance = service.source_exception_assurance(session, job)
    assert assurance["state"] == "REVIEW_REQUIRED" and assurance["total"] == 1
    assert assurance["mismatch_reasons"] == []
    from zk_add.source_exceptions import review_source_exception
    row = session.scalar(select(TerminalRecordManifest).where(TerminalRecordManifest.ordinal == 6))
    review_source_exception(session, row=row, actor="test", reason="Verified invalid source timestamp.", idempotency_key="review-six")
    service.refresh_reconciliation_assurance(session, job)
    session.commit()
    assert job.status == "COMPLETED"
    assert snapshot(coverage) == before and coverage.tail_exception_count == 2


@pytest.mark.parametrize("fault", ["signature", "retained_inactive", "wrong_binding", "regressed_tail", "changed_tail_chain"])
def test_short_certificate_cannot_borrow_unrelated_or_changed_active_coverage(reconciliation_db, fault):  # noqa: F811
    data = scenario(reconciliation_db)
    session, connector, job, coverage, _ = data
    finish(data)
    if fault == "signature":
        job.capture_certificate = {**job.capture_certificate, "evidence_signature": "a" * 64}
    elif fault == "retained_inactive":
        coverage.active = False
    elif fault == "wrong_binding":
        body = {k: v for k, v in job.capture_certificate.items() if not k.startswith("evidence_")}
        body["retained_coverage"] = {**body["retained_coverage"], "coverage_id": "unrelated"}
        job.capture_certificate = service._sealed_evidence(body)
    elif fault == "regressed_tail":
        coverage.source_committed_cursor -= 1
    else:
        coverage.source_committed_chain_digest = "b" * 64
    session.flush()
    assert service._assurance_coverage(session, job) is None
    assert service.source_exception_assurance(session, job)["state"] == "SCOPE_MISMATCH"


def test_different_replayed_manifest_never_overwrites_committed_certificates(reconciliation_db):  # noqa: F811
    data = scenario(reconciliation_db)
    session, connector, job, coverage, payload = data
    finish(data)
    before, saved = snapshot(coverage), deepcopy(job.capture_certificate)
    with pytest.raises(ValueError, match="Replayed"):
        service.apply_reconciliation_manifest(session, connector=connector,
            payload=payload.model_copy(update={"final_chain_digest": "b" * 64}))
    assert snapshot(coverage) == before and job.capture_certificate == saved


def test_zero_record_manifest_has_an_idempotent_committed_receipt(reconciliation_db):  # noqa: F811
    session, connector = reconciliation_db
    connector.zkt_device.attendance_count = 0
    job = create(session, connector, 0, "empty-source-manifest")
    payload = manifest(connector, job, service.source_epoch_uuid(session, job)).model_copy(
        update={"final_chain_digest": "0" * 64})
    service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
    session.commit()
    saved = deepcopy(job.capture_certificate)
    service.apply_reconciliation_manifest(session, connector=connector, payload=payload)
    session.commit()
    assert job.capture_certificate == saved
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 1


def test_actual_websocket_duplicate_manifest_returns_preserved_receipt_and_current_authority(reconciliation_db, monkeypatch):  # noqa: F811
    import json
    from uuid import uuid4
    from sqlalchemy.orm import sessionmaker
    from zk_add import web
    from zk_add.models import Connector
    from zk_add.schemas import Envelope, ReconciliationAssignmentReleaseRequest
    from zk_add.time_utils import utc_now
    data = scenario(reconciliation_db)
    session, connector, job, coverage, payload = data
    connector.boot_id, connector.last_sequence = "manifest-test-boot", 10
    session.commit()
    factory = sessionmaker(session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("zk_add.db.SessionLocal", factory)
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()), boot_id=connector.boot_id,
        seq=11, sent_at=utc_now(), type="reconcile_source_manifest", payload=payload.model_dump(mode="json"))
    first = web.persist_envelope(connector.id, envelope)
    assert first.ack["type"] == "error" and first.ack["code"] == "SOURCE_COVERAGE_RETAINED"
    assert first.ack["capture_certificate"]["certified_source_cursor"] == 7
    assert first.coverage["source_committed_cursor"] == 11
    json.dumps(first.coverage)
    session.expire_all()
    assert connector.last_sequence == 11
    tail(session, connector, coverage, 12)
    session.commit()
    for _ in range(2):
        replay = web.persist_envelope(connector.id, envelope)
        assert replay.ack == first.ack
        assert replay.coverage["source_committed_cursor"] == 12
    with factory() as db:
        release = ReconciliationAssignmentReleaseRequest(job_id=job.job_id, assignment_id=str(uuid4()),
            source_epoch=payload.source_epoch, generation=payload.generation,
            committed_next_ordinal=7, reason="TRANSIENT_STEP_FAILED")
        current_job = db.get(ReconciliationJob, job.id)
        saved = (current_job.status, current_job.phase, current_job.auto_retry_count, current_job.capture_certificate)
        for _ in range(2):
            service.apply_reconciliation_assignment_release(db, connector=db.get(Connector, connector.id), payload=release)
        db.commit()
        assert (current_job.status, current_job.phase, current_job.auto_retry_count, current_job.capture_certificate) == saved
        assert current_job.active_assignment_id is None
        for update in ({"generation": release.generation + 1}, {"committed_next_ordinal": 6},
                       {"committed_next_ordinal": 8}, {"source_epoch": str(uuid4())}):
            with pytest.raises(ValueError):
                service.apply_reconciliation_assignment_release(db, connector=db.get(Connector, connector.id),
                    payload=release.model_copy(update=update))
    with factory() as db:
        response, _ = web.persist_device_reconciliation_manifest(payload, (db, db.get(Connector, connector.id)))
        assert response["ok"] is False and response["code"] == "SOURCE_COVERAGE_RETAINED"
        assert response["capture_certificate"] == first.ack["capture_certificate"]
        assert response["source_coverage"]["source_committed_cursor"] == 12
    stale = envelope.model_copy(update={"payload": {**envelope.payload, "source_epoch": str(uuid4())}})
    with pytest.raises(ValueError, match="epoch"):
        web.persist_envelope(connector.id, stale)


def test_failed_database_commit_cannot_return_a_manifest_receipt(reconciliation_db, monkeypatch):  # noqa: F811
    from uuid import uuid4
    from sqlalchemy.orm import Session, sessionmaker
    from zk_add import web
    from zk_add.schemas import Envelope
    from zk_add.time_utils import utc_now
    session, connector, job, coverage, payload = scenario(reconciliation_db)
    before = snapshot(coverage)
    class CommitFailure(Session):
        def commit(self):
            raise RuntimeError("synthetic commit failure")
    monkeypatch.setattr("zk_add.db.SessionLocal", sessionmaker(session.get_bind(), class_=CommitFailure, autoflush=False))
    request = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()), boot_id="manifest-test-boot",
        seq=10, sent_at=utc_now(), type="reconcile_source_manifest", payload=payload.model_dump(mode="json"))
    with pytest.raises(RuntimeError, match="commit failure"):
        web.persist_envelope(connector.id, request)
    session.expire_all()
    assert job.capture_certified_at is None and snapshot(coverage) == before
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 1


@pytest.fixture
def pg_scenario(reconciliation_db):  # noqa: F811
    import os
    from uuid import uuid4
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from zk_add.models import Base
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    data = scenario(reconciliation_db)
    schema = "reconciliation_monotonic_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            deferred_snapshots = []
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in data[0].execute(select(table)).mappings()]
                if rows:
                    if table.name == "add_zkt_devices":
                        for row in rows:
                            deferred_snapshots.append((row["id"], row["identity_snapshot_id"]))
                            row["identity_snapshot_id"] = None
                    db.execute(table.insert(), rows)
                    if "id" in table.c:
                        db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :maximum)"),
                            {"table": table.name, "maximum": max(row["id"] for row in rows)})
            table = Base.metadata.tables["add_zkt_devices"]
            for device_id, snapshot_id in deferred_snapshots:
                db.execute(table.update().where(table.c.id == device_id).values(identity_snapshot_id=snapshot_id))
        yield sessionmaker(engine, autoflush=False, expire_on_commit=False), tuple(row.id for row in data[1:4]), data[4]
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("first_action", ["manifest", "tail"])
def test_postgres_tail_and_older_manifest_commit_in_either_order_without_regression(pg_scenario, first_action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zk_add.models import Connector
    factory, (connector_id, job_id, coverage_id), payload = pg_scenario
    waiting = Event()
    def act(db, action):
        connector = db.get(Connector, connector_id)
        if action == "manifest":
            service.apply_reconciliation_manifest(db, connector=connector, payload=payload)
        else:
            tail(db, connector, db.get(ReconciliationCoverage, coverage_id), 12)
        db.flush()
    def second():
        with factory() as db:
            waiting.set()
            act(db, "tail" if first_action == "manifest" else "manifest")
            db.commit()
    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        act(first, first_action)
        pending = pool.submit(second)
        assert waiting.wait(2)
        first.commit()
        pending.result(timeout=10)
    with factory() as db:
        coverage, job = db.get(ReconciliationCoverage, coverage_id), db.get(ReconciliationJob, job_id)
        assert coverage.active and coverage.source_committed_cursor == 12
        assert job.capture_certified_at and job.status == "COMPLETED"
        assert db.scalar(select(func.count(ReconciliationCoverage.id))) == 2
        assert service._assurance_coverage(db, job).active is False
        service.apply_reconciliation_manifest(db, connector=db.get(Connector, connector_id), payload=payload)
        db.commit()
        assert coverage.source_committed_cursor == 12


def test_legacy_seal_and_nullable_interpretation_fields_still_use_committed_receipts(reconciliation_db):  # noqa: F811
    data = scenario(reconciliation_db)
    session, connector, job, coverage, _ = data
    prior = session.get(ReconciliationJob, coverage.job_id)
    body = {k: v for k, v in coverage.capture_evidence.items()
            if k not in {"evidence_signature", "evidence_algorithm", "raw_preserved"}}
    legacy = service._sealed_evidence(body)
    coverage.capture_evidence, prior.capture_certificate = legacy, legacy
    for row in session.scalars(select(TerminalRecordManifest)):
        row.declared_disposition = row.protected_source_claim = None
    session.commit()
    finish(data)
    assert job.capture_certified_at and job.status == "COMPLETED"
    assert coverage.active and coverage.source_committed_cursor == 11
    assert coverage.capture_evidence == legacy


def test_postgres_source_epoch_change_committed_before_manifest_refuses_old_epoch(pg_scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zk_add.crypto import encrypt_text
    from zk_add.models import Connector, ReconciliationDivergence
    from zk_add.time_utils import utc_now
    factory, (connector_id, job_id, coverage_id), payload = pg_scenario
    waiting = Event()
    def manifest_after_change():
        with factory() as db:
            connector = db.get(Connector, connector_id)
            waiting.set()
            with pytest.raises(ValueError, match="epoch"):
                service.apply_reconciliation_manifest(db, connector=connector, payload=payload)
            db.rollback()
    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        first.scalar(select(Connector.id).where(Connector.id == connector_id).with_for_update())
        job = first.scalar(select(ReconciliationJob).where(ReconciliationJob.id == job_id).with_for_update())
        divergence = ReconciliationDivergence(job_id=job.id, source_epoch_id=job.source_epoch_id,
            ordinal=8, state="PROBING", old_raw_digest=source(8).raw_record_digest,
            new_raw_digest="d" * 64, old_disposition="TERMINAL_DUPLICATE", new_disposition="TERMINAL_DUPLICATE",
            protected_new_raw_record=encrypt_text(source(8).raw_record_b64), observations=[])
        first.add(divergence)
        first.flush()
        service._activate_recovery_epoch(first, job=job, divergence=divergence, now=utc_now())
        pending = pool.submit(manifest_after_change)
        assert waiting.wait(2)
        first.commit()
        pending.result(timeout=10)
    with factory() as db:
        assert db.get(ReconciliationJob, job_id).capture_certified_at is None
        assert db.get(ReconciliationCoverage, coverage_id).active is False
        assert db.scalar(select(func.count(ReconciliationCoverage.id))) == 1
        # Recovery retained both original custody and its copied prefix.
        assert db.scalar(select(func.count(TerminalRecordManifest.id))) == 18


def test_manifest_matching_current_authority_keeps_ordinary_positive_ack(reconciliation_db, monkeypatch):  # noqa: F811
    from uuid import uuid4
    from sqlalchemy.orm import sessionmaker
    from zk_add import web
    from zk_add.schemas import Envelope
    from zk_add.time_utils import utc_now
    session, connector = reconciliation_db
    connector.zkt_device.attendance_count = 5
    job = create(session, connector, 5, "current-authority-manifest")
    append(session, connector, job, 5)
    payload = manifest(connector, job, service.source_epoch_uuid(session, job))
    session.commit()
    monkeypatch.setattr("zk_add.db.SessionLocal", sessionmaker(session.get_bind(), autoflush=False, expire_on_commit=False))
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()), boot_id="manifest-test-boot",
        seq=10, sent_at=utc_now(), type="reconcile_source_manifest", payload=payload.model_dump(mode="json"))
    first = web.persist_envelope(connector.id, envelope)
    assert first.ack["type"] == "reconcile_manifest_ack" and first.coverage is None
    assert web.persist_envelope(connector.id, envelope).ack == first.ack


def retry_expectation(session, connector, job):
    from zk_add.schemas import ReconciliationRetryExpectedState
    from zk_add.time_utils import ensure_utc, utc_now
    job.status, job.phase = "NEEDS_ATTENTION", "FINAL_ASSURANCE"
    job.wait_reason = job.error_code = "SOURCE_EPOCH_RECOVERY_LIMIT"
    job.updated_at = utc_now()
    connector.boot_id, connector.ota_image_sha256 = "guarded-retry-boot", "a" * 64
    connector.firmware_diagnostics = {"sample_sequence": 7}
    connector.firmware_diagnostics_at = utc_now()
    session.commit()
    return ReconciliationRetryExpectedState(status=job.status, phase=job.phase,
        error_code=job.error_code, wait_reason=job.wait_reason,
        source_epoch=service.source_epoch_uuid(session, job), terminal_serial=job.terminal_serial,
        terminal_generation=job.terminal_generation, committed_next_ordinal=job.committed_next_ordinal,
        cutoff_count=job.cutoff_count, chain_digest=job.last_chain_digest, retry_count=job.retry_count,
        updated_at=ensure_utc(job.updated_at), connector_boot_id=connector.boot_id,
        firmware_version=connector.firmware_version, application_sha256=connector.ota_image_sha256,
        diagnostics_sample_sequence=7, diagnostics_at=ensure_utc(connector.firmware_diagnostics_at))


def guarded_retry(session, job, expected, *, action="retry"):
    return service.control_reconciliation_job(session, job=job, action=action, actor="test",
        reason="One reviewed retry from the exact held checkpoint.", idempotency_key="guarded-retry-once",
        expected_state=expected)


def test_guarded_retry_and_lost_response_replay_commit_one_audited_action(reconciliation_db):  # noqa: F811
    session, connector, job, coverage, _ = scenario(reconciliation_db)
    expected = retry_expectation(session, connector, job)
    before = job.retry_count
    guarded_retry(session, job, expected)
    session.commit()
    assert job.status == "QUEUED" and job.retry_count == before + 1
    guarded_retry(session, job, expected)
    session.commit()
    assert job.status == "QUEUED" and job.retry_count == before + 1
    events = list(session.scalars(select(ReconciliationEvent).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.idempotency_key == "guarded-retry-once")))
    assert len(events) == 1 and len(events[0].details["expected_state_sha256"]) == 64
    with pytest.raises(ValueError, match="idempotency"):
        guarded_retry(session, job, expected.model_copy(update={"phase": "DIFFERENT_PHASE"}))
    assert coverage.source_committed_cursor == 11


@pytest.mark.parametrize("field", ["status", "phase", "error_code", "wait_reason", "source_epoch", "terminal_serial",
    "terminal_generation", "committed_next_ordinal", "cutoff_count", "chain_digest", "retry_count", "updated_at",
    "connector_boot_id", "firmware_version", "application_sha256", "diagnostics_sample_sequence", "diagnostics_at"])
def test_guarded_retry_refuses_any_expected_state_drift_before_mutation(reconciliation_db, field):  # noqa: F811
    from datetime import timedelta
    session, connector, job, coverage, _ = scenario(reconciliation_db)
    expected = retry_expectation(session, connector, job)
    value = getattr(expected, field)
    if isinstance(value, int):
        value += 1
    elif field in {"updated_at", "diagnostics_at"}:
        value += timedelta(microseconds=1)
    elif field in {"application_sha256", "chain_digest"}:
        value = "b" * 64
    else:
        value = "changed"
    before = (job.status, job.phase, job.error_code, job.retry_count, job.active_assignment_id, job.updated_at)
    with pytest.raises(ValueError, match="evidence changed"):
        guarded_retry(session, job, expected.model_copy(update={field: value}))
    assert (job.status, job.phase, job.error_code, job.retry_count, job.active_assignment_id, job.updated_at) == before
    assert session.scalar(select(func.count(ReconciliationEvent.id)).where(
        ReconciliationEvent.idempotency_key == "guarded-retry-once")) == 0


def test_retry_precondition_is_optional_but_cannot_authorize_other_control_actions(reconciliation_db):  # noqa: F811
    from zk_add.schemas import ReconciliationControlRequest
    session, connector, job, _, _ = scenario(reconciliation_db)
    expected = retry_expectation(session, connector, job)
    for action in ("resume", "pause", "cancel"):
        with pytest.raises(ValueError, match="only"):
            guarded_retry(session, job, expected, action=action)
    legacy = ReconciliationControlRequest(reason="Legacy manual retry remains compatible.", password="synthetic",
        idempotency_key="legacy-control-key")
    assert legacy.expected_state is None
    for field in ("updated_at", "diagnostics_at"):
        payload = expected.model_dump(mode="json")
        payload[field] = "2026-01-01T00:00:00"
        with pytest.raises(ValueError, match="timezone"):
            type(expected).model_validate(payload)


@pytest.mark.parametrize("change", ["new_hold", "diagnostic_revision", "terminal_binding"])
def test_postgres_guarded_retry_rechecks_stale_session_after_locked_state_change(pg_scenario, change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zk_add.models import Connector
    factory, (connector_id, job_id, _), _ = pg_scenario
    with factory() as db:
        expected = retry_expectation(db, db.get(Connector, connector_id), db.get(ReconciliationJob, job_id))
    loaded, changed = Event(), Event()
    def retry_with_stale_rows():
        with factory() as db:
            stale_connector = db.get(Connector, connector_id)
            stale_binding = stale_connector.zkt_device
            stale_job = db.get(ReconciliationJob, job_id)
            loaded.set()
            assert changed.wait(3)
            with pytest.raises(ValueError, match="evidence changed"):
                guarded_retry(db, stale_job, expected)
            assert stale_binding is not None
            db.rollback()
    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(retry_with_stale_rows)
        assert loaded.wait(3)
        connector = first.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        job = first.scalar(select(ReconciliationJob).where(ReconciliationJob.id == job_id).with_for_update())
        if change == "new_hold":
            job.error_code = job.wait_reason = "SOURCE_MANIFEST_GAP"
        elif change == "diagnostic_revision":
            connector.firmware_diagnostics = {"sample_sequence": 8}
        else:
            connector.zkt_device.serial = "CHANGED-TERMINAL"
        first.flush()
        changed.set()
        first.commit()
        pending.result(timeout=10)
    with factory() as db:
        job = db.get(ReconciliationJob, job_id)
        assert job.status == "NEEDS_ATTENTION" and job.retry_count == expected.retry_count
        assert db.scalar(select(func.count(ReconciliationEvent.id)).where(
            ReconciliationEvent.idempotency_key == "guarded-retry-once")) == 0
