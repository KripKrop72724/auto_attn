"""Bridge admission exercises signed packages and predecessor identity, not labels."""
import base64
import hashlib
import json
import struct
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector, ZKTDevice
from zk_add.ota import (FirmwareRelease, _storage_predecessor_exclusion, sync_release_store,
                        _parse_release_hil_targets, _permitted_hil_targets,
                        FirmwareDeployment, FirmwareEvent, preview_campaign_scope,
                        create_campaign, assignment_for_connector)
from zk_add.settings import settings
from zk_add.storage_contract import validate_storage_contract
from zk_add.zkt_bridge_contract import (
    BRIDGE_MARKER, BRIDGE_VERSION, PREDECESSOR_IMAGES, bridge_contract,
    validate_bridge_image, signed_hil_targets, bridge_hil_targets, bridge_marker,
)


@pytest.fixture(params=["2.6.16", "2.6.17"], autouse=True)
def bridge_version(request, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "BRIDGE_VERSION", request.param)
    monkeypatch.setattr(sys.modules[__name__], "BRIDGE_MARKER", bridge_marker(request.param))


def bridge_image():
    image = bytearray(112)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:54] = BRIDGE_VERSION.encode()
    image[80:89] = b"zone_lite"
    return bytes(image) + BRIDGE_MARKER.encode() + b"\0"


def bridge_manifest():
    return {
        "release_id": f"zone-lite-{BRIDGE_VERSION}", "version": BRIDGE_VERSION,
        "git_sha": "a" * 40, "application_sha256": "c" * 64,
        "firmware_family": "zkt", "project_name": "zone_lite",
        "minimum_bootstrap_version": "2.4.12",
        "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "hil_targets": signed_hil_targets(),
        "queue_storage": bridge_contract(BRIDGE_VERSION),
    }


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def test_bridge_has_no_implicit_writer_authority():
    manifest = bridge_manifest()
    assert validate_storage_contract(manifest, BRIDGE_VERSION) == bridge_contract(BRIDGE_VERSION)
    assert manifest["queue_storage"]["delivery_authority"] == "LEGACY_UNTIL_PERSISTED_ADD_CUTOVER"
    validate_bridge_image(bridge_image(), BRIDGE_VERSION)
    with pytest.raises(ValueError, match="Journal writer"):
        validate_storage_contract(manifest, "2.7.0")


@pytest.mark.parametrize("field,value", [
    ("version", "2.7.0"), ("firmware_family", "hikvision"), ("project_name", "zone_lite_hikvision"),
    ("release_channel", "AVAILABLE"), ("minimum_bootstrap_version", "2.2.0"),
    ("queue_storage", None), ("queue_storage", {}),
    ("hil_targets", None), ("hil_targets", []), ("release_id", "another-bridge"),
])
def test_bridge_rejects_wrong_role_or_missing_contract(field, value):
    manifest = bridge_manifest()
    manifest[field] = value
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, BRIDGE_VERSION)


@pytest.mark.parametrize("field", list(bridge_contract(BRIDGE_VERSION)))
def test_every_bridge_capability_is_required(field):
    manifest = bridge_manifest()
    del manifest["queue_storage"][field]
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, BRIDGE_VERSION)


@pytest.mark.parametrize("field,value", [
    ("read_format", True), ("journal_write_format", True), ("journal_read_format", 1.0),
    ("journal_capture", 1), ("reader_mask", 31), ("journal_reader_mask", 31),
    ("write_format", 2), ("delivery_authority", "ADD"), ("compatibility_version", "2.5.4"),
    ("allowed_bootstrap_versions", ["2.4.12", "2.5.2", "2.6.15", "2.6.14"]),
    ("allowed_bootstrap_images", {**PREDECESSOR_IMAGES, "2.6.15": "b" * 64}),
])
def test_bridge_rejects_changed_or_coerced_capabilities(field, value):
    manifest = bridge_manifest()
    manifest["queue_storage"][field] = value
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, BRIDGE_VERSION)


@pytest.mark.parametrize("mutation", [
    lambda image: image[:50], lambda image: b"X" + image[1:],
    lambda image: image[:32] + b"XXXX" + image[36:],
    lambda image: image.replace(BRIDGE_VERSION.encode(), b"2.6.15"),
    lambda image: image.replace(b"zone_lite", b"wrong_app"),
    lambda image: image[:112], lambda image: image.replace(b"CAPTURE=1", b"CAPTURE=0"),
    lambda image: image + BRIDGE_MARKER.encode() + b"\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V3:UNKNOWN\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V2:LEGACY\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V1:LEGACY\0",
])
def test_bridge_binary_must_match_signed_capability(mutation):
    with pytest.raises(ValueError, match="compiled reader/capture marker"):
        validate_bridge_image(mutation(bridge_image()), BRIDGE_VERSION)


@pytest.mark.parametrize("version", list(PREDECESSOR_IMAGES))
def test_predecessor_is_pinned_to_image_and_ota_partition(session, version):
    predecessor = FirmwareRelease(
        release_id=f"zone-lite-{version}", version=version, git_sha="a" * 40,
        image_sha256="b" * 64, image_size=1024, signing_key_id="test",
        partition_layout="zone-lite-ota-v1", storage_name="predecessor",
        manifest={"application_sha256": PREDECESSOR_IMAGES[version]}, manifest_signature="test",
        state="HIL_ONLY" if version == "2.6.15" else "AVAILABLE",
    )
    session.add(predecessor)
    session.flush()
    release = FirmwareRelease(version=BRIDGE_VERSION, state="HIL_ONLY", manifest=bridge_manifest())
    connector = Connector(firmware_version=version, ota_image_sha256=PREDECESSOR_IMAGES[version],
                          ota_running_partition="ota_0")
    assert _storage_predecessor_exclusion(session, release, connector) is None
    connector.firmware_version = f"zone-lite-{version}"
    connector.ota_running_partition = "ota_1"
    assert _storage_predecessor_exclusion(session, release, connector) is None
    for partition in ("factory", None, "unknown"):
        connector.ota_running_partition = partition
        assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    connector.ota_running_partition = "ota_0"
    connector.ota_image_sha256 = "e" * 64
    assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    connector.ota_image_sha256 = PREDECESSOR_IMAGES[version]
    predecessor.manifest = {"application_sha256": "e" * 64}
    session.flush()
    assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    predecessor.manifest = {"application_sha256": PREDECESSOR_IMAGES[version]}
    predecessor.state = "REVOKED"
    session.flush()
    assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    session.delete(predecessor)
    session.flush()
    assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_IMAGE_UNVERIFIED"
    for firmware in ("2.6.14", "2.7.0", "2.6.16", None):
        connector.firmware_version = firmware
        assert _storage_predecessor_exclusion(session, release, connector) == "DIRECT_BOOTSTRAP_VERSION_UNQUALIFIED"
    release.state = "AVAILABLE"
    assert _storage_predecessor_exclusion(session, release, connector) == "JOURNAL_BRIDGE_HIL_ONLY"


@pytest.fixture
def signed_package(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", base64.b64encode(public).decode())
    monkeypatch.setattr(settings, "firmware_ota_enabled", True)
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    release = tmp_path / BRIDGE_VERSION
    release.mkdir()
    manifest = {**bridge_manifest(), "schema_version": 2, "image_name": "firmware.bin",
                "partition_layout": "zone-lite-ota-v1", "signing_key_id": "disposable-test-key"}
    marker = {"schema_version": 2, "git_sha": manifest["git_sha"],
              "application_sha256": manifest["application_sha256"],
              "targets": signed_hil_targets()[:1]}

    def publish(image=None):
        image = bridge_image() if image is None else image
        manifest.update(image_sha256=hashlib.sha256(image).hexdigest(), image_size=len(image))
        marker["image_sha256"] = manifest["image_sha256"]
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (release / "firmware.bin").write_bytes(image)
        (release / "manifest.json").write_bytes(canonical)
        signature = key.sign(canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        (release / "manifest.sig").write_text(base64.b64encode(signature).decode())
        (release / ".hil-only.json").write_text(json.dumps(marker))
    publish()
    return release, manifest, marker, publish


def test_signed_bridge_loads_only_in_quarantine(session, signed_package):
    release, _, _, _ = signed_package
    sync_release_store(session)
    session.commit()
    sync_release_store(session)
    rows = list(session.scalars(select(FirmwareRelease)))
    assert len(rows) == 1 and rows[0].state == "HIL_ONLY"
    assert rows[0].manifest["_hil_targets"] == signed_hil_targets()[:1]
    (release / ".hil-only.json").unlink()
    with pytest.raises(ValueError, match="exact HIL quarantine marker"):
        sync_release_store(session)
    assert rows[0].state == "HIL_ONLY"


@pytest.mark.parametrize("failure", ["no_marker", "legacy_mac", "wrong_schema", "wrong_scope_hash", "bad_binary", "bad_capability", "bad_signature"])
def test_even_signed_package_cannot_bypass_bridge_contract(session, signed_package, failure):
    release, manifest, marker, publish = signed_package
    if failure == "bad_binary":
        publish(bridge_image().replace(b"CAPTURE=1", b"CAPTURE=0"))
    elif failure == "bad_capability":
        manifest["queue_storage"]["journal_capture"] = False
        publish()
    elif failure == "bad_signature":
        (release / "manifest.sig").write_text(base64.b64encode(b"bad").decode())
    elif failure == "no_marker":
        (release / ".hil-only.json").unlink()
    else:
        if failure == "legacy_mac":
            del marker["targets"]
            marker["target_mac"] = "00:11:22:33:44:55"
        elif failure == "wrong_schema":
            marker["schema_version"] = 1
        else:
            marker["image_sha256"] = "f" * 64
        (release / ".hil-only.json").write_text(json.dumps(marker))
    from cryptography.exceptions import InvalidSignature
    with pytest.raises((ValueError, RuntimeError, InvalidSignature)):
        sync_release_store(session)
    assert session.scalar(select(FirmwareRelease)) is None


def test_signed_nationwide_scope_matches_publisher_inventory():
    file = Path(__file__).resolve().parents[2] / "deploy/add/hil-targets-zkt-270.json"
    assert json.loads(file.read_text()) == signed_hil_targets()


@pytest.mark.parametrize("count", range(1, 18))
def test_bridge_scope_can_expand_without_changing_older_campaigns(session, monkeypatch, count):
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    # A retained legacy global setting is deliberately different from the
    # bridge's signed scope; new admission must not reconfigure older releases.
    legacy = json.dumps([{"connector_id": "old", "mac": "00:11:22:33:44:55", "terminal_serial": "OLD"}])
    monkeypatch.setattr(settings, "firmware_hil_targets_json", legacy)
    targets = signed_hil_targets()[:count]
    release = FirmwareRelease(id=123, release_id=f"zone-lite-{BRIDGE_VERSION}", version=BRIDGE_VERSION,
        git_sha="a" * 40, image_sha256="b" * 64, state="HIL_ONLY",
        manifest={**bridge_manifest(), "_hil_targets": targets})
    assert [row.model_dump() for row in bridge_hil_targets(targets)] == targets
    assert [row.model_dump() for row in _permitted_hil_targets(session, release)] == targets[:1]
    assert settings.firmware_hil_targets_json == legacy
    monkeypatch.setattr(settings, "firmware_hil_enabled", False)
    with pytest.raises(ValueError, match="disabled"):
        _permitted_hil_targets(session, release)


@pytest.mark.parametrize("mutation", ["changed", "reordered", "missing", "extra", "duplicate", "unknown-key"])
def test_bridge_scope_rejects_unreviewed_identities_and_order(mutation):
    targets = signed_hil_targets()
    if mutation == "changed":
        targets[0]["terminal_serial"] = "different"
    elif mutation == "reordered":
        targets[:2] = reversed(targets[:2])
    elif mutation == "missing":
        targets.pop(0)
    elif mutation == "extra":
        targets.append(dict(targets[-1]))
    elif mutation == "duplicate":
        targets[1] = dict(targets[0])
    else:
        targets[0]["extra"] = True
    with pytest.raises(ValueError, match="exact nationwide prefix"):
        bridge_hil_targets(targets)
    manifest = {**bridge_manifest(), "hil_targets": targets}
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, BRIDGE_VERSION)


def test_legacy_scope_still_has_its_original_limit():
    with pytest.raises(ValueError, match="one to eight"):
        _parse_release_hil_targets(("zone-lite-2.6.14", "2.6.14", "a", "b", "c"), signed_hil_targets())


def test_real_bridge_campaign_and_scope_expansion_require_exact_previous_acceptance(session, signed_package, monkeypatch):
    package, _, marker, _ = signed_package
    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "firmware_hil_targets_json", None)
    session.add(FirmwareRelease(release_id="zone-lite-2.6.15", version="2.6.15", git_sha="d" * 40,
        image_sha256="e" * 64, image_size=1024, signing_key_id="test", partition_layout="zone-lite-ota-v1",
        storage_name="retained-2615", manifest={"application_sha256": PREDECESSOR_IMAGES["2.6.15"]},
        manifest_signature="test", state="HIL_ONLY"))
    connectors = []
    for index, target in enumerate(signed_hil_targets()[:2]):
        connector = Connector(connector_id=target["connector_id"], hardware_id=target["mac"],
            zone_id=f"ZONE-{index}", zone_name="Test", device_id=str(index), display_name="Test",
            firmware_family="zkt", firmware_version="2.6.15", connected=True,
            ota_capable=True, ota_secure_boot=True, ota_rollback_enabled=True,
            ota_partition_layout="zone-lite-ota-v1", ota_running_partition="ota_0",
            ota_image_sha256=PREDECESSOR_IMAGES["2.6.15"])
        connector.zkt_device = ZKTDevice(serial=target["terminal_serial"], expected_serial=target["terminal_serial"],
            confirmed_serial=target["terminal_serial"], terminal_binding_state="CONFIRMED")
        session.add(connector)
        connectors.append(connector)
    session.flush()
    preview = preview_campaign_scope(session, release_public_id=f"zone-lite-{BRIDGE_VERSION}", zone_id="ZONE-0")
    assert [row["connector_id"] for row in preview["eligible"]] == [connectors[0].connector_id]
    campaign = create_campaign(session, release_public_id=f"zone-lite-{BRIDGE_VERSION}", zone_id="ZONE-0",
        reason="Test bridge", typed_confirmation=BRIDGE_VERSION, actor="test", scope_token=preview["scope_token"],
        idempotency_key="test-bridge-campaign")
    offer = assignment_for_connector(session, connector=connectors[0], public_base="https://test.invalid")
    assert offer and offer["version"] == BRIDGE_VERSION
    assert assignment_for_connector(session, connector=connectors[1], public_base="https://test.invalid") is None
    deployment = session.scalar(select(FirmwareDeployment).where(FirmwareDeployment.campaign_id == campaign.id))
    release = session.get(FirmwareRelease, campaign.release_id)
    marker["targets"] = signed_hil_targets()[:2]
    (package / ".hil-only.json").write_text(json.dumps(marker))
    sync_release_store(session)
    assert _permitted_hil_targets(session, release)[0].connector_id == connectors[0].connector_id
    details = dict(outcome="PASS", target=signed_hil_targets()[0], git_sha=release.git_sha,
        artifact_sha256=release.image_sha256, application_sha256=release.manifest["application_sha256"])
    event = FirmwareEvent(deployment_id=deployment.id, state="HIL_ACCEPTED", details=details)
    session.add(event)
    session.flush()
    assert _permitted_hil_targets(session, release)[0].connector_id == connectors[0].connector_id
    deployment.status = "SUCCEEDED"
    session.flush()
    # The original bridge retains its historical verdict contract. Replacement
    # preparation requires a stored BRIDGE_READINESS_V1 run, exercised by the
    # real server-observation tests; a generic HIL event cannot stand in for it.
    expected = 1 if BRIDGE_VERSION == "2.6.16" else 0
    assert _permitted_hil_targets(session, release)[0].connector_id == connectors[expected].connector_id
    event.details = {**details, "application_sha256": "f" * 64}
    session.flush()
    assert _permitted_hil_targets(session, release)[0].connector_id == connectors[0].connector_id
    assert release.state == "HIL_ONLY"


def test_package_version_cannot_be_relabelled():
    other = "2.6.17" if BRIDGE_VERSION == "2.6.16" else "2.6.16"
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(bridge_manifest(), other)
    with pytest.raises(ValueError, match="compiled reader/capture marker"):
        validate_bridge_image(bridge_image(), other)
    renamed = bridge_image().replace(BRIDGE_VERSION.encode(), other.encode(), 1)
    with pytest.raises(ValueError, match="compiled reader/capture marker"):
        validate_bridge_image(renamed, other)
