"""Exact ordered HIL identities. Display names never authorize an update."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HilTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connector_id: str = Field(min_length=1, max_length=100)
    mac: str
    terminal_serial: str = Field(min_length=1, max_length=120)

    @field_validator("mac")
    @classmethod
    def canonical_mac(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
            raise ValueError("HIL target requires an exact ESP MAC")
        return value

    @field_validator("connector_id", "terminal_serial")
    @classmethod
    def exact_identity(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("HIL target identity cannot contain whitespace padding or controls")
        return value


def parse_hil_targets(value: Any) -> list[HilTarget]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("HIL scope requires an ordered list of one to eight exact targets")
    targets = [HilTarget.model_validate(row) for row in value]
    for field in ("connector_id", "mac", "terminal_serial"):
        if len({getattr(target, field) for target in targets}) != len(targets):
            raise ValueError(f"HIL scope repeats a target {field}")
    return targets


def target_matches(target: HilTarget, connector: Any) -> bool:
    terminal = connector.zkt_device
    return bool(
        connector.active
        and not connector.is_spare
        and connector.connector_id == target.connector_id
        and connector.hardware_id.lower() == target.mac
        and terminal is not None
        and terminal.serial == target.terminal_serial
        and terminal.confirmed_serial == target.terminal_serial
        and terminal.expected_serial == target.terminal_serial
        and terminal.terminal_binding_state == "CONFIRMED"
    )
