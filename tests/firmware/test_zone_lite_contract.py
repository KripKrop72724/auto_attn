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


def test_provisioner_requires_explicit_fleet_root(monkeypatch, tmp_path: Path):
    provisioner = load_provisioner()
    config = tmp_path / "zone.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("ADD_FLEET_ROOT_SECRET", raising=False)
    monkeypatch.setenv("ADD_PII_LOOKUP_KEY", "must-never-be-a-provisioning-root")

    with pytest.raises(ValueError, match="never a safe provisioning fallback"):
        provisioner.load_config(config)


def test_split_root_recovery_requires_trust_and_exact_mac(monkeypatch, tmp_path: Path):
    provisioner = load_provisioner()
    config = tmp_path / "zone.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ADD_FLEET_ROOT_SECRET", "production-fleet-root")
    monkeypatch.setenv("ZONE_LITE_NVS_HMAC_ROOT_SECRET", "known-original-nvs-root")
    loaded = provisioner.load_config(config)
    mac = "e0:72:a1:d6:3c:7c"

    with pytest.raises(RuntimeError, match="trust-existing-derived-hmac"):
        provisioner.validate_root_selection(
            mac=mac,
            fleet_root=loaded["fleet_root"],
            nvs_hmac_root=loaded["nvs_hmac_root"],
            recovery_confirmation=mac,
            trust_existing=False,
        )
    with pytest.raises(RuntimeError, match="exact ESP"):
        provisioner.validate_root_selection(
            mac=mac,
            fleet_root=loaded["fleet_root"],
            nvs_hmac_root=loaded["nvs_hmac_root"],
            recovery_confirmation="e0:72:a1:d6:f3:28",
            trust_existing=True,
        )
    assert provisioner.validate_root_selection(
        mac=mac,
        fleet_root=loaded["fleet_root"],
        nvs_hmac_root=loaded["nvs_hmac_root"],
        recovery_confirmation=mac,
        trust_existing=True,
    )


def test_split_root_recovery_can_never_burn_an_empty_efuse(monkeypatch, tmp_path: Path):
    provisioner = load_provisioner()
    monkeypatch.setattr(
        provisioner,
        "efuse_summary",
        lambda *_args, **_kwargs: {
            "BLOCK_KEY0": {"raw_value": "0x" + ("0" * 64), "writeable": True},
            "KEY_PURPOSE_0": {"value": "USER"},
        },
    )

    with pytest.raises(RuntimeError, match="forbidden for an empty eFuse"):
        provisioner.ensure_nvs_hmac_key(
            espefuse="espefuse.py",
            port="test-port",
            mac="e0:72:a1:d6:3c:7c",
            key_path=tmp_path / "unused-key.bin",
            summary_path=tmp_path / "summary.json",
            confirmation=None,
            trust_existing=True,
            split_root_recovery=True,
        )


def test_secure_nvs_and_generic_image_are_mandatory_defaults():
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_NVS_ENCRYPTION=y" in defaults
    assert "CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y" in defaults
    assert "CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0" in defaults
    provisioner = (FIRMWARE / "tools" / "provision_zone_lite.py").read_text(encoding="utf-8")
    assert "--confirm-efuse-burn-for" in provisioner
    assert "--trust-existing-derived-hmac" in provisioner
    assert "--confirm-split-root-recovery-for" in provisioner
    assert 'or os.environ.get("ADD_PII_LOOKUP_KEY")' not in provisioner
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


def test_malformed_legacy_user_ids_use_keyed_raw_record_preconditions():
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    header = (FIRMWARE / "main" / "add_connector.h").read_text(encoding="utf-8")
    runtime = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "set_terminal_user_fingerprints" in runtime
    assert "mbedtls_md_hmac" in runtime
    assert "runtime->bootstrap_secret" in runtime
    assert '"ZONE-LITE-ZKT-USER-IDENTITY-V1"' in runtime
    assert '"ZONE-LITE-ZKT-USER-STATE-V1"' in runtime
    assert '"terminal_identity_fingerprint"' in runtime
    assert '"terminal_state_fingerprint"' in runtime
    assert "has_expected_terminal_identity_fingerprint" in header
    assert "has_expected_terminal_state_fingerprint" in header
    assert '"terminal_identity_fingerprint"' in connector
    assert '"terminal_state_fingerprint"' in connector
    assert "user_matches_expected_state" in runtime
    assert "verified_terminal_identity_fingerprint" in runtime
    assert "verified_terminal_state_fingerprint" in runtime


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


def test_full_reconcile_releases_dump_and_bounds_downstream_serialization():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    reconcile = source[
        source.index("static bool reconcile_attendance_dump(") :
        source.index("static bool system_time_is_valid(")
    ]
    assert "attendance_event_t *reconcile_events = heap_caps_calloc" in reconcile
    assert "char **truth_events" not in reconcile
    assert "ADD_RECONCILE_MAX_BATCHES" not in source
    assert "#define ADD_RECONCILE_COMMIT_BATCHES 32" in source
    assert reconcile.index("free(data);") < reconcile.index("oracle_send_reconcile(")
    assert reconcile.index("oracle_send_reconcile(") < reconcile.index(
        "add_enqueue_reconcile_events("
    )
    assert 'reconcile_complete ? "true" : "false"' in reconcile
    assert "return reconcile_complete;" in reconcile


def test_recoverable_truth_memory_pressure_never_latches_fatal_led():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    payload = source[
        source.index("static char *build_reconcile_payload(") :
        source.index("static bool oracle_reconcile_body_ok(")
    ]
    sender = source[
        source.index("static bool oracle_send_reconcile(\n", source.index(payload)) :
        source.index("static void append_acked_uid_from_json_to_file(")
    ]
    assert "one transient JSON row at a time" in payload
    assert 'event_to_json(&events[i], "MANUAL_REPROCESS")' in payload
    assert "LED_STATUS_FATAL" not in sender
    assert "LED_STATUS_TRUTH_REPAIR" in sender


def test_ords_truth_and_outbox_https_requests_are_serialized():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    wrapper = source[
        source.index("static int http_post_json(const char *url") :
        source.index("static bool oracle_success_body(")
    ]
    assert "static SemaphoreHandle_t g_ords_http_lock;" in source
    assert "xSemaphoreTake(" in wrapper
    assert "g_ords_http_lock" in wrapper
    assert "http_post_json_unlocked(url, json, response_body)" in wrapper
    assert "xSemaphoreGive(g_ords_http_lock);" in wrapper
    assert "g_ords_http_lock = xSemaphoreCreateMutex();" in source


def test_full_reconcile_arbitrates_outbox_before_downloading_zkt_dump():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    reconcile = source[
        source.index("static bool reconcile_attendance_dump(") :
        source.index("static bool system_time_is_valid(")
    ]
    assert "static SemaphoreHandle_t g_ords_outbox_gate;" in source
    gate_at = reconcile.index("xSemaphoreTake(\n            g_ords_outbox_gate")
    dump_at = reconcile.index("zk_read_buffer(sock, ctx, CMD_ATTLOG_RRQ")
    assert gate_at < dump_at
    assert "xSemaphoreGive(g_ords_outbox_gate);" in reconcile
    assert "g_ords_outbox_gate = xSemaphoreCreateMutex();" in source


def test_large_zkt_buffer_reads_allow_slow_prepare_data_delivery():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "#define ZKT_IO_TIMEOUT_SEC 90" in source
    assert "#define ZKT_BUFFER_CHUNK_BYTES 0xffc0" in source
    assert "const uint32_t max_chunk = ZKT_BUFFER_CHUNK_BYTES;" in source
    assert "CMD_PREPARE_DATA" in source
    reader = source[
        source.index("static bool zk_read_buffer(") :
        source.index("static void zk_disconnect(")
    ]
    assert reader.count("CMD_FREE_DATA") >= 4


def test_main_task_stack_is_sized_for_persistent_dedup_rebuilds():
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert "CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192" in defaults


def test_add_reports_the_built_application_version():
    source = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    component = (FIRMWARE / "main" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert '#include "esp_app_desc.h"' in source
    assert "esp_app_format" in component
    assert "esp_app_get_description()" in source
    assert 'snprintf(value, sizeof(value), "zone-lite-%s", version);' in source
    assert source.count(
        'cJSON_AddStringToObject(payload, "firmware_version", firmware_version());'
    ) == 1
    assert source.count(
        'cJSON_AddStringToObject(root, "firmware_version", firmware_version());'
    ) == 1
    assert '"zone-lite-2.1.' not in source
