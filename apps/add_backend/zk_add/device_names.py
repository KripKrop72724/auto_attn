"""Device names in plain words, and the Life / Health division.

One rule for every screen: the public /status page, the installer and the
signed-in dashboard rows all name a device from its code here.

* "LF" followed by a space, "_" or "-" marks a State Life Life device; every
  other code is Health ("LFZONE-..." without a separator stays Health).
* "LF-ZONE-BLD9-03" reads "Building 9 · Device 3"; "ZONE-SLICTOWER-13FL" reads
  "SLIC Tower · 13th Floor". A device number is shown only where a place has
  several numbered devices among the listed (active, non-spare) devices, so a
  place with one device reads just "Swat".
* A spare adds " · Spare", which also tells apart a spare that shares its code
  with a device in use.

No database access here: the listed fleet is remembered in memory by whoever
already loads it (the /status refresh and the device list), and naming_for
only reads that memory, so it is safe at the end of every heartbeat.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

ZONES = (("life", "Life"), ("health", "Health"))
LOCATIONS = {"SLICTOWER": "SLIC Tower"}
# The city of a place that is not itself a city. Every other place is named
# after its city ("Lahore", "Swat").
CITY_BY_PLACE = {"SLIC Tower": "Islamabad"}
KARACHI_HEAD_OFFICE = re.compile(r"(Group & Pension )?Building \d+")

Division = Literal["life", "health"]


@dataclass(frozen=True)
class DeviceName:
    zone: str
    location: str
    unit: str | None  # "Device 3" or "13th Floor"
    number: int | None  # a numbered unit is named only when its location has several


def _ordinal(value: int) -> str:
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _location(code: str) -> str:
    compact = re.sub(r"[\s_-]+", "", code)
    if compact in LOCATIONS:
        return LOCATIONS[compact]
    building = re.fullmatch(r"(G&?P)?BLD(\d+)", compact)
    if building:
        name = f"Building {int(building.group(2))}"
        return f"Group & Pension {name}" if building.group(1) else name
    return " ".join(word.capitalize() for word in re.split(r"[\s_-]+", code) if word)


def device_name(code: str) -> DeviceName:
    value = (code or "").strip().upper()
    life = re.match(r"LF[\s_-]+", value)
    zone = "life" if life else "health"
    rest = re.sub(r"^ZONE[\s_-]+", "", value[life.end():] if life else value)
    floor = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})\s*FL", rest)
    if floor:
        return DeviceName(zone, _location(floor.group(1)), f"{_ordinal(int(floor.group(2)))} Floor", None)
    numbered = re.fullmatch(r"(.+?)[\s_-]+(\d{1,3})", rest)
    if numbered:
        number = int(numbered.group(2))
        return DeviceName(zone, _location(numbered.group(1)), f"Device {number}", number)
    return DeviceName(zone, _location(rest) or (code or "").strip(), None, None)


def natural(text: str) -> list:
    """A sort key that puts "Device 2" before "Device 10"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def device_code(connector) -> str:
    """The code a device is named from: its display name, else its zone id."""
    return connector.display_name or connector.zone_id or ""


def division(code: str) -> Division:
    return device_name(code).zone  # type: ignore[return-value]


def numbered_places(codes: Iterable[str]) -> dict[tuple[str, str], int]:
    """How many numbered devices each (division, place) has among these codes."""
    numbered: dict[tuple[str, str], int] = {}
    for code in codes:
        name = code if isinstance(code, DeviceName) else device_name(code)
        if name.number is not None:
            key = (name.zone, name.location)
            numbered[key] = numbered.get(key, 0) + 1
    return numbered


def unit_shown(name: DeviceName, numbered: Mapping[tuple[str, str], int] | None, *,
               listed: bool = True) -> bool:
    """Whether the name includes the unit.

    A floor is always shown. A device number is shown where its place has
    several numbered devices; a device that is not among the counted ones
    (listed=False) counts itself. With no fleet known yet (numbered is None,
    just after a restart) the number is shown: the safe direction.
    """
    if not name.unit:
        return False
    if name.number is None or numbered is None:
        return True
    return numbered.get((name.zone, name.location), 0) + (0 if listed else 1) > 1


def friendly_name(name: DeviceName, numbered: Mapping[tuple[str, str], int] | None, *,
                  spare: bool = False, listed: bool = True) -> str:
    # A spare is never counted and never counts itself: "Building 1 · Spare".
    shown = unit_shown(name, numbered, listed=listed or spare)
    text = f"{name.location} · {name.unit}" if shown else name.location
    return f"{text} · Spare" if spare else text


def city(name: DeviceName) -> str:
    if name.location in CITY_BY_PLACE:
        return CITY_BY_PLACE[name.location]
    if KARACHI_HEAD_OFFICE.fullmatch(name.location):
        return "Karachi"
    return name.location


# The listed fleet (active, non-spare connectors, the /status set), kept in
# memory: (numbered places, connector primary keys). None until first seen.
_fleet: tuple[dict[tuple[str, str], int], frozenset[int]] | None = None
_fleet_lock = threading.Lock()


def remember_fleet(connectors: Iterable) -> None:
    """Remember the listed fleet from connectors the caller already loaded.

    Inactive and spare connectors in the iterable are skipped, so a caller can
    pass every row it has.
    """
    global _fleet
    listed = [row for row in connectors if row.active and not row.is_spare]
    value = (numbered_places(device_code(row) for row in listed), frozenset(row.id for row in listed))
    with _fleet_lock:
        _fleet = value


def forget_fleet() -> None:
    """Drop the remembered fleet (tests, and a process that has not loaded it yet)."""
    global _fleet
    with _fleet_lock:
        _fleet = None


def naming_for(connector) -> dict:
    """The plain naming fields of one device row. Never queries."""
    fleet = _fleet
    name = device_name(device_code(connector))
    spare = bool(connector.is_spare)
    if fleet is None:
        numbered, listed = None, True
    else:
        numbered, members = fleet
        listed = connector.id in members
    return {
        "division": name.zone,
        "friendly_name": friendly_name(name, numbered, spare=spare, listed=listed),
        "place": name.location,
        "unit": name.unit,
        "city": city(name),
    }
