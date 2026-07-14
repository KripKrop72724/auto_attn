from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"


def load_provisioner():
    path = FIRMWARE / "tools" / "provision_zone_lite.py"
    spec = importlib.util.spec_from_file_location("zone_lite_provisioner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_per_device_hkdf_vectors_are_stable_and_domain_separated():
    provisioner = load_provisioner()
    root = "fleet-root-test-vector"
    mac = "e0:72:a1:d6:f3:28"
    assert provisioner.derive_bootstrap_secret(mac, root) == (
        "biqlubVCb_F6D1n3kdg78wFsavxEOMK3N4MHp1-fXsU="
    )
    assert provisioner.derive_nvs_hmac_key(mac, root).hex() == (
        "b9422e021421c10d07ba5f4ee05b260dff0d8cb051894d3d1b0f138b93b38092"
    )
    assert provisioner.derive_bootstrap_secret(mac, root).encode() != (
        provisioner.derive_nvs_hmac_key(mac, root)
    )


def test_secure_nvs_and_generic_image_are_mandatory_defaults():
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_NVS_ENCRYPTION=y" in defaults
    assert "CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y" in defaults
    assert "CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0" in defaults
    provisioner = (FIRMWARE / "tools" / "provision_zone_lite.py").read_text(encoding="utf-8")
    assert "--confirm-efuse-burn-for" in provisioner
    assert "--trust-existing-derived-hmac" in provisioner
    assert "TemporaryDirectory" in provisioner
    assert "validate_secure_build_config(project)" in provisioner
    sources = "\n".join(
        (FIRMWARE / "main" / name).read_text(encoding="utf-8")
        for name in ("zone_lite.c", "zone_config.c", "add_connector.c", "led_status.c")
    )
    assert '__has_include("zone_lite_config.h")' not in sources
    image_defaults = (FIRMWARE / "main" / "zone_lite_config.example.h").read_text(
        encoding="utf-8"
    )
    assert '#define ZONE_LITE_WIFI_SSID ""' in image_defaults
    assert '#define ZONE_LITE_ORDS_PASSWORD ""' in image_defaults
    assert '#define ZONE_LITE_ADD_DEVICE_TOKEN ""' in image_defaults
    runtime = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "if (!zone_config_get()->provisioned)" in runtime
    assert runtime.index("if (!zone_config_get()->provisioned)") < runtime.index("wifi_init_sta();")
    assert "nvs_flash_erase()" not in runtime


def test_provisioner_rejects_stale_insecure_effective_sdkconfig(tmp_path: Path):
    provisioner = load_provisioner()
    (tmp_path / "build" / "config").mkdir(parents=True)
    (tmp_path / "sdkconfig").write_text(
        "# CONFIG_NVS_ENCRYPTION is not set\n", encoding="utf-8"
    )
    (tmp_path / "build" / "config" / "sdkconfig.h").write_text(
        "/* insecure stale build */\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="without HMAC-backed NVS encryption"):
        provisioner.validate_secure_build_config(tmp_path)


def test_provisioner_accepts_matching_secure_effective_sdkconfig(tmp_path: Path):
    provisioner = load_provisioner()
    (tmp_path / "build" / "config").mkdir(parents=True)
    (tmp_path / "sdkconfig").write_text(
        "\n".join(provisioner.SECURE_SDKCONFIG_VALUES) + "\n", encoding="utf-8"
    )
    (tmp_path / "build" / "config" / "sdkconfig.h").write_text(
        "\n".join(provisioner.SECURE_BUILD_DEFINES) + "\n", encoding="utf-8"
    )

    provisioner.validate_secure_build_config(tmp_path)


def test_user_protocol_never_contains_attendance_clear_command():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "#define CMD_DELETE_USER 18" in source
    assert "CMD_CLEAR_ATTLOG" not in source
    assert "CMD_DELETE_USER" in source
    assert "attendance_count_before" in source
    assert "attendance_count_after" in source


def test_command_inbox_is_encrypted_bounded_recoverable_and_cancellable():
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    runtime = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "#define ADD_COMMAND_QUEUE_DEPTH 32" in connector
    assert "mbedtls_gcm_crypt_and_tag" in connector
    assert "restore_command_inbox" in connector
    assert "command_journal_append" in connector
    assert "s_running_command_id" in connector
    assert "COMMAND_ALREADY_RUNNING" in connector
    assert "command_error_is_retryable" in runtime
    assert "add_connector_command_retry" in runtime


def test_delete_persists_identity_before_zkt_mutation():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    branch = source[source.index('strcmp(command.command_type, "DELETE_USER") == 0') :]
    persist_at = branch.index("add_connector_persist_command_tombstone")
    delete_at = branch.index("zk_delete_user")
    assert persist_at < delete_at


def test_legacy_records_partial_snapshots_and_flapping_disable_writes():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "static const uint32_t user_record_sizes[] = {72, 28};" in source
    assert "LEGACY_USER_RECORD_READ_ONLY" in source
    assert "USER_SNAPSHOT_TRUNCATED" in source
    assert "ZONE_LITE_FLAP_QUIET_MS (5 * 60 * 1000)" in source
    assert "ZONE_LITE_DISCOVERY_FULL_SCAN_INTERVAL_MS (15 * 60 * 1000)" in source


def test_reconcile_and_restart_cadence_are_production_defaults():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    defaults = (FIRMWARE / "main" / "zone_lite_config.example.h").read_text(encoding="utf-8")
    assert "ZONE_LITE_RECONCILE_INTERVAL_MS (15 * 60 * 1000)" in defaults
    assert "ZONE_LITE_RESTART_SLOT_1_HOUR 2" in source
    assert "ZONE_LITE_RESTART_SLOT_2_HOUR 12" in source
    assert "ZONE_LITE_RESTART_SLOT_3_HOUR 22" in source
    assert 'nvs_set_i32(handle, "attn_count", g_last_synced_attendance_count)' in source
    assert 'nvs_set_i64(handle, "truth_epoch", g_last_full_truth_reconcile_epoch)' in source
    assert "g_last_full_truth_reconcile_epoch" in source
    assert "g_last_full_truth_reconcile_ms" in source
    assert "ZONE_LITE_LED_FAULT_LATCH_MS (2 * 60 * 1000)" in (
        FIRMWARE / "main" / "led_status.c"
    ).read_text(encoding="utf-8")
