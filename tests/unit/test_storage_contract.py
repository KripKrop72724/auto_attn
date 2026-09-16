from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease, _storage_predecessor_exclusion
from zk_add.storage_contract import COMPAT_MARKER, validate_storage_contract
from zk_add.time_utils import utc_now


def manifest(version):
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
    for version in ("2.5.4", "2.6.0"):
        assert validate_storage_contract(manifest(version), version)
        with pytest.raises(ValueError):
            validate_storage_contract({}, version)
    assert validate_storage_contract({}, "2.5.3") is None
    candidate = manifest("2.6.0")
    candidate["minimum_bootstrap_version"] = "2.2.0"
    with pytest.raises(ValueError):
        validate_storage_contract(candidate, "2.6.0")


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
