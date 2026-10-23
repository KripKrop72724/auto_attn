"""One-click reconcile: start, bulk start, cancel, archive and clear history."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import zk_add.hikvision_reconciliation  # noqa: F401  (registers its tables for create_all)
from zk_add.db import Base
from zk_add.models import (
    AuditEvent,
    Connector,
    ReconciliationChunk,
    ReconciliationCoverage,
    ReconciliationEvent,
    ReconciliationJob,
    TerminalRecordManifest,
)
from zk_add.reconciliation import (
    archive_reconciliation_job,
    cancel_and_archive_reconciliation_job,
    clear_reconciliation_history,
    control_reconciliation_job,
    create_bulk_reconciliation_jobs,
    create_reconciliation_job,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.service import onboard_connector
from zk_add.settings import settings
from zk_add.web import app, get_db


def _eligible(session: Session, *, mac: str, device_id: str, serial: str, zone: str = "ZONE-ONE") -> Connector:
    connector, _token, _created = onboard_connector(
        session, hardware_id=mac, zone_id=zone, zone_name=zone, device_id=device_id,
        firmware_version="2.3.0", expected_serial=serial, actor="test", ip_address="127.0.0.1",
    )
    connector.connected = True
    connector.lifecycle_state = "ONLINE"
    zkt = connector.zkt_device
    zkt.serial = zkt.confirmed_serial = serial
    zkt.online = True
    zkt.connection_state = "ONLINE"
    zkt.certification_state = "CERTIFIED"
    zkt.snapshot_complete = True
    zkt.identity_snapshot_stable = True
    zkt.capability_profile = {"history_stream_v1": True, "history_range_resume_verified": True}
    session.flush()
    return connector


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, monkeypatch):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy")
    monkeypatch.setattr(settings, "reconciliation_enabled", True)
    monkeypatch.setattr(settings, "identity_snapshot_gate_enabled", False)
    admin = schema = None
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
    else:
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "one_click_reconcile_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=30000"})
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
    try:
        with Session(engine) as session:
            connector = _eligible(session, mac="e0:72:a1:d6:f3:28", device_id="1", serial="ONECLICK0001")
            session.commit()
            yield session, connector
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def _client(session: Session):
    raw, admin = create_admin_session(session, username="StateHealthAdmin", ip_address="127.0.0.1",
                                      user_agent="pytest")
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw)
    return client, {"X-CSRF-Token": admin.csrf_token}


def _finished(session, connector, *, status="CANCELLED", key=None) -> ReconciliationJob:
    job = ReconciliationJob(
        connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
        reason="History row", idempotency_key=key or str(uuid4()), request_digest="a" * 64,
        status=status, terminal_serial=connector.zkt_device.serial,
    )
    session.add(job)
    session.flush()
    session.add(ReconciliationEvent(job_id=job.id, state=status, details={}))
    session.flush()
    return job


def test_one_click_start_and_cancel_need_no_reason_password_or_confirmation(db):
    session, connector = db
    client, headers = _client(session)
    url = f"/api/v1/devices/{connector.connector_id}/reconciliations/full-history"
    started = client.post(url, json={"idempotency_key": "one-click-start-1"}, headers=headers)
    assert started.status_code == 201, started.text
    job = started.json()
    assert job["status"] == "QUEUED" and job["archived_at"] is None
    row = session.scalar(select(ReconciliationJob).where(ReconciliationJob.job_id == job["job_id"]))
    assert row.reason == "One-click reconcile"
    # A lost-response replay returns the same job; an old tab's extra fields are ignored.
    replay = client.post(url, json={"idempotency_key": "one-click-start-1", "reason": "x", "password": "wrong",
                                    "confirmation": "anything"}, headers=headers)
    assert replay.status_code == 201 and replay.json()["job_id"] == job["job_id"]
    # Still one active job per terminal.
    second = client.post(url, json={"idempotency_key": "one-click-start-2"}, headers=headers)
    assert second.status_code == 409 and "already has active reconciliation" in second.json()["detail"]
    # CSRF still guards the mutation.
    assert client.post(url, json={"idempotency_key": "one-click-start-3"}).status_code in {401, 403}

    cancelled = client.post(f"/api/v1/reconciliations/{job['job_id']}/cancel",
                            json={"idempotency_key": "one-click-cancel-1"}, headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    session.refresh(row)
    assert row.wait_reason == "One-click cancel by operator"
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.action == "FULL_HISTORY_RECONCILIATION_CANCEL")) == 1


def test_service_create_ignores_a_stale_typed_confirmation(db):
    session, connector = db
    job = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="svc-key-0001",
                                    confirmation="RECONCILE wrong FROM START")
    again = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="svc-key-0001")
    assert again.job_id == job.job_id and job.reason == "One-click reconcile"


def test_bulk_start_isolates_each_terminal(db):
    session, first = db
    second = _eligible(session, mac="e0:72:a1:d6:f3:29", device_id="2", serial="ONECLICK0002", zone="ZONE-TWO")
    blocked = _eligible(session, mac="e0:72:a1:d6:f3:30", device_id="3", serial="ONECLICK0003", zone="ZONE-TWO")
    blocked.firmware_version = "2.2.0"
    running = _eligible(session, mac="e0:72:a1:d6:f3:31", device_id="4", serial="ONECLICK0004")
    session.commit()
    existing = create_reconciliation_job(session, connector=running, actor="test", idempotency_key="existing-0001")
    session.commit()
    ids = [first.connector_id, blocked.connector_id, running.connector_id, second.connector_id, "missing-terminal"]
    result = create_bulk_reconciliation_jobs(session, connector_ids=ids, actor="ops", idempotency_key="bulk-key-0001")
    session.commit()
    outcomes = {row["connector_id"]: row for row in result["results"]}
    assert [row["connector_id"] for row in result["results"]] == ids
    assert result["created"] == 2
    assert outcomes[first.connector_id]["outcome"] == outcomes[second.connector_id]["outcome"] == "CREATED"
    assert outcomes[second.connector_id]["zone_id"] == "ZONE-TWO"
    assert outcomes[blocked.connector_id]["outcome"] == "BLOCKED"
    assert "2.3.0" in outcomes[blocked.connector_id]["message"]
    assert outcomes[running.connector_id] == {**outcomes[running.connector_id], "outcome": "ALREADY_ACTIVE",
                                              "job_id": existing.job_id}
    assert outcomes["missing-terminal"]["outcome"] == "BLOCKED"
    # The blocked terminal rolled back alone; the others' jobs are committed.
    for connector in (first, second):
        assert session.scalar(select(func.count(ReconciliationJob.id)).where(
            ReconciliationJob.connector_id == connector.id)) == 1
    assert session.scalar(select(func.count(ReconciliationJob.id)).where(
        ReconciliationJob.connector_id == blocked.id)) == 0
    # A replay returns the same jobs instead of reporting them as already active.
    replay = create_bulk_reconciliation_jobs(session, connector_ids=ids, actor="ops", idempotency_key="bulk-key-0001")
    assert {row["connector_id"]: (row["outcome"], row["job_id"]) for row in replay["results"]}[first.connector_id] \
        == ("CREATED", outcomes[first.connector_id]["job_id"])
    # Hikvision-only scopes on a ZKT terminal are reported, not raised.
    tail = create_bulk_reconciliation_jobs(session, connector_ids=[blocked.connector_id], actor="ops",
                                           idempotency_key="bulk-key-0002", scope="RECENT_TAIL")
    assert tail["created"] == 0 and "Hikvision" in tail["results"][0]["message"]


def test_bulk_route_publishes_and_validates(db):
    session, connector = db
    client, headers = _client(session)
    response = client.post("/api/v1/reconciliations/bulk", headers=headers, json={
        "connector_ids": [connector.connector_id], "idempotency_key": "bulk-route-0001"})
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    assert response.json()["results"][0]["outcome"] == "CREATED"
    duplicate = client.post("/api/v1/reconciliations/bulk", headers=headers, json={
        "connector_ids": [connector.connector_id, connector.connector_id], "idempotency_key": "bulk-route-0002"})
    assert duplicate.status_code == 422
    long_key = "k" * 120
    long = client.post("/api/v1/reconciliations/bulk", headers=headers, json={
        "connector_ids": [connector.connector_id], "idempotency_key": long_key})
    assert long.status_code == 200 and long.json()["results"][0]["outcome"] == "ALREADY_ACTIVE"


def test_archive_hides_finished_jobs_and_refuses_open_ones(db):
    session, connector = db
    client, headers = _client(session)
    old = _finished(session, connector, status="COMPLETED")
    active = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="archive-0001")
    session.commit()
    refused = client.post(f"/api/v1/reconciliations/{active.job_id}/archive", headers=headers)
    assert refused.status_code == 409 and "cancel it before archiving" in refused.json()["detail"]
    archived = client.post(f"/api/v1/reconciliations/{old.job_id}/archive", headers=headers, json={})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] and archived.json()["archived_by"] == "StateHealthAdmin"
    again = client.post(f"/api/v1/reconciliations/{old.job_id}/archive", headers=headers)
    assert again.status_code == 200 and again.json()["archived_at"] == archived.json()["archived_at"]
    assert session.scalar(select(func.count(AuditEvent.id)).where(
        AuditEvent.action == "RECONCILIATION_ARCHIVED")) == 1

    listed = client.get("/api/v1/reconciliations").json()
    assert [row["job_id"] for row in listed["rows"]] == [active.job_id]
    assert listed["totals"]["all"] == 1 and listed["totals"]["completed"] == 0 and listed["filtered_total"] == 1
    everything = client.get("/api/v1/reconciliations", params={"include_archived": True}).json()
    assert [row["job_id"] for row in everything["rows"]] == [active.job_id, old.job_id]
    assert everything["totals"]["completed"] == 1
    single = client.get(f"/api/v1/reconciliations/{old.job_id}")
    assert single.status_code == 200 and single.json()["archived_at"]

    both = client.post(f"/api/v1/reconciliations/{active.job_id}/cancel-and-archive", headers=headers,
                       json={"idempotency_key": "cancel-archive-0001"})
    assert both.status_code == 200, both.text
    assert both.json()["status"] == "CANCELLED" and both.json()["archived_at"]
    assert client.get("/api/v1/reconciliations").json()["rows"] == []


def test_cancel_and_archive_service_is_one_transaction(db):
    session, connector = db
    job = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="cancel-arch-0002")
    session.commit()
    with pytest.raises(ValueError, match="cancel it before archiving"):
        archive_reconciliation_job(session, job=job, actor="ops")
    done = cancel_and_archive_reconciliation_job(session, job=job, actor="ops", idempotency_key="cancel-arch-key")
    assert done.status == "CANCELLED" and done.archived_by == "ops"
    assert session.scalar(select(ReconciliationEvent.state).where(
        ReconciliationEvent.job_id == job.id, ReconciliationEvent.idempotency_key == "cancel-arch-key")) == "CANCELLED"


def test_clear_history_cancels_deletes_bare_jobs_and_archives_evidence(db):
    session, connector = db
    zkt = connector.zkt_device
    bare = _finished(session, connector, status="CANCELLED")
    session.add(ReconciliationChunk(job_id=bare.id, generation=1, sequence=0, start_ordinal=0, end_ordinal=0,
                                    record_count=0, chunk_digest="c" * 64, resulting_chain_digest="d" * 64))
    covered = _finished(session, connector, status="COMPLETED")
    session.add(ReconciliationCoverage(
        zkt_device_id=zkt.id, job_id=covered.id, terminal_serial=zkt.serial, terminal_generation=1,
        certified_source_cursor=1, source_chain_digest="e" * 64, capture_state="CERTIFIED",
        oracle_state="CERTIFIED", active=False))
    manifested = _finished(session, connector, status="FAILED")
    session.add(TerminalRecordManifest(
        job_id=manifested.id, connector_id=connector.id, zkt_device_id=zkt.id, terminal_serial=zkt.serial,
        generation=1, ordinal=0, raw_record_digest="f" * 64, terminal_record_key="0" * 64, disposition="EVENT"))
    # A bare parent whose recovery child is kept stays too; a bare chain is deleted whole.
    kept_parent = _finished(session, connector)
    kept_child = _finished(session, connector, status="COMPLETED")
    kept_child.recovery_parent_job_id = kept_parent.id
    session.add(ReconciliationCoverage(
        zkt_device_id=zkt.id, job_id=kept_child.id, terminal_serial=zkt.serial, terminal_generation=1,
        certified_source_cursor=1, source_chain_digest="e" * 64, capture_state="CERTIFIED",
        oracle_state="CERTIFIED", active=False))
    gone_parent = _finished(session, connector)
    gone_child = _finished(session, connector)
    gone_child.recovery_parent_job_id = gone_parent.id
    already = _finished(session, connector, status="COMPLETED")
    already.archived_at = already.requested_at
    session.commit()
    active = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="clear-0001")
    session.commit()
    audits_before = session.scalar(select(func.count(AuditEvent.id)))
    gone = {bare.id, gone_parent.id, gone_child.id, active.id}

    result = clear_reconciliation_history(session, actor="ops", request_id="clear-history-0001", batch_size=2)
    assert result == {"cancelled": 1, "deleted": 4, "archived": 4, "kept_reasons": {
        "SOURCE_COVERAGE": 2, "TERMINAL_RECORD_MANIFEST": 1, "RECOVERY_PARENT": 1}, "kept_running": {}, "busy": 0}
    session.expire_all()
    remaining = {row.id: row for row in session.scalars(select(ReconciliationJob))}
    assert set(remaining) == {covered.id, manifested.id, kept_parent.id, kept_child.id, already.id}
    assert all(row.archived_at is not None for row in remaining.values())
    assert remaining[covered.id].archived_by == "ops"
    assert not session.scalar(select(func.count(ReconciliationEvent.id)).where(ReconciliationEvent.job_id.in_(gone)))
    assert not session.scalar(select(func.count(ReconciliationChunk.id)))
    # Audit rows are never removed: one cancel audit and one clear audit were added.
    assert session.scalar(select(func.count(AuditEvent.id))) == audits_before + 2
    assert session.scalar(select(AuditEvent.after).where(
        AuditEvent.action == "RECONCILIATION_HISTORY_CLEARED")) == result

    assert clear_reconciliation_history(session, actor="ops", request_id="clear-history-0001") == result
    second = clear_reconciliation_history(session, actor="ops")
    assert second == {"cancelled": 0, "deleted": 0, "archived": 0, "kept_reasons": {}, "kept_running": {}, "busy": 0}
    assert session.scalar(select(func.count(ReconciliationJob.id))) == 5


def test_clear_history_keeps_live_tail_capture_and_an_open_repair_source_running(db, monkeypatch):
    from zk_add import reconciliation
    from zk_add.models import AttendanceRepairJob

    session, connector = db
    tail_terminal = _eligible(session, mac="e0:72:a1:d6:f3:40", device_id="40", serial="ONECLICK0040")
    plain_terminal = _eligible(session, mac="e0:72:a1:d6:f3:41", device_id="41", serial="ONECLICK0041")
    repair_source = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="keep-0001")
    tail = create_reconciliation_job(session, connector=tail_terminal, actor="test", idempotency_key="keep-0002")
    plain = create_reconciliation_job(session, connector=plain_terminal, actor="test", idempotency_key="keep-0003")
    session.add(AttendanceRepairJob(
        connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
        idempotency_key="repair-keep-0001", request_digest="b" * 64, status="PREPARING_SOURCE",
        source_reconciliation_job_id=repair_source.id))
    session.commit()
    # A Hikvision recent-tail job keeps following its terminal: live capture, not history.
    monkeypatch.setattr(reconciliation, "hikvision_tail_job", lambda _session, job: job.id == tail.id)

    result = clear_reconciliation_history(session, actor="ops", request_id="clear-keep-0001")
    assert result["cancelled"] == 1
    assert result["kept_running"] == {"HIKVISION_TAIL_CAPTURE": 1, "ATTENDANCE_REPAIR_SOURCE": 1}
    session.expire_all()
    # Cancelled with nothing load-bearing on it, so the history sweep deleted it.
    assert session.get(ReconciliationJob, plain.id) is None
    for job in (repair_source, tail):
        kept = session.get(ReconciliationJob, job.id)
        assert kept.status not in {"CANCELLED", "COMPLETED", "FAILED", "INVALIDATED"} and kept.archived_at is None


class _LockTimeout(Exception):
    sqlstate = "55P03"


def _busy() -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE", {}, _LockTimeout("canceling statement due to lock timeout"))


def test_clear_history_skips_a_busy_job_and_the_next_call_finishes_it(db, monkeypatch):
    from zk_add import reconciliation

    session, connector = db
    other = _eligible(session, mac="e0:72:a1:d6:f3:50", device_id="50", serial="ONECLICK0050")
    busy_job = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="busy-0001")
    free_job = create_reconciliation_job(session, connector=other, actor="test", idempotency_key="busy-0002")
    session.commit()
    real = reconciliation.control_reconciliation_job

    def held(session, *, job, **kwargs):
        if job.id == busy_job.id:
            raise _busy()  # A device's own transaction holds this connector row.
        return real(session, job=job, **kwargs)

    monkeypatch.setattr(reconciliation, "control_reconciliation_job", held)
    first = clear_reconciliation_history(session, actor="ops", request_id="busy-clear-0001")
    assert (first["cancelled"], first["busy"]) == (1, 1)
    session.expire_all()
    assert session.get(ReconciliationJob, busy_job.id).status not in {"CANCELLED", "COMPLETED"}
    assert session.get(ReconciliationJob, free_job.id) is None  # cancelled, then deleted as bare history

    monkeypatch.setattr(reconciliation, "control_reconciliation_job", real)
    second = clear_reconciliation_history(session, actor="ops", request_id="busy-clear-0002")
    assert (second["cancelled"], second["busy"]) == (1, 0)
    assert session.get(ReconciliationJob, busy_job.id) is None


def test_clear_history_survives_a_real_postgres_lock_timeout(db):
    session, connector = db
    if session.get_bind().dialect.name != "postgresql":
        pytest.skip("Row lock timeouts are PostgreSQL behaviour")
    job = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="pg-busy-0001")
    session.commit()
    holder = Session(session.get_bind())
    try:
        # Another transaction (a device's heartbeat) holds the connector row.
        holder.execute(text("SELECT id FROM add_connectors WHERE id = :id FOR UPDATE"), {"id": connector.id})
        session.execute(text("SET lock_timeout = '300ms'"))
        result = clear_reconciliation_history(session, actor="ops", request_id="pg-busy-clear-0001")
        assert (result["cancelled"], result["busy"]) == (0, 1)
    finally:
        holder.rollback()
        holder.close()
    session.expire_all()
    assert session.get(ReconciliationJob, job.id).status == "QUEUED"
    finished = clear_reconciliation_history(session, actor="ops", request_id="pg-busy-clear-0002")
    assert (finished["cancelled"], finished["busy"]) == (1, 0)


def test_clear_history_route_answers_busy_not_500(db, monkeypatch):
    from zk_add import web

    session, _connector = db
    client, headers = _client(session)

    def busy(*_args, **_kwargs):
        raise _busy()

    monkeypatch.setattr(web, "clear_reconciliation_history", busy)
    response = client.post("/api/v1/reconciliations/clear-history", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ADD_BUSY"


def test_clear_history_route(db):
    session, connector = db
    client, headers = _client(session)
    _finished(session, connector)
    session.commit()
    response = client.post("/api/v1/reconciliations/clear-history", headers=headers,
                           json={"idempotency_key": "clear-route-0001"})
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert client.post("/api/v1/reconciliations/clear-history", headers=headers).json()["deleted"] == 0
    listed = client.get("/api/v1/reconciliations").json()
    assert listed["rows"] == [] and listed["totals"]["all"] == 0


def test_control_without_reason_keeps_default_text(db):
    session, connector = db
    job = create_reconciliation_job(session, connector=connector, actor="test", idempotency_key="pause-0001")
    paused = control_reconciliation_job(session, job=job, action="pause", actor="ops", idempotency_key="pause-key-1")
    assert paused.status == "PAUSED" and paused.wait_reason == "One-click pause by operator"
