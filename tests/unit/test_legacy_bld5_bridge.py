import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_hil_scope import bld5_2615_session, hil_session, parallel_269_session  # noqa: F401
from zk_add.legacy_bld5_bridge import BRIDGE_IDENTITY, accept_legacy_progress
from zk_add.models import DeviceTelemetry
from zk_add.ota import FirmwareDeployment, FirmwareEvent, FirmwareRelease, create_campaign, preview_campaign_scope
from zk_add.time_utils import utc_now
from zk_add.web import _FirmwareCapabilityIn, _FirmwareProgressIn, firmware_progress, report_firmware_capability


@pytest.fixture(name="legacy_bridge")
def bridge_fixture(request, monkeypatch):
    from zk_add.settings import settings

    session, _release, devices, _zones = request.getfixturevalue("bld5_2615_session")
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    device = devices[5]
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == BRIDGE_IDENTITY[0]))
    release.release_id, release.version, release.git_sha, release.image_sha256, application = BRIDGE_IDENTITY
    release.manifest = {"application_sha256": application}
    device.firmware_version = "zone-lite-2.5.2"
    device.ota_running_partition = "factory"
    device.ota_image_sha256 = "27128790bde3ce3d0e5e697bb35189379cda8600f5179fab075f127c2dc9671b"
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    campaign = create_campaign(
        session, release_public_id=release.release_id, zone_id=device.zone_id,
        reason="Exact BLD5 bridge", typed_confirmation=release.version,
        actor="test-admin", scope_token=scope["scope_token"], idempotency_key="legacy-bld5-bridge",
    )
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id))
    deployment.status = "READY_TO_BOOT"
    deployment.bytes_written = release.image_size
    device.boot_id = "verified-new-bridge-boot"
    device.firmware_version = "zone-lite-2.4.12"
    sample = DeviceTelemetry(
        connector_id=device.id, boot_id=device.boot_id, sequence=1,
        payload={"firmware_version": device.firmware_version},
    )
    session.add(sample)
    session.flush()
    return session, device, release, deployment, sample


def progress(session, device, release, deployment, state, error=None):
    return asyncio.run(firmware_progress(
        deployment.deployment_id,
        _FirmwareProgressIn(state=state, bytes_written=release.image_size,
                            running_version=release.version, error_code=error),
        (session, device),
    ))


def capability(session, device, release, digest=None):
    return asyncio.run(report_firmware_capability(
        _FirmwareCapabilityIn(
            capable=True, secure_boot=True, rollback_enabled=True,
            partition_layout=release.partition_layout, running_version=release.version,
            running_partition="ota_0", image_sha256=digest or BRIDGE_IDENTITY[4],
        ),
        (session, device),
    ))


def healthy_reports(session, device, release, deployment):
    for state, error in (("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH"),
                         ("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
        response = progress(session, device, release, deployment, state, error)
        assert response.status_code == 202
        assert json.loads(response.body)["awaiting_signed_capability"] is True
        assert deployment.status == "READY_TO_BOOT"


def test_legacy_bridge_completes_only_after_signed_capability_and_success_report(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    healthy_reports(session, device, release, deployment)
    with pytest.raises(HTTPException, match="Signed current-boot capability proof"):
        progress(session, device, release, deployment, "SUCCEEDED")
    assert deployment.status == "READY_TO_BOOT"
    assert capability(session, device, release)["accepted"] is True
    assert deployment.status == "RECONCILING"
    response = progress(session, device, release, deployment, "SUCCEEDED")
    assert response.status_code == 200
    assert deployment.status == "SUCCEEDED"
    event = session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "SUCCEEDED",
    ))
    assert event.details["image_sha256"] == BRIDGE_IDENTITY[4]
    assert event.details["running_partition"] == "ota_0"


@pytest.mark.parametrize("changed", ["digest", "boot", "stale", "healthy_report_missing"])
def test_legacy_bridge_cannot_complete_with_unverified_or_stale_proof(legacy_bridge, changed):
    session, device, release, deployment, sample = legacy_bridge
    if changed != "healthy_report_missing":
        healthy_reports(session, device, release, deployment)
    if changed == "boot":
        device.boot_id = sample.boot_id = "different-boot"
    if changed == "stale":
        sample.created_at = utc_now() - timedelta(seconds=60)
        with pytest.raises(HTTPException, match="Fresh current-boot telemetry"):
            capability(session, device, release)
    else:
        capability(session, device, release, digest="0" * 64 if changed == "digest" else None)
    assert deployment.status == "READY_TO_BOOT"
    with pytest.raises(HTTPException):
        progress(session, device, release, deployment, "SUCCEEDED")
    assert deployment.status == "READY_TO_BOOT"


@pytest.mark.parametrize("changed", ["connector", "serial", "zone", "family", "source", "artifact", "target"])
def test_legacy_bridge_adapter_is_limited_to_exact_release_and_zkt_target(legacy_bridge, changed):
    session, device, release, deployment, _sample = legacy_bridge
    if changed == "connector":
        device.connector_id = "replacement"
    elif changed == "serial":
        device.zkt_device.confirmed_serial = "replacement"
    elif changed == "zone":
        device.zone_id = "ZONE-OTHER"
    elif changed == "family":
        device.firmware_family = "hikvision"
    elif changed == "source":
        release.git_sha = "0" * 40
    elif changed == "artifact":
        release.image_sha256 = "0" * 64
    else:
        deployment.target_version = "2.6.15"
    report = {"state": "BOOTED_PENDING", "bytes_written": release.image_size,
              "running_version": release.version, "error_code": "WAITING_FOR_RUNTIME_HEALTH"}
    assert accept_legacy_progress(
        session, connector=device, deployment_id=deployment.deployment_id, report=report,
    ) is None
    assert deployment.status == "READY_TO_BOOT"


def test_legacy_bridge_does_not_accept_partial_new_protocol_fields(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    report = {"state": "BOOTED_PENDING", "bytes_written": release.image_size,
              "running_version": release.version, "running_partition": "ota_0"}
    assert accept_legacy_progress(
        session, connector=device, deployment_id=deployment.deployment_id, report=report,
    ) is None
