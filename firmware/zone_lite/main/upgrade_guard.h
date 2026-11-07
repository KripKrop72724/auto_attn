#pragma once
#include "durable_queue.h"
#include <string.h>

#define UG_CAPABILITY_VERSION 1U
#define UG_ALL_QUEUE_READERS 0x3fU
#define UG_COMPAT_VERSION "2.5.4"
#define UG_CANDIDATE_VERSION "2.6.0"
#define UG_DIRECT_VERSION "2.8.3"
bool ug_direct_predecessor_matches(const char *version, const uint8_t digest[32]);
/* ZKT 2.8.1: the other OTA slot is a rollback target only while the
 * bootloader could select it. A VALID image whose other slot holds no
 * selectable entry (a rollback or an interrupted download left it INVALID,
 * ABORTED or without an entry) has no rollback target to protect. */
typedef enum { UG_OTA_UNKNOWN = 0, UG_OTA_NO_ENTRY, UG_OTA_NEW, UG_OTA_PENDING_VERIFY,
               UG_OTA_VALID, UG_OTA_INVALID, UG_OTA_ABORTED, UG_OTA_UNDEFINED } ug_ota_state_t;
typedef enum { UG_SLOT_UNQUALIFIED = 0, UG_SLOT_PREDECESSOR, UG_SLOT_NONE_NO_ENTRY,
               UG_SLOT_NONE_INVALID, UG_SLOT_NONE_ABORTED, UG_SLOT_NONE_PENDING_VERIFY } ug_slot_verdict_t;
ug_slot_verdict_t ug_direct_slot_verdict(ug_ota_state_t running, ug_ota_state_t other, bool qualified);
const char *ug_slot_verdict_name(ug_slot_verdict_t verdict);
const char *ug_ota_state_name(ug_ota_state_t state);
typedef struct {
    uint32_t version, queue_format, reader_mask;
    char application_version[32];
    uint8_t application_digest[32];
    uint32_t crc;
} ug_capability_t;
bool ug_capability_valid(const ug_capability_t *capability);
bool ug_predecessor_matches(const ug_capability_t *capability, const char *version,
                            const uint8_t digest[32], bool secure_boot, bool previous_ota_slot);
