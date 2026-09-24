#include "zkt_credential_record.h"

#include <string.h>

static bool credential_spans(
    size_t size,
    size_t *pin_end,
    size_t *card_start)
{
    if (!pin_end || !card_start) return false;
    if (size == 28) {
        *pin_end = 8;
        *card_start = 16;
        return true;
    }
    if (size == 72) {
        *pin_end = 11;
        *card_start = 35;
        return true;
    }
    return false;
}

bool zkt_credential_record_clear(
    const uint8_t *original,
    size_t record_size,
    uint8_t *cleared,
    zkt_credential_fields_t *fields)
{
    size_t pin_end = 0;
    size_t card_start = 0;
    if (!original || !cleared || !fields ||
        !credential_spans(record_size, &pin_end, &card_start)) return false;
    fields->pin_present = false;
    fields->card_present = false;
    for (size_t i = 3; i < pin_end; ++i) {
        if (original[i] != 0) fields->pin_present = true;
    }
    for (size_t i = card_start; i < card_start + 4; ++i) {
        if (original[i] != 0) fields->card_present = true;
    }
    memcpy(cleared, original, record_size);
    memset(cleared + 3, 0, pin_end - 3);
    memset(cleared + card_start, 0, 4);
    return true;
}

bool zkt_credential_record_matches_clear(
    const uint8_t *original,
    const uint8_t *readback,
    size_t record_size)
{
    size_t pin_end = 0;
    size_t card_start = 0;
    if (!original || !readback ||
        !credential_spans(record_size, &pin_end, &card_start)) return false;
    for (size_t i = 0; i < record_size; ++i) {
        const bool credential_byte = (i >= 3 && i < pin_end) ||
            (i >= card_start && i < card_start + 4);
        if (credential_byte) {
            if (readback[i] != 0) return false;
        } else if (readback[i] != original[i]) {
            return false;
        }
    }
    return true;
}
