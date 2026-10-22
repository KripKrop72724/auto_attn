/* ZKT 2.7.5 boot storage repair against ESP-IDF 5.5.3's own SPIFFS.
 *
 * NOR flash in RAM (a program only clears bits, an erase sets 0xFF), power
 * cuts that tear a program (a clean prefix, or bits anywhere in it) or an
 * erase and drop every byte of RAM state, an NVS whose blobs survive them,
 * the repair engine through the same ports as the firmware, and the
 * firmware's own legacy_queue.c, file_transaction.c and reliability.c
 * draining the repaired store through a stdio shim.
 *
 * Scenarios build a store, damage it the way the field reports, the
 * power-cut simulator and the reviews showed (13FL's deleted queue head,
 * P02's erased or foreign page and leaked pages, duplicated index pages, a
 * missing index page, torn tails, dirty tails, stale cursors, torn first
 * append sizes, reused object ids, stale and disagreeing copies, stray
 * pages), repair it, and check:
 *   A1 custody: every byte the damage left readable is present at its offset
 *   A2 gaps: exactly the bytes nobody could read are reported, nothing else
 *      (a record flagged merged may span readable bytes between them)
 *   A3 drains: the real lq drain reads every queue to its end; ft recovers
 *   A4 health: appends are accepted, a probe file works, no leak remains, a
 *      queue segment takes "wb"
 *   A5 idempotence: a second check fixes nothing; a second repair is clean
 *   A6 removals: only probe files and the catalog are ever removed
 *   A7 rollback reader: every row a whole-file reader read before, it reads
 *   A8 cuts: a power cut anywhere inside the repair loses nothing a gap
 *      record does not name, and the next boots finish the repair
 *   A9 no stray page of a live file, and SPIFFS's own GC moves nothing a
 *      live file reads
 * and that the metadata mirror leaves the same flash and report. Special
 * runs cover resumes, restores after a check (a test-only hook stands in for
 * a check that changed a held file), stale manifests and the guards; the
 * workload runs the simulator's punches to a crammed store. */
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <setjmp.h>
#include <time.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "spiffs.h"
#include "spiffs_nucleus.h"

#include "command_inbox_merge.h"
#include "file_transaction.h"
#include "legacy_queue.h"
#include "reliability.h"
#include "span_digest.h"
#include "storage_exception.h"
#include "storage_exception_srr.h"
#include "storage_repair_core.h"
#include "storage_repair_mirror.h"
#include "storage_repair_plan.h"

typedef struct shim_file shim_file_t;
int shim_fclose(shim_file_t *file);

#define PAGE 256U
#define BLOCK 4096U
#define SPAN 251U
#define FD_COUNT 4U

static int verbose;
static void dump_write(void);
#define CHECK(condition, ...)                                                       \
    do {                                                                           \
        if (!(condition)) {                                                        \
            fprintf(stderr, "FAIL %s:%d: %s: ", __FILE__, __LINE__, #condition);    \
            fprintf(stderr, __VA_ARGS__);                                          \
            fprintf(stderr, "\n");                                                 \
            exit(1);                                                               \
        }                                                                          \
    } while (0)

/* ---- NOR flash with power cuts ------------------------------------ */
static uint8_t *flash;
static uint32_t flash_size;
static uint64_t flash_ops, flash_reads, cut_at;
static jmp_buf cut_point;
static uint64_t rng_state = 88172645463325252ULL;

static uint32_t rnd(void)
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return (uint32_t)rng_state;
}
static void seed(uint64_t value)
{
    rng_state = 88172645463325252ULL ^ (value * 0x9E3779B97F4A7C15ULL);
    for (int i = 0; i < 16; ++i) (void)rnd();
}

static bool raw_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    (void)context;
    if ((uint64_t)address + length > flash_size) return false;
    flash_reads++;
    memcpy(buffer, flash + address, length);
    return true;
}

/* 2.7.6: the flash operation numbered fail_program_at, a program, reports
 * success but does not stick (a worn sector); with worn_next_page, the
 * first page programmed into the store in a round's window becomes a worn
 * cell for the rest of that round. */
static uint64_t fail_program_at;
static bool round_active;          /* a reformat round is under way */
static uint64_t first_window_ops;  /* flash_ops when its first window opened */
static bool worn_next_page;
static bool worn_magic_next; /* round 2 review: the first window's magic becomes the worn cell (the block will not close) */
static bool worn_second_window; /* round 2 review: and the first page of the next window's block, a second worn cell */
static uint32_t worn_address = UINT32_MAX, worn_address2 = UINT32_MAX;
/* A program over a worn cell reports success; one bit stays set. */
static bool wear(uint8_t *mem, uint32_t address, const uint8_t *src, uint32_t length, uint32_t worn)
{
    if (worn < address || worn - address >= length) return false;
    for (uint32_t i = 0; i < length; ++i) mem[address + i] &= src[i];
    uint8_t want = (uint8_t)~src[worn - address];
    mem[worn] |= (uint8_t)(want & (uint8_t)(0U - want));
    return true;
}
static bool nor_write(uint8_t *mem, uint32_t size, uint32_t address, const void *buffer, uint32_t length)
{
    if ((uint64_t)address + length > size) return false;
    const uint8_t *src = buffer;
    flash_ops++;
    if (cut_at && flash_ops == cut_at) {
        if (rnd() & 1) {
            /* A clean prefix and one partial byte. */
            uint32_t n = rnd() % (length + 1);
            for (uint32_t i = 0; i < n; ++i) mem[address + i] &= src[i];
            if (n < length && (rnd() & 1)) mem[address + n] &= (uint8_t)(src[n] | (uint8_t)rnd());
        } else {
            /* NOR programs a page's cells in parallel: any byte may end
             * fully, partly or not programmed (a size field's high bytes
             * left 0xFF, a flags byte left 0xFD). */
            for (uint32_t i = 0; i < length; ++i) {
                uint32_t how = rnd() % 4;
                if (how == 1) mem[address + i] &= src[i];
                else if (how == 2) mem[address + i] &= (uint8_t)(src[i] | (uint8_t)rnd());
            }
        }
        longjmp(cut_point, 1);
    }
    if (mem == flash && worn_magic_next && round_active && first_window_ops && length == 2 && address % BLOCK == 252) {
        /* A magic byte the block's own magic programs (not 0xFF). */
        uint16_t magic = srx_spiffs_magic(flash_size / BLOCK, address / BLOCK);
        worn_address = (magic & 0xFF) != 0xFF ? address : address + 1;
        worn_magic_next = false;
    }
    if (mem == flash && worn_next_page && round_active && first_window_ops && length == PAGE) {
        for (uint32_t i = 0; i < length && worn_address == UINT32_MAX; ++i)
            if (src[i] != 0xFF) worn_address = address + i;
        worn_next_page = false;
    }
    if (mem == flash && worn_second_window && worn_address != UINT32_MAX && round_active && length == PAGE &&
        address / BLOCK != worn_address / BLOCK) {
        for (uint32_t i = 0; i < length && worn_address2 == UINT32_MAX; ++i)
            if (src[i] != 0xFF) worn_address2 = address + i;
        worn_second_window = false;
    }
    if (mem == flash && (wear(mem, address, src, length, worn_address) || wear(mem, address, src, length, worn_address2)))
        return true;
    /* A worn cell: the program reports success, one bit stays set. */
    if (fail_program_at && flash_ops == fail_program_at) {
        bool kept = false;
        for (uint32_t i = 0; i < length; ++i) {
            mem[address + i] &= src[i];
            uint8_t want = (uint8_t)~src[i];
            if (!kept && want) {
                mem[address + i] |= (uint8_t)(want & (uint8_t)(0U - want));
                kept = true;
            }
        }
        return true;
    }
    for (uint32_t i = 0; i < length; ++i) mem[address + i] &= src[i];
    return true;
}

/* The last cut tore a store block's erase through SPIFFS's own erase (its GC,
 * or 2.7.5's compaction; the round's own erases clear the magic first) and
 * left the block's magic valid over lookup entries that read free above
 * written pages: plain SPIFFS then programs over those pages. REVIEW_TO_END
 * sorts such cuts out when the old image runs next. */
static bool cut_tore_spiffs_erase;
static bool nor_erase(uint8_t *mem, uint32_t size, uint32_t address, uint32_t length)
{
    if ((uint64_t)address + length > size) return false;
    flash_ops++;
    if (cut_at && flash_ops == cut_at) {
        for (uint32_t i = 0; i < length; ++i)
            if (rnd() & 1) mem[address + i] = 0xFF;
        if (mem == flash && length == BLOCK && address % BLOCK == 0 && !round_active) {
            uint32_t free_over_data = 0;
            for (uint32_t e = 0; e + 1 < BLOCK / PAGE; ++e) {
                bool erased = true;
                for (uint32_t i = 0; i < PAGE && erased; ++i) erased = mem[address + (e + 1) * PAGE + i] == 0xFF;
                free_over_data += mem[address + 2 * e] == 0xFF && mem[address + 2 * e + 1] == 0xFF && !erased;
            }
            uint16_t magic = (uint16_t)(mem[address + 252] | mem[address + 253] << 8);
            cut_tore_spiffs_erase = free_over_data && magic == srx_spiffs_magic(flash_size / BLOCK, address / BLOCK);
        }
        longjmp(cut_point, 1);
    }
    memset(mem + address, 0xFF, length);
    return true;
}

static bool raw_write(void *context, uint32_t address, const void *buffer, uint32_t length)
{
    (void)context;
    return nor_write(flash, flash_size, address, buffer, length);
}

static bool raw_erase(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    return nor_erase(flash, flash_size, address, length);
}

static srm_mirror_t mirror;
static bool use_mirror;
static uint8_t *mirror_lookup, *mirror_headers, mirror_scratch[BLOCK];
/* 2.7.6 (round 1 review, low 4): the device's remount after a round keeps
 * its mirror (SPIFFS reads it through the HAL; writes, erases and refreshes
 * kept it current), so the harness's does too, and checks it against one
 * read fresh from flash. */
static bool keep_mirror;
static uint32_t mirror_inits, mirror_compares;

static s32_t hal_read(struct spiffs_t *fs, u32_t addr, u32_t size, u8_t *dst)
{
    (void)fs;
    bool ok = use_mirror ? srm_read(&mirror, addr, dst, size) : raw_read(NULL, addr, dst, size);
    return ok ? SPIFFS_OK : -1;
}
static s32_t hal_write(struct spiffs_t *fs, u32_t addr, u32_t size, u8_t *src)
{
    (void)fs;
    bool ok = use_mirror ? srm_write(&mirror, addr, src, size) : raw_write(NULL, addr, src, size);
    return ok ? SPIFFS_OK : -1;
}
static s32_t hal_erase(struct spiffs_t *fs, u32_t addr, u32_t size)
{
    (void)fs;
    bool ok = use_mirror ? srm_erase(&mirror, addr, size) : raw_erase(NULL, addr, size);
    return ok ? SPIFFS_OK : -1;
}

void spiffs_api_lock(struct spiffs_t *fs) { (void)fs; }
void spiffs_api_unlock(struct spiffs_t *fs) { (void)fs; }

static spiffs fs;
static spiffs_config cfg;
static u8_t work_buf[PAGE * 2];
static u8_t fd_buf[FD_COUNT * sizeof(spiffs_fd)];
static u8_t cache_buf[sizeof(spiffs_cache) + FD_COUNT * (sizeof(spiffs_cache_page) + PAGE)];
static bool callback_used;

static void plain_check_callback(spiffs *f, spiffs_check_type type, spiffs_check_report report, u32_t a, u32_t b)
{
    (void)f; (void)type; (void)report; (void)a; (void)b;
}

static s32_t mount_with(spiffs_check_callback callback)
{
    memset(&fs, 0, sizeof(fs));
    memset(fd_buf, 0, sizeof(fd_buf));
    memset(cache_buf, 0, sizeof(cache_buf));
    cfg.hal_read_f = hal_read;
    cfg.hal_write_f = hal_write;
    cfg.hal_erase_f = hal_erase;
    cfg.phys_size = flash_size;
    cfg.phys_addr = 0;
    cfg.phys_erase_block = BLOCK;
    cfg.log_block_size = BLOCK;
    cfg.log_page_size = PAGE;
    if (use_mirror && !keep_mirror) {
        static const srm_flash_t raw = {NULL, raw_read, raw_write, raw_erase};
        CHECK(srm_init(&mirror, &raw, flash_size, BLOCK, PAGE, mirror_lookup, mirror_headers, mirror_scratch,
                       sizeof(mirror_scratch)), "mirror init");
        mirror_inits++;
    }
    keep_mirror = false;
    callback_used = callback == srr_check_callback;
    return SPIFFS_mount(&fs, &cfg, work_buf, fd_buf, sizeof(fd_buf), cache_buf, sizeof(cache_buf), callback);
}
static void mount_plain(void) { CHECK(mount_with(plain_check_callback) == SPIFFS_OK, "mount %d", SPIFFS_errno(&fs)); }

static void format_flash(uint32_t kib)
{
    flash_size = kib * 1024U;
    free(flash);
    flash = malloc(flash_size);
    memset(flash, 0xFF, flash_size);
    free(mirror_lookup);
    free(mirror_headers);
    mirror_lookup = malloc(srm_lookup_bytes(flash_size, BLOCK, PAGE));
    mirror_headers = malloc(srm_header_bytes(flash_size, PAGE));
    bool saved = use_mirror;
    use_mirror = false;
    (void)mount_with(plain_check_callback);
    SPIFFS_unmount(&fs);
    CHECK(SPIFFS_format(&fs) == SPIFFS_OK, "format");
    use_mirror = saved;
}

/* ---- NVS: atomic blobs that survive power cuts --------------------- */
/* 2.7.6: the redo area (the 384 KiB past the last partition) lives beside
 * NVS here: it is not the store, so every snapshot a test takes of NVS takes
 * it too. Its writes and erases are counted and cut like the store's. */
#define NVS_SLOTS 64
#define REDO_BYTES (96U * 4096U)
static struct {
    struct {
        char ns[16], key[16];
        uint8_t blob[2048];
        size_t size;
        bool used;
    } slot[NVS_SLOTS];
    uint8_t redo[REDO_BYTES];
} nvs;

static int nvs_find(const char *ns, const char *key)
{
    for (int i = 0; i < NVS_SLOTS; ++i)
        if (nvs.slot[i].used && !strcmp(nvs.slot[i].ns, ns) && !strcmp(nvs.slot[i].key, key)) return i;
    return -1;
}
static int port_nvs_load(void *context, const char *ns, const char *key, void *blob, size_t size)
{
    (void)context;
    int i = nvs_find(ns, key);
    if (i < 0) return 0;
    if (nvs.slot[i].size != size) return -1;
    memcpy(blob, nvs.slot[i].blob, size);
    return 1;
}
static bool nvs_refuse_state; /* the repair journal cannot be saved */
/* The engine saves the reclaim journal this many more times, then cannot
 * (-1: always can; premount has journal_refused). */
static int32_t reclaim_saves_left = -1;
static bool engine_running;     /* srr_run is under way */
static bool port_nvs_store(void *context, const char *ns, const char *key, const void *blob, size_t size)
{
    (void)context;
    if (nvs_refuse_state && !strcmp(ns, SRR_NVS_NAMESPACE) && !strcmp(key, "state")) return false;
    if (reclaim_saves_left >= 0 && engine_running && !strcmp(ns, SRR_NVS_NAMESPACE) && !strcmp(key, "reclaim")) {
        if (!reclaim_saves_left) return false;
        reclaim_saves_left--;
    }
    CHECK(size <= sizeof(nvs.slot[0].blob), "blob %s/%s %zu bytes", ns, key, size);
    int i = nvs_find(ns, key);
    for (int k = 0; i < 0 && k < NVS_SLOTS; ++k)
        if (!nvs.slot[k].used) i = k;
    CHECK(i >= 0, "nvs full");
    snprintf(nvs.slot[i].ns, sizeof(nvs.slot[i].ns), "%s", ns);
    snprintf(nvs.slot[i].key, sizeof(nvs.slot[i].key), "%s", key);
    memcpy(nvs.slot[i].blob, blob, size);
    nvs.slot[i].size = size;
    nvs.slot[i].used = true;
    return true;
}
static bool port_nvs_erase(void *context, const char *ns, const char *key)
{
    (void)context;
    int i = nvs_find(ns, key);
    if (i >= 0) nvs.slot[i].used = false;
    return true;
}
static void nvs_clear(void)
{
    memset(nvs.slot, 0, sizeof(nvs.slot));
    memset(nvs.redo, 0xFF, sizeof(nvs.redo));
}

/* ---- the redo area (2.7.6) ----------------------------------------- */
static bool redo_enabled = true; /* false: no redo area, the store is left as 2.7.5 leaves it */
static bool redo_unreadable;     /* this boot cannot read the area */
static uint32_t redo_windows;    /* windows opened (magic cleared) */
static uint64_t window_ops[512]; /* flash_ops as each window of this boot's rounds opened */
static uint32_t window_count;
static bool redo_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    (void)context;
    if (redo_unreadable || (uint64_t)address + length > REDO_BYTES) return false;
    flash_reads++;
    memcpy(buffer, nvs.redo + address, length);
    return true;
}
/* Round 2 review: area writes that report an I/O error. The next
 * area_fail_next writes fail; writes area_fail_from .. area_fail_from +
 * area_fail_count - 1 (counted from area_writes = 0) fail; and every write
 * that covers area_stuck (a worn word that never programs) fails. */
static uint32_t area_writes, area_fail_next, area_fail_from, area_fail_count;
static uint32_t area_stuck = UINT32_MAX;
static bool redo_write(void *context, uint32_t address, const void *buffer, uint32_t length)
{
    (void)context;
    area_writes++;
    if (area_fail_from && area_writes >= area_fail_from && area_writes < area_fail_from + area_fail_count) return false;
    if (area_fail_next) {
        area_fail_next--;
        return false;
    }
    if (area_stuck >= address && area_stuck - address < length) return false;
    return nor_write(nvs.redo, REDO_BYTES, address, buffer, length);
}
static bool redo_erase(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    return nor_erase(nvs.redo, REDO_BYTES, address, length);
}
static void redo_window(void *context, bool open)
{
    (void)context;
    if (!open) return;
    redo_windows++;
    if (round_active && !first_window_ops) first_window_ops = flash_ops;
    if (round_active && window_count < sizeof(window_ops) / sizeof(window_ops[0])) window_ops[window_count++] = flash_ops;
}
static void port_log(void *context, const char *level, const char *code, const char *message);
static const srx_io_t redo_io = {
    .area_size = REDO_BYTES, .area_read = redo_read, .area_write = redo_write, .area_erase = redo_erase,
    .store_read = raw_read, .store_write = raw_write, .store_erase = raw_erase, .window = redo_window,
    .log = port_log,
};

/* ---- repair ports --------------------------------------------------- */
static uint64_t memory_in_use, memory_peak, memory_limit;
static uint64_t log_lines;
static uint32_t phase_changes;

typedef struct allocation {
    struct allocation *previous, *next;
    size_t bytes;
    size_t pad;
} allocation_t;
static allocation_t allocations = {&allocations, &allocations, 0, 0};

static size_t fail_alloc_bytes; /* an allocation of exactly this size fails (2.7.6: the round's 16 KiB) */
static void *port_alloc(void *context, size_t bytes)
{
    (void)context;
    if (memory_limit && memory_in_use + bytes > memory_limit) return NULL;
    if (fail_alloc_bytes && bytes == fail_alloc_bytes) return NULL;
    allocation_t *a = malloc(sizeof(allocation_t) + bytes);
    if (!a) return NULL;
    a->bytes = bytes;
    a->next = &allocations;
    a->previous = allocations.previous;
    allocations.previous->next = a;
    allocations.previous = a;
    memory_in_use += bytes;
    if (memory_in_use > memory_peak) memory_peak = memory_in_use;
    return a + 1;
}
static void port_release(void *context, void *pointer)
{
    (void)context;
    if (!pointer) return;
    allocation_t *a = (allocation_t *)pointer - 1;
    a->previous->next = a->next;
    a->next->previous = a->previous;
    memory_in_use -= a->bytes;
    free(a);
}
/* A power cut: the repair's RAM is gone. */
static void drop_all_memory(void)
{
    while (allocations.next != &allocations) port_release(NULL, allocations.next + 1);
    memory_in_use = 0;
}
static uint64_t port_now(void *context)
{
    (void)context;
    return (flash_ops + flash_reads / 64) / 8;
}
static bool print_log; /* REPAIR_LOG=1: the repair's log lines without -v's extra mounts */
/* Codes the repair logged since the test cleared them (2.7.6 tests). */
static char logged_codes[64][40];
static uint32_t logged_count;
static bool code_logged(const char *code)
{
    for (uint32_t i = 0; i < logged_count; ++i)
        if (!strcmp(logged_codes[i], code)) return true;
    return false;
}
static void port_log(void *context, const char *level, const char *code, const char *message)
{
    (void)context;
    log_lines++;
    if (!code_logged(code) && logged_count < 64) snprintf(logged_codes[logged_count++], sizeof(logged_codes[0]), "%s", code);
    if (verbose || print_log) printf("  [%s] %s %s\n", level, code, message);
}
static uint32_t writing_phases; /* phases that light the repair LED */
static void round_began(void);
static uint32_t boot_runs;      /* 2.7.6: runs of the repair in this boot (a round is followed by another) */
/* Stands in for something that changes the store as a phase starts. */
static void (*phase_hook)(srp_phase_t phase, uint32_t pass);
static void port_phase(void *context, srp_phase_t phase, uint32_t pass, bool writing)
{
    (void)context;
    phase_changes++;
    if (writing) writing_phases++;
    if (getenv("HAL_TRACE"))
        printf("  phase %s pass %u: %" PRIu64 " reads %" PRIu64 " writes\n", srp_phase_name(phase), pass, flash_reads,
               flash_ops);
    if (phase == SRP_PHASE_SURVEY && pass == 0) boot_runs++;
    if (phase == SRP_PHASE_REFORMAT && writing) round_began();
    if (phase_hook && boot_runs <= 1) phase_hook(phase, pass);
}
/* The soft deadline, in port_now units; 0: none. */
static uint64_t deadline_at;
static bool port_keep_going(void *context)
{
    return !deadline_at || port_now(context) < deadline_at;
}
static uint64_t port_remaining(void *context)
{
    if (!deadline_at) return UINT64_MAX / 2;
    uint64_t now = port_now(context);
    return now < deadline_at ? deadline_at - now : 0;
}
/* Stands in for a check that altered or deleted a held file. */
static bool (*check_hook)(uint32_t pass);
static bool port_after_check(void *context, uint32_t pass)
{
    (void)context;
    /* The hooks stand in for what one check did in a boot: its first run. */
    return check_hook && boot_runs <= 1 && check_hook(pass);
}
static s32_t mount_with(spiffs_check_callback callback);
static void round_ended(void);
/* The kept mirror equals one read fresh from flash (a lookup page's own
 * header slot aside: the mirror serves that page from its lookup copy). */
static void check_kept_mirror(void)
{
    static uint8_t *lookup, *headers;
    static size_t lookup_size, header_size;
    size_t need_lookup = srm_lookup_bytes(flash_size, BLOCK, PAGE), need_headers = srm_header_bytes(flash_size, PAGE);
    if (lookup_size < need_lookup) {
        free(lookup);
        lookup = malloc(lookup_size = need_lookup);
    }
    if (header_size < need_headers) {
        free(headers);
        headers = malloc(header_size = need_headers);
    }
    static const srm_flash_t raw = {NULL, raw_read, raw_write, raw_erase};
    srm_mirror_t fresh;
    uint64_t reads = flash_reads;
    CHECK(srm_init(&fresh, &raw, flash_size, BLOCK, PAGE, lookup, headers, mirror_scratch, sizeof(mirror_scratch)),
          "fresh mirror");
    flash_reads = reads;
    CHECK(mirror.valid && !memcmp(mirror.lookup, lookup, need_lookup), "the kept mirror's lookup pages differ from flash");
    for (uint32_t page = 0; page < flash_size / PAGE; ++page)
        CHECK(page % (BLOCK / PAGE) == 0 || !memcmp(mirror.headers + page * 5, headers + page * 5, 5),
              "the kept mirror's header of page %u differs from flash", page);
    mirror_compares++;
}

static bool port_remount(void *context)
{
    (void)context;
    round_ended();
    SPIFFS_unmount(&fs);
    if (use_mirror) {
        check_kept_mirror();
        keep_mirror = true;
    }
    return mount_with(srr_check_callback) == SPIFFS_OK;
}
static bool port_raw_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    return raw_read(context, address, buffer, length);
}
static void port_refresh(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    if (use_mirror) CHECK(srm_refresh(&mirror, address, length), "mirror refresh");
}
static int32_t nvs_available_now = -1; /* the NVS headroom a test gives the engine (-1: unknown) */
static int32_t port_nvs_available(void *context)
{
    (void)context;
    return nvs_available_now;
}
static uint32_t prefer_block = UINT32_MAX; /* a round takes this candidate first */
static bool port_prefer(void *context, uint32_t block)
{
    (void)context;
    return block == prefer_block;
}
static srr_ports_t ports = {
    .alloc = port_alloc, .release = port_release, .keep_going = port_keep_going, .remaining_ms = port_remaining,
    .now_ms = port_now, .log = port_log, .nvs_load = port_nvs_load, .nvs_store = port_nvs_store,
    .nvs_erase = port_nvs_erase, .phase = port_phase, .after_check = port_after_check,
    .raw_read = port_raw_read, .refresh = port_refresh, .window = redo_window, .nvs_available = port_nvs_available,
    .prefer = port_prefer,
};

static srr_config_t repair_config(void)
{
    srr_config_t config = {.hold_budget = 64ULL * 1024 * 1024, .image_tag = 0x2750};
    return config;
}

/* One boot: mount privately, repair, unmount. HAL operations are capped:
 * the firmware's deadline must never be the thing that ends a repair. */
static uint64_t hal_cap, hal_max, hal_max_cap;
/* The engine's CPU work: its patch lookups and mask scans (cpu_steps), capped
 * like the flash operations, and its CPU time (review run `cpu`). */
static uint64_t cpu_cap, cpu_max;
static double cpu_ms_max;
static double cpu_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}
/* 2.7.6: what the firmware glue does before any mount: the redo area read,
 * the open records completed on raw flash (or made byte sources), the
 * reclaim journal updated. `boot_area` is what the engine then gets. */
static srx_area_t boot_area;
static uint32_t engine_free_blocks; /* SPIFFS's own count when the repair returned */
static void check_reclaimed_structure(const char *what);
static void verify_reclaimed(const char *what);
static uint32_t boot_open_left;
static bool mount_blocked;      /* the glue would keep the store unmounted this boot */
static bool journal_refused;    /* the reclaim journal cannot be saved */
static void premount(srr_config_t *config)
{
    mount_blocked = false;
    if (!redo_enabled) {
        ports.redo = NULL;
        ports.remount = NULL;
        config->area = NULL;
        return;
    }
    static uint8_t work[3 * 4096];
    srp_reclaim_t journal;
    bool have = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) == 1 &&
        srp_reclaim_valid(&journal);
    if (!have) memset(&journal, 0, sizeof(journal));
    srx_discover(&redo_io, have && (journal.flags & SRP_RECLAIM_CLAIMED), work, &boot_area);
    bool inspected = boot_area.state == SRX_AREA_READY || boot_area.state == SRX_AREA_FOREIGN;
    boot_open_left = inspected ? srx_recover(&redo_io, flash_size / BLOCK, config->image_tag,
                                             have && (journal.flags & SRP_RECLAIM_SOURCES), work, &boot_area)
                               : 0;
    if (srx_journal_after_recovery(&journal, inspected, boot_open_left, srx_sources_left(&boot_area))) {
        bool saved = !journal_refused && port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal));
        mount_blocked = !saved && (journal.flags & SRP_RECLAIM_SOURCES);
    }
    config->sources_unread = have && srx_sources_unread(&journal, inspected);
    ports.redo = &redo_io;
    ports.remount = port_remount;
    config->area = &boot_area;
}

static void repair_boot(srr_report_t *report, const srr_config_t *base)
{
    uint64_t start_ops = flash_ops, start_reads = flash_reads;
    srr_config_t copy = *base, *config = &copy;
    boot_runs = 0;
    window_count = 0;
    premount(config);
    if (mount_blocked) {
        memset(report, 0, sizeof(*report));
        report->result = SRR_RESULT_NOT_RUN;
        snprintf(report->error_stage, sizeof(report->error_stage), "unmounted");
        return;
    }
    CHECK(mount_with(srr_check_callback) == SPIFFS_OK, "repair mount %d", SPIFFS_errno(&fs));
    double started = cpu_ms();
    engine_running = true;
    srr_run(&fs, config, &ports, report);
    engine_running = false;
    engine_free_blocks = (uint32_t)fs.free_blocks;
    /* Round 2 review, R2-4: the glue then keeps the VFS off the store. */
    if (report->reclaim.store_blocked) mount_blocked = true;
    double spent = cpu_ms() - started;
    if (spent > cpu_ms_max) cpu_ms_max = spent;
    if (report->cpu_steps > cpu_max) cpu_max = report->cpu_steps;
    if (cpu_cap) CHECK(report->cpu_steps <= cpu_cap, "repair took %u CPU steps (cap %" PRIu64 ")", report->cpu_steps,
                       cpu_cap);
    SPIFFS_unmount(&fs);
    CHECK(memory_in_use == 0, "repair leaked %" PRIu64 " bytes", memory_in_use);
    uint64_t used = (flash_ops - start_ops) + (flash_reads - start_reads);
    /* 2.7.6: a round is followed by another run of the repair; and a check
     * pass that deletes thousands of leaked pages (P02's half-removed file,
     * once a round gave the store room) reads every page header up to eight
     * times more per 512-page range. */
    uint64_t pages = flash_size / PAGE;
    uint64_t cap = hal_cap * (report->runs ? report->runs : 1) + 8U * pages * (pages / 512U + 1U);
    if (used > hal_max) {
        hal_max = used;
        hal_max_cap = cap;
    }
    if (hal_cap && used > cap) dump_write();
    if (hal_cap) CHECK(used <= cap, "repair used %" PRIu64 " HAL operations (cap %" PRIu64 ")", used, cap);
    if (verbose)
        printf("  repair %s: gaps %u spans %u checks %u hal %" PRIu64 " peak %" PRIu64 " KiB\n",
               srr_result_name(report->result), report->gaps, report->spans_rewritten, report->check_passes, used,
               memory_peak / 1024);
}

/* ---- file helpers --------------------------------------------------- */
static void write_file(const char *name, const uint8_t *data, uint32_t length)
{
    spiffs_file f = SPIFFS_open(&fs, name, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    CHECK(f >= 0, "create %s %d", name, SPIFFS_errno(&fs));
    for (uint32_t done = 0; done < length;) {
        uint32_t n = length - done < 4000 ? length - done : 4000;
        CHECK(SPIFFS_write(&fs, f, (void *)(data + done), (s32_t)n) == (s32_t)n, "write %s %d", name, SPIFFS_errno(&fs));
        done += n;
    }
    CHECK(SPIFFS_close(&fs, f) >= 0, "close %s", name);
}

static bool append_bytes(const char *name, const void *data, uint32_t length)
{
    spiffs_file f = SPIFFS_open(&fs, name, SPIFFS_O_CREAT | SPIFFS_O_APPEND | SPIFFS_O_RDWR, 0);
    if (f < 0) {
        SPIFFS_clearerr(&fs);
        return false;
    }
    bool ok = SPIFFS_write(&fs, f, (void *)data, (s32_t)length) == (s32_t)length && SPIFFS_fflush(&fs, f) >= 0;
    if (SPIFFS_close(&fs, f) < 0) ok = false;
    SPIFFS_clearerr(&fs);
    return ok;
}

/* The whole file, or -1 when any byte is unreadable. */
static int64_t read_file(const char *name, uint8_t *buffer, uint32_t capacity)
{
    spiffs_file f = SPIFFS_open(&fs, name, SPIFFS_O_RDONLY, 0);
    if (f < 0) {
        SPIFFS_clearerr(&fs);
        return -1;
    }
    int64_t total = 0;
    for (;;) {
        s32_t want = (s32_t)(capacity - total) < 4096 ? (s32_t)(capacity - total) : 4096;
        if (want <= 0) break;
        s32_t got = SPIFFS_read(&fs, f, buffer + total, want);
        if (got < 0) {
            total = -1;
            break;
        }
        total += got;
        if (got < want) break;
    }
    SPIFFS_close(&fs, f);
    SPIFFS_clearerr(&fs);
    return total;
}

static bool exists(const char *name)
{
    spiffs_stat st;
    bool found = SPIFFS_stat(&fs, name, &st) == SPIFFS_OK;
    SPIFFS_clearerr(&fs);
    return found;
}

/* ---- stdio shim for the firmware's own readers --------------------- */
struct shim_file {
    spiffs_file fd;
    int error;
    bool append, eof;
};
static shim_file_t shim_files[8];

static int shim_errno(void)
{
    s32_t e = SPIFFS_errno(&fs);
    SPIFFS_clearerr(&fs);
    return e == SPIFFS_ERR_NOT_FOUND || e == SPIFFS_ERR_DELETED ? ENOENT : EIO;
}

shim_file_t *shim_fopen(const char *path, const char *mode)
{
    spiffs_flags flags = SPIFFS_O_RDONLY;
    bool append = false;
    if (strchr(mode, 'a')) {
        flags = SPIFFS_O_CREAT | SPIFFS_O_RDWR | SPIFFS_O_APPEND;
        append = true;
    } else if (strchr(mode, 'w')) flags = SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR;
    else if (strchr(mode, '+')) flags = SPIFFS_O_RDWR;
    spiffs_file fd = SPIFFS_open(&fs, path, flags, 0);
    if (fd < 0) {
        errno = shim_errno();
        return NULL;
    }
    for (int i = 0; i < 8; ++i) {
        if (!shim_files[i].fd) {
            shim_files[i] = (shim_file_t){.fd = fd, .append = append};
            return &shim_files[i];
        }
    }
    SPIFFS_close(&fs, fd);
    errno = EMFILE;
    return NULL;
}
int shim_fclose(shim_file_t *file)
{
    int result = SPIFFS_close(&fs, file->fd) < 0 ? -1 : 0;
    SPIFFS_clearerr(&fs);
    file->fd = 0;
    return result;
}
size_t shim_fread(void *buffer, size_t size, size_t count, shim_file_t *file)
{
    s32_t want = (s32_t)(size * count);
    if (!want) return 0;
    s32_t got = SPIFFS_read(&fs, file->fd, buffer, want);
    if (got < 0) {
        errno = shim_errno();
        file->error = 1;
        return 0;
    }
    if (got < want) file->eof = true;
    return (size_t)got / size;
}
size_t shim_fwrite(const void *buffer, size_t size, size_t count, shim_file_t *file)
{
    s32_t n = (s32_t)(size * count);
    if (file->append) SPIFFS_lseek(&fs, file->fd, 0, SPIFFS_SEEK_END);
    s32_t done = SPIFFS_write(&fs, file->fd, (void *)buffer, n);
    if (done != n) {
        errno = shim_errno();
        file->error = 1;
        return 0;
    }
    return count;
}
int shim_fgetc(shim_file_t *file)
{
    unsigned char c;
    s32_t got = SPIFFS_read(&fs, file->fd, &c, 1);
    if (got < 0) {
        errno = shim_errno();
        file->error = 1;
        return EOF;
    }
    if (got == 0) {
        file->eof = true;
        return EOF;
    }
    return c;
}
char *shim_fgets(char *buffer, int size, shim_file_t *file)
{
    int n = 0;
    while (n < size - 1) {
        int c = shim_fgetc(file);
        if (c == EOF) break;
        buffer[n++] = (char)c;
        if (c == '\n') break;
    }
    if (!n) return NULL;
    buffer[n] = 0;
    return buffer;
}
int shim_fprintf(shim_file_t *file, const char *format, ...)
{
    char text[512];
    va_list args;
    va_start(args, format);
    int n = vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    if (n < 0 || (size_t)n >= sizeof(text)) return -1;
    return shim_fwrite(text, 1, (size_t)n, file) == (size_t)n ? n : -1;
}
int shim_fseek(shim_file_t *file, long offset, int whence)
{
    int w = whence == SEEK_SET ? SPIFFS_SEEK_SET : whence == SEEK_CUR ? SPIFFS_SEEK_CUR : SPIFFS_SEEK_END;
    if (SPIFFS_lseek(&fs, file->fd, (s32_t)offset, w) < 0) {
        errno = shim_errno();
        return -1;
    }
    file->eof = false;
    return 0;
}
long shim_ftell(shim_file_t *file)
{
    s32_t at = SPIFFS_tell(&fs, file->fd);
    SPIFFS_clearerr(&fs);
    return at;
}
int shim_ferror(shim_file_t *file) { return file->error; }
int shim_fflush(shim_file_t *file)
{
    int result = SPIFFS_fflush(&fs, file->fd) < 0 ? -1 : 0;
    SPIFFS_clearerr(&fs);
    return result;
}
int shim_fileno(shim_file_t *file) { return (int)(file - shim_files) + 100; }
int shim_fsync(int fd) { (void)fd; return 0; }
int shim_fstat(int fd, struct stat *status)
{
    spiffs_stat st;
    if (SPIFFS_fstat(&fs, shim_files[fd - 100].fd, &st) != SPIFFS_OK) {
        errno = shim_errno();
        return -1;
    }
    memset(status, 0, sizeof(*status));
    status->st_size = st.size == SPIFFS_UNDEFINED_LEN ? 0 : st.size;
    return 0;
}
int shim_stat(const char *path, struct stat *status)
{
    spiffs_stat st;
    if (SPIFFS_stat(&fs, path, &st) != SPIFFS_OK) {
        errno = shim_errno();
        return -1;
    }
    memset(status, 0, sizeof(*status));
    status->st_size = st.size == SPIFFS_UNDEFINED_LEN ? 0 : st.size;
    return 0;
}
int shim_remove(const char *path)
{
    if (SPIFFS_remove(&fs, path) != SPIFFS_OK) {
        errno = shim_errno();
        return -1;
    }
    return 0;
}
int shim_rename(const char *from, const char *to)
{
    if (SPIFFS_rename(&fs, from, to) != SPIFFS_OK) {
        errno = shim_errno();
        return -1;
    }
    return 0;
}

/* ---- raw SPIFFS structure, for building damage ------------------------ */
static uint16_t rd16(uint32_t address) { return (uint16_t)(flash[address] | (flash[address + 1] << 8)); }
static void wr16(uint32_t address, uint16_t value)
{
    flash[address] = (uint8_t)value;
    flash[address + 1] = (uint8_t)(value >> 8);
}
static uint32_t lookup_address(uint32_t pix) { return (pix / 16U) * BLOCK + (pix % 16U - 1U) * 2U; }

static bool header_named(uint32_t pix, const char *name)
{
    uint32_t a = pix * PAGE;
    uint8_t flags = flash[a + 4];
    return (rd16(a) & 0x8000) && rd16(a) != 0xFFFF && rd16(a + 2) == 0 &&
        !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) &&
        (flags & SPIFFS_PH_FLAG_DELET) && (flags & SPIFFS_PH_FLAG_IXDELE) &&
        !strncmp((const char *)flash + a + offsetof(spiffs_page_object_ix_header, name), name, 32);
}

static uint32_t header_of(const char *name)
{
    for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix)
        if (pix % 16 && header_named(pix, name)) return pix;
    CHECK(0, "no header for %s", name);
    return 0;
}

static uint32_t index_page(uint16_t obj, uint32_t ix_span)
{
    for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix) {
        uint32_t a = pix * PAGE;
        uint8_t flags = flash[a + 4];
        if (pix % 16 && rd16(a) == (uint16_t)(obj | 0x8000) && rd16(a + 2) == ix_span &&
            !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) &&
            (flags & SPIFFS_PH_FLAG_DELET))
            return pix;
    }
    CHECK(0, "no index page %u for object %04x", ix_span, obj);
    return 0;
}

/* Flash address of the index entry naming data span `span` of a file. */
static uint32_t entry_address(const char *name, uint32_t span)
{
    uint32_t header = header_of(name);
    uint16_t obj = rd16(header * PAGE) & 0x7FFF;
    if (span < 103) return header * PAGE + 49 + span * 2;
    uint32_t j = 1 + (span - 103) / 124;
    return index_page(obj, j) * PAGE + 8 + ((span - 103) % 124) * 2;
}
static uint16_t object_of(const char *name) { return rd16(header_of(name) * PAGE) & 0x7FFF; }

static uint32_t free_page(void)
{
    /* A free page in a block that already holds used pages, so the free
     * block count SPIFFS keeps does not change. */
    for (uint32_t pass = 0; pass < 2; ++pass)
        for (uint32_t pix = flash_size / PAGE; pix-- > 0;) {
            if (pix % 16 == 0 || rd16(lookup_address(pix)) != 0xFFFF) continue;
            bool erased = true;
            for (uint32_t i = 0; i < PAGE && erased; ++i) erased = flash[pix * PAGE + i] == 0xFF;
            bool block_used = false;
            for (uint32_t e = 1; e < 16; ++e)
                if (rd16(lookup_address((pix / 16) * 16 + e)) != 0xFFFF) block_used = true;
            if (erased && (block_used || pass == 1)) return pix;
        }
    CHECK(0, "no free page");
    return 0;
}

static void copy_page(uint32_t to, uint32_t from)
{
    memcpy(flash + to * PAGE, flash + from * PAGE, PAGE);
    wr16(lookup_address(to), rd16(lookup_address(from)));
}

static void flag_deleted(uint32_t pix)
{
    flash[pix * PAGE + 4] &= (uint8_t)~(SPIFFS_PH_FLAG_DELET | SPIFFS_PH_FLAG_USED);
    wr16(lookup_address(pix), 0);
}

static void erase_page(uint32_t pix)
{
    memset(flash + pix * PAGE, 0xFF, PAGE);
    wr16(lookup_address(pix), 0xFFFF);
}

/* ---- content -------------------------------------------------------- */
typedef struct {
    char name[33];
    uint8_t *data;
    uint32_t size;
    uint8_t *unreadable; /* 1 per byte made unreadable by the damage */
    bool json, ids;
} model_file_t;

#define MAX_MODEL 16
static model_file_t model[MAX_MODEL];
static uint32_t model_count;
static uint32_t row_seq;

static void model_reset(void)
{
    for (uint32_t i = 0; i < model_count; ++i) {
        free(model[i].data);
        free(model[i].unreadable);
    }
    memset(model, 0, sizeof(model));
    model_count = 0;
}

static model_file_t *model_of(const char *name)
{
    for (uint32_t i = 0; i < model_count; ++i)
        if (!strcmp(model[i].name, name)) return &model[i];
    return NULL;
}

static size_t json_row(char *out, size_t capacity)
{
    char uid[65];
    for (int i = 0; i < 64; ++i) uid[i] = "0123456789abcdef"[rnd() % 16];
    uid[64] = 0;
    size_t pad = 40 + rnd() % 400;
    char filler[512];
    for (size_t i = 0; i < pad; ++i) filler[i] = (char)('a' + (rnd() % 26));
    filler[pad] = 0;
    int n = snprintf(out, capacity,
                     "{\"event_uid\":\"%s\",\"seq\":%u,\"user_id\":\"%u\",\"capturetype\":\"LIVE\",\"note\":\"%s\"}\n", uid,
                     row_seq++, rnd() % 5000, filler);
    return (size_t)n;
}

static model_file_t *make_json_file(const char *name, uint32_t target)
{
    model_file_t *m = &model[model_count++];
    snprintf(m->name, sizeof(m->name), "%s", name);
    m->data = malloc(target + 1024);
    m->size = 0;
    m->json = true;
    char row[600];
    while (m->size < target) {
        size_t n = json_row(row, sizeof(row));
        memcpy(m->data + m->size, row, n);
        m->size += (uint32_t)n;
    }
    m->unreadable = calloc(m->size + 1, 1);
    write_file(name, m->data, m->size);
    return m;
}

static model_file_t *make_id_file(const char *name, uint32_t count)
{
    model_file_t *m = &model[model_count++];
    snprintf(m->name, sizeof(m->name), "%s", name);
    m->data = malloc(count * 66 + 1);
    m->size = 0;
    m->ids = true;
    for (uint32_t i = 0; i < count; ++i) {
        for (int k = 0; k < 64; ++k) m->data[m->size++] = (uint8_t)"0123456789abcdef"[rnd() % 16];
        m->data[m->size++] = '\n';
    }
    m->unreadable = calloc(m->size + 1, 1);
    write_file(name, m->data, m->size);
    return m;
}

static model_file_t *make_binary_file(const char *name, uint32_t size)
{
    model_file_t *m = &model[model_count++];
    snprintf(m->name, sizeof(m->name), "%s", name);
    m->data = malloc(size + 1);
    for (uint32_t i = 0; i < size; ++i) m->data[i] = (uint8_t)rnd();
    m->size = size;
    m->unreadable = calloc(size + 1, 1);
    write_file(name, m->data, size);
    return m;
}

static void mark_unreadable(model_file_t *m, uint32_t span)
{
    uint32_t start = span * SPAN, end = start + SPAN < m->size ? start + SPAN : m->size;
    for (uint32_t i = start; i < end; ++i) m->unreadable[i] = 1;
}

static uint32_t prefix_crc(const model_file_t *m, uint32_t offset)
{
    return srp_crc32(0, m->data, offset);
}

static void set_cursor(const char *ns, const char *key, uint32_t offset, uint32_t crc, uint32_t generation)
{
    lq_checkpoint_t cp = {.version = 1, .generation = generation, .offset = offset, .prefix_crc = crc};
    lq_checkpoint_seal(&cp);
    port_nvs_store(NULL, ns, key, &cp, sizeof(cp));
}

static uint32_t row_boundary_after(const model_file_t *m, uint32_t offset)
{
    while (offset < m->size && m->data[offset - 1] != '\n') ++offset;
    return offset;
}

/* ---- oracles -------------------------------------------------------- */
static srp_gaps_t gaps_now(void)
{
    srp_gaps_t gaps;
    memset(&gaps, 0, sizeof(gaps));
    (void)port_nvs_load(NULL, SRR_NVS_NAMESPACE, "gaps", &gaps, sizeof(gaps));
    return gaps;
}

/* A1, A2: every readable byte is at its offset; every unreadable byte, and
 * only those, is covered by a gap record and holds a fill byte. */
static void check_custody(const model_file_t *m, uint32_t generation_from, bool torn_terminated)
{
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_file(m->name, buffer, sizeof(buffer));
    CHECK(got >= 0, "%s unreadable after repair", m->name);
    uint32_t expect = m->size + (torn_terminated ? 1U : 0U);
    CHECK((uint32_t)got == expect, "%s size %" PRId64 " expected %u", m->name, got, expect);
    srp_gaps_t gaps = gaps_now();
    uint32_t reported = 0, unreadable = 0;
    for (uint32_t i = 0; i < m->size; ++i) {
        bool covered = false;
        for (uint32_t g = 0; g < gaps.count && !covered; ++g) {
            const srp_gap_t *gap = &gaps.gap[g];
            /* A merged record covers scattered unreadable bytes and the few
             * readable ones between them. */
            covered = gap->generation >= generation_from && !strcmp(gap->name, m->name) && i >= gap->offset &&
                i < gap->offset + gap->length && (m->unreadable[i] || !(gap->flags & SRP_GAP_MERGED));
        }
        if (m->unreadable[i]) {
            unreadable++;
            CHECK(covered, "%s byte %u unreadable but not reported", m->name, i);
            if (m->json) CHECK(buffer[i] == 0 || buffer[i] == '\n', "%s fill byte %u is %02x", m->name, i, buffer[i]);
            if (m->ids) CHECK(buffer[i] == '\n', "%s id fill byte %u is %02x", m->name, i, buffer[i]);
        } else {
            CHECK(buffer[i] == m->data[i], "%s byte %u changed %02x -> %02x", m->name, i, m->data[i], buffer[i]);
            if (covered) reported++;
        }
    }
    CHECK(!reported, "%s: %u readable bytes reported unreadable", m->name, reported);
    if (torn_terminated) CHECK(buffer[m->size] == '\n', "%s torn tail not terminated", m->name);
    (void)unreadable;
}

static int lq_load(void *context, lq_checkpoint_t *cp)
{
    const char **key = context;
    return port_nvs_load(NULL, key[0], key[1], cp, sizeof(*cp));
}
static bool lq_commit(void *context, const lq_checkpoint_t *cp)
{
    const char **key = context;
    return port_nvs_store(NULL, key[0], key[1], cp, sizeof(*cp));
}

/* A3: the firmware's own drain reads the queue to its end. Returns rows. */
static uint32_t drain(const char *name, const char *ns, const char *key, uint32_t *evidence_rows)
{
    const char *where[2] = {ns, key};
    lq_port_t port = {lq_load, lq_commit, (void *)where};
    legacy_queue_t q;
    memset(&q, 0, sizeof(q));
    dq_result_t opened = lq_open(&q, name, port);
    CHECK(opened == DQ_OK, "%s: lq_open %d errno %d", name, opened, errno);
    static char line[8193];
    uint32_t rows = 0;
    *evidence_rows = 0;
    for (;;) {
        lq_token_t token;
        dq_result_t peek = lq_peek(&q, line, sizeof(line), &token);
        if (peek == DQ_EMPTY) break;
        CHECK(peek == DQ_OK, "%s: lq_peek %d errno %d at %u", name, peek, errno, q.checkpoint.offset);
        size_t length = token.end - token.offset;
        bool evidence = token.evidence_required || memchr(line, 0, length) ||
            !rel_json_syntax_valid(line, length && line[length - 1] == '\n' ? length - 1 : length);
        if (evidence) (*evidence_rows)++;
        dq_result_t settled = evidence ? lq_settle_evidence(&q, &token) : lq_settle(&q, &token);
        CHECK(settled == DQ_OK, "%s: settle %d", name, settled);
        rows++;
    }
    return rows;
}

/* A5: a second check fixes nothing. */
static uint32_t idempotent_fixes;
static void count_fixes(spiffs *f, spiffs_check_type type, spiffs_check_report report, u32_t a, u32_t b)
{
    (void)f; (void)type; (void)a; (void)b;
    if (report != SPIFFS_CHECK_PROGRESS) idempotent_fixes++;
}
static void check_idempotent(void)
{
    CHECK(mount_with(count_fixes) == SPIFFS_OK, "remount");
    idempotent_fixes = 0;
    CHECK(SPIFFS_check(&fs) == SPIFFS_OK, "second check");
    CHECK(idempotent_fixes == 0, "second check still fixed %u things", idempotent_fixes);
    SPIFFS_unmount(&fs);
    srr_report_t again;
    srr_config_t config = repair_config();
    repair_boot(&again, &config);
    CHECK(again.result == SRR_RESULT_CLEAN, "second repair %s (spans %u gaps %u)", srr_result_name(again.result),
          again.spans_rewritten, again.gaps);
}

/* A4: appends, a probe file and room. */
static void check_health(const char *const *appendable, size_t count)
{
    mount_plain();
    for (size_t i = 0; i < count; ++i) {
        if (!exists(appendable[i])) continue;
        void *file = (void *)rel_open_append(appendable[i]);
        CHECK(file != NULL, "%s refuses appends errno %d", appendable[i], errno);
        shim_fclose(file);
    }
    static const uint8_t probe[64] = {1, 2, 3};
    write_file("/persistence-probe", probe, sizeof(probe));
    CHECK(SPIFFS_remove(&fs, "/persistence-probe") == SPIFFS_OK, "probe remove");
    SPIFFS_unmount(&fs);
}

/* A7: a whole-file reader (the 2.4.12/2.5.2 shape: line by line) still reads
 * every complete row that the damage did not touch. */
static void check_rollback_reader(const model_file_t *m)
{
    static uint8_t buffer[8 * 1024 * 1024];
    mount_plain();
    int64_t got = read_file(m->name, buffer, sizeof(buffer));
    SPIFFS_unmount(&fs);
    CHECK(got >= 0, "rollback reader %s", m->name);
    uint32_t start = 0;
    for (uint32_t i = 0; i < m->size; ++i) {
        if (m->data[i] != '\n') continue;
        bool touched = false;
        for (uint32_t k = start; k <= i && !touched; ++k) touched = m->unreadable[k];
        if (!touched) {
            /* The same bytes, still a row of their own. */
            CHECK(!memcmp(buffer + start, m->data + start, i - start + 1), "row at %u changed", start);
            CHECK(start == 0 || buffer[start - 1] == '\n', "%s row at %u lost its boundary", m->name, start);
        }
        start = i + 1;
    }
}

/* A9: every page whose lookup entry names a live file is one of its chosen
 * pages (its header, an index page of the file's spans, or a page an index
 * entry names). A stray page with the file's lookup id is what SPIFFS's own
 * GC moves and points an index entry at, whatever its flags say. */
static bool valid_index_flags(uint8_t flags)
{
    return !(flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) && (flags & SPIFFS_PH_FLAG_DELET);
}
static void check_no_strays(const char *scenario)
{
    uint32_t pages = flash_size / PAGE;
    uint8_t *chosen = calloc(pages, 1), *live_id = calloc(0x8000, 1);
    uint32_t *spans_of = calloc(0x8000, sizeof(uint32_t));
    for (uint32_t pix = 0; pix < pages; ++pix) {
        uint32_t a = pix * PAGE;
        uint16_t id = rd16(a);
        if (pix % 16 == 0 || id == 0xFFFF || !(id & 0x8000) || rd16(a + 2) != 0 || !valid_index_flags(flash[a + 4]) ||
            !(flash[a + 4] & SPIFFS_PH_FLAG_IXDELE))
            continue;
        uint32_t size;
        memcpy(&size, flash + a + 8, 4);
        if (size == 0xFFFFFFFFU) size = 0;
        live_id[id & 0x7FFF] = 1;
        spans_of[id & 0x7FFF] = (size + SPAN - 1) / SPAN;
        chosen[pix] = 1;
        for (uint32_t i = 0; i < 103 && i < spans_of[id & 0x7FFF]; ++i)
            if (rd16(a + 49 + i * 2) < pages) chosen[rd16(a + 49 + i * 2)] = 1;
    }
    for (uint32_t pix = 0; pix < pages; ++pix) {
        uint32_t a = pix * PAGE;
        uint16_t id = rd16(a);
        uint32_t j = rd16(a + 2);
        if (pix % 16 == 0 || id == 0xFFFF || !(id & 0x8000) || j == 0 || !valid_index_flags(flash[a + 4]) ||
            !live_id[id & 0x7FFF])
            continue;
        uint32_t spans = spans_of[id & 0x7FFF];
        if (spans <= 103 || j > 1 + (spans - 103 - 1) / 124) continue;
        chosen[pix] = 1;
        for (uint32_t i = 0; i < 124 && 103 + (j - 1) * 124 + i < spans; ++i)
            if (rd16(a + 8 + i * 2) < pages) chosen[rd16(a + 8 + i * 2)] = 1;
    }
    for (uint32_t pix = 0; pix < pages; ++pix) {
        if (pix % 16 == 0) continue;
        uint16_t lu = rd16(lookup_address(pix));
        if (lu == 0xFFFF || lu == 0 || !live_id[lu & 0x7FFF]) continue;
        CHECK(chosen[pix], "%s: page %04x (lookup %04x, header %04x span %u flags %02x) is a stray of a live file",
              scenario, pix, lu, rd16(pix * PAGE), rd16(pix * PAGE + 2), flash[pix * PAGE + 4]);
    }
    free(chosen);
    free(live_id);
    free(spans_of);
}

/* ---- scenarios ------------------------------------------------------- */
typedef struct {
    const char *name;
    void (*build)(void);
    bool expect_gaps;
} scenario_t;

static uint32_t base_generation;

/* A store shaped like the field: queues, outboxes, ids, commands, catalog. */
static void build_store(uint32_t queue_bytes)
{
    model_reset();
    row_seq = rnd() % 100000;
    mount_plain();
    make_json_file("/blocked_identity.jsonl", queue_bytes);
    make_json_file("/pending.jsonl", queue_bytes / 2);
    make_json_file("/add_pending.jsonl", 6000 + rnd() % 20000);
    make_id_file("/acked_uids.txt", 300 + rnd() % 300);
    make_id_file("/processed_commands.txt", 40);
    make_binary_file("/add_commands.jsonl", 1200);
    make_binary_file("/add_identities.enc", 9000);
    make_binary_file("/qo0000002a.q", 3000);
}

static void damage_deleted_head_with_copy(void)
{
    /* 13FL: the blocked queue's head page flagged deleted by an interrupted
     * GC move; the moved copy survives. Nothing is lost. */
    build_store(120000);
    model_file_t *m = model_of("/blocked_identity.jsonl");
    uint32_t cursor = row_boundary_after(m, 200 * SPAN - 300);
    set_cursor("legacy_queues", "blocked", cursor, prefix_crc(m, cursor), 9);
    uint32_t span = cursor / SPAN + 1;
    uint32_t pix = rd16(entry_address(m->name, span));
    uint32_t copy = free_page();
    copy_page(copy, pix);
    flag_deleted(pix);
    SPIFFS_unmount(&fs);
}

static void damage_deleted_head(void)
{
    /* 13FL: the referenced head page is flagged deleted and no copy exists;
     * its bytes are still on flash until the block is erased. */
    build_store(120000);
    model_file_t *m = model_of("/blocked_identity.jsonl");
    uint32_t cursor = row_boundary_after(m, 200 * SPAN - 300);
    set_cursor("legacy_queues", "blocked", cursor, prefix_crc(m, cursor), 9);
    flag_deleted(rd16(entry_address(m->name, cursor / SPAN + 1)));
    SPIFFS_unmount(&fs);
}

static void damage_erased_page(void)
{
    /* P02 (IS_FREE): the referenced page was erased. Those bytes are gone. */
    build_store(150000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 40 + rnd() % 60;
    erase_page(rd16(entry_address(m->name, span)));
    mark_unreadable(m, span);
    uint32_t cursor = row_boundary_after(m, 10 * SPAN);
    set_cursor("legacy_queues", "ords_pending", cursor, prefix_crc(m, cursor), 4);
    SPIFFS_unmount(&fs);
}

static void damage_foreign_page(void)
{
    /* P02 (DATA_SPAN_MISMATCH / another object): the entry names a page of
     * another file, which must stay untouched; this span's page is gone. */
    build_store(150000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 30 + rnd() % 40;
    uint32_t address = entry_address(m->name, span);
    erase_page(rd16(address));
    uint32_t other = rd16(entry_address("/blocked_identity.jsonl", 5 + rnd() % 50));
    wr16(address, (uint16_t)other);
    mark_unreadable(m, span);
    SPIFFS_unmount(&fs);
}

static void damage_span_mismatch(void)
{
    /* The entry names this file's own page of the next span. */
    build_store(150000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 20 + rnd() % 40;
    uint32_t address = entry_address(m->name, span);
    erase_page(rd16(address));
    wr16(address, rd16(entry_address(m->name, span + 1)));
    mark_unreadable(m, span);
    SPIFFS_unmount(&fs);
}

static void damage_duplicate_header(void)
{
    /* An append's header page move stopped before deleting the old header:
     * two headers, so the check would delete the file as multiply referenced. */
    build_store(80000);
    model_file_t *m = model_of("/add_pending.jsonl");
    uint32_t header = header_of(m->name);
    uint32_t copy = free_page();
    copy_page(copy, header);
    /* The stale copy: an older size. */
    uint32_t smaller = m->size - 50;
    memcpy(flash + copy * PAGE + 8, &smaller, 4);
    SPIFFS_unmount(&fs);
}

static void damage_duplicate_index(void)
{
    /* A GC move of an index page stopped before deleting the source. */
    build_store(120000);
    uint16_t obj = object_of("/blocked_identity.jsonl");
    copy_page(free_page(), index_page(obj, 1));
    SPIFFS_unmount(&fs);
}

static void damage_missing_index(void)
{
    /* An index page is gone; its data pages survive and are found again. */
    build_store(120000);
    uint16_t obj = object_of("/blocked_identity.jsonl");
    flag_deleted(index_page(obj, 2));
    SPIFFS_unmount(&fs);
}

static void damage_entry_past_end(void)
{
    /* An interrupted append left an index entry past the end; the next append
     * would AND its page index into it, and the check would chase it. */
    build_store(120000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t spans = (m->size + SPAN - 1) / SPAN;
    uint32_t address = entry_address(m->name, spans - 1) + 2;
    wr16(address, rd16(entry_address("/blocked_identity.jsonl", 3)));
    SPIFFS_unmount(&fs);
}

static void damage_dirty_tail(void)
{
    /* An interrupted append programmed bytes past the end of the last page. */
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t spans = (m->size + SPAN - 1) / SPAN;
    uint32_t pix = rd16(entry_address(m->name, spans - 1));
    uint32_t used = m->size % SPAN;
    if (used == 0 || used > SPAN - 4) {
        append_bytes(m->name, "{}\n", 3);
        m->size += 3;
        memcpy(m->data + m->size - 3, "{}\n", 3);
        free(m->unreadable);
        m->unreadable = calloc(m->size + 1, 1);
        spans = (m->size + SPAN - 1) / SPAN;
        pix = rd16(entry_address(m->name, spans - 1));
        used = m->size % SPAN;
    }
    flash[pix * PAGE + 5 + used] &= 0x7B;
    flash[pix * PAGE + 5 + used + 1] &= 0x22;
    SPIFFS_unmount(&fs);
}

static void damage_torn_tails(void)
{
    /* Interrupted appends: each target's last row has no newline. */
    build_store(60000);
    const char *names[] = {"/pending.jsonl", "/processed_commands.txt"};
    for (int i = 0; i < 2; ++i) {
        model_file_t *m = model_of(names[i]);
        const char *fragment = i == 0 ? "{\"event_uid\":\"abc" : "cmd-77";
        CHECK(append_bytes(m->name, fragment, (uint32_t)strlen(fragment)), "torn append");
        memcpy(m->data + m->size, fragment, strlen(fragment));
        m->size += (uint32_t)strlen(fragment);
        free(m->unreadable);
        m->unreadable = calloc(m->size + 1, 1);
    }
    SPIFFS_unmount(&fs);
}

static void damage_lookup_free(void)
{
    /* A used page whose lookup entry says free: the allocator would program
     * over it. */
    build_store(60000);
    model_file_t *m = model_of("/blocked_identity.jsonl");
    wr16(lookup_address(rd16(entry_address(m->name, 7))), 0xFFFF);
    SPIFFS_unmount(&fs);
}

static void damage_erased_bytes(void)
{
    /* A text page with some bytes reading erased: only those bytes are gaps. */
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 9;
    uint32_t pix = rd16(entry_address(m->name, span));
    for (uint32_t i = 100; i < 140; ++i) {
        flash[pix * PAGE + 5 + i] = 0xFF;
        m->unreadable[span * SPAN + i] = 1;
    }
    SPIFFS_unmount(&fs);
}

static void damage_leak_and_probe(void)
{
    /* P02 and 13FL together: a large removal stopped half way (leaked pages)
     * and the probe file's data page flagged deleted. */
    build_store(60000);
    static uint8_t big[160000];
    for (uint32_t i = 0; i < sizeof(big); ++i) big[i] = (uint8_t)('a' + i % 26);
    write_file("/old_pending.tmp", big, sizeof(big));
    uint32_t header = header_of("/old_pending.tmp");
    uint16_t obj = rd16(header * PAGE) & 0x7FFF;
    flash[header * PAGE + 4] &= (uint8_t)~SPIFFS_PH_FLAG_IXDELE;
    uint32_t freed = 0;
    for (uint32_t pix = 0; pix < flash_size / PAGE && freed < 200; ++pix)
        if (pix % 16 && rd16(pix * PAGE) == obj && rd16(lookup_address(pix)) == obj) {
            flag_deleted(pix);
            freed++;
        }
    static const uint8_t probe[32] = {9};
    write_file("/persistence-probe", probe, sizeof(probe));
    flag_deleted(rd16(entry_address("/persistence-probe", 0)));
    SPIFFS_unmount(&fs);
}

static void damage_stale_cursors(void)
{
    /* 2.4.12 rewrote the files after 2.7.x settled part of them: the cursors
     * no longer describe the bytes. One fails its CRC, one is past the end. */
    build_store(60000);
    model_file_t *pending = model_of("/pending.jsonl");
    uint32_t at = row_boundary_after(pending, 3000);
    set_cursor("legacy_queues", "ords_pending", at, prefix_crc(pending, at) ^ 1, 5);
    model_file_t *outbox = model_of("/add_pending.jsonl");
    set_cursor("add_legacy", "reconcile", outbox->size + 500, 0, 3);
    lq_checkpoint_t corrupt = {.version = 1, .generation = 2, .offset = 10};
    corrupt.crc = 1234;
    port_nvs_store(NULL, "legacy_queues", "blocked", &corrupt, sizeof(corrupt));
    SPIFFS_unmount(&fs);
}

static void damage_catalog(void)
{
    /* A catalog page lost: the catalog is dropped for ADD to resend. */
    build_store(60000);
    erase_page(rd16(entry_address("/add_identities.enc", 3)));
    ft_checkpoint_t cp = {.version = 1, .generation = 4, .phase = 0};
    cp.crc = dq_crc32(&cp, offsetof(ft_checkpoint_t, crc));
    port_nvs_store(NULL, "file_tx", "catalog", &cp, sizeof(cp));
    model_file_t *m = model_of("/add_identities.enc");
    m->size = 0;
    SPIFFS_unmount(&fs);
}

static model_file_t *new_model(const char *name, bool json, bool ids)
{
    model_file_t *m = &model[model_count++];
    snprintf(m->name, sizeof(m->name), "%s", name);
    m->data = malloc(1);
    m->unreadable = calloc(1, 1);
    m->json = json;
    m->ids = ids;
    return m;
}

static void model_append(model_file_t *m, const void *data, uint32_t n)
{
    m->data = realloc(m->data, m->size + n + 1);
    m->unreadable = realloc(m->unreadable, m->size + n + 1);
    memcpy(m->data + m->size, data, n);
    memset(m->unreadable + m->size, 0, n + 1);
    CHECK(append_bytes(m->name, data, n), "append %s %d", m->name, SPIFFS_errno(&fs));
    m->size += n;
}

static void model_append_ids(model_file_t *m, uint32_t count)
{
    for (uint32_t i = 0; i < count; ++i) {
        char id[66];
        for (int k = 0; k < 64; ++k) id[k] = "0123456789abcdef"[rnd() % 16];
        id[64] = '\n';
        model_append(m, id, 65);
    }
}

/* The lowest free page: the tail of the last block SPIFFS filled. */
static uint32_t lowest_free_page(void)
{
    for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix)
        if (pix % 16 && rd16(lookup_address(pix)) == 0xFFFF && flash[pix * PAGE + 4] == 0xFF) return pix;
    CHECK(0, "no free page");
    return 0;
}

static void damage_torn_lookup(void)
{
    /* NOR left bits of a lookup entry unprogrammed: an uncommitted copy of a
     * pending span has an entry naming no object, and live pages of another
     * file follow it in the block. SPIFFS_check deletes such a page after an
     * index search that overwrote the lookup page it is walking, then reads
     * the rest of the block through another block's entries (and deletes
     * live pages whose marking differs). The repair deletes it first. */
    build_store(120000);
    SPIFFS_unmount(&fs);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 103 + rnd() % 100; /* its index page lies in an earlier block */
    uint32_t pix = lowest_free_page();
    copy_page(pix, rd16(entry_address(m->name, span)));
    wr16(lookup_address(pix), (uint16_t)(object_of(m->name) | 0xFF00U));
    mount_plain();
    model_append_ids(model_of("/acked_uids.txt"), 60);
    SPIFFS_unmount(&fs);
}

static void full_store_with(uint32_t keep_erased);
static void damage_full_store(void) { full_store_with(15); }

/* Review 3 (boot), finding 1: the 8 MB workload's crammed stores. No free
 * block: the one SPIFFS kept for its GC holds deleted copies too, save five
 * erased pages; the damaged queue's work fits in them, but not with the
 * pages compacting the best block would need on top. The repair deferred
 * every boot, the queue unreadable from its damaged span. */
static void damage_thin_store(void) { full_store_with(5); }

static void full_store_with(uint32_t keep_erased)
{
    /* A store that has run a while: the queues interleaved in every block,
     * deleted pages everywhere and one free block. The pending queue's
     * head page is flagged deleted (its moved copy survives), and every
     * other free page holds a copy of it: one valid copy per block (an
     * interrupted GC move leaves one) and deleted ones. No block can be
     * compacted or quick-collected while the queue is damaged, so
     * normalization allocates from the pages SPIFFS keeps for its GC; the
     * check deletes the copies and compaction gives the pages back. */
    model_reset();
    row_seq = rnd() % 100000;
    mount_plain();
    model_file_t *pending = new_model("/pending.jsonl", true, false);
    model_file_t *blocked = new_model("/blocked_identity.jsonl", true, false);
    model_file_t *acked = new_model("/acked_uids.txt", false, true);
    char row[600];
    while (pending->size + blocked->size < flash_size / 2) {
        size_t n = json_row(row, sizeof(row));
        model_append(pending, row, (uint32_t)n);
        n = json_row(row, sizeof(row));
        model_append(blocked, row, (uint32_t)n);
        if (rnd() % 4 == 0) model_append_ids(acked, 1);
    }
    SPIFFS_unmount(&fs);
    uint32_t span = 10 + rnd() % 100;
    uint32_t head = rd16(entry_address(pending->name, span));
    uint32_t copy = free_page();
    copy_page(copy, head);
    flag_deleted(head);
    uint32_t blocks = flash_size / BLOCK, keep = blocks;
    for (uint32_t b = blocks; b-- > 0 && keep == blocks;) {
        bool empty = true;
        for (uint32_t e = 1; e < 16 && empty; ++e) empty = rd16(lookup_address(b * 16 + e)) == 0xFFFF;
        if (empty) keep = b;
    }
    CHECK(keep < blocks, "full_store: no free block");
    for (uint32_t pix = 0, pinned = blocks, left = keep_erased; pix < flash_size / PAGE; ++pix) {
        if (pix % 16 == 0 || rd16(lookup_address(pix)) != 0xFFFF) continue;
        if (pix / 16 == keep) {
            if (left) {
                left--;
                continue;
            }
            copy_page(pix, copy);
            flag_deleted(pix);
            continue;
        }
        copy_page(pix, copy);
        if (pinned == pix / 16) flag_deleted(pix);
        pinned = pix / 16;
    }
}

static void damage_torn_probe_size(void)
{
    /* The first append to a new file rewrites its header in place; a reset
     * there leaves a torn size. SPIFFS can neither truncate nor remove the
     * file and the check keeps it, so every later create of the name fails
     * (13FL's and P02's probe ENOENT). */
    build_store(60000);
    static const uint8_t probe[64] = {7};
    write_file("/repair-probe", probe, sizeof(probe));
    uint32_t header = header_of("/repair-probe");
    uint32_t torn = sizeof(probe) | 0xFFFFFF00U;
    memcpy(flash + header * PAGE + 8, &torn, 4);
    SPIFFS_unmount(&fs);
}

/* The ORDS drain caught up and lq_reclaim removed pending.jsonl; the next
 * punch creates it, and a reset during the first append's in-place header
 * program tears the size (high bytes left 0xFF, or bits anywhere). SPIFFS
 * can then neither read, append to, truncate nor remove the file. */
static void damage_torn_size(void)
{
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
    CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
    m->size = 0;
    char row[600];
    size_t n = json_row(row, sizeof(row));
    model_append(m, row, (uint32_t)n);
    if (rnd() & 1) {
        n = json_row(row, sizeof(row));
        uint8_t two[1200];
        memcpy(two, row, n);
        size_t k = json_row(row, sizeof(row));
        memcpy(two + n, row, k);
        /* One write of two rows: several data pages before the header. */
        CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
        m->size = 0;
        model_append(m, two, (uint32_t)(n + k));
    }
    uint32_t header = header_of(m->name), size;
    memcpy(&size, flash + header * PAGE + 8, 4);
    CHECK(size == m->size, "first append size %u", size);
    static const uint32_t masks[] = {0xFFFF0000U, 0x00370000U, 0x00000E00U, 0xFFFFFF00U, 0x01000000U};
    uint32_t torn = size | masks[rnd() % 5];
    memcpy(flash + header * PAGE + 8, &torn, 4);
    if (verbose) printf("  torn_size: %u bytes, header %04x size %08x\n", m->size, header, torn);
    SPIFFS_unmount(&fs);
}

/* A durable-queue segment opened with "wb" for its first record, and a reset
 * during that append's header program: dq_append's next "wb" would fail
 * forever, wedging the lane. */
static void damage_torn_segment(void)
{
    build_store(60000);
    model_file_t *m = model_of("/qo0000002a.q");
    m->data[m->size - 1] = '}';
    write_file(m->name, m->data, m->size);
    uint32_t header = header_of(m->name), size;
    memcpy(&size, flash + header * PAGE + 8, 4);
    uint32_t torn = size | (rnd() & 1 ? 0xFFFF0000U : 0x00F00000U);
    memcpy(flash + header * PAGE + 8, &torn, 4);
    SPIFFS_unmount(&fs);
}

/* SPIFFS reuses a removed file's object id: pending.jsonl reclaimed and
 * created again has the old file's id, and the old file's deleted pages of
 * the same spans are still on flash. A page of the new file is lost: the old
 * bytes must not take its place. */
static void damage_reincarnation(void)
{
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    uint16_t old = object_of(m->name);
    set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
    CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
    m->size = 0;
    char row[600];
    while (m->size < 20000) {
        size_t n = json_row(row, sizeof(row));
        model_append(m, row, (uint32_t)n);
    }
    CHECK(object_of(m->name) == old, "object id not reused");
    uint32_t span = 20 + rnd() % 40;
    erase_page(rd16(entry_address(m->name, span)));
    mark_unreadable(m, span);
    SPIFFS_unmount(&fs);
    /* Sometimes the old file's page of that span is caught mid-delete: its
     * lookup entry already deleted, its header still valid. */
    if (rnd() & 1) {
        bool found = false;
        for (uint32_t pix = 0; pix < flash_size / PAGE && !found; ++pix) {
            if (pix % 16 == 0 || rd16(pix * PAGE) != old || rd16(pix * PAGE + 2) != span || rd16(lookup_address(pix)))
                continue;
            flash[pix * PAGE + 4] |= SPIFFS_PH_FLAG_DELET;
            found = true;
        }
        CHECK(found, "no page of the earlier file");
    }
}

/* A binary file (a queue segment): an interrupted GC move left a finalized
 * copy of a span taken while it held 100 bytes; appends then filled the
 * referenced page in place; a later interrupted move flagged the referenced
 * page deleted (sometimes after copying it in full). */
static void damage_binary_stale(void)
{
    build_store(60000);
    model_file_t *m = model_of("/qo0000002a.q");
    uint32_t span = 5, pix = rd16(entry_address(m->name, span));
    SPIFFS_unmount(&fs);
    uint32_t stale = lowest_free_page();
    copy_page(stale, pix);
    memset(flash + stale * PAGE + 5 + 100, 0xFF, SPAN - 100);
    if (rnd() & 1) copy_page(free_page(), pix);
    flag_deleted(pix);
}

/* Two copies of a span whose referenced page is gone disagree on one byte:
 * that byte is a gap, every other byte is kept. */
static void damage_copies_disagree(void)
{
    build_store(60000);
    model_file_t *m = model_of("/qo0000002a.q");
    uint32_t span = 7, at = span * SPAN + 77;
    m->data[at] = 0x55;
    write_file(m->name, m->data, m->size);
    uint32_t pix = rd16(entry_address(m->name, span));
    SPIFFS_unmount(&fs);
    uint32_t a = free_page();
    copy_page(a, pix);
    uint32_t b = free_page();
    copy_page(b, pix);
    flash[b * PAGE + 5 + 77] = 0xAA;
    if (rnd() & 1) memset(flash + a * PAGE + 5 + 200, 0xFF, SPAN - 200); /* and one copy is stale */
    erase_page(pix);
    m->unreadable[at] = 1;
}

/* Pages whose lookup entry and header name a live file and that are none of
 * its pages: a header program torn on SPIFFS's append path (flags 0xFD:
 * finalized but still marked unused), an unfinalized copy, one past the end,
 * an index page. SPIFFS_check ignores 0xFD, but SPIFFS's GC moves every page
 * whose lookup entry names the file and points that span at the copy. */
static uint32_t stray_page;
static void damage_strays(void)
{
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    uint16_t obj = object_of(m->name);
    uint32_t spans = (m->size + SPAN - 1) / SPAN, ref = rd16(entry_address(m->name, 10));
    SPIFFS_unmount(&fs);
    static const uint8_t flags[] = {0xFD, 0xFE, 0xFF, 0xFC};
    uint32_t kind = rnd() % 4;
    stray_page = 0;
    for (uint32_t pix = 16 * 4; pix < flash_size / PAGE && !stray_page; ++pix) {
        if (pix % 16 == 0 || pix / 16 == ref / 16 || rd16(lookup_address(pix)) != 0xFFFF) continue;
        bool erased = true;
        for (uint32_t i = 0; i < PAGE && erased; ++i) erased = flash[pix * PAGE + i] == 0xFF;
        if (erased) stray_page = pix;
    }
    CHECK(stray_page, "no page");
    uint32_t p = stray_page;
    bool index = kind == 3;
    wr16(lookup_address(p), (uint16_t)(index ? obj | 0x8000 : obj));
    wr16(p * PAGE, (uint16_t)(index ? obj | 0x8000 : obj));
    wr16(p * PAGE + 2, (uint16_t)(index ? 1 : (rnd() & 1) ? 10 : spans + 2));
    flash[p * PAGE + 4] = index ? 0xF9 : flags[rnd() % 3];
    if (!index && flash[p * PAGE + 4] == 0xFE) memcpy(flash + p * PAGE + 5, flash + ref * PAGE + 5, 100);
}

/* P02's erased page, and its place since reused by an interrupted write of
 * the same span (header written, unfinalized, a few bytes programmed). The
 * index still names that place; an unfinalized page is never a source. */
static void damage_reused_ref(void)
{
    build_store(150000);
    model_file_t *m = model_of("/pending.jsonl");
    uint32_t span = 40 + rnd() % 60, pix = rd16(entry_address(m->name, span));
    uint16_t obj = object_of(m->name);
    erase_page(pix);
    mark_unreadable(m, span);
    wr16(lookup_address(pix), obj);
    wr16(pix * PAGE, obj);
    wr16(pix * PAGE + 2, (uint16_t)span);
    flash[pix * PAGE + 4] = 0xFE; /* used, not finalized */
    for (uint32_t i = 0; i < SPAN; i += 3) flash[pix * PAGE + 5 + i] = (uint8_t)('a' + i % 26);
    SPIFFS_unmount(&fs);
}

/* A text page with scattered erased bytes (a torn program): one record, its
 * bounds over every unreadable byte, flagged merged. */
static void damage_scattered_bytes(void)
{
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    for (uint32_t span = 11; span <= 12; ++span) {
        uint32_t pix = rd16(entry_address(m->name, span));
        for (uint32_t i = 3; i < SPAN; i += 7) {
            flash[pix * PAGE + 5 + i] = 0xFF;
            m->unreadable[span * SPAN + i] = 1;
        }
    }
    SPIFFS_unmount(&fs);
}

/* A probe file's removal stopped inside its header's delete: the header
 * hidden, its lookup entry half cleared (0x8007 left 0x0007: it now names a
 * data object). Pending's pages follow in the same block. SPIFFS_check's
 * handling of such an entry reads the rest of the block through another
 * block's lookup page and deletes live pages, then the file they belong to;
 * nothing claims the hidden probe, so its page goes before the check. */
static void damage_torn_hidden_lookup(void)
{
    build_store(60000);
    static const uint8_t probe[32] = {5};
    write_file("/persistence-probe", probe, sizeof(probe));
    uint32_t header = header_of("/persistence-probe");
    model_file_t *m = model_of("/pending.jsonl");
    char row[600];
    for (int i = 0; i < 12; ++i) {
        size_t n = json_row(row, sizeof(row));
        model_append(m, row, (uint32_t)n);
    }
    SPIFFS_unmount(&fs);
    uint16_t obj = rd16(header * PAGE);
    flash[header * PAGE + 4] &= (uint8_t)~SPIFFS_PH_FLAG_IXDELE;
    wr16(lookup_address(header), (uint16_t)(obj & 0x7FFF));
}

/* Review 3, finding 7: a recreated pending queue's first append tore its
 * header size with only low bits left set, so the size stays inside the
 * last data page. The bytes past the real end read erased; they were taken
 * for a SALVAGE gap and a junk row. The size its bytes hold is recovered. */
/* A bit the size clears whose tear keeps the size inside its last span. */
static bool torn_inside_possible(uint32_t size)
{
    for (uint32_t b = 1; b < 256; b <<= 1)
        if ((size | b) != size && ((size | b) + SPAN - 1) / SPAN == (size + SPAN - 1) / SPAN) return true;
    return false;
}

static void damage_torn_size_inside(void)
{
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
    CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
    m->size = 0;
    char row[600];
    size_t n;
    /* A row length whose size can tear inside its last span at all (one
     * like 0x1FB, with every low bit set, cannot). */
    do n = json_row(row, sizeof(row));
    while ((n % SPAN) > SPAN - 40 || (n % SPAN) < 8 || !torn_inside_possible((uint32_t)n));
    model_append(m, row, (uint32_t)n);
    uint32_t header = header_of(m->name), size;
    memcpy(&size, flash + header * PAGE + 8, 4);
    static const uint32_t masks[] = {0x01, 0x02, 0x10, 0x40, 0x03};
    uint32_t torn = size | (masks[rnd() % 5] & ~size);
    if (torn == size || (torn + SPAN - 1) / SPAN != (size + SPAN - 1) / SPAN)
        for (uint32_t b = 1; b < 256; b <<= 1)
            if ((size | b) != size && ((size | b) + SPAN - 1) / SPAN == (size + SPAN - 1) / SPAN) {
                torn = size | b;
                break;
            }
    CHECK(torn != size && (torn + SPAN - 1) / SPAN == (size + SPAN - 1) / SPAN, "torn size leaves the span");
    memcpy(flash + header * PAGE + 8, &torn, 4);
    SPIFFS_unmount(&fs);
}

/* Review 3, finding 6: a queue segment (dq_append writes its next record at
 * its end with "r+b") whose last page holds bytes past its end that an
 * interrupted append programmed: the repair cleaned dirty tails only for
 * append targets, so the next record was ANDed with them on flash. */
static void damage_segment_dirty_tail(void)
{
    build_store(60000);
    model_file_t *m = model_of("/qo0000002a.q");
    m->size = 2800;
    write_file(m->name, m->data, m->size);
    uint32_t spans = (m->size + SPAN - 1) / SPAN, used = m->size % SPAN;
    uint32_t pix = rd16(entry_address(m->name, spans - 1));
    for (uint32_t i = 0; i < 24; ++i) flash[pix * PAGE + 5 + used + i] &= (uint8_t)(0x30 + i);
    SPIFFS_unmount(&fs);
}

/* A10 (review 3): no live file's last page holds a programmed byte past its
 * end; SPIFFS's next append (any file: SPIFFS appends in place) would AND
 * its bytes with it. */
static void check_clean_tails(const char *scenario)
{
    uint32_t pages = flash_size / PAGE;
    for (uint32_t pix = 0; pix < pages; ++pix) {
        uint32_t a = pix * PAGE;
        if (pix % 16 == 0 || !(rd16(a) & 0x8000) || rd16(a) == 0xFFFF || rd16(a + 2) != 0 ||
            !valid_index_flags(flash[a + 4]) || !(flash[a + 4] & SPIFFS_PH_FLAG_IXDELE))
            continue;
        uint32_t size;
        memcpy(&size, flash + a + 8, 4);
        if (size == 0xFFFFFFFFU || size > flash_size || !(size % SPAN)) continue;
        uint32_t last = (size - 1) / SPAN, ref;
        if (last < 103) ref = rd16(a + 49 + last * 2);
        else {
            uint16_t obj = rd16(a) & 0x7FFF;
            uint32_t j = 1 + (last - 103) / 124, index = 0;
            for (uint32_t q = 0; q < pages && !index; ++q)
                if (q % 16 && rd16(q * PAGE) == (uint16_t)(obj | 0x8000) && rd16(q * PAGE + 2) == j &&
                    valid_index_flags(flash[q * PAGE + 4]))
                    index = q;
            if (!index) continue;
            ref = rd16(index * PAGE + 8 + ((last - 103) % 124) * 2);
        }
        if (ref >= pages) continue;
        for (uint32_t i = size % SPAN; i < SPAN; ++i)
            CHECK(flash[ref * PAGE + 5 + i] == 0xFF, "%s: %.32s (size %u) has byte %02x past its end at %u", scenario,
                  (const char *)flash + a + offsetof(spiffs_page_object_ix_header, name), size, flash[ref * PAGE + 5 + i], i - size % SPAN);
    }
}

static const char *const appendable[] = {"/pending.jsonl", "/blocked_identity.jsonl", "/add_pending.jsonl",
                                         "/acked_uids.txt", "/processed_commands.txt"};

static void verify_scenario(const char *name, const srr_report_t *report, bool torn)
{
    CHECK(report->result != SRR_RESULT_FAILED && report->result != SRR_RESULT_DEFERRED &&
              report->result != SRR_RESULT_STOPPED,
          "%s: repair %s at %s (%ld)", name, srr_result_name(report->result), report->error_stage,
          (long)report->error_code);
    CHECK(report->check_deleted_live == 0, "%s: the check deleted %u live files", name, report->check_deleted_live);
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) {
        model_file_t *m = &model[i];
        if (!strcmp(m->name, "/add_identities.enc") && m->size == 0) {
            CHECK(!exists(m->name), "%s: damaged catalog kept", name);
            continue;
        }
        CHECK(exists(m->name), "%s: %s removed", name, m->name);
        bool terminated = torn && (!strcmp(m->name, "/pending.jsonl") || !strcmp(m->name, "/processed_commands.txt"));
        check_custody(m, base_generation, terminated);
    }
    CHECK(!exists("/persistence-probe"), "%s: probe leftover kept", name);
    CHECK(!exists("/repair-probe") && !exists("/repair-probe-2"), "%s: repair probe left", name);
    SPIFFS_unmount(&fs);
    check_no_strays(name);
    check_clean_tails(name);
    mount_plain();
    uint32_t evidence;
    const struct { const char *name, *ns, *key; } queues[] = {
        {"/pending.jsonl", "legacy_queues", "ords_pending"},
        {"/blocked_identity.jsonl", "legacy_queues", "blocked"},
        {"/add_pending.jsonl", "add_legacy", "reconcile"},
    };
    for (size_t q = 0; q < 3; ++q) {
        model_file_t *m = model_of(queues[q].name);
        bool damaged = false;
        for (uint32_t i = 0; m && i < m->size; ++i) damaged |= m->unreadable[i] != 0;
        /* Drain a copy of the cursor: the stored one stays for A5. */
        lq_checkpoint_t saved;
        int had = port_nvs_load(NULL, queues[q].ns, queues[q].key, &saved, sizeof(saved));
        uint32_t rows = drain(queues[q].name, queues[q].ns, queues[q].key, &evidence);
        CHECK(rows > 0 || !m || !m->size, "%s: %s drained nothing", name, queues[q].name);
        CHECK(evidence <= (damaged ? 4U : 0U) + (torn && q == 0 ? 1U : 0U), "%s: %s %u evidence rows", name,
              queues[q].name, evidence);
        if (had == 1) port_nvs_store(NULL, queues[q].ns, queues[q].key, &saved, sizeof(saved));
        else port_nvs_erase(NULL, queues[q].ns, queues[q].key);
    }
    ft_checkpoint_t catalog;
    if (port_nvs_load(NULL, "file_tx", "catalog", &catalog, sizeof(catalog)) == 1) {
        static int loaded_context;
        (void)loaded_context;
    }
    SPIFFS_unmount(&fs);
    check_health(appendable, sizeof(appendable) / sizeof(appendable[0]));
    for (uint32_t i = 0; i < model_count; ++i)
        if (model[i].json && model[i].size) check_rollback_reader(&model[i]);
    /* SPIFFS's own GC on every block, as the runtime's writes would run it:
     * nothing a live file reads moves or goes. */
    mount_plain();
    for (uint32_t b = 0; b < flash_size / BLOCK && (uint32_t)fs.free_blocks > 2; ++b) {
        bool used = false;
        for (uint32_t e = 1; e < 16 && !used; ++e) used = rd16(lookup_address(b * 16 + e)) != 0xFFFF;
        if (!used || b % 3) continue;
        fs.cleaning = 1;
        s32_t res = spiffs_gc_clean(&fs, (spiffs_block_ix)b);
        fs.cleaning = 0;
        if (res == SPIFFS_OK) res = spiffs_gc_erase_page_stats(&fs, (spiffs_block_ix)b);
        if (res == SPIFFS_OK) res = spiffs_erase_block(&fs, (spiffs_block_ix)b);
        CHECK(res == SPIFFS_OK, "%s: SPIFFS GC of block %u failed %d", name, b, res);
    }
    for (uint32_t i = 0; i < model_count; ++i) {
        model_file_t *m = &model[i];
        if (!exists(m->name)) continue;
        bool terminated = torn && (!strcmp(m->name, "/pending.jsonl") || !strcmp(m->name, "/processed_commands.txt"));
        check_custody(m, base_generation, terminated);
    }
    SPIFFS_unmount(&fs);
    check_idempotent();
    /* A queue segment takes its next "wb" (dq_append's first record). */
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) {
        size_t length = strlen(model[i].name);
        if (length < 3 || strcmp(model[i].name + length - 2, ".q") || !exists(model[i].name)) continue;
        spiffs_file f = SPIFFS_open(&fs, model[i].name, SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
        CHECK(f >= 0, "%s: %s refuses \"wb\" (%d)", name, model[i].name, SPIFFS_errno(&fs));
        CHECK(SPIFFS_write(&fs, f, (void *)"{}", 2) == 2 && SPIFFS_close(&fs, f) >= 0, "%s: %s write", name,
              model[i].name);
    }
    SPIFFS_unmount(&fs);
}

static const scenario_t scenarios[] = {
    {"deleted_head_with_copy", damage_deleted_head_with_copy, false},
    {"deleted_head", damage_deleted_head, false},
    {"erased_page", damage_erased_page, true},
    {"foreign_page", damage_foreign_page, true},
    {"span_mismatch", damage_span_mismatch, true},
    {"duplicate_header", damage_duplicate_header, false},
    {"duplicate_index", damage_duplicate_index, false},
    {"missing_index", damage_missing_index, false},
    {"entry_past_end", damage_entry_past_end, false},
    {"dirty_tail", damage_dirty_tail, false},
    {"torn_tails", damage_torn_tails, false},
    {"lookup_free", damage_lookup_free, false},
    {"erased_bytes", damage_erased_bytes, true},
    {"leak_and_probe", damage_leak_and_probe, false},
    {"stale_cursors", damage_stale_cursors, false},
    {"catalog", damage_catalog, true},
    {"torn_lookup", damage_torn_lookup, false},
    {"full_store", damage_full_store, false},
    {"torn_probe_size", damage_torn_probe_size, false},
    {"torn_size", damage_torn_size, false},
    {"torn_segment", damage_torn_segment, false},
    {"reincarnation", damage_reincarnation, true},
    {"binary_stale", damage_binary_stale, false},
    {"copies_disagree", damage_copies_disagree, true},
    {"strays", damage_strays, false},
    {"reused_ref", damage_reused_ref, true},
    {"scattered_bytes", damage_scattered_bytes, true},
    {"torn_hidden_lookup", damage_torn_hidden_lookup, false},
    {"torn_size_inside", damage_torn_size_inside, false},
    {"segment_dirty_tail", damage_segment_dirty_tail, false},
    {"thin_store", damage_thin_store, false},
};

static void run_scenario(const scenario_t *s, uint64_t seed_value, uint32_t kib)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    s->build();
    srr_report_t report;
    srr_config_t config = repair_config();
    srp_state_t state;
    base_generation = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 ? state.generation + 1 : 1;
    repair_boot(&report, &config);
    if (verbose) printf("%s seed %" PRIu64 ": %s\n", s->name, seed_value, srr_result_name(report.result));
    CHECK(!s->expect_gaps || report.gaps > 0, "%s: no gap reported", s->name);
    CHECK(s->expect_gaps || report.gaps == 0, "%s: %u unexpected gaps", s->name, report.gaps);
    if (!strcmp(s->name, "segment_dirty_tail") && report.result != SRR_RESULT_FAILED) {
        /* dq_append's next record: "r+b", at its end. */
        model_file_t *m = model_of("/qo0000002a.q");
        uint8_t record[24];
        for (uint32_t i = 0; i < sizeof(record); ++i) record[i] = (uint8_t)(0xC5 ^ i);
        mount_plain();
        spiffs_file f = SPIFFS_open(&fs, m->name, SPIFFS_O_RDWR, 0);
        CHECK(f >= 0 && SPIFFS_lseek(&fs, f, (s32_t)m->size, SPIFFS_SEEK_SET) >= 0 &&
                  SPIFFS_write(&fs, f, record, sizeof(record)) == (s32_t)sizeof(record) && SPIFFS_close(&fs, f) >= 0,
              "segment append");
        static uint8_t buffer[8192];
        int64_t got = read_file(m->name, buffer, sizeof(buffer));
        SPIFFS_unmount(&fs);
        CHECK(got == (int64_t)(m->size + sizeof(record)) && !memcmp(buffer + m->size, record, sizeof(record)),
              "segment_dirty_tail: the next record was ANDed with the old bytes");
        m->data = realloc(m->data, m->size + sizeof(record) + 1);
        m->unreadable = realloc(m->unreadable, m->size + sizeof(record) + 1);
        memcpy(m->data + m->size, record, sizeof(record));
        memset(m->unreadable + m->size, 0, sizeof(record) + 1);
        m->size += sizeof(record);
    }
    verify_scenario(s->name, &report, !strcmp(s->name, "torn_tails"));
    if (!strcmp(s->name, "stale_cursors")) CHECK(report.cursors_reset == 3, "cursors reset %u", report.cursors_reset);
    if (!strcmp(s->name, "torn_lookup")) CHECK(report.lookup_fixed >= 1, "torn lookup entry left");
    if (!strcmp(s->name, "torn_probe_size")) CHECK(report.expendables_removed >= 1, "torn probe not purged");
    if (!strcmp(s->name, "torn_size") || !strcmp(s->name, "torn_segment") || !strcmp(s->name, "torn_size_inside"))
        CHECK(report.sizes_recovered == 1, "%s: size not recovered", s->name);

    if (!strcmp(s->name, "strays")) CHECK(report.strays_removed >= 1, "stray page %04x left", stray_page);
    if (!strcmp(s->name, "copies_disagree")) CHECK(report.gaps == 1 && report.unreadable_bytes == 1, "disagreement");
    if (!strcmp(s->name, "scattered_bytes")) CHECK(report.gaps == 1, "scattered bytes: %u records", report.gaps);
    if (!strcmp(s->name, "thin_store")) CHECK(report.reserve_pages > 0, "thin_store: no reserve used");
    if (!strcmp(s->name, "full_store"))
        CHECK(report.reserve_pages > 0 || report.blocks_compacted > 0, "full_store: no room made (%s)",
              srr_result_name(report.result));
    if (!strcmp(s->name, "leak_and_probe")) {
        CHECK(report.used_after < report.used_before, "leak not freed %u -> %u", report.used_before, report.used_after);
        CHECK(report.check_passes >= 1, "no check ran");
    }
}

/* The same damaged store repaired with and without the metadata mirror
 * must end in byte-identical flash and the same report. */
static void run_mirror_equivalence(const scenario_t *s, uint64_t seed_value, uint32_t kib)
{
    uint8_t *before, *after_plain;
    srr_report_t plain, mirrored;
    srr_config_t config = repair_config();
    seed(seed_value);
    nvs_clear();
    use_mirror = false;
    format_flash(kib);
    s->build();
    before = malloc(flash_size);
    memcpy(before, flash, flash_size);
    __typeof__(nvs) *nvs_before = malloc(sizeof(nvs));
    memcpy(nvs_before, &nvs, sizeof(nvs));
    repair_boot(&plain, &config);
    after_plain = malloc(flash_size);
    memcpy(after_plain, flash, flash_size);
    memcpy(flash, before, flash_size);
    memcpy(&nvs, nvs_before, sizeof(nvs));
    use_mirror = true;
    repair_boot(&mirrored, &config);
    use_mirror = false;
    CHECK(!memcmp(flash, after_plain, flash_size), "%s: mirror changed the repaired flash", s->name);
    CHECK(plain.result == mirrored.result && plain.gaps == mirrored.gaps &&
              plain.spans_rewritten == mirrored.spans_rewritten && plain.check_passes == mirrored.check_passes &&
              plain.check_fix_index == mirrored.check_fix_index && plain.check_delete_page == mirrored.check_delete_page,
          "%s: mirror changed the report", s->name);
    CHECK(mirror.hits > mirror.misses, "%s: mirror served %u of %u reads", s->name, mirror.hits,
          mirror.hits + mirror.misses);
    free(before);
    free(after_plain);
    free(nvs_before);
}

/* ---- budget, memory and the boot policy ------------------------------ */
/* Nothing held in PSRAM (13FL's budget shortfall): normalization still makes
 * every custody file consistent, so the check runs and loses nothing. */
static void run_without_holds(const scenario_t *s, uint64_t seed_value, uint32_t kib)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    s->build();
    srr_report_t report;
    srr_config_t config = repair_config();
    config.hold_budget = 0;
    base_generation = 1;
    repair_boot(&report, &config);
    CHECK(report.files_held == 0, "%s: held %u files with no budget", s->name, report.files_held);
    verify_scenario(s->name, &report, !strcmp(s->name, "torn_tails"));
}

/* Allocation failing at every level: the run fails or stops cleanly, and the
 * next run (memory back) still meets every oracle. */
static void run_with_memory_limits(const scenario_t *s, uint64_t seed_value, uint32_t kib)
{
    static const uint64_t limits[] = {16 * 1024, 64 * 1024, 160 * 1024, 320 * 1024};
    for (size_t i = 0; i < sizeof(limits) / sizeof(limits[0]); ++i) {
        seed(seed_value);
        nvs_clear();
        format_flash(kib);
        s->build();
        srr_report_t report;
        srr_config_t config = repair_config();
        memory_limit = limits[i];
        repair_boot(&report, &config);
        memory_limit = 0;
        CHECK(report.check_deleted_live == 0, "%s with %" PRIu64 " bytes: the check deleted a live file", s->name,
              limits[i]);
        srp_state_t state;
        base_generation = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 ? 1 : 1;
        repair_boot(&report, &config);
        verify_scenario(s->name, &report, !strcmp(s->name, "torn_tails"));
    }
}

static void store_state(uint32_t phase, uint32_t incomplete, uint32_t failures, uint32_t flags, uint32_t tag)
{
    srp_state_t state;
    memset(&state, 0, sizeof(state));
    state.generation = 3;
    state.phase = phase;
    state.incomplete = incomplete;
    state.failures = failures;
    state.flags = flags;
    state.image_tag = tag;
    srp_state_seal(&state);
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state));
}

/* D1: runs ended by a crash, a watchdog or the deadline stop repairs for the
 * image; another image tries again; an unconfirmed image after an incomplete
 * run normalizes only. Power cuts never count. */
static void run_policy(uint32_t kib)
{
    uint8_t *before = malloc(flash_size ? flash_size : kib * 1024U);
    /* Two crashed runs, and this boot follows a third: report only. */
    seed(4242);
    nvs_clear();
    format_flash(kib);
    damage_deleted_head();
    free(before);
    before = malloc(flash_size);
    memcpy(before, flash, flash_size);
    store_state(SRP_PHASE_CHECK, 2, 2, 0, 0);
    srr_report_t report;
    srr_config_t config = repair_config();
    config.previous_failed = true;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPORT_ONLY && report.mode == SRP_MODE_REPORT_ONLY, "guard: %s",
          srr_result_name(report.result));
    CHECK(!memcmp(before, flash, flash_size), "guard: report-only wrote to flash");
    /* The same image stays off, whatever resets follow. */
    config.previous_failed = false;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPORT_ONLY && !memcmp(before, flash, flash_size), "guard: same image ran");
    /* Another image tries again. */
    config.image_tag ^= 0xFFFF;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPAIRED && report.check_passes >= 1, "guard: new image %s",
          srr_result_name(report.result));
    /* Power cuts during runs never disable repairs. */
    seed(4243);
    nvs_clear();
    format_flash(kib);
    damage_deleted_head();
    store_state(SRP_PHASE_NORMALIZE, 7, 0, 0, 0);
    config = repair_config();
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPAIRED && report.incomplete == 8 && report.failures == 0,
          "power cuts counted: %s", srr_result_name(report.result));
    /* An unconfirmed image after an incomplete run: normalize, no check. */
    seed(4244);
    nvs_clear();
    format_flash(kib);
    damage_erased_page();
    store_state(SRP_PHASE_CHECK, 1, 0, 0, 0);
    config = repair_config();
    config.pending_verify = true;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_NORMALIZED && report.check_passes == 0 && report.spans_rewritten >= 1,
          "pending verify: %s passes %u", srr_result_name(report.result), report.check_passes);
    free(before);
}

/* ---- power cuts inside the repair ------------------------------------ */
/* A8: a cut at any flash write of the repair; boots repeat until a run
 * finishes. Nothing readable may be lost unless a gap record names it. */
static void run_cut_inside_repair(const scenario_t *s, uint64_t seed_value, uint32_t kib)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    s->build();
    srp_state_t state;
    base_generation = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 ? state.generation + 1 : 1;
    /* Count the repair's flash writes once, on a copy. */
    uint8_t *store = malloc(flash_size);
    memcpy(store, flash, flash_size);
    __typeof__(nvs) *nvs_before = malloc(sizeof(nvs));
    memcpy(nvs_before, &nvs, sizeof(nvs));
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t ops_before = flash_ops;
    repair_boot(&report, &config);
    uint64_t writes = flash_ops - ops_before;
    memcpy(flash, store, flash_size);
    memcpy(&nvs, nvs_before, sizeof(nvs));
    free(store);
    free(nvs_before);
    if (!writes) return;
    volatile uint32_t boots = 0;
    volatile uint32_t cuts = 0;
    uint64_t target = 1 + rnd() % writes;
    cut_at = flash_ops + target;
    for (;;) {
        if (setjmp(cut_point)) {
            /* Power cut: every byte of RAM state is gone. */
            cut_at = 0;
            cuts++;
            drop_all_memory();
            /* A second cut, sometimes, in the next boot's repair. */
            if (cuts < 2 && (rnd() & 1)) cut_at = flash_ops + 1 + rnd() % (writes + 1);
            continue;
        }
        CHECK(++boots < 12, "%s: repair never finished after %u boots", s->name, boots);
        srr_config_t next = repair_config();
        repair_boot(&report, &next);
        if (report.result != SRR_RESULT_STOPPED) break;
    }
    cut_at = 0;
    CHECK(report.result != SRR_RESULT_FAILED, "%s after cuts: %s at %s", s->name, srr_result_name(report.result),
          report.error_stage);
    mount_plain();
    srp_gaps_t gaps = gaps_now();
    for (uint32_t i = 0; i < model_count; ++i) {
        model_file_t *m = &model[i];
        if (!m->size || !strcmp(m->name, "/add_identities.enc")) continue;
        static uint8_t buffer[8 * 1024 * 1024];
        int64_t got = exists(m->name) ? read_file(m->name, buffer, sizeof(buffer)) : 0;
        CHECK(got >= 0, "%s after cuts (seed %" PRIu64 "): %s unreadable", s->name, seed_value, m->name);
        /* Every byte the damage left readable is back at its offset, or a
         * gap record of these runs names it: a truncated or missing file
         * included. */
        uint32_t unnamed = 0, first = UINT32_MAX;
        for (uint32_t k = 0; k < m->size; ++k) {
            if (m->unreadable[k] || ((uint64_t)got > k && buffer[k] == m->data[k])) continue;
            bool covered = false;
            for (uint32_t g = 0; g < gaps.count && !covered; ++g)
                covered = !strcmp(gaps.gap[g].name, m->name) && gaps.gap[g].generation >= base_generation &&
                    k >= gaps.gap[g].offset && k - gaps.gap[g].offset < gaps.gap[g].length;
            if (!covered && !unnamed++) first = k;
        }
        CHECK(!unnamed, "%s after cuts (seed %" PRIu64 "): %s lost %u bytes no gap record names (first %u, file %"
              PRId64 " of %u)", s->name, seed_value, m->name, unnamed, first, got, m->size);
    }
    SPIFFS_unmount(&fs);
}

/* ---- interrupted checks and restores ---------------------------------- */
static void manifest_begin(srp_manifest_t *man, uint32_t flags)
{
    memset(man, 0, sizeof(*man));
    man->generation = 3;
    man->image_tag = repair_config().image_tag;
    man->flags = flags;
}
static void manifest_add(srp_manifest_t *man, const model_file_t *m, uint8_t cls, const char *ns, const char *key)
{
    srp_manifest_file_t *f = &man->file[man->count++];
    snprintf(f->name, sizeof(f->name), "%s", m->name);
    f->cls = cls;
    f->object_id = object_of(m->name);
    f->size = m->size;
    f->crc = srp_crc32(0, m->data, m->size);
    lq_checkpoint_t cp;
    if (ns && port_nvs_load(NULL, ns, key, &cp, sizeof(cp)) == 1) {
        f->cursor_generation = cp.generation;
        f->flags |= SRP_MANIFEST_CURSOR;
    }
}
static void manifest_store(srp_manifest_t *man)
{
    srp_manifest_seal(man);
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "manifest", man, sizeof(*man));
}
static void hide_header(const char *name)
{
    uint32_t header = header_of(name);
    flash[header * PAGE + 4] &= (uint8_t)~SPIFFS_PH_FLAG_IXDELE;
}

/* Every byte of the model is back at its offset, or a gap record of these
 * runs names it. */
static void check_named(const char *what, const model_file_t *m, uint32_t generation_from)
{
    static uint8_t buffer[8 * 1024 * 1024];
    mount_plain();
    int64_t got = exists(m->name) ? read_file(m->name, buffer, sizeof(buffer)) : 0;
    srp_gaps_t gaps = gaps_now();
    SPIFFS_unmount(&fs);
    CHECK(got >= 0, "%s: %s unreadable", what, m->name);
    uint32_t unnamed = 0, first = UINT32_MAX;
    for (uint32_t k = 0; k < m->size; ++k) {
        if (m->unreadable[k] || ((uint64_t)got > k && buffer[k] == m->data[k])) continue;
        bool covered = false;
        for (uint32_t g = 0; g < gaps.count && !covered; ++g)
            covered = !strcmp(gaps.gap[g].name, m->name) && gaps.gap[g].generation >= generation_from &&
                k >= gaps.gap[g].offset && k - gaps.gap[g].offset < gaps.gap[g].length;
        if (!covered && !unnamed++) first = k;
    }
    CHECK(!unnamed, "%s: %s lost %u bytes no gap record names (first %u, file %" PRId64 " of %u)", what, m->name,
          unnamed, first, got, m->size);
}

/* Boots until a run finishes (a cut inside one is a boot that never finished). */
static void boot_until_done(const char *what)
{
    srr_report_t report;
    for (int boot = 0;; ++boot) {
        CHECK(boot < 8, "%s: repair never finished", what);
        srr_config_t config = repair_config();
        repair_boot(&report, &config);
        if (report.result != SRR_RESULT_STOPPED) break;
    }
    CHECK(report.result != SRR_RESULT_FAILED, "%s: %s at %s", what, srr_result_name(report.result),
          report.error_stage);
}

/* A reset during the check left the held pending queue hidden (the check had
 * begun deleting it). The next boot writes a copy of its header with the
 * flag set again (its pages are all there). The hidden pages stay until a
 * check runs, and none runs while the manifest is active, so a cut at any
 * write of that resume (each torn both ways) loses nothing at all: a later
 * boot finishes it and the file is whole, with no gap. */
static void run_resume_cuts(uint32_t kib)
{
    seed(9004);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    srp_manifest_t man;
    manifest_begin(&man, 0);
    manifest_add(&man, m, SRP_CLASS_ATTENDANCE, NULL, NULL);
    manifest_store(&man);
    store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
    SPIFFS_unmount(&fs);
    hide_header(m->name);
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t before = flash_ops;
    repair_boot(&report, &config);
    uint64_t writes = flash_ops - before;
    CHECK(report.files_restored == 1 && report.files_rebuilt == 0 && report.gaps == 0 &&
              report.result != SRR_RESULT_FAILED,
          "resume: %s, restored %u, rebuilt %u, gaps %u", srr_result_name(report.result), report.files_restored,
          report.files_rebuilt, report.gaps);
    mount_plain();
    check_custody(m, 4, false);
    CHECK(!exists("/pending.jsonl" SRP_RESTORE_SUFFIX), "resume: the rebuild was left under its temporary name");
    SPIFFS_unmount(&fs);
    uint32_t runs = 0;
    for (uint64_t k = 1; k <= writes; ++k) {
        for (int tear = 0; tear < 2; ++tear) {
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(9004 + k * 2 + (uint64_t)tear);
            volatile bool cut = false;
            cut_at = flash_ops + k;
            if (setjmp(cut_point)) {
                cut = true;
                drop_all_memory();
            } else {
                repair_boot(&report, &config);
            }
            cut_at = 0;
            if (!cut) continue;
            runs++;
            boot_until_done("resume after a cut");
            mount_plain();
            check_custody(m, 4, false);
            CHECK(!exists("/pending.jsonl" SRP_RESTORE_SUFFIX), "resume after a cut at write %" PRIu64
                  ": the rebuild was left under its temporary name", k);
            SPIFFS_unmount(&fs);
        }
    }
    free(image);
    free(saved);
    printf("resume: a hidden header restored, %u cuts at %" PRIu64 " writes passed\n", runs, writes);
}

/* The check deletes one held file and alters another (the hook stands in for
 * it): the first is rebuilt under a temporary name and renamed into place,
 * the second restored span by span, before anything else; also when the soft
 * deadline passes during the check, and with a cut at any write after it. */
static uint32_t hook_mode;
static bool check_alters(uint32_t pass)
{
    if (pass != 1 || !hook_mode) return false;
    bool changed = false;
    if (hook_mode & 1) changed |= SPIFFS_remove(&fs, "/pending.jsonl") == SPIFFS_OK;
    if (hook_mode & 2) {
        spiffs_file f = SPIFFS_open(&fs, "/blocked_identity.jsonl", SPIFFS_O_RDWR, 0);
        changed |= f >= 0 && SPIFFS_lseek(&fs, f, 3000, SPIFFS_SEEK_SET) >= 0 &&
            SPIFFS_write(&fs, f, (void *)"XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", 40) == 40;
        if (f >= 0) SPIFFS_close(&fs, f);
    }
    if (hook_mode & 4) deadline_at = port_now(NULL);
    if (hook_mode & 32) changed = true; /* every held file is compared and put back where it differs */
    if (hook_mode & 8) {
        /* And the store is full: the rebuild cannot finish. */
        static uint8_t filler[4000];
        memset(filler, 0x5A, sizeof(filler));
        spiffs_file f = SPIFFS_open(&fs, "/filler.bin", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
        while (f >= 0 && SPIFFS_write(&fs, f, filler, sizeof(filler)) == (s32_t)sizeof(filler)) {}
        if (f >= 0) SPIFFS_close(&fs, f);
    }
    SPIFFS_clearerr(&fs);
    return changed;
}

static void run_restores(uint32_t kib)
{
    check_hook = check_alters;
    for (uint32_t mode = 1; mode <= 7; ++mode) {
        seed(9100 + mode);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        SPIFFS_unmount(&fs);
        hook_mode = mode;
        srr_report_t report;
        srr_config_t config = repair_config();
        repair_boot(&report, &config);
        hook_mode = 0;
        deadline_at = 0;
        CHECK(report.result == ((mode & 4) ? SRR_RESULT_STOPPED : SRR_RESULT_REPAIRED),
              "restore mode %u: %s at %s", mode, srr_result_name(report.result), report.error_stage);
        CHECK(report.files_rebuilt == ((mode & 1) ? 1U : 0U) && report.files_restored == ((mode & 2) ? 1U : 0U) &&
                  report.gaps == 0,
              "restore mode %u: rebuilt %u restored %u gaps %u", mode, report.files_rebuilt, report.files_restored,
              report.gaps);
        mount_plain();
        for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
        CHECK(!exists("/pending.jsonl" SRP_RESTORE_SUFFIX), "restore: temporary file left");
        SPIFFS_unmount(&fs);
        srp_state_t state;
        CHECK(port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
                  !(state.flags & SRP_STATE_MANIFEST),
              "restore mode %u: manifest kept after every file went back", mode);
        boot_until_done("restore, next boot");
    }
    /* The check deleted a held file and the store is full: what does not go
     * back is a CHECK_CHANGED gap before the run fails, and the manifest stays
     * for the next boot, which settles it without reporting it twice. (A
     * store without a usable redo area: with one, 2.7.6 makes room in the
     * same boot, run_unheld_crammed.) */
    redo_enabled = false;
    seed(9150);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    hook_mode = 1 | 8;
    srr_report_t full;
    srr_config_t full_config = repair_config();
    repair_boot(&full, &full_config);
    hook_mode = 0;
    srp_gaps_t gaps = gaps_now();
    bool changed_gap = false;
    for (uint32_t g = 0; g < gaps.count; ++g)
        changed_gap |= gaps.gap[g].cause == SRP_GAP_CHECK_CHANGED && !strcmp(gaps.gap[g].name, "/pending.jsonl");
    srp_state_t state;
    CHECK(full.result == SRR_RESULT_FAILED && changed_gap &&
              port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
              (state.flags & SRP_STATE_MANIFEST) && state.phase == SRP_PHASE_RESTORE,
          "full restore: %s, gap %d", srr_result_name(full.result), changed_gap);
    check_named("full restore", model_of("/pending.jsonl"), 1);
    uint32_t recorded = gaps.count;
    repair_boot(&full, &full_config);
    CHECK(gaps_now().count == recorded && port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
              !(state.flags & SRP_STATE_MANIFEST),
          "full restore, next boot: %u gaps (was %u)", gaps_now().count, recorded);
    /* The held queue's restore fails for room, and the check also changed
     * the blocked queue, which the budget could not hold: its UNHELD_LOST
     * record must not hide the failed restore (the run fails, the manifest
     * stays), and the next boot settles both. */
    seed(9151);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    uint64_t all = 0;
    for (uint32_t i = 0; i < model_count; ++i) all += model[i].size;
    full_config = repair_config();
    full_config.hold_budget = all - model_of("/blocked_identity.jsonl")->size - 1;
    hook_mode = 1 | 2 | 8;
    repair_boot(&full, &full_config);
    hook_mode = 0;
    gaps = gaps_now();
    bool unheld = false, changed = false;
    for (uint32_t g = 0; g < gaps.count; ++g) {
        unheld |= gaps.gap[g].cause == SRP_GAP_UNHELD_LOST && !strcmp(gaps.gap[g].name, "/blocked_identity.jsonl");
        changed |= gaps.gap[g].cause == SRP_GAP_CHECK_CHANGED && !strcmp(gaps.gap[g].name, "/pending.jsonl");
    }
    CHECK(full.result == SRR_RESULT_FAILED && unheld && changed &&
              port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
              (state.flags & SRP_STATE_MANIFEST),
          "held and unheld restore: %s, unheld %d, changed %d", srr_result_name(full.result), unheld, changed);
    boot_until_done("held and unheld restore, next boot");
    CHECK(port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
              !(state.flags & SRP_STATE_MANIFEST), "held and unheld restore: manifest kept");
    redo_enabled = true;
    /* Cuts at sampled writes of the run whose check deleted and altered. */
    uint32_t runs = 0;
    for (uint32_t trial = 0; trial < 120; ++trial) {
        seed(9200 + trial);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        SPIFFS_unmount(&fs);
        uint8_t *image = malloc(flash_size);
        memcpy(image, flash, flash_size);
        __typeof__(nvs) *saved = malloc(sizeof(nvs));
        memcpy(saved, &nvs, sizeof(nvs));
        srr_report_t report;
        srr_config_t config = repair_config();
        hook_mode = 3;
        uint64_t before = flash_ops;
        repair_boot(&report, &config);
        uint64_t writes = flash_ops - before;
        memcpy(flash, image, flash_size);
        memcpy(&nvs, saved, sizeof(nvs));
        free(image);
        free(saved);
        volatile bool cut = false;
        cut_at = flash_ops + 1 + rnd() % writes;
        if (setjmp(cut_point)) {
            cut = true;
            drop_all_memory();
        } else {
            repair_boot(&report, &config);
        }
        cut_at = 0;
        hook_mode = 0;
        if (!cut) continue;
        runs++;
        boot_until_done("restore after a cut");
        for (uint32_t i = 0; i < model_count; ++i) check_named("restore after a cut", &model[i], 1);
    }
    check_hook = NULL;
    printf("restores: rebuilt and restored after the check, deadline and %u cuts passed\n", runs);
}

/* After an interrupted check, a file's owner may have replaced it: a
 * rolled-back image reclaimed and refilled the queue, or this image retired
 * it as settled. That is no gap. A hidden original whose name a new file
 * took goes after the new rows. A file that only changed is unverified. */
static void run_manifest_cases(uint32_t kib)
{
    for (uint32_t which = 0; which < 7; ++which) {
        seed(9300 + which);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        model_file_t *m = model_of(which == 4 ? "/processed_commands.txt" : "/pending.jsonl");
        set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
        srp_manifest_t man;
        manifest_begin(&man, which == 0 || which >= 4 ? SRP_MANIFEST_PENDING_IMAGE : 0);
        if (which >= 5) {
            /* Written by a newer unconfirmed image whose check a reset cut:
             * the bootloader rolled back to this image (5), which resumes it
             * before anything else writes, so the manifest is current; or to
             * another image (6), which ran meanwhile, so it is stale. */
            man.image_tag = 0x2760;
            man.rollback_tag = which == 5 ? repair_config().image_tag : 0x2740;
        }
        if (which == 4) manifest_add(&man, m, SRP_CLASS_OPERATIONAL, NULL, NULL);
        else manifest_add(&man, m, SRP_CLASS_ATTENDANCE, "legacy_queues", "ords_pending");
        manifest_store(&man);
        store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
        uint32_t original = m->size;
        uint8_t *kept = malloc(original);
        memcpy(kept, m->data, original);
        char row[600];
        if (which == 0 || which == 1) {
            /* The rolled-back image (0) or this one's level 2 (1) settled
             * and retired the queue; new punches follow. */
            set_cursor("legacy_queues", "ords_pending", 0, 0, 8);
            CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
            m->size = 0;
            while (m->size < 4000) {
                size_t n = json_row(row, sizeof(row));
                model_append(m, row, (uint32_t)n);
            }
        } else if (which == 2) {
            /* The check had begun deleting it; a rolled-back image then
             * created the name again (a new object) and appended rows. */
            SPIFFS_unmount(&fs);
            hide_header(m->name);
            mount_plain();
            m->size = 0;
            while (m->size < 3000) {
                size_t n = json_row(row, sizeof(row));
                model_append(m, row, (uint32_t)n);
            }
        } else if (which == 4) {
            /* A file with no queue cursor that the rolled-back image (the
             * manifest is an unconfirmed image's) rewrote meanwhile. */
            CHECK(SPIFFS_remove(&fs, m->name) == SPIFFS_OK, "remove");
            m->size = 0;
            model_append_ids(m, 30);
        } else {
            /* Same object, changed in place: nothing tells which bytes. */
            spiffs_file f = SPIFFS_open(&fs, m->name, SPIFFS_O_RDWR, 0);
            CHECK(f >= 0 && SPIFFS_lseek(&fs, f, 100, SPIFFS_SEEK_SET) >= 0 &&
                      SPIFFS_write(&fs, f, (void *)"ZZZZ", 4) == 4, "modify");
            SPIFFS_close(&fs, f);
            memcpy(m->data + 100, "ZZZZ", 4);
        }
        SPIFFS_unmount(&fs);
        srr_report_t report;
        srr_config_t config = repair_config();
        config.pending_verify = which == 0 || which == 4;
        repair_boot(&report, &config);
        srp_gaps_t gaps = gaps_now();
        bool unverified = false;
        if (which == 1) {
            /* 2.7.6 (review C3): the cursor's generation moved, which every
             * settled row does; that is no proof the queue was retired. The
             * new file reused the original's object id, so nothing says it
             * was replaced either: the original's range is reported. (A
             * level 2 retirement marks its entry settled first.) */
            unverified = report.gaps == 1 && gaps.count == 1 && gaps.gap[0].cause == SRP_GAP_CONTENT_UNVERIFIED &&
                gaps.gap[0].offset == 0 && gaps.gap[0].length == original;
            CHECK((report.files_superseded == 1 && report.gaps == 0) || (unverified && !report.files_superseded),
                  "manifest case 1: superseded %u gaps %u", report.files_superseded, report.gaps);
        } else if (which == 0 || which == 4 || which == 6) {
            CHECK(report.files_superseded == 1 && report.gaps == 0, "manifest case %u: superseded %u gaps %u", which,
                  report.files_superseded, report.gaps);
        } else if (which == 2) {
            CHECK(report.files_rebuilt == 1 && report.gaps == 0, "manifest case 2: rebuilt %u gaps %u",
                  report.files_rebuilt, report.gaps);
            /* The new rows, then the original's. */
            m->data = realloc(m->data, m->size + original + 2);
            memcpy(m->data + m->size, kept, original);
            m->size += original;
            free(m->unreadable);
            m->unreadable = calloc(m->size + 1, 1);
        } else {
            CHECK(report.gaps == 1 && gaps.count == 1 && gaps.gap[0].cause == SRP_GAP_CONTENT_UNVERIFIED &&
                      gaps.gap[0].offset == 0 && gaps.gap[0].length == original && !report.files_superseded,
                  "manifest case %u: gaps %u superseded %u", which, report.gaps, report.files_superseded);
        }
        if (which != 3 && which != 5 && !unverified) {
            mount_plain();
            check_custody(m, 4, false);
            SPIFFS_unmount(&fs);
        }
        srp_state_t state;
        CHECK(port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
                  !(state.flags & SRP_STATE_MANIFEST),
              "manifest case %u: manifest not retired", which);
        free(kept);
    }
    printf("manifest cases: superseded, merged, unverified and rolled back passed\n");
}

/* A crammed store whose damaged pending queue cannot be normalized until
 * room is made: level 2 retires the fully settled blocked queue (its cursor
 * cleared first, as lq_reclaim does). Neither a restore in that run nor a
 * resume after a reset may bring it back, and its settled rows never replay. */
static uint32_t erased_pages(void);
/* stale_full: the journal says an earlier run deferred for room, for a
 * shortage this store no longer has (review 4, boot finding 2): level 2
 * must still run. settled: the blocked queue's cursor says it is delivered. */
static srr_report_t retired_store(uint32_t kib, uint32_t trial, bool stale_full, bool settled,
                                  model_file_t **pending_out, model_file_t **blocked_out);
static void run_retired_stays_retired(uint32_t kib)
{
    uint32_t retired = 0;
    for (uint32_t trial = 0; trial < 6; ++trial) {
        model_file_t *pending = NULL, *blocked = NULL;
        srr_report_t report = retired_store(kib, trial, false, true, &pending, &blocked);
        CHECK(report.result != SRR_RESULT_FAILED, "retired: %s at %s", srr_result_name(report.result),
              report.error_stage);
        mount_plain();
        check_custody(pending, 1, false);
        bool gone = !exists(blocked->name);
        SPIFFS_unmount(&fs);
        lq_checkpoint_t cp;
        CHECK(port_nvs_load(NULL, "legacy_queues", "blocked", &cp, sizeof(cp)) == 1, "blocked cursor");
        if (report.settled_removed) {
            retired++;
            CHECK(gone && cp.generation == 13 && cp.offset == 0 && report.files_rebuilt == 0,
                  "retired: blocked brought back (exists %d, cursor generation %u offset %u, rebuilt %u)", !gone,
                  cp.generation, cp.offset, report.files_rebuilt);
        } else {
            CHECK(!gone && cp.generation == 12 && cp.offset == blocked->size, "kept: blocked changed");
        }
    }
    CHECK(retired, "no trial needed level 2");
    printf("retired files stay retired: %u of 6 trials retired the settled queue\n", retired);
}

static srp_state_t state_now(void)
{
    srp_state_t state;
    memset(&state, 0, sizeof(state));
    CHECK(port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1, "repair state");
    return state;
}

/* Review 4 (boot), finding 2: a FULL flag a past deferral left turned level 2
 * off for good, so a store one run with level 2 repairs deferred on every
 * boot, and a run with no work kept the flag. The flag describes the last
 * finished run only, and level 2 is skipped only when the shortage is the
 * one that run already had. */
static void run_full_flag(uint32_t kib)
{
    /* The deferral bookkeeping of 2.7.5: a store whose redo area is not
     * usable (with one, the store is reformatted instead: run_level2_useless,
     * the wedged builders). */
    redo_enabled = false;
    uint32_t repaired = 0;
    for (uint32_t trial = 0; trial < 6; ++trial) {
        model_file_t *pending = NULL, *blocked = NULL;
        srr_report_t control = retired_store(kib, trial, false, true, &pending, &blocked);
        srr_report_t report = retired_store(kib, trial, true, true, &pending, &blocked);
        CHECK(report.result == control.result && report.settled_removed == control.settled_removed &&
                  report.level == control.level,
              "full flag, trial %u: %s (level %u, %u retired) where a fresh journal gives %s (level %u, %u retired)",
              trial, srr_result_name(report.result), report.level, report.settled_removed,
              srr_result_name(control.result), control.level, control.settled_removed);
        mount_plain();
        check_custody(pending, 1, false);
        SPIFFS_unmount(&fs);
        srp_state_t state = state_now();
        CHECK(!(state.flags & SRP_STATE_FULL) || report.result == SRR_RESULT_DEFERRED,
              "full flag, trial %u: %s left FULL set", trial, srr_result_name(report.result));
        repaired += report.settled_removed != 0;
    }
    CHECK(repaired, "full flag: no trial needed level 2");
    /* A store level 2 cannot help (its queue is not settled) defers. Later
     * boots run level 2 again while a deferral still makes progress (the
     * shortage changes); once one is short of exactly what the last one was,
     * it skips level 2 and stays deferred. */
    model_file_t *pending = NULL, *blocked = NULL;
    srr_report_t report = retired_store(kib, 0, false, false, &pending, &blocked);
    CHECK(report.result == SRR_RESULT_DEFERRED, "full flag: the unsettled store %s", srr_result_name(report.result));
    srp_state_t state = state_now();
    CHECK((state.flags & SRP_STATE_FULL) && state.shortage && report.level == 2,
          "full flag: a deferral for room left flags %x shortage %x level %u", state.flags, state.shortage,
          report.level);
    uint32_t boots = 0;
    bool skipped = false;
    srr_config_t config = repair_config();
    while (report.result == SRR_RESULT_DEFERRED && !skipped && boots++ < 12) {
        repair_boot(&report, &config);
        state = state_now();
        skipped = report.level < 2;
        CHECK(!skipped || (report.result == SRR_RESULT_DEFERRED && (state.flags & SRP_STATE_FULL)),
              "full flag: level 2 skipped, then %s, flags %x", srr_result_name(report.result), state.flags);
    }
    CHECK(skipped || report.result != SRR_RESULT_DEFERRED, "full flag: %u boots deferred, each with level 2", boots);
    /* A run with no work clears the flag. */
    seed(9590);
    nvs_clear();
    format_flash(kib);
    model_reset();
    mount_plain();
    model_file_t *queue = make_json_file("/pending.jsonl", 20000);
    SPIFFS_unmount(&fs);
    store_state(SRP_PHASE_IDLE, 0, 0, SRP_STATE_FULL, 0);
    repair_boot(&report, &config);
    state = state_now();
    CHECK(report.result == SRR_RESULT_CLEAN && !(state.flags & SRP_STATE_FULL),
          "full flag: a clean run left %s, flags %x", srr_result_name(report.result), state.flags);
    mount_plain();
    check_custody(queue, 1, false);
    SPIFFS_unmount(&fs);
    redo_enabled = true;
    printf("full flag: a stale deferral does not turn level 2 off (%u of 6 retired), the same shortage skips it "
           "(after %u boots), a clean run clears it\n",
           repaired, boots);
}

/* Review 4 (boot), finding 3: the retired-file store damages thousands of
 * spans of one file (about 2,100 at 8 MiB), and the engine's patch lookups
 * were linear, called per byte: billions of steps, seconds of host CPU (and
 * minutes on the device, outside every pacing and deadline check). Its CPU
 * steps are capped in every run; at 8 MiB and more (run by hand: `review-cpu
 * --kib=8192`) its CPU time is bounded too. */
static void run_cpu_bound(uint32_t kib)
{
    double before = cpu_ms_max;
    cpu_ms_max = 0;
    uint32_t steps = 0;
    for (uint32_t trial = 0; trial < 2; ++trial) {
        model_file_t *pending = NULL, *blocked = NULL;
        srr_report_t report = retired_store(kib, trial, false, true, &pending, &blocked);
        CHECK(report.result != SRR_RESULT_FAILED, "cpu: %s", srr_result_name(report.result));
        if (report.cpu_steps > steps) steps = report.cpu_steps;
    }
    double limit = 250.0 * kib / 1024;
    if (kib >= 4096) CHECK(cpu_ms_max < limit, "cpu: a repair took %.0f ms of CPU (limit %.0f)", cpu_ms_max, limit);
    printf("cpu: the most damaged spans of one file: %u CPU steps, %.0f ms of CPU per repair\n", steps, cpu_ms_max);
    if (before > cpu_ms_max) cpu_ms_max = before;
}

static srr_report_t retired_store(uint32_t kib, uint32_t trial, bool stale_full, bool settled,
                                  model_file_t **pending_out, model_file_t **blocked_out)
{
    {
        seed(9500 + trial);
        nvs_clear();
        format_flash(kib);
        model_reset();
        row_seq = 1000;
        mount_plain();
        (void)make_id_file("/acked_uids.txt", 200);
        model_file_t *blocked = make_json_file("/blocked_identity.jsonl", flash_size / 100 * 42);
        if (settled) set_cursor("legacy_queues", "blocked", blocked->size, prefix_crc(blocked, blocked->size), 12);
        else set_cursor("legacy_queues", "blocked", 0, 0, 12);
        /* The pending queue fills the rest (any store size) until SPIFFS keeps
         * three to five free blocks, then more of its spans are damaged than
         * the erased and deleted pages could give room for: only retiring the
         * settled blocked queue makes it. */
        model_file_t *pending = new_model("/pending.jsonl", true, false);
        char chunk[4600];
        while (fs.free_blocks > 3 + trial % 3) {
            size_t used = 0;
            while (used < 4000) used += json_row(chunk + used, sizeof(chunk) - used);
            model_append(pending, chunk, (uint32_t)used);
        }
        SPIFFS_unmount(&fs);
        uint32_t deleted = 0;
        for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix) deleted += pix % 16 && rd16(lookup_address(pix)) == 0;
        /* Every other span: no block of them becomes all deleted, which
         * SPIFFS's quick GC would erase to make room. */
        uint32_t spans = pending->size / SPAN, damage = erased_pages() + deleted + 60;
        CHECK(2 * damage < spans && damage < blocked->size / SPAN / 2, "retired: pending holds %u spans, %u damaged",
              spans, damage);
        for (uint32_t k = 0; k < damage; ++k) flag_deleted(rd16(entry_address(pending->name, 1 + 2 * k)));
        if (stale_full) {
            srp_state_t state;
            memset(&state, 0, sizeof(state));
            state.generation = 7;
            state.flags = SRP_STATE_FULL;
            state.shortage = 0x5EED;
            srp_state_seal(&state);
            port_nvs_store(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state));
        }
        srr_report_t report;
        srr_config_t config = repair_config();
        /* The check is taken to have changed something: the restore after it
         * compares every held file, the retired queue's hold included. */
        check_hook = check_alters;
        hook_mode = 32;
        repair_boot(&report, &config);
        hook_mode = 0;
        check_hook = NULL;
        *pending_out = pending;
        *blocked_out = blocked;
        return report;
    }
}

/* R3: a custody file that cannot be made consistent (here: its unreadable
 * span cannot be recorded, the journal holding 24 unacknowledged records of
 * earlier runs) is left as it is, and no check runs while it is so. Once ADD
 * has acknowledged the records, the next run repairs it. */
static void run_no_check_while_inconsistent(uint32_t kib)
{
    seed(9600);
    nvs_clear();
    format_flash(kib);
    damage_erased_page();
    srp_gaps_t full;
    memset(&full, 0, sizeof(full));
    for (uint32_t i = 0; i < SRP_MAX_GAPS; ++i) {
        srp_gap_t *g = &full.gap[full.count++];
        snprintf(g->name, sizeof(g->name), "/old-%u", i);
        g->cause = SRP_GAP_SALVAGE;
        g->offset = i;
        g->length = 1;
        g->generation = 1;
    }
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "gaps", &full, sizeof(full));
    uint8_t *before = malloc(flash_size);
    memcpy(before, flash, flash_size);
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_DEFERRED && report.check_passes == 0 && report.records_dropped >= 1,
          "inconsistent custody: %s, %u check passes", srr_result_name(report.result), report.check_passes);
    /* The damaged queue's own pages are untouched for the next run. */
    mount_plain();
    CHECK(read_file("/pending.jsonl", before, flash_size) < 0, "the unrecorded gap was filled");
    SPIFFS_unmount(&fs);
    for (uint32_t i = 0; i < SRP_MAX_GAPS; ++i) full.gap[i].flags |= SRP_RECORD_ACKED;
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "gaps", &full, sizeof(full));
    base_generation = 1;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPAIRED_WITH_GAPS && report.check_passes >= 1, "after acknowledgement: %s",
          srr_result_name(report.result));
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    free(before);
    printf("no check while custody is inconsistent passed\n");
}

/* The guards that keep a run from starting: NVS too full for the journal's
 * peak, a journal that cannot be saved, and too little time for a check. */
static void run_guards(uint32_t kib)
{
    seed(9400);
    nvs_clear();
    format_flash(kib);
    damage_erased_page();
    uint8_t *before = malloc(flash_size);
    memcpy(before, flash, flash_size);
    srr_report_t report;
    srr_config_t config = repair_config();
    config.nvs_known = true;
    config.nvs_available_entries = srp_nvs_entries_needed(false, false, false, false) - 1;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_REPORT_ONLY && !strcmp(report.error_stage, "nvs_full") &&
              !memcmp(before, flash, flash_size),
          "nvs guard: %s", srr_result_name(report.result));
    nvs_refuse_state = true;
    config = repair_config();
    repair_boot(&report, &config);
    nvs_refuse_state = false;
    CHECK(report.result == SRR_RESULT_FAILED && !strcmp(report.error_stage, "journal") &&
              !memcmp(before, flash, flash_size) && gaps_now().count == 0,
          "journal guard: %s at %s, %u gaps", srr_result_name(report.result), report.error_stage, gaps_now().count);
    /* A check pass and its restore would not fit before the soft deadline:
     * normalization runs, the check does not, and no manifest is written. */
    config = repair_config();
    config.check_ms_per_mib = 4000000;
    deadline_at = port_now(NULL) + 2000000;
    repair_boot(&report, &config);
    deadline_at = 0;
    srp_state_t state;
    CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "time") && report.check_passes == 0 &&
              !report.manifest_written && report.spans_rewritten >= 1,
          "time guard: %s at %s passes %u", srr_result_name(report.result), report.error_stage, report.check_passes);
    CHECK(port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
              (state.flags & SRP_STATE_MANIFEST) == 0, "time guard: manifest");
    free(before);
    printf("guards: NVS headroom, journal and check time passed\n");
}

/* ---- third review round: custody regressions --------------------------- */
/* SPIFFS_check's DELETE_BAD_FILE is spiffs_delete_obj_lazy: it clears the
 * header's IXDELE flag and frees nothing; its later passes (or the next
 * check, or SPIFFS's GC) free the pages. These runs stop it right there: the
 * hook hides a file's header after pass 1, sometimes followed by a power cut
 * (nothing after the check's last write reaches flash). */
static jmp_buf review_cut;
static uint32_t review_mode;      /* 1: hide the victim; 2: then a power cut */
static const char *review_victim = "/pending.jsonl";
static uint64_t review_hook_ops;
static bool review_hook(uint32_t pass)
{
    if (pass != 1) return false;
    review_hook_ops = flash_ops;
    if (!(review_mode & 1)) return false;
    uint32_t header = header_of(review_victim);
    flash[header * PAGE + 4] &= (uint8_t)~SPIFFS_PH_FLAG_IXDELE;
#if SPIFFS_CACHE
    spiffs_cache_drop_page(&fs, (spiffs_page_ix)header);
#endif
    if (review_mode & 2) longjmp(review_cut, 1);
    return true;
}

/* One boot whose check hides `victim` after pass 1, then a power cut. */
static void review_boot_cut_after_hiding(const char *victim)
{
    review_victim = victim;
    review_mode = 1 | 2;
    check_hook = review_hook;
    srr_report_t report;
    srr_config_t config = repair_config();
    if (!setjmp(review_cut)) {
        repair_boot(&report, &config);
        CHECK(0, "the check hook never ran");
    }
    drop_all_memory();
    check_hook = NULL;
    review_mode = 0;
}

/* The file is whole at its offsets and no gap record of these runs names it. */
static void check_whole(const char *what, const model_file_t *m, uint32_t generation_from)
{
    static uint8_t buffer[8 * 1024 * 1024];
    mount_plain();
    int64_t got = exists(m->name) ? read_file(m->name, buffer, sizeof(buffer)) : -2;
    srp_gaps_t gaps = gaps_now();
    SPIFFS_unmount(&fs);
    CHECK(got == (int64_t)m->size && !memcmp(buffer, m->data, m->size), "%s: %s reads %" PRId64 " of %u bytes", what,
          m->name, got, m->size);
    for (uint32_t g = 0; g < gaps.count; ++g)
        CHECK(strcmp(gaps.gap[g].name, m->name) || gaps.gap[g].generation < generation_from,
              "%s: %s has a gap record (offset %u length %u cause %s)", what, m->name, gaps.gap[g].offset,
              gaps.gap[g].length, srp_gap_cause_name((srp_gap_cause_t)gaps.gap[g].cause));
}

static bool manifest_kept(void)
{
    srp_state_t state;
    return port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
        (state.flags & SRP_STATE_MANIFEST);
}

static void fill_gap_list(uint32_t unacked)
{
    srp_gaps_t list;
    memset(&list, 0, sizeof(list));
    for (uint32_t i = 0; i < unacked && i < SRP_MAX_GAPS; ++i) {
        srp_gap_t *g = &list.gap[list.count++];
        snprintf(g->name, sizeof(g->name), "/old-%u", i);
        g->cause = SRP_GAP_SALVAGE;
        g->offset = i;
        g->length = 1;
        g->generation = 1;
    }
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "gaps", &list, sizeof(list));
}

static void ack_gap_list(void)
{
    srp_gaps_t list = gaps_now();
    for (uint32_t i = 0; i < list.count; ++i) list.gap[i].flags |= SRP_RECORD_ACKED;
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "gaps", &list, sizeof(list));
}

/* Review 3, finding 1: the manifest named the first 12 held custody files in
 * flash order and never acked_uids.txt or a file of unknown name; a file left
 * out had no resume, and a reset right after the check hid it lost it with no
 * record. Every custody file is in the manifest now, in class priority; more
 * than it holds, or than the gap list could take one record each for, and no
 * check runs. */
static void run_manifest_coverage(uint32_t kib)
{
    static const char *const names[] = {"/add_live.jsonl", "/add_pending.jsonl", "/blocked_identity.jsonl",
                                         "/corrupt_ords.jsonl", "/add_corrupt.jsonl", "/add_corrupt.bak",
                                         "/pending.bak", "/add_live.bak", "/add_pending.bak", "/blocked_recovery.bak",
                                         "/add_cancelled.txt", "/processed_commands.txt", "/add_live.pos",
                                         "/add_pending.pos", "/notes.bin"};
    static const struct { uint32_t before; const char *victim; } cases[] = {
        {12, "/pending.jsonl"}, {15, "/pending.jsonl"}, {2, "/acked_uids.txt"}, {6, "/notes-2.bin"}};
    for (uint32_t c = 0; c < sizeof(cases) / sizeof(cases[0]); ++c) {
        seed(31001 + c);
        nvs_clear();
        format_flash(kib);
        model_reset();
        row_seq = 7000;
        mount_plain();
        for (uint32_t i = 0; i < cases[c].before; ++i) {
            if (strstr(names[i], ".txt")) make_id_file(names[i], 20);
            else if (strstr(names[i], ".pos") || strstr(names[i], ".bin")) make_binary_file(names[i], 11 + 300 * (i % 2));
            else make_json_file(names[i], 1500);
        }
        const char *victim = cases[c].victim;
        model_file_t *m = strstr(victim, ".txt") ? make_id_file(victim, 400)
                        : strstr(victim, ".bin")  ? make_binary_file(victim, 20000)
                                                  : make_json_file(victim, 30000);
        SPIFFS_unmount(&fs);
        review_boot_cut_after_hiding(victim);
        CHECK(manifest_kept(), "manifest coverage %u: no manifest after the cut", c);
        boot_until_done("manifest coverage");
        check_whole("manifest coverage", m, 1);
        CHECK(!manifest_kept(), "manifest coverage %u: manifest kept", c);
    }
    /* More custody files than the manifest holds (files of unknown name are
     * custody too): no check this boot, nothing written for it. */
    seed(31010);
    nvs_clear();
    format_flash(kib);
    build_store(20000);
    for (uint32_t i = 0; i < SRP_MANIFEST_FILES; ++i) {
        char name[32];
        snprintf(name, sizeof(name), "/extra-%02u.bin", i);
        uint8_t data[40];
        memset(data, (int)i, sizeof(data));
        write_file(name, data, sizeof(data));
    }
    SPIFFS_unmount(&fs);
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "manifest") && report.check_passes == 0 &&
              !report.manifest_written && !manifest_kept(),
          "manifest overflow: %s at %s, %u passes", srr_result_name(report.result), report.error_stage,
          report.check_passes);
    /* Room in the gap list for one record per manifest entry, or no check. */
    seed(31011);
    nvs_clear();
    format_flash(kib);
    build_store(20000);
    SPIFFS_unmount(&fs);
    fill_gap_list(SRP_MAX_GAPS - 3);
    store_state(SRP_PHASE_IDLE, 0, 0, 0, 0);
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "records") && report.check_passes == 0 &&
              !manifest_kept(),
          "records room: %s at %s, %u passes", srr_result_name(report.result), report.error_stage, report.check_passes);
    ack_gap_list();
    repair_boot(&report, &config);
    CHECK(report.check_passes >= 1 && report.result != SRR_RESULT_DEFERRED && report.result != SRR_RESULT_FAILED,
          "records room after acknowledgement: %s at %s", srr_result_name(report.result), report.error_stage);
    printf("manifest coverage: every custody file named, overflow and record room passed\n");
}

/* Review 3, finding 3: with 24 unacknowledged records the restore's or the
 * resume's record was dropped, yet the entry was marked reported or settled,
 * and the bytes were gone with nothing naming them. Now no manifest is
 * written (no check runs) unless the list has room for one record per entry,
 * and an entry whose record does not fit stays in the manifest, unsettled
 * (no check meanwhile), until it does. */
static void run_records_full(uint32_t kib)
{
    /* The check would remove a held queue on a full store: with the list
     * full, it never runs. */
    seed(31101);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    SPIFFS_unmount(&fs);
    fill_gap_list(SRP_MAX_GAPS);
    store_state(SRP_PHASE_IDLE, 0, 0, 0, 0);
    check_hook = check_alters;
    hook_mode = 1 | 8;
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    hook_mode = 0;
    check_hook = NULL;
    CHECK(report.check_passes == 0 && report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "records"),
          "records full, restore: %s at %s, %u passes", srr_result_name(report.result), report.error_stage,
          report.check_passes);
    check_whole("records full, restore", m, 1);
    /* A file changed in place after an interrupted check: its
     * CONTENT_UNVERIFIED record does not fit. */
    seed(31102);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    m = model_of("/pending.jsonl");
    set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
    srp_manifest_t man;
    manifest_begin(&man, 0);
    manifest_add(&man, m, SRP_CLASS_ATTENDANCE, "legacy_queues", "ords_pending");
    manifest_store(&man);
    spiffs_file f = SPIFFS_open(&fs, m->name, SPIFFS_O_RDWR, 0);
    CHECK(f >= 0 && SPIFFS_lseek(&fs, f, 100, SPIFFS_SEEK_SET) >= 0 && SPIFFS_write(&fs, f, (void *)"ZZZZ", 4) == 4,
          "modify");
    SPIFFS_close(&fs, f);
    for (uint32_t i = 100; i < 104; ++i) m->unreadable[i] = 1;
    SPIFFS_unmount(&fs);
    fill_gap_list(SRP_MAX_GAPS);
    store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
    for (int boot = 0; boot < 2; ++boot) {
        repair_boot(&report, &config);
        CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "records") &&
                  report.check_passes == 0 && manifest_kept(),
              "records full, resume boot %d: %s at %s", boot, srr_result_name(report.result), report.error_stage);
    }
    ack_gap_list();
    repair_boot(&report, &config);
    srp_gaps_t gaps = gaps_now();
    bool unverified = false;
    for (uint32_t g = 0; g < gaps.count; ++g)
        unverified |= !strcmp(gaps.gap[g].name, m->name) && gaps.gap[g].cause == SRP_GAP_CONTENT_UNVERIFIED &&
            gaps.gap[g].offset == 0 && gaps.gap[g].length == m->size;
    CHECK(unverified && !manifest_kept(), "records full, resume after acknowledgement: %s, recorded %d",
          srr_result_name(report.result), unverified);
    printf("records full: no check without room, entries wait unsettled until their record fits\n");
}

/* Review 3, finding 2: a hidden original on a crammed store. The resume
 * rebuilt it with SPIFFS writes whose GC wiped the hidden pages, renamed a
 * partial rebuild into place before recording its tail, and the next boot
 * took that rebuild for the owner's new file: 52 KB lost with no record. Its
 * header now comes back instead (one page), with a cut at sampled writes of
 * that resume, torn both ways; the file must come back whole, no gap. */
static void build_interleaved(uint32_t fill, model_file_t **out)
{
    model_reset();
    row_seq = 100;
    mount_plain();
    make_id_file("/acked_uids.txt", 50);
    model_file_t *m = new_model("/pending.jsonl", true, false);
    char row[600];
    static uint8_t ballast[2000];
    memset(ballast, 0x42, sizeof(ballast));
    while (m->size < 60000) {
        size_t n = json_row(row, sizeof(row));
        model_append(m, row, (uint32_t)n);
        CHECK(append_bytes("/ballast.bin", ballast, sizeof(ballast)), "ballast");
    }
    u32_t total = 0, used = 0;
    SPIFFS_info(&fs, &total, &used);
    while (used < total / 100 * fill && append_bytes("/ballast.bin", ballast, sizeof(ballast)))
        SPIFFS_info(&fs, &total, &used);
    *out = m;
}

static void run_crammed_resume(uint32_t kib, uint32_t fill, uint32_t stride)
{
    seed(9004);
    nvs_clear();
    format_flash(kib);
    model_file_t *m;
    build_interleaved(fill, &m);
    srp_manifest_t man;
    manifest_begin(&man, 0);
    manifest_add(&man, m, SRP_CLASS_ATTENDANCE, NULL, NULL);
    manifest_store(&man);
    store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
    SPIFFS_unmount(&fs);
    hide_header(m->name);
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t before = flash_ops;
    repair_boot(&report, &config);
    uint64_t writes = flash_ops - before;
    CHECK(report.gaps == 0 && report.result != SRR_RESULT_FAILED, "crammed resume %u%%: %s, %u gaps", fill,
          srr_result_name(report.result), report.gaps);
    check_whole("crammed resume", m, 4);
    uint32_t runs = 0;
    for (uint64_t k = 1; k <= writes; k += stride) {
        for (int tear = 0; tear < 2; ++tear) {
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(9004 + k * 2 + (uint64_t)tear);
            volatile bool cut = false;
            cut_at = flash_ops + k;
            if (setjmp(cut_point)) {
                cut = true;
                drop_all_memory();
            } else {
                repair_boot(&report, &config);
            }
            cut_at = 0;
            if (!cut) continue;
            runs++;
            boot_until_done("crammed resume after a cut");
            check_whole("crammed resume after a cut", m, 4);
        }
    }
    free(image);
    free(saved);
    printf("crammed resume at %u%%: header restored, %u cuts at %" PRIu64 " writes passed\n", fill, runs, writes);
}

/* The same in the run: the check hides a held queue on a crammed store (85%:
 * its rebuild could not fit), and a cut at sampled writes after the check. */
static void run_crammed_restore(uint32_t kib, uint32_t fill, uint32_t stride)
{
    seed(9005);
    nvs_clear();
    format_flash(kib);
    model_file_t *m;
    build_interleaved(fill, &m);
    SPIFFS_unmount(&fs);
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    review_victim = m->name;
    review_mode = 1;
    check_hook = review_hook;
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t end = flash_ops, at_hook = review_hook_ops;
    CHECK(report.result != SRR_RESULT_FAILED && report.gaps == 0, "crammed restore %u%%: %s at %s, %u gaps", fill,
          srr_result_name(report.result), report.error_stage, report.gaps);
    check_whole("crammed restore", m, 1);
    uint32_t runs = 0;
    for (uint64_t k = at_hook - start + 1; k <= end - start; k += stride) {
        for (int tear = 0; tear < 2; ++tear) {
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(7000 + k * 2 + (uint64_t)tear);
            volatile bool cut = false;
            cut_at = flash_ops + k;
            if (setjmp(cut_point)) {
                cut = true;
                drop_all_memory();
            } else {
                repair_boot(&report, &config);
            }
            cut_at = 0;
            if (!cut) continue;
            runs++;
            check_hook = NULL;
            boot_until_done("crammed restore after a cut");
            check_hook = review_hook;
            check_whole("crammed restore after a cut", m, 1);
        }
    }
    check_hook = NULL;
    review_mode = 0;
    free(image);
    free(saved);
    printf("crammed restore at %u%%: header restored, %u cuts after the check passed\n", fill, runs);
}

/* A held queue too large to rebuild beside itself (430 KB in a store 79%
 * used): the check hides it, the run must bring it back whole. */
static void run_hidden_large(uint32_t kib)
{
    seed(31201);
    nvs_clear();
    format_flash(kib);
    model_reset();
    row_seq = 3000;
    mount_plain();
    make_id_file("/acked_uids.txt", 100);
    u32_t total = 0, used = 0;
    SPIFFS_info(&fs, &total, &used);
    model_file_t *m = make_json_file("/pending.jsonl", total * 45 / 100);
    static uint8_t ballast[4000];
    memset(ballast, 0x42, sizeof(ballast));
    spiffs_file f = SPIFFS_open(&fs, "/ballast.bin", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    SPIFFS_info(&fs, &total, &used);
    while (f >= 0 && used + 8 * 4096 < total * 82 / 100 && SPIFFS_write(&fs, f, ballast, sizeof(ballast)) == 4000)
        SPIFFS_info(&fs, &total, &used);
    if (f >= 0) SPIFFS_close(&fs, f);
    SPIFFS_clearerr(&fs);
    SPIFFS_unmount(&fs);
    review_victim = m->name;
    review_mode = 1;
    check_hook = review_hook;
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    check_hook = NULL;
    review_mode = 0;
    CHECK(report.result != SRR_RESULT_FAILED && report.gaps == 0, "large hidden original: %s at %s, %u gaps",
          srr_result_name(report.result), report.error_stage, report.gaps);
    check_whole("large hidden original", m, 1);
    boot_until_done("large hidden original, next boot");
    check_whole("large hidden original, next boot", m, 1);
    printf("large hidden original: header restored, no rebuild needed\n");
}

/* The check removed a held queue outright (no hidden original) on a store
 * too full to rebuild it whole: the rebuild's missing tail is recorded
 * before the partial file takes the name, and a cut at any write after the
 * check (torn both ways) never lets the next boot take the repair's own
 * rebuild for the owner's new file. */
/* One boot with a power cut at its k-th flash write; true when it came. */
static bool boot_with_cut(uint64_t k)
{
    srr_report_t report;
    srr_config_t config = repair_config();
    cut_at = flash_ops + k;
    if (setjmp(cut_point)) {
        cut_at = 0;
        engine_running = false;
        drop_all_memory();
        return true;
    }
    repair_boot(&report, &config);
    cut_at = 0;
    return false;
}

static bool rebuild_hook(uint32_t pass)
{
    bool changed = check_alters(pass);
    if (pass == 1) review_hook_ops = flash_ops;
    return changed;
}

static void run_rebuild_cuts(uint32_t kib, uint32_t stride)
{
    seed(9150);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    check_hook = rebuild_hook;
    hook_mode = 1 | 8;
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t end = flash_ops, at_hook = review_hook_ops;
    uint32_t runs = 0;
    uint64_t only_k = getenv("REVIEW_K") ? strtoull(getenv("REVIEW_K"), NULL, 10) : 0;
    for (uint64_t k = at_hook - start + 1; k <= end - start; k += stride) {
        for (int tear = 0; tear < 2; ++tear) {
            if (only_k && k != only_k) continue;
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(9150 + k * 2 + (uint64_t)tear);
            print_log = only_k != 0;
            check_hook = rebuild_hook;
            hook_mode = 1 | 8;
            bool cut = boot_with_cut(k);
            hook_mode = 0;
            check_hook = NULL;
            if (!cut) continue;
            runs++;
            if (getenv("REVIEW_TRACE")) fprintf(stderr, "  partial rebuild: cut at write %" PRIu64 " (tear %d)\n", k, tear);
            boot_until_done("partial rebuild after a cut");
            for (uint32_t i = 0; i < model_count; ++i) check_named("partial rebuild after a cut", &model[i], 1);
            CHECK(!manifest_kept(), "partial rebuild after a cut: manifest kept");
        }
    }
    hook_mode = 0;
    check_hook = NULL;
    free(image);
    free(saved);
    printf("partial rebuild: %u cuts after the check passed\n", runs);
}

/* ---- the command inbox merge (command_inbox_merge.c on the shim) ------ */
/* A line "decrypts" here when it is all hex; its command_id is its first 16
 * characters. */
static bool hex_command_id(void *context, const char *line, char *id, size_t id_bytes)
{
    (void)context;
    size_t length = strlen(line);
    if (length < 17) return false;
    for (size_t k = 0; k < length; ++k)
        if (!strchr("0123456789abcdef", line[k])) return false;
    snprintf(id, id_bytes, "%.16s", line);
    return true;
}
static int inbox_tx_load(void *context, ft_checkpoint_t *cp)
{
    (void)context;
    return port_nvs_load(NULL, "file_tx", "commands", cp, sizeof(*cp));
}
static bool inbox_tx_commit(void *context, const ft_checkpoint_t *cp)
{
    (void)context;
    return port_nvs_store(NULL, "file_tx", "commands", cp, sizeof(*cp));
}
#define INBOX "/add_commands.jsonl"
#define INBOX_BAK "/add_commands.bak"
static bool inbox_merge(cim_result_t *result)
{
    static char merged[64 * 1024], ids[512][CIM_ID_BYTES], line[12288];
    memset(ids, 0, sizeof(ids));
    cim_config_t config = {
        .active = INBOX, .backup = INBOX_BAK, .stage = "/add_commands.tmp", .merge = "/add_commands.mrg",
        .merged = merged, .capacity = sizeof(merged), .ids = ids, .max_ids = 512, .line = line,
        .line_bytes = sizeof(line), .command_id = hex_command_id,
        .port = {inbox_tx_load, inbox_tx_commit, NULL}, .limit = sizeof(merged),
    };
    return cim_merge(&config, result);
}
static bool holds(const uint8_t *buffer, size_t size, const uint8_t *needle, size_t length)
{
    for (size_t i = 0; i + length <= size; ++i)
        if (!memcmp(buffer + i, needle, length)) return true;
    return false;
}
static void hex_line(char *out, uint32_t length)
{
    for (uint32_t k = 0; k + 1 < length; ++k) out[k] = "0123456789abcdef"[rnd() % 16];
    out[length - 1] = '\n';
}

/* Review 3, finding 5: an unreadable span of the inbox ending exactly where a
 * line starts is filled with NUL, newline included; the merge's splitter read
 * the damaged line and the intact one after it as one line and dropped both.
 * A NUL now ends a line. Every line with no unreadable byte is kept. */
static void run_inbox_boundary(uint32_t kib)
{
    for (uint32_t trial = 0; trial < 4; ++trial) {
        seed(31501 + trial);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        model_file_t *m = model_of(INBOX);
        /* The fifth line starts where span 2 starts; span 1 is erased. */
        static const uint32_t lengths[] = {125, 125, 125, 127, 160, 140, 150, 130};
        uint32_t shift = trial % 2 ? 1 : 0; /* and once a byte later: the next line is cut */
        char data[2000];
        uint32_t size = 0, starts[8];
        for (unsigned i = 0; i < 8; ++i) {
            starts[i] = size;
            uint32_t length = lengths[i] + (i == 3 ? shift : 0);
            hex_line(data + size, length);
            size += length;
        }
        free(m->data);
        free(m->unreadable);
        m->data = malloc(size + 1);
        memcpy(m->data, data, size);
        m->size = size;
        m->unreadable = calloc(size + 1, 1);
        write_file(m->name, m->data, m->size);
        uint32_t span = 1 + trial / 2;
        erase_page(rd16(entry_address(m->name, span)));
        mark_unreadable(m, span);
        SPIFFS_unmount(&fs);
        srr_report_t report;
        srr_config_t config = repair_config();
        repair_boot(&report, &config);
        CHECK(report.commands_damaged, "inbox boundary: the merge was not flagged");
        mount_plain();
        cim_result_t result;
        CHECK(inbox_merge(&result), "inbox boundary: merge failed");
        static uint8_t buffer[8192];
        int64_t got = read_file(INBOX, buffer, sizeof(buffer));
        SPIFFS_unmount(&fs);
        for (unsigned i = 0; i < 8; ++i) {
            uint32_t length = lengths[i] + (i == 3 ? shift : 0);
            bool damaged = false;
            for (uint32_t k = starts[i]; k < starts[i] + length; ++k) damaged |= m->unreadable[k] != 0;
            if (damaged) continue;
            CHECK(got > 0 && holds(buffer, (size_t)got, m->data + starts[i], length),
                  "inbox boundary (trial %u): intact line %u at %u (%u bytes) dropped by the merge", trial, i,
                  starts[i], length);
        }
    }
    printf("command inbox: every intact line kept across a filled span\n");
}

/* Review 3, finding 4: the merge removed the backup and the stage before the
 * merged lines were on flash; a cut there lost every line only the backup
 * held. A cut at every write of the merge, torn both ways, then the next
 * boot's repair and merge: all eight commands are in the inbox. */
static bool inbox_merge_cut(uint64_t k)
{
    cut_at = flash_ops + k;
    if (setjmp(cut_point)) {
        /* The reset closes every file. */
        cut_at = 0;
        memset(shim_files, 0, sizeof(shim_files));
        return true;
    }
    cim_result_t result;
    (void)inbox_merge(&result);
    cut_at = 0;
    return false;
}

static void run_inbox_window(uint32_t kib)
{
    seed(31601);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    char lines[8][200];
    for (unsigned i = 0; i < 8; ++i) hex_line(lines[i], 120 + i * 7);
    uint8_t active[2000], backup[2000];
    uint32_t active_size = 0, backup_size = 0;
    for (unsigned i = 0; i < 8; ++i) {
        if (i < 6) memcpy(active + active_size, lines[i], 120 + i * 7);
        if (i < 6) active_size += 120 + i * 7;
        memcpy(backup + backup_size, lines[i], 120 + i * 7);
        backup_size += 120 + i * 7;
    }
    write_file(INBOX, active, active_size);
    write_file(INBOX_BAK, backup, backup_size);
    SPIFFS_unmount(&fs);
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    mount_plain();
    uint64_t start = flash_ops;
    cim_result_t result;
    CHECK(inbox_merge(&result), "inbox window: uncut merge");
    uint64_t writes = flash_ops - start;
    SPIFFS_unmount(&fs);
    uint32_t runs = 0;
    for (uint64_t k = 1; k <= writes; ++k) {
        for (int tear = 0; tear < 2; ++tear) {
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(31600 + k * 2 + (uint64_t)tear);
            mount_plain();
            bool cut = inbox_merge_cut(k);
            if (!cut) {
                SPIFFS_unmount(&fs);
                continue;
            }
            drop_all_memory();
            runs++;
            /* The next boot: the repair (a torn page is its business), then
             * the merge again (its flag or its merge file is still there). */
            srr_report_t report;
            srr_config_t config = repair_config();
            repair_boot(&report, &config);
            mount_plain();
            CHECK(inbox_merge(&result), "inbox window: merge after a cut at write %" PRIu64 " failed", k);
            static uint8_t buffer[65536];
            int64_t got = read_file(INBOX, buffer, sizeof(buffer));
            CHECK(!exists("/add_commands.mrg"), "inbox window: merge file left");
            SPIFFS_unmount(&fs);
            for (unsigned i = 0; i < 8; ++i)
                CHECK(got > 0 && holds(buffer, (size_t)got, (const uint8_t *)lines[i], 120 + i * 7),
                      "inbox window: a cut at merge write %" PRIu64 " of %" PRIu64 " (tear %d) lost command %u", k,
                      writes, tear, i);
        }
    }
    free(image);
    free(saved);
    printf("command inbox: %u cuts in the merge lost no command\n", runs);
}

/* Review 3 (boot), finding 3: a never-damaged store that has run a while
 * (deleted pages from every append's header move, two free blocks) was
 * compacted back to three at every boot and reported REPAIRED, with a probe
 * file written and the repair LED lit through the check. Compaction is
 * upkeep: the result is CLEAN and no phase lights the LED. */
static void run_healthy_compaction(uint32_t kib)
{
    seed(31701);
    nvs_clear();
    format_flash(kib);
    model_reset();
    row_seq = 500;
    mount_plain();
    model_file_t *a = new_model("/pending.jsonl", true, false), *b = new_model("/blocked_identity.jsonl", true, false);
    char row[600];
    u32_t total = 0, used = 0;
    do {
        size_t n = json_row(row, sizeof(row));
        model_append(a, row, (uint32_t)n);
        n = json_row(row, sizeof(row));
        model_append(b, row, (uint32_t)n);
        SPIFFS_info(&fs, &total, &used);
    } while ((used < total / 100 * 70 || fs.free_blocks >= 3) && used < total / 100 * 85);
    CHECK(fs.free_blocks < 3, "healthy store: %d free blocks", (int)fs.free_blocks);
    SPIFFS_unmount(&fs);
    srr_report_t report;
    srr_config_t config = repair_config();
    writing_phases = 0;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_CLEAN && report.blocks_compacted > 0 && writing_phases == 0,
          "healthy store: %s, %u blocks compacted, %u phases lit the repair LED", srr_result_name(report.result),
          report.blocks_compacted, writing_phases);
    mount_plain();
    check_custody(a, 1, false);
    check_custody(b, 1, false);
    SPIFFS_unmount(&fs);
    printf("healthy store: compacted, CLEAN, no repair LED\n");
}

/* Review 3 (custody), A10: scattered unreadable bytes less than a page apart
 * are one merged gap record whose bounds cover the readable bytes between
 * them. A survey later in the same run that cannot read those bytes either
 * (the copy that held them erased: SPIFFS's GC in the run's own writes, here
 * as normalization starts, before compaction makes room and the store is
 * surveyed again) took the record's bounds for bytes nobody could read and
 * filled them. They come back from the hold; only the bytes unreadable when
 * the file was held are filled. */
static uint32_t merged_copy_page;
static void merged_phase_hook(srp_phase_t phase, uint32_t pass)
{
    if (phase != SRP_PHASE_NORMALIZE || pass != 0 || merged_copy_page == UINT32_MAX) return;
    erase_page(merged_copy_page);
#if SPIFFS_CACHE
    spiffs_cache_drop_page(&fs, (spiffs_page_ix)merged_copy_page);
#endif
    merged_copy_page = UINT32_MAX;
}

static void run_merged_from_hold(uint32_t kib)
{
    seed(31801);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    /* Crammed: every append to the ballast leaves deleted pages, and fewer
     * free blocks than the work wants, so compaction runs first. */
    static uint8_t ballast[1000];
    memset(ballast, 0x42, sizeof(ballast));
    u32_t total = 0, used = 0;
    do {
        CHECK(append_bytes("/ballast.bin", ballast, sizeof(ballast)), "ballast");
        SPIFFS_info(&fs, &total, &used);
    } while ((used < total / 100 * 70 || fs.free_blocks >= 3) && used < total / 100 * 85);
    SPIFFS_unmount(&fs);
    /* Span 5's page lost bytes 20..123 to a torn program; a finalized copy
     * of it holds all but 20..23 and 120..123. */
    uint32_t span = 5, pix = rd16(entry_address(m->name, span)), copy = free_page();
    copy_page(copy, pix);
    memset(flash + pix * PAGE + 5 + 20, 0xFF, 104);
    memset(flash + copy * PAGE + 5 + 20, 0xFF, 4);
    memset(flash + copy * PAGE + 5 + 120, 0xFF, 4);
    for (uint32_t i = 20; i < 24; ++i) m->unreadable[span * SPAN + i] = m->unreadable[span * SPAN + 100 + i] = 1;
    merged_copy_page = copy;
    phase_hook = merged_phase_hook;
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    phase_hook = NULL;
    CHECK(merged_copy_page == UINT32_MAX, "merged record: the copy was never erased");
    CHECK(report.result == SRR_RESULT_REPAIRED_WITH_GAPS && report.gaps == 1 && report.blocks_compacted > 0,
          "merged record: %s at %s, %u gaps, %u blocks compacted", srr_result_name(report.result), report.error_stage,
          report.gaps, report.blocks_compacted);
    srp_gaps_t gaps = gaps_now();
    CHECK(gaps.count == 1 && (gaps.gap[0].flags & SRP_GAP_MERGED) && gaps.gap[0].offset == span * SPAN + 20 &&
              gaps.gap[0].length == 104,
          "merged record: %u records, the first at %u length %u", gaps.count, gaps.gap[0].offset, gaps.gap[0].length);
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    printf("merged record: readable bytes inside its bounds came back from the hold\n");
}

/* "review" runs them all; "review-<part>" one of them. */
/* ---- review 4 (custody): the resume's merge and copies ----------------- */
/* Bytes of `data` at their own offsets in finalized, undeleted data pages of
 * any object whose header (hidden or not, any size) is named `name`, marked
 * in `present`; returns how many it marked. */
static uint32_t bytes_on_flash_if(const uint8_t *data, uint32_t size, const char *name, uint8_t *present,
                                  bool followed);
static uint32_t bytes_on_flash(const uint8_t *data, uint32_t size, const char *name, uint8_t *present)
{
    return bytes_on_flash_if(data, size, name, present, false);
}

/* followed: only pages a finalized page of the next span follows (an
 * append finalizes a page's header before its bytes, so the last page it
 * wrote when a cut came may hold a torn prefix). */
static uint32_t bytes_on_flash_if(const uint8_t *data, uint32_t size, const char *name, uint8_t *present,
                                  bool followed)
{
    uint32_t pages = flash_size / PAGE, n = 0, count = 0;
    uint16_t ids[16];
    for (uint32_t pix = 0; pix < pages && n < 16; ++pix) {
        uint32_t a = pix * PAGE;
        uint16_t id = rd16(a);
        uint8_t flags = flash[a + 4];
        if (pix % 16 == 0 || !(id & 0x8000) || id == 0xFFFF || rd16(a + 2) != 0) continue;
        if (flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) continue;
        if (!(flags & SPIFFS_PH_FLAG_DELET)) continue;
        if (strncmp((const char *)flash + a + offsetof(spiffs_page_object_ix_header, name), name, 32)) continue;
        ids[n++] = (uint16_t)(id & 0x7FFF);
    }
    for (uint32_t pix = 0; pix < pages; ++pix) {
        if (pix % 16 == 0) continue;
        uint32_t a = pix * PAGE;
        uint16_t obj = rd16(a), lu = rd16(lookup_address(pix));
        uint8_t flags = flash[a + 4];
        if ((obj & 0x8000) || lu != obj) continue;
        if ((flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL)) || !(flags & SPIFFS_PH_FLAG_DELET) ||
            !(flags & SPIFFS_PH_FLAG_INDEX))
            continue;
        bool mine = false;
        for (uint32_t i = 0; i < n && !mine; ++i) mine = ids[i] == obj;
        if (!mine) continue;
        uint32_t span = rd16(a + 2);
        bool next = !followed;
        for (uint32_t q = 0; q < pages && !next; ++q) {
            uint32_t b = q * PAGE;
            uint8_t g = flash[b + 4];
            next = q % 16 && rd16(b) == obj && rd16(b + 2) == span + 1 && rd16(lookup_address(q)) == obj &&
                !(g & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL)) && (g & SPIFFS_PH_FLAG_DELET) &&
                (g & SPIFFS_PH_FLAG_INDEX);
        }
        if (!next) continue;
        for (uint32_t i = 0; i < SPAN; ++i) {
            uint32_t off = span * SPAN + i;
            if (off < size && flash[a + 5 + i] == data[off] && !present[off]) {
                present[off] = 1;
                count++;
            }
        }
    }
    return count;
}

/* The original's bytes on flash under its name or the temporary one. */
static uint32_t original_on_flash(const uint8_t *data, uint32_t size, const char *name, uint8_t *present)
{
    char temp[40];
    snprintf(temp, sizeof(temp), "%s%s", name, SRP_RESTORE_SUFFIX);
    memset(present, 0, size + 1);
    return bytes_on_flash(data, size, name, present) + bytes_on_flash(data, size, temp, present);
}

static int64_t read_named(const char *name, uint8_t *buffer, uint32_t capacity)
{
    mount_plain();
    int64_t got = exists(name) ? read_file(name, buffer, capacity) : -2;
    SPIFFS_unmount(&fs);
    return got;
}

/* Rows (newline-terminated) of `data` found verbatim in `buffer`, skipping
 * rows `skip` marks (any byte unmarked in it); *rows: the rows counted. */
static uint32_t rows_found(const uint8_t *buffer, int64_t got, const uint8_t *data, uint32_t size,
                           const uint8_t *whole, uint32_t *rows)
{
    uint32_t found = 0;
    *rows = 0;
    for (uint32_t start = 0; start < size;) {
        const uint8_t *nl = memchr(data + start, '\n', size - start);
        uint32_t end = nl ? (uint32_t)(nl - data) + 1 : size;
        bool counted = true;
        for (uint32_t i = start; whole && i < end && counted; ++i) counted = whole[i] != 0;
        if (counted) {
            (*rows)++;
            found += got > 0 && holds(buffer, (size_t)got, data + start, end - start);
        }
        start = end;
    }
    return found;
}

/* The manifest of an interrupted unconfirmed run over /pending.jsonl, its
 * header hidden by the check; a rolled-back image then created the name
 * again with `new_rows` rows (written before the store filled, so the
 * original stays intact: raw rename of another file). Returns the
 * original's bytes (caller frees). */
static struct {
    char name[33];
    uint8_t data[8192];
    uint32_t size;
} merge_new_file;
static uint8_t *merge_setup(uint32_t kib, uint64_t seed_value, uint32_t new_rows, uint32_t fill, bool filler,
                            uint32_t *size_out)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    model_file_t *m = model_of("/pending.jsonl");
    set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
    srp_manifest_t man;
    manifest_begin(&man, SRP_MANIFEST_PENDING_IMAGE);
    manifest_add(&man, m, SRP_CLASS_ATTENDANCE, "legacy_queues", "ords_pending");
    manifest_store(&man);
    store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
    uint8_t *orig = malloc(m->size);
    memcpy(orig, m->data, m->size);
    *size_out = m->size;
    memset(&merge_new_file, 0, sizeof(merge_new_file));
    snprintf(merge_new_file.name, sizeof(merge_new_file.name), "/pending.jsonl");
    char row[600];
    for (uint32_t i = 0; i < new_rows; ++i) {
        size_t n = json_row(row, sizeof(row));
        CHECK(append_bytes("/pending.new", row, (uint32_t)n), "merge setup: new rows");
        CHECK(merge_new_file.size + n <= sizeof(merge_new_file.data), "merge setup: rows");
        memcpy(merge_new_file.data + merge_new_file.size, row, n);
        merge_new_file.size += (uint32_t)n;
    }
    static uint8_t ballast[4000];
    memset(ballast, 0x5A, sizeof(ballast));
    u32_t total = 0, used = 0;
    SPIFFS_info(&fs, &total, &used);
    if (filler)
        while (append_bytes("/filler.bin", ballast, sizeof(ballast))) {}
    else
        while (used < total / 100 * fill && append_bytes("/ballast.bin", ballast, 2000)) SPIFFS_info(&fs, &total, &used);
    SPIFFS_unmount(&fs);
    hide_header("/pending.jsonl");
    uint32_t header = header_of("/pending.new");
    char name[32];
    memset(name, 0, sizeof(name));
    snprintf(name, sizeof(name), "/pending.jsonl");
    memcpy(flash + header * PAGE + offsetof(spiffs_page_object_ix_header, name), name, sizeof(name));
    return orig;
}

/* The original (`size` bytes) is whole inside the live file, or the manifest
 * is kept with every byte of it still on flash under its name or the
 * temporary one; the new file's rows are intact either way. */
static void check_merged_or_kept(const char *what, const uint8_t *orig, uint32_t size)
{
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_named("/pending.jsonl", buffer, sizeof(buffer));
    bool whole = got >= (int64_t)size && holds(buffer, (size_t)got, orig, size);
    CHECK(got >= (int64_t)merge_new_file.size && !memcmp(buffer, merge_new_file.data, merge_new_file.size),
          "%s: the new file's rows are not intact (reads %" PRId64 ")", what, got);
    if (whole) return;
    uint8_t *present = calloc(size + 1, 1);
    uint32_t left = original_on_flash(orig, size, "/pending.jsonl", present);
    free(present);
    CHECK(manifest_kept() && left == size,
          "%s: the original is neither whole in the live file (reads %" PRId64 ") nor kept (manifest %d, %u of %u "
          "bytes on flash)",
          what, got, manifest_kept(), left, size);
}

/* Custody finding 1: the merge appended into the new file before its dirty
 * tail (an interrupted append's bytes past its end) was cleaned: the first
 * merged row was ANDed with them, the run settled and the original went. */
static void run_merge_dirty_tail(uint32_t kib)
{
    uint32_t size = 0;
    uint8_t *orig = merge_setup(kib, 50000, 3, 0, false, &size);
    uint32_t pix = rd16(entry_address("/pending.jsonl", (merge_new_file.size - 1) / SPAN));
    CHECK(merge_new_file.size % SPAN && merge_new_file.size % SPAN < SPAN - 40, "merge dirty: ends at %u",
          merge_new_file.size);
    char row[600];
    size_t n = json_row(row, sizeof(row));
    for (uint32_t i = 0; i < 30 && i < n; ++i) flash[pix * PAGE + 5 + merge_new_file.size % SPAN + i] &= (uint8_t)row[i];
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    CHECK(report.result != SRR_RESULT_FAILED, "merge dirty: %s at %s", srr_result_name(report.result),
          report.error_stage);
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_named("/pending.jsonl", buffer, sizeof(buffer));
    uint32_t rows = 0, found = rows_found(buffer, got, orig, size, NULL, &rows);
    CHECK(found == rows && holds(buffer, (size_t)got, orig, size) && !manifest_kept(),
          "merge dirty: %u of the original's %u rows verbatim, whole %d, manifest kept %d", found, rows,
          got > 0 && holds(buffer, (size_t)got, orig, size), manifest_kept());
    check_merged_or_kept("merge dirty", orig, size);
    free(orig);
    printf("merge after a dirty tail: the new file's tail cleaned first, %u of %u rows verbatim\n", found, rows);
}

/* Custody finding 2: a merge without room settled the entry (CHECK_CHANGED
 * in the original's offsets) and the next check freed the intact original.
 * Now the original waits under the temporary name (no SPIFFS GC wipes it
 * there), the entry stays, and the merge happens on the boot with room. */
static void run_merge_crammed(uint32_t kib)
{
    uint32_t size = 0;
    uint8_t *orig = merge_setup(kib, 46000, 6, 0, true, &size);
    uint8_t *present = calloc(size + 1, 1);
    CHECK(original_on_flash(orig, size, "/pending.jsonl", present) == size, "merge crammed: original not intact");
    for (int boot = 0; boot < 2; ++boot) {
        srr_report_t report;
        srr_config_t config = repair_config();
        config.pending_verify = true;
        repair_boot(&report, &config);
        CHECK(report.result != SRR_RESULT_FAILED, "merge crammed: %s at %s", srr_result_name(report.result),
              report.error_stage);
        check_merged_or_kept("merge crammed", orig, size);
    }
    mount_plain();
    CHECK(SPIFFS_remove(&fs, "/filler.bin") == SPIFFS_OK, "merge crammed: remove filler");
    SPIFFS_unmount(&fs);
    boot_until_done("merge crammed, with room");
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_named("/pending.jsonl", buffer, sizeof(buffer));
    CHECK(got > 0 && holds(buffer, (size_t)got, orig, size) && !manifest_kept(),
          "merge crammed: with room the original is not whole in /pending.jsonl (reads %" PRId64 ", manifest %d)", got,
          manifest_kept());
    srp_gaps_t gaps = gaps_now();
    for (uint32_t g = 0; g < gaps.count; ++g)
        CHECK(strcmp(gaps.gap[g].name, "/pending.jsonl"), "merge crammed: a gap record (%u+%u, %s) for an intact original",
              gaps.gap[g].offset, gaps.gap[g].length, srp_gap_cause_name((srp_gap_cause_t)gaps.gap[g].cause));
    free(present);
    free(orig);
    printf("merge on a crammed store: kept until there is room, then merged whole\n");
}

/* Custody finding 3: an original SPIFFS's GC (an older image's writes) left
 * only partly on flash was superseded with no record. Its readable rows go
 * after the new file's, its unreadable bytes are records there. 2.7.5 still
 * superseded it when its queue cursor's generation moved; that moves on
 * every settled row, so 2.7.6 brings it back then too. */
static void run_merge_partial(uint32_t kib)
{
    for (int moved = 0; moved < 2; ++moved) {
        seed(45000);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        model_file_t *m = model_of("/pending.jsonl");
        set_cursor("legacy_queues", "ords_pending", 0, 0, 7);
        srp_manifest_t man;
        manifest_begin(&man, SRP_MANIFEST_PENDING_IMAGE);
        manifest_add(&man, m, SRP_CLASS_ATTENDANCE, "legacy_queues", "ords_pending");
        manifest_store(&man);
        store_state(SRP_PHASE_CHECK, 0, 0, SRP_STATE_MANIFEST, 0);
        if (moved) set_cursor("legacy_queues", "ords_pending", 0, 0, 8);
        uint32_t size = m->size;
        uint8_t *orig = malloc(size);
        memcpy(orig, m->data, size);
        SPIFFS_unmount(&fs);
        hide_header(m->name);
        mount_plain();
        char row[600];
        memset(&merge_new_file, 0, sizeof(merge_new_file));
        for (int i = 0; i < 6; ++i) {
            size_t n = json_row(row, sizeof(row));
            CHECK(append_bytes("/pending.jsonl", row, (uint32_t)n), "merge partial: new rows");
            memcpy(merge_new_file.data + merge_new_file.size, row, n);
            merge_new_file.size += (uint32_t)n;
        }
        static uint8_t filler[4000];
        memset(filler, 0x5A, sizeof(filler));
        while (append_bytes("/filler.bin", filler, sizeof(filler))) {}
        SPIFFS_unmount(&fs);
        uint8_t *whole = calloc(size + 1, 1);
        uint32_t before = original_on_flash(orig, size, "/pending.jsonl", whole);
        CHECK(before < size && before > size / 2, "merge partial: %u of %u bytes of the original on flash", before,
              size);
        uint32_t superseded = 0;
        for (int boot = 0; boot < 2; ++boot) {
            srr_report_t report;
            srr_config_t config = repair_config();
            config.pending_verify = true;
            repair_boot(&report, &config);
            CHECK(report.result != SRR_RESULT_FAILED, "merge partial: %s at %s", srr_result_name(report.result),
                  report.error_stage);
            superseded += report.files_superseded;
        }
        mount_plain();
        (void)SPIFFS_remove(&fs, "/filler.bin");
        SPIFFS_unmount(&fs);
        boot_until_done("merge partial, with room");
        static uint8_t buffer[8 * 1024 * 1024];
        int64_t got = read_named("/pending.jsonl", buffer, sizeof(buffer));
        CHECK(got >= (int64_t)merge_new_file.size && !memcmp(buffer, merge_new_file.data, merge_new_file.size),
              "merge partial: the new file's rows changed");
        srp_gaps_t gaps = gaps_now();
        uint64_t recorded = 0;
        for (uint32_t g = 0; g < gaps.count; ++g)
            if (!strcmp(gaps.gap[g].name, "/pending.jsonl")) recorded += gaps.gap[g].length;
        uint32_t rows = 0, found = rows_found(buffer, got, orig, size, whole, &rows);
        /* 2.7.6 (review C3): a moved cursor generation (moved) no longer
         * supersedes the remnant: every settled row moves it, so it proves no
         * retirement. Both ways its readable rows come back. */
        CHECK(!superseded && found == rows && recorded >= size - before && !manifest_kept(),
              "merge partial%s: superseded %u, %u of %u whole rows verbatim, %" PRIu64 " bytes recorded for %u "
              "missing, manifest kept %d",
              moved ? " (cursor moved)" : "", superseded, found, rows, recorded, size - before, manifest_kept());
        free(whole);
        free(orig);
    }
    printf("merge of a partial original: its readable rows after the new file's, the rest recorded, whether or not "
           "its cursor moved\n");
}

/* Custody finding 2 under cuts: the merge on a store whose room comes from
 * compaction, a cut at every `stride`th write of the resume boot, torn both
 * ways. The original must end whole in the live file, or kept whole on
 * flash with the manifest. */
static void run_merge_cuts(uint32_t kib, uint32_t fill, uint32_t stride)
{
    uint32_t size = 0;
    uint8_t *orig = merge_setup(kib, 48000, 6, fill, false, &size);
    uint8_t *present = calloc(size + 1, 1);
    CHECK(original_on_flash(orig, size, "/pending.jsonl", present) == size, "merge cuts: original not intact");
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    srr_report_t report;
    srr_config_t config = repair_config();
    config.pending_verify = true;
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t writes = flash_ops - start;
    check_merged_or_kept("merge cuts, uncut", orig, size);
    uint32_t runs = 0;
    uint64_t only_k = getenv("REVIEW_K") ? strtoull(getenv("REVIEW_K"), NULL, 10) : 0;
    for (uint64_t k = 1; k <= writes; k += stride) {
        if (only_k && k != only_k) continue;
        for (uint32_t tear = 0; tear < 2; ++tear) {
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(49000 + k * 4 + tear);
            print_log = only_k != 0;
            volatile bool cut = false;
            cut_at = flash_ops + k;
            if (setjmp(cut_point)) {
                cut = true;
                drop_all_memory();
            } else {
                repair_boot(&report, &config);
            }
            cut_at = 0;
            if (!cut) continue;
            runs++;
            boot_until_done("merge cuts");
            print_log = false;
            char what[80];
            snprintf(what, sizeof(what), "merge cuts %u%%, cut at write %" PRIu64 " tear %u", fill, k, tear);
            check_merged_or_kept(what, orig, size);
        }
    }
    free(image);
    free(saved);
    free(present);
    free(orig);
    printf("merge under cuts (%u%% full): %u cut runs, the original merged or kept whole\n", fill, runs);
}

/* Custody finding 4: an unconfirmed boot rebuilt the queue whole under the
 * temporary name and a cut came before the rename; the rolled-back image
 * created the name again; the next 2.7.5 boot purged the rebuild. */
static void run_rebuild_name_taken(uint32_t kib)
{
    seed(43000);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    model_file_t *m = model_of("/pending.jsonl");
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    srr_config_t config = repair_config();
    config.pending_verify = true;
    config.rollback_tag = 0x2740;
    check_hook = rebuild_hook;
    hook_mode = 1;
    srr_report_t report;
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t end = flash_ops, at_hook = review_hook_ops;
    uint8_t *present = calloc(m->size + 1, 1);
    uint64_t chosen = 0;
    char row[600];
    size_t punch = 0;
    for (uint64_t k = end - start; k > at_hook - start && !chosen; --k) {
        memcpy(flash, image, flash_size);
        memcpy(&nvs, saved, sizeof(nvs));
        seed(44000 + k);
        check_hook = rebuild_hook;
        hook_mode = 1;
        bool cut = boot_with_cut(k);
        hook_mode = 0;
        check_hook = NULL;
        if (!cut) continue;
        memset(present, 0, m->size + 1);
        uint32_t in_rpr = bytes_on_flash(m->data, m->size, "/pending.jsonl.rpr", present);
        mount_plain();
        bool named = exists(m->name), rpr = exists("/pending.jsonl.rpr");
        bool punched = false;
        if (in_rpr == m->size && !named && rpr) {
            /* The rolled-back image runs: a punch creates the name. */
            punch = json_row(row, sizeof(row));
            punched = append_bytes(m->name, row, (uint32_t)punch);
        }
        SPIFFS_unmount(&fs);
        if (punched) chosen = k;
    }
    hook_mode = 0;
    check_hook = NULL;
    CHECK(chosen, "rebuild name taken: no cut left a whole rebuild under its temporary name");
    srr_config_t again = repair_config();
    again.pending_verify = true;
    repair_boot(&report, &again);
    CHECK(report.result != SRR_RESULT_FAILED, "rebuild name taken: %s at %s", srr_result_name(report.result),
          report.error_stage);
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_named(m->name, buffer, sizeof(buffer));
    CHECK(got > 0 && holds(buffer, (size_t)got, m->data, m->size) && holds(buffer, (size_t)got, (uint8_t *)row, punch),
          "rebuild name taken (cut at write %" PRIu64 "): the rebuild's rows or the punch are not in %s (reads %" PRId64
          ", manifest kept %d)",
          chosen, m->name, got, manifest_kept());
    srp_gaps_t gaps = gaps_now();
    for (uint32_t g = 0; g < gaps.count; ++g)
        CHECK(strcmp(gaps.gap[g].name, m->name), "rebuild name taken: a gap record (%u+%u, %s) for a whole rebuild",
              gaps.gap[g].offset, gaps.gap[g].length, srp_gap_cause_name((srp_gap_cause_t)gaps.gap[g].cause));
    free(present);
    free(image);
    free(saved);
    printf("whole rebuild, name taken after a rollback: merged after the new file's rows (cut at write %" PRIu64 ")\n",
           chosen);
}

/* Custody finding 5: the in-run restore of a file the budget did not hold
 * found no page to bring its header back, recorded it as lost and settled;
 * the next check freed every byte. It stays unsettled now, the run fails,
 * and the next boot (here with room) brings it back whole. */
static const char *const crammed_fill_names[] = {"/fill-a.bin", "/fill-b.bin", "/fill-c.bin"};
static uint32_t crammed_consumed;

/* Raw: every erased page becomes a full data page of an empty fill file, as
 * SPIFFS's first append programs a header in place: a consistent store with
 * no erased page at all. */
static void consume_erased(void)
{
    uint32_t pages = flash_size / PAGE;
    for (uint32_t b = 0; b < flash_size / BLOCK; ++b) {
        bool any_used = false;
        for (uint32_t e = 1; e < 16 && !any_used; ++e) {
            uint16_t lu = rd16(lookup_address(b * 16 + e));
            any_used = lu != 0xFFFF && lu != 0;
        }
        if (!any_used) {
            uint8_t lookup[PAGE];
            memcpy(lookup, flash + b * BLOCK, PAGE);
            memset(flash + b * BLOCK, 0xFF, BLOCK);
            memcpy(flash + b * BLOCK + 30, lookup + 30, PAGE - 30);
        }
    }
    crammed_consumed = 0;
    for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f) {
        uint32_t header = header_of(crammed_fill_names[f]);
        uint16_t obj = rd16(header * PAGE) & 0x7FFF;
        uint32_t span = 0;
        for (uint32_t pix = 0; pix < pages && span < 100; ++pix) {
            if (pix % 16 == 0 || rd16(lookup_address(pix)) != 0xFFFF) continue;
            bool erased = true;
            for (uint32_t i = 0; i < PAGE && erased; ++i) erased = flash[pix * PAGE + i] == 0xFF;
            if (!erased) continue;
            uint8_t *p = flash + pix * PAGE;
            p[0] = (uint8_t)obj;
            p[1] = (uint8_t)(obj >> 8);
            p[2] = (uint8_t)span;
            p[3] = (uint8_t)(span >> 8);
            p[4] = (uint8_t)(0xFF & ~(SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL));
            memset(p + 5, 'F', SPAN);
            wr16(lookup_address(pix), obj);
            wr16(header * PAGE + sizeof(spiffs_page_object_ix_header) + span * 2, (uint16_t)pix);
            span++;
            crammed_consumed++;
        }
        uint32_t size = span * SPAN;
        if (span) memcpy(flash + header * PAGE + offsetof(spiffs_page_object_ix_header, size), &size, 4);
    }
#if SPIFFS_CACHE
    for (uint32_t pix = 0; pix < pages; ++pix) spiffs_cache_drop_page(&fs, (spiffs_page_ix)pix);
#endif
    uint32_t free_blocks = 0;
    for (uint32_t b = 0; b < flash_size / BLOCK; ++b) {
        bool empty = true;
        for (uint32_t e = 1; e < 16 && empty; ++e) empty = rd16(lookup_address(b * 16 + e)) == 0xFFFF;
        free_blocks += empty;
    }
    fs.free_blocks = free_blocks;
    fs.stats_p_allocated += crammed_consumed;
}

static bool hide_and_cram(uint32_t pass)
{
    if (pass != 1) return false;
    review_hook_ops = flash_ops;
    uint32_t header = header_of("/pending.jsonl");
    flash[header * PAGE + 4] &= (uint8_t)~SPIFFS_PH_FLAG_IXDELE;
    consume_erased();
    if (review_mode & 2) longjmp(review_cut, 1);
    return true;
}

/* The check hides the queue and crams the store; with `cut`, a power cut
 * right after it, and the next boot's resume. */
static void unheld_crammed_boot(srr_report_t *report, const srr_config_t *config, bool cut)
{
    check_hook = hide_and_cram;
    review_mode = cut ? 2 : 0;
    if (cut) {
        if (!setjmp(review_cut)) {
            repair_boot(report, config);
            CHECK(0, "unheld crammed: the hook never ran");
        }
        drop_all_memory();
        check_hook = NULL;
    }
    repair_boot(report, config);
    check_hook = NULL;
    review_mode = 0;
}

static void run_unheld_crammed(uint32_t kib)
{
    /* Without a redo area (2.7.5's behaviour, kept when the area is not
     * usable); then with one, where the same boot reformats blocks in place
     * and brings the file back whole. */
    for (int redo = 0; redo < 2; ++redo)
    for (int resume = 0; resume < 2; ++resume) {
        redo_enabled = redo != 0;
        seed(47000);
        nvs_clear();
        format_flash(kib);
        build_store(60000);
        for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
            write_file(crammed_fill_names[f], NULL, 0);
        static uint8_t filler[4000];
        memset(filler, 0x5A, sizeof(filler));
        while (append_bytes("/filler.bin", filler, sizeof(filler))) {}
        SPIFFS_unmount(&fs);
        model_file_t *m = model_of("/pending.jsonl");
        srr_config_t config = repair_config();
        config.hold_budget = 0; /* no holds: the manifest and R3 protect every file */
        srr_report_t report;
        unheld_crammed_boot(&report, &config, resume != 0);
        if (redo) {
            check_whole("unheld crammed, reformatted", m, 1);
            CHECK(!manifest_kept() && report.reclaim.blocks > 0 && report.result != SRR_RESULT_FAILED,
                  "unheld crammed, reformatted (%s): %s at %s, manifest kept %d, %u blocks",
                  resume ? "resume" : "in run", srr_result_name(report.result), report.error_stage, manifest_kept(),
                  report.reclaim.blocks);
            check_reclaimed_structure("unheld crammed, reformatted");
            continue;
        }
        uint8_t *present = calloc(m->size + 1, 1);
        uint32_t left = original_on_flash(m->data, m->size, m->name, present);
        CHECK(manifest_kept() && left == m->size && report.result != SRR_RESULT_CLEAN &&
                  report.result != SRR_RESULT_REPAIRED && report.result != SRR_RESULT_REPAIRED_WITH_GAPS,
              "unheld crammed (%s): %s at %s, manifest kept %d, %u of %u bytes on flash",
              resume ? "resume" : "in run", srr_result_name(report.result), report.error_stage, manifest_kept(), left,
              m->size);
        /* Room again (the fill files go): the next boot brings it back. */
        mount_plain();
        for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
            (void)SPIFFS_remove(&fs, crammed_fill_names[f]);
        (void)SPIFFS_remove(&fs, "/filler.bin");
        SPIFFS_unmount(&fs);
        boot_until_done("unheld crammed, with room");
        check_whole("unheld crammed, with room", m, 1);
        CHECK(!manifest_kept(), "unheld crammed: manifest kept with room");
        free(present);
    }
    redo_enabled = true;
    printf("unheld file hidden on a store with no erased page: kept unsettled, back whole with room; with a redo "
           "area, back whole in the same boot\n");
}

/* Custody finding 6: a cut inside the repair's rebuild left finalized pages
 * past its committed size (dropped as strays), or a torn first-append size
 * (the copy taken for an expendable and purged): up to a whole 4,016-byte
 * write lost, recorded. Every page of the rebuild a later page follows (the
 * last one may be torn: an append finalizes first) stays now, and every
 * byte lost is recorded. */
static void run_rebuild_salvage(uint32_t kib, uint32_t stride, uint32_t tears)
{
    seed(41000);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    model_file_t *m = model_of("/pending.jsonl");
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    check_hook = rebuild_hook;
    hook_mode = 1;
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t end = flash_ops, at_hook = review_hook_ops;
    hook_mode = 0;
    check_hook = NULL;
    uint8_t *present = malloc(m->size + 1);
    static uint8_t buffer[8 * 1024 * 1024];
    uint32_t runs = 0;
    uint64_t only_k = getenv("REVIEW_K") ? strtoull(getenv("REVIEW_K"), NULL, 10) : 0;
    for (uint64_t k = at_hook - start + 1; k <= end - start; k += stride) {
        if (only_k && k != only_k) continue;
        for (uint32_t tear = 0; tear < tears; ++tear) {
            print_log = only_k != 0;
            memcpy(flash, image, flash_size);
            memcpy(&nvs, saved, sizeof(nvs));
            seed(52000 + k * 16 + tear);
            check_hook = rebuild_hook;
            hook_mode = 1;
            bool cut = boot_with_cut(k);
            hook_mode = 0;
            check_hook = NULL;
            if (!cut) continue;
            runs++;
            memset(present, 0, m->size + 1);
            (void)bytes_on_flash_if(m->data, m->size, "/pending.jsonl.rpr", present, true);
            (void)bytes_on_flash(m->data, m->size, m->name, present);
            boot_until_done("rebuild salvage");
            print_log = false;
            int64_t got = read_named(m->name, buffer, sizeof(buffer));
            uint32_t lost = 0, first = UINT32_MAX;
            for (uint32_t i = 0; i < m->size; ++i)
                if (present[i] && !(got > (int64_t)i && buffer[i] == m->data[i]) && !lost++) first = i;
            CHECK(!lost, "rebuild salvage: cut at write %" PRIu64 " tear %u: %u bytes that were on flash are gone "
                  "(first %u; the file reads %" PRId64 ")",
                  k, tear, lost, first, got);
            check_named("rebuild salvage", m, 1);
        }
    }
    free(present);
    free(image);
    free(saved);
    printf("rebuild salvage: %u cuts inside the rebuild, every byte it had on flash kept\n", runs);
}

/* Review 4 (boot), finding 1: on a store with no erased page and the
 * damaged queue in every block, level 2 dropped the identity catalog and
 * still had no erased page (deleting a file erases nothing; only compaction
 * does, and no block could be compacted). Level 2 runs only when its
 * removals could let a block be erased. And a store handed over with fewer
 * than two free blocks and fewer erased pages than SPIFFS's GC needs to
 * clean a block is not REPAIRED: DEFERRED, `gc_unsafe`, with the room. */
/* ---- 2.7.6: oracles on a store the last resort reformatted ------------- */

/* Raw structure: every block's magic and erase count, SPIFFS's free-tail
 * rule for entry 0, no free lookup entry over a written page; the redo area
 * holds no open record and the journal is not active. */
static void check_reclaimed_structure(const char *what)
{
    uint32_t blocks = flash_size / BLOCK, free_blocks = 0;
    for (uint32_t b = 0; b < blocks; ++b) {
        CHECK(rd16(b * BLOCK + 252) == srx_spiffs_magic(blocks, b), "%s: block %u magic %04x", what, b,
              rd16(b * BLOCK + 252));
        /* REVIEW_TO_END: a cut in SPIFFS's own erase of a block (its GC in
         * the run after the round) can leave its magic and no erase count,
         * which SPIFFS's mount tolerates; the round's own erases clear the
         * magic first, so no window or close ever leaves one. */
        CHECK(rd16(b * BLOCK + 254) != 0xFFFF || getenv("REVIEW_TO_END"), "%s: block %u without an erase count", what,
              b);
        bool entry0_free = rd16(lookup_address(b * 16 + 1)) == 0xFFFF;
        free_blocks += entry0_free;
        for (uint32_t e = 1; e < 16; ++e) {
            uint32_t pix = b * 16 + e;
            if (rd16(lookup_address(pix)) != 0xFFFF) {
                CHECK(!entry0_free, "%s: block %u counted free with entry %u used", what, b, e - 1);
                continue;
            }
            bool erased = true;
            for (uint32_t i = 0; i < PAGE && erased; ++i) erased = flash[pix * PAGE + i] == 0xFF;
            CHECK(erased, "%s: page %04x under a free lookup entry is written", what, pix);
        }
    }
    if (free_blocks != engine_free_blocks && getenv("REVIEW_TO_END"))
        printf("%s: SPIFFS counted %u free blocks, the flash has %u (after a cut in SPIFFS's own erase; relaxed under "
               "REVIEW_TO_END)\n", what, engine_free_blocks, free_blocks);
    else CHECK(free_blocks == engine_free_blocks, "%s: SPIFFS counted %u free blocks, the flash has %u", what,
          engine_free_blocks, free_blocks);
    static uint8_t sector[4096];
    for (uint32_t slot = 0; slot < REDO_BYTES / 4096; ++slot) {
        srx_record_t r;
        memcpy(sector, nvs.redo + slot * 4096, 4096);
        if (!srx_decode(sector, &r, NULL)) continue;
        CHECK(r.done != 0xFFFFFFFFU || srx_is_source(&r), "%s: record of block %u left open", what, r.block);
    }
    srp_reclaim_t journal;
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) == 1)
        CHECK(srp_reclaim_valid(&journal) && !(journal.flags & (SRP_RECLAIM_ACTIVE | SRP_RECLAIM_SOURCES)),
              "%s: reclaim journal flags %x", what, journal.flags);
}

/* Everything a reformatted store must be: the structure above, every model
 * file byte-exact (no gap), at least three free blocks, no stray page, clean
 * tails, appends and a probe file work, SPIFFS's own GC moves nothing a live
 * file reads, and a second repair is clean. */
static void verify_reclaimed(const char *what)
{
    check_reclaimed_structure(what);
    mount_plain();
    CHECK(fs.free_blocks >= 3, "%s: %d free blocks after the repair", what, (int)fs.free_blocks);
    for (uint32_t i = 0; i < model_count; ++i)
        if (model[i].size && exists(model[i].name)) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    check_no_strays(what);
    check_clean_tails(what);
    check_health(appendable, sizeof(appendable) / sizeof(appendable[0]));
    mount_plain();
    for (uint32_t b = 0; b < flash_size / BLOCK && (uint32_t)fs.free_blocks > 2; ++b) {
        bool used = false;
        for (uint32_t e = 1; e < 16 && !used; ++e) used = rd16(lookup_address(b * 16 + e)) != 0xFFFF;
        if (!used || b % 3) continue;
        fs.cleaning = 1;
        s32_t res = spiffs_gc_clean(&fs, (spiffs_block_ix)b);
        fs.cleaning = 0;
        if (res == SPIFFS_OK) res = spiffs_gc_erase_page_stats(&fs, (spiffs_block_ix)b);
        if (res == SPIFFS_OK) res = spiffs_erase_block(&fs, (spiffs_block_ix)b);
        CHECK(res == SPIFFS_OK, "%s: SPIFFS GC of block %u failed %d", what, b, res);
    }
    for (uint32_t i = 0; i < model_count; ++i)
        if (model[i].size && exists(model[i].name)) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    srr_report_t again;
    srr_config_t config = repair_config();
    repair_boot(&again, &config);
    CHECK(again.result == SRR_RESULT_CLEAN && again.reclaim.blocks == 0, "%s: the next repair %s (%u blocks)", what,
          srr_result_name(again.result), again.reclaim.blocks);
}

static void run_level2_useless(uint32_t kib)
{
    /* Without a redo area the store stays as 2.7.5 leaves it; with one
     * (2.7.6) its blocks are reformatted in place and it repairs. */
    for (int redo = 0; redo < 2; ++redo) {
        redo_enabled = redo != 0;
        seed(9700);
        nvs_clear();
        format_flash(kib);
        mount_plain();
        model_reset();
        (void)make_binary_file("/add_identities.enc", 400);
        SPIFFS_unmount(&fs);
        full_store_with(0);
        /* The one block of deleted pages full_store_with leaves holds a valid
         * copy of the damaged head too: no block is free to erase. */
        uint16_t obj = object_of("/pending.jsonl");
        bool pinned = false;
        for (uint32_t b = 0; b < flash_size / BLOCK && !pinned; ++b) {
            bool all_deleted = true;
            for (uint32_t e = 1; e < 16 && all_deleted; ++e) all_deleted = rd16(lookup_address(b * 16 + e)) == 0;
            if (!all_deleted) continue;
            flash[(b * 16 + 1) * PAGE + 4] |= SPIFFS_PH_FLAG_DELET;
            wr16(lookup_address(b * 16 + 1), obj);
            pinned = true;
        }
        CHECK(pinned, "level 2 useless: no block of deleted pages");
        srr_report_t report;
        srr_config_t config = repair_config();
        repair_boot(&report, &config);
        mount_plain();
        bool kept = exists("/add_identities.enc");
        SPIFFS_unmount(&fs);
        if (!redo) {
            CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "full") && report.level < 2 && kept,
                  "level 2 useless: %s at %s, level %u, catalog kept %d", srr_result_name(report.result),
                  report.error_stage, report.level, kept);
            continue;
        }
        CHECK(report.result == SRR_RESULT_REPAIRED && report.level == 3 && report.reclaim.blocks > 0 &&
                  !strcmp(report.reclaim.trigger, "full") && kept,
              "level 2 useless, reformatted: %s at %s, level %u, %u blocks, catalog kept %d",
              srr_result_name(report.result), report.error_stage, report.level, report.reclaim.blocks, kept);
        verify_reclaimed("level 2 useless, reformatted");
    }
    redo_enabled = true;
    printf("level 2 skipped where it frees nothing: DEFERRED without a redo area, the catalog kept; reformatted in "
           "place with one\n");
}

/* A consistent store with no free block and no erased page, live and
 * deleted pages in every block (two queues interleaved, one removed, the
 * free blocks' pages taken by fill files): no block's moves fit, so SPIFFS's
 * GC would stop half way in the next writes. The repair, which has nothing
 * to write, hands it over as DEFERRED (`gc_unsafe`, with the room it lacks),
 * not CLEAN. */
static void run_gc_unsafe_handover(uint32_t kib)
{
    for (int redo = 0; redo < 2; ++redo) {
        redo_enabled = redo != 0;
        seed(9710);
        nvs_clear();
        format_flash(kib);
        model_reset();
        row_seq = 700;
        mount_plain();
        for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
            write_file(crammed_fill_names[f], NULL, 0);
        model_file_t *pending = new_model("/pending.jsonl", true, false);
        char row[600];
        while (fs.free_blocks > 2) {
            size_t n = json_row(row, sizeof(row));
            model_append(pending, row, (uint32_t)n);
            if (!append_bytes("/ballast.jsonl", row, (uint32_t)n)) break;
        }
        CHECK(SPIFFS_remove(&fs, "/ballast.jsonl") == SPIFFS_OK, "gc unsafe: remove ballast");
        SPIFFS_unmount(&fs);
        consume_erased();
        srr_report_t report;
        srr_config_t config = repair_config();
        repair_boot(&report, &config);
        if (!redo) {
            CHECK(report.result == SRR_RESULT_DEFERRED && !strcmp(report.error_stage, "gc_unsafe") &&
                      report.room_needed > report.room_erased,
                  "gc unsafe: %s at %s (%u pages needed, %u erased)", srr_result_name(report.result),
                  report.error_stage, report.room_needed, report.room_erased);
            mount_plain();
            check_custody(pending, 1, false);
            SPIFFS_unmount(&fs);
            printf("a store SPIFFS's GC cannot clean: DEFERRED at gc_unsafe (%u pages needed, %u erased) without a "
                   "redo area\n", report.room_needed, report.room_erased);
            continue;
        }
        CHECK(report.result == SRR_RESULT_REPAIRED && report.reclaim.blocks > 0 &&
                  !strcmp(report.reclaim.trigger, "gc_unsafe") && report.runs == 2,
              "gc unsafe, reformatted: %s at %s, %u blocks, trigger %s, %u runs", srr_result_name(report.result),
              report.error_stage, report.reclaim.blocks, report.reclaim.trigger, report.runs);
        verify_reclaimed("gc unsafe, reformatted");
        printf("the same store with a redo area: %u blocks reformatted in place, %u deleted pages erased, healthy\n",
               report.reclaim.blocks, report.reclaim.pages_freed);
    }
    redo_enabled = true;
}

/* A partial rebuild left under the temporary name, its name taken by a
 * rolled-back image's punch, on a store with no room for the merge: the
 * copy waits, untouched (the finish's tail termination once appended a
 * '\n' to it, replacing the original's next byte with no record), and is
 * merged with room. */
static void run_partial_rebuild_waits(uint32_t kib)
{
    seed(43100);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    SPIFFS_unmount(&fs);
    model_file_t *m = model_of("/pending.jsonl");
    uint8_t *image = malloc(flash_size);
    memcpy(image, flash, flash_size);
    __typeof__(nvs) *saved = malloc(sizeof(nvs));
    memcpy(saved, &nvs, sizeof(nvs));
    srr_config_t config = repair_config();
    config.pending_verify = true;
    check_hook = rebuild_hook;
    hook_mode = 1;
    srr_report_t report;
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    uint64_t end = flash_ops, at_hook = review_hook_ops;
    static uint8_t buffer[8 * 1024 * 1024];
    uint64_t chosen = 0;
    int64_t copy = 0;
    for (uint64_t k = at_hook - start + 40; k < end - start && !chosen; ++k) {
        memcpy(flash, image, flash_size);
        memcpy(&nvs, saved, sizeof(nvs));
        seed(43200 + k);
        check_hook = rebuild_hook;
        hook_mode = 1;
        bool cut = boot_with_cut(k);
        hook_mode = 0;
        check_hook = NULL;
        if (!cut) continue;
        mount_plain();
        copy = exists("/pending.jsonl.rpr") ? read_file("/pending.jsonl.rpr", buffer, sizeof(buffer)) : -1;
        bool usable = !exists(m->name) && copy > 0 && copy < (int64_t)m->size && buffer[copy - 1] != '\n' &&
            !memcmp(buffer, m->data, (size_t)copy);
        if (usable) {
            char row[600];
            size_t n = json_row(row, sizeof(row));
            usable = append_bytes(m->name, row, (uint32_t)n);
            static uint8_t filler[4000];
            memset(filler, 0x5A, sizeof(filler));
            while (usable && append_bytes("/filler.bin", filler, sizeof(filler))) {}
        }
        SPIFFS_unmount(&fs);
        if (usable) chosen = k;
    }
    CHECK(chosen, "partial rebuild waits: no cut left a partial rebuild ending inside a row");
    srr_config_t again = repair_config();
    again.pending_verify = true;
    repair_boot(&report, &again);
    /* On a larger store compaction can find the merge its room. */
    CHECK(report.result != SRR_RESULT_FAILED && (manifest_kept() || kib > 1024),
          "partial rebuild waits: %s at %s, manifest kept %d", srr_result_name(report.result), report.error_stage,
          manifest_kept());
    int64_t after = copy;
    if (manifest_kept()) {
        after = read_named("/pending.jsonl.rpr", buffer, sizeof(buffer));
        CHECK(after >= copy && !memcmp(buffer, m->data, (size_t)after),
              "partial rebuild waits (cut at write %" PRIu64 "): the copy of %" PRId64 " bytes now reads %" PRId64
              " and is no longer the original's prefix",
              chosen, copy, after);
        mount_plain();
        CHECK(SPIFFS_remove(&fs, "/filler.bin") == SPIFFS_OK, "partial rebuild waits: remove filler");
        SPIFFS_unmount(&fs);
        boot_until_done("partial rebuild waits, with room");
    }
    int64_t got = read_named(m->name, buffer, sizeof(buffer));
    CHECK(got > 0 && holds(buffer, (size_t)got, m->data, (size_t)after) && !manifest_kept(),
          "partial rebuild waits: its %" PRId64 " bytes are not in %s (reads %" PRId64 ", manifest %d)", after,
          m->name, got, manifest_kept());
    free(image);
    free(saved);
    printf("partial rebuild waiting for room under its temporary name: untouched, then merged (cut at write %" PRIu64
           ")\n", chosen);
}

static void run_review_custody(uint32_t kib, const char *only)
{
    const char *part = only && !strncmp(only, "review-", 7) ? only + 7 : NULL;
#define REVIEW_PART(name) (!part || !strcmp(part, name))
    if (REVIEW_PART("coverage")) run_manifest_coverage(kib);
    if (REVIEW_PART("records")) run_records_full(kib);
    if (REVIEW_PART("resume")) {
        run_crammed_resume(kib, 88, kib > 1024 ? 211 : 7);
        run_crammed_resume(kib, 80, kib > 1024 ? 211 : 3);
    }
    if (REVIEW_PART("restore")) run_crammed_restore(kib, 85, kib > 1024 ? 211 : 5);
    if (REVIEW_PART("hidden")) run_hidden_large(kib);
    if (REVIEW_PART("rebuild")) run_rebuild_cuts(kib, kib > 1024 ? 211 : 4);
    if (REVIEW_PART("healthy")) run_healthy_compaction(kib);
    if (REVIEW_PART("full-flag")) run_full_flag(kib);
    if (REVIEW_PART("cpu")) run_cpu_bound(kib);
    if (REVIEW_PART("level2")) run_level2_useless(kib);
    if (REVIEW_PART("gc-unsafe")) run_gc_unsafe_handover(kib);
    /* Review 4 (custody): the resume's merge and the repair's own copies. */
    if (REVIEW_PART("merge-dirty")) run_merge_dirty_tail(kib);
    if (REVIEW_PART("merge-crammed")) run_merge_crammed(kib);
    if (REVIEW_PART("merge-partial")) run_merge_partial(kib);
    if (REVIEW_PART("merge-cuts")) run_merge_cuts(kib, 80, kib > 1024 ? 97 : getenv("REVIEW_FULL") ? 1 : 9);
    if (REVIEW_PART("rebuild-name")) run_rebuild_name_taken(kib);
    if (REVIEW_PART("rebuild-waits")) run_partial_rebuild_waits(kib);
    if (REVIEW_PART("unheld-crammed")) run_unheld_crammed(kib);
    if (REVIEW_PART("salvage")) run_rebuild_salvage(kib, kib > 1024 ? 97 : getenv("REVIEW_FULL") ? 1 : 5,
                                                      getenv("REVIEW_FULL") ? 4 : 2);
    if (REVIEW_PART("merged")) run_merged_from_hold(kib);
    if (REVIEW_PART("inbox") || REVIEW_PART("boundary")) run_inbox_boundary(kib);
    if (REVIEW_PART("inbox") || REVIEW_PART("window")) run_inbox_window(kib);
#undef REVIEW_PART
}

/* ---- 2.7.6: the last resort, against wedged stores --------------------- */
/* The released 2.7.5 engine (tests/firmware/fixtures/storage_repair_2_7_5,
 * byte-identical to 0954fd3): the image a 2.7.6 boot rolls back to. Its
 * structs are a prefix of 2.7.6's. */
void srr275_run(spiffs *f, const srr_config_t *config, const srr_ports_t *p, srr_report_t *report);
void srr275_check_callback(spiffs *f, spiffs_check_type type, spiffs_check_report report, u32_t a, u32_t b);

static void repair_boot_275(srr_report_t *report, const srr_config_t *config)
{
    memset(report, 0, sizeof(*report));
    CHECK(mount_with(srr275_check_callback) == SPIFFS_OK, "2.7.5 mount %d", SPIFFS_errno(&fs));
    srr275_run(&fs, config, &ports, report);
    SPIFFS_unmount(&fs);
    CHECK(memory_in_use == 0, "2.7.5 leaked %" PRIu64 " bytes", memory_in_use);
}

static srr_config_t config_2760(bool unconfirmed)
{
    srr_config_t config = repair_config();
    config.image_tag = 0x2760;
    config.pending_verify = unconfirmed;
    config.rollback_tag = unconfirmed ? 0x2520 : 0;
    return config;
}

static void boot_until_done_with(const char *what, const srr_config_t *config, srr_report_t *out)
{
    srr_report_t report;
    for (int boot = 0;; ++boot) {
        CHECK(boot < 10, "%s: repair never finished", what);
        repair_boot(&report, config);
        if (report.result != SRR_RESULT_STOPPED && report.result != SRR_RESULT_NOT_RUN) break;
    }
    CHECK(report.result != SRR_RESULT_FAILED, "%s: %s at %s", what, srr_result_name(report.result), report.error_stage);
    if (out) *out = report;
}

static bool boot_with_cut_with(uint64_t k, const srr_config_t *config)
{
    srr_report_t report;
    cut_at = flash_ops + k;
    if (setjmp(cut_point)) {
        cut_at = 0;
        engine_running = false;
        drop_all_memory();
        return true;
    }
    repair_boot(&report, config);
    cut_at = 0;
    return false;
}

/* ---- the round's oracle ------------------------------------------------ */
typedef struct {
    char name[33];
    int64_t size;
    uint8_t *data;
} file_copy_t;
static uint8_t *round_store;            /* the store as the round began */
static bool round_unconfirmed, round_files_wanted;
static uint64_t round_start_ops, round_end_ops;
static uint32_t round_checks, round_entry0_kept, round_failed_blocks, round_closed_blocks, round_checks_total;
static uint32_t journal_flags(void);
static file_copy_t round_files[32];
static uint32_t round_file_count;

static void free_files(file_copy_t *files, uint32_t *count)
{
    for (uint32_t i = 0; i < *count; ++i) free(files[i].data);
    *count = 0;
}

/* Every file SPIFFS lists, as plain SPIFFS reads it (-1: unreadable). */
static void capture_files(file_copy_t *files, uint32_t *count)
{
    free_files(files, count);
    spiffs_DIR dir;
    struct spiffs_dirent entry;
    CHECK(SPIFFS_opendir(&fs, "/", &dir), "opendir");
    static uint8_t buffer[8 * 1024 * 1024];
    while (SPIFFS_readdir(&dir, &entry) && *count < 32) {
        file_copy_t *f = &files[(*count)++];
        snprintf(f->name, sizeof(f->name), "%s", (const char *)entry.name);
        f->size = read_file(f->name, buffer, sizeof(buffer));
        f->data = f->size > 0 ? malloc((size_t)f->size) : NULL;
        if (f->data) memcpy(f->data, buffer, (size_t)f->size);
    }
    SPIFFS_closedir(&dir);
    SPIFFS_clearerr(&fs);
}

/* Review C1: after a reset in an unconfirmed window the old image's mount
 * erases the block; every file it can open reads exactly as before the
 * round, so no reader of any image (2.5.2's and 2.4.12's included) sees a
 * read error or a changed byte. */
static void check_files_unchanged(const char *what)
{
    file_copy_t now[32];
    uint32_t count = 0;
    capture_files(now, &count);
    CHECK(count == round_file_count, "%s: %u files listed, %u before the round", what, count, round_file_count);
    for (uint32_t i = 0; i < count; ++i) {
        const file_copy_t *before = NULL;
        for (uint32_t k = 0; k < round_file_count && !before; ++k)
            if (!strcmp(round_files[k].name, now[i].name)) before = &round_files[k];
        CHECK(before, "%s: %s appeared", what, now[i].name);
        CHECK(now[i].size == before->size && (now[i].size <= 0 || !memcmp(now[i].data, before->data, (size_t)now[i].size)),
              "%s: %s reads %" PRId64 " bytes (%" PRId64 " before the round) or other bytes", what, now[i].name,
              now[i].size, before->size);
    }
    free_files(now, &count);
}

static void round_began(void)
{
    if (!round_store) round_store = malloc(8U * 1024 * 1024 + 64U * BLOCK);
    memcpy(round_store, flash, flash_size);
    round_active = true;
    round_start_ops = flash_ops;
    first_window_ops = 0;
    if (round_files_wanted) capture_files(round_files, &round_file_count);
}

static void check_round(const uint8_t *before, const uint8_t *after)
{
    uint32_t pages = flash_size / PAGE, blocks = flash_size / BLOCK;
    uint8_t *referenced = calloc(pages, 1), *headered = calloc(0x8000, 1);
    uint16_t *named_by = calloc(pages, sizeof(uint16_t));
    for (uint32_t pix = 0; pix < pages; ++pix) {
        const uint8_t *page = before + pix * PAGE;
        uint16_t obj = (uint16_t)(page[0] | (page[1] << 8)), span = (uint16_t)(page[2] | (page[3] << 8));
        if (pix % 16 == 0 || obj == 0xFFFF || !(obj & 0x8000) || !(page[4] & SPIFFS_PH_FLAG_DELET)) continue;
        if (span == 0) headered[obj & 0x7FFF] = 1;
        uint32_t table = span == 0 ? 49 : 8, entries = span == 0 ? 103 : 124;
        for (uint32_t i = 0; i < entries; ++i) {
            uint16_t ref = (uint16_t)(page[table + i * 2] | (page[table + i * 2 + 1] << 8));
            if (ref >= pages || ref % 16 == 0) continue;
            referenced[ref] = 1;
            if (!named_by[ref]) named_by[ref] = (uint16_t)((obj & 0x7FFF) + 1);
        }
    }
    for (uint32_t b = 0; b < blocks; ++b) {
        const uint8_t *was = before + b * BLOCK, *is = after + b * BLOCK;
        if (!memcmp(was, is, BLOCK)) continue;
        round_checks++;
        CHECK(srx_magic_strong(blocks, b), "round: block %u has a weak magic and was reformatted", b);
        bool magic = srx_get16(is + 252) == srx_spiffs_magic(blocks, b);
        bool closed = magic && srx_erased(is, 252);
        for (uint32_t i = 256; closed && i < BLOCK; i += PAGE) closed = srx_erased(is + i, PAGE);
        /* The block's newest record says what the round did with it. */
        static uint8_t sector[4096];
        srx_record_t rec, newest;
        bool found = false;
        for (uint32_t slot = 0; slot < REDO_BYTES / 4096; ++slot) {
            memcpy(sector, nvs.redo + slot * 4096, 4096);
            if (srx_decode(sector, &rec, NULL) && rec.block == b && (!found || rec.sequence > newest.sequence)) {
                newest = rec;
                found = true;
            }
        }
        if (!magic || (closed && found && newest.done == 0xFFFFFFFFU)) {
            /* Only a block that would not verify: closed as SPIFFS's own
             * erase leaves a block, or (when it would not close) without its
             * magic; its record a source, or (round 2 review, R2-4: the
             * source bit would not take) open with its attempts used up or
             * the journal saying SOURCES. (A done block whose every entry
             * was dropped reads closed too: it is checked below.) */
            bool source = found && newest.done == 0xFFFFFFFFU &&
                (srx_is_source(&newest) || srx_attempts_used(newest.attempts) >= SRX_ATTEMPTS ||
                 (journal_flags() & SRP_RECLAIM_SOURCES));
            CHECK(source, "round: block %u ends %s and no source record", b, magic ? "closed" : "without its magic");
            CHECK(!magic || srx_get16(is + 254) != 0xFFFF, "round: block %u closed without an erase count", b);
            round_failed_blocks++;
            round_closed_blocks += magic;
            continue;
        }
        /* Reformatted: its record done (or a source whose `done` would not
         * take, the block holding its target). */
        CHECK(found && (newest.done != 0xFFFFFFFFU || srx_is_source(&newest)),
              "round: block %u changed and its record is neither done nor a source", b);
        CHECK(srx_get16(is + 252) == srx_spiffs_magic(blocks, b), "round: block %u ends without its magic", b);
        CHECK(!memcmp(was + 30, is + 30, 252 - 30), "round: block %u's other lookup bytes changed", b);
        bool later_kept = false;
        for (uint32_t e = SRX_ENTRIES; e-- > 0;) {
            uint32_t pix = b * 16 + 1 + e;
            uint16_t lu_was = srx_get16(was + e * 2), lu_is = srx_get16(is + e * 2);
            const uint8_t *page_was = before + pix * PAGE, *page_is = after + pix * PAGE;
            bool dropped = lu_is == 0xFFFF && srx_erased(page_is, PAGE) &&
                !(lu_was == 0xFFFF && srx_erased(page_was, PAGE));
            if (dropped) {
                /* R9: only a deleted page no valid index page names. */
                CHECK(lu_was == 0 && !(page_was[4] & SPIFFS_PH_FLAG_DELET) && !referenced[pix],
                      "round: block %u entry %u dropped a page something reads (lookup %04x flags %02x named %d)", b,
                      e, lu_was, page_was[4], referenced[pix]);
                CHECK(e || !later_kept, "round: block %u entry 0 dropped with a later entry kept", b);
                continue;
            }
            CHECK(lu_is == lu_was && !memcmp(page_was, page_is, PAGE), "round: block %u entry %u kept but changed", b, e);
            bool free_entry = lu_was == 0xFFFF && srx_erased(page_was, PAGE);
            if (!free_entry) later_kept = true;
            if (e == 0 && lu_was == 0 && !(page_was[4] & SPIFFS_PH_FLAG_DELET) && !referenced[pix] && !free_entry)
                round_entry0_kept++;
            if (!round_unconfirmed || free_entry) continue;
            /* Review C1: an unconfirmed window holds no page of any file. */
            uint16_t obj = srx_get16(page_was);
            bool header_page = obj != 0xFFFF && (obj & 0x8000) && (page_was[4] & SPIFFS_PH_FLAG_DELET);
            bool readable = lu_was != 0 ? headered[lu_was & 0x7FFF] != 0 : named_by[pix] && headered[named_by[pix] - 1];
            CHECK(!header_page && !readable && !(referenced[pix] && named_by[pix] && headered[named_by[pix] - 1]),
                  "round: unconfirmed window on block %u kept a page of a file (entry %u lookup %04x)", b, e, lu_was);
        }
    }
    free(referenced);
    free(headered);
    free(named_by);
}

static void round_ended(void)
{
    worn_address = worn_address2 = UINT32_MAX;
    if (!round_active) return;
    round_active = false;
    round_end_ops = flash_ops;
    uint32_t before = round_checks;
    check_round(round_store, flash);
    round_checks_total += round_checks - before;
}

/* A count of the gap records that name a model file. */
static uint32_t model_gaps(void)
{
    srp_gaps_t gaps = gaps_now();
    uint32_t n = 0;
    for (uint32_t g = 0; g < gaps.count; ++g)
        for (uint32_t i = 0; i < model_count; ++i) n += !strcmp(gaps.gap[g].name, model[i].name);
    return n;
}

/* Every model file reads exactly its bytes (gap records or not). */
static void check_models_exact(const char *what)
{
    static uint8_t buffer[8 * 1024 * 1024];
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) {
        const model_file_t *m = &model[i];
        int64_t got = read_file(m->name, buffer, sizeof(buffer));
        CHECK(got == (int64_t)m->size && !memcmp(buffer, m->data, m->size), "%s: %s reads %" PRId64 " of %u bytes", what,
              m->name, got, m->size);
    }
    SPIFFS_unmount(&fs);
}

/* ---- builders ------------------------------------------------------------ */
static uint32_t p02_span, p02_spans;

/* P02's damage on a built store (the pending queue and the outbox whose
 * cursor 2.7.5 reset), with no erased page left; `old_name` is the file whose
 * removal stopped. */
static void p02_damage(model_file_t *pending, model_file_t *live, const char *old_name)
{
    /* P02's cursor reset: one past its file. */
    set_cursor("add_legacy", "live", live->size + 500, 0, 3);
    uint32_t spans = pending->size / SPAN;
    uint16_t obj = object_of(pending->name);
    uint32_t header = header_of(pending->name);
    SPIFFS_unmount(&fs);
    /* The free pages, block by block. */
    uint32_t pages = flash_size / PAGE, free_count = 0;
    uint32_t *free_pages = calloc(pages, sizeof(uint32_t));
    for (uint32_t pix = 0; pix < pages; ++pix)
        if (pix % 16 && rd16(lookup_address(pix)) == 0xFFFF) free_pages[free_count++] = pix;
    /* Damaged spans in one index page, each with its referenced page deleted
     * and a valid copy elsewhere (an interrupted GC move): 2.7.5 refers each
     * to its copy (P02's 153 rewritten spans are 51 such, counted in each of
     * three attempts) and then needs one page, the index page's rewrite. */
    p02_spans = flash_size >= 8U * 1024 * 1024 ? 51 : 12;
    if (p02_spans + 6 > free_count) p02_spans = free_count > 6 ? free_count - 6 : 1;
    uint32_t first = 103 + 124 * (1 + rnd() % ((spans - 103) / 124 - 1)) + 2;
    if (first + p02_spans > 103 + 124 * ((first - 103) / 124 + 1)) p02_spans = 103 + 124 * ((first - 103) / 124 + 1) - first;
    p02_span = first;
    /* The queue's drain has settled rows before the damage, not past it. */
    uint32_t cursor = row_boundary_after(pending, first * SPAN / 2);
    set_cursor("legacy_queues", "ords_pending", cursor, prefix_crc(pending, cursor), 5);
    /* Live pages, one first in every block that has a free page (no block
     * left all deleted, which 2.7.5 could erase, even once the strays go):
     * the copies (and, past them, more copies of the first damaged span),
     * then four strays past the end (an append that never committed) and a
     * stale header copy with an older size (a page move that stopped). */
    uint8_t pin[PAGE];
    memcpy(pin, flash + rd16(entry_address(pending->name, first)) * PAGE, PAGE);
    uint8_t *taken = calloc(pages, 1);
    uint32_t copies = 0, extras = 0;
    for (int pass = 0; pass < 2; ++pass)
        for (uint32_t i = 0; i < free_count; ++i) {
            uint32_t pix = free_pages[i];
            bool first_in_block = i == 0 || free_pages[i - 1] / 16 != pix / 16;
            if (taken[pix] || (pass == 0) != first_in_block) continue;
            uint8_t *page = flash + pix * PAGE;
            if (copies < p02_spans) {
                uint32_t head = rd16(entry_address(pending->name, first + copies++));
                memcpy(page, flash + head * PAGE, PAGE);
                wr16(lookup_address(pix), obj);
            } else if (pass == 0) {
                memcpy(page, pin, PAGE);
                wr16(lookup_address(pix), obj);
            } else if (extras < 4) {
                memset(page, 0xFF, PAGE);
                wr16(pix * PAGE, obj);
                wr16(pix * PAGE + 2, (uint16_t)(spans + 2 + extras));
                page[4] = 0xFC;
                memset(page + 5, 'x', 100);
                wr16(lookup_address(pix), obj);
                extras++;
                taken[pix] = 2;
            } else if (extras == 4) {
                memcpy(page, flash + header * PAGE, PAGE);
                uint32_t older = pending->size - 50;
                memcpy(page + 8, &older, 4);
                wr16(lookup_address(pix), (uint16_t)(obj | 0x8000));
                extras++;
                taken[pix] = 2;
            } else {
                continue;
            }
            if (!taken[pix]) taken[pix] = 1;
        }
    CHECK(copies == p02_spans && extras == 5, "p02: %u copies, %u strays placed", copies, extras);
    for (uint32_t k = 0; k < p02_spans; ++k) {
        uint32_t head = rd16(entry_address(pending->name, first + k));
        flag_deleted(head);
        taken[head] = 3; /* the damage itself: never a pin's place below */
    }
    /* Every other free page a deleted copy: no erased page anywhere. */
    for (uint32_t i = 0; i < free_count; ++i) {
        uint32_t pix = free_pages[i];
        if (taken[pix]) continue;
        memcpy(flash + pix * PAGE, pin, PAGE);
        wr16(lookup_address(pix), obj);
        flag_deleted(pix);
    }
    /* No block may end all deleted once 2.7.5 drops the orphan index pages,
     * the strays and the duplicate: P02 logged LEVEL2_SKIPPED, no removal
     * there let a block be erased. A block that would is pinned by one more
     * copy over a deleted page. */
    uint16_t old_obj = (uint16_t)(rd16(header_of(old_name) * PAGE) & 0x7FFF);
    for (uint32_t b = 0; b < pages / 16; ++b) {
        bool live = false;
        uint32_t spare = 0;
        for (uint32_t e = 1; e < 16 && !live; ++e) {
            uint32_t pix = b * 16 + e;
            uint16_t lu = rd16(lookup_address(pix));
            if (lu == 0 && !spare && taken[pix] != 3) spare = pix;
            live = lu != 0 && lu != 0xFFFF && lu != (uint16_t)(old_obj | 0x8000) && taken[pix] != 2;
        }
        if (live || !spare) continue;
        memcpy(flash + spare * PAGE, pin, PAGE);
        wr16(lookup_address(spare), obj);
    }
    free(taken);
    free(free_pages);
}

/* Peshawar-02 as 2.7.5 left it (20 October 2026): no erased page, every
 * block holding a page of the damaged pending queue or of a file whose
 * removal stopped after its header (its orphan index pages over a fifth of
 * the store), deleted header and index copies everywhere, and a damaged span
 * whose referenced page is deleted while valid copies of it remain, so
 * normalization needs exactly one page (the index rewrite). */
static void build_p02(void)
{
    model_reset();
    row_seq = 4000;
    mount_plain();
    char row[600], chunk[4600];
    /* The file that will be half removed, written alone in small appends
     * (each moves its header and index page): its blocks hold its data pages
     * and those deleted copies only. */
    uint32_t written = 0;
    while (written < flash_size / 5) {
        size_t n = json_row(row, sizeof(row));
        CHECK(append_bytes("/blocked_identity.jsonl", row, (uint32_t)n), "p02: the old file");
        written += (uint32_t)n;
    }
    model_file_t *pending = new_model("/pending.jsonl", true, false);
    model_file_t *acked = new_model("/acked_uids.txt", false, true);
    model_file_t *live = new_model("/add_live.jsonl", true, false);
    model_file_t *outbox = new_model("/add_pending.jsonl", true, false);
    model_file_t *commands = new_model("/processed_commands.txt", false, true);
    u32_t total = 0, used = 0;
    for (;;) {
        SPIFFS_info(&fs, &total, &used);
        if (used >= total / 100 * 60) break;
        size_t at = 0;
        while (at < 1800) at += json_row(chunk + at, sizeof(chunk) - at);
        model_append(pending, chunk, (uint32_t)at);
        if (rnd() % 3 == 0) model_append_ids(acked, 2);
        if (rnd() % 7 == 0) model_append(live, row, (uint32_t)json_row(row, sizeof(row)));
        if (rnd() % 9 == 0) model_append(outbox, row, (uint32_t)json_row(row, sizeof(row)));
        if (rnd() % 13 == 0) model_append_ids(commands, 1);
    }
    p02_damage(pending, live, "/blocked_identity.jsonl");
    /* The old file's removal stopped after its header. */
    flag_deleted(header_of("/blocked_identity.jsonl"));
}

/* 2.7.5, released, on the built store: P02's report. */
static void p02_as_275(void)
{
    srr_report_t old;
    srr_config_t config = repair_config();
    config.pending_verify = true;
    logged_count = 0;
    repair_boot_275(&old, &config);
    CHECK(old.result == SRR_RESULT_DEFERRED && !strcmp(old.error_stage, "full") && old.error_code == SPIFFS_ERR_FULL &&
              old.room_needed == 1 && old.room_erased == 0 && old.orphan_index_removed > 0 && old.check_skipped &&
              old.blocks_compacted == 0 && old.reserve_pages == 0 && code_logged("STORAGE_REPAIR_LEVEL2_SKIPPED") &&
              code_logged("STORAGE_REPAIR_FULL") && old.spans_rewritten == 3 * p02_spans && old.strays_removed == 4 &&
              old.duplicates_removed == 1 && old.cursors_reset == 1 && old.gaps == 0,
          "p02: 2.7.5 %s at %s (%ld), %u pages needed, %u erased, %u orphans, %u spans rewritten (%u damaged), %u "
          "strays, %u duplicates, %u cursors reset, %u gaps, check skipped %d, level 2 skipped %d",
          srr_result_name(old.result), old.error_stage, (long)old.error_code, old.room_needed, old.room_erased,
          old.orphan_index_removed, old.spans_rewritten, p02_spans, old.strays_removed, old.duplicates_removed,
          old.cursors_reset, old.gaps, old.check_skipped, code_logged("STORAGE_REPAIR_LEVEL2_SKIPPED"));
}

/* Every record in the redo area. */
static uint32_t area_records(uint32_t *unconfirmed, uint32_t *sources, uint32_t *open)
{
    static uint8_t sector[4096];
    uint32_t n = 0;
    *unconfirmed = *sources = *open = 0;
    for (uint32_t slot = 0; slot < REDO_BYTES / 4096; ++slot) {
        srx_record_t r;
        memcpy(sector, nvs.redo + slot * 4096, 4096);
        if (!srx_decode(sector, &r, NULL)) continue;
        n++;
        *unconfirmed += (r.flags & SRX_FLAG_UNCONFIRMED) != 0;
        *sources += srx_is_source(&r);
        *open += r.done == 0xFFFFFFFFU && !srx_is_source(&r);
    }
    return n;
}

static void run_reclaim_p02(uint32_t kib, bool unconfirmed, bool unheld)
{
    seed(61000 + kib + (unconfirmed ? 7 : 0) + (unheld ? 3 : 0));
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    srr_config_t config = config_2760(unconfirmed);
    if (unheld) config.hold_budget = 0; /* 13FL: custody over the hold budget; the round needs no hold */
    round_checks = 0;
    round_unconfirmed = unconfirmed;
    logged_count = 0;
    srr_report_t report;
    repair_boot(&report, &config);
    round_unconfirmed = false;
    const srr_reclaim_report_t *rc = &report.reclaim;
    CHECK(report.result == SRR_RESULT_REPAIRED && report.level == 3 && rc->blocks > 0 && !strcmp(rc->trigger, "full") &&
              report.gaps == 0 && round_checks == rc->blocks && report.check_passes >= 1 &&
              rc->area == SRX_AREA_READY && code_logged("STORAGE_REPAIR_RECLAIM"),
          "p02 %u KiB%s%s: %s at %s, level %u, %u blocks (%u checked), trigger %s, %u gaps, %u passes",
          kib, unconfirmed ? " unconfirmed" : "", unheld ? " unheld" : "", srr_result_name(report.result),
          report.error_stage, report.level, rc->blocks, round_checks, rc->trigger, report.gaps, report.check_passes);
    uint32_t pending = 0, sources = 0, open = 0, records = area_records(&pending, &sources, &open);
    CHECK(records >= rc->blocks && !open && !sources && (!unconfirmed || pending == records),
          "p02: %u records, %u unconfirmed, %u sources, %u open", records, pending, sources, open);
    /* Round 1 review, low 1: ADD gets the boot's report over every run: the
     * first run's 2.7.5 work and room figures (P02's field report: 3 x 51
     * spans, 1 page needed, 0 erased) and the second run's spans too. */
    CHECK(report.runs == 2 && report.spans_rewritten >= 4 * p02_spans && report.room_needed == 1 &&
              report.room_erased == 0 && report.check_skipped == false,
          "p02: the boot's report: %u runs, %u spans rewritten (%u damaged), %u pages needed, %u erased", report.runs,
          report.spans_rewritten, p02_spans, report.room_needed, report.room_erased);
    if (unconfirmed) CHECK(rc->unconfirmed_blocks > 0, "p02 unconfirmed: no block holding nothing any image reads");
    if (kib >= 8192) CHECK(rc->low_magic_blocks > 0, "p02 8 MiB: no weak-magic block kept out");
    verify_reclaimed("p02");
    printf("p02 %u KiB%s%s: 2.7.5 DEFERRED (full, 1 page needed, 0 erased, %u spans rewritten, 4 strays, 1 "
           "duplicate, 1 cursor reset); 2.7.6 reformatted %u blocks (%u deleted "
           "pages erased, %u kept; %u candidates, %u holding nothing any image reads, %u custody, %u index, %u "
           "excluded, %u weak magic) in %u ms, then REPAIRED with no gap\n",
           kib, unconfirmed ? " unconfirmed" : " confirmed", unheld ? " with no holds" : "", 3 * p02_spans, rc->blocks,
           rc->pages_freed, rc->pages_kept, rc->candidates, rc->unconfirmed_blocks, rc->custody_blocks,
           rc->index_blocks, rc->excluded_blocks, rc->low_magic_blocks, rc->ms);
}

/* Round 1 review, low 1: 2.7.6 straight on P02's store (2.7.5 never ran):
 * the boot's report keeps the first run's strays, duplicate and cursor
 * reset, and the file entry of the queue whose cursor it reset. */
static void run_reclaim_p02_direct(uint32_t kib)
{
    seed(61050 + kib);
    nvs_clear();
    format_flash(kib);
    build_p02();
    srr_config_t config = config_2760(false);
    srr_report_t report;
    repair_boot(&report, &config);
    bool reset_noted = false;
    for (uint32_t i = 0; i < report.file_count; ++i)
        reset_noted |= !strcmp(report.file[i].name, "/add_live.jsonl") &&
            (report.file[i].actions & SRR_FILE_CURSOR_RESET);
    CHECK(report.result == SRR_RESULT_REPAIRED && report.runs == 2 && report.strays_removed >= 4 &&
              report.duplicates_removed >= 1 && report.cursors_reset >= 1 && report.orphan_index_removed > 0 &&
              report.room_needed == 1 && reset_noted && report.reclaim.blocks > 0,
          "p02 direct: %s, %u runs, %u strays, %u duplicates, %u cursors reset (noted %d), %u orphans, %u pages needed",
          srr_result_name(report.result), report.runs, report.strays_removed, report.duplicates_removed,
          report.cursors_reset, reset_noted, report.orphan_index_removed, report.room_needed);
    printf("p02 %u KiB, 2.7.6 first: the boot's report keeps the first run's strays, duplicate, cursor reset and "
           "room figures over its %u runs (%u strays, %u spans rewritten in all)\n", kib, report.runs,
           report.strays_removed, report.spans_rewritten);
}

/* ---- 2.7.7: Peshawar-02 as its first 2.7.6 boot found it ------------------ */
/* P02's first 2.7.6 boot (21 October 2026, unconfirmed, from 2.5.2) found no
 * block an unconfirmed image may take and 67 a confirmed one may: 1,817
 * blocks excluded, 88 with an index page, 68 with a weak magic, 34 pages
 * short. The 2.7.6 model above wrote the half-removed file alone and then
 * deleted its header, so its pages were leaked (read by nothing) and
 * clustered: 731 usable blocks. On the device nearly every block holds a page
 * the planner calls EXCLUDED. This builder writes P02's history interleaved,
 * as 2.5.2 appended for months: each step a pending chunk, acked IDs and rows
 * of the ORDS rewrite stage that was later removed, with quiet stretches of
 * pending alone; then 2.7.6's P02 damage; then the removal stopped after
 * flagging the stage's header (E2: a hidden header, its pages EXCLUDED). The
 * variants: E1 alone (the header deleted, the never-windowed file denser),
 * every custody block dropping exactly one page (the analytic worst case),
 * and too few custody pages for the shortfall. */
enum { P02F_HIDDEN = 0, P02F_UNWINDOWED, P02F_MIN_GAIN, P02F_SHORT };
static uint32_t p02field_variant;
#define P02F_REMOVED "/pending.tmp"
#define P02F_TAG 0x2770U
#define P02F_SHORT_BLOCKS 20U

static void p02field_rows(const char *name, uint32_t bytes)
{
    char row[600];
    for (uint32_t done = 0; done < bytes;) {
        size_t n = json_row(row, sizeof(row));
        CHECK(append_bytes(name, row, (uint32_t)n), "p02field: %s %d", name, SPIFFS_errno(&fs));
        done += (uint32_t)n;
    }
}

static void build_p02field(void)
{
    model_reset();
    row_seq = 9000;
    mount_plain();
    model_file_t *pending = new_model("/pending.jsonl", true, false);
    model_file_t *acked = new_model("/acked_uids.txt", false, true);
    model_file_t *live = new_model("/add_live.jsonl", true, false);
    model_file_t *outbox = new_model("/add_pending.jsonl", true, false);
    model_file_t *commands = new_model("/processed_commands.txt", false, true);
    bool e1 = p02field_variant == P02F_UNWINDOWED;
    char row[600], chunk[1600];
    uint32_t step = 0, quiet = 0, next_quiet = 25 + rnd() % 4;
    u32_t total = 0, used = 0;
    for (;;) {
        SPIFFS_info(&fs, &total, &used);
        if (used >= total / 100 * 62) break;
        size_t at = 0;
        while (at < 1000) at += json_row(chunk + at, sizeof(chunk) - at);
        model_append(pending, chunk, (uint32_t)at);
        ++step;
        if (quiet) {
            --quiet;
            continue;
        }
        if (step >= next_quiet) {
            /* A quiet stretch: pending alone for four steps. */
            quiet = 3;
            next_quiet = step + 4 + 25 + rnd() % 4;
            continue;
        }
        model_append_ids(acked, e1 ? 9 : 6);
        p02field_rows(P02F_REMOVED, e1 ? 400 : 550);
        if (rnd() % 7 == 0) model_append(live, row, (uint32_t)json_row(row, sizeof(row)));
        if (rnd() % 9 == 0) model_append(outbox, row, (uint32_t)json_row(row, sizeof(row)));
        if (rnd() % 13 == 0) model_append_ids(commands, 1);
    }
    p02_damage(pending, live, P02F_REMOVED);
    /* The stage's removal stopped after flagging its header (E2): hidden,
     * its pages still on flash. E1: the header deleted, its pages leaked. */
    if (e1) flag_deleted(header_of(P02F_REMOVED));
    else hide_header(P02F_REMOVED);
}

/* The blocks a confirmed round may take, as the planner sees them on this
 * model (the harness's own reading of section 6.1): a valid strong magic, a
 * droppable page past the entry-0 rule, no header or index page, and every
 * kept page either a non-last data page of /pending.jsonl (custody) or
 * leaked (no header anywhere). `blocks` gets 1 per such block. */
static uint32_t p02field_custody_blocks(uint8_t *blocks, uint16_t *gain)
{
    uint32_t pages = flash_size / PAGE, count = 0;
    uint8_t *referenced = calloc(pages, 1), *headered = calloc(0x8000, 1);
    for (uint32_t pix = 0; pix < pages; ++pix) {
        const uint8_t *page = flash + pix * PAGE;
        uint16_t obj = (uint16_t)(page[0] | (page[1] << 8)), span = (uint16_t)(page[2] | (page[3] << 8));
        if (pix % 16 == 0 || obj == 0xFFFF || !(obj & 0x8000) || !(page[4] & SPIFFS_PH_FLAG_DELET)) continue;
        if (span == 0) headered[obj & 0x7FFF] = 1;
        uint32_t table = span == 0 ? 49 : 8, entries = span == 0 ? 103 : 124;
        for (uint32_t i = 0; i < entries; ++i) {
            uint16_t ref = (uint16_t)(page[table + i * 2] | (page[table + i * 2 + 1] << 8));
            if (ref < pages && ref % 16) referenced[ref] = 1;
        }
    }
    mount_plain();
    uint16_t pending = object_of("/pending.jsonl");
    spiffs_stat st;
    CHECK(SPIFFS_stat(&fs, "/pending.jsonl", &st) == SPIFFS_OK, "p02field: stat pending");
    SPIFFS_unmount(&fs);
    uint32_t last_span = (st.size + SPAN - 1) / SPAN - 1;
    for (uint32_t b = 0; b < flash_size / BLOCK; ++b) {
        blocks[b] = 0;
        if (gain) gain[b] = 0;
        if (srx_get16(flash + b * BLOCK + 252) != srx_spiffs_magic(flash_size / BLOCK, b) ||
            !srx_magic_strong(flash_size / BLOCK, b))
            continue;
        bool usable = true, custody = false;
        uint32_t dropped = 0;
        bool later_kept = false, drop0 = false;
        for (uint32_t e = SRX_ENTRIES; e-- > 0 && usable;) {
            uint32_t pix = b * 16 + 1 + e;
            const uint8_t *page = flash + pix * PAGE;
            uint16_t lu = rd16(lookup_address(pix)), obj = (uint16_t)(page[0] | (page[1] << 8));
            uint16_t span = (uint16_t)(page[2] | (page[3] << 8));
            if (lu == 0xFFFF && srx_erased(page, PAGE)) continue;
            if (lu == 0 && !(page[4] & SPIFFS_PH_FLAG_DELET) && !referenced[pix]) {
                if (e == 0) drop0 = true;
                else dropped++;
                continue;
            }
            later_kept = true;
            if (lu == 0 || lu == 0xFFFF || (obj & 0x8000) || lu != obj) usable = false;
            else if (!headered[lu & 0x7FFF]) continue; /* leaked */
            else if (lu == pending && span < last_span) custody = true;
            else usable = false;
        }
        if (drop0 && !later_kept) dropped++;
        if (!usable || !custody || !dropped) continue;
        blocks[b] = 1;
        if (gain) gain[b] = (uint16_t)dropped;
        count++;
    }
    free(referenced);
    free(headered);
    return count;
}

/* Every droppable page of a custody block but `keep` becomes a leaked page
 * (an object with no header anywhere): kept, read by nothing. */
static void p02field_leak_drops(uint32_t b, uint32_t keep)
{
    uint8_t *referenced = NULL;
    (void)referenced;
    uint32_t kept = 0;
    for (uint32_t e = SRX_ENTRIES; e-- > 0;) {
        uint32_t pix = b * 16 + 1 + e;
        uint8_t *page = flash + pix * PAGE;
        if (rd16(lookup_address(pix)) != 0 || (page[4] & SPIFFS_PH_FLAG_DELET)) continue;
        if (e > 0 && kept < keep) {
            ++kept;
            continue;
        }
        /* A finalized data page of object 0x7EEE, never headered. NOR: the
         * page is rewritten from erased in the model's flash directly. */
        memset(page, 0xFF, PAGE);
        wr16(pix * PAGE, 0x7EEE);
        wr16(pix * PAGE + 2, (uint16_t)(e + 1));
        page[4] = 0xFC;
        memset(page + 5, 'l', 64);
        wr16(lookup_address(pix), 0x7EEE);
    }
}

static srr_config_t config_2770(bool unconfirmed)
{
    srr_config_t config = repair_config();
    config.image_tag = P02F_TAG; /* confirmation keeps the image's tag (critique 4) */
    config.pending_verify = unconfirmed;
    config.rollback_tag = unconfirmed ? 0x2520 : 0;
    /* The exception's round boot takes custody blocks only with every custody
     * file held (critique 11). */
    config.custody_needs_hold = !unconfirmed;
    return config;
}

/* The glue's redo-area test on the unconfirmed boot (critique 3): CLAIMED in
 * the journal first, then one slot programmed and erased. */
static srx_test_t p02field_area_test(void)
{
    static uint8_t scratch[4096];
    srp_reclaim_t journal;
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) != 1 ||
        !srp_reclaim_valid(&journal))
        memset(&journal, 0, sizeof(journal));
    if (!(journal.flags & SRP_RECLAIM_CLAIMED)) {
        journal.flags |= SRP_RECLAIM_CLAIMED;
        srp_reclaim_seal(&journal);
        if (!port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal))) return SRX_TEST_NOT_RUN;
    }
    return srx_area_test(&redo_io, &boot_area, scratch, NULL);
}

/* 2.7.4's inventory as the VFS would read the store: every file span by span
 * (sx_inventory_t, as storage_repair.c hands it to the gate). */
static void p02field_inventory(sx_inventory_t *v)
{
    static uint8_t buffer[SPAN];
    memset(v, 0, sizeof(*v));
    mount_plain();
    spiffs_DIR dir;
    struct spiffs_dirent entry;
    CHECK(SPIFFS_opendir(&fs, "/", &dir), "p02field: opendir");
    v->done = v->complete = true;
    while (SPIFFS_readdir(&dir, &entry)) {
        v->files++;
        CHECK(v->count < SX_INVENTORY_FILES, "p02field: more than %u files", SX_INVENTORY_FILES);
        sx_inventory_file_t *f = &v->file[v->count++];
        snprintf(f->name, sizeof(f->name), "%.32s", (const char *)entry.name + 1);
        f->bytes = entry.size;
        spiffs_file fd = SPIFFS_open(&fs, (const char *)entry.name, SPIFFS_O_RDONLY, 0);
        if (fd < 0) {
            f->first_error = ENOENT;
            f->unreadable_bytes = entry.size;
            f->other_unreadable = true;
            SPIFFS_clearerr(&fs);
            continue;
        }
        for (uint32_t at = 0; at < entry.size; at += SPAN) {
            uint32_t want = entry.size - at < SPAN ? entry.size - at : SPAN;
            SPIFFS_lseek(&fs, fd, (s32_t)at, SPIFFS_SEEK_SET);
            if (SPIFFS_read(&fs, fd, buffer, (s32_t)want) == (s32_t)want) continue;
            SPIFFS_clearerr(&fs);
            if (!f->first_error) {
                f->first_error = EIO;
                f->first_error_offset = at;
            }
            f->unreadable_bytes += want;
            f->unreadable_spans++;
            f->last_error_offset = at;
            f->span_digest = span_digest_add(f->span_digest, at / SPAN);
        }
        SPIFFS_close(&fs, fd);
        v->unreadable_files += f->first_error != 0;
    }
    SPIFFS_closedir(&dir);
    SPIFFS_clearerr(&fs);
    SPIFFS_unmount(&fs);
}

/* The gate's storage terms on the unconfirmed boot's own report, with ADD,
 * the OTA path and the runtime as P02's 2.7.6 boot had them. */
static sx_reason_t p02field_gate(const srr_report_t *report, srx_test_t test)
{
    static sx_repair_t repair;
    static sx_inventory_t inventory;
    static sx_runtime_t runtime;
    sx_from_srr(report, &repair);
    repair.storage_allowed = !mount_blocked;
    repair.pending_verify = true;
    snprintf(repair.area_test, sizeof(repair.area_test), "%s", srx_test_name(test));
    p02field_inventory(&inventory);
    memset(&runtime, 0, sizeof(runtime));
    runtime.qs_known = runtime.qs_observed = runtime.qs_available = runtime.led_known = true;
    runtime.probe_error = ENOENT;
    runtime.probe_operation = "persistence_open_write";
    runtime.spiffs_mounted = runtime.health_known = runtime.add_connected = true;
    runtime.capability_accepted = runtime.ota_precheck_ok = runtime.passive_slot_ok = true;
    runtime.health_blockers = SX_H_QS_RECOVERY | SX_H_QS_PERSISTENCE | SX_H_QS_PROBE_ERROR;
    runtime.internal_largest = 63488;
    sx_input_t input = {.production_role = true, .running_pending = true, .ota_deployment = true,
                        .repair = &repair, .inventory = &inventory, .runtime = &runtime};
    return sx_evaluate(&input);
}

/* The store as P02's 2.7.6 boot left it, for the confirmed boot: built, the
 * released 2.7.5 run on it (P02's 20 October report), the variant's
 * conversion, the unconfirmed 2.7.7 boot (deferred, nothing taken) and the
 * glue's area test. Returns that boot's report. */
static uint8_t *p02field_custody; /* the custody blocks before the confirmed boot */
static srr_report_t p02field_unconfirmed;
static void p02field_prepare(void)
{
    build_p02field();
    p02_as_275();
    uint32_t blocks = flash_size / BLOCK;
    if (!p02field_custody) p02field_custody = malloc(8U * 1024U * 1024U / BLOCK);
    uint16_t *gain = calloc(blocks, sizeof(uint16_t));
    p02field_custody_blocks(p02field_custody, gain);
    if (p02field_variant == P02F_MIN_GAIN || p02field_variant == P02F_SHORT) {
        uint32_t kept = 0;
        for (uint32_t b = 0; b < blocks; ++b) {
            if (!p02field_custody[b]) continue;
            bool keep = p02field_variant == P02F_MIN_GAIN || kept < P02F_SHORT_BLOCKS;
            p02field_leak_drops(b, keep ? 1 : 0);
            kept += keep;
        }
        p02field_custody_blocks(p02field_custody, gain);
    }
    free(gain);
    /* P02FIELD_IMAGE=<path>: the store as P02's 2.7.5 boot left it, the
     * hidden-header model at 8 MiB, for the runbook's bench run (an image of
     * the 8 MiB storage partition, written raw at 0x7A0000). */
    const char *image = getenv("P02FIELD_IMAGE");
    if (image && p02field_variant == P02F_HIDDEN && flash_size == 8U * 1024U * 1024U) {
        FILE *out = fopen(image, "wb");
        CHECK(out && fwrite(flash, 1, flash_size, out) == flash_size && fclose(out) == 0,
              "p02field: could not write %s", image);
        printf("p02field: the store as P02's 2.7.5 boot left it written to %s\n", image);
    }
    srr_config_t config = config_2770(true);
    uint32_t checked = round_checks;
    logged_count = 0;
    repair_boot(&p02field_unconfirmed, &config);
    /* An unconfirmed boot takes no custody block (2.7.6 section 8). */
    CHECK(round_checks == checked && !p02field_unconfirmed.reclaim.blocks,
          "p02field: the unconfirmed boot reformatted %u blocks", p02field_unconfirmed.reclaim.blocks);
    uint32_t pending_records = 0, sources = 0, open = 0;
    CHECK(!area_records(&pending_records, &sources, &open), "p02field: the unconfirmed boot wrote a record");
}

/* ---- cuts at every write of the round ------------------------------------ */
typedef struct {
    uint8_t *flash;
    __typeof__(nvs) *nvs;
} snapshot_t;
static snapshot_t snapshot_take(void)
{
    snapshot_t s = {malloc(flash_size), malloc(sizeof(nvs))};
    memcpy(s.flash, flash, flash_size);
    memcpy(s.nvs, &nvs, sizeof(nvs));
    return s;
}
static void snapshot_put(const snapshot_t *s)
{
    memcpy(flash, s->flash, flash_size);
    memcpy(&nvs, s->nvs, sizeof(nvs));
}
static void snapshot_free(snapshot_t *s)
{
    free(s->flash);
    free(s->nvs);
}

/* The old image after a reset in an unconfirmed window: its mount erases
 * the block, every file reads as before, and it keeps working: punches,
 * an outbox row, a probe file. Its rows join the models. */
static bool model_try_append(model_file_t *m, const void *data, uint32_t n)
{
    if (!append_bytes(m->name, data, n)) return false; /* a full store refuses: the row stays with its producer */
    m->data = realloc(m->data, m->size + n + 1);
    m->unreadable = realloc(m->unreadable, m->size + n + 1);
    memcpy(m->data + m->size, data, n);
    memset(m->unreadable + m->size, 0, n + 1);
    m->size += n;
    return true;
}

static void old_image_runs(const char *what, bool unchanged)
{
    mount_plain();
    if (unchanged) check_files_unchanged(what);
    char row[600];
    model_file_t *pending = model_of("/pending.jsonl"), *outbox = model_of("/add_pending.jsonl");
    for (int i = 0; i < 6 && pending; ++i)
        if (!model_try_append(pending, row, (uint32_t)json_row(row, sizeof(row)))) break;
    if (outbox) (void)model_try_append(outbox, row, (uint32_t)json_row(row, sizeof(row)));
    static const uint8_t probe[32] = {3};
    spiffs_file f = SPIFFS_open(&fs, "/persistence-probe", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    if (f >= 0) {
        (void)SPIFFS_write(&fs, f, (void *)probe, sizeof(probe));
        SPIFFS_close(&fs, f);
    }
    (void)SPIFFS_remove(&fs, "/persistence-probe");
    SPIFFS_clearerr(&fs);
    SPIFFS_unmount(&fs);
}

typedef enum { AFTER_SAME = 0, AFTER_OLD_IMAGE, AFTER_RELEASED_275 } after_cut_t;
/* Round 2 review: the round's first window is a worn cell in the measured
 * boot and in every cut boot (not in the boots after the cut). */
static bool sweep_worn;

/* A cut at every `stride`th write and erase of the round (from its first
 * record to its remount), both tears; then the next boots. Confirmed: the
 * redo completes every block, custody byte-exact with no gap at all.
 * Unconfirmed: the old image runs first (plain SPIFFS, or the released 2.7.5
 * repair), then 2.7.6 again. */
/* Every byte of the file its own, or covered by a gap record: custody or
 * evidence. Returns the bytes covered and changed. */
static uint32_t custody_or_evidence(const model_file_t *m, const char *what)
{
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_file(m->name, buffer, sizeof(buffer));
    CHECK(got >= (int64_t)m->size, "%s: %s reads %" PRId64 " of %u bytes", what, m->name, got, m->size);
    srp_gaps_t gaps = gaps_now();
    uint32_t lost = 0;
    for (uint32_t i = 0; i < m->size; ++i) {
        bool same = buffer[i] == m->data[i];
        if (same && !m->unreadable[i]) continue;
        bool covered = false;
        for (uint32_t g = 0; g < gaps.count && !covered; ++g)
            covered = !strcmp(gaps.gap[g].name, m->name) && i >= gaps.gap[g].offset &&
                i < gaps.gap[g].offset + gaps.gap[g].length;
        CHECK(covered, "%s: %s byte %u changed (%02x -> %02x) with no gap record", what, m->name, i, m->data[i],
              buffer[i]);
        lost += !same;
    }
    return lost;
}

/* The same, measured without failing: bytes of the file lost, and how many
 * of them no gap record covers (all of them for a file that does not read). */
static void custody_measured(const model_file_t *m, uint32_t *lost, uint32_t *uncovered)
{
    static uint8_t buffer[8 * 1024 * 1024];
    int64_t got = read_file(m->name, buffer, sizeof(buffer));
    if (got < (int64_t)m->size) {
        *lost += m->size;
        *uncovered += m->size;
        return;
    }
    srp_gaps_t gaps = gaps_now();
    for (uint32_t i = 0; i < m->size; ++i) {
        if (buffer[i] == m->data[i] && !m->unreadable[i]) continue;
        bool covered = false;
        for (uint32_t g = 0; g < gaps.count && !covered; ++g)
            covered = !strcmp(gaps.gap[g].name, m->name) && i >= gaps.gap[g].offset &&
                i < gaps.gap[g].offset + gaps.gap[g].length;
        *lost += buffer[i] != m->data[i];
        *uncovered += !covered;
    }
}

static uint32_t p02_queue_block(void);
static uint64_t env_u64(const char *name) { return getenv(name) ? strtoull(getenv(name), NULL, 10) : 0; }
static void run_reclaim_cuts(const char *what, void (*build)(void), uint32_t kib, bool unconfirmed, after_cut_t after,
                             uint32_t stride)
{
    /* Verification runs (not CI): REVIEW_BUILD_SALT and REVIEW_SALT seed the
     * store and the cut boots afresh; REVIEW_PREFER_QUEUE puts P02's first
     * window over the queue's own pages; REVIEW_TO_END cuts every write from
     * the round's first record to the end of the boot (the run after the
     * round, its restores and settlement included). */
    seed(62000 + kib + env_u64("REVIEW_BUILD_SALT") * 7919U);
    nvs_clear();
    format_flash(kib);
    /* 2.7.7: P02's field store as its unconfirmed boot left it; the round is
     * the confirmed boot's (the same image tag). */
    if (build == build_p02field) {
        p02field_prepare();
        CHECK(p02field_area_test() == SRX_TEST_PASSED, "%s: the area test", what);
    } else {
        build();
        if (build == build_p02) p02_as_275();
    }
    snapshot_t image = snapshot_take();
    model_file_t *saved = calloc(MAX_MODEL, sizeof(model_file_t));
    uint32_t saved_count = model_count;
    for (uint32_t i = 0; i < model_count; ++i) {
        saved[i] = model[i];
        saved[i].data = malloc(model[i].size + 1);
        saved[i].unreadable = malloc(model[i].size + 1);
        memcpy(saved[i].data, model[i].data, model[i].size + 1);
        memcpy(saved[i].unreadable, model[i].unreadable, model[i].size + 1);
    }
    srr_config_t config = build == build_p02field ? config_2770(false) : config_2760(unconfirmed);
    srr_report_t report;
    uint64_t start = flash_ops;
    round_start_ops = round_end_ops = 0;
    uint32_t closed_before = round_closed_blocks;
    if (getenv("REVIEW_PREFER_QUEUE") && build == build_p02) prefer_block = p02_queue_block();
    worn_next_page = sweep_worn;
    repair_boot(&report, &config);
    worn_next_page = false;
    worn_address = UINT32_MAX;
    CHECK(report.reclaim.blocks > 0 && round_end_ops > round_start_ops, "%s: no round to cut", what);
    CHECK(!sweep_worn || (report.reclaim.failed == 1 && round_closed_blocks == closed_before + 1),
          "%s: %u failed blocks, %u closed", what, report.reclaim.failed, round_closed_blocks - closed_before);
    uint64_t first = round_start_ops - start + 1, last = round_end_ops - start, round_last = last;
    if (getenv("REVIEW_TO_END")) last = flash_ops - start;
    uint32_t evidenced = 0, spiffs_torn = 0, spiffs_torn_lost = 0;
    uint32_t runs = 0, rollbacks = 0;
    uint64_t only_k = getenv("REVIEW_K") ? strtoull(getenv("REVIEW_K"), NULL, 10) : 0;
    for (uint64_t k = first; k <= last; k += stride) {
        if (only_k && k != only_k) continue;
        bool after_round = k > round_last;
        for (uint32_t tear = 0; tear < 2; ++tear) {
            snapshot_put(&image);
            for (uint32_t i = 0; i < saved_count; ++i) {
                free(model[i].data);
                free(model[i].unreadable);
                model[i] = saved[i];
                model[i].data = malloc(saved[i].size + 1);
                model[i].unreadable = malloc(saved[i].size + 1);
                memcpy(model[i].data, saved[i].data, saved[i].size + 1);
                memcpy(model[i].unreadable, saved[i].unreadable, saved[i].size + 1);
            }
            model_count = saved_count;
            seed(63000 + k * 2 + tear + env_u64("REVIEW_SALT") * 1000003U);
            round_files_wanted = unconfirmed;
            print_log = only_k != 0;
            worn_next_page = sweep_worn;
            cut_tore_spiffs_erase = false;
            bool cut = boot_with_cut_with(k, &config);
            worn_next_page = false;
            worn_address = UINT32_MAX;
            round_files_wanted = false;
            round_active = false;
            if (!cut) continue;
            runs++;
            char label[120];
            snprintf(label, sizeof(label), "%s, cut at write %" PRIu64 " of %" PRIu64 "..%" PRIu64 " (tear %u)", what, k,
                     first, last, tear);
            if (unconfirmed && after != AFTER_SAME) {
                rollbacks++;
                /* Review C1 holds for a cut in the round's windows; one in the
                 * run after it (REVIEW_TO_END) changes files on purpose. */
                old_image_runs(label, !after_round);
                if (after == AFTER_RELEASED_275) {
                    srr_report_t old;
                    srr_config_t rolled = repair_config();
                    rolled.image_tag = 0x2750;
                    repair_boot_275(&old, &rolled);
                    /* 2.7.5 finds nothing missing the window could have cost:
                     * no gap record, the store as wedged as it found it. */
                    CHECK(old.result != SRR_RESULT_FAILED && (after_round || !model_gaps()), "%s: 2.7.5 as the old "
                          "image %s, %u gap records on custody", label, srr_result_name(old.result), model_gaps());
                }
            }
            srr_report_t done;
            boot_until_done_with(label, &config, &done);
            print_log = false;
            if (after_round && unconfirmed && after != AFTER_SAME && cut_tore_spiffs_erase) {
                /* REVIEW_TO_END: the cut tore an erase through SPIFFS's own
                 * erase (its GC in the probe's writes, or 2.7.5's compaction)
                 * and the block kept a valid magic over lookup entries
                 * reading free above written pages; the old image's plain
                 * SPIFFS programs over them (the AND of two pages: a queue's
                 * header renamed, the queue gone). SPIFFS's own exposure at
                 * every GC erase, the application's included, and 2.7.5's
                 * on its own unconfirmed boot: counted and printed, not
                 * judged here (design section 15). */
                spiffs_torn++;
                uint32_t lost = 0, uncovered = 0;
                mount_plain();
                for (uint32_t i = 0; i < model_count; ++i) custody_measured(&model[i], &lost, &uncovered);
                SPIFFS_unmount(&fs);
                spiffs_torn_lost += lost != 0;
                printf("%s: after the round, SPIFFS's own erase torn with its magic valid, then the old image: "
                       "%u bytes lost, %u of them with no gap record (not judged)\n", label, lost, uncovered);
                check_reclaimed_structure(label);
                continue;
            }
            if (after_round && unconfirmed && after != AFTER_SAME) {
                /* REVIEW_TO_END: a cut in the run after the round (2.7.5's own
                 * writes, SPIFFS page moves among them), then the old image's
                 * plain SPIFFS writing over what the cut tore. Every byte is
                 * its own or covered by a gap record: custody or evidence. */
                uint32_t lost = 0;
                mount_plain();
                for (uint32_t i = 0; i < model_count; ++i) lost += custody_or_evidence(&model[i], label);
                SPIFFS_unmount(&fs);
                if (lost) {
                    evidenced++;
                    printf("%s: after the round, through the old image: %u bytes lost, each covered by a gap record\n",
                           label, lost);
                }
            } else {
                CHECK(!model_gaps(), "%s: %u gap records name custody", label, model_gaps());
                mount_plain();
                for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
                SPIFFS_unmount(&fs);
            }
            check_reclaimed_structure(label);
        }
    }
    for (uint32_t i = 0; i < saved_count; ++i) {
        free(saved[i].data);
        free(saved[i].unreadable);
    }
    free(saved);
    snapshot_free(&image);
    prefer_block = UINT32_MAX;
    if (evidenced)
        printf("%s: %u cuts after the round (through the old image) lost bytes, every one covered by a gap record\n",
               what, evidenced);
    if (spiffs_torn)
        printf("%s: %u cuts after the round tore SPIFFS's own erase with the magic valid, then the old image (%u of "
               "them lost bytes): not judged\n", what, spiffs_torn, spiffs_torn_lost);
    printf("%s: %u cuts in the round%s (writes %" PRIu64 "..%" PRIu64 ", every %u), %u through the old image; "
           "custody byte-exact, no gap%s\n", what, runs, getenv("REVIEW_TO_END") ? " and to the end of the boot" : "",
           first, last, stride, rollbacks, evidenced || spiffs_torn ? " but for those" : "");
}

/* The gc-unsafe store of run_gc_unsafe_handover (T2). */
static void build_gc_unsafe(void)
{
    model_reset();
    row_seq = 700;
    mount_plain();
    for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
        write_file(crammed_fill_names[f], NULL, 0);
    model_file_t *pending = new_model("/pending.jsonl", true, false);
    char row[600];
    while (fs.free_blocks > 2) {
        size_t n = json_row(row, sizeof(row));
        model_append(pending, row, (uint32_t)n);
        if (!append_bytes("/ballast.jsonl", row, (uint32_t)n)) break;
    }
    CHECK(SPIFFS_remove(&fs, "/ballast.jsonl") == SPIFFS_OK, "gc unsafe: remove ballast");
    SPIFFS_unmount(&fs);
    consume_erased();
}

/* ---- the redo area and its records ----------------------------------------- */
/* p02 confirmed, 2.7.5 run, and the boot that follows cut inside the first
 * window (its block erased, nothing programmed back yet). */
static void p02_cut_in_window(uint32_t kib, uint64_t seed_value, const srr_config_t *config)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    snapshot_t image = snapshot_take();
    srr_report_t report;
    uint64_t start = flash_ops;
    repair_boot(&report, config);
    CHECK(first_window_ops > start, "no window opened");
    uint64_t k = first_window_ops - start + 2; /* the window's erase */
    snapshot_put(&image);
    snapshot_free(&image);
    seed(seed_value);
    CHECK(boot_with_cut_with(k, config), "the cut never came");
    round_active = false;
}

static uint32_t journal_flags(void)
{
    srp_reclaim_t journal;
    return port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) == 1 &&
            srp_reclaim_valid(&journal)
        ? journal.flags
        : 0;
}

static void run_reclaim_area(uint32_t kib)
{
    srr_report_t report;
    srr_config_t config = config_2760(false);
    uint32_t pending, sources, open;

    /* Foreign: one byte in one sector of an area this device never wrote. The
     * area is never written; the store stays as 2.7.5 leaves it. */
    seed(64001);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    nvs.redo[5 * 4096 + 100] = 0x00;
    uint8_t *area = malloc(REDO_BYTES);
    memcpy(area, nvs.redo, REDO_BYTES);
    logged_count = 0;
    repair_boot(&report, &config);
    CHECK(report.result == SRR_RESULT_DEFERRED && report.reclaim.area == SRX_AREA_FOREIGN &&
              report.reclaim.area_foreign == 1 && report.reclaim.area_first_foreign == 5 && !report.reclaim.blocks &&
              code_logged("STORAGE_REPAIR_RECLAIM_UNAVAILABLE") && !memcmp(area, nvs.redo, REDO_BYTES) &&
              report.reclaim.candidates > 0,
          "foreign area: %s, area %s, %u foreign, %u blocks", srr_result_name(report.result),
          srx_area_name((srx_area_state_t)report.reclaim.area), report.reclaim.area_foreign, report.reclaim.blocks);
    free(area);

    /* Claimed: a torn first record is our own, a free slot. Every other slot
     * holds a done record: the ring wraps over them. */
    seed(64002);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    srp_reclaim_t journal = {.flags = SRP_RECLAIM_CLAIMED, .reclaim_id = 40};
    srp_reclaim_seal(&journal);
    port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal));
    static uint8_t target[4096], sector[4096];
    for (uint32_t slot = 1; slot < REDO_BYTES / 4096; ++slot) {
        /* A done record of some block, keeping nothing (it only needs to
         * decode: done records are never acted upon). */
        srx_record_t h;
        memset(&h, 0, sizeof(h));
        h.reclaim_id = 40;
        h.sequence = slot;
        h.image_tag = 0x2760;
        h.block = (uint16_t)(slot % (flash_size / BLOCK));
        h.block_count = (uint16_t)(flash_size / BLOCK);
        memset(target, 0xFF, sizeof(target));
        memcpy(target, flash + h.block * BLOCK, 256);
        h.old_crc = srp_crc32(0, flash + h.block * BLOCK, BLOCK);
        h.new_crc = srp_crc32(0, target, sizeof(target));
        srx_encode(&h, target, sector);
        memset(sector + SRX_OFF_DONE, 0, 4);
        memcpy(nvs.redo + slot * 4096, sector, 4096);
    }
    memset(nvs.redo, 0x5A, 300); /* the torn first record */
    repair_boot(&report, &config);
    uint32_t records = area_records(&pending, &sources, &open);
    CHECK(report.result == SRR_RESULT_REPAIRED && report.reclaim.blocks > 0 && report.reclaim.area == SRX_AREA_READY &&
              records == REDO_BYTES / 4096 && !open,
          "claimed area, torn slot, full ring: %s, %u blocks, %u records, %u open", srr_result_name(report.result),
          report.reclaim.blocks, records, open);
    verify_reclaimed("ring wrap");

    /* Another writer: a cut in a confirmed window, then an image without the
     * redo records mounts the store (erasing the block) and appends. The
     * record must never be replayed over that: its pages become byte
     * sources, the spans they held go back, the record retires. */
    p02_cut_in_window(kib, 64003, &config);
    mount_plain();
    char row[600];
    model_file_t *queue = model_of("/pending.jsonl");
    for (int i = 0; i < 4; ++i) CHECK(model_try_append(queue, row, (uint32_t)json_row(row, sizeof(row))), "other writer");
    SPIFFS_unmount(&fs);
    logged_count = 0;
    boot_until_done_with("another writer", &config, &report);
    CHECK(report.reclaim.sources >= 1 && code_logged("STORAGE_REPAIR_RECLAIM_SOURCES") && !model_gaps() &&
              (journal_flags() & (SRP_RECLAIM_ACTIVE | SRP_RECLAIM_SOURCES)) == 0,
          "another writer: %u sources, %u gap records, journal %x", report.reclaim.sources, model_gaps(), journal_flags());
    records = area_records(&pending, &sources, &open);
    bool from_record = false;
    for (uint32_t i = 0; i < report.file_count; ++i) from_record |= (report.file[i].actions & SRR_FILE_REDO_SOURCE) != 0;
    CHECK(!open && (from_record || report.reclaim.settled), "another writer: %u open records, bytes from the record %d, "
          "%u settled", open, from_record, report.reclaim.settled);
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    check_reclaimed_structure("another writer");

    /* A worn cell: a block that never verifies is closed (erased, its
     * magic), its record a source, and the round goes on (round 2 review,
     * R2-1); the next run puts its pages back. */
    seed(64004);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    worn_next_page = true;
    logged_count = 0;
    repair_boot(&report, &config);
    worn_next_page = false;
    worn_address = UINT32_MAX;
    CHECK(code_logged("STORAGE_REPAIR_RECLAIM_FAILED") && report.reclaim.failed == 1 && report.reclaim.blocks > 0 &&
              !model_gaps() && report.result != SRR_RESULT_FAILED,
          "worn block: %s, %u failed, %u blocks, %u gap records", srr_result_name(report.result), report.reclaim.failed,
          report.reclaim.blocks, model_gaps());
    boot_until_done_with("worn block, next boot", &config, &report);
    check_models_exact("worn block");
    records = area_records(&pending, &sources, &open);
    CHECK(!open && !model_gaps(), "worn block: %u open, %u gaps", open, model_gaps());

    /* The attempts tally: the redo browns out at its erase four boots in a
     * row; the fifth makes the record a source and the store repairs. */
    p02_cut_in_window(kib, 64005, &config);
    for (int boot = 0; boot < (int)SRX_ATTEMPTS; ++boot) CHECK(boot_with_cut_with(3, &config), "tally boot %d", boot);
    logged_count = 0;
    boot_until_done_with("attempts tally", &config, &report);
    CHECK(boot_area.exhausted == 1 || report.reclaim.sources >= 1, "attempts tally: %u exhausted, %u sources",
          boot_area.exhausted, report.reclaim.sources);
    check_models_exact("attempts tally");
    CHECK(!model_gaps(), "attempts tally: %u gaps", model_gaps());

    /* The attempts guard says report only: the redo still completes the
     * block (it finishes an interrupted write), nothing else is written. */
    p02_cut_in_window(kib, 64006, &config);
    store_state(SRP_PHASE_REFORMAT, 2, 2, 0, 0);
    srr_config_t guarded = config;
    guarded.previous_failed = true;
    repair_boot(&report, &guarded);
    records = area_records(&pending, &sources, &open);
    CHECK(report.result == SRR_RESULT_REPORT_ONLY && boot_area.redone == 1 && !open,
          "report only: %s, %u redone, %u open", srr_result_name(report.result), boot_area.redone, open);
    guarded.image_tag ^= 0x10000; /* another image tries again */
    boot_until_done_with("report only, then another image", &guarded, &report);
    check_models_exact("report only");

    /* The area unreadable while a round was active: SOURCES goes to the
     * journal before the store is mounted; the boot fills what it cannot read
     * (recorded); the next one, reading the area, puts the bytes back. */
    p02_cut_in_window(kib, 64007, &config);
    redo_unreadable = true;
    repair_boot(&report, &config);
    redo_unreadable = false;
    CHECK(journal_flags() & SRP_RECLAIM_SOURCES, "unreadable area: journal %x", journal_flags());
    boot_until_done_with("unreadable area, next boot", &config, &report);
    check_models_exact("unreadable area");
    CHECK(!(journal_flags() & SRP_RECLAIM_SOURCES), "unreadable area: journal still %x", journal_flags());
    /* ... and when SOURCES cannot be saved, the store stays unmounted; the
     * next boot redoes the block. */
    p02_cut_in_window(kib, 64008, &config);
    redo_unreadable = journal_refused = true;
    repair_boot(&report, &config);
    redo_unreadable = journal_refused = false;
    CHECK(mount_blocked && report.result == SRR_RESULT_NOT_RUN, "refused journal: the store was mounted");
    boot_until_done_with("refused journal, next boot", &config, &report);
    check_models_exact("refused journal");
    CHECK(!model_gaps() && boot_area.redone == 1, "refused journal: %u gaps, %u redone", model_gaps(), boot_area.redone);
    printf("redo area: foreign untouched, a torn first record and a full ring reused, another writer's block from "
           "sources, a worn block, four browned-out redos, report-only redo, an unreadable area and a refused journal\n");
}

/* The same p02 store repaired with and without the metadata mirror: the
 * same flash, the same redo area, the same report. */
static void run_reclaim_mirror(uint32_t kib)
{
    srr_report_t plain, mirrored;
    srr_config_t config = config_2760(false);
    seed(64100);
    nvs_clear();
    use_mirror = false;
    format_flash(kib);
    build_p02();
    p02_as_275();
    snapshot_t image = snapshot_take();
    repair_boot(&plain, &config);
    snapshot_t after = snapshot_take();
    snapshot_put(&image);
    use_mirror = true;
    mirror_inits = mirror_compares = 0;
    repair_boot(&mirrored, &config);
    use_mirror = false;
    /* The device's remount: one mirror for the boot, kept across the round's
     * remount and equal to flash there. */
    CHECK(mirror_inits == 1 && mirror_compares == mirrored.reclaim.rounds && mirror_compares >= 1,
          "reclaim: %u mirror inits, %u kept-mirror checks for %u rounds", mirror_inits, mirror_compares,
          mirrored.reclaim.rounds);
    CHECK(!memcmp(flash, after.flash, flash_size) && !memcmp(nvs.redo, after.nvs->redo, REDO_BYTES),
          "reclaim: the mirror changed the store or the redo area");
    CHECK(plain.result == mirrored.result && plain.reclaim.blocks == mirrored.reclaim.blocks &&
              plain.reclaim.pages_freed == mirrored.reclaim.pages_freed && plain.check_passes == mirrored.check_passes,
          "reclaim: the mirror changed the report");
    snapshot_free(&image);
    snapshot_free(&after);
    printf("reclaim mirror equivalence passed (%u blocks)\n", plain.reclaim.blocks);
}

/* Cuts at every write of the boot recovery's redo itself, both tears. */
static void run_redo_cuts(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    p02_cut_in_window(kib, 64200, &config);
    snapshot_t image = snapshot_take();
    srr_report_t report;
    repair_boot(&report, &config);
    CHECK(boot_area.redone == 1, "redo cuts: no redo");
    uint32_t runs = 0;
    for (uint64_t k = 1; k <= 24; ++k) {
        for (uint32_t tear = 0; tear < 2; ++tear) {
            snapshot_put(&image);
            seed(64300 + k * 2 + tear);
            if (!boot_with_cut_with(k, &config)) continue;
            runs++;
            boot_until_done_with("redo cut", &config, &report);
            check_models_exact("redo cut");
            CHECK(!model_gaps(), "redo cut at %" PRIu64 ": %u gaps", k, model_gaps());
            check_reclaimed_structure("redo cut");
        }
    }
    snapshot_free(&image);
    printf("redo: %u cuts inside the boot recovery's redo, custody byte-exact, no gap\n", runs);
}

/* ---- round 1 review of the implementation ---------------------------------- */
/* The records in the redo area, oldest first: the i-th opened the round's
 * i-th window (no retries in these stores). */
static uint32_t records_by_sequence(srx_record_t *out, uint32_t *slots, uint32_t capacity)
{
    static uint8_t sector[4096];
    uint32_t n = 0;
    for (uint32_t slot = 0; slot < REDO_BYTES / 4096; ++slot) {
        srx_record_t r;
        memcpy(sector, nvs.redo + slot * 4096, 4096);
        if (!srx_decode(sector, &r, NULL) || n == capacity) continue;
        uint32_t at = n++;
        while (at && out[at - 1].sequence > r.sequence) {
            out[at] = out[at - 1];
            slots[at] = slots[at - 1];
            --at;
        }
        out[at] = r;
        slots[at] = slot;
    }
    return n;
}

/* Data pages of the file a record keeps that its index names now (the
 * store as the round began). */
static uint32_t kept_pages_of(const uint8_t *sector, const srx_record_t *r, const char *name)
{
    uint16_t obj = object_of(name);
    uint32_t n = 0;
    for (uint32_t e = 0; e < SRX_ENTRIES; ++e) {
        const uint8_t *page = srx_kept_page(sector, r, e);
        if (!page || srx_get16(page) != obj) continue;
        n += rd16(entry_address(name, srx_get16(page + 2))) == r->block * 16U + 1U + e;
    }
    return n;
}

/* A block of P02's queue holding at least six pages its index names, the
 * rest deleted pages: a round given it first puts a window over the queue's
 * own bytes (the round's ranking takes blocks of deleted pages first). */
static uint32_t p02_queue_block(void)
{
    model_file_t *pending = model_of("/pending.jsonl");
    uint16_t obj = object_of(pending->name);
    uint32_t spans = pending->size / SPAN;
    for (uint32_t sp = spans / 3; sp + 30 < spans; ++sp) {
        uint32_t b = rd16(entry_address(pending->name, sp)) / 16, kept = 0, dropped = 0;
        bool ok = true;
        for (uint32_t e = 1; e < 16 && ok; ++e) {
            uint32_t q = b * 16 + e;
            uint16_t lu = rd16(lookup_address(q));
            if (lu == 0) {
                dropped += !(flash[q * PAGE + 4] & SPIFFS_PH_FLAG_DELET);
                continue;
            }
            if (lu == 0xFFFF) continue;
            ok = lu == obj && rd16(q * PAGE) == obj && rd16(entry_address(pending->name, rd16(q * PAGE + 2))) == q;
            kept++;
        }
        if (ok && dropped && kept >= 6) return b;
    }
    return UINT32_MAX;
}

/* Round 1 review, D1 and the low finding: a cut in the window of a block
 * holding the queue's referenced pages, then a boot that cannot read the
 * redo area (SOURCES journaled, the mount erases the block), then boots that
 * can. CURSOR_STALE: an early span nothing can read and a cursor past it
 * whose prefix CRC is another file's (the reviewer's scenario: block 133 of
 * the 1 MiB store, seed 64007); it cannot be verified (PREFIX_GAP, left as
 * it is). CURSOR_BEFORE: the same damage, a valid cursor before it.
 * CURSOR_PAST: no damage, a valid cursor past the window's spans (its rows
 * delivered). Records keep only a verified cursor offset, and no offset
 * settles bytes the file can still get back (D1): every readable byte comes
 * back. Gap records name only the bytes nobody could read (low: the boot
 * without the area leaves the queue's unreadable bytes alone instead of
 * filling them). */
enum { CURSOR_STALE, CURSOR_BEFORE, CURSOR_PAST };
static void run_unread_area(uint32_t kib, void (*build)(void), const char *what, int mode)
{
    bool stale = mode == CURSOR_STALE;
    srr_config_t config = config_2760(false);
    bool p02 = build == build_p02;
    uint64_t seed_value = p02 ? 64007 : 64510 + kib;
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    build();
    model_file_t *pending = model_of("/pending.jsonl");
    uint16_t obj = object_of(pending->name);
    if (mode != CURSOR_PAST) {
        uint32_t pix = rd16(entry_address(pending->name, 2));
        wr16(pix * PAGE, 0x0155);
        wr16(lookup_address(pix), 0x0155);
        mark_unreadable(pending, 2);
    }
    uint32_t spans = pending->size / SPAN;
    /* Before the damage: the last row boundary at or before span 2. */
    uint32_t cursor = 2 * SPAN;
    while (mode == CURSOR_BEFORE && cursor > 0 && pending->data[cursor - 1] != '\n') --cursor;
    if (mode != CURSOR_BEFORE) cursor = row_boundary_after(pending, (spans - 8) * SPAN);
    set_cursor("legacy_queues", "ords_pending", cursor, prefix_crc(pending, cursor) ^ (stale ? 0x5A5A5A5AU : 0U), 9);
    /* On P02 a block of the queue's own pages goes first. */
    prefer_block = p02 ? p02_queue_block() : UINT32_MAX;
    CHECK(!p02 || prefer_block != UINT32_MAX, "%s: no block of the queue's referenced pages", what);
    snapshot_t image = snapshot_take();
    srr_report_t report;
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    srx_record_t records[64];
    uint32_t slots[64], count = records_by_sequence(records, slots, 64), chosen = UINT32_MAX, kept = 0;
    CHECK(count == window_count && count, "%s: %u records, %u windows", what, count, window_count);
    /* D1: a record keeps the queue's cursor offset only when it was verified
     * (a cursor past an unreadable span never is); checked once the boots
     * below showed what an unverified one costs. */
    uint32_t wrong_cursor = UINT32_MAX;
    for (uint32_t i = 0; i < count; ++i)
        for (uint32_t k = 0; k < records[i].objects; ++k) {
            const srx_object_t *entry = &records[i].object[k];
            bool kept_offset = (entry->flags & SRX_OBJECT_CURSOR) != 0;
            if (entry->obj == obj && (stale ? kept_offset : !kept_offset || entry->cursor_offset != cursor))
                wrong_cursor = records[i].sequence;
        }
    uint8_t *sectors = malloc((size_t)count * 4096);
    for (uint32_t i = 0; i < count; ++i) memcpy(sectors + (size_t)i * 4096, nvs.redo + slots[i] * 4096, 4096);
    snapshot_put(&image);
    snapshot_free(&image);
    /* The first window over pages the queue's index names. */
    for (uint32_t i = 0; i < count && chosen == UINT32_MAX; ++i) {
        kept = kept_pages_of(sectors + (size_t)i * 4096, &records[i], pending->name);
        if (kept) chosen = i;
    }
    free(sectors);
    CHECK(chosen != UINT32_MAX, "%s: no window over pages the queue's index names (%u records)", what, count);
    uint64_t k = window_ops[chosen] - start + 2; /* the window's erase */
    seed(seed_value);
    CHECK(boot_with_cut_with(k, &config), "%s: the cut never came", what);
    round_active = false;
    redo_unreadable = true;
    logged_count = 0;
    repair_boot(&report, &config);
    redo_unreadable = false;
    CHECK((journal_flags() & SRP_RECLAIM_SOURCES) && code_logged("STORAGE_REPAIR_RECLAIM_SOURCES"),
          "%s: the unreadable area left the journal %x", what, journal_flags());
    srr_report_t after;
    boot_until_done_with(what, &config, &after);
    prefer_block = UINT32_MAX;
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    check_reclaimed_structure(what);
    CHECK(wrong_cursor == UINT32_MAX, "%s: record %u keeps the queue's cursor offset %s", what, wrong_cursor,
          stale ? "although it could not be verified" : "wrong or not at all although it was verified");
    printf("%s: a cut in block %u's window (%u pages the queue's index names), then a boot without the redo area "
           "(%s), then %s: every readable byte back, gap records only for the bytes nobody could read\n",
           what, records[chosen].block, kept, srr_result_name(report.result), srr_result_name(after.result));
}

/* Round 1 review, D2 (T5): the check frees a held file, or changes one, and
 * leaves no erased page, so its restore has no page (for the rebuild's
 * header, or the changed span's new page). With a redo area the round makes
 * room while the hold still lives and the file goes back byte for byte, with
 * no gap record (the command dedupe file, removed or changed). When even the
 * round cannot make room for all of it (the 30 KB queue on a store crammed
 * with files never put in a window), what still cannot go back is recorded
 * once, after the round. Without a redo area (2.7.5's outcome) its bytes are
 * recorded once: the manifest entry is marked reported, so the resume adds
 * no second record for the same range. */
static const char *t5_victim;
static bool t5_alter;
static bool t5_remove_and_cram(uint32_t pass)
{
    if (pass != 1) return false;
    if (t5_alter) {
        spiffs_file f = SPIFFS_open(&fs, t5_victim, SPIFFS_O_RDWR, 0);
        static const uint8_t junk[16] = "XXXXXXXXXXXXXXX";
        CHECK(f >= 0 && SPIFFS_lseek(&fs, f, 300, SPIFFS_SEEK_SET) == 300 &&
                  SPIFFS_write(&fs, f, (void *)junk, sizeof(junk)) == (s32_t)sizeof(junk),
              "t5: change %s", t5_victim);
        SPIFFS_close(&fs, f);
    } else {
        CHECK(SPIFFS_remove(&fs, t5_victim) == SPIFFS_OK, "t5: remove %s", t5_victim);
    }
    SPIFFS_clearerr(&fs);
    consume_erased();
    return true;
}

static uint32_t gaps_naming(const char *name)
{
    srp_gaps_t gaps = gaps_now();
    uint32_t n = 0;
    for (uint32_t g = 0; g < gaps.count; ++g) n += !strcmp(gaps.gap[g].name, name);
    return n;
}

static void run_restore_room(uint32_t kib)
{
    static const struct {
        const char *victim;
        bool alter, room;
    } cases[] = {{"/processed_commands.txt", false, true}, {"/pending.jsonl", false, false},
                 {"/processed_commands.txt", true, true}};
    for (uint32_t v = 0; v < sizeof(cases) / sizeof(cases[0]); ++v) {
        for (int redo = 0; redo < 2; ++redo) {
            redo_enabled = redo != 0;
            seed(47000 + v);
            nvs_clear();
            format_flash(kib);
            build_store(60000);
            for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
                write_file(crammed_fill_names[f], NULL, 0);
            static uint8_t filler[4000];
            memset(filler, 0x5A, sizeof(filler));
            while (append_bytes("/filler.bin", filler, sizeof(filler))) {}
            SPIFFS_unmount(&fs);
            t5_victim = cases[v].victim;
            t5_alter = cases[v].alter;
            const char *how = t5_alter ? "changed" : "freed";
            srr_config_t config = config_2760(false);
            srr_report_t report;
            check_hook = t5_remove_and_cram;
            logged_count = 0;
            repair_boot(&report, &config);
            check_hook = NULL;
            uint32_t blocks = report.reclaim.blocks;
            if (redo && !cases[v].room) {
                /* The round cannot make room for 30 KB here: recorded once. */
                CHECK(!strcmp(report.reclaim.trigger, "restore") && report.reclaim.blocks > 0 &&
                          code_logged("STORAGE_REPAIR_RESTORE_WAITING") && code_logged("STORAGE_REPAIR_RESTORE_RETRIED") &&
                          gaps_naming(t5_victim) == 1,
                      "t5 %s, room short after the round: %s, %u blocks, %u gap records", t5_victim,
                      srr_result_name(report.result), report.reclaim.blocks, gaps_naming(t5_victim));
                repair_boot(&report, &config);
                CHECK(gaps_naming(t5_victim) == 1, "t5 %s: the same bytes recorded again (%u records)", t5_victim,
                      gaps_naming(t5_victim));
                printf("t5 %s %s: the round could not make room for all of it (%u blocks); recorded once after the "
                       "round, never twice\n", t5_victim, how, blocks);
            } else if (redo) {
                CHECK(!strcmp(report.reclaim.trigger, "restore") && report.reclaim.blocks > 0 &&
                          code_logged("STORAGE_REPAIR_RESTORE_WAITING") && code_logged("STORAGE_REPAIR_RESTORE_RETRIED") &&
                          !gaps_naming(t5_victim) && report.result != SRR_RESULT_FAILED,
                      "t5 %s %s with a redo area: %s at %s, trigger '%s', %u blocks, %u gap records", t5_victim, how,
                      srr_result_name(report.result), report.error_stage, report.reclaim.trigger,
                      report.reclaim.blocks, gaps_naming(t5_victim));
                boot_until_done_with("t5, next boot", &config, NULL);
                CHECK(!gaps_naming(t5_victim), "t5 %s: %u gap records", t5_victim, gaps_naming(t5_victim));
                mount_plain();
                for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
                SPIFFS_unmount(&fs);
                printf("t5 %s %s by the check on a store without room: the round made room (%u blocks) while its "
                       "hold lived; back byte for byte, no gap record\n", t5_victim, how, blocks);
            } else {
                CHECK(report.result == SRR_RESULT_FAILED && gaps_naming(t5_victim) == 1,
                      "t5 %s %s without a redo area: %s, %u gap records", t5_victim, how,
                      srr_result_name(report.result), gaps_naming(t5_victim));
                repair_boot(&report, &config);
                CHECK(gaps_naming(t5_victim) == 1, "t5 %s: the resume recorded the same bytes again (%u records)",
                      t5_victim, gaps_naming(t5_victim));
                printf("t5 %s %s without a redo area: recorded once (2.7.5's outcome, no second record)\n", t5_victim,
                       how);
            }
        }
    }
    redo_enabled = true;
    t5_alter = false;
}

/* Round 1 review, D3: an erase of a window cut short whose magic reads valid
 * again. A strong magic cannot in the byte model (the exhaustive test in
 * storage_repair_plan_host.c); a bit-level tear can. On the same image the
 * block is completed from its record (it is a torn erase, not a block
 * another image wrote), so no half-erased block mounts. */
static void run_torn_magic(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    uint32_t redone = 0;
    for (uint32_t trial = 0; trial < 6; ++trial) {
        p02_cut_in_window(kib, 64600 + trial, &config);
        srx_record_t records[64];
        uint32_t slots[64], count = records_by_sequence(records, slots, 64), open = UINT32_MAX;
        for (uint32_t i = 0; i < count && open == UINT32_MAX; ++i)
            if (records[i].done == 0xFFFFFFFFU && !srx_is_source(&records[i])) open = i;
        CHECK(open != UINT32_MAX, "torn magic: no open record");
        uint32_t block = records[open].block, blocks = flash_size / BLOCK;
        CHECK(srx_magic_strong(blocks, block), "torn magic: block %u has a weak magic", block);
        wr16(block * BLOCK + 252, srx_spiffs_magic(blocks, block));
        srr_report_t report;
        logged_count = 0;
        repair_boot(&report, &config);
        CHECK(boot_area.redone == 1 && !boot_area.to_sources,
              "torn magic, trial %u: block %u with a valid magic after a torn erase: %u redone, %u sources", trial, block,
              boot_area.redone, boot_area.to_sources);
        boot_until_done_with("torn magic", &config, &report);
        check_models_exact("torn magic");
        CHECK(!model_gaps(), "torn magic: %u gap records", model_gaps());
        check_reclaimed_structure("torn magic");
        redone++;
    }
    printf("torn magic: %u erases cut short with the magic reading valid, each block completed from its record\n",
           redone);
}

/* Round 1 review, low 6: a boot whose record became a byte source cannot
 * allocate the round's 16 KiB buffer. Without it the run would fill the
 * spans the record holds and record them as lost: it fails before writing
 * anything, and the next boot puts the bytes back. */
static void run_sources_memory(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    seed(64007);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    prefer_block = p02_queue_block();
    CHECK(prefer_block != UINT32_MAX, "sources without memory: no block of the queue's pages");
    snapshot_t image = snapshot_take();
    srr_report_t report;
    uint64_t start = flash_ops;
    repair_boot(&report, &config);
    CHECK(first_window_ops > start, "sources without memory: no window opened");
    uint64_t k = first_window_ops - start + 2; /* the window's erase, over the queue's pages */
    snapshot_put(&image);
    snapshot_free(&image);
    seed(64007);
    CHECK(boot_with_cut_with(k, &config), "sources without memory: the cut never came");
    round_active = false;
    prefer_block = UINT32_MAX;
    mount_plain();
    char row[600];
    model_file_t *queue = model_of("/pending.jsonl");
    for (int i = 0; i < 4; ++i) CHECK(model_try_append(queue, row, (uint32_t)json_row(row, sizeof(row))), "other writer");
    SPIFFS_unmount(&fs);
    fail_alloc_bytes = 4U * SRX_BLOCK;
    logged_count = 0;
    repair_boot(&report, &config);
    fail_alloc_bytes = 0;
    CHECK(report.result == SRR_RESULT_FAILED && !strcmp(report.error_stage, "sources_memory") &&
              boot_area.to_sources == 1 && !model_gaps(),
          "sources without memory: %s at %s, %u sources, %u gap records", srr_result_name(report.result),
          report.error_stage, boot_area.to_sources, model_gaps());
    boot_until_done_with("sources without memory, next boot", &config, &report);
    check_models_exact("sources without memory");
    CHECK(!model_gaps(), "sources without memory: %u gap records", model_gaps());
    printf("byte sources without memory for them: the run failed before writing; the next boot put them back\n");
}

/* ---- round 2 review of the implementation -------------------------------- */

/* The T5 store of run_restore_room: crammed with files never put in a
 * window, the check frees the held command dedupe file and leaves no room. */
static void t5_store(uint32_t kib, uint64_t seed_value)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    build_store(60000);
    for (uint32_t f = 0; f < sizeof(crammed_fill_names) / sizeof(crammed_fill_names[0]); ++f)
        write_file(crammed_fill_names[f], NULL, 0);
    static uint8_t filler[4000];
    memset(filler, 0x5A, sizeof(filler));
    while (append_bytes("/filler.bin", filler, sizeof(filler))) {}
    SPIFFS_unmount(&fs);
}

/* Round 2 review, R2-1: the round's first window does not verify (a worn
 * cell) while a restore waits for the round (T5: the check freed the 2,600
 * held bytes of /processed_commands.txt on a store without room). The
 * block is closed as SPIFFS's own erase leaves a block and the round goes
 * on; the file goes back byte for byte with no gap record (6e6a982 stopped
 * the round after "0 blocks reformatted" and recorded the file as lost,
 * CHECK_CHANGED, with 17 candidates left). When the block will not close
 * either (a worn cell in its magic) the round stops and the remount erases
 * it; the restore still runs again, room being decided from the erased
 * pages after the remount and not from the blocks reformatted. A second
 * worn window in the same round is closed too and stops the round (one
 * close per round: SR_RECLAIM_CLOSES). */
static void run_worn_round(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    srr_report_t report;
    for (int closes = 2; closes >= 0; --closes) {
        const char *how = closes == 2 ? "two worn windows, both closed, the round stopped at the second"
                        : closes ? "a worn cell in the first window, closed" : "a worn magic, the block not closed";
        t5_store(kib, 47000);
        t5_victim = "/processed_commands.txt";
        t5_alter = false;
        check_hook = t5_remove_and_cram;
        logged_count = 0;
        worn_next_page = closes != 0;
        worn_second_window = closes == 2;
        worn_magic_next = closes == 0;
        uint32_t closed_before = round_closed_blocks, failed_before = round_failed_blocks;
        repair_boot(&report, &config);
        worn_next_page = worn_magic_next = worn_second_window = false;
        worn_address = worn_address2 = UINT32_MAX;
        check_hook = NULL;
        uint32_t closed = round_closed_blocks - closed_before, failed = round_failed_blocks - failed_before;
        uint32_t want_failed = closes == 2 ? 2U : 1U, want_closed = (uint32_t)closes;
        CHECK(report.reclaim.failed == want_failed && failed == want_failed && closed == want_closed &&
                  (closes == 1 ? report.reclaim.blocks > 0 : report.reclaim.blocks == 0) &&
                  code_logged("STORAGE_REPAIR_RESTORE_WAITING") && code_logged("STORAGE_REPAIR_RESTORE_RETRIED") &&
                  !gaps_naming(t5_victim) && report.result != SRR_RESULT_FAILED,
              "t5, %s: %s at %s, %u blocks, %u failed (%u closed), %u gap records naming %s", how,
              srr_result_name(report.result), report.error_stage, report.reclaim.blocks, report.reclaim.failed, closed,
              gaps_naming(t5_victim), t5_victim);
        uint32_t blocks = report.reclaim.blocks;
        boot_until_done_with("t5, worn, next boot", &config, &report);
        CHECK(!gaps_naming(t5_victim), "t5, %s: %u gap records", how, gaps_naming(t5_victim));
        mount_plain();
        for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
        SPIFFS_unmount(&fs);
        check_reclaimed_structure(how);
        printf("t5 %s freed by the check, %s: %u blocks reformatted, back byte for byte, no gap record\n", t5_victim,
               how, blocks);
    }
}

/* Round 2 review, R2-2: a cut in the window of block 133 (14 pages the
 * queue's index names; block 1,067 at 8 MiB), then a boot whose recovery
 * cannot write the area (both tries at the attempts word an I/O error): the
 * record stays open, the journal says SOURCES, the mount erases the block.
 * That boot's run takes the open record as a byte source: no SALVAGE fill,
 * no gap record (6e6a982: 3,514 readable bytes recorded twice as lost and
 * filled, put back only by the next boot). Stuck: the record's settle word
 * never programs (it can never become a source nor settle); every boot reads
 * it as a source, nothing is filled, nothing is lost. No journal: NVS lost
 * the reclaim journal before that boot; recovery still journals SOURCES for
 * the record it left open, so no later boot redoes it. */
static void run_open_record(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    for (int variant = 0; variant < 3; ++variant) {
        bool stuck = variant == 1;
        const char *what = variant == 1 ? "open record, its settle word stuck"
                         : variant == 2 ? "open record, the journal lost" : "open record";
        uint64_t seed_value = 64007;
        seed(seed_value);
        nvs_clear();
        format_flash(kib);
        build_p02();
        model_file_t *pending = model_of("/pending.jsonl");
        uint32_t cursor = row_boundary_after(pending, 2 * SPAN);
        set_cursor("legacy_queues", "ords_pending", cursor, prefix_crc(pending, cursor), 9);
        prefer_block = p02_queue_block();
        CHECK(prefer_block != UINT32_MAX, "%s: no block of the queue's referenced pages", what);
        snapshot_t image = snapshot_take();
        srr_report_t report;
        uint64_t start = flash_ops;
        repair_boot(&report, &config);
        srx_record_t records[64];
        uint32_t slots[64], count = records_by_sequence(records, slots, 64), chosen = UINT32_MAX, kept = 0;
        uint8_t *sectors = malloc((size_t)count * 4096);
        for (uint32_t i = 0; i < count; ++i) memcpy(sectors + (size_t)i * 4096, nvs.redo + slots[i] * 4096, 4096);
        snapshot_put(&image);
        snapshot_free(&image);
        for (uint32_t i = 0; i < count && chosen == UINT32_MAX; ++i) {
            kept = kept_pages_of(sectors + (size_t)i * 4096, &records[i], pending->name);
            if (kept) chosen = i;
        }
        free(sectors);
        CHECK(chosen != UINT32_MAX, "%s: no window over the queue's pages", what);
        uint64_t k = window_ops[chosen] - start + 2; /* the window's erase */
        seed(seed_value);
        CHECK(boot_with_cut_with(k, &config), "%s: the cut never came", what);
        round_active = false;
        area_fail_next = 2;
        if (stuck) area_stuck = slots[chosen] * 4096 + SRX_OFF_SETTLE;
        if (variant == 2) port_nvs_erase(NULL, SRR_NVS_NAMESPACE, "reclaim");
        logged_count = 0;
        repair_boot(&report, &config);
        area_fail_next = 0;
        uint32_t left = boot_open_left, flags = journal_flags();
        CHECK(left == 1 && (flags & SRP_RECLAIM_SOURCES) && !model_gaps() && !report.unreadable_bytes &&
                  report.result != SRR_RESULT_FAILED && report.result != SRR_RESULT_REPAIRED_WITH_GAPS,
              "%s: block %u, recovery left %u open (journal %x); the boot %s with %u gap records, %u unreadable bytes",
              what, records[chosen].block, left, flags, srr_result_name(report.result), model_gaps(),
              report.unreadable_bytes);
        srr_report_t after;
        for (int boot = 0; boot < 2; ++boot) boot_until_done_with(what, &config, &after);
        uint32_t pend, srcs, open;
        (void)area_records(&pend, &srcs, &open);
        area_stuck = UINT32_MAX;
        prefer_block = UINT32_MAX;
        mount_plain();
        for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
        SPIFFS_unmount(&fs);
        CHECK(!model_gaps() && open == (stuck ? 1U : 0U) && (stuck || !(journal_flags() & SRP_RECLAIM_SOURCES)),
              "%s: %u gap records, %u open records, journal %x", what, model_gaps(), open, journal_flags());
        if (!stuck) check_reclaimed_structure(what);
        printf("%s: a cut in block %u's window (%u pages the queue's index names), recovery could not write the "
               "area: the same boot read the open record as a source (%s), no fill, no gap record\n", what,
               records[chosen].block, kept, srr_result_name(report.result));
    }
}

/* Round 2 review, R2-3: one genuinely unreadable 251-byte span on P02's
 * wedged store, one confirmed boot: run 1 defers (recording it), the round,
 * run 2 repairs. One gap record, and the boot's report counts it once
 * (6e6a982: two records, generations 2 and 3, and 502 bytes). */
static void run_gap_once(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    seed(64007);
    nvs_clear();
    format_flash(kib);
    build_p02();
    p02_as_275();
    model_file_t *pending = model_of("/pending.jsonl");
    uint32_t pix = rd16(entry_address(pending->name, 2));
    wr16(pix * PAGE, 0x0155);
    wr16(lookup_address(pix), 0x0155);
    mark_unreadable(pending, 2);
    srr_report_t report;
    logged_count = 0;
    repair_boot(&report, &config);
    srp_gaps_t gaps = gaps_now();
    uint32_t naming = 0, bytes = 0, file_gaps = 0, file_bytes = 0;
    for (uint32_t g = 0; g < gaps.count; ++g)
        if (!strcmp(gaps.gap[g].name, pending->name)) {
            naming++;
            bytes += gaps.gap[g].length;
        }
    for (uint32_t i = 0; i < report.file_count; ++i)
        if (!strcmp(report.file[i].name, pending->name)) {
            file_gaps = report.file[i].gaps;
            file_bytes = report.file[i].unreadable_bytes;
        }
    CHECK(report.runs >= 2 && report.reclaim.blocks > 0 && naming == 1 && bytes == SPAN && report.gaps == 1 &&
              report.unreadable_bytes == SPAN && file_gaps == 1 && file_bytes == SPAN &&
              report.result == SRR_RESULT_REPAIRED_WITH_GAPS,
          "one span unreadable: %u runs, %s; %u records (%u bytes); the report says %u gaps, %u bytes (the file %u, %u)",
          report.runs, srr_result_name(report.result), naming, bytes, report.gaps, report.unreadable_bytes, file_gaps,
          file_bytes);
    mount_plain();
    for (uint32_t i = 0; i < model_count; ++i) check_custody(&model[i], 1, false);
    SPIFFS_unmount(&fs);
    printf("one unreadable span over %u runs of a boot: one gap record, %u bytes in the boot's report\n", report.runs,
           report.unreadable_bytes);
}

/* Round 2 review, R2-4: the round's window over the queue's own pages does
 * not verify (a worn cell) and the area will not take the record's source
 * bit (an I/O error). The round no longer ignores it: the record's attempts
 * are used up and the journal keeps ACTIVE with SOURCES, the round stops,
 * and the next boot makes the record a source, never redoing it over a
 * block the store has written since (the run after the round put the
 * record's pages back, possibly into that very block). And when neither
 * the attempts word nor the journal takes it either, nothing writes the
 * store again that boot (no remount, no run, the VFS kept off it): the next
 * boot decides the record from the block as the round left it. */
static void run_unmarked_source(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    for (int journaled = 1; journaled >= 0; --journaled) {
        const char *what = journaled ? "source bit refused" : "source bit, attempts and journal refused";
        seed(62000 + kib);
        nvs_clear();
        format_flash(kib);
        build_p02();
        p02_as_275();
        prefer_block = p02_queue_block();
        uint32_t b = prefer_block;
        CHECK(b != UINT32_MAX, "%s: no block of the queue's pages", what);
        worn_next_page = true;
        area_writes = 0;
        area_fail_from = 2; /* the record is area write 1; the source bit's two tries 2 and 3 */
        area_fail_count = journaled ? 2 : 4; /* and the attempts word's two, 4 and 5 */
        reclaim_saves_left = journaled ? -1 : 1; /* the round's first journal save only */
        logged_count = 0;
        srr_report_t report;
        repair_boot(&report, &config);
        worn_next_page = false;
        worn_address = UINT32_MAX;
        area_fail_from = area_fail_count = 0;
        reclaim_saves_left = -1;
        bool blocked = mount_blocked;
        round_active = false; /* blocked: the round never reached its remount (its oracle) */
        uint32_t flags = journal_flags();
        srx_record_t records[64];
        uint32_t slots[64], count = records_by_sequence(records, slots, 64);
        const srx_record_t *rec = NULL;
        for (uint32_t i = 0; i < count && !rec; ++i)
            if (records[i].block == b) rec = &records[i];
        CHECK(rec && !srx_is_source(rec) && rec->done == 0xFFFFFFFFU, "%s: no open record of block %u", what, b);
        bool closed = rd16(b * BLOCK + 252) == srx_spiffs_magic(flash_size / BLOCK, b);
        for (uint32_t e = 1; closed && e < 16; ++e) closed = rd16(lookup_address(b * 16 + e)) == 0xFFFF || journaled;
        if (journaled)
            CHECK(!blocked && report.result != SRR_RESULT_FAILED && (flags & SRP_RECLAIM_SOURCES) &&
                      (flags & SRP_RECLAIM_ACTIVE) && srx_attempts_used(rec->attempts) >= SRX_ATTEMPTS && closed,
                  "%s: %s, journal %x, attempts %u, block %u closed %d", what, srr_result_name(report.result), flags,
                  srx_attempts_used(rec->attempts), b, closed);
        else
            CHECK(blocked && report.reclaim.store_blocked && report.result == SRR_RESULT_FAILED &&
                      !strcmp(report.error_stage, "reclaim_journal") && !(flags & SRP_RECLAIM_SOURCES) &&
                      srx_attempts_used(rec->attempts) == 0 && closed,
                  "%s: %s at %s, store kept unmounted %d, journal %x, attempts %u, block %u closed %d", what,
                  srr_result_name(report.result), report.error_stage, blocked, flags, srx_attempts_used(rec->attempts),
                  b, closed);
        logged_count = 0;
        repair_boot(&report, &config);
        CHECK(boot_area.redone == 0 && boot_area.reasserted == 0 && boot_area.to_sources == 1,
              "%s: the next boot's recovery: %u redone, %u re-asserted, %u sources", what, boot_area.redone,
              boot_area.reasserted, boot_area.to_sources);
        boot_until_done_with(what, &config, &report);
        prefer_block = UINT32_MAX;
        check_models_exact(what);
        CHECK(!model_gaps(), "%s: %u gap records", what, model_gaps());
        check_reclaimed_structure(what);
        printf("%s: block %u closed, the record never redone (a source the next boot), custody byte-exact, no gap\n",
               what, b);
    }
}

/* Round 2 review, note 1: a source record's entry that ended (here
 * reported: its file was gone or held other bytes there) is never a source
 * again, and only a file's own fill bytes count as fill (JSON rows: NUL and
 * newlines, never '0'). Crafted records over a live queue: a reported entry
 * of span 5 while span 5 reads as a SALVAGE fill (a later generation's
 * bytes, lost and filled); and an open entry whose bytes differ from span 5
 * only where the live file has '0'. 6e6a982 put the record's bytes over the
 * live file's in both. */
static void run_source_generations(uint32_t kib)
{
    srr_config_t config = config_2760(false);
    for (int reported = 1; reported >= 0; --reported) {
        const char *what = reported ? "a reported entry over a filled span" : "an open entry over a span with '0's";
        seed(64700 + (uint64_t)reported);
        nvs_clear();
        format_flash(kib);
        model_reset();
        mount_plain();
        model_file_t *q = make_json_file("/pending.jsonl", 20000);
        const uint32_t k = 5;
        uint8_t live[SPAN], held[SPAN];
        memcpy(live, q->data + k * SPAN, SPAN);
        uint32_t zeros = 0;
        if (reported) {
            char row[600];
            size_t n = json_row(row, sizeof(row));
            for (uint32_t i = 0; i < SPAN; ++i) held[i] = (uint8_t)row[i % n];
            memset(live, 0, SPAN);
            spiffs_file f = SPIFFS_open(&fs, q->name, SPIFFS_O_RDWR, 0);
            CHECK(f >= 0 && SPIFFS_lseek(&fs, f, (s32_t)(k * SPAN), SPIFFS_SEEK_SET) == (s32_t)(k * SPAN) &&
                      SPIFFS_write(&fs, f, live, SPAN) == (s32_t)SPAN,
                  "%s: fill span %u", what, k);
            SPIFFS_close(&fs, f);
            memcpy(q->data + k * SPAN, live, SPAN);
        } else {
            memcpy(held, live, SPAN);
            for (uint32_t i = 0; i < SPAN; ++i)
                if (held[i] == '0') {
                    held[i] = '1';
                    zeros++;
                }
            CHECK(zeros, "%s: span %u has no '0'", what, k);
        }
        SPIFFS_unmount(&fs);
        /* One kept page: span k of the queue under its id and name. */
        uint32_t blocks = flash_size / BLOCK, block = blocks - 1, pix = rd16(entry_address(q->name, k));
        static uint8_t target[4096], sector[4096];
        memset(target, 0xFF, sizeof(target));
        uint16_t obj = object_of(q->name);
        target[0] = (uint8_t)obj;
        target[1] = (uint8_t)(obj >> 8);
        memcpy(target + PAGE, flash + pix * PAGE, 5);
        memcpy(target + PAGE + 5, held, SPAN);
        target[252] = (uint8_t)srx_spiffs_magic(blocks, block);
        target[253] = (uint8_t)(srx_spiffs_magic(blocks, block) >> 8);
        target[254] = target[255] = 0;
        srx_record_t h;
        memset(&h, 0, sizeof(h));
        h.reclaim_id = h.sequence = 1;
        h.image_tag = 0x2760;
        h.block = (uint16_t)block;
        h.kept_mask = 1;
        h.block_count = (uint16_t)blocks;
        h.new_crc = srp_crc32(0, target, sizeof(target));
        h.objects = 1;
        h.object[0].obj = obj;
        h.object[0].cls = (uint8_t)srp_classify(q->name).cls;
        snprintf(h.object[0].name, sizeof(h.object[0].name), "%s", q->name);
        srx_encode(&h, target, sector);
        uint32_t settle = ~SRX_SETTLE_SOURCE & (reported ? ~(1U << 16) : ~0U);
        for (int i = 0; i < 4; ++i) sector[SRX_OFF_SETTLE + i] = (uint8_t)(settle >> (8 * i));
        srx_record_t check;
        CHECK(srx_decode(sector, &check, NULL) && srx_is_source(&check), "%s: crafted record", what);
        memcpy(nvs.redo, sector, sizeof(sector));
        srp_reclaim_t journal = {.flags = SRP_RECLAIM_CLAIMED | SRP_RECLAIM_HELD, .reclaim_id = 1};
        srp_reclaim_seal(&journal);
        port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal));
        srr_report_t report;
        logged_count = 0;
        repair_boot(&report, &config);
        static uint8_t buffer[64 * 1024];
        mount_plain();
        int64_t got = read_file(q->name, buffer, sizeof(buffer));
        SPIFFS_unmount(&fs);
        bool exact = got == (int64_t)q->size && !memcmp(buffer, q->data, q->size);
        uint32_t differ = 0;
        for (uint32_t i = 0; got == (int64_t)q->size && i < q->size; ++i) differ += buffer[i] != q->data[i];
        bool from_record = false;
        for (uint32_t i = 0; i < report.file_count; ++i)
            from_record |= (report.file[i].actions & SRR_FILE_REDO_SOURCE) != 0;
        /* The open entry's file holds other bytes there: reported once. */
        CHECK(exact && !from_record && gaps_naming(q->name) == (reported ? 0U : 1U),
              "%s: %s; the queue reads %" PRId64 " of %u bytes, %u differ; bytes from the record %d; %u gap records",
              what, srr_result_name(report.result), got, q->size, differ, from_record, gaps_naming(q->name));
        printf("source generations, %s: the live bytes kept\n", what);
    }
}

/* ---- 2.7.7: the confirm-first exception on P02's field store ---------------- */
static bool within(uint32_t got, uint32_t want, uint32_t percent)
{
    uint32_t slack = (want * percent + 99U) / 100U;
    return got + slack >= want && got <= want + slack;
}

/* A cut at every write of the area test: the next discovery reads the area
 * READY (the slot this device's own torn write, a free one), never FOREIGN;
 * and without CLAIMED first it would read FOREIGN (why the glue saves it). */
static void p02field_area_test_cuts(void)
{
    static uint8_t work[4096];
    snapshot_t image = snapshot_take();
    uint32_t cuts = 0, foreign_unclaimed = 0;
    for (uint64_t k = 1;; ++k) {
        snapshot_put(&image);
        seed(65000 + k);
        cut_at = flash_ops + k;
        bool cut = false;
        if (setjmp(cut_point)) cut = true;
        else (void)p02field_area_test();
        cut_at = 0;
        if (!cut) break;
        cuts++;
        srx_area_t area;
        srx_discover(&redo_io, true, work, &area);
        CHECK(area.state == SRX_AREA_READY, "area test cut %" PRIu64 ": the area reads %s", k, srx_area_name(area.state));
        srx_discover(&redo_io, false, work, &area);
        foreign_unclaimed += area.state == SRX_AREA_FOREIGN;
    }
    snapshot_put(&image);
    snapshot_free(&image);
    CHECK(cuts >= 2 && foreign_unclaimed, "area test: %u cuts, %u foreign without CLAIMED", cuts, foreign_unclaimed);
    printf("p02field: the redo area test cut at each of its %u writes: the area stays READY (CLAIMED first; %u of "
           "those would read FOREIGN without it)\n", cuts, foreign_unclaimed);
}

static void run_reclaim_p02field(uint32_t kib, uint32_t variant)
{
    static const char *const names[] = {"hidden header", "unwindowed files", "minimum gain", "custody short"};
    p02field_variant = variant;
    seed(64000 + kib + variant * 17 + env_u64("REVIEW_BUILD_SALT") * 7919U);
    nvs_clear();
    format_flash(kib);
    p02field_prepare();
    const srr_report_t *u = &p02field_unconfirmed;
    const srr_reclaim_report_t *x = &u->reclaim;
    uint32_t mine = 0;
    for (uint32_t b = 0; b < flash_size / BLOCK; ++b) mine += p02field_custody[b];
    printf("p02field %u KiB %s: unconfirmed 2.7.7 %s at %s (%ld), short %s, %u rounds, %u candidates (%u holding "
           "nothing any image reads, %u custody, %u gain), %u index, %u excluded (%u hidden, %u unwindowed, %u last "
           "page, %u index flag, %u busy, %u mixed; most %s, %u blocks), %u weak magic; %u pages short of %u, %u "
           "erased at mount; the harness counts %u custody blocks\n",
           kib, names[variant], srr_result_name(u->result), u->error_stage, (long)u->error_code, x->short_reason,
           x->rounds, x->candidates, x->unconfirmed_blocks, x->custody_blocks, x->custody_gain, x->index_blocks,
           x->excluded_blocks, x->excluded_hidden, x->excluded_unwindowed, x->excluded_last_page,
           x->excluded_index_flag, x->excluded_busy, x->excluded_mixed, x->excluded_file, x->excluded_file_blocks,
           x->low_magic_blocks, x->want, x->target, x->erased_at_mount, mine);
    CHECK(u->result == SRR_RESULT_DEFERRED && !strcmp(u->error_stage, "full") && u->error_code == SPIFFS_ERR_FULL &&
              !strcmp(x->trigger, "full") && !strcmp(x->short_reason, "candidate") && !x->rounds && !x->candidates &&
              !x->unconfirmed_blocks && !u->gaps && !u->files_unheld_custody && !u->manifest_active &&
              x->mount_measured && !x->erased_at_mount && x->want > 0 && x->target > x->want - 1,
          "p02field %s: the unconfirmed boot is not P02's 21 October report", names[variant]);
    /* The harness's own reading of the confirmed rules agrees with the planner. */
    CHECK(mine == x->custody_blocks, "p02field %s: the harness counts %u custody blocks, the planner %u", names[variant],
          mine, x->custody_blocks);
    CHECK(x->excluded_hidden + x->excluded_unwindowed + x->excluded_last_page + x->excluded_index_flag +
                  x->excluded_mixed + x->excluded_busy == x->excluded_blocks,
          "p02field: the excluded breakdown adds up to %u of %u", x->excluded_hidden + x->excluded_unwindowed +
          x->excluded_last_page + x->excluded_index_flag + x->excluded_mixed + x->excluded_busy, x->excluded_blocks);
    if (kib >= 8192 && variant == P02F_HIDDEN) {
        /* The fit to the field: custody and index within 10%, excluded within
         * 2%, the weak magics, the shortfall within 4 pages. */
        CHECK(within(x->custody_blocks, 67, 10) && within(x->index_blocks, 88, 10) &&
                  within(x->excluded_blocks, 1817, 2) && x->low_magic_blocks == 68 && x->want + 4 >= 34 &&
                  x->want <= 38,
              "p02field: %u custody, %u index, %u excluded, %u weak, %u short: not P02's histogram", x->custody_blocks,
              x->index_blocks, x->excluded_blocks, x->low_magic_blocks, x->want);
        /* E2 is what excludes the blocks, and the hidden stage is the object. */
        CHECK(!strcmp(x->excluded_file, P02F_REMOVED) && x->excluded_hidden + x->excluded_mixed > x->excluded_unwindowed,
              "p02field: the breakdown names %s", x->excluded_file);
    }
    if (variant == P02F_UNWINDOWED)
        CHECK(!x->excluded_hidden && strcmp(x->excluded_file, P02F_REMOVED),
              "p02field E1: %u blocks excluded by a hidden header (%s)", x->excluded_hidden, x->excluded_file);
    if (variant == P02F_MIN_GAIN) CHECK(x->custody_gain == x->custody_blocks, "p02field: minimum gain %u of %u blocks",
                                        x->custody_gain, x->custody_blocks);
    /* The glue's area test, then the gate on this boot's own report. */
    srx_test_t test = p02field_area_test();
    CHECK(test == SRX_TEST_PASSED, "p02field: the area test %s", srx_test_name(test));
    if (variant == P02F_HIDDEN) p02field_area_test_cuts();
    sx_reason_t gate = p02field_gate(u, test);
    /* The worst case at 1 MiB has fewer custody blocks than pages short:
     * every one drops a page, and that is still too few. */
    bool short_of_room = variant == P02F_SHORT || (variant == P02F_MIN_GAIN && kib < 8192);
    sx_reason_t expected = short_of_room ? SX_CUSTODY_SHORT : SX_OK;
    CHECK(gate == expected, "p02field %s: the gate says %s, expected %s", names[variant], sx_reason_name(gate),
          sx_reason_name(expected));
    CHECK(short_of_room == (x->custody_gain < x->want), "p02field %s: %u custody pages for %u short", names[variant],
          x->custody_gain, x->want);
    if (short_of_room) {
        printf("p02field %u KiB %s: %u custody pages for %u short: the gate refuses %s, the image rolls back as 2.7.6\n",
               kib, names[variant], x->custody_gain, x->want, sx_reason_name(gate));
        return;
    }
    /* The confirmed boot: the same image, pending_verify now false. */
    srr_config_t config = config_2770(false);
    srr_report_t report;
    uint32_t checked = round_checks;
    logged_count = 0;
    repair_boot(&report, &config);
    const srr_reclaim_report_t *c = &report.reclaim;
    static sx_repair_t after;
    sx_from_srr(&report, &after);
    after.storage_allowed = !mount_blocked;
    CHECK(report.result == SRR_RESULT_REPAIRED && !report.gaps && c->blocks > 0 && round_checks - checked == c->blocks &&
              c->erased_after >= x->want && !c->failed && !c->held_mismatch && !c->waiting &&
              sx_round_outcome(&after) == SX_ROUND_OK,
          "p02field %s confirmed: %s at %s, %u gaps, %u blocks, %u erased after (%u short), %u failed, outcome %s",
          names[variant], srr_result_name(report.result), report.error_stage, report.gaps, c->blocks, c->erased_after,
          x->want, c->failed, sx_outcome_name(sx_round_outcome(&after)));
    /* Every block the round reformatted was a custody block: no excluded,
     * index or weak-magic block touched. */
    static uint8_t sector[4096];
    uint32_t records = 0;
    for (uint32_t slot = 0; slot < REDO_BYTES / 4096; ++slot) {
        srx_record_t r;
        memcpy(sector, nvs.redo + slot * 4096, 4096);
        if (!srx_decode(sector, &r, NULL)) continue;
        records++;
        CHECK(r.block < flash_size / BLOCK && p02field_custody[r.block] && !(r.flags & SRX_FLAG_UNCONFIRMED),
              "p02field: block %u reformatted, not a custody block", r.block);
    }
    CHECK(records == c->blocks, "p02field: %u records for %u blocks", records, c->blocks);
    check_models_exact("p02field confirmed");
    verify_reclaimed("p02field confirmed");
    printf("p02field %u KiB %s: the gate ELIGIBLE; the confirmed boot reformatted %u custody blocks (%u deleted "
           "pages erased, %u kept, %u erased after), then REPAIRED with no gap, every file byte-exact\n",
           kib, names[variant], c->blocks, c->pages_freed, c->pages_kept, c->erased_after);
}

/* Critique 4: confirmation keeps the image's tag, so the attempts guard's
 * state carries over to the round boot. Two runs of this image already ended
 * by the deadline (the third ends the guard), or the guard already stopped
 * repairs for it: the round boot only reports, and the outcome is no retry. */
static void run_p02field_guard(uint32_t kib)
{
    p02field_variant = P02F_HIDDEN;
    for (int mode = 0; mode < 2; ++mode) {
        seed(64500 + kib + (uint64_t)mode);
        nvs_clear();
        format_flash(kib);
        p02field_prepare();
        if (mode == 0) store_state(SRP_PHASE_SURVEY, 2, 2, 0, P02F_TAG);
        else store_state(SRP_PHASE_IDLE, 0, 0, SRP_STATE_REPAIR_DISABLED, P02F_TAG);
        srr_config_t config = config_2770(false);
        config.previous_failed = mode == 0;
        srr_report_t report;
        repair_boot(&report, &config);
        static sx_repair_t after;
        sx_from_srr(&report, &after);
        after.storage_allowed = true;
        CHECK(report.result == SRR_RESULT_REPORT_ONLY && !report.reclaim.blocks &&
                  sx_round_outcome(&after) == SX_ROUND_GUARD && !sx_outcome_retryable(sx_round_outcome(&after)),
              "p02field guard %d: %s, %u blocks", mode, srr_result_name(report.result), report.reclaim.blocks);
        /* With another image's tag (the design's first proof) the guard
         * would have let the round run: the stop is per image (counted
         * failures stop every image). */
        if (mode == 0) continue;
        nvs_clear();
        format_flash(kib);
        seed(64500 + kib + (uint64_t)mode);
        p02field_prepare();
        if (mode == 0) store_state(SRP_PHASE_SURVEY, 2, 2, 0, P02F_TAG);
        else store_state(SRP_PHASE_IDLE, 0, 0, SRP_STATE_REPAIR_DISABLED, P02F_TAG);
        config.image_tag = P02F_TAG + 1;
        repair_boot(&report, &config);
        CHECK(report.result != SRR_RESULT_REPORT_ONLY, "p02field guard %d: another tag still only reports", mode);
    }
    printf("p02field %u KiB: the attempts guard's state carries over to the round boot (same image tag): REPORT_ONLY, "
           "no round, no retry; with repairs stopped for the image, another image's tag would have run it\n", kib);
}

/* 2.7.7: every refusal rolls back exactly as 2.7.6 does. The unconfirmed
 * 2.7.7 boot (and the glue's area test) leaves the store byte for byte as
 * 2.7.6's unconfirmed boot leaves it, so the old image the bootloader takes
 * after a refusal finds 2.7.6's store; the gate refuses an unheld custody
 * file and a failed area test on that boot's own report; and the
 * exception's round boot (critique 11) takes no custody block when its run
 * could not hold every custody file: short "hold", nothing written, a
 * retryable outcome. */
static void run_p02field_refusals(uint32_t kib)
{
    p02field_variant = P02F_HIDDEN;
    seed(64700 + kib);
    nvs_clear();
    format_flash(kib);
    build_p02field();
    p02_as_275();
    snapshot_t built = snapshot_take();
    srr_config_t c276 = config_2760(true), c277 = config_2770(true);
    srr_report_t r276, r277;
    repair_boot(&r276, &c276);
    uint8_t *store276 = malloc(flash_size);
    memcpy(store276, flash, flash_size);
    snapshot_put(&built);
    repair_boot(&r277, &c277);
    CHECK(p02field_area_test() == SRX_TEST_PASSED, "p02field refusals: the area test");
    uint32_t differ = 0;
    for (uint32_t i = 0; i < flash_size; ++i) differ += store276[i] != flash[i];
    CHECK(!differ && r276.result == r277.result && r277.result == SRR_RESULT_DEFERRED && !r277.reclaim.blocks,
          "p02field refusals: the unconfirmed 2.7.7 boot left %u store bytes unlike 2.7.6's (%s, %s)", differ,
          srr_result_name(r276.result), srr_result_name(r277.result));
    CHECK(p02field_gate(&r277, SRX_TEST_PASSED) == SX_OK, "p02field refusals: the gate on the ordinary boot");
    CHECK(p02field_gate(&r277, SRX_TEST_FAILED) == SX_AREA_TEST, "p02field refusals: a failed area test");
    CHECK(p02field_gate(&r277, SRX_TEST_NO_SLOT) == SX_AREA_TEST, "p02field refusals: no slot for the area test");
    snapshot_t unconfirmed = snapshot_take();
    /* Too little memory to hold the queue: the gate refuses on the run's own
     * report, and the store is again 2.7.6's. */
    snapshot_put(&built);
    srr_config_t starved = c277;
    starved.hold_budget = 64U * 1024U;
    srr_report_t r;
    repair_boot(&r, &starved);
    sx_reason_t gate = p02field_gate(&r, SRX_TEST_PASSED);
    CHECK(r.files_unheld_custody > 0 && gate == SX_CUSTODY_UNHELD && !r.reclaim.blocks,
          "p02field refusals: %u custody files unheld, the gate says %s", r.files_unheld_custody, sx_reason_name(gate));
    /* The exception's round boot whose run cannot hold every custody file. */
    snapshot_put(&unconfirmed);
    srr_config_t round = config_2770(false);
    round.hold_budget = 64U * 1024U;
    uint8_t *before = malloc(flash_size);
    memcpy(before, flash, flash_size);
    uint32_t checked = round_checks;
    repair_boot(&r, &round);
    static sx_repair_t after;
    sx_from_srr(&r, &after);
    after.storage_allowed = !mount_blocked;
    sx_outcome_t outcome = sx_round_outcome(&after);
    uint32_t pending_records = 0, sources = 0, open = 0;
    CHECK(!r.reclaim.blocks && round_checks == checked && !strcmp(r.reclaim.short_reason, "hold") &&
              r.reclaim.held_out > 0 && outcome == SX_ROUND_HOLD && sx_outcome_retryable(outcome) && !r.gaps &&
              !area_records(&pending_records, &sources, &open),
          "p02field refusals: the unheld round boot took %u blocks, short %s (%u held out), outcome %s, %u gaps",
          r.reclaim.blocks, r.reclaim.short_reason, r.reclaim.held_out, sx_outcome_name(outcome), r.gaps);
    uint32_t written = 0;
    for (uint32_t i = 0; i < flash_size; ++i) written += before[i] != flash[i];
    CHECK(!written, "p02field refusals: the unheld round boot changed %u store bytes", written);
    free(before);
    free(store276);
    snapshot_free(&built);
    snapshot_free(&unconfirmed);
    printf("p02field %u KiB refusals: the unconfirmed 2.7.7 boot leaves 2.7.6's store byte for byte; the gate refuses "
           "an unheld custody file and a failed area test; the round boot without every custody file held takes no "
           "block (short hold, retryable)\n", kib);
}

static void run_reclaim(uint32_t kib, const char *only)
{
    const char *part = only && !strncmp(only, "reclaim-", 8) ? only + 8 : NULL;
#define RECLAIM_PART(name) (!part || !strcmp(part, name))
    bool full = getenv("REVIEW_FULL") != NULL;
    uint32_t stride = full ? 1 : kib > 1024 ? 41 : 3;
    if (RECLAIM_PART("p02")) {
        run_reclaim_p02(kib, false, false);
        run_reclaim_p02(kib, true, false);
        run_reclaim_p02(kib, false, true);
        run_reclaim_p02_direct(kib);
    }
    if (RECLAIM_PART("area")) run_reclaim_area(kib);
    /* 2.7.7: the confirm-first exception on P02's field store (CI at 8 MiB):
     * the fit, the gate, the confirmed round, the worst case, too few custody
     * pages, the guard carried over, and cuts in the confirmed round. */
    if (RECLAIM_PART("p02field")) {
        run_reclaim_p02field(kib, P02F_HIDDEN);
        run_reclaim_p02field(kib, P02F_UNWINDOWED);
        run_reclaim_p02field(kib, P02F_MIN_GAIN);
        run_reclaim_p02field(kib, P02F_SHORT);
        run_p02field_guard(kib);
        run_p02field_refusals(kib);
        p02field_variant = P02F_HIDDEN;
        run_reclaim_cuts("p02field confirmed", build_p02field, kib, false, AFTER_SAME, stride);
        if (kib >= 8192) {
            p02field_variant = P02F_MIN_GAIN;
            run_reclaim_cuts("p02field confirmed, minimum gain", build_p02field, kib, false, AFTER_SAME,
                             full ? 1 : stride * 3);
        }
        p02field_variant = P02F_HIDDEN;
    }
    if (RECLAIM_PART("mirror")) {
        run_reclaim_mirror(kib);
        /* The device's mirror through cuts in the round (low 4). */
        use_mirror = true;
        run_reclaim_cuts("p02 confirmed, mirrored", build_p02, kib, false, AFTER_SAME, full ? 1 : kib > 1024 ? 83 : 5);
        use_mirror = false;
    }
    if (RECLAIM_PART("redo")) run_redo_cuts(kib);
    /* Round 1 review of the implementation. */
    if (RECLAIM_PART("stale")) {
        run_unread_area(kib, build_p02, "p02, a stale cursor", CURSOR_STALE);
        run_unread_area(kib, build_p02, "p02, a cursor before the damage", CURSOR_BEFORE);
        run_unread_area(kib, build_p02, "p02, a verified cursor past the window", CURSOR_PAST);
        run_unread_area(kib, build_gc_unsafe, "gc-unsafe, a cursor before the damage", CURSOR_BEFORE);
    }
    if (RECLAIM_PART("restore")) run_restore_room(kib);
    if (RECLAIM_PART("torn")) run_torn_magic(kib);
    if (RECLAIM_PART("sources")) run_sources_memory(kib);
    /* Round 2 review of the implementation (`reclaim-round2` runs them all). */
#define ROUND2_PART(name) (RECLAIM_PART(name) || !strcmp(part, "round2"))
    if (ROUND2_PART("worn")) run_worn_round(kib);
    if (ROUND2_PART("open")) run_open_record(kib);
    if (ROUND2_PART("gaps")) run_gap_once(kib);
    if (ROUND2_PART("unmarked")) run_unmarked_source(kib);
    if (ROUND2_PART("generations")) run_source_generations(kib);
#undef ROUND2_PART
    if (RECLAIM_PART("cuts")) {
        run_reclaim_cuts("p02 confirmed", build_p02, kib, false, AFTER_SAME, stride);
        run_reclaim_cuts("gc-unsafe confirmed", build_gc_unsafe, kib, false, AFTER_SAME, stride);
        /* A worn cell in the first window (R2-1): cuts in its failed
         * verifies, its close and source bit, and the windows after it. */
        sweep_worn = true;
        run_reclaim_cuts("p02 confirmed, a worn first window", build_p02, kib, false, AFTER_SAME, stride);
        sweep_worn = false;
    }
    if (RECLAIM_PART("rollback")) {
        run_reclaim_cuts("p02 unconfirmed, old image", build_p02, kib, true, AFTER_OLD_IMAGE, stride);
        run_reclaim_cuts("p02 unconfirmed, released 2.7.5", build_p02, kib, true, AFTER_RELEASED_275,
                         full ? 1 : kib > 1024 ? 83 : 7);
        sweep_worn = true;
        run_reclaim_cuts("p02 unconfirmed, a worn first window, old image", build_p02, kib, true, AFTER_OLD_IMAGE,
                         full ? 1 : kib > 1024 ? 83 : 7);
        sweep_worn = false;
    }
#undef RECLAIM_PART
    printf("reclaim: %u reformatted blocks checked page by page (%u kept a deleted entry 0, %u failed a verify: %u "
           "closed, the others left for SPIFFS to erase)\n", round_checks_total, round_entry0_kept, round_failed_blocks,
           round_closed_blocks);
}

/* ---- organic: the power-cut simulator's workload ----------------------- */
static const uint8_t *find_seq(const uint8_t *row, size_t length)
{
    static const char key[] = "\"seq\":";
    for (size_t i = 0; i + sizeof(key) - 1 <= length; ++i)
        if (!memcmp(row + i, key, sizeof(key) - 1)) return row + i;
    return NULL;
}

#define ORGANIC_MAX_SEQ (1U << 18)
/* 1: committed. 2: committed after an append failed in the same boot. A
 * failed append (ENOSPC on a crammed store) can leave its bytes past the end
 * of the last page and index entries past the end; SPIFFS's next append
 * programs over them in place, so it is the AND of two rows on flash before
 * any repair runs. Such rows are counted apart from rows a repair lost. Each
 * committed row's offset, length and the repair generation it was committed
 * after: a missing row is explained only by a later gap record over it. */
static uint8_t *committed, *row_queue;
static uint32_t *row_offset, *row_generation;
static uint16_t *row_length;
static uint32_t next_seq, organic_ticks, organic_cut_count, organic_reclaims, organic_segments, organic_gc_stops;
static uint32_t organic_blocked; /* verifications of a queue a run without room left unreadable */
static uint32_t organic_gc_unsafe; /* runs that handed over a store SPIFFS's GC cannot clean */
static uint32_t organic_rounds, organic_reclaimed; /* 2.7.6: rounds of the last resort, blocks reformatted */
static bool organic_blocked_now[2]; /* the last verification's */
static srr_report_t organic_last;  /* the last finished run's report */
static bool append_failed_this_boot;
/* Odd seeds: pending.jsonl is retired whenever its drain catches up, and
 * the blocked queue (never retired) fills the store meanwhile. Even seeds:
 * pending only, never retired, so the store runs crammed. */
static bool organic_reclaiming;
static const char *const organic_queue[2] = {"/pending.jsonl", "/blocked_identity.jsonl"};
/* The "wb" lane: a queue segment rewritten from offset 0, as dq_append opens
 * a new segment; its committed bytes once the write and close returned. */
static uint8_t segment_model[1024];
static uint32_t segment_size, segment_generation;

static uint32_t repair_generation(void)
{
    srp_state_t state;
    return port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 ? state.generation : 0;
}

static uint32_t current_size(const char *name)
{
    spiffs_stat st;
    if (SPIFFS_stat(&fs, name, &st) != SPIFFS_OK) {
        SPIFFS_clearerr(&fs);
        return 0;
    }
    return st.size == SPIFFS_UNDEFINED_LEN ? 0 : st.size;
}

static bool organic_append(uint32_t queue)
{
    char row[600];
    row_seq = next_seq;
    size_t n = json_row(row, sizeof(row));
    uint32_t seq = next_seq++;
    if (seq >= ORGANIC_MAX_SEQ) return false;
    uint32_t offset = current_size(organic_queue[queue]);
    bool ok = append_bytes(organic_queue[queue], row, (uint32_t)n);
    if (ok) {
        committed[seq] = append_failed_this_boot ? 2 : 1;
        row_queue[seq] = (uint8_t)queue;
        row_offset[seq] = offset;
        row_length[seq] = (uint16_t)n;
        row_generation[seq] = repair_generation();
    } else append_failed_this_boot = true;
    return ok;
}

static void organic_segment(void)
{
    /* Uncommitted from the moment "wb" truncates it. */
    segment_size = 0;
    int32_t free_blocks = fs.free_blocks;
    spiffs_file f = SPIFFS_open(&fs, "/qo0000002a.q", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    if (f < 0) {
        s32_t e = SPIFFS_errno(&fs);
        SPIFFS_clearerr(&fs);
        /* With fewer than two free blocks SPIFFS's own GC in the truncate
         * can run out of erased pages half way through a block (it reports
         * NOT_FOUND): the store is full, and the next repair mends what
         * that GC left. Any other refusal is a lane the repair left wedged. */
        if ((e == SPIFFS_ERR_NOT_FOUND || e == SPIFFS_ERR_OUT_OF_FILE_DESCS) && free_blocks < 2) {
            /* OUT_OF_FILE_DESCS: SPIFFS_close keeps its descriptor when its
             * cache flush fails on such a store (the next boot frees it). */
            organic_gc_stops++;
            return;
        }
        if (e != SPIFFS_ERR_FULL) dump_write();
        CHECK(e == SPIFFS_ERR_FULL, "organic: the segment refuses \"wb\" (%d): its lane is wedged", e);
        return;
    }
    uint8_t record[sizeof(segment_model)];
    char row[600];
    size_t n = json_row(row, sizeof(row));
    if (n > sizeof(record) - 16) n = sizeof(record) - 16;
    uint32_t words[4] = {0x32514c5aU, (uint32_t)n, organic_segments, rnd()};
    memcpy(record, words, 16);
    memcpy(record + 16, row, n);
    bool ok = SPIFFS_write(&fs, f, record, (s32_t)(16 + n)) == (s32_t)(16 + n);
    if (SPIFFS_close(&fs, f) < 0) ok = false;
    SPIFFS_clearerr(&fs);
    if (!ok) return;
    memcpy(segment_model, record, 16 + n);
    segment_size = (uint32_t)(16 + n);
    segment_generation = repair_generation();
    organic_segments++;
}

static void organic_tick(void)
{
    organic_ticks++;
    if (organic_reclaiming && organic_ticks % 37 == 0 && (rnd() & 1)) {
        /* The drain caught up: every row is settled and lq_reclaim retires
         * the file; the next punch creates it again (often with the same
         * object id, its first append programming the header in place). */
        for (uint32_t k = 0; k < next_seq && k < ORGANIC_MAX_SEQ; ++k)
            if (!row_queue[k]) committed[k] = 0;
        if (SPIFFS_remove(&fs, "/pending.jsonl") == SPIFFS_OK) organic_reclaims++;
        SPIFFS_clearerr(&fs);
    }
    (void)organic_append(0);
    if (organic_reclaiming) (void)organic_append(1);
    if (organic_ticks % 3 == 0) {
        static const uint8_t probe[32] = {1};
        spiffs_file f = SPIFFS_open(&fs, "/persistence-probe", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
        if (f >= 0) {
            (void)SPIFFS_write(&fs, f, (void *)probe, sizeof(probe));
            SPIFFS_close(&fs, f);
        }
        (void)SPIFFS_remove(&fs, "/persistence-probe");
        SPIFFS_clearerr(&fs);
    }
    if (organic_ticks % 5 == 0) {
        char id[80];
        int n = snprintf(id, sizeof(id), "%064x\n", rnd());
        (void)append_bytes("/acked_uids.txt", id, (uint32_t)n);
    }
    if (organic_ticks % 23 == 0) organic_segment();
    if (organic_ticks % 40 == 0) {
        static uint8_t catalog[12000];
        for (size_t i = 0; i < sizeof(catalog); ++i) catalog[i] = (uint8_t)rnd();
        spiffs_file f = SPIFFS_open(&fs, "/add_identities.tmp", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
        if (f >= 0) {
            (void)SPIFFS_write(&fs, f, catalog, sizeof(catalog));
            SPIFFS_close(&fs, f);
            (void)SPIFFS_remove(&fs, "/add_identities.enc");
            (void)SPIFFS_rename(&fs, "/add_identities.tmp", "/add_identities.enc");
        }
        SPIFFS_clearerr(&fs);
    }
}

/* Debug: index entries of /pending.jsonl past its end (the next append ANDs
 * its page index into such an entry). */
static void dump_pending_index(const char *when)
{
    uint32_t header = 0;
    for (uint32_t pix = 0; pix < flash_size / PAGE && !header; ++pix)
        if (pix % 16 && header_named(pix, "/pending.jsonl")) header = pix;
    if (!header) return;
    uint16_t obj = rd16(header * PAGE) & 0x7FFF;
    uint32_t size = 0;
    memcpy(&size, flash + header * PAGE + 8, 4);
    if (size == 0xFFFFFFFF) size = 0;
    uint32_t spans = (size + SPAN - 1) / SPAN;
    for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix) {
        uint32_t a = pix * PAGE;
        if (pix % 16 == 0 || rd16(a) != (uint16_t)(obj | 0x8000)) continue;
        uint8_t flags = flash[a + 4];
        if (flags & (SPIFFS_PH_FLAG_USED | SPIFFS_PH_FLAG_FINAL | SPIFFS_PH_FLAG_INDEX)) continue;
        if (!(flags & SPIFFS_PH_FLAG_DELET)) continue;
        uint32_t j = rd16(a + 2);
        uint32_t first = j == 0 ? 0 : 103 + (j - 1) * 124, entries = j == 0 ? 103 : 124;
        uint32_t table = j == 0 ? 49 : 8;
        for (uint32_t i = 0; i < entries; ++i) {
            uint16_t ref = rd16(a + table + i * 2);
            if (first + i >= spans && ref != 0xFFFF)
                printf("  %s: pending (size %u spans %u) index page %04x span %u entry %u (span %u) = %04x lu %04x\n",
                       when, size, spans, pix, j, i, first + i, ref, rd16(lookup_address(pix)));
        }
    }
}

/* Debug: which committed rows a plain mount reads before the repair. */
static uint8_t organic_before[ORGANIC_MAX_SEQ];
static void organic_probe_before(void)
{
    static uint8_t buffer[8 * 1024 * 1024];
    memset(organic_before, 0, sizeof(organic_before));
    if (mount_with(plain_check_callback) != SPIFFS_OK) return;
    int64_t got = read_file("/pending.jsonl", buffer, sizeof(buffer));
    printf("  organic: before repair pending reads %" PRId64 "\n", got);
    for (int64_t start = 0, i = 0; i < got; ++i) {
        if (buffer[i] != '\n') continue;
        const char *seq = (const char *)find_seq(buffer + start, (size_t)(i - start));
        if (seq) {
            unsigned long value = strtoul(seq + 6, NULL, 10);
            if (value < ORGANIC_MAX_SEQ) organic_before[value] = 1;
        }
        start = i + 1;
    }
    SPIFFS_unmount(&fs);
}

static bool gap_names(const srp_gaps_t *gaps, const char *name, uint32_t generation, uint32_t offset, uint32_t length)
{
    for (uint32_t g = 0; g < gaps->count; ++g) {
        const srp_gap_t *gap = &gaps->gap[g];
        if (!strcmp(gap->name, name) && gap->generation > generation && gap->offset < offset + length &&
            offset < gap->offset + gap->length)
            return true;
    }
    return false;
}

/* Erased pages on flash (lookup free, page erased). */
static uint32_t erased_pages(void)
{
    uint32_t n = 0;
    for (uint32_t pix = 0; pix < flash_size / PAGE; ++pix) {
        if (pix % 16 == 0 || rd16(lookup_address(pix)) != 0xFFFF) continue;
        bool erased = true;
        for (uint32_t i = 0; i < PAGE && erased; ++i) erased = flash[pix * PAGE + i] == 0xFF;
        n += erased;
    }
    return n;
}

static void organic_verify(uint32_t *lost, uint32_t *explained, uint32_t *anded)
{
    static uint8_t buffer[8 * 1024 * 1024];
    static uint8_t present[ORGANIC_MAX_SEQ];
    memset(present, 0, sizeof(present));
    int64_t got = 0;
    bool blocked[2] = {false, false};
    for (uint32_t q = 0; q < 2; ++q) {
        got = exists(organic_queue[q]) ? read_file(organic_queue[q], buffer, sizeof(buffer)) : 0;
        if (got < 0) {
            /* Only a store with no room for the work (SPIFFS's own GC in the
             * workload's writes used every erased page): the run deferred,
             * said how many pages it needed and had, the flash agrees, and
             * the file's unreadable spans are reported. Its rows stay on
             * flash, checked once a later run has room. */
            const srr_report_t *l = &organic_last;
            bool reported = false;
            for (uint32_t i = 0; i < l->file_count; ++i)
                reported |= !strcmp(l->file[i].name, organic_queue[q]) && l->file[i].blocked_spans > 0;
            if (l->result == SRR_RESULT_DEFERRED && !strcmp(l->error_stage, "full") && l->room_erased < l->room_needed &&
                erased_pages() <= l->room_erased && reported) {
                blocked[q] = true;
                organic_blocked++;
                continue;
            }
            dump_write();
        }
        CHECK(got >= 0, "organic: %s unreadable after repair", organic_queue[q]);
        for (int64_t start = 0, i = 0; i < got; ++i) {
            if (buffer[i] != '\n') continue;
            const char *seq = (const char *)find_seq(buffer + start, (size_t)(i - start));
            if (seq) {
                unsigned long value = strtoul(seq + 6, NULL, 10);
                if (value < ORGANIC_MAX_SEQ && row_queue[value] == q) present[value] = 1;
            }
            start = i + 1;
        }
    }
    srp_gaps_t gaps = gaps_now();
    for (uint32_t s = 0; s < next_seq && s < ORGANIC_MAX_SEQ; ++s) {
        if (!committed[s] || present[s] || blocked[row_queue[s]]) continue;
        bool named = gap_names(&gaps, organic_queue[row_queue[s]], row_generation[s], row_offset[s], row_length[s]);
        if (committed[s] == 2) (*anded)++;
        else if (named) (*explained)++;
        else (*lost)++;
        if (verbose) printf("  organic: committed row %u (offset %u) missing after cut %u (tick %u)%s; readable before "
                            "repair: %d\n", s, row_offset[s], organic_cut_count, organic_ticks,
                            named ? " (gap recorded)" : "", organic_before[s]);
        committed[s] = 0;
    }
    /* The "wb" lane's last committed segment. */
    if (segment_size) {
        got = exists("/qo0000002a.q") ? read_file("/qo0000002a.q", buffer, sizeof(buffer)) : 0;
        CHECK(got >= 0, "organic: the segment is unreadable after repair");
        for (uint32_t k = 0; k < segment_size; ++k) {
            if ((uint64_t)got > k && buffer[k] == segment_model[k]) continue;
            CHECK(gap_names(&gaps, "/qo0000002a.q", segment_generation, k, 1),
                  "organic: segment byte %u lost (file %" PRId64 " of %u) and no gap record names it", k, got,
                  segment_size);
            (*explained)++;
            segment_size = 0;
            break;
        }
    }
    organic_blocked_now[0] = blocked[0];
    organic_blocked_now[1] = blocked[1];
    /* Health: the queues take appends. */
    for (uint32_t q = 0; q < 2; ++q) {
        if (!exists(organic_queue[q]) || blocked[q]) continue;
        void *file = (void *)rel_open_append(organic_queue[q]);
        CHECK(file != NULL, "organic: %s refuses appends after repair (errno %d)", organic_queue[q], errno);
        shim_fclose(file);
    }
}

/* REPAIR_DUMP=<prefix>: the flash and NVS before the last repair, written
 * when the workload's oracle fails, for --replay=<prefix>. */
static uint8_t *dump_flash;
static __typeof__(nvs) dump_nvs;
static void dump_write_to(const char *prefix);
static void dump_before_repair(void)
{
    if (!getenv("REPAIR_DUMP")) return;
    if (!dump_flash) dump_flash = malloc(8U * 1024 * 1024);
    memcpy(dump_flash, flash, flash_size);
    memcpy(&dump_nvs, &nvs, sizeof(nvs));
    /* REPAIR_DUMP_AT=<run>: the store before that run, whatever follows. */
    if (getenv("REPAIR_DUMP_AT") && repair_generation() + 1 == (uint32_t)atoi(getenv("REPAIR_DUMP_AT"))) {
        char prefix[512];
        snprintf(prefix, sizeof(prefix), "%s-at", getenv("REPAIR_DUMP"));
        dump_write_to(prefix);
    }
}
static void dump_write_to(const char *prefix)
{
    if (!prefix || !dump_flash) return;
    char path[512];
    snprintf(path, sizeof(path), "%s.flash", prefix);
    FILE *f = fopen(path, "wb");
    fwrite(dump_flash, 1, flash_size, f);
    fclose(f);
    snprintf(path, sizeof(path), "%s.nvs", prefix);
    f = fopen(path, "wb");
    fwrite(&dump_nvs, 1, sizeof(dump_nvs), f);
    fclose(f);
    printf("  dumped the store before the last repair to %s.*\n", prefix);
}
static void dump_write(void) { dump_write_to(getenv("REPAIR_DUMP")); }

/* Boots after a cut: repair (cut again sometimes) until a run finishes. */
static void repair_until_done(uint64_t seed_value, volatile uint32_t *cuts, bool cut_inside)
{
    volatile int boot = 0;
    cut_at = cut_inside ? flash_ops + 1 + rnd() % 400 : 0;
    for (;;) {
        if (setjmp(cut_point)) {
            (*cuts)++;
            drop_all_memory();
            cut_at = 0;
            continue;
        }
        CHECK(++boot < 12, "organic seed %" PRIu64 ": repair never finished", seed_value);
        srr_report_t report;
        srr_config_t config = repair_config();
        dump_before_repair();
        repair_boot(&report, &config);
        cut_at = 0;
        CHECK(report.result != SRR_RESULT_FAILED, "organic seed %" PRIu64 ": repair failed at %s %ld", seed_value,
              report.error_stage, (long)report.error_code);
        CHECK(report.check_deleted_live == 0, "organic seed %" PRIu64 ": the check deleted %u live files", seed_value,
              report.check_deleted_live);
        organic_last = report;
        organic_rounds += report.reclaim.rounds;
        organic_reclaimed += report.reclaim.blocks;
        if (!strcmp(report.error_stage, "gc_unsafe")) organic_gc_unsafe++;
        if (report.result != SRR_RESULT_STOPPED) return;
    }
}

/* Test only: the partition grows by `blocks` erased blocks, every block's
 * magic rewritten for the new length (SPIFFS_USE_MAGIC_LENGTH), as if the
 * store had room. */
static void grow_flash(uint32_t blocks)
{
    uint32_t old = flash_size;
    flash_size += blocks * BLOCK;
    flash = realloc(flash, flash_size);
    CHECK(flash, "grow flash");
    memset(flash + old, 0xFF, flash_size - old);
    free(mirror_lookup);
    free(mirror_headers);
    mirror_lookup = malloc(srm_lookup_bytes(flash_size, BLOCK, PAGE));
    mirror_headers = malloc(srm_header_bytes(flash_size, PAGE));
    uint32_t count = flash_size / BLOCK;
    for (uint32_t b = 0; b < count; ++b) wr16(b * BLOCK + PAGE - 4, (uint16_t)(0x20140529U ^ PAGE ^ (count - b)));
}

/* --prefill=<percent>: the workload runs without cuts until the store is
 * that full, then `ticks` more with cuts (an 8 MB store crammed in minutes
 * rather than an hour of repairs under the sanitizers). */
static uint32_t organic_prefill, organic_end;
static void run_organic(uint64_t seed_value, uint32_t kib, uint32_t ticks, uint32_t mean_gap)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    free(committed);
    free(row_queue);
    free(row_offset);
    free(row_generation);
    free(row_length);
    organic_reclaiming = seed_value & 1;
    committed = calloc(ORGANIC_MAX_SEQ, 1);
    row_queue = calloc(ORGANIC_MAX_SEQ, 1);
    row_offset = calloc(ORGANIC_MAX_SEQ, sizeof(uint32_t));
    row_generation = calloc(ORGANIC_MAX_SEQ, sizeof(uint32_t));
    row_length = calloc(ORGANIC_MAX_SEQ, sizeof(uint16_t));
    next_seq = organic_ticks = organic_reclaims = organic_segments = organic_gc_stops = organic_blocked = 0;
    organic_gc_unsafe = organic_rounds = organic_reclaimed = 0;
    segment_size = 0;
    volatile uint32_t cuts = 0, repairs = 0;
    uint32_t lost = 0, explained = 0, anded = 0;
    append_failed_this_boot = false;
    mount_plain();
    organic_end = ticks;
    if (organic_prefill) {
        u32_t total = 0, used = 0;
        do {
            organic_tick();
            SPIFFS_info(&fs, &total, &used);
        } while ((uint64_t)used * 100 < (uint64_t)total * organic_prefill && organic_ticks < ORGANIC_MAX_SEQ / 2);
        organic_end += organic_ticks;
    }
    cut_at = flash_ops + 1 + rnd() % (2 * mean_gap);
    while (organic_ticks < organic_end) {
        if (setjmp(cut_point)) {
            /* Power cut during the workload: boot, repair, carry on. */
            cuts++;
            organic_cut_count = cuts;
            append_failed_this_boot = false;
            drop_all_memory();
            if (verbose) organic_probe_before();
            if (verbose > 1) dump_pending_index("before repair");
            repair_until_done(seed_value, &cuts, rnd() % 3 == 0);
            if (verbose > 1) dump_pending_index("after repair");
            repairs++;
            mount_plain();
            organic_verify(&lost, &explained, &anded);
            cut_at = flash_ops + 1 + rnd() % (2 * mean_gap);
            continue;
        }
        organic_tick();
        if (verbose > 1) {
            /* Debug: every committed row still reads after every tick. */
            static uint8_t buffer[8 * 1024 * 1024];
            int64_t got = read_file("/pending.jsonl", buffer, sizeof(buffer));
            uint32_t missing = 0, first = 0;
            static uint8_t seen[ORGANIC_MAX_SEQ];
            memset(seen, 0, sizeof(seen));
            for (int64_t start = 0, i = 0; i < got; ++i) {
                if (buffer[i] != '\n') continue;
                const char *seq = (const char *)find_seq(buffer + start, (size_t)(i - start));
                if (seq) {
                    unsigned long value = strtoul(seq + 6, NULL, 10);
                    if (value < ORGANIC_MAX_SEQ) seen[value] = 1;
                }
                start = i + 1;
            }
            for (uint32_t k = 0; k < next_seq; ++k)
                if (committed[k] && !seen[k] && !missing++) first = k;
            if (got < 0 || missing) {
                printf("  organic tick %u (cut %u): pending reads %" PRId64 ", %u committed rows missing (first %u)\n",
                       organic_ticks, organic_cut_count, got, missing, first);
                verbose = 1;
            }
        }
    }
    cut_at = 0;
    SPIFFS_unmount(&fs);
    repair_until_done(seed_value, &cuts, false);
    mount_plain();
    organic_verify(&lost, &explained, &anded);
    /* The workload ended on a queue a store with no room left unreadable: its
     * rows are still unchecked. The device keeps working (SPIFFS's own GC in
     * its writes may make room), so the workload runs on without cuts, a
     * stretch at a time, each followed by a boot's repair. */
    uint32_t stretches = 0;
    while ((organic_blocked_now[0] || organic_blocked_now[1]) && stretches < 3) {
        ++stretches;
        for (uint32_t t = 0; t < 200; ++t) organic_tick();
        SPIFFS_unmount(&fs);
        repair_until_done(seed_value, &cuts, false);
        mount_plain();
        organic_verify(&lost, &explained, &anded);
    }
    bool wedged = organic_blocked_now[0] || organic_blocked_now[1];
    /* 2.7.6: with a redo area a wedge is a failure, not an allowance. */
    CHECK(!wedged || !redo_enabled, "organic seed %" PRIu64 ": WEDGED at the end (%u erased pages) with a redo area",
          seed_value, erased_pages());
    if (wedged) {
        /* Wedged: no erased page for the work and no block the repair may
         * compact (the damaged queue has pages in every one), and SPIFFS's GC
         * has nothing to move pages into, so no write gets room. The repair
         * defers every boot and reports the blocked spans; its last resort (a
         * reformat with every custody byte held) is not in 2.7.5. Its rows
         * must still be on flash: given room (test only: the partition grows
         * by 64 erased blocks), one repair makes the queue read, and every
         * row it held is there or under a gap record. */
        uint32_t erased = erased_pages();
        SPIFFS_unmount(&fs);
        grow_flash(64);
        repair_until_done(seed_value, &cuts, false);
        mount_plain();
        organic_verify(&lost, &explained, &anded);
        CHECK(!organic_blocked_now[0] && !organic_blocked_now[1],
              "organic seed %" PRIu64 ": a queue still unreadable with room", seed_value);
        printf("organic seed %" PRIu64 ": WEDGED at the end (%u erased pages; a queue unreadable for room through %u "
               "more stretches of 200 ticks); with room it reads whole\n", seed_value, erased, stretches);
    } else if (stretches) {
        printf("organic seed %" PRIu64 ": ended on a queue left unreadable for room; it read whole after %u more "
               "stretch(es) of 200 ticks\n", seed_value, stretches);
    }
    u32_t total = 0, used = 0;
    SPIFFS_info(&fs, &total, &used);
    SPIFFS_unmount(&fs);
    printf("organic seed %" PRIu64 ": %u cuts, %u repairs, %u committed rows lost, %u named by gap records, "
           "%u ANDed by SPIFFS after a failed append, %u files reclaimed, %u segments, %u stopped GCs, "
           "%u checks of a queue a store with no room left unreadable, %u stores handed over GC-unsafe, %u rounds "
           "reformatted %u blocks in place, used %u%%\n",
           seed_value, cuts, repairs, lost, explained, anded, organic_reclaims, organic_segments, organic_gc_stops,
           organic_blocked, organic_gc_unsafe, organic_rounds, organic_reclaimed,
           total ? (unsigned)(100ULL * used / total) : 0);
    CHECK(lost == 0, "organic seed %" PRIu64 ": %u committed rows lost without a gap record", seed_value, lost);
}

/* --replay=<prefix>: one repair on a dumped store, with its log, and where
 * each queue reads afterwards. */
static void replay(const char *prefix)
{
    char path[512];
    snprintf(path, sizeof(path), "%s.flash", prefix);
    FILE *f = fopen(path, "rb");
    CHECK(f, "%s", path);
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    format_flash((uint32_t)(size / 1024));
    CHECK(fread(flash, 1, flash_size, f) == flash_size, "read flash");
    fclose(f);
    snprintf(path, sizeof(path), "%s.nvs", prefix);
    f = fopen(path, "rb");
    CHECK(f && fread(&nvs, 1, sizeof(nvs), f) == sizeof(nvs), "read nvs");
    fclose(f);
    print_log = true;
    srr_report_t report;
    srr_config_t config = repair_config();
    repair_boot(&report, &config);
    printf("replay: %s at '%s' (%ld)\n", srr_result_name(report.result), report.error_stage, (long)report.error_code);
    mount_plain();
    static const char *const names[] = {"/pending.jsonl", "/blocked_identity.jsonl", "/qo0000002a.q"};
    static uint8_t buffer[8 * 1024 * 1024];
    for (size_t i = 0; i < 3; ++i) {
        if (!exists(names[i])) continue;
        spiffs_file fd = SPIFFS_open(&fs, names[i], SPIFFS_O_RDONLY, 0);
        spiffs_stat st;
        SPIFFS_fstat(&fs, fd, &st);
        uint32_t bad = 0;
        for (uint32_t off = 0; off < st.size; off += SPAN) {
            uint32_t want = st.size - off < SPAN ? st.size - off : SPAN;
            SPIFFS_lseek(&fs, fd, (s32_t)off, SPIFFS_SEEK_SET);
            if (SPIFFS_read(&fs, fd, buffer, (s32_t)want) != (s32_t)want) {
                if (bad++ < 4) printf("replay: %s span %u unreadable (%d)\n", names[i], off / SPAN, SPIFFS_errno(&fs));
                SPIFFS_clearerr(&fs);
            }
        }
        SPIFFS_close(&fs, fd);
        printf("replay: %s size %u, %u unreadable spans\n", names[i], st.size, bad);
    }
    /* The "wb" lane's next segment, as the workload opens it. */
    spiffs_file seg = SPIFFS_open(&fs, "/qo0000002a.q", SPIFFS_O_CREAT | SPIFFS_O_TRUNC | SPIFFS_O_RDWR, 0);
    printf("replay: segment \"wb\" %s (%d), %d free blocks\n", seg >= 0 ? "opens" : "refused", seg >= 0 ? 0 : SPIFFS_errno(&fs),
           (int)fs.free_blocks);
    if (seg >= 0) SPIFFS_close(&fs, seg);
    SPIFFS_clearerr(&fs);
    SPIFFS_unmount(&fs);
}

int main(int argc, char **argv)
{
    print_log = getenv("REPAIR_LOG") != NULL;
    const char *only = NULL;
    uint32_t seeds = 3, kib = 1024, organic_seeds = 4, organic_ticks_max = 1500, mean_gap = 300, first_seed = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "-v")) verbose = 1;
        else if (!strcmp(argv[i], "-vv")) verbose = 2;
        else if (!strncmp(argv[i], "--seeds=", 8)) seeds = (uint32_t)atoi(argv[i] + 8);
        else if (!strncmp(argv[i], "--kib=", 6)) kib = (uint32_t)atoi(argv[i] + 6);
        else if (!strncmp(argv[i], "--organic=", 10)) organic_seeds = (uint32_t)atoi(argv[i] + 10);
        else if (!strncmp(argv[i], "--ticks=", 8)) organic_ticks_max = (uint32_t)atoi(argv[i] + 8);
        else if (!strncmp(argv[i], "--gap=", 6)) mean_gap = (uint32_t)atoi(argv[i] + 6);
        else if (!strncmp(argv[i], "--first-seed=", 13)) first_seed = (uint32_t)atoi(argv[i] + 13);
        else if (!strncmp(argv[i], "--prefill=", 10)) organic_prefill = (uint32_t)atoi(argv[i] + 10);
        else if (!strncmp(argv[i], "--replay=", 9)) {
            replay(argv[i] + 9);
            return 0;
        } else if (!strncmp(argv[i], "--cut=", 6)) {
            /* One cut run: --cut=<scenario>:<seed>, as a failure names it. */
            const char *colon = strchr(argv[i], ':');
            CHECK(colon, "--cut=<scenario>:<seed>");
            for (size_t k = 0; k < sizeof(scenarios) / sizeof(scenarios[0]); ++k)
                if (!strncmp(argv[i] + 6, scenarios[k].name, (size_t)(colon - argv[i] - 6)) &&
                    strlen(scenarios[k].name) == (size_t)(colon - argv[i] - 6))
                    run_cut_inside_repair(&scenarios[k], strtoull(colon + 1, NULL, 10), kib);
            puts("spiffs repair host tests passed");
            return 0;
        }
        else only = argv[i];
    }
    /* The worst case the firmware's deadline is sized for: four check passes
     * (each reads every page header once per 512-page range) and ten surveys
     * (every block, then each file's index pages and text spans). */
    uint64_t pages = (uint64_t)kib * 1024U / PAGE;
    hal_cap = 4U * (pages * (pages / 512U + 1U) + 4U * pages) + 10U * (pages / 16U + 4U * pages);
    /* Linear in the store: a few scans of every damaged span's bytes and a
     * logarithmic lookup per span (3.7 M steps for the retired-file store's
     * 2,100 damaged spans at 8 MiB; the linear lookups took 3.9 billion). */
    cpu_cap = 512U * pages;
    size_t count = sizeof(scenarios) / sizeof(scenarios[0]);
    for (size_t i = 0; i < count; ++i) {
        if (only && strcmp(only, scenarios[i].name) && strcmp(only, "scenarios")) continue;
        for (uint32_t s = 0; s < seeds; ++s) run_scenario(&scenarios[i], 1000 + (first_seed + s) * 7919 + i, kib);
        printf("scenario %-24s %u seeds passed\n", scenarios[i].name, seeds);
    }
    if (!only || !strcmp(only, "mirror")) {
        for (size_t i = 0; i < count; ++i) run_mirror_equivalence(&scenarios[i], 77 + i, kib);
        printf("mirror equivalence passed for %zu scenarios\n", count);
    }
    if (!only || !strcmp(only, "budget")) {
        for (size_t i = 0; i < count; ++i) run_without_holds(&scenarios[i], 31 + i, kib);
        for (size_t i = 0; i < count; ++i) run_with_memory_limits(&scenarios[i], 63 + i, kib);
        run_policy(kib);
        printf("no holds, failing allocations and the boot policy passed\n");
    }
    if (!only || !strncmp(only, "review", 6)) run_review_custody(kib, only);
    if (!only || !strncmp(only, "reclaim", 7)) run_reclaim(kib, only);
    if (!only || !strcmp(only, "special")) {
        run_resume_cuts(kib);
        run_restores(kib);
        run_manifest_cases(kib);
        run_retired_stays_retired(kib);
        run_no_check_while_inconsistent(kib);
        run_guards(kib);
    }
    if (!only || !strcmp(only, "cuts")) {
        uint32_t runs = 0;
        for (size_t i = 0; i < count; ++i)
            for (uint32_t s = 0; s < seeds; ++s, ++runs)
                run_cut_inside_repair(&scenarios[i], 5000 + (first_seed + s) * 31 + i, kib);
        printf("cuts inside the repair: %u runs passed\n", runs);
    }
    if (!only || !strcmp(only, "organic")) {
        for (uint32_t s = 0; s < organic_seeds; ++s) run_organic(11 + first_seed + s, kib, organic_ticks_max, mean_gap);
    }
    printf("largest repair: %" PRIu64 " HAL operations (cap %" PRIu64 "), %" PRIu64 " CPU steps (cap %" PRIu64
           "), %.0f ms of CPU; %u blocks reformatted in place, each checked page by page\n",
           hal_max, hal_max_cap, cpu_max, cpu_cap, cpu_ms_max, round_checks_total);
    puts("spiffs repair host tests passed");
    model_reset();
    free(flash);
    free(committed);
    free(row_queue);
    free(row_offset);
    free(row_generation);
    free(row_length);
    free(mirror_lookup);
    free(mirror_headers);
    return 0;
}
