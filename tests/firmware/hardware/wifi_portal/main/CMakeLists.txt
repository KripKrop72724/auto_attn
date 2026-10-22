get_filename_component(FLEET_MAIN "${CMAKE_CURRENT_LIST_DIR}/../../../../../firmware/zone_lite/main" ABSOLUTE)
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/test-generated")
idf_component_register(
    SRCS "main.c" "${FLEET_MAIN}/setup_portal.c"
         "${FLEET_MAIN}/setup_portal_client_auth.c"
         "${FLEET_MAIN}/setup_portal_socket_guard.c"
         "${FLEET_MAIN}/setup_portal_assets.c"
    INCLUDE_DIRS "." "${FLEET_MAIN}" "${CMAKE_BINARY_DIR}/test-generated"
    REQUIRES esp_wifi esp_netif esp_http_server esp_driver_gpio nvs_flash
             esp_timer app_update json)
# Disposable test-only AP password. It is NOT the fleet setup secret and this
# harness cannot be signed/published as fleet firmware.
file(WRITE "${CMAKE_BINARY_DIR}/test-generated/setup_password.h"
    "#define ZONE_LITE_SETUP_PASSWORD \"HIL-Spare-Only-2026\"\n#define ZONE_LITE_SETUP_PASSWORD_IS_PRODUCTION 1\n")
