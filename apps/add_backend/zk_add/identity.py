from __future__ import annotations

import re
from dataclasses import dataclass


CNIC_SUFFIX_RE = re.compile(r"^(?P<display>.*?)(?P<shift>-S)?-(?P<cnic>\d{13})$")


@dataclass(frozen=True)
class ParsedIdentity:
    raw_name: str
    display_name: str
    cnic: str | None
    shift_worker: bool


def parse_machine_name(value: str | None) -> ParsedIdentity:
    raw = " ".join(str(value or "").strip().split())
    match = CNIC_SUFFIX_RE.match(raw)
    if not match:
        return ParsedIdentity(raw, raw, None, False)
    return ParsedIdentity(
        raw_name=raw,
        display_name=" ".join(match.group("display").strip().split()),
        cnic=match.group("cnic"),
        shift_worker=bool(match.group("shift")),
    )


def build_machine_name(
    *, display_name: str, current_raw_name: str, byte_limit: int
) -> str:
    parsed = parse_machine_name(current_raw_name)
    cleaned = " ".join(display_name.strip().split())
    if not cleaned:
        raise ValueError("Display name is required.")
    suffix = ""
    if parsed.cnic:
        suffix = f"-S-{parsed.cnic}" if parsed.shift_worker else f"-{parsed.cnic}"
    available = byte_limit - len(suffix.encode("utf-8"))
    if available < 1:
        raise ValueError("This model cannot preserve the existing CNIC in its name field.")
    encoded = bytearray()
    for character in cleaned:
        part = character.encode("utf-8")
        if len(encoded) + len(part) > available:
            break
        encoded.extend(part)
    prefix = encoded.decode("utf-8").strip()
    if not prefix:
        raise ValueError("Display name cannot fit in the device name field.")
    result = f"{prefix}{suffix}"
    if len(result.encode("utf-8")) > byte_limit:
        raise ValueError("Device name exceeds the model byte limit.")
    return result

