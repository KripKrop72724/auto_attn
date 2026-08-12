import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "zone_lite"
PORTAL = (FIRMWARE / "main" / "setup_portal.c").read_text(encoding="utf-8")
PORTAL_HEADER = (FIRMWARE / "main" / "setup_portal.h").read_text(encoding="utf-8")
ZONE = (FIRMWARE / "main" / "zone_lite.c").read_text(encoding="utf-8")
ASSETS = (FIRMWARE / "main" / "setup_portal_assets.c").read_text(encoding="utf-8")
SDKCONFIG = (FIRMWARE / "sdkconfig").read_text(encoding="utf-8")


def test_setup_ap_credential_is_build_injected_and_portal_fails_closed_without_it() -> None:
    cmake = (FIRMWARE / "main" / "CMakeLists.txt").read_text(encoding="utf-8")
    template = (FIRMWARE / "main" / "setup_password.h.in").read_text(encoding="utf-8")
    assert '"$ENV{ZONE_LITE_SETUP_PASSWORD}"' in cmake
    assert "ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 0" in cmake
    assert "ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 1" in cmake
    assert "@ZONE_LITE_SETUP_PASSWORD@" in template
    assert "if (!ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION)" in PORTAL
    assert "ZONE_LITE_SETUP_PASSWORD" in PORTAL
    assert "strlcpy((char *)ap.ap.password, ZONE_LITE_SETUP_PASSWORD" in PORTAL
    assert "constant_time_equal" in PORTAL
    assert "constant_time_equal(csrf->valuestring, s_csrf)" in PORTAL
    assert 'GetObjectItemCaseSensitive(input, "setup_password")' not in PORTAL
    assert "setup-password" not in ASSETS


def test_setup_ap_is_bounded_isolated_and_dormant_during_normal_operation() -> None:
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    assert '"SLIC ATTENDANCE-%02X%02X"' in PORTAL
    assert "ap.ap.max_connection = 1" in PORTAL
    assert "WIFI_AUTH_WPA2_PSK" in PORTAL
    assert "esp_wifi_set_mode(WIFI_MODE_APSTA)" in PORTAL
    assert "esp_wifi_set_mode(WIFI_MODE_STA)" in PORTAL
    assert "request_from_ap" in PORTAL
    request_guard = PORTAL[
        PORTAL.index("static bool request_from_ap") :
        PORTAL.index("static void set_ap_auth_tracking")
    ]
    assert "esp_wifi_ap_get_sta_list(&" not in request_guard
    assert '#include "esp_wifi_ap_get_sta_list.h"' not in PORTAL
    assert "esp_wifi_ap_get_sta_list_with_ip(&" not in PORTAL
    assert "ap_station_is_currently_associated" in PORTAL
    assert "esp_wifi_ap_get_sta_list(&stations)" in PORTAL
    assert "setup_portal_client_auth_allows" in PORTAL
    assert "IP_EVENT_AP_STAIPASSIGNED" in ZONE
    assert "setup_portal_handle_ap_station_ip_assigned" in ZONE
    assert "setup_portal_handle_ap_station_connected" in ZONE
    assert "setup_portal_handle_ap_station_disconnected" in ZONE
    assert "PORTAL_CLIENT_AUTH_WAIT_MS 1500" in PORTAL
    assert "xEventGroupWaitBits" in PORTAL
    assert "struct sockaddr_storage local" in request_guard
    assert "struct sockaddr_storage peer" in request_guard
    assert "getsockname(fd" in request_guard
    assert "setup_portal_socket_guard_allows" in request_guard
    assert "CONFIG_LWIP_IPV6=y" in SDKCONFIG
    assert "ESP_NETIF_CAPTIVEPORTAL_URI" in PORTAL
    assert "PORTAL_HTTP_MAX_OPEN_SOCKETS 7" in PORTAL
    assert "config.max_open_sockets = PORTAL_HTTP_MAX_OPEN_SOCKETS" in PORTAL
    assert 'httpd_resp_set_hdr(request, "Connection", "close")' in PORTAL
    for captive_path in (
        "/hotspot-detect.html",
        "/library/test/success.html",
        "/generate_204",
        "/gen_204",
        "/connecttest.txt",
        "/ncsi.txt",
    ):
        assert f'.uri = "{captive_path}"' in PORTAL
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


def test_setup_client_authorization_survives_event_order_and_rejects_stale_leases(
    tmp_path: Path,
) -> None:
    auth_source = FIRMWARE / "main" / "setup_portal_client_auth.c"
    harness = tmp_path / "portal_auth_harness.c"
    executable = tmp_path / "portal_auth_harness"
    harness.write_text(
        textwrap.dedent(
            """
            #include <assert.h>
            #include <stdint.h>
            #include "setup_portal_client_auth.h"

            int main(void) {
                const uint8_t mac_a[6] = {0, 1, 2, 3, 4, 5};
                const uint8_t mac_b[6] = {6, 7, 8, 9, 10, 11};
                const uint32_t ip_a = UINT32_C(0x02fea8c0);
                const uint32_t ip_b = UINT32_C(0x03fea8c0);
                setup_portal_client_auth_t state;

                setup_portal_client_auth_reset(&state);
                assert(state.association_generation == 0);
                assert(!setup_portal_client_auth_allows(&state, ip_a));

                setup_portal_client_auth_connected(&state, mac_a, true);
                assert(state.association_generation == 1);
                assert(!setup_portal_client_auth_allows(&state, ip_a));
                assert(!setup_portal_client_auth_ip_assigned(&state, mac_b, ip_b, false));
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, true));
                assert(state.lease_generation == state.association_generation);
                assert(setup_portal_client_auth_allows(&state, ip_a));
                assert(!setup_portal_client_auth_allows(&state, ip_b));

                /* A DHCP renewal atomically replaces the prior address. */
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_b, true));
                assert(!setup_portal_client_auth_allows(&state, ip_a));
                assert(setup_portal_client_auth_allows(&state, ip_b));
                assert(!setup_portal_client_auth_disconnected(&state, mac_b, false));
                assert(setup_portal_client_auth_allows(&state, ip_b));
                assert(setup_portal_client_auth_disconnected(&state, mac_a, false));
                assert(state.association_generation == 2);
                assert(!setup_portal_client_auth_allows(&state, ip_b));

                /* DHCP may be delivered before the association callback. */
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, true));
                assert(state.association_generation == 3);
                assert(setup_portal_client_auth_allows(&state, ip_a));
                setup_portal_client_auth_connected(&state, mac_a, true);
                assert(state.association_generation == 3);
                assert(setup_portal_client_auth_allows(&state, ip_a));

                /* A delayed DHCP event cannot resurrect a disconnected client. */
                assert(setup_portal_client_auth_disconnected(&state, mac_a, false));
                assert(state.association_generation == 4);
                assert(!setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, false));
                assert(!setup_portal_client_auth_allows(&state, ip_a));

                /* A delayed connect callback cannot resurrect a client either. */
                setup_portal_client_auth_connected(&state, mac_a, false);
                assert(!setup_portal_client_auth_allows(&state, ip_a));

                /* A real reconnect is accepted even if DHCP is observed first. */
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, true));
                assert(state.association_generation == 5);
                setup_portal_client_auth_connected(&state, mac_a, true);
                assert(setup_portal_client_auth_allows(&state, ip_a));

                /* A queued disconnect observed after physical re-association
                   starts a fresh generation and cannot retain the old lease. */
                assert(setup_portal_client_auth_disconnected(&state, mac_a, true));
                assert(state.association_generation == 6);
                assert(!setup_portal_client_auth_allows(&state, ip_a));
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, true));
                assert(setup_portal_client_auth_allows(&state, ip_a));
                setup_portal_client_auth_connected(&state, mac_a, true);
                assert(setup_portal_client_auth_allows(&state, ip_a));

                /* Even if a disconnect callback is lost, the next same-MAC
                   connect callback cannot reuse the preceding generation. */
                setup_portal_client_auth_connected(&state, mac_a, true);
                assert(state.association_generation == 7);
                assert(!setup_portal_client_auth_allows(&state, ip_a));
                assert(setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, true));
                assert(setup_portal_client_auth_allows(&state, ip_a));

                /* A new association invalidates the old lease immediately. */
                setup_portal_client_auth_connected(&state, mac_b, true);
                assert(state.association_generation == 8);
                assert(!setup_portal_client_auth_allows(&state, ip_a));
                assert(!setup_portal_client_auth_ip_assigned(&state, mac_a, ip_a, false));
                assert(setup_portal_client_auth_ip_assigned(&state, mac_b, ip_b, true));
                assert(setup_portal_client_auth_allows(&state, ip_b));
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(FIRMWARE / "main"),
            str(auth_source),
            str(harness),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)

    assert "IP_EVENT_AP_STAIPASSIGNED" in ZONE
    assert "setup_portal_handle_ap_station_ip_assigned" in PORTAL_HEADER


def test_setup_socket_guard_handles_ipv4_and_dual_stack_without_cross_interface_access(
    tmp_path: Path,
) -> None:
    guard_source = FIRMWARE / "main" / "setup_portal_socket_guard.c"
    harness = tmp_path / "portal_socket_guard_harness.c"
    executable = tmp_path / "portal_socket_guard_harness"
    harness.write_text(
        textwrap.dedent(
            """
            #include <arpa/inet.h>
            #include <assert.h>
            #include <stdint.h>
            #include <string.h>
            #include "setup_portal_socket_guard.h"

            static struct sockaddr_in ipv4(const char *text) {
                struct sockaddr_in address = {0};
                address.sin_family = AF_INET;
                assert(inet_pton(AF_INET, text, &address.sin_addr) == 1);
                return address;
            }

            static struct sockaddr_in6 ipv6(const char *text) {
                struct sockaddr_in6 address = {0};
                address.sin6_family = AF_INET6;
                assert(inet_pton(AF_INET6, text, &address.sin6_addr) == 1);
                return address;
            }

            int main(void) {
                struct sockaddr_in local4 = ipv4("192.168.254.1");
                struct sockaddr_in peer4 = ipv4("192.168.254.2");
                struct sockaddr_in wrong_local4 = ipv4("192.168.100.21");
                struct sockaddr_in wrong_peer4 = ipv4("192.168.253.2");
                struct sockaddr_in6 local6 = ipv6("::ffff:192.168.254.1");
                struct sockaddr_in6 peer6 = ipv6("::ffff:192.168.254.2");
                struct sockaddr_in6 pure6 = ipv6("fd00::2");
                uint32_t peer_ip = 0;

                assert(setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local4, sizeof(local4),
                    (struct sockaddr *)&peer4, sizeof(peer4), &peer_ip));
                assert(peer_ip == peer4.sin_addr.s_addr);

                peer_ip = 0;
                assert(setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local6, sizeof(local6),
                    (struct sockaddr *)&peer6, sizeof(peer6), &peer_ip));
                assert(peer_ip == peer4.sin_addr.s_addr);

                assert(setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local4, sizeof(local4),
                    (struct sockaddr *)&peer6, sizeof(peer6), NULL));
                assert(!setup_portal_socket_guard_allows(
                    (struct sockaddr *)&wrong_local4, sizeof(wrong_local4),
                    (struct sockaddr *)&peer4, sizeof(peer4), NULL));
                peer_ip = UINT32_MAX;
                assert(!setup_portal_socket_guard_allows(
                    (struct sockaddr *)&wrong_local4, sizeof(wrong_local4),
                    (struct sockaddr *)&peer4, sizeof(peer4), &peer_ip));
                assert(peer_ip == 0);
                assert(!setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local4, sizeof(local4),
                    (struct sockaddr *)&wrong_peer4, sizeof(wrong_peer4), NULL));
                assert(!setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local6, sizeof(local6),
                    (struct sockaddr *)&pure6, sizeof(pure6), NULL));
                assert(!setup_portal_socket_guard_allows(
                    (struct sockaddr *)&local4, sizeof(local4) - 1,
                    (struct sockaddr *)&peer4, sizeof(peer4), NULL));
                assert(!setup_portal_socket_guard_allows(
                    NULL, 0, (struct sockaddr *)&peer4, sizeof(peer4), NULL));
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(FIRMWARE / "main"),
            str(guard_source),
            str(harness),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)


def test_recovery_and_manual_activation_windows_match_the_operating_contract() -> None:
    assert "PORTAL_RECOVERY_MS     (2 * 60 * 1000)" in PORTAL
    assert "PORTAL_BUTTON_HOLD_MS  5000" in PORTAL
    assert "PORTAL_MANUAL_IDLE_MS  (10 * 60 * 1000)" in PORTAL
    assert "PORTAL_STABLE_CLOSE_MS (30 * 1000)" in PORTAL
    assert "PORTAL_STA_RETRY_MS    5000" in PORTAL
    assert "PORTAL_ACTIVE_STA_RETRY_MS (60 * 1000)" in PORTAL
    assert "PORTAL_PENDING_ROLLBACK_MS (15 * 60 * 1000)" in PORTAL
    assert "esp_wifi_connect()" in PORTAL
    assert "esp_ota_mark_app_invalid_rollback_and_reboot()" in PORTAL
    assert "running_app_pending_verify()" in PORTAL
    assert "GPIO_NUM_0" in PORTAL
    assert "ota_manager_busy()" in PORTAL


def test_active_portal_preserves_softap_beacon_airtime():
    controller = PORTAL[
        PORTAL.index("static void controller_task") :
        PORTAL.index("esp_err_t setup_portal_prepare")
    ]
    disconnect = PORTAL[
        PORTAL.index("bool setup_portal_handle_sta_disconnected") :
        PORTAL.index("bool setup_portal_handle_sta_got_ip")
    ]
    assert "s_last_sta_retry_ms = s_last_activity_ms" in PORTAL
    assert "s_active\n            ? PORTAL_ACTIVE_STA_RETRY_MS" in controller
    assert "current - s_last_sta_retry_ms >= retry_interval" in controller
    assert "if (s_active) return true;" in disconnect


def test_user_scan_preempts_only_a_disconnected_recovery_probe() -> None:
    networks = PORTAL[
        PORTAL.index("static esp_err_t networks_handler") :
        PORTAL.index("static esp_err_t status_handler")
    ]
    assert "if (s_disconnected_since_ms)" in networks
    assert "s_last_sta_retry_ms = now_ms()" in networks
    assert "esp_wifi_disconnect()" in networks
    assert "esp_wifi_scan_start(&scan, true)" in networks
    assert 'cJSON_AddStringToObject(network, "security"' in networks
    assert "WIFI_AUTH_WPA_WPA2_PSK" in PORTAL


def test_state_life_portal_exposes_complete_single_password_flow() -> None:
    for text in (
        "STATE LIFE INSURANCE CORPORATION OF PAKISTAN",
        "Zone Lite Wi-Fi setup",
        "Choose Wi-Fi",
        "Test &amp; save",
        "Scan again",
        "Hidden network",
        "Wi-Fi password",
        "Test connection and save",
    ):
        assert text in ASSETS
    assert "toggle-password" in ASSETS
    assert "setup-password" not in ASSETS
    assert "setup_password:" not in ASSETS
    assert "JSON.stringify({csrf:$('#csrf').value,ssid,password})" in ASSETS


def test_only_network_selection_routes_are_exposed() -> None:
    for route in ("/api/networks", "/api/status", "/api/network"):
        assert f'.uri = "{route}"' in PORTAL
    for forbidden in ("/api/log", "/api/user", "/api/attendance", "/api/ota", "/api/file", "/api/zkt"):
        assert forbidden not in PORTAL
    assert "No attendance, user, device-control, or update functions are exposed" in ASSETS
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
    assert "wipe_json_password" in PORTAL


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
