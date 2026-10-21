"""Administrator-selected attendance delivery requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class DirectOrdsCnicChoice(BaseModel):
    """Which CNIC to send when the punch-time CNIC is not today's CNIC."""

    model_config = ConfigDict(extra="forbid")
    event_id: int = Field(ge=1)
    choice: Literal["PUNCH_TIME", "CURRENT"]


class DirectOrdsStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=500)
    password: SecretStr = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    # Optional so an older dashboard keeps working: punches that need a
    # choice or a "send now" confirmation are then skipped, never guessed.
    cnic_choices: list[DirectOrdsCnicChoice] = Field(default_factory=list, max_length=500)
    send_now_event_ids: list[int] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_selection(self):
        if any(value < 1 for value in self.event_ids) or len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("Select distinct saved punches.")
        selected = set(self.event_ids)
        chosen = [row.event_id for row in self.cnic_choices]
        if len(set(chosen)) != len(chosen) or not set(chosen) <= selected:
            raise ValueError("Choose one CNIC per selected punch.")
        if (
            len(set(self.send_now_event_ids)) != len(self.send_now_event_ids)
            or not set(self.send_now_event_ids) <= selected
        ):
            raise ValueError("Confirm sending now only for selected punches.")
        return self


class DirectOrdsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ids: list[int] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_selection(self):
        if any(value < 1 for value in self.event_ids) or len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("Select distinct saved punches.")
        return self


class DirectOrdsRecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=1, max_length=512)
