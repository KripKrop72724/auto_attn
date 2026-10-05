#include "zkt_journal_compat.h"
#include "durable_queue.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint8_t bytes[ZJ_READER_PROOF_BYTES];
    bool exists, fail_write, persisted_on_failure, bad_readback;
    unsigned reads, writes, fail_read_at;
} storage_t;
static storage_t storage;
static int load(void *context, uint8_t bytes[ZJ_READER_PROOF_BYTES])
{
    storage_t *s = context;
    if (++s->reads == s->fail_read_at) return -1;
    if (!s->exists) return 0;
    memcpy(bytes, s->bytes, ZJ_READER_PROOF_BYTES);
    if (s->bad_readback && s->writes) bytes[0] ^= 1;
    return 1;
}
static bool commit(void *context, const uint8_t bytes[ZJ_READER_PROOF_BYTES])
{
    storage_t *s = context;
    ++s->writes;
    if (!s->fail_write || s->persisted_on_failure) {
        memcpy(s->bytes, bytes, ZJ_READER_PROOF_BYTES);
        s->exists = true;
    }
    return !s->fail_write;
}
static zj_reader_proof_port_t port(void) { return (zj_reader_proof_port_t){load, commit, &storage}; }
static void crc(void)
{
    uint32_t value = dq_crc32(storage.bytes, ZJ_READER_PROOF_BYTES - 4);
    for (unsigned i = 0; i < 4; ++i) storage.bytes[188 + i] = (uint8_t)(value >> (8 * i));
}
static zj_reader_environment_t bridge(void)
{
    return (zj_reader_environment_t){.application = "zone_lite", .version = ZJ_BRIDGE_VERSION,
        .secure_boot = true, .encrypted_nvs = true, .ota_slot = true, .image_validated = true,
        .reader_ready = true, .delivery_ready = true, .persistence_verified = true};
}
static zj_reader_identity_t identity(void)
{
    return (zj_reader_identity_t){.image_digest = {11}, .terminal_digest = {22},
        .capture_epoch = {33}, .layout_digest = {44}, .slot_address = 0x520000, .slot_size = 0x280000};
}
int main(void)
{
    zj_reader_environment_t b = bridge(), w = bridge();
    w.version = ZJ_WRITER_VERSION;
    w.image_validated = false; /* New writer may still be pending local health. */
    zj_reader_identity_t previous = identity(), current = identity();
    current.slot_address = 0x2a0000;
    current.image_digest[0] = 55;
    assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_MISSING);
    assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_MISSING);
    assert(!storage.writes);
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK && storage.writes == 1);
    uint8_t original[ZJ_READER_PROOF_BYTES];
    memcpy(original, storage.bytes, sizeof(original));
    zj_reader_identity_t decoded;
    uint64_t generation;
    assert(zj_reader_proof_decode(original, &decoded, &generation) && generation == 1);
    assert(!memcmp(decoded.image_digest, previous.image_digest, 32));
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK && storage.writes == 1);
    assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_OK);
    assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_OK);
    assert(zj_reader_check_update(port(), &w, &current, previous.slot_address, previous.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_PROTECTED_SLOT);
    assert(zj_reader_check_update(port(), &b, &previous, previous.slot_address, previous.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_PROTECTED_SLOT);
    assert(zj_reader_check_update(port(), &b, &previous, previous.slot_address - 0x10000, previous.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_PROTECTED_SLOT);
    assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size, "2.6.15") == ZJ_COMPAT_UPDATE_TARGET);
    assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size, "2.7.1") == ZJ_COMPAT_UPDATE_TARGET);
    assert(zj_reader_check_update(port(), &b, &previous, 0xffff0000, 0x10000, ZJ_WRITER_VERSION) == ZJ_COMPAT_INVALID);
    assert(zj_reader_check_update(port(), &b, &previous, 1, current.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_INVALID);
    assert(storage.writes == 1);
    /* Writer validation and subsequent rollback readers consume the same
     * persisted blob. Neither can infer success from just a version label. */
    FILE *file = fopen("reader-proof.bin", "wb");
    assert(file && fwrite(original, 1, sizeof(original), file) == sizeof(original) && !fclose(file));
    memset(&storage, 0, sizeof(storage));
    file = fopen("reader-proof.bin", "rb");
    assert(file && fread(storage.bytes, 1, sizeof(original), file) == sizeof(original) && !fclose(file));
    storage.exists = true;
    assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_OK);
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK && !storage.writes);

    const char *historical[] = {"2.6.16", "2.6.17", "2.6.18"};
    for (size_t i = 0; i < sizeof(historical) / sizeof(*historical); ++i) {
        if (!strcmp(historical[i], ZJ_BRIDGE_VERSION)) continue;
        memcpy(storage.bytes + 168, historical[i], strlen(historical[i]) + 1);
        crc();
        if (strcmp(historical[i], ZJ_BRIDGE_VERSION) > 0) {
            assert(!zj_reader_proof_decode(storage.bytes, &decoded, &generation));
            assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_CORRUPT && !storage.writes);
            memcpy(storage.bytes, original, sizeof(original));
            continue;
        }
        assert(zj_reader_proof_decode(storage.bytes, &decoded, &generation) && generation == 1);
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_VERSION);
        assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size,
            ZJ_WRITER_VERSION) == ZJ_COMPAT_VERSION);
        zj_reader_identity_t rebound = previous; rebound.terminal_digest[0] ^= 1;
        assert(zj_reader_attest(port(), &b, &rebound) == ZJ_COMPAT_BINDING && !storage.writes);
        assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK && storage.writes == 1);
        assert(zj_reader_proof_decode(storage.bytes, &decoded, &generation) && generation == 2);
        assert(!memcmp(storage.bytes + 168, ZJ_BRIDGE_VERSION, sizeof(ZJ_BRIDGE_VERSION)));
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_OK);
        memcpy(storage.bytes, original, sizeof(original)); storage.writes = 0;
    }
    for (unsigned byte = 0; byte < ZJ_READER_PROOF_BYTES; ++byte) {
        storage.bytes[byte] ^= 1;
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_CORRUPT);
        assert(zj_reader_check_selected(port(), &w, &current, &b, &previous) == ZJ_COMPAT_CORRUPT);
        assert(zj_reader_check_update(port(), &b, &previous, current.slot_address, current.slot_size, ZJ_WRITER_VERSION) == ZJ_COMPAT_CORRUPT);
        assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_CORRUPT && !storage.writes);
        assert(!zj_reader_proof_decode(storage.bytes, &decoded, &generation) && !generation);
        for (size_t i = 0; i < sizeof(decoded); ++i) assert(!((uint8_t *)&decoded)[i]);
        memcpy(storage.bytes, original, sizeof(original));
    }
    /* Unknown formats, reserved bytes and capability bits fail even with a
     * valid checksum, rather than silently granting a future reader format. */
    unsigned semantic_offsets[] = {0, 8, 12, 16, 20, 24, 28, 32, 44, 168, 178, 187};
    for (unsigned i = 0; i < sizeof(semantic_offsets) / sizeof(*semantic_offsets); ++i) {
        storage.bytes[semantic_offsets[i]] ^= 1;
        crc();
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_CORRUPT);
        memcpy(storage.bytes, original, sizeof(original));
    }
    for (unsigned i = 0; i < 8; ++i) {
        zj_reader_environment_t changed = b;
        switch (i) {
            case 0: changed.secure_boot = false; break;
            case 1: changed.encrypted_nvs = false; break;
            case 2: changed.ota_slot = false; break;
            case 3: changed.image_validated = false; break;
            case 4: changed.reader_ready = false; break;
            case 5: changed.delivery_ready = false; break;
            case 6: changed.persistence_verified = false; break;
            case 7: changed.recovery_pending = true; break;
        }
        assert(zj_reader_attest(port(), &changed, &previous) != ZJ_COMPAT_OK && !storage.writes);
        assert(zj_reader_check_update(port(), &changed, &previous, current.slot_address, current.slot_size, ZJ_WRITER_VERSION) != ZJ_COMPAT_OK && !storage.writes);
    }
    zj_reader_environment_t changed = b;
    changed.application = "zone_lite_hikvision";
    assert(zj_reader_attest(port(), &changed, &previous) == ZJ_COMPAT_VERSION);
    changed = b; changed.version = "2.6.15";
    assert(zj_reader_check_writer(port(), &w, &current, &changed, &previous) == ZJ_COMPAT_VERSION);
    changed = b; changed.image_validated = false;
    assert(zj_reader_check_writer(port(), &w, &current, &changed, &previous) == ZJ_COMPAT_SECURITY);
    assert(zj_reader_check_selected(port(), &w, &current, &changed, &previous) == ZJ_COMPAT_OK);
    assert(!storage.writes); /* Selection retry cannot renew proof or authorize writing. */
    changed = w; changed.reader_ready = false;
    assert(zj_reader_check_writer(port(), &changed, &current, &b, &previous) == ZJ_COMPAT_NOT_READY);
    changed = w; changed.secure_boot = false;
    assert(zj_reader_check_writer(port(), &changed, &current, &b, &previous) == ZJ_COMPAT_SECURITY);
    changed = w; changed.version = "2.7.1";
    assert(zj_reader_check_writer(port(), &changed, &current, &b, &previous) == ZJ_COMPAT_VERSION);
    for (unsigned i = 0; i < 6; ++i) {
        zj_reader_identity_t wrong = previous;
        switch (i) {
            case 0: wrong.image_digest[1] ^= 1; break;
            case 1: wrong.terminal_digest[1] ^= 1; break;
            case 2: wrong.capture_epoch[1] ^= 1; break;
            case 3: wrong.layout_digest[1] ^= 1; break;
            case 4: wrong.slot_address += 0x10000; break;
            case 5: wrong.slot_size -= 0x10000; break;
        }
        assert(zj_reader_check_writer(port(), &w, &current, &b, &wrong) != ZJ_COMPAT_OK);
        assert(zj_reader_check_update(port(), &b, &wrong, current.slot_address, current.slot_size, ZJ_WRITER_VERSION) != ZJ_COMPAT_OK && !storage.writes);
        if (i >= 1 && i <= 3)
            assert(zj_reader_attest(port(), &b, &wrong) == ZJ_COMPAT_BINDING && !storage.writes);
    }
    zj_reader_identity_t wrong = current;
    wrong.slot_address = previous.slot_address;
    assert(zj_reader_check_writer(port(), &w, &wrong, &b, &previous) == ZJ_COMPAT_ROLLBACK);
    wrong.slot_address = previous.slot_address - 0x10000;
    assert(zj_reader_check_writer(port(), &w, &wrong, &b, &previous) == ZJ_COMPAT_ROLLBACK);
    wrong = current; wrong.slot_address = 0xffff0000; wrong.slot_size = 0x10000;
    assert(zj_reader_check_writer(port(), &w, &wrong, &b, &previous) == ZJ_COMPAT_INVALID);
    wrong = current; memset(wrong.image_digest, 0, 32);
    assert(zj_reader_check_writer(port(), &w, &wrong, &b, &previous) == ZJ_COMPAT_INVALID);
    assert(!storage.writes);

    for (unsigned scenario = 0; scenario < 5; ++scenario) {
        memset(&storage, 0, sizeof(storage));
        if (scenario == 0) storage.fail_read_at = 1;
        if (scenario == 1 || scenario == 2) storage.fail_write = true;
        if (scenario == 2) storage.persisted_on_failure = true;
        if (scenario == 3) storage.fail_read_at = 2;
        if (scenario == 4) storage.bad_readback = true;
        assert(zj_reader_attest(port(), &b, &previous) != ZJ_COMPAT_OK);
        if (scenario == 0) assert(!storage.writes);
        storage.fail_read_at = 0; storage.fail_write = false; storage.bad_readback = false;
        assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK);
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_OK);
    }
    /* A torn first commit never becomes a valid capability. Preserve it for
     * explicit repair instead of overwriting evidence or opening the writer. */
    for (unsigned prefix = 0; prefix < sizeof(original); ++prefix) {
        memset(&storage, 0, sizeof(storage));
        storage.exists = true;
        memcpy(storage.bytes, original, prefix);
        assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_CORRUPT && !storage.writes);
        assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_CORRUPT);
    }
    memset(&storage, 0, sizeof(storage));
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK);
    previous.image_digest[2] ^= 1;
    assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_ROLLBACK);
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_OK && storage.writes == 2);
    assert(zj_reader_proof_decode(storage.bytes, &decoded, &generation) && generation == 2);
    assert(zj_reader_check_writer(port(), &w, &current, &b, &previous) == ZJ_COMPAT_OK);
    memset(storage.bytes + 160, 0xff, 8); crc();
    previous.image_digest[2] ^= 1;
    assert(zj_reader_attest(port(), &b, &previous) == ZJ_COMPAT_EXHAUSTED && storage.writes == 2);
    assert(!zj_reader_proof_decode(NULL, &decoded, &generation));
    assert(zj_reader_attest((zj_reader_proof_port_t){0}, &b, &previous) == ZJ_COMPAT_INVALID);
    assert(zj_reader_check_writer(port(), NULL, &current, &b, &previous) == ZJ_COMPAT_INVALID);
    for (unsigned i = ZJ_COMPAT_INVALID; i <= ZJ_COMPAT_ANTI_ROLLBACK; ++i)
        assert(zj_compat_error((zj_compat_result_t)i)[0]);
    assert(!zj_compat_error(ZJ_COMPAT_OK)[0]);
    puts("Durable journal reader proof, rollback identity and interruption checks passed");
    return 0;
}
