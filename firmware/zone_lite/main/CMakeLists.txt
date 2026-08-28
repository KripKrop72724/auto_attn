set(ZONE_LITE_SETUP_PASSWORD "$ENV{ZONE_LITE_SETUP_PASSWORD}")
if(ZONE_LITE_SETUP_PASSWORD STREQUAL "")
    set(ZONE_LITE_SETUP_PASSWORD "SLIC-CI-ONLY")
    set(ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 0)
else()
    string(LENGTH "${ZONE_LITE_SETUP_PASSWORD}" ZONE_LITE_SETUP_PASSWORD_LENGTH)
    if(ZONE_LITE_SETUP_PASSWORD_LENGTH LESS 8 OR ZONE_LITE_SETUP_PASSWORD_LENGTH GREATER 63)
        message(FATAL_ERROR "ZONE_LITE_SETUP_PASSWORD must contain 8-63 characters")
    endif()
    set(ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 1)
endif()
set(ZONE_LITE_GENERATED_DIR "${CMAKE_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${ZONE_LITE_GENERATED_DIR}")
configure_file("${CMAKE_CURRENT_LIST_DIR}/setup_password.h.in" "${ZONE_LITE_GENERATED_DIR}/setup_password.h" @ONLY)

idf_component_register(
    SRCS "zone_lite.c" "led_status.c" "zone_config.c" "add_connector.c" "ota_manager.c" "setup_portal.c" "setup_portal_client_auth.c" "setup_portal_socket_guard.c" "setup_portal_assets.c"
    INCLUDE_DIRS "." "${ZONE_LITE_GENERATED_DIR}"
    REQUIRES esp_wifi esp_netif esp_http_server esp_driver_gpio nvs_flash lwip esp_http_client esp_https_ota app_update esp_partition esp-tls esp_app_format json spiffs mbedtls esp_timer led_strip esp_websocket_client
)

# This definition is deliberately absent from ordinary, canary, and nationwide
# builds.  The dedicated Quetta workflow enables it for one exact-MAC HIL image.
if(ZONE_LITE_QUETTA_DIAGNOSTIC)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_QUETTA_DIAGNOSTIC=1)
endif()
