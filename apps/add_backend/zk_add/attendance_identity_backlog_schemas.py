"""Owner requests for releasing a zone's held legacy dump punches."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class IdentityBacklogReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=120)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class IdentityBacklogStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=512)
