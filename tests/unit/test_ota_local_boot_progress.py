"""A current boot wait is telemetry, not a terminal OTA failure."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector, DeviceAlert
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease
from zk_add.schemas import HeartbeatPayload
from zk_add.service import LOCAL_BOOT_WAIT_REASONS, apply_ota_heartbeat_diagnostics


@pytest.fixture
def attempt():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        connector = Connector(connector_id="boot-test", hardware_id="00:11:22:33:44:55",
            zone_id="SYNTHETIC", zone_name="Synthetic", device_id="1", display_name="Synthetic",
            firmware_family="zkt", firmware_version="zone-lite-2.6.17", ota_state="UPDATING")
        release = FirmwareRelease(release_id="boot-test", version="2.6.17", git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1380352, signing_key_id="test-only",
            partition_layout="zone-lite-ota-v1", storage_name="synthetic.bin", state="HIL_ONLY",
            manifest={"application_sha256": "c" * 64, "firmware_family": "zkt"},
            manifest_signature="synthetic-not-signed")
        session.add_all([connector, release])
        session.flush()
        campaign = FirmwareCampaign(campaign_id="boot-test", release_id=release.id,
            zone_id=connector.zone_id, status="ACTIVE", actor="test", idempotency_key="boot-test",
            reason="Synthetic local boot check", typed_confirmation=release.version)
        session.add(campaign)
        session.flush()
        deployment = FirmwareDeployment(deployment_id="boot-test", campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, status="READY_TO_BOOT",
            previous_version="2.6.15", target_version=release.version, bytes_written=release.image_size)
        session.add(deployment)
        session.flush()
        payload = HeartbeatPayload(firmware_version="zone-lite-2.6.17", uptime_seconds=22, ota={
            "state": "READY_TO_BOOT", "target_version": "2.6.17", "running_version": "2.6.17",
            "running_partition": "ota_0", "image_sha256": "c" * 64, "capable": True,
            "secure_boot": True, "rollback_enabled": True, "partition_layout": "zone-lite-ota-v1",
            "bytes_written": release.image_size, "image_size": release.image_size,
            "boot_health_checks": 17, "boot_health_last_ready": False,
            "last_error": "BOOT_LOCAL_JOURNAL_RECOVERY",
        })
        yield session, connector, release, campaign, deployment, payload
    engine.dispose()


@pytest.mark.parametrize("version", ["2.6.16", "2.6.17", "2.7.0"])
@pytest.mark.parametrize("reason", sorted(LOCAL_BOOT_WAIT_REASONS))
def test_current_signed_image_may_continue_its_local_boot_wait(attempt, version, reason):
    session, connector, release, campaign, deployment, payload = attempt
    release.version = deployment.target_version = payload.ota.target_version = version
    payload.firmware_version = f"zone-lite-{version}"
    payload.ota.running_version = version
    payload.ota.last_error = reason
    apply_ota_heartbeat_diagnostics(session, connector=connector, payload=payload)
    assert deployment.status == "READY_TO_BOOT"
    assert campaign.status == "ACTIVE"
    assert connector.ota_state == "UPDATING"
    assert session.scalar(select(FirmwareEvent)) is None
    assert session.scalar(select(DeviceAlert)) is None


@pytest.mark.parametrize(("field", "value"), [
    ("state", "FAILED"), ("state", "DOWNLOADING"),
    ("last_error", "BOOT_HEALTH_TIMEOUT"), ("last_error", "BOOT_LOCAL_MARK_VALID_FAILED"),
    ("last_error", "BOOT_LOCAL_IMAGE_UNSUPPORTED"), ("last_error", "DOWNLOAD_BEGIN_FAILED"),
    ("last_error", "BOOT_LOCAL_UNRECOGNIZED"),
    ("running_version", "2.6.15"), ("running_version", None),
    ("running_partition", "factory"), ("image_sha256", "d" * 64),
    ("secure_boot", False), ("rollback_enabled", False),
    ("partition_layout", "unknown"), ("bytes_written", 0), ("image_size", 0),
    ("boot_health_checks", 0), ("boot_health_checks", 900), ("boot_health_last_ready", True),
])
def test_real_failures_and_unproven_waits_still_stop_the_campaign(attempt, field, value):
    session, connector, _, campaign, deployment, payload = attempt
    setattr(payload.ota, field, value)
    apply_ota_heartbeat_diagnostics(session, connector=connector, payload=payload)
    assert deployment.status == "FAILED"
    assert deployment.error_code == payload.ota.last_error
    assert campaign.status == "PAUSED"
    assert session.scalar(select(FirmwareEvent)).state == "FAILED"


@pytest.mark.parametrize("uptime", [None, -1, 961, 2**32])
def test_wait_is_bounded_by_current_boot_age(attempt, uptime):
    session, connector, _, campaign, deployment, payload = attempt
    payload.uptime_seconds = uptime
    apply_ota_heartbeat_diagnostics(session, connector=connector, payload=payload)
    assert deployment.status == "FAILED"
    assert campaign.status == "PAUSED"


@pytest.mark.parametrize("mismatch", ["legacy", "family", "deployment", "running_image", "unsigned_digest"])
def test_wait_reason_cannot_waive_other_contracts(attempt, mismatch):
    session, connector, release, campaign, deployment, payload = attempt
    if mismatch == "legacy":
        release.version = deployment.target_version = payload.ota.target_version = "2.6.15"
        payload.firmware_version = payload.ota.running_version = "2.6.15"
    elif mismatch == "family":
        connector.firmware_family = "hikvision"
    elif mismatch == "deployment":
        deployment.status = "RECONCILING"
    elif mismatch == "running_image":
        payload.firmware_version = "zone-lite-2.6.15"
    else:
        release.manifest = {}
    apply_ota_heartbeat_diagnostics(session, connector=connector, payload=payload)
    assert deployment.status == "FAILED"
    assert campaign.status == "PAUSED"


def test_wait_does_not_resume_an_operator_paused_campaign(attempt):
    session, connector, _, campaign, deployment, payload = attempt
    campaign.status = "PAUSED"
    campaign.pause_reason = "Operator review"
    apply_ota_heartbeat_diagnostics(session, connector=connector, payload=payload)
    assert deployment.status == "READY_TO_BOOT"
    assert campaign.status == "PAUSED"
    assert campaign.pause_reason == "Operator review"
