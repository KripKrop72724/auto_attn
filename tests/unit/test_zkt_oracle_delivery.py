"""Loss/replay, stale workers and content conflicts on ADD-owned Oracle delivery."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import MetaData, create_engine, event as sa_event, func, select, text
from sqlalchemy.orm import sessionmaker

from zk_add import zkt_oracle_delivery as delivery, worker
from zk_add.crypto import cnic_lookup, decrypt_json, encrypt_cnic
from zk_add.db import Base
from zk_add.models import (AttendanceEvent, Connector, OrdsOutbox, TerminalRecordManifest,
    TerminalSourceEpoch, ZKTDevice, ZktOccurrenceAlias, ZktOracleIntent, ZktOracleContentReceipt,
    ZktOracleMembershipReceipt)
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_custody import occurrence_id


@pytest.fixture(params=["sqlite", "postgres"])
def store(tmp_path, monkeypatch, request):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "synthetic-oracle-evidence-key")
    monkeypatch.setattr(settings, "ords_base_url", "https://synthetic.invalid/attendance")
    monkeypatch.setattr(settings, "ords_username", "synthetic")
    monkeypatch.setattr(settings, "ords_password", "synthetic")
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "zkt_oracle_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'evidence.db'}", connect_args={"check_same_thread": False})
    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    request.addfinalizer(cleanup)
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
    with sessions() as db:
        connector = Connector(connector_id="synthetic-zkt", hardware_id="aa:bb:cc:dd:ee:ff",
            zone_id="test", zone_name="test", device_id="test", display_name="test", zkt_custody_enabled=True)
        db.add(connector)
        db.flush()
        connector.zkt_device = ZKTDevice(connector_id=connector.id, serial="TEST01", confirmed_serial="TEST01")
        db.flush()
        epoch = TerminalSourceEpoch(zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1)
        db.add(epoch)
        db.flush()
        raw_digest = hashlib.sha256(b"synthetic source bytes").hexdigest()
        identity = occurrence_id("TEST01", epoch.epoch_id, 0, raw_digest)
        now = utc_now().replace(microsecond=0)
        event = AttendanceEvent(event_uid=identity, connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            device_serial="TEST01", user_id="1007", device_event_time=now, captured_at=now,
            source="CURRENT_RECONCILE", status="1", punch="0", ords_status="PENDING", clock_quality="OK",
            display_name="Synthetic Person", cnic_encrypted=encrypt_cnic("1234512345671"),
            cnic_lookup_hash=cnic_lookup("1234512345671"), identity_resolution_status="RESOLVED",
            manual_release_required=False)
        db.add(event)
        db.flush()
        manifest = TerminalRecordManifest(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
            terminal_serial="TEST01", generation=1, source_epoch_id=epoch.id, ordinal=0, canonical_source=True,
            raw_record_digest=raw_digest, terminal_record_key=raw_digest, disposition="EVENT", attendance_event_id=event.id)
        outbox = OrdsOutbox(attendance_event_id=event.id, status="PENDING", delivery_type="CURRENT_RECONCILE")
        db.add_all([manifest, outbox])
        db.flush()
        alias = ZktOccurrenceAlias(occurrence_id=identity, zkt_device_id=connector.zkt_device.id,
            source_epoch_id=epoch.id, ordinal=0, manifest_id=manifest.id, raw_digest=raw_digest, attendance_event_id=event.id)
        db.add(alias)
        db.flush()
        delivery.register_intent(db, connector=connector, event=event, outbox=outbox, alias=alias)
        db.commit()
    yield sessions


def verification_response(check, classification):
    matched = classification == "MATCH"
    raw = matched or classification in {"DOWNSTREAM_PENDING", "DOWNSTREAM_HOLD", "IDENTITY_HOLD"}
    downstream = ("RAW_ONLY" if check["projection"]["raw_punch"] == "T" else "MATCH") if matched else "NOT_VERIFIED"
    return {"success": True, "contract_version": "2", "verification_scope": "ORACLE_RAW_DAY_TIMES_V2",
        "request_digest": check["request_digest"], "results": [{"event_uid": check["projection"]["event_uid"],
            "classification": classification, "current_content_token": "a" * 64,
            "raw_projection_verified": raw, "downstream_status": downstream}]}


def claim(store):
    values = worker.claim_ords_batch(10)
    assert len(values) == 1
    ordinary, owned = delivery.split_claims(values)
    assert not ordinary and len(owned) == 1
    return owned[0]


def retry_now(store):
    with store() as db:
        row = db.scalar(select(OrdsOutbox))
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
        db.commit()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_payload_freezes_encrypted_and_replay_retains_content_identity(store):
    first = claim(store)
    delivery.persist_result(first, "UNKNOWN")
    retry_now(store)
    second = claim(store)
    assert first["payload"] == second["payload"] and first["check"] == second["check"]
    assert first["payload_digest"] == second["payload_digest"] and second["attempt"] == first["attempt"] + 1
    with store() as db:
        intent = db.scalar(select(ZktOracleIntent))
        assert "Synthetic" not in intent.protected_payload and "1234512345671" not in intent.protected_payload
        assert decrypt_json(intent.protected_payload) == first["payload"]
        assert count(db, ZktOracleContentReceipt) == 0


def test_uid_membership_http_success_and_firmware_claim_cannot_complete_an_intent(store):
    value = claim(store)
    with store() as db:
        worker.apply_ords_confirmation(db, claimed_id=value["row_id"], path="ORDS_MEMBERSHIP_CHECK")
        worker.apply_ords_delivery_result(db, claimed_id=value["row_id"], status=200,
            body={"success": True}, transport_error=None, response_parsed=True)
        db.commit()
        row = db.scalar(select(OrdsOutbox))
        assert row.status == "IN_FLIGHT" and row.acknowledged_at is None
    delivery.persist_result(value, "MATCH", "1" * 64)
    with store() as db:
        row = db.scalar(select(OrdsOutbox))
        event = db.scalar(select(AttendanceEvent))
        proof = db.scalar(select(ZktOracleContentReceipt))
        assert row.status == event.ords_status == "ACKED_CHECK"
        assert proof.payload_digest == value["payload_digest"] and proof.claim_attempt == value["attempt"]
        assert proof.verification_scope == "ORACLE_RAW_DAY_TIMES_V2"
        assert event.oracle_confirmation_path == "ADD_ZKT_PROJECTION_V2"
        event.oracle_confirmed_at = row.acknowledged_at = utc_now() - timedelta(days=1)
        row.last_attempt_at = utc_now() - timedelta(days=2)
        db.commit()
    assert not worker.claim_confirmed_membership_audit_batch()
    delivery.persist_result(value, "MATCH", "1" * 64)
    with store() as db:
        assert count(db, ZktOracleContentReceipt) == 1


@pytest.mark.parametrize("classification", ["MISMATCH", "IMMUTABLE_MISMATCH", "CROSS_DEVICE_UID_COLLISION", "CHANGED"])
def test_content_conflicts_hold_without_replacement_or_success(store, classification):
    value = claim(store)
    delivery.persist_result(value, classification, "a" * 64)
    with store() as db:
        row = db.scalar(select(OrdsOutbox))
        event = db.scalar(select(AttendanceEvent))
        assert row.status == event.ords_status == "QUARANTINED_IDENTITY_CONFLICT"
        assert row.next_attempt_at is None and event.oracle_confirmed_at is None
        assert count(db, ZktOracleContentReceipt) == 0 and count(db, AttendanceEvent) == 1


def test_stale_worker_cannot_complete_or_post_after_new_attempt_claims_row(store):
    first = claim(store)
    delivery.persist_result(first, "UNKNOWN")
    retry_now(store)
    second = claim(store)
    assert not delivery.reserve_post(first)
    delivery.persist_result(first, "MATCH", "a" * 64)
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
        assert count(db, ZktOracleContentReceipt) == 0
    delivery.persist_result(second, "MATCH", "b" * 64)
    with store() as db:
        assert db.scalar(select(ZktOracleContentReceipt)).content_token == "b" * 64


@pytest.mark.parametrize("change", ["identity", "raw_source", "terminal", "epoch", "epoch_uuid", "manifest_link", "disabled_feature"])
def test_current_evidence_is_revalidated_without_downgrading_delivery_route(store, change):
    value = claim(store)
    with store() as db:
        if change == "identity":
            db.scalar(select(AttendanceEvent)).display_name = "Changed Person"
        elif change == "raw_source":
            db.scalar(select(TerminalRecordManifest)).raw_record_digest = "b" * 64
        elif change == "terminal":
            db.scalar(select(ZKTDevice)).confirmed_serial = "REPLACED"
        elif change == "epoch":
            prior = db.scalar(select(TerminalSourceEpoch))
            other = TerminalSourceEpoch(zkt_device_id=prior.zkt_device_id, terminal_generation=1, sequence=2)
            db.add(other)
            db.flush()
            db.scalar(select(TerminalRecordManifest)).source_epoch_id = other.id
        elif change == "epoch_uuid":
            db.scalar(select(TerminalSourceEpoch)).epoch_id = str(uuid4())
        elif change == "manifest_link":
            db.scalar(select(TerminalRecordManifest)).attendance_event_id = None
        else:
            db.scalar(select(Connector)).zkt_custody_enabled = False
        db.commit()
    if change == "disabled_feature":
        assert delivery.reserve_post(value)  # Durable intent survives feature rollback.
        delivery.persist_result(value, "MATCH", "a" * 64)
        expected = "ACKED_CHECK"
    else:
        assert not delivery.reserve_post(value)
        delivery.persist_result(value, "MATCH", "a" * 64)
        expected = "QUARANTINED_IDENTITY_CONFLICT"
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == expected


def test_encryption_failure_leaves_no_half_frozen_payload_and_retries(store, monkeypatch):
    original = delivery.encrypt_json
    calls = 0
    def encrypt(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic unavailable key")
        return original(value)
    monkeypatch.setattr(delivery, "encrypt_json", encrypt)
    assert delivery.split_claims(worker.claim_ords_batch(10)) == ([], [])
    with store() as db:
        intent = db.scalar(select(ZktOracleIntent))
        assert intent.payload_digest is intent.protected_payload is intent.protected_check is None
        assert db.scalar(select(OrdsOutbox)).status == "FAILED_RETRYABLE"
    retry_now(store)
    assert claim(store)["payload_digest"]


def test_unavailable_key_after_freeze_retains_payload_and_recovers(store, monkeypatch):
    value = claim(store)
    old_key = settings.pii_fernet_key
    monkeypatch.setattr(settings, "pii_fernet_key", "")
    delivery.persist_result(value, "MATCH", "a" * 64)
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "FAILED_RETRYABLE"
        assert count(db, ZktOracleContentReceipt) == 0
    monkeypatch.setattr(settings, "pii_fernet_key", old_key)
    retry_now(store)
    assert claim(store)["payload"] == value["payload"]


def test_receipt_storage_failure_rolls_back_completion(store):
    value = claim(store)
    def fail(_mapper, _connection, _target):
        raise RuntimeError("synthetic receipt storage failure")
    sa_event.listen(ZktOracleContentReceipt, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="receipt storage"):
            delivery.persist_result(value, "MATCH", "a" * 64)
    finally:
        sa_event.remove(ZktOracleContentReceipt, "before_insert", fail)
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
        assert db.scalar(select(AttendanceEvent)).oracle_confirmed_at is None
        assert count(db, ZktOracleContentReceipt) == 0


@pytest.mark.parametrize("response", [
    None, [], {}, {"success": True, "results": []}, {"success": True, "results": [None]},
    {"success": True, "results": [{"event_uid": "wrong", "classification": "MATCH", "current_content_token": "a" * 64}]},
])
def test_malformed_content_response_never_becomes_success(store, monkeypatch, response):
    value = claim(store)
    async def request(*_args, **_kwargs):
        return response
    monkeypatch.setattr(delivery, "_ords_request", request)
    assert asyncio.run(delivery.verify(value)) == ("UNKNOWN", None)
    response = {"success": True, "results": [{"event_uid": value["payload"]["event_uid"],
        "classification": ["MATCH"], "current_content_token": "a" * 64}]}
    assert asyncio.run(delivery.verify(value)) == ("UNKNOWN", None)


@pytest.mark.parametrize("lost_response", [False, True])
def test_oracle_commit_then_lost_response_is_verified_before_retiring(store, monkeypatch, lost_response):
    value = claim(store)
    oracle = {}
    checks = []
    posts = []
    async def request(_path, *, payload):
        assert _path == "raw-captures/delivery-v2/check"
        checks.append(payload)
        uid = payload["projection"]["event_uid"]
        classification = "MATCH" if uid in oracle else "MISSING"
        return verification_response(payload, classification)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, _url, *, json):
            assert _url.endswith("/raw-captures")
            with store() as db:
                assert db.scalar(select(ZktOracleIntent)).post_attempts == 1
                assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
            posts.append(json)
            oracle[json["event_uid"]] = json
            if lost_response:
                raise httpx.ReadTimeout("synthetic lost reply")
            return httpx.Response(200, json={"success": True})
    monkeypatch.setattr(delivery, "_ords_request", request)
    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    asyncio.run(delivery.deliver([value], concurrency=1))
    assert posts == [value["payload"]] and len(checks) == 2
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "ACKED_CHECK"
        assert count(db, ZktOracleContentReceipt) == 1
        assert count(db, AttendanceEvent) == 1


def test_successful_post_without_verification_keeps_retry_and_never_reposts_a_matching_row(store, monkeypatch):
    first = claim(store)
    oracle = {}
    posts = []
    checks = 0
    async def request(_path, *, payload):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("synthetic check outage after commit")
        uid = payload["projection"]["event_uid"]
        return verification_response(payload, "MATCH" if uid in oracle else "MISSING")
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, _url, *, json):
            posts.append(json)
            oracle[json["event_uid"]] = json
            return httpx.Response(200, json={"success": True})
    monkeypatch.setattr(delivery, "_ords_request", request)
    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    asyncio.run(delivery.deliver([first], concurrency=1))
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "FAILED_RETRYABLE"
        assert count(db, ZktOracleContentReceipt) == 0
    retry_now(store)
    second = claim(store)
    asyncio.run(delivery.deliver([second], concurrency=1))
    assert len(posts) == 1 and posts[0] == second["payload"] and checks == 3
    with store() as db:
        assert count(db, ZktOracleContentReceipt) == 1


def test_registered_intent_is_selected_before_ordinary_or_manual_lanes(store, monkeypatch):
    seen = []
    async def journal(values, *, concurrency):
        seen.extend(values)
    async def forbidden(*_args, **_kwargs):
        pytest.fail("ADD-owned intent reached ordinary membership lane")
    monkeypatch.setattr(delivery, "deliver", journal)
    monkeypatch.setattr(worker, "_deliver_ordinary_claims", forbidden)
    monkeypatch.setattr(worker, "ords_circuit_is_open", lambda: False)
    asyncio.run(worker.deliver_ords_batch(limit=5, concurrency=2))
    assert len(seen) == 1 and seen[0]["intent_id"]


def test_recorded_receipt_contains_no_plaintext_employee_or_cnic(store):
    value = claim(store)
    delivery.persist_result(value, "MATCH", "a" * 64)
    with store() as db:
        receipt = db.scalar(select(ZktOracleContentReceipt))
        public = json.dumps({column.name: str(getattr(receipt, column.name)) for column in receipt.__table__.columns})
        assert "Synthetic Person" not in public and "1234512345671" not in public


def test_postgres_concurrent_verifiers_commit_one_receipt(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    if store.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Real row-lock qualification requires PostgreSQL")
    value = claim(store)
    barrier = Barrier(2)
    def confirm():
        barrier.wait(timeout=5)
        delivery.persist_result(value, "MATCH", "a" * 64)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: confirm(), range(2)))
    with store() as db:
        assert count(db, ZktOracleContentReceipt) == 1
        assert db.scalar(select(OrdsOutbox)).status == "ACKED_CHECK"


def test_additive_migration_and_rollback_retain_payloads_and_receipts(store, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions/20261004_0046_zkt_oracle_content.py"
    spec = importlib.util.spec_from_file_location("zkt_oracle_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = store.kw["bind"]
    with engine.begin() as connection:
        ZktOracleMembershipReceipt.__table__.drop(connection)
        ZktOracleContentReceipt.__table__.drop(connection)
        ZktOracleIntent.__table__.drop(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
    with store() as db:
        assert count(db, ZktOracleIntent) == count(db, ZktOracleContentReceipt) == 0
        delivery.register_intent(db, connector=db.scalar(select(Connector)), event=db.scalar(select(AttendanceEvent)),
            outbox=db.scalar(select(OrdsOutbox)), alias=db.scalar(select(ZktOccurrenceAlias)))
        db.commit()
    value = claim(store)
    delivery.persist_result(value, "MATCH", "a" * 64)
    with store() as db:
        payload = db.scalar(select(ZktOracleIntent)).protected_payload
        receipt_id = db.scalar(select(ZktOracleContentReceipt)).receipt_id
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with store() as db:
        assert db.scalar(select(ZktOracleIntent)).protected_payload == payload
        assert db.scalar(select(ZktOracleContentReceipt)).receipt_id == receipt_id
        assert decrypt_json(payload) == value["payload"]


def test_split_retains_legacy_route_and_refuses_registration_of_legacy_identity(store):
    with store() as db:
        intent = db.scalar(select(ZktOracleIntent))
        db.delete(intent)
        event = db.scalar(select(AttendanceEvent))
        event.event_uid = "b" * 64
        db.flush()
        with pytest.raises(ValueError, match="OCCURRENCE_BINDING"):
            delivery.register_intent(db, connector=db.scalar(select(Connector)), event=event,
                outbox=db.scalar(select(OrdsOutbox)), alias=db.scalar(select(ZktOccurrenceAlias)))
        db.commit()
    values = worker.claim_ords_batch(10)
    ordinary, content = delivery.split_claims(values)
    assert ordinary == values and not content


@pytest.mark.parametrize("change", ["version", "scope", "request", "core_only", "raw_false", "raw_string", "pending_day", "wrong_raw_only"])
def test_v2_rejects_weaker_wrong_request_and_partial_proof(store, monkeypatch, change):
    value = claim(store)
    response = verification_response(value["check"], "MATCH")
    if change == "version":
        response["contract_version"] = "1"
    elif change == "scope":
        response["verification_scope"] = "ORACLE_RAW_CORE_V1"
    elif change == "request":
        response["request_digest"] = "f" * 64
    elif change == "core_only":
        response.pop("verification_scope")
    elif change == "raw_false":
        response["results"][0]["raw_projection_verified"] = False
    elif change == "raw_string":
        response["results"][0]["raw_projection_verified"] = "true"
    elif change == "pending_day":
        response["results"][0]["downstream_status"] = "NOT_VERIFIED"
    else:
        response["results"][0]["downstream_status"] = "RAW_ONLY"
    async def request(*_args, **_kwargs): return response
    monkeypatch.setattr(delivery, "_ords_request", request)
    assert asyncio.run(delivery.verify(value)) == ("UNKNOWN", None)


@pytest.mark.parametrize("classification", ["DOWNSTREAM_PENDING", "DOWNSTREAM_HOLD", "IDENTITY_HOLD"])
def test_raw_match_with_unfinished_day_never_posts_or_completes(store, monkeypatch, classification):
    value = claim(store)
    async def request(*_args, **_kwargs): return verification_response(value["check"], classification)
    class ForbiddenClient:
        def __init__(self, **_kwargs): pytest.fail("Preserved raw record was reposted")
    monkeypatch.setattr(delivery, "_ords_request", request)
    monkeypatch.setattr(delivery.httpx, "AsyncClient", ForbiddenClient)
    asyncio.run(delivery.deliver([value], concurrency=1))
    with store() as db:
        row = db.scalar(select(OrdsOutbox))
        assert row.status == ("FAILED_RETRYABLE" if classification == "DOWNSTREAM_PENDING" else "QUARANTINED_IDENTITY_CONFLICT")
        assert row.last_error.endswith(classification if classification != "DOWNSTREAM_PENDING" else "DOWNSTREAM_VERIFICATION_PENDING")
        assert not row.acknowledged_at and count(db, ZktOracleContentReceipt) == 0


@pytest.mark.parametrize("drift, expected", [(None, None), (0, "0.000"), (1.2345, "1.235"), (-1.2345, "-1.235")])
def test_projection_declares_precision_and_timezone_without_changing_original(store, drift, expected):
    value = claim(store)
    payload = dict(value["payload"], clockdiff=drift, timestamp="2026-10-01T09:23:45.123456+05:00")
    check = delivery.projection_check(payload)
    assert check["projection"]["clockdiff"] == expected
    assert check["projection"]["timestamp"] == "2026-10-01T04:23:45.123456Z"
    assert payload["timestamp"].endswith("+05:00") and payload["clockdiff"] == drift
    assert {"status", "punch", "zone_name"}.isdisjoint(check["projection"])
    assert set(delivery.PROJECTION_FIELDS) | {"clockdiff"} == set(check["projection"])


@pytest.mark.parametrize("drift", [True, "NaN", "Infinity", "10000000", "9999999.9999"])
def test_unrepresentable_clock_does_not_get_silently_truncated(store, drift):
    value = claim(store)
    with pytest.raises(delivery.EvidenceChanged, match="UNREPRESENTABLE"):
        delivery.projection_check(dict(value["payload"], clockdiff=drift))


@pytest.mark.parametrize("older_reader", [False, True])
def test_frozen_verification_contract_cannot_be_silently_upgraded_or_downgraded(store, monkeypatch, older_reader):
    from zk_add.crypto import encrypt_json
    first = claim(store)
    delivery.persist_result(first, "UNKNOWN")
    legacy = {"contract_version": "1", "items": [{"event_uid": first["payload"]["event_uid"]}]}
    if older_reader:
        current = delivery._current
        def older(session, intent, row):
            event, payload, _ = current(session, intent, row)
            return event, payload, legacy
        monkeypatch.setattr(delivery, "_current", older)
    else:
        with store() as db:
            db.scalar(select(ZktOracleIntent)).protected_check = encrypt_json(legacy)
            db.commit()
    with store() as db:
        frozen = db.scalar(select(ZktOracleIntent)).protected_check
    retry_now(store)
    assert delivery.split_claims(worker.claim_ords_batch(10)) == ([], [])
    with store() as db:
        assert db.scalar(select(OrdsOutbox)).status == "QUARANTINED_IDENTITY_CONFLICT"
        assert db.scalar(select(ZktOracleIntent)).protected_check == frozen
        assert count(db, ZktOracleContentReceipt) == 0
