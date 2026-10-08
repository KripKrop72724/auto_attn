"""The factory bridge cannot fall through ordinary same-version OTA admission."""
from copy import deepcopy
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from zk_add.zkt_bridge_contract import (bridge_contract, bridge_marker, signed_hil_targets,
                                       validate_bridge_manifest, validate_bridge_image)
from zk_add.zkt_factory_contract import (FACTORY_TARGETS, factory_trial_exposure,
    factory_trial_signing_contract, factory_trial_targets, validate_factory_trial_contract)
from zk_add.storage_contract import validate_storage_contract
from zk_add.ota import _parse_release_hil_targets


def manifest():
    return {"release_id": "zone-lite-2.6.22", "version": "2.6.22", "project_name": "zone_lite",
        "firmware_family": "zkt", "release_channel": "EXPERIMENTAL_HIL_ONLY",
        "minimum_bootstrap_version": "2.5.2", "hil_targets": signed_hil_targets(),
        "queue_storage": bridge_contract("2.6.22"), "factory_trial": factory_trial_signing_contract()}


def test_three_installed_factory_pins_remain_distinct_inside_seventeen_device_scope():
    value = manifest()
    assert validate_storage_contract(value, "2.6.22") == value["queue_storage"]
    assert value["queue_storage"]["allowed_bootstrap_versions"] == []
    assert value["queue_storage"]["allowed_bootstrap_images"] == {}
    assert len(value["hil_targets"]) == 17
    assert len({p["factory_application_sha256"] for p in FACTORY_TARGETS}) == 3
    assert {p["factory_version"] for p in FACTORY_TARGETS} == {"2.5.2"}
    for size in (1, 2, 3):
        expected = factory_trial_targets()[:size]
        assert factory_trial_exposure(expected) == expected
        assert [p.model_dump() for p in _parse_release_hil_targets(("zone-lite-2.6.22", "2.6.22"), expected)] == expected


@pytest.mark.parametrize("change", ["missing", "unknown", "all17", "generation_bool", "swapped", "wrong_digest",
                                    "no_gate", "extra", "schema_bool", "baseline", "generic_image"])
def test_factory_contract_is_exact(change):
    value = manifest()
    if change == "missing":
        del value["factory_trial"]
    elif change == "unknown":
        value["factory_trial"]["targets"] = []
    elif change == "all17":
        value["factory_trial"]["targets"] = signed_hil_targets()
    elif change == "generation_bool":
        value["factory_trial"]["targets"][1]["onboarding_generation"] = True
    elif change == "swapped":
        value["factory_trial"]["targets"].reverse()
    elif change == "wrong_digest":
        value["factory_trial"]["targets"][0]["factory_application_sha256"] = "1" * 64
    elif change == "no_gate":
        value["factory_trial"]["require_3fl_writer_hil"] = False
    elif change == "extra":
        value["factory_trial"]["operator_override"] = True
    elif change == "schema_bool":
        value["factory_trial"]["schema_version"] = True
    elif change == "baseline":
        value["minimum_bootstrap_version"] = "2.4.12"
    else:
        value["queue_storage"]["allowed_bootstrap_images"] = {"2.5.2": "a" * 64}
    with pytest.raises(ValueError):
        validate_bridge_manifest(value)


def test_returned_signing_policy_does_not_mutate_canonical_pins():
    first = factory_trial_signing_contract()
    first["targets"][0]["mac"] = "changed"
    assert validate_factory_trial_contract(factory_trial_signing_contract())


@pytest.mark.parametrize("count", [1, 2, 3])
def test_factory_signing_helper_accepts_bounded_stdin_prefix(count, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    program = str(Path(__file__).resolve().parents[2] / "scripts/build_zkt_factory_contract.py")
    payload = json.dumps(factory_trial_targets()[:count]).encode() + b"\r\n"
    for extra in ([], ["--manifest", str(path)]):
        result = subprocess.run([sys.executable, program, "--exposure-stdin", *extra],
                                input=payload, capture_output=True, timeout=15)
        assert result.returncode == 0 and not result.stderr
        if not extra:
            assert json.loads(result.stdout) == factory_trial_signing_contract()
        else:
            assert not result.stdout


@pytest.mark.parametrize("payload", [b"", b"[]", b"{}", b"\xff", b" " * 4097,
    json.dumps(factory_trial_targets()[0]).encode(),
    json.dumps(factory_trial_targets()).replace('"', '').encode(),
    json.dumps(list(reversed(factory_trial_targets()))).encode(),
    json.dumps(signed_hil_targets()[:1]).encode()])
def test_factory_signing_helper_rejects_invalid_stdin_without_policy_output(payload):
    program = str(Path(__file__).resolve().parents[2] / "scripts/build_zkt_factory_contract.py")
    result = subprocess.run([sys.executable, program, "--exposure-stdin"], input=payload,
                            capture_output=True, timeout=15)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr == b"Factory trial policy or exact exposure is invalid.\n"


@pytest.mark.parametrize("targets", [[], signed_hil_targets()[:1], signed_hil_targets(),
    factory_trial_targets()[1:], list(reversed(factory_trial_targets())), factory_trial_targets() * 2])
def test_exposure_is_only_exact_factory_prefix(targets):
    with pytest.raises(ValueError):
        factory_trial_exposure(targets)


def test_factory_implementation_marker_and_descriptor_are_required():
    image = bytearray(112)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:54] = b"2.6.22"
    image[80:89] = b"zone_lite"
    image = bytes(image) + bridge_marker("2.6.22").encode() + b"\0"
    validate_bridge_image(image, "2.6.22")
    for changed in (image.replace(b":FACTORY_TRIAL=1", b""), image.replace(b"2.6.22", b"2.6.21"),
                    image + bridge_marker("2.6.22").encode() + b"\0"):
        with pytest.raises(ValueError):
            validate_bridge_image(changed, "2.6.22")


def test_old_bridge_does_not_gain_factory_exception():
    old = deepcopy(manifest())
    old.update(version="2.6.21", release_id="zone-lite-2.6.21", minimum_bootstrap_version="2.4.12",
               queue_storage=bridge_contract("2.6.21"))
    with pytest.raises(ValueError):
        validate_bridge_manifest(old)
    del old["factory_trial"]
    validate_bridge_manifest(old)


@pytest.mark.parametrize("change", [None, "no_marker", "three_fl", "all17", "bad_signature", "no_factory_policy"])
def test_real_catalog_signature_validation_preserves_exact_factory_quarantine(tmp_path, monkeypatch, change):
    import base64
    import hashlib
    import json
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from zk_add.models import Base
    from zk_add.ota import FirmwareRelease, sync_release_store
    from zk_add.settings import settings
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", base64.b64encode(public).decode())
    monkeypatch.setattr(settings, "firmware_store_path", str(tmp_path))
    monkeypatch.setattr(settings, "firmware_hil_enabled", True)
    directory = tmp_path / "2.6.22"
    directory.mkdir()
    image = bytearray(112)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:54], image[80:89] = b"2.6.22", b"zone_lite"
    image = bytes(image) + bridge_marker("2.6.22").encode() + b"\0"
    value = {**manifest(), "schema_version": 2, "git_sha": "a" * 40,
        "image_name": "firmware.bin", "image_sha256": hashlib.sha256(image).hexdigest(),
        "application_sha256": "b" * 64, "image_size": len(image),
        "partition_layout": "zone-lite-ota-v1", "signing_key_id": "isolated-factory-signing-key"}
    if change == "no_factory_policy":
        del value["factory_trial"]
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    (directory / "manifest.json").write_bytes(canonical)
    (directory / "firmware.bin").write_bytes(image)
    (directory / "manifest.sig").write_text(base64.b64encode(b"wrong" if change == "bad_signature" else signature).decode())
    if change != "no_marker":
        targets = signed_hil_targets()[:1] if change == "three_fl" else signed_hil_targets() if change == "all17" else factory_trial_targets()[:1]
        (directory / ".hil-only.json").write_text(json.dumps({"schema_version": 2, "targets": targets,
            **{key: value[key] for key in ("git_sha", "application_sha256", "image_sha256")}}))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            if change:
                with pytest.raises((ValueError, RuntimeError, InvalidSignature)):
                    sync_release_store(session)
                assert session.scalar(select(FirmwareRelease)) is None
            else:
                sync_release_store(session)
                session.commit()
                sync_release_store(session)
                release = session.scalar(select(FirmwareRelease))
                assert release.state == "HIL_ONLY"
                assert release.manifest["hil_targets"] == signed_hil_targets()
                assert release.manifest["_hil_targets"] == factory_trial_targets()[:1]
                assert release.manifest["factory_trial"] == factory_trial_signing_contract()
    finally:
        engine.dispose()
