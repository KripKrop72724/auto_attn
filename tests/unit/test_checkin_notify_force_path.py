"""An administrator-approved (forced) Oracle delivery records one notification row.

The row carries the approved payload's CNIC: the one Oracle received. No
notification is sent here.
"""

from sqlalchemy import delete, func, select

from test_attendance_force_release import approve, checked, tick, store as store  # noqa: F401
from test_attendance_repair import CORRECT_CNIC, repair_store as repair_store  # noqa: F401
from zk_add import attendance_force_delivery as delivery
from zk_add.crypto import decrypt_cnic
from zk_add.models import AttendanceCheckinNotification as Notification, AttendanceEvent
from zk_add.worker import claim_ords_batch


def test_forced_delivery_match_records_one_row_with_the_approved_cnic(store):
    sessions, _, _ = store
    # The fixture saves the punch as already confirmed; start from a punch
    # confirmed before this feature, which has no notification row.
    with sessions() as db:
        db.execute(delete(Notification))
        db.commit()
    approve(store, checked(store))
    tick(sessions)
    _ordinary, forced = delivery.split_claims(claim_ords_batch(1))
    assert len(forced) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(Notification.id))) == 0
    delivery.persist_result(forced[0], "MATCH", "c" * 64)
    delivery.persist_result(forced[0], "MATCH", "c" * 64)
    with sessions() as db:
        rows = db.scalars(select(Notification)).all()
        event = db.scalar(select(AttendanceEvent))
    assert len(rows) == 1 and rows[0].attendance_event_id == event.id
    assert event.ords_status == "ACKED_CHECK"
    assert rows[0].confirmation_path == event.oracle_confirmation_path
    assert decrypt_cnic(rows[0].cnic_encrypted) == forced[0]["payload"]["cnic"] == CORRECT_CNIC
