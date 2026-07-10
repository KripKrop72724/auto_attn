idf_component_register(
    SRCS "zone_lite.c" "led_status.c" "add_connector.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif nvs_flash lwip esp_http_client esp-tls json spiffs mbedtls esp_timer led_strip esp_websocket_client
)
