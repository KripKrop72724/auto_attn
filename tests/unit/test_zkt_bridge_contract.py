"""Bridge admission exercises signed packages and predecessor identity, not labels."""
import base64
import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import FirmwareRelease, _storage_predecessor_exclusion, sync_release_store
from zk_add.settings import settings
from zk_add.storage_contract import validate_storage_contract
from zk_add.zkt_bridge_contract import (
    BRIDGE_MARKER, BRIDGE_VERSION, PREDECESSOR_IMAGES, bridge_contract,
    validate_bridge_image,
)


def bridge_image():
    image = bytearray(112)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:54] = b"2.6.16"
    image[80:89] = b"zone_lite"
    return bytes(image) + BRIDGE_MARKER.encode() + b"\0"


def bridge_manifest():
    return {
        "release_id": "zone-lite-2.6.16", "version": BRIDGE_VERSION,
        "git_sha": "a" * 40, "application_sha256": "c" * 64,
        "firmware_family": "zkt", "project_name": "zone_lite",
        "minimum_bootstrap_version": "2.4.12",
        "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "queue_storage": bridge_contract(),
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
    assert validate_storage_contract(manifest, BRIDGE_VERSION) == bridge_contract()
    assert manifest["queue_storage"]["delivery_authority"] == "LEGACY_UNTIL_PERSISTED_ADD_CUTOVER"
    validate_bridge_image(bridge_image())
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, "2.7.0")


@pytest.mark.parametrize("field,value", [
    ("version", "2.7.0"), ("firmware_family", "hikvision"), ("project_name", "zone_lite_hikvision"),
    ("release_channel", "AVAILABLE"), ("minimum_bootstrap_version", "2.2.0"),
    ("queue_storage", None), ("queue_storage", {}),
])
def test_bridge_rejects_wrong_role_or_missing_contract(field, value):
    manifest = bridge_manifest()
    manifest[field] = value
    with pytest.raises(ValueError, match="Journal bridge"):
        validate_storage_contract(manifest, BRIDGE_VERSION)


@pytest.mark.parametrize("field", list(bridge_contract()))
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
    lambda image: image.replace(b"2.6.16", b"2.6.15"),
    lambda image: image.replace(b"zone_lite", b"wrong_app"),
    lambda image: image[:112], lambda image: image.replace(b"CAPTURE=1", b"CAPTURE=0"),
    lambda image: image + BRIDGE_MARKER.encode() + b"\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V3:UNKNOWN\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V2:LEGACY\0",
    lambda image: image + b"ZONE_STORAGE_CONTRACT_V1:LEGACY\0",
])
def test_bridge_binary_must_match_signed_capability(mutation):
    with pytest.raises(ValueError, match="compiled reader/capture marker"):
        validate_bridge_image(mutation(bridge_image()))


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
              "targets": [{"connector_id": "bridge-test", "mac": "00:11:22:33:44:55",
                           "terminal_serial": "BRIDGE-TEST"}]}

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
    assert rows[0].manifest["_hil_targets"][0]["terminal_serial"] == "BRIDGE-TEST"
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
