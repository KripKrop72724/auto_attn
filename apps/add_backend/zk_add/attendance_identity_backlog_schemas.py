"""Owner requests for releasing a zone's held legacy dump punches."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class IdentityBacklogScope(BaseModel):
    """The frozen zone check, echoed exactly as the preview returned it."""

    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(min_length=1, max_length=100)
    as_of: str = Field(min_length=1, max_length=64)
    until_id: int = Field(ge=0)
    window_hours: int = Field(ge=0, le=14 * 24)
    connectors: list[str] = Field(min_length=1, max_length=64)


class IdentityBacklogGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=100)
    source: Literal["DUMP_RECONNECT", "DUMP_STARTUP"]
    age: Literal["AUTOMATIC_WINDOW", "UP_TO_7_DAYS", "UP_TO_30_DAYS", "OLDER"]


class IdentityBacklogReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    scope: IdentityBacklogScope
    expires_at: datetime
    signature: str = Field(min_length=64, max_length=64)
    groups: list[IdentityBacklogGroup] = Field(min_length=1, max_length=512)
    # At most this many punches are released, from the chosen groups only.
    approved_count: int = Field(ge=1, le=1_000_000)
    # Punches the check counted as held in the chosen groups.
    previewed_held: int = Field(ge=1, le=1_000_000)


class IdentityBacklogStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=512)
