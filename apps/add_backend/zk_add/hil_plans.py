"""One-approval staged HIL plans (owner decision, 21 October 2026).

The owner approves, once and with the password step-up, an ordered plan for
one HIL_ONLY release: stages, each a set of exact HIL targets that may run
together inside the existing fleet guard (at most two unsettled upgrades
nationwide, one per location; RECONCILING still holds a slot). ADD then starts
each stage's one-zone campaigns itself and advances only when every device of
the stage reports SUCCEEDED.

Nothing here is a new gate or a bypass. Each campaign is created through the
same ``preview_campaign_scope`` + ``create_campaign`` calls an operator's
request makes (exact HIL scope, storage predecessors, family, nationwide
schedule and reservation), and every offer still passes
``assignment_for_connector`` and its fleet guard. The approval replaces only
the interactive password prompt of each campaign. The plan adds stops of its
own: it HALTS (no further campaigns, never-offered campaigns paused, an alert,
an audit row and a browser event) on any FAILED, ROLLED_BACK, PAUSED or
otherwise unsuccessful deployment or campaign, a hold, a preflight refusal or
warning, an admission refusal, a changed release or plan pin, and a timeout.

Plans are immutable once approved: the release identity (release id, version,
artifact and application digests) and the exact device list are pinned,
digested and sealed with the fleet root secret, and re-verified on every
step. Only one plan is open at a time; plans expire.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.models import Connector, utc_column
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

logger = logging.getLogger(__name__)

OPEN_STATES = ("RUNNING", "PAUSED")
TERMINAL_STATES = ("COMPLETED", "HALTED", "CANCELLED", "EXPIRED")
SYSTEM_ACTOR = "system:hil-plan"
HALT_ALERT_CODE = "FIRMWARE_HIL_PLAN_HALTED"
# The fleet guard admits at most two unsettled ZKT upgrades nationwide.
MAX_STAGE_DEVICES = 2
MAX_STAGES = 18
DEFAULT_EXPIRY_HOURS = 72
MAX_EXPIRY_HOURS = 72
DEFAULT_STAGE_TIMEOUT_MINUTES = 360
MIN_STAGE_TIMEOUT_MINUTES = 30
MAX_STAGE_TIMEOUT_MINUTES = 1440
# Same two-key namespace as the ZKT assignment lock; a different key.
PLAN_LOCK_KEY = 271
# Never cancelled by a plan cancellation: bytes may already be on the ESP.
CANCEL_REFUSED_STATES = ("OFFERED", "DOWNLOADING")
UNSUCCESSFUL_TERMINAL = ("FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED")


class FirmwareHilPlan(Base):
    __tablename__ = "add_firmware_hil_plans"
    __table_args__ = (
        UniqueConstraint("approved_by", "idempotency_key", name="uq_add_hil_plan_actor_key"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_releases.id"), index=True)
    release_public_id: Mapped[str] = mapped_column(String(100))
    release_version: Mapped[str] = mapped_column(String(80))
    image_sha256: Mapped[str] = mapped_column(String(64))
    application_sha256: Mapped[str] = mapped_column(String(64))
    stages: Mapped[list] = mapped_column(JSON)
    plan_digest: Mapped[str] = mapped_column(String(64))
    seal: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING", index=True)
    # "OPEN" while RUNNING or PAUSED, NULL afterwards: one open plan at a time,
    # enforced by the database across API workers.
    open_guard: Mapped[str | None] = mapped_column(String(8), unique=True)
    current_stage: Mapped[int] = mapped_column(Integer, default=0)
    stage_entered_at: Mapped[Any] = utc_column()
    stage_started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    waiting_reason: Mapped[str | None] = mapped_column(String(200))
    halt_code: Mapped[str | None] = mapped_column(String(120))
    halt_reason: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    approved_at: Mapped[Any] = utc_column()
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True))
    stage_timeout_seconds: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[Any] = utc_column()
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))


class PlanHalt(Exception):
    def __init__(self, code: str, message: str, connector_id: str | None = None):
        super().__init__(message)
        self.code = code[:120]
        self.message = message
        self.connector_id = connector_id


# --------------------------------------------------------------------------
# Pins, digest and seal


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _pins(release: dict, stages: list[list[dict]], expires_in_hours: int,
          stage_timeout_minutes: int) -> dict:
    return {
        "release": {key: release[key] for key in
                    ("release_id", "version", "image_sha256", "application_sha256")},
        "stages": [[{key: device[key] for key in
                     ("zone_id", "connector_id", "mac", "terminal_serial", "location")}
                    for device in stage] for stage in stages],
        "expires_in_hours": expires_in_hours,
        "stage_timeout_minutes": stage_timeout_minutes,
    }


def _digest(pins: dict) -> str:
    return hashlib.sha256(_canonical(pins).encode()).hexdigest()


def _seal(plan_id: str, plan_digest: str, approved_by: str) -> str:
    from zk_add.ota import _scope_signing_key
    material = _canonical({"plan_id": plan_id, "plan_digest": plan_digest, "approved_by": approved_by})
    return hmac.new(_scope_signing_key(), material.encode(), hashlib.sha256).hexdigest()


def _stored_pins(plan: FirmwareHilPlan) -> dict:
    return _pins({"release_id": plan.release_public_id, "version": plan.release_version,
                  "image_sha256": plan.image_sha256, "application_sha256": plan.application_sha256},
                 plan.stages, int(round((ensure_utc(plan.expires_at) - ensure_utc(plan.approved_at))
                                        .total_seconds() / 3600)),
                 plan.stage_timeout_seconds // 60)


def plan_actor(plan: FirmwareHilPlan) -> str:
    return f"hil-plan:{plan.plan_id}"


def campaign_key(plan: FirmwareHilPlan, stage: int, connector_id: str) -> str:
    return f"hil-plan:{plan.plan_id}:{stage}:{connector_id}"


# --------------------------------------------------------------------------
# Resolution (preview and approval share it)


def _location(connector_id: str) -> str | None:
    from zk_add.zkt270_scope import BY_ID
    from zk_add.zkt274_bld9_k50 import reserved_location
    target = BY_ID.get(connector_id)
    return target.location if target is not None else reserved_location(connector_id)


def _release_for_plan(session: Session, release_public_id: str):
    from zk_add.ota import FirmwareRelease, _application_sha256
    from zk_add.terminal_families import release_family
    if not settings.firmware_hil_enabled:
        raise ValueError("Firmware HIL quarantine is disabled.")
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_public_id))
    if release is None or release.state != "HIL_ONLY" or release.revoked_at is not None:
        raise ValueError("A staged HIL plan requires an unrevoked HIL_ONLY release.")
    try:
        family = release_family(release.manifest or {})
    except ValueError as error:
        raise ValueError("The release firmware family is invalid.") from error
    if family != "zkt":
        raise ValueError("Staged HIL plans cover ZKT releases under the nationwide fleet guard only.")
    application = _application_sha256(release)
    if application is None:
        raise ValueError("Firmware release lacks the ESP application digest required by OTA bootstraps.")
    if not isinstance((release.manifest or {}).get("_hil_targets"), list):
        raise ValueError("A staged HIL plan requires the release's exact signed HIL target list.")
    return release, application


def _exposed_targets(release, application: str) -> list:
    from zk_add.ota import _parse_release_hil_targets
    identity = (release.release_id, release.version, release.git_sha, release.image_sha256, application)
    return _parse_release_hil_targets(identity, (release.manifest or {}).get("_hil_targets"))


def _resolve_zone(session: Session, release, targets: list, zone_id: str) -> dict:
    from zk_add.zkt274_bld9_k50 import admits
    connectors = list(session.scalars(select(Connector).where(
        Connector.zone_id == zone_id, Connector.active == True)  # noqa: E712
        .order_by(Connector.connector_id)))
    matches = [(target, row) for row in connectors for target in targets if admits(target, row)]
    if not matches:
        raise ValueError(f"{zone_id}: no active device in this zone is an exact HIL target of "
                         f"{release.release_id}.")
    if len(matches) != 1:
        raise ValueError(f"{zone_id}: more than one exact HIL target is active in this zone.")
    target, connector = matches[0]
    location = _location(target.connector_id)
    if location is None:
        raise ValueError(f"{zone_id}: the fleet guard has no location for this device.")
    return {"zone_id": zone_id, "connector_id": target.connector_id, "mac": target.mac,
            "terminal_serial": target.terminal_serial, "location": location,
            "display_name": connector.display_name}


def _preflight_problem(scope: dict, device: dict) -> PlanHalt | None:
    """A preflight that is not exactly this one device, unheld and unwarned."""
    eligible = [row["connector_id"] for row in scope.get("eligible") or []]
    if eligible != [device["connector_id"]]:
        return PlanHalt("PREFLIGHT_SCOPE_MISMATCH",
                        f"{device['zone_id']}: preflight scope is {eligible or 'empty'}, "
                        f"not the pinned device.", device["connector_id"])
    schedule = scope.get("hil_schedule") or {}
    if schedule.get("hold"):
        return PlanHalt("PREFLIGHT_HOLD", f"{device['zone_id']}: nationwide HIL scope held: "
                        f"{schedule['hold']}", device["connector_id"])
    if schedule and schedule.get("selected") not in (None, {key: device[key] for key in
                                                           ("connector_id", "mac", "terminal_serial")}):
        return PlanHalt("PREFLIGHT_HOLD", f"{device['zone_id']}: the nationwide HIL schedule selects "
                        "another target.", device["connector_id"])
    warnings = [*(scope.get("stability_warnings") or []), *(scope.get("warnings") or [])]
    if warnings:
        text_ = "; ".join(str(row.get("message") or row.get("code") or row) if isinstance(row, dict)
                          else str(row) for row in warnings)
        return PlanHalt("PREFLIGHT_WARNING", f"{device['zone_id']}: preflight warning: {text_}",
                        device["connector_id"])
    return None


def _unsettled(session: Session) -> tuple[set[str], bool]:
    from zk_add.zkt_ota_admission import reservation_snapshot
    snapshot = reservation_snapshot(session)
    occupied = {row["connector_id"] for row in snapshot["reservations"] if not row["later_installation"]}
    return occupied, bool(snapshot["scan_complete"])


def resolve_plan(session: Session, *, release_public_id: str, stages: list[list[str]],
                 expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
                 stage_timeout_minutes: int = DEFAULT_STAGE_TIMEOUT_MINUTES) -> dict:
    """Validate and pin a proposed plan. Read-only; raises ValueError on refusal."""
    from zk_add.ota import FirmwareCampaign, preview_campaign_scope
    if not 1 <= expires_in_hours <= MAX_EXPIRY_HOURS:
        raise ValueError(f"Plan expiry must be between 1 and {MAX_EXPIRY_HOURS} hours.")
    if not MIN_STAGE_TIMEOUT_MINUTES <= stage_timeout_minutes <= MAX_STAGE_TIMEOUT_MINUTES:
        raise ValueError(f"Stage timeout must be between {MIN_STAGE_TIMEOUT_MINUTES} and "
                         f"{MAX_STAGE_TIMEOUT_MINUTES} minutes.")
    if not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"A plan has between 1 and {MAX_STAGES} stages.")
    zones = [zone for stage in stages for zone in stage]
    if any(not 1 <= len(stage) <= MAX_STAGE_DEVICES for stage in stages):
        raise ValueError("Each stage runs one or two devices: the fleet guard admits at most two "
                         "unsettled upgrades nationwide.")
    if len(set(zones)) != len(zones):
        raise ValueError("A zone may appear only once in a plan.")
    release, application = _release_for_plan(session, release_public_id)
    targets = _exposed_targets(release, application)
    resolved = [[_resolve_zone(session, release, targets, zone) for zone in stage] for stage in stages]
    for index, stage in enumerate(resolved, start=1):
        locations = [device["location"] for device in stage]
        if len(set(locations)) != len(locations):
            raise ValueError(f"Stage {index}: the fleet guard runs one upgrade per location "
                             f"({', '.join(locations)}).")
    busy = list(session.execute(select(FirmwareCampaign.zone_id, FirmwareCampaign.campaign_id,
                                       FirmwareCampaign.status)
                                .where(FirmwareCampaign.zone_id.in_(zones),
                                       FirmwareCampaign.status.in_(["ACTIVE", "PAUSED"]))))
    if busy:
        raise ValueError("Finish or cancel the open campaign in each plan zone first: " + ", ".join(
            f"{zone} ({status.lower()} campaign {campaign_id})" for zone, campaign_id, status in busy))
    occupied, complete = _unsettled(session)
    if not complete:
        raise ValueError("The fleet reservation scan is incomplete; no plan can be approved.")
    held = [device["zone_id"] for stage in resolved for device in stage if device["connector_id"] in occupied]
    if held:
        raise ValueError("An earlier installation on these devices is still unsettled: " + ", ".join(held))
    by_id = {row.connector_id: row for row in session.scalars(select(Connector).where(
        Connector.connector_id.in_([device["connector_id"] for stage in resolved for device in stage])))}
    for stage in resolved:
        for device in stage:
            try:
                scope = preview_campaign_scope(session, release_public_id=release.release_id,
                                               zone_id=device["zone_id"])
            except (ValueError, RuntimeError) as error:
                raise ValueError(f"{device['zone_id']}: preflight refused: {error}") from error
            problem = _preflight_problem(scope, device)
            if problem is not None:
                raise ValueError(problem.message)
            row = by_id[device["connector_id"]]
            device.update(connected=bool(row.connected), firmware_version=row.firmware_version)
    release_pin = {"release_id": release.release_id, "version": release.version,
                   "image_sha256": release.image_sha256, "application_sha256": application}
    pins = _pins(release_pin, resolved, expires_in_hours, stage_timeout_minutes)
    return {"release": release_pin, "stages": resolved, "plan_digest": _digest(pins),
            "expires_in_hours": expires_in_hours, "stage_timeout_minutes": stage_timeout_minutes,
            "guard": {"max_parallel": MAX_STAGE_DEVICES, "one_per_location": True}}


# --------------------------------------------------------------------------
# Approval


def _postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _assignment_lock(session: Session) -> None:
    """Serialize against ZKT offers, which hold this lock through their commit.

    A never-offered deployment paused or cancelled under it cannot be offered
    by an assignment that read it as PENDING a moment earlier.
    """
    if _postgres(session):
        from zk_add.zkt_ota_admission import LOCK_KEY, LOCK_NAMESPACE
        session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                        {"namespace": LOCK_NAMESPACE, "key": LOCK_KEY})


def create_plan(session: Session, *, release_public_id: str, stages: list[list[str]], reason: str,
                typed_confirmation: str, plan_digest: str, actor: str, idempotency_key: str,
                expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
                stage_timeout_minutes: int = DEFAULT_STAGE_TIMEOUT_MINUTES) -> tuple[FirmwareHilPlan, bool]:
    """Record the owner's single approval. The caller has completed step-up."""
    replay = session.scalar(select(FirmwareHilPlan).where(
        FirmwareHilPlan.approved_by == actor, FirmwareHilPlan.idempotency_key == idempotency_key))
    if replay is not None:
        if replay.plan_digest != plan_digest or replay.release_public_id != release_public_id:
            raise ValueError("That idempotency key belongs to another HIL plan.")
        return replay, False
    if _postgres(session):
        from zk_add.zkt_ota_admission import LOCK_NAMESPACE
        session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                        {"namespace": LOCK_NAMESPACE, "key": PLAN_LOCK_KEY})
    current = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.status.in_(OPEN_STATES))
                             .execution_options(populate_existing=True))
    if current is not None:
        raise ValueError(f"HIL plan {current.plan_id} is still {current.status.lower()}; "
                         "cancel it or let it finish first.")
    resolved = resolve_plan(session, release_public_id=release_public_id, stages=stages,
                            expires_in_hours=expires_in_hours, stage_timeout_minutes=stage_timeout_minutes)
    if resolved["plan_digest"] != plan_digest:
        raise ValueError("The plan changed since it was previewed. Preview it again before approving.")
    if typed_confirmation != resolved["release"]["version"]:
        raise ValueError("Typed firmware version does not match the release.")
    from zk_add.ota import FirmwareRelease
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_public_id))
    now = utc_now()
    plan_id = secrets.token_hex(16)
    stored = [[{key: device[key] for key in ("zone_id", "connector_id", "mac", "terminal_serial",
                                             "location", "display_name")} for device in stage]
              for stage in resolved["stages"]]
    plan = FirmwareHilPlan(
        plan_id=plan_id, release_id=release.id, release_public_id=release.release_id,
        release_version=release.version, image_sha256=release.image_sha256,
        application_sha256=resolved["release"]["application_sha256"], stages=stored,
        plan_digest=plan_digest, seal=_seal(plan_id, plan_digest, actor), status="RUNNING",
        open_guard="OPEN", current_stage=0, stage_entered_at=now, reason=reason, approved_by=actor,
        idempotency_key=idempotency_key, approved_at=now, expires_at=now + timedelta(hours=expires_in_hours),
        stage_timeout_seconds=stage_timeout_minutes * 60, updated_at=now)
    session.add(plan)
    session.flush()
    return plan, True


# --------------------------------------------------------------------------
# Execution


def _stage_campaigns(session: Session, plan: FirmwareHilPlan, stage: int):
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment
    rows = []
    for device in plan.stages[stage]:
        campaign = session.scalar(select(FirmwareCampaign).where(
            FirmwareCampaign.actor == plan_actor(plan),
            FirmwareCampaign.idempotency_key == campaign_key(plan, stage, device["connector_id"]))
            .execution_options(populate_existing=True))
        deployment = None
        if campaign is not None:
            deployment = session.scalar(select(FirmwareDeployment).where(
                FirmwareDeployment.campaign_id == campaign.id).order_by(FirmwareDeployment.id.desc())
                .limit(1).execution_options(populate_existing=True))
        rows.append((device, campaign, deployment))
    return rows


def _plan_campaigns(session: Session, plan: FirmwareHilPlan):
    from zk_add.ota import FirmwareCampaign
    return list(session.scalars(select(FirmwareCampaign).where(FirmwareCampaign.actor == plan_actor(plan))
                                .order_by(FirmwareCampaign.id).with_for_update()
                                .execution_options(populate_existing=True)))


def _never_offered_campaigns(session: Session, plan: FirmwareHilPlan):
    """Plan campaigns whose every deployment is still PENDING and never offered."""
    from zk_add.ota import FirmwareDeployment
    result = []
    for campaign in _plan_campaigns(session, plan):
        if campaign.status not in {"ACTIVE", "PAUSED"}:
            continue
        deployments = list(session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id == campaign.id).with_for_update()
            .execution_options(populate_existing=True)))
        if deployments and all(row.status == "PENDING" and row.offered_at is None for row in deployments):
            result.append((campaign, deployments))
    return result


def _connector(session: Session, connector_id: str) -> Connector | None:
    return session.scalar(select(Connector).where(Connector.connector_id == connector_id))


def _audit(session: Session, plan: FirmwareHilPlan, action: str, outcome: str, **after: Any) -> None:
    from zk_add.audit import append_audit
    append_audit(session, actor=SYSTEM_ACTOR, action=action, target_type="firmware_hil_plan",
                 target_id=plan.plan_id, outcome=outcome,
                 after={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                        "release_id": plan.release_public_id, "stage": plan.current_stage + 1, **after})


def _finish(session: Session, plan: FirmwareHilPlan, status: str, *, code: str | None = None,
            message: str | None = None, connector_id: str | None = None, actor: str | None = None,
            campaign_action: str = "pause") -> dict:
    """End an open plan; stop every campaign the plan created that never offered bytes."""
    now = utc_now()
    _assignment_lock(session)
    stopped = []
    for campaign, deployments in _never_offered_campaigns(session, plan):
        if campaign_action == "cancel":
            campaign.status = "CANCELLED"
            for deployment in deployments:
                deployment.status = "CANCELLED"
                deployment.updated_at = now
        else:
            if campaign.status != "ACTIVE":
                continue
            campaign.status = "PAUSED"
        campaign.pause_reason = f"HIL plan {plan.plan_id} {status.lower()}: {code or status}"[:200]
        campaign.updated_at = now
        stopped.append(campaign.campaign_id)
    plan.status = status
    plan.open_guard = None
    plan.halt_code = code
    plan.halt_reason = message
    plan.waiting_reason = None
    plan.completed_at = now
    plan.updated_at = now
    after = {"code": code, "message": message, "stopped_campaigns": stopped}
    if actor is None:
        _audit(session, plan, f"FIRMWARE_HIL_PLAN_{status}", status, **after)
    else:
        from zk_add.audit import append_audit
        append_audit(session, actor=actor, action=f"FIRMWARE_HIL_PLAN_{status}", target_type="firmware_hil_plan",
                     target_id=plan.plan_id, outcome=status,
                     after={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                            "release_id": plan.release_public_id, "stage": plan.current_stage + 1,
                            "reason": message, "stopped_campaigns": stopped})
    if status in {"HALTED", "EXPIRED"}:
        from zk_add.service import upsert_alert
        stage = plan.stages[min(plan.current_stage, len(plan.stages) - 1)]
        connector = _connector(session, connector_id or stage[0]["connector_id"])
        if connector is not None:
            upsert_alert(session, connector, code=HALT_ALERT_CODE, severity="HIGH",
                         message=f"Staged HIL plan {plan.plan_id} {status.lower()} at stage "
                                 f"{plan.current_stage + 1}: {message}"[:1000],
                         details={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                                  "release_id": plan.release_public_id, "halt_code": code,
                                  "stage": plan.current_stage + 1})
    return {"plan_id": plan.plan_id, "status": status, "halt_code": code}


def _verify_pins(session: Session, plan: FirmwareHilPlan):
    from zk_add.ota import FirmwareRelease, _application_sha256
    if (not hmac.compare_digest(plan.seal, _seal(plan.plan_id, plan.plan_digest, plan.approved_by))
            or _digest(_stored_pins(plan)) != plan.plan_digest):
        raise PlanHalt("PLAN_PIN_CHANGED", "The approved plan no longer matches its sealed pins.")
    release = session.get(FirmwareRelease, plan.release_id)
    if (release is None or release.release_id != plan.release_public_id or release.version != plan.release_version
            or release.image_sha256 != plan.image_sha256
            or _application_sha256(release) != plan.application_sha256):
        raise PlanHalt("RELEASE_CHANGED", "The pinned release identity changed.")
    if release.state != "HIL_ONLY" or release.revoked_at is not None:
        raise PlanHalt("RELEASE_UNAVAILABLE", f"The pinned release is {release.state}, not HIL_ONLY.")
    return release


def _wait_reason(session: Session, plan: FirmwareHilPlan, devices: list[dict]) -> str | None:
    """Slot contention and offline devices wait; a held device halts."""
    occupied, complete = _unsettled(session)
    if not complete:
        raise PlanHalt("FLEET_RESERVATION_SCAN_LIMIT", "The fleet reservation scan is incomplete.")
    for device in devices:
        if device["connector_id"] in occupied:
            raise PlanHalt("DEVICE_INSTALLATION_UNSETTLED",
                           f"{device['zone_id']}: an installation outside this plan is still unsettled.",
                           device["connector_id"])
    if len(occupied) + len(devices) > MAX_STAGE_DEVICES:
        return "FLEET_SLOTS_BUSY: " + ", ".join(sorted(occupied))
    for connector_id in sorted(occupied):
        location = _location(connector_id)
        if location is None or location in {device["location"] for device in devices}:
            return f"LOCATION_BUSY: {location or 'UNKNOWN'} ({connector_id})"
    for device in devices:
        connector = _connector(session, device["connector_id"])
        if connector is None or not connector.connected:
            return f"DEVICE_OFFLINE: {device['zone_id']}"
    return None


def _start_stage(session: Session, plan: FirmwareHilPlan, now) -> list[str]:
    """Preflight every device, then create each campaign through the existing path."""
    from zk_add.ota import create_campaign, preview_campaign_scope
    stage = plan.stages[plan.current_stage]
    scopes = []
    for device in stage:
        try:
            scope = preview_campaign_scope(session, release_public_id=plan.release_public_id,
                                           zone_id=device["zone_id"])
        except Exception as error:  # an unexpected refusal is still a refusal
            raise PlanHalt("PREFLIGHT_REFUSED", f"{device['zone_id']}: {error}", device["connector_id"]) from error
        problem = _preflight_problem(scope, device)
        if problem is not None:
            raise problem
        scopes.append(scope)
    created = []
    total = len(plan.stages)
    for device, scope in zip(stage, scopes):
        try:
            campaign = create_campaign(
                session, release_public_id=plan.release_public_id, zone_id=device["zone_id"],
                reason=(f"Staged HIL plan {plan.plan_id} stage {plan.current_stage + 1} of {total}, "
                        f"approved by {plan.approved_by}: {plan.reason}"),
                typed_confirmation=plan.release_version, actor=plan_actor(plan),
                scope_token=scope["scope_token"],
                idempotency_key=campaign_key(plan, plan.current_stage, device["connector_id"]))
        except Exception as error:
            raise PlanHalt("CAMPAIGN_REFUSED", f"{device['zone_id']}: {error}", device["connector_id"]) from error
        from zk_add.zkt_hil_schedule import campaign_audit_scope
        _audit(session, plan, "FIRMWARE_HIL_PLAN_CAMPAIGN_CREATED", "ACTIVE",
               campaign_id=campaign.campaign_id, zone_id=device["zone_id"],
               connector_id=device["connector_id"], **campaign_audit_scope(session, campaign))
        created.append(campaign.campaign_id)
    plan.stage_started_at = now
    plan.waiting_reason = None
    plan.updated_at = now
    return created


def _monitor_stage(session: Session, plan: FirmwareHilPlan) -> bool:
    """Raise PlanHalt on any unsuccessful state; return True when every device SUCCEEDED."""
    from zk_add.zkt_hil_schedule import _post_verdict_hold
    rows = _stage_campaigns(session, plan, plan.current_stage)
    for device, campaign, deployment in rows:
        zone, connector_id = device["zone_id"], device["connector_id"]
        if campaign is None or deployment is None:
            raise PlanHalt("STAGE_CAMPAIGN_MISSING", f"{zone}: the stage campaign is missing.", connector_id)
        # The deployment first: a FAILED or ROLLED_BACK report also pauses its campaign.
        if deployment.status in UNSUCCESSFUL_TERMINAL:
            raise PlanHalt(f"DEPLOYMENT_{deployment.status}", f"{zone}: deployment {deployment.deployment_id} "
                           f"{deployment.status} ({deployment.error_code or 'no error code'}).", connector_id)
        if campaign.status not in {"ACTIVE", "COMPLETED"}:
            raise PlanHalt(f"CAMPAIGN_{campaign.status}", f"{zone}: campaign {campaign.campaign_id} is "
                           f"{campaign.status}: {campaign.pause_reason or 'no reason recorded'}", connector_id)
        if deployment.status == "PENDING" and deployment.error_code:
            raise PlanHalt(f"ADMISSION_REFUSED:{deployment.error_code}", f"{zone}: offer refused by "
                           f"the fleet guard: {deployment.error_code}.", connector_id)
    if not all(deployment.status == "SUCCEEDED" for _device, _campaign, deployment in rows):
        return False
    # A new proven defect after the upgrade stops expansion, as it does for
    # the nationwide schedule. Missing telemetry is not an invented failure.
    for device, _campaign, deployment in rows:
        connector = _connector(session, device["connector_id"])
        completed = ensure_utc(deployment.completed_at or deployment.updated_at).isoformat()
        hold = _post_verdict_hold(session, connector, run_completed_at=completed) if connector else "MISSING"
        if hold:
            raise PlanHalt(f"POST_INSTALL_HOLD:{hold}", f"{device['zone_id']}: {hold} after SUCCEEDED.",
                           device["connector_id"])
    return True


def _lock_plan(session: Session, plan_pk: int, *, skip_locked: bool) -> FirmwareHilPlan | None:
    return session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.id == plan_pk)
                          .with_for_update(skip_locked=skip_locked)
                          .execution_options(populate_existing=True))


def advance_plan(session: Session, plan_pk: int) -> dict | None:
    """One idempotent step. Safe to repeat, restart, or run from two workers."""
    plan = _lock_plan(session, plan_pk, skip_locked=True)
    if plan is None or plan.status not in OPEN_STATES:
        return None
    now = utc_now()
    if now >= ensure_utc(plan.expires_at):
        return _finish(session, plan, "EXPIRED", code="PLAN_EXPIRED",
                       message="The approval expired before the plan completed.")
    _verify_pins(session, plan)
    timeout = timedelta(seconds=plan.stage_timeout_seconds)
    if plan.stage_started_at is not None:
        if _monitor_stage(session, plan):
            _audit(session, plan, "FIRMWARE_HIL_PLAN_STAGE_SUCCEEDED", "SUCCEEDED")
            if plan.current_stage + 1 >= len(plan.stages):
                return _finish(session, plan, "COMPLETED")
            plan.current_stage += 1
            plan.stage_entered_at = now
            plan.stage_started_at = None
            plan.waiting_reason = None
            plan.updated_at = now
            return {"plan_id": plan.plan_id, "status": plan.status, "stage": plan.current_stage + 1}
        if plan.status == "RUNNING" and now >= ensure_utc(plan.stage_started_at) + timeout:
            raise PlanHalt("STAGE_TIMEOUT", f"Stage {plan.current_stage + 1} did not reach SUCCEEDED within "
                           f"{plan.stage_timeout_seconds // 60} minutes.")
        return None
    if plan.status != "RUNNING":
        return None
    devices = plan.stages[plan.current_stage]
    waiting = _wait_reason(session, plan, devices)
    if waiting:
        if now >= ensure_utc(plan.stage_entered_at) + timeout:
            raise PlanHalt("STAGE_START_TIMEOUT", f"Stage {plan.current_stage + 1} could not start within "
                           f"{plan.stage_timeout_seconds // 60} minutes: {waiting}")
        if plan.waiting_reason != waiting[:200]:
            plan.waiting_reason = waiting[:200]
            plan.updated_at = now
            return {"plan_id": plan.plan_id, "status": plan.status, "waiting_reason": plan.waiting_reason}
        return None
    created = _start_stage(session, plan, now)
    return {"plan_id": plan.plan_id, "status": plan.status, "stage": plan.current_stage + 1,
            "campaigns": created}


def record_halt(session: Session, plan_pk: int, halt: PlanHalt) -> dict | None:
    """Persist a halt in a fresh transaction; anything the failed step wrote is gone."""
    plan = _lock_plan(session, plan_pk, skip_locked=False)
    if plan is None or plan.status not in OPEN_STATES:
        return None
    return _finish(session, plan, "HALTED", code=halt.code, message=halt.message, connector_id=halt.connector_id)


def tick(session_scope=None) -> list[dict]:
    """Advance every open plan, each in its own transactions."""
    if session_scope is None:
        from zk_add.db import session_scope
    with session_scope() as session:
        open_ids = list(session.scalars(select(FirmwareHilPlan.id)
                                        .where(FirmwareHilPlan.status.in_(OPEN_STATES))))
    updates = []
    for plan_pk in open_ids:
        try:
            try:
                with session_scope() as session:
                    update = advance_plan(session, plan_pk)
            except PlanHalt as halt:
                with session_scope() as session:
                    update = record_halt(session, plan_pk, halt)
        except Exception:  # e.g. a database error: retried next tick from durable state
            logger.exception("Staged HIL plan %s step failed", plan_pk)
            update = {"plan_pk": plan_pk, "error": "STEP_FAILED"}
        if update:
            updates.append(update)
    return updates


async def run_hil_plan_loop(stop: asyncio.Event, *, interval: float = 10.0) -> None:
    from zk_add.realtime import browser_events
    while not stop.is_set():
        try:
            for update in await asyncio.to_thread(tick):
                if "error" in update:
                    await browser_events.publish("backend_error", {
                        "code": "HIL_PLAN_STEP_FAILED",
                        "message": "A staged HIL plan step failed; it retries from its saved state."})
                else:
                    await browser_events.publish("firmware", {"hil_plan": update})
        except Exception as exc:  # the next tick retries from durable state
            logger.exception("Staged HIL plan step failed")
            await browser_events.publish("backend_error", {"code": "HIL_PLAN_LOOP_ERROR",
                                                           "message": str(exc)[:500]})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# Owner controls


def control_plan(session: Session, *, plan_id: str, action: str, actor: str, reason: str) -> dict:
    """Pause, resume or cancel. The caller has completed step-up."""
    plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))
    if plan is None:
        raise LookupError("HIL plan not found.")
    plan = _lock_plan(session, plan.id, skip_locked=False)
    if plan.status not in OPEN_STATES:
        raise ValueError(f"This HIL plan is already {plan.status.lower()}.")
    now = utc_now()
    from zk_add.audit import append_audit

    def audit(outcome: str, **extra: Any) -> None:
        append_audit(session, actor=actor, action=f"FIRMWARE_HIL_PLAN_{action.upper()}",
                     target_type="firmware_hil_plan", target_id=plan.plan_id, outcome=outcome,
                     after={"plan_id": plan.plan_id, "approved_by": plan.approved_by, "reason": reason,
                            "stage": plan.current_stage + 1, **extra})

    if action == "pause":
        if plan.status != "RUNNING":
            raise ValueError("Only a running HIL plan can be paused.")
        plan.status = "PAUSED"
        plan.updated_at = now
        audit("PAUSED")
    elif action == "resume":
        if plan.status != "PAUSED":
            raise ValueError("Only a paused HIL plan can be resumed.")
        if now >= ensure_utc(plan.expires_at):
            raise ValueError("This HIL plan's approval has expired.")
        try:
            _verify_pins(session, plan)
        except PlanHalt as halt:
            raise ValueError(halt.message) from halt
        plan.status = "RUNNING"
        # Timeouts do not run while paused: the current phase starts afresh.
        if plan.stage_started_at is None:
            plan.stage_entered_at = now
        else:
            plan.stage_started_at = now
        plan.updated_at = now
        audit("RUNNING")
    elif action == "cancel":
        from zk_add.ota import FirmwareDeployment
        _assignment_lock(session)
        campaigns = _plan_campaigns(session, plan)
        in_flight = list(session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id.in_([row.id for row in campaigns]),
            FirmwareDeployment.status.in_(CANCEL_REFUSED_STATES)).with_for_update()
            .execution_options(populate_existing=True))) if campaigns else []
        if in_flight:
            raise ValueError("Cancel refused: deployment " + ", ".join(
                f"{row.deployment_id} is {row.status}" for row in in_flight)
                + ". It is never cancelled by a plan; pause the plan and retry once it settles.")
        return _finish(session, plan, "CANCELLED", code="OWNER_CANCELLED", message=reason, actor=actor,
                       campaign_action="cancel")
    else:
        raise ValueError("Unknown HIL plan action.")
    return {"plan_id": plan.plan_id, "status": plan.status}


# --------------------------------------------------------------------------
# Views


def serialize_plan(session: Session, plan: FirmwareHilPlan) -> dict:
    stages = []
    for index, stage in enumerate(plan.stages):
        rows = _stage_campaigns(session, plan, index)
        started = any(campaign is not None for _device, campaign, _deployment in rows)
        if index < plan.current_stage or (plan.status == "COMPLETED"):
            state = "SUCCEEDED"
        elif index > plan.current_stage:
            state = "QUEUED"
        elif plan.status in TERMINAL_STATES:
            state = plan.status
        else:
            state = "RUNNING" if started else "WAITING"
        stages.append({
            "index": index + 1, "status": state,
            "devices": [{
                **{key: device.get(key) for key in ("zone_id", "connector_id", "display_name", "location",
                                                    "mac", "terminal_serial")},
                "campaign_id": campaign.campaign_id if campaign else None,
                "campaign_status": campaign.status if campaign else None,
                "deployment_id": deployment.deployment_id if deployment else None,
                "deployment_status": deployment.status if deployment else None,
                "error_code": deployment.error_code if deployment else None,
            } for device, campaign, deployment in rows],
        })
    return {
        "plan_id": plan.plan_id, "status": plan.status,
        "release": {"release_id": plan.release_public_id, "version": plan.release_version,
                    "image_sha256": plan.image_sha256, "application_sha256": plan.application_sha256},
        "plan_digest": plan.plan_digest, "reason": plan.reason, "approved_by": plan.approved_by,
        "approved_at": plan.approved_at, "expires_at": plan.expires_at,
        "stage_timeout_minutes": plan.stage_timeout_seconds // 60,
        "current_stage": min(plan.current_stage + 1, len(plan.stages)), "stage_count": len(plan.stages),
        "stage_entered_at": plan.stage_entered_at, "stage_started_at": plan.stage_started_at,
        "waiting_reason": plan.waiting_reason, "halt_code": plan.halt_code, "halt_reason": plan.halt_reason,
        "completed_at": plan.completed_at, "updated_at": plan.updated_at, "stages": stages,
    }


def plan_rows(session: Session, *, limit: int = 20) -> list[dict]:
    plans = session.scalars(select(FirmwareHilPlan).order_by(FirmwareHilPlan.id.desc()).limit(limit))
    return [serialize_plan(session, plan) for plan in plans]


def plan_detail(session: Session, plan_id: str) -> dict | None:
    plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))
    return serialize_plan(session, plan) if plan else None
