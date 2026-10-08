from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.hil_scope import HilTarget, parse_hil_targets, target_matches
from zk_add.hil_2615_cities import CITY_FACTORY_PREDECESSORS, CITY_TARGETS, SIGNED_BRIDGE_IDENTITIES
from zk_add.storage_recovery import (
    OFFERABLE_STATES as RECOVERY_OFFERABLE_STATES,
    RELEASE_IDS as RECOVERY_RELEASE_IDS,
    recovery_hil_targets,
    validate_recovery_image,
)
from zk_add.models import Connector, DeviceTelemetry, utc_column
from zk_add.settings import settings
from zk_add.terminal_families import require_family_match, release_family, require_production_qualification
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.storage_contract import (
    CANDIDATE_VERSION, COMPAT_MARKER, COMPAT_VERSION, DIRECT_BASELINE_IMAGES,
    validate_storage_contract,
)

OTA_LAYOUT = "zone-lite-ota-v1"
HIL_MARKER = ".hil-only.json"
# This one signed artifact has three independent first-stage HIL sites.  The
# identities below bind the policy to the published image and its existing
# exact-scope marker; no additional connector can be admitted by this rule.
HIL_269_PARALLEL_IDENTITY = (
    "zone-lite-2.6.9",
    "2.6.9",
    "b71da5291d8940c28b3179b26dc97e0a90da3841",
    "1ab737d94cc7965cf5d04fb02af17ad7b642c3bb3a56f9531235cf93bd62134d",
    "ad71339fef6926b21a21a05c1e1c4e30a936e0df5be6160871c7283841ad91b8",
)
HIL_269_PARALLEL_PREFIX_SIZE = 3
HIL_269_EXACT_TARGETS = (
    HilTarget(connector_id="ef1b6fe9-592b-4cf3-95e7-9c6b600f7812",
              mac="ac:27:6e:a5:47:64", terminal_serial="AEXH232260005"),
    HilTarget(connector_id="4567587c-29ee-4e59-92a4-6c36650a84aa",
              mac="e0:72:a1:d6:3c:7c", terminal_serial="PGB1261200077"),
    HilTarget(connector_id="2ca9a4c2-5ae4-4330-8d14-840223672897",
              mac="a4:cb:8f:d4:66:64", terminal_serial="PGB1261200074"),
    HilTarget(connector_id="bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e",
              mac="e0:72:a1:d7:05:c4", terminal_serial="CJH9211060009"),
    HilTarget(connector_id="233dac02-eb1b-4598-a876-e3a7b1ecfd54",
              mac="e0:72:a1:d5:08:a0", terminal_serial="CJH9211060002"),
)
# Append BLD5 only to these already published, immutable 2.6.15 bytes. The
# original five configured targets remain valid for every earlier release.
HIL_2615_BLD5_IDENTITY = (
    "zone-lite-2.6.15", "2.6.15",
    "a88998346d5b1ce1ddf4d19e3963b7245e46633b",
    "e2a2167fca307d73dbeb495bcc26baa535591794066b02a3de2848589c28887f",
    "832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6",
)
HIL_2615_BLD5_TARGET = HilTarget(
    connector_id="510baddb-8eff-4817-bc48-549ee34bbd0f",
    mac="ac:27:6e:a4:4e:d4", terminal_serial="PGB1261300022",
)
HIL_2615_BLD5_TARGETS = (*HIL_269_EXACT_TARGETS, HIL_2615_BLD5_TARGET)
# Preserve the six published identities and append the eight newly reviewed
# devices. Peshawar's two original identities join this independent city trial.
HIL_2615_CITY_TARGETS = (*HIL_2615_BLD5_TARGETS, *CITY_TARGETS.values())
# The live 3FL connector boots the signed 2.4.12 application from factory.
# A direct 2.6.x boot would lack a qualified predecessor in the other OTA
# slot. Bridge only this exact device through the already published 2.5.2
# image, then allow the current signed HIL update after 2.5.2 boot confirmation.
FACTORY_3FL_BRIDGE_RELEASE = (
    "zone-lite-2.5.2", "2.5.2",
    "e818e8e7db5d9aa1c92b798d03d088026b36bbe9f4672f908450aa4aa85ef564",
    DIRECT_BASELINE_IMAGES["2.5.2"],
)
FACTORY_3FL_BRIDGE_ZONE = "ZONE-SLICTOWER-3FL"
FACTORY_3FL_BRIDGE_TARGET = HIL_269_EXACT_TARGETS[2]
ACTIVE_DEPLOYMENT_STATES = {
    "OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"
}
TERMINAL_DEPLOYMENT_STATES = {
    "SUCCEEDED", "FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"
}
DEPLOYMENT_TRANSITIONS = {
    "PENDING": {"OFFERED", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"},
    "OFFERED": {
        "OFFERED", "DOWNLOADING", "READY_TO_BOOT", "BOOTED_PENDING",
        "FAILED", "CANCELLED", "RELEASE_REVOKED",
    },
    "DOWNLOADING": {
        "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING",
        "FAILED", "CANCELLED", "RELEASE_REVOKED",
    },
    "VERIFYING": {
        "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING",
        "FAILED", "CANCELLED", "RELEASE_REVOKED",
    },
    "READY_TO_BOOT": {"READY_TO_BOOT", "BOOTED_PENDING", "FAILED", "ROLLED_BACK", "RELEASE_REVOKED"},
    "BOOTED_PENDING": {"BOOTED_PENDING", "RECONCILING", "FAILED", "ROLLED_BACK", "RELEASE_REVOKED"},
    "RECONCILING": {"RECONCILING", "SUCCEEDED", "FAILED", "ROLLED_BACK", "RELEASE_REVOKED"},
}
SEMVER_PATTERN = re.compile(r"^(?:zone-lite-)?(\d+)\.(\d+)\.(\d+)$")


class FirmwareRelease(Base):
    __tablename__ = "add_firmware_releases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    git_sha: Mapped[str] = mapped_column(String(64), index=True)
    image_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    image_size: Mapped[int] = mapped_column(BigInteger)
    signing_key_id: Mapped[str] = mapped_column(String(80))
    partition_layout: Mapped[str] = mapped_column(String(80))
    minimum_bootstrap_version: Mapped[str] = mapped_column(String(80), default="2.2.0")
    storage_name: Mapped[str] = mapped_column(String(255), unique=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    manifest_signature: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30), default="AVAILABLE", index=True)
    published_at: Mapped[Any] = utc_column()
    revoked_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(120))


class FirmwareCampaign(Base):
    __tablename__ = "add_firmware_campaigns"
    __table_args__ = (
        UniqueConstraint("actor", "idempotency_key", name="uq_add_firmware_campaign_actor_idempotency"),
        Index(
            "uq_add_firmware_campaign_active_zone",
            "zone_id",
            unique=True,
            postgresql_where=text("status IN ('ACTIVE', 'PAUSED')"),
            sqlite_where=text("status IN ('ACTIVE', 'PAUSED')"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_releases.id"), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    actor: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text)
    typed_confirmation: Mapped[str] = mapped_column(String(80))
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    legacy_skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    pause_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[Any] = utc_column()
    updated_at: Mapped[Any] = utc_column()


class FirmwareDeployment(Base):
    __tablename__ = "add_firmware_deployments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_campaigns.id"), index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_releases.id"), index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    previous_version: Mapped[str | None] = mapped_column(String(80))
    target_version: Mapped[str] = mapped_column(String(80))
    bytes_written: Mapped[int] = mapped_column(BigInteger, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    offered_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[Any] = utc_column()
    updated_at: Mapped[Any] = utc_column()


class FirmwareEvent(Base):
    __tablename__ = "add_firmware_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_deployments.id"), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[Any] = utc_column()


class FirmwareDownloadGrant(Base):
    __tablename__ = "add_firmware_download_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_deployments.id"), index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[Any] = utc_column()
    last_used_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))


class FirmwareHilRun(Base):
    __tablename__ = "add_firmware_hil_runs"
    __table_args__ = (
        UniqueConstraint("actor", "idempotency_key", name="uq_add_hil_run_actor_key"),
        Index("uq_add_hil_run_active_connector", "connector_id", unique=True,
              postgresql_where=text("status = 'OBSERVING'"),
              sqlite_where=text("status = 'OBSERVING'")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_deployments.id"), index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_releases.id"), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="OBSERVING", index=True)
    target: Mapped[dict] = mapped_column(JSON)
    release_identity: Mapped[dict] = mapped_column(JSON)
    baseline: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[Any] = utc_column()
    ends_at: Mapped[Any] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSON, default=dict)


def _require_previous_candidate_acceptance(
    session: Session, release: FirmwareRelease, targets: list[HilTarget], index: int,
) -> None:
    if release.version != COMPAT_VERSION or index == 0:
        return
    candidate = session.scalar(select(FirmwareRelease).where(
        FirmwareRelease.version == CANDIDATE_VERSION,
        FirmwareRelease.state == "HIL_ONLY",
    ))
    if candidate is None or (candidate.manifest or {}).get("_hil_targets") != [
        target.model_dump() for target in targets
    ]:
        raise ValueError("The matching hardening candidate must pass the previous target before compatibility rollout continues.")
    events = list(session.execute(
        select(FirmwareEvent, Connector, FirmwareDeployment)
        .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(Connector, FirmwareDeployment.connector_id == Connector.id)
        .where(
            FirmwareDeployment.release_id == candidate.id,
            FirmwareEvent.state.in_(["HIL_ACCEPTED", "HIL_FAILED", "HIL_INCOMPLETE"]),
        ).order_by(FirmwareEvent.id)
    ))
    for target in targets[:index]:
        evidence = [
            (event, deployment) for event, connector, deployment in events
            if target_matches(target, connector)
            and (event.details or {}).get("target") == target.model_dump()
            and event.details.get("git_sha") == candidate.git_sha
            and event.details.get("artifact_sha256") == candidate.image_sha256
            and event.details.get("application_sha256") == _application_sha256(candidate)
        ]
        if not evidence:
            raise ValueError("The previous target has no hardening-candidate HIL acceptance.")
        event, deployment = evidence[-1]
        if event.state != "HIL_ACCEPTED" or event.details.get("outcome") != "PASS" or deployment.status != "SUCCEEDED":
            raise ValueError("The previous target must pass hardening-candidate HIL before the next compatibility update.")


def _parallel_hil_prefix(release: FirmwareRelease, targets: list[HilTarget]) -> int:
    identity = (
        release.release_id, release.version, release.git_sha,
        release.image_sha256, _application_sha256(release),
    )
    exact_scope = tuple(targets) == HIL_269_EXACT_TARGETS
    published_269 = identity == HIL_269_PARALLEL_IDENTITY
    signed_patch = (
        release.release_id == f"zone-lite-{release.version}"
        and release.version in {"2.6.10", "2.6.11", "2.6.12", "2.6.13", "2.6.14", "2.6.15"}
        and release.state == "HIL_ONLY"
        and bool(re.fullmatch(r"[0-9a-f]{40}", release.git_sha))
        and bool(re.fullmatch(r"[0-9a-f]{64}", release.image_sha256))
        and _application_sha256(release) is not None
    )
    return HIL_269_PARALLEL_PREFIX_SIZE if exact_scope and (published_269 or signed_patch) else 0


def _is_2615_bld5_extension(release: FirmwareRelease, targets: list[HilTarget]) -> bool:
    return (
        release.state == "HIL_ONLY"
        and (release.release_id, release.version, release.git_sha,
             release.image_sha256, _application_sha256(release)) == HIL_2615_BLD5_IDENTITY
        and tuple(targets) == HIL_2615_BLD5_TARGETS
    )


def _parse_release_hil_targets(identity: tuple, raw: Any) -> list[HilTarget]:
    if identity[:2] == ("zone-lite-2.6.22", "2.6.22"):
        from zk_add.zkt_factory_contract import factory_trial_exposure
        return [HilTarget.model_validate(item) for item in factory_trial_exposure(raw)]
    if identity[:2] in {("zone-lite-2.6.16", "2.6.16"), ("zone-lite-2.6.17", "2.6.17"), ("zone-lite-2.6.18", "2.6.18"), ("zone-lite-2.6.19", "2.6.19"), ("zone-lite-2.6.20", "2.6.20"), ("zone-lite-2.6.21", "2.6.21"), ("zone-lite-2.6.23", "2.6.23"), ("zone-lite-2.7.0", "2.7.0")}:
        from zk_add.zkt_bridge_contract import bridge_hil_targets
        return bridge_hil_targets(raw)
    if identity[1] in RECOVERY_RELEASE_IDS and identity[0] == RECOVERY_RELEASE_IDS[identity[1]]:
        return recovery_hil_targets(raw)
    # The general parser retains its eight-device limit. Only the exact
    # already signed image and reviewed fourteen-device scope can exceed it.
    if identity == HIL_2615_BLD5_IDENTITY and raw == [target.model_dump() for target in HIL_2615_CITY_TARGETS]:
        return list(HIL_2615_CITY_TARGETS)
    return parse_hil_targets(raw)


def _permitted_hil_targets(session: Session, release: FirmwareRelease) -> list[HilTarget] | None:
    from zk_add.bridge_observation import EVENTS as BRIDGE_EVENTS, ready_event_matches
    raw = (release.manifest or {}).get("_hil_targets")
    if raw is None:
        return None
    if release.version in RECOVERY_RELEASE_IDS:
        # Exact signed one-shot scope. Each target starts independently and is
        # never ordered behind another target's acceptance: the image always
        # returns to 2.5.2. The shared ordered HIL configuration is untouched.
        if not settings.firmware_hil_enabled:
            raise ValueError("Ordered firmware HIL quarantine is disabled.")
        validate_storage_contract(release.manifest or {}, release.version)
        if release.state != "HIL_ONLY" or release.release_id != RECOVERY_RELEASE_IDS[release.version]:
            raise ValueError("Storage recovery is restricted to its exact HIL-only release.")
        return recovery_hil_targets(raw)
    bridge = release.version in {"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23", "2.7.0"}
    if not settings.firmware_hil_enabled or (not bridge and not settings.firmware_hil_targets_json):
        raise ValueError("Ordered firmware HIL quarantine is disabled.")
    if bridge:
        # Exact signed scope replaces the old shared configuration for the
        # bridge only. Registering it cannot change an older campaign's scope.
        validate_storage_contract(release.manifest or {}, release.version)
    identity = (release.release_id, release.version, release.git_sha,
                release.image_sha256, _application_sha256(release))
    targets = _parse_release_hil_targets(identity, raw)
    configured = targets if bridge else parse_hil_targets(json.loads(settings.firmware_hil_targets_json))
    bld5_extension = _is_2615_bld5_extension(release, targets)
    city_extension = (release.state == "HIL_ONLY" and identity == HIL_2615_BLD5_IDENTITY
                      and tuple(targets) == HIL_2615_CITY_TARGETS)
    if targets != configured and not (
        (bld5_extension or city_extension) and tuple(configured) == HIL_269_EXACT_TARGETS
    ):
        raise ValueError("Ordered HIL targets do not match the configured exact scope.")
    from zk_add.zkt_hil_schedule import schedule
    nationwide = schedule(session, release)
    if nationwide is not None:
        if nationwide["hold"]:
            raise ValueError("Nationwide HIL scope held: " + nationwide["hold"])
        if nationwide["selected"] is None:
            raise ValueError("No online pending target is available in the exposed HIL scope; deferred targets remain incomplete.")
        return [HilTarget.model_validate(nationwide["selected"])]
    events = list(session.execute(
        select(FirmwareEvent, Connector, FirmwareDeployment)
        .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(Connector, FirmwareDeployment.connector_id == Connector.id)
        .where(
            FirmwareDeployment.release_id == release.id,
            FirmwareEvent.state.in_(["HIL_ACCEPTED", "HIL_FAILED", "HIL_INCOMPLETE", *BRIDGE_EVENTS]),
        ).order_by(FirmwareEvent.id)
    ))
    def accepted(target: HilTarget) -> bool:
        evidence = [
            (event, deployment)
            for event, connector, deployment in events
            if target_matches(target, connector)
            and event.details.get("target") == target.model_dump()
            and event.details.get("git_sha") == release.git_sha
            and event.details.get("artifact_sha256") == release.image_sha256
            and event.details.get("application_sha256") == _application_sha256(release)
        ]
        if not evidence:
            return False
        latest, deployment = evidence[-1]
        if release.version in {"2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23"}:
            return deployment.status == "SUCCEEDED" and ready_event_matches(session, latest, deployment, release)
        if release.version == "2.7.0":
            from zk_add.hil_runs import accepted_full_event_matches
            return accepted_full_event_matches(session, latest, deployment, release)
        return (latest.state == "HIL_ACCEPTED" and latest.details.get("outcome") == "PASS"
                and deployment.status == "SUCCEEDED")

    if city_extension:
        pending = [target for target in targets if not accepted(target)]
        if pending:
            return pending
        raise ValueError("All reviewed city HIL targets already have acceptance; release remains HIL_ONLY.")

    parallel_prefix = (HIL_269_PARALLEL_PREFIX_SIZE if bld5_extension
                       else _parallel_hil_prefix(release, targets))
    if parallel_prefix:
        independent = targets[:parallel_prefix]
        if bld5_extension:
            independent = [*independent, HIL_2615_BLD5_TARGET]
        pending = [target for target in independent if not accepted(target)]
        if pending:
            return pending
    ordered_end = len(targets) - 1 if bld5_extension else len(targets)
    for index, target in enumerate(targets[parallel_prefix:ordered_end], start=parallel_prefix):
        if not accepted(target):
            _require_previous_candidate_acceptance(session, release, targets, index)
            return [target]
    raise ValueError("All ordered HIL targets already have acceptance; release remains HIL_ONLY.")


def _ordered_hil_target(session: Session, release: FirmwareRelease) -> HilTarget | None:
    """Compatibility view for callers that need the first permitted target."""
    permitted = _permitted_hil_targets(session, release)
    return permitted[0] if permitted else None


def _factory_3fl_bridge_target(release: FirmwareRelease, zone_id: str) -> HilTarget | None:
    city_predecessor = CITY_FACTORY_PREDECESSORS.get(zone_id)
    if city_predecessor and release.release_id == f"zone-lite-{city_predecessor[2]}":
        identity = (release.release_id, release.version, release.git_sha,
                    release.image_sha256, _application_sha256(release))
        if release.state != "AVAILABLE" or identity != SIGNED_BRIDGE_IDENTITIES[city_predecessor[2]]:
            raise ValueError("The city factory bridge requires the exact published signed image.")
        return CITY_TARGETS[zone_id]
    if zone_id != FACTORY_3FL_BRIDGE_ZONE or release.release_id != "zone-lite-2.5.2":
        return None
    identity = (
        release.release_id, release.version,
        release.image_sha256, _application_sha256(release),
    )
    if release.state != "AVAILABLE" or identity != FACTORY_3FL_BRIDGE_RELEASE:
        raise ValueError("The SLICTOWER 3FL bridge requires the exact published 2.5.2 image.")
    return FACTORY_3FL_BRIDGE_TARGET


def _factory_3fl_bridge_exclusion(connector: Connector, target: HilTarget) -> str | None:
    if not target_matches(target, connector):
        return "BRIDGE_EXACT_IDENTITY_MISMATCH"
    if not connector.connected:
        return "BRIDGE_TARGET_OFFLINE"
    predecessor = CITY_FACTORY_PREDECESSORS.get(connector.zone_id)
    version, digest = predecessor[:2] if predecessor else ("2.4.12", DIRECT_BASELINE_IMAGES["2.4.12"])
    if (not _versions_match(connector.firmware_version, version)
            or connector.ota_running_partition != "factory"
            or connector.ota_image_sha256 != digest):
        return "BRIDGE_FACTORY_PREDECESSOR_MISMATCH"
    return None


def capability_is_eligible(connector: Connector) -> bool:
    return bool(connector.ota_capable and connector.ota_secure_boot and connector.ota_rollback_enabled
                and connector.ota_partition_layout == OTA_LAYOUT)


def semantic_version(value: str | None) -> tuple[int, int, int] | None:
    match = SEMVER_PATTERN.fullmatch((value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_at_least(running: str | None, minimum: str) -> bool:
    running_version = semantic_version(running)
    minimum_version = semantic_version(minimum)
    return bool(running_version and minimum_version and running_version >= minimum_version)


def configured_hil_mac(release: FirmwareRelease) -> str:
    value = (settings.firmware_hikvision_hil_target_mac
             if release_family(release.manifest or {}) == "hikvision"
             else settings.firmware_hil_target_mac)
    return (value or "").strip().lower()


def _scope_exclusion_reason(
    connector: Connector, *, hil_target_mac: str = "", minimum_version: str = "2.2.0"
) -> str | None:
    if not connector.ota_capable:
        return "OTA_NOT_CAPABLE"
    if not connector.ota_secure_boot:
        return "SECURE_BOOT_REQUIRED"
    if not connector.ota_rollback_enabled:
        return "ROLLBACK_REQUIRED"
    if connector.ota_partition_layout != OTA_LAYOUT:
        return "PARTITION_LAYOUT_MISMATCH"
    if not version_at_least(connector.firmware_version, minimum_version):
        return "BOOTSTRAP_VERSION_TOO_OLD"
    if hil_target_mac and connector.hardware_id.lower() != hil_target_mac:
        return "HIL_TARGET_MISMATCH"
    return None


def _scope_digest(
    release: FirmwareRelease, zone_id: str, connectors: list[Connector],
    eligible: list[Connector], schedule_state: dict | None = None, *, session: Session | None = None,
) -> str:
    payload = {
        "release_id": release.release_id,
        "release_state": release.state,
        "artifact_sha256": release.image_sha256,
        "application_sha256": _application_sha256(release),
        "hil_targets": (release.manifest or {}).get("_hil_targets"),
        "hil_target_mac": (release.manifest or {}).get("_hil_target_mac"),
        "nationwide_schedule": schedule_state["decision_sha256"] if schedule_state else None,
        "eligible": sorted(row.connector_id for row in eligible),
        "version": release.version,
        "zone_id": zone_id,
        "connectors": [
            {
                "connector_id": row.connector_id,
                "active": row.active,
                "is_spare": row.is_spare,
                "firmware_version": row.firmware_version,
                "terminal_serial": row.zkt_device.serial if row.zkt_device else None,
                "confirmed_serial": row.zkt_device.confirmed_serial if row.zkt_device else None,
                "expected_serial": row.zkt_device.expected_serial if row.zkt_device else None,
                "hardware_id": row.hardware_id.lower(),
                "ota_capable": row.ota_capable,
                "ota_secure_boot": row.ota_secure_boot,
                "ota_rollback_enabled": row.ota_rollback_enabled,
                "ota_partition_layout": row.ota_partition_layout,
            }
            for row in sorted(connectors, key=lambda item: item.connector_id)
        ],
    }
    if release.version == "2.6.22":
        from zk_add.zkt_factory_trial import predecessor_snapshot
        payload["factory_trial_baselines"] = {row.connector_id: predecessor_snapshot(session, release, row)
            for row in eligible}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scope_signing_key() -> bytes:
    return settings.effective_fleet_root_secret.encode()


def _encode_scope_token(payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(_scope_signing_key(), encoded, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded.decode()}.{encoded_signature.decode()}"


def _decode_scope_token(token: str) -> dict[str, Any]:
    try:
        encoded, encoded_signature = token.split(".", 1)
        expected = hmac.new(_scope_signing_key(), encoded.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("Firmware scope preview token is invalid.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Firmware scope preview token is invalid.")
    return payload


def _storage_predecessor_exclusion(session: Session, release: FirmwareRelease, connector: Connector,
                                   *, deployment_id: int | None = None) -> str | None:
    try:
        contract = validate_storage_contract(release.manifest or {}, release.version)
    except ValueError:
        return "STORAGE_CONTRACT_INVALID"
    if release.version == "2.6.22":
        from zk_add.zkt_factory_trial import admission_hold
        return admission_hold(session, release, connector, own_deployment_id=deployment_id)
    if release.version == "2.7.0":
        from zk_add.zkt_writer_contract import writer_predecessor_hold
        return writer_predecessor_hold(session, connector, release) or (
            None if connector.zkt_custody_enabled else "JOURNAL_ADD_CUSTODY_DISABLED")
    if release.version in {"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.23"} and release.state != "HIL_ONLY":
        return "JOURNAL_BRIDGE_HIL_ONLY"
    if release.version in RECOVERY_RELEASE_IDS and release.state != "HIL_ONLY":
        return "STORAGE_RECOVERY_HIL_ONLY"
    if contract and contract.get("allowed_bootstrap_versions") is not None:
        qualified_version = next(
            (
                version for version in contract["allowed_bootstrap_versions"]
                if _versions_match(connector.firmware_version, version)
            ),
            None,
        )
        if qualified_version is None:
            return "DIRECT_BOOTSTRAP_VERSION_UNQUALIFIED"
        expected_digest = contract["allowed_bootstrap_images"][qualified_version]
        hil_retry = (release.version == "2.6.7" and qualified_version == "2.6.6") or (
            release.version == "2.6.8" and qualified_version in {"2.6.6", "2.6.7"}) or (
            release.version == "2.6.9" and qualified_version in {"2.6.6", "2.6.7", "2.6.8"}) or (
            release.version == "2.6.10" and qualified_version in {"2.6.6", "2.6.7", "2.6.8", "2.6.9"}) or (
            release.version in {"2.6.11", "2.6.12", "2.6.13", "2.6.14", "2.6.15"} and qualified_version in {"2.6.6", "2.6.7", "2.6.8", "2.6.9", "2.6.10"}) or (
            release.version in {"2.6.13", "2.6.14", "2.6.15"} and qualified_version == "2.6.12") or (
            release.version in {"2.6.14", "2.6.15"} and qualified_version == "2.6.13") or (
            release.version == "2.6.15" and qualified_version == "2.6.14") or (
            release.version in {"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.23"} and qualified_version == "2.6.15")
        allowed_state = {"AVAILABLE", "HIL_ONLY"} if hil_retry else {"AVAILABLE"}
        predecessor = session.scalar(select(FirmwareRelease).where(
            FirmwareRelease.release_id == f"zone-lite-{qualified_version}",
            FirmwareRelease.state.in_(allowed_state),
        ))
        if (predecessor is None or _application_sha256(predecessor) != expected_digest or
                connector.ota_image_sha256 != expected_digest or
                connector.ota_running_partition not in {"ota_0", "ota_1"}):
            return "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    if not contract or contract["write_format"] != 2:
        return None
    if not _versions_match(connector.firmware_version, COMPAT_VERSION):
        return "COMPATIBILITY_FIRMWARE_REQUIRED"
    reported = connector.firmware_diagnostics_at
    storage = (connector.firmware_diagnostics or {}).get("storage") or {}
    if (reported is None or not 0 <= (utc_now() - ensure_utc(reported)).total_seconds() <= 45 or
            storage.get("upgrade_ready") is not True or storage.get("upgrade_contract") != COMPAT_MARKER or
            storage.get("upgrade_error") or storage.get("durability") != "HEALTHY" or
            storage.get("persistence_verified") is not True or storage.get("recovery_complete") is not True):
        return "COMPATIBILITY_RECOVERY_NOT_VERIFIED"
    # Require the latest successful, digest-checked boot, not any old version claim.
    accepted = session.execute(select(FirmwareEvent, FirmwareDeployment, FirmwareRelease)
        .join(FirmwareDeployment, FirmwareEvent.deployment_id == FirmwareDeployment.id)
        .join(FirmwareRelease, FirmwareDeployment.release_id == FirmwareRelease.id)
        .where(FirmwareDeployment.connector_id == connector.id, FirmwareEvent.state == "SUCCEEDED")
        .order_by(FirmwareEvent.id.desc()).limit(1)).first()
    if accepted is None:
        return "COMPATIBILITY_ACCEPTANCE_MISSING"
    event, deployment, predecessor = accepted
    details = event.details or {}
    try:
        predecessor_contract = validate_storage_contract(predecessor.manifest or {}, predecessor.version)
    except ValueError:
        predecessor_contract = None
    if (deployment.status != "SUCCEEDED" or predecessor.state not in {"AVAILABLE", "HIL_ONLY"} or
            predecessor.version != COMPAT_VERSION or not predecessor_contract or
            details.get("image_sha256") != _application_sha256(predecessor) or
            not _versions_match(details.get("running_version"), COMPAT_VERSION) or
            details.get("running_partition") not in {"ota_0", "ota_1"}):
        return "COMPATIBILITY_ACCEPTANCE_MISMATCH"
    return None


def _campaign_scope(
    session: Session,
    *,
    release_public_id: str,
    zone_id: str,
) -> tuple[FirmwareRelease, list[Connector], list[Connector], list[tuple[Connector, str]]]:
    sync_release_store(session)
    release = session.scalar(
        select(FirmwareRelease).where(
            FirmwareRelease.release_id == release_public_id,
            FirmwareRelease.state.in_(["AVAILABLE", "HIL_ONLY"]),
        )
    )
    if release is None:
        raise ValueError("Firmware release is not available.")
    if _application_sha256(release) is None:
        raise ValueError("Firmware release lacks the ESP application digest required by OTA bootstraps.")
    if release.state == "AVAILABLE" and not settings.firmware_ota_enabled:
        raise ValueError("National firmware OTA remains disabled.")
    hil_target_mac = str((release.manifest or {}).get("_hil_target_mac") or "").lower()
    permitted_targets = _permitted_hil_targets(session, release) if release.state == "HIL_ONLY" else None
    bridge_target = _factory_3fl_bridge_target(release, zone_id)
    if release.state == "HIL_ONLY" and permitted_targets is None:
        configured_target = configured_hil_mac(release)
        if not settings.firmware_hil_enabled or not configured_target:
            raise ValueError("Firmware HIL quarantine is disabled.")
        if hil_target_mac != configured_target:
            raise ValueError("HIL release target does not match the configured ESP MAC.")
    connectors = list(
        session.scalars(
            select(Connector)
            .where(Connector.zone_id == zone_id, Connector.active == True)  # noqa: E712
            .order_by(Connector.display_name.asc(), Connector.connector_id.asc())
        ).all()
    )
    excluded: list[tuple[Connector, str]] = []
    eligible: list[Connector] = []
    for connector in connectors:
        reason = _scope_exclusion_reason(
            connector,
            hil_target_mac=hil_target_mac if release.state == "HIL_ONLY" and permitted_targets is None else "",
            minimum_version=release.minimum_bootstrap_version,
        )
        try:
            require_family_match(connector.firmware_family, release.manifest or {})
        except ValueError:
            reason = "FIRMWARE_FAMILY_MISMATCH"
        if permitted_targets and not any(target_matches(target, connector) for target in permitted_targets):
            reason = "HIL_EXACT_IDENTITY_MISMATCH"
        if bridge_target:
            reason = _factory_3fl_bridge_exclusion(connector, bridge_target) or reason
        if not reason:
            reason = _storage_predecessor_exclusion(session, release, connector)
        if reason:
            excluded.append((connector, reason))
        else:
            eligible.append(connector)
    if release.state == "HIL_ONLY":
        # Keep the quarantine boundary explicit even though the exclusion
        # classifier above already rejects every non-target connector.
        if permitted_targets is None:
            eligible = [row for row in eligible if row.hardware_id.lower() == hil_target_mac]
        else:
            eligible = [row for row in eligible if any(
                target_matches(target, row) for target in permitted_targets
            )]
        if len(eligible) != 1:
            if permitted_targets is not None:
                target_row = next(
                    (row for row in connectors if any(
                        row.connector_id == target.connector_id for target in permitted_targets
                    )),
                    None,
                )
                if target_row is None:
                    raise ValueError(
                        "HIL campaign requires exactly one eligible connector with the target MAC; "
                        "the exact target is not active in this zone."
                    )
                exclusion = next(
                    (reason for row, reason in excluded if row.id == target_row.id),
                    None,
                )
                if exclusion:
                    raise ValueError(
                        "HIL campaign requires exactly one eligible connector with the target MAC; "
                        f"target exclusion: {exclusion}."
                    )
            raise ValueError("HIL campaign requires exactly one eligible connector with the target MAC.")
    if bridge_target and len(eligible) != 1:
        target_row = next((row for row in connectors if row.connector_id == bridge_target.connector_id), None)
        exclusion = next((reason for row, reason in excluded if row.id == target_row.id), None) if target_row else None
        raise ValueError(
            "Factory-to-OTA bridge requires exactly one eligible exact connector"
            + (f"; target exclusion: {exclusion}." if exclusion else ".")
        )
    return release, connectors, eligible, excluded


def preview_campaign_scope(
    session: Session,
    *,
    release_public_id: str,
    zone_id: str,
    ttl_seconds: int = 5 * 60,
) -> dict[str, Any]:
    release, connectors, eligible, excluded = _campaign_scope(
        session,
        release_public_id=release_public_id,
        zone_id=zone_id,
    )
    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    from zk_add.zkt_hil_schedule import schedule
    nationwide = schedule(session, release)
    digest = _scope_digest(release, zone_id, connectors, eligible, nationwide, session=session)
    token = _encode_scope_token(
        {
            "release_id": release.release_id,
            "zone_id": zone_id,
            "scope_digest": digest,
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(12),
        }
    )

    def connector_summary(row: Connector) -> dict[str, Any]:
        return {
            "connector_id": row.connector_id,
            "display_name": row.display_name,
            "zone_id": row.zone_id,
            "hardware_id": row.hardware_id,
            "firmware_version": row.firmware_version,
            "connected": row.connected,
            "ota_state": row.ota_state,
        }

    return {
        "scope_token": token,
        "expires_at": expires_at,
        "release": {
            "release_id": release.release_id,
            "version": release.version,
            "state": release.state,
        },
        "zone_id": zone_id,
        "hil_schedule": nationwide,
        "counts": {
            "candidates": len(connectors),
            "eligible": len(eligible),
            "excluded": len(excluded),
            "offline": sum(1 for row in connectors if not row.connected),
        },
        "eligible": [connector_summary(row) for row in eligible],
        "excluded": [
            {**connector_summary(row), "reason": reason}
            for row, reason in excluded
        ],
    }


def verify_campaign_scope_token(
    session: Session,
    *,
    token: str,
    release_public_id: str,
    zone_id: str,
) -> tuple[FirmwareRelease, list[Connector], list[Connector]]:
    payload = _decode_scope_token(token)
    if int(payload.get("exp") or 0) <= int(utc_now().timestamp()):
        raise ValueError("Firmware scope preview expired. Refresh the preview and confirm again.")
    if payload.get("release_id") != release_public_id or payload.get("zone_id") != zone_id:
        raise ValueError("Firmware scope preview does not match this release and zone.")
    release, connectors, eligible, _excluded = _campaign_scope(
        session,
        release_public_id=release_public_id,
        zone_id=zone_id,
    )
    from zk_add.zkt_hil_schedule import schedule
    if payload.get("scope_digest") != _scope_digest(release, zone_id, connectors, eligible, schedule(session, release), session=session):
        raise ValueError("Firmware scope changed. Refresh the preview before starting the campaign.")
    return release, connectors, eligible


def _versions_match(running: str | None, target: str) -> bool:
    return semantic_version(running) is not None and semantic_version(running) == semantic_version(target)


def _application_sha256(release: FirmwareRelease) -> str | None:
    value = str((release.manifest or {}).get("application_sha256") or "")
    if len(value) != 64 or value != value.lower() or any(
        character not in "0123456789abcdef" for character in value
    ):
        return None
    return value


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("Only one byte range is supported.")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator or not start_text.isdigit():
        raise ValueError("Invalid byte range.")
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ValueError("Byte range is outside the firmware image.")
    return start, min(end, size - 1)


def _verify_manifest(manifest: dict, signature_b64: str) -> None:
    if not settings.firmware_signing_public_key_pem_b64:
        raise RuntimeError("ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64 is required for OTA.")
    public_key = serialization.load_pem_public_key(base64.b64decode(settings.firmware_signing_public_key_pem_b64))
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    public_key.verify(base64.b64decode(signature_b64), canonical,
                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


def sync_release_store(session: Session) -> None:
    if not (settings.firmware_ota_enabled or settings.firmware_hil_enabled):
        return
    root = Path(settings.firmware_store_path).resolve()
    if not root.is_dir():
        raise RuntimeError("Configured firmware store is unavailable.")
    for manifest_path in root.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        release_id = str(manifest.get("release_id", ""))
        if not release_id:
            continue
        signature = manifest_path.with_name("manifest.sig").read_text(encoding="ascii").strip()
        _verify_manifest(manifest, signature)
        release_family(manifest)
        validate_storage_contract(manifest, str(manifest.get("version", "")))
        image_name = os.path.basename(str(manifest["image_name"]))
        image = manifest_path.parent / image_name
        image_bytes = image.read_bytes()
        digest = hashlib.sha256(image_bytes).hexdigest()
        if not hmac.compare_digest(digest, str(manifest["image_sha256"])) or image.stat().st_size != int(manifest["image_size"]):
            raise RuntimeError(f"Firmware release {release_id} failed immutable artifact verification.")
        if manifest.get("version") in {"2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21", "2.6.22", "2.6.23"}:
            from zk_add.zkt_bridge_contract import validate_bridge_image
            validate_bridge_image(image_bytes, manifest["version"])
        if manifest.get("version") == "2.7.0":
            from zk_add.zkt_writer_contract import validate_writer_image
            validate_writer_image(image_bytes, manifest)
        if manifest.get("version") in RECOVERY_RELEASE_IDS:
            validate_recovery_image(image_bytes, manifest["version"])
        application_digest = str(manifest.get("application_sha256") or "")
        if application_digest and (
            len(application_digest) != 64 or application_digest != application_digest.lower() or
            any(character not in "0123456789abcdef" for character in application_digest)
        ):
            raise RuntimeError(f"Firmware release {release_id} has an invalid ESP application digest.")
        if int(manifest.get("schema_version", 1)) >= 2 and not application_digest:
            raise RuntimeError(f"Firmware release {release_id} is missing its ESP application digest.")
        if manifest.get("partition_layout") != OTA_LAYOUT:
            raise RuntimeError(f"Firmware release {release_id} has an unknown partition layout.")
        marker_path = manifest_path.parent / HIL_MARKER
        hil_target_mac = None
        hil_targets = None
        desired_state = "AVAILABLE"
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if manifest.get("release_channel") == "EXPERIMENTAL_HIL_ONLY" and (
                type(marker.get("schema_version")) is not int or marker["schema_version"] != 2
                or "targets" not in marker
            ):
                raise ValueError("Experimental journal bridge requires ordered exact HIL identities.")
            if "targets" in marker:
                identity = (release_id, str(manifest.get("version", "")),
                            str(manifest["git_sha"]), digest, application_digest)
                hil_targets = [target.model_dump() for target in _parse_release_hil_targets(identity, marker["targets"])]
                if marker.get("target_mac"):
                    raise RuntimeError("HIL marker cannot mix ordered and legacy target scopes.")
                if marker.get("application_sha256") != application_digest:
                    raise RuntimeError("Ordered HIL marker must bind the application digest.")
            else:
                hil_target_mac = str(marker.get("target_mac") or "").strip().lower()
            if not hil_target_mac and not hil_targets:
                raise RuntimeError(f"Firmware release {release_id} has an invalid HIL quarantine marker.")
            if str(marker.get("git_sha") or "") != str(manifest["git_sha"]):
                raise RuntimeError(f"Firmware release {release_id} HIL marker has a different source SHA.")
            if str(marker.get("image_sha256") or "") != digest:
                raise RuntimeError(f"Firmware release {release_id} HIL marker has a different image hash.")
            desired_state = "HIL_ONLY"
        if desired_state == "AVAILABLE":
            if manifest.get("release_channel") == "EXPERIMENTAL_HIL_ONLY":
                raise ValueError("Experimental journal bridge requires an exact HIL quarantine marker.")
            require_production_qualification(manifest)
        stored_manifest = {
            **manifest,
            "_publication_mode": desired_state,
            "_hil_target_mac": hil_target_mac,
            "_hil_targets": hil_targets,
        }
        existing = session.scalar(select(FirmwareRelease).where(
            FirmwareRelease.release_id == release_id))
        if existing is not None:
            immutable = {
                "version": existing.version, "git_sha": existing.git_sha,
                "image_sha256": existing.image_sha256, "image_size": existing.image_size,
                "signing_key_id": existing.signing_key_id, "partition_layout": existing.partition_layout,
                "minimum_bootstrap_version": existing.minimum_bootstrap_version,
            }
            if any(manifest.get(key, "2.2.0" if key == "minimum_bootstrap_version" else None) != value
                   for key, value in immutable.items()):
                raise RuntimeError(f"Firmware release {release_id} changed immutable identity.")
            previous_signed = {key: value for key, value in (existing.manifest or {}).items()
                               if not key.startswith("_")}
            if previous_signed != manifest:
                raise RuntimeError(f"Firmware release {release_id} changed its signed manifest.")
            if existing.state != "REVOKED":
                existing.state = desired_state
                existing.manifest = stored_manifest
            continue
        session.add(FirmwareRelease(
            release_id=release_id, version=str(manifest["version"]), git_sha=str(manifest["git_sha"]),
            image_sha256=digest, image_size=image.stat().st_size, signing_key_id=str(manifest["signing_key_id"]),
            partition_layout=str(manifest["partition_layout"]),
            minimum_bootstrap_version=str(manifest.get("minimum_bootstrap_version", "2.2.0")),
            storage_name=f"{manifest_path.parent.name}/{image_name}", manifest=stored_manifest,
            manifest_signature=signature, state=desired_state))
    session.flush()


def create_campaign(
    session: Session,
    *,
    release_public_id: str,
    zone_id: str,
    reason: str,
    typed_confirmation: str,
    actor: str,
    scope_token: str,
    idempotency_key: str,
) -> FirmwareCampaign:
    replay = session.scalar(
        select(FirmwareCampaign).where(
            FirmwareCampaign.actor == actor,
            FirmwareCampaign.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        replay_release = session.get(FirmwareRelease, replay.release_id)
        if (
            replay.zone_id != zone_id
            or replay_release is None
            or replay_release.release_id != release_public_id
        ):
            raise ValueError("That idempotency key belongs to another firmware campaign.")
        return replay
    from zk_add.zkt_hil_schedule import lock_campaign_scope, schedule, reserve
    lock_campaign_scope(session, release_public_id)
    release, connectors, eligible = verify_campaign_scope_token(
        session,
        token=scope_token,
        release_public_id=release_public_id,
        zone_id=zone_id,
    )
    if typed_confirmation != release.version:
        raise ValueError("Typed firmware version does not match the release.")
    nationwide = schedule(session, release)
    if nationwide and nationwide["reservation"]:
        raise ValueError("Nationwide HIL scope already has an unsettled reservation.")
    if session.scalar(select(FirmwareCampaign).where(
        FirmwareCampaign.zone_id == zone_id, FirmwareCampaign.status.in_(["ACTIVE", "PAUSED"]))):
        raise ValueError("This zone already has an active or paused firmware campaign.")
    campaign = FirmwareCampaign(campaign_id=secrets.token_hex(16), release_id=release.id, zone_id=zone_id,
        actor=actor, idempotency_key=idempotency_key, reason=reason,
        typed_confirmation=typed_confirmation, eligible_count=len(eligible),
        legacy_skipped_count=len(connectors) - len(eligible))
    session.add(campaign)
    session.flush()
    for connector in eligible:
        deployment = FirmwareDeployment(deployment_id=secrets.token_hex(16), campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, previous_version=connector.firmware_version,
            target_version=release.version)
        session.add(deployment)
        session.flush()
        reserve(session, release, campaign, deployment, nationwide)
        if release.version == "2.6.22":
            from zk_add.zkt_factory_trial import reserve_trial
            reserve_trial(session, release, campaign, deployment, connector)
    return campaign


def _validated_firmware_public_base(public_base: str) -> str:
    value = public_base.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as error:
        raise RuntimeError("Firmware public base URL is invalid.") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "Firmware public base URL must be one credential-free HTTPS origin."
        )
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def assignment_for_connector(session: Session, *, connector: Connector, public_base: str) -> dict[str, Any] | None:
    from zk_add.zkt_ota_admission import pending_offer_hold, try_assignment_lock

    public_base = _validated_firmware_public_base(public_base)
    if not capability_is_eligible(connector):
        return None
    if not try_assignment_lock(session, connector):
        return None
    active = session.scalar(select(FirmwareDeployment).join(FirmwareCampaign).where(
        FirmwareCampaign.zone_id == connector.zone_id, FirmwareCampaign.status == "ACTIVE",
        FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES)).order_by(FirmwareDeployment.id))
    if active is not None and active.connector_id != connector.id:
        return None
    deployment = active
    pending_offer = False
    if deployment is None:
        deployment = session.scalar(select(FirmwareDeployment).join(FirmwareCampaign).where(
            FirmwareCampaign.zone_id == connector.zone_id, FirmwareCampaign.status == "ACTIVE",
            FirmwareDeployment.status == "PENDING").order_by(FirmwareDeployment.id))
        if deployment is None or deployment.connector_id != connector.id:
            return None
        pending_offer = True
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or release.state not in {"AVAILABLE", "HIL_ONLY"}:
        return None
    if release.version in RECOVERY_RELEASE_IDS and deployment.status not in RECOVERY_OFFERABLE_STATES:
        # A selected one-shot image is never re-offered after the device returns.
        return None
    try:
        require_family_match(connector.firmware_family, release.manifest or {})
        if release.state == "AVAILABLE":
            require_production_qualification(release.manifest or {})
    except ValueError:
        return None
    if not version_at_least(connector.firmware_version, release.minimum_bootstrap_version):
        return None
    if _storage_predecessor_exclusion(session, release, connector, deployment_id=deployment.id):
        return None
    try:
        bridge_target = _factory_3fl_bridge_target(release, connector.zone_id)
    except ValueError:
        return None
    if bridge_target and _factory_3fl_bridge_exclusion(connector, bridge_target):
        return None
    if release.state == "AVAILABLE" and not settings.firmware_ota_enabled:
        return None
    if release.state == "HIL_ONLY":
        try:
            permitted_targets = _permitted_hil_targets(session, release)
        except ValueError:
            return None
        if permitted_targets:
            if not any(target_matches(target, connector) for target in permitted_targets):
                return None
        else:
            target = str((release.manifest or {}).get("_hil_target_mac") or "").lower()
            configured = configured_hil_mac(release)
            if not settings.firmware_hil_enabled or not target or target != configured:
                return None
            if connector.hardware_id.lower() != target:
                return None
    application_digest = _application_sha256(release)
    if application_digest is None:
        return None
    # A heartbeat version string is not boot, digest, or reconciliation evidence.
    # Only the checked progress transitions may complete this deployment.
    if release.version == "2.6.22":
        from zk_add.zkt_factory_trial import _trial
        try:
            _trial(session, connector, deployment.deployment_id, require_new_boot=False)
        except ValueError:
            return None
    if pending_offer:
        hold = pending_offer_hold(session, connector)
        if hold:
            deployment.error_code = hold
            deployment.error_message = "Waiting for the nationwide ZKT upgrade slot and exact target checks."
            return None
        details = {}
        if release.version == "2.7.0":
            from zk_add.zkt_reader_evidence import current_reader_admission
            try:
                details["reader_admission"] = current_reader_admission(release, connector)
            except ValueError:
                return None
        deployment.status = "OFFERED"
        deployment.error_code = deployment.error_message = None
        deployment.offered_at = utc_now()
        deployment.attempt_count += 1
        session.add(FirmwareEvent(deployment_id=deployment.id, state="OFFERED", details=details))
    elif release.version == "2.7.0":
        from zk_add.zkt_reader_evidence import admitted_reader, current_reader_admission
        try:
            if admitted_reader(session, deployment, release) != current_reader_admission(release, connector):
                return None
        except ValueError:
            return None
    token = secrets.token_urlsafe(32)
    session.add(FirmwareDownloadGrant(token_hash=hashlib.sha256(token.encode()).hexdigest(),
        deployment_id=deployment.id, connector_id=connector.id,
        expires_at=utc_now() + timedelta(seconds=settings.firmware_download_grant_seconds)))
    return {"deployment_id": deployment.deployment_id, "release_id": release.release_id,
        "version": release.version, "image_sha256": application_digest,
        "artifact_sha256": release.image_sha256, "image_size": release.image_size,
        "partition_layout": release.partition_layout,
        "firmware_family": release_family(release.manifest or {}),
        "download_url": f"{public_base}/device/v2/firmware/download/{token}"}


def record_progress(
    session: Session,
    *,
    connector: Connector,
    deployment_public_id: str,
    state: str,
    bytes_written: int,
    running_version: str | None = None,
    running_partition: str | None = None,
    image_sha256: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> FirmwareDeployment:
    if state not in ACTIVE_DEPLOYMENT_STATES | TERMINAL_DEPLOYMENT_STATES:
        raise ValueError("Unknown firmware deployment state.")
    deployment = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.deployment_id == deployment_public_id,
        FirmwareDeployment.connector_id == connector.id).with_for_update().execution_options(populate_existing=True))
    if deployment is None:
        raise ValueError("Unknown firmware deployment.")
    failed_boot_return = (state == "ROLLED_BACK" and deployment.target_version == "2.7.0"
        and error_code in {"BOOT_HEALTH_TIMEOUT", "PREVIOUS_FIRMWARE_OBSERVED"}
        and (deployment.status in {"READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"}
        or (deployment.status == "FAILED" and deployment.error_code == "BOOT_HEALTH_TIMEOUT")))
    if deployment.status in TERMINAL_DEPLOYMENT_STATES and not failed_boot_return:
        return deployment
    allowed = DEPLOYMENT_TRANSITIONS.get(deployment.status, set())
    if state not in allowed and not failed_boot_return:
        raise ValueError(f"Illegal firmware transition {deployment.status} -> {state}.")
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None:
        raise ValueError("Firmware release is unavailable.")
    recovery = None
    if release.version == "2.6.22" and state in {"BOOTED_PENDING", "RECONCILING", "SUCCEEDED"}:
        from zk_add.zkt_factory_trial import revoked_evidence
        revoked_evidence(session, connector, deployment, release, current_boot=True)
    if failed_boot_return:
        from zk_add.zkt_rollback_receipt import verify_failed_boot_return

        recovery = verify_failed_boot_return(session, connector, deployment, release,
            bytes_written=bytes_written, running_version=running_version,
            running_partition=running_partition, image_sha256=image_sha256)
        recovery["reported_reason"] = error_code
    if bytes_written < deployment.bytes_written or bytes_written > release.image_size:
        raise ValueError("Firmware byte progress is outside the signed artifact bounds.")
    if state in {"READY_TO_BOOT", "BOOTED_PENDING"} and bytes_written != release.image_size:
        raise ValueError("Boot progress requires the complete signed firmware image.")
    if state in {"BOOTED_PENDING", "RECONCILING", "SUCCEEDED"}:
        if not _versions_match(running_version, deployment.target_version):
            raise ValueError("Reported running firmware does not match the deployment target.")
        if running_partition not in {"ota_0", "ota_1"}:
            raise ValueError("Reported running partition is not an OTA application slot.")
        expected_digest = _application_sha256(release)
        if not expected_digest or image_sha256 != expected_digest:
            raise ValueError("Reported running image digest does not match the signed release.")
    deployment.status = state
    deployment.bytes_written = max(deployment.bytes_written, bytes_written)
    deployment.error_code = error_code
    deployment.error_message = error_message
    deployment.updated_at = utc_now()
    session.add(FirmwareEvent(deployment_id=deployment.id, state=state,
                              details={
                                  "bytes_written": deployment.bytes_written,
                                  "error_code": error_code,
                                  "running_version": running_version,
                                  "running_partition": running_partition,
                                  "image_sha256": image_sha256,
                                  **({"recovery": recovery} if recovery else {}),
                              }))
    if state in TERMINAL_DEPLOYMENT_STATES:
        deployment.completed_at = utc_now()
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    if state in {"FAILED", "ROLLED_BACK"} and campaign is not None:
        campaign.status = "PAUSED"
        campaign.pause_reason = f"{connector.connector_id}: {state} ({error_code or 'UNKNOWN'})"
        campaign.updated_at = utc_now()
    connector.ota_state = ("OTA_READY" if state == "SUCCEEDED" else
                           "ROLLBACK_REQUIRED" if state == "ROLLED_BACK" else
                           "UPDATING" if state in ACTIVE_DEPLOYMENT_STATES else connector.ota_state)
    return deployment


def progress_receipt(session: Session, deployment: FirmwareDeployment, *, requested_state: str) -> dict:
    """Describe transition outcome; the caller commits before returning it to the peer.

    Artifact fields come from the release, never from the device's request. A
    terminal failure returned for a later progress POST is not acceptance of
    the requested transition merely because the HTTP request succeeded.
    """
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None:
        raise ValueError("Firmware release is unavailable.")
    receipt = {
        "schema_version": 1,
        "deployment_id": deployment.deployment_id,
        "state": deployment.status,
        "target_version": deployment.target_version,
        "application_sha256": _application_sha256(release),
        "confirm": requested_state == "BOOTED_PENDING" and deployment.status == "BOOTED_PENDING",
    }
    if deployment.status == "ROLLED_BACK" and deployment.target_version == "2.7.0":
        event = session.scalar(select(FirmwareEvent).where(
            FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "ROLLED_BACK")
            .order_by(FirmwareEvent.id.desc()).limit(1))
        details = (event.details or {}) if event else {}
        if (details.get("recovery") or {}).get("schema_version") == 1:
            receipt["rollback_application_sha256"] = details.get("image_sha256")
            if (details["recovery"].get("reader_admission") or {}).get("schema_version") == 1:
                receipt["reader_admission"] = details["recovery"]["reader_admission"]
    return receipt


def previous_firmware_return_evidence(
    session: Session, *, connector: Connector,
    deployment: FirmwareDeployment, payload: Any,
) -> dict[str, Any] | None:
    """Recognize a return without inventing a bootloader or reset cause.

    Version strings alone, cached connector fields, and one delayed heartbeat
    cannot terminate an attempt. Require a digest-checked target boot followed
    by two coherent observations of a newer boot on the previous firmware.
    """
    ota = payload.ota
    if (deployment.status not in {"BOOTED_PENDING", "RECONCILING"}
            or not deployment.previous_version or not connector.boot_id
            or not _versions_match(payload.firmware_version, deployment.previous_version)
            or _versions_match(payload.firmware_version, deployment.target_version)
            or (ota.running_version and not _versions_match(ota.running_version, payload.firmware_version))
            or ota.state.upper() == "UPDATING"
            or (ota.target_version and not _versions_match(ota.target_version, deployment.target_version))
            or payload.uptime_seconds is None or payload.uptime_seconds < 0):
        return None
    release = session.get(FirmwareRelease, deployment.release_id)
    target_boot = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["BOOTED_PENDING", "RECONCILING"]),
    ).order_by(FirmwareEvent.id.desc()).limit(1))
    if release is None or target_boot is None:
        return None
    details = target_boot.details or {}
    if (not _application_sha256(release)
            or details.get("image_sha256") != _application_sha256(release)
            or not _versions_match(details.get("running_version"), deployment.target_version)
            or details.get("running_partition") not in {"ota_0", "ota_1"}):
        return None
    now = utc_now()
    boot_at = now - timedelta(seconds=payload.uptime_seconds)
    # Permit network scheduling skew, but never infer a rollback from an
    # observation belonging to the pre-install boot.
    if boot_at <= ensure_utc(target_boot.created_at) + timedelta(seconds=10):
        return None
    previous = session.scalar(select(DeviceTelemetry).where(
        DeviceTelemetry.connector_id == connector.id,
    ).order_by(DeviceTelemetry.id.desc()).limit(1))
    if (previous is None or previous.boot_id != connector.boot_id
            or previous.sequence >= connector.last_sequence
            or previous.uptime_seconds is None
            or previous.uptime_seconds < 0
            or ensure_utc(previous.created_at) <= ensure_utc(target_boot.created_at)):
        return None
    age = (now - ensure_utc(previous.created_at)).total_seconds()
    uptime_delta = payload.uptime_seconds - previous.uptime_seconds
    old = previous.payload or {}
    old_ota = old.get("ota") or {}
    if (not 15 <= age <= 90 or uptime_delta <= 0 or abs(uptime_delta - age) > 10
            or not _versions_match(old.get("firmware_version"), deployment.previous_version)
            or old_ota.get("state", "").upper() == "UPDATING"
            or (old_ota.get("target_version") and not _versions_match(old_ota["target_version"], deployment.target_version))):
        return None
    if (ota.image_sha256 and old_ota.get("image_sha256")
            and ota.image_sha256 != old_ota["image_sha256"]):
        return None
    return {
        "source": "authenticated_heartbeat_return", "reset_cause": "not_reported",
        "deployment_id": deployment.deployment_id,
        "target_version": deployment.target_version,
        "previous_version": deployment.previous_version,
        "target_boot_event_id": target_boot.id,
        "prior_telemetry_id": previous.id, "boot_id": connector.boot_id,
        "sequence": connector.last_sequence, "uptime_seconds": payload.uptime_seconds,
        "observed_at": now.isoformat(),
    }


def _serialize_release(row: FirmwareRelease, session: Session) -> dict[str, Any]:
    from zk_add.zkt_hil_schedule import schedule
    nationwide = None
    next_target = None
    allowed_targets = None
    scope_message = None
    if row.state == "HIL_ONLY" and (row.manifest or {}).get("_hil_targets") is not None:
        try:
            nationwide = schedule(session, row)
            permitted = _permitted_hil_targets(session, row)
            allowed_targets = [target.model_dump() for target in permitted or []]
            next_target = allowed_targets[0] if allowed_targets else None
        except ValueError as exc:
            scope_message = str(exc)
    return {
        "release_id": row.release_id,
        "version": row.version,
        "firmware_family": release_family(row.manifest or {}),
        "display_name": f"{'HIK' if release_family(row.manifest or {}) == 'hikvision' else 'ZKT'} Zone Lite {row.version}",
        "git_sha": row.git_sha,
        "image_sha256": row.image_sha256,
        "image_size": row.image_size,
        "state": row.state,
        "application_sha256": _application_sha256(row),
        "partition_layout": row.partition_layout,
        "signing_key_id": row.signing_key_id,
        "published_at": row.published_at,
        "revoked_at": row.revoked_at,
        "revoked_by": row.revoked_by,
        "hil_target_mac": (row.manifest or {}).get("_hil_target_mac"),
        "hil_targets": (row.manifest or {}).get("_hil_targets"),
        "hil_next_target": next_target,
        "hil_allowed_targets": allowed_targets,
        "hil_scope_message": scope_message,
        "hil_schedule": nationwide,
    }


def release_page(
    session: Session,
    *,
    query: str | None = None,
    state: str | None = None,
    cursor: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a stable newest-first release page without changing the legacy row shape."""

    sync_release_store(session)
    clauses = []
    if query and query.strip():
        term = f"%{query.strip()}%"
        clauses.append(
            or_(
                FirmwareRelease.version.ilike(term),
                FirmwareRelease.release_id.ilike(term),
                FirmwareRelease.git_sha.ilike(term),
                FirmwareRelease.image_sha256.ilike(term),
                FirmwareRelease.signing_key_id.ilike(term),
            )
        )
    if state:
        clauses.append(FirmwareRelease.state == state.upper())

    filtered_total = session.scalar(
        select(func.count(FirmwareRelease.id)).where(*clauses)
    ) or 0
    statement = select(FirmwareRelease).where(*clauses)
    if cursor is not None:
        statement = statement.where(FirmwareRelease.id < cursor)
    statement = statement.order_by(FirmwareRelease.id.desc())
    if limit is not None:
        rows = list(session.scalars(statement.limit(limit + 1)).all())
        page = rows[:limit]
        next_cursor = page[-1].id if len(rows) > limit and page else None
    else:
        page = list(session.scalars(statement).all())
        next_cursor = None

    totals = {"all": 0, "available": 0, "hil_only": 0, "revoked": 0}
    for release_state, count in session.execute(
        select(FirmwareRelease.state, func.count(FirmwareRelease.id)).group_by(
            FirmwareRelease.state
        )
    ):
        normalized = str(release_state).lower()
        totals[normalized] = int(count)
        totals["all"] += int(count)
    return {
        "rows": [_serialize_release(row, session) for row in page],
        "next_cursor": next_cursor,
        "filtered_total": int(filtered_total),
        "totals": totals,
    }


def release_rows(session: Session) -> list[dict[str, Any]]:
    """Compatibility helper for callers that still require the complete release list."""

    return release_page(session)["rows"]


def _transport_diagnostics(
    session: Session,
    deployment: FirmwareDeployment,
) -> dict[str, Any]:
    """Return bounded, credential-free evidence for one OTA transfer attempt."""

    (
        grants_issued,
        grants_reached,
        first_grant_issued_at,
        latest_grant_issued_at,
        latest_grant_expires_at,
        last_endpoint_reached_at,
    ) = session.execute(
        select(
            func.count(FirmwareDownloadGrant.id),
            func.count(FirmwareDownloadGrant.last_used_at),
            func.min(FirmwareDownloadGrant.created_at),
            func.max(FirmwareDownloadGrant.created_at),
            func.max(FirmwareDownloadGrant.expires_at),
            func.max(FirmwareDownloadGrant.last_used_at),
        ).where(FirmwareDownloadGrant.deployment_id == deployment.id)
    ).one()

    latest_telemetry = session.scalar(
        select(DeviceTelemetry)
        .where(DeviceTelemetry.connector_id == deployment.connector_id)
        .order_by(DeviceTelemetry.id.desc())
        .limit(1)
    )
    window_started_at = deployment.offered_at or deployment.created_at
    window_ended_at = deployment.completed_at or utc_now()
    telemetry_samples, minimum_free_heap, weakest_rssi = session.execute(
        select(
            func.count(DeviceTelemetry.id),
            func.min(DeviceTelemetry.free_heap),
            func.min(DeviceTelemetry.rssi),
        ).where(
            DeviceTelemetry.connector_id == deployment.connector_id,
            DeviceTelemetry.created_at >= window_started_at,
            DeviceTelemetry.created_at <= window_ended_at,
        )
    ).one()

    return {
        "download_grants": {
            "issued_count": int(grants_issued or 0),
            "reached_count": int(grants_reached or 0),
            "endpoint_reached": bool(grants_reached),
            "first_issued_at": first_grant_issued_at,
            "latest_issued_at": latest_grant_issued_at,
            "latest_expires_at": latest_grant_expires_at,
            "last_reached_at": last_endpoint_reached_at,
        },
        "telemetry": {
            "window_started_at": window_started_at,
            "window_ended_at": window_ended_at,
            "sample_count": int(telemetry_samples or 0),
            "minimum_free_heap": minimum_free_heap,
            "weakest_rssi": weakest_rssi,
            "latest": (
                {
                    "free_heap": latest_telemetry.free_heap,
                    "rssi": latest_telemetry.rssi,
                    "uptime_seconds": latest_telemetry.uptime_seconds,
                    "outbox_depth": latest_telemetry.outbox_depth,
                    "current_activity": latest_telemetry.current_activity,
                    "created_at": latest_telemetry.created_at,
                }
                if latest_telemetry is not None
                else None
            ),
        },
    }


def _deployment_rows(
    session: Session,
    campaign: FirmwareCampaign,
    *,
    include_events: bool,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    deployments = list(
        session.scalars(
            select(FirmwareDeployment)
            .where(FirmwareDeployment.campaign_id == campaign.id)
            .order_by(FirmwareDeployment.id.asc())
        ).all()
    )
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for deployment in deployments:
        counts[deployment.status] = counts.get(deployment.status, 0) + 1
        connector = session.get(Connector, deployment.connector_id)
        events = []
        if include_events:
            events = list(
                session.scalars(
                    select(FirmwareEvent)
                    .where(FirmwareEvent.deployment_id == deployment.id)
                    .order_by(FirmwareEvent.id.desc())
                    .limit(20)
                ).all()
            )
        rows.append(
            {
                "deployment_id": deployment.deployment_id,
                "connector_id": connector.connector_id if connector else None,
                "display_name": connector.display_name if connector else None,
                "hardware_id": connector.hardware_id if connector else None,
                "zone_id": connector.zone_id if connector else None,
                "status": deployment.status,
                "previous_version": deployment.previous_version,
                "target_version": deployment.target_version,
                "bytes_written": deployment.bytes_written,
                "attempt_count": deployment.attempt_count,
                "error_code": deployment.error_code,
                "error_message": deployment.error_message,
                "offered_at": deployment.offered_at,
                "completed_at": deployment.completed_at,
                "updated_at": deployment.updated_at,
                "transport_diagnostics": (
                    _transport_diagnostics(session, deployment)
                    if include_events and deployment.offered_at is not None
                    else None
                ),
                "events": [
                    {
                        "state": event.state,
                        "details": event.details or {},
                        "created_at": event.created_at,
                    }
                    for event in events
                ],
            }
        )
    return counts, rows


def _serialize_campaign(
    session: Session,
    campaign: FirmwareCampaign,
    *,
    include_deployments: bool,
    include_events: bool,
) -> dict[str, Any]:
    counts, deployments = _deployment_rows(
        session, campaign, include_events=include_events
    )
    release = session.get(FirmwareRelease, campaign.release_id)
    zone_name = session.scalar(
        select(Connector.zone_name)
        .where(Connector.zone_id == campaign.zone_id)
        .order_by(Connector.id.asc())
        .limit(1)
    )
    return {
        "campaign_id": campaign.campaign_id,
        "release_id": release.release_id if release else None,
        "release_state": release.state if release else None,
        "zone_id": campaign.zone_id,
        "zone_name": zone_name,
        "version": release.version if release else None,
        "status": campaign.status,
        "eligible": campaign.eligible_count,
        "legacy_skipped": campaign.legacy_skipped_count,
        "counts": counts,
        "pause_reason": campaign.pause_reason,
        "actor": campaign.actor,
        "reason": campaign.reason,
        "deployments": deployments if include_deployments else [],
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
    }


def campaign_page(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    zone_id: str | None = None,
    release_id: str | None = None,
    cursor: int | None = None,
    limit: int | None = None,
    include_deployments: bool = False,
) -> dict[str, Any]:
    """Return campaign summaries with exact national and filtered counts."""

    sync_release_store(session)
    clauses = []
    if query and query.strip():
        term = f"%{query.strip()}%"
        clauses.append(
            or_(
                FirmwareCampaign.campaign_id.ilike(term),
                FirmwareCampaign.zone_id.ilike(term),
                FirmwareCampaign.actor.ilike(term),
                FirmwareRelease.release_id.ilike(term),
                FirmwareRelease.version.ilike(term),
            )
        )
    if status:
        clauses.append(FirmwareCampaign.status == status.upper())
    if zone_id:
        clauses.append(FirmwareCampaign.zone_id == zone_id)
    if release_id:
        clauses.append(FirmwareRelease.release_id == release_id)

    filtered_total = session.scalar(
        select(func.count(FirmwareCampaign.id))
        .join(FirmwareRelease, FirmwareCampaign.release_id == FirmwareRelease.id)
        .where(*clauses)
    ) or 0
    statement = (
        select(FirmwareCampaign)
        .join(FirmwareRelease, FirmwareCampaign.release_id == FirmwareRelease.id)
        .where(*clauses)
    )
    if cursor is not None:
        statement = statement.where(FirmwareCampaign.id < cursor)
    statement = statement.order_by(FirmwareCampaign.id.desc())
    if limit is not None:
        fetched = list(session.scalars(statement.limit(limit + 1)).all())
        page = fetched[:limit]
        next_cursor = page[-1].id if len(fetched) > limit and page else None
    else:
        page = list(session.scalars(statement).all())
        next_cursor = None

    campaign_totals: dict[str, int] = {"all": 0}
    for campaign_state, count in session.execute(
        select(FirmwareCampaign.status, func.count(FirmwareCampaign.id)).group_by(
            FirmwareCampaign.status
        )
    ):
        campaign_totals[str(campaign_state).lower()] = int(count)
        campaign_totals["all"] += int(count)
    deployment_totals: dict[str, int] = {"all": 0}
    for deployment_state, count in session.execute(
        select(FirmwareDeployment.status, func.count(FirmwareDeployment.id)).group_by(
            FirmwareDeployment.status
        )
    ):
        deployment_totals[str(deployment_state).lower()] = int(count)
        deployment_totals["all"] += int(count)

    return {
        "rows": [
            _serialize_campaign(
                session,
                row,
                include_deployments=include_deployments,
                include_events=include_deployments,
            )
            for row in page
        ],
        "next_cursor": next_cursor,
        "filtered_total": int(filtered_total),
        "totals": {
            "campaigns": campaign_totals,
            "deployments": deployment_totals,
        },
    }


def campaign_detail(session: Session, campaign_id: str) -> dict[str, Any] | None:
    sync_release_store(session)
    campaign = session.scalar(
        select(FirmwareCampaign).where(FirmwareCampaign.campaign_id == campaign_id)
    )
    if campaign is None:
        return None
    return _serialize_campaign(
        session, campaign, include_deployments=True, include_events=True
    )


def campaign_rows(session: Session) -> list[dict[str, Any]]:
    """Compatibility helper retaining the original detailed list response."""

    return campaign_page(session, include_deployments=True)["rows"]


def resolve_download(session: Session, token: str) -> tuple[FirmwareRelease, Path]:
    grant = session.scalar(select(FirmwareDownloadGrant).where(
        FirmwareDownloadGrant.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        FirmwareDownloadGrant.expires_at > utc_now()))
    if grant is None:
        raise ValueError("Firmware download grant is invalid or expired.")
    deployment = session.get(FirmwareDeployment, grant.deployment_id)
    release = session.get(FirmwareRelease, deployment.release_id) if deployment else None
    if release is None or release.state not in {"AVAILABLE", "HIL_ONLY"}:
        raise ValueError("Firmware release is unavailable.")
    if release.version in RECOVERY_RELEASE_IDS and deployment.status not in RECOVERY_OFFERABLE_STATES:
        raise ValueError("A selected one-shot storage recovery image is never downloaded again.")
    if release.state == "HIL_ONLY":
        campaign = session.get(FirmwareCampaign, deployment.campaign_id)
        if campaign is None or campaign.status != "ACTIVE" or deployment.status not in ACTIVE_DEPLOYMENT_STATES:
            raise ValueError("HIL firmware campaign is not active.")
        target = str((release.manifest or {}).get("_hil_target_mac") or "").lower()
        configured = configured_hil_mac(release)
        connector = session.get(Connector, grant.connector_id)
        permitted_targets = _permitted_hil_targets(session, release)
        if permitted_targets:
            if connector is None or not any(target_matches(item, connector) for item in permitted_targets):
                raise ValueError("HIL firmware grant exact target mismatch.")
        else:
            if not settings.firmware_hil_enabled or not target or target != configured:
                raise ValueError("HIL firmware release is unavailable.")
            if connector is None or connector.hardware_id.lower() != target:
                raise ValueError("HIL firmware grant target mismatch.")
    connector = session.get(Connector, grant.connector_id)
    if connector is None or _storage_predecessor_exclusion(session, release, connector, deployment_id=deployment.id):
        raise ValueError("Firmware storage predecessor is no longer eligible.")
    if release.version == "2.7.0":
        from zk_add.zkt_reader_evidence import admitted_reader, current_reader_admission
        if admitted_reader(session, deployment, release) != current_reader_admission(release, connector):
            raise ValueError("Firmware grant retained-reader identity changed.")
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    bridge_target = _factory_3fl_bridge_target(release, campaign.zone_id if campaign else connector.zone_id)
    if bridge_target and (campaign is None or campaign.status != "ACTIVE"
                          or connector.zone_id != campaign.zone_id
                          or _factory_3fl_bridge_exclusion(connector, bridge_target)):
        raise ValueError("Factory-to-OTA bridge exact target or predecessor changed.")
    if release.version == "2.6.22":
        from zk_add.zkt_factory_trial import _trial
        _trial(session, connector, deployment.deployment_id, require_new_boot=False)
    root = Path(settings.firmware_store_path).resolve()
    image = (root / release.storage_name).resolve()
    if root not in image.parents or not image.is_file():
        raise ValueError("Firmware image is unavailable.")
    grant.last_used_at = utc_now()
    return release, image
