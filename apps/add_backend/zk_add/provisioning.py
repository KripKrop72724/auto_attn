from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
from zk_add.time_utils import utc_now

HARDWARE_PROFILE = "esp32s3-16mb-zone-lite-v1"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HEX_PSK_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")
MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
RFC1918_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SECRET_KEYS = frozenset(
    {
        "password",
        "password_wifi",
        "wifi_password",
        "comm_key",
        "communication_key",
        "fleet_root",
        "fleet_root_secret",
        "ords_password",
        "connector_token",
        "hmac_key",
        "raw_nvs",
        "bootstrap_secret",
        "private_key",
    }
)


class ProvisioningState(StrEnum):
    WAITING_FOR_COMPANION = "WAITING_FOR_COMPANION"
    WAITING_FOR_DEVICE = "WAITING_FOR_DEVICE"
    INSPECTING = "INSPECTING"
    CONFIGURING = "CONFIGURING"
    PREFLIGHT_READY = "PREFLIGHT_READY"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    PACKAGE_PREPARING = "PACKAGE_PREPARING"
    PACKAGE_READY = "PACKAGE_READY"
    EFUSE_BURNING = "EFUSE_BURNING"
    EFUSE_VERIFIED = "EFUSE_VERIFIED"
    FLASHING = "FLASHING"
    READBACK_VERIFYING = "READBACK_VERIFYING"
    LOCAL_VERIFIED = "LOCAL_VERIFIED"
    BOOT_VERIFYING = "BOOT_VERIFYING"
    WAITING_FOR_ONBOARDING = "WAITING_FOR_ONBOARDING"
    WAITING_FOR_TERMINAL_CONFIRMATION = "WAITING_FOR_TERMINAL_CONFIRMATION"
    VERIFYING_SITE = "VERIFYING_SITE"
    VERIFIED_ONLINE = "VERIFIED_ONLINE"
    SITE_VALIDATION_PENDING = "SITE_VALIDATION_PENDING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = {
    ProvisioningState.VERIFIED_ONLINE,
    ProvisioningState.SITE_VALIDATION_PENDING,
    ProvisioningState.RECOVERY_REQUIRED,
    ProvisioningState.FAILED,
    ProvisioningState.CANCELLED,
    ProvisioningState.EXPIRED,
}

STATE_ORDER = [state for state in ProvisioningState if state not in TERMINAL_STATES]
STATE_RANK = {state.value: index for index, state in enumerate(STATE_ORDER)}
IRREVERSIBLE_STATES = {
    ProvisioningState.EFUSE_BURNING.value,
    ProvisioningState.EFUSE_VERIFIED.value,
    ProvisioningState.FLASHING.value,
    ProvisioningState.READBACK_VERIFYING.value,
}


class ProvisioningCompanion(Base):
    __tablename__ = "add_provisioning_companions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    companion_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    installation_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    public_key: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    application_version: Mapped[str] = mapped_column(String(40), index=True)
    pairing_code_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    paired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    paired_operator: Mapped[str | None] = mapped_column(String(120), index=True)
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class FactoryFirmwareBundle(Base):
    __tablename__ = "add_factory_firmware_bundles"
    __table_args__ = (
        UniqueConstraint(
            "hardware_profile",
            "version",
            "manifest_sha256",
            name="uq_add_factory_bundle_immutable",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bundle_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    hardware_profile: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(80), index=True)
    git_sha: Mapped[str] = mapped_column(String(64), index=True)
    partition_layout: Mapped[str] = mapped_column(String(80))
    manifest_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    manifest_signature: Mapped[str] = mapped_column(Text)
    signing_key_ids: Mapped[list] = mapped_column(JSON, default=list)
    setup_password_supplied: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(30), default="HIL_ONLY", index=True)
    storage_prefix: Mapped[str] = mapped_column(String(255), unique=True)
    published_at: Mapped[datetime] = utc_column()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(120))


class ProvisioningSession(Base):
    __tablename__ = "add_provisioning_sessions"
    __table_args__ = (
        UniqueConstraint(
            "operator", "idempotency_key", name="uq_add_provisioning_session_request"
        ),
        Index(
            "uq_add_provisioning_active_mac",
            "hardware_mac",
            unique=True,
            postgresql_where=text(
                "hardware_mac IS NOT NULL AND state NOT IN "
                "('VERIFIED_ONLINE','SITE_VALIDATION_PENDING','RECOVERY_REQUIRED',"
                "'FAILED','CANCELLED','EXPIRED')"
            ),
            sqlite_where=text(
                "hardware_mac IS NOT NULL AND state NOT IN "
                "('VERIFIED_ONLINE','SITE_VALIDATION_PENDING','RECOVERY_REQUIRED',"
                "'FAILED','CANCELLED','EXPIRED')"
            ),
        ),
        Index(
            "uq_add_provisioning_active_companion",
            "companion_id",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('VERIFIED_ONLINE','SITE_VALIDATION_PENDING',"
                "'RECOVERY_REQUIRED','FAILED','CANCELLED','EXPIRED')"
            ),
            sqlite_where=text(
                "state NOT IN ('VERIFIED_ONLINE','SITE_VALIDATION_PENDING',"
                "'RECOVERY_REQUIRED','FAILED','CANCELLED','EXPIRED')"
            ),
        ),
        Index(
            "uq_add_provisioning_active_operator",
            "operator",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('VERIFIED_ONLINE','SITE_VALIDATION_PENDING',"
                "'RECOVERY_REQUIRED','FAILED','CANCELLED','EXPIRED')"
            ),
            sqlite_where=text(
                "state NOT IN ('VERIFIED_ONLINE','SITE_VALIDATION_PENDING',"
                "'RECOVERY_REQUIRED','FAILED','CANCELLED','EXPIRED')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    operator: Mapped[str] = mapped_column(String(120), index=True)
    companion_id: Mapped[int] = mapped_column(
        ForeignKey("add_provisioning_companions.id"), index=True
    )
    hardware_mac: Mapped[str | None] = mapped_column(String(17), index=True)
    hardware_classification: Mapped[str | None] = mapped_column(String(60), index=True)
    hardware_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str | None] = mapped_column(String(40), index=True)
    bundle_id: Mapped[int | None] = mapped_column(
        ForeignKey("add_factory_firmware_bundles.id"), index=True
    )
    zone_id: Mapped[str | None] = mapped_column(String(64), index=True)
    zone_name: Mapped[str | None] = mapped_column(String(120))
    device_id: Mapped[str | None] = mapped_column(String(31), index=True)
    preferred_ip: Mapped[str | None] = mapped_column(String(15))
    zkt_port: Mapped[int | None] = mapped_column(Integer)
    config_digest: Mapped[str | None] = mapped_column(String(64))
    recipient_public_key: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(50), default=ProvisioningState.WAITING_FOR_COMPANION.value, index=True
    )
    progress: Mapped[int] = mapped_column(Integer, default=0)
    last_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    artifact_id: Mapped[str | None] = mapped_column(String(100), index=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    artifact_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connector_id: Mapped[int | None] = mapped_column(ForeignKey("add_connectors.id"), index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    irreversible_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class ProvisioningEvent(Base):
    __tablename__ = "add_provisioning_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_add_provisioning_event_sequence"),
        UniqueConstraint(
            "session_id",
            "source",
            "source_sequence",
            name="uq_add_provisioning_event_source_sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("add_provisioning_sessions.id"), index=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(50), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(30), index=True)
    source_sequence: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = utc_column()


class ProvisionedDeviceRecord(Base):
    __tablename__ = "add_provisioned_device_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hardware_mac: Mapped[str] = mapped_column(String(17), unique=True, index=True)
    derivation_version: Mapped[str] = mapped_column(String(40))
    root_label: Mapped[str] = mapped_column(String(80))
    efuse_purpose: Mapped[str] = mapped_column(String(40))
    secure_boot_digests: Mapped[list] = mapped_column(JSON, default=list)
    hardware_profile: Mapped[str] = mapped_column(String(80), index=True)
    bundle_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    last_session_id: Mapped[int] = mapped_column(
        ForeignKey("add_provisioning_sessions.id"), index=True
    )
    verified_at: Mapped[datetime] = utc_column()
    updated_at: Mapped[datetime] = utc_column()


class ProvisioningCompanionNonce(Base):
    __tablename__ = "add_provisioning_companion_nonces"
    __table_args__ = (
        UniqueConstraint("companion_id", "nonce", name="uq_add_provisioning_companion_nonce"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    companion_id: Mapped[int] = mapped_column(
        ForeignKey("add_provisioning_companions.id"), index=True
    )
    nonce: Mapped[str] = mapped_column(String(120))
    request_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utc_column()


class ProvisioningConfiguration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    wifi_ssid: str
    wifi_password: str
    communication_key: int = Field(ge=0, le=4_294_967_295)
    zkt_port: int = Field(default=4370, ge=1, le=65_535)
    preferred_ip: str = "0.0.0.0"
    device_id: str
    zone_id: str
    zone_name: str

    @field_validator("wifi_ssid")
    @classmethod
    def validate_ssid(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if not 1 <= size <= 32:
            raise ValueError("Wi-Fi SSID must contain 1–32 UTF-8 bytes.")
        return value

    @field_validator("wifi_password")
    @classmethod
    def validate_wifi_password(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if HEX_PSK_PATTERN.fullmatch(value) or 8 <= size <= 63:
            return value
        raise ValueError(
            "Wi-Fi password must contain 8–63 UTF-8 bytes or be a 64-digit hex PSK."
        )

    @field_validator("preferred_ip")
    @classmethod
    def validate_preferred_ip(cls, value: str) -> str:
        if value == "0.0.0.0":
            return value
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError as exc:
            raise ValueError("Preferred IP must be 0.0.0.0 or a private IPv4 address.") from exc
        if not any(address in network for network in RFC1918_NETWORKS):
            raise ValueError("Fixed preferred IP must be RFC1918 unicast.")
        return str(address)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not 1 <= len(value) <= 31 or not ID_PATTERN.fullmatch(value):
            raise ValueError("Device ID must be 1–31 ASCII letters, digits, dots, dashes or underscores.")
        return value

    @field_validator("zone_id")
    @classmethod
    def validate_zone_id(cls, value: str) -> str:
        if not 1 <= len(value) <= 64 or not ID_PATTERN.fullmatch(value):
            raise ValueError("Zone ID must be 1–64 ASCII letters, digits, dots, dashes or underscores.")
        return value

    @field_validator("zone_name")
    @classmethod
    def validate_zone_name(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value:
            raise ValueError("Zone name must not have leading or trailing spaces.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Zone name cannot contain control characters.")
        if not 1 <= len(value.encode("utf-8")) <= 120:
            raise ValueError("Zone name must contain 1–120 UTF-8 bytes.")
        return value

    def public_metadata(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "device_id": self.device_id,
            "preferred_ip": self.preferred_ip,
            "zkt_port": self.zkt_port,
            "wifi_ssid_sha256": hashlib.sha256(self.wifi_ssid.encode()).hexdigest(),
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":")
        ).encode()
        digest_key = settings.provisioning_pairing_secret
        if digest_key:
            return hmac.new(digest_key.encode(), encoded, hashlib.sha256).hexdigest()
        # Physical provisioning cannot be enabled without the key; this branch
        # keeps disabled-mode validation and offline tooling deterministic.
        return hashlib.sha256(encoded).hexdigest()


class HardwareInspection(BaseModel):
    hardware_mac: str
    chip: str
    flash_size_bytes: int
    psram_mode: str
    psram_size_bytes: int
    efuse_classification: str
    secure_boot_digests: list[str] = Field(default_factory=list)
    recipient_public_key: str
    usb_identity: dict[str, str | int] = Field(default_factory=dict)

    @field_validator("hardware_mac")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", ":")
        if not MAC_PATTERN.fullmatch(normalized):
            raise ValueError("Detected ESP Wi-Fi MAC is invalid.")
        return normalized

    @model_validator(mode="after")
    def validate_profile(self) -> "HardwareInspection":
        if self.chip.upper() != "ESP32-S3":
            raise ValueError("Zone Lite requires an ESP32-S3.")
        if self.flash_size_bytes != 16 * 1024 * 1024:
            raise ValueError("Zone Lite requires exactly 16 MB flash.")
        if self.psram_mode.lower() not in {"octal", "opi"}:
            raise ValueError("Zone Lite requires octal PSRAM.")
        if self.psram_size_bytes != 8 * 1024 * 1024:
            raise ValueError("Zone Lite requires exactly 8 MB octal PSRAM.")
        return self


def pairing_code_hash(code: str) -> str:
    secret = settings.provisioning_pairing_secret
    if not secret:
        raise RuntimeError("Provisioning pairing secret is not configured.")
    return hashlib.sha256(f"{secret}:{code}".encode()).hexdigest()


def sanitize_event_details(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)[:80]
            if key.lower() in SECRET_KEYS or any(part in key.lower() for part in SECRET_KEYS):
                clean[key] = "[REDACTED]"
            else:
                clean[key] = sanitize_event_details(raw_value, depth=depth + 1)
        return clean
    if isinstance(value, list):
        return [sanitize_event_details(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def semver_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, str], ...]]:
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
        value,
    )
    if not match:
        return (-1, -1, -1, -1, ((1, value),))
    raw_suffix = match.group(4)
    # Stable releases sort above prereleases. Numeric prerelease identifiers
    # sort numerically and below non-numeric identifiers, per SemVer 2.0.
    suffix = tuple(
        (0, f"{int(item):020d}") if item.isdigit() else (1, item)
        for item in (raw_suffix or "").split(".")
        if item
    )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if raw_suffix is None else 0,
        suffix,
    )


def latest_factory_bundle(session: Session) -> FactoryFirmwareBundle | None:
    sync_factory_bundle_store(session)
    rows = session.scalars(
        select(FactoryFirmwareBundle).where(
            FactoryFirmwareBundle.hardware_profile == settings.provisioning_hardware_profile,
            FactoryFirmwareBundle.state == "AVAILABLE",
            FactoryFirmwareBundle.setup_password_supplied.is_(True),
        )
    ).all()
    return max(rows, key=lambda row: semver_key(row.version), default=None)


def _verify_factory_manifest(manifest: dict[str, Any], signature: str) -> None:
    if not settings.firmware_signing_public_key_pem_b64:
        raise RuntimeError("A firmware signing public key is required for factory bundles.")
    public_key = serialization.load_pem_public_key(
        base64.b64decode(settings.firmware_signing_public_key_pem_b64)
    )
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    public_key.verify(
        base64.b64decode(signature),
        canonical,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


def sync_factory_bundle_store(session: Session) -> None:
    if not settings.provisioning_enabled:
        return
    root = settings.provisioning_factory_store_path
    from pathlib import Path

    store = Path(root).resolve()
    if not store.is_dir():
        raise RuntimeError("Configured factory firmware store is unavailable.")
    observed_available: set[tuple[str, str]] = set()
    for manifest_path in store.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        signature_path = manifest_path.with_name("manifest.sig")
        signature = signature_path.read_text(encoding="ascii").strip()
        _verify_factory_manifest(manifest, signature)
        if manifest.get("hardware_profile") != settings.provisioning_hardware_profile:
            continue
        if manifest.get("partition_layout") != "zone-lite-factory-v1":
            raise RuntimeError("Factory bundle has an unknown partition layout.")
        if manifest.get("setup_password_supplied") is not True:
            raise RuntimeError("Factory bundle lacks protected setup-password build evidence.")
        git_sha = str(manifest.get("git_sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
            raise RuntimeError("Factory bundle source SHA must be the full Git commit.")
        images = manifest.get("images")
        if not isinstance(images, list) or not images:
            raise RuntimeError("Factory bundle has no signed image inventory.")
        expected_offsets = {0, 0x10000, 0x17000, 0x20000}
        offsets: set[int] = set()
        for image in images:
            if not isinstance(image, dict):
                raise RuntimeError("Factory bundle image metadata is invalid.")
            name = Path(str(image.get("name", ""))).name
            if not name or name != image.get("name"):
                raise RuntimeError("Factory bundle image path is invalid.")
            path = manifest_path.parent / name
            if not path.is_file():
                raise RuntimeError(f"Factory bundle image {name} is missing.")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if not hmac.compare_digest(digest, str(image.get("sha256", ""))):
                raise RuntimeError(f"Factory bundle image {name} failed SHA-256 verification.")
            if path.stat().st_size != int(image.get("size", -1)):
                raise RuntimeError(f"Factory bundle image {name} has a size mismatch.")
            offsets.add(int(image.get("offset", -1)))
        if not expected_offsets.issubset(offsets):
            raise RuntimeError("Factory bundle is missing a required immutable flash range.")
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_digest = hashlib.sha256(canonical).hexdigest()
        bundle_id = str(manifest.get("bundle_id", ""))
        version = str(manifest.get("version", ""))
        if not bundle_id or semver_key(version)[0] < 0:
            raise RuntimeError("Factory bundle identity or semantic version is invalid.")
        marker = manifest_path.with_name(".hil-only.json")
        desired_state = "HIL_ONLY" if marker.is_file() else "AVAILABLE"
        key = (settings.provisioning_hardware_profile, version)
        if desired_state == "AVAILABLE" and key in observed_available:
            raise RuntimeError("More than one AVAILABLE factory bundle exists for a version.")
        if desired_state == "AVAILABLE":
            observed_available.add(key)
        existing = session.scalar(
            select(FactoryFirmwareBundle).where(
                FactoryFirmwareBundle.bundle_id == bundle_id
            )
        )
        if existing:
            if existing.manifest_sha256 != manifest_digest:
                raise RuntimeError("Published factory bundle bytes are immutable.")
            if existing.state != "REVOKED":
                existing.state = desired_state
            continue
        session.add(
            FactoryFirmwareBundle(
                bundle_id=bundle_id,
                hardware_profile=settings.provisioning_hardware_profile,
                version=version,
                git_sha=git_sha,
                partition_layout="zone-lite-factory-v1",
                manifest_sha256=manifest_digest,
                manifest=manifest,
                manifest_signature=signature,
                signing_key_ids=list(manifest.get("signing_key_ids") or []),
                setup_password_supplied=True,
                state=desired_state,
                storage_prefix=manifest_path.parent.name,
            )
        )
    session.flush()


def active_session_for_mac(session: Session, hardware_mac: str) -> ProvisioningSession | None:
    return session.scalar(
        select(ProvisioningSession).where(
            ProvisioningSession.hardware_mac == hardware_mac,
            ProvisioningSession.state.not_in([state.value for state in TERMINAL_STATES]),
        )
    )


def ensure_assignment_available(
    session: Session,
    *,
    hardware_mac: str,
    zone_id: str,
    device_id: str,
) -> Connector | None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        # Serialize both the physical MAC and logical destination. Sorting the
        # independent lock keys keeps concurrent transfers deadlock-free.
        for lock_key in sorted(
            {
                f"add-provisioning:mac:{hardware_mac}",
                f"add-provisioning:assignment:{zone_id}:{device_id}",
            }
        ):
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
    existing = session.scalar(select(Connector).where(Connector.hardware_id == hardware_mac))
    collision = session.scalar(
        select(Connector).where(
            Connector.zone_id == zone_id,
            Connector.device_id == device_id,
            Connector.active.is_(True),
            Connector.hardware_id != hardware_mac,
        )
    )
    if collision:
        raise ValueError(
            f"Device ID {device_id} is already active in zone {zone_id}."
        )
    return existing


def append_provisioning_event(
    session: Session,
    row: ProvisioningSession,
    *,
    state: str,
    progress: int,
    source: str,
    details: dict[str, Any] | None = None,
    source_sequence: int | None = None,
) -> ProvisioningEvent:
    try:
        target = ProvisioningState(state)
    except ValueError as exc:
        raise ValueError("Unknown provisioning state.") from exc
    resumes_site_pending = (
        row.state == ProvisioningState.SITE_VALIDATION_PENDING.value
        and target
        in {
            ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION,
            ProvisioningState.VERIFYING_SITE,
            ProvisioningState.VERIFIED_ONLINE,
        }
    )
    if (
        row.state in {item.value for item in TERMINAL_STATES}
        and row.state != target.value
        and not resumes_site_pending
    ):
        raise ValueError("A terminal provisioning session cannot advance.")
    if source_sequence is not None:
        existing = session.scalar(
            select(ProvisioningEvent).where(
                ProvisioningEvent.session_id == row.id,
                ProvisioningEvent.source == source,
                ProvisioningEvent.source_sequence == source_sequence,
            )
        )
        if existing and existing.state == target.value:
            return existing
        if existing:
            raise ValueError("Provisioning source event sequence is conflicting.")
    current_rank = STATE_RANK.get(row.state, len(STATE_ORDER))
    target_rank = STATE_RANK.get(target.value, len(STATE_ORDER))
    if target not in TERMINAL_STATES and target_rank < current_rank and not resumes_site_pending:
        raise ValueError("Provisioning state cannot move backwards.")
    if not 0 <= progress <= 100:
        raise ValueError("Provisioning progress must be between 0 and 100.")
    event_sequence = row.last_sequence + 1
    event = ProvisioningEvent(
        session_id=row.id,
        sequence=event_sequence,
        state=target.value,
        progress=max(row.progress, progress),
        source=source,
        source_sequence=source_sequence,
        details=sanitize_event_details(details or {}),
    )
    session.add(event)
    row.state = target.value
    row.progress = event.progress
    row.last_sequence = event_sequence
    row.updated_at = utc_now()
    if target.value in IRREVERSIBLE_STATES and row.irreversible_started_at is None:
        row.irreversible_started_at = utc_now()
    if target in TERMINAL_STATES:
        row.completed_at = utc_now()
    return event


def serialize_bundle(row: FactoryFirmwareBundle | None) -> dict[str, Any] | None:
    if row is None:
        return None
    images = (row.manifest or {}).get("images", [])
    return {
        "bundle_id": row.bundle_id,
        "hardware_profile": row.hardware_profile,
        "version": row.version,
        "git_sha": row.git_sha,
        "partition_layout": row.partition_layout,
        "manifest_sha256": row.manifest_sha256,
        "signing_key_ids": row.signing_key_ids,
        "images": [
            {
                "name": item.get("name"),
                "offset": item.get("offset"),
                "size": item.get("size"),
                "sha256": item.get("sha256"),
            }
            for item in images
            if isinstance(item, dict)
        ],
        "state": row.state,
        "published_at": row.published_at,
    }


def serialize_session(
    session: Session, row: ProvisioningSession, *, include_events: bool = False
) -> dict[str, Any]:
    companion = session.get(ProvisioningCompanion, row.companion_id)
    bundle = session.get(FactoryFirmwareBundle, row.bundle_id) if row.bundle_id else None
    payload: dict[str, Any] = {
        "session_id": row.session_id,
        "operator": row.operator,
        "companion_id": companion.companion_id if companion else None,
        "hardware_mac": row.hardware_mac,
        "hardware_classification": row.hardware_classification,
        "hardware_evidence": sanitize_event_details(row.hardware_evidence),
        "mode": row.mode,
        "bundle": serialize_bundle(bundle),
        "zone_id": row.zone_id,
        "zone_name": row.zone_name,
        "device_id": row.device_id,
        "preferred_ip": row.preferred_ip,
        "zkt_port": row.zkt_port,
        "state": row.state,
        "progress": row.progress,
        "cancellable": row.irreversible_started_at is None and row.state not in {
            item.value for item in TERMINAL_STATES
        },
        "result": sanitize_event_details(row.result),
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    connector = session.get(Connector, row.connector_id) if row.connector_id else None
    zkt = connector.zkt_device if connector else None
    payload["terminal"] = (
        {
            "observed_serial": zkt.serial,
            "model": zkt.model,
            "ip_address": zkt.ip_address,
            "binding_state": zkt.terminal_binding_state,
            "certification_state": zkt.certification_state,
            "writes_disabled_reason": zkt.writes_disabled_reason,
        }
        if zkt
        else None
    )
    if include_events:
        events = session.scalars(
            select(ProvisioningEvent)
            .where(ProvisioningEvent.session_id == row.id)
            .order_by(ProvisioningEvent.sequence)
        ).all()
        payload["events"] = [
            {
                "sequence": item.sequence,
                "state": item.state,
                "progress": item.progress,
                "source": item.source,
                "details": sanitize_event_details(item.details),
                "created_at": item.created_at,
            }
            for item in events
        ]
    return payload
