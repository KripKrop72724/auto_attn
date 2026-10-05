"""Recovery refines a failed writer attempt only with its accepted bridge image."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareEvent,
    FirmwareRelease,
    record_progress,
)
from zk_add.time_utils import utc_now


@pytest.fixture(params=["sqlite", "postgres"])
def attempt(request):
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None
        )
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "zkt_rollback_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(
            url,
            connect_args={
                "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"
            },
        )
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")

    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

    request.addfinalizer(cleanup)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = utc_now()
        connector = Connector(
            connector_id="synthetic-recovery",
            hardware_id="00:11:22:33:44:55",
            zone_id="SYNTHETIC",
            zone_name="Synthetic",
            device_id="1",
            display_name="Synthetic",
            firmware_family="zkt",
            firmware_version="2.6.17",
        )
        releases = [
            FirmwareRelease(
                release_id=f"synthetic-{version}",
                version=version,
                git_sha=character * 40,
                image_sha256=character * 64,
                image_size=1024,
                signing_key_id="test-only",
                partition_layout="zone-lite-ota-v1",
                storage_name=f"synthetic-{version}.bin",
                state="HIL_ONLY",
                manifest={"application_sha256": digest * 64, "firmware_family": "zkt"},
                manifest_signature="synthetic-not-signed",
            )
            for version, character, digest in (("2.6.17", "a", "c"), ("2.7.0", "b", "d"))
        ]
        session.add_all([connector, *releases])
        session.flush()
        campaigns = [
            FirmwareCampaign(
                campaign_id=f"synthetic-{i}",
                release_id=release.id,
                zone_id=connector.zone_id,
                status="ACTIVE" if i else "COMPLETED",
                actor="test",
                idempotency_key=f"synthetic-{i}",
                reason="Synthetic failed boot test",
                typed_confirmation=release.version,
            )
            for i, release in enumerate(releases)
        ]
        session.add_all(campaigns)
        session.flush()
        bridge = FirmwareDeployment(
            deployment_id="synthetic-bridge",
            campaign_id=campaigns[0].id,
            release_id=releases[0].id,
            connector_id=connector.id,
            previous_version="2.6.15",
            target_version="2.6.17",
            status="SUCCEEDED",
            bytes_written=1024,
            created_at=now - timedelta(minutes=5),
        )
        writer = FirmwareDeployment(
            deployment_id="synthetic-writer",
            campaign_id=campaigns[1].id,
            release_id=releases[1].id,
            connector_id=connector.id,
            previous_version="2.6.17",
            target_version="2.7.0",
            status="READY_TO_BOOT",
            bytes_written=1024,
            created_at=now,
        )
        session.add_all([bridge, writer])
        session.flush()
        event = FirmwareEvent(
            deployment_id=bridge.id,
            state="SUCCEEDED",
            created_at=now - timedelta(minutes=1),
            details={
                "image_sha256": "c" * 64,
                "running_version": "2.6.17",
                "running_partition": "ota_0",
                "bytes_written": 1024,
            },
        )
        session.add(event)
        session.commit()
        yield session, connector, writer, bridge, event, releases, campaigns


def fail(attempt):
    session, connector, writer, *_ = attempt
    record_progress(
        session,
        connector=connector,
        deployment_public_id=writer.deployment_id,
        state="FAILED",
        bytes_written=1024,
        running_version="2.7.0",
        running_partition="ota_1",
        image_sha256="d" * 64,
        error_code="BOOT_HEALTH_TIMEOUT",
    )
    session.commit()


def recover(attempt, **changes):
    from zk_add import web

    session, connector, writer, *_ = attempt
    values = dict(
        state="ROLLED_BACK",
        bytes_written=1024,
        running_version="2.6.17",
        running_partition="ota_0",
        image_sha256="c" * 64,
        error_code="BOOT_HEALTH_TIMEOUT",
    )
    values.update(changes)
    return asyncio.run(
        web.firmware_progress(
            writer.deployment_id, web._FirmwareProgressIn(**values), (session, connector)
        )
    )


@pytest.mark.parametrize("failed_report_committed", [False, True])
def test_verified_return_is_committed_replayable_and_preserves_failure(
    attempt, failed_report_committed
):
    session, connector, writer, bridge, event, releases, campaigns = attempt
    if failed_report_committed:
        fail(attempt)
    receipt = recover(attempt)
    assert receipt["state"] == "ROLLED_BACK"
    assert receipt["rollback_application_sha256"] == "c" * 64
    assert receipt["application_sha256"] == "d" * 64
    assert campaigns[1].status == "PAUSED"
    assert writer.error_code == "BOOT_HEALTH_TIMEOUT"
    events = session.scalars(
        select(FirmwareEvent)
        .where(FirmwareEvent.deployment_id == writer.id)
        .order_by(FirmwareEvent.id)
    ).all()
    assert [item.state for item in events] == (["FAILED"] if failed_report_committed else []) + [
        "ROLLED_BACK"
    ]
    assert events[-1].details["recovery"]["bridge_boot_event_id"] == event.id
    assert events[-1].details["recovery"]["bridge_deployment_id"] == bridge.deployment_id
    assert recover(attempt) == receipt
    assert len(
        session.scalars(select(FirmwareEvent).where(FirmwareEvent.deployment_id == writer.id)).all()
    ) == len(events)
    # A replay cannot substitute the digest used by the receipt.
    assert recover(attempt, image_sha256="e" * 64)["rollback_application_sha256"] == "c" * 64


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "partition",
        "version",
        "bytes",
        "previous_version",
        "bridge_failed",
        "bridge_revoked",
        "bridge_event_digest",
        "bridge_event_time",
        "no_event",
        "wrong_family",
        "failed_digest",
        "failed_partition",
        "newer_deployment",
    ],
)
def test_unproven_return_cannot_acknowledge_or_change_failed_attempt(attempt, fault):
    from fastapi import HTTPException

    session, connector, writer, bridge, event, releases, campaigns = attempt
    fail(attempt)
    changes = {}
    if fault == "digest":
        changes["image_sha256"] = "e" * 64
    if fault == "partition":
        changes["running_partition"] = "ota_1"
    if fault == "version":
        changes["running_version"] = "2.6.15"
    if fault == "bytes":
        changes["bytes_written"] = 512
    if fault == "previous_version":
        writer.previous_version = "2.6.15"
    if fault == "bridge_failed":
        bridge.status = "FAILED"
    if fault == "bridge_revoked":
        releases[0].state = "REVOKED"
    if fault == "bridge_event_digest":
        event.details = {**event.details, "image_sha256": "f" * 64}
    if fault == "bridge_event_time":
        event.created_at = utc_now() + timedelta(minutes=10)
    if fault == "no_event":
        session.delete(event)
    if fault == "wrong_family":
        connector.firmware_family = "hikvision"
    if fault in {"failed_digest", "failed_partition"}:
        failure = session.scalar(
            select(FirmwareEvent).where(FirmwareEvent.deployment_id == writer.id)
        )
        key, value = (
            ("image_sha256", "f" * 64)
            if fault == "failed_digest"
            else ("running_partition", "ota_0")
        )
        failure.details = {**failure.details, key: value}
    if fault == "newer_deployment":
        session.add(
            FirmwareDeployment(
                deployment_id="later-attempt",
                campaign_id=campaigns[1].id,
                release_id=releases[1].id,
                connector_id=connector.id,
                target_version="2.7.0",
                status="PENDING",
            )
        )
    session.commit()
    with pytest.raises(HTTPException) as error:
        recover(attempt, **changes)
    assert error.value.status_code == 409
    assert writer.status == "FAILED" and campaigns[1].status == "PAUSED"
    assert not session.scalar(
        select(FirmwareEvent).where(
            FirmwareEvent.deployment_id == writer.id, FirmwareEvent.state == "ROLLED_BACK"
        )
    )


@pytest.mark.parametrize(
    "state,error",
    [
        ("FAILED", "OTHER_FAILURE"),
        ("CANCELLED", None),
        ("SUCCEEDED", None),
        ("RELEASE_REVOKED", None),
        ("SUPERSEDED", None),
    ],
)
def test_recovery_does_not_reopen_an_unrelated_terminal_outcome(attempt, state, error):
    session, _, writer, *_ = attempt
    writer.status, writer.error_code = state, error
    session.commit()
    receipt = recover(attempt)
    assert receipt["state"] == state and "rollback_application_sha256" not in receipt


def test_failed_commit_cannot_issue_recovery_receipt(attempt, monkeypatch):
    session, _, writer, *_ = attempt
    fail(attempt)

    def unavailable():
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(session, "commit", unavailable)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        recover(attempt)
    session.rollback()
    assert writer.status == "FAILED"
    assert not session.scalar(
        select(FirmwareEvent).where(
            FirmwareEvent.deployment_id == writer.id, FirmwareEvent.state == "ROLLED_BACK"
        )
    )


@pytest.mark.parametrize("state", ["READY_TO_BOOT", "BOOTED_PENDING", "RECONCILING"])
def test_return_without_failure_intent_does_not_infer_reset_cause(attempt, state):
    session, _, writer, *_ = attempt
    writer.status = state
    session.commit()
    receipt = recover(attempt, error_code="PREVIOUS_FIRMWARE_OBSERVED")
    assert receipt["state"] == "ROLLED_BACK"
    event = session.scalar(
        select(FirmwareEvent).where(
            FirmwareEvent.deployment_id == writer.id, FirmwareEvent.state == "ROLLED_BACK"
        )
    )
    assert event.details["recovery"]["reported_reason"] == "PREVIOUS_FIRMWARE_OBSERVED"
    assert event.details["recovery"]["failure_report_received"] is False


def test_simultaneous_recovery_reports_share_one_committed_event(attempt):
    session, connector, writer, *_ = attempt
    engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required for row-lock concurrency")
    fail(attempt)
    connector_id, writer_id = connector.id, writer.id
    session.rollback()
    barrier = threading.Barrier(2)

    def send():
        with Session(engine) as peer:
            device, deployment = (
                peer.get(Connector, connector_id),
                peer.get(FirmwareDeployment, writer_id),
            )
            assert deployment.status == "FAILED"
            barrier.wait(timeout=10)
            return recover((peer, device, deployment))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send) for _ in range(2)]
        receipts = [future.result(timeout=15) for future in futures]
    assert receipts[0] == receipts[1] and receipts[0]["state"] == "ROLLED_BACK"
    events = session.scalars(
        select(FirmwareEvent).where(
            FirmwareEvent.deployment_id == writer_id, FirmwareEvent.state == "ROLLED_BACK"
        )
    ).all()
    assert len(events) == 1
