"""Identity evidence for historical rows must cover their original event time."""

from datetime import datetime, timedelta

from zk_add.time_utils import ensure_utc


def historical_identity_is_supported(
    *,
    serial: str | None,
    bound_serial: str | None,
    confirmed_serial: str | None,
    uid: str | None,
    expected_uid: str | None,
    fingerprint: str | None,
    expected_fingerprint: str | None,
    event_time: datetime,
    continuity_started: datetime | None,
    snapshot_observed: datetime | None,
    snapshot_stable: bool,
    tolerance_seconds: int,
) -> bool:
    if not (
        serial
        and bound_serial
        and confirmed_serial
        and uid
        and expected_uid
        and fingerprint
        and expected_fingerprint
        and continuity_started
        and snapshot_observed
        and snapshot_stable
    ):
        return False
    if not (
        serial == bound_serial
        and serial == confirmed_serial
        and uid == expected_uid
        and len(fingerprint) == 64
        and len(expected_fingerprint) == 64
        and fingerprint == expected_fingerprint
    ):
        return False
    return (
        ensure_utc(continuity_started)
        <= ensure_utc(event_time)
        <= ensure_utc(snapshot_observed) + timedelta(seconds=max(0, tolerance_seconds))
    )
