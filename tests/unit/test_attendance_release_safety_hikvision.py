"""The manual Oracle send's punch-time identity checks on Hikvision terminals.

Hikvision identities come from ISAPI profile scans, which record the same
retained identity intervals as a ZKT user snapshot once both scans match.
A punch held before ADD knew the employee profile has no record at the punch
time and needs an explicit confirmation; a punch carrying its own CNIC is
sent with it, and a punch a profile record covers needs no choice.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from test_hikvision_delivery import payload, policy, publish_profile
from test_hikvision_evidence import db as evidence_db
from zk_add import attendance_direct_ords as direct
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.crypto import decrypt_json
from zk_add.hikvision_evidence import preserve_observation
from zk_add.models import AttendanceEvent, AttendanceIdentityHistory, AttendanceRecoveryItem as Item
from zk_add.time_utils import ensure_utc

PROFILE_CNIC = "1234512345671"


@pytest.fixture
def db(monkeypatch):
    yield from evidence_db.__wrapped__(monkeypatch)


def held_before_profile(session, connector):
    """A plain-name punch saved before the employee profile scan published."""
    policy(session, connector)
    preserve_observation(session, connector, payload(name="Plain historic name"))
    session.commit()
    publish_profile(session, connector)
    event = session.scalar(select(AttendanceEvent))
    assert event.ords_status == "BLOCKED_IDENTITY" and event.cnic_encrypted is None
    return event


def request(session, event_id, key, *, choice=None):
    digest = direct.preview(session, [event_id])["rows"][0]["evidence_digest"] if choice else None
    return DirectOrdsStartRequest(
        event_ids=[event_id], reason="HR confirmed the employee", password="test-password",
        idempotency_key=f"hikvision-release-safety-{key}",
        cnic_choices=[{"event_id": event_id, "choice": choice, "evidence_digest": digest}] if choice else [],
    )


def test_profile_scan_records_the_identity_history_the_send_reads(db):
    session, connector = db
    held_before_profile(session, connector)
    record = session.scalar(select(AttendanceIdentityHistory))
    assert record is not None and record.user_id == "00111" and record.terminal_serial == "terminal"


def test_hikvision_punch_before_the_profile_needs_an_explicit_confirmation(db):
    session, connector = db
    event = held_before_profile(session, connector)
    row = direct.preview(session, [event.id])["rows"][0]
    assert row["exclusion"] is None
    assert row["punch_time_cnic"] == "NONE" and row["record_position"] == "BEFORE_FIRST_RECORD"
    assert "PUNCH_BEFORE_FIRST_OBSERVATION" in row["evidence_signals"]
    assert row["cnic_choices"] == ["CURRENT"] and row["decision_kind"] == "CONFIRM_TODAY"
    assert PROFILE_CNIC not in str(row)

    direct.create(session, actor="operator", request=request(session, event.id, "unconfirmed"))
    session.commit()
    assert session.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"

    direct.create(session, actor="operator", request=request(session, event.id, "confirmed", choice="CURRENT"))
    session.commit()
    ready = session.scalars(select(Item).order_by(Item.id.desc())).first()
    assert ready.status == "READY" and ready.result["cnic_source"] == "SYNCED_USER"
    assert decrypt_json(ready.result["payload_encrypted"])["cnic"] == PROFILE_CNIC


def test_hikvision_punch_with_its_own_cnic_needs_no_choice(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(name="Test-S-1234512345671"))
    session.commit()
    event = session.scalar(select(AttendanceEvent))
    publish_profile(session, connector)
    row = direct.preview(session, [event.id])["rows"][0]
    assert row["cnic_source"] == "SAVED_PUNCH" and row["cnic_choice_required"] is False
    assert row["punch_time_cnic"] is None


def test_hikvision_punch_covered_by_a_profile_record_needs_no_choice(db):
    session, connector = db
    event = held_before_profile(session, connector)
    record = session.scalar(select(AttendanceIdentityHistory))
    record.observed_from = ensure_utc(event.device_event_time) - timedelta(days=1)
    session.commit()
    row = direct.preview(session, [event.id])["rows"][0]
    assert row["punch_time_cnic"] == "MATCHES_CURRENT"
    assert row["cnic_choice_required"] is False and row["decision_kind"] is None
