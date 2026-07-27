from pathlib import Path

from zk_add.settings import settings


ROOT = Path(__file__).resolve().parents[2]


def test_hil_quarantine_is_disabled_by_default() -> None:
    assert settings.firmware_hil_enabled is False
    assert settings.firmware_hil_target_mac is None


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
        ".state == \"HIL_ONLY\"",
        ".counts.SUCCEEDED == 1",
        ".legacy_skipped == 0",
        ".firmware_version == $firmware",
        ".ota_state == \"OTA_READY\"",
        ".current_activity == \"LIVE_CAPTURE\"",
        ".zkt.connection_state == \"ONLINE\"",
        ".zkt.certification_state == \"CERTIFIED\"",
        ".zkt.attendance_count >= $minimum",
        "deploy/add/promote-firmware.ps1",
        "test ! -e release/.hil-only.json",
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
