"""Combined factory/matrix gates use actual stored rows and seals.

Synthetic23/22 identities are isolated to this fixture; the deployment
policy remains empty and BLOCKED. Stored-verdict linkage is tested here; the
collector tests separately establish how observation evidence earns a verdict.
"""
from copy import deepcopy
from datetime import timedelta
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from reader_matrix_fixtures import admit, proof, synthetic_matrix
from test_hil_runs import ready  # noqa: F401
from test_hil_scope import hil_session  # noqa: F401
from test_zkt_factory_contract import manifest
from test_zkt_factory_trial import factory as factory_case  # noqa: F401
from zk_add import zkt_factory_trial as trial, zkt_reader_matrix as matrix
from zk_add.hil_runs import _release_identity
from zk_add.hil_observation import COLLECTOR_VERSION, evidence_digest
from zk_add.models import Base, Connector
from zk_add.ota import FirmwareCampaign, FirmwareRelease, FirmwareDeployment, FirmwareEvent, FirmwareHilRun
from zk_add.time_utils import utc_now
from zk_add.zkt_bridge_contract import bridge_contract, signed_hil_targets
from zk_add.zkt_reader_evidence import reader_entry_for_manifest
from zk_add.zkt_reader_matrix import writer_matrix_contract


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "VERSIONS", ("2.6.23", "2.6.22"))
    value = synthetic_matrix()
    path = tmp_path / "synthetic-reader-policy.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(matrix, "MATRIX_PATH", path)
    return value


@pytest.fixture
def writer(ready, pinned):  # noqa: F811
    session, release, device, deployment, *_ = ready
    targets = signed_hil_targets()[:2]
    devices = list(session.scalars(select(Connector).order_by(Connector.id).limit(2)))
    for connector, target in zip(devices, targets):
        connector.connector_id, connector.hardware_id = target["connector_id"], target["mac"]
        for field in ("serial", "expected_serial", "confirmed_serial"):
            setattr(connector.zkt_device, field, target["terminal_serial"])
    contract = writer_matrix_contract()
    release.release_id, release.version = "zone-lite-2.7.0", "2.7.0"
    release.manifest = {**release.manifest, "release_id": release.release_id, "version": release.version,
        "firmware_family": "zkt", "project_name": "zone_lite", "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "minimum_bootstrap_version": matrix.minimum_reader_version(pinned), "runtime_profile": "ZKT_JOURNAL_V1",
        "queue_storage": contract, "hil_targets": signed_hil_targets(), "_hil_targets": targets}
    deployment.target_version = release.version
    selection = admit(session, release, deployment, pinned, version="2.6.23")
    reader_proof = proof(selection)
    reader_proof.pop("sampled_uptime_ms")
    now = utc_now()
    identity = _release_identity(release).model_dump(mode="json")
    run = FirmwareHilRun(run_id="full-run", deployment_id=deployment.id, connector_id=device.id,
        release_id=release.id, actor="test", idempotency_key="full-run", status="HIL_ACCEPTED",
        target=targets[0], release_identity=identity, baseline={"profile": "FULL_REMOTE_HIL_V1", "reader_admission": selection, "qualified_reader": reader_proof},
        started_at=now - timedelta(minutes=16), ends_at=now - timedelta(minutes=1), completed_at=now,
        result={"profile": "FULL_REMOTE_HIL_V1", "outcome": "PASS", "reasons": []})
    # This fixture tests stored-seal linkage only. The collector's independent
    # tests establish whether real stored observations may produce this result.
    sealed = {"reader_admission": selection, "qualified_reader": reader_proof,
        "samples": [{"qualified_reader": reader_proof}], "current_sample": {"qualified_reader": reader_proof},
        "collector_version": COLLECTOR_VERSION, "outcome": "PASS", "reasons": [],
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


@pytest.fixture
def prepared(writer):
    session, final, old_deployment, run, event, targets = writer
    original_campaign_id = old_deployment.campaign_id
    entries = [reader_entry_for_manifest(final.manifest, version)['reader'] for version in ('2.6.23', '2.6.22')]
    releases = []
    for entry in entries:
        release = FirmwareRelease(release_id=entry['release_id'], version=entry['version'],
            git_sha=entry['source_sha'], image_sha256=entry['artifact_sha256'], image_size=123,
            signing_key_id=entry['signing_key_id'], partition_layout='zone-lite-ota-v1',
            storage_name=entry['version'] + '/firmware.bin', manifest_signature='test-signature', state='HIL_ONLY',
            manifest={**(manifest() if entry['version'] == '2.6.22' else {
                'release_id': entry['release_id'], 'version': entry['version'], 'firmware_family': 'zkt',
                'project_name': 'zone_lite', 'release_channel': 'EXPERIMENTAL_HIL_ONLY',
                'minimum_bootstrap_version': '2.4.12', 'hil_targets': signed_hil_targets(),
                'queue_storage': bridge_contract(entry['version'])}),
                'application_sha256': entry['application_sha256']})
        session.add(release)
        releases.append(release)
    session.flush()
    bridge, factory = releases
    bridge_campaign = FirmwareCampaign(campaign_id='qualified-reader-history', release_id=bridge.id,
        zone_id='ZONE-HIL', status='COMPLETED', actor='test', idempotency_key='qualified-reader-history',
        reason='isolated linkage fixture', typed_confirmation='2.6.23')
    session.add(bridge_campaign)
    session.flush()
    old_deployment.release_id, old_deployment.campaign_id = bridge.id, bridge_campaign.id
    old_deployment.target_version, old_deployment.previous_version = '2.6.23', '2.6.15'
    new = FirmwareDeployment(deployment_id='final-writer', release_id=final.id, campaign_id=original_campaign_id,
        connector_id=old_deployment.connector_id, target_version='2.7.0', previous_version='2.6.23',
        status='SUCCEEDED', bytes_written=final.image_size)
    session.add(new)
    session.flush()
    for row in session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == old_deployment.id)):
        row.deployment_id = new.id
    run.deployment_id = new.id
    sealed = deepcopy(run.result['evidence'])
    sealed['scope']['deployment_id'] = new.id
    digest = evidence_digest(sealed)
    run.result = {**run.result, 'evidence': sealed, 'evidence_sha256': digest}
    event.details = {**event.details, 'evidence_sha256': digest}
    complete = run.started_at - timedelta(minutes=1)
    identity = _release_identity(bridge).model_dump(mode='json')
    bridge_run = FirmwareHilRun(run_id='qualified-reader-run', deployment_id=old_deployment.id,
        connector_id=old_deployment.connector_id, release_id=bridge.id, actor='test', idempotency_key='reader-ready',
        status='BRIDGE_READY', target=targets[0], release_identity=identity, baseline={'profile':'BRIDGE_READINESS_V1'},
        started_at=complete-timedelta(minutes=15), ends_at=complete, completed_at=complete,
        result={'outcome':'READY','reasons':[]})
    session.add(bridge_run)
    session.add(FirmwareEvent(deployment_id=old_deployment.id, state='BRIDGE_READY', details={
        **identity, 'target':targets[0], 'run_id':bridge_run.run_id, 'profile':'BRIDGE_READINESS_V1','outcome':'READY'}))
    session.flush()
    return session, factory, bridge, final, bridge_campaign, run, event


@pytest.fixture(params=["sqlite", "postgres"])
def combined(prepared, request):
    if request.param == "sqlite":
        yield prepared
        return
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set isolated PostgreSQL test URL for combined gate qualification")
    schema = "factory_matrix_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={
        "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        prepared[0].flush()
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in prepared[0].execute(select(table)).mappings()]
                if rows:
                    db.execute(table.insert(), rows)
                    if "id" in table.c:
                        db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :maximum)"),
                                   {"table": table.name, "maximum": max(row["id"] for row in rows)})
        with Session(engine) as session:
            yield session, *(session.get(type(row), row.id) for row in prepared[1:])
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_real_matrix_and_stored_full_verdict_authorize_only_exact_signed_factory_package(combined):
    session, factory, *_ = combined
    evidence = trial.dependencies(session, factory)
    assert len(evidence['proofs']) == 2
    assert evidence['proofs'][0]['release']['version'] == '2.6.23'
    assert evidence['proofs'][1]['release']['version'] == '2.7.0'
    assert evidence['matrix_sha256'] == reader_entry_for_manifest(combined[3].manifest, '2.6.23')['matrix_sha256']


@pytest.mark.parametrize('change', ['factory_hash','factory_source','reader_hash','reader_revoked',
    'reader_campaign_cancelled','writer_revoked','writer_incomplete','writer_unsealed','writer_label_only',
    'reader_proof_missing','reader_wrong_selected_version','new_storage_fault'])
def test_failed_missing_or_changed_real_dependency_never_admits_factory(combined, change):
    session, factory, bridge, final, bridge_campaign, run, event = combined
    if change == 'factory_hash':
        factory.image_sha256 = 'e'*64
    elif change == 'factory_source':
        factory.git_sha = 'f'*40
    elif change == 'reader_hash':
        bridge.image_sha256 = 'e'*64
    elif change == 'reader_revoked':
        bridge.state = 'REVOKED'
    elif change == 'reader_campaign_cancelled':
        bridge_campaign.status = 'CANCELLED'
    elif change == 'writer_revoked':
        final.state = 'REVOKED'
    elif change == 'writer_incomplete':
        run.status = 'HIL_INCOMPLETE'
    elif change == 'writer_unsealed':
        run.result = {**run.result, 'evidence_sha256':'0'*64}
    elif change == 'writer_label_only':
        session.delete(run)
    elif change in {'reader_proof_missing','reader_wrong_selected_version'}:
        sealed = deepcopy(run.result['evidence'])
        if change == 'reader_proof_missing':
            sealed['samples'][0].pop('qualified_reader')
        else:
            sealed['reader_admission']['reader']['version'] = '2.6.22'
        digest = evidence_digest(sealed)
        run.result = {**run.result, 'evidence':sealed, 'evidence_sha256':digest}
        event.details = {**event.details,'evidence_sha256':digest}
    elif change == 'new_storage_fault':
        from zk_add.models import Connector
        device = session.get(Connector, run.connector_id)
        device.firmware_diagnostics_at = utc_now()
        device.firmware_diagnostics = {'boot_id':device.boot_id, 'storage':{'error_code':'WRITE_FAILED'}}
    session.flush()
    with pytest.raises(ValueError):
        trial.dependencies(session, factory)


def test_factory_reader_needs_real_revocation_and_its_own_stored_readiness(factory_case, pinned):  # noqa: F811
    # Dependency linkage is exercised above without stubs. This lifecycle test
    # uses the component fixture's prerequisite to exercise real22 proof rows.
    from test_zkt_factory_trial import installed, proof as factory_proof
    from reader_matrix_fixtures import writer_manifest
    from zk_add.models import DeviceTelemetry
    from zk_add.ota import _storage_predecessor_exclusion

    session, bridge, devices, _ = factory_case
    entry = next(row for row in pinned["readers"] if row["version"] == "2.6.22")
    bridge.git_sha, bridge.image_sha256 = entry["source_sha"], entry["artifact_sha256"]
    bridge.signing_key_id = entry["signing_key_id"]
    bridge.manifest = {**bridge.manifest, "application_sha256": entry["application_sha256"]}
    deployment, reservation = installed(factory_case)
    device = devices[0]
    device.ota_image_sha256 = entry["application_sha256"]
    telemetry = session.scalar(select(DeviceTelemetry).where(DeviceTelemetry.connector_id == device.id))
    telemetry.payload = {**telemetry.payload, "ota": {
        **telemetry.payload["ota"], "image_sha256": entry["application_sha256"]}}
    value = {**factory_proof(factory_case, deployment, reservation, "FACTORY_VERIFIED"),
             "reader_application_sha256": entry["application_sha256"]}
    trial.accept_proof(session, device, deployment.deployment_id, value)
    deployment.status = "SUCCEEDED"
    now = utc_now()
    identity = _release_identity(bridge).model_dump(mode="json")
    target = {key: reservation["baseline"]["target"][key]
              for key in ("connector_id", "mac", "terminal_serial")}
    run = FirmwareHilRun(run_id="factory_case-reader-ready", deployment_id=deployment.id,
        connector_id=device.id, release_id=bridge.id, actor="test", idempotency_key="factory_case-ready",
        status="BRIDGE_READY", target=target, release_identity=identity,
        baseline={"profile": "BRIDGE_READINESS_V1"}, started_at=now - timedelta(minutes=16),
        ends_at=now - timedelta(minutes=1), completed_at=now, result={"outcome": "READY", "reasons": []})
    session.add_all([run, FirmwareEvent(deployment_id=deployment.id, state="BRIDGE_READY", details={
        **identity, "target": target, "run_id": run.run_id, "profile": "BRIDGE_READINESS_V1", "outcome": "READY"})])
    final = FirmwareRelease(version="2.7.0", state="HIL_ONLY", manifest=writer_manifest(pinned))
    session.flush()
    assert _storage_predecessor_exclusion(session, final, device) == "JOURNAL_BRIDGE_READY_NOT_VERIFIED"
    revoked = {**value, "proof_state": "FACTORY_FALLBACK_REVOKED", "checkpoint_sha256": "2" * 64}
    trial.accept_proof(session, device, deployment.deployment_id, revoked)
    receipt = trial.revoked_evidence(session, device, deployment, bridge)
    # A real revocation still cannot repair a readiness certificate that did
    # not bind it; only a newly bound stored observation may qualify the reader.
    assert _storage_predecessor_exclusion(session, final, device) == "JOURNAL_BRIDGE_READY_NOT_VERIFIED"
    run.baseline = {**run.baseline, "factory_fallback_revocation": receipt}
    device.zkt_custody_enabled = True
    device.firmware_diagnostics_at = now
    device.firmware_diagnostics = {"boot_id": device.boot_id, "sampled_at": now.isoformat(),
        "journal_runtime": {"observed": True, "reader_ready": True, "phase": "READY", "compatibility": ""},
        "storage": {"persistence_verified": True, "recovery_complete": True,
                    "upgrade_ready": True, "durability": "HEALTHY"}}
    session.flush()
    assert _storage_predecessor_exclusion(session, final, device) is None
    stored = session.get(FirmwareEvent, receipt["event_id"])
    stored.details = {**stored.details, "authorization": "0" * 64}
    session.flush()
    assert _storage_predecessor_exclusion(session, final, device) == "JOURNAL_BRIDGE_READY_NOT_VERIFIED"
