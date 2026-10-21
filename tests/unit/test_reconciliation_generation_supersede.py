"""A re-onboarded connector certifies over its previous generation's coverage only with raw lineage proof.

MULTAN-01 (job e57a3a37) re-onboarded from generation 1 to 2 while its
generation-1 coverage kept tailing past the new job's cutoff. Same-epoch
retention could never bind across generations, so sealing held forever.
"""
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from test_reconciliation import _raw_record, reconciliation_db, SERIAL  # noqa: F401
from test_reconciliation_monotonic_coverage import (manifest, postgres_copy, postgres_url, retry_expectation,
                                                    snapshot, source)
from zk_add import reconciliation as service
from zk_add.models import (AttendanceEvent, Connector, DeviceAlert, OrdsOutbox, ReconciliationChunk,
                           ReconciliationCoverage, ReconciliationDivergence, ReconciliationEvent, ReconciliationJob,
                           TerminalRecordManifest, TerminalSourceEpoch)
from zk_add.schemas import (AttendanceEventIn, Envelope, ReconciliationAnchorRequest, ReconciliationChunkRequest,
                           ReconciliationSourceRecord, SourceTailChunkRequest)
from zk_add.time_utils import utc_now

BINDING = "SOURCE_COVERAGE_RETENTION_BINDING_MISMATCH"


def drifted(ordinal):
    """Identical raw bytes, but a newer firmware derived a different record key."""
    row = source(ordinal)
    return row.model_copy(update={"terminal_record_key": hashlib.sha256(b"2.7.5-key" + bytes([ordinal])).hexdigest(),
                                  "occurrence_index": 2})


def punch(ordinal, capture="FULL_HISTORY"):
    raw = _raw_record(uid=7, hour=8, minute=ordinal)
    return ReconciliationSourceRecord(ordinal=ordinal, raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(b"punch" + raw).hexdigest(), occurrence_index=1, disposition="EVENT",
        raw_record_b64=base64.b64encode(raw).decode(), raw_timestamp=ordinal, observed_uid="7", observed_user_id="1007",
        event=AttendanceEventIn(event_uid=hashlib.sha256(f"punch-{ordinal}".encode()).hexdigest(), uid="7",
            user_id="1007", raw_name="Ayesha-3520212345671",
            device_event_time=datetime(2026, 8, 6, 3, ordinal, tzinfo=timezone.utc), captured_at=utc_now(),
            source=capture, terminal_serial=SERIAL, terminal_identity_fingerprint="a" * 64, punch=0, status=0,
            clock_quality="OK", raw_event={"terminal_uid": "7"}))


def create(session, connector, cutoff, key, record=source):
    job = service.create_reconciliation_job(session, connector=connector, actor="test",
        reason="Verify preserved source custody.", confirmation="RECONCILE 1 FROM START", idempotency_key=key)
    service.apply_reconciliation_anchor(session, connector=connector, payload=ReconciliationAnchorRequest(
        job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
        generation=job.terminal_generation, terminal_generation=job.terminal_generation,
        terminal_serial=SERIAL, cutoff_count=cutoff, latest_terminal_count=connector.zkt_device.attendance_count,
        record_size=40, source_total_bytes=4 + cutoff * 40, first_anchor_digest=record(0).raw_record_digest))
    return job


def scan(session, connector, job, end, record=source, width=100):
    sequence = session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id))
    while job.committed_next_ordinal < end:
        start, stop = job.committed_next_ordinal, min(job.committed_next_ordinal + width, end)
        draft = ReconciliationChunkRequest(job_id=job.job_id, source_epoch=service.source_epoch_uuid(session, job),
            generation=job.terminal_generation, sequence=sequence, start_ordinal=start, end_ordinal=stop,
            previous_chain_digest=job.last_chain_digest, chunk_digest="0" * 64, resulting_chain_digest="0" * 64,
            records=[record(i) for i in range(start, stop)])
        digest = service.reconciliation_chunk_digest(draft)
        chain = service.reconciliation_chain_digest(job.last_chain_digest, start_ordinal=start, end_ordinal=stop,
                                                    chunk_digest=digest)
        _, chunk, _ = service.apply_reconciliation_chunk(session, connector=connector,
            payload=draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain}))
        assert chunk is not None
        sequence += 1


def tail_payload(session, coverage, start, stop, record=source, **update):
    epoch = session.get(TerminalSourceEpoch, coverage.source_epoch_id)
    draft = SourceTailChunkRequest(source_epoch=epoch.epoch_id, terminal_serial=SERIAL,
        terminal_generation=coverage.terminal_generation, record_size=40, start_ordinal=start, end_ordinal=stop,
        latest_terminal_count=stop, previous_chain_digest=coverage.source_committed_chain_digest,
        chunk_digest="0" * 64, resulting_chain_digest="0" * 64, records=[record(i) for i in range(start, stop)])
    digest = service.reconciliation_chunk_digest(draft)
    chain = service.reconciliation_chain_digest(coverage.source_committed_chain_digest,
        start_ordinal=start, end_ordinal=stop, chunk_digest=digest)
    return draft.model_copy(update={"chunk_digest": digest, "resulting_chain_digest": chain, **update})


def tail(session, connector, coverage, end, record=source):
    while coverage.source_committed_cursor < end:
        start = coverage.source_committed_cursor
        _, chunk, _, error = service.apply_source_tail_chunk(session, connector=connector,
            payload=tail_payload(session, coverage, start, min(start + 100, end), record))
        assert error is None and chunk is not None
    connector.zkt_device.attendance_count = end


def seal(session, connector, job):
    return service.apply_reconciliation_manifest(session, connector=connector,
        payload=manifest(connector, job, service.source_epoch_uuid(session, job)))


def active(session):
    return session.scalar(select(ReconciliationCoverage).where(ReconciliationCoverage.active == True))  # noqa: E712


def generation_scenario(db, *, baseline=5, current=11, cutoff=7, old=source, new=source, recovery_at=None,
                        old_complete=False):
    """Generation 1 certifies `baseline` and tails to `current`; the connector re-onboards; a generation-2 job scans `cutoff`."""
    session, connector = db
    connector.firmware_version = "zone-lite-2.6.20"
    connector.zkt_device.attendance_count = current
    prior = create(session, connector, baseline, "generation-one", old)
    if recovery_at is not None:
        # MULTAN-01's epoch 46 is a recovery epoch: its prefix is RECOVERY_PREFIX copies.
        scan(session, connector, prior, recovery_at, old)
        from zk_add.crypto import encrypt_text
        divergence = ReconciliationDivergence(job_id=prior.id, source_epoch_id=prior.source_epoch_id,
            ordinal=recovery_at, state="PROBING", old_raw_digest="e" * 64,
            new_raw_digest=old(recovery_at).raw_record_digest, old_disposition="TERMINAL_DUPLICATE",
            new_disposition="TERMINAL_DUPLICATE", protected_new_raw_record=encrypt_text(old(recovery_at).raw_record_b64),
            observations=[])
        session.add(divergence)
        session.flush()
        service._activate_recovery_epoch(session, job=prior, divergence=divergence, now=utc_now())
    scan(session, connector, prior, baseline, old)
    seal(session, connector, prior)
    if old_complete:
        prior.status = "COMPLETED"  # its Oracle membership was certified before the re-onboard
    coverage = active(session)
    tail(session, connector, coverage, current, old)
    connector.onboarding_generation = 2
    # A lagging tail: the terminal can hold more records than the old tail committed.
    connector.zkt_device.attendance_count = max(current, cutoff)
    job = create(session, connector, cutoff, "generation-two", new)
    scan(session, connector, job, cutoff, new, width=97)
    session.commit()
    assert job.terminal_generation == 2 and coverage.terminal_generation == 1
    return session, connector, prior, coverage, job


def test_previous_generation_coverage_is_superseded_with_sealed_lineage_and_positive_ack(reconciliation_db, monkeypatch):  # noqa: F811
    from zk_add import web
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    old_epoch = session.get(TerminalSourceEpoch, old.source_epoch_id)
    connector.boot_id, connector.last_sequence = "generation-boot", 10
    session.commit()
    monkeypatch.setattr("zk_add.db.SessionLocal", sessionmaker(session.get_bind(), autoflush=False, expire_on_commit=False))
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()), boot_id="generation-boot", seq=11,
        sent_at=utc_now(), type="reconcile_source_manifest",
        payload=manifest(connector, job, service.source_epoch_uuid(session, job)).model_dump(mode="json"))
    outcome = web.persist_envelope(connector.id, envelope)
    # A positive ACK makes every Zone Lite generation install generation 2 at the cutoff.
    assert outcome.ack["type"] == "reconcile_manifest_ack" and outcome.coverage is None
    assert outcome.ack["source_epoch"] == service.source_epoch_uuid(session, job)
    session.expire_all()
    new = active(session)
    assert (new.job_id, new.terminal_generation, new.source_committed_cursor) == (job.id, 2, 7)
    assert (old.active, old.invalidated_reason, old.source_committed_cursor) == (
        False, service.GENERATION_SUPERSEDED_REASON, 11)
    assert old_epoch.state == "SUPERSEDED" and old_epoch.superseded_at is not None
    certificate = job.capture_certificate
    assert service._valid_sealed_evidence(certificate) and "retained_coverage" not in certificate
    sealed = certificate["superseded_generation"]
    assert (sealed["coverage_id"], sealed["terminal_generation"], sealed["source_epoch"]) == (
        old.coverage_id, 1, old_epoch.epoch_id)
    assert (sealed["source_committed_cursor"], sealed["replayed_after_cutoff"]) == (11, 4)
    assert sealed["lineage"]["compared_records"] == 7 and len(sealed["lineage"]["lineage_sha256"]) == 64
    assert sealed["interpretation_drift"]["count"] == 0
    assert sealed["capture_evidence_signature"] == old.capture_evidence["evidence_signature"]
    assert job.status == "COMPLETED"
    states = [row.state for row in session.scalars(select(ReconciliationEvent).where(ReconciliationEvent.job_id == job.id))]
    assert "SOURCE_GENERATION_SUPERSEDED" in states
    from zk_add.models import AuditEvent
    assert session.scalar(select(AuditEvent).where(AuditEvent.action == "SOURCE_COVERAGE_GENERATION_SUPERSEDED"))
    # The lost-ACK replay returns the same receipt and the same positive ACK.
    assert web.persist_envelope(connector.id, envelope).ack == outcome.ack
    # Generation 2 re-reads ordinals 7..11 that generation 1 already committed.
    tail(session, connector, new, 11)
    session.commit()
    assert new.source_committed_cursor == 11 and old.source_committed_cursor == 11


def test_generation_two_tail_replays_old_tail_without_duplicate_attendance_or_outbox(reconciliation_db):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(
        reconciliation_db, old=lambda i: punch(i, "CURRENT_RECONCILE" if i >= 5 else "FULL_HISTORY"),
        new=punch, old_complete=True)
    events = session.scalar(select(func.count(AttendanceEvent.id)))
    outbox = session.scalar(select(func.count(OrdsOutbox.id)))
    assert events == 11
    seal(session, connector, job)
    new = active(session)
    assert new.job_id == job.id and job.capture_certified_at is not None
    tail(session, connector, new, 11, lambda i: punch(i, "CURRENT_RECONCILE"))
    session.commit()
    assert session.scalar(select(func.count(AttendanceEvent.id))) == events
    assert session.scalar(select(func.count(OrdsOutbox.id))) == outbox
    linked = session.scalar(select(func.count(TerminalRecordManifest.id)).where(
        TerminalRecordManifest.source_epoch_id == new.source_epoch_id,
        TerminalRecordManifest.attendance_event_id.is_not(None)))
    assert linked == 11


def test_production_sized_previous_generation_is_proven_across_pages(reconciliation_db):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(reconciliation_db, baseline=3086, current=4512, cutoff=3172)
    seal(session, connector, job)
    sealed = job.capture_certificate["superseded_generation"]
    assert job.status == "COMPLETED" and active(session).job_id == job.id
    assert sealed["lineage"]["compared_records"] == 3172 and sealed["replayed_after_cutoff"] == 1340
    expected = hashlib.sha256()
    for ordinal in range(3172):
        expected.update(json.dumps([ordinal, source(ordinal).raw_record_digest, 40], separators=(",", ":")).encode() + b"\n")
    assert sealed["lineage"]["lineage_sha256"] == expected.hexdigest()


def test_recovery_epoch_with_recovery_prefix_and_record_key_drift_still_proves_lineage(reconciliation_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "LINEAGE_PROOF_PAGE", 2)  # several pages across all three source kinds
    session, connector, prior, old, job = generation_scenario(reconciliation_db, recovery_at=3, new=drifted)
    old_epoch = session.get(TerminalSourceEpoch, old.source_epoch_id)
    assert old_epoch.sequence == 2 and old_epoch.parent_epoch_id is not None
    kinds = set(session.scalars(select(TerminalRecordManifest.source_kind).where(
        TerminalRecordManifest.source_epoch_id == old.source_epoch_id)))
    assert kinds == {"RECOVERY_PREFIX", "BASELINE", "TAIL"}
    seal(session, connector, job)
    assert job.capture_certified_at is not None and active(session).job_id == job.id
    drift = job.capture_certificate["superseded_generation"]["interpretation_drift"]
    assert drift["count"] == 7 and drift["first_ordinals"] == list(range(7)) and len(drift["drift_sha256"]) == 64
    assert old_epoch.state == "SUPERSEDED" and old.invalidated_reason == service.GENERATION_SUPERSEDED_REASON


def test_shorter_previous_generation_coverage_also_requires_and_records_lineage(reconciliation_db):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(reconciliation_db, baseline=5, current=5, cutoff=7)
    seal(session, connector, job)
    sealed = job.capture_certificate["superseded_generation"]
    assert sealed["lineage"]["compared_records"] == 5 and sealed["replayed_after_cutoff"] == 0
    assert old.invalidated_reason == service.GENERATION_SUPERSEDED_REASON


def test_real_lineage_divergence_holds_alerts_refuses_retry_and_cancel_keeps_custody(reconciliation_db):  # noqa: F811
    def changed(ordinal):
        if ordinal != 3:
            return source(ordinal)
        raw = b"\x03changed" + b"\0" * 32
        return source(3).model_copy(update={"raw_record_digest": hashlib.sha256(raw).hexdigest(),
                                            "raw_record_b64": base64.b64encode(raw).decode()})
    session, connector, prior, old, job = generation_scenario(reconciliation_db, new=changed)
    before = snapshot(old)
    old_epoch = session.get(TerminalSourceEpoch, old.source_epoch_id)
    seal(session, connector, job)
    session.commit()
    assert (job.status, job.error_code, job.capture_certified_at) == (
        "NEEDS_ATTENTION", service.SOURCE_GENERATION_LINEAGE_DIVERGED, None)
    assert "ordinal 3" in job.error_message
    assert snapshot(old) == before and old_epoch.state == "ACTIVE"
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 1
    alert = session.scalar(select(DeviceAlert).where(DeviceAlert.code == "ADD_SOURCE_GENERATION_LINEAGE_DIVERGED"))
    assert alert.state == "OPEN" and alert.details["ordinal"] == 3
    assert alert.details["previous_raw_record_digest"] == source(3).raw_record_digest
    assert service.predicted_finalisation_hold(session, job) == service.SOURCE_GENERATION_LINEAGE_DIVERGED
    state, message = service._operator_status(job, connected=True)
    assert state == "REVIEW_REQUIRED" and "Cancelling is safe" in message
    for action in ("retry", "resume"):
        with pytest.raises(ValueError, match="Retry would hold again"):
            service.control_reconciliation_job(session, job=job, action=action, actor="test",
                reason="Retry the held generation seal.", idempotency_key=f"lineage-{action}")
    assert job.status == "NEEDS_ATTENTION" and job.retry_count == 0
    service.control_reconciliation_job(session, job=job, action="cancel", actor="test",
        reason="Documented exit: keep the existing coverage.", idempotency_key="lineage-cancel")
    session.commit()
    assert job.status == "CANCELLED" and snapshot(old) == before
    tail(session, connector, old, 12)  # the generation-1 tail keeps custody
    session.commit()
    assert old.active and old.source_committed_cursor == 12


@pytest.mark.parametrize("fault", ["missing_epoch", "superseded_epoch", "wrong_generation_epoch", "serial",
                                   "old_gap", "limit"])
def test_unprovable_previous_generation_binding_holds_without_replacing_it(reconciliation_db, monkeypatch, fault):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    old_epoch = session.get(TerminalSourceEpoch, old.source_epoch_id)
    expected = BINDING
    if fault == "missing_epoch":
        old.source_epoch_id = None
    elif fault == "superseded_epoch":
        old_epoch.state = "SUPERSEDED"
    elif fault == "wrong_generation_epoch":
        old_epoch.terminal_generation = 3
    elif fault == "serial":
        old.terminal_serial = "ANOTHER-TERMINAL"
    elif fault == "old_gap":
        session.delete(session.scalar(select(TerminalRecordManifest).where(
            TerminalRecordManifest.source_epoch_id == old.source_epoch_id, TerminalRecordManifest.ordinal == 2)))
        expected = "SOURCE_COVERAGE_CANONICAL_GAP"
    else:
        monkeypatch.setattr(service, "COVERAGE_PROOF_MAX_RECORDS", 6)
        expected = "SOURCE_COVERAGE_PROOF_LIMIT"
    session.flush()
    before = snapshot(old)
    seal(session, connector, job)
    assert (job.status, job.error_code, job.capture_certified_at) == ("NEEDS_ATTENTION", expected, None)
    assert snapshot(old) == before
    assert session.scalar(select(func.count(ReconciliationCoverage.id))) == 1
    # Same evidence, same answer: a plain retry is refused before any mutation.
    with pytest.raises(ValueError, match="Retry would hold again"):
        service.control_reconciliation_job(session, job=job, action="retry", actor="test",
            reason="Retry the held generation seal.", idempotency_key="binding-retry")


def test_newer_coverage_generation_holds_the_older_job_as_stale(reconciliation_db):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    old.terminal_generation = 3
    session.get(TerminalSourceEpoch, old.source_epoch_id).terminal_generation = 3
    session.flush()
    before = snapshot(old)
    seal(session, connector, job)
    assert (job.status, job.error_code) == ("NEEDS_ATTENTION", service.SOURCE_GENERATION_STALE)
    assert snapshot(old) == before and old.terminal_generation == 3
    service.control_reconciliation_job(session, job=job, action="cancel", actor="test",
        reason="The stale generation job cannot seal.", idempotency_key="stale-cancel")
    preflight = service.preflight_reconciliation(session, connector)
    assert preflight["generation"]["state"] == "NEWER_THAN_CONNECTOR"
    assert [row["code"] for row in preflight["hard_blockers"]] == [service.SOURCE_GENERATION_STALE]


def test_e57a3a37_shaped_hold_certifies_on_one_retry_without_rescan(reconciliation_db):  # noqa: F811
    session, connector, prior, old, job = generation_scenario(reconciliation_db, baseline=5, current=12, cutoff=9)
    # Before this fix sealing held here; production retried once and held again.
    service._safety_hold(session, job, BINDING, "The longer committed source coverage could not be safely retained.")
    session.commit()
    chunks = session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id))
    manifests = session.scalar(select(func.count(TerminalRecordManifest.id)).where(
        TerminalRecordManifest.source_epoch_id == job.source_epoch_id))
    preflight = service.preflight_reconciliation(session, connector)
    generation = preflight["generation"]
    assert (generation["state"], generation["connector_generation"], generation["coverage_generation"]) == (
        "PREVIOUS_GENERATION", 2, 1)
    assert not preflight["eligible"] and [(row["code"], row["job_id"]) for row in preflight["hard_blockers"]] == [
        ("RECONCILIATION_HELD", job.job_id)]
    assert BINDING in preflight["hard_blockers"][0]["message"]
    assert service.predicted_finalisation_hold(session, job) is None
    service.control_reconciliation_job(session, job=job, action="retry", actor="test",
        reason="Retry after the generation supersede fix.", idempotency_key="e57-retry")
    session.commit()
    assert (job.status, job.retry_count) == ("QUEUED", 1)
    # The scheduler offers the manifest handshake at the committed cutoff; the job never blocks itself.
    assignments = dict(service.assignment_rows(session))
    offer = assignments[connector.connector_id]
    assert offer["committed_next_ordinal"] == offer["cutoff_count"] == 9 and job.status == "QUEUED"
    seal(session, connector, job)
    session.commit()
    assert job.capture_certified_at is not None and job.status == "COMPLETED"
    assert active(session).job_id == job.id and old.invalidated_reason == service.GENERATION_SUPERSEDED_REASON
    assert session.scalar(select(func.count(ReconciliationChunk.id)).where(ReconciliationChunk.job_id == job.id)) == chunks
    assert session.scalar(select(func.count(TerminalRecordManifest.id)).where(
        TerminalRecordManifest.source_epoch_id == job.source_epoch_id)) == manifests
    assert service.preflight_reconciliation(session, connector)["generation"]["state"] == "MATCHED"


@pytest.mark.parametrize("with_epoch", [True, False])
def test_late_old_generation_tail_gets_structured_error_and_current_coverage(reconciliation_db, monkeypatch, with_epoch):  # noqa: F811
    from zk_add import web
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    old_epoch_id = session.get(TerminalSourceEpoch, old.source_epoch_id).epoch_id
    seal(session, connector, job)
    new = active(session)
    stale = tail_payload(session, old, 11, 12, source_epoch=old_epoch_id if with_epoch else None)
    connector.boot_id, connector.last_sequence = "stale-tail-boot", 20
    lifecycle = connector.lifecycle_state
    session.commit()
    before_new, before_old = snapshot(new), snapshot(old)
    monkeypatch.setattr("zk_add.db.SessionLocal", sessionmaker(session.get_bind(), autoflush=False, expire_on_commit=False))
    envelope = Envelope(connector_id=connector.connector_id, message_id=str(uuid4()), boot_id="stale-tail-boot",
        seq=21, sent_at=utc_now(), type="source_tail_chunk", payload=stale.model_dump(mode="json"))
    outcome = web.persist_envelope(connector.id, envelope)
    assert outcome.ack["type"] == "error" and outcome.ack["code"] == service.SOURCE_TAIL_STALE_GENERATION
    pushed = json.loads(json.dumps(outcome.coverage))
    assert pushed["type"] == "source_coverage" and pushed["active"] is True
    assert (pushed["terminal_generation"], pushed["source_committed_cursor"], pushed["source_epoch"]) == (
        2, 7, service.source_epoch_uuid(session, new))
    session.expire_all()
    assert snapshot(new) == before_new and snapshot(old) == before_old and new.active
    assert (connector.lifecycle_state, connector.last_error_code) == (lifecycle, None)
    assert session.scalar(select(func.count(DeviceAlert.id)).where(DeviceAlert.code.in_(
        ["DEVICE_MESSAGE_REJECTED", "ADD_SOURCE_COVERAGE_INVALIDATED"]))) == 0


def test_old_generation_tail_with_a_foreign_epoch_keeps_the_ordinary_rejection(reconciliation_db):  # noqa: F811
    from zk_add.rejection import EvidenceRejected
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    seal(session, connector, job)
    new = active(session)
    before = snapshot(new)
    with pytest.raises(EvidenceRejected, match="epoch"):
        service.apply_source_tail_chunk(session, connector=connector,
            payload=tail_payload(session, old, 11, 12, source_epoch=str(uuid4())))
    assert snapshot(new) == before and new.active


def test_guarded_retry_compares_the_jobs_generation_not_the_connectors(reconciliation_db):  # noqa: F811
    # SLICTOWER-3FL: its held job is generation 5 while the connector re-onboarded to 6.
    from test_reconciliation_monotonic_coverage import scenario
    session, connector, job, coverage, _ = scenario(reconciliation_db)
    expected = retry_expectation(session, connector, job)
    connector.onboarding_generation = job.terminal_generation + 1
    session.commit()
    with pytest.raises(ValueError, match="evidence changed"):
        service.control_reconciliation_job(session, job=job, action="retry", actor="test",
            reason="Guarded retry across a re-onboard.", idempotency_key="pinned-wrong",
            expected_state=expected.model_copy(update={"connector_generation": job.terminal_generation}))
    service.control_reconciliation_job(session, job=job, action="retry", actor="test",
        reason="Guarded retry across a re-onboard.", idempotency_key="pinned-right",
        expected_state=expected.model_copy(update={"connector_generation": connector.onboarding_generation}))
    session.commit()
    assert job.status == "QUEUED" and job.terminal_generation == connector.onboarding_generation - 1


def test_guarded_retry_without_connector_generation_keeps_its_original_digest(reconciliation_db):  # noqa: F811
    from test_reconciliation_monotonic_coverage import scenario
    session, connector, job, coverage, _ = scenario(reconciliation_db)
    expected = retry_expectation(session, connector, job)
    service.control_reconciliation_job(session, job=job, action="retry", actor="test",
        reason="Guarded retry from an older client.", idempotency_key="legacy-digest", expected_state=expected)
    event = session.scalar(select(ReconciliationEvent).where(ReconciliationEvent.idempotency_key == "legacy-digest"))
    legacy = expected.model_dump(mode="json")
    legacy.pop("connector_generation")
    assert event.details["expected_state_sha256"] == hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_attendance_repair_does_not_attach_to_a_deterministically_held_job(reconciliation_db):  # noqa: F811
    from zk_add.attendance_repair import RepairError, _attach_source_dependency
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    service._safety_hold(session, job, BINDING, "held")
    with pytest.raises(RepairError) as raised:
        _attach_source_dependency(session, job=SimpleNamespace(actor="test", job_id="repair"), connector=connector)
    assert raised.value.code == "SOURCE_DEPENDENCY_UNAVAILABLE" and job.job_id in str(raised.value)


def test_source_load_baseline_binds_after_the_generation_supersede(reconciliation_db):  # noqa: F811
    from zk_add.zkt_source_load import source_load_baseline
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    before = source_load_baseline(session, connector)
    assert before["reasons"] == ["SOURCE_BINDING_OR_EPOCH_UNVERIFIED"]
    seal(session, connector, job)
    session.commit()
    after = source_load_baseline(session, connector)
    assert "SOURCE_BINDING_OR_EPOCH_UNVERIFIED" not in after["reasons"]
    assert after["source_snapshot"]["generation"] == 2 and after["source_inventory_complete"]


def test_same_generation_retention_is_unchanged(reconciliation_db):  # noqa: F811
    from test_reconciliation_monotonic_coverage import finish, scenario
    data = scenario(reconciliation_db)
    session, connector, job, coverage, _ = data
    before = snapshot(coverage)
    finish(data)
    assert job.status == "COMPLETED" and snapshot(coverage) == before
    assert "superseded_generation" not in job.capture_certificate
    assert job.capture_certificate["retained_coverage"]["coverage_id"] == coverage.coverage_id


@pytest.fixture
def pg_generation(reconciliation_db):  # noqa: F811
    url = postgres_url()
    session, connector, prior, old, job = generation_scenario(reconciliation_db)
    payload = manifest(connector, job, service.source_epoch_uuid(session, job))
    old_epoch = session.get(TerminalSourceEpoch, old.source_epoch_id).epoch_id
    with postgres_copy(session, url) as factory:
        yield factory, (connector.id, job.id, old.id), payload, old_epoch


@pytest.mark.parametrize("first_action", ["manifest", "tail"])
def test_postgres_old_tail_and_new_generation_manifest_commit_in_either_order(pg_generation, first_action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    factory, (connector_id, job_id, old_id), payload, old_epoch = pg_generation
    waiting, results = Event(), {}

    def act(db, action):
        connector = db.get(Connector, connector_id)
        if action == "manifest":
            service.apply_reconciliation_manifest(db, connector=connector, payload=payload)
        else:
            old = db.get(ReconciliationCoverage, old_id)
            results["tail"] = service.apply_source_tail_chunk(db, connector=connector,
                payload=tail_payload(db, old, 11, 12, source_epoch=old_epoch))[3]
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
        old, job = db.get(ReconciliationCoverage, old_id), db.get(ReconciliationJob, job_id)
        new = active(db)
        assert job.capture_certified_at and job.status == "COMPLETED"
        assert (new.job_id, new.terminal_generation, new.source_committed_cursor) == (job_id, 2, 7)
        assert not old.active and old.invalidated_reason == service.GENERATION_SUPERSEDED_REASON
        if first_action == "tail":
            assert results["tail"] is None and old.source_committed_cursor == 12
            assert job.capture_certificate["superseded_generation"]["source_committed_cursor"] == 12
        else:
            assert results["tail"] == service.SOURCE_TAIL_STALE_GENERATION and old.source_committed_cursor == 11
        assert db.get(TerminalSourceEpoch, old.source_epoch_id).state == "SUPERSEDED"
        saved = deepcopy(job.capture_certificate)
        service.apply_reconciliation_manifest(db, connector=db.get(Connector, connector_id), payload=payload)
        db.commit()
        assert job.capture_certificate == saved and db.scalar(select(func.count(ReconciliationCoverage.id))) == 2
