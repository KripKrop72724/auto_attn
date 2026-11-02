"""Provisioner POST /internal/v2/nvs-packages: the installer's NVS-only build.

validate_request and nvs_rows run for real; only the builder subprocess is
replaced, so these tests need no ESP-IDF, fleet root or ORDS credentials.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "zone_lite" / "tools"
PATH = "/internal/v2/nvs-packages"
TOKEN = "test-only-internal-token-0123456789abcdef"
WIFI_PASSWORD = "test-only-wifi-passphrase"
HIK_PASSWORD = "test-only-terminal-secret"
COMM_KEY = 246813579
SECRETS = (TOKEN, WIFI_PASSWORD, HIK_PASSWORD, str(COMM_KEY))
RECIPIENT = base64.b64encode(X25519PrivateKey.generate().public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def load_provisioner_module():
    sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location(
            "add_provisioner_nvs_v2_test_app", ROOT / "apps" / "add_provisioner" / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TOOLS))


@pytest.fixture
def provisioner(monkeypatch, tmp_path):
    monkeypatch.setenv("ADD_PROVISIONING_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("ADD_PROVISIONING_ARTIFACT_PATH", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ADD_PROVISIONING_FACTORY_STORE_PATH", str(tmp_path / "no-factory-store"))
    module = load_provisioner_module()
    builds = []

    def fake_builder(request: dict, output: Path) -> None:
        builds.append(SimpleNamespace(request=dict(request), output=output))
        assert output.is_dir() and not any(output.iterdir())
        (output / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "aad": {"request_id": request["request_id"]},
            "ciphertext_sha256": "a" * 64, "hmac_key": {"ciphertext_sha256": "b" * 64},
        }), encoding="utf-8")
        (output / "provision.bin.enc").write_bytes(b"nvs-ciphertext")
        (output / "hmac-key.bin.enc").write_bytes(b"key-ciphertext")

    monkeypatch.setattr(module, "_run_builder", fake_builder)
    return SimpleNamespace(module=module, client=TestClient(module.app), builds=builds,
                           artifacts=tmp_path / "artifacts")


def zkt(**overrides) -> dict:
    values = {
        "firmware_family": "zkt", "zone_id": "ZONE-TEST-01", "zone_name": "ZONE-TEST-01",
        "device_id": "1", "wifi_ssid": "RE7", "wifi_password": WIFI_PASSWORD,
        "zkt_port": 4370, "zkt_comm_key": COMM_KEY, "zkt_preferred_ip": "0.0.0.0",
    }
    values.update(overrides)
    return values


def hik(**overrides) -> dict:
    values = {
        "firmware_family": "hikvision", "zone_id": "LF-ZONE-JHELUM-01",
        "zone_name": "LF-ZONE-JHELUM-01", "device_id": "1", "wifi_ssid": "RE7",
        "wifi_password": WIFI_PASSWORD, "zkt_port": 4370, "hik_host": "192.168.10.20",
        "hik_port": 80, "hik_transport": "http_digest", "hik_username": "admin",
        "hik_password": HIK_PASSWORD, "hik_expected_serial": "DS-TEST-SERIAL-0001",
        "hik_profile": "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1",
        "hik_source_epoch": "lf-zone-jhelum-01-20261101", "hik_ca_pem": "",
    }
    values.update(overrides)
    return values


def request_body(configuration: dict, **overrides) -> dict:
    body = {"request_id": "installer-test-0001", "target_mac": "AC:27:6E:A4:3D:30",
            "recipient_public_key": RECIPIENT, "configuration": configuration}
    body.update(overrides)
    return body


def post(provisioner, body, token: str | None = TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return provisioner.client.post(PATH, json=body, headers=headers)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_the_internal_token_is_required(provisioner, token):
    response = post(provisioner, request_body(zkt()), token=token)
    assert response.status_code == 401
    assert provisioner.builds == []


def test_a_zkt_package_needs_no_factory_bundle_and_writes_no_artifact(provisioner):
    response = post(provisioner, request_body(zkt()))
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"provisioning_manifest", "provisioning_ciphertext_b64", "hmac_ciphertext_b64"}
    assert base64.b64decode(body["provisioning_ciphertext_b64"]) == b"nvs-ciphertext"
    assert base64.b64decode(body["hmac_ciphertext_b64"]) == b"key-ciphertext"
    assert body["provisioning_manifest"]["aad"]["request_id"] == "installer-test-0001"
    (build,) = provisioner.builds
    request = build.request
    assert (request["request_id"], request["target_mac"], request["recipient_public_key_b64"]) == (
        "installer-test-0001", "ac:27:6e:a4:3d:30", RECIPIENT)
    assert (request["zone_device_id"], request["zone_id"], request["zone_name"]) == (
        "1", "ZONE-TEST-01", "ZONE-TEST-01")
    assert (request["zkt_port"], request["zkt_comm_key"], request["zkt_preferred_ip"]) == (
        4370, COMM_KEY, "0.0.0.0")
    assert (request["zkt_expected_serial"], request["zkt_recovery_enabled"]) == ("", False)
    assert not [key for key in request if key.startswith("hik_")]
    # The plaintext request lived only in a temporary directory, now removed.
    assert not build.output.exists() and not build.output.parent.exists()
    assert not provisioner.artifacts.exists()


def test_a_hikvision_package_forwards_the_terminal_settings(provisioner):
    configuration = hik()
    configuration.pop("zkt_port")
    response = post(provisioner, request_body(configuration))
    assert response.status_code == 201, response.text
    request = provisioner.builds[0].request
    for key, value in configuration.items():
        if key.startswith("hik_"):
            assert request[key] == value
    assert (request["zkt_comm_key"], request["zkt_preferred_ip"], request["zkt_port"]) == (
        0, "0.0.0.0", 4370)


def test_a_hikvision_certificate_may_fill_one_nvs_string(provisioner):
    # 3967 bytes is the most nvs_partition_gen (ESP-IDF 5.5.3) stores in one
    # string. One byte more is refused as the field (the table below).
    certificate = "c" * 3967
    response = post(provisioner, request_body(hik(
        hik_transport="https", hik_port=443, hik_ca_pem=certificate)))
    assert response.status_code == 201, response.text
    assert provisioner.builds[0].request["hik_ca_pem"] == certificate


def _without(values: dict, key: str) -> dict:
    return {name: value for name, value in values.items() if name != key}


@pytest.mark.parametrize(("body", "field"), [
    (request_body(zkt(zone_id="BAD ZONE")), "zone_id"),
    (request_body(zkt(zone_id="Z" * 65)), "zone_id"),
    (request_body(zkt(device_id="D" * 32)), "device_id"),
    (request_body(zkt(device_id="bad id")), "device_id"),
    (request_body(zkt(zone_name=" leading space")), "zone_name"),
    (request_body(zkt(wifi_ssid="")), "wifi_ssid"),
    (request_body(zkt(wifi_password="short-7")), "wifi_password"),
    (request_body(zkt(zkt_port="4370")), "zkt_port"),
    (request_body(zkt(zkt_port=0)), "zkt_port"),
    (request_body(zkt(zkt_comm_key=2**32)), "zkt_comm_key"),
    (request_body(_without(zkt(), "zkt_comm_key")), "zkt_comm_key"),
    (request_body(zkt(zkt_comm_key=True)), "zkt_comm_key"),
    (request_body(zkt(zkt_preferred_ip="8.8.8.8")), "zkt_preferred_ip"),
    (request_body(zkt(zkt_preferred_ip="not-an-address")), "zkt_preferred_ip"),
    (request_body(_without(zkt(), "zkt_preferred_ip")), "zkt_preferred_ip"),
    (request_body(_without(zkt(), "firmware_family")), "firmware_family"),
    (request_body(zkt(firmware_family="suprema")), "firmware_family"),
    (request_body(hik(hik_host="terminal.example")), "hik_host"),
    (request_body(hik(hik_host="8.8.8.8")), "hik_host"),
    (request_body(hik(hik_host="fe80::1")), "hik_host"),
    (request_body(hik(hik_username="u" * 64)), "hik_username"),
    (request_body(hik(hik_password="")), "hik_password"),
    (request_body(hik(hik_expected_serial="s" * 120)), "hik_expected_serial"),
    (request_body(hik(hik_source_epoch="")), "hik_source_epoch"),
    # nvs_partition_gen stores one string of at most 3967 bytes plus its NUL.
    (request_body(hik(hik_ca_pem="c" * 3968)), "hik_ca_pem"),
    (request_body(hik(hik_ca_pem="é" * 1984)), "hik_ca_pem"),
    (request_body(hik(hik_transport="telnet")), "hik_transport"),
    (request_body(hik(hik_port="80")), "hik_port"),
    (request_body(hik(zkt_comm_key=COMM_KEY)), "zkt_comm_key"),
    (request_body(zkt(), target_mac="not a mac!!!"), "target_mac"),
    (request_body(zkt(), request_id="bad id with spaces"), "request_id"),
    (request_body(zkt(), recipient_public_key=""), "recipient_public_key"),
    (_without(request_body(zkt()), "configuration"), "configuration"),
    (request_body(zkt(), unexpected="value"), "unexpected"),
])
def test_invalid_fields_are_named_without_their_values(provisioner, body, field):
    response = post(provisioner, body)
    assert response.status_code == 422, response.text
    assert response.json() == {"code": "INVALID_FIELD", "field": field}
    for value in ("terminal.example", "not-an-address", "not a mac!!!", "bad id with spaces", *SECRETS):
        assert value not in response.text
    assert provisioner.builds == []


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, ["python", "build_provisioning_package.py"]),
    KeyError("ADD_FLEET_ROOT_SECRET"),
    RuntimeError("Generated NVS partition has the wrong size"),
])
def test_a_builder_failure_is_build_failed(provisioner, monkeypatch, caplog, failure):
    def failing_builder(_request, _output):
        raise failure

    monkeypatch.setattr(provisioner.module, "_run_builder", failing_builder)
    caplog.set_level(logging.DEBUG)
    response = post(provisioner, request_body(hik()))
    assert response.status_code == 500
    assert response.json() == {"code": "BUILD_FAILED"}
    for secret in SECRETS:
        assert secret not in caplog.text
    assert "request_id=installer-test-0001" in caplog.text


def test_an_unrecognised_validation_error_is_build_failed(provisioner, monkeypatch):
    def refuse(_values):
        raise ValueError(f"something unexpected about {WIFI_PASSWORD}")

    monkeypatch.setattr(provisioner.module, "validate_request", refuse)
    response = post(provisioner, request_body(zkt()))
    assert (response.status_code, response.json()) == (500, {"code": "BUILD_FAILED"})
    assert WIFI_PASSWORD not in response.text


def test_the_builder_gets_the_fleet_root_and_no_other(monkeypatch, tmp_path):
    # ADD refuses resume and settings-only packages for boards that may hold
    # another root's key because of this (installer_api.settings_change_refusal).
    # If the builder ever takes a per-board root, revisit that rule.
    for name in ("ADD_FLEET_ROOT_SECRET", "ADD_ORDS_BASE_URL", "ADD_ORDS_USERNAME",
                 "ADD_ORDS_PASSWORD", "ADD_PII_LOOKUP_KEY", "ZONE_LITE_NVS_HMAC_ROOT_SECRET"):
        monkeypatch.setenv(name, f"test-only-{name.lower()}")
    module = load_provisioner_module()
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    module._run_builder({"request_id": "installer-test-0001"}, tmp_path / "package")
    (call,) = calls
    assert set(call["env"]) == {"PATH", "PYTHONPATH", "ADD_FLEET_ROOT_SECRET", "ADD_ORDS_BASE_URL",
                                "ADD_ORDS_USERNAME", "ADD_ORDS_PASSWORD"}
    assert call["env"]["ADD_FLEET_ROOT_SECRET"] == "test-only-add_fleet_root_secret"


def test_the_v1_endpoint_keeps_its_contract(provisioner):
    response = provisioner.client.post("/internal/v1/packages", json={},
                                       headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail and all(set(item) == {"type", "loc", "msg"} for item in detail)
    assert provisioner.builds == []
