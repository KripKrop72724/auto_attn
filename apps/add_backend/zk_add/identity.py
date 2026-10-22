from __future__ import annotations

import re
from dataclasses import dataclass


CNIC_SUFFIX_RE = re.compile(r"^(?P<display>.*?)(?P<shift>-S)?-(?P<cnic>\d{13})$")
# A name ending in something CNIC-like that ADD's "-<13 digits>" format does
# not accept, e.g. "A.Saleem-A4230111028815" (a letter typed before the
# digits) or "Ali Khan-42301-1102881-5" (a dashed CNIC). These are only ever
# reported for an operator to confirm with HR; the digits are never used.
SUSPECT_CNIC_SUFFIX_RE = re.compile(
    r"^(?P<display>.*?)(?P<shift>[\s_-]+S)?[\s_.-]+(?P<letters>[A-Za-z]{0,2})[\s.-]?"
    r"(?P<digits>\d{5}[\s.-]\d{7}[\s.-]\d|\d{13})$"
)


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


@dataclass(frozen=True)
class SuspectCnicName:
    """A terminal name whose CNIC-like suffix ADD refuses to parse."""

    display_name: str
    reason: str  # LETTER_BEFORE_DIGITS, SEPARATED_DIGITS or UNSUPPORTED_SEPARATOR
    shift_worker: bool = False  # the name carried the "-S" shift-worker marker


def suspect_cnic_name(value: str | None) -> SuspectCnicName | None:
    """Flag a CNIC-looking suffix that parse_machine_name does not accept.

    The result carries no digits on purpose: an operator must confirm the
    CNIC with HR and enter it, so a typo can never become someone's identity.
    """
    raw = " ".join(str(value or "").strip().split())
    if not raw or parse_machine_name(raw).cnic:
        return None
    match = SUSPECT_CNIC_SUFFIX_RE.match(raw)
    if not match:
        return None
    if match.group("letters"):
        reason = "LETTER_BEFORE_DIGITS"
    elif not match.group("digits").isdigit():
        reason = "SEPARATED_DIGITS"
    else:
        reason = "UNSUPPORTED_SEPARATOR"
    return SuspectCnicName(
        display_name=" ".join(match.group("display").strip(" -_.").split()),
        reason=reason,
        shift_worker=bool(match.group("shift")),
    )


def clean_display_name(value: str) -> str:
    """The display name without a CNIC-like suffix, for a name that gets a real CNIC.

    The terminal name field is 24 bytes; keeping "A.Saleem-A4230111028815" as
    the display name and appending "-<CNIC>" would store "A.Saleem-A-<CNIC>".
    """
    cleaned = " ".join(str(value or "").strip().split())
    parsed = parse_machine_name(cleaned)
    if parsed.cnic and parsed.display_name:
        cleaned = parsed.display_name
    suspect = suspect_cnic_name(cleaned)
    if suspect and suspect.display_name:
        cleaned = suspect.display_name
    return cleaned


def build_machine_name(
    *,
    display_name: str,
    cnic: str | None = None,
    shift_worker: bool = False,
    byte_limit: int = 24,
    current_raw_name: str | None = None,
) -> str:
    parsed = parse_machine_name(current_raw_name)
    supplied_cnic = cnic
    cnic = supplied_cnic or parsed.cnic
    if supplied_cnic is None and parsed.cnic:
        shift_worker = parsed.shift_worker
    cleaned = " ".join(display_name.strip().split())
    if not cleaned:
        raise ValueError("Display name is required.")
    suffix = ""
    if cnic:
        suffix = f"-S-{cnic}" if shift_worker else f"-{cnic}"
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
