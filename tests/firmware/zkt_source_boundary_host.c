#include "zkt_source_boundary.h"
#include <assert.h>

static uint8_t durable[ZSB_BYTES];
static bool present, uncertain;
static unsigned calls, fail_at, writes;
static int read_blob(void *unused, const char *key, uint8_t *out, size_t length)
{
    (void)unused; assert(!strcmp(key, "source_v1") && length == ZSB_BYTES);
    if (++calls == fail_at) return -1;
    if (!present) return 0;
    memcpy(out, durable, length); return 1;
}
static bool write_blob(void *unused, const char *key, const uint8_t *in, size_t length)
{
    (void)unused; assert(!strcmp(key, "source_v1") && length == ZSB_BYTES);
    ++writes;
    bool failed = ++calls == fail_at;
    if (!failed || uncertain) { memcpy(durable, in, length); present = true; }
    return !failed;
}
int main(void)
{
    uint32_t size;
    for (unsigned bytes = 8; bytes <= 40; bytes += 8) {
        bool supported = bytes == 8 || bytes == 16 || bytes == 40;
        assert(zsb_source_size(200000, 200000 * bytes + 4, 200000 * bytes, &size) == supported);
        assert(size == (supported ? bytes : 0));
    }
    assert(zsb_source_size(0, 4, 0, &size) && !size);
    assert(!zsb_source_size(-1, 4, 0, &size));
    assert(!zsb_source_size(1, 4, 0, &size));
    assert(!zsb_source_size(200001, 8000004, 8000000, &size));
    assert(!zsb_source_size(200000, 8000004, 8000001, &size));
    assert(!zsb_source_size(INT32_MAX, UINT32_MAX, UINT32_MAX - 4, &size));
    assert(!zsb_source_size(0, 0, 0, &size));
    zj_state_port_t port = {.read=read_blob, .write=write_blob};
    zj_reader_identity_t identity = {.capture_epoch={1}, .terminal_digest={2}, .image_digest={3}};
    zsb_facts_t proposed = {.next_ordinal=200000, .record_size=40, .anchor_digest={4},
        .sampled_uptime_ms=UINT64_C(1)<<33};
    zsb_record_t out;
    assert(zsb_open(port, &identity, NULL, &out) == ZJ_EMPTY && !writes);
    assert(zsb_open(port, &identity, &proposed, &out) == ZJ_OK && writes == 1);
    assert(out.facts.next_ordinal == 200000 && out.facts.record_size == 40);
    assert(out.facts.sampled_uptime_ms == proposed.sampled_uptime_ms);
    assert(!memcmp(out.capture_epoch, identity.capture_epoch, 16));
    /* A lost reply and later terminal growth never move the first boundary. */
    proposed.next_ordinal += 1000;
    assert(zsb_open(port, &identity, &proposed, &out) == ZJ_OK && writes == 1);
    assert(out.facts.next_ordinal == 200000);
    assert(zsb_open(port, &identity, NULL, &out) == ZJ_OK && writes == 1);
    uint8_t original[ZSB_BYTES]; memcpy(original, durable, sizeof(original));
    for (unsigned i=0; i<ZSB_BYTES; ++i) {
        durable[i] ^= 1;
        assert(zsb_open(port, &identity, &proposed, &out) == ZJ_CORRUPT && writes == 1);
        memcpy(durable, original, sizeof(original));
    }
    uint8_t *bindings[] = {identity.capture_epoch, identity.terminal_digest, identity.image_digest};
    for (unsigned i=0; i<3; ++i) {
        bindings[i][0] ^= 8;
        assert(zsb_open(port, &identity, &proposed, &out) == ZJ_STALE && writes == 1);
        assert(!out.facts.next_ordinal && !zsb_nonzero(out.writer_digest, 32));
        bindings[i][0] ^= 8;
    }
    for (unsigned after=0; after<2; ++after) for (unsigned fault=1; fault<=3; ++fault) {
        present=false; writes=calls=0; fail_at=fault; uncertain=after;
        zj_result_t result = zsb_open(port, &identity, &proposed, &out);
        assert(result == (fault == 1 ? ZJ_IO : ZJ_UNCERTAIN));
        assert(!out.facts.next_ordinal);
        fail_at=0;
        bool committed = present;
        unsigned before = writes;
        zsb_facts_t later = proposed; later.next_ordinal += 99;
        assert(zsb_open(port, &identity, &later, &out) == ZJ_OK);
        assert(writes == before + (committed ? 0U : 1U));
        assert(out.facts.next_ordinal == (committed ? proposed.next_ordinal : later.next_ordinal));
    }
    present=false; calls=writes=fail_at=0;
    for (unsigned n=1; n<48; ++n) {
        proposed.record_size=n;
        if (n==8 || n==16 || n==40) continue;
        assert(zsb_open(port, &identity, &proposed, &out) == ZJ_INVALID && !writes);
    }
    proposed.next_ordinal=UINT32_MAX; proposed.record_size=40;
    assert(zsb_open(port, &identity, &proposed, &out) == ZJ_INVALID && !writes);
    proposed=(zsb_facts_t){0};
    assert(zsb_open(port, &identity, &proposed, &out) == ZJ_OK && writes == 1);
    assert(!out.facts.next_ordinal && !out.facts.record_size);
    /* Checksum-valid malformed state remains preserved, never recreated. */
    durable[136]=1; zsb_put(durable+156, dq_crc32(durable, 156), 4);
    assert(zsb_open(port, &identity, &proposed, &out) == ZJ_CORRUPT && writes == 1);
    return 0;
}
