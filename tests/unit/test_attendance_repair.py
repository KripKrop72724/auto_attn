from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import attendance_repair as repair
from zk_add.crypto import cnic_lookup, decrypt_text, encrypt_cnic, encrypt_text
from zk_add.db import Base
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityRevision,
    AttendanceRepairCohort,
    AttendanceRepairItem,
    AttendanceRepairJob,
    AttendanceRepairEvent,
    AttendanceRepairOracleSlot,
    AttendanceRepairTarget,
    AuditChainHead,
    AuditEvent,
    DeviceUser,
    OracleIdentityRepairReceipt,
    OrdsOutbox,
    ReconciliationCoverage,
    ReconciliationJob,
    TerminalRecordManifest,
)
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import onboard_connector, replace_user_snapshot
from zk_add.service import block_undelivered_attendance
from zk_add.settings import settings
from zk_add.time_utils import utc_now


SERIAL = "ADZV211860253"
CORRECT_CNIC = "3520212345671"
WRONG_CNIC = "3520299999991"


@pytest.fixture()
def repair_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "repair-test-lookup-key")
    monkeypatch.setattr(settings, "fleet_root_secret", "repair-test-fleet-root")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "attendance_repair_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_repair_execution_enabled", True)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        future=True,
    )
    monkeypatch.setattr("zk_add.db.SessionLocal", test_sessions)

    with test_sessions() as session:
        connector, _token, _created = onboard_connector(
            session,
            hardware_id="e0:72:a1:d6:f3:28",
            zone_id="ZONE-TEST",
            zone_name="Test Zone",
            device_id="TEST-1",
            firmware_version="2.3.0",
            expected_serial=SERIAL,
            actor="test",
            ip_address="127.0.0.1",
        )
        connector.connected = True
        connector.lifecycle_state = "ONLINE"
        zkt = connector.zkt_device
        assert zkt is not None
        zkt.serial = SERIAL
        zkt.online = True
        zkt.connection_state = "ONLINE"
        zkt.snapshot_complete = True
        zkt.identity_snapshot_stable = True
        zkt.attendance_count = 1
        zkt.capability_profile = {
            "history_stream_v1": True,
            "history_range_resume_verified": True,
        }
        replace_user_snapshot(
            session,
            connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id="repair-stable-snapshot",
                complete=True,
                stable=True,
                observed_at=utc_now(),
                users=[
                    UserSnapshotRow(
                        uid="7",
                        user_id="1007",
                        name=f"Correct Name-{CORRECT_CNIC}",
                    )
                ],
            ),
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.zkt_device_id == zkt.id))
        assert user is not None
        event_time = datetime(2026, 8, 20, 3, 15, tzinfo=timezone.utc)
        event = AttendanceEvent(
            event_uid=hashlib.sha256(b"repair-event-1").hexdigest(),
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            device_user_id=user.id,
            identity_snapshot_id=zkt.identity_snapshot_id,
            identity_resolution_status="RESOLVED_CURRENT_SNAPSHOT",
            device_serial=SERIAL,
            uid="7",
            user_id="1007",
            display_name="Wrong Name",
            cnic_encrypted=encrypt_cnic(WRONG_CNIC),
            cnic_lookup_hash=cnic_lookup(WRONG_CNIC),
            cnic_last4=WRONG_CNIC[-4:],
            device_event_time=event_time,
            captured_at=event_time,
            source="FULL_HISTORY",
            status="0",
            punch="0",
            raw_punch=False,
            clock_quality="OK",
            raw_event={"source_record": "preserved"},
            ords_status="ACKED_CHECK",
        )
        session.add(event)
        session.flush()
        baseline = ReconciliationJob(
            connector_id=connector.id,
            zkt_device_id=zkt.id,
            actor="test",
            reason="Certified repair test source history.",
            idempotency_key="repair-baseline-test",
            request_digest="a" * 64,
            status="COMPLETED",
            phase="CERTIFIED",
            terminal_serial=SERIAL,
            terminal_generation=1,
            cutoff_count=1,
            latest_terminal_count=1,
            completed_at=utc_now(),
        )
        session.add(baseline)
        session.flush()
        session.add(
            TerminalRecordManifest(
                job_id=baseline.id,
                connector_id=connector.id,
                zkt_device_id=zkt.id,
                terminal_serial=SERIAL,
                generation=1,
                ordinal=0,
                canonical_source=True,
                raw_record_digest="b" * 64,
                terminal_record_key="c" * 64,
                attendance_event_id=event.id,
                disposition="EVENT",
                observed_uid="7",
                observed_user_id="1007",
            )
        )
        session.add(
            ReconciliationCoverage(
                zkt_device_id=zkt.id,
                job_id=baseline.id,
                terminal_serial=SERIAL,
                terminal_generation=1,
                certified_source_cursor=1,
                source_chain_digest="d" * 64,
                source_committed_cursor=1,
                source_committed_chain_digest="d" * 64,
                capture_state="SOURCE_CAPTURE_CERTIFIED",
                oracle_state="ORACLE_CERTIFIED",
                active=True,
                captured_at=utc_now(),
            )
        )
        session.commit()
        return test_sessions, connector.connector_id, user.user_key, event.event_uid


def _freeze_approved_job(sessions: sessionmaker[Session], connector_id: str, user_key: str) -> str:
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert connector is not None and user is not None
        candidates = repair.build_repair_candidates(
            session,
            connector=connector,
            user_keys=[user_key],
            date_from=None,
            date_to=None,
        )
        assert candidates["source_current"] is True
        current = candidates["targets"][0]["cohorts"][0]
        assert current["evidence_classification"] == "CURRENT_USER_LINEAGE"
        assert current["masked_identity"]["variant_count"] == 1
        encoded_current = json.dumps(current, default=str)
        assert "Wrong Name" not in encoded_current
        assert WRONG_CNIC not in encoded_current
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-prepare-0001",
        )
        repair._freeze_membership(session, job)
        session.flush()
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.oracle_classification = "MISMATCH"
        item.expected_oracle_token_encrypted = encrypt_text("a" * 64)
        item.state = "ORACLE_APPLY"
        session.flush()
        job.preview_digest = repair._sha(repair._preview_material(session, job))
        job.preview_expires_at = utc_now() + timedelta(minutes=15)
        job.status = "AWAITING_APPROVAL"
        job.phase = "PREVIEW_FROZEN"
        serialized = repair.serialize_repair_job(session, job)
        repair.approve_repair_job(
            session,
            job=job,
            actor="operator",
            reason="Correct identity confirmed against the terminal source.",
            typed_confirmation=serialized["typed_confirmation"],
            preview_digest=job.preview_digest,
            idempotency_key="repair-approve-0001",
        )
        session.commit()
        assert decrypt_text(job.reason) == (
            "Correct identity confirmed against the terminal source."
        )
        return job.job_id


def test_repair_activation_preserves_physical_facts_and_verifies_downstream(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        event = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
        )
        assert item is not None and event is not None
        physical_before = {
            "event_uid": event.event_uid,
            "uid": event.uid,
            "user_id": event.user_id,
            "device_event_time": event.device_event_time,
            "punch": event.punch,
            "raw_punch": event.raw_punch,
            "source": event.source,
            "raw_event": event.raw_event,
        }
        item.operation_payload_digest = "e" * 64
        item.state = "ADD_ACTIVATE"
        item.outcome = "UPDATED"
        session.add(
            OracleIdentityRepairReceipt(
                repair_item_id=item.id,
                operation_id=item.operation_id,
                payload_digest=item.operation_payload_digest,
                action="UPDATED",
                oracle_receipt_id=f"oracle-{item.operation_id}",
                current_content_token_encrypted=encrypt_text("oracle-token-after"),
                verified_identity_digest=item.desired_identity_digest,
                raw_content_verified_at=utc_now(),
                downstream_status="PENDING",
            )
        )
        job.status = "RUNNING"
        job.first_oracle_mutation_at = utc_now()
        operation_id = item.operation_id
        identity_digest = item.desired_identity_digest
        session.commit()

    repair._activate_verified_items()

    async def downstream_status(_path: str, *, payload=None):
        assert payload["operation_ids"] == [operation_id]
        return {
            "results": [
                {
                    "operation_id": operation_id,
                    "identity_digest": identity_digest,
                    "raw_content_verified": True,
                    "downstream_verified": True,
                    "stale_old_identity_absent": True,
                }
            ]
        }

    monkeypatch.setattr(repair, "_ords_request", downstream_status)
    asyncio.run(repair._verify_downstream())

    with sessions() as session:
        event = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
        )
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        revision = session.get(AttendanceIdentityRevision, event.effective_identity_revision_id)
        assert event is not None and item is not None and revision is not None
        assert {
            "event_uid": event.event_uid,
            "uid": event.uid,
            "user_id": event.user_id,
            "device_event_time": event.device_event_time,
            "punch": event.punch,
            "raw_punch": event.raw_punch,
            "source": event.source,
            "raw_event": event.raw_event,
        } == physical_before
        assert event.display_name == "Correct Name"
        assert event.cnic_lookup_hash == cnic_lookup(CORRECT_CNIC)
        assert event.identity_resolution_status == "RESOLVED_ATTENDANCE_REPAIR"
        assert event.identity_content_status == "VERIFIED"
        assert event.identity_downstream_confirmed_at is not None
        assert revision.state == "ACTIVE"
        assert item.state == "COMPLETE"
        assert job.status == "COMPLETED"
        evidence = repair.repair_evidence(session, job)
        encoded_evidence = json.dumps(evidence, default=str)
        assert evidence["certificate"]["valid"] is True
        assert CORRECT_CNIC not in encoded_evidence
        assert WRONG_CNIC not in encoded_evidence
        assert "Correct Name" not in encoded_evidence
        assert "Wrong Name" not in encoded_evidence
        streamed_evidence = json.loads(b"".join(repair.stream_repair_evidence(job_id)))
        assert streamed_evidence == json.loads(encoded_evidence)
        assert streamed_evidence["export_digest"] == evidence["export_digest"]
        detail = repair.serialize_repair_job(session, job, include_items=True)
        assert detail["downstream_impact"] == {
            "timezone": "Asia/Karachi",
            "calendar_days": 1,
            "employee_days": 1,
            "before_identity_day_groups": 1,
            "desired_identity_day_groups": 1,
            "first_date": "2026-08-20",
            "last_date": "2026-08-20",
        }
        user = session.get(DeviceUser, revision.effective_device_user_id)
        zkt = session.get(repair.ZKTDevice, event.zkt_device_id)
        assert user is not None and zkt is not None
        event.ords_status = "PENDING"
        session.flush()
        assert block_undelivered_attendance(session, zkt=zkt, user=user) == 0
        assert event.identity_resolution_status == "RESOLVED_ATTENDANCE_REPAIR"
        assert event.cnic_lookup_hash == cnic_lookup(CORRECT_CNIC)
        first_ledger_row = session.scalar(
            select(AttendanceRepairEvent)
            .where(AttendanceRepairEvent.job_id == job.id)
            .order_by(AttendanceRepairEvent.sequence)
            .limit(1)
        )
        assert first_ledger_row is not None
        first_ledger_row.details = {"tampered": True}
        session.flush()
        assert repair.repair_evidence(session, job)["certificate"]["valid"] is False


def test_prepare_idempotency_and_audit_chain_head(repair_store) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        head = session.get(AuditChainHead, 1)
        latest = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
        assert head is not None and latest is not None
        assert head.last_audit_event_id == latest.id
        assert head.last_hash == latest.row_hash
        assert job.preview_digest is not None
        assert job.reason != "Correct identity confirmed against the terminal source."


def test_feature_gates_default_to_fail_closed() -> None:
    # Pydantic defaults remain dark even though the fixture enables the
    # process-global settings object for other tests.
    from zk_add.settings import AddSettings

    defaults = AddSettings(
        _env_file=None,
        pii_fernet_key=None,
        pii_lookup_key=None,
    )
    assert defaults.attendance_repair_preview_enabled is False
    assert defaults.attendance_repair_execution_enabled is False
    assert defaults.attendance_repair_max_employees == 500
    assert defaults.attendance_repair_max_events == 250_000
    assert defaults.attendance_repair_oracle_concurrency == 2


def test_source_certificate_digest_is_stable_but_stale_coverage_is_rejected(
    repair_store,
) -> None:
    sessions, connector_id, _user_key, _event_uid = repair_store
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        assert connector is not None
        current_one, certificate_one, coverage = repair._source_certificate(session, connector)
        current_two, certificate_two, _coverage = repair._source_certificate(session, connector)
        assert current_one is True and current_two is True
        assert certificate_one["certificate_digest"] == certificate_two["certificate_digest"]
        assert coverage is not None
        coverage.captured_at = utc_now() - timedelta(
            seconds=settings.attendance_repair_source_max_age_seconds + 1
        )
        session.flush()
        stale, certificate_stale, _coverage = repair._source_certificate(session, connector)
        assert stale is False
        assert certificate_stale["source_fresh"] is False
        assert certificate_stale["certificate_digest"] != certificate_one["certificate_digest"]


def test_elapsed_source_age_alone_does_not_invalidate_a_frozen_preview(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert connector is not None and user is not None
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-elapsed-source-age",
        )
        repair._freeze_membership(session, job)
        session.flush()
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.oracle_classification = "MISMATCH"
        item.expected_oracle_token_encrypted = encrypt_text("a" * 64)
        item.state = "ORACLE_APPLY"
        session.flush()
        job.preview_digest = repair._sha(repair._preview_material(session, job))
        future = utc_now() + timedelta(
            seconds=settings.attendance_repair_source_max_age_seconds + 60
        )
        monkeypatch.setattr(repair, "utc_now", lambda: future)
        current, certificate, _coverage = repair._source_certificate(session, connector)
        assert current is False
        assert certificate["certificate_digest"] == job.source_certificate_digest
        repair.assert_preview_current(session, job)


def test_pakistan_date_scope_is_utc_half_open() -> None:
    start, end = repair._date_scope(date(2026, 8, 20), date(2026, 8, 21))
    assert start == datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 21, 19, 0, tzinfo=timezone.utc)


def test_approval_replay_rejects_same_key_with_different_body(repair_store) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        connector = session.get(repair.Connector, job.connector_id)
        assert job is not None and connector is not None
        confirmation = repair._expected_confirmation(job, connector)
        replay = repair.approve_repair_job(
            session,
            job=job,
            actor="operator",
            reason="Correct identity confirmed against the terminal source.",
            typed_confirmation=confirmation,
            preview_digest=job.preview_digest,
            idempotency_key="repair-approve-0001",
        )
        assert replay.id == job.id
        with pytest.raises(repair.RepairError, match="different approval request") as error:
            repair.approve_repair_job(
                session,
                job=job,
                actor="operator",
                reason="A different approval reason must not replay silently.",
                typed_confirmation=confirmation,
                preview_digest=job.preview_digest,
                idempotency_key="repair-approve-0001",
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_preview_rejects_effective_event_identity_drift(repair_store) -> None:
    sessions, connector_id, user_key, event_uid = repair_store
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert connector is not None and user is not None
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-preview-drift",
        )
        repair._freeze_membership(session, job)
        session.flush()
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.oracle_classification = "MISMATCH"
        item.expected_oracle_token_encrypted = encrypt_text("a" * 64)
        item.state = "ORACLE_APPLY"
        session.flush()
        job.preview_digest = repair._sha(repair._preview_material(session, job))
        job.preview_expires_at = utc_now() + timedelta(minutes=15)
        job.status = "AWAITING_APPROVAL"
        event = session.scalar(
            select(AttendanceEvent).where(AttendanceEvent.event_uid == event_uid)
        )
        assert event is not None
        event.display_name = "Concurrent identity edit"
        session.flush()
        with pytest.raises(repair.RepairError, match="effective identity changed") as error:
            repair.assert_preview_current(session, job)
        assert error.value.code == "EVENT_IDENTITY_DRIFT"


def test_oracle_payload_digest_covers_identity_and_immutable_facts(repair_store) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        event = session.get(AttendanceEvent, item.attendance_event_id)
        connector = session.get(repair.Connector, job.connector_id)
        assert item is not None and event is not None and connector is not None
        payload = repair._oracle_item_payload(
            event, item, connector=connector, include_operation=True
        )
        digest = payload["payload_digest"]
        assert len(digest) == 64
        payload["desired_identity"]["employee_name"] = "Changed Name"
        assert repair._oracle_operation_digest(payload) != digest
        payload["desired_identity"]["employee_name"] = "Correct Name"
        payload["immutable_facts"]["source_user_id"] = "changed-source-id"
        assert repair._oracle_operation_digest(payload) != digest


def test_oracle_precondition_review_does_not_create_a_receipt(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.operation_payload_digest = "f" * 64
        item.state = "ORACLE_VERIFY"
        job.status = "RUNNING"
        job.first_oracle_mutation_at = utc_now()
        session.add(
            AttendanceRepairOracleSlot(
                id=1,
                lease_owner="test-slot",
                lease_expires_at=utc_now() + timedelta(minutes=1),
                updated_at=utc_now(),
            )
        )
        operation_id = item.operation_id
        item_id = item.id
        session.commit()

    async def precondition_review(_path: str, *, payload=None):
        assert payload is not None
        return {
            "results": [
                {
                    "operation_id": operation_id,
                    "event_uid": payload["items"][0]["event_uid"],
                    "state": "PRECONDITION_FAILED",
                    "error_code": "CONTENT_PRECONDITION_MISMATCH",
                }
            ]
        }

    monkeypatch.setattr(repair, "_ords_request", precondition_review)
    asyncio.run(
        repair._apply_claimed_batch(
            job_id,
            [item_id],
            {"items": [{"event_uid": hashlib.sha256(b"repair-event-1").hexdigest()}]},
            1,
            "test-slot",
        )
    )
    with sessions() as session:
        item = session.get(AttendanceRepairItem, item_id)
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        receipt = session.scalar(
            select(OracleIdentityRepairReceipt).where(
                OracleIdentityRepairReceipt.repair_item_id == item_id
            )
        )
        assert item.state == "NEEDS_REVIEW"
        assert item.error_code == "CONTENT_PRECONDITION_MISMATCH"
        assert receipt is None
        assert job.status == "COMPLETED_WITH_ATTENTION"


def test_malformed_apply_response_recovers_as_unknown_oracle_outcome(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.operation_payload_digest = "f" * 64
        item.state = "ORACLE_VERIFY"
        item.oracle_attempt_count = 1
        job.status = "RUNNING"
        job.first_oracle_mutation_at = utc_now()
        session.add(
            AttendanceRepairOracleSlot(
                id=1,
                lease_owner="test-slot",
                lease_expires_at=utc_now() + timedelta(minutes=1),
                updated_at=utc_now(),
            )
        )
        item_id = item.id
        session.commit()

    async def malformed_success(_path: str, *, payload=None):
        assert payload is not None
        return {"unexpected": "success-envelope"}

    monkeypatch.setattr(repair, "_ords_request", malformed_success)
    asyncio.run(
        repair._apply_claimed_batch(
            job_id,
            [item_id],
            {"items": [{"event_uid": "frozen-event"}]},
            1,
            "test-slot",
        )
    )

    with sessions() as session:
        item = session.get(AttendanceRepairItem, item_id)
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        receipt = session.scalar(
            select(OracleIdentityRepairReceipt).where(
                OracleIdentityRepairReceipt.repair_item_id == item_id
            )
        )
        assert item is not None and job is not None
        assert item.state == "ORACLE_VERIFY"
        assert item.error_code == "ORDS_MALFORMED_RESPONSE"
        assert item.next_attempt_at is not None
        assert receipt is None
        assert job.status == "WAITING_ORACLE"


def test_execution_gate_still_forward_completes_known_oracle_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def source():
        calls.append("source")

    async def recover():
        calls.append("recover")

    def activate():
        calls.append("activate")

    async def downstream():
        calls.append("downstream")

    monkeypatch.setattr(settings, "attendance_repair_execution_enabled", False)
    monkeypatch.setattr(repair, "_advance_source_preparation", source)
    monkeypatch.setattr(repair, "_recover_oracle_operations", recover)
    monkeypatch.setattr(repair, "_activate_verified_items", activate)
    monkeypatch.setattr(repair, "_verify_downstream", downstream)
    monkeypatch.setattr(
        repair,
        "_claim_apply_batch",
        lambda: pytest.fail("execution gate admitted a new Oracle mutation"),
    )
    asyncio.run(repair.advance_attendance_repairs_once())
    assert calls == ["recover", "activate", "downstream", "source"]


def test_live_backlog_pauses_a_queued_job_before_oracle_mutation(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    monkeypatch.setattr(settings, "reconciliation_history_backlog_pause", 1)
    with sessions() as session:
        session.add(OrdsOutbox(status="PENDING", delivery_type="LIVE"))
        session.commit()

    assert repair._claim_apply_batch() is None

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert job.first_oracle_mutation_at is None
        assert item.state == "ORACLE_APPLY"


def test_repair_feature_configuration_requires_preview_and_add_credentials() -> None:
    from zk_add.settings import AddSettings

    base = {
        "admin_password_hash": "configured",
        "pii_fernet_key": Fernet.generate_key().decode(),
        "pii_lookup_key": "configured",
        # CI intentionally exports disposable ORDS credentials for integration
        # tests.  Override them here so this unit test proves the fail-closed
        # configuration contract independently of the runner environment.
        "ords_base_url": None,
        "ords_username": None,
        "ords_password": None,
    }
    with pytest.raises(RuntimeError, match="requires ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"):
        AddSettings(
            _env_file=None,
            **base,
            attendance_repair_preview_enabled=False,
            attendance_repair_execution_enabled=True,
        ).require_production_secrets()
    with pytest.raises(RuntimeError, match="ADD_ORDS_BASE_URL"):
        AddSettings(
            _env_file=None,
            **base,
            attendance_repair_preview_enabled=True,
            attendance_repair_execution_enabled=False,
        ).require_production_secrets()
    with pytest.raises(RuntimeError, match="must be at least twice"):
        AddSettings(
            _env_file=None,
            **base,
            attendance_repair_preview_enabled=True,
            attendance_repair_execution_enabled=False,
            attendance_repair_lease_seconds=30,
            ords_timeout_seconds=20,
        ).require_production_secrets()


def test_retryable_preview_failure_uses_durable_backoff(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert connector is not None and user is not None
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-preview-backoff",
        )
        repair._freeze_membership(session, job)
        session.flush()
        job_id = job.job_id
        session.commit()

    async def unavailable():
        raise repair.OracleRepairError(
            "Oracle is temporarily unavailable.",
            code="ORDS_HTTP_503",
            retryable=True,
            status_code=503,
        )

    monkeypatch.setattr(repair, "oracle_repair_capabilities", unavailable)
    asyncio.run(repair.classify_repair_preview(job_id))

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        assert job is not None
        assert job.status == "PREPARING_SOURCE"
        assert job.phase == "ORACLE_CLASSIFICATION"
        assert job.error_code == "ORDS_HTTP_503"
        assert job.preparation_attempt_count == 1
        assert job.next_attempt_at is not None
        assert repair.ensure_utc(job.next_attempt_at) > utc_now()

    calls: list[str] = []

    async def should_not_run(_job_id: str, *, max_batches: int = 5):
        calls.append(_job_id)

    monkeypatch.setattr(repair, "classify_repair_preview", should_not_run)
    asyncio.run(repair._advance_source_preparation())
    assert calls == []


def test_prepare_defers_membership_freeze_to_the_durable_worker(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    with sessions() as session:
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert connector is not None and user is not None
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-deferred-freeze",
        )
        job_id = job.job_id
        session.flush()
        assert job.phase == "MEMBERSHIP_FREEZE"
        assert (
            session.scalar(
                select(repair.func.count(AttendanceRepairItem.id)).where(
                    AttendanceRepairItem.job_id == job.id
                )
            )
            == 0
        )
        session.commit()

    classified: list[str] = []

    async def record_classification(public_job_id: str, *, max_batches: int = 5):
        classified.append(public_job_id)

    monkeypatch.setattr(repair, "classify_repair_preview", record_classification)
    asyncio.run(repair._advance_source_preparation())

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        assert job is not None
        assert job.phase == "ORACLE_CLASSIFICATION"
        assert (
            session.scalar(
                select(repair.func.count(AttendanceRepairItem.id)).where(
                    AttendanceRepairItem.job_id == job.id
                )
            )
            == 1
        )
    assert classified == [job_id]


def test_oracle_receipt_replay_must_match_original_durable_proof(
    repair_store,
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert item is not None
        item.operation_payload_digest = "f" * 64
        session.add(
            OracleIdentityRepairReceipt(
                repair_item_id=item.id,
                operation_id=item.operation_id,
                payload_digest=item.operation_payload_digest,
                action="UPDATED",
                oracle_receipt_id=f"oracle-{item.operation_id}",
                current_content_token_encrypted=encrypt_text("a" * 64),
                verified_identity_digest=item.desired_identity_digest,
                raw_content_verified_at=utc_now(),
                downstream_status="PENDING",
            )
        )
        session.flush()
        with pytest.raises(repair.OracleRepairError, match="conflicts") as error:
            repair._persist_oracle_receipt(
                session,
                item=item,
                result={
                    "operation_id": item.operation_id,
                    "event_uid": item.event_uid,
                    "identity_digest": item.desired_identity_digest,
                    "raw_content_verified": True,
                    "immutable_facts_unchanged": True,
                    "event_count_preserved": True,
                    "event_uid_unique": True,
                    "receipt_id": f"oracle-{item.operation_id}",
                    "current_content_token": "b" * 64,
                    "action": "UPDATED",
                },
            )
        assert error.value.code == "ORDS_RECEIPT_CONFLICT"


def test_retry_rejects_when_a_newer_terminal_job_is_active(repair_store) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == job.id)
        )
        assert job is not None and item is not None
        item.state = "NEEDS_REVIEW"
        item.error_code = "RETRY_EXHAUSTED"
        item.completed_at = utc_now()
        job.status = "COMPLETED_WITH_ATTENTION"
        job.phase = "CERTIFIED"
        job.first_oracle_mutation_at = utc_now()
        job.completed_at = utc_now()
        session.add(
            AttendanceRepairJob(
                connector_id=job.connector_id,
                zkt_device_id=job.zkt_device_id,
                actor="another-operator",
                status="PREPARING_SOURCE",
                phase="SOURCE_PREFLIGHT",
                idempotency_key="newer-active-repair",
                request_digest="9" * 64,
            )
        )
        session.commit()

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        with pytest.raises(repair.RepairError, match="Another employee repair") as error:
            repair.control_repair_job(
                session,
                job=job,
                action="retry",
                actor="operator",
                reason="Retry after correcting the external Oracle condition.",
                idempotency_key="retry-conflicting-active-job",
            )
        assert error.value.code == "ACTIVE_REPAIR_EXISTS"


def test_older_partial_repair_cannot_overwrite_a_newer_event_repair(repair_store) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    old_job_id = _freeze_approved_job(sessions, connector_id, user_key)
    with sessions() as session:
        old_job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == old_job_id)
        )
        old_item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == old_job.id)
        )
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert old_job is not None and old_item is not None
        assert connector is not None and user is not None
        old_item.state = "NEEDS_REVIEW"
        old_item.error_code = "RETRY_EXHAUSTED"
        old_item.completed_at = utc_now()
        old_job.status = "COMPLETED_WITH_ATTENTION"
        old_job.phase = "CERTIFIED"
        old_job.first_oracle_mutation_at = utc_now()
        old_job.completed_at = utc_now()
        session.flush()

        newer_job = repair.create_repair_job(
            session,
            connector=connector,
            actor="newer-operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="newer-overlapping-repair",
        )
        repair._freeze_membership(session, newer_job)
        session.flush()
        newer_item = session.scalar(
            select(AttendanceRepairItem).where(AttendanceRepairItem.job_id == newer_job.id)
        )
        assert newer_item is not None
        newer_item.state = "COMPLETE"
        newer_item.outcome = "NOOP_DOWNSTREAM_VERIFIED"
        newer_item.completed_at = utc_now()
        newer_job.status = "COMPLETED"
        newer_job.phase = "CERTIFIED"
        newer_job.completed_at = utc_now()
        session.commit()

    with sessions() as session:
        old_job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == old_job_id)
        )
        with pytest.raises(repair.RepairError, match="newer repair") as error:
            repair.control_repair_job(
                session,
                job=old_job,
                action="retry",
                actor="operator",
                reason="Retry after correcting the external Oracle condition.",
                idempotency_key="retry-superseded-old-job",
            )
        assert error.value.code == "REPAIR_SUPERSEDED"


def test_source_dependency_freeze_rolls_back_partial_membership(
    repair_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, connector_id, user_key, _event_uid = repair_store
    with sessions() as session:
        coverage = session.scalar(
            select(ReconciliationCoverage).where(ReconciliationCoverage.active)
        )
        connector = session.scalar(
            select(repair.Connector).where(repair.Connector.connector_id == connector_id)
        )
        user = session.scalar(select(DeviceUser).where(DeviceUser.user_key == user_key))
        assert coverage is not None and connector is not None and user is not None
        coverage.captured_at = utc_now() - timedelta(
            seconds=settings.attendance_repair_source_max_age_seconds + 1
        )
        session.flush()
        job = repair.create_repair_job(
            session,
            connector=connector,
            actor="operator",
            selections=[
                {
                    "user_key": user_key,
                    "expected_row_version": user.row_version,
                    "all_provable_history": True,
                    "cohort_tokens": [],
                }
            ],
            date_from=None,
            date_to=None,
            idempotency_key="repair-source-savepoint",
        )
        assert job.source_reconciliation_job_id is not None
        coverage.captured_at = utc_now()
        job_id = job.job_id
        session.commit()

    def partial_freeze(session: Session, job: AttendanceRepairJob) -> None:
        target = session.scalar(
            select(AttendanceRepairTarget).where(AttendanceRepairTarget.job_id == job.id)
        )
        assert target is not None
        session.add(
            AttendanceRepairCohort(
                target_id=target.id,
                cohort_token="f" * 64,
                evidence_classification="CURRENT_USER_LINEAGE",
                source_device_user_id=target.device_user_id,
                source_uid_digest="1" * 64,
                source_user_id_digest="2" * 64,
                membership_digest="3" * 64,
                first_event_at=utc_now(),
                last_event_at=utc_now(),
                event_count=1,
                selected=True,
            )
        )
        session.flush()
        raise repair.RepairError("Cohort changed during freeze.", code="COHORT_DRIFT")

    monkeypatch.setattr(repair, "_freeze_membership", partial_freeze)
    asyncio.run(repair._advance_source_preparation())

    with sessions() as session:
        job = session.scalar(
            select(AttendanceRepairJob).where(AttendanceRepairJob.job_id == job_id)
        )
        cohort_count = session.scalar(select(repair.func.count(AttendanceRepairCohort.id)))
        assert job is not None
        assert job.status == "NEEDS_ATTENTION"
        assert job.error_code == "COHORT_DRIFT"
        assert cohort_count == 0
