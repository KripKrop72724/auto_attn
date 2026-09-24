"""Administrator-selected attendance delivery requests."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class DirectOrdsStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def unique_selection(self):
        if any(value < 1 for value in self.event_ids) or len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("Select distinct saved punches.")
        return self


class DirectOrdsRecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=512)
