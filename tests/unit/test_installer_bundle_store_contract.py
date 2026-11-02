"""The installer bundle store reaches add-api read-only, in production and on rollback."""
from pathlib import Path

import yaml

from zk_add.settings import AddSettings

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = r"C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles"


def test_only_the_api_mounts_the_store_read_only():
    compose = yaml.safe_load((ROOT / "docker-compose.add.yml").read_text(encoding="utf-8"))
    mounts = [mount for mount in compose["services"]["add-api"]["volumes"]
              if mount.get("target") == "/installer-bundles"]
    assert mounts == [{
        "type": "bind",
        "source": "${ADD_INSTALLER_BUNDLE_STORE_HOST_PATH:-./.installer-bundles}",
        "target": "/installer-bundles",
        "read_only": True,
        "bind": {"create_host_path": True},
    }]
    for name, service in compose["services"].items():
        if name != "add-api":
            assert "installer-bundles" not in str(service.get("volumes", []))
    assert AddSettings.model_fields["installer_bundle_store_path"].default == "/installer-bundles"
    # The rollback override replaces no volume list, so an older image keeps the mount.
    rollback = yaml.safe_load(
        (ROOT / "deploy/add/docker-compose.rollback.yml").read_text(encoding="utf-8")
    )
    assert "volumes" not in rollback["services"]["add-api"]


def test_deploy_passes_and_creates_the_store_like_the_companion_store():
    workflow = yaml.safe_load((ROOT / ".github/workflows/add-deploy.yml").read_text(encoding="utf-8"))
    env = workflow["jobs"]["deploy"]["env"]
    assert env["ADD_INSTALLER_BUNDLE_STORE_HOST_PATH"] == (
        "${{ vars.ADD_INSTALLER_BUNDLE_STORE_HOST_PATH || '" + DEFAULT + "' }}"
    )
    assert env["ADD_COMPANION_RELEASE_STORE_HOST_PATH"] == (
        "${{ vars.ADD_COMPANION_RELEASE_STORE_HOST_PATH }}"
    )
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    block = deploy[deploy.index("$installerBundleStore = $env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH"):]
    block = block[:block.index("$env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH = $installerBundleStore")]
    assert f'"{DEFAULT}"' in block
    assert "New-Item -ItemType Directory -Path $installerBundleStore -Force" in block
    assert '$environment["ADD_INSTALLER_BUNDLE_STORE_HOST_PATH"] = $installerBundleStore' in block
    # Written before .env.add is generated and before any compose command runs.
    assert deploy.index(block) < deploy.index("$canonicalLines = New-Object")
    assert deploy.index(block) < deploy.index('Invoke-Docker -Arguments ($compose + @("config", "--quiet"))')
