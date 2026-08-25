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
from zk_add.models import Connector, DeviceTelemetry, utc_column
from zk_add.settings import settings
from zk_add.time_utils import utc_now

OTA_LAYOUT = "zone-lite-ota-v1"
HIL_MARKER = ".hil-only.json"
ACTIVE_DEPLOYMENT_STATES = {
    "OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"
}
TERMINAL_DEPLOYMENT_STATES = {
    "SUCCEEDED", "FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"
}
DEPLOYMENT_TRANSITIONS = {
    "PENDING": {"OFFERED", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"},
    "OFFERED": {"OFFERED", "DOWNLOADING", "FAILED", "CANCELLED", "RELEASE_REVOKED"},
    "DOWNLOADING": {"DOWNLOADING", "VERIFYING", "FAILED", "CANCELLED", "RELEASE_REVOKED"},
    "VERIFYING": {"VERIFYING", "READY_TO_BOOT", "FAILED", "CANCELLED", "RELEASE_REVOKED"},
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


def _scope_digest(release: FirmwareRelease, zone_id: str, connectors: list[Connector]) -> str:
    payload = {
        "release_id": release.release_id,
        "release_state": release.state,
        "version": release.version,
        "zone_id": zone_id,
        "connectors": [
            {
                "connector_id": row.connector_id,
                "active": row.active,
                "hardware_id": row.hardware_id.lower(),
                "ota_capable": row.ota_capable,
                "ota_secure_boot": row.ota_secure_boot,
                "ota_rollback_enabled": row.ota_rollback_enabled,
                "ota_partition_layout": row.ota_partition_layout,
            }
            for row in sorted(connectors, key=lambda item: item.connector_id)
        ],
    }
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
    if release.state == "HIL_ONLY":
        configured_target = (settings.firmware_hil_target_mac or "").strip().lower()
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
            hil_target_mac=hil_target_mac if release.state == "HIL_ONLY" else "",
            minimum_version=release.minimum_bootstrap_version,
        )
        if reason:
            excluded.append((connector, reason))
        else:
            eligible.append(connector)
    if release.state == "HIL_ONLY":
        # Keep the quarantine boundary explicit even though the exclusion
        # classifier above already rejects every non-target connector.
        eligible = [row for row in eligible if row.hardware_id.lower() == hil_target_mac]
        if len(eligible) != 1:
            raise ValueError("HIL campaign requires exactly one eligible connector with the target MAC.")
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
    digest = _scope_digest(release, zone_id, connectors)
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
    if payload.get("scope_digest") != _scope_digest(release, zone_id, connectors):
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
        image_name = os.path.basename(str(manifest["image_name"]))
        image = manifest_path.parent / image_name
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, str(manifest["image_sha256"])) or image.stat().st_size != int(manifest["image_size"]):
            raise RuntimeError(f"Firmware release {release_id} failed immutable artifact verification.")
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
        desired_state = "AVAILABLE"
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            hil_target_mac = str(marker.get("target_mac") or "").strip().lower()
            if not hil_target_mac:
                raise RuntimeError(f"Firmware release {release_id} has an invalid HIL quarantine marker.")
            if str(marker.get("git_sha") or "") != str(manifest["git_sha"]):
                raise RuntimeError(f"Firmware release {release_id} HIL marker has a different source SHA.")
            if str(marker.get("image_sha256") or "") != digest:
                raise RuntimeError(f"Firmware release {release_id} HIL marker has a different image hash.")
            desired_state = "HIL_ONLY"
        stored_manifest = {
            **manifest,
            "_publication_mode": desired_state,
            "_hil_target_mac": hil_target_mac,
        }
        existing = session.scalar(select(FirmwareRelease).where(
            FirmwareRelease.release_id == release_id))
        if existing is not None:
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
    release, connectors, eligible = verify_campaign_scope_token(
        session,
        token=scope_token,
        release_public_id=release_public_id,
        zone_id=zone_id,
    )
    if typed_confirmation != release.version:
        raise ValueError("Typed firmware version does not match the release.")
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
        session.add(FirmwareDeployment(deployment_id=secrets.token_hex(16), campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, previous_version=connector.firmware_version,
            target_version=release.version))
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
    public_base = _validated_firmware_public_base(public_base)
    if not capability_is_eligible(connector):
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
    if not version_at_least(connector.firmware_version, release.minimum_bootstrap_version):
        return None
    if release.state == "AVAILABLE" and not settings.firmware_ota_enabled:
        return None
    if release.state == "HIL_ONLY":
        target = str((release.manifest or {}).get("_hil_target_mac") or "").lower()
        configured = (settings.firmware_hil_target_mac or "").strip().lower()
        if not settings.firmware_hil_enabled or not target or target != configured:
            return None
        if connector.hardware_id.lower() != target:
            return None
    application_digest = _application_sha256(release)
    if application_digest is None:
        return None
    if (
        deployment.status in ACTIVE_DEPLOYMENT_STATES
        and _versions_match(connector.firmware_version, deployment.target_version)
    ):
        deployment.status = "SUCCEEDED"
        deployment.completed_at = utc_now()
        deployment.updated_at = utc_now()
        connector.ota_state = "OTA_READY"
        session.add(FirmwareEvent(
            deployment_id=deployment.id,
            state="SUCCEEDED",
            details={"recovered_from_running_target_version": True},
        ))
        return None
    if pending_offer:
        deployment.status = "OFFERED"
        deployment.offered_at = utc_now()
        deployment.attempt_count += 1
        session.add(FirmwareEvent(deployment_id=deployment.id, state="OFFERED", details={}))
    token = secrets.token_urlsafe(32)
    session.add(FirmwareDownloadGrant(token_hash=hashlib.sha256(token.encode()).hexdigest(),
        deployment_id=deployment.id, connector_id=connector.id,
        expires_at=utc_now() + timedelta(seconds=settings.firmware_download_grant_seconds)))
    return {"deployment_id": deployment.deployment_id, "release_id": release.release_id,
        "version": release.version, "image_sha256": application_digest,
        "artifact_sha256": release.image_sha256, "image_size": release.image_size,
        "partition_layout": release.partition_layout,
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
        FirmwareDeployment.connector_id == connector.id))
    if deployment is None:
        raise ValueError("Unknown firmware deployment.")
    if deployment.status in TERMINAL_DEPLOYMENT_STATES:
        return deployment
    allowed = DEPLOYMENT_TRANSITIONS.get(deployment.status, set())
    if state not in allowed:
        raise ValueError(f"Illegal firmware transition {deployment.status} -> {state}.")
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None:
        raise ValueError("Firmware release is unavailable.")
    if bytes_written < deployment.bytes_written or bytes_written > release.image_size:
        raise ValueError("Firmware byte progress is outside the signed artifact bounds.")
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


def _serialize_release(row: FirmwareRelease) -> dict[str, Any]:
    return {
        "release_id": row.release_id,
        "version": row.version,
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
        "rows": [_serialize_release(row) for row in page],
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
    if release.state == "HIL_ONLY":
        target = str((release.manifest or {}).get("_hil_target_mac") or "").lower()
        configured = (settings.firmware_hil_target_mac or "").strip().lower()
        connector = session.get(Connector, grant.connector_id)
        if not settings.firmware_hil_enabled or not target or target != configured:
            raise ValueError("HIL firmware release is unavailable.")
        if connector is None or connector.hardware_id.lower() != target:
            raise ValueError("HIL firmware grant target mismatch.")
    root = Path(settings.firmware_store_path).resolve()
    image = (root / release.storage_name).resolve()
    if root not in image.parents or not image.is_file():
        raise ValueError("Firmware image is unavailable.")
    grant.last_used_at = utc_now()
    return release, image
