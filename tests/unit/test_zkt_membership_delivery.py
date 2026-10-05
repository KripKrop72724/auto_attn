"""Explicit experimental Oracle membership, without inventing content proof."""
import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event as sa_event, select, text

import test_zkt_oracle_delivery as base
from test_zkt_oracle_delivery import claim, retry_now, count
from zk_add import zkt_oracle_delivery as delivery
from zk_add.crypto import decrypt_json
from zk_add.settings import settings
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, ZktOccurrenceAlias,
    ZktOracleIntent, ZktOracleContentReceipt, ZktOracleMembershipReceipt)

store = base.store


@pytest.fixture
def membership_store(store):
    # Replace the newly-created, unprepared test intent by explicit registration.
    # No production route or retained content intent is migrated by the feature.
    with store() as db:
        db.delete(db.scalar(select(ZktOracleIntent)))
        db.flush()
        delivery.register_intent(db, connector=db.scalar(select(Connector)), event=db.scalar(select(AttendanceEvent)),
            outbox=db.scalar(select(OrdsOutbox)), alias=db.scalar(select(ZktOccurrenceAlias)),
            verification_scope=delivery.MEMBERSHIP_SCOPE)
        db.commit()
    return store


def response(uid, present=True):
    return dict(success=True, received_count=1, existing_count=1 if present else 0,
                missing_count=0 if present else 1, missing_event_uids=[] if present else [uid])


def verifier(monkeypatch, present=True):
    async def request(uid):
        return response(uid, present)
    monkeypatch.setattr(delivery, "_membership_request", request)


def test_explicit_scope_and_payload_are_frozen_without_route_downgrade(membership_store):
    store = membership_store
    value = claim(store)
    assert value["check"]["verification_scope"] == delivery.MEMBERSHIP_SCOPE
    assert value["check"]["event_uids"] == [value["payload"]["event_uid"]]
    with store() as db:
        intent = db.scalar(select(ZktOracleIntent))
        assert decrypt_json(intent.protected_check) == value["check"]
        assert decrypt_json(intent.protected_payload) == value["payload"]
        with pytest.raises(ValueError, match="INTENT_CONFLICT"):
            delivery.register_intent(db, connector=db.scalar(select(Connector)), event=db.scalar(select(AttendanceEvent)),
                outbox=db.scalar(select(OrdsOutbox)), alias=db.scalar(select(ZktOccurrenceAlias)))
    delivery.persist_result(value, "UNKNOWN")
    retry_now(store)
    assert claim(store)["check"] == value["check"]


def test_membership_completion_creates_only_scoped_receipt(membership_store, monkeypatch):
    store = membership_store
    value = claim(store)
    verifier(monkeypatch)
    classification, proof = asyncio.run(delivery.verify(value))
    assert classification == "UID_PRESENT" and isinstance(proof, delivery.MembershipProof)
    delivery.persist_result(value, classification, proof)
    delivery.persist_result(value, classification, proof)
    with store() as db:
        receipt = db.scalar(select(ZktOracleMembershipReceipt))
        assert receipt.verification_scope == delivery.MEMBERSHIP_SCOPE
        assert receipt.payload_digest == value["payload_digest"]
        assert count(db, ZktOracleMembershipReceipt) == 1
        assert count(db, ZktOracleContentReceipt) == 0
        row = db.scalar(select(AttendanceEvent))
        assert row.ords_status == "ACKED_CHECK"
        assert row.oracle_confirmation_path == "ADD_ZKT_UID_ONLY_V1"
        assert row.identity_content_status == "NOT_CHECKED"
        assert row.identity_content_confirmed_at is None and row.identity_downstream_confirmed_at is None
        assert db.scalar(select(OrdsOutbox)).acknowledged_at is not None


@pytest.mark.parametrize("field,value", [
    ("success", False), ("success", 1), ("received_count", True), ("received_count", "1"),
    ("received_count", 2), ("existing_count", 0), ("missing_count", 1),
    ("missing_event_uids", ["a" * 64]), ("missing_event_uids", ""),
])
def test_malformed_membership_cannot_complete_or_fall_back(membership_store, monkeypatch, field, value):
    current = claim(membership_store)
    async def request(uid):
        return {**response(uid), field: value}
    monkeypatch.setattr(delivery, "_membership_request", request)
    assert asyncio.run(delivery.verify(current)) == ("UNKNOWN", None)


@pytest.mark.parametrize("mutation", ["uid", "payload", "request", "scope", "content-result"])
def test_other_request_or_verification_kind_cannot_settle_membership(membership_store, monkeypatch, mutation):
    store = membership_store
    value = claim(store)
    verifier(monkeypatch)
    classification, proof = asyncio.run(delivery.verify(value))
    if mutation == "uid":
        proof = replace(proof, event_uid="f" * 64)
    elif mutation == "payload":
        proof = replace(proof, payload_digest="f" * 64)
    elif mutation == "request":
        proof = replace(proof, request_digest="f" * 64)
    elif mutation == "scope":
        with store() as db:
            db.scalar(select(ZktOracleIntent)).verification_scope = delivery.VERIFICATION_SCOPE
            db.commit()
    else:
        classification, proof = "MATCH", "f" * 64
    delivery.persist_result(value, classification, proof)
    with store() as db:
        assert count(db, ZktOracleMembershipReceipt) == count(db, ZktOracleContentReceipt) == 0
        assert db.scalar(select(OrdsOutbox)).acknowledged_at is None


@pytest.mark.parametrize("lost_response", [False, True])
def test_oracle_commit_and_lost_reply_use_membership_before_retirement(membership_store, monkeypatch, lost_response):
    store = membership_store
    value = claim(store)
    found, posts, checks = set(), [], []
    async def request(uid):
        checks.append(uid)
        return response(uid, uid in found)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, url, *, json):
            assert url.endswith("/raw-captures")
            with store() as db:
                assert db.scalar(select(ZktOracleIntent)).post_attempts == 1
            posts.append(json)
            found.add(json["event_uid"])
            if lost_response:
                raise httpx.ReadTimeout("synthetic lost response")
            return httpx.Response(200, json={"success": True})
    monkeypatch.setattr(delivery, "_membership_request", request)
    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    asyncio.run(delivery.deliver([value], concurrency=1))
    assert len(posts) == 1 and len(checks) == 2
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "ACKED_CHECK"
        assert count(db, ZktOracleMembershipReceipt) == 1
        assert count(db, ZktOracleContentReceipt) == 0


def test_http_success_without_membership_is_not_completion(membership_store, monkeypatch):
    store = membership_store
    value = claim(store)
    verifier(monkeypatch, present=False)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, url, *, json): return httpx.Response(200, json={"success": True})
    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    asyncio.run(delivery.deliver([value], concurrency=1))
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "FAILED_RETRYABLE"
        assert count(db, ZktOracleMembershipReceipt) == 0


def test_v2_unavailable_does_not_switch_to_membership(store, monkeypatch):
    current = claim(store)
    async def request(path, *, payload):
        assert path == "raw-captures/delivery-v2/check"
        return None
    monkeypatch.setattr(delivery, "_ords_request", request)
    assert asyncio.run(delivery.verify(current)) == ("UNKNOWN", None)
    with store() as db:
        assert db.scalar(select(ZktOracleIntent)).verification_scope == delivery.VERIFICATION_SCOPE


@pytest.mark.parametrize("status", [200, 201, 404, 409, 500])
def test_membership_transport_uses_only_ordinary_credentials_and_exact_endpoint(monkeypatch, status):
    monkeypatch.setattr(settings, "ords_base_url", "https://synthetic.invalid/attendance")
    monkeypatch.setattr(settings, "ords_username", "ordinary-synthetic")
    monkeypatch.setattr(settings, "ords_password", "synthetic-only")
    monkeypatch.setattr(settings, "attendance_repair_ords_username", None)
    monkeypatch.setattr(settings, "attendance_repair_ords_password", None)
    uid = "a" * 64
    def handle(request):
        assert str(request.url) == "https://synthetic.invalid/attendance/raw-captures/check"
        assert request.method == "POST"
        assert request.headers["X-API-Username"] == "ordinary-synthetic"
        assert request.headers["X-API-Password"] == "synthetic-only"
        assert json.loads(request.content) == {"event_uids": [uid]}
        return httpx.Response(status, json=response(uid))
    original = httpx.AsyncClient
    monkeypatch.setattr(delivery.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    actual = asyncio.run(delivery._membership_request(uid))
    assert actual == (response(uid) if status == 200 else None)


def test_receipt_commit_failure_preserves_retryable_intent(membership_store, monkeypatch):
    store = membership_store
    value = claim(store)
    verifier(monkeypatch)
    result, proof = asyncio.run(delivery.verify(value))
    engine = store.kw["bind"]
    def fail(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().startswith("INSERT INTO add_zkt_oracle_membership_receipts"):
            raise RuntimeError("synthetic failed receipt write")
    sa_event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            delivery.persist_result(value, result, proof)
    finally:
        sa_event.remove(engine, "before_cursor_execute", fail)
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
        assert db.scalar(select(AttendanceEvent)).oracle_confirmed_at is None
        assert count(db, ZktOracleMembershipReceipt) == 0
    delivery.persist_result(value, result, proof)
    with store() as db:
        assert count(db, ZktOracleMembershipReceipt) == 1


def test_concurrent_membership_verifiers_commit_one_scoped_receipt(membership_store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store = membership_store
    if store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Real row locks require PostgreSQL")
    current = claim(store)
    verifier(monkeypatch)
    classification, proof = asyncio.run(delivery.verify(current))
    barrier = Barrier(2)
    def confirm():
        barrier.wait(timeout=5)
        delivery.persist_result(current, classification, proof)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: confirm(), range(2)))
    with store() as db:
        assert count(db, ZktOracleMembershipReceipt) == 1
        assert count(db, ZktOracleContentReceipt) == 0
        assert db.scalar(select(OrdsOutbox)).status == "ACKED_CHECK"


def test_new_attempt_rejects_late_membership_result(membership_store, monkeypatch):
    store = membership_store
    first = claim(store)
    verifier(monkeypatch)
    classification, proof = asyncio.run(delivery.verify(first))
    delivery.persist_result(first, "UNKNOWN")
    retry_now(store)
    second = claim(store)
    delivery.persist_result(first, classification, proof)
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
        assert count(db, ZktOracleMembershipReceipt) == 0
    assert not delivery.reserve_post(first)
    classification, proof = asyncio.run(delivery.verify(second))
    delivery.persist_result(second, classification, proof)
    with store() as db:
        assert count(db, ZktOracleMembershipReceipt) == 1


def test_additive_migration_preserves_existing_v2_authority_and_receipts(store, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261005_0050_zkt_membership_evidence.py"
    spec = importlib.util.spec_from_file_location("zkt_membership_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = store.kw["bind"]
    with engine.begin() as connection:
        ZktOracleMembershipReceipt.__table__.drop(connection)
        connection.execute(text("ALTER TABLE add_zkt_oracle_intents DROP COLUMN verification_scope"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
    with store() as db:
        assert db.scalar(select(ZktOracleIntent)).verification_scope == delivery.VERIFICATION_SCOPE
    current = claim(store)
    delivery.persist_result(current, "MATCH", "a" * 64)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with store() as db:
        assert db.scalar(select(ZktOracleIntent)).verification_scope == delivery.VERIFICATION_SCOPE
        assert count(db, ZktOracleContentReceipt) == 1
        assert count(db, ZktOracleMembershipReceipt) == 0
