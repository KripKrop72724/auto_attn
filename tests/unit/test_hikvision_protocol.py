import pytest

from zk_add.hikvision_protocol import SourceIdentityError, normalize_observation


def history(**overrides):
    # Synthetic values matching the observed wire shape; not personal device data.
    return {"serialNo": 30001, "major": 5, "minor": 75, "employeeNoString": "00012",
            "time": "2026-09-17T15:00:00+05:00", "name": "Synthetic", **overrides}


def stream(current=True, **overrides):
    return {"dateTime": "2026-09-17T10:00:00Z", "eventType": "AccessControllerEvent",
            "AccessControllerEvent": {"serialNo": 30001, "majorEventType": 5,
                                      "subEventType": 75, "employeeNoString": "00012",
                                      "currentEvent": current, **overrides}}


def normalize(value, **overrides):
    return normalize_observation(value, **{
        "terminal_serial": "SYNTHETIC-TERMINAL", "source_epoch": "epoch-one", **overrides,
    })


def test_live_replay_and_history_have_one_source_key_and_equal_physical_facts():
    items = [normalize(stream()), normalize(stream(False)), normalize(history())]
    assert len({item.event_uid for item in items}) == 1
    assert len({item.immutable_facts_digest for item in items}) == 1
    assert [item.channel for item in items] == ["LIVE", "REPLAY", "HISTORY"]
    assert items[0].employee_no == "00012"
    assert len(items[0].event_uid) == 64


def test_distinct_same_second_punches_are_not_collapsed():
    assert normalize(history()).event_uid != normalize(history(serialNo=30002)).event_uid


def test_identity_edit_does_not_change_event_uid_and_physical_conflicts_are_detectable():
    baseline = normalize(history())
    assert normalize(history(name="New display name")).event_uid == baseline.event_uid
    changed = normalize(history(employeeNoString="999"))
    assert changed.event_uid == baseline.event_uid
    assert changed.immutable_facts_digest != baseline.immutable_facts_digest


def test_terminals_and_explicit_epochs_are_isolated():
    baseline = normalize(history())
    assert normalize(history(), terminal_serial="OTHER").event_uid != baseline.event_uid
    assert normalize(history(), source_epoch="epoch-two").event_uid != baseline.event_uid


def test_unknown_codes_do_not_become_attendance_and_missing_identity_is_held():
    assert normalize(history()).disposition(frozenset()) == "UNCLASSIFIED"
    mapping = frozenset({(5, 75)})
    assert normalize(history()).disposition(mapping) == "ATTENDANCE_CANDIDATE"
    assert normalize(history(employeeNoString=None)).disposition(mapping) == "BLOCKED_IDENTITY"


def test_naive_time_uses_explicit_site_zone_without_guessing_attendance_direction():
    row = normalize(history(time="2026-09-17T15:00:00", attendanceStatus="undefined"))
    assert row.timezone_assumed is True
    assert row.event_time_utc == "2026-09-17T10:00:00+00:00"
    assert row.attendance_status is None


@pytest.mark.parametrize("fields", [
    {"serialNo": None}, {"serialNo": True}, {"serialNo": "30001"}, {"serialNo": 0},
    {"employeeNoString": 12}, {"employeeNoString": "0" * 33},
    {"employeeNoString": "12\x00"}, {"employeeNoString": " 12"},
    {"major": True}, {"minor": -1}, {"time": "not-a-time"}, {"time": None},
    {"time": "2026-09-17"},
])
def test_unstable_or_invalid_records_are_rejected_for_evidence_handling(fields):
    with pytest.raises(SourceIdentityError):
        normalize(history(**fields))


@pytest.mark.parametrize("timestamp", ["2026-11-01T01:30:00", "2026-03-08T02:30:00"])
def test_ambiguous_or_nonexistent_local_time_is_held(timestamp):
    with pytest.raises(SourceIdentityError, match="AMBIGUOUS"):
        normalize(history(time=timestamp), site_timezone="America/New_York")


def test_absent_current_event_flag_is_not_live():
    assert normalize(stream(None)).channel == "STREAM_UNKNOWN"


def test_xml_envelope_and_typed_fields_match_json_identity_without_losing_employee_zeros():
    raw = stream()
    raw["AccessControllerEvent"].update(serialNo="30001", majorEventType="5",
                                        subEventType="75", currentEvent="true")
    xml = normalize({"EventNotificationAlert": raw})
    assert xml.channel == "LIVE"
    assert xml.employee_no == "00012"
    assert xml.immutable_facts_digest == normalize(history()).immutable_facts_digest
