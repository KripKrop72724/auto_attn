#pragma once
#include <stdint.h>

/* Single OTA-task caller, before esp_https_ota_begin (including resumed OTA).
 * NULL permits this local install edge; a stable error refuses before erase.
 * Signature, exact artifact, source, capacity and HIL gates remain separate.
 * No filesystem or terminal lock is held on return or during a network call. */
const char *zj_ota_before_download(uint32_t address, uint32_t size, const char *version);
