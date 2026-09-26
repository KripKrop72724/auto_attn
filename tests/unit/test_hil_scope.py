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
                                  confirmed_serial="terminal-1", terminal_binding_state="CONFIRMED"),
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


def test_pending_terminal_pin_cannot_receive_candidate():
    device = connector()
    device.zkt_device.terminal_binding_state = "PENDING_DEVICE_ACK"
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
                                      confirmed_serial=target(i)["terminal_serial"],
                                      terminal_binding_state="CONFIRMED")
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


def test_hil_preflight_reports_the_exact_target_exclusion(hil_session):
    session, release, devices = hil_session
    devices[0].ota_capable = False
    with pytest.raises(ValueError, match="target exclusion: OTA_NOT_CAPABLE"):
        preview(session, release)


def test_hil_preflight_reports_unverified_direct_predecessor(hil_session):
    from zk_add.storage_contract import DIRECT_BASELINE_IMAGES, DIRECT_BASELINES, DIRECT_VERSION

    session, release, devices = hil_session
    release.version = DIRECT_VERSION
    release.minimum_bootstrap_version = DIRECT_BASELINES[0]
    release.manifest = {
        **release.manifest,
        "minimum_bootstrap_version": DIRECT_BASELINES[0],
        "queue_storage": {
            "schema_version": 2,
            "read_format": 2,
            "reader_mask": 63,
            "write_format": 1,
            "allowed_bootstrap_versions": list(DIRECT_BASELINES),
            "allowed_bootstrap_images": DIRECT_BASELINE_IMAGES,
        },
    }
    devices[0].firmware_version = "2.5.2"
    with pytest.raises(ValueError, match="target exclusion: DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"):
        preview(session, release)


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


@pytest.fixture
def parallel_269_session(hil_session, monkeypatch):
    import json
    from zk_add.ota import FirmwareRelease, HIL_269_EXACT_TARGETS, HIL_269_PARALLEL_IDENTITY
    from zk_add.settings import settings
    from zk_add.models import Connector, ZKTDevice
    from zk_add.storage_contract import CONTENTION_BASELINES, CONTENTION_BASELINE_IMAGES

    session, release, devices = hil_session
    release.release_id, release.version, release.git_sha, release.image_sha256, application = (
        HIL_269_PARALLEL_IDENTITY
    )
    release.minimum_bootstrap_version = "2.4.12"
    release.manifest = {
        "application_sha256": application,
        "minimum_bootstrap_version": "2.4.12",
        "_hil_targets": [row.model_dump() for row in HIL_269_EXACT_TARGETS],
        "queue_storage": {
            "schema_version": 2, "read_format": 2, "reader_mask": 63,
            "write_format": 1,
            "allowed_bootstrap_versions": list(CONTENTION_BASELINES),
            "allowed_bootstrap_images": CONTENTION_BASELINE_IMAGES,
        },
    }
    monkeypatch.setattr(settings, "firmware_hil_targets_json", json.dumps(release.manifest["_hil_targets"]))
    baseline_digest = CONTENTION_BASELINE_IMAGES["2.4.12"]
    session.add(FirmwareRelease(
        release_id="zone-lite-2.4.12", version="2.4.12", git_sha="d" * 40,
        image_sha256="e" * 64, image_size=1024, signing_key_id="production-key",
        partition_layout="zone-lite-ota-v1", minimum_bootstrap_version="2.2.0",
        storage_name="hil/baseline.bin", manifest_signature="test-signature", state="AVAILABLE",
        manifest={"application_sha256": baseline_digest},
    ))
    zones = (
        "ZONE-SWAT-01", "ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-3FL",
        "ZONE-PESHAWAR-02", "ZONE-PESHAWAR-06",
    )
    for index, (target_row, zone) in enumerate(zip(HIL_269_EXACT_TARGETS, zones)):
        if index < len(devices):
            device = devices[index]
        else:
            device = Connector(
                connector_id=target_row.connector_id, hardware_id=target_row.mac,
                zone_id=zone, zone_name=zone, device_id=str(index + 1),
                display_name=zone, connected=True, ota_capable=True,
                ota_secure_boot=True, ota_rollback_enabled=True,
                ota_partition_layout="zone-lite-ota-v1",
            )
            device.zkt_device = ZKTDevice()
            session.add(device)
            devices.append(device)
        device.connector_id = target_row.connector_id
        device.hardware_id = target_row.mac
        device.zone_id = zone
        device.is_spare = False
        device.firmware_version = "zone-lite-2.4.12"
        device.ota_image_sha256 = baseline_digest
        device.ota_running_partition = "ota_0"
        device.zkt_device.serial = target_row.terminal_serial
        device.zkt_device.expected_serial = target_row.terminal_serial
        device.zkt_device.confirmed_serial = target_row.terminal_serial
        device.zkt_device.terminal_binding_state = "CONFIRMED"
    session.flush()
    return session, release, devices, zones


def test_exact_269_first_three_can_start_independent_hil_campaigns(parallel_269_session):
    from zk_add.ota import assignment_for_connector, create_campaign, preview_campaign_scope

    session, release, devices, zones = parallel_269_session
    for index in (1, 2, 0):
        scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[index])
        assert [row["connector_id"] for row in scope["eligible"]] == [devices[index].connector_id]
        campaign = create_campaign(
            session, release_public_id=release.release_id, zone_id=zones[index],
            reason="Independent exact HIL", typed_confirmation=release.version,
            actor="test-admin", scope_token=scope["scope_token"],
            idempotency_key=f"parallel-{index}",
        )
        assert campaign.eligible_count == 1
        assert assignment_for_connector(session, connector=devices[index],
                                        public_base="https://test.invalid") is not None
    with pytest.raises(ValueError, match="exact target is not active"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[3])


def test_269_peshawar_waits_for_all_three_exact_acceptances(parallel_269_session):
    from sqlalchemy import select
    from zk_add.ota import (
        FirmwareDeployment, FirmwareEvent, HIL_269_EXACT_TARGETS,
        _permitted_hil_targets, create_campaign, preview_campaign_scope,
    )

    session, release, devices, zones = parallel_269_session
    for index in (1, 0, 2):
        scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[index])
        campaign = create_campaign(
            session, release_public_id=release.release_id, zone_id=zones[index],
            reason="Independent exact HIL", typed_confirmation=release.version,
            actor="test-admin", scope_token=scope["scope_token"],
            idempotency_key=f"accept-{index}",
        )
        deployment = session.scalar(select(FirmwareDeployment).where(
            FirmwareDeployment.campaign_id == campaign.id
        ))
        deployment.status = "SUCCEEDED"
        session.add(FirmwareEvent(
            deployment_id=deployment.id, state="HIL_ACCEPTED",
            details={
                "outcome": "PASS", "target": HIL_269_EXACT_TARGETS[index].model_dump(),
                "git_sha": release.git_sha, "artifact_sha256": release.image_sha256,
                "application_sha256": release.manifest["application_sha256"],
            },
        ))
        session.flush()
        if index != 2:
            with pytest.raises(ValueError, match="exact target is not active"):
                preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[3])
    assert _permitted_hil_targets(session, release) == [HIL_269_EXACT_TARGETS[3]]
    assert preview_campaign_scope(session, release_public_id=release.release_id,
                                  zone_id=zones[3])["counts"]["eligible"] == 1


def test_269_parallel_policy_requires_the_exact_published_identity(parallel_269_session):
    from zk_add.ota import HIL_269_EXACT_TARGETS, _permitted_hil_targets

    session, release, _devices, _zones = parallel_269_session
    release.git_sha = "0" * 40
    session.flush()
    assert _permitted_hil_targets(session, release) == [HIL_269_EXACT_TARGETS[0]]


@pytest.mark.parametrize("version", ["2.6.10", "2.6.11"])
def test_signed_patch_exact_scope_keeps_first_three_independent(parallel_269_session, version):
    from zk_add.ota import HIL_269_EXACT_TARGETS, _permitted_hil_targets
    from zk_add.storage_contract import (PRESSURE_BASELINES, PRESSURE_BASELINE_IMAGES,
                                         PROBE_BASELINES, PROBE_BASELINE_IMAGES)

    session, release, _devices, _zones = parallel_269_session
    release.release_id = f"zone-lite-{version}"
    release.version = version
    release.git_sha = "f" * 40
    release.image_sha256 = "9" * 64
    release.manifest = {
        **release.manifest,
        "application_sha256": "d" * 64,
        "queue_storage": {
            **release.manifest["queue_storage"],
            "allowed_bootstrap_versions": list(PROBE_BASELINES if version == "2.6.11" else PRESSURE_BASELINES),
            "allowed_bootstrap_images": PROBE_BASELINE_IMAGES if version == "2.6.11" else PRESSURE_BASELINE_IMAGES,
        },
    }
    session.flush()
    assert _permitted_hil_targets(session, release) == list(HIL_269_EXACT_TARGETS[:3])
    release.manifest = {**release.manifest, "_hil_targets": [
        *release.manifest["_hil_targets"][:2],
        {**release.manifest["_hil_targets"][2], "terminal_serial": "replacement"},
        *release.manifest["_hil_targets"][3:],
    ]}
    with pytest.raises(ValueError, match="configured exact scope"):
        _permitted_hil_targets(session, release)


def test_252_factory_bridge_selects_only_live_3fl_and_rechecks_grant(parallel_269_session, monkeypatch):
    from zk_add.models import Connector, ZKTDevice
    from zk_add.ota import (
        FACTORY_3FL_BRIDGE_RELEASE, HIL_269_EXACT_TARGETS,
        assignment_for_connector, create_campaign, preview_campaign_scope, resolve_download,
    )
    from zk_add.settings import settings

    session, release, devices, zones = parallel_269_session
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    release.release_id, release.version, release.image_sha256, app_digest = FACTORY_3FL_BRIDGE_RELEASE
    release.state = "AVAILABLE"
    release.minimum_bootstrap_version = "2.2.0"
    release.manifest = {"application_sha256": app_digest}
    target = devices[2]
    target.ota_running_partition = "factory"
    spare = Connector(
        connector_id="spare-3fl", hardware_id="aa:bb:cc:dd:ee:ff",
        zone_id=zones[2], zone_name=zones[2], device_id="spare",
        display_name="3FL spare", firmware_version="zone-lite-2.4.12",
        connected=False, is_spare=True, ota_capable=True, ota_secure_boot=True,
        ota_rollback_enabled=True, ota_partition_layout="zone-lite-ota-v1",
    )
    spare.zkt_device = ZKTDevice(serial="spare", expected_serial="spare",
                                  confirmed_serial="spare", terminal_binding_state="CONFIRMED")
    session.add(spare)
    session.flush()
    scope = preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[2])
    assert [row["connector_id"] for row in scope["eligible"]] == [HIL_269_EXACT_TARGETS[2].connector_id]
    assert scope["counts"]["excluded"] == 1
    run = create_campaign(
        session, release_public_id=release.release_id, zone_id=zones[2],
        reason="Exact factory bridge", typed_confirmation=release.version,
        actor="test-admin", scope_token=scope["scope_token"], idempotency_key="3fl-bridge",
    )
    assert run.eligible_count == 1
    assert assignment_for_connector(session, connector=spare, public_base="https://test.invalid") is None
    offer = assignment_for_connector(session, connector=target, public_base="https://test.invalid")
    assert offer is not None
    session.flush()
    grant = offer["download_url"].rsplit("/", 1)[1]
    assert resolve_download(session, grant)[0] == release
    target.zone_id = "ZONE-OTHER"
    with pytest.raises(ValueError, match="bridge exact target or predecessor changed"):
        resolve_download(session, grant)
    target.zone_id = zones[2]
    target.ota_running_partition = "ota_0"
    with pytest.raises(ValueError, match="bridge exact target or predecessor changed"):
        resolve_download(session, grant)


def test_252_factory_bridge_rejects_changed_identity_or_predecessor(parallel_269_session, monkeypatch):
    from zk_add.ota import FACTORY_3FL_BRIDGE_RELEASE, HIL_269_EXACT_TARGETS, preview_campaign_scope
    from zk_add.settings import settings

    session, release, devices, zones = parallel_269_session
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    release.release_id, release.version, release.image_sha256, app_digest = FACTORY_3FL_BRIDGE_RELEASE
    release.state = "AVAILABLE"
    release.minimum_bootstrap_version = "2.2.0"
    release.manifest = {"application_sha256": app_digest}
    target = devices[2]
    target.ota_running_partition = "factory"
    target.zkt_device.confirmed_serial = "replacement"
    with pytest.raises(ValueError, match="BRIDGE_EXACT_IDENTITY_MISMATCH"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[2])
    target.zkt_device.confirmed_serial = HIL_269_EXACT_TARGETS[2].terminal_serial
    target.ota_image_sha256 = "f" * 64
    with pytest.raises(ValueError, match="BRIDGE_FACTORY_PREDECESSOR_MISMATCH"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[2])
    release.image_sha256 = "f" * 64
    with pytest.raises(ValueError, match="exact published 2.5.2 image"):
        preview_campaign_scope(session, release_public_id=release.release_id, zone_id=zones[2])


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


def test_compatibility_second_target_waits_for_first_candidate_acceptance(hil_session):
    from sqlalchemy import select
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareEvent, FirmwareRelease, _ordered_hil_target
    from zk_add.storage_contract import COMPAT_VERSION, CANDIDATE_VERSION

    session, compatibility, devices = hil_session
    run = campaign(session, compatibility)
    compatibility.version = COMPAT_VERSION
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == run.id))
    assert _ordered_hil_target(session, compatibility).connector_id == "connector-1"
    deployment.status = "SUCCEEDED"
    run.status = "COMPLETED"
    session.add(FirmwareEvent(deployment_id=deployment.id, state="HIL_ACCEPTED", details={
        "outcome": "PASS", "target": target(1), "git_sha": compatibility.git_sha,
        "artifact_sha256": compatibility.image_sha256, "application_sha256": "c" * 64,
    }))
    session.flush()
    with pytest.raises(ValueError, match="matching hardening candidate"):
        _ordered_hil_target(session, compatibility)
    candidate = FirmwareRelease(
        release_id="hardening-candidate", version=CANDIDATE_VERSION, git_sha="d" * 40,
        image_sha256="e" * 64, image_size=1024, signing_key_id="production-key",
        partition_layout=compatibility.partition_layout, minimum_bootstrap_version=COMPAT_VERSION,
        storage_name="candidate.bin", manifest_signature="fixture", state="HIL_ONLY",
        manifest={"_hil_targets": [target(1), target(2)], "application_sha256": "f" * 64},
    )
    session.add(candidate)
    session.flush()
    with pytest.raises(ValueError, match="no hardening-candidate HIL acceptance"):
        _ordered_hil_target(session, compatibility)
    candidate_run = FirmwareCampaign(campaign_id="candidate-run", release_id=candidate.id, zone_id="ZONE-HIL",
                                    status="COMPLETED", actor="test", idempotency_key="candidate",
                                    reason="test", typed_confirmation=CANDIDATE_VERSION)
    session.add(candidate_run)
    session.flush()
    candidate_deployment = FirmwareDeployment(deployment_id="candidate-first", campaign_id=candidate_run.id,
        release_id=candidate.id, connector_id=devices[0].id, status="SUCCEEDED", target_version=CANDIDATE_VERSION)
    session.add(candidate_deployment)
    session.flush()
    details = {"outcome": "PASS", "target": target(1), "git_sha": candidate.git_sha,
               "artifact_sha256": candidate.image_sha256, "application_sha256": "f" * 64}
    proof = FirmwareEvent(deployment_id=candidate_deployment.id, state="HIL_ACCEPTED", details=details)
    session.add(proof)
    session.flush()
    assert _ordered_hil_target(session, compatibility).connector_id == "connector-2"
    for field, wrong in (("outcome", "INCOMPLETE"), ("artifact_sha256", "a" * 64),
                         ("git_sha", "b" * 40), ("application_sha256", "a" * 64), ("target", target(3))):
        proof.details = {**details, field: wrong}
        with pytest.raises(ValueError):
            _ordered_hil_target(session, compatibility)
    proof.details = details
    for state in ("HIL_FAILED", "HIL_INCOMPLETE"):
        proof.state = state
        with pytest.raises(ValueError):
            _ordered_hil_target(session, compatibility)
    proof.state = "HIL_ACCEPTED"
    candidate_deployment.status = "RECONCILING"
    with pytest.raises(ValueError):
        _ordered_hil_target(session, compatibility)
    candidate_deployment.status = "SUCCEEDED"
    candidate.state = "REVOKED"
    with pytest.raises(ValueError):
        _ordered_hil_target(session, compatibility)
    candidate.state = "HIL_ONLY"
    candidate.manifest = {**candidate.manifest, "_hil_targets": [target(1), target(3)]}
    with pytest.raises(ValueError):
        _ordered_hil_target(session, compatibility)
