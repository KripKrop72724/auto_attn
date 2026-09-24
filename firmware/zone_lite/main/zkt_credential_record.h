#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    bool pin_present;
    bool card_present;
} zkt_credential_fields_t;

/* Known ZKT 28/72-byte user formats only. This does not address palm or
 * model-specific authentication data outside the user record. */
bool zkt_credential_record_clear(
    const uint8_t *original,
    size_t record_size,
    uint8_t *cleared,
    zkt_credential_fields_t *fields);

/* Verify a terminal readback preserved every byte outside PIN and card. */
bool zkt_credential_record_matches_clear(
    const uint8_t *original,
    const uint8_t *readback,
    size_t record_size);
