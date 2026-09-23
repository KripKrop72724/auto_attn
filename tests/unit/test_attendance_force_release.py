from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from test_attendance_repair import repair_store as repair_store, CORRECT_CNIC
from zk_add import attendance_force_release as force
from zk_add import attendance_force_delivery as delivery
from zk_add.attendance_force_schemas import ForceCheckRequest
from zk_add.attendance_recovery import RecoveryError
from zk_add.crypto import encrypt_cnic, cnic_lookup, encrypt_text
from zk_add.models import (
    AttendanceEvent,
    AttendanceRecoveryJob as Job,
    AttendanceForceReleaseTask as Task,
    AttendanceForceReleaseDecision as Decision,
    AttendanceRecoveryItem as Item,
    Connector,
    DeviceCommand,
    DeviceUser,
    OrdsOutbox,
)
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import replace_user_snapshot
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.worker import claim_ords_batch, apply_ords_confirmation, apply_ords_delivery_result


@pytest.fixture()
def store(repair_store, monkeypatch):
    sessions, connector_id, user_key, event_uid = repair_store
    monkeypatch.setattr(settings, "attendance_force_release_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_force_release_execution_enabled", True)
    monkeypatch.setattr(settings, "attendance_force_release_allowed_connectors", "")
    with sessions() as db:
        connector = db.scalar(select(Connector).where(Connector.connector_id == connector_id))
        connector.boot_id = "force-test-boot"
        connector.zkt_device.confirmed_serial = connector.zkt_device.serial
        event = db.scalar(select(AttendanceEvent))
        event.ords_status = "BLOCKED_IDENTITY"
        event.identity_resolution_status = "BLOCKED_PROVENANCE"
        event.cnic_encrypted = event.cnic_lookup_hash = None
        event.oracle_confirmed_at = None
        event.raw_event = {
            **event.raw_event,
            "trusted_capture_terminal_serial": connector.zkt_device.serial,
        }
        db.commit()
    return sessions, connector_id, event_uid


def tick(sessions, *, rollback=False):
    with sessions() as db:
        force.advance_once(db)
        db.flush()
        if rollback:
            db.rollback()
        else:
            db.commit()


def sync(sessions, *, complete=True, success=True, fresh=True):
    with sessions() as db:
        task = db.scalar(select(Task).where(Task.status == "SYNCING"))
        assert task
        connector = db.get(Connector, task.connector_id)
        command = db.get(DeviceCommand, task.sync_command_id)
        command.started_at = utc_now() - timedelta(seconds=1)
        command.status = "SUCCEEDED" if success else "FAILED"
        command.completed_at = utc_now()
        if fresh:
            replace_user_snapshot(
                db,
                connector=connector,
                snapshot=UserSnapshotRequest(
                    snapshot_id=str(uuid4()),
                    complete=complete,
                    stable=complete,
                    observed_at=utc_now(),
                    users=[
                        UserSnapshotRow(
                            uid="7", user_id="1007", name=f"Correct Name-{CORRECT_CNIC}"
                        )
                    ],
                ),
            )
        db.commit()


def checked(store, *, key="force-check-one"):
    sessions, connector_id, _uid = store
    with sessions() as db:
        job = force.create_check(
            db,
            actor="operator",
            request=ForceCheckRequest(
                scope="SELECTED", connector_ids=[connector_id], idempotency_key=key
            ),
        )
        job_id = job.job_id
        db.commit()
    tick(sessions)  # baseline
    tick(sessions)  # enqueue refresh
    sync(sessions)
    tick(sessions)  # accept refresh
    tick(sessions)  # classify
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        assert job.status == "CHECKED"
    return job_id


def approve(store, job_id):
    sessions, _, _ = store
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        signature = force.serialize(db, job, "operator")["signature"]
        force.start(
            db,
            job,
            actor="operator",
            signature=signature,
            reason="Reviewed current employee",
            key="force-approval-one",
        )
        db.commit()
    tick(sessions)
    sync(sessions)
    tick(sessions)


def test_rollback_overlay_preserves_approved_outbox_without_legacy_delivery(store, monkeypatch):
    import asyncio
    from pathlib import Path
    import yaml
    from zk_add import worker

    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    tick(sessions)
    overlay = yaml.safe_load(
        (Path(__file__).parents[2] / "deploy/add/docker-compose.rollback.yml").read_text()
    )["services"]["add-api"]["environment"]
    assert overlay["ADD_ORDS_BASE_URL"] == ""
    assert overlay["ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] == "false"
    with monkeypatch.context() as paused:
        paused.setattr(settings, "ords_base_url", overlay["ADD_ORDS_BASE_URL"])

        def unexpected_claim(*args, **kwargs):
            raise AssertionError("A rollback image must not claim approved attendance")

        paused.setattr(worker, "claim_ords_batch", unexpected_claim)
        asyncio.run(worker.deliver_ords_batch())
    with sessions() as db:
        outbox = db.scalar(select(OrdsOutbox))
        assert outbox.status == "PENDING" and outbox.attempt_count == 0
        decision = db.scalar(select(Decision))
        saved_digest = decision.payload_digest
        assert saved_digest and db.scalar(select(Item)).status == "WAITING_ORACLE"
    # Restoring the qualified application's configuration resumes the original
    # approval. No identity is reconstructed by the legacy rollback worker.
    ordinary, forced = delivery.split_claims(claim_ords_batch(1))
    assert not ordinary and len(forced) == 1
    delivery.persist_result(forced[0], "MATCH", "c" * 64)
    with sessions() as db:
        assert db.scalar(select(Decision)).payload_digest == saved_digest
        assert db.scalar(select(AttendanceEvent)).ords_status == "ACKED_CHECK"


def test_no_history_manual_approval_content_ack_and_frozen_payload(store):
    sessions, _, _ = store
    job_id = checked(store)
    with sessions() as db:
        assert db.scalar(select(Item)).status == "READY"
        assert db.scalar(select(AttendanceEvent)).ords_status == "BLOCKED_IDENTITY"
        assert db.scalar(select(func.count(Decision.id))) == 0
    approve(store, job_id)
    tick(sessions, rollback=True)
    with sessions() as db:
        assert db.scalar(select(func.count(Decision.id))) == 0
        assert db.scalar(select(func.count(OrdsOutbox.id))) == 0
    tick(sessions)
    claims = claim_ords_batch(1)
    assert len(claims) == 1
    ordinary, forced = delivery.split_claims(claims)
    assert not ordinary and len(forced) == 1
    with sessions() as db:
        apply_ords_confirmation(db, claimed_id=claims[0][0], path="ORDS_MEMBERSHIP_CHECK")
        apply_ords_delivery_result(
            db,
            claimed_id=claims[0][0],
            status=200,
            body={"success": True},
            transport_error=None,
            response_parsed=True,
        )
        db.commit()
        assert (
            db.scalar(select(AttendanceEvent)).ords_status == "IN_FLIGHT"
            or db.scalar(select(AttendanceEvent)).ords_status == "PENDING"
        )
        assert db.scalar(select(AttendanceEvent)).oracle_confirmed_at is None
    delivery.persist_result(forced[0], "MATCH", "c" * 64)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Job)).status == "COMPLETED"
        event = db.scalar(select(AttendanceEvent))
        assert event.ords_status == "ACKED_CHECK"
        assert event.oracle_confirmed_at
        assert force.metadata(db, event)["administrator"] == "operator"
        assert db.scalar(select(func.count(Decision.id))) == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "CNIC_MISSING"),
        ("different", "CNIC_CONFLICT"),
        ("captured", "CNIC_CONFLICT"),
        ("binding", "TERMINAL_CHANGED"),
        ("uid", "UID_INVALID"),
        ("event", "EVENT_ID_INVALID"),
        ("timestamp", "TIME_INVALID"),
        ("conflict", "IDENTITY_CONFLICT"),
        ("unknown", "USER_MISSING"),
        ("damaged", "SOURCE_INVALID"),
    ],
)
def test_exclusions(store, change, code):
    sessions, _, _ = store
    checked(store)
    with sessions() as db:
        event, user, task = (
            db.scalar(select(AttendanceEvent)),
            db.scalar(select(DeviceUser)),
            db.scalar(select(Task)),
        )
        if change == "missing":
            user.cnic_encrypted = None
        if change == "different":
            event.cnic_encrypted = encrypt_cnic("3520299999991")
        if change == "captured":
            event.captured_cnic_lookup_hash = cnic_lookup("3520299999991")
        if change == "binding":
            db.get(
                Connector, task.connector_id
            ).zkt_device.terminal_binding_state = "SERIAL_CONFIRMATION_REQUIRED"
        if change == "uid":
            event.uid = "oops"
        if change == "event":
            event.event_uid = "bad"
        if change == "timestamp":
            event.clock_quality = "INVALID"
        if change == "conflict":
            user.identity_conflict_code = "DUPLICATE_CNIC"
        if change == "unknown":
            user.present = False
        if change == "damaged":
            event.punch = "999"
        db.flush()
        assert force.classify(db, event, db.get(Connector, task.connector_id), task)[1] == code


@pytest.mark.parametrize("fresh,success", [(False, True), (True, False)])
def test_command_and_new_snapshot_both_required(store, fresh, success):
    sessions, connector_id, _ = store
    with sessions() as db:
        force.create_check(
            db,
            actor="operator",
            request=ForceCheckRequest(
                scope="SELECTED", connector_ids=[connector_id], idempotency_key="freshness-test"
            ),
        )
        db.commit()
    tick(sessions)
    tick(sessions)
    sync(sessions, fresh=fresh, success=success)
    tick(sessions)
    with sessions() as db:
        assert not db.scalar(select(Item.id).where(Item.status == "READY"))
        assert db.scalar(select(Task)).status in {"SYNCING", "UNAVAILABLE"}


def test_changed_pre_sync_cnic_cannot_disappear(store):
    sessions, _, _ = store
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.cnic_encrypted = encrypt_cnic("3520299999991")
        user.cnic_lookup_hash = cnic_lookup("3520299999991")
        user.machine_name_encrypted = encrypt_text("Correct Name-3520299999991")
        db.commit()
    checked(store)
    with sessions() as db:
        assert db.scalar(select(Item)).status == "NEEDS_REVIEW"


def test_expired_approval_and_explicit_scope(store):
    sessions, _, _ = store
    with pytest.raises(ValueError):
        ForceCheckRequest(scope="SELECTED", connector_ids=[], idempotency_key="empty-scope")
    job_id = checked(store)
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        signature = force.serialize(db, job, "operator")["signature"]
        job.preview_expires_at = utc_now() - timedelta(seconds=1)
        db.flush()
        with pytest.raises(RecoveryError):
            force.start(
                db,
                job,
                actor="operator",
                signature=signature,
                reason="Reviewed",
                key="expired-approval",
            )


def test_sync_and_maintenance_never_release_held_event(store):
    sessions, connector_id, _ = store
    from zk_add.service import repair_attendance_delivery_backlog, enrich_undelivered_attendance

    with sessions() as db:
        connector, event, user = (
            db.scalar(select(Connector)),
            db.scalar(select(AttendanceEvent)),
            db.scalar(select(DeviceUser)),
        )
        enrich_undelivered_attendance(db, zkt=connector.zkt_device, user=user)
        repair_attendance_delivery_backlog(db)
        db.commit()
        assert event.ords_status == "BLOCKED_IDENTITY"
        assert event.manual_release_required
    assert claim_ords_batch(10) == []


@pytest.mark.parametrize("change", ["boot", "old_observation", "partial", "serial", "spare"])
def test_sync_rejects_reconnect_delayed_partial_or_wrong_device(store, change):
    from zk_add.models import DeviceUserSnapshot

    sessions, connector_id, _ = store
    with sessions() as db:
        force.create_check(
            db,
            actor="operator",
            request=ForceCheckRequest(
                scope="SELECTED", connector_ids=[connector_id], idempotency_key="bad-sync-evidence"
            ),
        )
        db.commit()
    tick(sessions)
    tick(sessions)
    sync(sessions)
    with sessions() as db:
        c = db.scalar(select(Connector))
        snap = db.get(DeviceUserSnapshot, c.zkt_device.identity_snapshot_id)
        if change == "boot":
            c.boot_id = "different-boot"
        elif change == "old_observation":
            snap.observed_at -= timedelta(minutes=15)
        elif change == "partial":
            snap.complete = False
        elif change == "serial":
            c.zkt_device.serial = "REPLACED"
        else:
            c.is_spare = True
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).status in {"UNAVAILABLE", "SYNCING"}
        assert db.scalar(select(func.count(Item.id))) == 0


def test_changed_employee_after_approval_is_skipped(store):
    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.identity_conflict_code = "REUSED_ID"
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Item)).status == "SKIPPED"
        assert db.scalar(select(func.count(Decision.id))) == 0
        assert db.scalar(select(AttendanceEvent)).ords_status == "BLOCKED_IDENTITY"


def test_pause_resume_stop_are_durable_idempotent_and_do_not_delete(store):
    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    with sessions() as db:
        job = db.scalar(select(Job))
        force.control(db, job, actor="operator", action="PAUSE", key="pause-request")
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Item)).status == "READY"
        job = db.scalar(select(Job))
        force.control(db, job, actor="operator", action="PAUSE", key="pause-request")
        force.control(db, job, actor="operator", action="RESUME", key="resume-request")
        db.commit()
        assert db.scalar(select(Task)).status == "SYNC_PENDING"
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).sync_round == 3
        force.control(
            db, db.scalar(select(Job)), actor="operator", action="STOP", key="stop-request"
        )
        db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Job)).status == "STOPPED"
        assert db.scalar(select(Item)).status == "STOPPED"
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 1
        assert db.scalar(select(func.count(Decision.id))) == 0


def test_later_punches_and_overlap_are_not_added_to_approval(store):
    import hashlib
    from copy import deepcopy

    sessions, _, _ = store
    job_id = checked(store)
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent))
        data = {
            c.name: deepcopy(getattr(original, c.name))
            for c in AttendanceEvent.__table__.columns
            if c.name != "id"
        }
        data["event_uid"] = hashlib.sha256(b"after-manual-check").hexdigest()
        db.add(AttendanceEvent(**data))
        db.commit()
    other = checked(store, key="second-admin-check")
    approve(store, job_id)
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == other))
        sig = force.serialize(db, job, "operator")["signature"]
        with pytest.raises(RecoveryError, match="Another release"):
            force.start(
                db, job, actor="operator", signature=sig, reason="Reviewed", key="overlap-approval"
            )
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(func.count(Decision.id))) == 1


@pytest.mark.parametrize(
    "classification",
    ["UNKNOWN", "MISSING", "MISMATCH", "IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION"],
)
def test_uncertain_and_conflicting_oracle_results_never_ack(store, classification):
    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    tick(sessions)
    _, forced = delivery.split_claims(claim_ords_batch(1))
    delivery.persist_result(forced[0], classification, "f" * 64)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        assert event.oracle_confirmed_at is None
        assert event.ords_status in {"FAILED_RETRYABLE", "QUARANTINED_IDENTITY_CONFLICT"}
        assert force.metadata(db, event) is not None


def test_force_api_requires_csrf_password_reason_and_reopens_idempotent_check(store, monkeypatch):
    from fastapi.testclient import TestClient
    from zk_add import web
    from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password

    sessions, connector_id, _ = store
    monkeypatch.setattr(web, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "admin_username", "operator")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("test-force-password"))
    client = TestClient(web.app)
    root = "/api/v2/attendance-force-releases"
    assert client.get(root).status_code == 401
    with sessions() as db:
        cookie, admin = create_admin_session(
            db, username="operator", ip_address=None, user_agent=None
        )
        csrf = admin.csrf_token
        db.commit()
    client.cookies.set(ADMIN_COOKIE, cookie)
    body = {
        "scope": "SELECTED",
        "connector_ids": [connector_id],
        "idempotency_key": "api-force-check",
    }
    assert client.post(root, json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    response = client.post(root, json=body)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert client.post(root, json=body).json()["job_id"] == job_id
    tick(sessions)
    tick(sessions)
    sync(sessions)
    tick(sessions)
    tick(sessions)
    run = client.get(f"{root}/{job_id}").json()
    body = {
        "signature": run["signature"],
        "reason": "Reviewed current employee",
        "password": "wrong",
        "idempotency_key": "api-force-approve",
    }
    assert client.post(f"{root}/{job_id}/start", json=body).status_code == 403
    body["password"] = "test-force-password"
    response = client.post(f"{root}/{job_id}/start", json=body)
    assert response.status_code == 202, response.text
    assert "test-force-password" not in response.text
    assert response.json()["status"] == "RUNNING"
    assert client.post(f"{root}/{job_id}/start", json=body).json()["job_id"] == job_id
    assert (
        client.post(
            f"/api/v2/attendance-recovery/jobs/{job_id}/control",
            json={
                "workflow": "SAFE_REPAIR",
                "action": "STOP",
                "password": "test-force-password",
            },
        ).status_code
        == 409
    )


def test_oracle_authentication_fault_remains_pending_and_visible(store):
    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    tick(sessions)
    _, claims = delivery.split_claims(claim_ords_batch(1))
    delivery.persist_result(claims[0], "UNKNOWN", error_code="ORDS_AUTHENTICATION_FAILED")
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        job = db.scalar(select(Job))
        assert event.oracle_confirmed_at is None
        assert force.metadata(db, event)["needs_attention"] is True
        assert force.counts(db, job)["attention"] == 1
        assert force.counts(db, job)["waiting"] == 1
        assert "credentials" in force.items_page(db, job)["rows"][0]["reason"]


def test_legitimate_uidless_format_and_harmless_historical_name_truncation(store):
    from zk_add.models import TerminalRecordManifest

    sessions, _, _ = store
    checked(store)
    with sessions() as db:
        event, connector, task = (
            db.scalar(select(AttendanceEvent)),
            db.scalar(select(Connector)),
            db.scalar(select(Task)),
        )
        event.uid = None
        event.display_name = "Correct Na"
        manifest = db.scalar(select(TerminalRecordManifest))
        manifest.record_size = 16
        manifest.observed_user_id = event.user_id
        db.flush()
        assert force.identity_proof(db, event, connector, task)[1] is None
        event.uid = "broken"
        assert force.identity_proof(db, event, connector, task)[1] == "UID_INVALID"


@pytest.mark.parametrize("namespace", ["current", "other", "empty"])
def test_historical_identity_conflicts_belong_to_their_terminal(store, namespace):
    from zk_add.models import AttendanceIdentityHistory
    from test_attendance_repair import WRONG_CNIC

    sessions, _, _ = store
    checked(store)
    with sessions() as db:
        event, connector, task, user = (
            db.scalar(select(AttendanceEvent)), db.scalar(select(Connector)),
            db.scalar(select(Task)), db.scalar(select(DeviceUser)),
        )
        serial = {"current": connector.zkt_device.serial, "other": "REPLACED-TERMINAL",
                  "empty": ""}[namespace]
        db.add(AttendanceIdentityHistory(
            zkt_device_id=connector.zkt_device.id, device_user_id=user.id,
            terminal_serial=serial, user_id=user.user_id, uid=user.uid,
            cnic_encrypted=encrypt_cnic(WRONG_CNIC), cnic_lookup_hash=cnic_lookup(WRONG_CNIC),
            first_snapshot_id=task.snapshot_id, last_snapshot_id=task.snapshot_id,
            last_revision=1, observed_from=utc_now(), observed_until=utc_now(),
        ))
        db.flush()
        assert force.identity_proof(db, event, connector, task)[1] == (
            None if namespace == "other" else "IDENTITY_CONFLICT"
        )
        if namespace == "other":
            event.device_serial = serial
            assert force.identity_proof(db, event, connector, task)[1] == "TERMINAL_CHANGED"


def test_empty_uid_is_not_evidence_linking_unrelated_legacy_users(store):
    from zk_add.models import IdentityTombstone, AttendanceForceReleaseUser as Baseline

    sessions, _, _ = store
    checked(store)
    with sessions() as db:
        event, connector, task, user = (
            db.scalar(select(AttendanceEvent)), db.scalar(select(Connector)),
            db.scalar(select(Task)), db.scalar(select(DeviceUser)),
        )
        event.uid, event.source, user.uid = None, "LIVE", ""
        db.scalar(select(Baseline)).uid = ""
        old = DeviceUser(zkt_device_id=connector.zkt_device.id, uid="", user_id="other",
                         display_name="Unrelated retired user", lifecycle_state="DELETED")
        db.add(old)
        db.flush()
        tombstone = IdentityTombstone(
            zkt_device_id=connector.zkt_device.id, device_user_id=old.id,
            device_serial=connector.zkt_device.serial, user_id=old.user_id, uid="",
            display_name_encrypted=encrypt_text("Unrelated retired user"),
        )
        db.add(tombstone)
        db.add(Baseline(task_id=task.id, device_user_id=old.id, user_id=old.user_id, uid=""))
        db.flush()
        assert force.identity_proof(db, event, connector, task)[1] is None
        tombstone.user_id = user.user_id
        db.flush()
        assert force.identity_proof(db, event, connector, task)[1] == "IDENTITY_CONFLICT"


@pytest.mark.parametrize("namespace", ["current", "other", "unknown", "empty"])
def test_deleted_identity_evidence_preserves_terminal_namespace(store, namespace):
    from zk_add.models import IdentityTombstone
    from test_attendance_repair import WRONG_CNIC

    sessions, _, _ = store
    checked(store)
    with sessions() as db:
        event, connector, task, user = (
            db.scalar(select(AttendanceEvent)), db.scalar(select(Connector)),
            db.scalar(select(Task)), db.scalar(select(DeviceUser)),
        )
        serial = {"current": connector.zkt_device.serial, "other": "REPLACED-TERMINAL",
                  "unknown": None, "empty": ""}[namespace]
        db.add(IdentityTombstone(
            zkt_device_id=connector.zkt_device.id, device_user_id=user.id,
            device_serial=serial, user_id=user.user_id, uid=user.uid,
            display_name_encrypted=encrypt_text("Synthetic previous employee"),
            cnic_encrypted=encrypt_cnic(WRONG_CNIC), cnic_lookup_hash=cnic_lookup(WRONG_CNIC),
        ))
        db.flush()
        assert force.identity_proof(db, event, connector, task)[1] == (
            None if namespace == "other" else "IDENTITY_CONFLICT"
        )


@pytest.mark.parametrize(
    "first,last,post_kind,expected",
    [
        ("MATCH", "MATCH", "unused", "ACKED_CHECK"),
        ("MISSING", "MATCH", "lost_reply", "ACKED_CHECK"),
        ("MISSING", "MISSING", "duplicate", "FAILED_RETRYABLE"),
        ("MISSING", "MISSING", "http_success", "FAILED_RETRYABLE"),
        ("MISMATCH", "MISMATCH", "unused", "QUARANTINED_IDENTITY_CONFLICT"),
    ],
)
def test_force_transport_requires_matching_content_even_after_success_or_duplicate(
    store, monkeypatch, first, last, post_kind, expected
):
    import asyncio
    import httpx

    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    tick(sessions)
    _, forced = delivery.split_claims(claim_ords_batch(1))
    answers, checks, posts = iter([first, last]), [], []

    async def content_check(path, *, payload):
        assert path == "raw-captures/identity-repairs/check"
        checks.append(payload)
        return {
            "success": True,
            "results": [
                {
                    "event_uid": forced[0]["payload"]["event_uid"],
                    "classification": next(answers),
                    "current_content_token": "c" * 64,
                }
            ],
        }

    def transport(request):
        posts.append(request)
        if post_kind == "lost_reply":
            raise httpx.ReadTimeout("simulated after commit", request=request)
        return httpx.Response(409 if post_kind == "duplicate" else 200, json={"success": True})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(delivery, "_ords_request", content_check)
    monkeypatch.setattr(
        delivery.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    monkeypatch.setattr(settings, "ords_base_url", "https://ords.invalid")
    monkeypatch.setattr(settings, "ords_username", "test")
    monkeypatch.setattr(settings, "ords_password", "test")
    asyncio.run(delivery.deliver_forced(forced, concurrency=1))
    assert len(posts) == (1 if first == "MISSING" else 0)
    assert len(checks) == (2 if first == "MISSING" else 1)
    with sessions() as db:
        row = db.scalar(select(AttendanceEvent))
        assert row.ords_status == expected
        assert bool(row.oracle_confirmed_at) == (expected == "ACKED_CHECK")


def test_generic_membership_audit_cannot_reclassify_a_forced_receipt(store):
    from zk_add.worker import claim_confirmed_membership_audit_batch

    sessions, _, _ = store
    job_id = checked(store)
    approve(store, job_id)
    tick(sessions)
    _, forced = delivery.split_claims(claim_ords_batch(1))
    delivery.persist_result(forced[0], "MATCH", "e" * 64)
    with sessions() as db:
        event, outbox = db.scalar(select(AttendanceEvent)), db.scalar(select(OrdsOutbox))
        event.oracle_confirmed_at = outbox.acknowledged_at = utc_now() - timedelta(days=2)
        outbox.last_attempt_at = event.oracle_confirmed_at
        db.commit()
    assert claim_confirmed_membership_audit_batch(10) == []


def test_slow_force_verification_does_not_delay_ordinary_delivery(monkeypatch):
    import asyncio
    from zk_add import worker

    monkeypatch.setattr(settings, "ords_base_url", "https://ords.invalid")
    monkeypatch.setattr(settings, "ords_username", "test")
    monkeypatch.setattr(settings, "ords_password", "test")
    monkeypatch.setattr(worker, "_ords_request_lock", asyncio.Lock())
    monkeypatch.setattr(worker, "ords_circuit_is_open", lambda: False)
    monkeypatch.setattr(worker, "claim_ords_batch", lambda _: ["live", "forced"])
    monkeypatch.setattr(delivery, "split_claims", lambda _: (["live"], ["forced"]))

    async def scenario():
        live_saved = asyncio.Event()

        async def ordinary(claims, **kwargs):
            assert claims == ["live"]
            live_saved.set()

        async def slow_force(claims, **kwargs):
            assert claims == ["forced"]
            await asyncio.wait_for(live_saved.wait(), timeout=1)

        monkeypatch.setattr(worker, "_deliver_ordinary_claims", ordinary)
        monkeypatch.setattr(delivery, "deliver_forced", slow_force)
        await worker.deliver_ords_batch(limit=5, concurrency=5)

    asyncio.run(scenario())


def test_manual_sync_concurrency_and_partial_device_availability(store):
    from zk_add.service import onboard_connector

    sessions, _, _ = store
    with sessions() as db:
        for index in range(3):
            connector, _, _ = onboard_connector(
                db,
                hardware_id=f"00:11:22:33:44:{index:02d}",
                zone_id="FORCE-TEST",
                zone_name="Same display name",
                device_id=f"FORCE-{index}",
                firmware_version="2.4.12",
                expected_serial=f"FORCE-SERIAL-{index}",
                actor="test",
                ip_address=None,
            )
            connector.connected = index != 2
            connector.boot_id = f"test-boot-{index}"
        db.commit()
        job = force.create_check(
            db,
            actor="operator",
            request=ForceCheckRequest(
                scope="ALL_PAKISTAN", idempotency_key="bounded-national-check"
            ),
        )
        db.commit()
        job_id = job.id
    for _ in range(14):
        tick(sessions)
    with sessions() as db:
        tasks = db.scalars(select(Task).where(Task.job_id == job_id)).all()
        assert len(tasks) == 4
        assert sum(task.status == "SYNCING" for task in tasks) == 2
        assert sum(task.status == "SYNC_PENDING" for task in tasks) == 1
        assert (
            sum(
                task.status == "UNAVAILABLE" and task.error_code == "DEVICE_OFFLINE"
                for task in tasks
            )
            == 1
        )
        commands = db.scalars(
            select(DeviceCommand).where(DeviceCommand.command_type == "REFRESH_USERS")
        ).all()
        assert len(commands) == 2 and len({c.idempotency_key for c in commands}) == 2
