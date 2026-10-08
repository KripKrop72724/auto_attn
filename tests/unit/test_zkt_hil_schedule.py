"""Connectivity deferral uses real stored verdicts and stable campaign reservations."""

from datetime import timedelta
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from zk_add.models import Base, Connector, DeviceAlert, ReconciliationJob

from test_hil_scope import hil_session  # noqa: F401
from zk_add.hil_runs import _release_identity
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareEvent,
    FirmwareHilRun,
    FirmwareRelease,
    _permitted_hil_targets,
    preview_campaign_scope,
    create_campaign,
    assignment_for_connector,
    resolve_download,
)
from zk_add.time_utils import utc_now
from zk_add.zkt_bridge_contract import bridge_contract, signed_hil_targets, PREDECESSOR_IMAGES
from zk_add.zkt_hil_schedule import schedule, RESERVATION


def ready_event(session, release, device, deployment):
    now = utc_now()
    identity = _release_identity(release).model_dump(mode="json")
    target = next(t for t in signed_hil_targets() if t["connector_id"] == device.connector_id)
    run = FirmwareHilRun(
        run_id="ready-" + str(deployment.id),
        deployment_id=deployment.id,
        connector_id=device.id,
        release_id=release.id,
        actor="test",
        idempotency_key="ready-" + str(deployment.id),
        status="BRIDGE_READY",
        target=target,
        release_identity=identity,
        baseline={"profile": "BRIDGE_READINESS_V1", "boot_id": device.boot_id},
        started_at=now - timedelta(minutes=16),
        ends_at=now - timedelta(minutes=1),
        completed_at=now,
        result={"outcome": "READY", "reasons": []},
    )
    event = FirmwareEvent(
        deployment_id=deployment.id,
        state="BRIDGE_READY",
        details={
            **identity,
            "target": target,
            "run_id": run.run_id,
            "profile": "BRIDGE_READINESS_V1",
            "outcome": "READY",
        },
    )
    session.add_all([run, event])
    session.flush()
    return run, event


@pytest.fixture(params=["2.6.20", "2.6.21"])
def fleet(hil_session, request):  # noqa: F811
    session, release, devices = hil_session
    release.release_id, release.version = f"zone-lite-{request.param}", request.param
    release.minimum_bootstrap_version = "2.4.12"
    release.manifest = {
        **release.manifest,
        "release_id": release.release_id,
        "version": release.version,
        "firmware_family": "zkt",
        "project_name": "zone_lite",
        "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "minimum_bootstrap_version": "2.4.12",
        "runtime_profile": "ZKT_LEGACY",
        "queue_storage": bridge_contract(release.version),
        "hil_targets": signed_hil_targets(),
        "_hil_targets": signed_hil_targets()[:3],
    }
    session.add(
        FirmwareRelease(
            release_id="zone-lite-2.6.15",
            version="2.6.15",
            git_sha="d" * 40,
            image_sha256="e" * 64,
            image_size=1024,
            signing_key_id="test",
            partition_layout=release.partition_layout,
            storage_name="legacy",
            manifest_signature="test",
            state="HIL_ONLY",
            manifest={"application_sha256": PREDECESSOR_IMAGES["2.6.15"]},
        )
    )
    for index, (device, target) in enumerate(zip(devices, signed_hil_targets())):
        device.connector_id, device.hardware_id = target["connector_id"], target["mac"]
        device.is_spare = False
        device.firmware_version = "2.6.15"
        device.ota_running_partition = "ota_0"
        device.ota_image_sha256 = PREDECESSOR_IMAGES["2.6.15"]
        for key in ("serial", "expected_serial", "confirmed_serial"):
            setattr(device.zkt_device, key, target["terminal_serial"])
        device.boot_id = "boot-" + str(index)
        device.last_seen_at = utc_now() - timedelta(minutes=5)
        device.firmware_diagnostics_at = device.last_seen_at
        device.firmware_diagnostics = {
            "boot_id": device.boot_id,
            "storage": {
                "durability": "HEALTHY",
                "persistence_verified": True,
                "recovery_complete": True,
                "upgrade_ready": True,
            },
        }
    devices[1].connected = False
    campaign = FirmwareCampaign(
        campaign_id="canary",
        release_id=release.id,
        zone_id=devices[0].zone_id,
        actor="test",
        idempotency_key="canary",
        reason="test",
        typed_confirmation=release.version,
        status="COMPLETED",
    )
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id="canary",
        campaign_id=campaign.id,
        release_id=release.id,
        connector_id=devices[0].id,
        status="SUCCEEDED",
        target_version=release.version,
    )
    session.add(deployment)
    session.flush()
    ready_event(session, release, devices[0], deployment)
    return session, release, devices


def create(fleet, key="next", preview=None):
    session, release, devices = fleet
    preview = preview or preview_campaign_scope(
        session, release_public_id=release.release_id, zone_id=devices[2].zone_id
    )
    return create_campaign(
        session,
        release_public_id=release.release_id,
        zone_id=devices[2].zone_id,
        reason="test",
        typed_confirmation=release.version,
        actor="test",
        scope_token=preview["scope_token"],
        idempotency_key=key,
    )


def test_offline_deferral_preserves_signed_denominator_and_real_counts(fleet):
    decision = schedule(fleet[0], fleet[1])
    assert decision["denominator"] == 17 and sum(decision["counts"].values()) == 17
    assert decision["counts"]["PASSED"] == 1
    assert decision["rows"][1]["status"] == "DEFERRED_OFFLINE"
    assert decision["selected"] == signed_hil_targets()[2]
    assert fleet[1].manifest["hil_targets"] == signed_hil_targets()


@pytest.mark.parametrize(
    "fault",
    [
        "online",
        "missing",
        "wrong-serial",
        "security",
        "storage",
        "reader",
        "error",
        "identity",
        "future",
        "recent",
        "wrong-boot",
        "failed",
        "uncertain",
    ],
)
def test_only_connectivity_can_defer(fleet, fault):
    session, release, devices = fleet
    device = devices[1]
    if fault == "online":
        device.connected = True
    elif fault == "missing":
        device.connector_id = "not-in-inventory"
    elif fault == "wrong-serial":
        device.zkt_device.serial = "replacement"
    elif fault == "security":
        device.ota_capable = device.ota_secure_boot = False
    elif fault == "storage":
        device.firmware_diagnostics = {
            "boot_id": device.boot_id,
            "storage": {"durability": "FAILED"},
        }
    elif fault == "reader":
        device.firmware_diagnostics = {
            **device.firmware_diagnostics,
            "journal_runtime": {"phase": "HELD"},
        }
    elif fault == "error":
        device.last_error_code = "PERSISTENCE_FAILED"
    elif fault == "identity":
        device.zkt_device.writes_disabled_reason = "REVIEW_REQUIRED"
    elif fault == "future":
        device.last_seen_at = utc_now() + timedelta(minutes=1)
    elif fault == "recent":
        device.last_seen_at = utc_now()
    elif fault == "wrong-boot":
        device.boot_id = "different"
    else:
        campaign = session.scalar(select(FirmwareCampaign))
        session.add(
            FirmwareDeployment(
                deployment_id="bad",
                campaign_id=campaign.id,
                release_id=release.id,
                connector_id=device.id,
                status="FAILED" if fault == "failed" else "CANCELLED",
                offered_at=utc_now(),
                target_version=release.version,
            )
        )
    session.flush()
    decision = schedule(session, release)
    assert decision["selected"] == signed_hil_targets()[1]
    assert decision["rows"][1]["status"] != "DEFERRED_OFFLINE"


@pytest.mark.parametrize(
    "fault", ["missing-run", "failed-run", "wrong-image", "future", "cancelled"]
)
def test_first_canary_cannot_be_skipped_or_forged(fleet, fault):
    session, release, devices = fleet
    devices[0].connected = False
    run = session.scalar(select(FirmwareHilRun))
    if fault == "missing-run":
        session.delete(run)
    elif fault == "failed-run":
        run.status = "BRIDGE_INCOMPLETE"
    elif fault == "wrong-image":
        run.release_identity = {**run.release_identity, "application_sha256": "0" * 64}
    elif fault == "future":
        run.completed_at = utc_now() + timedelta(minutes=1)
    else:
        session.scalar(select(FirmwareCampaign)).status = "CANCELLED"
    session.flush()
    assert schedule(session, release)["selected"] == signed_hil_targets()[0]


def test_create_revalidates_entire_schedule_after_reconnect(fleet):
    session, release, devices = fleet
    preview = preview_campaign_scope(
        session, release_public_id=release.release_id, zone_id=devices[2].zone_id
    )
    devices[1].connected = True
    with pytest.raises(ValueError, match="scope changed"):
        create(fleet, preview=preview)
    assert (
        session.scalar(
            select(FirmwareDeployment).where(FirmwareDeployment.connector_id == devices[2].id)
        )
        is None
    )


def test_reservation_retains_target_when_earlier_device_returns(fleet):
    session, release, devices = fleet
    campaign = create(fleet)
    assert create(fleet, key="next").id == campaign.id
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == RESERVATION))
    assert len(event.details["deferred"]) == 1
    devices[1].connected = True
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[2]
    assigned = assignment_for_connector(
        session, connector=devices[2], public_base="https://add.test"
    )
    assert assigned is not None
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[2]
    assert resolve_download(session, assigned["download_url"].rsplit("/", 1)[1])[0].id == release.id


@pytest.mark.parametrize(
    "fault", ["tamper", "cancel-offered", "failed", "revoked", "paused", "canary-invalid"]
)
def test_reservation_never_bypasses_holds(fleet, fault):
    session, release, devices = fleet
    campaign = create(fleet)
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == RESERVATION))
    deployment = session.get(FirmwareDeployment, event.deployment_id)
    if fault == "tamper":
        event.details = {**event.details, "target": signed_hil_targets()[1]}
    elif fault == "cancel-offered":
        campaign.status, deployment.status, deployment.offered_at = (
            "CANCELLED",
            "CANCELLED",
            utc_now(),
        )
    elif fault == "failed":
        deployment.status = "FAILED"
    elif fault == "revoked":
        deployment.status = "RELEASE_REVOKED"
    elif fault == "paused":
        campaign.status = "PAUSED"
    else:
        session.scalar(select(FirmwareHilRun)).status = "BRIDGE_INCOMPLETE"
    session.flush()
    assert schedule(session, release)["hold"]
    assert (
        assignment_for_connector(session, connector=devices[2], public_base="https://add.test")
        is None
    )


def test_never_offered_cancellation_restores_original_priority(fleet):
    session, release, devices = fleet
    campaign = create(fleet)
    deployment = session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id)
    )
    campaign.status = deployment.status = "CANCELLED"
    devices[1].connected = True
    session.flush()
    assert schedule(session, release)["selected"] == signed_hil_targets()[1]


def test_real_verdict_settles_reservation_and_restores_original_priority(fleet):
    session, release, devices = fleet
    campaign = create(fleet)
    deployment = session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id)
    )
    deployment.status, campaign.status = "SUCCEEDED", "COMPLETED"
    ready_event(session, release, devices[2], deployment)
    devices[1].connected = True
    assert schedule(session, release)["selected"] == signed_hil_targets()[1]


@pytest.mark.parametrize("fault", ["storage", "identity", "security", "reader"])
def test_reservation_keeps_faults_visible_without_rewriting_existing_admission(fleet, fault):
    session, release, devices = fleet
    create(fleet)
    assigned = assignment_for_connector(
        session, connector=devices[2], public_base="https://add.test"
    )
    assert assigned is not None
    device = devices[2]
    if fault == "storage":
        device.firmware_diagnostics = {
            **device.firmware_diagnostics,
            "storage": {"durability": "FAILED"},
        }
    elif fault == "identity":
        device.zkt_device.writes_disabled_reason = "REVIEW_REQUIRED"
    elif fault == "security":
        device.ota_secure_boot = False
    else:
        device.firmware_diagnostics = {
            **device.firmware_diagnostics,
            "journal_runtime": {"phase": "HELD"},
        }
    session.flush()
    decision = schedule(session, release)
    assert decision["selected"] == signed_hil_targets()[2]
    assert decision["rows"][2]["status"] == "BLOCKED"
    assert decision["counts"]["PASSED"] == 1
    if fault == "security":
        assert (
            assignment_for_connector(session, connector=device, public_base="https://add.test")
            is None
        )


def test_existing_legacy_recovery_admission_survives_reservation_and_boot_recovery(fleet):
    session, release, devices = fleet
    session.delete(session.scalar(select(FirmwareHilRun)))
    device = devices[0]
    device.firmware_diagnostics = {
        "storage": {
            "durability": "DEGRADED",
            "persistence_verified": False,
            "recovery_complete": False,
            "upgrade_ready": False,
            "upgrade_error": "STORAGE_DIRECT_ROLLBACK_IMAGE_UNQUALIFIED",
        }
    }
    campaign = create(fleet)
    assigned = assignment_for_connector(session, connector=device, public_base="https://add.test")
    assert assigned is not None
    assert resolve_download(session, assigned["download_url"].rsplit("/", 1)[1])[0].id == release.id
    deployment = session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id)
    )
    deployment.status = "BOOTED_PENDING"
    device.firmware_diagnostics = {"journal_runtime": {"phase": "RECOVERING"}}
    session.flush()
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[0]
    assert schedule(session, release)["counts"]["PASSED"] == 0


def test_missing_old_diagnostics_are_untested_prerequisites_not_invented_failure(fleet):
    session, release, devices = fleet
    devices[1].firmware_diagnostics = {}
    devices[1].firmware_diagnostics_at = None
    devices[1].boot_id = None
    row = schedule(session, release)["rows"][1]
    assert row["status"] == "DEFERRED_OFFLINE"
    assert row["prerequisites"] == ["PRESERVATION_DIAGNOSTICS_NOT_OBSERVED"]


def test_never_reported_offline_capability_is_untested_but_explicit_block_is_a_barrier(fleet):
    session, release, devices = fleet
    device = devices[1]
    device.ota_capable = device.ota_secure_boot = device.ota_rollback_enabled = False
    device.ota_state = "LEGACY_MANUAL_UPDATE"
    decision = schedule(session, release)
    assert decision["rows"][1]["status"] == "DEFERRED_OFFLINE"
    assert "OTA_CAPABILITY_NOT_OBSERVED" in decision["rows"][1]["prerequisites"]
    assert decision["selected"] == signed_hil_targets()[2]
    device.ota_state = "OTA_BLOCKED"
    decision = schedule(session, release)
    assert decision["rows"][1]["status"] == "BLOCKED"
    assert decision["selected"] == signed_hil_targets()[1]


@pytest.mark.parametrize("fault", ["persistence", "security", "identity", "source"])
def test_new_canary_fault_holds_expansion_without_erasing_historical_pass(fleet, fault):
    session, release, devices = fleet
    preview = preview_campaign_scope(
        session, release_public_id=release.release_id, zone_id=devices[2].zone_id
    )
    device = devices[0]
    device.firmware_diagnostics_at = utc_now()
    if fault == "persistence":
        device.firmware_diagnostics = {
            **device.firmware_diagnostics,
            "storage": {"durability": "FAILED"},
        }
    elif fault == "security":
        device.ota_capable = device.ota_secure_boot = False
    elif fault == "source":
        session.add(
            ReconciliationJob(
                connector_id=device.id,
                zkt_device_id=device.zkt_device.id,
                status="NEEDS_ATTENTION",
                actor="test",
                reason="Diverged source",
                idempotency_key="new-source-fault",
                request_digest="a" * 64,
                updated_at=utc_now(),
            )
        )
    else:
        device.zkt_device.writes_disabled_reason = "REVIEW_REQUIRED"
    decision = schedule(session, release)
    assert decision["rows"][0]["status"] == "PASSED"
    assert decision["rows"][0]["current_hold"] and decision["hold"]
    with pytest.raises(ValueError, match="held"):
        create(fleet, preview=preview)


def test_normal_canary_recovery_does_not_invent_a_post_verdict_failure(fleet):
    session, release, devices = fleet
    devices[0].firmware_diagnostics_at = utc_now()
    devices[0].firmware_diagnostics = {
        "boot_id": devices[0].boot_id,
        "storage": {"durability": "DEGRADED"},
        "journal_runtime": {"phase": "RECOVERING"},
    }
    assert schedule(session, release)["hold"] is None


def test_post_verdict_storage_incident_does_not_expire_or_clear_on_missing_diagnostics(
    fleet, monkeypatch
):
    from zk_add import zkt_hil_schedule

    session, release, devices = fleet
    device = devices[0]
    fault_time = utc_now()
    device.firmware_diagnostics_at = fault_time
    device.firmware_diagnostics = {"boot_id": device.boot_id, "storage": {"durability": "DEGRADED"}}
    assert schedule(session, release)["hold"]
    monkeypatch.setattr(zkt_hil_schedule, "utc_now", lambda: fault_time + timedelta(hours=1))
    assert schedule(session, release)["hold"]
    # Production diagnostic ingestion retains this alert until verified local
    # persistence succeeds; a later old/empty heartbeat does not resolve it.
    alert = DeviceAlert(
        connector_id=device.id,
        code="ESP_DURABILITY_FAULT",
        severity="HIGH",
        state="OPEN",
        message="Preservation failed",
        first_seen_at=fault_time,
        last_seen_at=fault_time,
    )
    session.add(alert)
    device.firmware_diagnostics, device.firmware_diagnostics_at = None, None
    session.flush()
    assert schedule(session, release)["hold"]
    alert.state, alert.resolved_at = "RESOLVED", fault_time + timedelta(minutes=1)
    device.firmware_diagnostics = {
        "boot_id": device.boot_id,
        "storage": {
            "durability": "HEALTHY",
            "persistence_verified": True,
            "recovery_complete": True,
            "upgrade_ready": True,
        },
    }
    device.firmware_diagnostics_at = alert.resolved_at
    session.flush()
    assert schedule(session, release)["hold"] is None


@pytest.mark.parametrize("fault", [None, "image", "bytes", "partition", "unproven"])
def test_cancelled_offer_settles_only_after_verified_later_installation(fleet, fault):
    session, release, devices = fleet
    campaign = create(fleet)
    assert assignment_for_connector(session, connector=devices[2], public_base="https://add.test")
    old = session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id)
    )
    now = utc_now()
    old.offered_at, old.updated_at = now - timedelta(minutes=5), now - timedelta(minutes=4)
    old.status = campaign.status = "CANCELLED"
    recovery_release = session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.version == "2.6.15")
    )
    recovery_campaign = FirmwareCampaign(
        campaign_id="recovery",
        release_id=recovery_release.id,
        zone_id=devices[2].zone_id,
        status="COMPLETED",
        actor="test",
        idempotency_key="recovery",
        reason="verified recovery",
        typed_confirmation=recovery_release.version,
    )
    session.add(recovery_campaign)
    session.flush()
    later = FirmwareDeployment(
        deployment_id="recovery",
        campaign_id=recovery_campaign.id,
        release_id=recovery_release.id,
        connector_id=devices[2].id,
        status="SUCCEEDED",
        target_version=recovery_release.version,
        bytes_written=recovery_release.image_size,
        offered_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
    )
    session.add(later)
    session.flush()
    evidence = {
        "bytes_written": recovery_release.image_size,
        "image_sha256": PREDECESSOR_IMAGES["2.6.15"],
        "running_partition": "ota_0",
        "running_version": recovery_release.version,
        "error_code": None,
    }
    if fault == "image":
        evidence["image_sha256"] = "0" * 64
    elif fault == "bytes":
        evidence["bytes_written"] -= 1
    elif fault == "partition":
        evidence["running_partition"] = "factory"
    if fault != "unproven":
        session.add(
            FirmwareEvent(
                deployment_id=later.id,
                state="SUCCEEDED",
                created_at=later.completed_at,
                details=evidence,
            )
        )
    session.flush()
    decision = schedule(session, release)
    assert decision["counts"]["PASSED"] == 1
    if fault:
        assert decision["hold"] == "RESERVATION_INSTALLATION_HELD"
    else:
        assert decision["hold"] is None and decision["reservation"] is None
        assert decision["selected"] == signed_hil_targets()[2]


def test_new_attempt_does_not_resurrect_a_historically_settled_reservation(fleet):
    session, release, devices = fleet
    campaign = create(fleet)
    old = session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id)
    )
    campaign.status, old.status = "COMPLETED", "SUCCEEDED"
    ready_event(session, release, devices[2], old)
    session.add(
        FirmwareDeployment(
            deployment_id="new-attempt",
            campaign_id=campaign.id,
            release_id=release.id,
            connector_id=devices[2].id,
            status="FAILED",
            target_version=release.version,
        )
    )
    session.flush()
    decision = schedule(session, release)
    assert decision["hold"] is None and decision["reservation"] is None
    assert decision["rows"][2]["status"] == "BLOCKED"
    assert decision["counts"]["PASSED"] == 1


def test_changed_offline_failure_invalidates_preview_even_when_selected_target_unchanged(fleet):
    session, release, devices = fleet
    preview = preview_campaign_scope(
        session, release_public_id=release.release_id, zone_id=devices[2].zone_id
    )
    devices[0].last_error_code = "NEW_FAILURE_AFTER_HISTORICAL_PASS"
    # A passed device is historical evidence and does not become an invented
    # additional pass. A hard fault on the actually deferred device is a barrier.
    devices[1].last_error_code = "STORAGE_FAILURE"
    with pytest.raises(ValueError):
        create(fleet, preview=preview)


@pytest.fixture
def pg_fleet(fleet):
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None
    )
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    schema = "zkt_schedule_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"
        },
    )
    try:
        Base.metadata.create_all(engine)
        source = fleet[0]
        source.flush()
        with engine.begin() as db:
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in source.execute(select(table)).mappings()]
                if rows:
                    db.execute(table.insert(), rows)
                    if "id" in table.c:
                        db.execute(
                            text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :maximum)"),
                            {"table": table.name, "maximum": max(row["id"] for row in rows)},
                        )
        yield sessionmaker(engine, expire_on_commit=False), fleet[1].release_id, fleet[2][2].zone_id
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_concurrent_creates_cannot_reserve_two_targets(pg_fleet):
    sessions, release_id, zone_id = pg_fleet
    with sessions() as db:
        preview = preview_campaign_scope(db, release_public_id=release_id, zone_id=zone_id)
    waiting = Event()

    def second_create():
        with sessions() as db:
            waiting.set()
            with pytest.raises(ValueError, match="scope changed|unsettled reservation"):
                create_campaign(
                    db,
                    release_public_id=release_id,
                    zone_id=zone_id,
                    reason="race",
                    typed_confirmation=release_id.removeprefix("zone-lite-"),
                    actor="second",
                    scope_token=preview["scope_token"],
                    idempotency_key="second",
                )
            db.rollback()

    with sessions() as first, ThreadPoolExecutor(max_workers=1) as pool:
        create_campaign(
            first,
            release_public_id=release_id,
            zone_id=zone_id,
            reason="race",
            typed_confirmation=release_id.removeprefix("zone-lite-"),
            actor="first",
            scope_token=preview["scope_token"],
            idempotency_key="first",
        )
        task = pool.submit(second_create)
        assert waiting.wait(2)
        first.commit()
        task.result(timeout=10)
    with sessions() as db:
        assert (
            len(list(db.scalars(select(FirmwareEvent).where(FirmwareEvent.state == RESERVATION))))
            == 1
        )


def test_postgres_reconnect_committed_before_create_invalidates_preview(pg_fleet):
    sessions, release_id, zone_id = pg_fleet
    with sessions() as stale:
        preview = preview_campaign_scope(stale, release_public_id=release_id, zone_id=zone_id)
        with sessions.begin() as live:
            device = live.scalar(
                select(Connector).where(
                    Connector.connector_id == signed_hil_targets()[1]["connector_id"]
                )
            )
            device.connected = True
        with pytest.raises(ValueError, match="scope changed"):
            create_campaign(
                stale,
                release_public_id=release_id,
                zone_id=zone_id,
                reason="stale",
                typed_confirmation=release_id.removeprefix("zone-lite-"),
                actor="first",
                scope_token=preview["scope_token"],
                idempotency_key="stale",
            )
