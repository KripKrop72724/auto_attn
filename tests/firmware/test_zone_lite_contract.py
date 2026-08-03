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
    assert "ZONE_LITE_USER_INTEGRITY_INTERVAL_MS (15 * 60 * 1000)" in source
    assert "zk_refresh_users_stable" in source
    assert "USER_SNAPSHOT_UNSTABLE" in source
    assert "integrity_due" not in source
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
    assert "collect_reconcile_window(" in reconcile
    assert "bool daily_windows = truth_enabled ||" in reconcile
    assert "reconcile_event_count > reconcile_capacity;" in reconcile
    assert "while (window_start_day <= day_end)" in reconcile
    assert "if (truth_delivery_ok)" in reconcile
    assert "char **truth_events" not in reconcile
    assert "ADD_RECONCILE_MAX_BATCHES" not in source
    assert "#define ADD_RECONCILE_COMMIT_BATCHES 32" in source
    release_gate = reconcile.index(
        "xSemaphoreGive(g_ords_outbox_gate);",
        reconcile.index("xSemaphoreGive(g_storage_lock);"),
    )
    collect_window = reconcile.index("collect_reconcile_window(")
    oracle_send = reconcile.index("oracle_send_reconcile(", collect_window)
    free_dump = reconcile.rindex("free(data);")
    assert release_gate < collect_window < oracle_send < free_dump
    assert reconcile.index("add_enqueue_reconcile_events(") < reconcile.index(
        "oracle_send_reconcile("
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


def test_truth_reconcile_uses_bounded_authoritative_day_windows():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    collector = source[
        source.index("static size_t collect_reconcile_window(") :
        source.index("static bool reconcile_attendance_dump(")
    ]
    payload = source[
        source.index("static char *build_reconcile_payload(") :
        source.index("static bool oracle_reconcile_body_ok(")
    ]
    sender = source[
        source.index("static bool oracle_send_reconcile(\n", source.index(payload)) :
        source.index("static void append_acked_uid_from_json_to_file(")
    ]
    assert "zk_timestamp_in_window(timestamp, year, month, start_day, end_day)" in collector
    assert "if (count >= capacity)" in collector
    assert "window_start_day" in payload
    assert "window_end_day" in payload
    assert '"window_start\\":\\"%04d-%02d-%02d\\"' in payload
    assert '"window_end\\":\\"%04d-%02d-%02d\\"' in payload
    assert "window_start_day" in sender
    assert "window_end_day" in sender
    assert "#define ZONE_LITE_ORDS_RECONCILE_TIMEOUT_MS 45000" in source
    assert "#define ZONE_LITE_ORDS_RECONCILE_TRANSPORT_RETRIES 1" in source
    assert "#define ZONE_LITE_ORDS_RECONCILE_RETRY_DELAY_MS 2000" in source
    assert "http_post_diagnostics_t diagnostics = {0};" in sender
    assert "http_post_json_with_timeout(" in sender
    assert "ZONE_LITE_ORDS_RECONCILE_TIMEOUT_MS" in sender
    assert "int status = esp_http_client_get_status_code(client);" in source
    assert "diagnostics->transport_error = status >= 0 ? ESP_OK : err;" in source
    assert "if (status >= 0 && captured.data != NULL)" in source
    assert "status >= 0 ||" in sender
    assert "transport_cycles > ZONE_LITE_ORDS_RECONCILE_TRANSPORT_RETRIES" in sender
    assert '"ORDS_RECONCILE_TRANSPORT_RETRY"' in sender
    assert '"ORDS_RECONCILE_TRANSPORT_RECOVERED"' in sender
    assert "esp_err_to_name(diagnostics.transport_error)" in sender
    assert "diagnostics.trust_source" in sender
    assert "transport_attempts" in sender
    assert '"ORDS_RECONCILE_BACKOFF"' in sender
    assert '"ORDS_RECONCILE_WINDOW_LIMIT"' in sender
    assert '"ORDS_RECONCILE_PAYLOAD_FAILED"' in sender
    assert '"ORDS_RECONCILE_ENDPOINT_MISSING"' in sender
    assert '"ORDS_RECONCILE_AUTH_FAILED"' in sender
    assert "status == 401 || status == 403" in sender
    assert "HTTP responses are not retried" in sender
    assert '"ORDS_RECONCILE_REJECTED"' in sender
    assert '"ORDS_RECONCILE_RESPONSE_INVALID"' in sender
    assert '"ORDS_RECONCILE_HTTP_FAILED"' in sender
    assert "body ? body" not in sender


def test_add_connected_truth_is_durably_delegated_before_direct_oracle():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    reconcile = source[
        source.index("static bool reconcile_attendance_dump(") :
        source.index("static size_t process_live_packet(")
    ]
    add_enqueue = reconcile.index(
        "bool window_add_ok = add_enqueue_reconcile_events("
    )
    delegated = reconcile.index(
        '"ORACLE_RECONCILE_DELEGATED_TO_ADD"',
        add_enqueue,
    )
    direct = reconcile.index(
        "window_truth_ok = oracle_send_reconcile(",
        delegated,
    )
    assert add_enqueue < delegated < direct
    assert "zone_config_get()->add_enabled &&" in reconcile
    assert "window_add_ok" in reconcile
    assert (
        "durably delegated to ADD for Oracle delivery and membership confirmation"
        in reconcile
    )
    assert (
        "!(zone_config_get()->add_enabled && window_add_ok)"
        in reconcile
    )


def test_firmware_version_change_forces_immediate_truth_reconcile():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert '#include "esp_app_desc.h"' in source
    assert 'nvs_get_str(handle, "truth_ver"' in source
    assert 'strcmp(truth_version, running_version) != 0' in source
    assert "g_force_truth_reconcile = true;" in source
    assert "bool current_truth_due = g_force_truth_reconcile ||" in source
    success = source.index("g_force_truth_reconcile = false;")
    save = source.index("nvs_save_runtime_state();", success)
    assert success < save
    assert 'nvs_set_str(handle, "truth_ver", version)' in source


def test_user_integrity_interval_starts_after_refresh_completion():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    integrity_block = source[
        source.index(
            "if (now_ms - last_user_integrity >= "
            "ZONE_LITE_USER_INTEGRITY_INTERVAL_MS)"
        ) : source.index("if (now_ms - last_time_sample >= 60000)")
    ]
    assert "last_user_integrity = uptime_ms();" in integrity_block
    assert "last_user_integrity = now_ms;" not in integrity_block
    assert integrity_block.index("last_user_integrity = uptime_ms();") < (
        integrity_block.index('add_connector_set_activity("LIVE_CAPTURE");')
    )
    reconcile_refresh = source[
        source.index("if (refreshed_users != user_count)") :
        source.index("int64_t record_delta", source.index("if (refreshed_users != user_count)"))
    ]
    assert "last_user_integrity = uptime_ms();" in reconcile_refresh
    assert "last_user_integrity = now_ms;" not in reconcile_refresh


def test_historical_truth_cursor_is_persisted_bounded_and_fail_closed():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")

    assert "ZONE_LITE_HISTORY_SCHEMA_VERSION 2" in source
    assert 'nvs_set_i32(handle, "hist_year", g_history_cursor_year)' in source
    assert 'nvs_set_i32(handle, "hist_month", g_history_cursor_month)' in source
    assert 'nvs_set_u8(handle, "hist_pending"' in source
    assert "find_attendance_month_bounds(" in source
    assert "find_next_attendance_month(" in source
    assert '"HISTORY_EMPTY_MONTHS_SKIPPED"' in source
    next_month_block = source[
        source.index("static bool find_next_attendance_month(") :
        source.index("static bool parse_attendance_record(")
    ]
    assert "not_before_year" in next_month_block
    assert "not_before_month" in next_month_block
    assert "not_after_year" in next_month_block
    assert "not_after_month" in next_month_block
    assert "compare_months(" in next_month_block
    historical_start = source.index("if (historical) {\n        int oldest_year")
    historical_selection = source[
        historical_start :
        source.index("const uint8_t *p = data + 4;", historical_start)
    ]
    assert historical_selection.index("find_attendance_month_bounds(") < (
        historical_selection.index("find_next_attendance_month(")
    )
    assert historical_selection.index("find_next_attendance_month(") < (
        historical_selection.index("g_history_cursor_year = next_year;")
    )
    assert "historical_reconcile = !counter_mismatch" in source
    assert "advance_month(" in source
    assert '"HISTORY_BACKFILL_COMPLETE"' in source
    assert '"HISTORY_BACKFILL_BLOCKED"' in source
    assert '"history_backfill"' in connector

    payload = source[
        source.index("static char *build_reconcile_payload(") :
        source.index("static bool oracle_reconcile_body_ok(")
    ]
    sender = source[
        source.index("static bool oracle_send_reconcile(\n", source.index(payload)) :
        source.index("static void append_acked_uid_from_json_to_file(")
    ]
    assert '"api_version\\":2' in payload
    assert '"terminal_event_count\\":%u' in payload
    assert '"identity_mapped_count\\":%u' in payload
    assert '"identity_map_complete\\":true' in payload
    assert "terminal_event_count != event_count" in sender
    assert "identity_mapped_count != event_count" in sender

    attestation = oracle.index("if v_identity_map_complete <> 'true'")
    first_delete = oracle.index("delete from hr_raw_attn_capture_events d", attestation)
    assert "or v_api_version <> 2" in oracle
    assert "v_terminal_event_count <> v_received" in oracle
    assert "v_identity_mapped_count <> v_received" in oracle
    assert "no destructive repair was performed" in oracle[attestation:first_delete]
    assert oracle.count(
        "delete from hr_raw_attn_capture_events d\n         where 1 = 0"
    ) == 2
    assert attestation < first_delete


def test_oracle_reconcile_auth_is_dual_verifier_and_non_destructive():
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")
    migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_allow_fleet_auth_non_destructive.sql"
    ).read_text(encoding="utf-8")

    assert "c_api_password_sha256 constant" in oracle
    assert "c_fleet_api_password_sha256 constant" in oracle
    assert "c_add_api_password_sha256 constant" in oracle
    assert "v_password_digest = c_api_password_sha256" in oracle
    assert "v_password_digest = c_fleet_api_password_sha256" in oracle
    assert "v_password_digest = c_add_api_password_sha256" in oracle
    assert "c_api_password constant varchar2" not in oracle
    assert "c_fleet_api_password constant varchar2" not in oracle
    assert "c_add_api_password constant varchar2" not in oracle
    assert "Non-destructive ZKT truth reconcile applied" in oracle
    assert oracle.count(
        "delete from hr_raw_attn_capture_events d\n         where 1 = 0"
    ) == 2

    assert "restore_original" in migration
    assert "l_ddl_attempted" in migration
    assert "REPLACE_WITH_FLEET_API_USERNAME" in migration
    assert "REPLACE_WITH_FLEET_64_CHARACTER_SHA256_HEX" in migration
    assert "c_fleet_api_password_sha256 constant" in migration
    assert "v_password_digest = c_fleet_api_password_sha256" in migration
    assert "where 1 = 0" in migration
    assert "delete from hr_raw_attn_capture_events d" in migration
    assert "insert into hr_raw_attn_capture_events" not in migration
    assert "update hr_raw_attn_capture_events" not in migration


def test_oracle_add_auth_migration_is_idempotent_and_non_destructive():
    migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_allow_add_auth_non_destructive.sql"
    ).read_text(encoding="utf-8")

    assert "restore_original" in migration
    assert "l_ddl_attempted" in migration
    assert "REPLACE_WITH_ADD_API_USERNAME" in migration
    assert "REPLACE_WITH_ADD_64_CHARACTER_SHA256_HEX" in migration
    assert "c_add_api_password_sha256 constant" in migration
    assert "v_password_digest = c_add_api_password_sha256" in migration
    assert "already installed" in migration
    assert "where 1 = 0" in migration
    assert "delete from hr_raw_attn_capture_events" in migration
    assert "insert into hr_raw_attn_capture_events" not in migration
    assert "update hr_raw_attn_capture_events" not in migration


def test_oracle_live_handler_routes_through_shared_non_destructive_package():
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")
    migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_route_live_capture_through_package.sql"
    ).read_text(encoding="utf-8")

    assert "slic_zkt_truth_api.post_live(:body_text)" in oracle
    assert "p_pattern => 'raw-captures'" in oracle
    assert "restore_original" in migration
    assert "l_change_attempted" in migration
    assert "SLIC_ZKT_TRUTH_API.POST_LIVE" in migration
    assert "dbms_lob.compare" in migration
    assert "ords.define_handler" in migration
    assert "insert into hr_raw_attn_capture_events (" not in migration.lower()
    assert "update hr_raw_attn_capture_events " not in migration.lower()


def test_oracle_live_insert_recomputes_daily_flags_without_delete():
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")
    helper = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_create_daily_flag_helper.sql"
    ).read_text(encoding="utf-8")
    migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_recompute_live_daily_flags_non_destructive.sql"
    ).read_text(encoding="utf-8")

    marker = "Non-destructive daily flag recomputation"
    assert oracle.count(marker) == 1
    live_start = oracle.index("procedure handle_event_array")
    live = oracle[live_start : oracle.index("procedure post_live", live_start)]
    assert "'F' check_in" in live
    assert "'F' check_out" in live
    assert "slic_zkt_recompute_daily_flags(p_body)" in live
    assert "delete from hr_raw_attn_capture_events" not in live

    assert "create or replace procedure slic_zkt_recompute_daily_flags" in helper
    assert "for update" in helper
    assert "merge into hr_raw_attn_capture_events d" in helper
    assert "partition by stored.cnic, stored.attendance_date" in helper
    assert "delete from hr_raw_attn_capture_events" not in helper
    assert "commit;" not in helper.lower()

    assert "restore_original" in migration
    assert "l_ddl_attempted" in migration
    assert marker in migration
    assert "slic_zkt_recompute_daily_flags(p_body)" in migration
    assert "where 1 = 0" in migration
    assert "delete from hr_raw_attn_capture_events d" in migration


def test_oracle_reconcile_recomputes_whole_day_flags_without_transient_true_rows():
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")
    migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260728_recompute_reconcile_daily_flags_non_destructive.sql"
    ).read_text(encoding="utf-8")
    cpu_migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260730_bound_noop_reconcile_cpu_non_destructive.sql"
    ).read_text(encoding="utf-8")

    package_body_start = oracle.index("create or replace package body")
    reconcile_start = oracle.index("procedure post_reconcile", package_body_start)
    reconcile = oracle[reconcile_start : oracle.index("end post_reconcile;")]
    assert "'F' check_in" in reconcile
    assert "'F' check_out" in reconcile
    assert "slic_zkt_recompute_daily_flags(p_body)" in reconcile
    assert "if v_inserted + v_corrected + v_deleted > 0 then" in reconcile
    assert "v_flag_corrected := 0" in reconcile
    assert "where nvl(d.datasync, 0) <> 0" in reconcile
    assert "merge into hr_raw_attn_capture_events d" not in reconcile
    assert reconcile.count(
        "delete from hr_raw_attn_capture_events d\n         where 1 = 0"
    ) == 2

    assert "restore_original" in migration
    assert "l_ddl_attempted" in migration
    assert "slic_zkt_recompute_daily_flags(p_body)" in migration
    assert "v_flag_corrected := 0" in migration
    assert "where 1 = 0" in migration
    assert "execute immediate l_patched_ddl" in migration
    assert "insert into hr_raw_attn_capture_events" not in migration.lower()
    assert "update hr_raw_attn_capture_events" not in migration.lower()

    assert "restore_original" in cpu_migration
    assert "attempted boolean := false" in cpu_migration
    assert "if v_inserted + v_corrected + v_deleted > 0 then" in cpu_migration
    assert "where nvl(d.datasync, 0) <> 0" in cpu_migration
    assert "where 1 = 0" in cpu_migration
    assert "execute immediate p" in cpu_migration
    assert "execute immediate o" in cpu_migration
    assert "delete from hr_raw_attn_capture_events" in cpu_migration.lower()
    assert "insert into hr_raw_attn_capture_events" not in cpu_migration.lower()


def test_oracle_timestamp_ingestion_and_repair_preserve_event_instants():
    oracle = (
        ROOT / "deploy" / "add" / "oracle" / "slic_zkt_truth_api.sql"
    ).read_text(encoding="utf-8")
    package_migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260729_canonicalize_event_timezone_non_destructive.sql"
    ).read_text(encoding="utf-8")
    data_migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260729_repair_event_timezone_rows_non_destructive.sql"
    ).read_text(encoding="utf-8")
    sentinel_migration = (
        ROOT
        / "deploy"
        / "add"
        / "oracle"
        / "20260729_quarantine_implausible_device_times_non_destructive.sql"
    ).read_text(encoding="utf-8")

    package_body_start = oracle.index("create or replace package body")
    parser_start = oracle.index("function parse_event_timestamp", package_body_start)
    parser = oracle[parser_start : oracle.index("end parse_event_timestamp;")]
    assert ") at time zone c_attendance_timezone;" in parser
    assert "from_tz(" in parser
    assert "function attendance_timestamp_is_plausible" in oracle
    assert "2010-01-01 00:00:00 Asia/Karachi" in oracle
    assert oracle.count("then 'SUSPECT_DEVICE_TIME'") == 4
    assert oracle.count("trust_status <> 'SUSPECT_DEVICE_TIME'") == 0
    assert oracle.count(
        "slic_zkt_truth_api.attendance_timestamp_is_plausible("
    ) >= 7

    assert "restore_original" in package_migration
    assert "l_ddl_attempted" in package_migration
    assert "where 1 = 0" in package_migration
    assert "q'~" not in package_migration
    assert "chr(39)" in package_migration
    assert "update hr_raw_attn_capture_events" not in package_migration.lower()
    assert package_migration.lower().count(
        "delete from hr_raw_attn_capture_events"
    ) == 2

    normalized = data_migration.lower()
    assert "lock table hr_raw_attn_capture_events" in normalized
    assert "in share row exclusive mode wait 30" in normalized
    assert "sys_extract_utc(event_timestamp)" in normalized
    assert "event_timestamp at time zone 'asia/karachi'" in normalized
    assert "l_unknown_mismatches" in normalized
    assert "count(*) - count(distinct event_uid)" in normalized
    assert "rollback;" in normalized
    assert normalized.count("commit;") == 1
    clear_before_move = normalized.index(
        "clear both the old and corrected day groups before moving any row"
    )
    attendance_date_move = normalized.index(
        "update hr_raw_attn_capture_events\n"
        "       set attendance_date = trunc("
    )
    assert clear_before_move < attendance_date_move
    assert "uk_hr_raw_attn_one_checkout" in normalized
    assert "clear_day_flags(" in normalized
    assert "delete from hr_raw_attn_capture_events" not in normalized
    assert "insert into hr_raw_attn_capture_events" not in normalized
    sentinel_normalized = sentinel_migration.lower()
    assert "delete from hr_raw_attn_capture_events" not in sentinel_normalized
    assert "expected exactly two proven sentinel rows" in sentinel_normalized
    assert "trust_status = 'suspect_device_time'" in sentinel_normalized
    assert "datasync = 0" in sentinel_normalized
    assert "l_matches <> 2" in sentinel_normalized
    assert "l_updated <> 2" in sentinel_normalized
    assert "restore_original" in sentinel_normalized
    assert "2010-01-01 00:00:00 asia/karachi" in sentinel_normalized


def test_firmware_excludes_zero_and_implausible_terminal_timestamps():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    validator_start = source.index(
        "static bool zk_attendance_timestamp_is_plausible("
    )
    validator_end = source.index("static void iso_from_zk_time(", validator_start)
    validator = source[validator_start:validator_end]
    builder_start = source.index("static bool build_attendance_event(")
    builder_end = source.index("static bool zk_timestamp_in_window(", builder_start)
    builder = source[builder_start:builder_end]

    assert "#define ZONE_LITE_MIN_ATTENDANCE_YEAR 2010" in source
    assert "if (value == 0) return false;" in validator
    assert "year >= ZONE_LITE_MIN_ATTENDANCE_YEAR" in validator
    assert "zk_attendance_timestamp_is_plausible(timestamp)" in builder
    assert "ATTENDANCE_TIMESTAMP_QUARANTINED" in builder
    assert builder.index("zk_attendance_timestamp_is_plausible(timestamp)") < builder.index(
        "build_event_uid(out)"
    )


def test_blocked_identity_recovery_uses_verified_add_alias_catalog():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    recovery = source[
        source.index("static bool recover_blocked_events_from_snapshot(") :
        source.index("static void storage_init(")
    ]
    assert "add_connector_lookup_identity(" in recovery
    assert "recovered_cnic" in recovery
    assert "recovered_name" in recovery
    assert "recovered_shift_worker" in recovery
    assert recovery.index("add_connector_lookup_identity(") < recovery.index(
        "cJSON_AddStringToObject(root, \"cnic\", recovered_cnic)"
    )
    assert "verified ADD identity alias" in recovery


def test_large_blocked_identity_backlog_is_deferred_before_storage_lock():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "#define ZONE_LITE_BLOCKED_RECOVERY_MAX_BYTES (64 * 1024)" in source
    recovery = source[
        source.index("static bool recover_blocked_events_from_snapshot(") :
        source.index("static void storage_init(")
    ]
    size_gate = recovery.index("blocked_stat.st_size > ZONE_LITE_BLOCKED_RECOVERY_MAX_BYTES")
    storage_lock = recovery.index("xSemaphoreTake(g_storage_lock")
    assert size_gate < storage_lock
    assert '"BLOCKED_IDENTITY_RECOVERY_DEFERRED"' in recovery
    assert "records remain preserved for bounded truth recovery" in recovery
    assert recovery.index("return true;", size_gate) < storage_lock


def test_fragmented_identity_catalog_is_reassembled_applied_and_forces_truth():
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    header = (FIRMWARE / "main" / "add_connector.h").read_text(encoding="utf-8")
    runtime = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")

    assert "#define ADD_MAX_INBOUND_BYTES (512 * 1024)" in connector
    assert "receive_inbound_fragment" in connector
    assert "event->payload_offset" in connector
    assert "offset != s_inbound_payload_received" in connector
    assert "s_inbound_payload_received == s_inbound_payload_expected" in connector
    assert "#define ADD_INBOUND_QUEUE_DEPTH 96" in connector
    assert "inbound_message_task" in connector
    assert 'xTaskCreate(inbound_message_task, "add_inbound", 12288' in connector
    assert "xQueueSend(s_inbound_messages, &message, 0)" in connector
    assert "parse_inbound(message.data, message.length)" in connector
    assert "cJSON *root = cJSON_Parse(data);" in connector
    assert "char *copy = calloc(1, len + 1);" not in connector
    assert "reset_inbound_payload();" in connector
    assert "ADD_IDENTITY_CATALOG_MAX_ROWS 4096" in connector
    assert "s_identity_catalog_generation++" in connector
    assert "add_connector_identity_catalog_generation" in connector
    assert "add_connector_identity_catalog_generation" in header
    assert '"IDENTITY_CATALOG_APPLIED"' in runtime
    assert "identity_catalog_generation != applied_identity_catalog_generation" in runtime
    applied = runtime.index("applied_identity_catalog_generation = identity_catalog_generation")
    forced = runtime.index("g_force_truth_reconcile = true;", applied)
    assert applied < forced


def test_large_identity_catalog_is_committed_as_bounded_encrypted_rows():
    connector = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
    backend = (ROOT / "apps/add_backend/zk_add/web.py").read_text()
    catalog = (ROOT / "apps/add_backend/zk_add/identity_catalog.py").read_text()
    realtime = (ROOT / "apps/add_backend/zk_add/realtime.py").read_text()

    assert "#define ADD_INBOUND_QUEUE_DEPTH 96" in connector
    assert 'cJSON_AddStringToObject(metadata, "schema_version", "3")' in connector
    assert 'cJSON_AddNumberToObject(metadata, "rows_count", row_count)' in connector
    assert "write_encrypted_json_line(file, metadata)" in connector
    assert "write_encrypted_json_line(file, row)" in connector
    assert "fflush(file) != 0 || fsync(fileno(file)) != 0" in connector
    assert "activate_identity_catalog(ADD_IDENTITY_CATALOG_TMP_PATH)" in connector
    assert "ADD_IDENTITY_CATALOG_BACKUP_PATH" in connector
    assert "backup_result == 0" in connector
    assert "errno != ENOENT" in connector
    assert "access(ADD_IDENTITY_CATALOG_PATH" not in connector
    assert '"IDENTITY_CATALOG_PERSIST_FAILED"' in connector
    assert '"type": "identity_catalog_begin"' in catalog
    assert '"type": "identity_catalog_chunk"' in catalog
    assert '"type": "identity_catalog_commit"' in catalog
    assert "IDENTITY_CATALOG_CHUNK_ROWS = 64" in catalog
    assert "from zk_add.identity_catalog import identity_catalog_messages" in backend
    assert "connector_hub.send_many" in backend
    assert "async def send_many" in realtime


def test_ota_boot_confirmation_waits_for_runtime_health_and_reports_stages():
    ota = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    connector = (FIRMWARE / "main" / "add_connector.c").read_text(encoding="utf-8")
    header = (FIRMWARE / "main" / "add_connector.h").read_text(encoding="utf-8")

    assert ota.count("esp_secure_boot_enabled()") == 2
    assert '"image_sha256"' in ota
    assert "esp_partition_get_sha256(running, digest)" in ota
    assert "OTA_BOOT_CONFIRM_SECONDS 900" in ota
    assert "add_connector_boot_health_ready()" in ota
    assert '"WAITING_FOR_RUNTIME_HEALTH"' in ota
    assert '"RUNTIME_HEALTHY"' in ota
    assert '"BOOT_HEALTH_TIMEOUT"' in ota
    healthy = ota.index('report_state("BOOTED_PENDING", "RUNTIME_HEALTHY")')
    valid = ota.index("esp_ota_mark_app_valid_cancel_rollback()", healthy)
    assert healthy < valid

    assert "bool add_connector_boot_health_ready(void)" in connector
    assert "s_identity_catalog_generation > 0" in connector
    assert 'strcmp(s_zkt.connection_state, "ONLINE") == 0' in connector
    assert 'strcmp(s_zkt.connection_state, "RECOVERING") == 0' in connector
    assert "authenticated_stability_elapsed" in connector
    assert "s_zkt.stability_since_epoch > 1700000000" in connector
    assert "ZONE_LITE_RECOVERY_STABILITY_MS" in connector
    assert "s_zkt.user_count >= 0" in connector
    assert "s_zkt.attendance_count >= 0" in connector
    assert "bool add_connector_boot_health_ready(void);" in header
    assert "restore_valid_identity_catalog();" in connector
    assert "s_identity_catalog_generation = 1" in connector


def test_authoritative_truth_requires_a_stable_terminal_dump():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    reconcile = source[
        source.index("static bool reconcile_attendance_dump(") :
        source.index("static bool system_time_is_valid(")
    ]
    stable_guard = reconcile.index("verified_records != records")
    window_send = reconcile.index("oracle_send_reconcile(", stable_guard)
    assert "parsed_records != (uint32_t)records" in reconcile
    assert "verified_users != (int32_t)users->count" in reconcile
    assert "authoritative replacement aborted" in reconcile
    assert stable_guard < window_send
    assert "Refusing an authoritative empty replacement" in reconcile


def test_ords_truth_and_outbox_https_requests_are_serialized():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    wrapper = source[
        source.index("static int http_post_json_with_timeout(") :
        source.index("static bool oracle_success_body(")
    ]
    assert "static SemaphoreHandle_t g_ords_http_lock;" in source
    assert "xSemaphoreTake(" in wrapper
    assert "g_ords_http_lock" in wrapper
    assert "http_post_json_unlocked(" in wrapper
    assert "timeout_ms" in wrapper
    assert "xSemaphoreGive(g_ords_http_lock);" in wrapper
    assert "ZONE_LITE_ORDS_TIMEOUT_MS" in wrapper
    assert "g_ords_http_lock = xSemaphoreCreateMutex();" in source


def test_reconcile_timeout_is_isolated_from_live_and_bulk_delivery():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    live = source[
        source.index("static oracle_delivery_result_t oracle_send_live(") :
        source.index("static char *build_bulk_payload(")
    ]
    bulk_start = source.index(
        "static bool oracle_send_bulk(char **events",
        source.index("static char *build_bulk_payload("),
    )
    bulk = source[bulk_start : source.index("static int days_in_month(", bulk_start)]
    sender = source[
        source.index("static bool oracle_send_reconcile(") :
        source.index("static void append_acked_uid_from_json_to_file(")
    ]
    assert "http_post_json(url, normalized_event, &body)" in live
    assert "http_post_json(url, payload, &body)" in bulk
    assert "ZONE_LITE_ORDS_RECONCILE_TIMEOUT_MS" not in live
    assert "ZONE_LITE_ORDS_RECONCILE_TIMEOUT_MS" not in bulk
    assert "ORDS_RECONCILE_TRANSPORT_RETRY" not in live
    assert "ORDS_RECONCILE_TRANSPORT_RETRY" not in bulk
    assert "http_post_json_with_timeout(" in sender


def test_oracle_receipts_are_durable_before_ords_rows_are_retired():
    zone_source = (FIRMWARE / "main" / "zone_lite.c").read_text()
    connector_source = (FIRMWARE / "main" / "add_connector.c").read_text()
    header_source = (FIRMWARE / "main" / "add_connector.h").read_text()

    assert "oracle_receipt_batch" in connector_source
    assert "oracle_receipt_payload_is_valid" in connector_source
    assert "add_connector_enqueue_oracle_receipts" in connector_source
    assert "add_connector_enqueue_oracle_receipts" in header_source
    assert '"FIRMWARE_LIVE"' in zone_source
    assert '"FIRMWARE_BULK"' in zone_source
    live_receipt = zone_source.index(
        "add_enqueue_json_receipts(\n                        live_event"
    )
    live_retire = zone_source.index(
        "append_acked_uid_from_json_to_file(line, acked_file)",
        live_receipt,
    )
    assert live_receipt < live_retire


def test_large_truth_stream_delegates_confirmation_to_durable_add_delivery():
    zone_source = (FIRMWARE / "main" / "zone_lite.c").read_text()
    connector_source = (FIRMWARE / "main" / "add_connector.c").read_text()
    reconcile = zone_source[
        zone_source.index("static bool reconcile_attendance_dump(") :
        zone_source.index("static bool system_time_is_valid(")
    ]

    assert "add_enqueue_truth_receipts(" not in reconcile
    assert '"FIRMWARE_RECONCILE"' not in reconcile
    assert '"ORACLE_RECONCILE_ACCEPTED"' in reconcile
    assert "per-event confirmation delegated to durable ADD delivery/check" in reconcile
    assert "truth_delivery_ok && add_delivery_ok" in reconcile
    assert "receipt_delivery_ok" not in reconcile
    assert "#define ADD_BULK_CAPACITY_WAIT_MS" in connector_source
    assert "#define ADD_ACK_TIMEOUT_MS 60000" in connector_source
    assert "ADD_BULK_CAPACITY_POLL_MS" in connector_source
    assert "refresh_capacity_deadline_on_progress" in connector_source
    assert "outbox->depth < *last_depth" in connector_source
    assert "*deadline_ms = monotonic_ms() + ADD_BULK_CAPACITY_WAIT_MS;" in connector_source
    assert "required_bytes" in connector_source
    assert "s_bulk_outbox.offset > 0" in connector_source
    assert "compact_outbox_locked(&s_bulk_outbox, true)" in connector_source
    assert "waiting for acknowledged rows before appending more truth" in connector_source


def test_full_reconcile_defers_until_prior_durable_add_backlog_drains():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    connector_source = (FIRMWARE / "main" / "add_connector.c").read_text()
    header_source = (FIRMWARE / "main" / "add_connector.h").read_text()
    gateway_start = source.index("uint32_t bulk_depth = 0;")
    gateway_reconcile = source[
        gateway_start :
        source.index('add_connector_set_activity("LIVE_CAPTURE");', gateway_start)
    ]
    deferred_start = gateway_reconcile.index(
        "if (!bulk_depth_available || bulk_depth > 0)"
    )
    deferred_end = gateway_reconcile.index(
        "} else {",
        gateway_reconcile.index("reconcile_succeeded = false;", deferred_start),
    )
    deferred = gateway_reconcile[
        deferred_start:deferred_end
    ]

    assert "add_connector_get_bulk_outbox_depth" in connector_source
    assert "add_connector_get_bulk_outbox_depth" in header_source
    assert "add_connector_get_bulk_outbox_depth(&bulk_depth)" in gateway_reconcile
    assert "!bulk_depth_available || bulk_depth > 0" in gateway_reconcile
    assert '"FULL_RECONCILE_DEFERRED_OUTBOX"' in deferred
    assert "reconcile_succeeded = false;" in deferred
    assert "g_force_truth_reconcile = false" not in deferred
    assert "g_last_full_truth_reconcile_epoch" not in deferred


def test_oracle_receipt_batches_collapse_duplicate_terminal_event_uids():
    connector_source = (FIRMWARE / "main" / "add_connector.c").read_text(
        encoding="utf-8"
    )
    receipt_enqueue = connector_source[
        connector_source.index("bool add_connector_enqueue_oracle_receipts(") :
        connector_source.index(
            "bool add_connector_enqueue_attendance_bulk(",
            connector_source.index("bool add_connector_enqueue_oracle_receipts("),
        )
    ]

    assert "size_t unique_count = 0;" in receipt_enqueue
    assert "size_t duplicate_count = 0;" in receipt_enqueue
    assert "strcmp(event_uids[previous], event_uids[i]) == 0" in receipt_enqueue
    assert "if (duplicate)" in receipt_enqueue
    assert "continue;" in receipt_enqueue
    assert "Collapsed %lu duplicate Oracle receipt event UID(s)" in receipt_enqueue
    assert "cJSON_CreateObject" not in receipt_enqueue
    assert "cJSON_CreateArray" not in receipt_enqueue
    assert "cJSON_CreateString" not in receipt_enqueue
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in receipt_enqueue
    assert "ADD_OUTBOX_LINE_BYTES - used" in receipt_enqueue
    assert "add_connector_enqueue_validated_line(" in receipt_enqueue


def test_full_reconcile_arbitrates_outbox_before_downloading_zkt_dump():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    reconcile = source[
        source.index("static bool reconcile_attendance_dump(") :
        source.index("static bool system_time_is_valid(")
    ]
    assert "static SemaphoreHandle_t g_ords_outbox_gate;" in source
    gate_at = reconcile.index("xSemaphoreTake(\n            g_ords_outbox_gate")
    dump_at = reconcile.index("CMD_ATTLOG_RRQ,")
    assert gate_at < dump_at
    assert "xSemaphoreGive(g_ords_outbox_gate);" in reconcile
    assert "g_ords_outbox_gate = xSemaphoreCreateMutex();" in source


def test_large_zkt_buffer_reads_allow_slow_prepare_data_delivery():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    assert "#define ZKT_IO_TIMEOUT_SEC 90" in source
    assert "#define ZKT_BUFFER_CHUNK_BYTES 0xffc0" in source
    assert "#define ZKT_BUFFER_RECOVERY_CHUNK_BYTES 0x4000" in source
    assert "#define ZKT_TRUTH_FRESH_SESSION_RETRIES 2" in source
    assert "attendance_chunk_bytes = g_truth_use_recovery_chunks" in source
    assert '"TRUTH_READ_RETRY_SESSION"' in source
    assert '"TRUTH_READ_RETRY_EXHAUSTED"' in source
    assert "g_truth_retry_session_requested = true;" in source
    assert "if (g_truth_retry_session_requested)" in source
    assert "if (!restarted && !truth_retry_session)" in source
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


def test_live_capture_uses_verified_snapshot_without_blocking_event_loop():
    source = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
    live_start = source.index("if (header->command == CMD_REG_EVENT")
    live = source[
        live_start : source.index("\n            }\n            free(packet);", live_start)
    ]
    assert live.index("zk_send_ack_only(") < live.index("process_live_packet(")
    assert "zk_refresh_users_stable(" not in live
    assert "zk_get_counts(" not in live
    assert 'add_connector_set_activity("VERIFYING_IDENTITY")' not in live
    assert 'add_connector_set_activity("LIVE_CAPTURE")' in live
    assert "recover_blocked_events_from_snapshot(users, NULL);" in source
    assert 'cJSON_AddStringToObject(payload, "state_hash", state_hash);' in source
    assert 'cJSON_AddBoolToObject(payload, "stable", true);' in source
    assert "BLOCKED_IDENTITY_REPAIRED" in source
