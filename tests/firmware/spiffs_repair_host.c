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

static bool raw_write(void *context, uint32_t address, const void *buffer, uint32_t length)
{
    (void)context;
    if ((uint64_t)address + length > flash_size) return false;
    const uint8_t *src = buffer;
    flash_ops++;
    if (cut_at && flash_ops == cut_at) {
        if (rnd() & 1) {
            /* A clean prefix and one partial byte. */
            uint32_t n = rnd() % (length + 1);
            for (uint32_t i = 0; i < n; ++i) flash[address + i] &= src[i];
            if (n < length && (rnd() & 1)) flash[address + n] &= (uint8_t)(src[n] | (uint8_t)rnd());
        } else {
            /* NOR programs a page's cells in parallel: any byte may end
             * fully, partly or not programmed (a size field's high bytes
             * left 0xFF, a flags byte left 0xFD). */
            for (uint32_t i = 0; i < length; ++i) {
                uint32_t how = rnd() % 4;
                if (how == 1) flash[address + i] &= src[i];
                else if (how == 2) flash[address + i] &= (uint8_t)(src[i] | (uint8_t)rnd());
            }
        }
        longjmp(cut_point, 1);
    }
    for (uint32_t i = 0; i < length; ++i) flash[address + i] &= src[i];
    return true;
}

static bool raw_erase(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    if ((uint64_t)address + length > flash_size) return false;
    flash_ops++;
    if (cut_at && flash_ops == cut_at) {
        for (uint32_t i = 0; i < length; ++i)
            if (rnd() & 1) flash[address + i] = 0xFF;
        longjmp(cut_point, 1);
    }
    memset(flash + address, 0xFF, length);
    return true;
}

static srm_mirror_t mirror;
static bool use_mirror;
static uint8_t *mirror_lookup, *mirror_headers, mirror_scratch[BLOCK];

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
    if (use_mirror) {
        static const srm_flash_t raw = {NULL, raw_read, raw_write, raw_erase};
        CHECK(srm_init(&mirror, &raw, flash_size, BLOCK, PAGE, mirror_lookup, mirror_headers, mirror_scratch,
                       sizeof(mirror_scratch)), "mirror init");
    }
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
#define NVS_SLOTS 64
static struct {
    char ns[16], key[16];
    uint8_t blob[2048];
    size_t size;
    bool used;
} nvs[NVS_SLOTS];

static int nvs_find(const char *ns, const char *key)
{
    for (int i = 0; i < NVS_SLOTS; ++i)
        if (nvs[i].used && !strcmp(nvs[i].ns, ns) && !strcmp(nvs[i].key, key)) return i;
    return -1;
}
static int port_nvs_load(void *context, const char *ns, const char *key, void *blob, size_t size)
{
    (void)context;
    int i = nvs_find(ns, key);
    if (i < 0) return 0;
    if (nvs[i].size != size) return -1;
    memcpy(blob, nvs[i].blob, size);
    return 1;
}
static bool nvs_refuse_state; /* the repair journal cannot be saved */
static bool port_nvs_store(void *context, const char *ns, const char *key, const void *blob, size_t size)
{
    (void)context;
    if (nvs_refuse_state && !strcmp(ns, SRR_NVS_NAMESPACE) && !strcmp(key, "state")) return false;
    CHECK(size <= sizeof(nvs[0].blob), "blob %s/%s %zu bytes", ns, key, size);
    int i = nvs_find(ns, key);
    for (int k = 0; i < 0 && k < NVS_SLOTS; ++k)
        if (!nvs[k].used) i = k;
    CHECK(i >= 0, "nvs full");
    snprintf(nvs[i].ns, sizeof(nvs[i].ns), "%s", ns);
    snprintf(nvs[i].key, sizeof(nvs[i].key), "%s", key);
    memcpy(nvs[i].blob, blob, size);
    nvs[i].size = size;
    nvs[i].used = true;
    return true;
}
static bool port_nvs_erase(void *context, const char *ns, const char *key)
{
    (void)context;
    int i = nvs_find(ns, key);
    if (i >= 0) nvs[i].used = false;
    return true;
}
static void nvs_clear(void) { memset(nvs, 0, sizeof(nvs)); }

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

static void *port_alloc(void *context, size_t bytes)
{
    (void)context;
    if (memory_limit && memory_in_use + bytes > memory_limit) return NULL;
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
static void port_log(void *context, const char *level, const char *code, const char *message)
{
    (void)context;
    log_lines++;
    if (verbose || print_log) printf("  [%s] %s %s\n", level, code, message);
}
static uint32_t writing_phases; /* phases that light the repair LED */
/* Stands in for something that changes the store as a phase starts. */
static void (*phase_hook)(srp_phase_t phase, uint32_t pass);
static void port_phase(void *context, srp_phase_t phase, uint32_t pass, bool writing)
{
    (void)context;
    phase_changes++;
    if (writing) writing_phases++;
    if (phase_hook) phase_hook(phase, pass);
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
    return check_hook && check_hook(pass);
}
static const srr_ports_t ports = {
    .alloc = port_alloc, .release = port_release, .keep_going = port_keep_going, .remaining_ms = port_remaining,
    .now_ms = port_now, .log = port_log, .nvs_load = port_nvs_load, .nvs_store = port_nvs_store,
    .nvs_erase = port_nvs_erase, .phase = port_phase, .after_check = port_after_check,
};

static srr_config_t repair_config(void)
{
    srr_config_t config = {.hold_budget = 64ULL * 1024 * 1024, .image_tag = 0x2750};
    return config;
}

/* One boot: mount privately, repair, unmount. HAL operations are capped:
 * the firmware's deadline must never be the thing that ends a repair. */
static uint64_t hal_cap, hal_max;
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
static void repair_boot(srr_report_t *report, const srr_config_t *config)
{
    uint64_t start_ops = flash_ops, start_reads = flash_reads;
    CHECK(mount_with(srr_check_callback) == SPIFFS_OK, "repair mount %d", SPIFFS_errno(&fs));
    double started = cpu_ms();
    srr_run(&fs, config, &ports, report);
    double spent = cpu_ms() - started;
    if (spent > cpu_ms_max) cpu_ms_max = spent;
    if (report->cpu_steps > cpu_max) cpu_max = report->cpu_steps;
    if (cpu_cap) CHECK(report->cpu_steps <= cpu_cap, "repair took %u CPU steps (cap %" PRIu64 ")", report->cpu_steps,
                       cpu_cap);
    SPIFFS_unmount(&fs);
    CHECK(memory_in_use == 0, "repair leaked %" PRIu64 " bytes", memory_in_use);
    uint64_t used = (flash_ops - start_ops) + (flash_reads - start_reads);
    if (used > hal_max) hal_max = used;
    if (hal_cap && used > hal_cap) dump_write();
    if (hal_cap) CHECK(used <= hal_cap, "repair used %" PRIu64 " HAL operations (cap %" PRIu64 ")", used, hal_cap);
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
    memcpy(nvs_before, nvs, sizeof(nvs));
    repair_boot(&plain, &config);
    after_plain = malloc(flash_size);
    memcpy(after_plain, flash, flash_size);
    memcpy(flash, before, flash_size);
    memcpy(nvs, nvs_before, sizeof(nvs));
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
    memcpy(nvs_before, nvs, sizeof(nvs));
    srr_report_t report;
    srr_config_t config = repair_config();
    uint64_t ops_before = flash_ops;
    repair_boot(&report, &config);
    uint64_t writes = flash_ops - ops_before;
    memcpy(flash, store, flash_size);
    memcpy(nvs, nvs_before, sizeof(nvs));
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
    memcpy(saved, nvs, sizeof(nvs));
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
            memcpy(nvs, saved, sizeof(nvs));
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
     * for the next boot, which settles it without reporting it twice. */
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
        memcpy(saved, nvs, sizeof(nvs));
        srr_report_t report;
        srr_config_t config = repair_config();
        hook_mode = 3;
        uint64_t before = flash_ops;
        repair_boot(&report, &config);
        uint64_t writes = flash_ops - before;
        memcpy(flash, image, flash_size);
        memcpy(nvs, saved, sizeof(nvs));
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
        if (which <= 1 || which == 4 || which == 6) {
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
        if (which != 3 && which != 5) {
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
    memcpy(saved, nvs, sizeof(nvs));
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
            memcpy(nvs, saved, sizeof(nvs));
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
    memcpy(saved, nvs, sizeof(nvs));
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
            memcpy(nvs, saved, sizeof(nvs));
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
    memcpy(saved, nvs, sizeof(nvs));
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
            memcpy(nvs, saved, sizeof(nvs));
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
    memcpy(saved, nvs, sizeof(nvs));
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
            memcpy(nvs, saved, sizeof(nvs));
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
    if (REVIEW_PART("merged")) run_merged_from_hold(kib);
    if (REVIEW_PART("inbox") || REVIEW_PART("boundary")) run_inbox_boundary(kib);
    if (REVIEW_PART("inbox") || REVIEW_PART("window")) run_inbox_window(kib);
#undef REVIEW_PART
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
    memcpy(&dump_nvs, nvs, sizeof(nvs));
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
           "%u checks of a queue a store with no room left unreadable, used %u%%\n",
           seed_value, cuts, repairs, lost, explained, anded, organic_reclaims, organic_segments, organic_gc_stops,
           organic_blocked, total ? (unsigned)(100ULL * used / total) : 0);
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
    CHECK(f && fread(nvs, 1, sizeof(nvs), f) == sizeof(nvs), "read nvs");
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
           "), %.0f ms of CPU\n",
           hal_max, hal_cap, cpu_max, cpu_cap, cpu_ms_max);
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
