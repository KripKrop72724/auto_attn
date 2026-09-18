from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]


def load_provisioner_module():
    tools_path = ROOT / "firmware" / "zone_lite" / "tools"
    sys.path.insert(0, str(tools_path))
    try:
        spec = importlib.util.spec_from_file_location(
            "add_provisioner_health_test_app",
            ROOT / "apps" / "add_provisioner" / "app.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(tools_path))


def test_liveness_does_not_require_provisioning_configuration(monkeypatch):
    for name in (
        "ADD_FLEET_ROOT_SECRET",
        "ADD_ORDS_BASE_URL",
        "ADD_ORDS_USERNAME",
        "ADD_ORDS_PASSWORD",
        "ADD_PROVISIONING_INTERNAL_TOKEN",
        "ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64",
    ):
        monkeypatch.delenv(name, raising=False)

    provisioner = load_provisioner_module()
    client = TestClient(provisioner.app)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503


def test_hikvision_device_configuration_reaches_encrypted_nvs_builder():
    provisioner = load_provisioner_module()
    from types import SimpleNamespace
    configuration = {
        "firmware_family": "hikvision", "wifi_ssid": "test-lan",
        "wifi_password": "test-only-network-password", "communication_key": None,
        "device_id": "HIK-TEST", "zone_id": "TEST", "zone_name": "Test zone",
        "hik_host": "192.168.10.20", "hik_port": 80, "hik_transport": "http_digest",
        "hik_username": "test-user", "hik_password": "test-only-terminal-password",
        "hik_expected_serial": "terminal-test", "hik_profile": "test-profile",
        "hik_source_epoch": "test-epoch", "hik_ca_pem": "",
    }
    request = provisioner._build_request(SimpleNamespace(
        configuration=configuration, session_id="test-session-123",
        hardware_mac="00:11:22:33:44:55", recipient_public_key="test-public-key",
    ))
    assert request["firmware_family"] == "hikvision"
    for key, value in configuration.items():
        if key.startswith("hik_"):
            assert request[key] == value
    assert request["zkt_comm_key"] == 0
