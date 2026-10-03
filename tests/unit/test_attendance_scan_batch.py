"""Preview reuse must preserve per-record policy and expire every batch."""
from copy import deepcopy
import hashlib

from sqlalchemy import delete, event as sql_event, select

from test_attendance_repair import repair_store as repair_store
from test_attendance_force_release import store as store, checked
from zk_add import attendance_force_release as force
from zk_add.crypto import cnic_lookup, encrypt_cnic, encrypt_text
from zk_add.models import (AttendanceEvent, AttendanceRecoveryItem as Item,
                           AttendanceRecoveryJob as Job, AttendanceForceReleaseTask as Task,
                           Connector, DeviceUser, OrdsOutbox, IdentityTombstone)


def clone(original, index):
    values = {column.name: deepcopy(getattr(original, column.name))
              for column in AttendanceEvent.__table__.columns if column.name != "id"}
    values["event_uid"] = hashlib.sha256(f"preview-batch-{index}".encode()).hexdigest()
    return AttendanceEvent(**values)


def test_batch_reuses_shared_reads_without_reusing_record_decisions(store):
    sessions, _, _ = store
    job_id = checked(store)
    with sessions() as db:
        connector = db.scalar(select(Connector).with_for_update())
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        task = db.scalar(select(Task))
        original = db.scalar(select(AttendanceEvent))
        rows = [original] + [clone(original, index) for index in range(1, 100)]
        db.add_all(rows[1:])
        db.flush()
        rows[1].uid = "bad"
        rows[2].punch = "999"
        rows[3].captured_cnic_lookup_hash = cnic_lookup("3520299999991")
        rows[4].identity_terminal_fingerprint = "0" * 64
        db.add(OrdsOutbox(attendance_event_id=rows[5].id, status="FAILED_PERMANENT"))
        db.flush()
        # The original uncached policy remains the reference for every row.
        expected = {row.id: force.classify(db, row, connector, task) for row in rows}
        assert expected[rows[1].id][1] == "UID_INVALID"
        assert expected[rows[2].id][1] == "SOURCE_INVALID"
        assert expected[rows[3].id][1] == "CNIC_CONFLICT"
        assert expected[rows[4].id][1] == "IDENTITY_CONFLICT"
        assert expected[rows[5].id][1] == "PERMANENT_REVIEW"
        db.execute(delete(Item))
        task.cursor, task.high_water_id, task.checked_count = 0, rows[-1].id, 0
        db.flush()
        queries = []
        def record(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)
        engine = sessions.kw["bind"]
        sql_event.listen(engine, "before_cursor_execute", record)
        try:
            force._scan(db, job, task, connector)
            db.flush()
        finally:
            sql_event.remove(engine, "before_cursor_execute", record)
        assert len(queries) <= 15, f"Repeated evidence reads: {len(queries)}"
        items = db.scalars(select(Item).order_by(Item.attendance_event_id)).all()
        assert len(items) == 100
        for item in items:
            state, reason, proof = expected[item.attendance_event_id]
            assert (item.status, item.error_code, item.result["proof"]) == (state, reason, proof)


def test_next_preview_batch_and_delivery_see_new_identity_conflict(store):
    sessions, _, _ = store
    job_id = checked(store)
    with sessions() as db:
        connector = db.scalar(select(Connector).with_for_update())
        job = db.scalar(select(Job).where(Job.job_id == job_id))
        task, user = db.scalar(select(Task)), db.scalar(select(DeviceUser))
        original = db.scalar(select(AttendanceEvent))
        second = clone(original, 999)
        db.add(second)
        db.flush()
        db.execute(delete(Item))
        task.cursor, task.high_water_id = 0, original.id
        force._scan(db, job, task, connector)
        db.flush()
        assert db.scalar(select(Item.status)) == "READY"
        db.commit()
        # A new transaction must not reuse the earlier preview's negative check.
        db.add(IdentityTombstone(zkt_device_id=connector.zkt_device.id,
            device_user_id=user.id, device_serial=connector.zkt_device.serial,
            user_id=user.user_id, uid=user.uid,
            display_name_encrypted=encrypt_text("Synthetic previous identity"),
            cnic_encrypted=encrypt_cnic("3520299999991"), cnic_lookup_hash=cnic_lookup("3520299999991")))
        db.flush()
        task.high_water_id = second.id
        force._scan(db, job, task, connector)
        db.flush()
        item = db.scalar(select(Item).where(Item.attendance_event_id == second.id))
        assert item.error_code == "IDENTITY_CONFLICT" and item.status == "NEEDS_REVIEW"
        # Delivery/approval calls have no scan cache and re-read current evidence.
        assert force.identity_proof(db, original, connector, task)[1] == "IDENTITY_CONFLICT"
