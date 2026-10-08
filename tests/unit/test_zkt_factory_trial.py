"""Factory trial state-machine tests, using actual committed ADD event rows.

The future final-writer dependency is supplied as a deterministic interface in
these component tests; the dependency's real seal/matrix integration is tested
separately when the V5 module is combined. No test asserts physical fallback.
"""
from copy import deepcopy
from datetime import timedelta
import sys
from types import ModuleType

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from test_hil_scope import hil_session  # noqa: F401
from test_zkt_factory_contract import manifest
from zk_add import zkt_factory_trial as trial
from zk_add.models import DeviceTelemetry
from zk_add.ota import (FirmwareCampaign, FirmwareDeployment, FirmwareEvent, preview_campaign_scope,
    create_campaign, assignment_for_connector, resolve_download, _storage_predecessor_exclusion)
from zk_add.time_utils import utc_now
from zk_add.zkt_factory_contract import FACTORY_TARGETS, FACTORY_LAYOUT_SHA256, factory_trial_targets
from zk_add.zkt_hil_schedule import schedule


@pytest.fixture
def factory(hil_session, monkeypatch):  # noqa: F811
    session, release, devices = hil_session
    release.release_id, release.version, release.minimum_bootstrap_version = "zone-lite-2.6.22", "2.6.22", "2.5.2"
    release.manifest = {**manifest(), "application_sha256": "c" * 64, "_hil_targets": factory_trial_targets()}
    for index, (device, pin) in enumerate(zip(devices, FACTORY_TARGETS)):
        device.connector_id, device.hardware_id = pin["connector_id"], pin["mac"]
        device.firmware_version, device.ota_running_partition = "zone-lite-2.5.2", "factory"
        device.ota_image_sha256 = pin["factory_application_sha256"]
        device.onboarding_generation = pin["onboarding_generation"]
        device.ota_state, device.boot_id = "OTA_READY", "factory-boot-" + str(index)
        device.is_spare = False
        device.zone_id = "ZONE-" + str(index)
        device.last_seen_at = utc_now()
        device.zkt_device.online, device.zkt_device.connection_state = True, "ONLINE"
        device.zkt_device.attendance_count = 100
        for key in ("serial", "expected_serial", "confirmed_serial"):
            setattr(device.zkt_device, key, pin["terminal_serial"])
    dependency = {"matrix_sha256": "d" * 64, "proofs": [{"run_id": "component-dependency-contract"}]}
    monkeypatch.setattr(trial, "dependencies", lambda *args: deepcopy(dependency))
    session.commit()
    return session, release, devices, dependency


def create(factory, key="factory-trial", preview=None):
    session, release, devices, _ = factory
    preview = preview or preview_campaign_scope(session, release_public_id=release.release_id, zone_id=devices[0].zone_id)
    result = create_campaign(session, release_public_id=release.release_id, zone_id=devices[0].zone_id,
        reason="exact experimental factory trial", typed_confirmation=release.version,
        actor="test", scope_token=preview["scope_token"], idempotency_key=key)
    session.commit()
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == result.id))
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == deployment.id,
        FirmwareEvent.state == trial.RESERVED))
    return deployment, trial._verified(event.details)


def installed(factory):
    session, release, devices, _ = factory
    deployment, reservation = create(factory)
    assignment = assignment_for_connector(session, connector=devices[0], public_base="https://add.example")
    assert assignment and assignment["deployment_id"] == deployment.deployment_id
    device = devices[0]
    deployment.status, deployment.bytes_written = "READY_TO_BOOT", release.image_size
    device.firmware_version, device.ota_image_sha256, device.ota_running_partition = "2.6.22", "c" * 64, "ota_0"
    device.boot_id = "new-22-boot"
    row = DeviceTelemetry(connector_id=device.id, boot_id=device.boot_id, sequence=3, uptime_seconds=100,
        created_at=utc_now(), payload={"ota": {"running_version": "2.6.22", "image_sha256": "c" * 64,
            "running_partition": "ota_0", "secure_boot": True, "rollback_enabled": True}})
    session.add(row)
    session.commit()
    return deployment, reservation


def proof(factory, deployment, reservation, state="FACTORY_FALLBACK_REVOKED"):
    return {"schema_version": 1, "trial_id": reservation["trial_id"], "challenge": reservation["challenge"],
        "deployment_id": deployment.deployment_id, "boot_id": factory[2][0].boot_id, "proof_state": state,
        "reader_application_sha256": "c" * 64, "onboarding_generation": 4,
        "factory_application_sha256": FACTORY_TARGETS[0]["factory_application_sha256"],
        "factory_signed_image_sha256": "e" * 64, "factory_signed_image_bytes": 1228800,
        "layout_sha256": FACTORY_LAYOUT_SHA256, "factory_address": 0x20000, "factory_size": 0x280000,
        "checkpoint_sha256": "1" * 64, "checkpoint_verified": True, "secure_boot_verified": True,
        "signature_verified": True, "encrypted_nvs": True, "rollback_enabled": True,
        "anti_rollback_disabled": True, "factory_fallback_verified": True, "legacy_only": True}


def test_schedule_never_fabricates_a_twenty_two_canary_or_erases_denominator(factory):
    decision = schedule(factory[0], factory[1])
    assert decision["denominator"] == 17 and decision["trial_target_count"] == 3
    assert decision["counts"]["NOT_APPLICABLE_TO_THIS_BRIDGE"] == 14
    assert decision["counts"]["PASSED"] == 0
    assert sum(decision["counts"].values()) == 17
    assert decision["selected"] == factory_trial_targets()[0]
    assert decision["rows"][0]["accepted"] is None


def test_real_campaign_reserves_unknown_legacy_diagnostics_without_claiming_preservation(factory):
    session, release, devices, _ = factory
    deployment, saved = create(factory)
    assert saved["baseline"]["unknown_legacy_diagnostics"] is True
    assert saved["baseline"]["queues"] is None and saved["baseline"]["source"] is None
    assert saved["baseline"]["pretrial_preservation"] == "NOT_ASSERTED"
    assert (trial._time(saved["expires_at"]) - trial._time(saved["reserved_at"])).total_seconds() == 3600
    assignment = assignment_for_connector(session, connector=devices[0], public_base="https://add.example")
    assert assignment and assignment["deployment_id"] == deployment.deployment_id
    token = assignment["download_url"].rsplit("/", 1)[-1]
    session.commit()
    assert resolve_download(session, token)[0].id == release.id


@pytest.mark.parametrize("change", ["generation", "boot", "digest"])
def test_preview_cas_rejects_rebinding_before_reservation(factory, change):
    session, release, devices, _ = factory
    preview = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=devices[0].zone_id)
    if change == "generation":
        devices[0].onboarding_generation += 1
    elif change == "boot":
        devices[0].boot_id = "new-factory-boot"
    else:
        devices[0].ota_image_sha256 = "e" * 64
    session.commit()
    with pytest.raises(ValueError):
        create(factory, preview=preview)
    assert not session.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state == trial.RESERVED))


@pytest.mark.parametrize("change", ["generation", "boot", "digest", "source_count", "revoked", "cancelled", "expired"])
def test_offered_trial_is_rechecked_before_assignment_and_download(factory, change):
    session, release, devices, _ = factory
    deployment, saved = create(factory)
    assignment = assignment_for_connector(session, connector=devices[0], public_base="https://add.example")
    token = assignment["download_url"].rsplit("/", 1)[-1]
    if change == "generation":
        devices[0].onboarding_generation += 1
    elif change == "boot":
        devices[0].boot_id = "unreviewed"
    elif change == "digest":
        devices[0].ota_image_sha256 = "e" * 64
    elif change == "source_count":
        devices[0].zkt_device.attendance_count = 99
    elif change == "revoked":
        release.state = "REVOKED"
    elif change == "cancelled":
        session.get(FirmwareCampaign, deployment.campaign_id).status = "CANCELLED"
    else:
        row = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == trial.RESERVED))
        saved["reserved_at"] = (utc_now() - timedelta(hours=2)).isoformat()
        saved["expires_at"] = (trial._time(saved["reserved_at"]) + timedelta(hours=1)).isoformat()
        row.details = trial._sealed(saved)
    session.commit()
    assert assignment_for_connector(session, connector=devices[0], public_base="https://add.example") is None
    with pytest.raises(ValueError):
        resolve_download(session, token)


def test_actual_new_boot_revoked_proof_is_stored_once_without_inventing_initial_report(factory):
    session, release, devices, _ = factory
    deployment, saved = installed(factory)
    context = trial.context(session, devices[0], deployment.deployment_id)
    assert context["expires_epoch"] == int(trial._time(context["expires_at"]).timestamp())
    value = proof(factory, deployment, saved)
    receipt = trial.accept_proof(session, devices[0], deployment.deployment_id, value)
    session.commit()
    assert trial.accept_proof(session, devices[0], deployment.deployment_id, value) == receipt
    session.commit()
    reports = list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS))))
    assert len(reports) == 1 and reports[0].state == "FACTORY_FALLBACK_REVOKED"
    body = trial._verified(reports[0].details)
    assert body["scope"] == "AUTHENTICATED_DEVICE_SOFTWARE_PROOF"
    assert body["physical_qualification"] == "NOT_PERFORMED"
    assert trial.revoked_evidence(session, devices[0], deployment, release, current_boot=True)["event_id"] == reports[0].id


def test_initial_proof_is_not_revocation_and_upgrade_keeps_both_observations(factory):
    session, release, devices, _ = factory
    deployment, saved = installed(factory)
    value = proof(factory, deployment, saved, "FACTORY_VERIFIED")
    trial.accept_proof(session, devices[0], deployment.deployment_id, value)
    session.commit()
    with pytest.raises(ValueError, match="REVOCATION_REQUIRED"):
        trial.revoked_evidence(session, devices[0], deployment, release)
    value = {**value, "proof_state": "FACTORY_FALLBACK_REVOKED", "checkpoint_sha256": "2" * 64}
    trial.accept_proof(session, devices[0], deployment.deployment_id, value)
    session.commit()
    assert len(list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS))))) == 2


@pytest.mark.parametrize("change", ["boot", "trial", "challenge", "generation", "reader", "factory", "bytes", "cancel", "revoked", "stale", "incomplete"])
def test_unverified_or_rebound_new_boot_cannot_create_receipt(factory, change):
    session, release, devices, _ = factory
    deployment, saved = installed(factory)
    value = proof(factory, deployment, saved)
    if change in {"boot", "trial", "challenge", "generation", "reader", "factory", "bytes"}:
        key, wrong = {"boot": ("boot_id", "unrelated"), "trial": ("trial_id", "00000000-0000-4000-8000-000000000000"),
            "challenge": ("challenge", "0" * 64), "generation": ("onboarding_generation", 5),
            "reader": ("reader_application_sha256", "a" * 64), "factory": ("factory_application_sha256", "a" * 64),
            "bytes": ("factory_signed_image_bytes", 0)}[change]
        value[key] = wrong
    elif change == "cancel":
        session.get(FirmwareCampaign, deployment.campaign_id).status = "CANCELLED"
    elif change == "revoked":
        release.state = "REVOKED"
    elif change == "stale":
        session.scalar(select(DeviceTelemetry)).created_at = utc_now() - timedelta(seconds=46)
    else:
        deployment.bytes_written = 0
    session.commit()
    with pytest.raises((ValueError, ValidationError)):
        trial.accept_proof(session, devices[0], deployment.deployment_id, value)
    assert not session.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS)))


@pytest.mark.parametrize("field,value", [("schema_version", True), ("checkpoint_verified", 1),
    ("onboarding_generation", True), ("signature_verified", False), ("factory_size", 0x280001),
    ("proof_state", "PASS"), ("raw_flash", "not allowed")])
def test_wire_contract_rejects_coerced_or_unbounded_claims(factory, field, value):
    deployment, saved = installed(factory)
    body = proof(factory, deployment, saved)
    body[field] = value
    with pytest.raises(ValidationError):
        trial.FactoryTrialProof.model_validate(body)


def test_known_storage_fault_never_becomes_an_unknown_experimental_obligation(factory):
    session, release, devices, _ = factory
    devices[0].firmware_diagnostics = {"storage": {"write_failures": 1}}
    assert _storage_predecessor_exclusion(session, release, devices[0]) == "FACTORY_REPORTED_PERSISTENCE_FAULT"


def test_trial_dependency_cannot_silently_fall_back_to_legacy_writer_contract(factory, monkeypatch):
    monkeypatch.undo()
    with pytest.raises(ValueError, match="MATRIX_UNAVAILABLE"):
        trial.dependencies(factory[0], factory[1])


def test_factory_dependency_requires_replacement23_and_never_substitutes_historical21(hil_session, monkeypatch):  # noqa: F811
    from zk_add.ota import FirmwareRelease
    session, factory_reader, _devices = hil_session
    factory_reader.version = "2.6.22"
    releases = []
    for index, version in enumerate(("2.7.0", "2.6.21")):
        row = FirmwareRelease(
            release_id="synthetic-" + version, version=version, git_sha="a" * 40,
            image_sha256=str(index + 4) * 64, image_size=1024, signing_key_id="production-key",
            partition_layout=factory_reader.partition_layout, minimum_bootstrap_version="2.2.0",
            storage_name="hil/unused-" + version + ".bin", manifest_signature="test-signature", state="HIL_ONLY",
            manifest={"application_sha256": "c" * 64},
        )
        releases.append(row)
        session.add(row)
    session.commit()
    requested = []

    def matrix_entry(_manifest, version):
        requested.append(version)
        identity = trial._release_identity(factory_reader if version == "2.6.22" else releases[1])
        return {"schema_version": 1, "matrix_sha256": "d" * 64,
            "reader": {"version": version, "release_id": identity["release_id"],
                "application_sha256": identity["application_sha256"],
                "artifact_sha256": identity["artifact_sha256"], "source_sha": identity["git_sha"],
                "signing_key_id": identity["signing_key_id"]}}

    interface = ModuleType("zk_add.zkt_reader_evidence")
    interface.reader_entry_for_manifest = matrix_entry
    interface.stored_reader_evidence_matches = lambda *_: pytest.fail("No final HIL proof may run without exact23")
    monkeypatch.setitem(sys.modules, "zk_add.zkt_reader_evidence", interface)
    with pytest.raises(ValueError, match="FACTORY_QUALIFIED23_ARTIFACT_REQUIRED"):
        trial.dependencies(session, factory_reader)
    assert requested == ["2.6.23", "2.6.22"]
    assert releases[1].version == "2.6.21" and releases[1].state == "HIL_ONLY"


def test_changed_revocation_checkpoint_or_proof_replay_is_rejected(factory):
    session, _release, devices, _ = factory
    deployment, saved = installed(factory)
    value = proof(factory, deployment, saved, "FACTORY_VERIFIED")
    trial.accept_proof(session, devices[0], deployment.deployment_id, value)
    session.commit()
    with pytest.raises(ValueError, match="CHECKPOINT_UNCHANGED"):
        trial.accept_proof(session, devices[0], deployment.deployment_id,
            {**value, "proof_state": "FACTORY_FALLBACK_REVOKED"})
    with pytest.raises(ValueError, match="REPLAY_CHANGED"):
        trial.accept_proof(session, devices[0], deployment.deployment_id,
            {**value, "factory_signed_image_sha256": "9" * 64})


def test_revocation_seal_cannot_be_rebound_to_different_release_or_boot(factory):
    session, release, devices, _ = factory
    deployment, saved = installed(factory)
    trial.accept_proof(session, devices[0], deployment.deployment_id, proof(factory, deployment, saved))
    session.commit()
    devices[0].boot_id = "later-legitimate-reader-boot"
    # Historical revocation is permanent; this does not assert the later boot's health.
    assert trial.revoked_evidence(session, devices[0], deployment, release)
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        trial.revoked_evidence(session, devices[0], deployment, release, current_boot=True)
    release.image_sha256 = "0" * 64
    with pytest.raises(ValueError):
        trial.revoked_evidence(session, devices[0], deployment, release)


@pytest.fixture
def pg_trial(factory):
    import os
    from uuid import uuid4
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from zk_add.models import Base
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
        os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None)
    if not url or not url.startswith("postgresql"):
        pytest.skip("Set isolated PostgreSQL test URL for concurrency qualification")
    deployment, saved = installed(factory)
    value = proof(factory, deployment, saved)
    schema = "factory_trial_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"})
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in factory[0].execute(select(table)).mappings()]
                if rows:
                    db.execute(table.insert(), rows)
                    if "id" in table.c:
                        db.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), :maximum)"),
                                   {"table": table.name, "maximum": max(row["id"] for row in rows)})
        yield sessionmaker(engine, expire_on_commit=False), factory[2][0].id, deployment.deployment_id, value
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_duplicate_proofs_serialize_to_one_durable_receipt(pg_trial):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zk_add.models import Connector
    sessions, connector_id, deployment_id, value = pg_trial
    waiting = Event()
    def second():
        with sessions() as db:
            waiting.set()
            result = trial.accept_proof(db, db.get(Connector, connector_id), deployment_id, value)
            db.commit()
            return result
    with sessions() as first, ThreadPoolExecutor(max_workers=1) as pool:
        receipt = trial.accept_proof(first, first.get(Connector, connector_id), deployment_id, value)
        future = pool.submit(second)
        assert waiting.wait(2)
        first.commit()
        assert future.result(timeout=10) == receipt
    with sessions() as db:
        assert len(list(db.scalars(select(FirmwareEvent).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS))))) == 1


@pytest.mark.parametrize("change", ["cancel", "revoke"])
def test_postgres_cancel_or_revocation_committed_before_proof_write_rejects(pg_trial, change):
    from sqlalchemy import event, update
    from zk_add.models import Connector
    from zk_add.ota import FirmwareRelease
    sessions, connector_id, deployment_id, value = pg_trial
    with sessions() as db:
        connector = db.get(Connector, connector_id)
        deployment = db.scalar(select(FirmwareDeployment).where(FirmwareDeployment.deployment_id == deployment_id))
        campaign_id, release_id = deployment.campaign_id, deployment.release_id
        connection = db.connection()
        changed = False
        def before_execute(conn, clause, multiparams, params, options):
            nonlocal changed
            if changed or not getattr(clause, "is_update", False) or clause.table.name != FirmwareDeployment.__tablename__:
                return
            changed = True
            with sessions.begin() as concurrent:
                if change == "cancel":
                    concurrent.execute(update(FirmwareCampaign).where(FirmwareCampaign.id == campaign_id).values(status="CANCELLED"))
                else:
                    concurrent.execute(update(FirmwareRelease).where(FirmwareRelease.id == release_id).values(state="REVOKED"))
        event.listen(connection, "before_execute", before_execute)
        try:
            with pytest.raises(ValueError, match="NO_LONGER_ACTIVE"):
                trial.accept_proof(db, connector, deployment_id, value)
            assert changed
            db.rollback()
        finally:
            event.remove(connection, "before_execute", before_execute)
    with sessions() as db:
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS)))


def test_expiry_before_first_offer_does_not_create_a_false_offered_state(factory):
    session, _release, devices, _ = factory
    deployment, saved = create(factory)
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.state == trial.RESERVED))
    saved["reserved_at"] = (utc_now() - timedelta(hours=2)).isoformat()
    saved["expires_at"] = (trial._time(saved["reserved_at"]) + timedelta(hours=1)).isoformat()
    event.details = trial._sealed(saved)
    session.commit()
    assert assignment_for_connector(session, connector=devices[0], public_base="https://add.example") is None
    assert deployment.status == "PENDING" and deployment.offered_at is None
    assert not session.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state == "OFFERED"))


def test_offline_wrong_factory_image_is_a_hold_not_a_connectivity_deferral(factory):
    session, release, devices, _ = factory
    devices[0].connected = False
    devices[0].last_seen_at = utc_now() - timedelta(minutes=5)
    assert schedule(session, release)["selected"] == factory_trial_targets()[1]
    devices[0].ota_image_sha256 = "e" * 64
    state = schedule(session, release)
    assert state["rows"][14]["status"] == "BLOCKED"
    assert state["selected"] == factory_trial_targets()[0]


def test_exact_layout_and_sector_aligned_signed_size_are_required(factory):
    session, _release, devices, _ = factory
    deployment, saved = installed(factory)
    value = proof(factory, deployment, saved)
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        trial.accept_proof(session, devices[0], deployment.deployment_id, {**value, "layout_sha256": "a" * 64})
    for size in (1, 4096, 8193, 0x280001):
        with pytest.raises(ValidationError):
            trial.FactoryTrialProof.model_validate({**value, "factory_signed_image_bytes": size})


def test_boot_confirmation_cannot_precede_durable_factory_revocation(factory):
    from zk_add.ota import record_progress
    session, release, devices, _ = factory
    deployment, saved = installed(factory)
    options = dict(connector=devices[0], deployment_public_id=deployment.deployment_id,
        state="BOOTED_PENDING", bytes_written=release.image_size, running_version="2.6.22",
        running_partition="ota_0", image_sha256="c" * 64)
    with pytest.raises(ValueError, match="REVOCATION_REQUIRED"):
        record_progress(session, **options)
    trial.accept_proof(session, devices[0], deployment.deployment_id, proof(factory, deployment, saved))
    session.commit()
    assert record_progress(session, **options).status == "BOOTED_PENDING"
