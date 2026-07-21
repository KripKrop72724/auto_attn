from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"


def test_partition_table_has_factory_two_ota_slots_and_preserved_storage() -> None:
    text = (FIRMWARE / "partitions.csv").read_text(encoding="utf-8")
    assert "factory" in text
    assert "ota_0" in text
    assert "ota_1" in text
    assert "storage" in text
    assert "0x280000" in text
    assert "0x800000" in text


def test_secure_boot_and_rollback_are_build_contracts() -> None:
    text = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in text
    assert "CONFIG_SECURE_BOOT_V2_ENABLED=y" in text
    assert "CONFIG_SECURE_BOOT_BUILD_SIGNED_BINARIES=n" in text
    assert "CONFIG_PARTITION_TABLE_OFFSET=0x10000" in text


def test_ota_manager_uses_safe_application_ota_and_first_boot_confirmation() -> None:
    text = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "esp_https_ota" in text
    assert "esp_ota_mark_app_valid_cancel_rollback" in text
    assert "esp_https_ota_finish" in text
    assert "esp_ota_mark_app_invalid_rollback_and_reboot" in text


def test_release_workflow_requires_hil_and_protected_environments() -> None:
    text = (ROOT / ".github" / "workflows" / "firmware-release.yml").read_text(encoding="utf-8")
    assert "hil_run_id" in text
    assert "firmware-signing" in text
    assert "firmware-production" in text
    assert "[self-hosted, Windows, X64]" in text
    assert "deploy/add/sign-firmware-release.ps1" in text
    assert "ZONE_LITE_SECURE_BOOT_ACTIVE_KEY_B64" not in text
