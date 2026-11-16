idf_component_register(
    SRCS "board_test_main.c" "sl_out.c" "mem_test.c" "flash_test.c" "radio_test.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_driver_tsens esp_wifi esp_netif esp_event nvs_flash spi_flash
                  esp_partition esp_psram esp_mm esp_rom esp_hw_support esp_system
                  esp_timer log freertos soc)
