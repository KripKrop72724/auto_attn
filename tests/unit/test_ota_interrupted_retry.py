"""Interrupted OTA attempts, after MULTAN-02's 2.6.15 -> 2.7.5 attempts on 21 Oct 2026.

Bounded resumption of a stalled 2.6.x download, the TARGET_BOOT_INTERRUPTED
classification and its evidence, the owner's step-up retry, ESP stability
preflight warnings and the 2.6.15 rollback-slot residue health latch. Each
test runs on SQLite and, when configured, PostgreSQL.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import ota
from zk_add.db import Base
from zk_add.device_health import evaluate_health
from zk_add.esp_stability import boot_history, stability_warnings
from zk_add.models import AuditEvent, Connector, DeviceAlert, DeviceLog, DeviceTelemetry, ZKTDevice
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareDownloadGrant,
    FirmwareEvent,
    FirmwareRelease,
    assignment_for_connector,
    campaign_detail,
    create_campaign,
    preview_campaign_scope,
)
from zk_add.ota_interruption import (
    DOWNLOAD_INTERRUPTED,
    DOWNLOAD_RETRY_OFFERED,
    TARGET_BOOT_INTERRUPTED,
    classify_return,
)
from zk_add.schemas import HeartbeatPayload
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.service import apply_ota_heartbeat_diagnostics, update_heartbeat
from zk_add.settings import settings
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.time_utils import parse_datetime, utc_now
from zk_add.web import app, get_db
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets

MULTAN_01, MULTAN_02 = TARGETS[3], TARGETS[4]
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


def telemetry(db, device, *, boot, at, uptime, firmware="2.6.15", ota_state="", activity=None, sequence=1):
    db.add(DeviceTelemetry(
        connector_id=device.id, boot_id=boot, sequence=sequence, uptime_seconds=uptime, created_at=at,
        current_activity=activity,
        payload={"firmware_version": firmware, "uptime_seconds": uptime,
                 "ota": {"state": ota_state, "target_version": "2.7.5"}}))
    db.flush()


def history(db, device, boot, *, start, minutes, firmware="2.6.15"):
    """Heartbeats every 15 s for one boot."""
    for index in range(minutes * 4):
        db.add(DeviceTelemetry(
            connector_id=device.id, boot_id=boot, sequence=index + 1, uptime_seconds=15 * index,
            created_at=start + timedelta(seconds=15 * index), payload={"firmware_version": firmware}))
    db.flush()


def stuck(firmware="2.6.15", error="DOWNLOAD_INCOMPLETE", state="DOWNLOADING", target="2.7.5",
          written=360_448, uptime=2400) -> HeartbeatPayload:
    return HeartbeatPayload(firmware_version=f"zone-lite-{firmware}", uptime_seconds=uptime, ota={
        "running_version": firmware, "running_partition": "ota_1", "image_sha256": FLEET_2615,
        "capable": True, "secure_boot": True, "rollback_enabled": True, "partition_layout": ota.OTA_LAYOUT,
        "state": state, "target_version": target, "bytes_written": written, "image_size": IMAGE_SIZE,
        "last_error": error})


def beat(db, device, payload):
    """The heartbeat order of update_heartbeat: OTA diagnostics first, then the sample is stored."""
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
    # The device resumes from its committed checkpoint, not this heartbeat's RAM count.
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


def booted_target(db, *, lifetime=47, last_code="WAITING_FOR_RUNTIME_HEALTH", target_boot_logs=False):
    rel = release(db)
    device = esp(db, MULTAN_02, "ZONE-MULTAN-02", boot="boot-old")
    peer = esp(db, MULTAN_01, "ZONE-MULTAN-01", boot="peer-a", firmware="2.7.5", slot="ota_0")
    run, deployment = campaign(db, rel, device, status="ACTIVE", deployment_status="BOOTED_PENDING",
                               bytes_written=IMAGE_SIZE)
    now = utc_now()
    returned = now - timedelta(seconds=43)
    started = returned - timedelta(seconds=lifetime)
    # Seven days on 2.6.15: three earlier 20-minute boots, then the pre-install boot.
    for index, boot in enumerate(("h1", "h2", "h3")):
        history(db, device, boot, start=now - timedelta(days=3 - index), minutes=20)
    history(db, device, "boot-old", start=started - timedelta(minutes=30), minutes=29)
    telemetry(db, device, boot="boot-old", at=started - timedelta(seconds=5), uptime=1795, activity="LIVE_CAPTURE")
    db.add(FirmwareEvent(deployment_id=deployment.id, state="READY_TO_BOOT", created_at=started - timedelta(seconds=30),
                         details={"bytes_written": IMAGE_SIZE}))
    db.add(FirmwareEvent(deployment_id=deployment.id, state="BOOTED_PENDING", created_at=started + timedelta(seconds=28),
                         details={"bytes_written": IMAGE_SIZE, "error_code": last_code, "running_version": "2.7.5",
                                  "running_partition": "ota_0", "image_sha256": APPLICATION}))
    if target_boot_logs:
        db.add(DeviceLog(connector_id=device.id, boot_id="boot-new", sequence=9, level="INFO", subsystem="reconcile",
                         code="FULL_RECONCILE", message="reconcile", received_at=returned - timedelta(seconds=12)))
        telemetry(db, device, boot="boot-new", at=returned - timedelta(seconds=5), uptime=lifetime - 5,
                  firmware="2.7.5", activity="FULL_RECONCILE")
    db.add(DeviceLog(connector_id=device.id, boot_id="boot-ret", sequence=4, level="INFO", subsystem="zkt",
                     code="ZKT_TIME_SYNC_OK", message="time set", device_time=returned + timedelta(seconds=22),
                     received_at=returned + timedelta(seconds=23)))
    # MULTAN-01 booted 60 s after MULTAN-02's reset.
    telemetry(db, peer, boot="peer-b", at=returned + timedelta(seconds=75), uptime=15, firmware="2.7.5")
    # The return boot's first heartbeat is the latest sample before this one.
    telemetry(db, device, boot="boot-ret", at=now - timedelta(seconds=30), uptime=13)
    device.boot_id, device.last_sequence = "boot-ret", 1
    db.commit()
    return rel, device, run, deployment


def returned_heartbeat(db, device):
    beat(db, device, HeartbeatPayload(firmware_version="zone-lite-2.6.15", uptime_seconds=43, ota={
        "running_version": "2.6.15", "running_partition": "ota_1", "image_sha256": FLEET_2615}))
    db.commit()


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
    assert 19 <= target["estimated_lifetime_seconds"]["min"] <= 20
    assert 52 <= target["estimated_lifetime_seconds"]["max"] <= 53
    assert target["failure_reports"] == [] and target["logs_received"] == 0 and target["telemetry_received"] == 0
    assert target["last_progress_code"] == "WAITING_FOR_RUNTIME_HEALTH" and target["progress_reports"] == 1
    assert diagnosis["previous_firmware_resets"]["boot_ids"] == 4
    assert diagnosis["previous_firmware_resets"]["resets"] == 3
    assert diagnosis["terminal"]["answered_within_30_seconds"] is True
    assert 21 <= diagnosis["terminal"]["time_sync_after_new_boot_seconds"] <= 23
    location = diagnosis["location_resets"]
    assert location["location"] == "MULTAN" and location["peers_checked"] == 1
    assert [(item["boot_id"], 59 <= item["offset_seconds"] <= 61) for item in location["resets"]] == [("peer-b", True)]
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


@pytest.mark.parametrize("version", ["2.6.17", "2.6.24", "2.7.0"])
def test_releases_with_their_own_return_evidence_are_never_classified(version):
    deployment = SimpleNamespace(status="BOOTED_PENDING")
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


def test_owner_retry_needs_step_up_typed_version_and_override_then_adds_one_attempt(db):
    _rel, device, run, deployment = booted_target(db)
    returned_heartbeat(db, device)
    detail = campaign_detail(db, run.campaign_id)
    row = next(item for item in detail["deployments"] if item["deployment_id"] == deployment.deployment_id)
    assert row["rollback_diagnosis"]["classification"] == TARGET_BOOT_INTERRUPTED
    assert row["retry"]["available"] is True and row["retry"]["interrupted_boots"] == 1
    assert {warning["code"] for warning in row["retry"]["stability_warnings"]} == {
        "ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    client, headers = admin_client(db)
    path = f"/api/v1/firmware/campaigns/{run.campaign_id}/retry-target"
    assert client.post(path, json=retry_body(deployment, password="wrong"), headers=headers).status_code == 403
    assert client.post(path, json=retry_body(deployment), headers={}).status_code == 403  # CSRF
    wrong = client.post(path, json=retry_body(deployment, typed_confirmation="2.7.6"), headers=headers)
    assert wrong.status_code == 409 and "Typed firmware version" in wrong.json()["detail"]
    unconfirmed = client.post(path, json=retry_body(deployment), headers=headers)
    assert unconfirmed.status_code == 409 and "override" in unconfirmed.json()["detail"]
    assert run.status == "PAUSED"
    response = client.post(path, json=retry_body(deployment, override_stability_warnings=True), headers=headers)
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
    again = client.post(path, json=retry_body(deployment, override_stability_warnings=True), headers=headers)
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
    run = create_campaign(db, idempotency_key="stability-override", override_stability_warnings=True, **arguments)
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


def test_rollback_slot_residue_is_a_warning_backed_by_adds_own_record(db):
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
    assert {reason.code: reason.tier for reason in health.reasons} == {
        "ESP_DURABILITY_FAULT": "WARNING", "ESP_LOCAL_FAILURE": "WARNING"}
    assert (health.derived_lifecycle, health.last_error_code) == ("ONLINE_WITH_WARNINGS", "OTA_ROLLBACK_SLOT_RESIDUE")
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
