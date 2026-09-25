from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease, _storage_predecessor_exclusion
from zk_add.storage_contract import (COMPAT_MARKER, DIRECT_BASELINES, DIRECT_MARKER,
                                     DIRECT_BASELINE_IMAGES, validate_storage_contract)
from zk_add.time_utils import utc_now


def manifest(version):
    if version in ("2.6.1", "2.6.2", "2.6.3", "2.6.4", "2.6.5"):
        return {"application_sha256": "c" * 64, "minimum_bootstrap_version": "2.4.12",
                "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63,
                                  "write_format": 1, "allowed_bootstrap_versions": list(DIRECT_BASELINES),
                                  "allowed_bootstrap_images": DIRECT_BASELINE_IMAGES.copy()}}
    return {"application_sha256": "c" * 64, "minimum_bootstrap_version": "2.5.4" if version == "2.6.0" else "2.2.0",
            "queue_storage": {"schema_version": 1, "read_format": 2, "reader_mask": 63,
                              "write_format": 2 if version == "2.6.0" else 1, "compatibility_version": "2.5.4"}}


@pytest.mark.parametrize("field,value", [("read_format", 1), ("reader_mask", 31), ("write_format", 1),
                                         ("schema_version", True), ("compatibility_version", "2.4.12")])
def test_signed_contract_rejects_unqualified_capabilities(field, value):
    candidate = manifest("2.6.0")
    candidate["queue_storage"][field] = value
    with pytest.raises(ValueError):
        validate_storage_contract(candidate, "2.6.0")


def test_signed_contract_required_for_both_storage_releases():
    for version in ("2.5.4", "2.6.0", "2.6.1", "2.6.2", "2.6.3", "2.6.4", "2.6.5"):
        assert validate_storage_contract(manifest(version), version)
        with pytest.raises(ValueError):
            validate_storage_contract({}, version)
    assert validate_storage_contract({}, "2.5.3") is None
    candidate = manifest("2.6.0")
    candidate["minimum_bootstrap_version"] = "2.2.0"
    with pytest.raises(ValueError):
        validate_storage_contract(candidate, "2.6.0")


def test_direct_predecessor_hashes_and_marker_agree_across_release_gates():
    root = Path(__file__).resolve().parents[2]
    guard = (root / "firmware/zone_lite/main/upgrade_guard.c").read_text()
    firmware = (root / "firmware/zone_lite/main/storage_upgrade.c").read_text()
    signing = (root / "deploy/add/firmware-storage-contract.ps1").read_text()
    for version, digest in DIRECT_BASELINE_IMAGES.items():
        assert f'!strcmp(version, "{version}")' in guard
        assert f'"{digest}"' in guard
        assert f"'{version}' = '{digest}'" in signing
    assert DIRECT_MARKER in firmware
    assert DIRECT_MARKER in signing


@pytest.mark.parametrize("bad", ["2.4.11", "2.5.4", "zone-lite-2.5.4", "2.6.0", "2.6.1", "2.6.2", "2.6.3", "2.6.4", "2.6.5", "2.7.0", None])
@pytest.mark.parametrize("direct_version", ["2.6.1", "2.6.2", "2.6.3", "2.6.4", "2.6.5"])
def test_direct_release_requires_exact_signed_predecessor(bad, direct_version):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        baselines = [FirmwareRelease(release_id=f"zone-lite-{version}", version=version,
            git_sha="a" * 40, image_sha256=("b" if version == "2.4.12" else "c") * 64, image_size=1024,
            signing_key_id="key", partition_layout="zone-lite-ota-v1",
            minimum_bootstrap_version="2.2.0", storage_name=version,
            manifest={"application_sha256": digest}, manifest_signature="fixture", state="AVAILABLE")
            for version, digest in DIRECT_BASELINE_IMAGES.items()]
        release = FirmwareRelease(release_id="direct", version=direct_version, git_sha="a" * 40,
            image_sha256="d" * 64, image_size=1024, signing_key_id="key",
            partition_layout="zone-lite-ota-v1", minimum_bootstrap_version="2.4.12",
            storage_name="direct", manifest=manifest(direct_version), manifest_signature="fixture", state="HIL_ONLY")
        connector = Connector(connector_id="guard-test", hardware_id="00:11:22:33:44:55",
            zone_id="TEST", zone_name="Test", device_id="1", display_name="test",
            firmware_version=bad)
        session.add_all([*baselines, release, connector])
        session.flush()
        assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_VERSION_UNQUALIFIED"
        for allowed in DIRECT_BASELINES:
            for reported in (allowed, f"zone-lite-{allowed}"):
                connector.firmware_version = reported
                connector.ota_image_sha256 = DIRECT_BASELINE_IMAGES[allowed]
                connector.ota_running_partition = "ota_0"
                assert _storage_predecessor_exclusion(session, release, connector) is None
                connector.ota_image_sha256 = "d" * 64
                assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
        release.manifest = {**release.manifest, "queue_storage": {**release.manifest["queue_storage"],
            "allowed_bootstrap_versions": ["2.4.12", "2.5.2", "2.5.4"]}}
        assert _storage_predecessor_exclusion(session, release, connector) == "STORAGE_CONTRACT_INVALID"
    engine.dispose()


def test_candidate_requires_fresh_healthy_compatibility_and_matching_acceptance():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        releases = []
        for version in ("2.5.4", "2.6.0"):
            releases.append(FirmwareRelease(release_id=version, version=version, git_sha="a" * 40,
                image_sha256=("b" if version == "2.5.4" else "d") * 64, image_size=1024, signing_key_id="key", partition_layout="zone-lite-ota-v1",
                minimum_bootstrap_version=manifest(version)["minimum_bootstrap_version"], storage_name=version,
                manifest=manifest(version), manifest_signature="fixture", state="HIL_ONLY"))
        connector = Connector(connector_id="guard-test", hardware_id="00:11:22:33:44:55", zone_id="TEST",
                              zone_name="Test", device_id="1", display_name="test", firmware_version="2.4.12")
        session.add_all([*releases, connector])
        session.flush()
        compat, candidate = releases
        def check():
            return _storage_predecessor_exclusion(session, candidate, connector)
        assert check() == "COMPATIBILITY_FIRMWARE_REQUIRED"
        connector.firmware_version = "2.5.4"
        assert check() == "COMPATIBILITY_RECOVERY_NOT_VERIFIED"
        storage = {"upgrade_ready": True, "upgrade_contract": COMPAT_MARKER, "upgrade_error": "",
                   "durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True}
        connector.firmware_diagnostics = {"storage": storage}
        connector.firmware_diagnostics_at = utc_now()
        assert check() == "COMPATIBILITY_ACCEPTANCE_MISSING"
        campaign = FirmwareCampaign(campaign_id="compat", release_id=compat.id, zone_id="TEST", status="COMPLETED",
                                    actor="test", idempotency_key="compat", reason="test", typed_confirmation="2.5.4")
        session.add(campaign)
        session.flush()
        deployment = FirmwareDeployment(deployment_id="compat", campaign_id=campaign.id, release_id=compat.id,
                                        connector_id=connector.id, status="SUCCEEDED", target_version="2.5.4")
        session.add(deployment)
        session.flush()
        event = FirmwareEvent(deployment_id=deployment.id, state="SUCCEEDED", details={
            "running_version": "2.5.4", "running_partition": "ota_0", "image_sha256": "c" * 64})
        session.add(event)
        session.flush()
        assert check() is None
        for field, bad in (("upgrade_ready", False), ("upgrade_contract", "wrong"), ("upgrade_error", "NVS_FAILED"),
                           ("durability", "UNKNOWN"), ("persistence_verified", False), ("recovery_complete", False)):
            connector.firmware_diagnostics = {"storage": {**storage, field: bad}}
            assert check() == "COMPATIBILITY_RECOVERY_NOT_VERIFIED"
        connector.firmware_diagnostics = {"storage": storage}
        connector.firmware_diagnostics_at = utc_now() - timedelta(seconds=46)
        assert check() == "COMPATIBILITY_RECOVERY_NOT_VERIFIED"
        connector.firmware_diagnostics_at = utc_now()
        for field, bad in (("image_sha256", "d" * 64), ("running_version", "2.4.12"), ("running_partition", "factory")):
            original = event.details
            event.details = {**original, field: bad}
            assert check() == "COMPATIBILITY_ACCEPTANCE_MISMATCH"
            event.details = original
        compat.state = "REVOKED"
        assert check() == "COMPATIBILITY_ACCEPTANCE_MISMATCH"
    engine.dispose()
