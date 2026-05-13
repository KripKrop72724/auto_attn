from datetime import datetime

from zk_common.hashing import attendance_event_uid, canonical_json


def test_attendance_event_uid_is_deterministic():
    kwargs = {
        "zone_id": "RWP-ZONE-01",
        "device_serial": "ADZV211860253",
        "user_id": "5",
        "device_event_time": datetime(2026, 5, 13, 9, 0, 0),
        "punch": 0,
        "source_uid": 5,
    }
    assert attendance_event_uid(**kwargs) == attendance_event_uid(**kwargs)


def test_canonical_json_sorts_keys_and_compacts():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
