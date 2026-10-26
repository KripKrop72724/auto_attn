"""ZKT 2.7.7: ADD's side of the confirm-first storage exception (owner
decision D3 (a), 21 October 2026; docs/zone-lite-2.7.7-confirm-first.md).

The heartbeat's exception state, confirmation reason and failing boot-health
terms are accepted leniently (an unreadable value is dropped, never the
heartbeat); the progress receipt echoes the stored error_code and says whether
the release and campaign still allow the exception (pausing the campaign is
ADD's veto); the evidence a report names survives the next transition in the
event history; an exception deployment ends FAILED with its own reason and
pauses the campaign; and ADD never offers a device whose redo area holds
anything an image that cannot read that area."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import (
    STORAGE_EXCEPTION_CODE,
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareEvent,
    FirmwareRelease,
    _redo_floor_exclusion,
    progress_receipt,
    record_progress,
)
from zk_add.schemas import HeartbeatPayload, OtaHeartbeatPayload, StorageRepairDiagnostics

DIGEST = "c" * 64


def _setup(session: Session, *, release_state: str = "HIL_ONLY", campaign_status: str = "ACTIVE"):
    release = FirmwareRelease(
        release_id="zone-lite-2.7.7", version="2.7.7", git_sha="a" * 40, image_sha256="b" * 64,
        image_size=1024, signing_key_id="production-key", partition_layout="zone-lite-ota-v1",
        minimum_bootstrap_version="2.2.0", storage_name="2.7.7/firmware.bin",
        manifest={"application_sha256": DIGEST}, manifest_signature="test-signature", state=release_state)
    connector = Connector(
        connector_id="peshawar-02", hardware_id="e0:72:a1:d7:05:c4", zone_id="ZONE-PESHAWAR",
        zone_name="Peshawar", device_id="2", display_name="Peshawar-02", firmware_version="2.5.2",
        firmware_family="zkt")
    session.add_all([release, connector])
    session.flush()
    campaign = FirmwareCampaign(
        campaign_id="campaign-p02", release_id=release.id, zone_id=connector.zone_id, status=campaign_status,
        actor="StateHealthAdmin", idempotency_key="campaign-p02-request", reason="2.7.7 for P02 alone",
        typed_confirmation="2.7.7", eligible_count=1, legacy_skipped_count=0)
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id="deployment-p02", campaign_id=campaign.id, release_id=release.id,
        connector_id=connector.id, status="READY_TO_BOOT", bytes_written=1024, previous_version="2.5.2",
        target_version="2.7.7")
    session.add(deployment)
    session.flush()
    return release, connector, campaign, deployment


def _progress(session, connector, deployment, state, error_code=None, error_message=None):
    return record_progress(
        session, connector=connector, deployment_public_id=deployment.deployment_id, state=state,
        bytes_written=1024, running_version="2.7.7", running_partition="ota_1", image_sha256=DIGEST,
        error_code=error_code, error_message=error_message)


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


EVIDENCE = "DEFERRED full; 34 pages short; custody 67 blocks, 268 droppable pages; 51 spans held in /pending.jsonl"


@pytest.mark.parametrize("release_state", ["AVAILABLE", "HIL_ONLY"])
def test_the_receipt_echoes_the_code_and_allows_the_exception(session, release_state):
    release, connector, campaign, deployment = _setup(session, release_state=release_state)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    receipt = progress_receipt(session, deployment, requested_state="BOOTED_PENDING")
    assert receipt["confirm"] and receipt["error_code"] == STORAGE_EXCEPTION_CODE
    assert receipt["storage_exception_allowed"] is True
    assert receipt["application_sha256"] == DIGEST and receipt["deployment_id"] == "deployment-p02"
    event = session.query(FirmwareEvent).filter_by(state="BOOTED_PENDING").one()
    assert event.details["error_code"] == STORAGE_EXCEPTION_CODE and event.details["error_message"] == EVIDENCE


@pytest.mark.parametrize(("release_state", "campaign_status"),
                         [("HIL_ONLY", "PAUSED"), ("HIL_ONLY", "CANCELLED"), ("REVOKED", "ACTIVE")])
def test_a_paused_campaign_or_a_revoked_release_withholds_the_exception(session, release_state, campaign_status):
    release, connector, campaign, deployment = _setup(session, campaign_status=campaign_status)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    release.state = release_state
    receipt = progress_receipt(session, deployment, requested_state="BOOTED_PENDING")
    assert receipt["confirm"] and receipt["storage_exception_allowed"] is False


def test_an_ordinary_report_carries_no_exception_permission(session):
    release, connector, campaign, deployment = _setup(session)
    _progress(session, connector, deployment, "BOOTED_PENDING", "WAITING_FOR_RUNTIME_HEALTH",
              "storage exception REFUSED: LED_CAUSE for 40 s")
    receipt = progress_receipt(session, deployment, requested_state="BOOTED_PENDING")
    assert receipt["error_code"] == "WAITING_FOR_RUNTIME_HEALTH" and "storage_exception_allowed" not in receipt
    # The operator sees the gate's state on the deployment and in its history.
    assert deployment.error_message == "storage exception REFUSED: LED_CAUSE for 40 s"


@pytest.mark.parametrize("code", ["STORAGE_REPAIR_EXCEPTION_UNREPAIRED", "STORAGE_REPAIR_EXCEPTION_UNHEALTHY"])
def test_an_exception_deployment_ends_failed_and_pauses_the_campaign(session, code):
    release, connector, campaign, deployment = _setup(session)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "confirmed; one restart")
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "ROUND_SHORT TIME")
    _progress(session, connector, deployment, "FAILED", code, "UNREPAIRED after 3 round boot(s)")
    assert deployment.status == "FAILED" and deployment.error_code == code
    assert campaign.status == "PAUSED" and code in campaign.pause_reason
    # A repeated report after a lost receipt names the ended deployment: the
    # device clears its journal on it (ota_progress_receipt_terminal).
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "late")
    receipt = progress_receipt(session, deployment, requested_state="RECONCILING")
    assert receipt["state"] == "FAILED" and receipt["error_code"] == code
    history = [(event.state, event.details.get("error_code")) for event in
               session.query(FirmwareEvent).order_by(FirmwareEvent.id)]
    assert history == [("BOOTED_PENDING", STORAGE_EXCEPTION_CODE), ("RECONCILING", STORAGE_EXCEPTION_CODE),
                       ("RECONCILING", STORAGE_EXCEPTION_CODE), ("FAILED", code)]


def test_a_confirmed_exception_still_succeeds_the_ordinary_way(session):
    release, connector, campaign, deployment = _setup(session)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "confirmed")
    _progress(session, connector, deployment, "SUCCEEDED")
    assert deployment.status == "SUCCEEDED" and connector.ota_state == "OTA_READY"
    states = [event.state for event in session.query(FirmwareEvent).order_by(FirmwareEvent.id)]
    assert states == ["BOOTED_PENDING", "RECONCILING", "SUCCEEDED"]


def _release(version: str) -> FirmwareRelease:
    return FirmwareRelease(release_id=f"zone-lite-{version}", version=version, state="AVAILABLE")


@pytest.mark.parametrize(("diagnostics", "excluded"), [
    ({"storage": {"repair": {"reclaim": {"ota_floor": True}}}}, True),
    ({"ota_runtime": {"confirm_reason": "STORAGE_REPAIR_EXCEPTION"}}, True),
    ({"ota_runtime": {"storage_exception": {"state": "ROUND_SHORT", "reason": "TIME"}}}, True),
    ({"ota_runtime": {"storage_exception": {"state": "REFUSED", "reason": "LED_CAUSE"}}}, False),
    ({"storage": {"repair": {"reclaim": {"ota_floor": False}}}}, False),
    ({}, False),
    (None, False),
])
def test_add_never_offers_an_image_that_cannot_read_the_redo_area(diagnostics, excluded):
    connector = Connector(firmware_family="zkt", firmware_diagnostics=diagnostics)
    for version in ("2.5.2", "2.6.27", "2.4.12", "2.7.5"):
        assert _redo_floor_exclusion(_release(version), connector) == ("STORAGE_REDO_FLOOR" if excluded else None)
    for version in ("2.7.6", "2.7.7", "2.8.0"):
        assert _redo_floor_exclusion(_release(version), connector) is None
    hikvision = Connector(firmware_family="hikvision", firmware_diagnostics=diagnostics)
    assert _redo_floor_exclusion(_release("2.5.2"), hikvision) is None


def test_the_floor_latches_and_survives_a_rollback_heartbeat(session):
    """Design 14.1 item 3: the heartbeat-fresh check evaporates when the
    device rolls back to 2.5.2, whose heartbeats carry no evidence. The latch
    keeps the floor; no later heartbeat clears it."""
    from zk_add.service import apply_firmware_diagnostics

    release, connector, campaign, deployment = _setup(session)
    assert connector.ota_floor_latched_at is None
    apply_firmware_diagnostics(session, connector=connector, payload=HeartbeatPayload(
        firmware_version="2.7.7", uptime_seconds=120,
        diagnostics={"storage": {"repair": {"reclaim": {"area": "READY", "ota_floor": True}}}}))
    assert connector.ota_floor_latched_at is not None
    apply_firmware_diagnostics(session, connector=connector, payload=HeartbeatPayload(
        firmware_version="2.5.2", uptime_seconds=30))
    assert connector.ota_floor_latched_at is not None
    assert (connector.firmware_diagnostics or {}).get("storage") is None
    assert _redo_floor_exclusion(_release("2.6.27"), connector) == "STORAGE_REDO_FLOOR"
    assert _redo_floor_exclusion(_release("2.7.6"), connector) is None


def test_a_refused_attempt_alone_never_latches_the_floor(session):
    """Design 14.2 item 2: a refused boot writes nothing but the area
    self-test's sector, which the test erases; the device's own narrowed
    floor reports false and ADD must not ban its recovery images."""
    from zk_add.service import apply_firmware_diagnostics

    release, connector, campaign, deployment = _setup(session)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    assert connector.ota_floor_latched_at is None
    apply_firmware_diagnostics(session, connector=connector, payload=HeartbeatPayload(
        firmware_version="2.7.7", uptime_seconds=300,
        diagnostics={"storage": {"repair": {"reclaim": {"area": "READY", "ota_floor": False}}}},
        ota={"state": "BOOTED_PENDING", "storage_exception": {"state": "REFUSED", "reason": "RECEIPT"}}))
    assert connector.ota_floor_latched_at is None
    _progress(session, connector, deployment, "ROLLED_BACK", "BOOT_HEALTH_TIMEOUT", "budget spent")
    connector.firmware_diagnostics = None
    assert _redo_floor_exclusion(_release("2.6.27"), connector) is None


def test_a_confirmation_latches_the_floor_through_the_progress_report(session):
    release, connector, campaign, deployment = _setup(session)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    assert connector.ota_floor_latched_at is None
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "confirmed; one restart")
    assert connector.ota_floor_latched_at is not None
    connector.firmware_diagnostics = None
    assert _redo_floor_exclusion(_release("2.6.25"), connector) == "STORAGE_REDO_FLOOR"


def _second_target(session, release, suffix="13fl"):
    connector = Connector(
        connector_id=f"slictower-{suffix}", hardware_id=f"aa:bb:cc:dd:ee:{suffix[-2:]}", zone_id="ZONE-SLICTOWER",
        zone_name="SLICTOWER", device_id="13", display_name=f"SLICTOWER {suffix}", firmware_version="2.4.12",
        firmware_family="zkt")
    session.add(connector)
    session.flush()
    campaign = FirmwareCampaign(
        campaign_id=f"campaign-{suffix}", release_id=release.id, zone_id=connector.zone_id, status="ACTIVE",
        actor="StateHealthAdmin", idempotency_key=f"campaign-{suffix}-key", reason=f"2.7.7 for {suffix}",
        typed_confirmation="2.7.7", eligible_count=1, legacy_skipped_count=0)
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(
        deployment_id=f"deployment-{suffix}", campaign_id=campaign.id, release_id=release.id,
        connector_id=connector.id, status="READY_TO_BOOT", bytes_written=1024, previous_version="2.4.12",
        target_version="2.7.7")
    session.add(deployment)
    session.flush()
    return connector, campaign, deployment


def test_one_exception_target_at_a_time(session):
    """H4, design 14.2 item 3: a second device cannot confirm while
    another's exception deployment is active, or two devices parked in
    RECONCILING awaiting coverage would pin both nationwide slots."""
    release, connector, campaign, deployment = _setup(session)
    other_connector, other_campaign, other_deployment = _second_target(session, release)
    _progress(session, connector, deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    _progress(session, connector, deployment, "RECONCILING", STORAGE_EXCEPTION_CODE, "confirmed")
    _progress(session, other_connector, other_deployment, "BOOTED_PENDING", STORAGE_EXCEPTION_CODE, EVIDENCE)
    receipt = progress_receipt(session, other_deployment, requested_state="BOOTED_PENDING")
    assert receipt["confirm"] is True and receipt["storage_exception_allowed"] is False
    # P02's deployment ends; 13FL's next report may confirm.
    _progress(session, connector, deployment, "FAILED", "STORAGE_REPAIR_EXCEPTION_UNREPAIRED", "ended")
    receipt = progress_receipt(session, other_deployment, requested_state="BOOTED_PENDING")
    assert receipt["storage_exception_allowed"] is True


def test_the_heartbeat_takes_the_exception_leniently():
    ota = OtaHeartbeatPayload.model_validate({
        "state": "READY_TO_BOOT", "target_version": "2.7.7",
        "storage_exception": {"state": "REFUSED", "reason": "LED_CAUSE", "since_seconds": 45},
        "boot_health_blockers": ["QS_PERSISTENCE", "QS_PROBE_ERROR", "LED_LOCAL_FAILURE", "bad name", 7],
    })
    assert ota.storage_exception.state == "REFUSED" and ota.storage_exception.since_seconds == 45
    assert ota.boot_health_blockers == ["QS_PERSISTENCE", "QS_PROBE_ERROR", "LED_LOCAL_FAILURE"]
    assert ota.confirm_reason is None
    confirmed = OtaHeartbeatPayload.model_validate({
        "confirm_reason": "STORAGE_REPAIR_EXCEPTION",
        "storage_exception": {"state": "ROUND_DONE", "restarts": 1, "round_boots": 1, "crashes": 0},
    })
    assert confirmed.confirm_reason == STORAGE_EXCEPTION_CODE and confirmed.storage_exception.restarts == 1
    # Unreadable parts are dropped; the heartbeat stays.
    odd = HeartbeatPayload.model_validate({"firmware_version": "2.7.7", "ota": {
        "state": "RECONCILING", "storage_exception": {"state": "lower", "restarts": 999},
        "confirm_reason": "not a code", "boot_health_blockers": "QS_PERSISTENCE"}})
    assert odd.ota.storage_exception is None and odd.ota.confirm_reason is None
    assert odd.ota.boot_health_blockers == [] and odd.ota.state == "RECONCILING"
    # A later firmware's unknown codes are text, never a validation failure.
    later = OtaHeartbeatPayload.model_validate({"storage_exception": {"state": "SOME_NEW_STATE",
                                                                      "reason": "SOME_NEW_REASON"}})
    assert later.storage_exception.state == "SOME_NEW_STATE"


def test_the_repair_report_keeps_the_2_7_7_fields():
    report = StorageRepairDiagnostics.model_validate({
        "result": "DEFERRED", "mode": "FULL", "manifest_active": False, "journal_names": False,
        "entries": [{"name": "/pending.jsonl", "class": "attendance", "size": 2526978, "blocked_spans": 51,
                     "blocked_offset": 493968, "blocked_last": 506518, "blocked_digest": 3735928559,
                     "actions": []}],
        "reclaim": {"area": "READY", "trigger": "full", "short": "candidate", "custody_blocks": 67,
                    "want": 34, "target": 34, "custody_gain": 268, "unconfirmed_gain": 0, "held_out": 0,
                    "excluded_hidden": 1500, "excluded_unwindowed": 100, "excluded_last_page": 0,
                    "excluded_index_flag": 0, "excluded_busy": 0, "excluded_mixed": 217,
                    "excluded_file": "/pending.tmp", "excluded_file_blocks": 1700, "erased_at_mount": 0,
                    "free_blocks_at_mount": 0, "area_test": "PASSED", "ota_floor": True}})
    assert report.reclaim.custody_gain == 268 and report.reclaim.area_test == "PASSED"
    assert report.reclaim.excluded_file == "/pending.tmp" and report.reclaim.ota_floor is True
    assert report.entries[0].blocked_last == 506518 and report.manifest_active is False
    # An unreadable object name drops only itself.
    odd = StorageRepairDiagnostics.model_validate({"reclaim": {"area": "READY", "excluded_file": "x" * 40,
                                                               "custody_gain": 5}})
    assert odd.reclaim is not None and odd.reclaim.excluded_file is None and odd.reclaim.custody_gain == 5


def test_the_floor_latch_migration_is_the_single_head_after_mains_public_status():
    # 2.7.7's column follows main's #421 migration; two heads would stop the
    # API container's "alembic upgrade head".
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2] / "apps/add_backend"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20261026_0059_ota_floor_latch"]
    assert script.get_revision("20261026_0059_ota_floor_latch").down_revision == "20261024_0058_public_status"
