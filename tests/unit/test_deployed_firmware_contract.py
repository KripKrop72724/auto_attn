"""A signed future package must not reach a backend that cannot admit it."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from zk_add import storage_contract
from zk_add.settings import settings
from zk_add.zkt_bridge_contract import bridge_contract, signed_hil_targets

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_deployed_firmware_contract.py"
spec = importlib.util.spec_from_file_location("deployed_firmware_contract", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def package(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", public)
    manifest = dict(version="2.6.16", release_id="zone-lite-2.6.16", firmware_family="zkt",
        project_name="zone_lite", release_channel="EXPERIMENTAL_HIL_ONLY",
        minimum_bootstrap_version="2.4.12", hil_targets=signed_hil_targets(),
        queue_storage=bridge_contract())

    def write(value=manifest):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        signature = key.sign(raw, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
            salt_length=32), hashes.SHA256())
        (tmp_path / "manifest.json").write_bytes(raw)
        (tmp_path / "manifest.sig").write_bytes(base64.b64encode(signature))
        return [tmp_path / "manifest.json", tmp_path / "manifest.sig", hashlib.sha256(raw).hexdigest()]

    return manifest, write, public


def test_actual_installed_signature_and_contract_with_cli(package):
    _, write, public = package
    args = write()
    expected = "ADD_FIRMWARE_CONTRACT_ACCEPTED:" + args[2]
    assert checker.check(*args) == expected
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
        env={**os.environ, "ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64": public},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == expected and result.stderr == ""


@pytest.mark.parametrize("fault", ["changed-bytes", "bad-signature", "wrong-trust-root", "oversize-manifest",
    "oversize-signature", "malformed", "array", "writer", "wrong-family", "missing-contract", "bad-identity"])
def test_refusal_has_no_arbitrary_package_or_configuration_output(package, monkeypatch, capsys, fault):
    manifest, write, _ = package
    if fault == "writer":
        manifest["version"] = "2.7.0"
    elif fault == "wrong-family":
        manifest["firmware_family"] = "unsupported"
    elif fault == "missing-contract":
        del manifest["queue_storage"]
    args = write([] if fault == "array" else manifest)
    if fault in {"changed-bytes", "oversize-manifest", "malformed"}:
        args[0].write_bytes(b"sensitive-package-text" * 4000 if fault == "oversize-manifest" else b"not-json")
        if fault != "changed-bytes":
            args[2] = hashlib.sha256(args[0].read_bytes()).hexdigest()
    if fault in {"bad-signature", "oversize-signature"}:
        args[1].write_bytes(b"sensitive-signature" * (1000 if fault == "oversize-signature" else 1))
    if fault == "wrong-trust-root":
        monkeypatch.setattr(settings, "firmware_signing_public_key_pem_b64", "unavailable-config-value")
    if fault == "bad-identity":
        args[2] = "BAD"
    assert checker.main(list(map(str, args))) == 1
    assert capsys.readouterr() == ("ADD_FIRMWARE_CONTRACT_REJECTED\n", "")


def test_uses_the_deployed_validator_and_refuses_old_backend(package, monkeypatch, capsys):
    _, write, _ = package
    args = write()
    checked = []

    def old_validator(manifest, version):
        checked.append(version)
        raise ValueError("older installed backend has no bridge support")

    monkeypatch.setattr(storage_contract, "validate_storage_contract", old_validator)
    assert checker.main(list(map(str, args))) == 1
    assert checked == ["2.6.16"]
    assert capsys.readouterr().out == "ADD_FIRMWARE_CONTRACT_REJECTED\n"
