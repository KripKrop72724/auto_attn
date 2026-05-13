from datetime import datetime, timezone

from zk_common.enums import ClockStatus, TrustStatus
from zk_zone_agent.fraud import FraudEngine


def test_live_attendance_flags_device_time_drift():
    engine = FraudEngine(drift_threshold_seconds=120)
    result = engine.classify_live_attendance(
        device_event_time=datetime(2026, 5, 13, 9, 0),
        zone_trusted_time=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        timezone_name="UTC",
        internet_online=True,
        current_clock_status=ClockStatus.OK,
    )
    assert result.trust_status == TrustStatus.SUSPECT_DEVICE_TIME
    assert result.fraud_score == 80


def test_clock_check_detects_jump():
    engine = FraudEngine(jump_threshold_seconds=15)
    result = engine.classify_clock_check(
        device_time=datetime(2026, 5, 13, 10, 10, 30),
        trusted_time=datetime(2026, 5, 13, 10, 0, 5, tzinfo=timezone.utc),
        timezone_name="UTC",
        previous_device_time=datetime(2026, 5, 13, 10, 0, 0),
        previous_trusted_time=datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert result.status == ClockStatus.SUSPICIOUS
    assert result.jump_seconds == 625
