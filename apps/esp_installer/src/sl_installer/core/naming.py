"""Names a person sees for a board: hotspot name, public name and Life/Health group.

``public_name_preview`` follows ADD's public status page
(apps/add_backend/zk_add/public_status.py, device_name), which names a device
from its ZONE NAME, so the Details screen can show the result before setup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

_LOCATIONS = {"SLICTOWER": "SLIC Tower"}
_HEX = "0123456789abcdef"


def normalize_mac(value: Optional[str]) -> Optional[str]:
    """``aa:bb:cc:dd:ee:ff`` from any 12-hex-digit spelling, else None."""
    if not value:
        return None
    digits = "".join(ch for ch in value.lower() if ch in _HEX)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def hotspot_name(mac: Optional[str]) -> Optional[str]:
    """The setup hotspot ``SLIC ATTENDANCE-XXXX``.

    The firmware's access point uses the board MAC with 1 added to the last byte,
    without carry, and names itself after the last two bytes
    (firmware/zone_lite/main/setup_portal.c).
    """
    normalized = normalize_mac(mac)
    if normalized is None:
        return None
    parts = normalized.split(":")
    fifth = int(parts[4], 16)
    sixth = (int(parts[5], 16) + 1) & 0xFF
    return f"SLIC ATTENDANCE-{fifth:02X}{sixth:02X}"


@dataclass(frozen=True)
class PublicName:
    name: str        # "Gujrat · Device 1" or "SLIC Tower · 3rd Floor"
    group: str       # "Life" or "Health"
    location: str
    unit: Optional[str]


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


def public_name_preview(zone_name: str) -> PublicName:
    """How the public status page will name a board whose ZONE NAME is ``zone_name``.

    ADD always shows the unit (device number or floor) here; the live page hides
    "Device 1" when a location has only one device.
    """
    value = (zone_name or "").strip().upper()
    life = re.match(r"LF[\s_-]+", value)
    group = "Life" if life else "Health"
    rest = re.sub(r"^ZONE[\s_-]+", "", value[life.end():] if life else value)
    floor = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})\s*FL", rest)
    if floor:
        location, unit = _location(floor.group(1)), f"{_ordinal(int(floor.group(2)))} Floor"
    else:
        numbered = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})", rest)
        if numbered:
            location, unit = _location(numbered.group(1)), f"Device {int(numbered.group(2))}"
        else:
            location, unit = _location(rest) or (zone_name or "").strip(), None
    name = f"{location} · {unit}" if unit else location
    return PublicName(name=name, group=group, location=location, unit=unit)


def suggest_zone_name(zone_id: str, known: Iterable[object] = ()) -> str:
    """ZONE NAME for a ZONE ID: the name ADD already uses, else the ID itself.

    ADD freezes a site's name at its first board, so reusing it keeps every board
    of a zone under one public name. ``known`` holds api.ZoneSummary items.
    """
    wanted = (zone_id or "").strip().upper()
    for zone in known:
        if str(getattr(zone, "zone_id", "")).upper() == wanted:
            name = str(getattr(zone, "zone_name", "") or "").strip()
            if name:
                return name
    return wanted
