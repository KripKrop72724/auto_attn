"""Read-side helpers for the Software updates page (UI/UX revamp ADV-17).

Filters, an attention-first order and plain fields for the update
(campaign) list. Nothing here changes a campaign, a deployment or a release.

* ``attention``: a paused update whose pause came from a device's failed or
  undone update, or a live (active or paused) update with a failed or
  undone device.
* ``waiting``: a live update with no device updating right now and a device
  queued with a hold (for example while it is offline), the same rule as
  the row's ``waiting`` object.
* ``pause_kind`` / ``pause_code`` / ``pause_device_name`` read the stored
  ``pause_reason`` (written by ``ota.deployment_pause_reason``, the one-click
  controls and the release revoke) so the page never parses English.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.orm import Session

from zk_add import connector_naming
from zk_add.models import Connector
from zk_add.ota import (
    ACTIVE_DEPLOYMENT_STATES,
    CAMPAIGN_CONTROL_REASONS,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
)
from zk_add.public_status import device_name
from zk_add.terminal_families import release_family

DEVICE_OUTCOME_REASON = re.compile(r"^(.+?): (FAILED|ROLLED_BACK) \(([^()]*)\)$")
LIVE_STATES = ("ACTIVE", "PAUSED")
FAILED_DEPLOYMENT_STATES = ("FAILED", "ROLLED_BACK")


def attention_condition():
    device_pause = and_(
        FirmwareCampaign.status == "PAUSED",
        or_(FirmwareCampaign.pause_reason.like("%: FAILED (%)"),
            FirmwareCampaign.pause_reason.like("%: ROLLED_BACK (%)")),
    )
    failed_device = and_(
        FirmwareCampaign.status.in_(LIVE_STATES),
        exists().where(
            FirmwareDeployment.campaign_id == FirmwareCampaign.id,
            FirmwareDeployment.status.in_(FAILED_DEPLOYMENT_STATES),
        ),
    )
    return or_(device_pause, failed_device)


def waiting_condition():
    return and_(
        FirmwareCampaign.status == "ACTIVE",
        ~exists().where(
            FirmwareDeployment.campaign_id == FirmwareCampaign.id,
            FirmwareDeployment.status.in_(sorted(ACTIVE_DEPLOYMENT_STATES)),
        ),
        exists().where(
            FirmwareDeployment.campaign_id == FirmwareCampaign.id,
            FirmwareDeployment.status == "PENDING",
            FirmwareDeployment.error_code.is_not(None),
        ),
    )


def attention_order() -> tuple:
    """Needs attention first, then updating, then newest."""

    return (
        case((attention_condition(), 0), else_=1),
        case((FirmwareCampaign.status == "ACTIVE", 0), else_=1),
        FirmwareCampaign.id.desc(),
    )


def created_window(created_from: datetime | None, created_to: datetime | None) -> list:
    clauses = []
    if created_from is not None:
        clauses.append(FirmwareCampaign.created_at >= created_from)
    if created_to is not None:
        clauses.append(FirmwareCampaign.created_at < created_to)
    return clauses


def zone_name_search(term: str):
    """Search also matches the zone's stored name (``Connector.zone_name``)."""

    return FirmwareCampaign.zone_id.in_(
        select(Connector.zone_id).where(Connector.zone_name.ilike(term))
    )


def _zone_connectors(session: Session, zone_ids: set[str]) -> dict[str, list[Connector]]:
    grouped: dict[str, list[Connector]] = {zone: [] for zone in zone_ids}
    if zone_ids:
        for connector in session.scalars(
            select(Connector).where(Connector.zone_id.in_(sorted(zone_ids))).order_by(Connector.id)
        ).all():
            grouped[connector.zone_id].append(connector)
    return grouped


def pause_fields(pause_reason: str | None, names: dict[str, str]) -> dict:
    """``pause_kind``, ``pause_code`` and the paused device's plain name."""

    reason = pause_reason or ""
    if not reason:
        return {"pause_kind": None, "pause_code": None, "pause_device_connector_id": None,
                "pause_device_name": None}
    match = DEVICE_OUTCOME_REASON.match(reason)
    if match:
        connector_id, state, code = match.groups()
        return {
            "pause_kind": "DEVICE_ROLLED_BACK" if state == "ROLLED_BACK" else "DEVICE_FAILED",
            "pause_code": code or "UNKNOWN",
            "pause_device_connector_id": connector_id,
            "pause_device_name": names.get(connector_id),
        }
    if reason == CAMPAIGN_CONTROL_REASONS["pause"]:
        kind = "ONE_CLICK"
    elif reason.startswith("Release revoked:"):
        kind = "RELEASE_REVOKED"
    else:
        kind = "OTHER"
    return {"pause_kind": kind, "pause_code": None, "pause_device_connector_id": None, "pause_device_name": None}


def decorate_rows(session: Session, rows: list[dict]) -> list[dict]:
    """Add ``place_name``, ``division`` and the pause fields to campaign rows."""

    grouped = _zone_connectors(session, {row["zone_id"] for row in rows if row.get("zone_id")})
    paused_ids = {
        match.group(1)
        for row in rows
        if (match := DEVICE_OUTCOME_REASON.match(row.get("pause_reason") or ""))
    }
    names = {
        connector.connector_id: connector_naming.friendly_name(connector)
        for connector in session.scalars(
            select(Connector).where(Connector.connector_id.in_(sorted(paused_ids)))
        ).all()
    } if paused_ids else {}
    decorated = []
    for row in rows:
        zone = row.get("zone_id") or ""
        members = [connector for connector in grouped.get(zone, []) if not connector.is_spare] or grouped.get(
            zone, [])
        if len(members) == 1:
            place = connector_naming.friendly_name(members[0])
            division = connector_naming.connector_division(members[0])
        else:
            place = device_name(zone).location
            division = (connector_naming.connector_division(members[0]) if members
                        else connector_naming.code_division(zone))
        decorated.append({
            **row,
            "place_name": place,
            "division": division,
            **pause_fields(row.get("pause_reason"), names),
        })
    return decorated


def release_ids_for_family(session: Session, family: str) -> list[int]:
    ids = []
    for release_pk, manifest in session.execute(select(FirmwareRelease.id, FirmwareRelease.manifest)).all():
        try:
            if release_family(manifest or {}) == family:
                ids.append(release_pk)
        except ValueError:
            continue
    return ids
