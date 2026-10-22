"""ADD-owned ZKT Oracle confirmations record exactly one check-in notification row.

Runs on SQLite and, when configured, PostgreSQL (the shared ``store`` fixture).
No notification is sent here: only the outbox row is checked.
"""

import asyncio

from sqlalchemy import func, select

import test_zkt_membership_delivery as membership
import test_zkt_oracle_delivery as base
from zk_add import zkt_oracle_delivery as delivery
from zk_add.crypto import decrypt_cnic
from zk_add.models import AttendanceCheckinNotification as Notification, AttendanceEvent

store = base.store
membership_store = membership.membership_store


def only_row(sessions) -> Notification:
    with sessions() as db:
        assert db.scalar(select(func.count(Notification.id))) == 1
        return db.scalar(select(Notification))


def test_content_receipt_confirmation_records_one_row_with_the_delivered_cnic(store):
    value = base.claim(store)
    delivery.persist_result(value, "UNKNOWN")
    with store() as db:
        assert db.scalar(select(func.count(Notification.id))) == 0
    base.retry_now(store)
    value = base.claim(store)
    delivery.persist_result(value, "MATCH", "1" * 64)
    delivery.persist_result(value, "MATCH", "1" * 64)  # A replayed result.
    row = only_row(store)
    with store() as db:
        event = db.scalar(select(AttendanceEvent))
    assert row.attendance_event_id == event.id and row.status == "PENDING"
    assert row.confirmation_path == "ADD_ZKT_PROJECTION_V2"
    assert decrypt_cnic(row.cnic_encrypted) == value["payload"]["cnic"]


def test_uid_membership_confirmation_records_one_row(membership_store, monkeypatch):
    value = base.claim(membership_store)
    membership.verifier(monkeypatch)
    classification, proof = asyncio.run(delivery.verify(value))
    delivery.persist_result(value, classification, proof)
    row = only_row(membership_store)
    assert row.confirmation_path == "ADD_ZKT_UID_ONLY_V1" and row.status == "PENDING"
    assert decrypt_cnic(row.cnic_encrypted) == value["payload"]["cnic"]


def test_a_held_or_missing_result_records_nothing(store):
    value = base.claim(store)
    delivery.persist_result(value, "MISSING")
    delivery.persist_result(value, "IDENTITY_HOLD")
    with store() as db:
        assert db.scalar(select(func.count(Notification.id))) == 0
