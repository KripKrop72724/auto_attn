#pragma once
#include "zkt_factory_trial.h"
#include "cJSON.h"

/* Exact22 only. Other images compile no factory trial implementation. */
void zf_platform_prepare(const char *deployment_id, const char *reader_digest);
bool zf_platform_storage_check(void);
bool zf_platform_startup_allowed(void);
const char *zf_platform_error(void);
bool zf_platform_revoke(void);
bool zf_platform_pending_fallback(void);
/* OTA coordinator only, after terminal session cleanup and acknowledged
 * storage-owner quiescence. Commits intent before selecting the factory;
 * no network, queue changes, image writes or forced OTA-data erase. */
bool zf_platform_select_factory(void);
bool zf_platform_proof(zf_proof_t *out);
bool zf_platform_add_proof(cJSON *root);
