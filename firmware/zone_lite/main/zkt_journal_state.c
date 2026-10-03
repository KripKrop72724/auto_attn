#include "zkt_journal_state.h"
#include "zkt_journal_compat.h"
#include "durable_queue.h"
#include <string.h>

static void erase(void *data, size_t length)
{
    volatile uint8_t *bytes = data;
    while (length--) *bytes++ = 0;
}
static uint64_t get64(const uint8_t *bytes)
{
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)bytes[i] << (8 * i);
    return value;
}
static void put64(uint8_t *bytes, uint64_t value)
{
    for (unsigned i = 0; i < 8; ++i) bytes[i] = (uint8_t)(value >> (8 * i));
}
static bool present(const uint8_t *bytes, size_t length)
{
    uint8_t found = 0;
    for (size_t i = 0; i < length; ++i) found |= bytes[i];
    return found != 0;
}
static void checksum(uint8_t bytes[ZJ_ROOT_BYTES])
{
    uint32_t crc = dq_crc32(bytes, ZJ_ROOT_BYTES - 4);
    for (unsigned i = 0; i < 4; ++i) bytes[ZJ_ROOT_BYTES - 4 + i] = (uint8_t)(crc >> (8 * i));
}
static bool valid(const uint8_t bytes[ZJ_ROOT_BYTES], const char *serial)
{
    uint8_t canonical[ZJ_ROOT_BYTES];
    memcpy(canonical, bytes, sizeof(canonical));
    checksum(canonical);
    uint64_t limit = get64(bytes + 8);
    bool ok = !memcmp(bytes, "ZJROOT01", 8) && limit && limit <= (uint64_t)ZJ_SEQUENCE_MAX + 1 &&
        present(bytes + 16, 32) && present(bytes + 48, 16) &&
        !memcmp(bytes, canonical, sizeof(canonical)) && !present(bytes + 145, 11);
    char expected[81] = {0};
    size_t length = strlen(serial);
    if (!length || length >= sizeof(expected)) ok = false;
    else memcpy(expected, serial, length);
    ok = ok && !memcmp(bytes + 64, expected, sizeof(expected));
    erase(canonical, sizeof(canonical));
    return ok;
}
static bool save(zj_state_t *state, const char *name, const uint8_t *bytes, size_t length)
{
    uint8_t verify[ZJ_ROOT_BYTES] = {0};
    bool ok = length <= sizeof(verify) && state->port.write(state->port.context, name, bytes, length) &&
        state->port.read(state->port.context, name, verify, length) == 1 && !memcmp(bytes, verify, length);
    erase(verify, sizeof(verify));
    if (!ok) state->ready = false;
    return ok;
}

zj_result_t zj_state_open(zj_state_t *state, zj_state_port_t port, const char *serial)
{
    if (!state || !serial || !port.read || !port.write || !port.random || !port.journal_absent)
        return ZJ_INVALID;
    zj_state_clear(state);
    state->port = port;
    /* Reuse the same strict terminal identifier grammar as the file format. */
    zj_metadata_t metadata = {.segment_id = 1, .capture_epoch = {1},
        .decoder_profile = "state", .decoder_version = "1"};
    if (!strlen(serial) || strlen(serial) >= sizeof(metadata.terminal_serial)) return ZJ_INVALID;
    memcpy(metadata.terminal_serial, serial, strlen(serial));
    uint8_t encoded[ZJ_META_BYTES];
    if (!zj_metadata_encode(&metadata, encoded)) return ZJ_INVALID;
    int loaded = port.read(port.context, "root", state->root, sizeof(state->root));
    if (loaded < 0) return ZJ_IO;
    if (loaded == 0) {
        /* Orphaned retirement state is also evidence that a root once existed.
         * Empty segments alone cannot authorize replacing a missing key. */
        uint8_t checkpoint[ZJ_CHECKPOINT_BYTES];
        int previous = port.read(port.context, "retirement", checkpoint, sizeof(checkpoint));
        erase(checkpoint, sizeof(checkpoint));
        if (previous < 0) return ZJ_IO;
        if (previous != 0) return ZJ_CORRUPT;
        uint8_t reader_proof[ZJ_READER_PROOF_BYTES];
        previous = port.read(port.context, "reader_v1", reader_proof, sizeof(reader_proof));
        erase(reader_proof, sizeof(reader_proof));
        if (previous < 0) return ZJ_IO;
        if (previous != 0 || !port.journal_absent(port.context)) return ZJ_CORRUPT;
        memset(state->root, 0, sizeof(state->root));
        memcpy(state->root, "ZJROOT01", 8);
        put64(state->root + 8, 1);
        memcpy(state->root + 64, serial, strlen(serial));
        if (!port.random(port.context, state->root + 16, 48)) return ZJ_IO;
        checksum(state->root);
        if (!valid(state->root, serial)) return ZJ_CORRUPT;
        if (!save(state, "root", state->root, sizeof(state->root))) return ZJ_UNCERTAIN;
    }
    if (!valid(state->root, serial)) return ZJ_CORRUPT;
    state->limit = get64(state->root + 8);
    state->ready = true;
    return ZJ_OK;
}

bool zj_state_identity(const zj_state_t *state, uint8_t master[32], uint8_t epoch[16])
{
    if (!state || !master || !epoch || !state->ready) return false;
    memcpy(master, state->root + 16, 32);
    memcpy(epoch, state->root + 48, 16);
    return true;
}
bool zj_state_reserve(void *context, uint64_t exclusive_limit)
{
    zj_state_t *state = context;
    if (!state || !state->ready || exclusive_limit <= state->limit ||
        exclusive_limit > (uint64_t)ZJ_SEQUENCE_MAX + 1) return false;
    uint8_t updated[ZJ_ROOT_BYTES];
    memcpy(updated, state->root, sizeof(updated));
    put64(updated + 8, exclusive_limit);
    checksum(updated);
    bool ok = save(state, "root", updated, sizeof(updated));
    if (ok) {
        memcpy(state->root, updated, sizeof(updated));
        state->limit = exclusive_limit;
    }
    erase(updated, sizeof(updated));
    return ok;
}
int zj_state_checkpoint_load(void *context, uint8_t checkpoint[ZJ_CHECKPOINT_BYTES])
{
    zj_state_t *state = context;
    if (!state || !state->ready || !checkpoint) return -1;
    return state->port.read(state->port.context, "retirement", checkpoint, ZJ_CHECKPOINT_BYTES);
}
bool zj_state_checkpoint_commit(void *context, const uint8_t checkpoint[ZJ_CHECKPOINT_BYTES])
{
    zj_state_t *state = context;
    return state && state->ready && checkpoint && save(state, "retirement", checkpoint, ZJ_CHECKPOINT_BYTES);
}
void zj_state_clear(zj_state_t *state)
{
    if (state) erase(state, sizeof(*state));
}
