/* ZKT 2.7.5 boot storage repair against ESP-IDF 5.5.3's own SPIFFS.
 *
 * NOR flash in RAM (a program only clears bits, an erase sets 0xFF), power
 * cuts that tear a program or an erase and drop every byte of RAM state, an
 * NVS whose blobs survive them, the repair engine through the same ports as
 * the firmware, and the firmware's own legacy_queue.c, file_transaction.c and
 * reliability.c draining the repaired store through a stdio shim.
 *
 * Scenarios build a store, damage it the way the field reports and the
 * power-cut simulator showed (13FL's deleted queue head, P02's erased or
 * foreign page and leaked pages, duplicated index pages, a missing index
 * page, torn tails, dirty tails, stale cursors), repair it, and check:
 *   A1 custody: every byte the damage left readable is present at its offset
 *   A2 gaps: exactly the bytes nobody could read are reported, nothing else
 *   A3 drains: the real lq drain reads every queue to its end; ft recovers
 *   A4 health: appends are accepted, a probe file works, no leak remains
 *   A5 idempotence: a second check fixes nothing; a second repair is clean
 *   A6 removals: only probe files and the catalog are ever removed
 *   A7 rollback reader: every row a whole-file reader read before, it reads
 *   A8 cuts: a power cut anywhere inside the repair loses nothing a gap
 *      record does not name, and the next boots finish the repair
 * and that the metadata mirror leaves the same flash and report. */
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "spiffs.h"
#include "spiffs_nucleus.h"

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
        uint32_t n = rnd() % (length + 1);
        for (uint32_t i = 0; i < n; ++i) flash[address + i] &= src[i];
        if (n < length && (rnd() & 1)) flash[address + n] &= (uint8_t)(src[n] | (uint8_t)rnd());
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
static bool port_nvs_store(void *context, const char *ns, const char *key, const void *blob, size_t size)
{
    (void)context;
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
static void port_log(void *context, const char *level, const char *code, const char *message)
{
    (void)context;
    log_lines++;
    if (verbose) printf("  [%s] %s %s\n", level, code, message);
}
static void port_phase(void *context, srp_phase_t phase, uint32_t pass, bool writing)
{
    (void)context; (void)phase; (void)pass; (void)writing;
    phase_changes++;
}
static const srr_ports_t ports = {
    .alloc = port_alloc, .release = port_release, .now_ms = port_now, .log = port_log,
    .nvs_load = port_nvs_load, .nvs_store = port_nvs_store, .nvs_erase = port_nvs_erase, .phase = port_phase,
};

static srr_config_t repair_config(void)
{
    srr_config_t config = {.hold_budget = 64ULL * 1024 * 1024, .image_tag = 0x2750};
    return config;
}

/* One boot: mount privately, repair, unmount. HAL operations are capped:
 * the firmware's deadline must never be the thing that ends a repair. */
static uint64_t hal_cap, hal_max;
static void repair_boot(srr_report_t *report, const srr_config_t *config)
{
    uint64_t start_ops = flash_ops, start_reads = flash_reads;
    CHECK(mount_with(srr_check_callback) == SPIFFS_OK, "repair mount %d", SPIFFS_errno(&fs));
    srr_run(&fs, config, &ports, report);
    SPIFFS_unmount(&fs);
    CHECK(memory_in_use == 0, "repair leaked %" PRIu64 " bytes", memory_in_use);
    uint64_t used = (flash_ops - start_ops) + (flash_reads - start_reads);
    if (used > hal_max) hal_max = used;
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
            covered = gap->generation >= generation_from && !strcmp(gap->name, m->name) && i >= gap->offset &&
                i < gap->offset + gap->length;
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

static void damage_full_store(void)
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
    for (uint32_t pix = 0, pinned = blocks; pix < flash_size / PAGE; ++pix) {
        if (pix % 16 == 0 || pix / 16 == keep || rd16(lookup_address(pix)) != 0xFFFF) continue;
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
    check_idempotent();
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
    verify_scenario(s->name, &report, !strcmp(s->name, "torn_tails"));
    if (!strcmp(s->name, "stale_cursors")) CHECK(report.cursors_reset == 3, "cursors reset %u", report.cursors_reset);
    if (!strcmp(s->name, "torn_lookup")) CHECK(report.lookup_fixed >= 1, "torn lookup entry left");
    if (!strcmp(s->name, "torn_probe_size")) CHECK(report.expendables_removed >= 1, "torn probe not purged");
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
        int64_t got = read_file(m->name, buffer, sizeof(buffer));
        bool interrupted = false;
        for (uint32_t g = 0; g < gaps.count; ++g)
            if (!strcmp(gaps.gap[g].name, m->name) && gaps.gap[g].cause != SRP_GAP_SALVAGE) interrupted = true;
        if (interrupted) continue; /* the loss is named exactly */
        CHECK(got >= 0, "%s after cuts: %s unreadable", s->name, m->name);
        for (uint32_t k = 0; k < m->size && k < (uint32_t)got; ++k)
            CHECK(m->unreadable[k] || buffer[k] == m->data[k], "%s after cuts: %s byte %u changed", s->name,
                  m->name, k);
    }
    SPIFFS_unmount(&fs);
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
 * any repair runs. Such rows are counted apart from rows a repair lost. */
static uint8_t *committed;
static uint32_t next_seq, organic_ticks, organic_cut_count;
static bool append_failed_this_boot;

static bool organic_append(void)
{
    char row[600];
    row_seq = next_seq;
    size_t n = json_row(row, sizeof(row));
    uint32_t seq = next_seq++;
    if (seq >= ORGANIC_MAX_SEQ) return false;
    bool ok = append_bytes("/pending.jsonl", row, (uint32_t)n);
    if (ok) committed[seq] = append_failed_this_boot ? 2 : 1;
    else append_failed_this_boot = true;
    return ok;
}

static void organic_tick(void)
{
    organic_ticks++;
    (void)organic_append();
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

static void organic_verify(uint32_t *lost, uint32_t *explained, uint32_t *anded)
{
    static uint8_t buffer[8 * 1024 * 1024];
    static uint8_t present[ORGANIC_MAX_SEQ];
    memset(present, 0, sizeof(present));
    int64_t got = read_file("/pending.jsonl", buffer, sizeof(buffer));
    CHECK(got >= 0, "organic: pending unreadable after repair");
    for (int64_t start = 0, i = 0; i < got; ++i) {
        if (buffer[i] != '\n') continue;
        const char *seq = (const char *)find_seq(buffer + start, (size_t)(i - start));
        if (seq) {
            unsigned long value = strtoul(seq + 6, NULL, 10);
            if (value < ORGANIC_MAX_SEQ) present[value] = 1;
        }
        start = i + 1;
    }
    srp_gaps_t gaps = gaps_now();
    bool any_gap = false;
    for (uint32_t g = 0; g < gaps.count; ++g) any_gap |= !strcmp(gaps.gap[g].name, "/pending.jsonl");
    for (uint32_t s = 0; s < next_seq && s < ORGANIC_MAX_SEQ; ++s) {
        if (!committed[s] || present[s]) continue;
        if (committed[s] == 2) (*anded)++;
        else if (any_gap) (*explained)++;
        else (*lost)++;
        if (verbose) printf("  organic: committed row %u missing after cut %u (tick %u)%s; readable before repair: %d\n", s,
                            organic_cut_count, organic_ticks, any_gap ? " (gap recorded)" : "", organic_before[s]);
        committed[s] = 0;
    }
}

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
        repair_boot(&report, &config);
        cut_at = 0;
        CHECK(report.result != SRR_RESULT_FAILED, "organic seed %" PRIu64 ": repair failed at %s %ld", seed_value,
              report.error_stage, (long)report.error_code);
        CHECK(report.check_deleted_live == 0, "organic seed %" PRIu64 ": the check deleted %u live files", seed_value,
              report.check_deleted_live);
        if (report.result != SRR_RESULT_STOPPED) return;
    }
}

static void run_organic(uint64_t seed_value, uint32_t kib, uint32_t ticks, uint32_t mean_gap)
{
    seed(seed_value);
    nvs_clear();
    format_flash(kib);
    free(committed);
    committed = calloc(ORGANIC_MAX_SEQ, 1);
    next_seq = organic_ticks = 0;
    volatile uint32_t cuts = 0, repairs = 0;
    uint32_t lost = 0, explained = 0, anded = 0;
    append_failed_this_boot = false;
    mount_plain();
    cut_at = flash_ops + 1 + rnd() % (2 * mean_gap);
    while (organic_ticks < ticks) {
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
    u32_t total = 0, used = 0;
    SPIFFS_info(&fs, &total, &used);
    SPIFFS_unmount(&fs);
    printf("organic seed %" PRIu64 ": %u cuts, %u repairs, %u committed rows lost, %u named by gap records, "
           "%u ANDed by SPIFFS after a failed append, used %u%%\n",
           seed_value, cuts, repairs, lost, explained, anded, total ? (unsigned)(100ULL * used / total) : 0);
    CHECK(lost == 0, "organic seed %" PRIu64 ": %u committed rows lost without a gap record", seed_value, lost);
}

int main(int argc, char **argv)
{
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
        else only = argv[i];
    }
    /* The worst case the firmware's deadline is sized for: four check passes
     * (each reads every page header once per 512-page range) and ten surveys
     * (every block, then each file's index pages and text spans). */
    uint64_t pages = (uint64_t)kib * 1024U / PAGE;
    hal_cap = 4U * (pages * (pages / 512U + 1U) + 4U * pages) + 10U * (pages / 16U + 4U * pages);
    size_t count = sizeof(scenarios) / sizeof(scenarios[0]);
    for (size_t i = 0; i < count; ++i) {
        if (only && strcmp(only, scenarios[i].name) && strcmp(only, "scenarios")) continue;
        for (uint32_t s = 0; s < seeds; ++s) run_scenario(&scenarios[i], 1000 + s * 7919 + i, kib);
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
    printf("largest repair: %" PRIu64 " HAL operations (cap %" PRIu64 ")\n", hal_max, hal_cap);
    puts("spiffs repair host tests passed");
    model_reset();
    free(flash);
    free(committed);
    free(mirror_lookup);
    free(mirror_headers);
    return 0;
}
