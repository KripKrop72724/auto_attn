#pragma once
#include "zkt_segmented_owner.h"
#include "zone_storage_paths.h"

enum { ZOL_PENDING, ZOL_BLOCKED };
#ifndef ZOL_PENDING_PATH
#define ZOL_PENDING_PATH ZONE_STORAGE_BASE "/pending.jsonl"
#define ZOL_PENDING_BACKUP_PATH ZONE_STORAGE_BASE "/pending.bak"
#define ZOL_PENDING_TEMP_PATH ZONE_STORAGE_BASE "/pending.tmp"
#define ZOL_BLOCKED_PATH ZONE_STORAGE_BASE "/blocked_identity.jsonl"
#define ZOL_BLOCKED_BACKUP_PATH ZONE_STORAGE_BASE "/blocked_recovery.bak"
#define ZOL_BLOCKED_TEMP_PATH ZONE_STORAGE_BASE "/blocked_recovery.tmp"
#endif

/* New-image callers submit copies. Unknown/recovering is never verified empty.
 * Producer admission invalidates the conservative RAM evidence immediately. */
dq_result_t zol_append(unsigned lane, const void *bytes, size_t length, qs_admission_t policy);
bool zol_pending_verified_empty(void);

/* Storage-task adapters; retained names and legacy_queues NVS ABI are unchanged. */
dq_result_t zol_owner_append(unsigned lane, const void *bytes, size_t length, qs_admission_t policy);
dq_result_t zol_owner_peek(unsigned lane, void *bytes, size_t capacity, size_t *length, lq_token_t *token);
dq_result_t zol_owner_settle(unsigned lane, const lq_token_t *token, bool custody);
