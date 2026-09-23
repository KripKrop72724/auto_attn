from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ForceCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["SELECTED", "ALL_PAKISTAN"]
    connector_ids: list[str] = Field(default_factory=list, max_length=1000)
    from_time: datetime | None = None
    to_time: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def explicit_scope(self):
        if (self.scope == "SELECTED") != bool(self.connector_ids):
            raise ValueError("Select devices or choose All Pakistan explicitly.")
        for value in (self.from_time, self.to_time):
            if value is not None and value.tzinfo is None:
                raise ValueError("Date limits must include their timezone.")
        if self.from_time and self.to_time and self.from_time >= self.to_time:
            raise ValueError("The end date must follow the start date.")
        return self


class ForceStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=3, max_length=500)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ForceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["PAUSE", "RESUME", "STOP"]
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class UserRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)
