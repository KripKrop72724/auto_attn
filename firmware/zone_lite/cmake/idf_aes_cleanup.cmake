# Replace exactly one source on the actual SDK crypto target. No global SDK
# mutation, link interposition, crypto-mode change or allocation-policy change.
if(NOT TARGET mbedcrypto)
    message(FATAL_ERROR "The reviewed AES cleanup requires the SDK mbedcrypto target")
endif()
set(_aes_original "$ENV{IDF_PATH}/components/mbedtls/port/aes/dma/esp_aes_dma_core.c")
set(_aes_generated "${CMAKE_BINARY_DIR}/generated/esp_aes_dma_core_cleanup.c")
set(_aes_patch "${CMAKE_CURRENT_LIST_DIR}/patch_idf_aes.py")
execute_process(COMMAND "${PYTHON}" "${_aes_patch}" "${_aes_original}" "${_aes_generated}"
    RESULT_VARIABLE _aes_result OUTPUT_QUIET ERROR_VARIABLE _aes_error)
if(NOT _aes_result EQUAL 0)
    message(FATAL_ERROR "Pinned AES cleanup generation failed: ${_aes_error}")
endif()
get_target_property(_aes_sources mbedcrypto SOURCES)
set(_aes_matches 0)
foreach(_aes_source IN LISTS _aes_sources)
    if(_aes_source STREQUAL _aes_original)
        math(EXPR _aes_matches "${_aes_matches} + 1")
    endif()
endforeach()
if(NOT _aes_matches EQUAL 1)
    message(FATAL_ERROR "The reviewed AES cleanup requires exactly one SDK DMA source")
endif()
list(REMOVE_ITEM _aes_sources "${_aes_original}")
list(APPEND _aes_sources "${_aes_generated}")
set_property(TARGET mbedcrypto PROPERTY SOURCES "${_aes_sources}")
# IDF normalizes build-directory debug paths, but its macro maps cover only the
# project and SDK roots. This generated source also uses __FILE__ in assertions.
# Apply the build map last, in the crypto target's directory scope, so repeated
# builds retain the same diagnostic filename without embedding their output path.
set_property(SOURCE "${_aes_generated}" TARGET_DIRECTORY mbedcrypto APPEND PROPERTY
    COMPILE_OPTIONS "-fmacro-prefix-map=${CMAKE_BINARY_DIR}=/IDF_BUILD")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_aes_original}" "${_aes_patch}")
message(STATUS "Applied reviewed ESP-IDF 5.5.3 AES output-allocation cleanup")
