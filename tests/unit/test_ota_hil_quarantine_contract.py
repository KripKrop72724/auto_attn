from pathlib import Path

from zk_add.settings import settings


ROOT = Path(__file__).resolve().parents[2]


def test_hil_quarantine_is_disabled_by_default() -> None:
    assert settings.firmware_hil_enabled is False
    assert settings.firmware_hil_target_mac is None


def test_deployment_can_explicitly_switch_from_hil_to_national_ota() -> None:
    workflow = (ROOT / ".github" / "workflows" / "add-deploy.yml").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text(encoding="utf-8")
    assert (
        "ADD_DEPLOY_FIRMWARE_OTA_ENABLED: "
        "${{ vars.ADD_FIRMWARE_OTA_ENABLED }}" in workflow
    )
    assert (
        '$environment["ADD_FIRMWARE_OTA_ENABLED"] = '
        "$env:ADD_DEPLOY_FIRMWARE_OTA_ENABLED" in deploy
    )
    assert (
        "ADD_DEPLOY_RECONCILIATION_ENABLED: "
        "${{ vars.ADD_RECONCILIATION_ENABLED }}" in workflow
    )
    assert (
        '$environment["ADD_RECONCILIATION_ENABLED"] = '
        "$env:ADD_DEPLOY_RECONCILIATION_ENABLED" in deploy
    )


def test_add_deployment_uses_the_validated_internal_ords_route() -> None:
    deploy = (ROOT / "deploy" / "add" / "deploy.ps1").read_text(encoding="utf-8")
    expected = (
        "https://local.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
    )
    assert f'$environment["ADD_ORDS_BASE_URL"] = "{expected}"' in deploy
    assert (
        f'if ($environment["ADD_ORDS_BASE_URL"] -ne "{expected}")'
        in deploy
    )


def test_publication_requires_exact_hil_target_and_promotion_preserves_bytes() -> None:
    publish = (ROOT / "deploy/add/publish-firmware.ps1").read_text(encoding="utf-8")
    promote = (ROOT / "deploy/add/promote-firmware.ps1").read_text(encoding="utf-8")
    assert "HIL_ONLY publication requires one exact ESP MAC" in publish
    assert "already exists outside HIL quarantine" in publish
    assert "Firmware image changed after HIL publication" in promote
    assert "$outputMarkerPath = Join-Path $output '.hil-only.json'" in promote
    assert "Remove-Item -LiteralPath $outputMarkerPath -Force" in promote
    assert "Remove-Item -LiteralPath $markerPath -Force" in promote


def test_production_canary_promotion_fails_closed_on_exact_runtime_evidence() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "firmware-canary-promote.yml"
    ).read_text(encoding="utf-8")
    for required in (
        'git merge-base --is-ancestor "$sha" origin/main',
        "runs-on: [self-hosted, Windows, X64]",
        "ADD_BASE_URL: http://127.0.0.1:8096",
        "'(?:^|[,\\s])add_admin=([^;,\\s]+)'",
        "$adminCookie.Secure = $false",
        "$session.Cookies.Add([Uri]$env:ADD_BASE_URL, $adminCookie)",
        "-WebSession $session",
        "/api/v1/auth/logout",
        "'X-CSRF-Token' = [string]$login.csrf_token",
        "$_.state -eq 'HIL_ONLY'",
        "[int]$_.counts.SUCCEEDED -eq 1",
        "[int]$_.legacy_skipped -eq 0",
        "CANARY_ACCEPTED:",
        "$reportedFirmwareVersion = ([string]$row.firmware_version).Trim()",
        "$reportedFirmwareVersion.StartsWith('zone-lite-')",
        "$reportedFirmwareVersion -eq $env:EXPECTED_VERSION",
        "$row.ota_state -eq 'OTA_READY'",
        "@('ONLINE', 'LIVE_CAPTURE') -contains [string]$row.current_activity",
        "$row.zkt.connection_state -eq 'ONLINE'",
        "$row.zkt.certification_state -eq 'CERTIFIED'",
        "$row.zkt.capabilities.history_stream_v1",
        "$row.zkt.capabilities.history_range_resume_verified",
        "[int]$row.zkt.attendance_count -ge [int]$env:MINIMUM_TERMINAL_ATTENDANCE",
        "/logs?limit=1000",
        "IDENTITY_CATALOG_APPLIED",
        "ORACLE_RECONCILE_DELEGATED_TO_ADD",
        "TRUTH_IDENTITY_INCOMPLETE",
        "FULL_RECONCILE_FAILED",
        "SOURCE_COMMITTED_BOUNDARY_DIVERGED",
        "ADD_SOURCE_COVERAGE_INVALIDATED",
        "/api/v1/attendance?device_id=$($row.connector_id)&limit=500",
        "$attendanceCount -ne [int]$row.zkt.attendance_count",
        "$unsafeDeliveryCount -ne 0",
        "$deliveryState -eq 'BLOCKED_IDENTITY'",
        "$deliveryState -eq 'QUARANTINED_IDENTITY_REUSE'",
        "all resolvable rows are Oracle-confirmed",
        "'ACKED', 'ACKED_CHECK'",
        "deploy/add/promote-firmware.ps1",
        "Test-Path -LiteralPath 'promoted-release/.hil-only.json'",
    ):
        assert required in workflow


def test_release_manifest_contract_matches_add_verifier() -> None:
    signer = (ROOT / "deploy/add/sign-firmware-release.ps1").read_text(encoding="utf-8")
    ota = (ROOT / "apps/add_backend/zk_add/ota.py").read_text(encoding="utf-8")
    for field in (
        "release_id",
        "git_sha",
        "image_name",
        "image_sha256",
        "application_sha256",
        "image_size",
    ):
        assert f"{field} =" in signer
        assert f'manifest["{field}"]' in ota or f'manifest.get("{field}"' in ota
    assert "rsa_pss_saltlen:32" in signer
    assert "[Convert]::ToBase64String" in signer
    assert '"image_sha256": application_digest' in ota
    assert '"artifact_sha256": release.image_sha256' in ota


def test_hil_assignment_is_fail_closed_to_one_hardware_id() -> None:
    ota = (ROOT / "apps/add_backend/zk_add/ota.py").read_text(encoding="utf-8")
    assert 'release.state == "HIL_ONLY"' in ota
    assert "row.hardware_id.lower() == hil_target_mac" in ota
    assert "connector.hardware_id.lower() != target" in ota
    assert "HIL campaign requires exactly one eligible connector" in ota
