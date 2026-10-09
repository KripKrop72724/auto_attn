#include "storage_orphan_core.h"
#include "storage_recovery_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ESP-IDF SPIFFS on-flash layout (CONFIG_SPIFFS_OBJ_NAME_LEN 32,
 * CONFIG_SPIFFS_META_LENGTH 4, unaligned index tables, 16-bit ids). The host
 * tests compare every offset with ESP-IDF's own spiffs_nucleus.h. */
#define SO_PH_SIZE 5U               /* spiffs_page_header */
#define SO_PH_FLAGS 4U
#define SO_IX_HDR_SIZE_AT 8U        /* spiffs_page_object_ix_header.size */
#define SO_IX_HDR_NAME_AT 13U
#define SO_IX_HDR_LEN 49U           /* sizeof(spiffs_page_object_ix_header) */
#define SO_IX_LEN 8U                /* sizeof(spiffs_page_object_ix) */
#define SO_NAME_LEN 32U
#define SO_FLAG_USED 0x01U          /* Active low, as every flag */
#define SO_FLAG_FINAL 0x02U
#define SO_FLAG_INDEX 0x04U
#define SO_FLAG_IXDELE 0x40U
#define SO_FLAG_DELET 0x80U
#define SO_ID_IX 0x8000U
#define SO_ID_FREE 0xffffU
#define SO_ID_DELETED 0x0000U
#define SO_SIZE_WINDOW 4096U        /* The stopped removal may have shortened the header */
#define SO_ROW_MAX SR_RECORD_MAX_BYTES
#define SO_PACE_EVERY 256U

typedef struct {
    const so_flash_t *flash;
    const so_target_t *target;
    so_outcome_t *outcome;
    uint32_t pages_per_block, lookup_pages, entries, blocks, max_pages, payload;
    uint16_t *lookup;
    uint8_t *page;
    uint32_t operations;
} so_run_t;

const char *so_result_name(so_result_t result)
{
    switch (result) {
    case SO_NOTHING_TO_DO: return "NOTHING_TO_DO";
    case SO_COMPLETE: return "COMPLETE";
    case SO_REFUSED: return "REFUSED";
    case SO_FAILED: return "FAILED";
    }
    return "UNKNOWN";
}

static void finish(so_run_t *run, so_result_t result, const char *code)
{
    run->outcome->result = result;
    snprintf(run->outcome->code, sizeof(run->outcome->code), "%s", code);
}

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint32_t page_of(const so_run_t *run, uint32_t index)
{
    return (index / run->entries) * run->pages_per_block + run->lookup_pages + index % run->entries;
}

static uint32_t lookup_address(const so_run_t *run, uint32_t index)
{
    return (index / run->entries) * run->pages_per_block * run->target->page_size + (index % run->entries) * 2U;
}

static void pace(so_run_t *run)
{
    if (run->flash->pace && ++run->operations % SO_PACE_EVERY == 0) run->flash->pace(run->flash->context);
}

static bool read_at(so_run_t *run, uint32_t address, void *buffer, size_t length)
{
    pace(run);
    return run->flash->read(run->flash->context, address, buffer, length);
}

static bool load_lookup(so_run_t *run, uint64_t *used)
{
    *used = 0;
    for (uint32_t block = 0; block < run->blocks; ++block) {
        uint32_t first = block * run->entries;
        if (!read_at(run, lookup_address(run, first), run->page, run->entries * 2U)) return false;
        for (uint32_t e = 0; e < run->entries; ++e) {
            run->lookup[first + e] = le16(run->page + e * 2U);
            if (run->lookup[first + e] != SO_ID_FREE && run->lookup[first + e] != SO_ID_DELETED) *used += run->payload;
        }
    }
    return true;
}

static bool valid_index_page(uint8_t flags)
{
    return !(flags & (SO_FLAG_USED | SO_FLAG_FINAL | SO_FLAG_INDEX)) && (flags & SO_FLAG_DELET);
}

/* spiffs_page_data_check: the checks a SPIFFS read applies to a data page. */
static bool readable_data_page(so_run_t *run, uint16_t pix, uint32_t span)
{
    if (pix == SO_ID_FREE || pix % run->pages_per_block < run->lookup_pages || pix > run->max_pages) return false;
    uint8_t header[SO_PH_SIZE];
    if (!read_at(run, (uint32_t)pix * run->target->page_size, header, sizeof(header))) return false;
    uint8_t flags = header[SO_PH_FLAGS];
    return !(flags & SO_FLAG_USED) && (flags & SO_FLAG_DELET) && !(flags & SO_FLAG_FINAL) &&
           (flags & SO_FLAG_INDEX) && le16(header + 2) == span;
}

typedef struct {
    so_run_t *run;
    char *row;
    size_t length;
    uint32_t start;
    bool tainted;
    uint32_t capacity;
} so_rows_t;

static bool in_unreadable(const so_target_t *target, uint32_t start, uint32_t end)
{
    for (size_t i = 0; i < target->unreadable_count; ++i) {
        uint32_t region_end = target->unreadable[i].offset + target->unreadable[i].length;
        if (start < region_end && end > target->unreadable[i].offset) return true;
    }
    return false;
}

static void end_row(so_rows_t *rows, uint32_t end)
{
    so_outcome_t *outcome = rows->run->outcome;
    bool empty = rows->length == 0 || (rows->length == 1 && rows->row[0] == '\r');
    if (!empty && !rows->tainted) {
        outcome->rows++;
        uint8_t uid[SR_UID_BYTES];
        /* Only a row the receipted run read whole is known to be in custody. */
        if (!in_unreadable(rows->run->target, rows->start, end) && sr_row_event_uid(rows->row, rows->length, uid)) {
            /* Counted beyond capacity so an excess refuses the run. */
            if (outcome->uids < rows->capacity)
                memcpy(outcome->uid_bytes + (size_t)outcome->uids * SR_UID_BYTES, uid, SR_UID_BYTES);
            outcome->uids++;
        }
    }
    rows->length = 0;
    rows->tainted = false;
    rows->start = end;
}

static void feed(so_rows_t *rows, uint32_t offset, const uint8_t *bytes, uint32_t length)
{
    for (uint32_t i = 0; i < length; ++i) {
        if (bytes[i] == '\n') {
            end_row(rows, offset + i + 1);
            continue;
        }
        if (rows->length == SO_ROW_MAX) rows->tainted = true;
        else rows->row[rows->length++] = (char)bytes[i];
    }
}

/* Reads the object as SPIFFS would: header table, then the one valid index
 * page per index span; an ambiguous or missing index page hides its spans. */
static bool reconstruct(so_run_t *run, uint16_t object, uint32_t header_pix, so_rows_t *rows)
{
    so_outcome_t *outcome = run->outcome;
    const uint32_t page_size = run->target->page_size;
    const uint32_t header_entries = (page_size - SO_IX_HDR_LEN) / 2U;
    const uint32_t index_entries = (page_size - SO_IX_LEN) / 2U;
    uint8_t *table = malloc(page_size);
    if (!table) return false;
    bool ok = true;
    uint32_t loaded = UINT32_MAX;
    bool table_valid = false;
    uint32_t position = 0;
    for (uint32_t span = 0; ok && span < outcome->spans; ++span) {
        uint32_t ix_span = span < header_entries ? 0 : 1 + (span - header_entries) / index_entries;
        if (ix_span != loaded) {
            loaded = ix_span;
            table_valid = false;
            uint32_t found = UINT32_MAX, count = 0;
            if (ix_span == 0) {
                found = header_pix;
                count = 1;
            } else {
                for (uint32_t i = 0; ok && i < run->blocks * run->entries; ++i) {
                    if (run->lookup[i] != (uint16_t)(object | SO_ID_IX)) continue;
                    uint8_t header[SO_PH_SIZE];
                    ok = read_at(run, page_of(run, i) * page_size, header, sizeof(header));
                    if (ok && le16(header) == (uint16_t)(object | SO_ID_IX) && le16(header + 2) == ix_span &&
                        valid_index_page(header[SO_PH_FLAGS])) {
                        found = page_of(run, i);
                        count++;
                    }
                }
            }
            if (ok && count == 1) {
                ok = read_at(run, found * page_size, table, page_size);
                table_valid = ok;
            }
        }
        uint32_t length = outcome->header_size - position < run->payload ? outcome->header_size - position : run->payload;
        uint16_t pix = SO_ID_FREE;
        if (table_valid) {
            uint32_t entry = ix_span == 0 ? span : (span - header_entries) % index_entries;
            pix = le16(table + (ix_span == 0 ? SO_IX_HDR_LEN : SO_IX_LEN) + entry * 2U);
        }
        if (ok && table_valid && readable_data_page(run, pix, span)) {
            ok = read_at(run, (uint32_t)pix * page_size + SO_PH_SIZE, run->page, length);
            if (ok) {
                feed(rows, position, run->page, length);
                outcome->readable_spans++;
            }
        } else {
            rows->tainted = true;
        }
        position += length;
    }
    if (ok && rows->length) end_row(rows, outcome->header_size);
    free(table);
    return ok;
}

/* spiffs_page_delete: lookup entry first, then the page's own flags. */
static bool free_page(so_run_t *run, uint32_t index)
{
    static const uint8_t deleted[2] = {0, 0};
    uint32_t address = page_of(run, index) * run->target->page_size + SO_PH_FLAGS;
    uint8_t flags;
    if (!run->flash->write(run->flash->context, lookup_address(run, index), deleted, 2) ||
        !read_at(run, address, &flags, 1))
        return false;
    flags &= (uint8_t)~(SO_FLAG_DELET | SO_FLAG_USED);
    pace(run);
    if (!run->flash->write(run->flash->context, address, &flags, 1)) return false;
    run->lookup[index] = SO_ID_DELETED;
    run->outcome->pages_freed++;
    return true;
}

void so_complete_orphan(const so_flash_t *flash, const so_target_t *target, so_outcome_t *outcome)
{
    if (!outcome) return;
    memset(outcome, 0, sizeof(*outcome));
    so_run_t run = {.flash = flash, .target = target, .outcome = outcome};
    if (!flash || !flash->read || !flash->write || !target || !target->name ||
        strlen(target->name) >= SO_NAME_LEN || !target->receipted_size || !target->expected_uids ||
        target->page_size < 64 || target->block_size % target->page_size ||
        target->partition_size % target->block_size || target->block_size / target->page_size < 2) {
        finish(&run, SO_REFUSED, "STORAGE_ORPHAN_INVALID_REQUEST");
        return;
    }
    run.pages_per_block = target->block_size / target->page_size;
    run.lookup_pages = run.pages_per_block * 2U / target->page_size;
    if (!run.lookup_pages) run.lookup_pages = 1;
    run.entries = run.pages_per_block - run.lookup_pages;
    run.blocks = target->partition_size / target->block_size;
    run.max_pages = target->partition_size / target->page_size;
    run.payload = target->page_size - SO_PH_SIZE;
    if (run.lookup_pages != 1 || target->page_size < SO_IX_HDR_LEN + 2U) {
        finish(&run, SO_REFUSED, "STORAGE_ORPHAN_GEOMETRY");
        return;
    }
    run.lookup = malloc((size_t)run.blocks * run.entries * sizeof(uint16_t));
    run.page = malloc(target->page_size);
    uint32_t capacity = target->expected_uids;
    outcome->uid_bytes = malloc((size_t)capacity * SR_UID_BYTES);
    char *row = malloc(SO_ROW_MAX);
    if (!run.lookup || !run.page || !outcome->uid_bytes || !row) {
        finish(&run, SO_FAILED, "STORAGE_ORPHAN_MEMORY");
        goto done;
    }
    if (!load_lookup(&run, &outcome->used_before)) {
        finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
        goto done;
    }
    /* Exactly one hidden header with this name and a receipted size. */
    uint8_t header[SO_IX_HDR_NAME_AT + SO_NAME_LEN];
    uint32_t header_index = 0;
    uint16_t object = 0;
    for (uint32_t i = 0; i < run.blocks * run.entries; ++i) {
        uint16_t id = run.lookup[i];
        if (id == SO_ID_FREE || !(id & SO_ID_IX)) continue;
        if (!read_at(&run, page_of(&run, i) * target->page_size, header, sizeof(header))) {
            finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
            goto done;
        }
        uint8_t flags = header[SO_PH_FLAGS];
        if (le16(header) != id || le16(header + 2) != 0 || !valid_index_page(flags) || (flags & SO_FLAG_IXDELE) ||
            memcmp(header + SO_IX_HDR_NAME_AT, target->name, strlen(target->name) + 1))
            continue;
        uint32_t size = le32(header + SO_IX_HDR_SIZE_AT);
        if (size > target->receipted_size || size + SO_SIZE_WINDOW < target->receipted_size) continue;
        outcome->candidates++;
        header_index = i;
        object = (uint16_t)(id & ~SO_ID_IX);
        outcome->header_size = size;
    }
    if (outcome->candidates == 0) {
        finish(&run, SO_NOTHING_TO_DO, "STORAGE_ORPHAN_NONE");
        goto done;
    }
    if (outcome->candidates > 1) {
        finish(&run, SO_REFUSED, "STORAGE_ORPHAN_AMBIGUOUS");
        goto done;
    }
    /* Every page the lookup assigns to the object; a page whose own header
     * names another object is left alone. A visible header with the same id
     * would mean the object is still a file. */
    uint32_t data_pages = 0;
    for (uint32_t i = 0; i < run.blocks * run.entries; ++i) {
        uint16_t id = run.lookup[i];
        if (id != object && id != (uint16_t)(object | SO_ID_IX)) continue;
        uint8_t own[SO_PH_SIZE];
        if (!read_at(&run, page_of(&run, i) * target->page_size, own, sizeof(own))) {
            finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
            goto done;
        }
        if (le16(own) != id) {
            outcome->pages_skipped++;
            continue;
        }
        if (i != header_index && (id & SO_ID_IX) && le16(own + 2) == 0 && valid_index_page(own[SO_PH_FLAGS]) &&
            (own[SO_PH_FLAGS] & SO_FLAG_IXDELE)) {
            finish(&run, SO_REFUSED, "STORAGE_ORPHAN_STILL_VISIBLE");
            goto done;
        }
        outcome->object_pages++;
        if (!(id & SO_ID_IX)) data_pages++;
    }
    outcome->spans = (outcome->header_size + run.payload - 1) / run.payload;
    uint32_t receipted_spans = (target->receipted_size + run.payload - 1) / run.payload;
    if (data_pages > receipted_spans + 64U || outcome->object_pages > receipted_spans + receipted_spans / 64U + 72U) {
        finish(&run, SO_REFUSED, "STORAGE_ORPHAN_PAGE_COUNT");
        goto done;
    }
    so_rows_t rows = {.run = &run, .row = row, .capacity = capacity};
    if (!reconstruct(&run, object, page_of(&run, header_index), &rows)) {
        finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
        goto done;
    }
    if (outcome->uids > target->expected_uids ||
        outcome->uids + target->uid_shortfall_limit < target->expected_uids) {
        finish(&run, SO_REFUSED, "STORAGE_ORPHAN_UID_MISMATCH");
        goto done;
    }
    /* Data and index pages first; the header last, so an interruption
     * leaves the object identifiable and nothing else changed. */
    for (int pass = 0; pass < 2; ++pass) {
        for (uint32_t i = 0; i < run.blocks * run.entries; ++i) {
            uint16_t id = run.lookup[i];
            if ((id != object && id != (uint16_t)(object | SO_ID_IX)) || (i == header_index) != (pass == 1)) continue;
            uint8_t own[2];
            if (!read_at(&run, page_of(&run, i) * target->page_size, own, sizeof(own))) {
                finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
                goto done;
            }
            if (le16(own) != id) continue;
            if (!free_page(&run, i)) {
                finish(&run, SO_FAILED, "STORAGE_ORPHAN_WRITE");
                goto done;
            }
        }
    }
    if (!load_lookup(&run, &outcome->used_after)) {
        finish(&run, SO_FAILED, "STORAGE_ORPHAN_READ");
        goto done;
    }
    finish(&run, SO_COMPLETE, "STORAGE_ORPHAN_COMPLETE");
done:
    if (outcome->result != SO_COMPLETE) {
        free(outcome->uid_bytes);
        outcome->uid_bytes = NULL;
        if (outcome->result != SO_NOTHING_TO_DO) outcome->uids = 0;
    }
    free(row);
    free(run.page);
    free(run.lookup);
}
