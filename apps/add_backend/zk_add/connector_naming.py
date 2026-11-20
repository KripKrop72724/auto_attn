"""Division and friendly name of a connector for the read-side list pages.

The rule is the one /status uses (``public_status.device_name``: ``LF``
followed by a space, ``_`` or ``-`` is Life, everything else is Health), so
a list filtered to "Life" holds exactly the devices /status calls Life.

The shared naming module (``device_names``, revamp step HOME-1) gives the
friendly name through ``naming_for``, including the "number only where a
place has several" rule. Before the first fleet refresh the name keeps the
unit ("Swat · Device 1"), the safe direction. Nothing here queries except
``connector_ids_for_division``.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_add.models import Connector
from zk_add.public_status import device_name

from zk_add.device_names import naming_for as _shared_naming_for

Division = Literal["life", "health"]
DIVISIONS: tuple[Division, ...] = ("life", "health")


def connector_code(connector: Connector) -> str:
    return connector.display_name or connector.zone_id or ""


def code_division(code: str | None) -> Division:
    return "life" if device_name(code or "").zone == "life" else "health"


def connector_division(connector: Connector) -> Division:
    return code_division(connector_code(connector))


def friendly_name(connector: Connector) -> str:
    try:
        shared = _shared_naming_for(connector)
    except Exception:  # pragma: no cover - never fail a list for a name
        shared = None
    if isinstance(shared, dict) and shared.get("friendly_name"):
        return str(shared["friendly_name"])
    name = device_name(connector_code(connector))
    text = f"{name.location} · {name.unit}" if name.unit else name.location
    if connector.is_spare:
        text = f"{text} · Spare"
    return text


def naming(connector: Connector) -> dict:
    """The two plain fields every list row carries for its device."""

    return {"division": connector_division(connector), "friendly_name": friendly_name(connector)}


def parse_division(value: str | None) -> Division | None:
    """``life`` / ``health`` (any case); empty or ``all`` means no filter."""

    normalized = (value or "").strip().lower()
    if normalized in {"", "all"}:
        return None
    if normalized not in DIVISIONS:
        raise ValueError("division must be life or health")
    return normalized  # type: ignore[return-value]


def connector_ids_for_division(
    db: Session, division: Division, *, include_spares: bool = True
) -> list[int]:
    """Primary keys of the connectors in one division (the table is small)."""

    statement = select(Connector.id, Connector.display_name, Connector.zone_id)
    if not include_spares:
        statement = statement.where(Connector.is_spare.is_(False))
    return [
        row.id
        for row in db.execute(statement).all()
        if code_division(row.display_name or row.zone_id) == division
    ]


def zone_ids_for_division(db: Session, division: Division) -> list[str]:
    """Zone ids of one division, for tables keyed by zone (recoveries, updates)."""

    rows = db.execute(select(Connector.display_name, Connector.zone_id)).all()
    return sorted({
        row.zone_id for row in rows
        if row.zone_id and code_division(row.display_name or row.zone_id) == division
    })
