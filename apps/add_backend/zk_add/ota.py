from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zk_add.db import Base
from zk_add.models import Connector, utc_column
from zk_add.settings import settings
from zk_add.time_utils import utc_now

OTA_LAYOUT = "zone-lite-ota-v1"
ACTIVE_DEPLOYMENT_STATES = {
    "OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"
}
TERMINAL_DEPLOYMENT_STATES = {
    "SUCCEEDED", "FAILED", "ROLLED_BACK", "CANCELLED", "SUPERSEDED", "RELEASE_REVOKED"
}


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
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("add_firmware_releases.id"), index=True)
    zone_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    actor: Mapped[str] = mapped_column(String(120))
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
    if not settings.firmware_ota_enabled:
        return
    root = Path(settings.firmware_store_path).resolve()
    if not root.is_dir():
        raise RuntimeError("Configured firmware store is unavailable.")
    for manifest_path in root.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        release_id = str(manifest.get("release_id", ""))
        if not release_id or session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id)):
            continue
        signature = manifest_path.with_name("manifest.sig").read_text(encoding="ascii").strip()
        _verify_manifest(manifest, signature)
        image_name = os.path.basename(str(manifest["image_name"]))
        image = manifest_path.parent / image_name
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, str(manifest["image_sha256"])) or image.stat().st_size != int(manifest["image_size"]):
            raise RuntimeError(f"Firmware release {release_id} failed immutable artifact verification.")
        if manifest.get("partition_layout") != OTA_LAYOUT:
            raise RuntimeError(f"Firmware release {release_id} has an unknown partition layout.")
        session.add(FirmwareRelease(
            release_id=release_id, version=str(manifest["version"]), git_sha=str(manifest["git_sha"]),
            image_sha256=digest, image_size=image.stat().st_size, signing_key_id=str(manifest["signing_key_id"]),
            partition_layout=str(manifest["partition_layout"]),
            minimum_bootstrap_version=str(manifest.get("minimum_bootstrap_version", "2.2.0")),
            storage_name=f"{manifest_path.parent.name}/{image_name}", manifest=manifest,
            manifest_signature=signature, state="AVAILABLE"))
    session.flush()


def create_campaign(session: Session, *, release_public_id: str, zone_id: str, reason: str,
                    typed_confirmation: str, actor: str) -> FirmwareCampaign:
    sync_release_store(session)
    release = session.scalar(select(FirmwareRelease).where(
        FirmwareRelease.release_id == release_public_id, FirmwareRelease.state == "AVAILABLE"))
    if release is None:
        raise ValueError("Firmware release is not available.")
    if typed_confirmation != release.version:
        raise ValueError("Typed firmware version does not match the release.")
    if session.scalar(select(FirmwareCampaign).where(
        FirmwareCampaign.zone_id == zone_id, FirmwareCampaign.status.in_(["ACTIVE", "PAUSED"]))):
        raise ValueError("This zone already has an active or paused firmware campaign.")
    connectors = list(session.scalars(select(Connector).where(
        Connector.zone_id == zone_id, Connector.active == True)).all())  # noqa: E712
    eligible = [row for row in connectors if capability_is_eligible(row)]
    campaign = FirmwareCampaign(campaign_id=secrets.token_hex(16), release_id=release.id, zone_id=zone_id,
        actor=actor, reason=reason, typed_confirmation=typed_confirmation, eligible_count=len(eligible),
        legacy_skipped_count=len(connectors) - len(eligible))
    session.add(campaign)
    session.flush()
    for connector in eligible:
        session.add(FirmwareDeployment(deployment_id=secrets.token_hex(16), campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, previous_version=connector.firmware_version,
            target_version=release.version))
    return campaign


def assignment_for_connector(session: Session, *, connector: Connector, public_base: str) -> dict[str, Any] | None:
    if not settings.firmware_ota_enabled or not capability_is_eligible(connector):
        return None
    active = session.scalar(select(FirmwareDeployment).join(FirmwareCampaign).where(
        FirmwareCampaign.zone_id == connector.zone_id, FirmwareCampaign.status == "ACTIVE",
        FirmwareDeployment.status.in_(ACTIVE_DEPLOYMENT_STATES)).order_by(FirmwareDeployment.id))
    if active is not None and active.connector_id != connector.id:
        return None
    deployment = active
    if deployment is None:
        deployment = session.scalar(select(FirmwareDeployment).join(FirmwareCampaign).where(
            FirmwareCampaign.zone_id == connector.zone_id, FirmwareCampaign.status == "ACTIVE",
            FirmwareDeployment.status == "PENDING").order_by(FirmwareDeployment.id))
        if deployment is None or deployment.connector_id != connector.id:
            return None
        deployment.status = "OFFERED"
        deployment.offered_at = utc_now()
        deployment.attempt_count += 1
        session.add(FirmwareEvent(deployment_id=deployment.id, state="OFFERED", details={}))
    release = session.get(FirmwareRelease, deployment.release_id)
    if release is None or release.state != "AVAILABLE":
        return None
    token = secrets.token_urlsafe(32)
    session.add(FirmwareDownloadGrant(token_hash=hashlib.sha256(token.encode()).hexdigest(),
        deployment_id=deployment.id, connector_id=connector.id,
        expires_at=utc_now() + timedelta(seconds=settings.firmware_download_grant_seconds)))
    return {"deployment_id": deployment.deployment_id, "release_id": release.release_id,
        "version": release.version, "image_sha256": release.image_sha256, "image_size": release.image_size,
        "partition_layout": release.partition_layout,
        "download_url": f"{public_base}/device/v2/firmware/download/{token}"}


def record_progress(session: Session, *, connector: Connector, deployment_public_id: str, state: str,
                    bytes_written: int, error_code: str | None, error_message: str | None) -> FirmwareDeployment:
    if state not in ACTIVE_DEPLOYMENT_STATES | TERMINAL_DEPLOYMENT_STATES:
        raise ValueError("Unknown firmware deployment state.")
    deployment = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.deployment_id == deployment_public_id,
        FirmwareDeployment.connector_id == connector.id))
    if deployment is None:
        raise ValueError("Unknown firmware deployment.")
    if deployment.status in TERMINAL_DEPLOYMENT_STATES:
        return deployment
    deployment.status = state
    deployment.bytes_written = max(deployment.bytes_written, bytes_written)
    deployment.error_code = error_code
    deployment.error_message = error_message
    deployment.updated_at = utc_now()
    session.add(FirmwareEvent(deployment_id=deployment.id, state=state,
                              details={"bytes_written": deployment.bytes_written, "error_code": error_code}))
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


def release_rows(session: Session) -> list[dict[str, Any]]:
    sync_release_store(session)
    return [{"release_id": row.release_id, "version": row.version, "git_sha": row.git_sha,
             "image_sha256": row.image_sha256, "image_size": row.image_size, "state": row.state,
             "partition_layout": row.partition_layout, "signing_key_id": row.signing_key_id,
             "published_at": row.published_at}
            for row in session.scalars(select(FirmwareRelease).order_by(FirmwareRelease.id.desc())).all()]


def campaign_rows(session: Session) -> list[dict[str, Any]]:
    result = []
    for campaign in session.scalars(select(FirmwareCampaign).order_by(FirmwareCampaign.id.desc())).all():
        deployments = list(session.scalars(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id == campaign.id)).all())
        counts: dict[str, int] = {}
        for deployment in deployments:
            counts[deployment.status] = counts.get(deployment.status, 0) + 1
        release = session.get(FirmwareRelease, campaign.release_id)
        result.append({"campaign_id": campaign.campaign_id, "zone_id": campaign.zone_id,
            "version": release.version if release else None, "status": campaign.status,
            "eligible": campaign.eligible_count, "legacy_skipped": campaign.legacy_skipped_count,
            "counts": counts, "pause_reason": campaign.pause_reason, "created_at": campaign.created_at})
    return result


def resolve_download(session: Session, token: str) -> tuple[FirmwareRelease, Path]:
    grant = session.scalar(select(FirmwareDownloadGrant).where(
        FirmwareDownloadGrant.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        FirmwareDownloadGrant.expires_at > utc_now()))
    if grant is None:
        raise ValueError("Firmware download grant is invalid or expired.")
    deployment = session.get(FirmwareDeployment, grant.deployment_id)
    release = session.get(FirmwareRelease, deployment.release_id) if deployment else None
    if release is None or release.state != "AVAILABLE":
        raise ValueError("Firmware release is unavailable.")
    root = Path(settings.firmware_store_path).resolve()
    image = (root / release.storage_name).resolve()
    if root not in image.parents or not image.is_file():
        raise ValueError("Firmware image is unavailable.")
    grant.last_used_at = utc_now()
    return release, image
