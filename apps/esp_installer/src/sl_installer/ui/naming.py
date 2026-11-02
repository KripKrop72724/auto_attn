"""Names and checks the screens show: board IDs, hotspot names, public names.

Pure Python (no Qt), so it is easy to test. ``public_name`` mirrors
``device_name`` in apps/add_backend/zk_add/public_status.py: the ZONE NAME a
board is set up with becomes its name on the public status page, and an "LF"
prefix followed by -, _ or a space puts it in the Life group.

The field checks follow build_provisioning_package.validate_request and
provision_zone_lite.nvs_rows, so a mistake is caught on the form instead of
at head office. Messages are plain words for the person typing.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from ..core import api

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOTSPOT_PREFIX = "SLIC ATTENDANCE-"
SETUP_PAGE_ADDRESS = "http://192.168.254.1"

LIFE = "Life"
HEALTH = "Health"

_LOCATIONS = {"SLICTOWER": "SLIC Tower"}
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


# ------------------------------------------------------------------ board IDs


def mac_bytes(mac: Optional[str]) -> Optional[bytes]:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(digits) != 12:
        return None
    return bytes.fromhex(digits)


def board_id(mac: Optional[str]) -> str:
    """The board's ID as people read it: AC:27:6E:A4:3D:30."""
    raw = mac_bytes(mac)
    if raw is None:
        return (mac or "").upper()
    return ":".join(f"{byte:02X}" for byte in raw)


def hotspot_name(mac: Optional[str]) -> Optional[str]:
    """The board's own Wi-Fi name (firmware setup_portal.c).

    The hotspot uses the board's Wi-Fi access point address: the board ID
    with 1 added to the last byte (no carry), and its last two bytes go into
    the name, e.g. ac:27:6e:a4:3d:30 -> SLIC ATTENDANCE-3D31.
    """
    raw = mac_bytes(mac)
    if raw is None:
        return None
    return f"{HOTSPOT_PREFIX}{raw[4]:02X}{(raw[5] + 1) & 0xFF:02X}"


# --------------------------------------------------------------- public name


@dataclass(frozen=True)
class PublicName:
    group: str  # LIFE or HEALTH
    location: str  # "Building 9"
    unit: Optional[str]  # "Device 3" or "13th Floor"
    number: Optional[int]  # a numbered unit is shown only when its place has several


def _ordinal(value: int) -> str:
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _location(code: str) -> str:
    compact = re.sub(r"[\s_-]+", "", code)
    if compact in _LOCATIONS:
        return _LOCATIONS[compact]
    building = re.fullmatch(r"(G&?P)?BLD(\d+)", compact)
    if building:
        name = f"Building {int(building.group(2))}"
        return f"Group & Pension {name}" if building.group(1) else name
    return " ".join(word.capitalize() for word in re.split(r"[\s_-]+", code) if word)


def public_name_parts(code: str) -> PublicName:
    value = (code or "").strip().upper()
    life = re.match(r"LF[\s_-]+", value)
    group = LIFE if life else HEALTH
    rest = re.sub(r"^ZONE[\s_-]+", "", value[life.end() :] if life else value)
    floor = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})\s*FL", rest)
    if floor:
        return PublicName(
            group, _location(floor.group(1)), f"{_ordinal(int(floor.group(2)))} Floor", None
        )
    numbered = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})", rest)
    if numbered:
        number = int(numbered.group(2))
        return PublicName(group, _location(numbered.group(1)), f"Device {number}", number)
    return PublicName(group, _location(rest) or (code or "").strip(), None, None)


def public_name(code: str, others: Iterable[str] = ()) -> str:
    """The name the public status page will show for ``code``.

    ``others`` are the names of devices ADD already knows; like the status
    page, "Device N" is added only when the place has several numbered ones.
    """
    name = public_name_parts(code)
    if not name.unit:
        return name.location
    if name.number is not None:
        mine = (code or "").strip().upper()
        siblings = 1
        for other in others:
            if (other or "").strip().upper() == mine:
                continue
            parts = public_name_parts(other)
            if parts.number is not None and (parts.group, parts.location) == (
                name.group,
                name.location,
            ):
                siblings += 1
        if siblings < 2:
            return name.location
    return f"{name.location} · {name.unit}"


def group_of(code: str) -> str:
    return public_name_parts(code).group


# ------------------------------------------------------------- device number


def zone_for(zone_id: str, zones: Sequence[api.ZoneSummary]) -> Optional[api.ZoneSummary]:
    wanted = (zone_id or "").strip().upper()
    for zone in zones:
        if zone.zone_id.strip().upper() == wanted:
            return zone
    return None


def next_free_device_id(zone_id: str, zones: Sequence[api.ZoneSummary], fallback: str = "1") -> str:
    """The smallest device number not yet used in the zone."""
    zone = zone_for(zone_id, zones)
    if zone is None:
        return fallback
    used = {int(value) for value in zone.device_ids if str(value).strip().isdigit()}
    number = 1
    while number in used:
        number += 1
    return str(number)


# -------------------------------------------------------------------- checks


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def zone_id_problem(value: str) -> Optional[str]:
    if not value:
        return "Type the ZONE ID. Head office gives you this."
    if utf8_len(value) > 64:
        return "The ZONE ID is too long. Use 64 characters or fewer."
    if any(char.isspace() for char in value):
        return "Do not use spaces. Use dashes instead, for example LF-ZONE-BLD9-03."
    if not IDENTIFIER.match(value):
        return (
            "Use only letters, numbers, dots, dashes and underscores. "
            "Start with a letter or a number."
        )
    return None


def zone_name_problem(value: str) -> Optional[str]:
    if not value:
        return "Type the ZONE NAME."
    if value != value.strip():
        return "Remove the spaces at the start or the end."
    if utf8_len(value) > 120:
        return "The ZONE NAME is too long. Use 120 characters or fewer."
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "The ZONE NAME has a character that cannot be used."
    return None


def device_id_problem(value: str) -> Optional[str]:
    if not value:
        return "Type the device number, for example 1."
    if utf8_len(value) > 31 or not IDENTIFIER.match(value):
        return "Use a short number, for example 1."
    return None


def wifi_name_problem(value: str) -> Optional[str]:
    if not value:
        return "Type the Wi-Fi name."
    if utf8_len(value) > 32:
        return "The Wi-Fi name is too long. Use 32 characters or fewer."
    return None


def wifi_password_problem(value: str) -> Optional[str]:
    if re.fullmatch(r"[0-9A-Fa-f]{64}", value or ""):
        return None
    if not value:
        return "Type the Wi-Fi password."
    if utf8_len(value) < 8:
        return "The Wi-Fi password must have at least 8 characters."
    if utf8_len(value) > 63:
        return "The Wi-Fi password is too long. Use 63 characters or fewer."
    return None


def number_in_range(text: str, low: int, high: int) -> Optional[int]:
    value = (text or "").strip()
    if not value.isdigit():
        return None
    number = int(value)
    return number if low <= number <= high else None


def port_problem(text: str) -> Optional[str]:
    if number_in_range(text, 1, 65535) is None:
        return "Use a number from 1 to 65535."
    return None


def comm_key_problem(text: str) -> Optional[str]:
    if number_in_range(text, 0, 0xFFFFFFFF) is None:
        return "Use numbers only, for example 0."
    return None


def lan_ipv4(text: str) -> bool:
    """True for a private (office network) IPv4 address such as 192.168.1.20."""
    try:
        address = ipaddress.IPv4Address((text or "").strip())
    except ValueError:
        return False
    return any(address in network for network in _LAN_NETWORKS)


def preferred_ip_problem(text: str) -> Optional[str]:
    if (text or "").strip() == "0.0.0.0" or lan_ipv4(text):
        return None
    return "Use the machine's address, like 192.168.1.20, or 0.0.0.0 to let the board find it."


def machine_ip_problem(text: str) -> Optional[str]:
    if not (text or "").strip():
        return "Type the attendance machine's address, for example 192.168.1.20."
    if not lan_ipv4(text):
        return "Use the address shown on the machine, for example 192.168.1.20."
    return None


def text_problem(value: str, what: str, max_bytes: int, required: bool = True) -> Optional[str]:
    if not value:
        return f"Type the {what}." if required else None
    if utf8_len(value) > max_bytes:
        return f"The {what} is too long."
    if "\x00" in value:
        return f"The {what} has a character that cannot be used."
    return None


# ------------------------------------------------------------------- version


def version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for piece in re.split(r"[.\-+]", (text or "").strip()):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def app_is_too_old(app_version: str, min_version: str) -> bool:
    need = version_tuple(min_version)
    return bool(need) and version_tuple(app_version) < need
