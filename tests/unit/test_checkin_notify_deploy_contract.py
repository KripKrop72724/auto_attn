"""The check-in notification key is an optional add-production secret."""

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_the_key_is_wired_like_the_ords_password_and_stays_optional() -> None:
    workflow_text = (ROOT / ".github/workflows/add-deploy.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/add/deploy.ps1").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = [job for job in workflow["jobs"].values() if job.get("environment") == "add-production"]
    wired = [job for job in jobs if "ADD_DEPLOY_ORDS_PASSWORD" in job.get("env", {})]
    assert wired, "the production deploy job must exist"
    for job in wired:
        assert job["env"]["ADD_DEPLOY_CHECKIN_NOTIFY_API_KEY"] == "${{ secrets.ADD_CHECKIN_NOTIFY_API_KEY }}"
        assert job["env"]["ADD_DEPLOY_CHECKIN_NOTIFY_MODE"] == "${{ vars.ADD_CHECKIN_NOTIFY_MODE }}"
    assert (
        '$environment["ADD_CHECKIN_NOTIFY_API_KEY"] = $env:ADD_DEPLOY_CHECKIN_NOTIFY_API_KEY' in deploy
    )
    assert 'if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_CHECKIN_NOTIFY_API_KEY))' in deploy
    # Absent key: the deployment still succeeds (the key is never required).
    required = re.search(r"\$required = @\((.*?)\)", deploy, re.DOTALL).group(1)
    assert "CHECKIN_NOTIFY" not in required
    # Failure diagnostics redact the key like every other secret.
    redacted = re.search(
        r'foreach \(\$name in @\(\s*"ADD_POSTGRES_PASSWORD"(.*?)\)\) \{', deploy, re.DOTALL
    ).group(1)
    assert '"ADD_CHECKIN_NOTIFY_API_KEY"' in redacted
    assert '-cnotin @("OFF", "PILOT", "ALL")' in deploy
    diagnostics = (ROOT / "deploy/add/collect-diagnostics.ps1").read_text(encoding="utf-8")
    pattern = re.search(r"\$entry\.Key -match '\(\?i\)\(([A-Z_|]+)\)'", diagnostics).group(1)
    assert re.search(pattern, "ADD_CHECKIN_NOTIFY_API_KEY")


def test_the_api_service_receives_the_protected_environment_file() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.add.yml").read_text(encoding="utf-8"))
    # deploy.ps1 writes every setting, the key included, into .env.add.
    assert compose["services"]["add-api"]["env_file"] == ".env.add"
