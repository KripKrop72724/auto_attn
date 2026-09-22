from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class SafeRepairCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_ids: list[str] = Field(default_factory=list, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=120)


class SafeRepairStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["SAFE_REPAIR"]
    check_id: str = Field(min_length=36, max_length=36)
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    password: SecretStr = Field(min_length=1, max_length=512)


class SafeRepairControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Literal["SAFE_REPAIR"]
    action: Literal["PAUSE", "RESUME", "STOP"]
    password: SecretStr = Field(min_length=1, max_length=512)
