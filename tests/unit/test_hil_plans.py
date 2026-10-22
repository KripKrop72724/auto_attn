"""One-approval staged HIL plans on SQLite and PostgreSQL.

Every campaign comes from the existing preflight and creation path, every offer
from ``assignment_for_connector`` and its fleet guard. The plan only decides
when to ask, and halts on anything that is not SUCCEEDED.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import os
import secrets
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from zk_add import hil_plans, ota
from zk_add.db import Base
from zk_add.hil_plans import FirmwareHilPlan, PlanHalt
from zk_add.models import AuditEvent, Connector, DeviceAlert, ZKTDevice
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease
from zk_add.settings import settings
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.time_utils import utc_now
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets

VERSION = "2.7.6"
APPLICATION = "e" * 64
# (nationwide index, zone). 13FL and 3FL share SLICTOWER; Swat is outside the plans.
DEVICES = {
    "13FL": (11, "ZONE-SLICTOWER-13FL"),
    "P02": (5, "ZONE-PESHAWAR-02"),
    "3FL": (0, "ZONE-SLICTOWER-3FL"),
    "SWAT": (1, "ZONE-SWAT-01"),
}
STAGES = [["ZONE-SLICTOWER-13FL", "ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]]


def signed_manifest(version=VERSION):
    baselines, images = platform_baselines(version)
    return {
        "application_sha256": APPLICATION, "firmware_family": "zkt", "git_sha": "a" * 40,
        "minimum_bootstrap_version": "2.4.12", "partition_layout": "zone-lite-ota-v1",
        "queue_storage": {"schema_version": 2, "read_format": 2, "reader_mask": 63, "write_format": 1,
                          "allowed_bootstrap_versions": list(baselines),
                          "allowed_bootstrap_images": dict(images)},
        "release_id": f"zone-lite-{version}", "version": version,
    }


@pytest.fixture(params=["sqlite", "postgres"])
def plan_store(tmp_path, request, monkeypatch):
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "hil_plan_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'plans.db'}", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def fast(connection, _record):  # disposable test file; durability is not under test
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA journal_mode=MEMORY")

    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    request.addfinalizer(cleanup)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        for fill, version, state in (("1", "2.4.12", "AVAILABLE"), ("2", "2.5.2", "AVAILABLE"),
                                     ("3", "2.6.15", "HIL_ONLY")):
            db.add(FirmwareRelease(
                release_id=f"zone-lite-{version}", version=version, git_sha="c" * 40, image_sha256=fill * 64,
                image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
                minimum_bootstrap_version="2.2.0", storage_name=f"hil/{version}.bin",
                manifest_signature="test-signature", state=state,
                manifest={"application_sha256": FLEET_BASELINE_IMAGES[version]}))
        # A registered journal release activates the nationwide ZKT offer guard.
        db.add(FirmwareRelease(
            release_id="zone-lite-2.7.0", version="2.7.0", git_sha="f" * 40, image_sha256="f" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/writer.bin", manifest_signature="test-signature",
            state="REVOKED", manifest={"application_sha256": "f" * 64}))
        signed = signed_manifest()
        db.add(FirmwareRelease(
            release_id=signed["release_id"], version=VERSION, git_sha="a" * 40, image_sha256="b" * 64,
            image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
            minimum_bootstrap_version="2.4.12", storage_name="hil/platform.bin", manifest_signature="test-signature",
            state="HIL_ONLY", manifest={**signed, "_publication_mode": "HIL_ONLY", "_hil_target_mac": None,
                                        "_hil_targets": signed_hil_targets()}))
        for index, zone in DEVICES.values():
            target = TARGETS[index].identity
            device = Connector(
                connector_id=target.connector_id, hardware_id=target.mac, zone_id=zone, zone_name=zone,
                device_id="1", display_name=TARGETS[index].name, firmware_version="zone-lite-2.6.15",
                connected=True, active=True, is_spare=False, ota_capable=True, ota_secure_boot=True,
                ota_rollback_enabled=True, ota_partition_layout=ota.OTA_LAYOUT, ota_running_partition="ota_0",
                ota_image_sha256=FLEET_BASELINE_IMAGES["2.6.15"], last_seen_at=utc_now())
            device.zkt_device = ZKTDevice(
                serial=target.terminal_serial, expected_serial=target.terminal_serial,
                confirmed_serial=target.terminal_serial, terminal_binding_state="CONFIRMED")
            db.add(device)
        db.commit()

    @contextmanager
    def scope():
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    scope.sessions = sessions
    scope.dialect = request.param
    return scope


def release_id():
    return f"zone-lite-{VERSION}"


def approve(scope, stages=STAGES, *, actor="StateHealthAdmin", key="plan-approval-1", **options):
    with scope() as session:
        preview = hil_plans.resolve_plan(session, release_public_id=release_id(), stages=stages, **options)
        session.rollback()
    with scope() as session:
        plan, created = hil_plans.create_plan(
            session, release_public_id=release_id(), stages=stages, reason="Owner approved staged HIL",
            typed_confirmation=VERSION, plan_digest=preview["plan_digest"], actor=actor,
            idempotency_key=key, **options)
        assert created
        return plan.plan_id


def plan_row(scope, plan_id):
    with scope() as session:
        return session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))


def device(session, name):
    return session.scalar(select(Connector).where(Connector.connector_id == TARGETS[DEVICES[name][0]].identity.connector_id))


def deployment_for(session, name):
    connector = device(session, name)
    return session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.connector_id == connector.id)
                          .order_by(FirmwareDeployment.id.desc()).limit(1))


def offer(scope, name):
    with scope() as session:
        return ota.assignment_for_connector(session, connector=device(session, name),
                                            public_base="https://test.invalid")


def progress(scope, name, *states, error=None):
    with scope() as session:
        connector = device(session, name)
        deployment = deployment_for(session, name)
        release = session.get(FirmwareRelease, deployment.release_id)
        for state in states:
            booted = state in {"BOOTED_PENDING", "RECONCILING", "SUCCEEDED"}
            ota.record_progress(
                session, connector=connector, deployment_public_id=deployment.deployment_id, state=state,
                bytes_written=release.image_size if state not in {"OFFERED", "DOWNLOADING"} else 0,
                running_version=release.version if booted else None,
                running_partition="ota_1" if booted else None,
                image_sha256=APPLICATION if booted else None, error_code=error)


def install(scope, name):
    assert offer(scope, name)["version"] == VERSION
    progress(scope, name, "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING",
             "SUCCEEDED")


def campaigns(scope, plan_id):
    with scope() as session:
        return list(session.scalars(select(FirmwareCampaign).where(
            FirmwareCampaign.actor == f"hil-plan:{plan_id}").order_by(FirmwareCampaign.id)))


def audits(scope, action):
    with scope() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)))


def tick(scope):
    return hil_plans.tick(scope)


def assert_halted(scope, plan_id, code_prefix, *, alerted=None):
    plan = plan_row(scope, plan_id)
    assert plan.status == "HALTED", (plan.status, plan.halt_code, plan.halt_reason)
    assert plan.halt_code.startswith(code_prefix), plan.halt_code
    assert plan.open_guard is None
    halt = audits(scope, "FIRMWARE_HIL_PLAN_HALTED")[-1]
    assert halt.actor == "system:hil-plan" and halt.target_id == plan_id
    assert halt.after["approved_by"] == "StateHealthAdmin" and halt.after["code"] == plan.halt_code
    with scope() as session:
        alerts = list(session.scalars(select(DeviceAlert).where(DeviceAlert.code == "FIRMWARE_HIL_PLAN_HALTED")))
        assert len(alerts) == 1 and alerts[0].details["plan_id"] == plan_id
        if alerted:
            assert alerts[0].connector_id == device(session, alerted).id
    # Halted means halted: nothing further is ever created.
    before = len(campaigns(scope, plan_id))
    tick(scope)
    tick(scope)
    assert len(campaigns(scope, plan_id)) == before
    return plan


# --------------------------------------------------------------------------
# Happy path


def test_two_stages_run_to_completion_through_the_existing_paths(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    approved = plan_row(scope, plan_id)
    assert approved.status == "RUNNING" and approved.open_guard == "OPEN"
    assert approved.expires_at - approved.approved_at == timedelta(hours=72)
    tick(scope)
    stage_one = campaigns(scope, plan_id)
    assert {row.zone_id for row in stage_one} == set(STAGES[0])
    assert all(row.status == "ACTIVE" and row.eligible_count == 1 and row.typed_confirmation == VERSION
               and f"HIL plan {plan_id} stage 1 of 2, approved by StateHealthAdmin" in row.reason
               for row in stage_one)
    tick(scope)  # restart-safe: no duplicates
    assert len(campaigns(scope, plan_id)) == 2
    # Both offers pass the unchanged fleet guard: two slots, two locations.
    install(scope, "13FL")
    tick(scope)
    assert plan_row(scope, plan_id).current_stage == 0
    install(scope, "P02")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.current_stage == 1 and plan.stage_started_at is None
    tick(scope)
    assert [row.zone_id for row in campaigns(scope, plan_id)][2:] == STAGES[1]
    install(scope, "3FL")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "COMPLETED" and plan.open_guard is None and plan.halt_code is None
    created = audits(scope, "FIRMWARE_HIL_PLAN_CAMPAIGN_CREATED")
    assert len(created) == 3 and all(row.actor == "system:hil-plan" and row.after["plan_id"] == plan_id
                                     and row.after["approved_by"] == "StateHealthAdmin" for row in created)
    assert len(audits(scope, "FIRMWARE_HIL_PLAN_STAGE_SUCCEEDED")) == 2
    assert len(audits(scope, "FIRMWARE_HIL_PLAN_COMPLETED")) == 1
    with scope() as session:
        view = hil_plans.plan_detail(session, plan_id)
        assert [stage["status"] for stage in view["stages"]] == ["SUCCEEDED", "SUCCEEDED"]
        assert all(row["deployment_status"] == "SUCCEEDED" for stage in view["stages"] for row in stage["devices"])
        assert session.scalar(select(func.count()).select_from(DeviceAlert)) == 0


def test_a_later_plan_may_be_approved_once_the_first_is_finished(plan_store):
    scope = plan_store
    first = approve(scope, [["ZONE-SWAT-01"]])
    with pytest.raises(ValueError, match="still running"):
        approve(scope, [["ZONE-PESHAWAR-02"]], key="plan-approval-2")
    tick(scope)
    install(scope, "SWAT")
    tick(scope)
    assert plan_row(scope, first).status == "COMPLETED"
    approve(scope, [["ZONE-PESHAWAR-02"]], key="plan-approval-2")


# --------------------------------------------------------------------------
# Halts


@pytest.mark.parametrize("kind", ["FAILED", "ROLLED_BACK", "CANCELLED_DEPLOYMENT", "CAMPAIGN_PAUSED"])
def test_unsuccessful_stage_device_halts_and_never_starts_the_next_stage(plan_store, kind):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "13FL")
    if kind == "FAILED":
        progress(scope, "13FL", "DOWNLOADING", "FAILED", error="DOWNLOAD_HASH_MISMATCH")
    elif kind == "ROLLED_BACK":
        progress(scope, "13FL", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "ROLLED_BACK",
                 error="BOOT_HEALTH_TIMEOUT")
    elif kind == "CANCELLED_DEPLOYMENT":
        progress(scope, "13FL", "CANCELLED")
    else:
        with scope() as session:
            campaign = session.get(FirmwareCampaign, deployment_for(session, "13FL").campaign_id)
            campaign.status, campaign.pause_reason = "PAUSED", "Owner paused for review"
    tick(scope)
    expected = {"FAILED": "DEPLOYMENT_FAILED", "ROLLED_BACK": "DEPLOYMENT_ROLLED_BACK",
                "CANCELLED_DEPLOYMENT": "DEPLOYMENT_CANCELLED", "CAMPAIGN_PAUSED": "CAMPAIGN_PAUSED"}[kind]
    assert_halted(scope, plan_id, expected, alerted="13FL")
    with scope() as session:
        # The never-offered sibling is paused so it is not offered after the halt.
        sibling = session.get(FirmwareCampaign, deployment_for(session, "P02").campaign_id)
        assert sibling.status == "PAUSED" and plan_id in sibling.pause_reason
        assert deployment_for(session, "P02").status == "PENDING"
        assert deployment_for(session, "3FL") is None
    assert offer(scope, "P02") is None


def test_admission_refusal_by_the_fleet_guard_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    # An operator's own campaign takes a slot after the stage started.
    with scope() as session:
        preview = ota.preview_campaign_scope(session, release_public_id=release_id(), zone_id="ZONE-SWAT-01")
        ota.create_campaign(session, release_public_id=release_id(), zone_id="ZONE-SWAT-01", reason="Manual HIL",
                            typed_confirmation=VERSION, actor="StateHealthAdmin",
                            scope_token=preview["scope_token"], idempotency_key="manual-swat")
    assert offer(scope, "SWAT")
    assert offer(scope, "13FL")
    assert offer(scope, "P02") is None  # third unsettled upgrade
    with scope() as session:
        assert deployment_for(session, "P02").error_code == "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    tick(scope)
    assert_halted(scope, plan_id, "ADMISSION_REFUSED:NATIONWIDE_TWO_UPGRADE_RESERVATIONS", alerted="P02")
    with scope() as session:
        # 13FL already received bytes: its campaign is left to finish safely.
        assert session.get(FirmwareCampaign, deployment_for(session, "13FL").campaign_id).status == "ACTIVE"


def test_post_install_hold_halts_before_the_next_stage(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    install(scope, "13FL")
    install(scope, "P02")
    with scope() as session:
        device(session, "P02").ota_secure_boot = False
    tick(scope)
    assert_halted(scope, plan_id, "POST_INSTALL_HOLD:SECURITY_CAPABILITY_REGRESSED", alerted="P02")
    with scope() as session:
        assert deployment_for(session, "3FL") is None


def test_preflight_refusal_at_stage_start_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    install(scope, "13FL")
    install(scope, "P02")
    with scope() as session:
        device(session, "3FL").zkt_device.terminal_binding_state = "PENDING_CONFIRMATION"
    tick(scope)  # advance
    tick(scope)  # stage 2 preflight
    assert_halted(scope, plan_id, "PREFLIGHT_REFUSED", alerted="3FL")
    with scope() as session:
        assert deployment_for(session, "3FL") is None


def test_preflight_warning_halts_without_creating_any_stage_campaign(plan_store, monkeypatch):
    scope = plan_store
    plan_id = approve(scope)
    original = ota.preview_campaign_scope

    def warned(session, **kwargs):
        result = original(session, **kwargs)
        if kwargs["zone_id"] == "ZONE-PESHAWAR-02":
            result["stability_warnings"] = [{"code": "ESP_RESTART_LOOP", "message": "ESP restarted 4 times"}]
        return result
    monkeypatch.setattr(ota, "preview_campaign_scope", warned)
    tick(scope)
    plan = assert_halted(scope, plan_id, "PREFLIGHT_WARNING", alerted="P02")
    assert "ESP restarted 4 times" in plan.halt_reason
    # 13FL passed its preflight but the stage is atomic: no campaign at all.
    assert campaigns(scope, plan_id) == []


def test_campaign_creation_refusal_rolls_back_the_whole_stage(plan_store, monkeypatch):
    scope = plan_store
    plan_id = approve(scope)
    original = ota.create_campaign

    def refuse_second(session, **kwargs):
        if kwargs["zone_id"] == "ZONE-PESHAWAR-02":
            raise ValueError("Nationwide HIL scope already has an unsettled reservation.")
        return original(session, **kwargs)
    monkeypatch.setattr(ota, "create_campaign", refuse_second)
    tick(scope)
    assert_halted(scope, plan_id, "CAMPAIGN_REFUSED", alerted="P02")
    assert campaigns(scope, plan_id) == []


def test_release_revocation_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    with scope() as session:
        release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id()))
        release.state, release.revoked_at = "REVOKED", utc_now()
    tick(scope)
    assert_halted(scope, plan_id, "RELEASE_UNAVAILABLE")


def test_approved_plan_is_immutable(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    with scope() as session:
        plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))
        stages = [list(stage) for stage in plan.stages]
        stages[1][0] = {**stages[1][0], "zone_id": "ZONE-SWAT-01"}
        plan.stages = stages
    tick(scope)
    assert_halted(scope, plan_id, "PLAN_PIN_CHANGED")
    assert campaigns(scope, plan_id) == []


def test_stage_timeout_halts_a_stalled_installation(plan_store, monkeypatch):
    scope = plan_store
    plan_id = approve(scope, stage_timeout_minutes=30)
    tick(scope)
    assert offer(scope, "13FL")
    later = utc_now() + timedelta(minutes=31)
    monkeypatch.setattr(hil_plans, "utc_now", lambda: later)
    tick(scope)
    assert_halted(scope, plan_id, "STAGE_TIMEOUT")


# --------------------------------------------------------------------------
# Guard and slot contention


def manual_offer(scope, name):
    zone = DEVICES[name][1]
    with scope() as session:
        preview = ota.preview_campaign_scope(session, release_public_id=release_id(), zone_id=zone)
        ota.create_campaign(session, release_public_id=release_id(), zone_id=zone, reason="Manual HIL",
                            typed_confirmation=VERSION, actor="StateHealthAdmin",
                            scope_token=preview["scope_token"], idempotency_key=f"manual-{name}")
    assert offer(scope, name)


def test_busy_fleet_slots_make_the_stage_wait_then_start(plan_store):
    scope = plan_store
    manual_offer(scope, "SWAT")
    plan_id = approve(scope)
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "RUNNING" and plan.waiting_reason.startswith("FLEET_SLOTS_BUSY")
    assert campaigns(scope, plan_id) == []
    # RECONCILING still holds the slot.
    progress(scope, "SWAT", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING")
    tick(scope)
    assert campaigns(scope, plan_id) == []
    progress(scope, "SWAT", "SUCCEEDED")
    tick(scope)
    assert {row.zone_id for row in campaigns(scope, plan_id)} == set(STAGES[0])
    assert plan_row(scope, plan_id).waiting_reason is None


def test_a_busy_location_waits_and_the_wait_is_bounded(plan_store, monkeypatch):
    scope = plan_store
    manual_offer(scope, "3FL")  # SLICTOWER is busy
    plan_id = approve(scope, [["ZONE-SLICTOWER-13FL"]], stage_timeout_minutes=30)
    tick(scope)
    assert plan_row(scope, plan_id).waiting_reason.startswith("LOCATION_BUSY: SLICTOWER")
    assert campaigns(scope, plan_id) == []
    later = utc_now() + timedelta(minutes=31)
    monkeypatch.setattr(hil_plans, "utc_now", lambda: later)
    tick(scope)
    assert_halted(scope, plan_id, "STAGE_START_TIMEOUT")


def test_an_offline_device_waits_without_a_campaign(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    with scope() as session:
        device(session, "P02").connected = False
    tick(scope)
    assert plan_row(scope, plan_id).waiting_reason == "DEVICE_OFFLINE: ZONE-PESHAWAR-02"
    assert campaigns(scope, plan_id) == []
    with scope() as session:
        device(session, "P02").connected = True
    tick(scope)
    assert len(campaigns(scope, plan_id)) == 2


def test_a_stage_must_fit_the_fleet_guard(plan_store):
    scope = plan_store
    with scope() as session:
        with pytest.raises(ValueError, match="one upgrade per location"):
            hil_plans.resolve_plan(session, release_public_id=release_id(),
                                   stages=[["ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-3FL"]])
        with pytest.raises(ValueError, match="one or two devices"):
            hil_plans.resolve_plan(session, release_public_id=release_id(),
                                   stages=[["ZONE-SLICTOWER-13FL", "ZONE-PESHAWAR-02", "ZONE-SWAT-01"]])
        with pytest.raises(ValueError, match="only once"):
            hil_plans.resolve_plan(session, release_public_id=release_id(),
                                   stages=[["ZONE-PESHAWAR-02"], ["ZONE-PESHAWAR-02"]])


def test_an_open_campaign_in_a_plan_zone_refuses_approval(plan_store):
    scope = plan_store
    manual_offer(scope, "P02")
    with scope() as session:
        with pytest.raises(ValueError, match="ZONE-PESHAWAR-02 \\(active campaign"):
            hil_plans.resolve_plan(session, release_public_id=release_id(), stages=STAGES)


# --------------------------------------------------------------------------
# Expiry, restart and concurrency


def test_expiry_stops_the_plan_and_pauses_never_offered_campaigns(plan_store, monkeypatch):
    scope = plan_store
    plan_id = approve(scope, expires_in_hours=1)
    tick(scope)
    assert offer(scope, "13FL")
    later = utc_now() + timedelta(hours=1, seconds=1)
    monkeypatch.setattr(hil_plans, "utc_now", lambda: later)
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "EXPIRED" and plan.halt_code == "PLAN_EXPIRED" and plan.open_guard is None
    with scope() as session:
        assert session.get(FirmwareCampaign, deployment_for(session, "P02").campaign_id).status == "PAUSED"
        # Bytes already offered: that campaign continues under its own safety states.
        assert session.get(FirmwareCampaign, deployment_for(session, "13FL").campaign_id).status == "ACTIVE"
        assert session.scalar(select(func.count()).select_from(DeviceAlert)
                              .where(DeviceAlert.code == "FIRMWARE_HIL_PLAN_HALTED")) == 1
    assert len(audits(scope, "FIRMWARE_HIL_PLAN_EXPIRED")) == 1


def test_a_crash_before_commit_leaves_nothing_and_the_restart_starts_the_stage_once(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    session = scope.sessions()
    plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == plan_id))
    hil_plans.advance_plan(session, plan.id)
    session.flush()
    session.close()  # process dies: the transaction never commits
    assert campaigns(scope, plan_id) == []
    for _ in range(3):  # a restarted scheduler repeats its step
        tick(scope)
    assert len(campaigns(scope, plan_id)) == 2
    assert len(audits(scope, "FIRMWARE_HIL_PLAN_CAMPAIGN_CREATED")) == 2


def test_two_workers_start_a_stage_once(plan_store):
    scope = plan_store
    if scope.dialect != "postgres":
        pytest.skip("Row locks are a PostgreSQL property; SQLite has one writer")
    plan_id = approve(scope)
    with scope() as session:
        plan_pk = session.scalar(select(FirmwareHilPlan.id).where(FirmwareHilPlan.plan_id == plan_id))
    start = Barrier(2)

    def worker():
        session = scope.sessions()
        try:
            start.wait(timeout=10)
            try:
                result = hil_plans.advance_plan(session, plan_pk)
            except PlanHalt as halt:
                session.rollback()
                result = hil_plans.record_halt(session, plan_pk, halt)
            session.commit()
            return result
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(worker) for _ in range(2)]]
    assert sum(1 for result in results if result and result.get("campaigns")) == 1
    assert len(campaigns(scope, plan_id)) == 2
    assert plan_row(scope, plan_id).status == "RUNNING"


def test_two_workers_cannot_approve_two_open_plans(plan_store):
    scope = plan_store
    if scope.dialect != "postgres":
        pytest.skip("Advisory and row locks are a PostgreSQL property")
    with scope() as session:
        digests = {zone: hil_plans.resolve_plan(session, release_public_id=release_id(),
                                                stages=[[zone]])["plan_digest"]
                   for zone in ("ZONE-SWAT-01", "ZONE-PESHAWAR-02")}
        session.rollback()
    start = Barrier(2)

    def worker(zone):
        session = scope.sessions()
        try:
            start.wait(timeout=10)
            plan, _ = hil_plans.create_plan(
                session, release_public_id=release_id(), stages=[[zone]], reason="Owner approved staged HIL",
                typed_confirmation=VERSION, plan_digest=digests[zone], actor="StateHealthAdmin",
                idempotency_key=f"race-{zone}")
            session.commit()
            return plan.plan_id
        except ValueError as error:
            session.rollback()
            return str(error)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in
                   [pool.submit(worker, zone) for zone in digests]]
    assert sum("still running" in result for result in results) == 1
    with scope() as session:
        assert session.scalar(select(func.count()).select_from(FirmwareHilPlan)) == 1


def test_approval_replay_is_idempotent_and_bound_to_its_digest(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    with scope() as session:
        digest = session.scalar(select(FirmwareHilPlan.plan_digest).where(FirmwareHilPlan.plan_id == plan_id))
        plan, created = hil_plans.create_plan(
            session, release_public_id=release_id(), stages=STAGES, reason="Owner approved staged HIL",
            typed_confirmation=VERSION, plan_digest=digest, actor="StateHealthAdmin",
            idempotency_key="plan-approval-1")
        assert plan.plan_id == plan_id and not created
        with pytest.raises(ValueError, match="another HIL plan"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=STAGES, reason="x" * 10,
                typed_confirmation=VERSION, plan_digest="0" * 64, actor="StateHealthAdmin",
                idempotency_key="plan-approval-1")


def test_a_changed_plan_needs_a_fresh_preview(plan_store):
    scope = plan_store
    with scope() as session:
        digest = hil_plans.resolve_plan(session, release_public_id=release_id(), stages=STAGES)["plan_digest"]
        session.rollback()
    with scope() as session:
        with pytest.raises(ValueError, match="Preview it again"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=[STAGES[1], STAGES[0]],
                reason="Owner approved staged HIL", typed_confirmation=VERSION, plan_digest=digest,
                actor="StateHealthAdmin", idempotency_key="reordered")
        with pytest.raises(ValueError, match="Typed firmware version"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=STAGES, reason="Owner approved staged HIL",
                typed_confirmation="2.7.5", plan_digest=digest, actor="StateHealthAdmin",
                idempotency_key="mistyped")


# --------------------------------------------------------------------------
# Scope


def test_only_exposed_hil_targets_of_a_hil_only_release_are_allowed(plan_store):
    scope = plan_store
    with scope() as session:
        release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id()))
        # Expose only the first six of the signed seventeen: 13FL (index 11) is outside.
        release.manifest = {**release.manifest, "_hil_targets": signed_hil_targets()[:6]}
    with scope() as session:
        with pytest.raises(ValueError, match="ZONE-SLICTOWER-13FL: no active device in this zone is an exact HIL"):
            hil_plans.resolve_plan(session, release_public_id=release_id(), stages=STAGES)
        assert hil_plans.resolve_plan(session, release_public_id=release_id(),
                                      stages=[["ZONE-PESHAWAR-02"]])["stages"][0][0]["location"] == "PESHAWAR"
        session.rollback()
    with scope() as session:
        # A swapped ESP is not the signed target even in the right zone.
        device(session, "P02").hardware_id = "aa:bb:cc:dd:ee:ff"
    with scope() as session:
        with pytest.raises(ValueError, match="ZONE-PESHAWAR-02: no active device"):
            hil_plans.resolve_plan(session, release_public_id=release_id(), stages=[["ZONE-PESHAWAR-02"]])
    with scope() as session:
        session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id())).state = "AVAILABLE"
    with scope() as session:
        with pytest.raises(ValueError, match="HIL_ONLY release"):
            hil_plans.resolve_plan(session, release_public_id=release_id(), stages=[["ZONE-SWAT-01"]])


# --------------------------------------------------------------------------
# Owner controls


def test_cancel_is_refused_while_a_deployment_is_offered(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "13FL")
    with scope() as session:
        with pytest.raises(ValueError, match="is OFFERED"):
            hil_plans.control_plan(session, plan_id=plan_id, action="cancel", actor="StateHealthAdmin",
                                   reason="Stop this plan now")
    progress(scope, "13FL", "DOWNLOADING")
    with scope() as session:
        with pytest.raises(ValueError, match="is DOWNLOADING"):
            hil_plans.control_plan(session, plan_id=plan_id, action="cancel", actor="StateHealthAdmin",
                                   reason="Stop this plan now")
    with scope() as session:
        assert plan_row(scope, plan_id).status == "RUNNING"
        assert deployment_for(session, "13FL").status == "DOWNLOADING"
        assert deployment_for(session, "P02").status == "PENDING"
    progress(scope, "13FL", "VERIFYING")
    with scope() as session:
        result = hil_plans.control_plan(session, plan_id=plan_id, action="cancel", actor="StateHealthAdmin",
                                        reason="Stop this plan now")
    assert result["status"] == "CANCELLED"
    with scope() as session:
        # Only the never-offered campaign is cancelled; VERIFYING continues.
        assert deployment_for(session, "P02").status == "CANCELLED"
        assert session.get(FirmwareCampaign, deployment_for(session, "P02").campaign_id).status == "CANCELLED"
        assert deployment_for(session, "13FL").status == "VERIFYING"
        assert session.get(FirmwareCampaign, deployment_for(session, "13FL").campaign_id).status == "ACTIVE"
    cancelled = audits(scope, "FIRMWARE_HIL_PLAN_CANCELLED")
    assert len(cancelled) == 1 and cancelled[0].actor == "StateHealthAdmin"
    tick(scope)
    assert plan_row(scope, plan_id).status == "CANCELLED"


def test_pause_holds_the_next_stage_and_resume_continues(plan_store):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-SWAT-01"], ["ZONE-PESHAWAR-02"]])
    tick(scope)
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="pause", actor="StateHealthAdmin",
                               reason="Owner wants to look first")
    install(scope, "SWAT")
    tick(scope)
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "PAUSED" and plan.current_stage == 1
    assert len(campaigns(scope, plan_id)) == 1
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="resume", actor="StateHealthAdmin",
                               reason="Owner reviewed stage one")
    tick(scope)
    assert [row.zone_id for row in campaigns(scope, plan_id)] == ["ZONE-SWAT-01", "ZONE-PESHAWAR-02"]


def test_a_failure_while_paused_still_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="pause", actor="StateHealthAdmin",
                               reason="Owner wants to look first")
    assert offer(scope, "P02")
    progress(scope, "P02", "DOWNLOADING", "FAILED", error="DOWNLOAD_TIMEOUT")
    tick(scope)
    assert_halted(scope, plan_id, "DEPLOYMENT_FAILED", alerted="P02")


# --------------------------------------------------------------------------
# API: step-up, CSRF, contract


@pytest.fixture
def api_client(plan_store, monkeypatch):
    from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
    from zk_add.web import app, get_db
    password = "synthetic-plan-password"
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password(password))
    with plan_store() as session:
        token, admin = create_admin_session(session, username="StateHealthAdmin", ip_address="127.0.0.1",
                                            user_agent="pytest")
        csrf = admin.csrf_token

    def get_session():
        session = plan_store.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setitem(app.dependency_overrides, get_db, get_session)
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, token)
    return client, {"X-CSRF-Token": csrf}, password


def test_api_requires_csrf_and_the_password_step_up(plan_store, api_client):
    client, headers, password = api_client
    preview = client.post("/api/v1/firmware/hil-plans/preview",
                          json={"release_id": release_id(), "stages": STAGES})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert [[row["zone_id"] for row in stage] for stage in body["stages"]] == STAGES
    approval = {"release_id": release_id(), "stages": STAGES, "reason": "Owner approved staged HIL",
                "typed_confirmation": VERSION, "plan_digest": body["plan_digest"],
                "idempotency_key": "api-plan-approval"}
    assert client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}).status_code == 403
    refused = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": "wrong"}, headers=headers)
    assert refused.status_code == 403
    assert client.post("/api/v1/firmware/hil-plans", json=approval, headers=headers).status_code == 422
    with plan_store() as session:
        assert session.scalar(select(func.count()).select_from(FirmwareHilPlan)) == 0
    created = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}, headers=headers)
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    assert created.json()["created"] is True and created.json()["status"] == "RUNNING"
    replay = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}, headers=headers)
    assert replay.status_code == 201 and replay.json()["plan_id"] == plan_id and replay.json()["created"] is False
    approved = audits(plan_store, "FIRMWARE_HIL_PLAN_APPROVED")
    assert len(approved) == 1 and approved[0].actor == "StateHealthAdmin"
    assert approved[0].after["plan_digest"] == body["plan_digest"]
    control = {"reason": "Owner pauses this plan", "password": "wrong"}
    assert client.post(f"/api/v1/firmware/hil-plans/{plan_id}/pause", json=control,
                       headers=headers).status_code == 403
    assert plan_row(plan_store, plan_id).status == "RUNNING"
    paused = client.post(f"/api/v1/firmware/hil-plans/{plan_id}/pause",
                         json={**control, "password": password}, headers=headers)
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    listed = client.get("/api/v1/firmware/hil-plans").json()["rows"]
    assert [row["plan_id"] for row in listed] == [plan_id]
    assert client.get(f"/api/v1/firmware/hil-plans/{plan_id}").json()["stages"][0]["status"] == "WAITING"
    assert client.get("/api/v1/firmware/hil-plans/unknown").status_code == 404
    assert client.post("/api/v1/firmware/hil-plans/unknown/cancel", json={**control, "password": password},
                       headers=headers).status_code == 404
    too_many = client.post("/api/v1/firmware/hil-plans/preview", json={
        "release_id": release_id(), "stages": [["ZONE-SLICTOWER-13FL", "ZONE-PESHAWAR-02", "ZONE-SWAT-01"]]})
    assert too_many.status_code == 422


def test_api_cancel_while_offered_is_a_conflict(plan_store, api_client):
    client, headers, password = api_client
    plan_id = approve(plan_store)
    tick(plan_store)
    assert offer(plan_store, "P02")
    response = client.post(f"/api/v1/firmware/hil-plans/{plan_id}/cancel",
                           json={"reason": "Owner cancels the plan", "password": password}, headers=headers)
    assert response.status_code == 409 and "OFFERED" in response.json()["detail"]
    assert plan_row(plan_store, plan_id).status == "RUNNING"


def test_openapi_exposes_the_plan_contract():
    from zk_add.web import app
    spec = app.openapi()
    for path in ("/api/v1/firmware/hil-plans", "/api/v1/firmware/hil-plans/preview",
                 "/api/v1/firmware/hil-plans/{plan_id}", "/api/v1/firmware/hil-plans/{plan_id}/{action}"):
        assert path in spec["paths"]
