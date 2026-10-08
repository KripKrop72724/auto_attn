"""A label cannot advance the nationwide writer without its completed run."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_hil_scope import hil_session  # noqa: F401
from test_hil_runs import ready  # noqa: F401
from zk_add.hil_runs import _release_identity
from zk_add.hil_observation import COLLECTOR_VERSION, evidence_digest
from zk_add.models import Connector
from zk_add.ota import FirmwareDeployment, FirmwareEvent, FirmwareHilRun, _permitted_hil_targets
from zk_add.time_utils import utc_now
from zk_add.zkt_bridge_contract import signed_hil_targets
from zk_add.zkt_writer_contract import writer_contract


@pytest.fixture
def writer(ready):  # noqa: F811
    session, release, device, deployment, *_ = ready
    targets = signed_hil_targets()[:2]
    devices = list(session.scalars(select(Connector).order_by(Connector.id).limit(2)))
    for connector, target in zip(devices, targets):
        connector.connector_id, connector.hardware_id = target["connector_id"], target["mac"]
        for field in ("serial", "expected_serial", "confirmed_serial"):
            setattr(connector.zkt_device, field, target["terminal_serial"])
    contract = writer_contract()
    release.release_id, release.version = "zone-lite-2.7.0", "2.7.0"
    release.manifest = {**release.manifest, "release_id": release.release_id, "version": release.version,
        "firmware_family": "zkt", "project_name": "zone_lite", "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "minimum_bootstrap_version": contract["compatibility_version"], "runtime_profile": "ZKT_JOURNAL_V1",
        "queue_storage": contract, "hil_targets": signed_hil_targets(), "_hil_targets": targets}
    deployment.target_version = release.version
    now = utc_now()
    identity = _release_identity(release).model_dump(mode="json")
    run = FirmwareHilRun(run_id="full-run", deployment_id=deployment.id, connector_id=device.id,
        release_id=release.id, actor="test", idempotency_key="full-run", status="HIL_ACCEPTED",
        target=targets[0], release_identity=identity, baseline={"profile": "FULL_REMOTE_HIL_V1"},
        started_at=now - timedelta(minutes=16), ends_at=now - timedelta(minutes=1), completed_at=now,
        result={"profile": "FULL_REMOTE_HIL_V1", "outcome": "PASS", "reasons": []})
    # This fixture tests stored-seal linkage only. The collector's independent
    # tests establish whether real stored observations may produce this result.
    sealed = {"collector_version": COLLECTOR_VERSION, "outcome": "PASS", "reasons": [],
        "baseline_sha256": evidence_digest(run.baseline),
        "scope": {"run_id": run.run_id, "deployment_id": run.deployment_id,
                  "target": run.target, "release_identity": run.release_identity},
        "window_start": run.started_at.isoformat(), "window_end": run.ends_at.isoformat(),
        "evaluated_at": now.isoformat()}
    digest = evidence_digest(sealed)
    run.result = {**run.result, "collector_version": COLLECTOR_VERSION,
                  "evidence_sha256": digest, "evidence": sealed}
    event = FirmwareEvent(deployment_id=deployment.id, state="HIL_ACCEPTED", details={
        **identity, "target": targets[0], "run_id": run.run_id,
        "collector_version": COLLECTOR_VERSION, "evidence_sha256": digest,
        "profile": "FULL_REMOTE_HIL_V1", "outcome": "PASS"})
    session.add_all([run, event])
    session.flush()
    return session, release, deployment, run, event, targets


def next_target(data):
    return _permitted_hil_targets(data[0], data[1])[0].model_dump()


def test_writer_progresses_only_after_matching_completed_full_run(writer):
    assert next_target(writer) == writer[-1][1]


def test_orphan_acceptance_label_cannot_advance_writer(writer):
    writer[0].delete(writer[3])
    writer[0].flush()
    assert next_target(writer) == writer[-1][0]


@pytest.mark.parametrize("change", [
    "observing", "incomplete", "bridge", "result_profile", "result_outcome", "reasons", "no_reasons",
    "early_completion", "short_window", "missing_completion", "future_completion", "wrong_target",
    "wrong_identity", "event_run", "event_profile", "event_version", "event_key", "wrong_connector",
    "wrong_release", "wrong_deployment", "superseded",
    "missing_seal", "seal_hash", "event_hash", "baseline_changed", "collector_version",
])
def test_unproven_or_mismatched_full_run_cannot_advance_writer(writer, change):
    session, release, deployment, run, event, targets = writer
    if change in {"observing", "incomplete"}:
        run.status = "OBSERVING" if change == "observing" else "HIL_INCOMPLETE"
    elif change == "bridge":
        run.baseline = {"profile": "BRIDGE_READINESS_V1"}
    elif change.startswith("result_"):
        key = change.removeprefix("result_")
        run.result = {**run.result, key: "BRIDGE_READINESS_V1" if key == "profile" else "INCOMPLETE"}
    elif change == "reasons":
        run.result = {**run.result, "reasons": ["RECOVERY_TESTS_MISSING_OR_REPEATED"]}
    elif change == "no_reasons":
        run.result = {k: v for k, v in run.result.items() if k != "reasons"}
    elif change == "early_completion":
        run.completed_at = run.ends_at - timedelta(seconds=1)
    elif change == "short_window":
        run.ends_at = run.started_at + timedelta(seconds=899)
    elif change == "missing_completion":
        run.completed_at = None
    elif change == "future_completion":
        run.completed_at = utc_now() + timedelta(hours=1)
    elif change == "wrong_target":
        run.target = targets[1]
    elif change == "wrong_identity":
        run.release_identity = {**run.release_identity, "application_sha256": "f" * 64}
    elif change in {"event_run", "event_profile", "event_version", "event_key"}:
        key = {"event_run": "run_id", "event_profile": "profile", "event_version": "version",
               "event_key": "signing_key_id"}[change]
        event.details = {**event.details, key: "wrong"}
    elif change.startswith("wrong_"):
        field = change.removeprefix("wrong_") + "_id"
        setattr(run, field, getattr(run, field) + 100)
    elif change == "missing_seal":
        run.result = {key: value for key, value in run.result.items() if key != "evidence"}
    elif change == "seal_hash":
        run.result = {**run.result, "evidence_sha256": "0" * 64}
    elif change == "event_hash":
        event.details = {**event.details, "evidence_sha256": "0" * 64}
    elif change == "baseline_changed":
        run.baseline = {**run.baseline, "source_cursor": 999}
    elif change == "collector_version":
        run.result = {**run.result, "collector_version": "UNRECOGNIZED"}
    else:
        session.add(FirmwareDeployment(deployment_id="newer-failed", campaign_id=deployment.campaign_id,
            release_id=release.id, connector_id=deployment.connector_id, status="FAILED",
            target_version=release.version))
    session.flush()
    assert next_target(writer) == targets[0]
