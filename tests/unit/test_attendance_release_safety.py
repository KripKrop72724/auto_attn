"""A manual Oracle send never delivers an impossible time or guesses an identity.

Karachi, Oct 2026: a manual Direct ORDS release sent rows dated 2027-10-11 and
2074-08-24, sent Toheed's 9 and 14 Oct punches under the CNIC he was given on
19 Oct, and overrode punches ADD's own proof released minutes later.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from test_add_backend import (
    connector_fixture,
    db as db,
    make_writable,
)
from test_attendance_force_release import store as store
from test_attendance_repair import CORRECT_CNIC, WRONG_CNIC, repair_store as repair_store
from zk_add import attendance_direct_ords as direct
from zk_add import attendance_force_delivery as delivery
from zk_add import worker
from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
from zk_add.attendance_release_safety import time_refusal
from zk_add.crypto import cnic_lookup, encrypt_cnic, encrypt_text, mask_cnic
from zk_add.identity import (
    build_machine_name,
    clean_display_name,
    parse_machine_name,
    suspect_cnic_name,
)
from zk_add.models import (
    AttendanceEvent,
    AttendanceForceReleaseDecision as Decision,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem as Item,
    Connector,
    DeviceAlert,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import replace_user_snapshot, serialize_command, update_device_user_command
from zk_add.time_utils import utc_now

# In these fixtures the employee's CNIC today is CORRECT_CNIC. PUNCH_CNIC is
# what the terminal held for the same user ID when the older punch happened.
PUNCH_CNIC = WRONG_CNIC


def start(event_id, *, key, choice=None, send_now=False):
    return DirectOrdsStartRequest(
        event_ids=[event_id], reason="Administrator reviewed saved punch",
        password="test-admin-password", idempotency_key=f"release-safety-{key}",
        cnic_choices=[{"event_id": event_id, "choice": choice}] if choice else [],
        send_now_event_ids=[event_id] if send_now else [],
    )


def held_event(sessions):
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = event.cnic_lookup_hash = event.display_name = None
        event.ords_status = "BLOCKED_IDENTITY"
        db.commit()
        return event.id


def add_punch_time_interval(db, event, *, cnic=PUNCH_CNIC, uid=None, revoked=False):
    """The terminal's retained identity for the user ID across the punch time."""
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == event.user_id))
    zkt = db.get(Connector, event.connector_id).zkt_device
    db.add(AttendanceIdentityHistory(
        zkt_device_id=zkt.id, device_user_id=user.id, terminal_serial=event.device_serial,
        user_id=event.user_id, uid=uid or event.uid, fingerprint="f" * 64,
        cnic_encrypted=encrypt_cnic(cnic), cnic_lookup_hash=cnic_lookup(cnic),
        display_name_encrypted=encrypt_text("Toheed at punch time"),
        first_snapshot_id=zkt.identity_snapshot_id, last_snapshot_id=zkt.identity_snapshot_id,
        last_revision=zkt.identity_snapshot_revision,
        observed_from=event.device_event_time - timedelta(days=1),
        observed_until=event.device_event_time + timedelta(days=1),
        closed=True, revoked=revoked,
    ))
    db.flush()


@pytest.mark.parametrize("when, quality, expected", [
    (datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc), "OK", None),
    (datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc), "UNKNOWN", None),
    (datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc), "INVALID", "CLOCK_INVALID"),
    (datetime(2027, 10, 11, 4, 0, tzinfo=timezone.utc), "OK", "TIME_IMPOSSIBLE"),
    (datetime(2074, 8, 24, 4, 0, tzinfo=timezone.utc), "OK", "TIME_IMPOSSIBLE"),
    (datetime(2009, 12, 31, 4, 0, tzinfo=timezone.utc), "OK", "TIME_IMPOSSIBLE"),
])
def test_time_refusal_rejects_invalid_clock_and_impossible_dates(when, quality, expected):
    captured = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
    event = AttendanceEvent(
        device_event_time=when, captured_at=captured, received_at=captured, clock_quality=quality,
    )
    assert time_refusal(event) == expected


@pytest.mark.parametrize("change, code", [
    ("invalid_clock", "CLOCK_INVALID"),
    ("year_2027", "TIME_IMPOSSIBLE"),
    ("year_2074", "TIME_IMPOSSIBLE"),
])
def test_direct_send_refuses_invalid_clock_and_impossible_time(store, change, code):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = encrypt_cnic(CORRECT_CNIC)  # a saved punch CNIC
        if change == "invalid_clock":
            event.clock_quality = "INVALID"
        else:
            year = 2027 if change == "year_2027" else 2074
            event.device_event_time = event.device_event_time.replace(year=year)
        db.commit()
        event_id = event.id
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        hint = direct.identity_hints_for_page(db, [event])[event_id]
        assert hint["eligible"] is False and hint["exclusion"] == code
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["exclusion"] == code and "never sent to Oracle" in row["exclusion_reason"]
        job = direct.create(db, actor="operator", request=start(event_id, key="refuse-time"))
        db.commit()
        item = db.scalar(select(Item))
        assert job.status == "COMPLETED_WITH_REVIEW"
        assert item.status == "SKIPPED" and item.error_code == code
        assert item.result["payload_encrypted"] is None
        assert db.scalar(select(Decision)) is None


def test_existing_approval_of_invalid_clock_is_quarantined_not_sent(store, monkeypatch):
    sessions, _connector_id, _uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = encrypt_cnic(CORRECT_CNIC)
        event_id = event.id
        direct.create(db, actor="operator", request=start(event_id, key="queued-before-fix"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        # An approval saved before this check, for a row whose clock was invalid.
        db.get(AttendanceEvent, event_id).clock_quality = "INVALID"
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        decision = db.scalar(select(Decision))
        assert direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision) is None
    normal, forced = delivery.split_claims(worker.claim_ords_batch(5))
    assert normal == [] and forced == []
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        outbox = db.scalar(select(OrdsOutbox))
        item = db.scalar(select(Item))
        assert event.ords_status == outbox.status == "QUARANTINED_INVALID_DEVICE_TIME"
        assert event.oracle_confirmed_at is None and outbox.next_attempt_at is None
        assert item.status == "NEEDS_REVIEW" and item.error_code == "QUARANTINED_INVALID_DEVICE_TIME"


def test_cnic_changed_since_punch_requires_choice_and_defaults_to_punch_time(store):
    """Toheed: CNIC edited on the terminal 19 Oct; 9 and 14 Oct punches keep the old one."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        hint = direct.identity_hints_for_page(db, [event])[event_id]
        assert hint["eligible"] is True
        assert hint["punch_time_cnic"] == "DIFFERS" and hint["cnic_choice_required"] is True
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["exclusion"] is None and row["cnic_choice_required"] is True
        assert row["cnic_choices"] == ["PUNCH_TIME", "CURRENT"]
        assert row["default_cnic_choice"] == "PUNCH_TIME"
        assert row["punch_time_cnic_masked"] == mask_cnic(PUNCH_CNIC)
        assert row["current_cnic_masked"] == mask_cnic(CORRECT_CNIC)
        assert PUNCH_CNIC not in str(row) and CORRECT_CNIC not in str(row)

        # Never silently today's CNIC: an older dashboard sending no choice is skipped.
        direct.create(db, actor="operator", request=start(event_id, key="no-choice"))
        db.commit()
        skipped = db.scalar(select(Item))
        assert skipped.status == "SKIPPED" and skipped.error_code == "CNIC_CHOICE_REQUIRED"
        assert db.scalar(select(Decision)) is None

        direct.create(db, actor="operator", request=start(event_id, key="punch-time", choice="PUNCH_TIME"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        decision = db.scalar(select(Decision))
        payload = direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision)
        assert payload["cnic"] == PUNCH_CNIC
        assert payload["employee_name"] == "Toheed at punch time"
        assert decision.proof["cnic_source"] == "PUNCH_TIME_HISTORY"
        assert decision.proof["cnic_choice"] == "PUNCH_TIME"
        assert decision.proof["punch_time_cnic_hash"] == cnic_lookup(PUNCH_CNIC)
        assert event.cnic_encrypted is None  # the saved punch is never rewritten


def test_explicit_current_choice_sends_todays_cnic(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        direct.create(db, actor="operator", request=start(event_id, key="current", choice="CURRENT"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        decision = db.scalar(select(Decision))
        payload = direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision)
        assert payload["cnic"] == CORRECT_CNIC
        assert decision.proof["cnic_choice"] == "CURRENT"


@pytest.mark.parametrize("interval", ["revoked", "other_uid", "two_cnics"])
def test_ambiguous_punch_time_identity_offers_only_explicit_current(store, interval):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        if interval == "revoked":
            add_punch_time_interval(db, event, revoked=True)
        elif interval == "other_uid":
            add_punch_time_interval(db, event, uid="99")
        else:
            add_punch_time_interval(db, event)
            add_punch_time_interval(db, event, cnic="3520255555551")
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "AMBIGUOUS"
        assert row["cnic_choices"] == ["CURRENT"] and row["default_cnic_choice"] is None
        direct.create(db, actor="operator", request=start(event_id, key="punch", choice="PUNCH_TIME"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"


def test_punch_time_interval_matching_today_needs_no_choice(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id), cnic=CORRECT_CNIC)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "MATCHES_CURRENT" and not row["cnic_choice_required"]
        # The saved terminal record plus this interval is ADD's own proof
        # (RETAINED_INTERVAL): the next snapshot would release it anyway.
        assert row["auto_release_at_next_snapshot"] is True
        direct.create(db, actor="operator", request=start(event_id, key="matches", send_now=True))
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "READY" and item.result["cnic_source"] == "SYNCED_USER"
        assert item.result["cnic_choice"] is None
        assert item.result["auto_release_override"] is True


def test_legacy_approval_stops_when_punch_time_cnic_differs(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=start(event_id, key="legacy", choice="CURRENT"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        decision = db.scalar(select(Decision))
        # Shape of an approval saved before per-punch choices existed.
        decision.proof = {
            key: value for key, value in decision.proof.items()
            if key not in {"cnic_choice", "punch_time_state", "punch_time_cnic_hash"}
        }
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        connector = db.get(Connector, event.connector_id)
        decision = db.scalar(select(Decision))
        assert direct.approved_payload(db, event, connector, decision)["cnic"] == CORRECT_CNIC
        add_punch_time_interval(db, event)
        assert direct.approved_payload(db, event, connector, decision) is None


def test_punch_auto_release_proof_would_release_needs_send_now(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        user = db.scalar(select(DeviceUser))
        event.captured_cnic_lookup_hash = user.cnic_lookup_hash  # the strongest automatic proof
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["auto_release_at_next_snapshot"] is True
        assert row["database_guard"] == "NOT_APPLICABLE"
        direct.create(db, actor="operator", request=start(event_id, key="no-send-now", choice="CURRENT"))
        db.commit()
        skipped = db.scalar(select(Item))
        assert skipped.error_code == "AUTO_RELEASE_PENDING"
        assert "next user snapshot" in skipped.result["reason"]
        direct.create(db, actor="operator", request=start(
            event_id, key="send-now", choice="CURRENT", send_now=True,
        ))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
        assert db.scalar(select(Decision)).proof["auto_release_override"] is True


def test_live_punch_after_latest_snapshot_waits_for_next_snapshot(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.source = "LIVE"
        event.device_event_time = event.captured_at = event.received_at = utc_now() + timedelta(seconds=30)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["auto_release_at_next_snapshot"] is False
        assert row["awaiting_next_snapshot"] is True
        direct.create(db, actor="operator", request=start(event_id, key="wait", choice="CURRENT"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "AUTO_RELEASE_PENDING"


def test_identity_evidence_reports_guard_cnics_record_and_auto_release(store):
    from zk_add.web import attendance_identity_evidence_report

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        # A dump row before any full-history or tail read linked it (Karachi, 7-20 Oct).
        for manifest in db.scalars(select(TerminalRecordManifest)).all():
            db.delete(manifest)
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        report = attendance_identity_evidence_report(event_id, auth=(db, None))
        assert report["source_proof"] is None
        assert report["database_guard"] == "NOT_APPLICABLE"
        assert report["saved_terminal_record"] is False
        assert report["punch_time_cnic"]["state"] == "DIFFERS"
        assert report["punch_time_cnic"]["cnic_masked"] == mask_cnic(PUNCH_CNIC)
        assert report["current_cnic_masked"] == mask_cnic(CORRECT_CNIC)
        assert report["punch_time_cnic_matches_current"] is False
        assert report["auto_release_at_next_snapshot"] is False
        codes = [hint["code"] for hint in report["hints"]]
        assert "NO_SAVED_TERMINAL_RECORD" in codes and "PUNCH_TIME_CNIC_DIFFERS" in codes
        assert PUNCH_CNIC not in str(report) and CORRECT_CNIC not in str(report)

        event = db.get(AttendanceEvent, event_id)
        event.captured_cnic_lookup_hash = db.scalar(select(DeviceUser)).cnic_lookup_hash
        db.flush()
        report = attendance_identity_evidence_report(event_id, auth=(db, None))
        assert report["auto_release_eligible"] is True
        assert report["auto_release_at_next_snapshot"] is True
        assert "AUTO_RELEASE_AT_NEXT_SNAPSHOT" in [hint["code"] for hint in report["hints"]]

        event.clock_quality = "INVALID"
        db.flush()
        report = attendance_identity_evidence_report(event_id, auth=(db, None))
        assert report["time_refusal"] == "CLOCK_INVALID"
        assert report["auto_release_at_next_snapshot"] is False


@pytest.mark.parametrize("name, reason, display", [
    ("A.Saleem-A4230111028815", "LETTER_BEFORE_DIGITS", "A.Saleem"),
    ("A.Saleem-AB4230111028815", "LETTER_BEFORE_DIGITS", "A.Saleem"),
    ("Ali Khan-42301-1102881-5", "SEPARATED_DIGITS", "Ali Khan"),
    ("Ali Khan-S-42301-1102881-5", "SEPARATED_DIGITS", "Ali Khan"),
    ("Ali Khan 4230111028815", "UNSUPPORTED_SEPARATOR", "Ali Khan"),
])
def test_suspect_cnic_names_are_flagged_and_never_parsed(name, reason, display):
    assert parse_machine_name(name).cnic is None
    suspect = suspect_cnic_name(name)
    assert suspect.reason == reason and suspect.display_name == display
    assert "4230111028815" not in repr(suspect)
    assert clean_display_name(name) == display


@pytest.mark.parametrize("name", [
    "Ali Khan-4230111028815", "Ali Khan-S-4230111028815", "Ali Khan", "Ward 12",
    "Ali-03001234567", "", None,
])
def test_valid_or_plain_names_are_not_suspect(name):
    assert suspect_cnic_name(name) is None


def test_setting_cnic_keeps_display_name_clean_within_terminal_limit():
    display = clean_display_name("A.Saleem-A4230111028815")
    assert build_machine_name(display_name=display, cnic="4230111028815") == "A.Saleem-4230111028815"
    assert build_machine_name(
        display_name="A.Saleem-A4230111028815", cnic="4230111028815",
    ) == "A.Saleem-A-4230111028815"  # what the clean name prevents


def snapshot_names(db, connector, rows):
    replace_user_snapshot(db, connector=connector, snapshot=UserSnapshotRequest(
        snapshot_id=f"names-{utc_now().timestamp()}", complete=True, stable=True,
        observed_at=utc_now(),
        users=[UserSnapshotRow(uid=uid, user_id=user_id, name=name) for uid, user_id, name in rows],
    ))
    db.flush()


def test_suspect_cnic_raises_alert_and_operator_cnic_cleans_name(db):
    from zk_add.web import device_user_identity_integrity, serialize_user

    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_names(db, connector, [
        ("55", "04048", "A.Saleem-A4230111028815"),
        ("56", "04049", "Bilal-3520212345671"),
    ])
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "USER_CNIC_FORMAT_SUSPECT"))
    assert alert.state == "OPEN" and alert.severity == "WARNING"
    assert alert.details == {"affected_users": 1, "user_ids": ["04048"]}
    assert "4230111028815" not in str(alert.details) + alert.message
    saleem = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "04048"))
    assert saleem.cnic_lookup_hash is None  # never auto-used
    shown = serialize_user(saleem, zkt=connector.zkt_device)
    assert shown["cnic_format_suspect"] is True
    assert shown["cnic_format_suspect_reason"] == "LETTER_BEFORE_DIGITS"
    assert shown["suggested_display_name"] == "A.Saleem"
    assert device_user_identity_integrity(db, zkt=connector.zkt_device)[0]["cnic_format_suspect_users"] == 1
    bilal = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "04049"))
    assert serialize_user(bilal, zkt=connector.zkt_device)["cnic_format_suspect"] is False

    command = update_device_user_command(
        db, connector=connector, user=saleem,
        display_name="A.Saleem-A4230111028815", cnic="4230111028815",
        shift_worker=False, privilege=0, expected_version=saleem.row_version,
        idempotency_key="saleem-cnic-confirmed-by-hr", actor="StateHealthAdmin",
    )
    wire = serialize_command(command)
    assert wire["desired_state"]["display_name"] == "A.Saleem"
    assert wire["desired_state"]["machine_name"] == wire["payload"]["name"] == "A.Saleem-4230111028815"

    snapshot_names(db, connector, [
        ("55", "04048", "A.Saleem-4230111028815"),
        ("56", "04049", "Bilal-3520212345671"),
    ])
    db.refresh(alert)
    assert alert.state != "OPEN"
