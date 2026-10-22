"""Interrupted OTA attempts, after MULTAN-02's 2.6.15 -> 2.7.5 attempts on 21 Oct 2026.

Bounded resumption of a stalled 2.6.x download, the TARGET_BOOT_INTERRUPTED
classification and its evidence, the owner's step-up retry, ESP stability
preflight warnings and the 2.6.15 rollback-slot residue health latch. Each
test runs on SQLite and, when configured, PostgreSQL.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import ota
from zk_add.db import Base
from zk_add.device_health import evaluate_health
from zk_add.esp_stability import boot_history, stability_warnings, warnings_digest
from zk_add.models import (
    AuditEvent, Connector, DeviceAlert, DeviceCommand, DeviceLog, DeviceTelemetry, ZKTDevice,
)
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareDownloadGrant,
    FirmwareEvent,
    FirmwareRelease,
    assignment_for_connector,
    campaign_detail,
    control_campaign,
    create_campaign,
    preview_campaign_scope,
)
from zk_add.ota_interruption import (
    DOWNLOAD_INTERRUPTED,
    DOWNLOAD_RETRY_NOT_TAKEN,
    DOWNLOAD_RETRY_OFFERED,
    DOWNLOAD_RETRY_WITHDRAWN,
    TARGET_BOOT_INTERRUPTED,
    TARGET_BOOT_INTERRUPTED_REPEATEDLY,
    ReturnEvidencePending,
    classify_return,
    expire_held_downloads,
    retry_interrupted_target,
    retry_refusal,
)
from zk_add.schemas import HeartbeatPayload
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add import service
from zk_add.service import apply_ota_heartbeat_diagnostics, record_device_reported_return, update_heartbeat
from zk_add.settings import settings
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.time_utils import parse_datetime, utc_now
from zk_add.web import _require_ota_connector, app, get_db
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt_ota_admission import reservation_snapshot

MULTAN_01, MULTAN_02, PESHAWAR_02 = TARGETS[3], TARGETS[4], TARGETS[5]
FLEET_2615 = FLEET_BASELINE_IMAGES["2.6.15"]
APPLICATION = "e" * 64
IMAGE_SIZE = 1_572_864


def _postgres_url() -> str | None:
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    return url if url and url.startswith("postgresql") else None


@pytest.fixture(scope="module")
def postgres_engine():
    """One disposable schema per module; each test starts from empty tables."""
    url = _postgres_url()
    if url is None:
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "ota_retry_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    try:
        metadata.create_all(engine)
        yield engine, metadata
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_download_resume_enabled", True)
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
    else:
        engine, metadata = request.getfixturevalue("postgres_engine")
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE " + ", ".join(
                f'"{table.name}"' for table in metadata.sorted_tables) + " RESTART IDENTITY CASCADE"))
    session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()
        if request.param == "sqlite":
            engine.dispose()


def platform_manifest(version: str) -> dict:
    baselines, images = platform_baselines(version)
    return {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(baselines),
                          "allowed_bootstrap_images": dict(images)},
        "release_id": f"zone-lite-{version}", "version": version,
    }


def release(db, version="2.7.5") -> FirmwareRelease:
    if db.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == "zone-lite-2.6.15")) is None:
        db.add(FirmwareRelease(
            release_id="zone-lite-2.6.15", version="2.6.15", git_sha="c" * 40, image_sha256="3" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.2.0", storage_name="hil/2.6.15.bin", manifest_signature="sig",
            state="HIL_ONLY", manifest={"application_sha256": FLEET_2615}))
    signed = platform_manifest(version)
    row = FirmwareRelease(
        release_id=signed["release_id"], version=version, git_sha="a" * 40,
        image_sha256=("b" if version == "2.7.5" else "d") * 64, image_size=IMAGE_SIZE,
        signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT, minimum_bootstrap_version="2.4.12",
        storage_name=f"hil/{version}.bin", manifest_signature="sig", state="HIL_ONLY",
        manifest={**signed, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                  "_hil_targets": signed_hil_targets()})
    db.add(row)
    db.flush()
    return row


def esp(db, target, zone, *, firmware="2.6.15", slot="ota_1", boot="boot-old", family="zkt") -> Connector:
    identity = target.identity
    device = Connector(
        connector_id=identity.connector_id, hardware_id=identity.mac, zone_id=zone, zone_name=zone,
        device_id="1", display_name=target.name, firmware_version=f"zone-lite-{firmware}",
        firmware_family=family, connected=True, active=True, is_spare=False, ota_capable=True,
        ota_secure_boot=True, ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT,
        ota_running_partition=slot, ota_image_sha256=FLEET_BASELINE_IMAGES.get(firmware, FLEET_2615),
        boot_id=boot, last_seen_at=utc_now(), lifecycle_state="ONLINE")
    device.zkt_device = ZKTDevice(
        serial=identity.terminal_serial, expected_serial=identity.terminal_serial,
        confirmed_serial=identity.terminal_serial, terminal_binding_state="CONFIRMED",
        online=True, connection_state="ONLINE", last_seen_at=utc_now(), last_transition_at=utc_now())
    db.add(device)
    db.flush()
    return device


def campaign(db, rel, device, *, status="ACTIVE", deployment_status="DOWNLOADING", bytes_written=327_680,
             previous="2.6.15", error_code=None, created_at=None, completed_at=None):
    row = FirmwareCampaign(
        campaign_id=secrets.token_hex(16), release_id=rel.id, zone_id=device.zone_id, status=status,
        actor="StateHealthAdmin", idempotency_key=secrets.token_hex(8), reason="MULTAN-02 2.7.5 HIL attempt",
        typed_confirmation=rel.version, eligible_count=1, legacy_skipped_count=0)
    db.add(row)
    db.flush()
    deployment = FirmwareDeployment(
        deployment_id=secrets.token_hex(16), campaign_id=row.id, release_id=rel.id, connector_id=device.id,
        status=deployment_status, previous_version=previous, target_version=rel.version,
        bytes_written=bytes_written, attempt_count=1, error_code=error_code,
        offered_at=utc_now() - timedelta(minutes=10), completed_at=completed_at,
        **({"created_at": created_at} if created_at else {}))
    db.add(deployment)
    db.flush()
    return row, deployment


def grant(db, deployment, at):
    db.add(FirmwareDownloadGrant(token_hash=secrets.token_hex(32), deployment_id=deployment.id,
                                 connector_id=deployment.connector_id, expires_at=at + timedelta(minutes=15),
                                 created_at=at, last_used_at=at + timedelta(seconds=2)))
    db.flush()


def telemetry(db, device, *, boot, at, uptime, firmware="2.6.15", ota_state="", activity=None, sequence=1,
              target="2.7.5"):
    db.add(DeviceTelemetry(
        connector_id=device.id, boot_id=boot, sequence=sequence, uptime_seconds=uptime, created_at=at,
        current_activity=activity,
        payload={"firmware_version": firmware, "uptime_seconds": uptime,
                 "ota": {"state": ota_state, "target_version": target}}))
    db.flush()


def history(db, device, boot, *, start, minutes, firmware="2.6.15"):
    """Heartbeats every 15 s for one boot."""
    for index in range(minutes * 4):
        db.add(DeviceTelemetry(
            connector_id=device.id, boot_id=boot, sequence=index + 1, uptime_seconds=15 * index,
            created_at=start + timedelta(seconds=15 * index), payload={"firmware_version": firmware}))
    db.flush()


def stuck(firmware="2.6.15", error="DOWNLOAD_INCOMPLETE", state="DOWNLOADING", target="2.7.5",
          written=360_448, uptime=None) -> HeartbeatPayload:
    return HeartbeatPayload(firmware_version=f"zone-lite-{firmware}", uptime_seconds=uptime, ota={
        "running_version": firmware, "running_partition": "ota_1", "image_sha256": FLEET_2615,
        "capable": True, "secure_boot": True, "rollback_enabled": True, "partition_layout": ota.OTA_LAYOUT,
        "state": state, "target_version": target, "bytes_written": written, "image_size": IMAGE_SIZE,
        "last_error": error})


def boot_started(db, device):
    """The current boot's start as ADD estimates it: the smallest receipt minus uptime."""
    starts = [created_at - timedelta(seconds=uptime) for created_at, uptime in db.execute(
        select(DeviceTelemetry.created_at, DeviceTelemetry.uptime_seconds).where(
            DeviceTelemetry.connector_id == device.id, DeviceTelemetry.boot_id == device.boot_id,
            DeviceTelemetry.uptime_seconds.is_not(None)))]
    return min((parse_datetime(value.isoformat()) for value in starts), default=None)


def beat(db, device, payload):
    """The heartbeat order of update_heartbeat: OTA diagnostics first, then the sample is stored.

    A payload without an uptime gets the one the ESP would report now on its current boot.
    """
    if payload.uptime_seconds is None:
        started = boot_started(db, device) or utc_now() - timedelta(minutes=10)
        payload = payload.model_copy(update={"uptime_seconds": int((utc_now() - started).total_seconds())})
    device.last_sequence += 1
    apply_ota_heartbeat_diagnostics(db, connector=device, payload=payload)
    db.add(DeviceTelemetry(connector_id=device.id, boot_id=device.boot_id, sequence=device.last_sequence,
                           uptime_seconds=payload.uptime_seconds, created_at=utc_now(),
                           payload=payload.model_dump(mode="json")))
    db.commit()


def offer(db, device):
    """One assignment request; each device request commits, like the endpoint."""
    assignment = assignment_for_connector(db, connector=device, public_base="https://test.invalid")
    db.commit()
    return assignment


def events(db, deployment, state):
    return list(db.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == state).order_by(FirmwareEvent.id)))


def alert(db, device, code):
    return db.scalar(select(DeviceAlert).where(DeviceAlert.connector_id == device.id, DeviceAlert.code == code,
                                               DeviceAlert.state == "OPEN"))


def stalled(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    run, deployment = campaign(db, rel, device)
    now = utc_now()
    grant(db, deployment, now - timedelta(minutes=7))
    telemetry(db, device, boot=device.boot_id, at=now - timedelta(seconds=20), uptime=2380, ota_state="UPDATING")
    return rel, device, run, deployment


def expire_backoff(db, deployment):
    latest = events(db, deployment, DOWNLOAD_INTERRUPTED)[-1]
    latest.details = {**latest.details, "retry_after": (utc_now() - timedelta(seconds=1)).isoformat()}
    db.flush()


# --- A1: a stalled 2.6.x download resumes inside the same deployment ---------------------------------


def test_2615_download_stall_keeps_the_campaign_and_backs_off(db):
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    assert (deployment.status, run.status, run.pause_reason) == ("DOWNLOADING", "ACTIVE", None)
    assert deployment.error_code == "DOWNLOAD_INTERRUPTED"
    assert "attempt 1 of 3" in deployment.error_message
    # ADD's progress stays the last checkpoint the ESP reported; the heartbeat's
    # in-RAM count (where 2.6.15 resumes on this boot) is kept as evidence.
    assert deployment.bytes_written == 327_680
    [interruption] = events(db, deployment, DOWNLOAD_INTERRUPTED)
    assert interruption.details["error_code"] == "DOWNLOAD_INCOMPLETE"
    assert interruption.details["bytes_written"] == 360_448
    retry_after = parse_datetime(interruption.details["retry_after"])
    assert utc_now() + timedelta(seconds=100) < retry_after <= utc_now() + timedelta(seconds=120)
    assert device.ota_state == "UPDATING" and device.last_error_code is None
    assert alert(db, device, "OTA_DEVICE_REPORTED_FAILURE") is None
    # The sticky last_error repeats in every later heartbeat of the same attempt.
    beat(db, device, stuck())
    beat(db, device, stuck())
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 1
    assert deployment.status == "DOWNLOADING"


def test_backoff_withholds_then_offers_each_retry_once_and_exhausts_at_three(db):
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    assert offer(db, device) is None  # 204 during the backoff
    expire_backoff(db, deployment)
    first = offer(db, device)
    assert first is not None and first["deployment_id"] == deployment.deployment_id
    # A lost response is re-offered, but it is still the same attempt.
    assert offer(db, device) is not None
    assert deployment.attempt_count == 2 and len(events(db, deployment, DOWNLOAD_RETRY_OFFERED)) == 1
    # A heartbeat whose predecessor arrived before the new grant cannot end the attempt.
    beat(db, device, stuck(written=425_984))
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 1
    beat(db, device, stuck(written=425_984))
    second = events(db, deployment, DOWNLOAD_INTERRUPTED)
    assert len(second) == 2 and second[-1].details["interruption"] == 2
    assert "attempt 2 of 3" in deployment.error_message and run.status == "ACTIVE"
    assert offer(db, device) is None
    expire_backoff(db, deployment)
    assert offer(db, device) is not None and deployment.attempt_count == 3
    beat(db, device, stuck(error="DOWNLOAD_BEGIN_FAILED", written=425_984))
    assert deployment.status == "DOWNLOADING"
    beat(db, device, stuck(error="DOWNLOAD_BEGIN_FAILED", written=425_984))
    assert deployment.status == "FAILED" and deployment.error_code == "DOWNLOAD_RETRIES_EXHAUSTED"
    assert "stopped 3 times" in deployment.error_message and "DOWNLOAD_BEGIN_FAILED" in deployment.error_message
    assert run.status == "PAUSED" and "DOWNLOAD_RETRIES_EXHAUSTED" in run.pause_reason
    assert device.ota_state == "OTA_BLOCKED" and device.last_error_code == "OTA_DOWNLOAD_RETRIES_EXHAUSTED"
    assert alert(db, device, "OTA_DEVICE_REPORTED_FAILURE").details["error_code"] == "DOWNLOAD_RETRIES_EXHAUSTED"
    assert offer(db, device) is None


def test_a_fast_begin_failure_is_counted_once_a_later_heartbeat_arrives(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    run, deployment = campaign(db, rel, device, deployment_status="OFFERED", bytes_written=0)
    now = utc_now()
    telemetry(db, device, boot=device.boot_id, at=now - timedelta(seconds=40), uptime=600)
    grant(db, deployment, now - timedelta(seconds=30))
    beat(db, device, stuck(error="DOWNLOAD_BEGIN_FAILED", written=0))
    assert events(db, deployment, DOWNLOAD_INTERRUPTED) == [] and deployment.status == "OFFERED"
    beat(db, device, stuck(error="DOWNLOAD_BEGIN_FAILED", written=0))
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 1
    assert (deployment.status, run.status) == ("OFFERED", "ACTIVE")


@pytest.mark.parametrize("firmware,family,error,target", [
    ("2.4.12", "zkt", "DOWNLOAD_BEGIN_FAILED", "2.7.5"),
    ("2.5.2", "zkt", "DOWNLOAD_BEGIN_FAILED", "2.7.5"),
    ("2.6.23", "zkt", "DOWNLOAD_INCOMPLETE", "2.7.5"),
    ("2.7.5", "zkt", "DOWNLOAD_INCOMPLETE", "2.7.6"),  # e.g. the Peshawar-02 2.7.6 HIL
    ("2.6.15", "zkt", "IMAGE_HASH_MISMATCH", "2.7.5"),
    ("2.6.15", "zkt", "DOWNLOAD_OR_SIGNATURE_FAILED", "2.7.5"),
    ("2.6.15", "hikvision", "DOWNLOAD_INCOMPLETE", "2.7.5"),
])
def test_every_other_download_error_still_fails_closed_at_once(db, firmware, family, error, target):
    rel = release(db, target)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", firmware=firmware, family=family)
    run, deployment = campaign(db, rel, device, previous=firmware)
    now = utc_now()
    grant(db, deployment, now - timedelta(minutes=7))
    telemetry(db, device, boot=device.boot_id, at=now - timedelta(seconds=20), uptime=2380, firmware=firmware)
    beat(db, device, stuck(firmware=firmware, error=error, target=target))
    assert (deployment.status, deployment.error_code) == ("FAILED", error)
    assert run.status == "PAUSED" and error in run.pause_reason
    assert events(db, deployment, DOWNLOAD_INTERRUPTED) == []


# --- A1b and A2: an interrupted target boot --------------------------------------------------------


def booted_target(db, *, lifetime=47, last_code="WAITING_FOR_RUNTIME_HEALTH", target_boot_logs=False,
                  previous="2.6.15", target="2.7.5"):
    rel = release(db, target)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="boot-old", firmware=previous)
    peer = esp(db, MULTAN_01, "ZONE-MULTAN-01", boot="peer-a", firmware="2.7.5", slot="ota_0")
    run, deployment = campaign(db, rel, device, status="ACTIVE", deployment_status="BOOTED_PENDING",
                               bytes_written=IMAGE_SIZE, previous=previous)
    now = utc_now()
    returned = now - timedelta(seconds=43)
    started = returned - timedelta(seconds=lifetime)
    # Seven days on 2.6.15: three earlier 20-minute boots, then the pre-install boot.
    for index, boot in enumerate(("h1", "h2", "h3")):
        history(db, device, boot, start=now - timedelta(days=3 - index), minutes=20, firmware=previous)
    history(db, device, "boot-old", start=started - timedelta(minutes=30), minutes=29, firmware=previous)
    telemetry(db, device, boot="boot-old", at=started - timedelta(seconds=5), uptime=1795, activity="LIVE_CAPTURE",
              firmware=previous)
    db.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT", created_at=started - timedelta(seconds=30),
                         details={"bytes_written": IMAGE_SIZE}))
    db.add(FirmwareEvent(deployment_id=deployment.id, state="BOOTED_PENDING", created_at=started + timedelta(seconds=28),
                         details={"bytes_written": IMAGE_SIZE, "error_code": last_code, "running_version": target,
                                  "running_partition": "ota_0", "image_sha256": APPLICATION}))
    if target_boot_logs:
        db.add(DeviceLog(connector_id=device.id, boot_id="boot-new", sequence=9, level="INFO", subsystem="reconcile",
                         code="FULL_RECONCILE", message="reconcile", received_at=returned - timedelta(seconds=12)))
        telemetry(db, device, boot="boot-new", at=returned - timedelta(seconds=5), uptime=lifetime - 5,
                  firmware=target, activity="FULL_RECONCILE")
    db.add(DeviceLog(connector_id=device.id, boot_id="boot-ret", sequence=4, level="INFO", subsystem="zkt",
                     code="ZKT_TIME_SYNC_OK", message="time set", device_time=returned + timedelta(seconds=22),
                     received_at=returned + timedelta(seconds=23)))
    # MULTAN-01 booted 60 s after MULTAN-02's reset.
    telemetry(db, peer, boot="peer-b", at=returned + timedelta(seconds=75), uptime=15, firmware="2.7.5")
    # The return boot's first heartbeat is the latest sample before this one.
    telemetry(db, device, boot="boot-ret", at=now - timedelta(seconds=30), uptime=13, firmware=previous,
              target=target)
    device.boot_id, device.last_sequence = "boot-ret", 1
    db.commit()
    db.info["booted_at"] = now
    return rel, device, run, deployment


def returned_heartbeat(db, device, previous="2.6.15"):
    """The return boot's second heartbeat, received exactly one second after booted_target's clock.

    ADD dates the inferred return from its own receipt, so a fixed receipt
    keeps every derived figure exact however long the database setup took.
    """
    with mock.patch.object(ota, "utc_now", lambda: db.info["booted_at"] + timedelta(seconds=1)):
        beat(db, device, HeartbeatPayload(firmware_version=f"zone-lite-{previous}", uptime_seconds=43, ota={
            "running_version": previous, "running_partition": "ota_1",
            "image_sha256": FLEET_BASELINE_IMAGES.get(previous, FLEET_2615)}))
    db.commit()


def returned_at(db):
    """The return boot's start as ADD estimates it after returned_heartbeat."""
    return db.info["booted_at"] + timedelta(seconds=1) - timedelta(seconds=43)


def test_short_unreported_target_boot_is_classified_interrupted_with_evidence(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", TARGET_BOOT_INTERRUPTED)
    assert "before the image was confirmed" in deployment.error_message
    assert run.status == "PAUSED" and TARGET_BOOT_INTERRUPTED in run.pause_reason
    assert device.last_error_code == "OTA_TARGET_BOOT_INTERRUPTED"
    row = alert(db, device, "OTA_DEVICE_ROLLED_BACK")
    assert row.details["classification"] == TARGET_BOOT_INTERRUPTED
    assert row.details["reset_cause"] == "not_reported"
    diagnosis = row.details["diagnosis"]
    target = diagnosis["target_boot"]
    assert target["estimated_lifetime_seconds"] == {"min": 20, "max": 53}
    assert target["failure_reports"] == [] and target["logs_received"] == 0 and target["telemetry_received"] == 0
    assert target["last_progress_code"] == "WAITING_FOR_RUNTIME_HEALTH" and target["progress_reports"] == 1
    assert diagnosis["previous_firmware_resets"]["boot_ids"] == 4
    assert diagnosis["previous_firmware_resets"]["resets"] == 3
    assert diagnosis["terminal"]["answered_within_30_seconds"] is True
    assert diagnosis["terminal"]["time_sync_after_new_boot_seconds"] == 22
    location = diagnosis["location_resets"]
    assert location["location"] == "MULTAN" and location["peers_checked"] == 1
    assert [(item["boot_id"], item["offset_seconds"]) for item in location["resets"]] == [("peer-b", 59)]
    assert diagnosis["reconcile_at_reset"]["running"] is None
    assert diagnosis["reconcile_at_reset"]["previous_firmware_activity_before_install"] == "LIVE_CAPTURE"
    assert "SITE_POWER" not in str(diagnosis)
    stored = events(db, deployment, "ROLLED_BACK")[-1]
    assert stored.details["diagnosis"]["classification"] == TARGET_BOOT_INTERRUPTED
    assert stored.details["return_evidence"]["boot_id"] == "boot-ret"
    health = evaluate_health(db, device)
    assert health.last_error_code == "OTA_TARGET_BOOT_INTERRUPTED"
    reason = next(item for item in health.reasons if item.code == "OTA_DEVICE_ROLLED_BACK")
    assert reason.tier == "WARNING" and "before the ESP returned to 2.6.15" in reason.evidence_summary


def test_target_boot_logs_and_reconcile_activity_are_reported(db):
    _rel, device, _run, deployment = booted_target(db, target_boot_logs=True)
    returned_heartbeat(db, device)
    diagnosis = alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["diagnosis"]
    assert diagnosis["target_boot"]["logs_received"] == 1 and diagnosis["target_boot"]["telemetry_received"] == 1
    assert diagnosis["target_boot"]["boot_ids"] == ["boot-new"]
    assert diagnosis["reconcile_at_reset"]["running"] is True
    assert diagnosis["reconcile_at_reset"]["last_reconcile_log"]["code"] == "FULL_RECONCILE"
    assert deployment.error_code == TARGET_BOOT_INTERRUPTED


@pytest.mark.parametrize("lifetime,last_code,reason", [
    (400, "WAITING_FOR_RUNTIME_HEALTH", "TARGET_LIFETIME_NOT_SHORT"),
    (47, "RUNTIME_HEALTHY", "TARGET_REPORTED_RUNTIME_HEALTHY"),
])
def test_other_returns_keep_previous_firmware_observed(db, lifetime, last_code, reason):
    _rel, device, run, deployment = booted_target(db, lifetime=lifetime, last_code=last_code)
    returned_heartbeat(db, device)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", "PREVIOUS_FIRMWARE_OBSERVED")
    row = alert(db, device, "OTA_DEVICE_ROLLED_BACK")
    assert row.details["classification"] == "UNCLASSIFIED"
    assert reason in row.details["diagnosis"]["unclassified_reasons"]
    assert evaluate_health(db, device).last_error_code == "OTA_PREVIOUS_FIRMWARE_OBSERVED"
    assert run.status == "PAUSED"


@pytest.mark.parametrize("version", ["2.6.17", "2.6.24", "2.7.0", "2.7.6"])
def test_releases_with_their_own_return_evidence_are_never_classified(version):
    deployment = SimpleNamespace(status="BOOTED_PENDING", previous_version="2.6.15")
    evidence = {"target_boot": {"last_progress_code": None, "failure_reports": [],
                                "estimated_lifetime_seconds": {"min": 10, "max": 40}}}
    classification, reasons = classify_return(deployment, SimpleNamespace(version=version), evidence)
    assert (classification, reasons) == ("UNCLASSIFIED", ["TARGET_HAS_OWN_RETURN_EVIDENCE"])
    assert classify_return(deployment, SimpleNamespace(version="2.7.5"), evidence)[0] == TARGET_BOOT_INTERRUPTED


# --- A1b: the owner's step-up retry ---------------------------------------------------------------


def admin_client(db):
    raw, admin = create_admin_session(db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest")
    db.commit()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw)
    return client, {"X-CSRF-Token": admin.csrf_token}


def retry_body(deployment, **changes):
    return {"deployment_id": deployment.deployment_id, "reason": "Site replaced the ESP power supply.",
            "typed_confirmation": "2.7.5", "password": "correct-password", **changes}


def retry_row(db, run, deployment):
    detail = campaign_detail(db, run.campaign_id)
    return next(item for item in detail["deployments"] if item["deployment_id"] == deployment.deployment_id)


def test_owner_retry_needs_step_up_typed_version_and_override_then_adds_one_attempt(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    detail = campaign_detail(db, run.campaign_id)
    row = next(item for item in detail["deployments"] if item["deployment_id"] == deployment.deployment_id)
    assert row["rollback_diagnosis"]["classification"] == TARGET_BOOT_INTERRUPTED
    assert row["retry"]["available"] is True and row["retry"]["interrupted_boots"] == 1
    assert "stability_warnings" not in row["retry"]  # the 7-day scan runs only in the owner's review
    client, headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    review = client.get(path, params={"deployment_id": deployment.deployment_id})
    assert review.status_code == 200, review.text
    review = review.json()
    assert review["available"] is True and review["pending_deployments"] == 0
    assert {warning["code"] for warning in review["stability_warnings"]} == {"ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    assert review["esp_state"]["connected"] is True and review["esp_state"]["boot_id"] == "boot-ret"
    assert client.post(path, json=retry_body(deployment, password="wrong"), headers=headers).status_code == 403
    assert client.post(path, json=retry_body(deployment), headers={}).status_code == 403  # CSRF
    wrong = client.post(path, json=retry_body(deployment, typed_confirmation="2.7.6"), headers=headers)
    assert wrong.status_code == 409 and "Typed firmware version" in wrong.json()["detail"]
    unconfirmed = client.post(path, json=retry_body(deployment), headers=headers)
    assert unconfirmed.status_code == 409 and "override" in unconfirmed.json()["detail"]
    # The override names exactly the warnings the owner reviewed.
    stale = client.post(path, json=retry_body(deployment, override_stability_warnings=True,
                                              stability_warnings_digest="0" * 64), headers=headers)
    assert stale.status_code == 409 and "changed since" in stale.json()["detail"]
    assert run.status == "PAUSED"
    digest = review["stability_warnings_digest"]
    response = client.post(path, json=retry_body(deployment, override_stability_warnings=True,
                                                 stability_warnings_digest=digest), headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ACTIVE" and body["retry_of"] == deployment.deployment_id
    db.expire_all()
    retry = db.scalar(select(FirmwareDeployment).where(FirmwareDeployment.deployment_id == body["deployment_id"]))
    assert (retry.status, retry.campaign_id, retry.connector_id) == ("PENDING", run.id, device.id)
    assert retry.target_version == "2.7.5" and retry.previous_version == "zone-lite-2.6.15"
    [requested] = events(db, retry, "RETRY_REQUESTED")
    assert {item["code"] for item in requested.details["stability_warnings_overridden"]} == {
        "ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "FIRMWARE_CAMPAIGN_RETRY_TARGET"))
    assert audit is not None and audit.actor == "StateHealthAdmin"
    campaign_row = db.scalar(select(FirmwareCampaign).where(FirmwareCampaign.id == run.id))
    assert (campaign_row.status, campaign_row.pause_reason) == ("ACTIVE", None)
    again = client.post(path, json=retry_body(deployment, override_stability_warnings=True,
                                              stability_warnings_digest=digest), headers=headers)
    assert again.status_code == 409
    assert db.scalar(select(func.count(FirmwareDeployment.id)).where(FirmwareDeployment.campaign_id == run.id)) == 2
    # The new attempt is offered only through the ordinary admission checks.
    device = db.get(Connector, device.id)
    assignment = offer(db, device)
    assert assignment is not None and assignment["deployment_id"] == retry.deployment_id


def test_retry_is_refused_for_an_unclassified_return_and_after_two_interrupted_boots(db):
    rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    client, headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    # An earlier interrupted boot of the same release on this ESP reaches the limit.
    _other, earlier = campaign(db, rel, device, status="CANCELLED", deployment_status="ROLLED_BACK",
                               bytes_written=IMAGE_SIZE, error_code=TARGET_BOOT_INTERRUPTED)
    db.commit()
    limited = client.post(path, json=retry_body(deployment, override_stability_warnings=True), headers=headers)
    assert limited.status_code == 409 and "UART" in limited.json()["detail"]
    earlier.error_code = "PREVIOUS_FIRMWARE_OBSERVED"
    deployment.error_code = "PREVIOUS_FIRMWARE_OBSERVED"
    db.commit()
    unclassified = client.post(path, json=retry_body(deployment, override_stability_warnings=True), headers=headers)
    assert unclassified.status_code == 409 and TARGET_BOOT_INTERRUPTED in unclassified.json()["detail"]
    missing = client.post(f"/api/v1/firmware/campaigns/{secrets.token_hex(16)}/retry-target",
                          json=retry_body(deployment), headers=headers)
    assert missing.status_code == 404
    assert run.status == "PAUSED"


# --- A3: stability preflight -----------------------------------------------------------------------


def test_boot_history_counts_resets_online_time_and_firmware(db):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    now = utc_now()
    history(db, device, "a", start=now - timedelta(hours=5), minutes=60)
    # A 30-minute silence inside one boot is not online time.
    history(db, device, "b", start=now - timedelta(hours=3), minutes=30)
    history(db, device, "b", start=now - timedelta(hours=2), minutes=30)
    history(db, device, "c", start=now - timedelta(hours=1), minutes=30, firmware="2.7.5")
    result = boot_history(db, device, until=now, firmware_version="2.6.15")
    assert (result["boot_ids"], result["resets"]) == (2, 1)
    assert 1.9 <= result["online_hours"] <= 2.0
    assert result["reset_rate_per_online_hour"] == pytest.approx(1 / result["online_hours"], abs=0.01)
    everything = boot_history(db, device, until=now)
    assert (everything["boot_ids"], everything["resets"]) == (3, 2)


def test_stability_warnings_follow_the_owner_thresholds(db):
    stable = esp(db, MULTAN_01, "ZONE-MULTAN-01", boot="steady")
    now = utc_now()
    history(db, stable, "steady", start=now - timedelta(hours=3), minutes=180)
    assert stability_warnings(db, stable) == []
    shaky = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="s4")
    for index in range(4):
        history(db, shaky, f"s{index + 1}", start=now - timedelta(minutes=80 - 20 * index), minutes=15)
    codes = {warning["code"]: warning for warning in stability_warnings(db, shaky)}
    assert set(codes) == {"ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    assert codes["ESP_RESET_RATE_HIGH"]["reset_rate_per_online_hour"] > 0.5
    assert codes["ESP_UPTIME_SHORT"]["uptime_seconds"] < 30 * 60
    shaky.connected = False
    assert {warning["code"] for warning in stability_warnings(db, shaky)} == {"ESP_RESET_RATE_HIGH"}


def test_campaign_creation_needs_the_owner_override_for_stability_warnings(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="s3")
    now = utc_now()
    for index in range(3):
        history(db, device, f"s{index + 1}", start=now - timedelta(minutes=50 - 20 * index), minutes=10)
    scope = preview_campaign_scope(db, release_public_id=rel.release_id, zone_id=device.zone_id)
    assert {warning["code"] for warning in scope["stability_warnings"]} == {"ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    arguments = dict(release_public_id=rel.release_id, zone_id=device.zone_id, reason="Retry 2.7.5 at MULTAN-02",
                     typed_confirmation="2.7.5", actor="StateHealthAdmin", scope_token=scope["scope_token"])
    with pytest.raises(ValueError, match="override"):
        create_campaign(db, idempotency_key="stability-refused", **arguments)
    # An override of other warnings than the preview showed is refused.
    with pytest.raises(ValueError, match="changed since the preview"):
        create_campaign(db, idempotency_key="stability-stale", override_stability_warnings=True,
                        stability_warnings_digest="0" * 64, **arguments)
    run = create_campaign(db, idempotency_key="stability-override", override_stability_warnings=True,
                          stability_warnings_digest=scope["stability_warnings_digest"], **arguments)
    db.flush()
    deployment = db.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    [record] = events(db, deployment, "STABILITY_WARNINGS_OVERRIDDEN")
    assert {item["code"] for item in record.details["warnings"]} == {"ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}


def test_a_stable_device_needs_no_override(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="steady")
    history(db, device, "steady", start=utc_now() - timedelta(hours=2), minutes=120)
    scope = preview_campaign_scope(db, release_public_id=rel.release_id, zone_id=device.zone_id)
    assert scope["stability_warnings"] == []
    run = create_campaign(db, release_public_id=rel.release_id, zone_id=device.zone_id, reason="Stable 2.7.5 HIL",
                          typed_confirmation="2.7.5", actor="StateHealthAdmin", scope_token=scope["scope_token"],
                          idempotency_key="stable-device")
    assert run.status == "ACTIVE" and run.eligible_count == 1


# --- A4: the 2.6.15 rollback-slot residue ----------------------------------------------------------


def residue_storage(**fields):
    return {"durability": "DEGRADED", "persistence_verified": False, "recovery_complete": True,
            "write_failures": 0, "read_failures": 0, "persistence_probe_failures": 0,
            "local_failure_source": "zone_lite.c:3196",
            "upgrade_error": "STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED", "upgrade_ready": False,
            "used_bytes": 116, "total_bytes": 1000, **fields}


def residue_record(db, device, *, written=360_448, accepted="2.6.15"):
    base = release(db, "2.7.5")
    accepted_release = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == accepted))
    now = utc_now()
    campaign(db, accepted_release, device, status="COMPLETED", deployment_status="SUCCEEDED",
             bytes_written=1024, previous="2.5.2", created_at=now - timedelta(days=9),
             completed_at=now - timedelta(days=8))
    campaign(db, base, device, status="CANCELLED", deployment_status="FAILED", bytes_written=written,
             error_code="DOWNLOAD_INCOMPLETE", created_at=now - timedelta(hours=5))
    db.commit()


def residue_heartbeat(db, device, sequence, *, storage=None, firmware="2.6.15", image=FLEET_2615):
    update_heartbeat(db, connector=device, boot_id="boot-res", sequence=sequence, payload=HeartbeatPayload(
        firmware_version=f"zone-lite-{firmware}", uptime_seconds=600 + sequence * 15, led_state="LOCAL_FAILURE",
        zkt={"online": True, "connection_state": "ONLINE", "serial": MULTAN_02.identity.terminal_serial},
        ota={"running_version": firmware, "running_partition": "ota_1", "image_sha256": image},
        diagnostics={"storage": storage or residue_storage(), "workers": [
            {"name": "add_delivery", "state": "RUNNING", "last_activity_uptime_ms": (600 + sequence * 15) * 1000},
            {"name": "ords_delivery", "state": "RUNNING", "last_activity_uptime_ms": (600 + sequence * 15) * 1000}]}))
    db.flush()


def test_rollback_slot_residue_is_labelled_but_stays_degraded(db):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    residue_record(db, device)
    residue_heartbeat(db, device, 1)
    durability = alert(db, device, "ESP_DURABILITY_FAULT")
    latch = durability.details["latch"]
    assert latch["kind"] == "OTA_ROLLBACK_SLOT_RESIDUE" and latch["source"] == "zone_lite.c:3196"
    assert latch["residue"]["target_version"] == "2.7.5" and latch["residue"]["bytes_written"] == 360_448
    assert durability.message.startswith("Persistence unverified - OTA rollback slot residue")
    assert alert(db, device, "ESP_LOCAL_FAILURE").details["latch"]["kind"] == "OTA_ROLLBACK_SLOT_RESIDUE"
    health = evaluate_health(db, device)
    # Only the code and message name the residue: 2.6.15 reports the first
    # latch's source only, so a later storage fault on this boot is invisible.
    assert {reason.code: reason.tier for reason in health.reasons}["ESP_DURABILITY_FAULT"] == "DEGRADED"
    assert {reason.code: reason.error_code for reason in health.reasons} == {
        "ESP_DURABILITY_FAULT": "OTA_ROLLBACK_SLOT_RESIDUE", "ESP_LOCAL_FAILURE": "OTA_ROLLBACK_SLOT_RESIDUE"}
    assert (health.derived_lifecycle, health.last_error_code) == ("DEGRADED", "OTA_ROLLBACK_SLOT_RESIDUE")
    assert any("persistence unverified - ota rollback slot residue" in (reason.evidence_summary or "").lower()
               for reason in health.reasons)
    # Any later storage I/O failure on the same boot removes the latch.
    residue_heartbeat(db, device, 2, storage=residue_storage(write_failures=1, error_code=5,
                                                              error_operation="local_write_commit"))
    assert "latch" not in alert(db, device, "ESP_DURABILITY_FAULT").details
    assert "latch" not in alert(db, device, "ESP_LOCAL_FAILURE").details
    assert evaluate_health(db, device).derived_lifecycle == "DEGRADED"


@pytest.mark.parametrize("change", [
    {"storage": {"write_failures": 1}},
    {"storage": {"read_failures": 2}},
    {"storage": {"persistence_probe_failures": 1}},
    {"storage": {"legacy_append_faults": 1}},
    {"storage": {"error_code": 5, "error_operation": "local_write_commit"}},
    {"storage": {"persistence_probe_error": 28}},
    {"storage": {"local_failure_source": "add_connector.c:2835"}},
    {"storage": {"local_failure_last_source": "zone_lite.c:7416"}},
    {"storage": {"upgrade_error": "STORAGE_DIRECT_BASELINE_UNQUALIFIED"}},
    {"storage": {"recovery_complete": False}},
    {"storage": {"fault_class": "CORRUPTION"}},
    {"firmware": "2.6.14"},
    {"image": "f" * 64},
])
def test_rollback_slot_residue_gates_keep_degraded(db, change):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    residue_record(db, device)
    residue_heartbeat(db, device, 1, storage=residue_storage(**change.get("storage", {})),
                      firmware=change.get("firmware", "2.6.15"), image=change.get("image", FLEET_2615))
    durability = alert(db, device, "ESP_DURABILITY_FAULT")
    assert durability is not None and (durability.details.get("latch") or {}).get("kind") != "OTA_ROLLBACK_SLOT_RESIDUE"
    assert evaluate_health(db, device).derived_lifecycle == "DEGRADED"


@pytest.mark.parametrize("record", ["none", "no_bytes", "not_after_2615"])
def test_rollback_slot_residue_needs_adds_record(db, record):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    if record == "no_bytes":
        residue_record(db, device, written=0)
    elif record == "not_after_2615":
        rel = release(db, "2.7.5")
        now = utc_now()
        campaign(db, rel, device, status="CANCELLED", deployment_status="FAILED", bytes_written=360_448,
                 created_at=now - timedelta(days=20))
        accepted = db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.6.15"))
        campaign(db, accepted, device, status="COMPLETED", deployment_status="SUCCEEDED", bytes_written=1024,
                 previous="2.5.2", created_at=now - timedelta(days=9), completed_at=now - timedelta(days=8))
        db.commit()
    residue_heartbeat(db, device, 1)
    assert "latch" not in alert(db, device, "ESP_DURABILITY_FAULT").details
    assert evaluate_health(db, device).derived_lifecycle == "DEGRADED"


# --- Review fixes: a held download is bounded and fails closed --------------------------------------


def reserved(db, deployment):
    return deployment.deployment_id in {row["deployment_id"] for row in reservation_snapshot(db)["reservations"]}


@pytest.mark.parametrize("action,status", [("pause", "PAUSED"), ("cancel", "CANCELLED")])
def test_pause_or_cancel_during_the_backoff_fails_the_held_download_closed(db, action, status):
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    assert deployment.status == "DOWNLOADING" and reserved(db, deployment)
    _campaign, failed = control_campaign(db, campaign_public_id=run.campaign_id, action=action,
                                         reason="Owner stop for the site visit")
    db.commit()
    assert failed == 1
    # FAILED, never CANCELLED: no grant was outstanding, so no uncertain reservation stays.
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert f"the campaign was {status.lower()}" in deployment.error_message
    assert (run.status, run.pause_reason) == (status, "Owner stop for the site visit")
    assert device.last_error_code == "OTA_DOWNLOAD_RETRY_WITHDRAWN" and device.ota_state == "OTA_BLOCKED"
    assert not reserved(db, deployment)
    beat(db, device, stuck())
    assert deployment.status == "FAILED" and offer(db, device) is None


def test_a_downloading_attempt_with_a_grant_outstanding_is_still_cancelled(db):
    _rel, device, run, deployment = stalled(db)
    _campaign, failed = control_campaign(db, campaign_public_id=run.campaign_id, action="cancel",
                                         reason="Owner cancel mid-download")
    db.commit()
    assert failed == 0 and deployment.status == "CANCELLED" and run.status == "CANCELLED"


def test_a_sticky_error_after_a_pause_fails_closed_as_before(db):
    _rel, device, run, deployment = stalled(db)
    run.status = "PAUSED"
    db.commit()
    beat(db, device, stuck())
    assert (deployment.status, deployment.error_code) == ("FAILED", "DOWNLOAD_INCOMPLETE")
    assert events(db, deployment, DOWNLOAD_INTERRUPTED) == []


def test_revoking_the_release_fails_a_held_download(db):
    rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    client, headers = admin_client(db)
    response = client.post(f"/api/v1/firmware/releases/{rel.release_id}/revoke", headers=headers,
                           json={"reason": "Signing key rotation for 2.7.5", "password": "correct-password"})
    assert response.status_code == 200, response.text
    db.expire_all()
    deployment = db.get(FirmwareDeployment, deployment.id)
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert "release was revoked" in deployment.error_message
    campaign_row = db.get(FirmwareCampaign, run.id)
    assert campaign_row.status == "PAUSED" and campaign_row.pause_reason.startswith("Release revoked:")
    assert not reserved(db, deployment)


@pytest.mark.parametrize("expired", [True, False])
def test_an_admission_refusal_fails_the_held_download_with_its_reason(db, expired):
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    # The running image no longer matches the qualified predecessor.
    device.ota_image_sha256 = "f" * 64
    if expired:
        expire_backoff(db, deployment)
    db.commit()
    assert offer(db, device) is None
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert "admission refused (DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED)" in deployment.error_message
    assert run.status == "PAUSED" and events(db, deployment, DOWNLOAD_RETRY_OFFERED) == []


def test_a_re_offer_not_taken_within_fifteen_minutes_fails_the_deployment(db):
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    latest = events(db, deployment, DOWNLOAD_INTERRUPTED)[-1]
    latest.details = {**latest.details, "retry_after": (utc_now() - timedelta(minutes=14)).isoformat()}
    db.commit()
    beat(db, device, stuck())
    assert deployment.status == "DOWNLOADING"
    latest.details = {**latest.details, "retry_after": (utc_now() - timedelta(minutes=16)).isoformat()}
    db.commit()
    beat(db, device, stuck())
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_NOT_TAKEN)
    assert "did not take ADD's re-offer within 15 min" in deployment.error_message
    assert run.status == "PAUSED" and not reserved(db, deployment)


# --- Review fixes: the ESP's own BOOTLOADER_ROLLBACK report ---------------------------------------


def device_report(deployment, previous="2.6.15", **changes):
    return {"state": "ROLLED_BACK", "bytes_written": IMAGE_SIZE, "running_version": previous,
            "running_partition": "ota_1", "image_sha256": FLEET_BASELINE_IMAGES.get(previous, FLEET_2615),
            "error_code": "BOOTLOADER_ROLLBACK", **changes}


def device_client(db, device):
    async def override():
        return db, device

    app.dependency_overrides[_require_ota_connector] = override
    return TestClient(app)


def test_the_esps_own_return_report_gets_the_same_classification(db):
    _rel, device, run, deployment = booted_target(db)
    client = device_client(db, device)
    response = client.post(f"/device/v2/firmware/deployments/{deployment.deployment_id}/progress",
                           json=device_report(deployment))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ROLLED_BACK"
    db.expire_all()
    deployment = db.get(FirmwareDeployment, deployment.id)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", TARGET_BOOT_INTERRUPTED)
    stored = events(db, deployment, "ROLLED_BACK")[-1].details
    assert stored["device_error_code"] == "BOOTLOADER_ROLLBACK"
    assert stored["return_evidence"]["source"] == "device_reported_bootloader_rollback"
    lifetime = stored["diagnosis"]["target_boot"]["estimated_lifetime_seconds"]
    assert 19 <= lifetime["min"] <= 20 and 52 <= lifetime["max"] <= 53
    device = db.get(Connector, device.id)
    assert device.last_error_code == "OTA_TARGET_BOOT_INTERRUPTED"
    assert alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["classification"] == TARGET_BOOT_INTERRUPTED
    assert db.get(FirmwareCampaign, run.id).status == "PAUSED"
    assert retry_row(db, run, deployment)["retry"]["available"] is True
    # The heartbeat path that arrives later changes nothing.
    returned_heartbeat(db, device)
    assert len(events(db, deployment, "ROLLED_BACK")) == 1
    assert db.scalar(select(func.count(DeviceAlert.id)).where(
        DeviceAlert.connector_id == device.id, DeviceAlert.code == "OTA_DEVICE_ROLLED_BACK")) == 1


def test_a_return_report_before_the_first_return_heartbeat_waits_bounded(db):
    _rel, device, run, deployment = booted_target(db)
    device.boot_id = "boot-ret-2"  # a newer boot that has sent no heartbeat yet
    db.commit()
    client = device_client(db, device)
    path = f"/device/v2/firmware/deployments/{deployment.deployment_id}/progress"
    waiting = client.post(path, json=device_report(deployment))
    assert waiting.status_code == 409 and "RETURN_EVIDENCE_PENDING" in waiting.json()["detail"]
    db.expire_all()
    assert db.get(FirmwareDeployment, deployment.id).status == "BOOTED_PENDING"
    with pytest.raises(ReturnEvidencePending):
        record_device_reported_return(db, connector=db.get(Connector, device.id),
                                      deployment_public_id=deployment.deployment_id, bytes_written=IMAGE_SIZE,
                                      running_version="2.6.15", running_partition="ota_1",
                                      image_sha256=FLEET_2615, error_code="BOOTLOADER_ROLLBACK", error_message=None)
    db.rollback()
    # Ten minutes after the target's last report it is recorded as the ESP said.
    for event in events(db, deployment, "BOOTED_PENDING"):
        event.created_at = utc_now() - timedelta(minutes=11)
    db.commit()
    accepted = client.post(path, json=device_report(deployment))
    assert accepted.status_code == 200, accepted.text
    db.expire_all()
    deployment = db.get(FirmwareDeployment, deployment.id)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", "BOOTLOADER_ROLLBACK")


@pytest.mark.parametrize("previous,target", [("2.5.2", "2.7.6"), ("2.5.2", "2.7.5")])
def test_returns_outside_the_verified_scope_are_unchanged(db, previous, target):
    _rel, device, run, deployment = booted_target(db, previous=previous, target=target)
    assert record_device_reported_return(
        db, connector=device, deployment_public_id=deployment.deployment_id, bytes_written=IMAGE_SIZE,
        running_version=previous, running_partition="ota_1", image_sha256=FLEET_BASELINE_IMAGES[previous],
        error_code="BOOTLOADER_ROLLBACK", error_message=None) is None
    db.rollback()
    # The heartbeat-inferred return is recorded as before, with evidence and no retry.
    returned_heartbeat(db, device, previous)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", "PREVIOUS_FIRMWARE_OBSERVED")
    diagnosis = alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["diagnosis"]
    assert "PREVIOUS_FIRMWARE_OUT_OF_SCOPE" in diagnosis["unclassified_reasons"]
    assert ("TARGET_HAS_OWN_RETURN_EVIDENCE" in diagnosis["unclassified_reasons"]) is (target == "2.7.6")
    assert retry_row(db, run, deployment)["retry"] is None
    # Even a record labelled interrupted is never retried from 2.4.12 or 2.5.2.
    deployment.error_code = TARGET_BOOT_INTERRUPTED
    run.status = "PAUSED"
    refusal = retry_refusal(db, run, deployment)
    assert refusal and ("owner decision 6" in refusal or "own return evidence" in refusal)


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_the_heartbeat_path_never_relabels_a_return_the_esp_reported_meanwhile(db, monkeypatch):
    _rel, device, run, deployment = booted_target(db)
    original = ota.previous_firmware_return_evidence

    def racing(session, **kwargs):
        evidence = original(session, **kwargs)
        with Session(bind=db.get_bind(), expire_on_commit=False, autoflush=False) as other:
            record_device_reported_return(
                other, connector=other.get(Connector, device.id), deployment_public_id=deployment.deployment_id,
                bytes_written=IMAGE_SIZE, running_version="2.6.15", running_partition="ota_1",
                image_sha256=FLEET_2615, error_code="BOOTLOADER_ROLLBACK", error_message=None)
            other.commit()
        return evidence

    monkeypatch.setattr(ota, "previous_firmware_return_evidence", racing)
    returned_heartbeat(db, device)
    db.expire_all()
    rolled = events(db, db.get(FirmwareDeployment, deployment.id), "ROLLED_BACK")
    assert len(rolled) == 1 and rolled[0].details["return_evidence"]["source"] == "device_reported_bootloader_rollback"
    assert db.scalar(select(func.count(DeviceAlert.id)).where(
        DeviceAlert.connector_id == device.id, DeviceAlert.code == "OTA_DEVICE_ROLLED_BACK")) == 1


# --- Review fixes: evidence ------------------------------------------------------------------------


def test_the_time_sync_uses_adds_receipt_not_the_esps_clock(db):
    _rel, device, _run, deployment = booted_target(db)
    sync = db.scalar(select(DeviceLog).where(DeviceLog.code == "ZKT_TIME_SYNC_OK"))
    sync.device_time = sync.received_at - timedelta(hours=1)  # an ESP clock an hour behind
    db.commit()
    returned_heartbeat(db, device)
    terminal = alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["diagnosis"]["terminal"]
    assert terminal["time_sync_after_new_boot_seconds"] == 22
    assert terminal["answered_within_30_seconds"] is True
    assert (terminal["source"], terminal["bound"]) == ("received_at", "upper")


@pytest.mark.parametrize("received_after,expected", [(-60, (None, None, "inconsistent")),
                                                      (95, (95, None, "received_at"))])
def test_a_late_or_inconsistent_time_sync_is_not_reported_as_within_30_seconds(db, received_after, expected):
    _rel, device, _run, _deployment = booted_target(db)
    sync = db.scalar(select(DeviceLog).where(DeviceLog.code == "ZKT_TIME_SYNC_OK"))
    sync.received_at = returned_at(db) + timedelta(seconds=received_after)
    db.commit()
    returned_heartbeat(db, device)
    terminal = alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["diagnosis"]["terminal"]
    assert (terminal["time_sync_after_new_boot_seconds"], terminal["answered_within_30_seconds"],
            terminal["source"]) == expected


def test_campaign_detail_reuses_the_stored_reset_history(db, monkeypatch):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    stored = events(db, deployment, "ROLLED_BACK")[-1].details["diagnosis"]["previous_firmware_resets"]
    from zk_add import ota_interruption

    def no_rescan(*_args, **_kwargs):
        raise AssertionError("campaign detail re-read 7 days of telemetry")

    monkeypatch.setattr(ota_interruption, "boot_history", no_rescan)
    row = retry_row(db, run, deployment)
    assert row["rollback_diagnosis"]["previous_firmware_resets"] == stored
    assert row["rollback_diagnosis"]["classification"] == TARGET_BOOT_INTERRUPTED


# --- Review fixes: resets ADD caused and the stability override ------------------------------------


def reboot_command(db, device, at):
    db.add(DeviceCommand(
        command_id=secrets.token_hex(8), connector_id=device.id, command_type="ESP_REBOOT",
        payload_encrypted="x", expected_state_encrypted="x", desired_state_encrypted="x",
        idempotency_key=secrets.token_hex(8), status="SUCCEEDED", actor="StateHealthAdmin",
        created_at=at, dispatched_at=at))
    db.flush()


def test_restarts_add_caused_are_not_resets(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", firmware="2.7.5", boot="c")
    _run, deployment = campaign(db, rel, device, deployment_status="SUCCEEDED", previous="2.6.15")
    now = utc_now()
    history(db, device, "a", start=now - timedelta(minutes=70), minutes=20)
    db.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT",
                         created_at=now - timedelta(minutes=50, seconds=10), details={}))
    history(db, device, "b", start=now - timedelta(minutes=50), minutes=20, firmware="2.7.5")
    reboot_command(db, device, now - timedelta(minutes=30, seconds=10))
    history(db, device, "c", start=now - timedelta(minutes=30), minutes=10, firmware="2.7.5")
    result = boot_history(db, device, until=now, firmware_version="2.7.5")
    assert (result["boot_ids"], result["resets"], result["add_restarts_excluded"],
            result["firmware_changes_excluded"]) == (2, 0, 1, 1)
    assert boot_history(db, device, until=now)["resets"] == 0
    # The current boot was started by the ESP_REBOOT: no short-uptime warning.
    assert stability_warnings(db, device) == []
    # A later reset ADD did not cause is one.
    history(db, device, "d", start=now - timedelta(minutes=19), minutes=10, firmware="2.7.5")
    device.boot_id = "d"
    assert boot_history(db, device, until=now, firmware_version="2.7.5")["resets"] == 1
    assert {warning["code"] for warning in stability_warnings(db, device)} == {
        "ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}


def test_an_interrupted_boots_own_return_is_not_a_reset_of_the_old_firmware(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    diagnosis = alert(db, device, "OTA_DEVICE_ROLLED_BACK").details["diagnosis"]
    assert diagnosis["previous_firmware_resets"]["resets"] == 3
    history_now = boot_history(db, device, until=utc_now(), firmware_version="2.6.15")
    # boot-old -> boot-ret spans the OTA's READY_TO_BOOT restart, so it is excluded...
    assert (history_now["resets"], history_now["add_restarts_excluded"]) == (3, 1)
    # ...but the return boot was not started by that restart: its short uptime still warns.
    assert "ESP_UPTIME_SHORT" in {warning["code"] for warning in stability_warnings(db, device)}


def test_a_successful_ota_boot_needs_no_uptime_override(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", firmware="2.7.5", boot="new")
    _run, deployment = campaign(db, rel, device, deployment_status="SUCCEEDED", previous="2.6.15")
    now = utc_now()
    history(db, device, "old", start=now - timedelta(hours=3), minutes=150)
    db.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT",
                         created_at=now - timedelta(minutes=29), details={}))
    history(db, device, "new", start=now - timedelta(minutes=28), minutes=10, firmware="2.7.5")
    assert stability_warnings(db, device) == []


# --- Review fixes: the owner's retry in a zone with several ESPs, and concurrency -------------------


def second_esp(db, run, rel, *, status="PENDING"):
    other = esp(db, PESHAWAR_02, "ZONE-MULTAN-02", boot="other")
    db.add(FirmwareDeployment(deployment_id=secrets.token_hex(16), campaign_id=run.id, release_id=rel.id,
                              connector_id=other.id, status=status, previous_version="2.6.15",
                              target_version=rel.version))
    db.commit()
    return other


def test_retry_names_the_pending_deployments_it_resumes(db):
    rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    second_esp(db, run, rel)
    row = retry_row(db, run, deployment)
    assert row["retry"]["available"] is True and row["retry"]["pending_deployments"] == 1
    client, headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    review = client.get(path, params={"deployment_id": deployment.deployment_id}).json()
    assert review["pending_deployments"] == 1
    body = retry_body(deployment, override_stability_warnings=True,
                      stability_warnings_digest=review["stability_warnings_digest"])
    unconfirmed = client.post(path, json=body, headers=headers)
    assert unconfirmed.status_code == 409 and "1 other pending" in unconfirmed.json()["detail"]
    confirmed = client.post(path, json={**body, "resume_pending_deployments": 1}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["resumed_pending_deployments"] == 1


def test_retry_is_refused_while_another_deployment_is_active(db):
    rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    second_esp(db, run, rel, status="DOWNLOADING")
    assert retry_row(db, run, deployment)["retry"]["refusal"] == "Another deployment in this campaign is still active."


def retry_arguments(db, run, deployment, device):
    return dict(campaign_public_id=run.campaign_id, deployment_public_id=deployment.deployment_id,
                actor="StateHealthAdmin", reason="Site replaced the ESP power supply.", typed_confirmation="2.7.5",
                override_stability_warnings=True,
                stability_warnings_digest=warnings_digest(stability_warnings(db, db.get(Connector, device.id))))


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_two_simultaneous_retries_add_one_attempt(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    arguments = retry_arguments(db, run, deployment, device)
    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        with Session(bind=db.get_bind(), expire_on_commit=False, autoflush=False) as session:
            barrier.wait()
            try:
                retry_interrupted_target(session, **arguments)
                session.commit()
                outcomes.append("ok")
            except ValueError as error:
                session.rollback()
                outcomes.append(str(error))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes)[-1] == "ok" and outcomes.count("ok") == 1
    assert db.scalar(select(func.count(FirmwareDeployment.id)).where(FirmwareDeployment.campaign_id == run.id)) == 2


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_a_cancel_racing_a_retry_cancels_the_new_attempt(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    arguments = retry_arguments(db, run, deployment, device)
    engine = db.get_bind()
    retrying = Session(bind=engine, expire_on_commit=False, autoflush=False)
    result = retry_interrupted_target(retrying, **arguments)
    retrying.flush()  # the new attempt exists, uncommitted, with both locks held
    finished = {}

    def cancel():
        with Session(bind=engine, expire_on_commit=False, autoflush=False) as session:
            control_campaign(session, campaign_public_id=run.campaign_id, action="cancel",
                             reason="Owner cancel during the retry")
            session.commit()
            finished["at"] = time.monotonic()

    thread = threading.Thread(target=cancel)
    thread.start()
    time.sleep(1.0)
    assert "at" not in finished  # the cancel waits for the retry's locks
    retrying.commit()
    retrying.close()
    thread.join(timeout=30)
    assert "at" in finished
    db.expire_all()
    retry = db.scalar(select(FirmwareDeployment).where(FirmwareDeployment.deployment_id == result["deployment_id"]))
    assert retry.status == "CANCELLED" and db.get(FirmwareCampaign, run.id).status == "CANCELLED"


# --- Review fixes: the rollback-slot residue -------------------------------------------------------


def earlier_boot(db, device, *, days, storage):
    db.add(DeviceTelemetry(connector_id=device.id, boot_id=f"earlier-{days}", sequence=1, uptime_seconds=900,
                           created_at=utc_now() - timedelta(days=days),
                           payload={"firmware_version": "zone-lite-2.6.15", "diagnostics": {"storage": storage}}))
    db.commit()


def test_rollback_slot_residue_needs_fourteen_clean_days(db):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    residue_record(db, device)
    earlier_boot(db, device, days=20, storage=residue_storage(write_failures=4, error_code=5))
    earlier_boot(db, device, days=6, storage=residue_storage(write_failures=3, error_code=5,
                                                             error_operation="local_write_commit"))
    residue_heartbeat(db, device, 1)
    durability = alert(db, device, "ESP_DURABILITY_FAULT")
    assert "latch" not in durability.details
    history_record = durability.details["residue_history"]
    assert history_record["clean"] is False and history_record["faults"][0]["boot_id"] == "earlier-6"
    assert evaluate_health(db, device).derived_lifecycle == "DEGRADED"


def test_rollback_slot_residue_history_is_read_once_per_boot(db, monkeypatch):
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    residue_record(db, device)
    earlier_boot(db, device, days=20, storage=residue_storage(write_failures=4, error_code=5))
    calls = []
    original = service.rollback_slot_residue_history

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "rollback_slot_residue_history", counted)
    residue_heartbeat(db, device, 1)
    residue_heartbeat(db, device, 2)
    assert len(calls) == 1
    latch = alert(db, device, "ESP_DURABILITY_FAULT").details["latch"]
    assert latch["kind"] == "OTA_ROLLBACK_SLOT_RESIDUE" and latch["residue"]["hardware_id"] == device.hardware_id
    assert latch["history"]["boots_checked"] == 0  # the 20-day-old boot is outside the window


@pytest.mark.parametrize("sources", ["2.6.15:add_connector.c:3764,2.6.15:add_connector.c:3787",
                                     "2.6.15:add_connector.c:3764,2.6.15:zone_lite.c:3196"])
def test_rollback_slot_residue_is_never_downgraded_by_the_latched_led_policy(db, monkeypatch, sources):
    assert "zone_lite.c:3196" not in type(settings).model_fields["device_health_latched_led_sources"].default
    monkeypatch.setattr(settings, "device_health_latched_led_sources", sources)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    residue_record(db, device)
    residue_heartbeat(db, device, 1)
    health = evaluate_health(db, device)
    assert health.derived_lifecycle == "DEGRADED" and health.last_error_code == "OTA_ROLLBACK_SLOT_RESIDUE"
    reason = next(item for item in health.reasons if item.code == "ESP_DURABILITY_FAULT")
    assert reason.tier == "DEGRADED" and "persistence probe did not run" in reason.evidence_summary


# --- Second review: bounded holds, scope, kill switch and the sweep --------------------------------


def interrupted(db):
    """A stalled 2.6.15 download with its first interruption recorded."""
    rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 1 and deployment.status == "DOWNLOADING"
    return rel, device, run, deployment


def set_retry_after(db, deployment, value):
    latest = events(db, deployment, DOWNLOAD_INTERRUPTED)[-1]
    latest.details = {**latest.details, "retry_after": value.isoformat()}
    db.commit()


def grants(db, deployment):
    return db.scalar(select(func.count(FirmwareDownloadGrant.id)).where(
        FirmwareDownloadGrant.deployment_id == deployment.id))


def test_a_held_download_whose_esp_rebooted_is_never_re_offered_after_its_deadline(db):
    _rel, device, run, deployment = interrupted(db)
    # The ESP reset during its backoff (2.6.x clears the sticky error) and returns days later.
    set_retry_after(db, deployment, utc_now() - timedelta(days=3))
    device.boot_id = "boot-after-outage"
    db.commit()
    beat(db, device, stuck(error="", uptime=30))
    assert deployment.status == "DOWNLOADING"
    assert offer(db, device) is None
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_NOT_TAKEN)
    assert "did not take ADD's re-offer within 15 min" in deployment.error_message
    assert events(db, deployment, DOWNLOAD_RETRY_OFFERED) == [] and grants(db, deployment) == 1
    assert db.get(FirmwareCampaign, run.id).status == "PAUSED" and not reserved(db, deployment)


def test_a_held_download_is_never_re_offered_after_the_resume_window(db):
    _rel, device, _run, deployment = interrupted(db)
    db.scalar(select(FirmwareDownloadGrant).where(FirmwareDownloadGrant.deployment_id == deployment.id)
              ).created_at = utc_now() - timedelta(minutes=70)
    events(db, deployment, DOWNLOAD_INTERRUPTED)[0].created_at = utc_now() - timedelta(minutes=61)
    db.commit()
    set_retry_after(db, deployment, utc_now() - timedelta(seconds=1))
    assert offer(db, device) is None
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_NOT_TAKEN)
    assert "60 min resume window after the first stop closed" in deployment.error_message
    assert grants(db, deployment) == 1


def test_the_sweep_fails_a_held_download_whose_esp_stays_offline(db):
    _rel, device, run, deployment = interrupted(db)
    device.connected = False
    db.commit()
    assert expire_held_downloads(db) == 0  # still inside its backoff and deadline
    db.commit()
    set_retry_after(db, deployment, utc_now() - timedelta(minutes=16))
    assert expire_held_downloads(db) == 1
    db.commit()
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_NOT_TAKEN)
    assert db.get(FirmwareCampaign, run.id).status == "PAUSED" and not reserved(db, deployment)
    assert expire_held_downloads(db) == 0


def test_the_sweep_fails_a_held_download_of_a_campaign_paused_by_any_path(db):
    _rel, _device, run, deployment = interrupted(db)
    run.status, run.pause_reason = "PAUSED", "Paused outside campaign control"
    db.commit()
    assert expire_held_downloads(db) == 1
    db.commit()
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert "the campaign is paused" in deployment.error_message


def test_the_sweep_leaves_a_re_offered_attempt_alone(db):
    _rel, device, _run, deployment = interrupted(db)
    expire_backoff(db, deployment)
    assert offer(db, device) is not None
    set_retry_after(db, deployment, utc_now() - timedelta(hours=2))
    assert expire_held_downloads(db) == 0
    assert deployment.status == "DOWNLOADING"


def test_with_the_switch_off_a_stall_fails_closed_at_once_as_before(db, monkeypatch):
    monkeypatch.setattr(settings, "firmware_download_resume_enabled", False)
    _rel, device, run, deployment = stalled(db)
    beat(db, device, stuck())
    assert (deployment.status, deployment.error_code) == ("FAILED", "DOWNLOAD_INCOMPLETE")
    assert run.status == "PAUSED" and events(db, deployment, DOWNLOAD_INTERRUPTED) == []


def test_the_switch_is_off_by_default():
    assert type(settings).model_fields["firmware_download_resume_enabled"].default is False


@pytest.mark.parametrize("path", ["offer", "sweep"])
def test_turning_the_switch_off_fails_a_held_download(db, monkeypatch, path):
    _rel, device, _run, deployment = interrupted(db)
    monkeypatch.setattr(settings, "firmware_download_resume_enabled", False)
    if path == "offer":
        assert offer(db, device) is None
    else:
        assert expire_held_downloads(db) == 1
        db.commit()
    assert (deployment.status, deployment.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert "download resume is turned off" in deployment.error_message


@pytest.mark.parametrize("target", ["2.7.6", "2.6.24", "2.7.0"])
def test_targets_with_their_own_return_evidence_still_fail_closed_at_once(db, target):
    rel = release(db, target)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02")
    run, deployment = campaign(db, rel, device)
    now = utc_now()
    grant(db, deployment, now - timedelta(minutes=7))
    telemetry(db, device, boot=device.boot_id, at=now - timedelta(seconds=20), uptime=2380,
              ota_state="UPDATING", target=target)
    beat(db, device, stuck(target=target))
    assert (deployment.status, deployment.error_code) == ("FAILED", "DOWNLOAD_INCOMPLETE")
    assert run.status == "PAUSED" and events(db, deployment, DOWNLOAD_INTERRUPTED) == []


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "ROLLED_BACK"])
def test_a_grant_of_a_terminal_deployment_downloads_nothing(db, status):
    _rel, device, _run, deployment = stalled(db)
    token = secrets.token_urlsafe(32)
    db.add(FirmwareDownloadGrant(token_hash=hashlib.sha256(token.encode()).hexdigest(), deployment_id=deployment.id,
                                 connector_id=device.id, expires_at=utc_now() + timedelta(minutes=10)))
    deployment.status = status
    db.commit()
    with pytest.raises(ValueError, match="no longer active"):
        ota.resolve_download(db, token)


# --- Second review: attempts are counted by when the ESP built a heartbeat ------------------------


def test_heartbeats_built_before_the_re_offer_never_end_the_new_attempt(db):
    _rel, device, _run, deployment = interrupted(db)
    expire_backoff(db, deployment)
    assert offer(db, device) is not None
    granted = parse_datetime(db.scalar(select(func.max(FirmwareDownloadGrant.created_at)).where(
        FirmwareDownloadGrant.deployment_id == deployment.id)).isoformat())
    before = int((granted - boot_started(db, device)).total_seconds())
    # A WebSocket read pause delivers, after the grant, two heartbeats built
    # 20 s and 5 s before the ESP fetched the re-offer.
    beat(db, device, stuck(uptime=before - 20))
    beat(db, device, stuck(uptime=before - 5))
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 1
    assert deployment.status == "DOWNLOADING"
    # Heartbeats the ESP built after the fetch do prove the next stop.
    beat(db, device, stuck())
    beat(db, device, stuck())
    assert len(events(db, deployment, DOWNLOAD_INTERRUPTED)) == 2
    assert "attempt 2 of 3" in deployment.error_message


# --- Second review: the ESP's own return report ---------------------------------------------------


def progress(client, deployment, **changes):
    return client.post(f"/device/v2/firmware/deployments/{deployment.deployment_id}/progress",
                       json=device_report(deployment, **changes))


def test_a_reset_seconds_after_the_target_boot_report_is_still_classified(db):
    _rel, device, run, deployment = booted_target(db)
    target_report = events(db, deployment, "BOOTED_PENDING")[-1]
    # The return boot started 3 s after ADD received the target's report,
    # inside the 10 s receipt skew: READY_TO_BOOT anchors it instead.
    device.boot_id = "boot-fast"
    db.add(DeviceTelemetry(connector_id=device.id, boot_id="boot-fast", sequence=1, uptime_seconds=14,
                           created_at=target_report.created_at + timedelta(seconds=17),
                           payload={"firmware_version": "zone-lite-2.6.15"}))
    db.commit()
    response = progress(device_client(db, device), deployment)
    assert response.status_code == 200, response.text
    db.expire_all()
    deployment = db.get(FirmwareDeployment, deployment.id)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", TARGET_BOOT_INTERRUPTED)
    stored = events(db, deployment, "ROLLED_BACK")[-1].details
    assert stored["return_evidence"]["boot_id"] == "boot-fast"
    assert stored["diagnosis"]["target_boot"]["estimated_lifetime_seconds"] == {"min": 3, "max": 36}


def test_a_delayed_sample_of_the_pre_install_boot_is_not_a_fast_return(db):
    _rel, device, _run, deployment = booted_target(db)
    target_report = events(db, deployment, "BOOTED_PENDING")[-1]
    # boot-old sent READY_TO_BOOT; a late sample of it estimates a start after that report.
    device.boot_id = "boot-old"
    db.add(DeviceTelemetry(connector_id=device.id, boot_id="boot-old", sequence=99, uptime_seconds=1800,
                           created_at=target_report.created_at + timedelta(seconds=1805),
                           payload={"firmware_version": "zone-lite-2.6.15"}))
    db.commit()
    response = progress(device_client(db, device), deployment)
    assert response.status_code == 409 and "RETURN_EVIDENCE_PENDING" in response.json()["detail"]
    db.expire_all()
    assert db.get(FirmwareDeployment, deployment.id).status == "BOOTED_PENDING"


def test_reports_outside_the_return_scope_stay_on_the_synchronous_path(db, monkeypatch):
    from zk_add import web

    _rel, device, _run, deployment = booted_target(db, previous="2.5.2", target="2.7.5")

    async def no_thread(*_args, **_kwargs):
        raise AssertionError("an out-of-scope progress report left the request's thread")

    monkeypatch.setattr(web, "run_in_threadpool", no_thread)
    response = progress(device_client(db, device), deployment, previous="2.5.2")
    assert response.status_code == 200, response.text
    db.expire_all()
    deployment = db.get(FirmwareDeployment, deployment.id)
    assert (deployment.status, deployment.error_code) == ("ROLLED_BACK", "BOOTLOADER_ROLLBACK")


# --- Second review: the owner's retry ---------------------------------------------------------------


def test_campaign_detail_never_runs_the_stability_scan(db, monkeypatch):
    from zk_add import esp_stability, ota_interruption

    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)

    def no_scan(*_args, **_kwargs):
        raise AssertionError("campaign detail ran the 7-day stability scan")

    monkeypatch.setattr(ota_interruption, "stability_warnings", no_scan)
    monkeypatch.setattr(esp_stability, "stability_warnings", no_scan)
    row = retry_row(db, run, deployment)
    assert row["retry"]["available"] is True and row["retry"]["pending_deployments"] == 0


def test_campaign_detail_reuses_a_complete_stored_peer_scan(db, monkeypatch):
    from zk_add import ota_interruption

    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    event = events(db, deployment, "ROLLED_BACK")[-1]
    diagnosis = event.details["diagnosis"]
    event.details = {**event.details, "diagnosis": {
        **diagnosis, "location_resets": {**diagnosis["location_resets"], "complete": True}}}
    db.commit()

    def no_rescan(*_args, **_kwargs):
        raise AssertionError("campaign detail re-read a complete peer scan")

    monkeypatch.setattr(ota_interruption, "peer_resets", no_rescan)
    assert retry_row(db, run, deployment)["rollback_diagnosis"]["location_resets"]["complete"] is True


def test_retry_is_refused_when_the_campaign_was_paused_for_another_reason(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    assert retry_row(db, run, deployment)["retry"]["available"] is True
    run.pause_reason = "Owner hold for a site survey"
    db.commit()
    refusal = retry_row(db, run, deployment)["retry"]["refusal"]
    assert "paused for another reason (Owner hold for a site survey)" in refusal


def test_retry_is_refused_while_the_esp_reports_storage_io_faults(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    device.firmware_diagnostics = {"storage": residue_storage(write_failures=2, error_code=5)}
    db.commit()
    client, headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    review = client.get(path, params={"deployment_id": deployment.deployment_id}).json()
    assert review["available"] is False and "write_failures=2" in review["refusal"]
    assert review["esp_state"]["storage_io_faults"] == {"write_failures": 2, "error_code": 5}
    assert review["stability_warnings"] == []
    refused = client.post(path, json=retry_body(deployment, override_stability_warnings=True), headers=headers)
    assert refused.status_code == 409 and "storage I/O faults" in refused.json()["detail"]
    # The 2.6.15 rollback-slot residue alone (no I/O fault) does not block the retry.
    db.expire_all()
    connector = db.get(Connector, device.id)
    connector.firmware_diagnostics = {"storage": residue_storage()}
    db.commit()
    assert retry_row(db, db.get(FirmwareCampaign, run.id), deployment)["retry"]["available"] is True


def test_the_retry_review_rejects_a_deployment_that_cannot_be_retried(db):
    _rel, device, run, deployment = stalled(db)
    client, _headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    assert client.get(path, params={"deployment_id": deployment.deployment_id}).status_code == 409
    assert client.get(path, params={"deployment_id": "missing"}).status_code == 404


def test_a_new_campaign_needs_the_override_after_two_interrupted_boots_of_the_release(db):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="steady")
    history(db, device, "steady", start=utc_now() - timedelta(hours=2), minutes=120)
    for _ in range(2):
        campaign(db, rel, device, status="CANCELLED", deployment_status="ROLLED_BACK", bytes_written=IMAGE_SIZE,
                 error_code=TARGET_BOOT_INTERRUPTED)
    db.commit()
    scope = preview_campaign_scope(db, release_public_id=rel.release_id, zone_id=device.zone_id)
    [warning] = scope["stability_warnings"]
    assert warning["code"] == TARGET_BOOT_INTERRUPTED_REPEATEDLY and "UART" in warning["message"]
    arguments = dict(release_public_id=rel.release_id, zone_id=device.zone_id, reason="Third 2.7.5 attempt",
                     typed_confirmation="2.7.5", actor="StateHealthAdmin", scope_token=scope["scope_token"])
    with pytest.raises(ValueError, match="override"):
        create_campaign(db, idempotency_key="interrupted-twice", **arguments)
    run = create_campaign(db, idempotency_key="interrupted-twice-override", override_stability_warnings=True,
                          stability_warnings_digest=scope["stability_warnings_digest"], **arguments)
    assert run.status == "ACTIVE"


# --- Second review: lock order and races on PostgreSQL ----------------------------------------------


def other_session(db):
    return Session(bind=db.get_bind(), expire_on_commit=False, autoflush=False)


def end_of_backoff_poll(db, device, out):
    with other_session(db) as session:
        out["assignment"] = assignment_for_connector(session, connector=session.get(Connector, device.id),
                                                     public_base="https://test.invalid")
        session.commit()


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_a_pause_that_commits_first_is_seen_by_the_end_of_backoff_poll(db, monkeypatch):
    from zk_add import ota_interruption

    _rel, device, run, deployment = interrupted(db)
    expire_backoff(db, deployment)
    db.commit()
    original = ota_interruption.lock_for_assignment
    reached, paused, out = threading.Event(), threading.Event(), {}

    def gated(session, connector, row):
        reached.set()
        paused.wait(20)  # the poll read the held deployment before the pause committed
        return original(session, connector, row)

    monkeypatch.setattr(ota_interruption, "lock_for_assignment", gated)
    thread = threading.Thread(target=end_of_backoff_poll, args=(db, device, out))
    thread.start()
    assert reached.wait(20)
    with other_session(db) as session:
        _campaign, failed = control_campaign(session, campaign_public_id=run.campaign_id, action="pause",
                                             reason="Owner pause at the end of the backoff")
        session.commit()
    paused.set()
    thread.join(30)
    assert failed == 1 and out == {"assignment": None}
    db.expire_all()
    row = db.get(FirmwareDeployment, deployment.id)
    assert (row.status, row.error_code) == ("FAILED", DOWNLOAD_RETRY_WITHDRAWN)
    assert events(db, row, DOWNLOAD_RETRY_OFFERED) == [] and grants(db, row) == 1


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_a_pause_waits_for_an_end_of_backoff_poll_that_locked_first(db, monkeypatch):
    from zk_add import ota_interruption

    _rel, device, run, deployment = interrupted(db)
    expire_backoff(db, deployment)
    db.commit()
    original = ota_interruption.download_retry_hold
    locked, release_poll, out = threading.Event(), threading.Event(), {}

    def gated(session, row, latest, **kwargs):
        locked.set()  # the poll holds the connector and deployment locks here
        release_poll.wait(20)
        return original(session, row, latest, **kwargs)

    def pause():
        with other_session(db) as session:
            out["failed"] = control_campaign(session, campaign_public_id=run.campaign_id, action="pause",
                                             reason="Owner pause at the end of the backoff")[1]
            session.commit()
            out["paused"] = True

    monkeypatch.setattr(ota_interruption, "download_retry_hold", gated)
    poller = threading.Thread(target=end_of_backoff_poll, args=(db, device, out))
    poller.start()
    assert locked.wait(20)
    pauser = threading.Thread(target=pause)
    pauser.start()
    time.sleep(1.0)
    assert "paused" not in out  # the pause waits for the poll's connector lock
    release_poll.set()
    poller.join(30)
    pauser.join(30)
    assert out["assignment"] is not None and out["failed"] == 0 and out["paused"] is True
    db.expire_all()
    row = db.get(FirmwareDeployment, deployment.id)
    # The ESP holds the re-offer's grant: the attempt stays active and reserved.
    assert row.status == "DOWNLOADING" and len(events(db, row, DOWNLOAD_RETRY_OFFERED)) == 1
    assert db.get(FirmwareCampaign, run.id).status == "PAUSED" and reserved(db, row)


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_an_assignment_waits_for_the_heartbeat_that_records_a_stop(db):
    _rel, device, _run, deployment = stalled(db)
    db.commit()
    out = {}
    with other_session(db) as heartbeat:
        connector = heartbeat.scalar(select(Connector).where(Connector.id == device.id).with_for_update())
        thread = threading.Thread(target=end_of_backoff_poll, args=(db, device, out))
        thread.start()
        try:
            time.sleep(1.0)
            assert out == {}  # the poll waits for the heartbeat's connector lock
            beat(heartbeat, connector, stuck())  # records the stop and commits
        finally:
            heartbeat.rollback()
            thread.join(30)
    # The poll then sees the stop and withholds the assignment for its backoff.
    assert out == {"assignment": None}
    db.expire_all()
    row = db.get(FirmwareDeployment, deployment.id)
    assert len(events(db, row, DOWNLOAD_INTERRUPTED)) == 1 and grants(db, row) == 1


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_a_return_report_and_the_return_heartbeat_never_deadlock(db, monkeypatch):
    from zk_add import ota_interruption

    _rel, device, _run, deployment = booted_target(db)
    original = ota_interruption.device_return_evidence
    holding, out = threading.Event(), {}

    def slow(session, **kwargs):
        holding.set()  # the report holds its locks here
        time.sleep(1.0)
        return original(session, **kwargs)

    monkeypatch.setattr(ota_interruption, "device_return_evidence", slow)
    monkeypatch.setattr(ota, "utc_now", lambda: db.info["booted_at"] + timedelta(seconds=1))

    def report():
        with other_session(db) as session:
            try:
                record_device_reported_return(
                    session, connector=session.get(Connector, device.id),
                    deployment_public_id=deployment.deployment_id, bytes_written=IMAGE_SIZE,
                    running_version="2.6.15", running_partition="ota_1", image_sha256=FLEET_2615,
                    error_code="BOOTLOADER_ROLLBACK", error_message=None)
                session.commit()
                out["report"] = "committed"
            except Exception as error:  # noqa: BLE001 - the outcome is the assertion
                session.rollback()
                out["report"] = repr(error)

    def heartbeat():
        assert holding.wait(20)
        with other_session(db) as session:
            try:
                # persist_envelope: the connector row first, then the OTA diagnostics.
                connector = session.scalar(select(Connector).where(Connector.id == device.id).with_for_update())
                connector.last_sequence += 1
                apply_ota_heartbeat_diagnostics(session, connector=connector, payload=HeartbeatPayload(
                    firmware_version="zone-lite-2.6.15", uptime_seconds=43, ota={
                        "running_version": "2.6.15", "running_partition": "ota_1", "image_sha256": FLEET_2615}))
                session.commit()
                out["heartbeat"] = "committed"
            except Exception as error:  # noqa: BLE001 - the outcome is the assertion
                session.rollback()
                out["heartbeat"] = repr(error)

    threads = [threading.Thread(target=report), threading.Thread(target=heartbeat)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert out == {"report": "committed", "heartbeat": "committed"}
    db.expire_all()
    row = db.get(FirmwareDeployment, deployment.id)
    [rolled] = events(db, row, "ROLLED_BACK")
    assert (row.status, row.error_code) == ("ROLLED_BACK", TARGET_BOOT_INTERRUPTED)
    assert rolled.details["return_evidence"]["source"] == "device_reported_bootloader_rollback"


# --- Second review: the telemetry index migration ---------------------------------------------------


def migration_module():
    path = (Path(__file__).resolve().parents[2] / "apps/add_backend/migrations/versions"
            / "20261021_ota_interrupted_boot_retry.py")
    spec = importlib.util.spec_from_file_location("ota_interrupted_boot_retry_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_deploy_builds_the_telemetry_index_only_for_a_small_table():
    module = migration_module()
    assert module.inline_build(0) and module.inline_build(module.INLINE_BUILD_LIMIT_BYTES)
    assert not module.inline_build(module.INLINE_BUILD_LIMIT_BYTES + 1) and not module.inline_build(None)
    assert module.OPERATOR_COMMAND.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS")


@pytest.mark.parametrize("db", ["postgres"], indirect=True)
def test_the_migration_keeps_a_pre_created_index_and_never_builds_a_large_one(db, monkeypatch, caplog):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = migration_module()
    engine = db.get_bind()

    def index_valid():
        with engine.connect() as connection:
            return connection.execute(text("SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = "
                                           "to_regclass('ix_add_device_telemetry_connector_created')")).scalar()

    def upgrade():
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    try:
        assert index_valid() is True
        upgrade()  # the operator's index: nothing to do
        assert index_valid() is True
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_add_device_telemetry_connector_created"))
        monkeypatch.setattr(module, "INLINE_BUILD_LIMIT_BYTES", -1)  # every table is "large"
        with caplog.at_level(logging.WARNING, logger="alembic"):
            upgrade()
        assert index_valid() is None and "CREATE INDEX CONCURRENTLY" in caplog.text
        monkeypatch.setattr(module, "INLINE_BUILD_LIMIT_BYTES", 512 * 1024 * 1024)
        upgrade()
        assert index_valid() is True
    finally:
        with engine.begin() as connection:
            connection.execute(text(module.OPERATOR_COMMAND.replace(" CONCURRENTLY", "")))
