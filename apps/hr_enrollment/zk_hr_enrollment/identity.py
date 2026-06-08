from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol


MAX_ZKT_NAME_BYTES = 24
CNIC_SUFFIX_RE = re.compile(r"^(?P<alias>.*?)(?P<shift>-S)?-(?P<cnic>\d{13})$")
FINGER_LABELS: dict[int, str] = {
    0: "Left Little",
    1: "Left Ring",
    2: "Left Middle",
    3: "Left Index",
    4: "Left Thumb",
    5: "Right Thumb",
    6: "Right Index",
    7: "Right Middle",
    8: "Right Ring",
    9: "Right Little",
}


class UserLike(Protocol):
    uid: str | int
    user_id: str | int
    name: str | None


@dataclass(frozen=True)
class MachineIdentity:
    alias: str
    cnic: str
    shift_worker: bool


def normalize_full_name(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_cnic(value: object) -> str:
    digits = "".join(char for char in str(value or "").strip() if char.isdigit())
    if len(digits) != 13:
        raise ValueError("CNIC must contain exactly 13 digits.")
    return digits


def build_alias(full_name: str) -> str:
    words = [_clean_word(word) for word in normalize_full_name(full_name).split()]
    words = [word for word in words if word]
    if not words:
        raise ValueError("Full name is required.")
    if len(words) >= 2:
        return f"{words[0][0]}{words[1]}"
    return words[0]


def build_machine_name(full_name: str, cnic: str, *, shift_worker: bool) -> str:
    cnic = normalize_cnic(cnic)
    suffix = f"-S-{cnic}" if shift_worker else f"-{cnic}"
    alias_limit = MAX_ZKT_NAME_BYTES - len(suffix.encode("utf-8"))
    if alias_limit < 1:
        raise ValueError("CNIC suffix does not fit within the device name limit.")
    alias = _truncate_utf8(build_alias(full_name), alias_limit).strip()
    if not alias:
        raise ValueError("Full name cannot fit before CNIC within the device name limit.")
    machine_name = f"{alias}{suffix}"
    if len(machine_name.encode("utf-8")) > MAX_ZKT_NAME_BYTES:
        raise ValueError(f"Device name must fit within {MAX_ZKT_NAME_BYTES} UTF-8 bytes.")
    return machine_name


def parse_machine_identity(name: str | None) -> MachineIdentity:
    normalized = normalize_full_name(name or "")
    match = CNIC_SUFFIX_RE.match(normalized)
    if not match:
        return MachineIdentity(alias=normalized, cnic="", shift_worker=False)
    return MachineIdentity(
        alias=normalize_full_name(match.group("alias")),
        cnic=match.group("cnic"),
        shift_worker=bool(match.group("shift")),
    )


def users_matching_cnic(users: Iterable[UserLike], cnic: str) -> list[UserLike]:
    normalized_cnic = normalize_cnic(cnic)
    return [user for user in users if parse_machine_identity(user.name).cnic == normalized_cnic]


def next_numeric_user_id(users: Iterable[UserLike]) -> int:
    used: set[int] = set()
    for user in users:
        for value in (user.uid, user.user_id):
            text = str(value or "").strip()
            if text.isdigit():
                used.add(int(text))
    candidate = (max(used) + 1) if used else 1
    while candidate in used:
        candidate += 1
    return candidate


def finger_label(fid: int) -> str:
    return FINGER_LABELS.get(fid, f"Finger {fid}")


def format_finger_list(fids: Iterable[int]) -> str:
    values = sorted(set(int(fid) for fid in fids))
    if not values:
        return "No fingers enrolled"
    return ", ".join(finger_label(fid) for fid in values)


def _clean_word(value: str) -> str:
    return "".join(char for char in value if char.isalnum())


def _truncate_utf8(value: str, byte_limit: int) -> str:
    output: list[str] = []
    used = 0
    for char in value:
        char_bytes = len(char.encode("utf-8"))
        if used + char_bytes > byte_limit:
            break
        output.append(char)
        used += char_bytes
    return "".join(output)

