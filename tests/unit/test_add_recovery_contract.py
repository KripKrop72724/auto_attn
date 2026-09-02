from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_watchdog_entrypoint_is_crlf_safe_on_windows_checkout():
    attributes = (ROOT / ".gitattributes").read_text()
    dockerfile = (ROOT / "deploy" / "add" / "watchdog" / "Dockerfile").read_text()

    assert "*.sh text eol=lf" in attributes
    assert "sed -i 's/\\r$//' /usr/local/bin/add-self-heal" in dockerfile


def test_rollback_healthcheck_is_compatible_with_an_older_api_image():
    override = (
        ROOT / "deploy" / "add" / "docker-compose.rollback.yml"
    ).read_text()
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text()

    assert "/health/ready" in override
    assert "docker_healthcheck.py" not in override
    assert '"-f", "deploy/add/docker-compose.rollback.yml"' in deploy
    assert "$rollbackCompose + @(" in deploy


def test_production_workflow_has_data_safe_core_recovery_mode():
    workflow = (ROOT / ".github" / "workflows" / "add-deploy.yml").read_text()

    assert "recover_core:" in workflow
    assert "Start the existing core containers and verify local service" in workflow
    assert "docker start $name" in workflow
    assert "docker start $web" in workflow
