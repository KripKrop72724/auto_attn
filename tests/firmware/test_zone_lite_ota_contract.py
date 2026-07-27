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


def test_ota_http_response_buffers_do_not_consume_task_stack() -> None:
    text = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "char response_data[OTA_HTTP_RESPONSE_BYTES]" not in text
    assert text.count("calloc(1, OTA_HTTP_RESPONSE_BYTES)") == 2
    assert text.count("free(response_data)") >= 4
    assert "#define OTA_HTTP_TRANSPORT_BUFFER_BYTES 4096" in text
    assert ".buffer_size = OTA_HTTP_TRANSPORT_BUFFER_BYTES" in text


def test_ota_capability_is_retried_until_add_acknowledges_it() -> None:
    text = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "bool capability_reported = false;" in text
    assert "if (!capability_reported)" in text
    assert "capability_reported = report_capability();" in text
    assert "capability_reported && !s_busy && fetch_assignment()" in text


def test_release_workflow_requires_hil_and_protected_environments() -> None:
    candidate = (ROOT / ".github" / "workflows" / "firmware-hil-candidate.yml").read_text(
        encoding="utf-8"
    )
    release = (ROOT / ".github" / "workflows" / "firmware-release.yml").read_text(
        encoding="utf-8"
    )

    assert "firmware-signing" in candidate
    assert "firmware-production" in candidate
    assert "[self-hosted, Windows, X64]" in candidate
    assert "deploy/add/sign-firmware-release.ps1" in candidate
    assert "HIL_ONLY" in candidate

    assert "hil_run_id" in release
    assert "firmware-production" in release
    assert "deploy/add/promote-firmware.ps1" in release
    assert "deploy/add/sign-firmware-release.ps1" not in release

    assert "ZONE_LITE_SECURE_BOOT_ACTIVE_KEY_B64" not in candidate
    assert "ZONE_LITE_SECURE_BOOT_ACTIVE_KEY_B64" not in release
