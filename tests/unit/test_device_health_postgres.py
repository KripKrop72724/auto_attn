"""Device health lock order against real PostgreSQL row locks, in a disposable schema."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.orm import sessionmaker

from zk_add import worker
from zk_add.db import Base
from zk_add.models import Connector, DeviceAlert
from zk_add.security import ADMIN_COOKIE, create_admin_session
from zk_add.service import onboard_connector, upsert_alert
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.web import app, get_db


@pytest.fixture()
def postgres(monkeypatch):
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "device_health_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        # Keep this database's DDL metadata independent of the ORM (see
        # test_safe_attendance_repair_postgres.py).
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr("zk_add.db.SessionLocal", sessions)
        monkeypatch.setattr(worker, "PROCESS_STARTED_AT", utc_now() - timedelta(hours=1))
        with sessions() as db:
            connector, _, _ = onboard_connector(
                db, hardware_id="e0:72:a1:00:00:42", zone_id="TEST", zone_name="Test", device_id="TEST",
                firmware_version="2.5.2", expected_serial="HEALTH-SERIAL", actor="test", ip_address="127.0.0.1")
            connector.connected, connector.lifecycle_state = True, "ONLINE"
            connector.last_seen_at = utc_now() - timedelta(seconds=60)
            db.commit()
            connector_id = connector.id
        yield sessions, connector_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_sweep_skips_connector_locked_by_heartbeat_and_never_overwrites_fresh_last_seen(postgres):
    sessions, connector_id = postgres
    with sessions() as heartbeat:
        connector = heartbeat.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        connector.last_seen_at = utc_now()
        heartbeat.flush()
        # The sweep runs while the heartbeat holds the row: it must skip it.
        assert worker.mark_stale_connectors(utc_now()) == []
        heartbeat.commit()
    assert worker.mark_stale_connectors(utc_now()) == []  # re-checked: now fresh
    with sessions() as db:
        connector = db.get(Connector, connector_id)
        assert connector.connected and connector.lifecycle_state == "ONLINE"
        connector.last_seen_at = utc_now() - timedelta(seconds=60)
        db.commit()
    assert [row["state"] for row in worker.mark_stale_connectors(utc_now())] == ["OFFLINE"]


def test_acknowledge_waits_for_heartbeat_lock_and_keeps_acknowledged_by(postgres):
    sessions, connector_id = postgres
    with sessions() as db:
        connector = db.get(Connector, connector_id)
        alert = upsert_alert(db, connector, code="ESP_LOCAL_FAILURE", severity="HIGH", message="LED",
                             details={"led_state": "LOCAL_FAILURE"})
        raw_session, admin = create_admin_session(db, username=settings.admin_username, ip_address="127.0.0.1",
                                                  user_agent="pytest")
        db.commit()
        alert_id, csrf = alert.id, admin.csrf_token

    def request_session():  # web.get_db with this schema's sessions
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = request_session
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    with sessions() as heartbeat, ThreadPoolExecutor(max_workers=1) as pool:
        connector = heartbeat.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        upsert_alert(heartbeat, connector, code="ESP_LOCAL_FAILURE", severity="HIGH", message="LED again",
                     details={"led_state": "LOCAL_FAILURE", "boot_id": "refreshed"})
        heartbeat.flush()
        pending = pool.submit(client.post, f"/api/v1/alerts/{alert_id}/acknowledge",
                              json={"note": "on site"}, headers={"X-CSRF-Token": csrf})
        time.sleep(0.5)
        assert not pending.done()  # serialized behind the heartbeat's connector lock
        heartbeat.commit()
        response = pending.result(timeout=10)
    assert response.status_code == 200, response.text
    with sessions() as db:
        row = db.get(DeviceAlert, alert_id)
        assert row.state == "OPEN" and row.details["boot_id"] == "refreshed"
        assert row.details["acknowledged_by"] == settings.admin_username
        assert row.details["acknowledgement_note"] == "on site"


def test_operator_resolve_serializes_with_heartbeat_without_deadlock(postgres, monkeypatch):
    from zk_add.security import hash_admin_password

    sessions, connector_id = postgres
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    with sessions() as db:
        connector = db.get(Connector, connector_id)
        alert = upsert_alert(db, connector, code="ESP_DELIVERY_WORKER_FAULT", severity="HIGH", message="worker",
                             details={"binding": "INFERRED_PREVIOUS"})
        raw_session, admin = create_admin_session(db, username=settings.admin_username, ip_address="127.0.0.1",
                                                  user_agent="pytest")
        db.commit()
        alert_id, csrf = alert.id, admin.csrf_token

    def request_session():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = request_session
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    with sessions() as heartbeat, ThreadPoolExecutor(max_workers=1) as pool:
        connector = heartbeat.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        upsert_alert(heartbeat, connector, code="ZKT_CLOCK_DRIFT", severity="WARNING", message="drift")
        connector.last_seen_at = utc_now()
        heartbeat.flush()
        pending = pool.submit(client.post, f"/api/v1/alerts/{alert_id}/resolve", headers={"X-CSRF-Token": csrf},
                              json={"reason": "Raised on an ended recovery boot", "password": "correct-password",
                                    "idempotency_key": "postgres-resolve-1"})
        time.sleep(0.5)
        assert not pending.done()  # the operator waits for the heartbeat's connector lock
        heartbeat.commit()
        response = pending.result(timeout=10)
    assert response.status_code == 200, response.text
    with sessions() as db:
        row = db.get(DeviceAlert, alert_id)
        assert row.state == "RESOLVED" and row.details["resolution"]["kind"] == "OPERATOR"
        assert db.scalar(select(DeviceAlert).where(DeviceAlert.code == "ZKT_CLOCK_DRIFT")).state == "OPEN"


@pytest.mark.parametrize("changes_scope", [False, True])
def test_cleanup_apply_racing_heartbeat_serializes_or_reports_scope_changed(postgres, changes_scope):
    from zk_add.device_health_cleanup import CleanupError, apply_plan, preview
    from zk_add.schemas import HealthCleanupApplyRequest

    sessions, connector_id = postgres
    with sessions() as db:
        connector = db.get(Connector, connector_id)
        stranded = upsert_alert(db, connector, code="ESP_DELIVERY_WORKER_FAULT", severity="HIGH",
                                message="worker", details={"binding": "INFERRED_PREVIOUS"})
        connector.last_error_code = "ESP_DELIVERY_WORKER_FAULT"
        db.commit()
        scope, alert_id = [connector.connector_id], stranded.id
        plan = preview(db, connector_ids=scope, actor="admin")
    body = HealthCleanupApplyRequest(
        connector_ids=scope, digest=plan["digest"], expires_at=plan["expires_at"], signature=plan["signature"],
        alert_ids=[alert_id], error_fix_connector_ids=[], reason="Stranded recovery-run worker fault",
        typed_confirmation="RESOLVE 1 ALERTS ON 1 DEVICES", password="unused", idempotency_key="postgres-cleanup")

    def run():
        with sessions() as db:
            try:
                result = apply_plan(db, body=body, actor="admin", ip_address=None)
            except CleanupError as error:
                return error.detail["code"]
            db.commit()
            return result

    with sessions() as heartbeat, ThreadPoolExecutor(max_workers=1) as pool:
        connector = heartbeat.scalar(select(Connector).where(Connector.id == connector_id).with_for_update())
        connector.last_seen_at = utc_now()
        if changes_scope:
            connector.boot_id = "a-new-boot"
        heartbeat.flush()
        pending = pool.submit(run)
        time.sleep(0.5)
        assert not pending.done()  # apply locks the connector first and waits
        heartbeat.commit()
        outcome = pending.result(timeout=10)
    with sessions() as db:
        state = db.get(DeviceAlert, alert_id).state
    if changes_scope:
        assert (outcome, state) == ("SCOPE_CHANGED", "OPEN")
    else:
        assert outcome["resolved_alert_ids"] == [alert_id] and state == "RESOLVED"
