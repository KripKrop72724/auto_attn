#include "zkt_journal_state.h"
#include "zkt_journal_compat.h"
#include "durable_queue.h"
#include <assert.h>
#include <string.h>

typedef struct {
    uint8_t root[ZJ_ROOT_BYTES], retirement[ZJ_CHECKPOINT_BYTES];
    bool root_exists, retirement_exists, proof_exists, absent, uncertain;
    unsigned calls, fail_at, writes, randoms;
} fake_t;
static fake_t fake;
static bool fault(fake_t *f) { return ++f->calls == f->fail_at; }
static int read_blob(void *context, const char *name, uint8_t *out, size_t length)
{
    fake_t *f = context;
    if (fault(f)) return -1;
    if (!strcmp(name, "reader_v1")) {
        assert(length == ZJ_READER_PROOF_BYTES);
        memset(out, 0, length);
        return f->proof_exists ? 1 : 0;
    }
    bool root = !strcmp(name, "root");
    assert(length == (root ? ZJ_ROOT_BYTES : ZJ_CHECKPOINT_BYTES));
    if (!(root ? f->root_exists : f->retirement_exists)) return 0;
    memcpy(out, root ? f->root : f->retirement, length);
    return 1;
}
static bool write_blob(void *context, const char *name, const uint8_t *bytes, size_t length)
{
    fake_t *f = context;
    ++f->writes;
    bool failed = fault(f), root = !strcmp(name, "root");
    if (!failed || f->uncertain) {
        memcpy(root ? f->root : f->retirement, bytes, length);
        if (root) f->root_exists = true;
        else f->retirement_exists = true;
    }
    return !failed;
}
static bool random_bytes(void *context, uint8_t *out, size_t length)
{
    fake_t *f = context;
    ++f->randoms;
    if (fault(f)) return false;
    for (size_t i = 0; i < length; ++i) out[i] = (uint8_t)(i + 1);
    return true;
}
static bool absent(void *context)
{
    fake_t *f = context;
    return !fault(f) && f->absent;
}
static zj_state_port_t port(void)
{
    return (zj_state_port_t){read_blob, write_blob, random_bytes, absent, &fake};
}
static void reset(void)
{
    memset(&fake, 0, sizeof(fake));
    fake.absent = true;
}
int main(void)
{
    zj_state_t state;
    reset();
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
    assert(state.ready && state.limit == 1 && fake.writes == 1 && fake.randoms == 1);
    assert(zj_state_authority(&state) == ZJ_AUTHORITY_LEGACY);
    assert(!zj_state_reserve(&state, 257) && state.ready && fake.writes == 1);
    uint8_t key[32], epoch[16], original_key[32], original_epoch[16];
    assert(zj_state_identity(&state, original_key, original_epoch));
    assert(zj_state_enable_add(&state) == ZJ_OK && zj_state_authority(&state) == ZJ_AUTHORITY_ADD);
    assert(zj_state_enable_add(&state) == ZJ_OK && fake.writes == 2);
    assert(zj_state_reserve(&state, 257));
    assert(!zj_state_reserve(&state, 257) && !zj_state_reserve(&state, 1));
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK && state.limit == 257);
    assert(zj_state_identity(&state, key, epoch));
    assert(!memcmp(key, original_key, 32) && !memcmp(epoch, original_epoch, 16));
    assert(fake.writes == 3 && fake.randoms == 1 && zj_state_authority(&state) == ZJ_AUTHORITY_ADD);
    unsigned writes = fake.writes;
    assert(zj_state_open(&state, port(), "REPLACED-TERMINAL") == ZJ_CORRUPT);
    assert(!state.ready && !zj_state_identity(&state, key, epoch) && writes == fake.writes);
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
    uint8_t original[ZJ_ROOT_BYTES];
    memcpy(original, fake.root, sizeof(original));
    for (unsigned byte = 0; byte < ZJ_ROOT_BYTES; ++byte) {
        fake.root[byte] ^= 1;
        assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT);
        assert(!state.ready && writes == fake.writes);
        memcpy(fake.root, original, sizeof(original));
    }
    reset();
    fake.absent = false;
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT);
    assert(!fake.writes && !fake.randoms);

    reset();
    fake.proof_exists = true;
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT);
    assert(!fake.writes && !fake.randoms);
    reset();
    fake.retirement_exists = true;
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT);
    assert(!fake.writes && !fake.randoms);

    for (unsigned uncertain = 0; uncertain < 2; ++uncertain) {
        /* Write failure may occur before or after the commit. A readback
         * failure can also hide success; none permits sequence allocation. */
        for (unsigned boundary = 1; boundary <= 2; ++boundary) {
            reset();
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            fake.calls = 0; fake.fail_at = boundary; fake.uncertain = uncertain;
            assert(zj_state_enable_add(&state) == ZJ_UNCERTAIN);
            assert(zj_state_authority(&state) == ZJ_AUTHORITY_UNKNOWN);
            assert(!zj_state_reserve(&state, 257));
            fake.fail_at = 0;
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            bool committed = boundary == 2 || uncertain;
            assert(zj_state_authority(&state) == (committed ? ZJ_AUTHORITY_ADD : ZJ_AUTHORITY_LEGACY));
            unsigned before = fake.writes;
            assert(zj_state_enable_add(&state) == ZJ_OK);
            assert(fake.writes == before + (committed ? 0U : 1U));
            assert(zj_state_reserve(&state, 257));
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            assert(zj_state_authority(&state) == ZJ_AUTHORITY_ADD);
        }
        for (unsigned boundary = 1; boundary <= 8; ++boundary) {
            reset();
            fake.fail_at = boundary;
            fake.uncertain = uncertain;
            zj_result_t result = zj_state_open(&state, port(), "TEST-TERMINAL");
            assert(result == ZJ_OK || !state.ready);
            fake.fail_at = 0;
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            assert(zj_state_identity(&state, key, epoch));
        }
        for (unsigned boundary = 1; boundary <= 2; ++boundary) {
            reset();
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            assert(zj_state_enable_add(&state) == ZJ_OK);
            fake.calls = 0;
            fake.fail_at = boundary;
            fake.uncertain = uncertain;
            assert(!zj_state_reserve(&state, 257) && !state.ready);
            assert(!zj_state_reserve(&state, 513));
            fake.fail_at = 0;
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            uint64_t limit = state.limit;
            assert(limit == 1 || limit == 257);
            zj_sequence_t sequence;
            uint64_t allocated;
            assert(zj_sequence_init(&sequence, limit, zj_state_reserve, &state));
            assert(zj_sequence_next(&sequence, &allocated) && allocated == limit);
            assert(allocated < state.limit);

            uint8_t cp[ZJ_CHECKPOINT_BYTES] = {1}, restored[ZJ_CHECKPOINT_BYTES];
            fake.calls = 0;
            fake.fail_at = boundary;
            assert(!zj_state_checkpoint_commit(&state, cp) && !state.ready);
            fake.fail_at = 0;
            assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
            int loaded = zj_state_checkpoint_load(&state, restored);
            assert(loaded == 0 || (loaded == 1 && !memcmp(cp, restored, sizeof(cp))));
        }
    }
    /* Even a checksum-valid root cannot regress authority after reserving
     * capture identities. Retain it for recovery instead of replacing it. */
    reset();
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK);
    assert(zj_state_enable_add(&state) == ZJ_OK && zj_state_reserve(&state, 257));
    fake.root[145] = 0;
    uint32_t crc = dq_crc32(fake.root, ZJ_ROOT_BYTES - 4);
    for (unsigned i = 0; i < 4; ++i) fake.root[ZJ_ROOT_BYTES - 4 + i] = (uint8_t)(crc >> (8 * i));
    writes = fake.writes;
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT && fake.writes == writes);
    zj_state_clear(&state);
    for (size_t i = 0; i < sizeof(state); ++i) assert(!((uint8_t *)&state)[i]);
    return 0;
}
