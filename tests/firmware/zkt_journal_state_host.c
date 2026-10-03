#include "zkt_journal_state.h"
#include <assert.h>
#include <string.h>

typedef struct {
    uint8_t root[ZJ_ROOT_BYTES], retirement[ZJ_CHECKPOINT_BYTES];
    bool root_exists, retirement_exists, absent, uncertain;
    unsigned calls, fail_at, writes, randoms;
} fake_t;
static fake_t fake;
static bool fault(fake_t *f) { return ++f->calls == f->fail_at; }
static int read_blob(void *context, const char *name, uint8_t *out, size_t length)
{
    fake_t *f = context;
    if (fault(f)) return -1;
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
    uint8_t key[32], epoch[16], original_key[32], original_epoch[16];
    assert(zj_state_identity(&state, original_key, original_epoch));
    assert(zj_state_reserve(&state, 257));
    assert(!zj_state_reserve(&state, 257) && !zj_state_reserve(&state, 1));
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_OK && state.limit == 257);
    assert(zj_state_identity(&state, key, epoch));
    assert(!memcmp(key, original_key, 32) && !memcmp(epoch, original_epoch, 16));
    assert(fake.writes == 2 && fake.randoms == 1);
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
    fake.absent = true;
    fake.retirement_exists = true;
    assert(zj_state_open(&state, port(), "TEST-TERMINAL") == ZJ_CORRUPT);
    assert(!fake.writes && !fake.randoms);

    for (unsigned uncertain = 0; uncertain < 2; ++uncertain) {
        for (unsigned boundary = 1; boundary <= 7; ++boundary) {
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
    zj_state_clear(&state);
    for (size_t i = 0; i < sizeof(state); ++i) assert(!((uint8_t *)&state)[i]);
    return 0;
}
