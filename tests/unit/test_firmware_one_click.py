"""One-click firmware updates: several zones at once, auto-complete, state-checked controls, archive, clear history.

Every OTA admission and safety guard is unchanged; these tests pin that the
one-click paths go through the same scope preview, zone serialization and
stability override, and that history clean-up never deletes evidence. Each
test runs on SQLite and, when configured, PostgreSQL.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import ota
from zk_add.db import Base
from zk_add.firmware_campaigns import clear_history, create_for_zones
from zk_add.models import AuditEvent, Connector, DeviceTelemetry, ZKTDevice
from zk_add.ota import (
    ONE_CLICK_REASON,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareDownloadGrant,
    FirmwareEvent,
    FirmwareRelease,
    campaign_detail,
    campaign_page,
    record_progress,
)
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.settings import settings
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.time_utils import utc_now
from zk_add.web import app, get_db
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets

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
    url = _postgres_url()
    if url is None:
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "one_click_test_" + uuid4().hex
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


def admin_client(db):
    raw, admin = create_admin_session(db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest")
    db.commit()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw)
    return client, {"X-CSRF-Token": admin.csrf_token}


def platform_release(db, version="2.7.5") -> FirmwareRelease:
    """The HIL-only 2.7 platform: each signed target starts independently (zone by zone)."""
    if db.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == "zone-lite-2.6.15")) is None:
        db.add(FirmwareRelease(
            release_id="zone-lite-2.6.15", version="2.6.15", git_sha="c" * 40, image_sha256="3" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.2.0", storage_name="hil/2.6.15.bin", manifest_signature="sig",
            state="HIL_ONLY", manifest={"application_sha256": FLEET_2615}))
    baselines, images = platform_baselines(version)
    signed = {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(baselines),
                          "allowed_bootstrap_images": dict(images)},
        "release_id": f"zone-lite-{version}", "version": version,
    }
    row = FirmwareRelease(
        release_id=signed["release_id"], version=version, git_sha="a" * 40, image_sha256="b" * 64,
        image_size=IMAGE_SIZE, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        minimum_bootstrap_version="2.4.12", storage_name=f"hil/{version}.bin", manifest_signature="sig",
        state="HIL_ONLY", manifest={**signed, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                                    "_hil_targets": signed_hil_targets()})
    db.add(row)
    db.flush()
    return row


def esp(db, target, zone, *, active=True, steady=True) -> Connector:
    identity = target.identity
    device = Connector(
        connector_id=identity.connector_id, hardware_id=identity.mac, zone_id=zone, zone_name=zone,
        device_id="1", display_name=target.name, firmware_version="zone-lite-2.6.15", firmware_family="zkt",
        connected=True, active=active, is_spare=False, ota_capable=True, ota_secure_boot=True,
        ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_1",
        ota_image_sha256=FLEET_2615, boot_id="steady" if steady else "s3", last_seen_at=utc_now(),
        lifecycle_state="ONLINE")
    device.zkt_device = ZKTDevice(
        serial=identity.terminal_serial, expected_serial=identity.terminal_serial,
        confirmed_serial=identity.terminal_serial, terminal_binding_state="CONFIRMED",
        online=True, connection_state="ONLINE", last_seen_at=utc_now(), last_transition_at=utc_now())
    db.add(device)
    db.flush()
    now = utc_now()
    if steady:
        boots = [("steady", now - timedelta(hours=2), 120)]
    else:
        boots = [(f"s{index + 1}", now - timedelta(minutes=50 - 20 * index), 10) for index in range(3)]
    for boot, start, minutes in boots:
        for index in range(minutes * 4):
            db.add(DeviceTelemetry(connector_id=device.id, boot_id=boot, sequence=index + 1,
                                   uptime_seconds=15 * index, created_at=start + timedelta(seconds=15 * index),
                                   payload={"firmware_version": "2.6.15"}))
    db.flush()
    return device


def plain_release(db, version="3.0.1") -> FirmwareRelease:
    row = FirmwareRelease(
        release_id=f"synthetic-{version}", version=version, git_sha="a" * 40, image_sha256=version[0] * 64,
        image_size=1024, signing_key_id="test", partition_layout=ota.OTA_LAYOUT, storage_name=f"{version}.bin",
        manifest={"application_sha256": "c" * 64}, manifest_signature="sig", state="AVAILABLE")
    db.add(row)
    db.flush()
    return row


def plain_connector(db, name) -> Connector:
    octets = hashlib.sha256(name.encode()).hexdigest()[:6]
    device = Connector(connector_id=f"connector-{name}",
                       hardware_id="02:00:00:" + ":".join(octets[index:index + 2] for index in (0, 2, 4)),
                       zone_id=f"ZONE-{name.upper()}", zone_name=name, device_id="1", display_name=name,
                       firmware_version="3.0.0")
    db.add(device)
    db.flush()
    return device


def campaign(db, rel, zone_id, *, status="ACTIVE", deployments=()):
    """deployments: (connector, status, offered, updated_at) tuples."""
    row = FirmwareCampaign(campaign_id=secrets.token_hex(16), release_id=rel.id, zone_id=zone_id, status=status,
                           actor="StateHealthAdmin", idempotency_key=secrets.token_hex(8), reason="Synthetic",
                           typed_confirmation=rel.version, eligible_count=len(deployments))
    db.add(row)
    db.flush()
    rows = []
    for connector, state, offered, updated_at in deployments:
        deployment = FirmwareDeployment(
            deployment_id=secrets.token_hex(16), campaign_id=row.id, release_id=rel.id, connector_id=connector.id,
            status=state, previous_version="3.0.0", target_version=rel.version,
            bytes_written=0, attempt_count=1 if offered else 0,
            offered_at=utc_now() - timedelta(hours=8) if offered else None,
            updated_at=updated_at or utc_now())
        db.add(deployment)
        db.flush()
        rows.append(deployment)
    return row, rows


def audits(db, action):
    return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)))


# --- One click: no password, typed version or reason ------------------------------------------------


def test_one_click_create_needs_no_password_typed_version_or_reason(db):
    rel = platform_release(db)
    multan = esp(db, TARGETS[3], "ZONE-MULTAN-01")
    other = esp(db, TARGETS[4], "ZONE-MULTAN-02")
    client, headers = admin_client(db)
    scope = client.post("/api/v1/firmware/campaigns/preflight", json={
        "release_id": rel.release_id, "zone_id": multan.zone_id})
    assert scope.status_code == 200, scope.text
    body = {"release_id": rel.release_id, "zone_id": multan.zone_id, "scope_token": scope.json()["scope_token"],
            "idempotency_key": "one-click-create-0001"}
    assert client.post("/api/v1/firmware/campaigns", json=body).status_code == 403  # CSRF stays
    created = client.post("/api/v1/firmware/campaigns", json=body, headers=headers)
    assert created.status_code == 201, created.text
    row = db.scalar(select(FirmwareCampaign).where(FirmwareCampaign.campaign_id == created.json()["campaign_id"]))
    assert (row.status, row.reason, row.typed_confirmation, row.eligible_count) == (
        "ACTIVE", ONE_CLICK_REASON, rel.version, 1)
    # An older open tab still sends a password and typed text: both are ignored.
    scope = client.post("/api/v1/firmware/campaigns/preflight", json={
        "release_id": rel.release_id, "zone_id": other.zone_id}).json()
    legacy = client.post("/api/v1/firmware/campaigns", headers=headers, json={
        "release_id": rel.release_id, "zone_id": other.zone_id, "scope_token": scope["scope_token"],
        "idempotency_key": "one-click-create-0002", "password": "wrong", "typed_confirmation": "9.9.9",
        "reason": "Old tab"})
    assert legacy.status_code == 201, legacy.text
    assert {audit.target_id for audit in audits(db, "FIRMWARE_CAMPAIGN_CREATED")} == {multan.zone_id, other.zone_id}


# --- Several zones in one click ---------------------------------------------------------------------


def test_bulk_creates_per_zone_reports_busy_and_blocked_and_isolates_each_zone(db):
    rel = platform_release(db)
    steady = esp(db, TARGETS[3], "ZONE-MULTAN-01")
    shaky = esp(db, TARGETS[4], "ZONE-MULTAN-02", steady=False)
    busy = esp(db, TARGETS[5], "ZONE-PESHAWAR-02")
    finished = esp(db, TARGETS[6], "ZONE-FINISHED")
    gone = esp(db, TARGETS[7], "ZONE-GONE", active=False)
    older = db.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == "zone-lite-2.6.15"))
    running, _ = campaign(db, older, busy.zone_id, deployments=[(busy, "PENDING", False, None)])
    done, _ = campaign(db, older, finished.zone_id, deployments=[(finished, "SUCCEEDED", True, None)])
    failed, _ = campaign(db, older, gone.zone_id, deployments=[(gone, "FAILED", True, None)])
    db.commit()
    client, headers = admin_client(db)
    zones = [steady.zone_id, shaky.zone_id, busy.zone_id, finished.zone_id, gone.zone_id]
    body = {"release_id": rel.release_id, "zone_ids": zones, "idempotency_key": "bulk-request-0001"}
    assert client.post("/api/v1/firmware/campaigns/bulk", json=body).status_code == 403  # CSRF
    response = client.post("/api/v1/firmware/campaigns/bulk", json=body, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    outcomes = {row["zone_id"]: row for row in result["results"]}
    assert [row["zone_id"] for row in result["results"]] == zones
    assert {zone: row["outcome"] for zone, row in outcomes.items()} == {
        steady.zone_id: "CREATED", shaky.zone_id: "CREATED", busy.zone_id: "BUSY",
        finished.zone_id: "CREATED", gone.zone_id: "BLOCKED"}
    assert result["created"] == 3
    assert running.campaign_id in outcomes[busy.zone_id]["message"]
    assert "not active in this zone" in outcomes[gone.zone_id]["message"]
    # The shaky ESP's warnings are reported and overridden by digest, as before.
    assert {item["code"] for item in outcomes[shaky.zone_id]["stability_warnings"]} == {
        "ESP_RESET_RATE_HIGH", "ESP_UPTIME_SHORT"}
    assert outcomes[steady.zone_id]["stability_warnings"] == []
    db.expire_all()
    shaky_deployment = db.scalar(select(FirmwareDeployment).join(FirmwareCampaign).where(
        FirmwareCampaign.campaign_id == outcomes[shaky.zone_id]["campaign_id"]))
    assert db.scalar(select(FirmwareEvent.state).where(
        FirmwareEvent.deployment_id == shaky_deployment.id)) == "STABILITY_WARNINGS_OVERRIDDEN"
    # A fully succeeded campaign is closed as COMPLETED to free its zone.
    assert db.get(FirmwareCampaign, done.id).status == "COMPLETED"
    assert outcomes[finished.zone_id]["closed"] == [{"campaign_id": done.campaign_id, "status": "COMPLETED"}]
    # The blocked zone's savepoint rolled back the cancel of its finished campaign.
    assert db.get(FirmwareCampaign, failed.id).status == "ACTIVE"
    assert db.scalar(select(func.count(FirmwareCampaign.id)).where(FirmwareCampaign.zone_id == gone.zone_id)) == 1
    assert db.get(FirmwareCampaign, running.id).status == "ACTIVE"
    created = [row for row in audits(db, "FIRMWARE_CAMPAIGN_CREATED") if row.after.get("bulk")]
    assert {row.target_id for row in created} == {steady.zone_id, shaky.zone_id, finished.zone_id}
    assert [row.target_id for row in audits(db, "FIRMWARE_CAMPAIGN_COMPLETED")] == [done.campaign_id]
    # The same request again creates nothing new.
    again = client.post("/api/v1/firmware/campaigns/bulk", json=body, headers=headers).json()
    assert {row["zone_id"]: row["campaign_id"] for row in again["results"] if row["outcome"] == "CREATED"} == {
        zone: outcomes[zone]["campaign_id"] for zone in (steady.zone_id, shaky.zone_id, finished.zone_id)}
    assert db.scalar(select(func.count(FirmwareCampaign.id))) == 6
    # Distinct zones, 1 to 30, are a request contract.
    for zone_ids in ([], [steady.zone_id, steady.zone_id], [f"Z-{index}" for index in range(31)]):
        bad = client.post("/api/v1/firmware/campaigns/bulk", headers=headers,
                          json={**body, "zone_ids": zone_ids, "idempotency_key": "bulk-request-0002"})
        assert bad.status_code == 422


def test_bulk_needs_an_enabled_ota_channel(db, monkeypatch):
    rel = platform_release(db)
    esp(db, TARGETS[3], "ZONE-MULTAN-01")
    monkeypatch.setattr(settings, "firmware_hil_enabled", False)
    monkeypatch.setattr(settings, "firmware_ota_enabled", False)
    client, headers = admin_client(db)
    refused = client.post("/api/v1/firmware/campaigns/bulk", headers=headers, json={
        "release_id": rel.release_id, "zone_ids": ["ZONE-MULTAN-01"], "idempotency_key": "bulk-disabled-1"})
    assert refused.status_code == 409
    assert db.scalar(select(func.count(FirmwareCampaign.id))) == 0


def test_bulk_function_reports_a_waiting_fleet_slot_without_changing_it(db, monkeypatch):
    rel = platform_release(db)
    device = esp(db, TARGETS[3], "ZONE-MULTAN-01")
    from zk_add import zkt_ota_admission
    monkeypatch.setattr(zkt_ota_admission, "pending_offer_hold",
                        lambda _session, _connector, _release=None: "NATIONWIDE_TWO_UPGRADE_RESERVATIONS")
    result = create_for_zones(db, release_public_id=rel.release_id, zone_ids=[device.zone_id],
                              actor="StateHealthAdmin", idempotency_key="bulk-waiting-0001")
    [row] = result["results"]
    assert (row["outcome"], row["fleet_hold"]) == ("CREATED", "NATIONWIDE_TWO_UPGRADE_RESERVATIONS")
    deployment = db.scalar(select(FirmwareDeployment))
    assert (deployment.status, deployment.error_code) == ("PENDING", None)
    # The assignment path records the hold on the queued device; the list shows it as waiting.
    deployment.error_code = "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    deployment.error_message = "Waiting for the nationwide ZKT upgrade slot and exact target checks."
    db.flush()
    [summary] = campaign_page(db, limit=10)["rows"]
    assert summary["waiting"]["code"] == "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    assert summary["waiting"]["connector_id"] == device.connector_id


# --- Auto-complete -----------------------------------------------------------------------------------


def succeed(db, connector, deployment):
    """Each device progress request commits; flush between transitions like it."""
    for state, written in (("DOWNLOADING", 512), ("VERIFYING", 1024), ("READY_TO_BOOT", 1024)):
        record_progress(db, connector=connector, deployment_public_id=deployment.deployment_id,
                        state=state, bytes_written=written)
        db.flush()
    for state in ("BOOTED_PENDING", "RECONCILING", "SUCCEEDED"):
        record_progress(db, connector=connector, deployment_public_id=deployment.deployment_id, state=state,
                        bytes_written=1024, running_version="3.0.1", running_partition="ota_1",
                        image_sha256="c" * 64)
        db.flush()


def test_the_last_success_completes_the_campaign_and_frees_the_zone(db):
    rel = plain_release(db)
    first, second = plain_connector(db, "one"), plain_connector(db, "two")
    second.zone_id = first.zone_id
    row, (one, two) = campaign(db, rel, first.zone_id, deployments=[
        (first, "OFFERED", True, None), (second, "OFFERED", True, None)])
    succeed(db, first, one)
    assert (one.status, row.status) == ("SUCCEEDED", "ACTIVE")
    succeed(db, second, two)
    db.flush()
    assert (two.status, db.get(FirmwareCampaign, row.id).status) == ("SUCCEEDED", "COMPLETED")
    # A failure anywhere keeps the campaign for the owner (paused as before), never COMPLETED.
    other = plain_connector(db, "three")
    failing, (bad, good) = campaign(db, rel, other.zone_id, deployments=[
        (other, "OFFERED", True, None), (plain_connector(db, "four"), "OFFERED", True, None)])
    record_progress(db, connector=other, deployment_public_id=bad.deployment_id, state="FAILED",
                    bytes_written=0, error_code="DOWNLOAD_FAILED")
    db.flush()
    good_connector = db.get(Connector, good.connector_id)
    succeed(db, good_connector, good)
    db.flush()
    assert db.get(FirmwareCampaign, failing.id).status == "PAUSED"


# --- State-checked one-click controls ---------------------------------------------------------------


def test_controls_are_one_click_and_state_checked(db):
    rel = plain_release(db)
    device = plain_connector(db, "control")
    live, _ = campaign(db, rel, device.zone_id, deployments=[(device, "PENDING", False, None)])
    cancelled, _ = campaign(db, rel, device.zone_id, status="CANCELLED")
    completed, _ = campaign(db, rel, device.zone_id, status="COMPLETED")
    db.commit()
    client, headers = admin_client(db)

    def control(row, action, **body):
        return client.post(f"/api/v1/firmware/campaigns/{row.campaign_id}/{action}", json=body or None,
                           headers=headers)

    assert client.post(f"/api/v1/firmware/campaigns/{live.campaign_id}/pause").status_code == 403  # CSRF
    assert control(live, "resume").status_code == 409  # not paused
    paused = control(live, "pause")
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    assert control(live, "pause").status_code == 409
    for row in (cancelled, completed):
        for action in ("pause", "resume", "cancel"):
            refused = control(row, action)
            assert refused.status_code == 409, (row.status, action, refused.text)
    db.expire_all()
    # Resuming a cancelled campaign while its zone holds a live one was an
    # IntegrityError (500); it is now refused by state, a 409.
    assert (db.get(FirmwareCampaign, cancelled.id).status, db.get(FirmwareCampaign, completed.id).status) == (
        "CANCELLED", "COMPLETED")
    assert db.get(FirmwareCampaign, live.id).status == "PAUSED"
    resumed = control(live, "resume", reason="Site power restored", password="ignored")
    assert resumed.status_code == 200 and resumed.json()["status"] == "ACTIVE"
    cancelled_now = control(live, "cancel")
    assert cancelled_now.status_code == 200 and cancelled_now.json()["status"] == "CANCELLED"
    db.expire_all()
    assert db.scalar(select(FirmwareDeployment.status).where(FirmwareDeployment.campaign_id == live.id)) == "CANCELLED"
    assert [row.action for row in db.scalars(select(AuditEvent).where(
        AuditEvent.target_id == live.campaign_id).order_by(AuditEvent.id))] == [
        "FIRMWARE_CAMPAIGN_PAUSE", "FIRMWARE_CAMPAIGN_RESUME", "FIRMWARE_CAMPAIGN_CANCEL"]


# --- Archive ------------------------------------------------------------------------------------------


def test_archive_hides_finished_campaigns_and_keeps_their_evidence(db):
    rel = plain_release(db)
    device = plain_connector(db, "archive")
    live, _ = campaign(db, rel, device.zone_id, deployments=[(device, "PENDING", False, None)])
    cancelled, (evidence,) = campaign(db, rel, device.zone_id, status="CANCELLED",
                                      deployments=[(device, "CANCELLED", True, None)])
    completed, _ = campaign(db, rel, "ZONE-OTHER", status="COMPLETED")
    db.add(FirmwareEvent(deployment_id=evidence.id, state="OFFERED", details={}))
    db.commit()
    client, headers = admin_client(db)
    assert client.post(f"/api/v1/firmware/campaigns/{live.campaign_id}/archive", headers=headers).status_code == 409
    assert client.post(f"/api/v1/firmware/campaigns/{secrets.token_hex(16)}/archive",
                       headers=headers).status_code == 404
    first = client.post(f"/api/v1/firmware/campaigns/{cancelled.campaign_id}/archive", headers=headers)
    assert first.status_code == 200 and first.json()["archived_by"] == "StateHealthAdmin"
    again = client.post(f"/api/v1/firmware/campaigns/{cancelled.campaign_id}/archive", headers=headers)
    assert again.status_code == 200 and again.json()["archived_at"] == first.json()["archived_at"]
    assert len(audits(db, "FIRMWARE_CAMPAIGN_ARCHIVED")) == 1
    listed = client.get("/api/v1/firmware/campaigns", params={"view": "summary"}).json()
    assert {row["campaign_id"] for row in listed["rows"]} == {live.campaign_id, completed.campaign_id}
    assert listed["totals"]["campaigns"] == {"all": 2, "active": 1, "completed": 1}
    assert listed["totals"]["archived_campaigns"] == 1
    with_archived = client.get("/api/v1/firmware/campaigns", params={"include_archived": True}).json()
    assert {row["campaign_id"] for row in with_archived["rows"]} == {
        live.campaign_id, cancelled.campaign_id, completed.campaign_id}
    detail = client.get(f"/api/v1/firmware/campaigns/{cancelled.campaign_id}").json()
    assert detail["archived_at"] and detail["deployments"][0]["deployment_id"] == evidence.deployment_id
    bulk = client.post("/api/v1/firmware/campaigns/archive-cancelled", headers=headers)
    assert bulk.status_code == 200 and bulk.json() == {"archived": 1}
    assert client.post("/api/v1/firmware/campaigns/archive-cancelled", headers=headers).json() == {"archived": 0}
    db.expire_all()
    assert db.get(FirmwareCampaign, completed.id).archived_at is not None
    assert db.get(FirmwareCampaign, live.id).archived_at is None
    # Archiving never touches deployments or their events.
    assert db.get(FirmwareDeployment, evidence.id).status == "CANCELLED"
    assert db.scalar(select(func.count(FirmwareEvent.id))) == 1


# --- Clear firmware history ---------------------------------------------------------------------------


def test_clear_history_completes_cancels_keeps_deletes_and_archives(db):
    rel = plain_release(db)
    devices = {name: plain_connector(db, name) for name in
               ("done", "idle", "fresh", "stale", "offered", "shared", "reserved", "granted")}
    now = utc_now()
    done, _ = campaign(db, rel, devices["done"].zone_id, deployments=[(devices["done"], "SUCCEEDED", True, None)])
    idle, (idle_deployment,) = campaign(db, rel, devices["idle"].zone_id,
                                        deployments=[(devices["idle"], "PENDING", False, None)])
    db.add(FirmwareEvent(deployment_id=idle_deployment.id, state="STABILITY_WARNINGS_OVERRIDDEN", details={}))
    idle_id, idle_deployment_id = idle.campaign_id, idle_deployment.id
    fresh, _ = campaign(db, rel, devices["fresh"].zone_id,
                        deployments=[(devices["fresh"], "BOOTED_PENDING", True, now - timedelta(minutes=5))])
    stale, _ = campaign(db, rel, devices["stale"].zone_id,
                        deployments=[(devices["stale"], "VERIFYING", True, now - timedelta(hours=7))])
    offered, _ = campaign(db, rel, devices["offered"].zone_id, status="CANCELLED",
                          deployments=[(devices["offered"], "CANCELLED", True, None)])
    # A never-offered attempt whose ESP has other history is archived, never deleted.
    campaign(db, rel, devices["shared"].zone_id, status="COMPLETED",
             deployments=[(devices["shared"], "SUCCEEDED", True, None)])
    shared, _ = campaign(db, rel, devices["shared"].zone_id, status="CANCELLED",
                         deployments=[(devices["shared"], "CANCELLED", False, None)])
    reserved, (reservation,) = campaign(db, rel, devices["reserved"].zone_id, status="CANCELLED",
                                        deployments=[(devices["reserved"], "CANCELLED", False, None)])
    db.add(FirmwareEvent(deployment_id=reservation.id, state="HIL_SCOPE_RESERVED", details={}))
    granted, (grant_deployment,) = campaign(db, rel, devices["granted"].zone_id, status="CANCELLED",
                                            deployments=[(devices["granted"], "CANCELLED", False, None)])
    db.add(FirmwareDownloadGrant(token_hash=secrets.token_hex(32), deployment_id=grant_deployment.id,
                                 connector_id=devices["granted"].id, expires_at=now))
    db.commit()
    client, headers = admin_client(db)
    assert client.post("/api/v1/firmware/campaigns/clear-history").status_code == 403  # CSRF
    response = client.post("/api/v1/firmware/campaigns/clear-history", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {
        "completed": 1, "cancelled": 2, "deleted": 1, "archived": 7, "more": False,
        "kept_running": [{"campaign_id": fresh.campaign_id, "zone_id": fresh.zone_id, "connector": "fresh",
                          "connector_id": "connector-fresh", "state": "BOOTED_PENDING"}],
    }
    db.expire_all()
    status = {row.campaign_id: (row.status, row.archived_at is not None)
              for row in db.scalars(select(FirmwareCampaign))}
    assert status[done.campaign_id] == ("COMPLETED", True)
    assert idle_id not in status  # never offered: deleted with its deployment and event
    assert db.scalar(select(func.count(FirmwareDeployment.id)).where(
        FirmwareDeployment.connector_id == devices["idle"].id)) == 0
    assert db.scalar(select(func.count(FirmwareEvent.id)).where(FirmwareEvent.deployment_id == idle_deployment_id)) == 0
    assert status[fresh.campaign_id] == ("ACTIVE", False)
    assert status[stale.campaign_id] == ("CANCELLED", True)
    for row in (offered, shared, reserved, granted):
        assert status[row.campaign_id] == ("CANCELLED", True)
    # Evidence of every archived campaign is unchanged.
    assert db.scalar(select(FirmwareDeployment.status).where(FirmwareDeployment.campaign_id == stale.id)) == "VERIFYING"
    assert db.scalar(select(func.count(FirmwareEvent.id)).where(FirmwareEvent.deployment_id == reservation.id)) == 1
    [cleared] = audits(db, "FIRMWARE_HISTORY_CLEARED")
    assert cleared.after["deleted_ids"] == [idle_id]
    assert cleared.after["kept_running"] == [fresh.campaign_id]
    assert {row.target_id for row in audits(db, "FIRMWARE_CAMPAIGN_CANCEL")} == {idle_id, stale.campaign_id}
    # Safe to call twice: only the running install is reported again.
    again = client.post("/api/v1/firmware/campaigns/clear-history", headers=headers).json()
    assert {key: again[key] for key in ("completed", "cancelled", "deleted", "archived")} == {
        "completed": 0, "cancelled": 0, "deleted": 0, "archived": 0}
    assert [row["campaign_id"] for row in again["kept_running"]] == [fresh.campaign_id]
    assert len(audits(db, "FIRMWARE_HISTORY_CLEARED")) == 2


def test_clear_history_never_deletes_a_writer_attempt(db):
    rel = plain_release(db, "2.7.0")
    device = plain_connector(db, "writer")
    row, _ = campaign(db, rel, device.zone_id, status="CANCELLED",
                      deployments=[(device, "CANCELLED", False, None)])
    result = clear_history(db, actor="StateHealthAdmin")
    assert (result["deleted"], result["archived"]) == (0, 1)
    db.expire_all()
    assert db.get(FirmwareCampaign, row.id).archived_at is not None


def test_detail_serializes_archive_fields(db):
    rel = plain_release(db)
    device = plain_connector(db, "detail")
    row, _ = campaign(db, rel, device.zone_id, status="COMPLETED",
                      deployments=[(device, "SUCCEEDED", True, None)])
    row.archived_at, row.archived_by = utc_now(), "StateHealthAdmin"
    db.flush()
    detail = campaign_detail(db, row.campaign_id)
    assert detail["archived_by"] == "StateHealthAdmin" and detail["waiting"] is None
    assert campaign_page(db)["rows"] == []
    assert [item["campaign_id"] for item in campaign_page(db, include_archived=True)["rows"]] == [row.campaign_id]
