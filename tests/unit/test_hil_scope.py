from types import SimpleNamespace

import pytest

from zk_add.hil_scope import parse_hil_targets, target_matches


def target(index=1):
    return {"connector_id": f"connector-{index}", "mac": f"a4:cb:8f:d4:66:0{index}",
            "terminal_serial": f"terminal-{index}"}


def connector():
    return SimpleNamespace(
        active=True, is_spare=False, connector_id="connector-1",
        hardware_id="a4:cb:8f:d4:66:01", display_name="same name",
        zkt_device=SimpleNamespace(serial="terminal-1", expected_serial="terminal-1",
                                  confirmed_serial="terminal-1"),
    )


def test_order_is_preserved_and_duplicate_identity_rejected():
    assert [row.connector_id for row in parse_hil_targets([target(2), target(1)])] == [
        "connector-2", "connector-1"]
    for field in target():
        duplicate = target(2)
        duplicate[field] = target()[field]
        with pytest.raises(ValueError):
            parse_hil_targets([target(), duplicate])
    for invalid in (None, [], {}, [target()] * 9, [{**target(), "display_name": "same name"}],
                    [{**target(), "mac": "*"}], [{**target(), "terminal_serial": " terminal-1"}]):
        with pytest.raises(ValueError):
            parse_hil_targets(invalid)


@pytest.mark.parametrize("field,value", [
    ("active", False), ("is_spare", True), ("connector_id", "other"),
    ("hardware_id", "a4:cb:8f:d4:66:02"), ("zkt_device", None),
])
def test_matching_requires_all_connector_evidence(field, value):
    device = connector()
    exact = parse_hil_targets([target()])[0]
    assert target_matches(exact, device)
    setattr(device, field, value)
    assert not target_matches(exact, device)


@pytest.mark.parametrize("field", ["serial", "expected_serial", "confirmed_serial"])
def test_replaced_or_unconfirmed_terminal_cannot_receive_candidate(field):
    device = connector()
    setattr(device.zkt_device, field, "replacement")
    assert not target_matches(parse_hil_targets([target()])[0], device)


@pytest.fixture
def hil_session(monkeypatch, tmp_path):
    import json
    import secrets
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from zk_add.models import Base, Connector, ZKTDevice
    from zk_add import ota
    from zk_add.settings import settings

    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_hil_targets_json", json.dumps([target(1), target(2)]))
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(ota, "sync_release_store", lambda _session: None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        release = ota.FirmwareRelease(
            release_id="ordered-hil", version="2.5.99", git_sha="a" * 40,
            image_sha256="b" * 64, image_size=1024, signing_key_id="production-key",
            partition_layout=ota.OTA_LAYOUT, minimum_bootstrap_version="2.2.0",
            storage_name="hil/firmware.bin", manifest_signature="test-signature", state="HIL_ONLY",
            manifest={"application_sha256": "c" * 64, "_hil_targets": [target(1), target(2)]},
        )
        session.add(release)
        devices = []
        for i in (1, 2, 3):
            row = Connector(
                connector_id=f"connector-{i}", hardware_id=target(i)["mac"],
                zone_id="ZONE-HIL", zone_name="HIL", device_id=str(i),
                display_name="same name", firmware_version="2.5.4", connected=True,
                ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
                ota_partition_layout=ota.OTA_LAYOUT, is_spare=i == 3,
            )
            row.zkt_device = ZKTDevice(serial=target(i)["terminal_serial"],
                                      expected_serial=target(i)["terminal_serial"],
                                      confirmed_serial=target(i)["terminal_serial"])
            session.add(row)
            devices.append(row)
        session.flush()
        (tmp_path / "hil").mkdir()
        (tmp_path / release.storage_name).write_bytes(b"test artifact")
        yield session, release, devices
    engine.dispose()


def preview(session, release):
    from zk_add.ota import preview_campaign_scope
    return preview_campaign_scope(session, release_public_id=release.release_id, zone_id="ZONE-HIL")


def campaign(session, release):
    from zk_add.ota import create_campaign
    scope = preview(session, release)
    return create_campaign(session, release_public_id=release.release_id, zone_id="ZONE-HIL",
                           reason="Bounded HIL", typed_confirmation=release.version,
                           actor="test-admin", scope_token=scope["scope_token"],
                           idempotency_key="test-campaign")


def test_ordered_preview_excludes_second_target_and_spare(hil_session):
    session, release, _devices = hil_session
    result = preview(session, release)
    assert [row["connector_id"] for row in result["eligible"]] == ["connector-1"]
    assert {row["connector_id"] for row in result["excluded"]} == {"connector-2", "connector-3"}


@pytest.mark.parametrize("field,value", [
    ("hardware_id", "00:11:22:33:44:55"), ("is_spare", True), ("active", False),
])
def test_wrong_target_cannot_start_campaign(hil_session, field, value):
    session, release, devices = hil_session
    setattr(devices[0], field, value)
    with pytest.raises(ValueError, match="exactly one eligible"):
        campaign(session, release)


def test_stale_preview_cannot_retarget_changed_terminal(hil_session):
    from zk_add.ota import verify_campaign_scope_token
    session, release, devices = hil_session
    scope = preview(session, release)
    devices[0].zkt_device.serial = "replacement"
    with pytest.raises(ValueError):
        verify_campaign_scope_token(session, token=scope["scope_token"],
                                    release_public_id=release.release_id, zone_id="ZONE-HIL")


def test_assignment_and_download_recheck_exact_identity(hil_session):
    from zk_add.ota import assignment_for_connector, resolve_download
    session, release, devices = hil_session
    campaign(session, release)
    assert assignment_for_connector(session, connector=devices[1], public_base="https://test.invalid") is None
    offer = assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid")
    assert offer and offer["artifact_sha256"] == release.image_sha256
    session.flush()
    token = offer["download_url"].rsplit("/", 1)[1]
    assert resolve_download(session, token)[0] == release
    devices[0].zkt_device.confirmed_serial = "replacement"
    assert assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid") is None
    with pytest.raises(ValueError, match="exact target mismatch"):
        resolve_download(session, token)


def test_acceptance_requires_same_candidate_hashes_and_pass(hil_session):
    from sqlalchemy import select
    from zk_add.ota import FirmwareDeployment, FirmwareEvent
    session, release, _devices = hil_session
    run = campaign(session, release)
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    details = {"outcome": "INCOMPLETE", "target": target(1), "git_sha": release.git_sha,
               "artifact_sha256": release.image_sha256, "application_sha256": "c" * 64}
    evidence = FirmwareEvent(deployment_id=deployment.id, state="HIL_ACCEPTED", details=details)
    session.add(evidence)
    session.flush()
    assert preview(session, release)["eligible"][0]["connector_id"] == "connector-1"
    evidence.details = {**details, "outcome": "PASS", "artifact_sha256": "d" * 64}
    session.flush()
    assert preview(session, release)["eligible"][0]["connector_id"] == "connector-1"
    evidence.details = {**details, "outcome": "PASS"}
    session.flush()
    assert preview(session, release)["eligible"][0]["connector_id"] == "connector-1"
    deployment.status = "SUCCEEDED"
    session.flush()
    assert preview(session, release)["eligible"][0]["connector_id"] == "connector-2"
    assert release.state == "HIL_ONLY"


def test_order_changed_after_preview_rejected(hil_session, monkeypatch):
    import json
    from zk_add.ota import verify_campaign_scope_token
    from zk_add.settings import settings
    session, release, _devices = hil_session
    scope = preview(session, release)
    release.manifest = {**release.manifest, "_hil_targets": [target(2), target(1)]}
    monkeypatch.setattr(settings, "firmware_hil_targets_json", json.dumps([target(2), target(1)]))
    with pytest.raises(ValueError, match="scope changed"):
        verify_campaign_scope_token(session, token=scope["scope_token"],
                                    release_public_id=release.release_id, zone_id="ZONE-HIL")


@pytest.mark.parametrize("state", ["PAUSED", "CANCELLED"])
def test_paused_or_cancelled_hil_rejects_existing_download_grant(hil_session, state):
    from zk_add.ota import assignment_for_connector, resolve_download
    session, release, devices = hil_session
    run = campaign(session, release)
    offer = assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid")
    session.flush()
    run.status = state
    with pytest.raises(ValueError, match="not active"):
        resolve_download(session, offer["download_url"].rsplit("/", 1)[1])


def test_concurrent_campaign_cannot_expand_hil_scope(hil_session):
    from zk_add.ota import create_campaign
    session, release, _devices = hil_session
    campaign(session, release)
    scope = preview(session, release)
    with pytest.raises(ValueError, match="active or paused"):
        create_campaign(session, release_public_id=release.release_id, zone_id="ZONE-HIL",
                        reason="Concurrent", typed_confirmation=release.version, actor="test-admin",
                        scope_token=scope["scope_token"], idempotency_key="second-campaign")


def test_revoked_release_rejects_grant_and_assignment(hil_session):
    from zk_add.ota import assignment_for_connector, resolve_download
    session, release, devices = hil_session
    campaign(session, release)
    offer = assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid")
    session.flush()
    release.state = "REVOKED"
    assert assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid") is None
    with pytest.raises(ValueError, match="unavailable"):
        resolve_download(session, offer["download_url"].rsplit("/", 1)[1])


@pytest.mark.parametrize("state", ["OFFERED", "DOWNLOADING", "VERIFYING", "READY_TO_BOOT",
                                    "BOOTED_PENDING", "RECONCILING"])
def test_matching_heartbeat_version_cannot_complete_hil_deployment(hil_session, state):
    from sqlalchemy import select
    from zk_add.ota import FirmwareDeployment, FirmwareEvent, assignment_for_connector
    session, release, devices = hil_session
    run = campaign(session, release)
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    deployment.status = state
    devices[0].firmware_version = release.version
    session.flush()
    offer = assignment_for_connector(session, connector=devices[0], public_base="https://test.invalid")
    assert offer and offer["image_sha256"] == release.manifest["application_sha256"]
    assert deployment.status == state
    assert deployment.completed_at is None
    assert session.scalar(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "SUCCEEDED")) is None
