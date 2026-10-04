import pytest
from pydantic import ValidationError

from zk_add.capture_latency import CaptureLatencyHistogram, UPPER_MS
from zk_add.schemas import FirmwareDiagnostics


def histogram(values):
    buckets = [0] * len(UPPER_MS)
    for value in values:
        buckets[next(i for i, bound in enumerate(UPPER_MS) if value <= bound)] += 1
    return {"schema_version": 1, "samples": len(values), "max_ms": max(values, default=0),
            "buckets": buckets, "saturated": False}


def test_capture_latency_preserves_distribution_and_conservative_percentiles():
    data = histogram([7] * 95 + [251] * 4 + [17000])
    stats = CaptureLatencyHistogram.model_validate(data)
    assert stats.percentile_upper_ms(95) == 10
    assert stats.percentile_upper_ms(99) == 500
    assert stats.percentile_upper_ms(100) == 17000
    parsed = FirmwareDiagnostics(workers=[{"name": "capture", "state": "RUNNING",
        "completed_operations": 100, "failures": 12, "timeouts": 3,
        "packet_commit_latency_ms": data, "fragment_commit_latency_ms": histogram([0, 1, 5])}])
    saved = parsed.model_dump(mode="json")["workers"][0]
    assert saved["packet_commit_latency_ms"] == data
    assert saved["failures"] == 12 and saved["timeouts"] == 3
    assert saved["fragment_commit_latency_ms"]["samples"] == 3


def test_capture_latency_empty_and_saturated_do_not_offer_percentiles():
    assert CaptureLatencyHistogram.model_validate(histogram([])).percentile_upper_ms(99) is None
    data = histogram([500])
    data.update(samples=0xFFFFFFFF, saturated=True)
    data["buckets"][8] = 0xFFFFFFFF
    stats = CaptureLatencyHistogram.model_validate(data)
    assert stats.percentile_upper_ms(99) is None
    for value in (0, 101, True, 99.5):
        with pytest.raises(ValueError):
            stats.percentile_upper_ms(value)


@pytest.mark.parametrize("change", [
    {"samples": -1}, {"samples": True}, {"max_ms": 2**32}, {"samples": 2},
    {"max_ms": 500}, {"max_ms": 0}, {"buckets": [1]}, {"saturated": True},
    {"schema_version": True}, {"schema_version": 2}, {"saturated": "false"},
    {"buckets": [0, False, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
])
def test_capture_latency_rejects_incoherent_or_coerced_measurements(change):
    with pytest.raises(ValidationError):
        CaptureLatencyHistogram.model_validate({**histogram([7]), **change})


def test_capture_latency_exact_boundaries_and_empty_maximum():
    for upper in UPPER_MS:
        value = CaptureLatencyHistogram.model_validate(histogram([upper]))
        assert value.percentile_upper_ms(99) == upper
    with pytest.raises(ValidationError):
        CaptureLatencyHistogram.model_validate({**histogram([]), "max_ms": 1})
