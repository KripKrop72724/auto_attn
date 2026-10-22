"""One-approval staged HIL plans (owner decision, 21 October 2026).

The owner approves, once and with the password step-up, an ordered plan for
one HIL_ONLY 2.7 platform release: stages, each a set of exact HIL targets
that may run together inside the existing fleet guard (at most two unsettled
upgrades nationwide, one per location; RECONCILING still holds a slot). ADD
then starts each stage's one-zone campaigns itself and advances only when
every device of the stage reports SUCCEEDED and then proves itself on the new
boot: a soak of continuous heartbeats, healthy storage diagnostics, a
connected terminal link and attendance delivery.

Nothing here is a new gate or a bypass. Each campaign is created through the
same ``preview_campaign_scope`` + ``create_campaign`` calls an operator's
request makes (exact HIL scope, storage predecessors, family, nationwide
schedule and reservation), and every offer still passes
``assignment_for_connector`` and its fleet guard. The approval replaces only
the interactive password prompt of each campaign. With nobody at the
keyboard, the plan re-checks what the owner looked at before each campaign:
the device's own holds (identity, source reviews, health, storage), which
must be exactly those the owner accepted at approval. It HALTS (no further
campaigns, never-offered campaigns paused, an alert on the affected device,
an audit row and a browser event) on any FAILED, ROLLED_BACK, PAUSED or
otherwise unsuccessful deployment or campaign, a new device hold, a defect
after the install, a preflight refusal, a lasting admission refusal, a
changed release or plan pin, and a timeout.

Plans are immutable once approved: the release identity (release id, version,
artifact and application digests), the exact device list with the holds the
owner accepted, the soak and the install window are pinned, digested and
sealed with the fleet root secret, and re-verified on every step. Only one
plan is open at a time; plans expire, and no stage starts unless it can
finish before the approval ends.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta, timezone
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.models import Connector, DeviceTelemetry, ReconciliationJob, utc_column
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
# ESP_RESTART_LOOP clears once the ESP stays up for 30 minutes.
DEFAULT_SOAK_MINUTES = 30
MIN_SOAK_MINUTES = 10
MAX_SOAK_MINUTES = 720
# Stages start only inside this local window (start inclusive, end exclusive):
# by default after the morning check-in peak, while people are at the office,
# and before the evening check-out.
PAKISTAN_TIME = timezone(timedelta(hours=5))
DEFAULT_WINDOW_START_HOUR = 10
DEFAULT_WINDOW_END_HOUR = 16
# Same two-key namespace as the ZKT assignment lock; a different key.
PLAN_LOCK_KEY = 271
UNSUCCESSFUL_TERMINAL = ("FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED")
# Fleet-guard refusals that clear on their own; a stage waits for them.
TRANSIENT_ADMISSION = frozenset({
    "NATIONWIDE_TWO_UPGRADE_RESERVATIONS", "NATIONWIDE_LOCATION_BUSY", "NATIONWIDE_TARGET_OFFLINE"})
# Device conditions the owner may accept at approval: the storage, delivery,
# terminal-link and message faults a 2.7 install is meant to repair (P02 and
# 13FL carry them today), and a source review that already exists. Anything
# else (identity, duplicate serial, restart loop, fatal, capability, an
# unknown code) refuses the plan. Each accepted hold is pinned exactly.
ACCEPTABLE_HOLDS = frozenset({
    "ESP_DURABILITY_FAULT", "ESP_PRESERVATION_UNVERIFIED", "ESP_DELIVERY_WORKER_FAULT", "ESP_LOCAL_FAILURE",
    "DEVICE_MESSAGE_REJECTED", "ZKT_CONNECTION_FLAPPING", "PRESERVATION_NOT_VERIFIED", "JOURNAL_READER_HOLD"})
SOURCE_REVIEW_HOLD = "SOURCE_OR_IDENTITY_REVIEW_REQUIRED"
SOURCE_REVIEW_LIMIT = 20
# Raised or refreshed after the install, these are defects even when the
# owner accepted them beforehand: the install did not repair them.
POST_INSTALL_DEFECTS = frozenset({
    "ESP_DURABILITY_FAULT", "ESP_PRESERVATION_UNVERIFIED", "ESP_LOCAL_FAILURE", "ESP_DELIVERY_WORKER_FAULT",
    "ESP_RESTART_LOOP", "ESP_FATAL", "QUARANTINED_DUPLICATE_SERIAL", "ZKT_SERIAL_MISMATCH"})
COMMITTED_BATCH_OUTCOMES = ("COMMITTED", "COMMITTED_WITH_DUPLICATES", "COMMITTED_WITH_QUARANTINE")
DEVICE_PIN_KEYS = ("zone_id", "connector_id", "mac", "terminal_serial", "location", "accepted_holds")


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
    soak_seconds: Mapped[int] = mapped_column(Integer)
    window_start_hour: Mapped[int] = mapped_column(Integer)
    window_end_hour: Mapped[int] = mapped_column(Integer)
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


def _pins(release: dict, stages: list[list[dict]], *, expires_in_hours: int, stage_timeout_minutes: int,
          soak_minutes: int, window: tuple[int, int]) -> dict:
    return {
        "release": {key: release[key] for key in
                    ("release_id", "version", "image_sha256", "application_sha256")},
        "stages": [[{key: device.get(key, []) if key == "accepted_holds" else device[key]
                     for key in DEVICE_PIN_KEYS} for device in stage] for stage in stages],
        "expires_in_hours": expires_in_hours,
        "stage_timeout_minutes": stage_timeout_minutes,
        "soak_minutes": soak_minutes,
        "install_window_pakistan_hours": list(window),
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
                 plan.stages,
                 expires_in_hours=int(round((ensure_utc(plan.expires_at) - ensure_utc(plan.approved_at))
                                            .total_seconds() / 3600)),
                 stage_timeout_minutes=plan.stage_timeout_seconds // 60, soak_minutes=plan.soak_seconds // 60,
                 window=(plan.window_start_hour, plan.window_end_hour))


def plan_actor(plan: FirmwareHilPlan) -> str:
    return f"hil-plan:{plan.plan_id}"


def campaign_key(plan: FirmwareHilPlan, stage: int, connector_id: str) -> str:
    return f"hil-plan:{plan.plan_id}:{stage}:{connector_id}"


def _pause_marker(plan: FirmwareHilPlan) -> str:
    return f"HIL plan {plan.plan_id} paused by its owner"


# --------------------------------------------------------------------------
# Device holds: what the owner looked at before each manual campaign


def _health_holds(session: Session, connector: Connector) -> set[str]:
    """Gating health reasons; an earlier failed install is history (owner decision 2)."""
    from zk_add.device_health import evaluate_health
    holds = {reason.code for reason in evaluate_health(session, connector).reasons
             if reason.gating and not reason.code.startswith("OTA_DEVICE_")}
    # The stored device error (the legacy writers' in SHADOW mode); OTA_* is
    # an earlier install's outcome, the same history as above.
    if connector.last_error_code and not connector.last_error_code.startswith("OTA_"):
        holds.add(connector.last_error_code)
    return holds


def _state_holds(session: Session, connector: Connector) -> set[str]:
    """Capability, terminal identity and source reviews; never cleared by an install."""
    from zk_add.ota import capability_is_eligible
    from zk_add.zkt_hil_schedule import _capability_untested
    holds = set()
    if connector.firmware_family != "zkt" or (not capability_is_eligible(connector)
                                               and not _capability_untested(connector)):
        holds.add("SECURITY_OR_CAPABILITY_UNVERIFIED")
    if connector.zkt_device is not None and connector.zkt_device.writes_disabled_reason:
        holds.add("TERMINAL_IDENTITY_HOLD")
    jobs = list(session.scalars(select(ReconciliationJob.job_id).where(
        ReconciliationJob.connector_id == connector.id, ReconciliationJob.status == "NEEDS_ATTENTION")
        .order_by(ReconciliationJob.id).limit(SOURCE_REVIEW_LIMIT + 1)))
    if len(jobs) > SOURCE_REVIEW_LIMIT:
        holds.add("SOURCE_REVIEW_SCAN_LIMIT")
    holds.update(f"{SOURCE_REVIEW_HOLD}:{job_id}" for job_id in jobs[:SOURCE_REVIEW_LIMIT])
    journal = (connector.firmware_diagnostics or {}).get("journal_storage") or {}
    if isinstance(journal, dict) and journal.get("hil_reboot_persistence_incident") is True:
        holds.add("JOURNAL_PERSISTENCE_INCIDENT")
    return holds


def _diagnostic_holds(connector: Connector) -> set[str]:
    """The last reported storage and journal state, as zkt_hil_schedule._known_hold reads it."""
    diagnostics = connector.firmware_diagnostics or {}
    storage = diagnostics.get("storage") or {}
    holds = set()
    if (storage.get("durability") not in {None, "HEALTHY"}
            or any(storage.get(name) is False for name in
                   ("persistence_verified", "recovery_complete", "upgrade_ready"))
            or any(storage.get(name) for name in ("error_code", "upgrade_error", "persistence_probe_error",
                   "legacy_error_code", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults"))):
        holds.add("PRESERVATION_NOT_VERIFIED")
    # Only a journal image has a reader to wait for. The 2.7 platform in phase
    # L reports runtime_profile ZKT_LEGACY and a DISABLED journal runtime
    # (zkt_journal_runtime.c), which is its normal state.
    runtime = diagnostics.get("journal_runtime")
    if diagnostics.get("runtime_profile") == "ZKT_JOURNAL_V1" and (
            not isinstance(runtime, dict) or runtime.get("phase") != "READY"
            or runtime.get("reader_ready") is not True or runtime.get("compatibility") != ""):
        holds.add("JOURNAL_READER_HOLD")
    return holds


def device_holds(session: Session, connector: Connector) -> list[str]:
    return sorted(_state_holds(session, connector) | _health_holds(session, connector)
                  | _diagnostic_holds(connector))


def acceptable_hold(hold: str) -> bool:
    return hold in ACCEPTABLE_HOLDS or hold.startswith(f"{SOURCE_REVIEW_HOLD}:")


# --------------------------------------------------------------------------
# Resolution (preview and approval share it)


def _location(connector_id: str) -> str | None:
    from zk_add.zkt270_scope import BY_ID
    from zk_add.zkt274_bld9_k50 import reserved_location
    target = BY_ID.get(connector_id)
    return target.location if target is not None else reserved_location(connector_id)


def _release_for_plan(session: Session, release_public_id: str):
    from zk_add.ota import RECOVERY_RELEASE_IDS, FirmwareRelease, _application_sha256
    from zk_add.storage_contract import FLEET_PLATFORM_VERSIONS
    from zk_add.terminal_families import release_family
    from zk_add.zkt_hil_schedule import applies as nationwide_schedule_applies
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
    # Only the 2.7 platform starts each exact target independently. One-shot
    # storage recovery images, the frozen journal bridges and writer and the
    # legacy ordered releases stay manual, one campaign at a time.
    if (release.version not in FLEET_PLATFORM_VERSIONS or release.release_id != f"zone-lite-{release.version}"
            or release.version in RECOVERY_RELEASE_IDS or nationwide_schedule_applies(release)):
        raise ValueError("Staged HIL plans cover the 2.7 platform releases ("
                         + ", ".join(FLEET_PLATFORM_VERSIONS) + ") only.")
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
    # The ESP stability preflight (branch fix/ota-interrupted-boot-retry)
    # adds "stability_warnings" here and makes create_campaign refuse them
    # without the owner's explicit override. A plan never overrides them: it
    # stops, and the owner decides in a manual campaign. Today's preview
    # does not report them; the device holds above cover restart loops.
    warnings = scope.get("stability_warnings") or []
    if warnings:
        text_ = "; ".join(str(row.get("message") or row.get("code") or row) if isinstance(row, dict)
                          else str(row) for row in warnings)
        return PlanHalt("PREFLIGHT_WARNING", f"{device['zone_id']}: ESP stability warning: {text_}",
                        device["connector_id"])
    return None


def _slot_holders(session: Session) -> tuple[list[dict], bool]:
    from zk_add.zkt_ota_admission import reservation_snapshot
    snapshot = reservation_snapshot(session)
    rows: dict[str, dict] = {}
    for row in snapshot["reservations"]:
        if not row["later_installation"] and row["connector_id"] not in rows:
            rows[row["connector_id"]] = {"connector_id": row["connector_id"], "status": row["status"],
                                         "campaign_status": row["campaign_status"],
                                         "location": _location(row["connector_id"])}
    if rows:
        for connector in session.scalars(select(Connector).where(Connector.connector_id.in_(list(rows)))):
            rows[connector.connector_id].update(zone_id=connector.zone_id, display_name=connector.display_name)
    return sorted(rows.values(), key=lambda row: row["connector_id"]), bool(snapshot["scan_complete"])


def _holder_text(holder: dict) -> str:
    return (f"{holder.get('zone_id') or holder['connector_id']} ({holder['status']}, campaign "
            f"{str(holder['campaign_status']).lower()})")


def validate_options(*, stages: list[list[str]], expires_in_hours: int, stage_timeout_minutes: int,
                     soak_minutes: int, window_start_hour: int, window_end_hour: int) -> None:
    if not 1 <= expires_in_hours <= MAX_EXPIRY_HOURS:
        raise ValueError(f"Plan expiry must be between 1 and {MAX_EXPIRY_HOURS} hours.")
    if not MIN_STAGE_TIMEOUT_MINUTES <= stage_timeout_minutes <= MAX_STAGE_TIMEOUT_MINUTES:
        raise ValueError(f"Stage timeout must be between {MIN_STAGE_TIMEOUT_MINUTES} and "
                         f"{MAX_STAGE_TIMEOUT_MINUTES} minutes.")
    if stage_timeout_minutes >= expires_in_hours * 60:
        raise ValueError("The stage timeout must be shorter than the approval: no stage starts unless "
                         "it can finish before the approval expires.")
    if not MIN_SOAK_MINUTES <= soak_minutes <= MAX_SOAK_MINUTES:
        raise ValueError(f"The soak must be between {MIN_SOAK_MINUTES} and {MAX_SOAK_MINUTES} minutes.")
    if soak_minutes >= stage_timeout_minutes:
        raise ValueError("The soak must be shorter than the stage timeout, which covers install and soak.")
    if not 0 <= window_start_hour < window_end_hour <= 24:
        raise ValueError("The install window is a start hour before an end hour, 0 to 24, Pakistan time.")
    if not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"A plan has between 1 and {MAX_STAGES} stages.")
    zones = [zone for stage in stages for zone in stage]
    if any(not 1 <= len(stage) <= MAX_STAGE_DEVICES for stage in stages):
        raise ValueError("Each stage runs one or two devices: the fleet guard admits at most two "
                         "unsettled upgrades nationwide.")
    if len(set(zones)) != len(zones):
        raise ValueError("A zone may appear only once in a plan.")


def resolve_plan(session: Session, *, release_public_id: str, stages: list[list[str]],
                 expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
                 stage_timeout_minutes: int = DEFAULT_STAGE_TIMEOUT_MINUTES,
                 soak_minutes: int = DEFAULT_SOAK_MINUTES,
                 window_start_hour: int = DEFAULT_WINDOW_START_HOUR,
                 window_end_hour: int = DEFAULT_WINDOW_END_HOUR) -> dict:
    """Validate and pin a proposed plan. Read-only; raises ValueError on refusal."""
    from zk_add import ota
    from zk_add.ota import FirmwareCampaign
    validate_options(stages=stages, expires_in_hours=expires_in_hours, stage_timeout_minutes=stage_timeout_minutes,
                     soak_minutes=soak_minutes, window_start_hour=window_start_hour, window_end_hour=window_end_hour)
    zones = [zone for stage in stages for zone in stage]
    # One release-store verification for the whole plan, not one per device.
    ota.sync_release_store(session)
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
    holders, complete = _slot_holders(session)
    if not complete:
        raise ValueError("The fleet reservation scan is incomplete; no plan can be approved.")
    plan_ids = {device["connector_id"] for stage in resolved for device in stage}
    held = [device["zone_id"] for stage in resolved for device in stage
            if device["connector_id"] in {holder["connector_id"] for holder in holders}]
    if held:
        raise ValueError("An earlier installation on these devices is still unsettled: " + ", ".join(held))
    # A stage the current slot holders make impossible would only wait and
    # time out (Lahore-02 has held one slot since 2 October).
    for index, stage in enumerate(resolved, start=1):
        others = [holder for holder in holders if holder["connector_id"] not in plan_ids]
        if len(others) + len(stage) > MAX_STAGE_DEVICES:
            raise ValueError(f"Stage {index} needs {len(stage)} upgrade slots, but "
                             + ", ".join(_holder_text(holder) for holder in others)
                             + f" already hold {len(others)} of {MAX_STAGE_DEVICES}. Use one-device stages, "
                             "or wait until they settle.")
        for holder in others:
            if holder["location"] is None or holder["location"] in {device["location"] for device in stage}:
                raise ValueError(f"Stage {index}: {_holder_text(holder)} holds the upgrade slot of location "
                                 f"{holder['location'] or 'UNKNOWN'}; the fleet guard runs one per location.")
    by_id = {row.connector_id: row for row in session.scalars(select(Connector).where(
        Connector.connector_id.in_(list(plan_ids))))}
    refused = []
    for stage in resolved:
        for device in stage:
            row = by_id[device["connector_id"]]
            holds = device_holds(session, row)
            blocking = [hold for hold in holds if not acceptable_hold(hold)]
            if blocking:
                refused.append(f"{device['zone_id']}: {', '.join(blocking)}")
            device["accepted_holds"] = holds
    if refused:
        raise ValueError("These device holds need a person before any install: " + "; ".join(refused))
    for stage in resolved:
        for device in stage:
            try:
                scope = ota.preview_campaign_scope(session, release_public_id=release.release_id,
                                                   zone_id=device["zone_id"], sync_store=False)
            except (ValueError, RuntimeError) as error:
                raise ValueError(f"{device['zone_id']}: preflight refused: {error}") from error
            problem = _preflight_problem(scope, device)
            if problem is not None:
                raise ValueError(problem.message)
            row = by_id[device["connector_id"]]
            device.update(connected=bool(row.connected), firmware_version=row.firmware_version)
    release_pin = {"release_id": release.release_id, "version": release.version,
                   "image_sha256": release.image_sha256, "application_sha256": application}
    window = (window_start_hour, window_end_hour)
    pins = _pins(release_pin, resolved, expires_in_hours=expires_in_hours,
                 stage_timeout_minutes=stage_timeout_minutes, soak_minutes=soak_minutes, window=window)
    return {"release": release_pin, "stages": resolved, "plan_digest": _digest(pins),
            "expires_in_hours": expires_in_hours, "stage_timeout_minutes": stage_timeout_minutes,
            "soak_minutes": soak_minutes, "window_start_hour": window_start_hour,
            "window_end_hour": window_end_hour, "slot_holders": holders,
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


def _replay(session: Session, actor: str, idempotency_key: str, release_public_id: str,
            plan_digest: str) -> FirmwareHilPlan | None:
    replay = session.scalar(select(FirmwareHilPlan).where(
        FirmwareHilPlan.approved_by == actor, FirmwareHilPlan.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True))
    if replay is not None and (replay.plan_digest != plan_digest or replay.release_public_id != release_public_id):
        raise ValueError("That idempotency key belongs to another HIL plan.")
    return replay


def create_plan(session: Session, *, release_public_id: str, stages: list[list[str]], reason: str,
                typed_confirmation: str, plan_digest: str, actor: str, idempotency_key: str,
                expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
                stage_timeout_minutes: int = DEFAULT_STAGE_TIMEOUT_MINUTES,
                soak_minutes: int = DEFAULT_SOAK_MINUTES,
                window_start_hour: int = DEFAULT_WINDOW_START_HOUR,
                window_end_hour: int = DEFAULT_WINDOW_END_HOUR) -> tuple[FirmwareHilPlan, bool]:
    """Record the owner's single approval. The caller has completed step-up."""
    replay = _replay(session, actor, idempotency_key, release_public_id, plan_digest)
    if replay is not None:
        return replay, False
    if _postgres(session):
        from zk_add.zkt_ota_admission import LOCK_NAMESPACE
        session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                        {"namespace": LOCK_NAMESPACE, "key": PLAN_LOCK_KEY})
        # A concurrent retry with the same key committed while this one waited.
        replay = _replay(session, actor, idempotency_key, release_public_id, plan_digest)
        if replay is not None:
            return replay, False
    current = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.status.in_(OPEN_STATES))
                             .execution_options(populate_existing=True))
    if current is not None:
        raise ValueError(f"HIL plan {current.plan_id} is still {current.status.lower()}; "
                         "cancel it or let it finish first.")
    resolved = resolve_plan(session, release_public_id=release_public_id, stages=stages,
                            expires_in_hours=expires_in_hours, stage_timeout_minutes=stage_timeout_minutes,
                            soak_minutes=soak_minutes, window_start_hour=window_start_hour,
                            window_end_hour=window_end_hour)
    if resolved["plan_digest"] != plan_digest:
        raise ValueError("The plan changed since it was previewed (devices, holds or settings). Preview it "
                         "again before approving.")
    if typed_confirmation != resolved["release"]["version"]:
        raise ValueError("Typed firmware version does not match the release.")
    from zk_add.ota import FirmwareRelease
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_public_id))
    now = utc_now()
    plan_id = secrets.token_hex(16)
    stored = [[{key: device[key] for key in (*DEVICE_PIN_KEYS, "display_name")} for device in stage]
              for stage in resolved["stages"]]
    plan = FirmwareHilPlan(
        plan_id=plan_id, release_id=release.id, release_public_id=release.release_id,
        release_version=release.version, image_sha256=release.image_sha256,
        application_sha256=resolved["release"]["application_sha256"], stages=stored,
        plan_digest=plan_digest, seal=_seal(plan_id, plan_digest, actor), status="RUNNING",
        open_guard="OPEN", current_stage=0, stage_entered_at=now, reason=reason, approved_by=actor,
        idempotency_key=idempotency_key, approved_at=now, expires_at=now + timedelta(hours=expires_in_hours),
        stage_timeout_seconds=stage_timeout_minutes * 60, soak_seconds=soak_minutes * 60,
        window_start_hour=window_start_hour, window_end_hour=window_end_hour, updated_at=now)
    session.add(plan)
    session.flush()
    return plan, True


# --------------------------------------------------------------------------
# Campaign rows


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
                                .order_by(FirmwareCampaign.id).execution_options(populate_existing=True)))


def _locked_open_campaigns(session: Session, plan: FirmwareHilPlan):
    """Every ACTIVE or PAUSED plan campaign with its deployments, both row-locked.

    Locks each deployment before its campaign, the order record_progress uses.
    """
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment
    result = []
    for campaign in _plan_campaigns(session, plan):
        if campaign.status not in {"ACTIVE", "PAUSED"}:
            continue
        deployments = list(session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id == campaign.id).order_by(FirmwareDeployment.id).with_for_update()
            .execution_options(populate_existing=True)))
        campaign = session.scalar(select(FirmwareCampaign).where(FirmwareCampaign.id == campaign.id)
                                  .with_for_update().execution_options(populate_existing=True))
        if campaign.status in {"ACTIVE", "PAUSED"} and deployments:
            result.append((campaign, deployments))
    return result


def _never_offered(deployments) -> bool:
    return all(row.status == "PENDING" and row.offered_at is None for row in deployments)


def _all_succeeded(deployments) -> bool:
    return all(row.status == "SUCCEEDED" for row in deployments)


def _complete_succeeded(locked, now) -> list[str]:
    """A campaign whose every deployment SUCCEEDED has nothing left to offer.

    COMPLETED frees its zone for the next plan or campaign (no per-campaign
    password cancel) and stays valid evidence for HIL runs and acceptance.
    """
    completed = []
    for campaign, deployments in locked:
        if _all_succeeded(deployments):
            campaign.status = "COMPLETED"
            campaign.updated_at = now
            completed.append(campaign.campaign_id)
    return completed


def _connector(session: Session, connector_id: str) -> Connector | None:
    return session.scalar(select(Connector).where(Connector.connector_id == connector_id))


def _audit(session: Session, plan: FirmwareHilPlan, action: str, outcome: str, **after: Any) -> None:
    from zk_add.audit import append_audit
    append_audit(session, actor=SYSTEM_ACTOR, action=action, target_type="firmware_hil_plan",
                 target_id=plan.plan_id, outcome=outcome,
                 after={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                        "release_id": plan.release_public_id, "stage": plan.current_stage + 1, **after})


def _halt_alert(session: Session, plan: FirmwareHilPlan, connector: Connector, status: str,
                code: str | None, message: str | None) -> None:
    """A device-attributed halt: no health effect, operator-resolvable (device_health.POLICY)."""
    from zk_add.service import open_alert_row, upsert_alert
    earlier = open_alert_row(session, connector, HALT_ALERT_CODE)
    previous = []
    if earlier is not None:
        details = earlier.details or {}
        previous = [*(details.get("previous_halts") or []),
                    {key: details.get(key) for key in ("plan_id", "halt_code", "stage", "halted_at")}][-10:]
    upsert_alert(session, connector, code="FIRMWARE_HIL_PLAN_HALTED", severity="WARNING",
                 message=f"Staged HIL plan {plan.plan_id} {status.lower()} at stage "
                         f"{plan.current_stage + 1}: {message}"[:1000],
                 details={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                          "release_id": plan.release_public_id, "halt_code": code,
                          "stage": plan.current_stage + 1, "halted_at": utc_now().isoformat(),
                          "previous_halts": previous})


def _finish(session: Session, plan: FirmwareHilPlan, status: str, *, code: str | None = None,
            message: str | None = None, connector_id: str | None = None, actor: str | None = None,
            campaign_action: str = "pause") -> dict:
    """End an open plan; stop every plan campaign that never offered bytes.

    A deployment that received bytes always continues under its own campaign
    and safety states. A campaign whose deployments all SUCCEEDED completes.
    """
    now = utc_now()
    _assignment_lock(session)
    locked = _locked_open_campaigns(session, plan)
    completed = _complete_succeeded(locked, now)
    stopped = []
    for campaign, deployments in locked:
        if campaign.campaign_id in completed or not _never_offered(deployments):
            continue
        if campaign_action == "cancel":
            campaign.status = "CANCELLED"
            for deployment in deployments:
                deployment.status = "CANCELLED"
                deployment.updated_at = now
        else:
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
    after = {"code": code, "message": message, "stopped_campaigns": stopped, "completed_campaigns": completed,
             "connector_id": connector_id}
    if actor is None:
        _audit(session, plan, f"FIRMWARE_HIL_PLAN_{status}", status, **after)
    else:
        from zk_add.audit import append_audit
        append_audit(session, actor=actor, action=f"FIRMWARE_HIL_PLAN_{status}", target_type="firmware_hil_plan",
                     target_id=plan.plan_id, outcome=status,
                     after={"plan_id": plan.plan_id, "approved_by": plan.approved_by,
                            "release_id": plan.release_public_id, "stage": plan.current_stage + 1,
                            "reason": message, "stopped_campaigns": stopped, "completed_campaigns": completed})
    # Only a halt that a device caused is raised on that device. Expiry,
    # timeouts of slot waits and release or pin changes are plan-level: the
    # plan row, its audit row and the browser event carry them.
    if status == "HALTED" and connector_id is not None:
        connector = _connector(session, connector_id)
        if connector is not None:
            _halt_alert(session, plan, connector, status, code, message)
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


# --------------------------------------------------------------------------
# Stage start


def _window_closed(plan: FirmwareHilPlan, now) -> str | None:
    local = ensure_utc(now).astimezone(PAKISTAN_TIME)
    if plan.window_start_hour <= local.hour < plan.window_end_hour:
        return None
    return (f"INSTALL_WINDOW_CLOSED: stages start {plan.window_start_hour:02d}:00-"
            f"{plan.window_end_hour:02d}:00 Pakistan time")


def _check_stage_holds(session: Session, devices: list[dict]) -> None:
    """Any hold the owner did not accept at approval halts before an install."""
    for device in devices:
        connector = _connector(session, device["connector_id"])
        if connector is None:
            raise PlanHalt("DEVICE_MISSING", f"{device['zone_id']}: the pinned device is missing.")
        new = [hold for hold in device_holds(session, connector) if hold not in set(device.get("accepted_holds") or [])]
        if new:
            raise PlanHalt(f"DEVICE_HOLD:{new[0]}", f"{device['zone_id']}: {', '.join(new)} "
                           "appeared after the approval; a person reviews the device before any install.",
                           device["connector_id"])


def _wait_reason(session: Session, plan: FirmwareHilPlan, devices: list[dict]) -> tuple[str, str | None] | None:
    """Slot contention and offline devices wait; a held device halts."""
    holders, complete = _slot_holders(session)
    if not complete:
        raise PlanHalt("FLEET_RESERVATION_SCAN_LIMIT", "The fleet reservation scan is incomplete.")
    occupied = {holder["connector_id"] for holder in holders}
    for device in devices:
        if device["connector_id"] in occupied:
            raise PlanHalt("DEVICE_INSTALLATION_UNSETTLED",
                           f"{device['zone_id']}: an installation outside this plan is still unsettled.",
                           device["connector_id"])
    if len(occupied) + len(devices) > MAX_STAGE_DEVICES:
        return "FLEET_SLOTS_BUSY: " + ", ".join(_holder_text(holder) for holder in holders), None
    for holder in holders:
        if holder["location"] is None or holder["location"] in {device["location"] for device in devices}:
            return f"LOCATION_BUSY: {holder['location'] or 'UNKNOWN'} ({_holder_text(holder)})", None
    for device in devices:
        connector = _connector(session, device["connector_id"])
        if connector is None or not connector.connected:
            return f"DEVICE_OFFLINE: {device['zone_id']}", device["connector_id"]
    return None


def _start_stage(session: Session, plan: FirmwareHilPlan, now) -> list[str]:
    """Preflight every device, then create each campaign through the existing path.

    A refusal halts. A database error (deadlock, lock timeout) propagates, so
    the whole step rolls back and the next tick retries from durable state.
    """
    from zk_add import ota
    stage = plan.stages[plan.current_stage]
    try:
        ota.sync_release_store(session)
    except DBAPIError:
        raise
    except Exception as error:
        raise PlanHalt("RELEASE_STORE_UNVERIFIED", f"The firmware release store failed verification: {error}") \
            from error
    scopes = []
    for device in stage:
        try:
            scope = ota.preview_campaign_scope(session, release_public_id=plan.release_public_id,
                                               zone_id=device["zone_id"], sync_store=False)
        except DBAPIError:
            raise
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
            campaign = ota.create_campaign(
                session, release_public_id=plan.release_public_id, zone_id=device["zone_id"],
                reason=(f"Staged HIL plan {plan.plan_id} stage {plan.current_stage + 1} of {total}, "
                        f"approved by {plan.approved_by}: {plan.reason}"),
                typed_confirmation=plan.release_version, actor=plan_actor(plan),
                scope_token=scope["scope_token"],
                idempotency_key=campaign_key(plan, plan.current_stage, device["connector_id"]))
        except DBAPIError:
            raise
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


# --------------------------------------------------------------------------
# Monitoring and the post-install settle


def _settle_reason(session: Session, plan: FirmwareHilPlan, device: dict, deployment, now) -> str | None:
    """None once the upgraded device has proven itself on its new boot.

    Raises PlanHalt on a defect proven after SUCCEEDED; otherwise returns why
    it is still settling. Re-checked on every tick, so telemetry that arrives
    after SUCCEEDED is never missed.
    """
    from zk_add.device_health import evaluate_health, hard_storage_evidence, terminal_link
    from zk_add.models import AttendanceBatchReceipt, ZktObservationReceipt
    from zk_add.ota import _versions_match
    from zk_add.zkt_hil_schedule import _post_verdict_hold
    zone, connector_id = device["zone_id"], device["connector_id"]
    connector = _connector(session, connector_id)
    if connector is None:
        raise PlanHalt("POST_INSTALL_HOLD:DEVICE_MISSING", f"{zone}: the pinned device is missing.", connector_id)
    completed = ensure_utc(deployment.completed_at or deployment.updated_at)
    hold = _post_verdict_hold(session, connector, run_completed_at=completed.isoformat())
    if hold:
        raise PlanHalt(f"POST_INSTALL_HOLD:{hold}", f"{zone}: {hold} after SUCCEEDED.", connector_id)
    accepted = set(device.get("accepted_holds") or [])
    new_state = sorted(_state_holds(session, connector) - accepted)
    if new_state:
        raise PlanHalt(f"POST_INSTALL_HOLD:{new_state[0]}", f"{zone}: {', '.join(new_state)} after SUCCEEDED.",
                       connector_id)
    for reason in evaluate_health(session, connector, now=now).reasons:
        if (not reason.gating or reason.alert is None or reason.code.startswith("OTA_DEVICE_")
                or ensure_utc(reason.alert.last_seen_at) < completed):
            continue  # an earlier boot's alert, accepted at approval, is not new evidence
        if reason.code in POST_INSTALL_DEFECTS or reason.code not in accepted:
            raise PlanHalt(f"POST_INSTALL_HOLD:{reason.code}",
                           f"{zone}: {reason.code} raised after SUCCEEDED: {reason.message}"[:1000], connector_id)
    if (not _versions_match(connector.firmware_version, plan.release_version)
            or (connector.ota_image_sha256 and connector.ota_image_sha256 != plan.application_sha256)):
        return f"{zone}: heartbeats do not report {plan.release_version} yet"
    diagnostics = connector.firmware_diagnostics or {}
    sampled = connector.firmware_diagnostics_at
    if (not connector.connected or not connector.boot_id or sampled is None or ensure_utc(sampled) <= completed
            or diagnostics.get("boot_id") != connector.boot_id):
        return f"{zone}: waiting for storage diagnostics from the new boot"
    if hard_storage_evidence(diagnostics):
        raise PlanHalt("POST_INSTALL_HOLD:STORAGE_FAULT", f"{zone}: the new boot reports a storage fault.",
                       connector_id)
    storage = diagnostics.get("storage") or {}
    if (storage.get("durability") != "HEALTHY" or storage.get("persistence_verified") is not True
            or storage.get("recovery_complete") is not True):
        return f"{zone}: storage not yet verified HEALTHY"
    holds = _diagnostic_holds(connector)
    if holds:
        return f"{zone}: {', '.join(sorted(holds))} on the new boot"
    link = terminal_link(connector, now)
    if link["state"] != "CONNECTED":
        return f"{zone}: terminal link {link['state']}"
    soak = timedelta(seconds=plan.soak_seconds)
    window_start = now - soak
    if window_start < completed:
        return f"{zone}: soak until {(completed + soak).strftime('%H:%M')} UTC"
    # Continuous heartbeats on one boot through the whole soak: no gap longer
    # than ADD's own offline threshold, and no reboot.
    gap = timedelta(seconds=settings.offline_after_seconds)
    rows = list(session.execute(select(DeviceTelemetry.created_at, DeviceTelemetry.boot_id,
                                       DeviceTelemetry.outbox_depth)
                                .where(DeviceTelemetry.connector_id == connector.id,
                                       DeviceTelemetry.created_at >= window_start)
                                .order_by(DeviceTelemetry.created_at, DeviceTelemetry.id)))
    times = [window_start, *(ensure_utc(row.created_at) for row in rows), now]
    if (not rows or any(row.boot_id != connector.boot_id for row in rows)
            or any(later - earlier > gap for earlier, later in zip(times, times[1:]))):
        return f"{zone}: soak restarts after a heartbeat gap or reboot"
    # Attendance delivery: nothing waiting on the ESP, or ADD accepted a batch
    # from the new image (a backlog drains through accepted batches).
    if rows[-1].outbox_depth != 0:
        delivered = session.scalar(select(AttendanceBatchReceipt.id).where(
            AttendanceBatchReceipt.connector_id == connector.id,
            AttendanceBatchReceipt.outcome.in_(COMMITTED_BATCH_OUTCOMES),
            AttendanceBatchReceipt.last_seen_at >= completed).limit(1)) or session.scalar(
            select(ZktObservationReceipt.id).where(ZktObservationReceipt.connector_id == connector.id,
                                                   ZktObservationReceipt.committed_at >= completed).limit(1))
        if not delivered:
            return f"{zone}: attendance waiting on the ESP; no batch accepted since the install"
    return None


def _monitor_stage(session: Session, plan: FirmwareHilPlan, now) -> list[tuple[dict, str]]:
    """Raise PlanHalt on any unsuccessful state; return what is still pending (empty when settled).

    An installed device is checked on every tick, even while its stage
    partner still installs, so a defect after SUCCEEDED halts at once.
    """
    rows = _stage_campaigns(session, plan, plan.current_stage)
    pending = []
    for device, campaign, deployment in rows:
        zone, connector_id = device["zone_id"], device["connector_id"]
        if campaign is None or deployment is None:
            raise PlanHalt("STAGE_CAMPAIGN_MISSING", f"{zone}: the stage campaign is missing.", connector_id)
        if deployment.status == "SUCCEEDED":
            # Installed, whatever its campaign's status since (a cancel after success).
            reason = _settle_reason(session, plan, device, deployment, now)
            if reason:
                pending.append((device, f"SETTLING: {reason}"))
            continue
        # The deployment first: a FAILED or ROLLED_BACK report also pauses its campaign.
        if deployment.status in UNSUCCESSFUL_TERMINAL:
            raise PlanHalt(f"DEPLOYMENT_{deployment.status}", f"{zone}: deployment {deployment.deployment_id} "
                           f"{deployment.status} ({deployment.error_code or 'no error code'}).", connector_id)
        if (campaign.status == "PAUSED" and plan.status == "PAUSED" and campaign.pause_reason == _pause_marker(plan)
                and deployment.status == "PENDING" and deployment.offered_at is None):
            pending.append((device, f"{zone}: paused with the plan before any offer"))
            continue
        if campaign.status != "ACTIVE":
            raise PlanHalt(f"CAMPAIGN_{campaign.status}", f"{zone}: campaign {campaign.campaign_id} is "
                           f"{campaign.status}: {campaign.pause_reason or 'no reason recorded'}", connector_id)
        if deployment.status == "PENDING" and deployment.error_code:
            if deployment.error_code not in TRANSIENT_ADMISSION:
                raise PlanHalt(f"ADMISSION_REFUSED:{deployment.error_code}", f"{zone}: offer refused by "
                               f"the fleet guard: {deployment.error_code}.", connector_id)
            pending.append((device, f"{zone}: offer waiting: {deployment.error_code}"))
            continue
        pending.append((device, f"{zone}: {deployment.status}"))
    return pending


def _lock_plan(session: Session, plan_pk: int, *, skip_locked: bool) -> FirmwareHilPlan | None:
    return session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.id == plan_pk)
                          .with_for_update(skip_locked=skip_locked)
                          .execution_options(populate_existing=True))


def _note_waiting(plan: FirmwareHilPlan, waiting: str, now) -> dict | None:
    if plan.waiting_reason == waiting[:200]:
        return None
    plan.waiting_reason = waiting[:200]
    plan.updated_at = now
    return {"plan_id": plan.plan_id, "status": plan.status, "waiting_reason": plan.waiting_reason}


def advance_plan(session: Session, plan_pk: int) -> dict | None:
    """One idempotent step. Safe to repeat, restart, or run from two workers."""
    plan = _lock_plan(session, plan_pk, skip_locked=True)
    if plan is None or plan.status not in OPEN_STATES:
        return None
    now = utc_now()
    _verify_pins(session, plan)
    timeout = timedelta(seconds=plan.stage_timeout_seconds)
    if plan.stage_started_at is not None:
        # A started stage is supervised to its end, even past the approval's
        # expiry: expiry only stops new stages.
        pending = _monitor_stage(session, plan, now)
        if not pending:
            completed = _complete_succeeded(_locked_open_campaigns(session, plan), now)
            _audit(session, plan, "FIRMWARE_HIL_PLAN_STAGE_SUCCEEDED", "SUCCEEDED", completed_campaigns=completed,
                   soak_minutes=plan.soak_seconds // 60)
            if plan.current_stage + 1 >= len(plan.stages):
                return _finish(session, plan, "COMPLETED")
            plan.current_stage += 1
            plan.stage_entered_at = now
            plan.stage_started_at = None
            plan.waiting_reason = None
            plan.updated_at = now
            return {"plan_id": plan.plan_id, "status": plan.status, "stage": plan.current_stage + 1}
        if plan.status == "RUNNING" and now >= ensure_utc(plan.stage_started_at) + timeout:
            # Attributed to a device that has not installed, else to one still settling.
            device, _reason = next((row for row in pending if not row[1].startswith("SETTLING")), pending[0])
            raise PlanHalt("STAGE_TIMEOUT", f"Stage {plan.current_stage + 1} did not reach SUCCEEDED and settle "
                           f"within {plan.stage_timeout_seconds // 60} minutes: "
                           + "; ".join(text_ for _device, text_ in pending), device["connector_id"])
        return _note_waiting(plan, "; ".join(text_ for _device, text_ in pending), now)
    if now >= ensure_utc(plan.expires_at) - timeout:
        message = ("The approval expired before the plan completed." if now >= ensure_utc(plan.expires_at) else
                   f"The approval ends before stage {plan.current_stage + 1} could finish within its "
                   f"{plan.stage_timeout_seconds // 60}-minute timeout.")
        return _finish(session, plan, "EXPIRED", code="PLAN_EXPIRED", message=message)
    if plan.status != "RUNNING":
        return None
    closed = _window_closed(plan, now)
    if closed:
        return _note_waiting(plan, closed, now)
    if (plan.waiting_reason or "").startswith("INSTALL_WINDOW_CLOSED"):
        plan.stage_entered_at = now  # the start timeout counts open-window time only
    devices = plan.stages[plan.current_stage]
    _check_stage_holds(session, devices)
    waiting = _wait_reason(session, plan, devices)
    if waiting:
        reason, connector_id = waiting
        if now >= ensure_utc(plan.stage_entered_at) + timeout:
            raise PlanHalt("STAGE_START_TIMEOUT", f"Stage {plan.current_stage + 1} could not start within "
                           f"{plan.stage_timeout_seconds // 60} minutes: {reason}", connector_id)
        return _note_waiting(plan, reason, now)
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
        # A stage device not yet offered is held too; one already offered
        # bytes continues (stopping it mid-install is never safer).
        _assignment_lock(session)
        paused = []
        for campaign, deployments in _locked_open_campaigns(session, plan):
            if campaign.status == "ACTIVE" and _never_offered(deployments):
                campaign.status = "PAUSED"
                campaign.pause_reason = _pause_marker(plan)
                campaign.updated_at = now
                paused.append(campaign.campaign_id)
        plan.status = "PAUSED"
        plan.updated_at = now
        audit("PAUSED", paused_campaigns=paused)
    elif action == "resume":
        if plan.status != "PAUSED":
            raise ValueError("Only a paused HIL plan can be resumed.")
        if now >= ensure_utc(plan.expires_at):
            raise ValueError("This HIL plan's approval has expired.")
        try:
            _verify_pins(session, plan)
        except PlanHalt as halt:
            raise ValueError(halt.message) from halt
        resumed = []
        for campaign, deployments in _locked_open_campaigns(session, plan):
            if (campaign.status == "PAUSED" and campaign.pause_reason == _pause_marker(plan)
                    and _never_offered(deployments)):
                campaign.status = "ACTIVE"
                campaign.pause_reason = None
                campaign.updated_at = now
                resumed.append(campaign.campaign_id)
        plan.status = "RUNNING"
        # Timeouts do not run while paused: the current phase starts afresh.
        if plan.stage_started_at is None:
            plan.stage_entered_at = now
        else:
            plan.stage_started_at = now
        plan.updated_at = now
        audit("RUNNING", resumed_campaigns=resumed)
    elif action == "cancel":
        # Never refused: it cancels only the plan campaigns that never offered
        # bytes; an installation in progress continues under its campaign.
        return _finish(session, plan, "CANCELLED", code="OWNER_CANCELLED", message=reason, actor=actor,
                       campaign_action="cancel")
    else:
        raise ValueError("Unknown HIL plan action.")
    return {"plan_id": plan.plan_id, "status": plan.status}


# --------------------------------------------------------------------------
# Views


def _views(session: Session, plans: list[FirmwareHilPlan]) -> list[dict]:
    """Serialize plans with two bounded queries for all of their campaigns."""
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment
    if not plans:
        return []
    campaigns = {(row.actor, row.idempotency_key): row for row in session.scalars(
        select(FirmwareCampaign).where(FirmwareCampaign.actor.in_([plan_actor(plan) for plan in plans])))}
    latest: dict[int, Any] = {}
    if campaigns:
        for row in session.scalars(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id.in_(
                [campaign.id for campaign in campaigns.values()])).order_by(FirmwareDeployment.id)):
            latest[row.campaign_id] = row
    views = []
    for plan in plans:
        stages = []
        for index, stage in enumerate(plan.stages):
            rows = []
            for device in stage:
                campaign = campaigns.get((plan_actor(plan), campaign_key(plan, index, device["connector_id"])))
                rows.append((device, campaign, latest.get(campaign.id) if campaign else None))
            if index < plan.current_stage or plan.status == "COMPLETED":
                state = "SUCCEEDED"
            elif index > plan.current_stage:
                state = "QUEUED"
            elif plan.status in TERMINAL_STATES:
                state = plan.status
            elif plan.stage_started_at is None:
                state = "WAITING"
            elif all(deployment is not None and deployment.status == "SUCCEEDED" for _d, _c, deployment in rows):
                state = "SETTLING"
            else:
                state = "RUNNING"
            stages.append({
                "index": index + 1, "status": state,
                "devices": [{
                    **{key: device.get(key) for key in ("zone_id", "connector_id", "display_name", "location",
                                                        "mac", "terminal_serial")},
                    "accepted_holds": list(device.get("accepted_holds") or []),
                    "campaign_id": campaign.campaign_id if campaign else None,
                    "campaign_status": campaign.status if campaign else None,
                    "deployment_id": deployment.deployment_id if deployment else None,
                    "deployment_status": deployment.status if deployment else None,
                    "error_code": deployment.error_code if deployment else None,
                } for device, campaign, deployment in rows],
            })
        views.append({
            "plan_id": plan.plan_id, "status": plan.status,
            "release": {"release_id": plan.release_public_id, "version": plan.release_version,
                        "image_sha256": plan.image_sha256, "application_sha256": plan.application_sha256},
            "plan_digest": plan.plan_digest, "reason": plan.reason, "approved_by": plan.approved_by,
            "approved_at": plan.approved_at, "expires_at": plan.expires_at,
            "stage_timeout_minutes": plan.stage_timeout_seconds // 60, "soak_minutes": plan.soak_seconds // 60,
            "window_start_hour": plan.window_start_hour, "window_end_hour": plan.window_end_hour,
            "current_stage": min(plan.current_stage + 1, len(plan.stages)), "stage_count": len(plan.stages),
            "stage_entered_at": plan.stage_entered_at, "stage_started_at": plan.stage_started_at,
            "waiting_reason": plan.waiting_reason, "halt_code": plan.halt_code, "halt_reason": plan.halt_reason,
            "completed_at": plan.completed_at, "updated_at": plan.updated_at, "stages": stages,
        })
    return views


def serialize_plan(session: Session, plan: FirmwareHilPlan) -> dict:
    return _views(session, [plan])[0]


def plan_rows(session: Session, *, limit: int = 20) -> list[dict]:
    return _views(session, list(session.scalars(
        select(FirmwareHilPlan).order_by(FirmwareHilPlan.id.desc()).limit(limit))))


def plan_detail(session: Session, plan_id: str) -> dict | None:
    plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))
    return serialize_plan(session, plan) if plan else None
