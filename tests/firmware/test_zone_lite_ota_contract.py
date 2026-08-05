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


def test_ota_restart_waits_for_an_atomic_zkt_safepoint() -> None:
    ota = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    runtime = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")

    update = ota[
        ota.index("static bool perform_update(void)") :
        ota.index("static bool confirm_or_report_rollback(void)")
    ]
    assert "while (!add_connector_claim_ota_restart())" in update
    assert 'report_state("READY_TO_BOOT", "WAITING_FOR_ZKT_SAFEPOINT")' in update
    assert update.index("wait_for_zkt_safepoint();") < update.index("esp_restart();")
    assert "static bool s_ota_restart_claimed;" in connector
    assert 'strlcpy(s_activity, "OTA_RESTART"' in connector
    assert 'strcmp(s_activity, "LIVE_CAPTURE") == 0' in connector
    assert 'strcmp(s_zkt.connection_state, "ONLINE") == 0' in connector
    assert "add_connector_begin_pending_command_activity()" in runtime
    assert runtime.count("add_connector_begin_exclusive_activity(") >= 5


def test_ota_hash_uses_the_esp_application_digest_contract() -> None:
    text = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "esp_partition_get_sha256(target, digest)" in text
    assert "hash_partition_bytes" not in text


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
    assert "capability_reported && !s_busy" in text
    assert "capability_reported = report_capability();" in text


def test_ota_success_is_durable_and_same_version_is_never_downloaded_again() -> None:
    text = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "acknowledge_pending_success" in text
    assert 'strcmp(s_journal.state, "RECONCILING") != 0' in text
    assert 'if (report_state("SUCCEEDED", NULL)) {' in text
    assert "ADD did not acknowledge OTA success; retaining journal for retry" in text
    assert 'strcmp(running->version, version->valuestring) == 0' in text
    assert '(void)perform_update();' in text


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
    assert "docker run --rm" in candidate
    assert "espressif/idf:v5.5.3" in candidate
    assert "container: espressif/idf:v5.5.3" not in candidate

    assert "hil_run_id" in release
    assert "firmware-production" in release
    assert "deploy/add/promote-firmware.ps1" in release
    assert "deploy/add/sign-firmware-release.ps1" not in release

    assert "ZONE_LITE_SECURE_BOOT_ACTIVE_KEY_B64" not in candidate
    assert "ZONE_LITE_SECURE_BOOT_ACTIVE_KEY_B64" not in release

def test_release_signer_uses_a_docker_mountable_ephemeral_workspace() -> None:
    signer = (ROOT / "deploy" / "add" / "sign-firmware-release.ps1").read_text(encoding="utf-8")
    assert "$env:RUNNER_TEMP" in signer
    assert "$env:GITHUB_WORKSPACE" in signer
    assert "Join-Path $env:TEMP" not in signer
