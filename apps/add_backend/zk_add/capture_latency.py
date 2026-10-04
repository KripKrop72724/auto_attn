"""Bounded device capture measurements, not an end-to-end release verdict."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UPPER_MS = (0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000, 15000, 0xFFFFFFFF)
Counter = Annotated[int, Field(strict=True, ge=0, le=0xFFFFFFFF)]


class CaptureLatencyHistogram(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    buckets: tuple[Counter, ...] = Field(min_length=len(UPPER_MS), max_length=len(UPPER_MS))
    samples: Counter
    max_ms: Counter
    saturated: bool = Field(strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("Latency schema version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent_measurement(self):
        if sum(self.buckets) != self.samples:
            raise ValueError("Latency histogram count mismatch")
        if self.saturated and self.samples != 0xFFFFFFFF:
            raise ValueError("Latency saturation requires an exhausted counter")
        if not self.samples:
            if self.max_ms:
                raise ValueError("An empty latency histogram cannot have a maximum")
        else:
            last = max(i for i, count in enumerate(self.buckets) if count)
            actual = next(i for i, bound in enumerate(UPPER_MS) if self.max_ms <= bound)
            if actual != last:
                raise ValueError("Latency maximum does not match its final occupied bucket")
        return self

    def percentile_upper_ms(self, percentile: int) -> int | None:
        if type(percentile) is not int or not 1 <= percentile <= 100:
            raise ValueError("Percentile must be between one and 100")
        if not self.samples or self.saturated:
            return None
        rank = (self.samples * percentile + 99) // 100
        total = 0
        for count, upper in zip(self.buckets, UPPER_MS, strict=True):
            total += count
            if total >= rank:
                return min(upper, self.max_ms)
        return None
