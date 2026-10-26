#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Single OTA-task caller, before esp_https_ota_begin (including resumed OTA).
 * NULL permits this local install edge; a stable error refuses before erase.
 * Signature, exact artifact, source, capacity and HIL gates remain separate.
 * No filesystem or terminal lock is held on return or during a network call. */
const char *zj_ota_before_download(uint32_t address, uint32_t size, const char *version);

/* ZKT 2.7.7, storage-repair builds: the OTA precheck for this image's own
 * version (the floor and the legacy-evidence check), read once per boot. */
bool zj_ota_precheck_cached(void);

/* ZKT 2.7.7, storage-repair builds: the last bridge-era journal inventory the
 * guard took (zkt_legacy_evidence.h) as storage.journal_evidence; nothing
 * before the guard first ran. False only when the object cannot be built. */
struct cJSON;
bool zj_ota_append_legacy_evidence(struct cJSON *storage);
