#include "zkt_journal_compat.h"
#include "durable_queue.h"
#include "zkt_journal_codec.h"
#include <string.h>

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p)
{
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= (uint64_t)p[i] << (8 * i);
    return value;
}
static void put32(uint8_t *p, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(value >> (8 * i));
}
static void put64(uint8_t *p, uint64_t value)
{
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(value >> (8 * i));
}
static bool nonzero(const uint8_t *p, size_t length)
{
    uint8_t value = 0;
    for (size_t i = 0; i < length; ++i) value |= p[i];
    return value != 0;
}
static bool identity_valid(const zj_reader_identity_t *id)
{
    return id && nonzero(id->image_digest, 32) && nonzero(id->terminal_digest, 32) &&
        nonzero(id->capture_epoch, 16) && nonzero(id->layout_digest, 32) &&
        id->slot_address && !(id->slot_address % 0x10000U) &&
        id->slot_size >= 0x10000U && !(id->slot_size % 0x10000U) &&
        id->slot_address <= UINT32_MAX - id->slot_size;
}
static bool binding_equal(const zj_reader_identity_t *a, const zj_reader_identity_t *b)
{
    return !memcmp(a->terminal_digest, b->terminal_digest, 32) &&
        !memcmp(a->capture_epoch, b->capture_epoch, 16) &&
        !memcmp(a->layout_digest, b->layout_digest, 32);
}
static bool image_equal(const zj_reader_identity_t *a, const zj_reader_identity_t *b)
{
    return !memcmp(a->image_digest, b->image_digest, 32) &&
        a->slot_address == b->slot_address && a->slot_size == b->slot_size;
}
static bool separate_slots(const zj_reader_identity_t *a, const zj_reader_identity_t *b)
{
    return a->slot_address + a->slot_size <= b->slot_address ||
        b->slot_address + b->slot_size <= a->slot_address;
}
static void encode(const zj_reader_identity_t *id, uint64_t generation,
                   uint8_t bytes[ZJ_READER_PROOF_BYTES])
{
    memset(bytes, 0, ZJ_READER_PROOF_BYTES);
    memcpy(bytes, "ZJREAD01", 8);
    put32(bytes + 8, 1);  /* Proof schema. */
    put32(bytes + 12, ZJ_READER_PROOF_BYTES);
    put32(bytes + 16, ZJ_FORMAT_VERSION);
    put32(bytes + 20, ZJ_READER_MASK);
    put32(bytes + 24, 0x0f); /* Authenticated records, opaque recovery, receipt
                             * retirement and checkpoint replay are required. */
    put32(bytes + 28, 1); /* Encryption root schema. */
    put32(bytes + 32, 1); /* Retirement schema. */
    put32(bytes + 36, id->slot_address);
    put32(bytes + 40, id->slot_size);
    memcpy(bytes + 48, id->image_digest, 32);
    memcpy(bytes + 80, id->terminal_digest, 32);
    memcpy(bytes + 112, id->capture_epoch, 16);
    memcpy(bytes + 128, id->layout_digest, 32);
    put64(bytes + 160, generation);
    memcpy(bytes + 168, ZJ_BRIDGE_VERSION, sizeof(ZJ_BRIDGE_VERSION));
    put32(bytes + 188, dq_crc32(bytes, 188));
}
bool zj_reader_proof_decode(const uint8_t bytes[ZJ_READER_PROOF_BYTES],
                            zj_reader_identity_t *id, uint64_t *generation)
{
    if (id) memset(id, 0, sizeof(*id));
    if (generation) *generation = 0;
    if (!bytes || !id || !generation) return false;
    zj_reader_identity_t decoded = {.slot_address = get32(bytes + 36), .slot_size = get32(bytes + 40)};
    memcpy(decoded.image_digest, bytes + 48, 32);
    memcpy(decoded.terminal_digest, bytes + 80, 32);
    memcpy(decoded.capture_epoch, bytes + 112, 16);
    memcpy(decoded.layout_digest, bytes + 128, 32);
    uint64_t revision = get64(bytes + 160);
    uint8_t canonical[ZJ_READER_PROOF_BYTES];
    encode(&decoded, revision, canonical);
    if (!revision || !identity_valid(&decoded) || memcmp(bytes, canonical, sizeof(canonical))) return false;
    *id = decoded;
    *generation = revision;
    return true;
}
static zj_compat_result_t environment(const zj_reader_environment_t *e, const char *version,
                                      bool validated, bool runtime)
{
    if (!e || !e->application || !e->version) return ZJ_COMPAT_INVALID;
    if (strcmp(e->application, "zone_lite") || strcmp(e->version, version)) return ZJ_COMPAT_VERSION;
    if (!e->secure_boot || !e->encrypted_nvs || !e->ota_slot || (validated && !e->image_validated))
        return ZJ_COMPAT_SECURITY;
    if (runtime && (!e->reader_ready || !e->delivery_ready || !e->persistence_verified || e->recovery_pending))
        return ZJ_COMPAT_NOT_READY;
    return ZJ_COMPAT_OK;
}
static zj_compat_result_t load(zj_reader_proof_port_t port, uint8_t bytes[ZJ_READER_PROOF_BYTES],
                              zj_reader_identity_t *identity, uint64_t *generation)
{
    if (!port.read) return ZJ_COMPAT_INVALID;
    int read = port.read(port.context, bytes);
    if (read == 0) return ZJ_COMPAT_MISSING;
    if (read != 1) return ZJ_COMPAT_IO;
    return zj_reader_proof_decode(bytes, identity, generation) ? ZJ_COMPAT_OK : ZJ_COMPAT_CORRUPT;
}
zj_compat_result_t zj_reader_attest(zj_reader_proof_port_t port,
    const zj_reader_environment_t *bridge, const zj_reader_identity_t *identity)
{
    if (!port.read || !port.write || !identity_valid(identity)) return ZJ_COMPAT_INVALID;
    zj_compat_result_t result = environment(bridge, ZJ_BRIDGE_VERSION, true, true);
    if (result != ZJ_COMPAT_OK) return result;
    uint8_t proof[ZJ_READER_PROOF_BYTES], verify[ZJ_READER_PROOF_BYTES];
    zj_reader_identity_t previous;
    uint64_t generation = 0;
    result = load(port, proof, &previous, &generation);
    if (result == ZJ_COMPAT_OK) {
        if (!binding_equal(identity, &previous)) return ZJ_COMPAT_BINDING;
        if (image_equal(identity, &previous)) return ZJ_COMPAT_OK;
    } else if (result != ZJ_COMPAT_MISSING) return result;
    if (generation == UINT64_MAX) return ZJ_COMPAT_EXHAUSTED;
    encode(identity, generation + 1, proof);
    if (!port.write(port.context, proof) || port.read(port.context, verify) != 1 ||
        memcmp(proof, verify, sizeof(proof))) return ZJ_COMPAT_UNCERTAIN;
    return ZJ_COMPAT_OK;
}
static zj_compat_result_t check_retained_bridge(zj_reader_proof_port_t port,
    const zj_reader_environment_t *writer, const zj_reader_identity_t *current,
    const zj_reader_environment_t *rollback, const zj_reader_identity_t *previous,
    bool require_validated_state)
{
    if (!identity_valid(current) || !identity_valid(previous)) return ZJ_COMPAT_INVALID;
    zj_compat_result_t result = environment(writer, ZJ_WRITER_VERSION, false, true);
    if (result != ZJ_COMPAT_OK) return result;
    result = environment(rollback, ZJ_BRIDGE_VERSION, require_validated_state, false);
    if (result != ZJ_COMPAT_OK) return result;
    if (!separate_slots(current, previous)) return ZJ_COMPAT_ROLLBACK;
    uint8_t proof[ZJ_READER_PROOF_BYTES];
    zj_reader_identity_t attested;
    uint64_t generation;
    result = load(port, proof, &attested, &generation);
    if (result != ZJ_COMPAT_OK) return result;
    if (!binding_equal(current, previous) || !binding_equal(current, &attested)) return ZJ_COMPAT_BINDING;
    return image_equal(previous, &attested) ? ZJ_COMPAT_OK : ZJ_COMPAT_ROLLBACK;
}
zj_compat_result_t zj_reader_check_writer(zj_reader_proof_port_t port,
    const zj_reader_environment_t *writer, const zj_reader_identity_t *current,
    const zj_reader_environment_t *rollback, const zj_reader_identity_t *previous)
{
    return check_retained_bridge(port, writer, current, rollback, previous, true);
}
zj_compat_result_t zj_reader_check_selected(zj_reader_proof_port_t port,
    const zj_reader_environment_t *writer, const zj_reader_identity_t *current,
    const zj_reader_environment_t *rollback, const zj_reader_identity_t *previous)
{
    return check_retained_bridge(port, writer, current, rollback, previous, false);
}
zj_compat_result_t zj_reader_check_update(zj_reader_proof_port_t port,
    const zj_reader_environment_t *bridge, const zj_reader_identity_t *current,
    uint32_t target_address, uint32_t target_size, const char *target_version)
{
    if (!identity_valid(current) || !target_version) return ZJ_COMPAT_INVALID;
    if (bridge && bridge->version && !strcmp(bridge->version, ZJ_WRITER_VERSION))
        return ZJ_COMPAT_PROTECTED_SLOT;
    zj_compat_result_t result = environment(bridge, ZJ_BRIDGE_VERSION, true, true);
    if (result != ZJ_COMPAT_OK) return result;
    if (strcmp(target_version, ZJ_WRITER_VERSION)) return ZJ_COMPAT_UPDATE_TARGET;
    zj_reader_identity_t target = *current;
    target.slot_address = target_address;
    target.slot_size = target_size;
    if (!identity_valid(&target)) return ZJ_COMPAT_INVALID;
    if (!separate_slots(current, &target)) return ZJ_COMPAT_PROTECTED_SLOT;
    uint8_t proof[ZJ_READER_PROOF_BYTES];
    zj_reader_identity_t attested;
    uint64_t generation;
    result = load(port, proof, &attested, &generation);
    if (result != ZJ_COMPAT_OK) return result;
    if (!binding_equal(current, &attested)) return ZJ_COMPAT_BINDING;
    return image_equal(current, &attested) ? ZJ_COMPAT_OK : ZJ_COMPAT_ROLLBACK;
}
const char *zj_compat_error(zj_compat_result_t result)
{
    switch (result) {
        case ZJ_COMPAT_OK: return "";
        case ZJ_COMPAT_INVALID: return "JOURNAL_READER_INPUT_INVALID";
        case ZJ_COMPAT_VERSION: return "JOURNAL_READER_VERSION_MISMATCH";
        case ZJ_COMPAT_SECURITY: return "JOURNAL_READER_SECURITY_REQUIRED";
        case ZJ_COMPAT_NOT_READY: return "JOURNAL_READER_RUNTIME_NOT_READY";
        case ZJ_COMPAT_MISSING: return "JOURNAL_READER_PROOF_MISSING";
        case ZJ_COMPAT_IO: return "JOURNAL_READER_PROOF_UNAVAILABLE";
        case ZJ_COMPAT_CORRUPT: return "JOURNAL_READER_PROOF_CORRUPT";
        case ZJ_COMPAT_BINDING: return "JOURNAL_READER_BINDING_MISMATCH";
        case ZJ_COMPAT_ROLLBACK: return "JOURNAL_ROLLBACK_IMAGE_MISMATCH";
        case ZJ_COMPAT_UNCERTAIN: return "JOURNAL_READER_COMMIT_UNCERTAIN";
        case ZJ_COMPAT_EXHAUSTED: return "JOURNAL_READER_GENERATION_EXHAUSTED";
        case ZJ_COMPAT_UPDATE_TARGET: return "JOURNAL_OTA_TARGET_UNSUPPORTED";
        case ZJ_COMPAT_PROTECTED_SLOT: return "JOURNAL_ROLLBACK_SLOT_PROTECTED";
        case ZJ_COMPAT_SELECTION_EXPIRED: return "JOURNAL_READER_SELECTION_EXPIRED";
        case ZJ_COMPAT_SELECTION_UNCERTAIN: return "JOURNAL_READER_SELECTION_UNCERTAIN";
        case ZJ_COMPAT_ANTI_ROLLBACK: return "JOURNAL_READER_ANTI_ROLLBACK_UNQUALIFIED";
        default: return "JOURNAL_READER_RESULT_UNKNOWN";
    }
}
