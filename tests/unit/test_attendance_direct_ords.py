"""Direct Oracle approval still has durable custody and two explicit exclusions."""

from contextlib import nullcontext
import asyncio

import httpx

import pytest
from sqlalchemy import select

from test_attendance_force_release import store as store
from test_attendance_repair import CORRECT_CNIC, WRONG_CNIC, repair_store as repair_store
from zk_add import attendance_direct_ords as direct
from zk_add import attendance_force_delivery as delivery
from zk_add import worker
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.attendance_recovery import RecoveryError
from zk_add.crypto import encrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceRecoveryItem as Item,
    AttendanceRecoveryJob as Job,
    Connector,
    DeviceUser,
    OrdsOutbox,
)
from zk_add.settings import settings


def request(event_id, *, key="direct-approval-one"):
    return DirectOrdsStartRequest(
        event_ids=[event_id], reason="Administrator reviewed saved punch",
        password="test-admin-password", idempotency_key=key,
    )


def source_ready(sessions):
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = encrypt_cnic(CORRECT_CNIC)
        event.display_name = "Captured employee"
        db.commit()
        return event.id


def test_identity_conflict_is_approved_and_queued_without_rewriting_source(store):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        original_uid = event.event_uid
        original_identity = event.identity_resolution_status
        job = direct.create(db, actor="operator", request=request(event_id))
        assert direct.create(db, actor="operator", request=request(event_id)).id == job.id
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        decision = db.scalar(select(Decision))
        outbox = db.scalar(select(OrdsOutbox))
        item = db.scalar(select(Item))
        assert event.event_uid == original_uid
        assert event.identity_resolution_status == original_identity
        assert event.ords_status == outbox.status == "PENDING"
        assert decision.proof["policy"] == direct.POLICY
        assert item.status == "WAITING_ORACLE"
        assert direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision) is not None
        assert len(db.scalars(select(Decision)).all()) == 1


@pytest.mark.parametrize("missing", ["user", "current_cnic"])
def test_only_unknown_user_or_missing_cnic_is_excluded(store, missing):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        if missing == "user":
            db.scalar(select(DeviceUser)).present = False
        else:
            db.scalar(select(DeviceUser)).cnic_encrypted = None
        db.commit()
    with sessions() as db:
        job = direct.create(db, actor="operator", request=request(event_id))
        db.commit()
        assert job.status == "COMPLETED_WITH_REVIEW"
        assert db.scalar(select(Item)).error_code == ("UNKNOWN_USER" if missing == "user" else "CNIC_MISSING")
        assert db.scalar(select(Decision)) is None


def test_held_punch_without_saved_cnic_uses_synced_user_and_keeps_source_unchanged(store):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event_id = event.id
        original_uid = event.event_uid
        event.cnic_encrypted = None
        event.ords_status = "BLOCKED_IDENTITY"
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        hint = direct.identity_hints_for_page(db, [event])[event_id]
        assert hint == {
            "eligible": True, "cnic_source": "SYNCED_USER", "exclusion": None,
        }
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "READY"
        assert item.result["cnic_source"] == "SYNCED_USER"
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        decision = db.scalar(select(Decision))
        payload = direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision)
        assert payload["cnic"] == CORRECT_CNIC
        assert payload["employee_name"] == db.scalar(select(DeviceUser)).display_name
        assert event.cnic_encrypted is None
        assert event.event_uid == original_uid
        assert event.ords_status == "PENDING"
        assert db.scalar(select(Item)).status == "WAITING_ORACLE"


def test_synced_cnic_change_after_approval_stops_dispatch(store):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = None
        event.ords_status = "BLOCKED_IDENTITY"
        event_id = event.id
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        db.scalar(select(DeviceUser)).cnic_encrypted = encrypt_cnic(WRONG_CNIC)
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
        assert db.scalar(select(Item)).error_code == "SOURCE_CHANGED"
        assert db.scalar(select(Decision)) is None


def test_synced_cnic_change_after_queue_blocks_approved_payload(store):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = None
        event.ords_status = "BLOCKED_IDENTITY"
        event_id = event.id
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        db.scalar(select(DeviceUser)).cnic_encrypted = encrypt_cnic(WRONG_CNIC)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert direct.approved_payload(
            db, event, db.get(Connector, event.connector_id), db.scalar(select(Decision)),
        ) is None


def test_missing_saved_and_current_cnic_is_excluded(store):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = None
        event.ords_status = "BLOCKED_IDENTITY"
        db.scalar(select(DeviceUser)).cnic_encrypted = None
        event_id = event.id
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert direct.identity_hints_for_page(db, [event])[event_id]["exclusion"] == "CNIC_MISSING"
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_MISSING"
        assert db.scalar(select(Decision)) is None


def test_unreadable_saved_cnic_stays_visible_and_uses_synced_user(store):
    from zk_add.web import serialize_attendance

    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = "damaged-protected-value"
        event.ords_status = "BLOCKED_IDENTITY"
        db.commit()
        assert serialize_attendance(event)["cnic_masked"] is None
        assert direct.identity_hints_for_page(db, [event])[event.id]["cnic_source"] == "SYNCED_USER"


def test_duplicate_request_conflict_and_changed_source_cannot_send(store):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        with pytest.raises(RecoveryError, match="request key"):
            direct.create(
                db, actor="operator",
                request=DirectOrdsStartRequest(
                    event_ids=[event_id], reason="Different approval reason",
                    password="test-admin-password", idempotency_key="direct-approval-one",
                ),
            )
        event = db.get(AttendanceEvent, event_id)
        event.punch = "1"
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        assert db.scalar(select(Item)).error_code == "SOURCE_CHANGED"
        assert db.scalar(select(Decision)) is None
        assert db.scalar(select(Job)).status == "COMPLETED_WITH_REVIEW"


def test_invalid_uid_is_attempted_only_under_explicit_direct_approval(store, monkeypatch):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        db.get(AttendanceEvent, event_id).event_uid = "saved-but-invalid-id"
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(db))
        claims = worker.claim_ords_batch(1)
        assert len(claims) == 1
        assert claims[0][1]["event_uid"] == "saved-but-invalid-id"
        db.commit()
    with sessions() as db:
        monkeypatch.setattr(delivery, "session_scope", lambda: nullcontext(db))
        normal, forced = delivery.split_claims(claims)
        assert normal == [] and len(forced) == 1 and forced[0]["direct"] is True
        delivery.persist_result(forced[0], "MATCH", token="a" * 64)
        db.commit()
        assert db.get(AttendanceEvent, event_id).ords_status == "ACKED_CHECK"
        assert db.scalar(select(Item)).result["oracle_content_token"] == "a" * 64


def test_direct_send_attempts_known_oracle_conflict_but_acks_only_matching_content(store, monkeypatch):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    _normal, forced = delivery.split_claims(worker.claim_ords_batch(1))
    assert len(forced) == 1
    checks, posts = [], []

    async def content_check(path, *, payload):
        checks.append(payload)
        return {"success": True, "results": [{
            "event_uid": forced[0]["payload"]["event_uid"],
            "classification": "MISMATCH" if len(checks) == 1 else "MATCH",
            "current_content_token": "a" * 64,
        }]}

    def transport(request):
        posts.append(request)
        return httpx.Response(200, json={"success": True})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(delivery, "_ords_request", content_check)
    monkeypatch.setattr(
        delivery.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    monkeypatch.setattr(settings, "ords_base_url", "https://ords.invalid")
    monkeypatch.setattr(settings, "ords_username", "test")
    monkeypatch.setattr(settings, "ords_password", "test")
    asyncio.run(delivery.deliver_forced(forced, concurrency=1))
    assert len(posts) == 1 and len(checks) == 2
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "ACKED_CHECK"
        assert db.scalar(select(Item)).result["direct_post_attempts"] == 1


def test_uncertain_direct_post_is_not_repeated_without_definitive_missing_content(store, monkeypatch):
    sessions, _connector_id, _uid = store
    event_id = source_ready(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=request(event_id))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    _normal, forced = delivery.split_claims(worker.claim_ords_batch(1))
    checks, posts = [], []

    async def unknown_check(path, *, payload):
        checks.append(payload)
        return {"success": False, "results": []}

    def transport(request):
        posts.append(request)
        raise httpx.ReadTimeout("reply lost", request=request)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(delivery, "_ords_request", unknown_check)
    monkeypatch.setattr(
        delivery.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    monkeypatch.setattr(settings, "ords_base_url", "https://ords.invalid")
    monkeypatch.setattr(settings, "ords_username", "test")
    monkeypatch.setattr(settings, "ords_password", "test")
    asyncio.run(delivery.deliver_forced(forced, concurrency=1))
    assert len(posts) == 1
    with sessions() as db:
        outbox = db.scalar(select(OrdsOutbox))
        outbox.status = "PENDING"
        db.commit()
    _normal, retry = delivery.split_claims(worker.claim_ords_batch(1))
    asyncio.run(delivery.deliver_forced(retry, concurrency=1))
    assert len(posts) == 1
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "FAILED_RETRYABLE"
        assert db.scalar(select(Item)).result["direct_post_attempts"] == 1
        item = db.scalar(select(Item))
        item.result = {**item.result, "needs_attention": True, "reason": "Oracle verification unavailable."}
        db.flush()
        assert direct.serialize(db, db.scalar(select(Job)))["attention"] == 1
        assert direct.items_page(db, db.scalar(select(Job)))["rows"][0]["status"] == "NEEDS_ATTENTION"
