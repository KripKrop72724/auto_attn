"""Clock-skew rescue must keep exact HIL scope, HMAC, and nonce replay gates."""

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zk_add.db import Base
from zk_add.hil_clock_recovery import trusted_hil_clock_matches
from zk_add.models import Connector, ConnectorCredential, DeviceTelemetry, ZKTDevice
from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease, OTA_LAYOUT
from zk_add.protocol import body_sha256, sign_request
from zk_add.security import authenticate_connector_body, connector_token_hash
from zk_add.settings import settings
from zk_add.storage_contract import DIRECT_BASELINE_IMAGES, DIRECT_BASELINES, DIRECT_VERSIONS
from zk_add.time_utils import utc_now


@pytest.fixture(params=DIRECT_VERSIONS)
def clock_hil(monkeypatch, request):
    version = request.param
    target = {
        "connector_id": "clock-hil-connector", "mac": "a4:cb:8f:d4:66:01",
        "terminal_serial": "clock-hil-terminal",
    }
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_hil_targets_json", json.dumps([target]))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        baseline_digest = DIRECT_BASELINE_IMAGES["2.5.2"]
        predecessor = FirmwareRelease(
            release_id="zone-lite-2.5.2", version="2.5.2", git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1024, signing_key_id="production-key",
            partition_layout=OTA_LAYOUT, storage_name="baseline/firmware.bin",
            manifest_signature="signed", state="AVAILABLE",
            manifest={"application_sha256": baseline_digest},
        )
        release = FirmwareRelease(
            release_id=f"zone-lite-{version}", version=version, git_sha="c" * 40,
            image_sha256="d" * 64, image_size=1024, signing_key_id="production-key",
            partition_layout=OTA_LAYOUT, minimum_bootstrap_version="2.4.12",
            storage_name="hil/firmware.bin", manifest_signature="signed", state="HIL_ONLY",
            manifest={
                "application_sha256": "e" * 64,
                "minimum_bootstrap_version": "2.4.12",
                "_hil_targets": [target],
                "queue_storage": {
                    "schema_version": 2, "read_format": 2, "reader_mask": 63,
                    "write_format": 1,
                    "allowed_bootstrap_versions": list(DIRECT_BASELINES),
                    "allowed_bootstrap_images": DIRECT_BASELINE_IMAGES,
                },
            },
        )
        connector = Connector(
            connector_id=target["connector_id"], hardware_id=target["mac"],
            zone_id="CLOCK-HIL", zone_name="Clock HIL", device_id="1",
            display_name="clock canary", firmware_version="zone-lite-2.5.2",
            connected=True, boot_id="clock-boot", active=True,
            ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
            ota_partition_layout=OTA_LAYOUT, ota_running_partition="ota_0",
            ota_image_sha256=baseline_digest,
        )
        connector.zkt_device = ZKTDevice(
            serial=target["terminal_serial"], expected_serial=target["terminal_serial"],
            confirmed_serial=target["terminal_serial"], terminal_binding_state="CONFIRMED",
        )
        session.add_all([predecessor, release, connector])
        session.flush()
        campaign = FirmwareCampaign(
            campaign_id="clock-hil-campaign", release_id=release.id,
            zone_id=connector.zone_id, status="ACTIVE", actor="test",
            idempotency_key="clock-hil", reason="clock canary",
            typed_confirmation=version,
        )
        session.add(campaign)
        session.flush()
        session.add(FirmwareDeployment(
            deployment_id="clock-hil-deployment", campaign_id=campaign.id,
            release_id=release.id, connector_id=connector.id, status="PENDING",
            previous_version="2.5.2", target_version=version,
        ))
        session.add(ConnectorCredential(
            connector_id=connector.id, token_hash=connector_token_hash("test-device-token"),
            token_last4="oken",
            active=True,
        ))
        now = utc_now()
        for seconds_ago in (45, 5):
            observed = now - timedelta(seconds=seconds_ago)
            session.add(DeviceTelemetry(
                connector_id=connector.id, boot_id=connector.boot_id,
                created_at=observed,
                payload={"_trusted_envelope_sent_at": (observed - timedelta(hours=2)).isoformat()},
            ))
        session.flush()
        yield session, connector, release, now
    engine.dispose()


def test_skewed_hil_request_keeps_signature_and_replay_protection(clock_hil):
    session, connector, _release, now = clock_hil
    request_timestamp = (now - timedelta(hours=2)).isoformat()
    assert trusted_hil_clock_matches(
        session, connector=connector, request_timestamp=now - timedelta(hours=2), now=now,
    )
    token = "test-device-token"
    path = "/device/v2/firmware/capability"
    body = b'{"capable":true}'
    digest = body_sha256(body)
    signature = sign_request(token=token, method="POST", path=path,
                             timestamp=request_timestamp, nonce="clock-nonce", body_hash=digest)
    arguments = (session, f"Bearer {token}", connector.connector_id,
                 request_timestamp, "clock-nonce", digest, signature, body, "POST", path)
    with pytest.raises(HTTPException) as denied:
        authenticate_connector_body(*arguments)
    assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as forged:
        authenticate_connector_body(
            session, f"Bearer {token}", connector.connector_id, request_timestamp,
            "forged-nonce", digest, "0" * 64, body, "POST", path,
            allow_hil_clock_recovery=True,
        )
    assert forged.value.status_code == 401
    assert authenticate_connector_body(*arguments, allow_hil_clock_recovery=True) is connector
    with pytest.raises(HTTPException) as replay:
        authenticate_connector_body(*arguments, allow_hil_clock_recovery=True)
    assert replay.value.status_code == 409


def test_clock_recovery_fails_closed_on_stale_or_changed_identity(clock_hil):
    session, connector, _release, now = clock_hil
    assert not trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2, minutes=10), now=now,
    )
    connector.zkt_device.confirmed_serial = "replacement"
    assert not trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2), now=now,
    )
    connector.zkt_device.confirmed_serial = connector.zkt_device.serial
    campaign = session.scalar(select(FirmwareCampaign))
    campaign.zone_id = "WRONG-ZONE"
    assert not trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2), now=now,
    )


def test_clock_recovery_requires_fresh_advancing_samples(clock_hil):
    session, connector, _release, now = clock_hil
    rows = list(session.query(DeviceTelemetry).order_by(DeviceTelemetry.id.desc()).limit(2))
    rows[0].created_at = now - timedelta(minutes=3)
    assert not trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2), now=now,
    )


def test_clock_recovery_carries_verified_new_image_through_boot_ack(clock_hil):
    session, connector, release, now = clock_hil
    deployment = session.scalar(select(FirmwareDeployment).where(
        FirmwareDeployment.connector_id == connector.id
    ))
    connector.firmware_version = release.version
    connector.ota_image_sha256 = release.manifest["application_sha256"]
    deployment.status = "READY_TO_BOOT"
    assert trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2), now=now,
    )
    deployment.status = "SUCCEEDED"
    assert not trusted_hil_clock_matches(
        session, connector=connector,
        request_timestamp=now - timedelta(hours=2), now=now,
    )
