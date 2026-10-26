#include "zkt_legacy_evidence.h"

#include <stdio.h>
#include <string.h>

#include "durable_queue.h"
#include "storage_repair_plan.h"

static uint64_t get64(const uint8_t *bytes)
{
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)bytes[i] << (8 * i);
    return value;
}

static bool any(const uint8_t *bytes, size_t length)
{
    uint8_t found = 0;
    for (size_t i = 0; i < length; ++i) found |= bytes[i];
    return found != 0;
}

/* The layout zkt_journal_state.c writes and checks (valid()): "ZJROOT01",
 * the sequence limit, the 32-byte key and 16-byte epoch, the terminal serial
 * NUL-padded to 81 bytes, the authority byte (1 = ADD), ten zero bytes and
 * the CRC-32 of the first 156 bytes. The serial is not compared: this image
 * does not need to know the bridge's terminal to read who held authority. */
zle_root_t zle_root_classify(const uint8_t *bytes, size_t length)
{
    if (!bytes || length != ZLE_ROOT_BYTES) return ZLE_ROOT_INVALID;
    uint32_t crc = 0;
    for (unsigned i = 0; i < 4; ++i) crc |= (uint32_t)bytes[ZLE_ROOT_BYTES - 4 + i] << (8 * i);
    if (memcmp(bytes, "ZJROOT01", 8) || crc != dq_crc32(bytes, ZLE_ROOT_BYTES - 4)) return ZLE_ROOT_INVALID;
    uint64_t limit = get64(bytes + 8);
    if (!limit || limit > (uint64_t)INT64_MAX + 1 || !any(bytes + 16, 32) || !any(bytes + 48, 16) ||
        bytes[145] > 1 || any(bytes + 146, 10))
        return ZLE_ROOT_INVALID;
    const uint8_t *serial = bytes + 64;
    size_t serial_length = strnlen((const char *)serial, 81);
    if (!serial_length || serial_length > 80 || any(serial + serial_length, 81 - serial_length))
        return ZLE_ROOT_INVALID;
    if (bytes[145] == 1) return ZLE_ROOT_ADD;
    /* Legacy authority can never reserve a sequence: its limit stays 1. */
    return limit == 1 ? ZLE_ROOT_LEGACY : ZLE_ROOT_INVALID;
}

void zle_note_key(zle_inventory_t *inventory, const char *key)
{
    if (!inventory) return;
    ++inventory->keys;
    if (key && !strcmp(key, "reader_v1")) inventory->reader_proof = true;
    else if (!key || strcmp(key, "root")) ++inventory->unknown_keys;
}

void zle_note_file(zle_inventory_t *inventory, const char *name, uint64_t size, bool read_ok, uint64_t read_bytes)
{
    if (!inventory) return;
    ++inventory->files;
    inventory->bytes += size > UINT32_MAX - inventory->bytes ? UINT32_MAX - inventory->bytes : (uint32_t)size;
    char object[SRP_NAME_BYTES + 1];
    int written = snprintf(object, sizeof(object), "/%s", name ? name : "");
    srp_kind_t kind = srp_classify(object);
    if (written <= 0 || (size_t)written >= sizeof(object) || kind.cls != SRP_CLASS_EVIDENCE ||
        !(kind.flags & SRP_SEGMENT) || strncmp(object, "/zktj", 5))
        ++inventory->not_segment;
    if (size > ZLE_FILE_BYTES_MAX) ++inventory->oversized;
    else if (!read_ok || read_bytes != size) ++inventory->unreadable;
}

static zle_verdict_t verdict(zle_state_t state, const char *error, const char *reason)
{
    return (zle_verdict_t){.state = state, .error = error, .reason = reason};
}

zle_verdict_t zle_decide(const zle_inventory_t *in)
{
    if (!in) return verdict(ZLE_REFUSED, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "NO_INVENTORY");
    /* Unreadable facts keep their pre-2.7.7 refusals (the guard retries a
     * busy store; an NVS error was never permission). */
    if (in->namespace_present && (!in->namespace_listed || in->root == ZLE_ROOT_UNREADABLE))
        return verdict(ZLE_REFUSED, "JOURNAL_READER_PROOF_UNAVAILABLE", "NVS_UNREADABLE");
    if (!in->store_listed) return verdict(ZLE_REFUSED, "JOURNAL_OTA_STORAGE_UNAVAILABLE", "STORE_UNREADABLE");
    if (!in->namespace_present && !in->files) return verdict(ZLE_NONE, NULL, "NONE");
    const char *refused = NULL;
    if (!in->runtime_disabled) refused = "JOURNAL_RUNTIME_ACTIVE";
    else if (in->owner_started) refused = "JOURNAL_OWNER_STARTED";
    else if (in->namespace_present && in->root == ZLE_ROOT_ADD) refused = "ADD_AUTHORITY";
    else if (in->namespace_present && in->root == ZLE_ROOT_INVALID) refused = "ROOT_INVALID";
    else if (in->unknown_keys) refused = "NAMESPACE_KEYS";
    else if (in->files && (!in->namespace_present || in->root != ZLE_ROOT_LEGACY)) refused = "FILES_WITHOUT_ROOT";
    else if (in->files > ZLE_FILES_MAX) refused = "FILE_LIMIT";
    else if (in->not_segment) refused = "FILE_NOT_SEGMENT";
    else if (in->oversized) refused = "FILE_OVERSIZED";
    else if (in->unreadable) refused = "FILE_UNREADABLE";
    if (refused) return verdict(ZLE_REFUSED, "JOURNAL_LEGACY_EVIDENCE_PRESENT", refused);
    return verdict(ZLE_ACCEPTED, NULL, "BRIDGE_LEFTOVERS");
}

const char *zle_root_name(zle_root_t root)
{
    switch (root) {
    case ZLE_ROOT_ABSENT: return "ABSENT";
    case ZLE_ROOT_LEGACY: return "LEGACY";
    case ZLE_ROOT_ADD: return "ADD";
    case ZLE_ROOT_INVALID: return "INVALID";
    case ZLE_ROOT_UNREADABLE: return "UNREADABLE";
    }
    return "UNKNOWN";
}

const char *zle_state_name(zle_state_t state)
{
    switch (state) {
    case ZLE_NONE: return "NONE";
    case ZLE_ACCEPTED: return "ACCEPTED";
    case ZLE_REFUSED: return "REFUSED";
    }
    return "UNKNOWN";
}
