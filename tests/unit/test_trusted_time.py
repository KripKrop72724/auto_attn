from datetime import datetime, timezone

from zk_zone_agent.trusted_time import TrustedTimeService


def test_trusted_time_uses_head_office_anchor_plus_monotonic_elapsed():
    wall = [datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)]
    mono = [1_000_000_000]
    service = TrustedTimeService(wall_clock=lambda: wall[0], monotonic_ns=lambda: mono[0])

    service.update_from_head_office(datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc))
    mono[0] += 5_000_000_000

    now = service.now()
    assert now.source == "HEAD_OFFICE_MONOTONIC"
    assert now.value == datetime(2026, 5, 13, 11, 0, 5, tzinfo=timezone.utc)


def test_pc_clock_tamper_detects_wall_clock_jump():
    wall = [datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)]
    mono = [1_000_000_000]
    service = TrustedTimeService(wall_clock=lambda: wall[0], monotonic_ns=lambda: mono[0])

    assert service.check_pc_clock_tamper() is None
    mono[0] += 5_000_000_000
    wall[0] = datetime(2026, 5, 13, 8, 0, 5, tzinfo=timezone.utc)

    tamper = service.check_pc_clock_tamper()
    assert tamper is not None
    assert tamper.jump_seconds == -7200
