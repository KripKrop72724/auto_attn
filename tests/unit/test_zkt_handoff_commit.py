"""Signed test scope, real custody and transactional handoff; no field grants."""

import base64
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest
from sqlalchemy import event, func, select

from reader_matrix_fixtures import admit, pinned, proof  # noqa: F401
import test_zkt_handoff as boundary
from test_zkt_source_load import store as store
from zk_add.crypto import decrypt_json
from zk_add.hil_scope import HilTarget
from zk_add.models import (
    AttendanceEvent,
    AuditEvent,
    DeviceTelemetry,
    ZktCustodyWork,
    ZktLegacyHandoff,
    ZktSourceCutover,
)
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease
from zk_add.schemas import HeartbeatPayload, LegacyInventoryDiagnostics
from zk_add.service import apply_firmware_diagnostics
from zk_add.settings import settings
from zk_add.time_utils import utc_now
from zk_add.zkt_handoff_commit import commit_handoff, wake_source_page
from zk_add.zkt_reader_matrix import writer_matrix_contract
from zk_add import zkt_bridge_contract, zkt_handoff_commit

source_store = boundary.source_store


def inventory():
    return dict(
        schema_version=1,
        fresh=True,
        verified_empty=True,
        idle=True,
        generation="18446744073709551614",
        required_mask=8191,
        empty_mask=8191,
    )


@pytest.fixture
def prepared(source_store, monkeypatch, pinned):  # noqa: F811
    monkeypatch.setattr(settings, "pii_lookup_key", "isolated-handoff-test-key")
    with source_store() as db:
        connector, now = boundary.prepared(db)
        connector.zkt_device.model = "G3"
        connector.zkt_device.expected_serial = connector.zkt_device.serial
        connector.zkt_device.terminal_binding_state = "CONFIRMED"
        target = SimpleNamespace(
            identity=HilTarget(
                connector_id=connector.connector_id,
                mac=connector.hardware_id,
                terminal_serial=connector.zkt_device.serial,
            ),
            model="G3",
        )
        # Test-only approved inventory, signed by an isolated ephemeral key.
        monkeypatch.setattr(zkt_bridge_contract, "TARGETS", (target,))
        monkeypatch.setattr(zkt_handoff_commit, "BY_ID", {connector.connector_id: target})
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        monkeypatch.setattr(
            settings, "firmware_signing_public_key_pem_b64", base64.b64encode(public).decode()
        )
        manifest = dict(
            version="2.7.0",
            release_id="zone-lite-2.7.0",
            firmware_family="zkt",
            project_name="zone_lite",
            release_channel="EXPERIMENTAL_HIL_ONLY",
            minimum_bootstrap_version="2.6.22",
            runtime_profile="ZKT_JOURNAL_V1",
            hil_targets=[target.identity.model_dump()],
            queue_storage=writer_matrix_contract(),
            application_sha256="b" * 64,
            image_sha256="c" * 64,
            git_sha="d" * 40,
        )
        signature = base64.b64encode(
            key.sign(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        ).decode()
        release = FirmwareRelease(
            release_id=manifest["release_id"],
            version="2.7.0",
            git_sha="d" * 40,
            image_sha256="c" * 64,
            image_size=1024,
            signing_key_id="synthetic",
            partition_layout="zone-lite-ota-v1",
            storage_name="synthetic",
            manifest={**manifest, "_hil_targets": manifest["hil_targets"]},
            manifest_signature=signature,
            state="HIL_ONLY",
        )
        db.add(release)
        db.flush()
        campaign = FirmwareCampaign(
            campaign_id="test-handoff",
            release_id=release.id,
            zone_id=connector.zone_id,
            actor="test",
            idempotency_key="test",
            reason="test",
            typed_confirmation="2.7.0",
            status="ACTIVE",
        )
        db.add(campaign)
        db.flush()
        db.add(
            FirmwareDeployment(
                deployment_id="test-install",
                campaign_id=campaign.id,
                release_id=release.id,
                connector_id=connector.id,
                status="RECONCILING",
                target_version="2.7.0",
            )
        )
        db.flush()
        deployment = db.scalar(select(FirmwareDeployment))
        selection = admit(db, release, deployment, pinned)
        diagnostics = deepcopy(connector.firmware_diagnostics)
        diagnostics["qualified_reader"] = proof(selection)
        diagnostics.update(
            legacy_inventory=inventory(),
            journal_runtime=dict(
                observed=True,
                phase="READY",
                reader_ready=True,
                writer_ready=True,
                delivery_authority="ADD",
                compatibility="",
                start_attempts=3,
                storage_starts=1,
                delivery_starts=1,
                capture_starts=1,
                proof_attempts=1,
                failures=0,
                sampled_uptime_ms=100000,
            ),
            storage=dict(
                durability="HEALTHY",
                persistence_verified=True,
                recovery_complete=True,
                upgrade_ready=True,
            ),
            journal_storage=dict(
                observed=True,
                fresh=True,
                ready=True,
                durability="HEALTHY",
                checkpoint_recovery_pending=False,
                sampled_uptime_ms=100000,
                mailbox_capacity=8,
                mailbox_high_watermark=2,
                pending_appends=0,
            ),
            queues=[
                dict(
                    name="legacy_migration",
                    count_known=True,
                    records=0,
                    count_reason="VERIFIED_EMPTY",
                )
            ],
            workers=[
                dict(name=name, state="RUNNING", operation="idle", last_activity_uptime_ms=100000)
                for name in (
                    "storage_owner",
                    "capture",
                    "add_delivery",
                    "legacy_add_delivery",
                    "legacy_ords_delivery",
                )
            ],
        )
        apply_firmware_diagnostics(
            db, connector, HeartbeatPayload(diagnostics=diagnostics), sampled_at=now
        )
        diagnostics = connector.firmware_diagnostics
        for seq, age in [(8, 30), (9, 0)]:
            sample = deepcopy(diagnostics)
            sample["sample_sequence"] = seq
            sample["sampled_at"] = (now - timedelta(seconds=age)).isoformat()
            db.add(
                DeviceTelemetry(
                    connector_id=connector.id,
                    boot_id=connector.boot_id,
                    sequence=seq,
                    uptime_seconds=100,
                    created_at=now - timedelta(seconds=age),
                    payload={
                        "diagnostics": sample,
                        "_trusted_envelope_sent_at": (now - timedelta(seconds=age)).isoformat(),
                        "ota": dict(
                            image_sha256="b" * 64,
                            secure_boot=True,
                            rollback_enabled=True,
                            running_version="2.7.0",
                        ),
                    },
                )
            )
        db.commit()
        return source_store, connector.connector_id, utc_now()


def issue(db, connector, now, **changes):
    return commit_handoff(
        db,
        connector,
        **dict(
            release_id="zone-lite-2.7.0",
            actor="test",
            idempotency_key="handoff-test-key",
            now=now,
            **changes,
        ),
    )


@pytest.mark.parametrize("fault", ["rolled_back", "cancelled_campaign", "changed_reader_sample"])
def test_cached_install_or_sample_cannot_grant_custody(prepared, fault):
    factory, connector, now = prepared
    with factory() as db:
        install = db.scalar(select(FirmwareDeployment))
        campaign = db.get(FirmwareCampaign, install.campaign_id)
        sample = db.scalar(select(DeviceTelemetry).order_by(DeviceTelemetry.id.desc()).limit(1))
        before = (install.status, campaign.status, deepcopy(sample.payload))
        with factory() as peer:
            if fault == "rolled_back":
                peer.get(FirmwareDeployment, install.id).status = "ROLLED_BACK"
            elif fault == "cancelled_campaign":
                peer.get(FirmwareCampaign, campaign.id).status = "CANCELLED"
            else:
                row = peer.get(DeviceTelemetry, sample.id)
                payload = deepcopy(row.payload)
                payload["diagnostics"]["qualified_reader"]["verified"] = False
                row.payload = payload
            peer.commit()
        assert (install.status, campaign.status, sample.payload) == before
        with pytest.raises(ValueError):
            issue(db, connector, now)
        assert db.scalar(select(func.count()).select_from(ZktLegacyHandoff)) == 0
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 0


def test_actual_signed_custody_handoff_replays_receipt_without_minting_attendance(prepared):
    factory, connector, now = prepared
    with factory() as db:
        result = issue(db, connector, now)
        db.commit()
        assert result["state"] == "COMMITTED" and result["oracle_delivery"] == "NOT_ASSERTED"
        row = db.scalar(select(ZktLegacyHandoff))
        evidence = decrypt_json(row.protected_evidence)
        assert evidence["legacy_inventory"]["generation"] == inventory()["generation"]
        assert (
            evidence["historical_completeness"]
            == evidence["profile_qualification"]
            == "NOT_ASSERTED"
        )
        assert evidence["physical_qualification"] == "NOT_PERFORMED"
        assert db.scalar(select(ZktSourceCutover.first_new_ordinal)) == 3
        assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == 0
        assert issue(db, connector, now + timedelta(days=1)) == result
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
        with pytest.raises(ValueError, match="ALREADY_COMMITTED"):
            commit_handoff(
                db,
                connector,
                release_id="zone-lite-2.7.0",
                actor="test",
                idempotency_key="changed",
                now=now,
            )


@pytest.mark.parametrize(
    "fault",
    [
        "missing-sample",
        "same-sequence",
        "old-sample",
        "fast-sample",
        "wrong-boot",
        "changed-generation",
        "pending-read",
        "not-empty",
        "missing-domain",
        "wrong-image",
        "signature",
        "revoked",
        "failed-install",
        "not-enabled",
        "worker-busy",
        "worker-missing",
        "unknown-count",
        "wrong-anchor",
        "storage-error",
        "writer-not-ready",
        "reader-not-ready",
        "wrong-authority",
        "runtime-recovering",
    ],
)
def test_no_permit_through_missing_or_changed_proof(prepared, fault):
    factory, connector_id, now = prepared
    with factory() as db:
        from zk_add.models import Connector

        connector = db.scalar(select(Connector))
        rows = db.scalars(select(DeviceTelemetry).order_by(DeviceTelemetry.id)).all()
        previous, current = rows
        payload = deepcopy(current.payload)
        diag = payload["diagnostics"]
        release = db.scalar(select(FirmwareRelease))
        if fault == "missing-sample":
            db.delete(previous)
        elif fault == "same-sequence":
            previous.sequence = current.sequence
        elif fault == "old-sample":
            current.created_at = now - timedelta(seconds=91)
        elif fault == "fast-sample":
            previous.created_at = now - timedelta(seconds=1)
        elif fault == "wrong-boot":
            previous.boot_id = "wrong"
        elif fault == "changed-generation":
            diag["legacy_inventory"]["generation"] = "9"
        elif fault == "pending-read":
            diag["legacy_inventory"]["idle"] = False
        elif fault == "not-empty":
            diag["legacy_inventory"]["verified_empty"] = False
        elif fault == "missing-domain":
            diag["legacy_inventory"]["empty_mask"] = 8190
        elif fault == "wrong-image":
            payload["ota"]["image_sha256"] = "e" * 64
        elif fault == "signature":
            release.manifest_signature = base64.b64encode(b"invalid").decode()
        elif fault == "revoked":
            release.state = "REVOKED"
        elif fault == "failed-install":
            db.scalar(select(FirmwareDeployment)).status = "ROLLED_BACK"
        elif fault == "not-enabled":
            connector.zkt_custody_enabled = False
        elif fault == "worker-busy":
            diag["workers"][-1]["operation"] = "waiting for acknowledgement"
        elif fault == "worker-missing":
            diag["workers"].pop()
        elif fault == "unknown-count":
            diag["queues"][0]["count_known"] = False
        elif fault == "wrong-anchor":
            diag["source_boundary"]["anchor_digest"] = "e" * 64
        elif fault == "storage-error":
            diag["storage"]["legacy_read_faults"] = 1
        elif fault == "writer-not-ready":
            diag["journal_runtime"]["writer_ready"] = False
        elif fault == "reader-not-ready":
            diag["journal_runtime"]["reader_ready"] = False
        elif fault == "wrong-authority":
            diag["journal_runtime"]["delivery_authority"] = "LEGACY"
        elif fault == "runtime-recovering":
            diag["journal_runtime"]["phase"] = "RECOVERING"
        current.payload = payload
        db.flush()
        with pytest.raises(ValueError):
            issue(db, connector_id, now)
        assert db.scalar(select(func.count()).select_from(ZktLegacyHandoff)) == 0
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 0
        assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == 0


def test_audit_failure_rolls_back_both_handoff_and_permit(prepared):
    factory, connector, now = prepared
    with factory() as db:

        def fail(mapper, connection, target):
            raise RuntimeError("synthetic audit disk failure")

        event.listen(AuditEvent, "before_insert", fail)
        try:
            with pytest.raises(RuntimeError, match="synthetic audit"):
                issue(db, connector, now)
                db.commit()
        finally:
            db.rollback()
            event.remove(AuditEvent, "before_insert", fail)
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 0
        assert db.scalar(select(func.count()).select_from(ZktLegacyHandoff)) == 0
        result = issue(db, connector, now)
        db.commit()
        assert result["state"] == "COMMITTED"


def test_handoff_wake_cursor_is_bounded_and_survives_restart(prepared):
    factory, connector, now = prepared
    with factory() as db:
        from zk_add.zkt_custody_work import advance_work_batch

        assert advance_work_batch(db, time_budget_ms=None).processed == 3
        assert all(row.next_attempt_at is None for row in db.scalars(select(ZktCustodyWork)))
        issue(db, connector, now)
        row = db.scalar(select(ZktLegacyHandoff))
        wake_source_page(db, row.connector_id, limit=1)
        assert 0 < row.wake_cursor < row.wake_through
        db.commit()
    with factory() as db:
        row = db.scalar(select(ZktLegacyHandoff))
        wake_source_page(db, row.connector_id, limit=1)
        assert (
            db.scalar(
                select(func.count())
                .select_from(ZktCustodyWork)
                .where(ZktCustodyWork.next_attempt_at.is_not(None))
            )
            == 2
        )
        db.commit()


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"generation": "18446744073709551616"},
        {"generation": "01"},
        {"empty_mask": True},
        {"idle": 1},
        {"verified_empty": True, "required_mask": 0},
    ],
)
def test_inventory_types_and_all_64_generation_bits_are_preserved(change):
    with pytest.raises(ValueError):
        LegacyInventoryDiagnostics.model_validate({**inventory(), **change})


def test_concurrent_handoffs_replay_one_committed_receipt(prepared):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    factory, connector, now = prepared
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Row lock scheduling requires PostgreSQL")
    barrier = Barrier(2)

    def attempt():
        with factory() as db:
            barrier.wait(timeout=5)
            result = issue(db, connector, now)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = [
            task.result(timeout=15) for task in (pool.submit(attempt), pool.submit(attempt))
        ]
    assert one == two
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ZktLegacyHandoff)) == 1
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_admin_api_requires_csrf_and_commits_before_success(prepared, monkeypatch, failure):
    from fastapi.testclient import TestClient
    from zk_add import web
    from zk_add.security import ADMIN_COOKIE, create_admin_session

    factory, connector, _now = prepared
    monkeypatch.setattr(web, "SessionLocal", factory)
    prior = web.app.dependency_overrides.copy()
    web.app.dependency_overrides.clear()
    client = TestClient(web.app, raise_server_exceptions=False)
    body = {"release_id": "zone-lite-2.7.0", "idempotency_key": "api-handoff-test"}
    paths = [f"/api/v1/devices/{connector}/zkt-custody/{verb}" for verb in ("enable", "handoff")]

    def fail(_mapper, _connection, target):
        if target.action == "ZKT_EXPERIMENTAL_HANDOFF_COMMITTED":
            raise RuntimeError("synthetic audit disk failure")

    try:
        for path in paths:
            assert client.post(path, json=body).status_code == 401
        with factory() as db:
            token, admin = create_admin_session(
                db, username="test", ip_address=None, user_agent="test"
            )
            db.commit()
            csrf = admin.csrf_token
        client.cookies.set(ADMIN_COOKIE, token)
        for path in paths:
            assert client.post(path, json=body).status_code == 403
        if failure:
            event.listen(AuditEvent, "before_insert", fail)
        response = client.post(paths[1], json=body, headers={"X-CSRF-Token": csrf})
        assert response.status_code == (500 if failure else 200)
        with factory() as independent:
            receipt = independent.scalar(select(ZktLegacyHandoff))
            assert (receipt is not None) is not failure
            assert (independent.scalar(select(ZktSourceCutover)) is not None) is not failure
            if not failure:
                assert response.json()["receipt_id"] == receipt.receipt_id
        if not failure:
            replay = client.post(paths[1], json=body, headers={"X-CSRF-Token": csrf})
            assert replay.json() == response.json()
            status = client.get(f"/api/v1/devices/{connector}/zkt-custody").json()
            assert status["legacy_handoff"] == response.json()
            assert status["oracle_completion"] == "NOT_ASSERTED"
    finally:
        if failure:
            event.remove(AuditEvent, "before_insert", fail)
        client.close()
        web.app.dependency_overrides.clear()
        web.app.dependency_overrides.update(prior)


def test_handoff_migration_is_additive_and_retains_receipt_after_downgrade(prepared, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from zk_add.db import Base

    factory, connector, now = prepared
    path = (
        Path(__file__).resolve().parents[2]
        / "apps/add_backend/migrations/versions/20261005_0052_zkt_legacy_handoff.py"
    )
    spec = importlib.util.spec_from_file_location("zkt_handoff_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        ZktLegacyHandoff.__table__.drop(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        context = MigrationContext.configure(
            connection,
            opts={
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    kind != "table" or name == ZktLegacyHandoff.__tablename__
                )
            },
        )
        assert compare_metadata(context, Base.metadata) == []
    with factory() as db:
        issued = issue(db, connector, now)
        db.commit()
        preserved = db.scalar(select(ZktLegacyHandoff.protected_evidence))
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
    with factory() as db:
        assert issue(db, connector, now) == issued
        assert db.scalar(select(ZktLegacyHandoff.protected_evidence)) == preserved


@pytest.mark.parametrize("change", ["encrypted-receipt", "digest", "authority"])
def test_changed_retained_proof_is_held_without_disclosing_raw_data(prepared, change):
    from zk_add.models import Connector
    from zk_add.zkt_handoff_commit import handoff_status

    factory, connector, now = prepared
    with factory() as db:
        issue(db, connector, now)
        row = db.scalar(select(ZktLegacyHandoff))
        if change == "encrypted-receipt":
            row.protected_evidence = "damaged"
        elif change == "digest":
            row.evidence_digest = "f" * 64
        else:
            db.scalar(select(ZktSourceCutover)).protected_authority = "damaged"
        db.flush()
        assert handoff_status(db, db.scalar(select(Connector))) == {
            "state": "HELD",
            "reason": "HANDOFF_RETAINED_EVIDENCE_CHANGED",
        }
        with pytest.raises(ValueError, match="HANDOFF_RETAINED_EVIDENCE_CHANGED"):
            issue(db, connector, now)


@pytest.mark.parametrize("fault", ["admission_missing", "admission_other", "missing", "image", "matrix", "slot", "generation"])
def test_handoff_requires_two_matching_selected_reader_proofs(prepared, fault):
    from zk_add.ota import FirmwareEvent
    factory, connector, now = prepared
    with factory() as db:
        if fault.startswith("admission"):
            event = db.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "OFFERED"))
            if fault == "admission_missing":
                db.delete(event)
            else:
                value = deepcopy(event.details)
                value["reader_admission"]["reader"]["version"] = "2.6.22"
                event.details = value
        else:
            sample = db.scalar(select(DeviceTelemetry).order_by(DeviceTelemetry.id.asc()))
            payload = deepcopy(sample.payload)
            if fault == "missing":
                payload["diagnostics"].pop("qualified_reader")
            else:
                field, value = {"image": ("application_sha256", "e" * 64),
                    "matrix": ("matrix_sha256", "e" * 64), "slot": ("slot_address", 0x520000),
                    "generation": ("proof_generation", "8")}[fault]
                payload["diagnostics"]["qualified_reader"][field] = value
            sample.payload = payload
        db.commit()
        with pytest.raises(ValueError):
            issue(db, connector, now)
        assert db.scalar(select(func.count()).select_from(ZktSourceCutover)) == 0
        assert db.scalar(select(func.count()).select_from(ZktLegacyHandoff)) == 0
