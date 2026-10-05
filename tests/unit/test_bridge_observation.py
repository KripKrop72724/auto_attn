from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_hil_runs import ready  # noqa: F401
from test_hil_scope import hil_session  # noqa: F401
from zk_add import bridge_observation, hil_runs
from zk_add.bridge_observation import PROFILE, complete_bridge_run
from zk_add.hil_scope import HilTarget
from zk_add.models import DeviceTelemetry
from zk_add.ota import FirmwareEvent, _permitted_hil_targets
from zk_add.time_utils import ensure_utc
from zk_add.zkt_bridge_contract import bridge_contract, signed_hil_targets


@pytest.fixture
def bridge(ready, monkeypatch):  # noqa: F811
    session, release, device, deployment, job, coverage, telemetry = ready
    target = signed_hil_targets()[0]
    device.connector_id = target["connector_id"]
    device.hardware_id = target["mac"]
    device.zkt_device.serial = device.zkt_device.expected_serial = device.zkt_device.confirmed_serial = target["terminal_serial"]
    device.zkt_device.online = True
    job.terminal_serial = coverage.terminal_serial = target["terminal_serial"]
    release.release_id, release.version = "zone-lite-2.6.17", "2.6.17"
    release.manifest = {"release_id": release.release_id, "version": release.version,
        "firmware_family": "zkt", "project_name": "zone_lite", "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "minimum_bootstrap_version": "2.4.12", "hil_targets": signed_hil_targets(),
        "queue_storage": bridge_contract("2.6.17"), "runtime_profile": "ZKT_LEGACY",
        "application_sha256": "c" * 64, "_hil_targets": signed_hil_targets()[:2]}
    deployment.target_version = release.version
    payload = deepcopy(telemetry.payload)
    payload["ota"]["running_version"] = release.version
    payload["zkt"].update(serial=target["terminal_serial"], online=True)
    diagnostics = payload["diagnostics"]
    diagnostics.update(schema_version=2, runtime_profile="ZKT_LEGACY", delivery_authority="LEGACY_DUAL",
        boot_id=device.boot_id, sampled_uptime_ms=100000,
        journal_runtime={"observed": True, "phase": "READY", "reader_ready": True,
                         "compatibility": "", "delivery_authority": "LEGACY"},
        journal_storage={"observed": True, "fresh": True, "ready": True, "durability": "HEALTHY",
                         "checkpoint_recovery_pending": False, "sampled_uptime_ms": 100000})
    diagnostics["storage"].update(read_failures=0, write_failures=0)
    diagnostics["workers"] = [{"name": name, "state": "RUNNING", "last_activity_uptime_ms": 100000,
                               "restart_count": 0} for name in
                              ("add_delivery", "ords_delivery", "storage_owner", "journal_add_delivery")]
    start = ensure_utc(telemetry.created_at)
    payload["_trusted_envelope_sent_at"] = start.isoformat()
    telemetry.payload, telemetry.sequence = payload, 10
    session.flush()
    monkeypatch.setattr(hil_runs, "utc_now", lambda: start)
    run = hil_runs.start_run(session, deployment_id=deployment.deployment_id, target=HilTarget(**target),
                            actor="admin", idempotency_key="bridge-observation", profile=PROFILE)
    rows = []
    for seconds in range(30, 901, 30):
        sample = deepcopy(payload)
        stamp = start + timedelta(seconds=seconds)
        sample["_trusted_envelope_sent_at"] = stamp.isoformat()
        tick = (100 + seconds) * 1000
        sample["diagnostics"]["sampled_uptime_ms"] = tick
        sample["diagnostics"]["journal_storage"]["sampled_uptime_ms"] = tick
        for worker in sample["diagnostics"]["workers"]:
            worker["last_activity_uptime_ms"] = tick
        row = DeviceTelemetry(connector_id=device.id, boot_id=device.boot_id,
            uptime_seconds=100 + seconds, sequence=10 + seconds, created_at=stamp, payload=sample)
        session.add(row)
        rows.append(row)
    session.flush()
    monkeypatch.setattr(bridge_observation, "utc_now", lambda: start + timedelta(seconds=901))
    return session, release, device, deployment, run, rows


def test_bridge_readiness_advances_bridge_only_and_preserves_unperformed_checks(bridge):
    session, release, _, _, run, _ = bridge
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[0]
    complete_bridge_run(session, run.run_id, actor="admin")
    assert run.status == "BRIDGE_READY" and run.result["outcome"] == "READY"
    assert run.result["reasons"] == [] and len(run.result["samples"]) == 31
    assert run.result["writer_hil"] == run.result["oracle_delivery"] == "NOT_ASSERTED"
    assert run.result["fault_injection"] == run.result["physical_qualification"] == "NOT_PERFORMED"
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[1]
    assert complete_bridge_run(session, run.run_id, actor="admin").id == run.id
    events = list(session.scalars(select(FirmwareEvent)))
    assert len(events) == 1 and events[0].state == "BRIDGE_READY"
    assert release.state == "HIL_ONLY"


def test_observation_profile_is_part_of_replay_identity(bridge):
    session, _, _, deployment, run, _ = bridge
    with pytest.raises(ValueError, match="different scope"):
        hil_runs.start_run(session, deployment_id=deployment.deployment_id,
            target=HilTarget(**run.target), actor="admin", idempotency_key="bridge-observation")


def test_bridge_cannot_complete_early_or_evaluate_full_hil(bridge, monkeypatch):
    session, _, _, _, run, _ = bridge
    monkeypatch.setattr(bridge_observation, "utc_now", lambda: ensure_utc(run.ends_at) - timedelta(seconds=1))
    with pytest.raises(ValueError, match="complete 15-minute"):
        complete_bridge_run(session, run.run_id, actor="admin")
    run.baseline = {**run.baseline, "profile": "FULL_REMOTE_HIL_V1"}
    with pytest.raises(ValueError, match="not found"):
        complete_bridge_run(session, run.run_id, actor="admin")
    assert not list(session.scalars(select(FirmwareEvent)))


@pytest.mark.parametrize("fault", ["gap", "missing-final", "stale", "wrong-boot", "wrong-image", "replay",
    "stopped-worker", "missing-owner", "unknown-queue", "reader", "storage", "write-error", "reset-counter",
    "source-epoch", "source-regression", "tail-pending", "offline-terminal", "stale-owner", "missing-clock"])
def test_missing_or_bad_server_evidence_never_advances_bridge(bridge, fault):
    session, release, _, _, run, rows = bridge
    row = rows[14]
    sample = deepcopy(row.payload)
    diagnostics = sample["diagnostics"]
    if fault == "gap":
        session.delete(row)
    if fault == "missing-final":
        for final in rows[-2:]:
            session.delete(final)
    if fault == "stale":
        sample["_trusted_envelope_sent_at"] = (ensure_utc(row.created_at) - timedelta(seconds=46)).isoformat()
    if fault == "wrong-boot":
        row.boot_id = "different-boot"
    if fault == "wrong-image":
        sample["ota"]["image_sha256"] = "d" * 64
    if fault == "replay":
        row.sequence = rows[13].sequence
    if fault == "stopped-worker":
        diagnostics["workers"][0]["state"] = "STOPPED"
    if fault == "missing-owner":
        diagnostics["workers"].pop(2)
    if fault == "unknown-queue":
        diagnostics["queues"][0]["count_known"] = False
    if fault == "reader":
        diagnostics["journal_runtime"]["reader_ready"] = False
    if fault == "storage":
        diagnostics["storage"]["persistence_verified"] = False
    if fault in {"write-error", "reset-counter"}:
        diagnostics["storage"]["write_failures"] = 1
    if fault == "source-epoch":
        diagnostics["source_generation"] = 2
    if fault == "source-regression":
        diagnostics["committed_source_cursor"] = 99
    if fault == "tail-pending":
        last = deepcopy(rows[-1].payload)
        last["zkt"]["attendance_count"] += 1
        rows[-1].payload = last
    if fault == "offline-terminal":
        sample["zkt"]["online"] = False
    if fault == "stale-owner":
        diagnostics["journal_storage"]["sampled_uptime_ms"] -= 46000
    if fault == "missing-clock":
        sample.pop("_trusted_envelope_sent_at")
    row.payload = sample
    session.flush()
    complete_bridge_run(session, run.run_id, actor="admin")
    assert run.status == "BRIDGE_INCOMPLETE" and run.result["reasons"]
    assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[0]


@pytest.mark.parametrize("fault", ["revoked", "paused", "disconnected", "replacement", "current-stale"])
def test_changed_current_state_cannot_use_old_healthy_observation(bridge, fault, monkeypatch):
    from zk_add.ota import FirmwareCampaign

    session, release, device, deployment, run, _ = bridge
    if fault == "revoked":
        release.state = "REVOKED"
    if fault == "paused":
        session.get(FirmwareCampaign, deployment.campaign_id).status = "PAUSED"
    if fault == "disconnected":
        device.connected = False
    if fault == "replacement":
        device.zkt_device.confirmed_serial = "replacement"
    if fault == "current-stale":
        monkeypatch.setattr(bridge_observation, "utc_now", lambda: ensure_utc(run.ends_at) + timedelta(seconds=46))
    complete_bridge_run(session, run.run_id, actor="admin")
    assert run.status == "BRIDGE_INCOMPLETE"


def test_unbacked_event_or_full_hil_label_cannot_advance_bridge(bridge):
    session, release, _, deployment, run, _ = bridge
    for state, outcome in (("BRIDGE_READY", "READY"), ("HIL_ACCEPTED", "PASS")):
        event = FirmwareEvent(deployment_id=deployment.id, state=state,
            details={**run.release_identity, "target": run.target, "profile": PROFILE,
                     "run_id": run.run_id, "outcome": outcome})
        session.add(event)
        session.flush()
        assert _permitted_hil_targets(session, release)[0].model_dump() == signed_hil_targets()[0]


def test_bridge_readiness_is_not_general_hil_acceptance(hil_session):  # noqa: F811
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment

    session, release, devices = hil_session
    campaign = FirmwareCampaign(campaign_id="bridge-only", release_id=release.id, zone_id=devices[0].zone_id,
        actor="admin", idempotency_key="bridge-only", reason="test", typed_confirmation=release.version)
    session.add(campaign)
    session.flush()
    deployment = FirmwareDeployment(deployment_id="bridge-only", campaign_id=campaign.id,
        release_id=release.id, connector_id=devices[0].id, target_version=release.version, status="SUCCEEDED")
    session.add(deployment)
    session.flush()
    session.add(FirmwareEvent(deployment_id=deployment.id, state="BRIDGE_READY", details={
        "target": release.manifest["_hil_targets"][0], "git_sha": release.git_sha,
        "artifact_sha256": release.image_sha256, "application_sha256": "c" * 64,
        "profile": PROFILE, "outcome": "READY"}))
    session.flush()
    assert _permitted_hil_targets(session, release)[0].connector_id == devices[0].connector_id
