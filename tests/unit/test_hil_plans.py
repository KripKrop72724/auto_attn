"""One-approval staged HIL plans on SQLite and PostgreSQL.

Every campaign comes from the existing preflight and creation path, every offer
from ``assignment_for_connector`` and its fleet guard. The plan only decides
when to ask: it re-checks each device's holds before a stage, advances only
after every upgraded device proves itself on its new boot, and halts on
anything that is not SUCCEEDED.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import secrets
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from zk_add import hil_plans, ota
from zk_add.db import Base
from zk_add.device_health import evaluate_health
from zk_add.hil_plans import FirmwareHilPlan, PlanHalt
from zk_add.models import (
    AttendanceBatchReceipt,
    AuditEvent,
    Connector,
    DeviceAlert,
    DeviceTelemetry,
    ReconciliationJob,
    ZKTDevice,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease
from zk_add.settings import settings
from zk_add.storage_contract import FLEET_BASELINE_IMAGES, platform_baselines
from zk_add.time_utils import utc_now
from zk_add.zkt270_scope import TARGETS
from zk_add.zkt_bridge_contract import signed_hil_targets

VERSION = "2.7.6"
APPLICATION = "e" * 64
SOAK = hil_plans.DEFAULT_SOAK_MINUTES
# (nationwide index, zone). 13FL and 3FL share SLICTOWER; Swat is outside the plans.
DEVICES = {
    "13FL": (11, "ZONE-SLICTOWER-13FL"),
    "P02": (5, "ZONE-PESHAWAR-02"),
    "3FL": (0, "ZONE-SLICTOWER-3FL"),
    "SWAT": (1, "ZONE-SWAT-01"),
}
STAGES = [["ZONE-SLICTOWER-13FL", "ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]]
# Most tests run at any hour; the install window has tests of its own.
ANY_HOUR = {"window_start_hour": 0, "window_end_hour": 24}
PAKISTAN = timezone(timedelta(hours=5))


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


class Clock:
    """The plan's clock; the OTA code keeps the real one."""

    def __init__(self):
        self.offset = timedelta(0)

    def now(self):
        return utc_now() + self.offset

    def advance(self, **delta):
        self.offset += timedelta(**delta)
        return self.now()

    def set_pakistan_hour(self, hour: int):
        local = self.now().astimezone(PAKISTAN)
        target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        self.offset += target - local


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(hil_plans, "utc_now", value.now)
    return value


def release_id():
    return f"zone-lite-{VERSION}"


def preview(scope, stages=STAGES, **options):
    with scope() as session:
        result = hil_plans.resolve_plan(session, release_public_id=release_id(), stages=stages,
                                        **{**ANY_HOUR, **options})
        session.rollback()
    return result


def approve(scope, stages=STAGES, *, actor="StateHealthAdmin", key="plan-approval-1", **options):
    options = {**ANY_HOUR, **options}
    digest = preview(scope, stages, **options)["plan_digest"]
    with scope() as session:
        plan, created = hil_plans.create_plan(
            session, release_public_id=release_id(), stages=stages, reason="Owner approved staged HIL",
            typed_confirmation=VERSION, plan_digest=digest, actor=actor, idempotency_key=key, **options)
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


def campaign_for(session, name):
    return session.get(FirmwareCampaign, deployment_for(session, name).campaign_id)


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


def healthy_diagnostics(boot, **storage):
    return {"boot_id": boot, "runtime_profile": "ZKT_LEGACY",
            "storage": {"durability": "HEALTHY", "persistence_verified": True, "recovery_complete": True,
                        "upgrade_ready": True, **storage},
            "journal_runtime": {"observed": True, "phase": "DISABLED", "reader_ready": False}}


def new_boot(scope, name, at, *, boot=None, outbox=0, diagnostics=None, gap=None, soak=SOAK):
    """Heartbeats from the upgraded image: version, diagnostics and a soak of telemetry up to ``at``."""
    boot = boot or f"boot-{name}-{VERSION}"
    with scope() as session:
        connector = device(session, name)
        connector.firmware_version = f"zone-lite-{VERSION}"
        connector.ota_image_sha256 = APPLICATION
        connector.boot_id = boot
        connector.connected = True
        connector.last_seen_at = at
        connector.firmware_diagnostics = diagnostics or healthy_diagnostics(boot)
        connector.firmware_diagnostics_at = at
        connector.zkt_device.connection_state = "ONLINE"
        moment = at - timedelta(minutes=soak)
        while moment <= at:
            if gap is None or not gap[0] <= moment <= gap[1]:
                session.add(DeviceTelemetry(connector_id=connector.id, boot_id=boot, outbox_depth=outbox,
                                            created_at=moment))
            moment += timedelta(seconds=15)


def settle(scope, clock, *names):
    at = clock.advance(minutes=SOAK + 1)
    for name in names:
        new_boot(scope, name, at)


def campaigns(scope, plan_id):
    with scope() as session:
        return list(session.scalars(select(FirmwareCampaign).where(
            FirmwareCampaign.actor == f"hil-plan:{plan_id}").order_by(FirmwareCampaign.id)))


def audits(scope, action):
    with scope() as session:
        return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)))


def tick(scope):
    return hil_plans.tick(scope)


def halt_alerts(scope):
    with scope() as session:
        return list(session.scalars(select(DeviceAlert).where(DeviceAlert.code == "FIRMWARE_HIL_PLAN_HALTED")))


def open_alert(scope, name, code, *, severity="HIGH", **details):
    with scope() as session:
        session.add(DeviceAlert(connector_id=device(session, name).id, code=code, severity=severity,
                                state="OPEN", message=f"{code} on {name}", details=details))


def assert_halted(scope, plan_id, code_prefix, *, alerted=None):
    plan = plan_row(scope, plan_id)
    assert plan.status == "HALTED", (plan.status, plan.halt_code, plan.halt_reason)
    assert plan.halt_code.startswith(code_prefix), plan.halt_code
    assert plan.open_guard is None
    halt = audits(scope, "FIRMWARE_HIL_PLAN_HALTED")[-1]
    assert halt.actor == "system:hil-plan" and halt.target_id == plan_id
    assert halt.after["approved_by"] == "StateHealthAdmin" and halt.after["code"] == plan.halt_code
    alerts = halt_alerts(scope)
    if alerted is None:
        # A plan-level halt (timeouts of slot waits, release or pin changes) is
        # on the plan, its audit row and the browser event, never on a device.
        assert alerts == []
    else:
        assert len(alerts) == 1 and alerts[0].details["plan_id"] == plan_id
        assert alerts[0].severity == "WARNING"
        with scope() as session:
            assert alerts[0].connector_id == device(session, alerted).id
    # Halted means halted: nothing further is ever created.
    before = len(campaigns(scope, plan_id))
    tick(scope)
    tick(scope)
    assert len(campaigns(scope, plan_id)) == before
    return plan


# --------------------------------------------------------------------------
# Happy path


def test_two_stages_run_to_completion_through_the_existing_paths(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope)
    approved = plan_row(scope, plan_id)
    assert approved.status == "RUNNING" and approved.open_guard == "OPEN"
    assert approved.expires_at - approved.approved_at == timedelta(hours=72)
    assert approved.soak_seconds == SOAK * 60
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
    install(scope, "P02")
    tick(scope)
    # SUCCEEDED is not enough: the new boot has not proven itself yet.
    plan = plan_row(scope, plan_id)
    assert plan.current_stage == 0 and plan.waiting_reason.startswith("SETTLING")
    with scope() as session:
        assert hil_plans.plan_detail(session, plan_id)["stages"][0]["status"] == "SETTLING"
    settle(scope, clock, "13FL", "P02")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.current_stage == 1 and plan.stage_started_at is None
    # A finished stage's campaigns complete and free their zones.
    assert {row.status for row in campaigns(scope, plan_id)} == {"COMPLETED"}
    tick(scope)
    assert [row.zone_id for row in campaigns(scope, plan_id)][2:] == STAGES[1]
    install(scope, "3FL")
    settle(scope, clock, "3FL")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "COMPLETED" and plan.open_guard is None and plan.halt_code is None
    assert {row.status for row in campaigns(scope, plan_id)} == {"COMPLETED"}
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


def test_a_completed_plan_frees_its_zones_for_the_next_cycle(plan_store, clock):
    scope = plan_store
    first = approve(scope, [["ZONE-SWAT-01"]])
    with pytest.raises(ValueError, match="still running"):
        approve(scope, [["ZONE-PESHAWAR-02"]], key="plan-approval-2")
    tick(scope)
    install(scope, "SWAT")
    settle(scope, clock, "SWAT")
    tick(scope)
    assert plan_row(scope, first).status == "COMPLETED"
    assert [row.status for row in campaigns(scope, first)] == ["COMPLETED"]
    with scope() as session:  # as if the ESP needed the image again (reflashed by USB)
        connector = device(session, "SWAT")
        connector.firmware_version, connector.ota_image_sha256 = "zone-lite-2.6.15", FLEET_BASELINE_IMAGES["2.6.15"]
    # The same zone again, without a per-campaign password cancel first.
    second = approve(scope, [["ZONE-SWAT-01"], ["ZONE-PESHAWAR-02"]], key="plan-approval-2")
    tick(scope)
    assert [row.zone_id for row in campaigns(scope, second)] == ["ZONE-SWAT-01"]


def test_cancelling_a_succeeded_stage_campaign_does_not_halt_its_partner(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [STAGES[0]])
    tick(scope)
    install(scope, "13FL")
    assert offer(scope, "P02")
    with scope() as session:  # the owner's habit: cancel a campaign once it succeeded
        campaign = campaign_for(session, "13FL")
        campaign.status, campaign.pause_reason = "CANCELLED", "done updated"
    tick(scope)
    assert plan_row(scope, plan_id).status == "RUNNING"
    progress(scope, "P02", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING",
             "SUCCEEDED")
    settle(scope, clock, "13FL", "P02")
    tick(scope)
    assert plan_row(scope, plan_id).status == "COMPLETED"
    with scope() as session:
        assert campaign_for(session, "13FL").status == "CANCELLED"
        assert campaign_for(session, "P02").status == "COMPLETED"


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
            campaign = campaign_for(session, "13FL")
            campaign.status, campaign.pause_reason = "PAUSED", "Owner paused for review"
    tick(scope)
    expected = {"FAILED": "DEPLOYMENT_FAILED", "ROLLED_BACK": "DEPLOYMENT_ROLLED_BACK",
                "CANCELLED_DEPLOYMENT": "DEPLOYMENT_CANCELLED", "CAMPAIGN_PAUSED": "CAMPAIGN_PAUSED"}[kind]
    assert_halted(scope, plan_id, expected, alerted="13FL")
    with scope() as session:
        # The never-offered sibling is paused so it is not offered after the halt.
        sibling = campaign_for(session, "P02")
        assert sibling.status == "PAUSED" and plan_id in sibling.pause_reason
        assert deployment_for(session, "P02").status == "PENDING"
        assert deployment_for(session, "3FL") is None
    assert offer(scope, "P02") is None


def test_the_halt_alert_has_no_health_effect_and_an_operator_can_resolve_it(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "13FL")
    progress(scope, "13FL", "DOWNLOADING", "FAILED", error="DOWNLOAD_TIMEOUT")
    tick(scope)
    assert_halted(scope, plan_id, "DEPLOYMENT_FAILED", alerted="13FL")
    with scope() as session:
        health = evaluate_health(session, device(session, "13FL"))
        assert "FIRMWARE_HIL_PLAN_HALTED" not in {reason.code for reason in health.reasons}
        halted = next(row for row in health.other_active_alerts if row.code == "FIRMWARE_HIL_PLAN_HALTED")
        assert halted.gating is False and halted.operator["resolvable"] is True
    # A second plan halting on the same device keeps the first halt's record.
    with scope() as session:
        for name in ("13FL", "P02"):
            campaign = campaign_for(session, name)
            campaign.status = "CANCELLED"
    second = approve(scope, [["ZONE-SLICTOWER-13FL"]], key="plan-approval-2")
    tick(scope)
    assert offer(scope, "13FL")
    progress(scope, "13FL", "DOWNLOADING", "FAILED", error="DOWNLOAD_TIMEOUT")
    tick(scope)
    alerts = halt_alerts(scope)
    assert len(alerts) == 1 and alerts[0].details["plan_id"] == second
    assert [row["plan_id"] for row in alerts[0].details["previous_halts"]] == [plan_id]


def test_a_transient_guard_refusal_waits_and_a_lasting_one_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    # An operator's own campaign takes a slot after the stage started.
    manual_offer(scope, "SWAT")
    assert offer(scope, "13FL")
    assert offer(scope, "P02") is None  # third unsettled upgrade
    with scope() as session:
        assert deployment_for(session, "P02").error_code == "NATIONWIDE_TWO_UPGRADE_RESERVATIONS"
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "RUNNING"
    assert "offer waiting: NATIONWIDE_TWO_UPGRADE_RESERVATIONS" in plan.waiting_reason
    install_manual(scope, "SWAT")
    assert offer(scope, "P02")  # the slot settled: the same campaign is offered
    tick(scope)
    assert plan_row(scope, plan_id).status == "RUNNING"
    with scope() as session:
        # A refusal that does not clear by itself stops the plan.
        row = deployment_for(session, "P02")
        row.status, row.offered_at, row.error_code = "PENDING", None, "NATIONWIDE_ACTIVE_LOCATION_UNKNOWN"
    tick(scope)
    assert_halted(scope, plan_id, "ADMISSION_REFUSED:NATIONWIDE_ACTIVE_LOCATION_UNKNOWN", alerted="P02")
    with scope() as session:
        # 13FL already received bytes: its campaign is left to finish safely.
        assert campaign_for(session, "13FL").status == "ACTIVE"


def test_post_install_capability_regression_halts_before_the_next_stage(plan_store):
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


def test_a_defect_reported_after_succeeded_halts_even_when_the_telemetry_is_late(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]])
    tick(scope)
    install(scope, "P02")
    tick(scope)  # nothing from the new boot yet: still settling, never advanced
    assert plan_row(scope, plan_id).waiting_reason.startswith("SETTLING")
    clock.advance(minutes=SOAK + 5)
    tick(scope)
    assert plan_row(scope, plan_id).current_stage == 0
    # The first heartbeats arrive late and raise a restart loop.
    open_alert(scope, "P02", "ESP_RESTART_LOOP")
    tick(scope)
    assert_halted(scope, plan_id, "POST_INSTALL_HOLD:ESP_RESTART_LOOP", alerted="P02")
    with scope() as session:
        assert deployment_for(session, "3FL") is None


def test_an_accepted_fault_refreshed_by_the_new_boot_halts(plan_store, clock):
    scope = plan_store
    open_alert(scope, "P02", "ESP_DURABILITY_FAULT")  # accepted at approval: the release should repair it
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]])
    tick(scope)
    install(scope, "P02")
    settle(scope, clock, "P02")
    with scope() as session:
        alert = session.scalar(select(DeviceAlert).where(DeviceAlert.code == "ESP_DURABILITY_FAULT"))
        alert.last_seen_at = utc_now()  # the new boot reports the fault again
    tick(scope)
    assert_halted(scope, plan_id, "POST_INSTALL_HOLD:POST_VERDICT_PERSISTENCE_FAULT", alerted="P02")


def test_an_accepted_fault_from_the_old_boot_does_not_block_the_settle(plan_store, clock):
    scope = plan_store
    open_alert(scope, "P02", "ESP_DURABILITY_FAULT")
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"]])
    assert plan_row(scope, plan_id).stages[0][0]["accepted_holds"] == ["ESP_DURABILITY_FAULT"]
    tick(scope)
    install(scope, "P02")
    settle(scope, clock, "P02")
    tick(scope)
    assert plan_row(scope, plan_id).status == "COMPLETED"


def test_a_stage_partner_defect_halts_while_the_other_still_installs(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    install(scope, "13FL")
    assert offer(scope, "P02")
    progress(scope, "P02", "DOWNLOADING")
    at = clock.advance(minutes=1)
    new_boot(scope, "13FL", at, diagnostics=healthy_diagnostics(f"boot-13FL-{VERSION}", durability="FULL"))
    tick(scope)
    assert_halted(scope, plan_id, "POST_INSTALL_HOLD:STORAGE_FAULT", alerted="13FL")
    with scope() as session:
        # P02 already has bytes: it continues under its own campaign.
        assert deployment_for(session, "P02").status == "DOWNLOADING"
        assert campaign_for(session, "P02").status == "ACTIVE"


def test_the_settle_needs_new_boot_diagnostics_a_continuous_soak_and_delivery(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]])
    tick(scope)
    install(scope, "P02")

    def waiting():
        tick(scope)
        plan = plan_row(scope, plan_id)
        assert plan.current_stage == 0 and plan.status == "RUNNING", plan.halt_reason
        return plan.waiting_reason

    assert "do not report 2.7.6" in waiting()
    with scope() as session:
        connector = device(session, "P02")
        connector.firmware_version, connector.boot_id = f"zone-lite-{VERSION}", "boot-new"
        connector.ota_image_sha256 = APPLICATION
    assert "storage diagnostics from the new boot" in waiting()
    new_boot(scope, "P02", clock.now(), boot="boot-new", soak=1)
    assert "soak until" in waiting()
    at = clock.advance(minutes=SOAK + 1)
    # A two-minute silence inside the soak: it has to run again.
    new_boot(scope, "P02", at, boot="boot-new", gap=(at - timedelta(minutes=10), at - timedelta(minutes=8)))
    assert "heartbeat gap or reboot" in waiting()
    at = clock.advance(minutes=SOAK + 1)
    # The ESP rebooted during the soak.
    new_boot(scope, "P02", at, boot="boot-again")
    with scope() as session:
        session.add(DeviceTelemetry(connector_id=device(session, "P02").id, boot_id="boot-new", outbox_depth=0,
                                    created_at=at - timedelta(minutes=5)))
    assert "heartbeat gap or reboot" in waiting()
    at = clock.advance(minutes=SOAK + 1)
    # Punches wait on the ESP and nothing was delivered since the install.
    new_boot(scope, "P02", at, boot="boot-final", outbox=4)
    assert "attendance waiting on the ESP" in waiting()
    with scope() as session:
        connector = device(session, "P02")
        session.add(AttendanceBatchReceipt(
            connector_id=connector.id, zkt_device_id=connector.zkt_device.id, batch_id="after-install",
            payload_digest="d" * 64, reported_digest_key="sha256", outcome="COMMITTED", item_count=4,
            accepted_count=4))
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.current_stage == 1, plan.waiting_reason
    stage = audits(scope, "FIRMWARE_HIL_PLAN_STAGE_SUCCEEDED")
    assert len(stage) == 1 and stage[0].after["soak_minutes"] == SOAK


def test_preflight_refusal_at_stage_start_halts(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    install(scope, "13FL")
    install(scope, "P02")
    settle(scope, clock, "13FL", "P02")
    with scope() as session:
        device(session, "3FL").zkt_device.terminal_binding_state = "PENDING_CONFIRMATION"
    tick(scope)  # advance
    tick(scope)  # stage 2 preflight
    assert_halted(scope, plan_id, "PREFLIGHT_REFUSED", alerted="3FL")
    with scope() as session:
        assert deployment_for(session, "3FL") is None


def test_stability_warnings_from_the_retry_branch_preflight_halt(plan_store, monkeypatch):
    """Forward compatibility: fix/ota-interrupted-boot-retry adds stability_warnings to the
    preview and makes create_campaign refuse them without the owner's override. A plan
    never overrides; it stops. On main today the restart-loop hold below covers this."""
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


def test_a_database_error_at_stage_start_retries_instead_of_halting(plan_store, monkeypatch):
    scope = plan_store
    plan_id = approve(scope)
    original = ota.create_campaign
    failures = []

    def deadlock_once(session, **kwargs):
        if not failures:
            failures.append(kwargs["zone_id"])
            raise OperationalError("INSERT INTO add_firmware_campaigns", {}, Exception("deadlock detected"))
        return original(session, **kwargs)
    monkeypatch.setattr(ota, "create_campaign", deadlock_once)
    assert tick(scope) == [{"plan_pk": plan_row(scope, plan_id).id, "error": "STEP_FAILED"}]
    assert plan_row(scope, plan_id).status == "RUNNING" and campaigns(scope, plan_id) == []
    tick(scope)
    assert len(campaigns(scope, plan_id)) == 2


def test_release_revocation_halts_at_plan_level(plan_store):
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
    second = approve(scope, [["ZONE-SWAT-01"]], key="plan-approval-2")
    with scope() as session:  # the accepted holds and the soak are sealed too
        plan = session.scalar(select(FirmwareHilPlan).where(FirmwareHilPlan.plan_id == second))
        plan.soak_seconds = 600
    tick(scope)
    assert plan_row(scope, second).halt_code == "PLAN_PIN_CHANGED"


def test_stage_timeout_halts_on_the_device_that_stalled(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, stage_timeout_minutes=60)
    tick(scope)
    install(scope, "13FL")
    new_boot(scope, "13FL", clock.advance(minutes=SOAK + 1))
    assert offer(scope, "P02")
    clock.advance(minutes=31)
    tick(scope)
    assert_halted(scope, plan_id, "STAGE_TIMEOUT", alerted="P02")


def test_an_unsettled_installation_on_a_stage_device_halts(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"], ["ZONE-SLICTOWER-3FL"]])
    tick(scope)
    install(scope, "P02")
    settle(scope, clock, "P02")
    # An operator's 3FL campaign offered bytes and was then cancelled: the
    # ESP may still be installing, so the guard keeps its slot unsettled.
    manual_offer(scope, "3FL")
    with scope() as session:
        deployment = deployment_for(session, "3FL")
        deployment.status = "CANCELLED"
        session.get(FirmwareCampaign, deployment.campaign_id).status = "CANCELLED"
    tick(scope)  # stage one succeeded
    tick(scope)  # stage two is held, not started
    assert_halted(scope, plan_id, "DEVICE_INSTALLATION_UNSETTLED", alerted="3FL")
    assert [row.zone_id for row in campaigns(scope, plan_id)] == ["ZONE-PESHAWAR-02"]


# --------------------------------------------------------------------------
# Device holds: what the owner looked at before each manual campaign


def test_holds_that_need_a_person_refuse_approval(plan_store):
    scope = plan_store
    with scope() as session:
        device(session, "P02").zkt_device.writes_disabled_reason = "TERMINAL_SWAP_REVIEW"
    with pytest.raises(ValueError, match="ZONE-PESHAWAR-02: TERMINAL_IDENTITY_HOLD"):
        preview(scope)
    with scope() as session:
        device(session, "P02").zkt_device.writes_disabled_reason = None
    open_alert(scope, "13FL", "ESP_RESTART_LOOP")
    with pytest.raises(ValueError, match="ZONE-SLICTOWER-13FL: ESP_RESTART_LOOP"):
        preview(scope)
    with scope() as session:
        session.scalar(select(DeviceAlert).where(DeviceAlert.code == "ESP_RESTART_LOOP")).state = "RESOLVED"
        device(session, "3FL").last_error_code = "QUARANTINED_DUPLICATE_SERIAL"
    with pytest.raises(ValueError, match="ZONE-SLICTOWER-3FL: QUARANTINED_DUPLICATE_SERIAL"):
        preview(scope)
    with scope() as session:
        device(session, "3FL").last_error_code = None
        device(session, "3FL").ota_secure_boot = False
    with pytest.raises(ValueError, match="SECURITY_OR_CAPABILITY_UNVERIFIED"):
        preview(scope)


def test_existing_faults_are_shown_pinned_and_accepted(plan_store):
    scope = plan_store
    open_alert(scope, "13FL", "ESP_PRESERVATION_UNVERIFIED", severity="WARNING")
    with scope() as session:
        connector = device(session, "P02")
        connector.last_error_code = "ESP_LOCAL_FAILURE"
        job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
                                reason="Source review", idempotency_key="review", request_digest="a" * 64,
                                status="NEEDS_ATTENTION")
        session.add(job)
        session.flush()
        job_id = job.job_id
    result = preview(scope)
    holds = {row["zone_id"]: row["accepted_holds"] for row in result["stages"][0]}
    assert holds == {"ZONE-SLICTOWER-13FL": ["ESP_PRESERVATION_UNVERIFIED"],
                     "ZONE-PESHAWAR-02": ["ESP_LOCAL_FAILURE", f"SOURCE_OR_IDENTITY_REVIEW_REQUIRED:{job_id}"]}
    # The digest pins the holds: a cleared or changed hold needs a fresh preview.
    with scope() as session:
        device(session, "P02").last_error_code = None
    assert preview(scope)["plan_digest"] != result["plan_digest"]
    with scope() as session:
        device(session, "P02").last_error_code = "ESP_LOCAL_FAILURE"
    plan_id = approve(scope)
    assert plan_row(scope, plan_id).plan_digest == result["plan_digest"]
    tick(scope)
    assert len(campaigns(scope, plan_id)) == 2


def test_a_hold_that_appears_after_approval_halts_before_any_install(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    install(scope, "13FL")
    install(scope, "P02")
    settle(scope, clock, "13FL", "P02")
    with scope() as session:
        connector = device(session, "3FL")
        job = ReconciliationJob(connector_id=connector.id, zkt_device_id=connector.zkt_device.id, actor="test",
                                reason="Source review", idempotency_key="review", request_digest="a" * 64,
                                status="NEEDS_ATTENTION")
        session.add(job)
    tick(scope)  # stage one settled
    tick(scope)  # stage two: 3FL is held
    assert_halted(scope, plan_id, "DEVICE_HOLD:SOURCE_OR_IDENTITY_REVIEW_REQUIRED", alerted="3FL")
    with scope() as session:
        assert deployment_for(session, "3FL") is None


def test_a_restart_loop_after_approval_halts_the_stage_before_its_campaigns(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    open_alert(scope, "P02", "ESP_RESTART_LOOP")
    tick(scope)
    assert_halted(scope, plan_id, "DEVICE_HOLD:ESP_RESTART_LOOP", alerted="P02")
    assert campaigns(scope, plan_id) == []


# --------------------------------------------------------------------------
# Guard, slot contention and the install window


def manual_offer(scope, name):
    zone = DEVICES[name][1]
    with scope() as session:
        result = ota.preview_campaign_scope(session, release_public_id=release_id(), zone_id=zone)
        ota.create_campaign(session, release_public_id=release_id(), zone_id=zone, reason="Manual HIL",
                            typed_confirmation=VERSION, actor="StateHealthAdmin",
                            scope_token=result["scope_token"], idempotency_key=f"manual-{name}")
    assert offer(scope, name)


def install_manual(scope, name):
    progress(scope, name, "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING",
             "SUCCEEDED")


def test_a_permanent_slot_holder_is_shown_and_refuses_an_impossible_stage(plan_store):
    """Lahore-02 has held a slot (RECONCILING, campaign cancelled) since 2 October."""
    scope = plan_store
    manual_offer(scope, "3FL")
    progress(scope, "3FL", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING")
    with scope() as session:
        campaign_for(session, "3FL").status = "CANCELLED"
    with pytest.raises(ValueError, match=r"Stage 1 needs 2 upgrade slots, but ZONE-SLICTOWER-3FL "
                                         r"\(RECONCILING, campaign cancelled\) already hold 1 of 2"):
        preview(scope, [["ZONE-SWAT-01", "ZONE-PESHAWAR-02"]])
    with pytest.raises(ValueError, match="holds the upgrade slot of location SLICTOWER"):
        preview(scope, [["ZONE-SLICTOWER-13FL"]])
    result = preview(scope, [["ZONE-SWAT-01"], ["ZONE-PESHAWAR-02"]])
    assert [(row["zone_id"], row["status"]) for row in result["slot_holders"]] == [
        ("ZONE-SLICTOWER-3FL", "RECONCILING")]


def test_slots_taken_after_approval_make_the_stage_wait_then_start(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    manual_offer(scope, "SWAT")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "RUNNING" and plan.waiting_reason.startswith("FLEET_SLOTS_BUSY: ZONE-SWAT-01")
    assert campaigns(scope, plan_id) == []
    # RECONCILING still holds the slot.
    progress(scope, "SWAT", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING")
    tick(scope)
    assert campaigns(scope, plan_id) == []
    progress(scope, "SWAT", "SUCCEEDED")
    tick(scope)
    assert {row.zone_id for row in campaigns(scope, plan_id)} == set(STAGES[0])
    assert plan_row(scope, plan_id).waiting_reason is None


def test_a_busy_location_waits_and_the_wait_is_bounded(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-SLICTOWER-13FL"]], stage_timeout_minutes=30, soak_minutes=10)
    manual_offer(scope, "3FL")  # SLICTOWER becomes busy
    tick(scope)
    assert plan_row(scope, plan_id).waiting_reason.startswith("LOCATION_BUSY: SLICTOWER")
    assert campaigns(scope, plan_id) == []
    clock.advance(minutes=31)
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


def test_stages_start_only_inside_the_install_window(plan_store, clock):
    scope = plan_store
    clock.set_pakistan_hour(3)
    plan_id = approve(scope, window_start_hour=10, window_end_hour=16)
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.waiting_reason.startswith("INSTALL_WINDOW_CLOSED: stages start 10:00-16:00")
    assert campaigns(scope, plan_id) == []
    clock.advance(hours=7)  # 10:00 Pakistan time, seven hours after approval
    with scope() as session:
        device(session, "P02").connected = False
    tick(scope)
    # The start timeout counts open-window time only: no STAGE_START_TIMEOUT.
    plan = plan_row(scope, plan_id)
    assert plan.status == "RUNNING" and plan.waiting_reason == "DEVICE_OFFLINE: ZONE-PESHAWAR-02"
    with scope() as session:
        device(session, "P02").connected = True
    tick(scope)
    assert len(campaigns(scope, plan_id)) == 2


def test_plan_options_are_validated(plan_store):
    scope = plan_store
    for options, message in (
            ({"stage_timeout_minutes": 120, "soak_minutes": 120}, "soak must be shorter"),
            ({"expires_in_hours": 1, "stage_timeout_minutes": 60}, "shorter than the approval"),
            ({"window_start_hour": 16, "window_end_hour": 10}, "install window"),
            ({"soak_minutes": 5}, "between 10 and")):
        with pytest.raises(ValueError, match=message):
            preview(scope, **options)


def test_a_stage_must_fit_the_fleet_guard(plan_store):
    scope = plan_store
    with pytest.raises(ValueError, match="one upgrade per location"):
        preview(scope, [["ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-3FL"]])
    with pytest.raises(ValueError, match="one or two devices"):
        preview(scope, [["ZONE-SLICTOWER-13FL", "ZONE-PESHAWAR-02", "ZONE-SWAT-01"]])
    with pytest.raises(ValueError, match="only once"):
        preview(scope, [["ZONE-PESHAWAR-02"], ["ZONE-PESHAWAR-02"]])


def test_an_open_campaign_in_a_plan_zone_refuses_approval(plan_store):
    scope = plan_store
    manual_offer(scope, "P02")
    with pytest.raises(ValueError, match="ZONE-PESHAWAR-02 \\(active campaign"):
        preview(scope)


# --------------------------------------------------------------------------
# Expiry, restart and concurrency


def test_expiry_stops_a_stage_that_could_not_finish_in_time(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, expires_in_hours=2, stage_timeout_minutes=60)
    with scope() as session:
        device(session, "P02").connected = False
    tick(scope)
    assert campaigns(scope, plan_id) == []
    clock.advance(minutes=61)  # under an hour left: a stage could not finish its timeout
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "EXPIRED" and plan.halt_code == "PLAN_EXPIRED" and plan.open_guard is None
    assert "before stage 1 could finish" in plan.halt_reason
    assert campaigns(scope, plan_id) == [] and halt_alerts(scope) == []
    assert len(audits(scope, "FIRMWARE_HIL_PLAN_EXPIRED")) == 1


def test_a_started_stage_is_supervised_past_expiry_and_completes(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"]], expires_in_hours=2, stage_timeout_minutes=90)
    tick(scope)
    install(scope, "P02")
    clock.advance(minutes=85)
    new_boot(scope, "P02", clock.advance(hours=1))  # the approval ended meanwhile
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "COMPLETED", (plan.status, plan.halt_code)


def test_expiry_after_a_stage_succeeded_never_starts_the_next(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-PESHAWAR-02"], ["ZONE-SWAT-01"]], expires_in_hours=2,
                      stage_timeout_minutes=90)
    tick(scope)
    install(scope, "P02")
    new_boot(scope, "P02", clock.advance(minutes=SOAK + 60))
    tick(scope)
    assert plan_row(scope, plan_id).current_stage == 1
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "EXPIRED" and [row.zone_id for row in campaigns(scope, plan_id)] == ["ZONE-PESHAWAR-02"]


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
    digests = {zone: preview(scope, [[zone]])["plan_digest"] for zone in ("ZONE-SWAT-01", "ZONE-PESHAWAR-02")}
    start = Barrier(2)

    def worker(zone):
        session = scope.sessions()
        try:
            start.wait(timeout=10)
            plan, _ = hil_plans.create_plan(
                session, release_public_id=release_id(), stages=[[zone]], reason="Owner approved staged HIL",
                typed_confirmation=VERSION, plan_digest=digests[zone], actor="StateHealthAdmin",
                idempotency_key=f"race-{zone}", **ANY_HOUR)
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


def test_a_concurrent_retry_with_the_same_key_returns_the_same_plan(plan_store):
    scope = plan_store
    if scope.dialect != "postgres":
        pytest.skip("Advisory locks are a PostgreSQL property")
    digest = preview(scope, [["ZONE-SWAT-01"]])["plan_digest"]
    start = Barrier(2)

    def worker():
        session = scope.sessions()
        try:
            start.wait(timeout=10)
            plan, created = hil_plans.create_plan(
                session, release_public_id=release_id(), stages=[["ZONE-SWAT-01"]],
                reason="Owner approved staged HIL", typed_confirmation=VERSION, plan_digest=digest,
                actor="StateHealthAdmin", idempotency_key="double-click", **ANY_HOUR)
            session.commit()
            return plan.plan_id, created
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=30) for future in [pool.submit(worker) for _ in range(2)]]
    assert len({plan_id for plan_id, _ in results}) == 1
    assert sorted(created for _, created in results) == [False, True]


def test_approval_replay_is_idempotent_and_bound_to_its_digest(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    with scope() as session:
        digest = session.scalar(select(FirmwareHilPlan.plan_digest).where(FirmwareHilPlan.plan_id == plan_id))
        plan, created = hil_plans.create_plan(
            session, release_public_id=release_id(), stages=STAGES, reason="Owner approved staged HIL",
            typed_confirmation=VERSION, plan_digest=digest, actor="StateHealthAdmin",
            idempotency_key="plan-approval-1", **ANY_HOUR)
        assert plan.plan_id == plan_id and not created
        with pytest.raises(ValueError, match="another HIL plan"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=STAGES, reason="x" * 10,
                typed_confirmation=VERSION, plan_digest="0" * 64, actor="StateHealthAdmin",
                idempotency_key="plan-approval-1", **ANY_HOUR)


def test_a_changed_plan_needs_a_fresh_preview(plan_store):
    scope = plan_store
    digest = preview(scope)["plan_digest"]
    with scope() as session:
        with pytest.raises(ValueError, match="Preview it again"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=[STAGES[1], STAGES[0]],
                reason="Owner approved staged HIL", typed_confirmation=VERSION, plan_digest=digest,
                actor="StateHealthAdmin", idempotency_key="reordered", **ANY_HOUR)
        with pytest.raises(ValueError, match="Preview it again"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=STAGES, reason="Owner approved staged HIL",
                typed_confirmation=VERSION, plan_digest=digest, actor="StateHealthAdmin",
                idempotency_key="longer-soak", **{**ANY_HOUR, "soak_minutes": 60})
        with pytest.raises(ValueError, match="Typed firmware version"):
            hil_plans.create_plan(
                session, release_public_id=release_id(), stages=STAGES, reason="Owner approved staged HIL",
                typed_confirmation="2.7.5", plan_digest=digest, actor="StateHealthAdmin",
                idempotency_key="mistyped", **ANY_HOUR)


# --------------------------------------------------------------------------
# Scope


def test_only_exposed_hil_targets_of_a_hil_only_release_are_allowed(plan_store):
    scope = plan_store
    with scope() as session:
        release = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id()))
        # Expose only the first six of the signed seventeen: 13FL (index 11) is outside.
        release.manifest = {**release.manifest, "_hil_targets": signed_hil_targets()[:6]}
    with pytest.raises(ValueError, match="ZONE-SLICTOWER-13FL: no active device in this zone is an exact HIL"):
        preview(scope)
    assert preview(scope, [["ZONE-PESHAWAR-02"]])["stages"][0][0]["location"] == "PESHAWAR"
    with scope() as session:
        # A swapped ESP is not the signed target even in the right zone.
        device(session, "P02").hardware_id = "aa:bb:cc:dd:ee:ff"
    with pytest.raises(ValueError, match="ZONE-PESHAWAR-02: no active device"):
        preview(scope, [["ZONE-PESHAWAR-02"]])
    with scope() as session:
        session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == release_id())).state = "AVAILABLE"
    with pytest.raises(ValueError, match="HIL_ONLY release"):
        preview(scope, [["ZONE-SWAT-01"]])


@pytest.mark.parametrize("version", ["2.6.25", "2.6.15", "2.7.0"])
def test_recovery_bridge_and_legacy_releases_stay_manual(plan_store, version):
    scope = plan_store
    with scope() as session:
        row = session.scalar(select(FirmwareRelease).where(FirmwareRelease.release_id == f"zone-lite-{version}"))
        if row is None:
            row = FirmwareRelease(
                release_id=f"zone-lite-{version}", version=version, git_sha="d" * 40, image_sha256="d" * 64,
                image_size=1024, signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
                minimum_bootstrap_version="2.2.0", storage_name="hil/recovery.bin",
                manifest_signature="test-signature", state="HIL_ONLY", manifest={})
            session.add(row)
        row.state, row.revoked_at = "HIL_ONLY", None
        row.manifest = {"application_sha256": "d" * 64, "firmware_family": "zkt", "version": version,
                        "_hil_targets": signed_hil_targets()}
    with scope() as session:
        with pytest.raises(ValueError, match="2.7 platform releases"):
            hil_plans.resolve_plan(session, release_public_id=f"zone-lite-{version}", stages=[["ZONE-SWAT-01"]],
                                   **ANY_HOUR)


# --------------------------------------------------------------------------
# Owner controls


def test_cancel_never_waits_on_an_installation_and_never_touches_it(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "13FL")
    progress(scope, "13FL", "DOWNLOADING")
    with scope() as session:
        result = hil_plans.control_plan(session, plan_id=plan_id, action="cancel", actor="StateHealthAdmin",
                                        reason="Stop this plan now")
    assert result["status"] == "CANCELLED"
    with scope() as session:
        # Only the never-offered campaign is cancelled; the download continues.
        assert deployment_for(session, "P02").status == "CANCELLED"
        assert campaign_for(session, "P02").status == "CANCELLED"
        assert deployment_for(session, "13FL").status == "DOWNLOADING"
        assert campaign_for(session, "13FL").status == "ACTIVE"
    assert offer(scope, "P02") is None
    cancelled = audits(scope, "FIRMWARE_HIL_PLAN_CANCELLED")
    assert len(cancelled) == 1 and cancelled[0].actor == "StateHealthAdmin"
    assert halt_alerts(scope) == []
    tick(scope)
    assert plan_row(scope, plan_id).status == "CANCELLED"


def test_pause_holds_an_unoffered_stage_device_and_resume_releases_it(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope, [["ZONE-SWAT-01"], ["ZONE-PESHAWAR-02"]])
    tick(scope)
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="pause", actor="StateHealthAdmin",
                               reason="Owner wants to look first")
    assert offer(scope, "SWAT") is None  # paused with the plan before any offer
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "PAUSED" and "paused with the plan" in plan.waiting_reason
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="resume", actor="StateHealthAdmin",
                               reason="Owner reviewed the plan")
        assert campaign_for(session, "SWAT").status == "ACTIVE"
    install(scope, "SWAT")
    settle(scope, clock, "SWAT")
    tick(scope)
    tick(scope)
    assert [row.zone_id for row in campaigns(scope, plan_id)] == ["ZONE-SWAT-01", "ZONE-PESHAWAR-02"]


def test_pause_after_one_device_downloads_stops_the_other(plan_store, clock):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "13FL")
    progress(scope, "13FL", "DOWNLOADING")
    with scope() as session:
        result = hil_plans.control_plan(session, plan_id=plan_id, action="pause", actor="StateHealthAdmin",
                                        reason="Owner wants to look first")
        assert result["status"] == "PAUSED"
        assert campaign_for(session, "P02").status == "PAUSED"
        assert campaign_for(session, "13FL").status == "ACTIVE"  # bytes already sent
    assert offer(scope, "P02") is None
    progress(scope, "13FL", "VERIFYING", "READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING", "SUCCEEDED")
    settle(scope, clock, "13FL")
    tick(scope)
    plan = plan_row(scope, plan_id)
    assert plan.status == "PAUSED" and plan.current_stage == 0
    paused = audits(scope, "FIRMWARE_HIL_PLAN_PAUSE")
    assert len(paused) == 1 and len(paused[0].after["paused_campaigns"]) == 1


def test_a_failure_while_paused_still_halts(plan_store):
    scope = plan_store
    plan_id = approve(scope)
    tick(scope)
    assert offer(scope, "P02")
    with scope() as session:
        hil_plans.control_plan(session, plan_id=plan_id, action="pause", actor="StateHealthAdmin",
                               reason="Owner wants to look first")
    progress(scope, "P02", "DOWNLOADING", "FAILED", error="DOWNLOAD_TIMEOUT")
    tick(scope)
    assert_halted(scope, plan_id, "DEPLOYMENT_FAILED", alerted="P02")
    with scope() as session:
        assert campaign_for(session, "13FL").status == "PAUSED"
        assert "halted" in campaign_for(session, "13FL").pause_reason


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
    options = {"soak_minutes": 45, **ANY_HOUR}
    preview_response = client.post("/api/v1/firmware/hil-plans/preview",
                                   json={"release_id": release_id(), "stages": STAGES, **options})
    assert preview_response.status_code == 200, preview_response.text
    body = preview_response.json()
    assert [[row["zone_id"] for row in stage] for stage in body["stages"]] == STAGES
    assert body["soak_minutes"] == 45 and body["slot_holders"] == []
    assert all(row["accepted_holds"] == [] for stage in body["stages"] for row in stage)
    approval = {"release_id": release_id(), "stages": STAGES, "reason": "Owner approved staged HIL",
                "typed_confirmation": VERSION, "plan_digest": body["plan_digest"],
                "idempotency_key": "api-plan-approval", **options}
    assert client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}).status_code == 403
    refused = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": "wrong"}, headers=headers)
    assert refused.status_code == 403
    assert client.post("/api/v1/firmware/hil-plans", json=approval, headers=headers).status_code == 422
    # The digest covers the settings: the default soak is another plan.
    other = {key: value for key, value in approval.items() if key != "soak_minutes"}
    assert client.post("/api/v1/firmware/hil-plans", json={**other, "password": password},
                       headers=headers).status_code == 409
    with plan_store() as session:
        assert session.scalar(select(func.count()).select_from(FirmwareHilPlan)) == 0
    created = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}, headers=headers)
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    assert created.json()["created"] is True and created.json()["status"] == "RUNNING"
    assert created.json()["soak_minutes"] == 45 and created.json()["window_end_hour"] == 24
    replay = client.post("/api/v1/firmware/hil-plans", json={**approval, "password": password}, headers=headers)
    assert replay.status_code == 201 and replay.json()["plan_id"] == plan_id and replay.json()["created"] is False
    approved = audits(plan_store, "FIRMWARE_HIL_PLAN_APPROVED")
    assert len(approved) == 1 and approved[0].actor == "StateHealthAdmin"
    assert approved[0].after["plan_digest"] == body["plan_digest"] and approved[0].after["soak_minutes"] == 45
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
    late_window = client.post("/api/v1/firmware/hil-plans/preview", json={
        "release_id": release_id(), "stages": STAGES, "window_start_hour": 24})
    assert late_window.status_code == 422


def test_api_cancel_while_offered_leaves_the_offer_running(plan_store, api_client):
    client, headers, password = api_client
    plan_id = approve(plan_store)
    tick(plan_store)
    assert offer(plan_store, "P02")
    response = client.post(f"/api/v1/firmware/hil-plans/{plan_id}/cancel",
                           json={"reason": "Owner cancels the plan", "password": password}, headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    with plan_store() as session:
        assert deployment_for(session, "P02").status == "OFFERED"
        assert deployment_for(session, "13FL").status == "CANCELLED"


def test_openapi_exposes_the_plan_contract():
    from zk_add.web import app
    spec = app.openapi()
    for path in ("/api/v1/firmware/hil-plans", "/api/v1/firmware/hil-plans/preview",
                 "/api/v1/firmware/hil-plans/{plan_id}", "/api/v1/firmware/hil-plans/{plan_id}/{action}"):
        assert path in spec["paths"]


def test_approval_and_controls_never_block_the_event_loop(monkeypatch):
    """Both run their blocking work (step-up, preflights, locks) in the threadpool."""
    import asyncio
    from zk_add import web
    calls = []

    async def threadpool(function, *args):
        calls.append(function.__name__)
        return {"plan_id": "p", "status": "RUNNING"}

    async def publish(*_args):
        return None
    monkeypatch.setattr(web, "run_in_threadpool", threadpool)
    monkeypatch.setattr(web.browser_events, "publish", publish)
    asyncio.run(web.approve_firmware_hil_plan(body=None, auth=None))
    asyncio.run(web.control_firmware_hil_plan(plan_id="p", action="pause", body=None, auth=None))
    assert calls == ["_approve_hil_plan", "_control_hil_plan"]


def test_the_maintenance_loop_publishes_plan_steps_and_failures(monkeypatch):
    import asyncio
    from zk_add.realtime import browser_events
    published = []

    async def publish(topic, payload):
        published.append((topic, payload))
    monkeypatch.setattr(browser_events, "publish", publish)
    monkeypatch.setattr(hil_plans, "tick", lambda: [{"plan_id": "p", "status": "HALTED"},
                                                    {"plan_pk": 1, "error": "STEP_FAILED"}])

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(hil_plans.run_hil_plan_loop(stop, interval=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
    asyncio.run(run())
    assert ("firmware", {"hil_plan": {"plan_id": "p", "status": "HALTED"}}) in published
    assert any(topic == "backend_error" and payload["code"] == "HIL_PLAN_STEP_FAILED"
               for topic, payload in published)


def test_pakistan_window_boundaries():
    plan = FirmwareHilPlan(window_start_hour=10, window_end_hour=16)
    at = datetime(2026, 10, 22, 4, 59, tzinfo=timezone.utc)  # 09:59 Pakistan time
    assert hil_plans._window_closed(plan, at)
    assert hil_plans._window_closed(plan, at + timedelta(minutes=1)) is None  # 10:00
    assert hil_plans._window_closed(plan, at + timedelta(hours=6)) is None  # 15:59
    assert hil_plans._window_closed(plan, at + timedelta(hours=6, minutes=1))  # 16:00
