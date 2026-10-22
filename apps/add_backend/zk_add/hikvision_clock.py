"""Clock evidence is contemporaneous proof, never a historical timestamp rewrite."""
from datetime import datetime
from pydantic import BaseModel, Field

from zk_add.time_utils import ensure_utc


class HikvisionClockSample(BaseModel):
    device_epoch: int = Field(ge=1_577_836_800, le=4_102_444_799, strict=True)
    sampled_epoch: int = Field(ge=1_577_836_800, le=4_102_444_799, strict=True)

    @property
    def drift_seconds(self) -> int:
        return self.device_epoch - self.sampled_epoch


def clock_evidence(sample: HikvisionClockSample | None, event_time: datetime,
                   captured_epoch: int, channel: str, timezone_assumed: bool) -> tuple[str, int | None]:
    # A current clock cannot certify full retained history. Older firmware has no
    # sample; replay/retained events keep UNKNOWN without altering their timestamp.
    if (sample is None or channel not in {"POLL", "STREAM", "PUSH"} or timezone_assumed
            or not 0 <= captured_epoch - sample.sampled_epoch <= 120
            or abs(event_time.timestamp() - sample.device_epoch) > 120):
        return "UNKNOWN", None
    drift = sample.drift_seconds
    return ("OK" if abs(drift) <= 120 else "DRIFTED"), drift


def esp_clock_offset_seconds(received_at: datetime, sent_at: datetime | None) -> int | None:
    """ADD receive time minus the ESP's own envelope time; positive means the ESP is slow.

    Only an envelope built at send time (a heartbeat) measures the ESP clock.
    """
    if sent_at is None:
        return None
    offset = (ensure_utc(received_at) - ensure_utc(sent_at)).total_seconds()
    return int(round(offset)) if abs(offset) <= 366 * 86400 else None


ESP_OFFSET_WINDOW = 3
ESP_OFFSET_AGREEMENT_SECONDS = 30


def stable_esp_offset(offsets: list) -> int | None:
    """The ESP clock offset recent heartbeats agree on, or None while unproven.

    Each measurement uses ADD's processing time, so a heartbeat that waited in
    a queue or a socket buffer only ever adds to its offset. The smallest
    recent offset is therefore the closest to the truth, and the two smallest
    must agree before ADD acts on it.
    """
    values = sorted(value for value in offsets if type(value) is int)
    if len(values) < 2 or values[1] - values[0] > ESP_OFFSET_AGREEMENT_SECONDS:
        return None
    return values[0]
