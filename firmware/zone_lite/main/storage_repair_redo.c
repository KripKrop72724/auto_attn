#include "storage_repair_redo.h"

#include <stdio.h>
#include <string.h>

uint16_t srx_get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
uint32_t srx_get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void put16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}
static void put32(uint8_t *p, uint32_t v)
{
    for (int i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}

uint16_t srx_spiffs_magic(uint32_t block_count, uint32_t block)
{
    return (uint16_t)(0x20140529U ^ SRX_PAGE ^ (block_count - block));
}

static unsigned popcount16(uint16_t v)
{
    unsigned n = 0;
    for (; v; v &= (uint16_t)(v - 1)) ++n;
    return n;
}

/* Weak: two set bits or fewer, or every byte 0x00 or 0xFF (round 1 review,
 * D3). The window clears the magic to 0x0000 before the erase; an erase cut
 * short leaves each byte erased or not, so 0x00FF (block 810 of 2,048) or
 * 0xFF00 can read valid again over a half-erased block. */
bool srx_magic_strong(uint32_t block_count, uint32_t block)
{
    uint16_t magic = srx_spiffs_magic(block_count, block);
    uint8_t low = (uint8_t)magic, high = (uint8_t)(magic >> 8);
    bool bytewise = (low == 0x00 || low == 0xFF) && (high == 0x00 || high == 0xFF);
    return popcount16(magic) >= 3 && !bytewise;
}

bool srx_has_magic(const uint8_t *lookup, uint32_t block_count, uint32_t block)
{
    return srx_get16(lookup + SRX_MAGIC_OFFSET) == srx_spiffs_magic(block_count, block);
}

bool srx_erased(const uint8_t *bytes, size_t length)
{
    for (size_t i = 0; i < length; ++i)
        if (bytes[i] != 0xFF) return false;
    return true;
}

/* ---- record ------------------------------------------------------------ */
uint32_t srx_target(const uint8_t *old, uint16_t drop_mask, uint16_t erase_count, uint32_t block_count,
                    uint32_t block, uint8_t *target)
{
    memcpy(target, old, SRX_BLOCK);
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        if (!(drop_mask & (1U << e))) continue;
        put16(target + e * 2, SRX_ID_FREE);
        memset(target + (1 + e) * SRX_PAGE, 0xFF, SRX_PAGE);
    }
    put16(target + SRX_ERASE_COUNT_OFFSET, erase_count);
    put16(target + SRX_MAGIC_OFFSET, srx_spiffs_magic(block_count, block));
    return srp_crc32(0, target, SRX_BLOCK);
}

void srx_encode(const srx_record_t *h, const uint8_t *target, uint8_t *sector)
{
    memset(sector, 0xFF, SRX_BLOCK);
    put32(sector + 0, SRX_RECORD_MAGIC);
    put16(sector + 4, SRX_RECORD_VERSION);
    put16(sector + 6, SRX_HEADER_BYTES);
    put32(sector + 8, h->reclaim_id);
    put32(sector + 12, h->sequence);
    put32(sector + 16, h->image_tag);
    sector[20] = h->flags;
    sector[21] = h->objects > SRX_OBJECTS ? SRX_OBJECTS : h->objects;
    put16(sector + 22, h->block);
    put16(sector + 24, h->kept_mask);
    put16(sector + 26, h->drop_mask);
    put16(sector + 28, h->erase_count);
    put16(sector + 30, h->block_count);
    put32(sector + 32, h->old_crc);
    put32(sector + 36, h->new_crc);
    for (uint32_t i = 0; i < sector[21]; ++i) {
        uint8_t *o = sector + SRX_OFF_OBJECTS + i * SRX_OBJECT_BYTES;
        const srx_object_t *src = &h->object[i];
        memset(o, 0, SRX_OBJECT_BYTES);
        put16(o, src->obj);
        o[2] = src->cls;
        o[3] = src->flags;
        put32(o + 4, src->cursor_offset);
        size_t n = strnlen(src->name, SRX_OBJECT_BYTES - 9);
        memcpy(o + 8, src->name, n);
    }
    put32(sector + SRX_OFF_HEADER_CRC, srp_crc32(0, sector, SRX_OFF_HEADER_CRC));
    memcpy(sector + SRX_OFF_LOOKUP, target, SRX_PAGE);
    uint32_t at = SRX_OFF_PAGES;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        if (!(h->kept_mask & (1U << e))) continue;
        memcpy(sector + at, target + (1 + e) * SRX_PAGE, SRX_PAGE);
        at += SRX_PAGE;
    }
}

static unsigned kept_count(uint16_t mask)
{
    unsigned n = 0;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) n += (mask >> e) & 1U;
    return n;
}

bool srx_decode(const uint8_t *sector, srx_record_t *r, uint8_t *target)
{
    if (srx_get32(sector) != SRX_RECORD_MAGIC || srx_get16(sector + 4) != SRX_RECORD_VERSION ||
        srx_get16(sector + 6) != SRX_HEADER_BYTES ||
        srx_get32(sector + SRX_OFF_HEADER_CRC) != srp_crc32(0, sector, SRX_OFF_HEADER_CRC))
        return false;
    memset(r, 0, sizeof(*r));
    r->reclaim_id = srx_get32(sector + 8);
    r->sequence = srx_get32(sector + 12);
    r->image_tag = srx_get32(sector + 16);
    r->flags = sector[20];
    r->objects = sector[21];
    r->block = srx_get16(sector + 22);
    r->kept_mask = srx_get16(sector + 24);
    r->drop_mask = srx_get16(sector + 26);
    r->erase_count = srx_get16(sector + 28);
    r->block_count = srx_get16(sector + 30);
    r->old_crc = srx_get32(sector + 32);
    r->new_crc = srx_get32(sector + 36);
    if (r->objects > SRX_OBJECTS || (r->kept_mask & r->drop_mask) || (r->kept_mask >> SRX_ENTRIES) ||
        (r->drop_mask >> SRX_ENTRIES) || kept_count(r->kept_mask) > SRX_ENTRIES - 1U || r->block >= r->block_count)
        return false;
    for (uint32_t i = 0; i < r->objects; ++i) {
        const uint8_t *o = sector + SRX_OFF_OBJECTS + i * SRX_OBJECT_BYTES;
        r->object[i].obj = srx_get16(o);
        r->object[i].cls = o[2];
        r->object[i].flags = o[3];
        r->object[i].cursor_offset = srx_get32(o + 4);
        memcpy(r->object[i].name, o + 8, SRX_OBJECT_BYTES - 8);
        r->object[i].name[SRX_OBJECT_BYTES - 8] = 0;
    }
    r->done = srx_get32(sector + SRX_OFF_DONE);
    r->settle = srx_get32(sector + SRX_OFF_SETTLE);
    r->attempts = srx_get32(sector + SRX_OFF_ATTEMPTS);
    /* The target the record describes must be the one it was written for. */
    uint32_t crc = srp_crc32(0, sector + SRX_OFF_LOOKUP, SRX_PAGE);
    uint8_t erased[SRX_PAGE];
    memset(erased, 0xFF, sizeof(erased));
    uint32_t at = SRX_OFF_PAGES;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        const uint8_t *page = erased;
        if (r->kept_mask & (1U << e)) {
            page = sector + at;
            at += SRX_PAGE;
        }
        crc = srp_crc32(crc, page, SRX_PAGE);
        if (target) memcpy(target + (1 + e) * SRX_PAGE, page, SRX_PAGE);
    }
    if (target) memcpy(target, sector + SRX_OFF_LOOKUP, SRX_PAGE);
    return crc == r->new_crc;
}

const uint8_t *srx_kept_page(const uint8_t *sector, const srx_record_t *r, uint32_t entry)
{
    if (entry >= SRX_ENTRIES || !(r->kept_mask & (1U << entry))) return NULL;
    uint32_t order = 0;
    for (uint32_t e = 0; e < entry; ++e) order += (r->kept_mask >> e) & 1U;
    return sector + SRX_OFF_PAGES + order * SRX_PAGE;
}

unsigned srx_attempts_used(uint32_t attempts)
{
    unsigned n = 0;
    for (uint32_t b = 0; b < 32; ++b) n += !((attempts >> b) & 1U);
    return n;
}

bool srx_is_source(const srx_record_t *r) { return !(r->settle & SRX_SETTLE_SOURCE); }

bool srx_retired(const srx_record_t *r)
{
    return srx_is_source(r) && (r->settle & r->kept_mask) == 0;
}

bool srx_retained(const srx_record_t *r)
{
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e)
        if ((r->kept_mask & (1U << e)) && (r->settle & (1U << e)) && !(r->settle & (1U << (16 + e)))) return true;
    return false;
}

/* ---- plan --------------------------------------------------------------- */
static bool droppable(const srx_entry_t *e)
{
    return e->lu == SRX_ID_DELETED && !(e->flags & SRX_PH_DELET) && !e->referenced;
}

static bool free_entry(const srx_entry_t *e) { return e->lu == SRX_ID_FREE && e->erased; }

srx_plan_t srx_plan_block(const srx_entry_t entry[SRX_ENTRIES], bool magic_ok, bool unconfirmed)
{
    srx_plan_t p = {0, 0, 0, 0, false, SRX_PLAN_OK};
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        const srx_entry_t *x = &entry[e];
        bool mislabeled = (x->lu == SRX_ID_FREE && !x->erased) ||
            (x->lu != SRX_ID_FREE && x->lu != SRX_ID_DELETED && x->lu != x->obj);
        if (mislabeled) {
            p.reason = SRX_PLAN_MISLABELED;
            return p;
        }
        if (free_entry(x)) continue;
        if (droppable(x)) p.dropped |= (uint16_t)(1U << e);
        else p.kept |= (uint16_t)(1U << e);
    }
    /* Entry 0 is what SPIFFS counts a free block by. */
    if ((p.dropped & 1U) && (p.kept & ~1U)) {
        p.dropped &= (uint16_t)~1U;
        p.kept |= 1U;
    }
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) p.gain += (p.dropped >> e) & 1U;
    for (uint32_t e = SRX_ENTRIES; e-- > 0;) {
        if (p.kept & (1U << e)) break;
        p.tail += (p.dropped >> e) & 1U;
    }
    if (!p.dropped) {
        p.reason = SRX_PLAN_NO_GAIN;
        return p;
    }
    if (!magic_ok) {
        p.reason = SRX_PLAN_MAGIC;
        return p;
    }
    srx_plan_reason_t worst = SRX_PLAN_OK;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        if (!(p.kept & (1U << e))) continue;
        const srx_entry_t *x = &entry[e];
        if (x->index) worst = SRX_PLAN_INDEX;
        else if (x->keep == SRX_KEEP_EXCLUDED && worst != SRX_PLAN_INDEX) worst = SRX_PLAN_EXCLUDED;
        else if (x->keep == SRX_KEEP_CUSTODY) p.custody = true;
    }
    if (worst == SRX_PLAN_OK && p.custody && unconfirmed) worst = SRX_PLAN_CUSTODY;
    p.reason = worst;
    return p;
}

const char *srx_plan_reason_name(srx_plan_reason_t reason)
{
    switch (reason) {
    case SRX_PLAN_OK: return "OK";
    case SRX_PLAN_MISLABELED: return "MISLABELED";
    case SRX_PLAN_NO_GAIN: return "NO_GAIN";
    case SRX_PLAN_MAGIC: return "MAGIC";
    case SRX_PLAN_INDEX: return "INDEX";
    case SRX_PLAN_EXCLUDED: return "EXCLUDED";
    case SRX_PLAN_CUSTODY: return "CUSTODY";
    }
    return "UNKNOWN";
}

/* ---- writes -------------------------------------------------------------- */
static void say(const srx_io_t *io, const char *level, const char *code, const char *message)
{
    if (io->log) io->log(io->context, level, code, message);
}

static void window(const srx_io_t *io, bool open)
{
    if (io->window) io->window(io->context, open);
}

srx_write_t srx_write_block(const srx_io_t *io, uint32_t block, const uint8_t *target, uint8_t *readback,
                            unsigned retries)
{
    uint32_t base = block * SRX_BLOCK;
    static const uint8_t cleared[2] = {0, 0};
    srx_write_t result = SRX_WRITE_VERIFY;
    for (unsigned attempt = 0; attempt <= retries; ++attempt) {
        if (attempt && io->may_retry && !io->may_retry(io->context)) break;
        window(io, true);
        bool ok = io->store_write(io->context, base + SRX_MAGIC_OFFSET, cleared, 2) &&
            io->store_erase(io->context, base, SRX_BLOCK);
        for (uint32_t e = 0; ok && e < SRX_ENTRIES; ++e) {
            const uint8_t *page = target + (1 + e) * SRX_PAGE;
            if (!srx_erased(page, SRX_PAGE)) ok = io->store_write(io->context, base + (1 + e) * SRX_PAGE, page, SRX_PAGE);
        }
        if (ok && !srx_erased(target, SRX_LOOKUP_BYTES)) ok = io->store_write(io->context, base, target, SRX_LOOKUP_BYTES);
        if (ok) ok = io->store_write(io->context, base + SRX_ERASE_COUNT_OFFSET, target + SRX_ERASE_COUNT_OFFSET, 2);
        if (ok) ok = io->store_write(io->context, base + SRX_MAGIC_OFFSET, target + SRX_MAGIC_OFFSET, 2);
        window(io, false);
        if (!ok) {
            result = SRX_WRITE_IO;
            break;
        }
        if (io->store_read(io->context, base, readback, SRX_BLOCK) && !memcmp(readback, target, SRX_BLOCK))
            return SRX_WRITE_OK;
        result = SRX_WRITE_VERIFY;
    }
    /* A block that does not hold its target is left for SPIFFS's mount to
     * erase: its kept pages are in the record. */
    (void)io->store_write(io->context, base + SRX_MAGIC_OFFSET, cleared, 2);
    return result;
}

/* A reset at any of these writes leaves the record open (its source bit is
 * cleared only after this returns) and the block either without a magic (a
 * mount erases it; the same image's recovery redoes it) or closed: valid,
 * every entry erased. Recovery takes a closed block for another writer's
 * (no kept byte left) and makes the record a source. */
bool srx_close_block(const srx_io_t *io, uint32_t block, const uint8_t *target, uint8_t *readback)
{
    uint32_t base = block * SRX_BLOCK;
    static const uint8_t cleared[2] = {0, 0};
    bool ok = io->store_erase(io->context, base, SRX_BLOCK) &&
        io->store_write(io->context, base + SRX_ERASE_COUNT_OFFSET, target + SRX_ERASE_COUNT_OFFSET, 2) &&
        io->store_write(io->context, base + SRX_MAGIC_OFFSET, target + SRX_MAGIC_OFFSET, 2) &&
        io->store_read(io->context, base, readback, SRX_BLOCK);
    for (uint32_t i = 0; ok && i < SRX_BLOCK; ++i)
        ok = readback[i] == (i >= SRX_MAGIC_OFFSET && i < SRX_MAGIC_OFFSET + 4U ? target[i] : 0xFFU);
    if (!ok) (void)io->store_write(io->context, base + SRX_MAGIC_OFFSET, cleared, 2);
    return ok;
}

bool srx_write_record(const srx_io_t *io, uint32_t slot, const uint8_t *sector, uint8_t *scratch)
{
    uint32_t base = slot * SRX_BLOCK;
    if (!io->area_read(io->context, base, scratch, SRX_BLOCK)) return false;
    if (!srx_erased(scratch, SRX_BLOCK) && !io->area_erase(io->context, base, SRX_BLOCK)) return false;
    return io->area_write(io->context, base, sector, SRX_BLOCK) && io->area_read(io->context, base, scratch, SRX_BLOCK) &&
        !memcmp(scratch, sector, SRX_BLOCK);
}

bool srx_clear_bits(const srx_io_t *io, uint32_t slot, uint32_t offset, uint32_t bits)
{
    uint8_t value[4], back[4];
    put32(value, ~bits);
    uint32_t at = slot * SRX_BLOCK + offset;
    for (int attempt = 0; attempt < 2; ++attempt) {
        if (!io->area_write(io->context, at, value, 4) || !io->area_read(io->context, at, back, 4)) continue;
        if (!(srx_get32(back) & bits)) return true;
    }
    return false;
}

/* ---- area ---------------------------------------------------------------- */
const char *srx_area_name(srx_area_state_t state)
{
    switch (state) {
    case SRX_AREA_ABSENT: return "ABSENT";
    case SRX_AREA_READY: return "READY";
    case SRX_AREA_FOREIGN: return "FOREIGN";
    case SRX_AREA_OVERLAP: return "OVERLAP";
    case SRX_AREA_UNREADABLE: return "UNREADABLE";
    case SRX_AREA_NO_MEMORY: return "NO_MEMORY";
    }
    return "UNKNOWN";
}

static srx_slot_t classify(const srx_record_t *r)
{
    if (r->done != 0xFFFFFFFFU) return SRX_SLOT_DONE;
    if (srx_retired(r)) return SRX_SLOT_RETIRED;
    if (srx_is_source(r)) return SRX_SLOT_SOURCE;
    return SRX_SLOT_OPEN;
}

void srx_discover(const srx_io_t *io, bool claimed, uint8_t *sector, srx_area_t *area)
{
    memset(area, 0, sizeof(*area));
    area->slots = io->area_size / SRX_BLOCK;
    if (area->slots > SRX_SLOTS) area->slots = SRX_SLOTS;
    area->state = SRX_AREA_READY;
    for (uint32_t s = 0; s < area->slots; ++s) {
        if (!io->area_read(io->context, s * SRX_BLOCK, sector, SRX_BLOCK)) {
            area->state = SRX_AREA_UNREADABLE;
            area->slots = s;
            return;
        }
        srx_record_t r;
        if (srx_erased(sector, SRX_BLOCK)) {
            area->slot[s] = SRX_SLOT_ERASED;
        } else if (srx_decode(sector, &r, NULL)) {
            area->slot[s] = (uint8_t)classify(&r);
            area->block[s] = r.block;
            area->sequence[s] = r.sequence;
            if (r.sequence >= area->newest_sequence) area->newest_sequence = r.sequence;
            if (r.reclaim_id >= area->newest_reclaim) area->newest_reclaim = r.reclaim_id;
        } else if (claimed) {
            area->slot[s] = SRX_SLOT_TORN;
        } else {
            area->slot[s] = SRX_SLOT_FOREIGN;
            if (!area->foreign++) {
                area->first_foreign = s;
                area->foreign_crc = srp_crc32(0, sector, SRX_BLOCK);
            }
            area->state = SRX_AREA_FOREIGN;
        }
    }
}

bool srx_slot_free(const srx_area_t *area, uint32_t slot)
{
    if (slot >= area->slots) return false;
    uint8_t s = area->slot[slot];
    return s == SRX_SLOT_ERASED || s == SRX_SLOT_TORN || s == SRX_SLOT_DONE || s == SRX_SLOT_RETIRED;
}

const char *srx_test_name(srx_test_t test)
{
    switch (test) {
    case SRX_TEST_NOT_RUN: return "NOT_RUN";
    case SRX_TEST_PASSED: return "PASSED";
    case SRX_TEST_FAILED: return "FAILED";
    case SRX_TEST_NO_SLOT: return "NO_SLOT";
    }
    return "UNKNOWN";
}

srx_test_t srx_area_test(const srx_io_t *io, const srx_area_t *area, uint8_t *scratch, uint32_t *slot_out)
{
    if (slot_out) *slot_out = UINT32_MAX;
    if (!io || !area || !scratch || area->state != SRX_AREA_READY || !io->area_size) return SRX_TEST_NOT_RUN;
    /* An erased slot first (no extra erase), then a torn, retired or done one
     * (its block long completed: nothing reads its copy). */
    static const uint8_t preference[] = {SRX_SLOT_ERASED, SRX_SLOT_TORN, SRX_SLOT_RETIRED, SRX_SLOT_DONE};
    uint32_t slot = area->slots;
    for (size_t p = 0; p < sizeof(preference) && slot == area->slots; ++p)
        for (uint32_t s = area->slots; s-- > 0;)
            if (area->slot[s] == preference[p] && srx_slot_free(area, s)) {
                slot = s;
                break;
            }
    if (slot == area->slots) return SRX_TEST_NO_SLOT;
    if (slot_out) *slot_out = slot;
    uint32_t base = slot * SRX_BLOCK;
    if (!io->area_read(io->context, base, scratch, SRX_BLOCK)) return SRX_TEST_FAILED;
    if (!srx_erased(scratch, SRX_BLOCK) && !io->area_erase(io->context, base, SRX_BLOCK)) return SRX_TEST_FAILED;
    for (uint32_t i = 0; i < SRX_BLOCK; ++i) scratch[i] = (uint8_t)((i * 37U + 11U) ^ (i >> 8));
    put32(scratch, SRX_TEST_MAGIC);
    bool ok = io->area_write(io->context, base, scratch, SRX_BLOCK);
    /* Read back over the pattern regenerated: a short or wrong program fails. */
    uint8_t expect[64];
    for (uint32_t at = 0; ok && at < SRX_BLOCK; at += sizeof(expect)) {
        for (uint32_t i = 0; i < sizeof(expect); ++i) expect[i] = (uint8_t)(((at + i) * 37U + 11U) ^ ((at + i) >> 8));
        if (!at) put32(expect, SRX_TEST_MAGIC);
        ok = io->area_read(io->context, base + at, scratch, sizeof(expect)) && !memcmp(scratch, expect, sizeof(expect));
    }
    /* Erased again whatever the program did, and read back erased. */
    bool erased = io->area_erase(io->context, base, SRX_BLOCK) && io->area_read(io->context, base, scratch, SRX_BLOCK) &&
        srx_erased(scratch, SRX_BLOCK);
    return ok && erased ? SRX_TEST_PASSED : SRX_TEST_FAILED;
}

bool srx_block_busy(const srx_area_t *area, uint32_t block)
{
    for (uint32_t s = 0; s < area->slots; ++s)
        if ((area->slot[s] == SRX_SLOT_OPEN || area->slot[s] == SRX_SLOT_SOURCE) && area->block[s] == block) return true;
    return false;
}

/* An erase of the window cut short, whose magic happens to read valid (round
 * 1 review, D3): every byte the record knows (kept pages and their lookup
 * entries, erased entries, the lookup page's other bytes) is its byte with
 * bits only set, as an erase sets them, and some kept byte is not erased yet.
 * A mount's erase leaves no kept byte, and a page programmed since into a
 * kept or erased entry clears bits the record has set. Bytes the record does
 * not hold (dropped pages and their lookup entries, the old erase count, the
 * cleared magic) may read anything. */
bool srx_torn_erase(const srx_record_t *r, const uint8_t *raw_block, const uint8_t *target)
{
    bool remnant = false;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        if (r->drop_mask & (1U << e)) continue;
        for (uint32_t i = e * 2; i < e * 2 + 2; ++i)
            if ((raw_block[i] & target[i]) != target[i]) return false;
        const uint8_t *now = raw_block + (1 + e) * SRX_PAGE, *want = target + (1 + e) * SRX_PAGE;
        bool kept = (r->kept_mask & (1U << e)) != 0;
        for (uint32_t i = 0; i < SRX_PAGE; ++i) {
            if ((now[i] & want[i]) != want[i]) return false;
            remnant |= kept && now[i] != 0xFF;
        }
    }
    for (uint32_t i = SRX_ENTRIES * 2; i < SRX_LOOKUP_BYTES; ++i)
        if ((raw_block[i] & target[i]) != target[i]) return false;
    return remnant;
}

srx_action_t srx_decide(const srx_record_t *r, const uint8_t *raw_block, const uint8_t *target, uint32_t block_count,
                        uint32_t running_tag, bool force_sources)
{
    if (r->done != 0xFFFFFFFFU || srx_is_source(r)) return SRX_ACT_NONE;
    if ((r->flags & SRX_FLAG_UNCONFIRMED) || r->image_tag != running_tag || force_sources ||
        r->block_count != block_count)
        return SRX_ACT_SOURCE;
    if (srx_attempts_used(r->attempts) >= SRX_ATTEMPTS) return SRX_ACT_EXHAUSTED;
    uint32_t crc = srp_crc32(0, raw_block, SRX_BLOCK);
    if (crc == r->new_crc) return SRX_ACT_REASSERT;
    /* No magic: no mount has happened since (any mount erases such a block),
     * so the only writes since the record are the round's own. */
    if (!srx_has_magic(raw_block, block_count, r->block) || crc == r->old_crc) return SRX_ACT_REDO;
    /* The same image, no journal sources: only the round's own erase, torn. */
    if (target && srx_torn_erase(r, raw_block, target)) return SRX_ACT_REDO;
    return SRX_ACT_CHANGED;
}

static void recover_one(const srx_io_t *io, uint32_t slot, uint32_t block_count, uint32_t running_tag,
                        bool force_sources, uint8_t *work, srx_area_t *area)
{
    uint8_t *sector = work, *target = work + SRX_BLOCK, *raw = work + 2 * SRX_BLOCK;
    char message[160];
    srx_record_t r;
    if (!io->area_read(io->context, slot * SRX_BLOCK, sector, SRX_BLOCK) || !srx_decode(sector, &r, target)) return;
    bool read = r.block < block_count && io->store_read(io->context, r.block * SRX_BLOCK, raw, SRX_BLOCK);
    if (!read && r.block < block_count) {
        area->failed++;
        return; /* the next boot tries again; nothing was written */
    }
    srx_action_t action = srx_decide(&r, raw, target, block_count, running_tag, force_sources);
    switch (action) {
    case SRX_ACT_NONE: return;
    case SRX_ACT_REASSERT: {
        /* The block is complete: a torn magic or erase count may still read
         * right, so both are programmed again, then done. */
        bool ok = io->store_write(io->context, r.block * SRX_BLOCK + SRX_ERASE_COUNT_OFFSET,
                                  target + SRX_ERASE_COUNT_OFFSET, 2) &&
            io->store_write(io->context, r.block * SRX_BLOCK + SRX_MAGIC_OFFSET, target + SRX_MAGIC_OFFSET, 2) &&
            io->store_read(io->context, r.block * SRX_BLOCK, raw, SRX_BLOCK) && !memcmp(raw, target, SRX_BLOCK) &&
            srx_clear_bits(io, slot, SRX_OFF_DONE, 0xFFFFFFFFU);
        if (!ok) {
            area->failed++;
            return;
        }
        area->reasserted++;
        area->slot[slot] = SRX_SLOT_DONE;
        snprintf(message, sizeof(message), "block %u: complete after a reset; magic re-asserted, record done",
                 (unsigned)r.block);
        say(io, "WARN", "STORAGE_REPAIR_RECLAIM_REDONE", message);
        return;
    }
    case SRX_ACT_REDO: {
        unsigned used = srx_attempts_used(r.attempts);
        if (!srx_clear_bits(io, slot, SRX_OFF_ATTEMPTS, 1U << used)) {
            area->failed++;
            return;
        }
        srx_write_t written = srx_write_block(io, r.block, target, raw, 2);
        if (written == SRX_WRITE_OK && srx_clear_bits(io, slot, SRX_OFF_DONE, 0xFFFFFFFFU)) {
            area->redone++;
            area->slot[slot] = SRX_SLOT_DONE;
            snprintf(message, sizeof(message), "block %u completed from the redo area after a reset (attempt %u)",
                     (unsigned)r.block, used + 1);
            say(io, "WARN", "STORAGE_REPAIR_RECLAIM_REDONE", message);
            return;
        }
        if (written != SRX_WRITE_VERIFY) {
            area->failed++;
            return; /* an I/O error: the next boot tries again (the tally bounds it) */
        }
        snprintf(message, sizeof(message), "block %u would not verify after a reset; its record serves as a source",
                 (unsigned)r.block);
        say(io, "ERROR", "STORAGE_REPAIR_RECLAIM_FAILED", message);
        action = SRX_ACT_EXHAUSTED;
        break;
    }
    default: break;
    }
    if (!srx_clear_bits(io, slot, SRX_OFF_SETTLE, SRX_SETTLE_SOURCE)) {
        area->failed++;
        return;
    }
    area->slot[slot] = SRX_SLOT_SOURCE;
    area->to_sources++;
    if (action == SRX_ACT_EXHAUSTED) area->exhausted++;
    snprintf(message, sizeof(message), "block %u: record %u.%u is a byte source (%s)", (unsigned)r.block,
             (unsigned)r.reclaim_id, (unsigned)r.sequence,
             action == SRX_ACT_CHANGED     ? "the block was written since"
             : action == SRX_ACT_EXHAUSTED ? "its redo attempts are used"
             : (r.flags & SRX_FLAG_UNCONFIRMED) ? "written by an unconfirmed image"
                                                : "another image or the journal");
    say(io, "ERROR", "STORAGE_REPAIR_RECLAIM_SOURCES", message);
}

uint32_t srx_recover(const srx_io_t *io, uint32_t block_count, uint32_t running_tag, bool force_sources, uint8_t *work,
                     srx_area_t *area)
{
    /* Oldest record first, each once. */
    uint8_t order[SRX_SLOTS];
    uint32_t count = 0;
    for (uint32_t s = 0; s < area->slots; ++s) {
        if (area->slot[s] != SRX_SLOT_OPEN) continue;
        uint32_t at = count++;
        while (at && area->sequence[order[at - 1]] > area->sequence[s]) {
            order[at] = order[at - 1];
            --at;
        }
        order[at] = (uint8_t)s;
    }
    for (uint32_t i = 0; i < count; ++i) recover_one(io, order[i], block_count, running_tag, force_sources, work, area);
    uint32_t open = 0;
    for (uint32_t s = 0; s < area->slots; ++s) open += area->slot[s] == SRX_SLOT_OPEN;
    return open;
}

bool srx_journal_after_recovery(srp_reclaim_t *journal, bool inspected, uint32_t open_left, uint32_t sources_left)
{
    uint32_t flags = journal->flags;
    if (!inspected) {
        if (flags & SRP_RECLAIM_ACTIVE) journal->flags |= SRP_RECLAIM_SOURCES;
    } else {
        if (open_left) journal->flags |= SRP_RECLAIM_SOURCES | SRP_RECLAIM_ACTIVE;
        else journal->flags &= ~(uint32_t)(SRP_RECLAIM_ACTIVE | SRP_RECLAIM_SOURCES);
        if (sources_left) journal->flags |= SRP_RECLAIM_HELD;
        else journal->flags &= ~(uint32_t)SRP_RECLAIM_HELD;
    }
    if (journal->flags == flags) return false;
    srp_reclaim_seal(journal);
    return true;
}

uint32_t srx_sources_left(const srx_area_t *area)
{
    uint32_t n = 0;
    for (uint32_t s = 0; s < area->slots; ++s) n += area->slot[s] == SRX_SLOT_SOURCE;
    return n;
}

bool srx_sources_unread(const srp_reclaim_t *journal, bool inspected)
{
    return !inspected && (journal->flags & (SRP_RECLAIM_ACTIVE | SRP_RECLAIM_SOURCES | SRP_RECLAIM_HELD));
}
