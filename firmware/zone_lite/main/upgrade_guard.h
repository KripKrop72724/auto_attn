#pragma once
#include "durable_queue.h"
#include <string.h>

#define UG_CAPABILITY_VERSION 1U
#define UG_ALL_QUEUE_READERS 0x3fU
#define UG_COMPAT_VERSION "2.5.4"
#define UG_CANDIDATE_VERSION "2.6.0"
#define UG_DIRECT_VERSION "2.6.8"
bool ug_direct_predecessor_matches(const char *version, const uint8_t digest[32]);
typedef struct {
    uint32_t version, queue_format, reader_mask;
    char application_version[32];
    uint8_t application_digest[32];
    uint32_t crc;
} ug_capability_t;
bool ug_capability_valid(const ug_capability_t *capability);
bool ug_predecessor_matches(const ug_capability_t *capability, const char *version,
                            const uint8_t digest[32], bool secure_boot, bool previous_ota_slot);
