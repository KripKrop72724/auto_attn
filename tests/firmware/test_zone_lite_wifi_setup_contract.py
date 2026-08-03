from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"
PORTAL = (FIRMWARE / "main" / "setup_portal.c").read_text(encoding="utf-8")
ASSETS = (FIRMWARE / "main" / "setup_portal_assets.c").read_text(encoding="utf-8")


def test_setup_credential_is_build_injected_and_portal_fails_closed_without_it() -> None:
    cmake = (FIRMWARE / "main" / "CMakeLists.txt").read_text(encoding="utf-8")
    template = (FIRMWARE / "main" / "setup_password.h.in").read_text(encoding="utf-8")
    assert '"$ENV{ZONE_LITE_SETUP_PASSWORD}"' in cmake
    assert "ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 0" in cmake
    assert "ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 1" in cmake
    assert "@ZONE_LITE_SETUP_PASSWORD@" in template
    assert "if (!ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION)" in PORTAL
    assert "ZONE_LITE_SETUP_PASSWORD" in PORTAL
    assert "constant_time_equal" in PORTAL
    assert "s_failed_passwords >= 5" in PORTAL
    assert "s_lockout_until_ms = current + 60000" in PORTAL


def test_setup_ap_is_bounded_isolated_and_dormant_during_normal_operation() -> None:
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert '"SLIC ATTENDANCE-%02X%02X"' in PORTAL
    assert "ap.ap.max_connection = 1" in PORTAL
    assert "WIFI_AUTH_WPA2_PSK" in PORTAL
    assert "esp_wifi_set_mode(WIFI_MODE_APSTA)" in PORTAL
    assert "esp_wifi_set_mode(WIFI_MODE_STA)" in PORTAL
    assert "request_from_ap" in PORTAL
    assert "local_ip == 0xC0A8FE01UL" in PORTAL
    assert "(peer_ip & PORTAL_MASK) == PORTAL_NET" in PORTAL
    assert "ESP_NETIF_CAPTIVEPORTAL_URI" in PORTAL
    prepare = PORTAL[
        PORTAL.index("esp_err_t setup_portal_prepare") :
        PORTAL.index("esp_err_t setup_portal_start_controller")
    ]
    start = PORTAL[
        PORTAL.index("static esp_err_t portal_start") :
        PORTAL.index("static void portal_stop")
    ]
    stop = PORTAL[
        PORTAL.index("static void portal_stop") :
        PORTAL.index("static void controller_task")
    ]
    assert "esp_netif_dhcps_start" not in prepare
    assert "esp_netif_dhcps_start(s_ap_netif)" in start
    assert "esp_netif_dhcps_stop(s_ap_netif)" in stop
    assert "CONFIG_LWIP_IP_FORWARD=n" in defaults
    assert "ip_napt_enable" not in PORTAL


def test_recovery_and_manual_activation_windows_match_the_operating_contract() -> None:
    assert "PORTAL_RECOVERY_MS     (2 * 60 * 1000)" in PORTAL
    assert "PORTAL_BUTTON_HOLD_MS  5000" in PORTAL
    assert "PORTAL_MANUAL_IDLE_MS  (10 * 60 * 1000)" in PORTAL
    assert "PORTAL_STABLE_CLOSE_MS (30 * 1000)" in PORTAL
    assert "PORTAL_STA_RETRY_MS    5000" in PORTAL
    assert "PORTAL_PENDING_ROLLBACK_MS (15 * 60 * 1000)" in PORTAL
    assert "esp_wifi_connect()" in PORTAL
    assert "esp_ota_mark_app_invalid_rollback_and_reboot()" in PORTAL
    assert "running_app_pending_verify()" in PORTAL
    assert "GPIO_NUM_0" in PORTAL
    assert "ota_manager_busy()" in PORTAL


def test_only_network_selection_routes_are_exposed() -> None:
    for route in ("/api/networks", "/api/status", "/api/network"):
        assert f'.uri = "{route}"' in PORTAL
    for forbidden in ("/api/log", "/api/user", "/api/attendance", "/api/ota", "/api/file", "/api/zkt"):
        assert forbidden not in PORTAL
    assert "No attendance, device, or update controls are exposed" in ASSETS
    assert "http://" not in ASSETS
    assert "https://" not in ASSETS
    assert "Content-Security-Policy" in PORTAL


def test_candidate_network_is_tested_before_one_atomic_encrypted_nvs_commit() -> None:
    config = (FIRMWARE / "main" / "zone_config.c").read_text(encoding="utf-8")
    assert "VALIDATION_TIMEOUT_MS  30000" in PORTAL
    assert "esp_wifi_get_config(WIFI_IF_STA, &old_configuration)" in PORTAL
    assert "restore_station(&old_configuration)" in PORTAL
    assert PORTAL.index("VALIDATION_OK_BIT") < PORTAL.index("zone_config_save_wifi(candidate->ssid")
    save_wifi = config[config.index("esp_err_t zone_config_save_wifi"):]
    assert 'nvs_set_str(handle, "wifi_ssid", ssid)' in save_wifi
    assert 'nvs_set_str(handle, "wifi_pass", password)' in save_wifi
    assert save_wifi.count("nvs_commit(handle)") == 1
    assert "secure_zero(candidate" in PORTAL
    assert "wipe_json_credentials" in PORTAL


def test_ota_and_local_network_change_are_mutually_exclusive() -> None:
    ota = (FIRMWARE / "main" / "ota_manager.c").read_text(encoding="utf-8")
    assert "!setup_portal_active() && fetch_assignment()" in ota
    assert "if (ota_manager_busy())" in PORTAL
    assert "ESP_OTA_IMG_PENDING_VERIFY" in PORTAL


def test_release_pipeline_keeps_setup_enabled_binaries_out_of_public_releases() -> None:
    candidate = (ROOT / ".github" / "workflows" / "firmware-hil-candidate.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "firmware-release.yml").read_text(encoding="utf-8")
    canary = (ROOT / ".github" / "workflows" / "firmware-canary-promote.yml").read_text(encoding="utf-8")
    assert "secrets.ZONE_LITE_SETUP_PASSWORD" in candidate
    assert "-e ZONE_LITE_SETUP_PASSWORD" in candidate
    assert "retention-days: 1" in candidate
    assert "gh release create" not in release
    assert "gh release create" not in canary


def test_nationwide_rollout_is_canary_gated_batched_and_fail_closed() -> None:
    workflow = (ROOT / ".github" / "workflows" / "firmware-nationwide-rollout.yml").read_text(encoding="utf-8")
    rollout = (ROOT / "deploy" / "add" / "Invoke-NationwideFirmwareRollout.ps1").read_text(encoding="utf-8")
    assert "CANARY_ACCEPTED:" in rollout
    assert "[ValidateRange(1, 5)][int]$BatchSize" in rollout
    assert "NATIONWIDE_HALTED:" in rollout
    assert "ROLLED_BACK" in rollout
    assert "NATIONWIDE_ACCEPTED:" in rollout
    assert "Post-rollout stability verification failed" in rollout
    assert "Invoke-WebRequest -UseBasicParsing -Method Post" in rollout
    assert "'(?:^|[,\\s])add_admin=([^;,\\s]+)'" in rollout
    assert "$adminCookie.Secure = $false" in rollout
    assert "$script:Session.Cookies.Add([Uri]$BaseUrl, $adminCookie)" in rollout
    assert "-SessionVariable Session" not in rollout
    assert "environment: firmware-production" in workflow
    assert "ref: main" in workflow
