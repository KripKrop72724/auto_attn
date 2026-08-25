from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"
RUNTIME = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
CONNECTOR = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
CONFIG = (FIRMWARE / "main" / "zone_config.c").read_text(encoding="utf-8")


def test_250_advertises_esp_management_but_fail_closes_terminal_writes() -> None:
    assert "project(zone_lite VERSION 2.5.0)" in (FIRMWARE / "CMakeLists.txt").read_text()
    assert 'cJSON_AddBoolToObject(payload, "comm_key_management", true)' in CONNECTOR
    assert 'cJSON_AddBoolToObject(zkt_json, "comm_key_write_v1", false)' in CONNECTOR
    assert '"COMM_KEY_TERMINAL_WRITE_UNSUPPORTED"' in RUNTIME


def test_comm_key_command_is_isolated_and_bound_to_aead_context() -> None:
    assert 'strcmp(command->command_type, "APPLY_CONFIG") == 0' in CONNECTOR
    assert "s_config_commands" in CONNECTOR
    assert "mbedtls_hkdf(" in RUNTIME
    assert "mbedtls_gcm_auth_decrypt(" in RUNTIME
    assert '"zone-lite-config-v1\\n%s\\n%s\\n%s\\n%lu\\n%s\\n%s\\n%lld"' in RUNTIME
    assert "mbedtls_platform_zeroize(key" in RUNTIME
    assert "mbedtls_platform_zeroize(plaintext" in RUNTIME
    assert "mbedtls_platform_zeroize(&candidate_key" in RUNTIME


def test_comm_key_is_committed_only_after_exact_serial_authentication() -> None:
    authenticate = RUNTIME.index("find_zkt_with_comm_key(", RUNTIME.index("comm_key_manager_task"))
    serial_check = RUNTIME.index("COMM_KEY_TERMINAL_SERIAL_MISMATCH", authenticate)
    persist = RUNTIME.index("zone_config_save_zkt_comm_key(", serial_check)
    success = RUNTIME.index('command.command_id, "SUCCEEDED"', persist)
    assert authenticate < serial_check < persist < success
    assert 'strcmp(serial, expected_serial) == 0' in RUNTIME


def test_encrypted_nvs_commit_has_recoverable_operation_journal() -> None:
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_NVS_ENCRYPTION=y" in defaults
    assert "CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y" in defaults
    assert "CONFIG_MBEDTLS_HKDF_C=y" in defaults
    for key in (
        '"zkt_key_pending"',
        '"zkt_rev_pending"',
        '"zkt_op_pending"',
        '"zkt_key_phase"',
        '"zkt_op_applied"',
    ):
        assert key in CONFIG
    assert CONFIG.index('nvs_set_u8(handle, "zkt_key_phase", 1)') < CONFIG.index(
        'nvs_set_u32(handle, "zkt_key", comm_key)'
    )
    assert "recover_pending_comm_key" in CONFIG
    assert "zkt_comm_key_operation_id" in CONFIG


def test_duplicate_after_reset_reauthenticates_before_reporting_success() -> None:
    manager = RUNTIME[RUNTIME.index("static void comm_key_manager_task") :]
    assert "already_applied" in manager
    assert "zkt_comm_key_operation_id" in manager
    assert manager.index("already_applied") < manager.index("find_zkt_with_comm_key(")
    assert manager.index("find_zkt_with_comm_key(") < manager.index("authentication_verified")
