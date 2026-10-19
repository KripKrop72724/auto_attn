import asyncio
import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from test_hil_scope import bld5_2615_session, hil_session, parallel_269_session  # noqa: F401
from zk_add.hil_2615_cities import CITY_FACTORY_PREDECESSORS, CITY_TARGETS
from zk_add import zkt274_bld9_k50 as bld9_k50
from zk_add.legacy_bld5_bridge import BRIDGE_IDENTITY, accept_legacy_progress
from zk_add.db import Base
from zk_add.models import Connector, DeviceTelemetry, ZKTDevice
from zk_add.ota import (
    FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease, create_campaign, preview_campaign_scope,
)
from zk_add.time_utils import ensure_utc, utc_now
from zk_add.web import _FirmwareCapabilityIn, _FirmwareProgressIn, firmware_progress, report_firmware_capability


@pytest.fixture(name="legacy_bridge", params=["LF-ZONE-BLD5-01", "ZONE-LAHORE-01", "ZONE-LAHORE-02", bld9_k50.ZONE])
def bridge_fixture(request, monkeypatch):
    from zk_add.settings import settings

    session, _release, devices, _zones = request.getfixturevalue("bld5_2615_session")
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    device = devices[5]
    if request.param in CITY_TARGETS or request.param == bld9_k50.ZONE:
        target = CITY_TARGETS.get(request.param, bld9_k50.TARGET)
        device.zone_id = request.param
        device.connector_id, device.hardware_id = target.connector_id, target.mac
        device.zkt_device.serial = device.zkt_device.expected_serial = device.zkt_device.confirmed_serial = target.terminal_serial
    release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == BRIDGE_IDENTITY[0]))
    release.release_id, release.version, release.git_sha, release.image_sha256, application = BRIDGE_IDENTITY
    release.manifest = {"application_sha256": application}
    device.firmware_version = "zone-lite-2.5.2"
    device.ota_running_partition = "factory"
    device.ota_image_sha256 = "27128790bde3ce3d0e5e697bb35189379cda8600f5179fab075f127c2dc9671b"
    if request.param in CITY_FACTORY_PREDECESSORS:
        device.ota_image_sha256 = CITY_FACTORY_PREDECESSORS[request.param][1]
    if request.param == bld9_k50.ZONE:
        device.ota_image_sha256 = bld9_k50.FACTORY_PREDECESSOR[1]
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=device.zone_id)
    campaign = create_campaign(
        session, release_public_id=release.release_id, zone_id=device.zone_id,
        reason="Exact legacy bridge", typed_confirmation=release.version,
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
    # Evidence kept while a bridge is paused is limited to the same exact target.
    session.get(FirmwareCampaign, deployment.campaign_id).status = "PAUSED"
    capability(session, device, release)
    assert deployment.status == "READY_TO_BOOT"
    assert not session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["LEGACY_BOOT_CAPABILITY_REPORT", "LEGACY_BOOT_CAPABILITY_VERIFIED"]),
    )).all()


def test_legacy_bridge_does_not_accept_partial_new_protocol_fields(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    report = {"state": "BOOTED_PENDING", "bytes_written": release.image_size,
              "running_version": release.version, "running_partition": "ota_0"}
    assert accept_legacy_progress(
        session, connector=device, deployment_id=deployment.deployment_id, report=report,
    ) is None


# ---------------------------------------------------------------------------
# 19 October 2026: zone-lite-2.4.12 on ZONE-BLD9-01 stayed READY_TO_BOOT.
# ADD request sessions do not autoflush (zk_add.db.SessionLocal) and roll back
# on any error (get_db); the helpers below reproduce both, unlike the tests
# above, whose fixture session autoflushes.

GATED_ZONES = {"ZONE-LAHORE-01", "ZONE-LAHORE-02", bld9_k50.ZONE}
X_BOOT = "ac276ea33328-f4928d2c"
FACTORY_BOOT = "ac276ea33328-0b5e2a71"


def production(session):
    session.commit()
    session.autoflush = False


def call(session, endpoint, body, device, *path):
    """One device request through the real endpoint; ADD's get_db rolls back an error."""
    try:
        response = asyncio.run(endpoint(*path, body, (session, device)))
    except HTTPException as error:
        session.rollback()
        return error.status_code, error.detail
    return getattr(response, "status_code", 200), response


def report(session, device, release, deployment, state, error=None):
    return call(session, firmware_progress, _FirmwareProgressIn(
        state=state, bytes_written=release.image_size, running_version=release.version, error_code=error,
    ), device, deployment.deployment_id)


def report_capability(session, device, release, partition="ota_0", digest=None):
    return call(session, report_firmware_capability, _FirmwareCapabilityIn(
        capable=True, secure_boot=True, rollback_enabled=True, partition_layout=release.partition_layout,
        running_version=release.version, running_partition=partition, image_sha256=digest or BRIDGE_IDENTITY[4],
    ), device)


def heartbeat(session, device, firmware="2.4.12", boot_id=None, at=None):
    device.boot_id = boot_id or device.boot_id
    session.add(DeviceTelemetry(connector_id=device.id, boot_id=boot_id or device.boot_id, sequence=1,
                                payload={"firmware_version": firmware}, created_at=at or utc_now()))
    session.commit()


def elapse(session, device, deployment, seconds):
    """Move every recorded instant back, as if `seconds` had passed."""
    shift = timedelta(seconds=seconds)
    for row in session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id)):
        row.created_at = ensure_utc(row.created_at) - shift
    for row in session.scalars(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == device.id)):
        row.created_at = ensure_utc(row.created_at) - shift
    if deployment.offered_at is not None:
        deployment.offered_at = ensure_utc(deployment.offered_at) - shift
    session.commit()


def bridge_events(session, deployment, state):
    return list(session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == state,
    ).order_by(FirmwareEvent.id)))


def run_2412_device(session, device, release, deployment, minutes=8):
    """Drive the exact 2.4.12 ota_task (45690c40 ota_manager.c) after boot.

    confirm_or_report_rollback: WAITING... until healthy, RUNTIME_HEALTHY,
    RECONCILING, 45 s, one SUCCEEDED. Then every 60 s: the capability report
    until ADD accepts it (never again in this boot), then SUCCEEDED until ADD
    accepts it. Heartbeats keep current-boot telemetry fresh throughout.
    """
    log = []
    for state, error, wait in (("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH", 117),
                               ("BOOTED_PENDING", "RUNTIME_HEALTHY", 1), ("RECONCILING", None, 45)):
        log.append((state, error, report(session, device, release, deployment, state, error)[0]))
        elapse(session, device, deployment, wait)
        heartbeat(session, device)
    log.append(("SUCCEEDED", None, report(session, device, release, deployment, "SUCCEEDED")[0]))
    capability_reported = False
    for _minute in range(minutes):
        if log[-1][0] == "SUCCEEDED" and log[-1][2] == 200:
            break
        if not capability_reported:
            log.append(("CAPABILITY", None, report_capability(session, device, release)[0]))
            capability_reported = log[-1][2] == 200
        if capability_reported:
            log.append(("SUCCEEDED", None, report(session, device, release, deployment, "SUCCEEDED")[0]))
            if log[-1][2] == 200:
                break
        elapse(session, device, deployment, 60)
        heartbeat(session, device)
    return log


def test_exact_2412_device_sequence_completes_under_production_sessions(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    log = run_2412_device(session, device, release, deployment)
    # Before the fix: CAPABILITY 409 "Illegal firmware transition READY_TO_BOOT
    # -> RECONCILING" while the reports were under 180 s old, then 200 with no
    # proof, then SUCCEEDED 409 every minute for ever.
    assert log == [
        ("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH", 202), ("BOOTED_PENDING", "RUNTIME_HEALTHY", 202),
        ("RECONCILING", None, 202), ("SUCCEEDED", None, 409), ("CAPABILITY", None, 200), ("SUCCEEDED", None, 200),
    ]
    assert deployment.status == "SUCCEEDED"
    proof, = bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")
    assert proof.details["boot_id"] == device.boot_id
    assert proof.details["evidence"]["source"] == "CAPABILITY_REPORT"
    assert [event.state for event in session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state.in_(["BOOTED_PENDING", "RECONCILING", "SUCCEEDED"]),
    ).order_by(FirmwareEvent.id))] == ["BOOTED_PENDING", "RECONCILING", "SUCCEEDED"]


def test_capability_before_reports_completes_on_the_reconciling_report(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    status, body = report_capability(session, device, release)
    assert status == 200 and body["accepted"] is True
    assert deployment.status == "READY_TO_BOOT"
    assert len(bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_REPORT")) == 1
    for state, error in (("BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH"), ("BOOTED_PENDING", "RUNTIME_HEALTHY")):
        assert report(session, device, release, deployment, state, error)[0] == 202
        assert deployment.status == "READY_TO_BOOT"
    elapse(session, device, deployment, 900)
    heartbeat(session, device)
    status, response = report(session, device, release, deployment, "RECONCILING")
    assert status == 200 and json.loads(response.body)["awaiting_signed_capability"] is False
    assert deployment.status == "RECONCILING"
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 200
    assert deployment.status == "SUCCEEDED"


def test_capability_long_after_the_reports_still_binds_the_current_boot(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    for state, error in (("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
        assert report(session, device, release, deployment, state, error)[0] == 202
    elapse(session, device, deployment, 3 * 3600)
    heartbeat(session, device)
    assert report_capability(session, device, release)[0] == 200
    assert deployment.status == "RECONCILING"
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 200
    assert deployment.status == "SUCCEEDED"


@pytest.mark.parametrize("order", ["capability_first", "reports_first"])
@pytest.mark.parametrize("changed", ["digest", "factory_partition", "boot"])
def test_wrong_or_other_boot_capability_never_completes_in_either_order(legacy_bridge, order, changed):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)

    def send_capability():
        assert report_capability(
            session, device, release, partition="factory" if changed == "factory_partition" else "ota_0",
            digest="0" * 64 if changed == "digest" else None,
        )[0] == 200

    if order == "capability_first":
        if changed == "boot":
            # An earlier boot reported the exact image, then the device restarted.
            heartbeat(session, device, boot_id="earlier-2412-boot")
        send_capability()
        if changed == "boot":
            heartbeat(session, device, boot_id=X_BOOT)
    for state, error in (("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
        assert report(session, device, release, deployment, state, error)[0] == 202
    if order == "reports_first":
        if changed == "boot":
            # The reporting boot ended; the next boot reports the exact image.
            heartbeat(session, device, boot_id="later-2412-boot")
        send_capability()
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 409
    assert deployment.status == "READY_TO_BOOT"
    assert not bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")


@pytest.mark.parametrize("order", ["reports_first", "capability_first"])
def test_capability_accepted_while_the_campaign_is_paused_completes_after_resume(legacy_bridge, order):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    if order == "reports_first":
        for state, error in (("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
            assert report(session, device, release, deployment, state, error)[0] == 202
    campaign.status = "PAUSED"
    session.commit()
    # ADD answers 200, so 2.4.12 never sends this report again in this boot.
    status, body = report_capability(session, device, release)
    assert status == 200 and body["accepted"] is True
    assert len(bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_REPORT")) == 1
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 409
    assert deployment.status == "READY_TO_BOOT"
    assert not bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")
    campaign.status = "ACTIVE"
    session.commit()
    elapse(session, device, deployment, 1800)
    heartbeat(session, device)
    if order == "capability_first":
        assert report(session, device, release, deployment, "BOOTED_PENDING", "RUNTIME_HEALTHY")[0] == 202
        assert deployment.status == "READY_TO_BOOT"
        assert report(session, device, release, deployment, "RECONCILING")[0] == 200
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 200
    assert deployment.status == "SUCCEEDED"
    proof, = bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")
    assert proof.details["evidence"]["source"] == "CAPABILITY_REPORT"


@pytest.mark.parametrize("inactive", ["paused", "cancelled", "release_revoked"])
@pytest.mark.parametrize("changed", [None, "digest", "rebooted"])
def test_capability_kept_while_the_bridge_is_inactive_never_moves_the_deployment(legacy_bridge, inactive, changed):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    for state, error in (("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
        assert report(session, device, release, deployment, state, error)[0] == 202
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    if inactive == "release_revoked":
        release.state = "REVOKED"
    else:
        campaign.status = inactive.upper()
    session.commit()
    if changed == "rebooted":
        heartbeat(session, device, boot_id="later-2412-boot")
    assert report_capability(session, device, release, digest="0" * 64 if changed == "digest" else None)[0] == 200
    assert len(bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_REPORT")) == (changed != "digest")
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 409
    assert deployment.status == "READY_TO_BOOT"
    if inactive == "paused" and changed is not None:
        # Resuming never turns a wrong or other-boot report into this boot's proof.
        campaign.status = "ACTIVE"
        session.commit()
        assert report(session, device, release, deployment, "SUCCEEDED")[0] == 409
        assert deployment.status == "READY_TO_BOOT"
    assert not bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")


def stuck_incident(session, device, release, deployment, now=None):
    """The production state left by the pre-fix bridge at 19 October 2026 ~14:05.

    Offer 13:59:02 and staging 13:59:35 by the factory 2.5.2 boot; the 2.4.12
    boot's own reports 13:59:51, 14:01:48 and 14:01:49; its one capability
    report accepted after the 180 s window, so ADD kept only the connector
    fields; SUCCEEDED refused every minute since. Telemetry stays fresh.
    """
    for sample in session.scalars(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == device.id)):
        session.delete(sample)
    now = now or utc_now()
    offered = now - timedelta(hours=3)
    predecessor_digest = bld9_k50.FACTORY_PREDECESSOR[1]
    if device.zone_id in CITY_FACTORY_PREDECESSORS:
        predecessor_digest = CITY_FACTORY_PREDECESSORS[device.zone_id][1]
    deployment.offered_at = offered
    session.add(FirmwareEvent(deployment_id=deployment.id, state="OFFERED", details={}, created_at=offered))
    heartbeat(session, device, firmware="2.5.2", boot_id=FACTORY_BOOT, at=offered + timedelta(seconds=10))
    heartbeat(session, device, firmware="2.5.2", boot_id=FACTORY_BOOT, at=offered + timedelta(seconds=25))
    session.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT", created_at=offered + timedelta(seconds=33),
                              details={"bytes_written": release.image_size, "error_code": None,
                                       "running_version": "2.5.2", "running_partition": "factory",
                                       "image_sha256": predecessor_digest}))
    session.commit()
    # The 2.4.12 boot is first heard after the factory image staged it and rebooted.
    heartbeat(session, device, boot_id=X_BOOT, at=offered + timedelta(seconds=40))
    for seconds, state, error in ((49, "BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH"),
                                  (166, "BOOTED_PENDING", "RUNTIME_HEALTHY"), (167, "RECONCILING", None)):
        session.add(FirmwareEvent(deployment_id=deployment.id, state="LEGACY_BOOT_REPORT",
                                  created_at=offered + timedelta(seconds=seconds),
                                  details={"boot_id": X_BOOT, "reported_state": state, "error_code": error}))
    # What report_firmware_capability committed for 2.4.12's one accepted report.
    device.firmware_version, device.ota_capable, device.ota_secure_boot = "2.4.12", True, True
    device.ota_rollback_enabled, device.ota_partition_layout = True, release.partition_layout
    device.ota_running_partition, device.ota_image_sha256 = "ota_0", BRIDGE_IDENTITY[4]
    heartbeat(session, device, boot_id=X_BOOT)


def test_stuck_incident_device_completes_on_its_next_success_retry_without_reboot(legacy_bridge):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    stuck_incident(session, device, release, deployment)
    status, _response = report(session, device, release, deployment, "SUCCEEDED")
    if device.zone_id not in GATED_ZONES:
        # BLD5's offer never required the factory fields, so they cannot be attributed.
        assert status == 409 and deployment.status == "READY_TO_BOOT"
        assert not bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")
        return
    assert status == 200
    assert deployment.status == "SUCCEEDED"
    proof, = bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")
    assert proof.details["boot_id"] == X_BOOT
    assert proof.details["running_partition"] == "ota_0"
    assert proof.details["image_sha256"] == BRIDGE_IDENTITY[4]
    assert proof.details["evidence"]["source"] == "STORED_CAPABILITY"
    assert proof.details["evidence"]["previous_boot_id"] == FACTORY_BOOT
    staged, = bridge_events(session, deployment, "READY_TO_BOOT")
    first_heard = datetime.fromisoformat(proof.details["evidence"]["boot_first_heard_at"])
    assert ensure_utc(staged.created_at) < first_heard < ensure_utc(staged.created_at) + timedelta(seconds=10)
    succeeded, = bridge_events(session, deployment, "SUCCEEDED")
    assert succeeded.details["image_sha256"] == BRIDGE_IDENTITY[4]
    assert succeeded.details["running_partition"] == "ota_0"


@pytest.mark.parametrize("legacy_bridge", [bld9_k50.ZONE], indirect=True)
@pytest.mark.parametrize("changed", [
    "rebooted", "other_boot_report", "other_boot_capability", "restaged_after_reports", "staged_from_ota", "staged_with_target_digest", "staged_by_2412",
    "heard_before_staging",
    "not_staged", "not_offered", "staged_before_offer", "other_boot_ran_2412", "previous_boot_not_factory",
    "no_previous_boot", "digest", "partition", "secure_boot", "layout", "stale_telemetry",
])
def test_stored_capability_is_refused_unless_it_can_only_be_this_boot(legacy_bridge, changed):
    session, device, release, deployment, _sample = legacy_bridge
    production(session)
    stuck_incident(session, device, release, deployment)
    staged = bridge_events(session, deployment, "READY_TO_BOOT")[0]
    if changed == "rebooted":
        heartbeat(session, device, boot_id="ac276ea33328-9d00e113")
    elif changed == "other_boot_report":
        session.add(FirmwareEvent(deployment_id=deployment.id, state="LEGACY_BOOT_REPORT", details={
            "boot_id": "ac276ea33328-9d00e113", "reported_state": "BOOTED_PENDING",
            "error_code": "WAITING_FOR_RUNTIME_HEALTH"}))
    elif changed == "other_boot_capability":
        session.add(FirmwareEvent(deployment_id=deployment.id, state="LEGACY_BOOT_CAPABILITY_REPORT", details={
            "boot_id": "ac276ea33328-9d00e113", "running_partition": "ota_0", "image_sha256": BRIDGE_IDENTITY[4]}))
    elif changed == "restaged_after_reports":
        session.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT", details=staged.details))
    elif changed in {"staged_from_ota", "staged_with_target_digest", "staged_by_2412"}:
        staged.details = {**staged.details, **{
            "staged_from_ota": {"running_partition": "ota_1"},
            "staged_with_target_digest": {"image_sha256": BRIDGE_IDENTITY[4]},
            "staged_by_2412": {"running_version": "2.4.12"},
        }[changed]}
    elif changed == "heard_before_staging":
        heartbeat(session, device, boot_id=X_BOOT, at=ensure_utc(staged.created_at) - timedelta(seconds=1))
    elif changed == "not_staged":
        session.delete(staged)
    elif changed == "not_offered":
        deployment.offered_at = None
    elif changed == "staged_before_offer":
        deployment.offered_at = ensure_utc(staged.created_at) + timedelta(seconds=1)
    elif changed == "other_boot_ran_2412":
        heartbeat(session, device, boot_id="ac276ea33328-77aa0c01", at=ensure_utc(staged.created_at))
    elif changed == "previous_boot_not_factory":
        for sample in session.scalars(select(DeviceTelemetry).where(DeviceTelemetry.boot_id == FACTORY_BOOT)):
            sample.payload = {"firmware_version": "2.6.15"}
    elif changed == "no_previous_boot":
        for sample in session.scalars(select(DeviceTelemetry).where(DeviceTelemetry.boot_id == FACTORY_BOOT)):
            session.delete(sample)
    elif changed == "digest":
        device.ota_image_sha256 = "0" * 64
    elif changed == "partition":
        device.ota_running_partition = "factory"
    elif changed == "secure_boot":
        device.ota_secure_boot = False
    elif changed == "layout":
        device.ota_partition_layout = "zone-lite-ota-v0"
    else:
        elapse(session, device, deployment, 46)
    session.commit()
    if changed not in {"rebooted", "stale_telemetry"}:
        heartbeat(session, device, boot_id=X_BOOT)
    assert report(session, device, release, deployment, "SUCCEEDED") == (409, (
        "Fresh current-boot telemetry is required for the exact legacy bridge." if changed == "stale_telemetry"
        else "Signed current-boot capability proof is required before bridge completion."))
    assert deployment.status == "READY_TO_BOOT"
    assert not bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")


@pytest.fixture(params=["sqlite", "postgres"])
def k50_store(tmp_path, request):
    """Only the ZONE-BLD9-01 bridge rows, in a session configured like ADD's."""
    admin = schema = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "legacy_bridge_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=30000"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy-bridge.db'}")

    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

    request.addfinalizer(cleanup)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    request.addfinalizer(session.close)
    release = FirmwareRelease(
        release_id=BRIDGE_IDENTITY[0], version=BRIDGE_IDENTITY[1], git_sha=BRIDGE_IDENTITY[2],
        image_sha256=BRIDGE_IDENTITY[3], image_size=1_386_640, signing_key_id="fleet-key-0",
        partition_layout="zone-lite-ota-v1", minimum_bootstrap_version="2.2.0",
        storage_name="zone-lite-2.4.12/zone_lite.bin", manifest={"application_sha256": BRIDGE_IDENTITY[4]},
        manifest_signature="test-signature", state="AVAILABLE",
    )
    target = bld9_k50.TARGET
    device = Connector(
        connector_id=target.connector_id, hardware_id=target.mac, zone_id=bld9_k50.ZONE, zone_name="BLD9 K50",
        device_id="k50", display_name="ZONE-BLD9-01", firmware_family="zkt", firmware_version="2.5.2",
        connected=True, active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
        ota_rollback_enabled=True, ota_partition_layout="zone-lite-ota-v1", ota_running_partition="factory",
        ota_image_sha256=bld9_k50.FACTORY_PREDECESSOR[1], boot_id=FACTORY_BOOT,
    )
    device.zkt_device = ZKTDevice(serial=target.terminal_serial, expected_serial=target.terminal_serial,
                                  confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED")
    session.add_all([release, device])
    session.flush()
    campaign = FirmwareCampaign(
        campaign_id="zone-lite-2.4.12-bld9-k50", release_id=release.id, zone_id=bld9_k50.ZONE, status="ACTIVE",
        actor="owner", idempotency_key="bld9-k50-bridge", reason="Owner 2.4.12 bridge",
        typed_confirmation="2.4.12",
    )
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id="bld9-k50-2412", campaign_id=campaign.id, release_id=release.id, connector_id=device.id,
        status="READY_TO_BOOT", previous_version="2.5.2", target_version="2.4.12",
        bytes_written=release.image_size, attempt_count=1,
    )
    session.add(deployment)
    session.commit()
    return session, device, release, deployment


def test_k50_exact_device_sequence_completes_on_each_database(k50_store):
    session, device, release, deployment = k50_store
    heartbeat(session, device, boot_id=X_BOOT)
    assert run_2412_device(session, device, release, deployment)[-3:] == [
        ("SUCCEEDED", None, 409), ("CAPABILITY", None, 200), ("SUCCEEDED", None, 200)]
    assert deployment.status == "SUCCEEDED"


def test_k50_capability_kept_across_a_campaign_pause_on_each_database(k50_store):
    session, device, release, deployment = k50_store
    heartbeat(session, device, boot_id=X_BOOT)
    for state, error in (("BOOTED_PENDING", "RUNTIME_HEALTHY"), ("RECONCILING", None)):
        assert report(session, device, release, deployment, state, error)[0] == 202
    campaign = session.get(FirmwareCampaign, deployment.campaign_id)
    campaign.status = "PAUSED"
    session.commit()
    assert report_capability(session, device, release)[0] == 200
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 409
    campaign.status = "ACTIVE"
    session.commit()
    assert report(session, device, release, deployment, "SUCCEEDED")[0] == 200
    assert deployment.status == "SUCCEEDED"


@pytest.mark.parametrize("changed", [None, "rebooted", "other_boot_ran_2412", "digest"])
def test_k50_stuck_incident_completes_only_for_its_own_boot_on_each_database(k50_store, changed):
    session, device, release, deployment = k50_store
    stuck_incident(session, device, release, deployment)
    if changed == "rebooted":
        heartbeat(session, device, boot_id="ac276ea33328-9d00e113")
    elif changed == "other_boot_ran_2412":
        heartbeat(session, device, boot_id="ac276ea33328-77aa0c01",
                  at=ensure_utc(deployment.offered_at) + timedelta(seconds=40))
        heartbeat(session, device, boot_id=X_BOOT)
    elif changed == "digest":
        device.ota_image_sha256 = "0" * 64
        session.commit()
    status, _response = report(session, device, release, deployment, "SUCCEEDED")
    assert (status, deployment.status) == ((200, "SUCCEEDED") if changed is None else (409, "READY_TO_BOOT"))
    assert len(bridge_events(session, deployment, "LEGACY_BOOT_CAPABILITY_VERIFIED")) == (changed is None)
