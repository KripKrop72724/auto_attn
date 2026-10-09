/* Completes a stopped SPIFFS removal with ESP-IDF 5.5.3's own SPIFFS, built
 * for the host with the Zone Lite configuration, on an emulated NOR flash.
 * Every orphan is produced by SPIFFS_remove() itself on a file with corrupt
 * data pages, and every result is checked by remounting and SPIFFS_check(). */
#include "spiffs.h"
#include "spiffs_nucleus.h"
#include "storage_orphan_core.h"
#include "storage_recovery_core.h"

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FLASH_SIZE (768U * 1024U)
#define BLOCK 4096U
#define PAGE 256U
#define PAYLOAD (PAGE - 5U)
/* Names as ESP-IDF's VFS stores them: the path below the mount point. */
#define BLOCKED "/blocked_identity.jsonl"
#define MAX_ROWS 1200U

/* The engine's fixed offsets must be ESP-IDF's layout. */
_Static_assert(sizeof(spiffs_page_header) == 5 && offsetof(spiffs_page_header, flags) == 4, "page header");
_Static_assert(offsetof(spiffs_page_object_ix_header, size) == 8, "header size field");
_Static_assert(offsetof(spiffs_page_object_ix_header, name) == 13, "header name field");
_Static_assert(sizeof(spiffs_page_object_ix_header) == 49, "index header");
_Static_assert(sizeof(spiffs_page_object_ix) == 8, "index page");
_Static_assert(SPIFFS_PH_FLAG_USED == 0x01 && SPIFFS_PH_FLAG_FINAL == 0x02 && SPIFFS_PH_FLAG_INDEX == 0x04 &&
               SPIFFS_PH_FLAG_IXDELE == 0x40 && SPIFFS_PH_FLAG_DELET == 0x80, "page flags");
_Static_assert(SPIFFS_OBJ_ID_IX_FLAG == 0x8000, "index id flag");

static uint8_t g_flash[FLASH_SIZE];
static long g_write_budget = -1;
static unsigned g_check_fixes;

static s32_t hal_read(struct spiffs_t *fs, u32_t addr, u32_t size, u8_t *dst)
{
    (void)fs;
    memcpy(dst, g_flash + addr, size);
    return SPIFFS_OK;
}

static s32_t hal_write(struct spiffs_t *fs, u32_t addr, u32_t size, u8_t *src)
{
    (void)fs;
    for (u32_t i = 0; i < size; ++i) g_flash[addr + i] &= src[i]; /* NOR: 1 -> 0 only */
    return SPIFFS_OK;
}

static s32_t hal_erase(struct spiffs_t *fs, u32_t addr, u32_t size)
{
    (void)fs;
    memset(g_flash + addr, 0xff, size);
    return SPIFFS_OK;
}

static void check_report(struct spiffs_t *fs, spiffs_check_type type, spiffs_check_report report, u32_t a, u32_t b)
{
    (void)fs; (void)type; (void)a; (void)b;
    if (report != SPIFFS_CHECK_PROGRESS) g_check_fixes++;
}

static spiffs g_fs;
static u8_t g_work[PAGE * 2];
static u8_t g_fds[sizeof(spiffs_fd) * 8];
static u8_t g_cache[sizeof(spiffs_cache) + 8 * (sizeof(spiffs_cache_page) + PAGE)];

static void mount(void)
{
    spiffs_config cfg = {
        .hal_read_f = hal_read, .hal_write_f = hal_write, .hal_erase_f = hal_erase,
        .phys_size = FLASH_SIZE, .phys_addr = 0, .phys_erase_block = BLOCK,
        .log_block_size = BLOCK, .log_page_size = PAGE,
    };
    s32_t res = SPIFFS_mount(&g_fs, &cfg, g_work, g_fds, sizeof(g_fds), g_cache, sizeof(g_cache), check_report);
    if (res == SPIFFS_ERR_NOT_A_FS) {
        SPIFFS_unmount(&g_fs);
        assert(SPIFFS_format(&g_fs) == SPIFFS_OK);
        res = SPIFFS_mount(&g_fs, &cfg, g_work, g_fds, sizeof(g_fds), g_cache, sizeof(g_cache), check_report);
    }
    assert(res == SPIFFS_OK);
}

static void fresh(void)
{
    memset(g_flash, 0xff, sizeof(g_flash));
    mount();
}

static u32_t used_bytes(void)
{
    u32_t total = 0, used = 0;
    assert(SPIFFS_info(&g_fs, &total, &used) == SPIFFS_OK);
    return used;
}

static void append(const char *name, const char *data, size_t length)
{
    spiffs_file fh = SPIFFS_open(&g_fs, name, SPIFFS_O_CREAT | SPIFFS_O_RDWR | SPIFFS_O_APPEND, 0);
    assert(fh >= 0);
    assert(SPIFFS_write(&g_fs, fh, (void *)data, (s32_t)length) == (s32_t)length);
    assert(SPIFFS_close(&g_fs, fh) == SPIFFS_OK);
}

static char *read_file(const char *name, size_t *length)
{
    spiffs_stat st;
    if (SPIFFS_stat(&g_fs, name, &st) != SPIFFS_OK) return NULL;
    char *data = malloc(st.size + 1);
    assert(data);
    spiffs_file fh = SPIFFS_open(&g_fs, name, SPIFFS_O_RDONLY, 0);
    assert(fh >= 0);
    s32_t got = st.size ? SPIFFS_read(&g_fs, fh, data, (s32_t)st.size) : 0;
    assert(got == (s32_t)st.size);
    SPIFFS_close(&g_fs, fh);
    data[st.size] = 0;
    *length = st.size;
    return data;
}

static bool flash_read(void *context, uint32_t address, void *buffer, size_t length)
{
    (void)context;
    if (address + length > FLASH_SIZE) return false;
    memcpy(buffer, g_flash + address, length);
    return true;
}

static bool flash_write(void *context, uint32_t address, const void *buffer, size_t length)
{
    (void)context;
    if (address + length > FLASH_SIZE || g_write_budget == 0) return false;
    if (g_write_budget > 0) g_write_budget--;
    for (size_t i = 0; i < length; ++i) g_flash[address + i] &= ((const uint8_t *)buffer)[i];
    return true;
}

static unsigned g_paced;
static void flash_pace(void *context) { (void)context; g_paced++; }

static const so_flash_t k_flash = {.read = flash_read, .write = flash_write, .pace = flash_pace};

/* One retained blocked file built like the field one: JSON rows of about 355
 * bytes, written interleaved with the other queues. */
typedef struct {
    char *data;
    size_t length;
    uint32_t row_start[MAX_ROWS], row_end[MAX_ROWS];
    uint8_t uid[MAX_ROWS][32];
    unsigned rows;
} blocked_t;

static void make_row(char *out, size_t capacity, unsigned index, unsigned seed, uint8_t uid[32])
{
    char hex[65];
    for (unsigned b = 0; b < 32; ++b) {
        /* Unique per row and generation: index and seed occupy the first bytes. */
        unsigned value = b == 0 ? index & 0xff : b == 1 ? index >> 8 : b == 2 ? seed : index * 131U + b * 7U;
        uid[b] = (uint8_t)(value & 0xff);
        snprintf(hex + b * 2, 3, "%02x", uid[b]);
    }
    snprintf(out, capacity,
             "{\"capturetype\":\"RECONCILE\",\"device_serial\":\"CJH9211060009\",\"event_uid\":\"%s\","
             "\"punch_time\":\"2026-09-%02uT08:%02u:00+05:00\",\"user_id\":\"%u\",\"verify\":\"FP\","
             "\"zone\":\"ZONE-PESHAWAR-02\",\"seq\":%u,\"pad\":\"%0*u\"}\n",
             hex, 1 + index % 28, index % 60, 1000 + index, index, (int)(40 + index % 9), 0U);
}

static void write_world(blocked_t *blocked, unsigned rows, unsigned seed, char **pending, size_t *pending_length)
{
    memset(blocked, 0, sizeof(*blocked));
    size_t capacity = (size_t)rows * 512;
    blocked->data = calloc(1, capacity);
    *pending = calloc(1, capacity);
    assert(blocked->data && *pending);
    *pending_length = 0;
    char row[512];
    for (unsigned i = 0; i < rows; ++i) {
        make_row(row, sizeof(row), i, seed, blocked->uid[i]);
        size_t length = strlen(row);
        blocked->row_start[i] = (uint32_t)blocked->length;
        memcpy(blocked->data + blocked->length, row, length);
        blocked->length += length;
        blocked->row_end[i] = (uint32_t)blocked->length;
        append(BLOCKED, row, length);
        if (i % 3 == 0) {
            int written = snprintf(row, sizeof(row), "{\"pending\":%u,\"seed\":%u}\n", i, seed);
            memcpy(*pending + *pending_length, row, (size_t)written);
            *pending_length += (size_t)written;
            append("/pending.jsonl", row, (size_t)written);
        }
        if (i % 5 == 0) append("/acked_uids.txt", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", 65);
    }
    blocked->rows = rows;
}

static spiffs_obj_id object_of(const char *name)
{
    spiffs_stat st;
    assert(SPIFFS_stat(&g_fs, name, &st) == SPIFFS_OK);
    return st.obj_id;
}

/* The valid data page of one span, found through the lookup tables. */
static uint32_t data_page(spiffs_obj_id object, uint32_t span)
{
    const uint32_t per_block = BLOCK / PAGE;
    for (uint32_t block = 0; block < FLASH_SIZE / BLOCK; ++block) {
        for (uint32_t entry = 0; entry < per_block - 1; ++entry) {
            const uint8_t *lu = g_flash + block * BLOCK + entry * 2;
            if ((uint16_t)(lu[0] | (lu[1] << 8)) != object) continue;
            uint32_t pix = block * per_block + 1 + entry;
            const uint8_t *ph = g_flash + pix * PAGE;
            if ((uint16_t)(ph[2] | (ph[3] << 8)) == span && (ph[4] & SPIFFS_PH_FLAG_DELET)) return pix;
        }
    }
    assert(!"data page not found");
    return 0;
}

typedef struct {
    so_region_t regions[8];
    size_t count;
} damage_t;

/* Corruptions as found on Peshawar-02: a span mismatch (EIO) mid-file, and in
 * the tail a page deleted under a live reference (ENOENT) plus another span
 * mismatch where SPIFFS_remove() stops. Regions mirror the transfer's 32-byte
 * probe resolution. */
static void damage(spiffs_obj_id object, const blocked_t *blocked, damage_t *out)
{
    uint32_t spans = (uint32_t)((blocked->length + PAYLOAD - 1) / PAYLOAD);
    uint32_t mid = spans * 2 / 5, deleted = spans - 7, stop = spans - 5;
    uint32_t list[3] = {mid, deleted, stop};
    out->count = 0;
    for (int i = 0; i < 3; ++i) {
        uint32_t pix = data_page(object, list[i]);
        uint8_t *ph = g_flash + pix * PAGE;
        if (list[i] == deleted) ph[4] &= (uint8_t)~SPIFFS_PH_FLAG_DELET;
        else ph[2] ^= 0x01; /* span_ix no longer matches the index */
        uint32_t start = list[i] * PAYLOAD & ~31U, end = ((list[i] + 1) * PAYLOAD + 31U) & ~31U;
        out->regions[out->count++] = (so_region_t){start, end - start};
    }
}

static bool outside(const damage_t *damage_list, uint32_t start, uint32_t end)
{
    for (size_t i = 0; i < damage_list->count; ++i)
        if (start < damage_list->regions[i].offset + damage_list->regions[i].length && end > damage_list->regions[i].offset)
            return false;
    return true;
}

typedef struct {
    blocked_t blocked;
    char *pending;
    size_t pending_length;
    damage_t damage;
    unsigned receipted_uids;
    uint32_t used_after_remove;
} world_t;

/* Builds the field state: rows receipted, then SPIFFS_remove() stops early. */
static void make_orphan(world_t *world, unsigned rows, unsigned seed)
{
    fresh();
    write_world(&world->blocked, rows, seed, &world->pending, &world->pending_length);
    spiffs_obj_id object = object_of(BLOCKED);
    SPIFFS_unmount(&g_fs);
    damage(object, &world->blocked, &world->damage);
    /* The receipted run counts the UIDs of whole valid rows it could read. */
    world->receipted_uids = 0;
    for (unsigned i = 0; i < world->blocked.rows; ++i) {
        uint8_t uid[32];
        const blocked_t *b = &world->blocked;
        if (outside(&world->damage, b->row_start[i], b->row_end[i]) &&
            sr_row_event_uid(b->data + b->row_start[i], b->row_end[i] - b->row_start[i] - 1, uid)) {
            assert(!memcmp(uid, b->uid[i], 32));
            world->receipted_uids++;
        }
    }
    assert(world->receipted_uids + 6 >= world->blocked.rows);
    mount();
    uint32_t before = used_bytes();
    (void)SPIFFS_remove(&g_fs, BLOCKED);
    spiffs_stat st;
    assert(SPIFFS_stat(&g_fs, BLOCKED, &st) != SPIFFS_OK);     /* The name is gone ... */
    world->used_after_remove = used_bytes();
    assert(before - world->used_after_remove <= 8 * PAYLOAD);   /* ... the pages are not. */
    SPIFFS_unmount(&g_fs);
}

static so_target_t target_for(const world_t *world)
{
    return (so_target_t){
        .partition_size = FLASH_SIZE, .block_size = BLOCK, .page_size = PAGE, .name = BLOCKED,
        .receipted_size = (uint32_t)world->blocked.length, .unreadable = world->damage.regions,
        .unreadable_count = world->damage.count, .expected_uids = world->receipted_uids,
        .uid_shortfall_limit = 8,
    };
}

static void free_world(world_t *world)
{
    free(world->blocked.data);
    free(world->pending);
}

static void test_completes_the_hidden_file_and_nothing_else(void)
{
    world_t world;
    make_orphan(&world, 900, 1);
    so_target_t target = target_for(&world);
    g_paced = 0;
    so_outcome_t outcome;
    so_complete_orphan(&k_flash, &target, &outcome);
    if (outcome.result != SO_COMPLETE)
        fprintf(stderr, "outcome %s candidates=%u size=%u/%zu pages=%u spans=%u readable=%u rows=%u uids=%u/%u\n",
                outcome.code, outcome.candidates, outcome.header_size, world.blocked.length, outcome.object_pages,
                outcome.spans, outcome.readable_spans, outcome.rows, outcome.uids, world.receipted_uids);
    assert(outcome.result == SO_COMPLETE && !strcmp(outcome.code, "STORAGE_ORPHAN_COMPLETE"));
    /* The stopped removal may already have shortened the header's size. */
    assert(outcome.hidden_headers == 1);
    assert(outcome.candidates == 1 && outcome.header_size <= world.blocked.length &&
           outcome.header_size + 4096 >= world.blocked.length);
    assert(outcome.pages_freed == outcome.object_pages && outcome.pages_skipped == 0);
    assert(outcome.used_before == world.used_after_remove && g_paced > 0);
    /* Every recorded UID belongs to a whole receipted row, in file order; the
     * rows the stopped removal already freed are the only shortfall. */
    assert(outcome.uids <= world.receipted_uids && outcome.uids + 4 >= world.receipted_uids);
    unsigned next = 0;
    for (unsigned i = 0; i < world.blocked.rows && next < outcome.uids; ++i) {
        if (!outside(&world.damage, world.blocked.row_start[i], world.blocked.row_end[i])) continue;
        if (!memcmp(outcome.uid_bytes + (size_t)next * 32, world.blocked.uid[i], 32)) next++;
    }
    assert(next == outcome.uids);
    free(outcome.uid_bytes);
    /* SPIFFS agrees: space released, other files intact, nothing to repair. */
    g_check_fixes = 0;
    mount();
    assert(used_bytes() == outcome.used_after);
    assert(world.used_after_remove - outcome.used_after >= (world.blocked.length / PAYLOAD) * PAYLOAD);
    size_t length = 0;
    char *pending = read_file("/pending.jsonl", &length);
    assert(pending && length == world.pending_length && !memcmp(pending, world.pending, length));
    free(pending);
    spiffs_stat st;
    assert(SPIFFS_stat(&g_fs, BLOCKED, &st) != SPIFFS_OK);
    assert(SPIFFS_check(&g_fs) == SPIFFS_OK && g_check_fixes == 0);
    /* The released pages are reusable: the size of the old file fits again. */
    char *chunk = calloc(1, 4096);
    assert(chunk);
    memset(chunk, 'x', 4096);
    for (size_t written = 0; written < world.blocked.length; written += 4096) append("/refill.bin", chunk, 4096);
    free(chunk);
    SPIFFS_unmount(&g_fs);
    free_world(&world);
}

static void test_no_hidden_file_changes_nothing(void)
{
    world_t world;
    fresh();
    write_world(&world.blocked, 200, 2, &world.pending, &world.pending_length);
    SPIFFS_unmount(&g_fs);
    uint8_t *before = malloc(FLASH_SIZE);
    assert(before);
    memcpy(before, g_flash, FLASH_SIZE);
    so_target_t target = {
        .partition_size = FLASH_SIZE, .block_size = BLOCK, .page_size = PAGE, .name = BLOCKED,
        .receipted_size = (uint32_t)world.blocked.length, .expected_uids = 200, .uid_shortfall_limit = 8,
    };
    so_outcome_t outcome;
    so_complete_orphan(&k_flash, &target, &outcome);
    assert(outcome.result == SO_NOTHING_TO_DO && !strcmp(outcome.code, "STORAGE_ORPHAN_NONE"));
    assert(!outcome.uid_bytes && !memcmp(before, g_flash, FLASH_SIZE));
    free(before);
    free_world(&world);
}

static void expect_refusal_unchanged(so_target_t target, const char *code, so_result_t result)
{
    uint8_t *before = malloc(FLASH_SIZE);
    assert(before);
    memcpy(before, g_flash, FLASH_SIZE);
    so_outcome_t outcome;
    so_complete_orphan(&k_flash, &target, &outcome);
    assert(outcome.result == result && !strcmp(outcome.code, code));
    assert(!outcome.uid_bytes && !memcmp(before, g_flash, FLASH_SIZE));
    free(before);
}

static void test_mismatched_receipt_refuses_unchanged(void)
{
    world_t world;
    make_orphan(&world, 400, 3);
    so_target_t target = target_for(&world);
    target.expected_uids = world.receipted_uids + 20;     /* More rows than the object holds */
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_UID_MISMATCH", SO_REFUSED);
    target = target_for(&world);
    target.uid_shortfall_limit = 0;
    target.expected_uids = world.receipted_uids + 2;
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_UID_MISMATCH", SO_REFUSED);
    target = target_for(&world);
    target.receipted_size += 5000;                        /* Not the receipted generation */
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_NONE", SO_NOTHING_TO_DO);
    target = target_for(&world);
    target.name = "/blocked_recovery.bak";
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_NONE", SO_NOTHING_TO_DO);
    /* The name without ESP-IDF's leading '/' is a different object name. */
    target = target_for(&world);
    target.name = BLOCKED + 1;
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_NONE", SO_NOTHING_TO_DO);
    free_world(&world);
}

static void test_two_hidden_candidates_are_ambiguous(void)
{
    world_t world;
    make_orphan(&world, 300, 4);
    /* A second identical generation, hidden the same way. */
    mount();
    blocked_t again;
    char *pending = NULL;
    size_t pending_length = 0;
    write_world(&again, 300, 4, &pending, &pending_length);
    spiffs_obj_id object = object_of(BLOCKED);
    SPIFFS_unmount(&g_fs);
    damage_t ignored;
    damage(object, &again, &ignored);
    mount();
    (void)SPIFFS_remove(&g_fs, BLOCKED);
    SPIFFS_unmount(&g_fs);
    expect_refusal_unchanged(target_for(&world), "STORAGE_ORPHAN_AMBIGUOUS", SO_REFUSED);
    free(again.data);
    free(pending);
    free_world(&world);
}

static void test_new_visible_file_with_the_same_name_is_kept(void)
{
    world_t world;
    make_orphan(&world, 500, 5);
    mount();
    const char *fresh_row = "{\"event_uid\":\"new\",\"note\":\"written by 2.5.2 after the stopped removal\"}\n";
    append(BLOCKED, fresh_row, strlen(fresh_row));
    SPIFFS_unmount(&g_fs);
    so_target_t target = target_for(&world);
    so_outcome_t outcome;
    so_complete_orphan(&k_flash, &target, &outcome);
    assert(outcome.result == SO_COMPLETE && outcome.candidates == 1);
    free(outcome.uid_bytes);
    g_check_fixes = 0;
    mount();
    size_t length = 0;
    char *kept = read_file(BLOCKED, &length);
    assert(kept && length == strlen(fresh_row) && !memcmp(kept, fresh_row, length));
    free(kept);
    assert(SPIFFS_check(&g_fs) == SPIFFS_OK && g_check_fixes == 0);
    SPIFFS_unmount(&g_fs);
    free_world(&world);
}

static void test_interrupted_release_keeps_the_filesystem_mountable(void)
{
    world_t world;
    make_orphan(&world, 600, 6);
    so_target_t target = target_for(&world);
    so_outcome_t outcome;
    g_write_budget = 300;
    so_complete_orphan(&k_flash, &target, &outcome);
    g_write_budget = -1;
    assert(outcome.result == SO_FAILED && !strcmp(outcome.code, "STORAGE_ORPHAN_WRITE"));
    assert(!outcome.uid_bytes && outcome.uids == 0);
    mount();
    size_t length = 0;
    char *pending = read_file("/pending.jsonl", &length);
    assert(pending && length == world.pending_length && !memcmp(pending, world.pending, length));
    free(pending);
    SPIFFS_unmount(&g_fs);
    /* The header was kept for last, so the object is still found; with data
     * pages already released the UID proof no longer matches and nothing
     * further is changed. */
    expect_refusal_unchanged(target, "STORAGE_ORPHAN_UID_MISMATCH", SO_REFUSED);
    free_world(&world);
}

int main(void)
{
    test_completes_the_hidden_file_and_nothing_else();
    test_no_hidden_file_changes_nothing();
    test_mismatched_receipt_refuses_unchanged();
    test_two_hidden_candidates_are_ambiguous();
    test_new_visible_file_with_the_same_name_is_kept();
    test_interrupted_release_keeps_the_filesystem_mountable();
    puts("spiffs orphan tests passed");
    return 0;
}
