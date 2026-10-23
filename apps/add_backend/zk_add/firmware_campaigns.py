"""One-click firmware campaign operations: several zones at once, archive, clear history.

None of this changes an OTA admission or safety guard. Every campaign is still
created by ota.create_campaign from a fresh signed scope preview (zone
serialization, HIL target order and schedule, stability override digest), and
every offer still passes assignment_for_connector. There is no fleet-wide limit
on simultaneous upgrades; a campaign whose device is offline is created and
waits (deployment PENDING), exactly like a campaign created on its own.

Archiving only hides a finished campaign; its deployments, events and grants
are untouched. Clearing history deletes a campaign only when nothing about it
can be evidence for another check (see _deletable).
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from zk_add.audit import append_audit
from zk_add.db import database_busy
from zk_add.models import Connector
from zk_add.ota import (
    ONE_CLICK_REASON,
    TERMINAL_DEPLOYMENT_STATES,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareDownloadGrant,
    FirmwareEvent,
    FirmwareHilRun,
    FirmwareRelease,
    complete_if_succeeded,
    control_campaign,
    create_campaign,
    preview_campaign_scope,
)
from zk_add.time_utils import ensure_utc, utc_now

BULK_ZONE_LIMIT = 30
LIVE_STATES = ("ACTIVE", "PAUSED")
CLOSED_STATES = ("CANCELLED", "COMPLETED")
# An install past its download: cancelling the campaign would end the
# heartbeat's rollback watch for it (service.py watches live campaigns only).
MID_INSTALL_STATES = frozenset({"VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"})
MID_INSTALL_FRESH = timedelta(hours=6)
NEVER_OFFERED_STATES = frozenset({"PENDING", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"})
# Events a never-offered attempt may carry that no other check reads. A HIL
# scope or factory-trial reservation is sealed evidence and is never deleted.
DISPOSABLE_EVENTS = frozenset({"STABILITY_WARNINGS_OVERRIDDEN", "RETRY_REQUESTED"})
# zkt_factory_trial refuses a connector with any 2.7.0 writer attempt on record.
WRITER_HISTORY_VERSIONS = frozenset({"2.7.0"})
HISTORY_LIMIT = 2000
CHUNK = 400
CLEAR_REASON = "Cleared from firmware history"
CLOSE_REASON = "Closed for a new one-click update"


class ZoneBusy(ValueError):
    """The zone already runs a campaign with a device still in progress or queued."""


def _chunks(values: Iterable[Any], size: int = CHUNK) -> Iterable[list[Any]]:
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def zone_idempotency_key(idempotency_key: str, zone_id: str) -> str:
    """One stable key per zone of a bulk request, within the 120-character column."""
    return "bulk:" + hashlib.sha256(f"{idempotency_key}\n{zone_id}".encode()).hexdigest()[:56]


def _audit(session: Session, actor: str, action: str, campaign: FirmwareCampaign, **after: Any) -> None:
    append_audit(session, actor=actor, action=action, target_type="firmware_campaign",
                 target_id=campaign.campaign_id, outcome=campaign.status,
                 after={"zone_id": campaign.zone_id, **after})


def _complete(session: Session, campaign: FirmwareCampaign) -> bool:
    """Lock the deployments (record_progress order), then complete when all SUCCEEDED."""
    session.scalars(select(FirmwareDeployment.id).where(FirmwareDeployment.campaign_id == campaign.id)
                    .order_by(FirmwareDeployment.id).with_for_update()).all()
    session.flush()
    return complete_if_succeeded(session, campaign)


def _cancel(session: Session, campaign: FirmwareCampaign, *, actor: str, reason: str, source: str) -> int:
    campaign, failed = control_campaign(session, campaign_public_id=campaign.campaign_id, action="cancel",
                                        reason=reason)
    _audit(session, actor, "FIRMWARE_CAMPAIGN_CANCEL", campaign, reason=reason, source=source,
           **({"held_downloads_failed": failed} if failed else {}))
    return failed


def _statuses(session: Session, campaign: FirmwareCampaign) -> list[str]:
    return list(session.scalars(select(FirmwareDeployment.status).where(
        FirmwareDeployment.campaign_id == campaign.id).execution_options(populate_existing=True)))


def _free_zone(session: Session, zone_id: str, *, actor: str) -> list[dict[str, str]]:
    """Close the zone's finished live campaign so a new one can start; ZoneBusy if one still runs."""
    closed = []
    for campaign in session.scalars(select(FirmwareCampaign).where(
            FirmwareCampaign.zone_id == zone_id, FirmwareCampaign.status.in_(LIVE_STATES))
            .order_by(FirmwareCampaign.id)).all():
        statuses = _statuses(session, campaign)
        running = [status for status in statuses if status not in TERMINAL_DEPLOYMENT_STATES]
        if running:
            raise ZoneBusy(f"Zone {zone_id} already runs campaign {campaign.campaign_id} "
                           f"({len(running)} device{'s' if len(running) != 1 else ''} queued or updating).")
        if statuses and _complete(session, campaign):
            _audit(session, actor, "FIRMWARE_CAMPAIGN_COMPLETED", campaign, source="one-click update")
        else:
            _cancel(session, campaign, actor=actor, reason=CLOSE_REASON, source="one-click update")
        closed.append({"campaign_id": campaign.campaign_id, "status": campaign.status})
    return closed


def _fleet_hold(session: Session, campaign: FirmwareCampaign, release: FirmwareRelease) -> str | None:
    """Why the zone's first device would wait right now (read only; offers re-check everything)."""
    from zk_add.zkt_ota_admission import pending_offer_hold

    first = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id,
                                                            FirmwareDeployment.status == "PENDING")
                           .order_by(FirmwareDeployment.id).limit(1))
    connector = session.get(Connector, first.connector_id) if first is not None else None
    if connector is None:
        return None
    return pending_offer_hold(session, connector, release)


def _create_zone(session: Session, *, release_public_id: str, zone_id: str, actor: str,
                 key: str) -> dict[str, Any]:
    replay = session.scalar(select(FirmwareCampaign).where(FirmwareCampaign.actor == actor,
                                                           FirmwareCampaign.idempotency_key == key))
    if replay is not None:
        release = session.get(FirmwareRelease, replay.release_id)
        if replay.zone_id != zone_id or release is None or release.release_id != release_public_id:
            raise ValueError("That idempotency key belongs to another firmware campaign.")
        return {"outcome": "CREATED", "campaign": replay, "release": release, "replayed": True,
                "stability_warnings": [], "closed": [], "eligible": replay.eligible_count}
    closed = _free_zone(session, zone_id, actor=actor)
    preview = preview_campaign_scope(session, release_public_id=release_public_id, zone_id=zone_id)
    if not preview["counts"]["eligible"]:
        reasons = sorted({row["reason"] for row in preview["excluded"]})
        raise ValueError("No device in this zone is eligible for this release"
                         + (f" ({', '.join(reasons)})." if reasons else "."))
    warnings = preview["stability_warnings"]
    campaign = create_campaign(
        session, release_public_id=release_public_id, zone_id=zone_id, actor=actor,
        scope_token=preview["scope_token"], idempotency_key=key, reason=ONE_CLICK_REASON,
        # The warnings are reported with the result; the override still names
        # exactly the warnings this preview computed.
        override_stability_warnings=bool(warnings),
        stability_warnings_digest=preview["stability_warnings_digest"] if warnings else None)
    session.flush()
    from zk_add.zkt_hil_schedule import campaign_audit_scope

    append_audit(session, actor=actor, action="FIRMWARE_CAMPAIGN_CREATED", target_type="zone",
                 target_id=zone_id, outcome=campaign.status,
                 after={"campaign_id": campaign.campaign_id, "release_id": release_public_id, "bulk": True,
                        **campaign_audit_scope(session, campaign),
                        **({"override_stability_warnings": True,
                            "stability_warnings_digest": preview["stability_warnings_digest"]}
                           if warnings else {})})
    release = session.get(FirmwareRelease, campaign.release_id)
    return {"outcome": "CREATED", "campaign": campaign, "release": release, "replayed": False,
            "stability_warnings": warnings, "closed": closed, "eligible": campaign.eligible_count}


def create_for_zones(session: Session, *, release_public_id: str, zone_ids: list[str], actor: str,
                     idempotency_key: str) -> dict[str, Any]:
    """Create one campaign per zone, each in its own savepoint; one zone's refusal never undoes another."""
    if not 1 <= len(zone_ids) <= BULK_ZONE_LIMIT or len(set(zone_ids)) != len(zone_ids):
        raise ValueError(f"Choose 1 to {BULK_ZONE_LIMIT} distinct zones.")
    results = []
    for zone_id in zone_ids:
        row: dict[str, Any] = {"zone_id": zone_id, "outcome": "BLOCKED", "campaign_id": None, "status": None,
                               "message": None, "stability_warnings": [], "eligible": 0, "fleet_hold": None,
                               "closed": []}
        try:
            with session.begin_nested():
                created = _create_zone(session, release_public_id=release_public_id, zone_id=zone_id,
                                       actor=actor, key=zone_idempotency_key(idempotency_key, zone_id))
        except ZoneBusy as error:
            row.update(outcome="BUSY", message=str(error))
        except (ValueError, RuntimeError) as error:
            row.update(outcome="BLOCKED", message=str(error))
        except IntegrityError:
            # The zone's partial unique index: another campaign started meanwhile.
            row.update(outcome="BUSY", message=f"Another firmware campaign started in zone {zone_id} meanwhile.")
        else:
            campaign = created["campaign"]
            row.update(outcome="CREATED", campaign_id=campaign.campaign_id, status=campaign.status,
                       stability_warnings=created["stability_warnings"], eligible=created["eligible"],
                       closed=created["closed"],
                       message="Already created by this request." if created["replayed"] else None)
            if campaign.status == "ACTIVE" and created["release"] is not None:
                row["fleet_hold"] = _fleet_hold(session, campaign, created["release"])
        results.append(row)
    return {"release_id": release_public_id, "results": results,
            "created": sum(1 for row in results if row["outcome"] == "CREATED")}


def archive_campaign(session: Session, *, campaign_public_id: str, actor: str) -> tuple[FirmwareCampaign, bool]:
    """Hide one finished campaign; returns it and whether this call archived it. Idempotent."""
    campaign = session.scalar(select(FirmwareCampaign).where(FirmwareCampaign.campaign_id == campaign_public_id)
                              .with_for_update().execution_options(populate_existing=True))
    if campaign is None:
        raise LookupError("Firmware campaign not found.")
    if campaign.status not in CLOSED_STATES:
        raise ValueError(f"Only a cancelled or completed campaign can be archived; this one is "
                         f"{campaign.status.lower()}.")
    if campaign.archived_at is not None:
        return campaign, False
    campaign.archived_at = utc_now()
    campaign.archived_by = actor
    return campaign, True


def archive_closed(session: Session, *, actor: str) -> list[str]:
    """Archive every cancelled or completed campaign not archived yet; returns their public IDs."""
    rows = list(session.execute(select(FirmwareCampaign.id, FirmwareCampaign.campaign_id).where(
        FirmwareCampaign.status.in_(CLOSED_STATES), FirmwareCampaign.archived_at.is_(None))
        .order_by(FirmwareCampaign.id)))
    now = utc_now()
    for chunk in _chunks([row.id for row in rows]):
        session.execute(update(FirmwareCampaign).where(
            FirmwareCampaign.id.in_(chunk), FirmwareCampaign.status.in_(CLOSED_STATES),
            FirmwareCampaign.archived_at.is_(None)).values(archived_at=now, archived_by=actor)
            .execution_options(synchronize_session=False))
    session.expire_all()
    return [row.campaign_id for row in rows]


def _deletable(session: Session, campaigns: list[FirmwareCampaign]) -> set[int]:
    """The closed campaigns whose deletion cannot change any OTA check's input.

    Each deployment was never offered (no bytes, no attempt, no grant), no HIL
    run names it, it carries only disposable events and it is not a 2.7.0
    writer attempt. Its connector's whole deployment history must also lie in
    campaigns deleted here: a "latest deployment" or "immediately preceding
    attempt" check elsewhere then reads the same history before and after.
    """
    if not campaigns:
        return set()
    by_campaign: dict[int, list[FirmwareDeployment]] = {campaign.id: [] for campaign in campaigns}
    for chunk in _chunks(by_campaign):
        for deployment in session.scalars(select(FirmwareDeployment).where(
                FirmwareDeployment.campaign_id.in_(chunk))):
            by_campaign[deployment.campaign_id].append(deployment)
    candidates = {
        campaign_id for campaign_id, deployments in by_campaign.items()
        if all(row.offered_at is None and row.status in NEVER_OFFERED_STATES and not row.bytes_written
               and not row.attempt_count and row.target_version not in WRITER_HISTORY_VERSIONS
               for row in deployments)
    }
    deployment_ids = {row.id: row.campaign_id for campaign_id in candidates for row in by_campaign[campaign_id]}
    for chunk in _chunks(deployment_ids):
        for model in (FirmwareDownloadGrant, FirmwareHilRun):
            for deployment_id in session.scalars(select(model.deployment_id).where(model.deployment_id.in_(chunk))):
                candidates.discard(deployment_ids[deployment_id])
        for deployment_id in session.scalars(select(FirmwareEvent.deployment_id).where(
                FirmwareEvent.deployment_id.in_(chunk), FirmwareEvent.state.not_in(DISPOSABLE_EVENTS))):
            candidates.discard(deployment_ids[deployment_id])
    connectors = {row.connector_id for campaign_id in candidates for row in by_campaign[campaign_id]}
    history: dict[int, set[int]] = {}
    for chunk in _chunks(connectors):
        for connector_id, campaign_id in session.execute(select(
                FirmwareDeployment.connector_id, FirmwareDeployment.campaign_id).where(
                FirmwareDeployment.connector_id.in_(chunk))):
            history.setdefault(connector_id, set()).add(campaign_id)
    changed = True
    while changed:
        changed = False
        for campaign_id in sorted(candidates):
            if any(not history.get(row.connector_id, set()) <= candidates for row in by_campaign[campaign_id]):
                candidates.discard(campaign_id)
                changed = True
    return candidates


def _delete(session: Session, campaign_ids: set[int]) -> int:
    """Delete in FK order: events, deployments, campaigns. Returns the campaigns deleted."""
    if not campaign_ids:
        return 0
    deployment_ids = []
    for chunk in _chunks(sorted(campaign_ids)):
        session.scalars(select(FirmwareCampaign.id).where(FirmwareCampaign.id.in_(chunk))
                        .order_by(FirmwareCampaign.id).with_for_update()).all()
        deployment_ids.extend(session.scalars(select(FirmwareDeployment.id).where(
            FirmwareDeployment.campaign_id.in_(chunk))))
    for chunk in _chunks(deployment_ids):
        session.execute(delete(FirmwareEvent).where(FirmwareEvent.deployment_id.in_(chunk))
                        .execution_options(synchronize_session=False))
        session.execute(delete(FirmwareDeployment).where(FirmwareDeployment.id.in_(chunk))
                        .execution_options(synchronize_session=False))
    deleted = 0
    for chunk in _chunks(sorted(campaign_ids)):
        deleted += session.execute(delete(FirmwareCampaign).where(
            FirmwareCampaign.id.in_(chunk), FirmwareCampaign.status.in_(CLOSED_STATES))
            .execution_options(synchronize_session=False)).rowcount or 0
    return deleted


def clear_history(session: Session, *, actor: str, now=None) -> dict[str, Any]:
    """The owner's "cancel and remove all old firmware attempts", in one call.

    1. Each live campaign: every deployment SUCCEEDED -> COMPLETED. A device
       mid-install (VERIFYING..RECONCILING) updated in the last 6 hours keeps
       its campaign running (cancelling would end the heartbeat's rollback
       watch). Anything else is cancelled through control_campaign.
    2. Each closed campaign not archived: deleted with its deployments and
       events only when _deletable allows it; otherwise archived (hidden, its
       evidence unchanged).
    Bounded per call; calling it again finishes any remainder and is a no-op after.
    """
    now = now or utc_now()
    completed, cancelled, kept, busy = [], [], [], []
    live = list(session.scalars(select(FirmwareCampaign).where(FirmwareCampaign.status.in_(LIVE_STATES))
                                .order_by(FirmwareCampaign.id).limit(HISTORY_LIMIT + 1)))
    more = len(live) > HISTORY_LIMIT
    for campaign in live[:HISTORY_LIMIT]:
        deployments = list(session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id == campaign.id).order_by(FirmwareDeployment.id)
            .execution_options(populate_existing=True)))
        if deployments and all(row.status == "SUCCEEDED" for row in deployments):
            try:
                with session.begin_nested():
                    done = _complete(session, campaign)
            except OperationalError as error:
                if not database_busy(error):
                    raise
                busy.append(campaign.campaign_id)  # A device's own transaction holds its row; next call.
                continue
            if done:
                _audit(session, actor, "FIRMWARE_CAMPAIGN_COMPLETED", campaign, source="clear-history")
                completed.append(campaign.campaign_id)
                continue
        fresh = [row for row in deployments if row.status in MID_INSTALL_STATES
                 and row.updated_at is not None and ensure_utc(row.updated_at) >= now - MID_INSTALL_FRESH]
        if fresh:
            connector = session.get(Connector, fresh[0].connector_id)
            kept.append({"campaign_id": campaign.campaign_id, "zone_id": campaign.zone_id,
                         "connector": connector.display_name if connector else None,
                         "connector_id": connector.connector_id if connector else None,
                         "state": fresh[0].status})
            continue
        try:
            with session.begin_nested():
                _cancel(session, campaign, actor=actor, reason=CLEAR_REASON, source="clear-history")
        except ValueError:
            continue  # Its state changed meanwhile (for example it just completed).
        except OperationalError as error:
            if not database_busy(error):
                raise
            busy.append(campaign.campaign_id)  # A device's own transaction holds its row; next call.
            continue
        cancelled.append(campaign.campaign_id)
    session.flush()
    closed = list(session.scalars(select(FirmwareCampaign).where(
        FirmwareCampaign.status.in_(CLOSED_STATES), FirmwareCampaign.archived_at.is_(None))
        .order_by(FirmwareCampaign.id).limit(HISTORY_LIMIT + 1)))
    more = more or len(closed) > HISTORY_LIMIT
    closed = closed[:HISTORY_LIMIT]
    removable = _deletable(session, closed)
    deleted = [campaign.campaign_id for campaign in closed if campaign.id in removable]
    archived = [campaign.campaign_id for campaign in closed if campaign.id not in removable]
    deleted_count = _delete(session, removable)
    archived_count = 0
    for chunk in _chunks([campaign.id for campaign in closed if campaign.id not in removable]):
        archived_count += session.execute(update(FirmwareCampaign).where(
            FirmwareCampaign.id.in_(chunk), FirmwareCampaign.archived_at.is_(None))
            .values(archived_at=now, archived_by=actor).execution_options(synchronize_session=False)).rowcount or 0
    session.expire_all()
    result = {"completed": len(completed), "cancelled": len(cancelled), "kept_running": kept,
              "busy": len(busy), "deleted": deleted_count, "archived": archived_count, "more": more or bool(busy)}
    append_audit(session, actor=actor, action="FIRMWARE_HISTORY_CLEARED", target_type="firmware_campaigns",
                 target_id=None, outcome="CLEARED",
                 after={**{key: value for key, value in result.items() if key != "kept_running"},
                        "kept_running": [row["campaign_id"] for row in kept],
                        "completed_ids": completed[:200], "cancelled_ids": cancelled[:200], "busy_ids": busy[:200],
                        "deleted_ids": deleted[:200], "archived_ids": archived[:200]})
    return result
