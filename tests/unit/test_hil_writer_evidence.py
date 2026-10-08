"""Synthetic source ingress to typed Oracle proof; no field qualification."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import event as sa_event, select

from test_zkt_source_attendance import intake, permit, prepared, source_store, store  # noqa: F401
from zk_add import zkt_custody_work
from zk_add.attendance_repair import _protected_digest
from zk_add.crypto import decrypt_json, encrypt_json
from zk_add.hil_runs import _release_identity
from zk_add.hil_writer_evidence import collect_writer_attendance
from zk_add.models import (
    AttendanceEvent, Connector, ReconciliationCoverage, ReconciliationJob,
    SourceTailChunk, TerminalRecordManifest, TerminalSourceEpoch, ZktOccurrenceAlias,
    ZktOracleContentReceipt, ZktOracleIntent, ZktOracleMembershipReceipt, ZktSourceAttendance,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareHilRun, FirmwareRelease, OTA_LAYOUT
from zk_add.service import oracle_payload
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.zkt_oracle_delivery import MEMBERSHIP_SCOPE, VERIFICATION_SCOPE, verification_check


@pytest.fixture
def observed(prepared):  # noqa: F811
    with prepared() as db:
        connector = db.scalar(select(Connector))
        connector.zkt_device.expected_serial = connector.zkt_device.serial
        connector.zkt_device.terminal_binding_state = "CONFIRMED"
        permit(db)
        intake(db, length=2)
        assert zkt_custody_work.advance_work_batch(db, time_budget_ms=None).processed == 2
        db.commit()
        coverage = db.scalar(select(ReconciliationCoverage))
        epoch = db.get(TerminalSourceEpoch, coverage.source_epoch_id)
        job = db.get(ReconciliationJob, coverage.job_id)
        now = utc_now()
        release = FirmwareRelease(release_id="synthetic-writer", version="2.7.0", git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1024, signing_key_id="synthetic", partition_layout=OTA_LAYOUT,
            minimum_bootstrap_version="2.6.19", storage_name="synthetic.bin", manifest_signature="synthetic",
            state="HIL_ONLY", manifest={"application_sha256": "c" * 64, "runtime_profile": "ZKT_JOURNAL_V1"})
        db.add(release)
        db.flush()
        campaign = FirmwareCampaign(campaign_id="synthetic", release_id=release.id, zone_id=connector.zone_id,
            actor="test", idempotency_key="synthetic", reason="Synthetic evidence test", typed_confirmation="2.7.0")
        db.add(campaign)
        db.flush()
        deployment = FirmwareDeployment(deployment_id="synthetic", campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, target_version="2.7.0", status="SUCCEEDED")
        db.add(deployment)
        db.flush()
        run = FirmwareHilRun(run_id="synthetic-writer-run", deployment_id=deployment.id,
            connector_id=connector.id, release_id=release.id, actor="test", idempotency_key="synthetic",
            target={"connector_id": connector.connector_id, "mac": connector.hardware_id,
                    "terminal_serial": connector.zkt_device.serial},
            release_identity=_release_identity(release).model_dump(mode="json"),
            started_at=now - timedelta(minutes=1), ends_at=now + timedelta(minutes=14),
            baseline={"profile": "FULL_REMOTE_HIL_V1", "coverage_id": coverage.coverage_id,
                "job_id": job.job_id, "source_epoch_id": epoch.id, "source_epoch": epoch.epoch_id,
                "source_generation": 1, "source_cursor": 0, "source_chain": "a" * 64}, result={})
        db.add(run)
        db.commit()
    return prepared


def seal_oracle(db, *, scope=MEMBERSHIP_SCOPE):
    connector = db.scalar(select(Connector))
    for intent in db.scalars(select(ZktOracleIntent)):
        event = db.get(AttendanceEvent, intent.attendance_event_id)
        payload = oracle_payload(connector, connector.zkt_device, event, "SYNTHETIC-PRIVATE-CNIC")
        payload["employee_name"] = "SYNTHETIC-PRIVATE-NAME"
        check = verification_check(payload, scope)
        intent.verification_scope = scope
        intent.protected_payload, intent.protected_check = encrypt_json(payload), encrypt_json(check)
        intent.payload_digest, intent.prepared_at = _protected_digest(payload), utc_now()
        values = dict(intent_id=intent.id, event_uid=event.event_uid, payload_digest=intent.payload_digest,
            verification_scope=scope, claim_attempt=1)
        if scope == MEMBERSHIP_SCOPE:
            receipt = ZktOracleMembershipReceipt(**values, request_digest=check["request_digest"], response_digest="d" * 64)
        else:
            receipt = ZktOracleContentReceipt(**values, request_digest=_protected_digest(check), content_token="e" * 64)
        db.add(receipt)
    db.commit()


def collect(db, **kwargs):
    run = db.scalar(select(FirmwareHilRun))
    return collect_writer_attendance(db, run.run_id, now=ensure_utc(run.ends_at) + timedelta(seconds=1), **kwargs)


@pytest.mark.parametrize("scope", [MEMBERSHIP_SCOPE, VERIFICATION_SCOPE])
def test_real_source_derivation_is_linked_to_typed_receipts_without_mutating_or_finalizing(observed, scope):
    with observed() as db:
        seal_oracle(db, scope=scope)
        statements = []
        def record(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        sa_event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            report = collect(db)
        finally:
            sa_event.remove(db.get_bind(), "before_cursor_execute", record)
        assert report["reasons"] == []
        assert len(report["occurrences"]) == 2
        assert len({item["occurrence_id"] for item in report["occurrences"]}) == 2
        assert all(item["custody"]["tail_chunk_id"] for item in report["occurrences"])
        receipts = [item["oracle_receipts"][0] for item in report["occurrences"]]
        assert all(row["verification_scope"] == scope for row in receipts)
        assert all(row["table"] == (ZktOracleMembershipReceipt if scope == MEMBERSHIP_SCOPE
                                   else ZktOracleContentReceipt).__tablename__ for row in receipts)
        assert all(row["oracle_raw_content_and_day_times"] == (
            "NOT_ASSERTED" if scope == MEMBERSHIP_SCOPE else "RECORDED") for row in receipts)
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert not db.new and not db.dirty and not db.deleted
        run = db.scalar(select(FirmwareHilRun))
        assert run.status == "OBSERVING" and run.result == {}
        assert report["hil_verdict"] == "NOT_EVALUATED"
        serialized = json.dumps(report)
        assert "SYNTHETIC-PRIVATE" not in serialized and "protected_payload" not in serialized


def test_pending_attendance_and_oracle_do_not_disappear(observed):
    with observed() as db:
        link = db.scalar(select(ZktSourceAttendance).order_by(ZktSourceAttendance.id))
        db.delete(link)
        db.commit()
        report = collect(db)
        assert len(report["occurrences"]) == 2
        assert "LOGICAL_ATTENDANCE_MISSING_OR_AMBIGUOUS" in report["reasons"]
        assert "ORACLE_INTENT_NOT_PREPARED" in report["reasons"]
        assert all(not row["oracle_receipts"] for row in report["occurrences"])


def test_success_labels_cannot_substitute_for_a_typed_receipt(observed):
    with observed() as db:
        seal_oracle(db)
        for receipt in db.scalars(select(ZktOracleMembershipReceipt)):
            db.delete(receipt)
        for row in db.scalars(select(AttendanceEvent)):
            row.ords_status = "ACKED_CHECK"
            row.oracle_confirmed_at = utc_now()
        db.commit()
        report = collect(db)
        assert report["reasons"] == ["TYPED_ORACLE_RECEIPT_MISSING"]
        assert all(not row["oracle_receipts"] for row in report["occurrences"])


def test_matching_digest_does_not_bind_a_different_punch_payload(observed):
    with observed() as db:
        seal_oracle(db)
        intent = db.scalar(select(ZktOracleIntent).order_by(ZktOracleIntent.id))
        payload = decrypt_json(intent.protected_payload)
        payload["punch"] = "99"
        check = verification_check(payload, MEMBERSHIP_SCOPE)
        intent.protected_payload, intent.protected_check = encrypt_json(payload), encrypt_json(check)
        intent.payload_digest = _protected_digest(payload)
        receipt = db.scalar(select(ZktOracleMembershipReceipt).where(ZktOracleMembershipReceipt.intent_id == intent.id))
        receipt.payload_digest = intent.payload_digest
        receipt.request_digest = check["request_digest"]
        db.commit()
        assert "ORACLE_FROZEN_PAYLOAD_UNVERIFIED" in collect(db)["reasons"]


@pytest.mark.parametrize("fault,reason", [
    ("unpinned", "RUN_SOURCE_EPOCH_UNPINNED"), ("epoch", "RUN_SOURCE_BINDING_CHANGED"),
    ("generation", "RUN_SOURCE_BINDING_CHANGED"), ("serial", "RUN_DEVICE_OR_RELEASE_BINDING_CHANGED"),
    ("image", "RUN_DEVICE_OR_RELEASE_BINDING_CHANGED"), ("profile", "FULL_WRITER_OBSERVATION_REQUIRED"),
])
def test_run_scope_cannot_be_inferred_or_retargeted(observed, fault, reason):
    with observed() as db:
        run = db.scalar(select(FirmwareHilRun))
        if fault in {"unpinned", "epoch", "generation", "profile"}:
            values = dict(run.baseline)
            if fault == "unpinned":
                values.pop("source_epoch")
            elif fault == "epoch":
                values["source_epoch"] = "wrong-epoch"
            elif fault == "generation":
                values["source_generation"] = 2
            else:
                values["profile"] = "BRIDGE_READINESS_V1"
            run.baseline = values
        elif fault == "serial":
            db.scalar(select(Connector)).zkt_device.confirmed_serial = "REPLACED"
        else:
            run.release_identity = {**run.release_identity, "application_sha256": "f" * 64}
        db.commit()
        report = collect(db)
        assert report["reasons"] == [reason] and report["occurrences"] == []


@pytest.mark.parametrize("fault,reason", [
    ("raw", "SOURCE_RAW_CUSTODY_UNVERIFIED"), ("alias", "OCCURRENCE_ALIAS_BINDING_CHANGED"),
    ("tail", "SOURCE_TAIL_RECEIPT_BINDING_CHANGED"), ("facts", "LOGICAL_ATTENDANCE_BINDING_UNVERIFIED"),
    ("intent", "ORACLE_INTENT_BINDING_CHANGED"), ("payload", "ORACLE_FROZEN_PAYLOAD_UNVERIFIED"),
    ("receipt-digest", "ORACLE_RECEIPT_BINDING_CHANGED"), ("receipt-uid", "ORACLE_RECEIPT_BINDING_CHANGED"),
    ("receipt-scope", "ORACLE_RECEIPT_BINDING_CHANGED"), ("receipt-late", "ORACLE_RECEIPT_BINDING_CHANGED"),
])
def test_broken_chain_is_explicit_and_cannot_adopt_an_unrelated_receipt(observed, fault, reason):
    with observed() as db:
        seal_oracle(db)
        if fault == "raw":
            db.scalar(select(TerminalRecordManifest).order_by(TerminalRecordManifest.id)).protected_raw_record = "corrupt"
        elif fault == "alias":
            db.scalar(select(ZktOccurrenceAlias).order_by(ZktOccurrenceAlias.id)).raw_digest = "f" * 64
        elif fault == "tail":
            db.scalar(select(SourceTailChunk)).generation += 1
        elif fault == "facts":
            db.scalar(select(AttendanceEvent).order_by(AttendanceEvent.id)).user_id = "wrong"
        elif fault == "intent":
            other = Connector(connector_id="unrelated", hardware_id="aa:bb:cc:dd:ee:99",
                zone_id="other", zone_name="other", device_id="other", display_name="other")
            db.add(other)
            db.flush()
            db.scalar(select(ZktOracleIntent).order_by(ZktOracleIntent.id)).connector_id = other.id
        elif fault == "payload":
            db.scalar(select(ZktOracleIntent).order_by(ZktOracleIntent.id)).payload_digest = "f" * 64
        else:
            receipt = db.scalar(select(ZktOracleMembershipReceipt).order_by(ZktOracleMembershipReceipt.id))
            if fault == "receipt-digest":
                receipt.request_digest = "f" * 64
            elif fault == "receipt-uid":
                receipt.event_uid = "unrelated"
            elif fault == "receipt-scope":
                receipt.verification_scope = VERIFICATION_SCOPE
            else:
                receipt.verified_at = db.scalar(select(FirmwareHilRun.ends_at)) + timedelta(seconds=1)
        db.commit()
        report = collect(db)
        assert reason in report["reasons"]
        assert report["hil_verdict"] == "NOT_EVALUATED"


def test_receipt_id_collision_across_tables_never_becomes_content_proof(observed):
    with observed() as db:
        seal_oracle(db)
        membership = db.scalar(select(ZktOracleMembershipReceipt).order_by(ZktOracleMembershipReceipt.id))
        db.add(ZktOracleContentReceipt(id=membership.id, intent_id=membership.intent_id,
            event_uid=membership.event_uid, payload_digest=membership.payload_digest,
            request_digest=membership.request_digest, verification_scope=VERIFICATION_SCOPE,
            content_token="f" * 64, claim_attempt=1))
        db.commit()
        report = collect(db)
        assert "ORACLE_RECEIPT_BINDING_CHANGED" in report["reasons"]
        assert all(receipt["table"] == ZktOracleMembershipReceipt.__tablename__
                   and receipt["oracle_raw_content_and_day_times"] == "NOT_ASSERTED"
                   for item in report["occurrences"] for receipt in item["oracle_receipts"])


def test_multiple_content_tokens_remain_ambiguous(observed):
    with observed() as db:
        seal_oracle(db, scope=VERIFICATION_SCOPE)
        receipt = db.scalar(select(ZktOracleContentReceipt).order_by(ZktOracleContentReceipt.id))
        db.add(ZktOracleContentReceipt(intent_id=receipt.intent_id, event_uid=receipt.event_uid,
            payload_digest=receipt.payload_digest, request_digest=receipt.request_digest,
            verification_scope=VERIFICATION_SCOPE, content_token="f" * 64, claim_attempt=2))
        db.commit()
        assert "TYPED_ORACLE_RECEIPT_AMBIGUOUS" in collect(db)["reasons"]


def test_bound_is_explicit_and_query_work_is_bounded(observed):
    with observed() as db:
        seal_oracle(db)
        statements = []
        def record(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        sa_event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            report = collect(db, limit=1)
        finally:
            sa_event.remove(db.get_bind(), "before_cursor_execute", record)
        assert len(report["occurrences"]) == 1 and report["truncated"]
        assert "SOURCE_OCCURRENCE_LIMIT" in report["reasons"]
        assert len(statements) < 50


def test_exact_window_excludes_older_custody_even_when_delivery_is_recent(observed):
    with observed() as db:
        seal_oracle(db)
        run = db.scalar(select(FirmwareHilRun))
        for row in db.scalars(select(TerminalRecordManifest)):
            row.created_at = ensure_utc(run.started_at) - timedelta(seconds=1)
        db.commit()
        report = collect(db)
        assert report["occurrences"] == [] and report["reasons"] == ["NO_SOURCE_OCCURRENCES_IN_WINDOW"]


def test_dirty_session_is_never_flushed_into_evidence(observed):
    with observed() as db:
        run = db.scalar(select(FirmwareHilRun))
        run.result = {"uncommitted": True}
        report = collect_writer_attendance(db, run.run_id, now=ensure_utc(run.ends_at))
        assert report["reasons"] == ["UNCOMMITTED_SESSION_STATE"]
        db.rollback()
        assert db.scalar(select(FirmwareHilRun)).result == {}


def test_missing_run_invalid_limit_and_unfinished_interval_are_not_empty_success(observed):
    with observed() as db:
        assert collect_writer_attendance(db, "absent")["reasons"] == ["HIL_RUN_NOT_FOUND"]
        for limit in (0, 513, True, "1"):
            with pytest.raises(ValueError, match="LIMIT_INVALID"):
                collect_writer_attendance(db, "absent", limit=limit)
        run = db.scalar(select(FirmwareHilRun))
        assert collect_writer_attendance(db, run.run_id, now=ensure_utc(run.started_at))["reasons"] == [
            "OBSERVATION_WINDOW_INCOMPLETE_OR_INVALID"]
