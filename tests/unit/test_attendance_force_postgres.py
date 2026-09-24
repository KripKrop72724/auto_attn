"""Manual approval races and rollback protection against actual PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from threading import Barrier

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from test_attendance_repair import repair_store as repair_store
from test_safe_attendance_repair import store as store
from test_safe_attendance_repair_postgres import postgres_store as postgres_store
from test_attendance_force_release import checked, approve, tick
from zk_add import attendance_force_release as force, attendance_force_delivery as delivery
from zk_add import attendance_direct_ords as direct
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.attendance_force_schemas import ForceCheckRequest
from zk_add.models import (
    Connector,
    AttendanceEvent,
    AttendanceRecoveryJob as Job,
    AttendanceForceReleaseDecision as Decision,
)
from zk_add.settings import settings
from zk_add.worker import claim_ords_batch


@pytest.fixture()
def force_pg(postgres_store, monkeypatch):
    sessions, connector_id = postgres_store
    monkeypatch.setattr(settings, "attendance_force_release_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_force_release_execution_enabled", True)
    monkeypatch.setattr(settings, "attendance_force_release_allowed_connectors", "")
    with sessions() as db:
        connector = db.scalar(select(Connector))
        connector.connected = True
        connector.boot_id = "force-postgres-boot"
        connector.zkt_device.confirmed_serial = connector.zkt_device.serial
        event = db.scalar(select(AttendanceEvent))
        event.raw_event = {
            **event.raw_event,
            "trusted_capture_terminal_serial": connector.zkt_device.serial,
        }
        db.commit()
        uid = event.event_uid
    path = (
        Path(__file__).parents[2]
        / "apps/add_backend/migrations/versions/20260923_0035_manual_force_release.py"
    )
    spec = spec_from_file_location("force_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with sessions.kw["bind"].begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.install_delivery_guards()
    return sessions, connector_id, uid


def test_concurrent_duplicate_checks_are_one_saved_request(force_pg):
    sessions, connector_id, _ = force_pg
    # Seed singleton exactly as production migration does.
    with sessions() as db:
        force._lock_scheduler(db)
        db.commit()
    barrier = Barrier(2)

    def submit(_):
        barrier.wait(timeout=10)
        with sessions() as db:
            job = force.create_check(
                db,
                actor="operator",
                request=ForceCheckRequest(
                    scope="SELECTED",
                    connector_ids=[connector_id],
                    idempotency_key="concurrent-force-check",
                ),
            )
            db.commit()
            return job.job_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert len(set(results)) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(Job.id))) == 1


def test_direct_ords_approval_survives_postgres_guard_and_requires_content_receipt(force_pg):
    from zk_add.models import OrdsOutbox, AttendanceRecoveryItem as Item

    sessions, _connector_id, _uid = force_pg
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = None
        event.display_name = "Captured employee"
        event_id = event.id
        db.commit()
    with sessions() as db:
        direct.create(
            db, actor="operator",
            request=DirectOrdsStartRequest(
                event_ids=[event_id], reason="Reviewed conflicting saved punch",
                password="test-password", idempotency_key="direct-pg-approval",
            ),
        )
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        assert db.scalar(select(Decision)).proof["policy"] == direct.POLICY
        assert db.scalar(select(Decision)).proof["cnic_source"] == "SYNCED_USER"
        assert db.scalar(select(OrdsOutbox)).status == "PENDING"
        assert db.scalar(select(Item)).status == "WAITING_ORACLE"
        # The deferred trigger must reject a false ACK without Oracle content proof.
        db.scalar(select(OrdsOutbox)).status = "ACKED_CHECK"
        db.get(AttendanceEvent, event_id).ords_status = "ACKED_CHECK"
        with pytest.raises(DBAPIError, match="matching Oracle content verification"):
            db.commit()


def test_conflict_diagnostics_separate_terminal_namespaces_without_exporting_identity(force_pg):
    import json
    from test_attendance_repair import WRONG_CNIC, CORRECT_CNIC
    from zk_add.crypto import encrypt_cnic, cnic_lookup
    from zk_add.models import (
        AttendanceIdentityHistory, AttendanceRecoveryItem as Item,
        AttendanceForceReleaseTask as Task, DeviceUser,
    )
    from zk_add.time_utils import utc_now

    path = Path(__file__).parents[2] / "scripts/attendance_repair_diagnostics.py"
    spec = spec_from_file_location("force_diagnostics", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    sessions, _, _ = force_pg
    checked(force_pg)
    with sessions() as db:
        item, task, user = db.scalar(select(Item)), db.scalar(select(Task)), db.scalar(select(DeviceUser))
        item.error_code, item.status = "IDENTITY_CONFLICT", "NEEDS_REVIEW"
        history = dict(
            zkt_device_id=user.zkt_device_id, device_user_id=user.id,
            user_id=user.user_id, uid=user.uid,
            cnic_encrypted=encrypt_cnic(WRONG_CNIC), cnic_lookup_hash=cnic_lookup(WRONG_CNIC),
            first_snapshot_id=task.snapshot_id, last_snapshot_id=task.snapshot_id,
            last_revision=1, observed_from=utc_now(), observed_until=utc_now(),
        )
        db.add(AttendanceIdentityHistory(**history, terminal_serial="REPLACED-TERMINAL"))
        db.flush()
        rows = db.execute(text(module.FORCE_CONFLICT_SQL)).mappings().all()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["history_other_terminal"] is True
        assert row["history_current_or_unknown_terminal"] is False
        assert set(row) == {
            "run_id", "item_id", "current_conflict", "captured_user_changed",
            "captured_conflict", "captured_fingerprint_changed", "other_user_row",
            "tombstone_current_or_unknown_terminal", "tombstone_other_terminal",
            "history_current_or_unknown_terminal", "history_other_terminal", "baseline_changed",
        }
        assert all(isinstance(v, bool) for k, v in row.items() if k not in {"run_id", "item_id"})
        output = json.dumps(row)
        assert all(value not in output for value in (user.display_name, WRONG_CNIC, CORRECT_CNIC))
        db.add(AttendanceIdentityHistory(**history, terminal_serial=task.terminal_serial))
        db.flush()
        assert db.execute(text(module.FORCE_CONFLICT_SQL)).mappings().one()["history_current_or_unknown_terminal"]


def test_old_application_sql_cannot_reopen_held_attendance(force_pg):
    sessions, _, _ = force_pg
    with pytest.raises(DBAPIError, match="explicit administrator approval"):
        with sessions() as db:
            db.execute(
                text(
                    "UPDATE add_attendance_events SET ords_status='PENDING', manual_release_required=false"
                )
            )
            db.commit()
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        assert event.ords_status == "BLOCKED_IDENTITY" and event.manual_release_required


def test_atomic_force_approval_and_content_receipt_survive_database_guards(force_pg):
    sessions, _, _ = force_pg
    job_id = checked(force_pg)
    approve(force_pg, job_id)
    tick(sessions, rollback=True)
    with sessions() as db:
        assert db.scalar(select(func.count(Decision.id))) == 0
    tick(sessions)
    claims = claim_ords_batch(1)
    _, forced = delivery.split_claims(claims)
    assert len(forced) == 1
    with pytest.raises(DBAPIError, match="content verification"):
        with sessions() as db:
            db.execute(text("UPDATE add_attendance_events SET ords_status='ACKED_CHECK'"))
            db.commit()
    delivery.persist_result(forced[0], "MATCH", "f" * 64)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Job)).status == "COMPLETED"
        assert db.scalar(select(AttendanceEvent)).oracle_confirmed_at is not None


def test_competing_approved_checks_have_one_owner(force_pg):
    from zk_add.attendance_recovery import RecoveryError

    sessions, _, _ = force_pg
    ids = [checked(force_pg, key=f"overlap-pg-{index}") for index in range(2)]
    barrier = Barrier(2)

    def submit(identifier):
        with sessions() as db:
            job = db.scalar(select(Job).where(Job.job_id == identifier))
            signature = force.serialize(db, job, "operator")["signature"]
            barrier.wait(timeout=10)
            try:
                force.start(
                    db,
                    job,
                    actor="operator",
                    signature=signature,
                    reason="Verified current employee",
                    key="approve-" + identifier,
                )
                db.commit()
                return "APPROVED"
            except RecoveryError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ids)) == ["APPROVED", "RELEASE_ALREADY_RUNNING"]


def test_hundred_thousand_backlog_keeps_login_and_live_delivery_responsive(force_pg, monkeypatch):
    from copy import deepcopy
    import hashlib
    from threading import Event
    from time import monotonic
    from fastapi.testclient import TestClient
    from sqlalchemy import literal, String
    from zk_add import web
    from zk_add.models import DeviceUser, OrdsOutbox, AttendanceRecoveryItem as Item
    from zk_add.security import hash_admin_password
    from test_attendance_force_release import sync

    sessions, connector_id, _ = force_pg
    with sessions() as db:
        table = AttendanceEvent.__table__
        columns = [c.name for c in table.columns if c.name != "id"]
        source = table.alias("original")
        series = func.generate_series(1, 99999).table_valued("value").render_derived()
        values = [
            func.concat(
                func.md5(series.c.value.cast(String)),
                func.md5(literal("force-load-") + series.c.value.cast(String)),
            )
            if name == "event_uid"
            else source.c[name]
            for name in columns
        ]
        db.execute(
            table.insert().from_select(
                columns, select(*values).select_from(source.join(series, literal(True)))
            )
        )
        db.commit()
        assert db.scalar(select(func.count(AttendanceEvent.id))) == 100000
        job = force.create_check(
            db,
            actor="operator",
            request=ForceCheckRequest(
                scope="SELECTED", connector_ids=[connector_id], idempotency_key="large-force-check"
            ),
        )
        job_id = job.job_id
        db.commit()
    tick(sessions)
    tick(sessions)
    sync(sessions)
    tick(sessions)
    scanning = Event()
    durations = []

    def scan():
        for _ in range(1002):
            scanning.set()
            started = monotonic()
            tick(sessions)
            durations.append(monotonic() - started)
            with sessions() as db:
                if db.scalar(select(Job.status).where(Job.job_id == job_id)) == "CHECKED":
                    return
        raise AssertionError("The bounded scan did not finish")

    monkeypatch.setattr(web, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "admin_username", "force-load-admin")
    monkeypatch.setattr(
        settings, "admin_password_hash", hash_admin_password("force-load-test-password")
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(scan)
        assert scanning.wait(10)
        client = TestClient(web.app)
        for _ in range(3):
            start = monotonic()
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "force-load-admin", "password": "force-load-test-password"},
            )
            assert response.status_code == 200, response.text
            assert monotonic() - start < 3, "Backlog transactions blocked login"
        work.result(timeout=600)
    assert max(durations) < 10, "A batch exceeded the bounded transaction budget"
    with sessions() as db:
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        assert force.counts(db, job)["ready"] == 100000
    approve(force_pg, job_id)
    tick(sessions)  # Exactly 100 decisions/outbox rows, while the rest stay held.
    with sessions() as db:
        original = db.scalar(select(AttendanceEvent).order_by(AttendanceEvent.id))
        user = db.scalar(select(DeviceUser))
        data = {
            c.name: deepcopy(getattr(original, c.name)) for c in table.columns if c.name != "id"
        }
        data.update(
            source="LIVE",
            manual_release_required=False,
            identity_resolution_status="RESOLVED",
            cnic_encrypted=user.cnic_encrypted,
            cnic_lookup_hash=user.cnic_lookup_hash,
            ords_status="PENDING",
        )
        for index in range(20):
            row = AttendanceEvent(
                **{**data, "event_uid": hashlib.sha256(f"force-live-{index}".encode()).hexdigest()}
            )
            db.add(row)
            db.flush()
            db.add(OrdsOutbox(attendance_event_id=row.id, status="PENDING", delivery_type="LIVE"))
        db.commit()
        assert db.scalar(select(func.count(Decision.id))) == 100
        assert db.scalar(select(func.count(Item.id)).where(Item.status == "READY")) == 99900
    for _ in range(4):
        claims = claim_ords_batch(5)
        assert len(claims) == 5
        assert sum(payload["capturetype"] == "LIVE" for _, payload, _, _ in claims) == 4
        assert sum(payload["capturetype"] != "LIVE" for _, payload, _, _ in claims) == 1


def test_populated_upgrade_backfills_holds_and_rollback_preserves_approval(
    postgres_store, monkeypatch
):
    """Exercise 0034-shaped tables, not only current create_all metadata."""
    from sqlalchemy import inspect
    from zk_add.models import AttendanceSafeRepairDecision

    sessions, connector_id = postgres_store
    engine = sessions.kw["bind"]
    path = (
        Path(__file__).parents[2]
        / "apps/add_backend/migrations/versions/20260923_0035_manual_force_release.py"
    )
    spec = spec_from_file_location("force_upgrade", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        # The fixture has a real held punch and user list before the upgrade.
        for suffix in ("controls", "decisions", "users", "tasks", "scheduler"):
            conn.execute(text("DROP TABLE add_attendance_force_release_" + suffix))
        conn.execute(text("UPDATE add_attendance_events SET ords_status='FIRMWARE_RECEIPT_UNVERIFIED', identity_resolution_status='BLOCKED_PROVENANCE'"))
        conn.execute(text("ALTER TABLE add_attendance_events DROP COLUMN manual_release_required"))
        conn.execute(
            text("ALTER TABLE add_attendance_events DROP COLUMN captured_cnic_lookup_hash")
        )
        conn.execute(text("ALTER TABLE add_device_user_snapshots DROP COLUMN connector_boot_id"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
    assert "add_attendance_force_release_decisions" in inspect(engine).get_table_names()
    monkeypatch.setattr(settings, "attendance_force_release_preview_enabled", True)
    monkeypatch.setattr(settings, "attendance_force_release_execution_enabled", True)
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        assert event.manual_release_required and event.ords_status == "BLOCKED_IDENTITY"
        connector = db.scalar(select(Connector))
        connector.connected, connector.boot_id = True, "upgraded-test-boot"
        connector.zkt_device.confirmed_serial = connector.zkt_device.serial
        event.raw_event = {
            **event.raw_event,
            "trusted_capture_terminal_serial": connector.zkt_device.serial,
        }
        db.commit()
        uid = event.event_uid
    fixture = (sessions, connector_id, uid)
    job_id = checked(fixture)
    approve(fixture, job_id)
    tick(sessions)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
    with sessions() as db:
        assert db.scalar(select(func.count(Decision.id))) == 1
        assert db.scalar(select(AttendanceEvent)).ords_status == "PENDING"
        assert db.scalar(select(func.count(AttendanceSafeRepairDecision.id))) == 0
    with pytest.raises(DBAPIError, match="content verification"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE add_attendance_events SET ords_status='ACKED'"))
