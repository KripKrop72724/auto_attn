"""Public device status: is each device's attendance reaching ADD?

The /status page and its API are for management and other systems such as
APEX dashboards, with no sign-in. Each device is Online or Offline, with the
time that state began, and nothing else:

* Offline when ADD cannot receive the device's attendance: the ESP is not
  connected (health tier OFFLINE), or the ESP is up but cannot read its
  terminal (terminal link DISCONNECTED, which device_health reports only after
  terminal_disconnected_warning_seconds; ERROR; or no terminal bound).
* Online otherwise. DEGRADED and ONLINE_WITH_WARNINGS devices are Online: their
  attendance still arrives.

A change must hold for public_status_{offline,online}_grace_seconds before the
page shows it, so an ESP reconnect, a planned terminal restart or an ADD
restart never makes a device flicker or resets its "since". Pending changes
live in memory, so after a restart the grace starts again. A device seen for
the first time is saved only after offline_startup_grace_seconds of process
uptime, when the ESPs dropped by the restart have reconnected. Spare
connectors are not listed.

Names: "LF-" marks a State Life Life zone device and anything else is Health;
"LF-ZONE-BLD9-03" reads "Building 9 · Device 3".
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from zk_add.db import session_scope
from zk_add.device_health import evaluate_health_batch
from zk_add.models import Connector, PublicDeviceStatus
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, iso_utc, utc_now

logger = logging.getLogger(__name__)

ONLINE, OFFLINE = "online", "offline"
ZONES = (("life", "Life"), ("health", "Health"))
TERMINAL_DOWN = frozenset({"DISCONNECTED", "ERROR"})
LOCATIONS = {"SLICTOWER": "SLIC Tower"}
HEARTBEAT_SECONDS = 30


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


def _natural(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def observed_status(connector: Connector, health) -> tuple[str, datetime | None]:
    """Online or Offline now, and when the evidence says that condition began."""
    link = health.terminal_link
    if health.tier == "OFFLINE":
        return OFFLINE, connector.last_seen_at
    if link["state"] in TERMINAL_DOWN or link["reason"] == "NO_TERMINAL":
        return OFFLINE, link["since"]
    return ONLINE, link["since"] if link["state"] == "CONNECTED" else None


def _bounded(value: datetime | None, floor: datetime | None, now: datetime) -> datetime:
    value = ensure_utc(value) if value else now
    if floor is not None and value < ensure_utc(floor):
        value = ensure_utc(floor)
    return min(value, now)


class StatusTracker:
    """Applies the grace periods and keeps each device's "since" in the database."""

    def __init__(self, started_at: datetime | None = None) -> None:
        self.started_at = started_at or utc_now()
        # connector pk -> (status, first observed at, evidence since)
        self._pending: dict[int, tuple[str, datetime, datetime | None]] = {}
        self._lock = threading.Lock()

    def refresh(self, now: datetime | None = None) -> dict:
        with self._lock, session_scope() as session:
            return self.refresh_in(session, now or utc_now())

    def refresh_in(self, session: Session, now: datetime) -> dict:
        connectors = session.scalars(
            select(Connector)
            .where(Connector.active.is_(True), Connector.is_spare.is_(False))
            .options(selectinload(Connector.zkt_device))
            .order_by(Connector.id)
        ).all()
        healths = evaluate_health_batch(session, connectors, now=now)
        stored = {row.connector_id: row for row in session.scalars(select(PublicDeviceStatus))}
        grace = {OFFLINE: timedelta(seconds=settings.public_status_offline_grace_seconds),
                 ONLINE: timedelta(seconds=settings.public_status_online_grace_seconds)}
        warming = now < self.started_at + timedelta(seconds=settings.offline_startup_grace_seconds)
        rows = []
        for connector in connectors:
            status, evidence = observed_status(connector, healths[connector.id])
            row = stored.get(connector.id)
            if row is None:
                row = PublicDeviceStatus(connector_id=connector.id, status=status,
                                         since=_bounded(evidence, None, now), updated_at=now)
                if not warming:
                    session.add(row)
            elif row.status == status:
                self._pending.pop(connector.id, None)
            else:
                pending = self._pending.get(connector.id)
                if pending is None or pending[0] != status:
                    pending = self._pending[connector.id] = (status, now, evidence)
                if now - pending[1] >= grace[status]:
                    # Offline since the last contact or terminal loss; Online
                    # since ADD first saw attendance could flow again.
                    since = pending[2] if status == OFFLINE else pending[1]
                    row.status, row.since, row.updated_at = status, _bounded(since, row.since, now), now
                    self._pending.pop(connector.id, None)
            rows.append((connector, row.status, ensure_utc(row.since)))
        current = {connector.id for connector in connectors}
        for connector_id in set(self._pending) - current:
            del self._pending[connector_id]
        return build_snapshot(rows, now)


def build_snapshot(rows, now: datetime) -> dict:
    named = [(device_name(connector.display_name or connector.zone_id), connector, status, since)
             for connector, status, since in rows]
    numbered: dict[tuple[str, str], int] = {}
    for name, *_ in named:
        if name.number is not None:
            numbered[(name.zone, name.location)] = numbered.get((name.zone, name.location), 0) + 1
    zone_rank = {key: rank for rank, (key, _title) in enumerate(ZONES)}
    devices = []
    for name, connector, status, since in sorted(named, key=lambda item: (
            zone_rank[item[0].zone], _natural(item[0].location), _natural(item[0].unit or ""),
            item[1].display_name or "")):
        shown = name.unit and (name.number is None or numbered[(name.zone, name.location)] > 1)
        devices.append({
            "name": f"{name.location} · {name.unit}" if shown else name.location,
            "location": name.location,
            "unit": name.unit if shown else None,
            "zone": name.zone,
            "code": connector.display_name,
            "status": status,
            "since": iso_utc(since),
        })
    return _summarize(devices, iso_utc(now))


def _summarize(devices: list[dict], updated_at: str) -> dict:
    zones = []
    for key, title in ZONES:
        members = [device for device in devices if device["zone"] == key]
        online = sum(device["status"] == ONLINE for device in members)
        zones.append({"zone": key, "name": title, "total": len(members), "online": online,
                      "offline": len(members) - online})
    online = sum(device["status"] == ONLINE for device in devices)
    return {"updated_at": updated_at, "total": len(devices), "online": online,
            "offline": len(devices) - online, "zones": zones, "devices": devices}


def for_zone(snapshot: dict, zone: str | None) -> dict:
    if zone is None:
        return snapshot
    result = _summarize([device for device in snapshot["devices"] if device["zone"] == zone],
                        snapshot["updated_at"])
    result["zones"] = [row for row in result["zones"] if row["zone"] == zone]
    return result


class StatusHub:
    """The latest snapshot, pushed to live page and API subscribers when it changes."""

    def __init__(self) -> None:
        self.snapshot: dict | None = None
        self._subscribers: set[asyncio.Queue[dict]] = set()

    def publish(self, snapshot: dict) -> None:
        previous, self.snapshot = self.snapshot, snapshot
        if previous is not None and _content(previous) == _content(snapshot):
            return
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()  # only the newest snapshot matters
            queue.put_nowait(snapshot)

    async def current(self) -> dict:
        if self.snapshot is None:
            self.publish(await asyncio.to_thread(tracker.refresh))
        return self.snapshot

    def full(self) -> bool:
        return len(self._subscribers) >= settings.public_status_max_streams

    async def updates(self) -> AsyncIterator[dict]:
        """The current snapshot, then each change; unchanged, it is resent as a heartbeat.

        A subscriber exists only while the generator runs, so a response that
        never starts streaming holds no slot.
        """
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield await self.current()
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield self.snapshot
        finally:
            self._subscribers.discard(queue)


def _content(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "updated_at"}


async def public_status_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            hub.publish(await asyncio.to_thread(tracker.refresh))
        except Exception:
            logger.exception("Public status refresh failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.public_status_refresh_seconds)
        except asyncio.TimeoutError:
            pass


tracker = StatusTracker()
hub = StatusHub()
