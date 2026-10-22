/* Physical regression test using the real portal implementation. Test-only
 * NVS records the next scenario; no fleet credentials or attendance code. */
#include <stdio.h>
#include <string.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "ota_manager.h"
#include "setup_portal.h"
#include "zone_config.h"

bool ota_manager_busy(void) { return false; }
bool ota_manager_owns_boot_rollback(void) { return true; }
esp_err_t zone_config_save_wifi(const char *ssid, const char *password)
{
    (void)ssid; (void)password;
    /* This test observes AP startup; it cannot save fleet settings. */
    return ESP_ERR_NOT_SUPPORTED;
}

static void event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_err_t result = esp_wifi_connect();
        printf("HIL_CONNECT_RESULT %s\n", esp_err_to_name(result));
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *e = data;
        printf("HIL_DISCONNECT reason=%u\n", e->reason);
        (void)setup_portal_handle_sta_disconnected();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        (void)setup_portal_handle_sta_got_ip();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        setup_portal_handle_ap_station_connected(((wifi_event_ap_staconnected_t *)data)->mac);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        setup_portal_handle_ap_station_disconnected(((wifi_event_ap_stadisconnected_t *)data)->mac);
    } else if (base == IP_EVENT && id == IP_EVENT_AP_STAIPASSIGNED) {
        ip_event_ap_staipassigned_t *e = data;
        setup_portal_handle_ap_station_ip_assigned(e->mac, e->ip.addr);
    }
}

void app_main(void)
{
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    const uint8_t spare[] = {0x14,0xc1,0x9f,0x46,0xae,0x1c};
    if (memcmp(mac, spare, sizeof(spare))) {
        printf("HIL_REFUSED_WRONG_BOARD\n");
        return;
    }
    ESP_ERROR_CHECK(nvs_flash_init()); /* Never automatically erase NVS. */
    nvs_handle_t test;
    ESP_ERROR_CHECK(nvs_open("hil_wifi", NVS_READWRITE, &test));
    uint8_t scenario = 0;
    (void)nvs_get_u8(test, "scenario", &scenario);
    if (scenario > 1) scenario = 0;
    printf("HIL_CASE %s MAC=14:c1:9f:46:ae:1c\n", scenario ? "RE7" : "EMPTY");
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(setup_portal_prepare(NULL));
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, event, NULL));
    wifi_config_t wifi = {0};
    if (scenario) {
        strcpy((char *)wifi.sta.ssid, "RE7");
        strcpy((char *)wifi.sta.password, "HIL-wrong-password");
    }
    wifi.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(setup_portal_start_controller());
    int64_t started = esp_timer_get_time();
    for (int index = 0; index < 28; ++index) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        printf("HIL_OBSERVE elapsed_ms=%lld ap_active=%d\n",
               (esp_timer_get_time()-started)/1000, setup_portal_active());
        fflush(stdout);
    }
    wifi_config_t ap = {0};
    esp_err_t ap_result = esp_wifi_get_config(WIFI_IF_AP, &ap);
    printf("HIL_RESULT case=%s ap_active=%d ap_config=%s ssid=%s\n",
           scenario ? "RE7" : "EMPTY", setup_portal_active(),
           esp_err_to_name(ap_result), ap_result == ESP_OK ? (char *)ap.ap.ssid : "unavailable");
    fflush(stdout);
    if (!scenario) {
        ESP_ERROR_CHECK(nvs_set_u8(test, "scenario", 1));
        ESP_ERROR_CHECK(nvs_commit(test));
        nvs_close(test);
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_restart();
    }
    nvs_close(test);
    printf("HIL_FINISHED no_secure_boot_or_attendance_claim\n");
}
