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
    SRCS "lease_guard.c" "file_transaction.c" "worker_retry.c" "storage_upgrade.c" "upgrade_guard.c" "evidence_receipt.c" "storage_budget.c" "uid_cache.c" "delivery_scheduler.c" "legacy_queue.c" "durable_queue.c" "queue_store.c" "reliability.c" "zone_lite.c" "zkt_clock.c" "zkt_credential_record.c" "led_status.c" "zone_config.c" "add_connector.c" "ota_manager.c" "setup_portal.c" "setup_portal_client_auth.c" "setup_portal_socket_guard.c" "setup_portal_assets.c"
    INCLUDE_DIRS "." "${ZONE_LITE_GENERATED_DIR}"
    REQUIRES esp_wifi esp_netif esp_http_server esp_driver_gpio nvs_flash lwip esp_http_client esp_https_ota app_update esp_partition esp-tls esp_app_format json spiffs mbedtls esp_timer led_strip esp_websocket_client
)
target_sources(${COMPONENT_LIB} PRIVATE "zkt_record.c" "zkt_socket_io.c" "add_source_wire.c" "ota_progress_receipt.c" "legacy_storage_health.c")
if(NOT ZONE_LITE_FAMILY STREQUAL "hikvision")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_QUEUE_OWNER=1)
    target_sources(${COMPONENT_LIB} PRIVATE "zkt_journal_codec.c" "zkt_journal_crypto.c" "zkt_journal_store.c"
        "zkt_journal_state.c" "zkt_journal_compat.c" "zkt_reader_platform.c" "zkt_ota_guard.c" "zkt_rollback.c"
        "zkt_storage_mailbox.c" "zkt_storage_owner.c" "zkt_runtime_checkpoint.c" "zkt_lease_store.c" "zkt_catalog_store.c" "zkt_catalog_client.c"
        "zkt_command_id_store.c" "zkt_command_id_client.c" "zkt_hil_reboot.c"
        "zkt_segmented_store.c" "zkt_segmented_client.c" "zkt_legacy_attendance.c"
        "zkt_custody_wire.c" "zkt_custody_receipt.c" "zkt_journal_delivery.c" "zkt_journal_transport.c"
        "zkt_journal_capture.c" "zkt_capture_runtime.c" "zkt_journal_boot.c" "zkt_journal_runtime.c"
        "zkt_journal_diagnostics.c" "zkt_source_boundary_client.c")
endif()

# This compiles the journal runtime. Only the exact bridge role is admitted to
# experimental HIL; the candidate still requires separate cutover qualification.
if(ZONE_LITE_JOURNAL_WRITES)
    if(ZONE_LITE_FAMILY STREQUAL "hikvision")
        message(FATAL_ERROR "The attendance journal writer is ZKT-only")
    endif()
    if(ZONE_LITE_SEGMENTED_WRITES OR ZONE_LITE_DIRECT_LEGACY_UPGRADE)
        message(FATAL_ERROR "Journal images cannot mix legacy storage upgrade modes")
    endif()
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_JOURNAL_WRITES=1)
    if(PROJECT_VER STREQUAL "2.6.16" OR PROJECT_VER STREQUAL "2.6.17" OR PROJECT_VER STREQUAL "2.6.18" OR PROJECT_VER STREQUAL "2.6.19" OR PROJECT_VER STREQUAL "2.6.20")
        target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_JOURNAL_BRIDGE_IMAGE=1 ZJ_BRIDGE_VERSION="${PROJECT_VER}")
        if(NOT PROJECT_VER STREQUAL "2.6.16")
            target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_JOURNAL_REPLACEMENT_BRIDGE=1)
        endif()
    elseif(PROJECT_VER STREQUAL "2.7.0")
        target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_JOURNAL_WRITER_IMAGE=1)
    else()
        message(FATAL_ERROR "Journal storage requires an explicitly versioned reader/writer role")
    endif()
endif()
if(ZONE_LITE_FAMILY STREQUAL "hikvision")
    if(NOT CONFIG_ESP_HTTP_CLIENT_ENABLE_DIGEST_AUTH OR NOT CONFIG_MBEDTLS_DEFAULT_MEM_ALLOC)
        message(FATAL_ERROR "Hikvision requires Digest authentication and PSRAM-capable TLS allocation; regenerate sdkconfig with sdkconfig.hikvision.defaults")
    endif()
    target_sources(${COMPONENT_LIB} PRIVATE "hikvision_clock.c" "hikvision_stream.c" "hikvision_http.c" "hikvision_api.c" "hikvision_commands.c" "hikvision_runtime.c")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_HIKVISION=1)
endif()

# This definition is deliberately absent from ordinary, canary, and nationwide
# builds.  The dedicated Quetta workflow enables it for one exact-MAC HIL image.
if(ZONE_LITE_QUETTA_DIAGNOSTIC)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_QUETTA_DIAGNOSTIC=1)
endif()

# The compatibility image retains legacy writes. The hardening release workflow
# explicitly enables this only after its predecessor-image qualification.
if(ZONE_LITE_SEGMENTED_WRITES)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_SEGMENTED_WRITES=1)
endif()
if(ZONE_LITE_DIRECT_LEGACY_UPGRADE)
    if(ZONE_LITE_SEGMENTED_WRITES OR ZONE_LITE_FAMILY STREQUAL "hikvision")
        message(FATAL_ERROR "Direct ZKT upgrade requires legacy writes and the ZKT family")
    endif()
    if(NOT CONFIG_MBEDTLS_DEFAULT_MEM_ALLOC OR NOT CONFIG_SPIRAM_USE_MALLOC)
        message(FATAL_ERROR "Direct ZKT OTA requires PSRAM-capable TLS buffers after delivery startup")
    endif()
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ZONE_LITE_DIRECT_LEGACY_UPGRADE=1)
endif()
