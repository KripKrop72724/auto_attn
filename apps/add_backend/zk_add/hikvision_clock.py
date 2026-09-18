"""Clock evidence is contemporaneous proof, never a historical timestamp rewrite."""
from datetime import datetime
from pydantic import BaseModel, Field


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
