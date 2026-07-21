idf_component_register(
    SRCS "zone_lite.c" "led_status.c" "zone_config.c" "add_connector.c" "ota_manager.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif nvs_flash lwip esp_http_client esp_https_ota app_update esp_partition esp-tls esp_app_format json spiffs mbedtls esp_timer led_strip esp_websocket_client
)
