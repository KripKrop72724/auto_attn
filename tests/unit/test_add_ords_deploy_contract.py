from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_add_deploy_uses_environment_scoped_ords_secrets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "add-deploy.yml").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text(encoding="utf-8")

    assert "ADD_DEPLOY_ORDS_USERNAME: ${{ secrets.ADD_ORDS_USERNAME }}" in workflow
    assert "ADD_DEPLOY_ORDS_PASSWORD: ${{ secrets.ADD_ORDS_PASSWORD }}" in workflow
    assert (
        "ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_USERNAME: "
        "${{ secrets.ADD_ATTENDANCE_REPAIR_ORDS_USERNAME }}" in workflow
    )
    assert (
        "ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_PASSWORD: "
        "${{ secrets.ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD }}" in workflow
    )
    assert '$environment["ADD_ORDS_USERNAME"] = $env:ADD_DEPLOY_ORDS_USERNAME' in deploy
    assert '$environment["ADD_ORDS_PASSWORD"] = $env:ADD_DEPLOY_ORDS_PASSWORD' in deploy
    assert (
        '$environment["ADD_ATTENDANCE_REPAIR_ORDS_USERNAME"] = '
        "$env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_USERNAME" in deploy
    )
    assert (
        '$environment["ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"] = '
        "$env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_PASSWORD" in deploy
    )


def test_add_deploy_requires_authenticated_ords_probes() -> None:
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text(encoding="utf-8")

    assert "function Assert-OrdsAuthentication" in deploy
    assert "function Assert-OrdsContainerAuthentication" in deploy
    assert '"/raw-captures/check"' in deploy
    assert "Assert-OrdsAuthentication `" in deploy
    assert "Assert-OrdsContainerAuthentication" in deploy
    assert "function Assert-OrdsRepairAuthentication" in deploy
    assert '"/raw-captures/identity-repairs/capabilities"' in deploy
    assert "ADD_ATTENDANCE_REPAIR_ORDS_USERNAME" in deploy
    assert "ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD" in deploy
    assert "must not reuse the connector/fleet Oracle credential" in deploy
    assert "ORDS_AUTH_OK" in deploy
    assert '-notcontains "ORDS_AUTH_OK"' in deploy


def test_add_deploy_only_tolerates_ords_outage_with_unchanged_credentials() -> None:
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text(encoding="utf-8")

    assert '$status -in @(502, 503, 504)' in deploy
    assert 'exc.code in (502, 503, 504)' in deploy
    assert 'ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] -ne "true"' in deploy
    for setting in ("ADD_ORDS_BASE_URL", "ADD_ORDS_USERNAME", "ADD_ORDS_PASSWORD"):
        assert (
            f'$priorEnvironment["{setting}"] -ceq $environment["{setting}"]'
            in deploy
        )
    assert '-AllowUnavailable:$allowDegradedOrds' in deploy
