from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.db import Base
from zk_add.models import Connector, ZKTDevice
from zk_add.provisioning import (
    FactoryFirmwareBundle,
    HardwareInspection,
    ProvisioningCompanion,
    ProvisioningConfiguration,
    ProvisioningSession,
    ProvisioningState,
    append_provisioning_event,
    sanitize_event_details,
    semver_key,
)
from zk_add.time_utils import utc_now
from zk_add.provisioning_api import (
    TerminalBindingRequest,
    _classify_inspection,
    _verify_companion_release_manifest,
    correlate_onboarding,
)
from zk_add.settings import AddSettings, settings


@pytest.fixture
def provisioning_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def valid_configuration(**overrides):
    values = {
        "wifi_ssid": "State Life Office",
        "wifi_password": "correct horse battery staple",
        "communication_key": 4_294_967_295,
        "zkt_port": 4370,
        "preferred_ip": "0.0.0.0",
        "device_id": "ZKT.01",
        "zone_id": "ZONE-PESH-01",
        "zone_name": "Peshawar Branch 1",
    }
    values.update(overrides)
    return values


def test_shared_configuration_boundaries_and_public_metadata():
    row = ProvisioningConfiguration.model_validate(valid_configuration())
    assert row.preferred_ip == "0.0.0.0"
    assert row.public_metadata()["zkt_port"] == 4370
    assert "wifi_password" not in row.public_metadata()
    assert "communication_key" not in row.public_metadata()
    assert len(row.digest()) == 64
    ProvisioningConfiguration.model_validate(
        valid_configuration(wifi_password="a" * 64)
    )
    ProvisioningConfiguration.model_validate(
        valid_configuration(preferred_ip="192.168.20.4")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wifi_ssid", "é" * 17),
        ("wifi_password", "short"),
        ("communication_key", 4_294_967_296),
        ("zkt_port", 0),
        ("preferred_ip", "8.8.8.8"),
        ("preferred_ip", "127.0.0.1"),
        ("device_id", "x" * 32),
        ("zone_id", "has spaces"),
        ("zone_name", " trailing "),
        ("zone_name", "bad\nname"),
    ],
)
def test_shared_configuration_rejects_unsafe_values(field: str, value):
    with pytest.raises(ValidationError):
        ProvisioningConfiguration.model_validate(valid_configuration(**{field: value}))


def test_sanitizer_recursively_redacts_every_secret_class():
    sanitized = sanitize_event_details(
        {
            "wifi_password": "never-store-me",
            "nested": {"comm_key": 123, "connector_token": "token", "safe": "OK"},
            "raw_nvs_payload": "bytes",
        }
    )
    assert sanitized["wifi_password"] == "[REDACTED]"
    assert sanitized["nested"]["comm_key"] == "[REDACTED]"
    assert sanitized["nested"]["connector_token"] == "[REDACTED]"
    assert sanitized["raw_nvs_payload"] == "[REDACTED]"
    assert sanitized["nested"]["safe"] == "OK"
    assert "never-store-me" not in str(sanitized)


def test_provisioning_events_are_monotonic_idempotent_and_terminal(provisioning_db: Session):
    companion = ProvisioningCompanion(
        installation_id="installation-identity-0001",
        public_key="public",
        platform="windows-x64",
        application_version="1.0.0",
        paired=True,
    )
    provisioning_db.add(companion)
    provisioning_db.flush()
    row = ProvisioningSession(
        operator="StateHealthAdmin",
        companion_id=companion.id,
        state=ProvisioningState.WAITING_FOR_DEVICE.value,
        idempotency_key="test-session-one",
        expires_at=utc_now() + timedelta(minutes=30),
    )
    provisioning_db.add(row)
    provisioning_db.flush()
    inspected = append_provisioning_event(
        provisioning_db,
        row,
        state=ProvisioningState.CONFIGURING.value,
        progress=5,
        source="COMPANION",
        source_sequence=1,
    )
    replay = append_provisioning_event(
        provisioning_db,
        row,
        state=ProvisioningState.CONFIGURING.value,
        progress=5,
        source="COMPANION",
        source_sequence=1,
    )
    assert replay is inspected
    with pytest.raises(ValueError, match="backwards"):
        append_provisioning_event(
            provisioning_db,
            row,
            state=ProvisioningState.INSPECTING.value,
            progress=5,
            source="COMPANION",
            source_sequence=2,
        )
    append_provisioning_event(
        provisioning_db,
        row,
        state=ProvisioningState.FAILED.value,
        progress=5,
        source="SERVER",
    )
    with pytest.raises(ValueError, match="terminal"):
        append_provisioning_event(
            provisioning_db,
            row,
            state=ProvisioningState.CONFIGURING.value,
            progress=6,
            source="SERVER",
        )


def test_semver_selection_key_orders_production_versions():
    assert semver_key("2.10.0") > semver_key("2.9.99")
    assert semver_key("2.4.5") > semver_key("2.4.5-rc.1")
    assert semver_key("2.4.5+build.9") == semver_key("2.4.5+build.1")
    assert semver_key("not-a-version")[0] == -1


def test_hardware_profile_requires_exact_s3_flash_and_octal_psram():
    inspection = HardwareInspection(
        hardware_mac="e0:72:a1:d6:f3:28",
        chip="ESP32-S3",
        flash_size_bytes=16 * 1024 * 1024,
        psram_mode="octal",
        psram_size_bytes=8 * 1024 * 1024,
        efuse_classification="BLANK",
        recipient_public_key="recipient",
    )
    assert inspection.psram_size_bytes == 8 * 1024 * 1024
    with pytest.raises(ValidationError, match="8 MB octal PSRAM"):
        HardwareInspection.model_validate(
            {**inspection.model_dump(), "psram_size_bytes": 2 * 1024 * 1024}
        )


def test_terminal_binding_serial_is_safe_for_firmware_result_json():
    assert TerminalBindingRequest(observed_serial="SN-1234:5", password="password")
    with pytest.raises(ValidationError):
        TerminalBindingRequest(observed_serial='bad"serial', password="password")


def test_legacy_root_never_trusts_a_companion_boolean(provisioning_db: Session):
    inspection = HardwareInspection(
        hardware_mac="e0:72:a1:d6:f3:28",
        chip="ESP32-S3",
        flash_size_bytes=16 * 1024 * 1024,
        psram_mode="octal",
        psram_size_bytes=8 * 1024 * 1024,
        efuse_classification="LEGACY",
        recipient_public_key="recipient",
    )
    assert _classify_inspection(provisioning_db, inspection, True) == (
        "UNKNOWN_FOREIGN",
        "RECOVERY",
    )


def test_enabling_provisioning_requires_every_independent_verification_key():
    configured = {
        "admin_password_hash": "hash",
        "pii_fernet_key": "fernet",
        "pii_lookup_key": "lookup",
        "fleet_root_secret": "fleet",
        "provisioning_enabled": True,
        "provisioning_pairing_secret": "pairing",
        "provisioning_internal_token": "internal",
    }
    with pytest.raises(RuntimeError, match="COMPANION_RELEASE_PUBLIC_KEY"):
        AddSettings(_env_file=None, **configured).require_production_secrets()
    AddSettings(
        _env_file=None,
        **configured,
        provisioning_companion_release_public_key_b64="release-public",
        firmware_signing_public_key_pem_b64="factory-public",
    ).require_production_secrets()


def test_factory_bundle_identity_is_immutable_shape(provisioning_db: Session):
    bundle = FactoryFirmwareBundle(
        bundle_id="zone-lite-2.4.5-6132c9b773b9",
        hardware_profile="esp32s3-16mb-zone-lite-v1",
        version="2.4.5",
        git_sha="6132c9b773b9a4016173e9b99dfde6ccc5dc29e5",
        partition_layout="zone-lite-factory-v1",
        manifest_sha256="a" * 64,
        manifest={"images": []},
        manifest_signature="signature",
        signing_key_ids=["active", "reserve-1", "reserve-2"],
        setup_password_supplied=True,
        state="AVAILABLE",
        storage_prefix="zone-lite-2.4.5-6132c9b773b9",
    )
    provisioning_db.add(bundle)
    provisioning_db.flush()
    assert bundle.setup_password_supplied


def test_latest_companion_release_uses_highest_verified_semver(
    tmp_path: Path, monkeypatch
):
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    root = tmp_path / "releases"
    for version in ("1.9.9", "1.10.0"):
        directory = root / "windows-x64" / version
        directory.mkdir(parents=True)
        artifact = directory / "add-provisioning-companion-windows-x64.exe"
        artifact.write_bytes(version.encode())
        manifest = {
            "schema_version": 1,
            "platform": "windows-x64",
            "version": version,
            "filename": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size": artifact.stat().st_size,
            "git_sha": "a" * 40,
            "os_signed": False,
            "published_at": "2026-08-09T00:00:00+00:00",
        }
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (directory / "manifest.json").write_bytes(canonical)
        (directory / "manifest.sig").write_text(
            base64.b64encode(private.sign(canonical)).decode(), encoding="ascii"
        )
    monkeypatch.setattr(settings, "provisioning_companion_release_path", str(root))
    monkeypatch.setattr(
        settings, "provisioning_companion_release_public_key_b64", public_b64
    )

    manifest, artifact = _verify_companion_release_manifest("windows-x64")
    assert manifest["version"] == "1.10.0"
    assert artifact.parent.name == "1.10.0"


def test_provisioning_onboarding_resets_a_prior_terminal_pin(provisioning_db: Session):
    companion = ProvisioningCompanion(
        installation_id="installation-identity-0002",
        public_key="public",
        platform="windows-x64",
        application_version="1.0.0",
        paired=True,
    )
    connector = Connector(
        connector_id="connector-one",
        hardware_id="e0:72:a1:d6:f3:28",
        zone_id="ZONE-NEW",
        zone_name="New zone",
        device_id="ZKT-NEW",
        display_name="New zone / ZKT-NEW",
    )
    provisioning_db.add_all([companion, connector])
    provisioning_db.flush()
    terminal = ZKTDevice(
        connector_id=connector.id,
        serial="NEW-SERIAL",
        expected_serial="OLD-SERIAL",
        confirmed_serial="OLD-SERIAL",
        terminal_binding_state="CONFIRMED",
        serial_confirmed_by="previous-session",
        certification_state="CERTIFIED",
    )
    row = ProvisioningSession(
        operator="StateHealthAdmin",
        companion_id=companion.id,
        hardware_mac=connector.hardware_id,
        zone_id=connector.zone_id,
        device_id=connector.device_id,
        state=ProvisioningState.WAITING_FOR_ONBOARDING.value,
        idempotency_key="test-session-two",
        expires_at=utc_now() + timedelta(minutes=30),
    )
    provisioning_db.add_all([terminal, row])
    provisioning_db.flush()

    assert correlate_onboarding(provisioning_db, connector) is row
    assert terminal.expected_serial is None
    assert terminal.confirmed_serial is None
    assert terminal.terminal_binding_state == "SERIAL_CONFIRMATION_REQUIRED"
    assert row.state == ProvisioningState.WAITING_FOR_TERMINAL_CONFIRMATION.value
