"""Recover attendance read side: Life / Health, serial search, scope counts (UI/UX revamp ADV-10)."""

import base64
import hashlib

from fastapi.testclient import TestClient

from test_add_backend import SERIAL, connector_fixture, db as db
from zk_add.crypto import encrypt_text
from zk_add.models import ReconciliationJob, TerminalRecordManifest
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.web import app, get_db


def _client(db) -> TestClient:
    raw_session, _admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest",
    )
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client


def _connectors(db):
    health = connector_fixture(db)
    life = connector_fixture(db, hardware_id="aa:bb:cc:dd:ee:31", expected_serial="LIFE-SERIAL-31")
    life.display_name = "LF-ZONE-BLD9-03"
    life.zone_id = "LF-ZONE-BLD9"
    db.commit()
    return health, life


def _job(db, connector, *, status, serial, key):
    job = ReconciliationJob(
        connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
        reason="Synthetic list test", idempotency_key=key, request_digest=hashlib.sha256(key.encode()).hexdigest(),
        status=status, terminal_serial=serial,
    )
    db.add(job)
    db.flush()
    return job


def _exception(db, connector, ordinal, disposition):
    raw = ordinal.to_bytes(8, "big")
    db.add(TerminalRecordManifest(
        job_id=None, chunk_id=None, connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
        terminal_serial=SERIAL, generation=1, ordinal=ordinal, source_kind="TAIL", canonical_source=True,
        record_size=8, raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(f"exception-{connector.id}-{ordinal}".encode()).hexdigest(),
        occurrence_index=1, attendance_event_id=None, disposition=disposition,
        protected_raw_record=encrypt_text(base64.b64encode(raw).decode()), error_code="ZKT_TIMESTAMP_OUT_OF_RANGE",
    ))


def test_reconciliations_division_filters_rows_and_totals_and_q_matches_serial(db):
    health, life = _connectors(db)
    health_job = _job(db, health, status="NEEDS_ATTENTION", serial="HEALTHSERIAL1", key="health-job-1")
    life_job = _job(db, life, status="COMPLETED", serial="LIFESERIAL77", key="life-job-1")
    db.commit()
    client = _client(db)

    everything = client.get("/api/v1/reconciliations").json()
    assert everything["totals"]["all"] == 2
    by_id = {row["job_id"]: row for row in everything["rows"]}
    assert by_id[life_job.job_id]["division"] == "life"
    assert by_id[life_job.job_id]["friendly_name"] == "Building 9 · Device 3"
    assert by_id[health_job.job_id]["division"] == "health"

    life_only = client.get("/api/v1/reconciliations", params={"division": "life"}).json()
    assert [row["job_id"] for row in life_only["rows"]] == [life_job.job_id]
    assert life_only["totals"]["all"] == 1
    assert life_only["totals"]["completed"] == 1
    assert life_only["totals"]["attention"] == 0
    assert life_only["filtered_total"] == 1

    serial = client.get("/api/v1/reconciliations", params={"q": "lifeserial7"}).json()
    assert [row["job_id"] for row in serial["rows"]] == [life_job.job_id]
    assert client.get("/api/v1/reconciliations", params={"division": "both"}).status_code == 422


def test_source_exceptions_division_filters_rows_and_totals(db):
    health, life = _connectors(db)
    _exception(db, health, 11, "INVALID_TIME")
    _exception(db, life, 12, "MALFORMED")
    _exception(db, life, 13, "IDENTITY_UNRESOLVED")
    db.commit()
    client = _client(db)
    everything = client.get("/api/v1/source-exceptions").json()
    assert everything["totals"]["all"] == 3
    life_only = client.get("/api/v1/source-exceptions", params={"division": "life"}).json()
    assert len(life_only["rows"]) == 2
    assert life_only["totals"]["all"] == 2
    assert life_only["totals"]["invalid_time"] == 0
    assert life_only["totals"]["identity_unresolved"] == 1
    assert life_only["totals"]["affected_terminals"] == 1
    health_only = client.get("/api/v1/source-exceptions", params={"division": "health"}).json()
    assert health_only["totals"]["all"] == 1 and health_only["filtered_total"] == 1


def test_source_exception_scope_carries_identity_unresolved_counts(db):
    health, _life = _connectors(db)
    job = _job(db, health, status="COMPLETED", serial=SERIAL, key="scope-job-1")
    db.commit()
    client = _client(db)
    scope = client.get("/api/v1/source-exceptions", params={"job_id": job.job_id}).json()["scope"]
    assurance = scope["source_exception_assurance"]
    assert {"identity_unresolved", "identity_unresolved_open", "open", "total"} <= set(assurance)
