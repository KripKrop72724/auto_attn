from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from test_attendance_repair import repair_store as repair_store, CORRECT_CNIC
from test_attendance_force_release import store as store, tick
from zk_add import attendance_force_release as force
from zk_add import web
from zk_add.attendance_force_schemas import ForceCheckRequest
from zk_add.models import (
    AttendanceForceReleaseTask as Task, AttendanceRecoveryItem as Item,
    Connector, DeviceCommand, DeviceCommandEvent, DeviceUserSnapshot,
)
from zk_add.schemas import Envelope
from zk_add.time_utils import utc_now


@pytest.fixture()
def refresh(store, monkeypatch):
    sessions, connector_id, _ = store
    with sessions() as db:
        force.create_check(db, actor="operator", request=ForceCheckRequest(
            scope="SELECTED", connector_ids=[connector_id], idempotency_key="ordered-sync-check",
        ))
        db.commit()
    tick(sessions)
    tick(sessions)
    with sessions() as db:
        task = db.scalar(select(Task))
        command = db.get(DeviceCommand, task.sync_command_id)
        connector = db.get(Connector, task.connector_id)
        pk, command_id, boot = connector.id, command.command_id, connector.boot_id
        sequence = connector.last_sequence

    @contextmanager
    def scope():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(web, "session_scope", scope)
    device_time = utc_now() - timedelta(hours=2)

    def send(kind, *, offset=0, observed_offset=None, snapshot_id=None, boot_id=None):
        nonlocal sequence
        sequence += 1
        when = device_time + timedelta(seconds=offset)
        if kind == "user_snapshot":
            payload = {
                "snapshot_id": snapshot_id or str(uuid4()), "complete": True, "stable": True,
                "observed_at": (device_time + timedelta(seconds=offset if observed_offset is None else observed_offset)).isoformat(),
                "users": [{"uid": "7", "user_id": "1007", "name": f"Correct Name-{CORRECT_CNIC}"}],
            }
        else:
            payload = {"command_id": command_id, "status": kind, "result": {}}
        envelope = Envelope(
            schema_version="2", message_id=str(uuid4()), connector_id=connector_id,
            boot_id=boot_id or boot, seq=sequence, sent_at=when,
            type="user_snapshot" if kind == "user_snapshot" else "command_update", payload=payload,
        )
        return web.persist_envelope(pk, envelope)

    return sessions, send


@pytest.mark.parametrize("late_ack", [False, True])
def test_authenticated_refresh_with_skewed_clock_proves_fresh_roster(refresh, late_ack):
    sessions, send = refresh
    send("RUNNING")
    if late_ack:
        send("ACKNOWLEDGED", offset=1)
    send("user_snapshot", offset=2)
    send("SUCCEEDED", offset=3)
    tick(sessions)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).status == "CHECKED"
        assert db.scalar(select(func.count(Item.id)).where(Item.status == "READY")) == 1
        assert db.scalar(select(func.count(DeviceCommandEvent.id)).where(
            DeviceCommandEvent.status.like("SYNC_READ_%"))) == 3


@pytest.mark.parametrize("fault", [
    "cached_bytes", "clock_backward", "clock_jump", "wrong_boot", "missing_begin",
    "missing_finish", "finish_before_roster", "replayed_snapshot_id", "changed_saved_evidence",
    "damaged_begin",
])
def test_uncertain_or_replayed_clock_skewed_roster_cannot_be_approved(refresh, fault):
    sessions, send = refresh
    snapshot_id = str(uuid4())
    if fault == "replayed_snapshot_id":
        send("user_snapshot", offset=-2, snapshot_id=snapshot_id)
    if fault != "missing_begin":
        send("RUNNING")
    if fault == "damaged_begin":
        with sessions() as db:
            db.scalar(select(DeviceCommandEvent).where(
                DeviceCommandEvent.status == "SYNC_READ_BEGIN")).details = {}
            db.commit()
    if fault == "finish_before_roster":
        send("SUCCEEDED", offset=1)
    send("user_snapshot", offset=2,
         observed_offset=-120 if fault == "cached_bytes" else -1 if fault == "clock_backward" else 2,
         snapshot_id=snapshot_id, boot_id="replacement-boot" if fault == "wrong_boot" else None)
    if fault not in {"missing_finish", "finish_before_roster"}:
        send("SUCCEEDED", offset=700 if fault == "clock_jump" else 3,
             boot_id="replacement-boot" if fault == "wrong_boot" else None)
    if fault == "changed_saved_evidence":
        with sessions() as db:
            snapshot = db.scalar(select(DeviceUserSnapshot).order_by(DeviceUserSnapshot.id.desc()))
            snapshot.observed_at -= timedelta(minutes=5)
            db.commit()
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).status in {"SYNCING", "UNAVAILABLE"}
        assert db.scalar(select(func.count(Item.id))) == 0


def test_snapshot_commit_failure_keeps_ordering_evidence_uncommitted(refresh, monkeypatch):
    from zk_add import attendance_sync_evidence

    sessions, send = refresh
    send("RUNNING")
    original = attendance_sync_evidence.record_roster

    def fail_after_append(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(attendance_sync_evidence, "record_roster", fail_after_append)
    with pytest.raises(RuntimeError, match="injected"):
        send("user_snapshot", offset=2)
    send("SUCCEEDED", offset=3)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).status == "SYNCING"
        assert not db.scalar(select(DeviceCommandEvent.id).where(
            DeviceCommandEvent.status == "SYNC_READ_ROSTER"))


def test_cached_snapshot_id_is_rejected_even_when_its_clock_looks_fresh(refresh):
    sessions, send = refresh
    snapshot_id = str(uuid4())
    # The fixture's clock starts two hours behind; these frames are near server
    # time. Replaying the pre-command roster must not use the clock fallback.
    send("user_snapshot", offset=7200, snapshot_id=snapshot_id)
    send("RUNNING", offset=7201)
    send("user_snapshot", offset=7202, observed_offset=7200, snapshot_id=snapshot_id)
    send("SUCCEEDED", offset=7203)
    tick(sessions)
    with sessions() as db:
        assert db.scalar(select(Task)).status == "SYNCING"
        assert db.scalar(select(func.count(Item.id))) == 0
