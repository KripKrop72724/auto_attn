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


def start(db, event_id, *, key, choice=None, send_now=False, held_ok=False, digest=None):
    """A send request whose CNIC choice echoes the evidence the preview shows now."""
    if choice and digest is None:
        digest = direct.preview(db, [event_id])["rows"][0]["evidence_digest"] or "0" * 64
    return DirectOrdsStartRequest(
        event_ids=[event_id], reason="Administrator reviewed saved punch",
        password="test-admin-password", idempotency_key=f"release-safety-{key}",
        cnic_choices=[{
            "event_id": event_id, "choice": choice, "evidence_digest": digest,
            "held_by_other_confirmed": held_ok,
        }] if choice else [],
        send_now_event_ids=[event_id] if send_now else [],
    )


def clone_event(db, event, **changes):
    """Another saved punch like ``event`` (a fresh event UID)."""
    from uuid import uuid4

    from sqlalchemy import inspect as sa_inspect

    values = {
        column.key: getattr(event, column.key)
        for column in sa_inspect(AttendanceEvent).column_attrs if column.key != "id"
    }
    values.update(event_uid=uuid4().hex + uuid4().hex, **changes)
    row = AttendanceEvent(**values)
    db.add(row)
    db.flush()
    return row


def held_event(sessions):
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.cnic_encrypted = event.cnic_lookup_hash = event.display_name = None
        event.ords_status = "BLOCKED_IDENTITY"
        db.commit()
        return event.id


def add_interval(db, event, *, cnic, start, end, closed=True, revoked=False, uid=None, name="Retained name"):
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == event.user_id))
    zkt = db.get(Connector, event.connector_id).zkt_device
    db.add(AttendanceIdentityHistory(
        zkt_device_id=zkt.id, device_user_id=user.id, terminal_serial=event.device_serial,
        user_id=event.user_id, uid=uid or event.uid, fingerprint="f" * 64,
        cnic_encrypted=encrypt_cnic(cnic), cnic_lookup_hash=cnic_lookup(cnic),
        display_name_encrypted=encrypt_text(name),
        first_snapshot_id=zkt.identity_snapshot_id, last_snapshot_id=zkt.identity_snapshot_id,
        last_revision=zkt.identity_snapshot_revision,
        observed_from=event.device_event_time + start, observed_until=event.device_event_time + end,
        closed=closed, revoked=revoked,
    ))
    db.flush()


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


def punch_time_record(db):
    return db.scalar(select(AttendanceIdentityHistory).where(
        AttendanceIdentityHistory.cnic_lookup_hash == cnic_lookup(PUNCH_CNIC),
    ))


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
        job = direct.create(db, actor="operator", request=start(db, event_id, key="refuse-time"))
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
        direct.create(db, actor="operator", request=start(db, event_id, key="queued-before-fix"))
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
        direct.create(db, actor="operator", request=start(db, event_id, key="no-choice"))
        db.commit()
        skipped = db.scalar(select(Item))
        assert skipped.status == "SKIPPED" and skipped.error_code == "CNIC_CHOICE_REQUIRED"
        assert db.scalar(select(Decision)) is None

        direct.create(db, actor="operator", request=start(db, event_id, key="punch-time", choice="PUNCH_TIME"))
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
        direct.create(db, actor="operator", request=start(db, event_id, key="current", choice="CURRENT"))
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


def test_a_changed_cnic_choice_cannot_reuse_the_same_request_key(store):
    from zk_add.attendance_recovery import RecoveryError

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        job = direct.create(db, actor="operator", request=start(db, event_id, key="same-key", choice="PUNCH_TIME"))
        assert direct.create(
            db, actor="operator", request=start(db, event_id, key="same-key", choice="PUNCH_TIME"),
        ).id == job.id
        with pytest.raises(RecoveryError, match="request key"):
            direct.create(db, actor="operator", request=start(db, event_id, key="same-key", choice="CURRENT"))
        assert job.scope["cnic_choices"] == {str(event_id): "PUNCH_TIME"}


def test_choices_and_send_now_must_name_selected_punches():
    with pytest.raises(ValueError, match="one CNIC per selected punch"):
        DirectOrdsStartRequest(
            event_ids=[1], reason="Reviewed", password="x", idempotency_key="release-safety-validate",
            cnic_choices=[{"event_id": 2, "choice": "CURRENT", "evidence_digest": "a" * 64}],
        )
    with pytest.raises(ValueError, match="one CNIC per selected punch"):
        DirectOrdsStartRequest(
            event_ids=[1], reason="Reviewed", password="x", idempotency_key="release-safety-validate",
            cnic_choices=[
                {"event_id": 1, "choice": "CURRENT", "evidence_digest": "a" * 64},
                {"event_id": 1, "choice": "PUNCH_TIME", "evidence_digest": "a" * 64},
            ],
        )
    with pytest.raises(ValueError, match="sending now only for selected"):
        DirectOrdsStartRequest(
            event_ids=[1], reason="Reviewed", password="x", idempotency_key="release-safety-validate",
            send_now_event_ids=[3],
        )
    with pytest.raises(ValueError):
        DirectOrdsStartRequest(
            event_ids=[1], reason="Reviewed", password="x", idempotency_key="release-safety-validate",
            cnic_choices=[{"event_id": 1, "choice": "WHOEVER_HOLDS_THE_ID", "evidence_digest": "a" * 64}],
        )


@pytest.mark.parametrize("interval", ["revoked", "other_uid", "two_cnics"])
def test_another_cnic_at_punch_time_is_refused_never_sent_as_today(store, interval):
    """Safety review 1: user ID 100 was Ali (another UID, CNIC P) at punch time and is now Babar.

    The only evidence names a different CNIC, so neither a bulk "today's CNIC"
    tick nor an explicit CURRENT choice may send Babar's CNIC for Ali's punch.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        if interval == "revoked":
            # A Toheed-style old interval revoked by a transient duplicate-CNIC conflict.
            add_punch_time_interval(db, event, revoked=True)
        elif interval == "other_uid":
            add_punch_time_interval(db, event, uid="99")
        else:
            add_punch_time_interval(db, event)
            add_punch_time_interval(db, event, cnic="3520255555551")
        db.commit()
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        hint = direct.identity_hints_for_page(db, [event])[event_id]
        assert hint["eligible"] is False and hint["exclusion"] == "CNIC_CONFLICT"
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CONFLICT"
        assert row["exclusion"] == "CNIC_CONFLICT" and "another CNIC" in row["exclusion_reason"]
        assert row["cnic_choices"] == [] and row["default_cnic_choice"] is None
        assert row["decision_kind"] is None
        assert mask_cnic(PUNCH_CNIC) in row["other_cnics_masked"]
        assert PUNCH_CNIC not in str(row)
        if interval == "other_uid":
            assert "OTHER_UID_RECORD" in row["evidence_signals"]
        for choice in ("CURRENT", "PUNCH_TIME"):
            direct.create(db, actor="operator", request=start(db, event_id, key=f"conflict-{choice}", choice=choice))
            db.commit()
        items = db.scalars(select(Item)).all()
        assert [item.error_code for item in items] == ["CNIC_CONFLICT", "CNIC_CONFLICT"]
        assert all(item.result["payload_encrypted"] is None for item in items)
        assert db.scalar(select(Decision)) is None


def test_revoked_interval_with_todays_cnic_needs_an_explicit_per_punch_choice(store):
    """Safety review 3: a revoked record never counts as "matches today's CNIC"."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id), cnic=CORRECT_CNIC, revoked=True)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "AMBIGUOUS" and row["cnic_choice_required"] is True
        assert row["cnic_choices"] == ["CURRENT"] and row["default_cnic_choice"] is None
        assert row["evidence_signals"] == ["REVOKED_RECORD"]
        # Confirmed one punch at a time, never in a terminal-wide bulk tick.
        assert row["decision_kind"] == "CONFIRM_TODAY" and row["decision_group"] == f"row|{event_id}"
        direct.create(db, actor="operator", request=start(db, event_id, key="revoked-no-choice"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"


def test_unresolved_identity_conflict_is_shown_and_needs_explicit_choice(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_punch_time_interval(db, event, cnic=CORRECT_CNIC)
        db.scalar(select(DeviceUser)).identity_conflict_code = "DUPLICATE_USER_CNIC"
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "AMBIGUOUS"
        assert row["identity_conflict_code"] == "DUPLICATE_USER_CNIC"
        assert "IDENTITY_CONFLICT_UNRESOLVED" in row["evidence_signals"]
        assert row["decision_group"] == f"row|{event_id}"


@pytest.mark.parametrize("with_interval", [True, False])
def test_terminal_captured_cnic_that_differs_from_today_refuses_the_send(store, with_interval):
    """Safety review 2 / correctness 2: the terminal's own captured CNIC is evidence.

    Toheed's 19 Oct edit: LIVE punches captured as …609-0 from the cached
    roster while the new …609-2 interval covers them.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.captured_cnic_lookup_hash = cnic_lookup(PUNCH_CNIC)
        if with_interval:
            add_punch_time_interval(db, event, cnic=CORRECT_CNIC)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CONFLICT" and row["exclusion"] == "CNIC_CONFLICT"
        assert "CAPTURED_CNIC_DIFFERS" in row["evidence_signals"]
        direct.create(db, actor="operator", request=start(db, event_id, key="captured", choice="CURRENT", send_now=True))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CONFLICT"


def test_terminal_captured_cnic_equal_to_today_matches_without_a_choice(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        db.get(AttendanceEvent, event_id).captured_cnic_lookup_hash = cnic_lookup(CORRECT_CNIC)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "MATCHES_CURRENT" and row["cnic_choice_required"] is False


@pytest.mark.parametrize("evidence", ["tombstone", "tombstone_no_cnic", "linked_user"])
def test_another_holder_of_the_user_id_at_punch_time_refuses_the_send(store, evidence):
    """A deleted identity, or the terminal user the punch was linked to at ingest."""
    from zk_add.models import IdentityTombstone

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        zkt = db.get(Connector, event.connector_id).zkt_device
        other = DeviceUser(
            zkt_device_id=zkt.id, user_key="previous-holder", uid="99", user_id=event.user_id,
            display_name="Previous holder", cnic_encrypted=encrypt_cnic(PUNCH_CNIC),
            cnic_lookup_hash=cnic_lookup(PUNCH_CNIC), present=False, lifecycle_state="DELETED",
        )
        db.add(other)
        db.flush()
        if evidence == "linked_user":
            event.device_user_id = other.id
        else:
            db.add(IdentityTombstone(
                zkt_device_id=zkt.id, device_user_id=other.id, device_serial=zkt.serial,
                uid="99", user_id=event.user_id, display_name_encrypted=encrypt_text("Previous holder"),
                cnic_encrypted=None if evidence == "tombstone_no_cnic" else encrypt_cnic(PUNCH_CNIC),
                cnic_lookup_hash=None if evidence == "tombstone_no_cnic" else cnic_lookup(PUNCH_CNIC),
            ))
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CONFLICT" and row["exclusion"] == "CNIC_CONFLICT"
        assert ("PUNCH_LINKED_TO_OTHER_USER" if evidence == "linked_user" else "PREVIOUS_HOLDER_DELETED") in (
            row["evidence_signals"]
        )


def test_a_holder_deleted_before_the_punch_is_not_evidence(store):
    from zk_add.models import IdentityTombstone

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        zkt = db.get(Connector, event.connector_id).zkt_device
        db.add(IdentityTombstone(
            zkt_device_id=zkt.id, device_user_id=db.scalar(select(DeviceUser)).id + 100,
            device_serial=zkt.serial, uid="99", user_id=event.user_id,
            display_name_encrypted=encrypt_text("Earlier holder"),
            cnic_encrypted=encrypt_cnic(PUNCH_CNIC), cnic_lookup_hash=cnic_lookup(PUNCH_CNIC),
            created_at=event.device_event_time - timedelta(days=30),
        ))
        db.commit()
    with sessions() as db:
        assert direct.preview(db, [event_id])["rows"][0]["punch_time_cnic"] == "NONE"


def test_punch_in_a_gap_between_different_cnics_is_chosen_per_punch_without_a_default(store):
    """Correctness 4: a CNIC edited between two snapshots leaves the punch in a gap.

    The records on either side disagree (the old CNIC before, today's after),
    so the administrator chooses for this punch alone, with nothing selected.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_interval(db, event, cnic=PUNCH_CNIC, start=-timedelta(days=5), end=-timedelta(minutes=1))
        add_interval(db, event, cnic=CORRECT_CNIC, start=timedelta(minutes=2), end=timedelta(days=1), closed=False)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CHANGED_NEAR_PUNCH"
        assert row["record_position"] == "BETWEEN_RECORDS"
        assert row["cnic_choices"] == ["PUNCH_TIME", "CURRENT"] and row["default_cnic_choice"] is None
        assert row["punch_time_cnic_basis"] == "NEAREST_RECORD"
        assert row["punch_time_cnic_masked"] == mask_cnic(PUNCH_CNIC)
        assert row["other_cnics_masked"] == [mask_cnic(PUNCH_CNIC)]
        assert row["decision_kind"] == "CHOOSE_CNIC" and row["decision_group"] == f"row|{event_id}"
        direct.create(db, actor="operator", request=start(db, event_id, key="gap-none"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"
        direct.create(db, actor="operator", request=start(db, event_id, key="gap-old", choice="PUNCH_TIME"))
        db.commit()
    ticks(sessions, 1)
    with sessions() as db:
        payload = approved(db, event_id)
        assert payload["cnic"] == PUNCH_CNIC and payload["employee_name"] == "Retained name"


def test_no_record_rows_are_confirmed_once_per_terminal_user_id(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        same = clone_event(db, event, device_event_time=event.device_event_time + timedelta(hours=1))
        other_serial = clone_event(db, event, device_serial="OTHER-SERIAL")
        db.commit()
        ids = [event_id, same.id, other_serial.id]
    with sessions() as db:
        rows = {row["event_id"]: row for row in direct.preview(db, ids)["rows"]}
        first = rows[event_id]
        assert first["punch_time_cnic"] == "NONE"
        assert rows[ids[1]]["record_position"] == first["record_position"]
        assert first["decision_kind"] == "CONFIRM_TODAY"
        assert first["decision_group"].startswith("today|")
        assert rows[ids[1]]["decision_group"] == first["decision_group"]
        # Groups never span terminal serials.
        assert rows[ids[2]]["decision_group"] != first["decision_group"]


def add_colleague_holding(db, event, cnic=PUNCH_CNIC):
    db.add(DeviceUser(
        zkt_device_id=event.zkt_device_id, user_key="colleague", uid="8", user_id="1008",
        display_name="Colleague", cnic_encrypted=encrypt_cnic(cnic),
        cnic_lookup_hash=cnic_lookup(cnic), present=True, lifecycle_state="ACTIVE",
    ))
    db.flush()


def test_punch_time_default_is_withheld_when_another_active_user_holds_that_cnic(store):
    """Safety review 6 / D1: the old CNIC may be a colleague's real CNIC.

    No default, and the server itself refuses the punch-time CNIC unless the
    request explicitly confirms it, so an older or faulty dialog cannot send
    a colleague's CNIC by defaulting to it.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_punch_time_interval(db, event)
        add_colleague_holding(db, event)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "DIFFERS"
        assert row["punch_time_cnic_held_by_other"] is True
        assert "PUNCH_TIME_CNIC_HELD_BY_OTHER" in row["evidence_signals"]
        assert row["cnic_choices"] == ["PUNCH_TIME", "CURRENT"]
        assert row["default_cnic_choice"] is None and row["decision_kind"] == "CHOOSE_CNIC"

        direct.create(db, actor="operator", request=start(db, event_id, key="held-unconfirmed", choice="PUNCH_TIME"))
        db.commit()
        refused = db.scalar(select(Item))
        assert refused.status == "SKIPPED" and refused.error_code == "PUNCH_TIME_CNIC_HELD_BY_OTHER"
        assert refused.result["payload_encrypted"] is None

        direct.create(db, actor="operator", request=start(
            db, event_id, key="held-confirmed", choice="PUNCH_TIME", held_ok=True,
        ))
        db.commit()
        ready = db.scalars(select(Item).order_by(Item.id.desc())).first()
        assert ready.status == "READY" and "HELD_BY_OTHER" in ready.result["evidence_tokens"]
    ticks(sessions, 1)
    with sessions() as db:
        decision = db.scalar(select(Decision))
        assert "HELD_BY_OTHER" in decision.proof["evidence_tokens"]
        assert approved(db, event_id)["cnic"] == PUNCH_CNIC


def test_a_new_holder_of_the_punch_time_cnic_stops_an_approval_that_did_not_accept_it(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        direct.create(db, actor="operator", request=start(db, event_id, key="before-holder", choice="PUNCH_TIME"))
        db.commit()
    ticks(sessions, 1)
    with sessions() as db:
        assert approved(db, event_id)["cnic"] == PUNCH_CNIC
        add_colleague_holding(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        assert approved(db, event_id) is None


def test_a_choice_made_against_older_evidence_is_skipped(store):
    """D2: the preview showed no record; a snapshot then revoked and conflicted it."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        seen = direct.preview(db, [event_id])["rows"][0]
        assert seen["punch_time_cnic"] == "NONE" and seen["evidence_digest"]
        event = db.get(AttendanceEvent, event_id)
        add_punch_time_interval(db, event, cnic=CORRECT_CNIC, revoked=True)
        db.scalar(select(DeviceUser)).identity_conflict_code = "DUPLICATE_USER_CNIC"
        db.commit()
    with sessions() as db:
        now = direct.preview(db, [event_id])["rows"][0]
        assert now["punch_time_cnic"] == "AMBIGUOUS"
        assert now["evidence_digest"] != seen["evidence_digest"]
        direct.create(db, actor="operator", request=start(
            db, event_id, key="stale", choice="CURRENT", digest=seen["evidence_digest"],
        ))
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "SKIPPED" and item.error_code == "CNIC_CHOICE_STALE"
        assert "Reload the checks" in item.result["reason"]
        assert db.scalar(select(Decision)) is None


def test_a_punch_time_choice_is_bound_to_the_record_it_named(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        seen = direct.preview(db, [event_id])["rows"][0]["evidence_digest"]
        # Another retained record names a further CNIC at the punch time.
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id), cnic="3520255555551")
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=start(
            db, event_id, key="stale-punch-time", choice="PUNCH_TIME", digest=seen,
        ))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_STALE"


def test_punch_in_a_break_between_records_of_the_old_cnic_takes_that_cnic(store):
    """Correctness 2: Toheed's punch falls in a break between two …609-0 records.

    The break is routine (a partial snapshot closes every interval). Both sides
    are the same terminal user with the same old CNIC, so it is the punch-time
    CNIC and the default; today's CNIC never applies by one tick.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_interval(db, event, cnic=PUNCH_CNIC, start=-timedelta(days=5), end=-timedelta(hours=2))
        add_interval(db, event, cnic=PUNCH_CNIC, start=timedelta(hours=1), end=timedelta(days=4), name="Toheed")
        add_interval(db, event, cnic=CORRECT_CNIC, start=timedelta(days=5), end=timedelta(days=6), closed=False)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "DIFFERS" and row["record_position"] == "BETWEEN_RECORDS"
        assert row["punch_time_cnic_basis"] == "BRIDGED_GAP"
        assert "GAP_BRIDGED_SAME_CNIC" in row["evidence_signals"]
        assert row["cnic_choices"] == ["PUNCH_TIME", "CURRENT"]
        assert row["default_cnic_choice"] == "PUNCH_TIME"
        assert row["punch_time_cnic_masked"] == mask_cnic(PUNCH_CNIC)
        assert row["decision_kind"] == "CHOOSE_CNIC"
        direct.create(db, actor="operator", request=start(db, event_id, key="bridged", choice="PUNCH_TIME"))
        db.commit()
    ticks(sessions, 1)
    with sessions() as db:
        payload = approved(db, event_id)
        assert payload["cnic"] == PUNCH_CNIC and payload["employee_name"] == "Toheed"


def test_break_between_records_of_another_terminal_user_offers_only_that_cnic(store):
    """The old CNIC on both sides but different terminal records: not bridged, never today's."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_interval(db, event, cnic=PUNCH_CNIC, start=-timedelta(days=5), end=-timedelta(hours=2))
        add_interval(db, event, cnic=PUNCH_CNIC, start=timedelta(hours=1), end=timedelta(days=4))
        records = db.scalars(select(AttendanceIdentityHistory).order_by(AttendanceIdentityHistory.id)).all()
        records[-1].device_user_id = records[0].device_user_id + 1000
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CHANGED_NEAR_PUNCH"
        assert row["cnic_choices"] == ["PUNCH_TIME"] and row["default_cnic_choice"] is None
        direct.create(db, actor="operator", request=start(db, event_id, key="not-bridged", choice="CURRENT"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"


def test_two_other_cnics_around_the_punch_refuse_the_send(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_interval(db, event, cnic=PUNCH_CNIC, start=-timedelta(days=5), end=-timedelta(hours=2))
        add_interval(db, event, cnic="3520255555551", start=timedelta(hours=1), end=timedelta(days=4))
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "CONFLICT" and row["exclusion"] == "CNIC_CONFLICT"
        assert row["cnic_choices"] == []


def test_punch_before_the_first_record_is_flagged_and_grouped_apart(store):
    """D3: ADD first saw the user ID 90 days after the early punch."""
    from zk_add.time_utils import ensure_utc
    from zk_add.web import attendance_identity_evidence_report

    sessions, _connector_id, _uid = store
    gap_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, gap_id)
        add_interval(db, event, cnic=CORRECT_CNIC, start=-timedelta(days=5), end=-timedelta(days=4))
        add_interval(db, event, cnic=CORRECT_CNIC, start=timedelta(hours=1), end=timedelta(hours=2), closed=False)
        early = clone_event(db, event, device_event_time=event.device_event_time - timedelta(days=95))
        first_seen = ensure_utc(event.device_event_time - timedelta(days=5))
        db.commit()
        early_id = early.id
    with sessions() as db:
        rows = {row["event_id"]: row for row in direct.preview(db, [early_id, gap_id])["rows"]}
        early, gap = rows[early_id], rows[gap_id]
        assert early["exclusion"] is None and gap["exclusion"] is None
        assert early["punch_time_cnic"] == "NONE" and early["record_position"] == "BEFORE_FIRST_RECORD"
        assert "PUNCH_BEFORE_FIRST_OBSERVATION" in early["evidence_signals"]
        assert ensure_utc(early["first_observed_at"]) == first_seen
        assert gap["punch_time_cnic"] == "NONE" and gap["record_position"] == "BETWEEN_RECORDS"
        assert gap["first_observed_at"] is None
        # One tick never covers both: punches older than ADD's history are confirmed apart.
        assert early["decision_group"].startswith("today|") and gap["decision_group"].startswith("today|")
        assert early["decision_group"] != gap["decision_group"]
        codes = [hint["code"] for hint in attendance_identity_evidence_report(early_id, auth=(db, None))["hints"]]
        assert "PUNCH_BEFORE_FIRST_OBSERVATION" in codes


def test_a_saved_name_that_is_not_todays_user_is_decided_per_punch(store):
    """Correctness 8: the name saved with a held punch is soft evidence."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.display_name = "Someone Else"
        same_name = clone_event(db, event, display_name="CORRECT  name")
        db.commit()
        same_id = same_name.id
    with sessions() as db:
        rows = {row["event_id"]: row for row in direct.preview(db, [event_id, same_id])["rows"]}
        named = rows[event_id]
        assert named["punch_time_cnic"] == "NONE" and "NAME_DIFFERS" in named["evidence_signals"]
        assert named["punch_display_name"] == "Someone Else"
        assert named["decision_group"] == f"row|{event_id}"
        # Case, spacing and a CNIC suffix are not a different name.
        assert "NAME_DIFFERS" not in rows[same_id]["evidence_signals"]
        assert rows[same_id]["decision_group"].startswith("today|")


def test_decision_groups_never_carry_a_cnic_hash(store):
    """Correctness 7: group ids are opaque."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["decision_group"].startswith("choose|")
        for value in (cnic_lookup(PUNCH_CNIC), cnic_lookup(CORRECT_CNIC)):
            assert value[:8] not in row["decision_group"] and value[:8] not in row["evidence_digest"]


def test_punch_time_cnic_of_another_terminal_record_does_not_offer_today(store):
    """A deleted and re-added terminal user is another record: only its own CNIC is offered."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        previous = DeviceUser(
            zkt_device_id=event.zkt_device_id, user_key="re-enrolled", uid=event.uid,
            user_id=event.user_id, display_name="Earlier record", present=False, lifecycle_state="DELETED",
        )
        db.add(previous)
        db.flush()
        add_punch_time_interval(db, event)
        punch_time_record(db).device_user_id = previous.id
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "DIFFERS" and row["cnic_choices"] == ["PUNCH_TIME"]
        direct.create(db, actor="operator", request=start(db, event_id, key="re-enrolled", choice="CURRENT"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "CNIC_CHOICE_REQUIRED"


def test_punch_time_send_uses_the_punch_name_never_todays(store):
    """Correctness 7: an interval without a name falls back to the punch's own name."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.display_name = "Name on the punch"
        add_punch_time_interval(db, event)
        punch_time_record(db).display_name_encrypted = None
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="punch-name", choice="PUNCH_TIME"))
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "READY"
        from zk_add.crypto import decrypt_json

        payload = decrypt_json(item.result["payload_encrypted"])
        assert payload["cnic"] == PUNCH_CNIC and payload["employee_name"] == "Name on the punch"
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.display_name = None
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="no-name", choice="PUNCH_TIME"))
        db.commit()
        latest = db.scalars(select(Item).order_by(Item.id.desc())).first()
        assert latest.error_code == "PUNCH_TIME_NAME_MISSING"


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
        direct.create(db, actor="operator", request=start(db, event_id, key="matches", send_now=True))
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "READY" and item.result["cnic_source"] == "SYNCED_USER"
        assert item.result["cnic_choice"] is None
        assert item.result["auto_release_override"] is True


def test_legacy_approval_stops_when_punch_time_cnic_differs(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="legacy", choice="CURRENT"))
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
        direct.create(db, actor="operator", request=start(db, event_id, key="no-send-now", choice="CURRENT"))
        db.commit()
        skipped = db.scalar(select(Item))
        assert skipped.error_code == "AUTO_RELEASE_PENDING"
        assert "next user snapshot" in skipped.result["reason"]
        direct.create(db, actor="operator", request=start(
            db, event_id, key="send-now", choice="CURRENT", send_now=True,
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
        direct.create(db, actor="operator", request=start(db, event_id, key="wait", choice="CURRENT"))
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
    assert alert.state == "OPEN" and alert.severity == "INFO"
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


def test_cleaning_a_suspect_name_keeps_its_shift_worker_marker(db):
    """Correctness 6: "Ali-S-A4230111028815" must not silently lose its -S marker."""
    from zk_add.web import serialize_user

    assert suspect_cnic_name("Ali Khan-S-42301-1102881-5").shift_worker is True
    assert suspect_cnic_name("A.Saleem-A4230111028815").shift_worker is False
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_names(db, connector, [("57", "04050", "Ali-S-A4230111028815")])
    ali = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "04050"))
    assert ali.shift_worker is False  # the unparsed name never set the flag
    shown = serialize_user(ali, zkt=connector.zkt_device)
    assert shown["suggested_display_name"] == "Ali" and shown["suggested_shift_worker"] is True
    command = update_device_user_command(
        db, connector=connector, user=ali,
        display_name="Ali-S-A4230111028815", cnic="4230111028815",
        shift_worker=False, privilege=0, expected_version=ali.row_version,
        idempotency_key="ali-shift-marker-kept", actor="StateHealthAdmin",
    )
    wire = serialize_command(command)
    assert wire["desired_state"]["display_name"] == "Ali"
    assert wire["desired_state"]["shift_worker"] is True
    assert wire["payload"]["name"] == "Ali-S-4230111028815"


def queue_current(sessions, event_id, key):
    """Approve today's CNIC for a punch with no record, and queue it (decision saved)."""
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key=key, choice="CURRENT", send_now=True))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
        assert db.scalar(select(Decision)) is not None


def ticks(sessions, count):
    """Scheduler ticks, each in its own committed transaction as in production."""
    for _ in range(count):
        with sessions() as db:
            direct.advance_once(db)
            db.commit()


def approved(db, event_id, decision=None):
    event = db.get(AttendanceEvent, event_id)
    decision = decision or db.scalars(select(Decision).order_by(Decision.id.desc())).first()
    return direct.approved_payload(db, event, db.get(Connector, event.connector_id), decision)


def test_better_evidence_for_the_same_cnic_never_stops_an_approval(store):
    """Both reviews, finding 4/1: the next snapshot covers the punch with today's CNIC.

    The payload is identical, so the approval keeps delivering (as on main)
    instead of being blocked forever.
    """
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "improves")
    with sessions() as db:
        assert approved(db, event_id)["cnic"] == CORRECT_CNIC
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id), cnic=CORRECT_CNIC)
        db.commit()
    with sessions() as db:
        assert approved(db, event_id)["cnic"] == CORRECT_CNIC
    normal, forced = delivery.split_claims(worker.claim_ords_batch(5))
    assert normal == [] and len(forced) == 1
    assert forced[0]["payload"]["cnic"] == CORRECT_CNIC


def test_better_evidence_before_queueing_still_queues(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="ready", choice="CURRENT", send_now=True))
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id), cnic=CORRECT_CNIC)
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
        assert db.scalar(select(Item)).status == "WAITING_ORACLE"


def test_new_contradicting_evidence_stops_an_approval_and_it_can_be_replaced(store):
    """Owner question 3: a stopped approval no longer blocks a new explicit approval."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "stopped")
    with sessions() as db:
        # A backfilled snapshot shows the terminal held another CNIC then.
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        assert approved(db, event_id) is None
    assert delivery.split_claims(worker.claim_ords_batch(5)) == ([], [])
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "BLOCKED_IDENTITY"
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["exclusion"] is None and row["replaces_stopped_approval"] is True
        assert row["punch_time_cnic"] == "DIFFERS"
        direct.create(db, actor="operator", request=start(db, event_id, key="replacement", choice="PUNCH_TIME"))
        db.commit()
    ticks(sessions, 2)
    with sessions() as db:
        decisions = db.scalars(select(Decision).order_by(Decision.id)).all()
        assert len(decisions) == 2
        assert approved(db, event_id, decisions[-1])["cnic"] == PUNCH_CNIC
        items = {item.job_id: item for item in db.scalars(select(Item)).all()}
        first, second = (items[decision.job_id] for decision in decisions)
        assert first.status in {"NEEDS_REVIEW", "SKIPPED"}
        assert second.status == "WAITING_ORACLE"
        assert db.get(AttendanceEvent, event_id).ords_status == "PENDING"


def test_an_approval_that_may_have_reached_oracle_is_never_replaced(store):
    """D5 / correctness 3: a POST was attempted (reply lost), then the approval stopped."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "posted")
    with sessions() as db:
        decision = db.scalar(select(Decision))
        item = db.get(Item, decision.item_id)
        item.result = {**item.result, "direct_post_attempts": 1}
        event = db.get(AttendanceEvent, event_id)
        event.ords_status = db.scalar(select(OrdsOutbox)).status = "FAILED_RETRYABLE"
        add_punch_time_interval(db, event)
        db.commit()
    with sessions() as db:
        assert approved(db, event_id) is None
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["exclusion"] == "PREVIOUS_POST_UNCERTAIN"
        assert row["replaces_stopped_approval"] is False
        assert "may already have reached Oracle" in row["exclusion_reason"]
        direct.create(db, actor="operator", request=start(db, event_id, key="replace-posted", choice="PUNCH_TIME"))
        db.commit()
        skipped = db.scalars(select(Item).order_by(Item.id.desc())).first()
        assert skipped.status == "SKIPPED" and skipped.error_code == "PREVIOUS_POST_UNCERTAIN"
        assert len(db.scalars(select(Decision)).all()) == 1


def test_an_approval_that_automatic_release_took_over_is_never_replaced(store):
    from zk_add.service import release_synced_cnic_attendance_event

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "taken-over")
    corrected = "3520255555551"
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.cnic_encrypted, user.cnic_lookup_hash = encrypt_cnic(corrected), cnic_lookup(corrected)
        db.commit()
    delivery.split_claims(worker.claim_ords_batch(5))
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        event.captured_cnic_lookup_hash = cnic_lookup(corrected)
        zkt = db.get(Connector, event.connector_id).zkt_device
        assert release_synced_cnic_attendance_event(db, zkt=zkt, user=db.scalar(select(DeviceUser)), row=event)
        db.commit()
    with sessions() as db:
        assert direct.preview(db, [event_id])["rows"][0]["exclusion"] == "ALREADY_DELIVERING"


def test_slow_automatic_release_checks_run_before_the_scheduler_lock(store, monkeypatch):
    """D6: the per-punch proof and guard never run while the locks are held."""
    from zk_add import attendance_release_safety as safety

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    calls = []
    real_preview, real_lock = direct.auto_release_preview, direct._lock_scheduler

    def preview(*args, **kwargs):
        calls.append("auto_release")
        return real_preview(*args, **kwargs)

    def lock(session):
        calls.append("lock")
        return real_lock(session)

    monkeypatch.setattr(direct, "auto_release_preview", preview)
    monkeypatch.setattr(direct, "_lock_scheduler", lock)
    assert safety.auto_release_preview is real_preview
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="lock-order", choice="CURRENT"))
        db.commit()
        assert db.scalar(select(Item)).status == "READY"
    assert calls[-1] == "lock" and "auto_release" in calls


def test_a_waiting_item_is_closed_when_a_later_approval_replaces_it(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "first")
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        db.commit()
    with sessions() as db:
        direct.create(db, actor="operator", request=start(db, event_id, key="second", choice="PUNCH_TIME"))
        db.commit()
    ticks(sessions, 3)
    with sessions() as db:
        first = db.scalar(select(Item).where(Item.job_id == min(item.job_id for item in db.scalars(select(Item)))))
        assert first.status == "SKIPPED" and first.error_code == "SUPERSEDED"


def test_pending_punch_time_approval_survives_an_hr_cnic_correction(store):
    """Toheed: HR confirms …609-0, so today's CNIC is corrected to the punch-time CNIC."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        direct.create(db, actor="operator", request=start(db, event_id, key="toheed", choice="PUNCH_TIME"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.cnic_encrypted, user.cnic_lookup_hash = encrypt_cnic(PUNCH_CNIC), cnic_lookup(PUNCH_CNIC)
        db.commit()
    with sessions() as db:
        assert approved(db, event_id)["cnic"] == PUNCH_CNIC


def test_todays_cnic_approval_stops_when_todays_cnic_changes(store):
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "today-changes")
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.cnic_encrypted, user.cnic_lookup_hash = encrypt_cnic(PUNCH_CNIC), cnic_lookup(PUNCH_CNIC)
        db.commit()
    with sessions() as db:
        assert approved(db, event_id) is None


def test_automatic_release_takes_over_a_stopped_direct_approval(store):
    """Safety review 4: ADD's own proof must be able to deliver once an approval stopped.

    Before, the stale decision blocked the punch after every automatic
    release (a 15-minute loop that never delivered).
    """
    from zk_add.service import release_synced_cnic_attendance_event

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "takeover")
    corrected = "3520255555551"
    with sessions() as db:
        user = db.scalar(select(DeviceUser))
        user.cnic_encrypted, user.cnic_lookup_hash = encrypt_cnic(corrected), cnic_lookup(corrected)
        db.commit()
    assert delivery.split_claims(worker.claim_ords_batch(5)) == ([], [])
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert event.ords_status == "BLOCKED_IDENTITY"
        # The terminal's own capture now proves the corrected CNIC.
        event.captured_cnic_lookup_hash = cnic_lookup(corrected)
        zkt = db.get(Connector, event.connector_id).zkt_device
        assert release_synced_cnic_attendance_event(db, zkt=zkt, user=db.scalar(select(DeviceUser)), row=event)
        db.commit()
    normal, forced = delivery.split_claims(worker.claim_ords_batch(5))
    assert forced == [] and len(normal) == 1
    assert normal[0][1]["cnic"] == corrected
    with sessions() as db:
        assert db.scalar(select(OrdsOutbox)).status == "IN_FLIGHT"
        event = db.get(AttendanceEvent, event_id)
        event.ords_status, event.oracle_confirmed_at = "ACKED_CHECK", utc_now()
        db.commit()
    with sessions() as db:
        assert db.scalar(select(Item)).status == "WAITING_ORACLE"
        direct.advance_once(db)
        db.commit()
        item = db.scalar(select(Item))
        assert item.status == "SKIPPED" and item.error_code == "AUTO_RELEASED"


def test_a_valid_direct_approval_is_not_taken_over(store):
    from zk_add.attendance_direct_ords import superseded_by_automatic_release

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    queue_current(sessions, event_id, "still-valid")
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert superseded_by_automatic_release(db, event, db.scalar(select(Decision))) is False


def test_punch_whose_records_contradict_today_is_never_awaiting_a_snapshot(store):
    """Correctness 5: such a row can never release automatically, so no "send now" prompt."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        add_punch_time_interval(db, event)
        zkt = db.get(Connector, event.connector_id).zkt_device
        event.captured_at = event.received_at = zkt.identity_snapshot_observed_at + timedelta(minutes=1)
        db.commit()
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["punch_time_cnic"] == "DIFFERS"
        assert row["awaiting_next_snapshot"] is False and row["auto_release_at_next_snapshot"] is False


def test_unreadable_database_guard_still_requires_send_now(store, monkeypatch):
    """Safety review 7: a guard probe failure never removes the "send now" gate."""
    from zk_add import attendance_release_safety as safety

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        db.get(AttendanceEvent, event_id).captured_cnic_lookup_hash = db.scalar(select(DeviceUser)).cnic_lookup_hash
        db.commit()
    monkeypatch.setattr(safety, "database_guard_state", lambda *_args, **_kwargs: "UNAVAILABLE")
    with sessions() as db:
        row = direct.preview(db, [event_id])["rows"][0]
        assert row["database_guard"] == "UNAVAILABLE"
        assert row["auto_release_at_next_snapshot"] is True
        direct.create(db, actor="operator", request=start(db, event_id, key="guard-unavailable"))
        db.commit()
        assert db.scalar(select(Item)).error_code == "AUTO_RELEASE_PENDING"


def test_unreadable_retained_name_never_breaks_delivery_claims(store):
    """Correctness 6: a corrupt encrypted name must not abort the worker's claim transaction."""
    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        add_punch_time_interval(db, db.get(AttendanceEvent, event_id))
        direct.create(db, actor="operator", request=start(db, event_id, key="corrupt", choice="PUNCH_TIME"))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    with sessions() as db:
        punch_time_record(db).display_name_encrypted = "not-a-fernet-token"
        db.commit()
    # The name changed, so the frozen approval no longer matches: held, not crashed.
    assert delivery.split_claims(worker.claim_ords_batch(5)) == ([], [])
    with sessions() as db:
        assert db.get(AttendanceEvent, event_id).ords_status == "BLOCKED_IDENTITY"
        assert direct.preview(db, [event_id])["rows"][0]["punch_time_cnic"] == "DIFFERS"


def test_many_intervals_never_give_a_truncated_answer(store, monkeypatch):
    """Both reviews (ledger hints): bounded per terminal user ID, unprovable when overflowing."""
    from zk_add import attendance_release_safety as safety

    sessions, _connector_id, _uid = store
    event_id = held_event(sessions)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        for _ in range(4):  # overlapping duplicates after snapshot backfills
            add_punch_time_interval(db, event)
        db.commit()
    monkeypatch.setattr(safety, "CHUNK_ROW_LIMIT", 3)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        assert direct.identity_hints_for_page(db, [event])[event_id]["punch_time_cnic"] == "DIFFERS"
    monkeypatch.setattr(safety, "PER_KEY_LIMIT", 2)
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        hint = direct.identity_hints_for_page(db, [event])[event_id]
        assert hint["punch_time_cnic"] == "CONFLICT" and hint["eligible"] is False


def test_cnic_format_alert_is_info_and_not_refreshed_while_unchanged(db):
    """Safety review 5: a running HIL observation must not see a new WARNING every snapshot."""
    from zk_add.hil_observation import _review

    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_names(db, connector, [("55", "04048", "A.Saleem-A4230111028815")])
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "USER_CNIC_FORMAT_SUSPECT"))
    seen = alert.last_seen_at
    snapshot_names(db, connector, [("55", "04048", "A.Saleem-A4230111028815")])
    db.refresh(alert)
    assert alert.severity == "INFO" and alert.last_seen_at == seen
    review = _review([], [alert], [], preservation_verified=True)
    assert review["unclassified_alert_ids"] == [] and review["adverse_alert_ids"] == []


def test_cnic_format_alert_clears_in_the_snapshot_that_removes_the_user(db):
    """Correctness 8: the refresh reads this snapshot's users, not the previous ones."""
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    snapshot_names(db, connector, [
        ("55", "04048", "A.Saleem-A4230111028815"), ("56", "04049", "Bilal-3520212345671"),
    ])
    alert = db.scalar(select(DeviceAlert).where(DeviceAlert.code == "USER_CNIC_FORMAT_SUSPECT"))
    assert alert.state == "OPEN"
    snapshot_names(db, connector, [("56", "04049", "Bilal-3520212345671")])
    db.refresh(alert)
    assert alert.state != "OPEN"


def test_unreadable_terminal_name_never_fails_a_user_snapshot(db, monkeypatch):
    from zk_add import attendance_release_safety as safety

    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)

    def broken(_value):
        raise ValueError("corrupt ciphertext")

    monkeypatch.setattr(safety, "decrypt_text", broken)
    snapshot_names(db, connector, [("55", "04048", "A.Saleem-A4230111028815")])
    assert db.scalar(select(DeviceUser).where(DeviceUser.user_id == "04048")) is not None
