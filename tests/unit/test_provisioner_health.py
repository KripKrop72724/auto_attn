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
