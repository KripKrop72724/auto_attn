#include "storage_repair.h"

#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "add_connector.h"
#include "boot_report.h"
#include "esp_app_desc.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "led_status.h"
#include "nvs.h"
#include "queue_store.h"
#include "spiffs_api.h" /* esp_spiffs_t: SPIFFS_LOCK takes its mutex */
#include "spiffs_nucleus.h"
#include "storage_exception_srr.h"
#include "storage_inventory.h"
#include "storage_repair_core.h"
#include "storage_repair_mirror.h"
#include "storage_repair_redo.h"

static const char *TAG = "storage_repair";

/* The hard deadline restarts the ESP; the soft one stops the engine at its
 * next safe point first, so a long run ends in STOPPED rather than a reset.
 * An unconfirmed image gets less: a reset there rolls back to the old one. */
#define SR_DEADLINE_PENDING_US (5LL * 60LL * 1000000LL)
#define SR_DEADLINE_CONFIRMED_US (15LL * 60LL * 1000000LL)
#define SR_SOFT_MARGIN_US (45LL * 1000000LL)
#define SR_YIELD_US 20000LL
#define SR_BOUNCE_BYTES 4096U
#define SR_FD_COUNT 4U
/* PSRAM kept free for the boot after the repair (the seen-UID cache alone
 * takes 2 MiB), and what the engine needs besides the holds. */
#define SR_PSRAM_RESERVE (2560U * 1024U)
#define SR_ENGINE_BYTES (640U * 1024U)
#define SR_HOLD_CAP (7U * 1024U * 1024U)
#define SR_LOG_LINES 24U
/* SPIFFS_check's expected time per MiB of store before a pass is measured
 * (the engine starts a pass only when this and the restore fit before the
 * soft deadline): with the metadata mirror, and reading flash. */
#define SR_CHECK_MS_PER_MIB_MIRROR 4000U
#define SR_CHECK_MS_PER_MIB_FLASH 8000U
/* 2.7.6: the redo area, the 384 KiB past the last partition of the 16 MiB
 * chip (storage ends at 0xFA0000); reached only through a partition handle
 * registered for exactly that range. */
#define SR_REDO_OFFSET 0xFA0000U
#define SR_REDO_BYTES 0x60000U
#define SR_CHIP_BYTES 0x1000000U
#define SR_REDO_SUBTYPE 0x99
/* While a block's window is open (its magic cleared, not yet programmed
 * back) the hard deadline waits once, this long, before it restarts. */
#define SR_WINDOW_GRACE_US 2000000LL
/* The redo records and the round hold Zone Lite's SPIFFS geometry: 256-byte
 * pages in 4 KiB blocks, with SPIFFS's length magic (round 1 review, low 5;
 * the engine checks the mounted geometry again before any round). */
_Static_assert(CONFIG_SPIFFS_PAGE_SIZE == SRX_PAGE, "the redo records hold 256-byte SPIFFS pages");
_Static_assert(SRX_BLOCK == 4096U && SRX_ENTRIES + 1U == SRX_BLOCK / SRX_PAGE, "the redo records hold 4 KiB blocks");
#if !defined(CONFIG_SPIFFS_USE_MAGIC) || !defined(CONFIG_SPIFFS_USE_MAGIC_LENGTH)
#error "the redo records assume SPIFFS's length magic"
#endif

typedef struct {
    char level[6], code[40], message[200];
} sr_line_t;

static srr_report_t s_report;
static bool s_ran, s_mount_failed, s_mirror, s_previous_deadline, s_previous_known, s_previous_returned;
static unsigned s_previous_phase, s_previous_pass;
static int32_t s_mount_error;
static uint64_t s_budget;
static uint32_t s_nvs_free_before, s_nvs_free_after, s_nvs_available_before, s_nvs_available_after;
static uint32_t s_internal_before, s_internal_after, s_stack_free;
static sr_line_t *s_lines;
static atomic_uint s_lines_count, s_lines_sent, s_lines_dropped;
static atomic_uint s_records_sent;
/* Gap and cursor-reset records awaiting, and holding, ADD's acknowledgement:
 * counted from NVS once after the run and kept here, so the outbox task and
 * the heartbeat never read NVS when nothing is pending. */
static atomic_uint s_records_pending, s_records_acked;
static atomic_bool s_damage_noted;

static const esp_partition_t *s_partition;
static const esp_partition_t *s_redo;
static srx_area_t s_area;
static bool s_mount_blocked;
static atomic_bool s_window_open, s_deadline_deferred;
static uint8_t *s_bounce;
static srm_mirror_t s_mirror_state;
static int64_t s_last_yield_us, s_soft_deadline_us;
static esp_timer_handle_t s_deadline_timer;
static volatile unsigned s_phase, s_pass;

static void count_records(void);

/* ---- 2.7.7: the confirm-first storage exception ------------------------- */
static atomic_uint s_store_writes;
static sx_record_t s_exception;      /* the record as this boot left it */
static bool s_exception_present, s_exception_active, s_running_pending, s_previous_failed;
static uint32_t s_image_tag;
static srx_test_t s_area_test;
static bool s_probe_hold, s_floor_seen, s_ota_floor;

/* Every program and erase of the VFS's SPIFFS (esp_spiffs.c takes these
 * through its HAL pointers; the link wraps them, CMakeLists.txt). The private
 * mount of the repair has its own HAL and is not counted. */
s32_t __real_spiffs_api_write(spiffs *fs, uint32_t addr, uint32_t size, uint8_t *src);
s32_t __real_spiffs_api_erase(spiffs *fs, uint32_t addr, uint32_t size);
s32_t __wrap_spiffs_api_write(spiffs *fs, uint32_t addr, uint32_t size, uint8_t *src);
s32_t __wrap_spiffs_api_erase(spiffs *fs, uint32_t addr, uint32_t size);
s32_t __wrap_spiffs_api_write(spiffs *fs, uint32_t addr, uint32_t size, uint8_t *src)
{
    atomic_fetch_add(&s_store_writes, 1);
    return __real_spiffs_api_write(fs, addr, size, src);
}
s32_t __wrap_spiffs_api_erase(spiffs *fs, uint32_t addr, uint32_t size)
{
    atomic_fetch_add(&s_store_writes, 1);
    return __real_spiffs_api_erase(fs, addr, size);
}

/* ---- flash, through one internal bounce buffer ------------------------ */
/* esp_flash allocates a temporary internal buffer of up to 16 KiB for every
 * read into PSRAM, and programs from PSRAM 32 bytes at a time; repair buffers
 * live in PSRAM, so every transfer goes through one internal 4 KiB buffer. */
static void maybe_yield(void)
{
    int64_t now = esp_timer_get_time();
    if (now - s_last_yield_us >= SR_YIELD_US) {
        vTaskDelay(1);
        s_last_yield_us = esp_timer_get_time();
    }
}

static bool raw_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    (void)context;
    maybe_yield();
    if (esp_ptr_internal(buffer)) return esp_partition_read(s_partition, address, buffer, length) == ESP_OK;
    uint8_t *out = buffer;
    while (length) {
        uint32_t n = length < SR_BOUNCE_BYTES ? length : SR_BOUNCE_BYTES;
        if (esp_partition_read(s_partition, address, s_bounce, n) != ESP_OK) return false;
        memcpy(out, s_bounce, n);
        out += n;
        address += n;
        length -= n;
    }
    return true;
}

static bool raw_write(void *context, uint32_t address, const void *buffer, uint32_t length)
{
    (void)context;
    maybe_yield();
    if (esp_ptr_internal(buffer)) return esp_partition_write(s_partition, address, buffer, length) == ESP_OK;
    const uint8_t *in = buffer;
    while (length) {
        uint32_t n = length < SR_BOUNCE_BYTES ? length : SR_BOUNCE_BYTES;
        memcpy(s_bounce, in, n);
        if (esp_partition_write(s_partition, address, s_bounce, n) != ESP_OK) return false;
        in += n;
        address += n;
        length -= n;
    }
    return true;
}

static bool raw_erase(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    maybe_yield();
    return esp_partition_erase_range(s_partition, address, length) == ESP_OK;
}

/* The redo area through the same bounce buffer; nothing when it is not
 * registered. */
static bool area_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    (void)context;
    if (!s_redo || !s_bounce) return false;
    maybe_yield();
    uint8_t *out = buffer;
    while (length) {
        uint32_t n = length < SR_BOUNCE_BYTES ? length : SR_BOUNCE_BYTES;
        if (esp_partition_read(s_redo, address, s_bounce, n) != ESP_OK) return false;
        memcpy(out, s_bounce, n);
        out += n;
        address += n;
        length -= n;
    }
    return true;
}

static bool area_write(void *context, uint32_t address, const void *buffer, uint32_t length)
{
    (void)context;
    if (!s_redo || !s_bounce) return false;
    maybe_yield();
    const uint8_t *in = buffer;
    while (length) {
        uint32_t n = length < SR_BOUNCE_BYTES ? length : SR_BOUNCE_BYTES;
        memcpy(s_bounce, in, n);
        if (esp_partition_write(s_redo, address, s_bounce, n) != ESP_OK) return false;
        in += n;
        address += n;
        length -= n;
    }
    return true;
}

static bool area_erase(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    if (!s_redo) return false;
    maybe_yield();
    return esp_partition_erase_range(s_redo, address, length) == ESP_OK;
}

static void port_window(void *context, bool open);
static void port_log(void *context, const char *level, const char *code, const char *message);

/* The redo area for the engine and the boot recovery; the store raw. */
static const srx_io_t REDO_IO = {
    .area_size = SR_REDO_BYTES, .area_read = area_read, .area_write = area_write, .area_erase = area_erase,
    .store_read = raw_read, .store_write = raw_write, .store_erase = raw_erase, .window = port_window,
    .log = port_log,
};

static s32_t hal_read(spiffs *fs, u32_t addr, u32_t size, u8_t *dst)
{
    (void)fs;
    return (s_mirror ? srm_read(&s_mirror_state, addr, dst, size) : raw_read(NULL, addr, dst, size)) ? SPIFFS_OK : -1;
}
static s32_t hal_write(spiffs *fs, u32_t addr, u32_t size, u8_t *src)
{
    (void)fs;
    return (s_mirror ? srm_write(&s_mirror_state, addr, src, size) : raw_write(NULL, addr, src, size)) ? SPIFFS_OK : -1;
}
static s32_t hal_erase(spiffs *fs, u32_t addr, u32_t size)
{
    (void)fs;
    return (s_mirror ? srm_erase(&s_mirror_state, addr, size) : raw_erase(NULL, addr, size)) ? SPIFFS_OK : -1;
}

/* ---- ports ---------------------------------------------------------- */
static void *port_alloc(void *context, size_t bytes)
{
    (void)context;
    return heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
}
static void port_release(void *context, void *pointer)
{
    (void)context;
    heap_caps_free(pointer);
}
static bool port_keep_going(void *context)
{
    (void)context;
    return esp_timer_get_time() < s_soft_deadline_us;
}
static uint64_t port_remaining(void *context)
{
    (void)context;
    int64_t left = s_soft_deadline_us - esp_timer_get_time();
    return left > 0 ? (uint64_t)(left / 1000) : 0;
}
static void port_yield(void *context)
{
    (void)context;
    maybe_yield();
}
static uint64_t port_now(void *context)
{
    (void)context;
    return (uint64_t)(esp_timer_get_time() / 1000);
}

static void queue_line(const char *level, const char *code, const char *message)
{
    unsigned index = atomic_load(&s_lines_count);
    if (!s_lines || index >= SR_LOG_LINES) {
        atomic_fetch_add(&s_lines_dropped, 1);
        return;
    }
    sr_line_t *line = &s_lines[index];
    snprintf(line->level, sizeof(line->level), "%s", level);
    snprintf(line->code, sizeof(line->code), "%s", code);
    snprintf(line->message, sizeof(line->message), "%s", message);
    atomic_store(&s_lines_count, index + 1);
}

static void port_log(void *context, const char *level, const char *code, const char *message)
{
    (void)context;
    if (!strcmp(level, "ERROR")) ESP_LOGE(TAG, "%s %s", code, message);
    else if (!strcmp(level, "WARN")) ESP_LOGW(TAG, "%s %s", code, message);
    else ESP_LOGI(TAG, "%s %s", code, message);
    if (strcmp(level, "DEBUG")) queue_line(level, code, message);
}

static int port_nvs_load(void *context, const char *ns, const char *key, void *blob, size_t size)
{
    (void)context;
    nvs_handle_t handle;
    esp_err_t result = nvs_open(ns, NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    size_t length = size;
    result = nvs_get_blob(handle, key, blob, &length);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    return result == ESP_OK && length == size ? 1 : -1;
}

static bool port_nvs_store(void *context, const char *ns, const char *key, const void *blob, size_t size)
{
    (void)context;
    nvs_handle_t handle;
    if (nvs_open(ns, NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t result = nvs_set_blob(handle, key, blob, size);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}

static bool port_nvs_erase(void *context, const char *ns, const char *key)
{
    (void)context;
    nvs_handle_t handle;
    if (nvs_open(ns, NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t result = nvs_erase_key(handle, key);
    if (result == ESP_OK || result == ESP_ERR_NVS_NOT_FOUND) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK;
}

static void port_phase(void *context, srp_phase_t phase, uint32_t pass, bool writing)
{
    (void)context;
    s_phase = phase;
    s_pass = pass;
    boot_report_repair(phase, pass, false);
    /* A distinct light while the repair writes: a power cut now costs a
     * resume, and staff should not mistake it for a hang. */
    led_status_set(writing ? LED_STATUS_STORAGE_REPAIR : LED_STATUS_BOOTING);
}

/* ---- 2.7.6: the last resort's ports -------------------------------------- */
/* The private mount, at file scope: the engine remounts it after a round. */
static esp_spiffs_t s_efs;
static spiffs s_fs;
static uint8_t *s_work, *s_fds, *s_cache;
static size_t s_fds_bytes, s_cache_bytes;

static bool port_raw_read(void *context, uint32_t address, void *buffer, uint32_t length)
{
    return raw_read(context, address, buffer, length);
}

static void port_refresh(void *context, uint32_t address, uint32_t length)
{
    (void)context;
    if (s_mirror) (void)srm_refresh(&s_mirror_state, address, length);
}

static bool port_remount(void *context)
{
    (void)context;
    SPIFFS_unmount(&s_fs);
    return SPIFFS_mount(&s_fs, &s_efs.cfg, s_work, s_fds, s_fds_bytes, s_cache, s_cache_bytes, srr_check_callback) ==
        SPIFFS_OK;
}

static void port_window(void *context, bool open)
{
    (void)context;
    atomic_store(&s_window_open, open);
}

static int32_t port_nvs_available(void *context)
{
    (void)context;
    nvs_stats_t stats;
    return nvs_get_stats(NULL, &stats) == ESP_OK ? (int32_t)stats.available_entries : -1;
}

static const srr_ports_t PORTS = {
    .alloc = port_alloc,
    .release = port_release,
    .keep_going = port_keep_going,
    .remaining_ms = port_remaining,
    .yield = port_yield,
    .now_ms = port_now,
    .log = port_log,
    .nvs_load = port_nvs_load,
    .nvs_store = port_nvs_store,
    .nvs_erase = port_nvs_erase,
    .phase = port_phase,
    .redo = &REDO_IO,
    .raw_read = port_raw_read,
    .refresh = port_refresh,
    .remount = port_remount,
    .window = port_window,
    .nvs_available = port_nvs_available,
};

/* ---- the deadline ---------------------------------------------------- */
/* Never depends on the repair code: if a run outlives it, whatever it is
 * doing, the breadcrumb is kept and the ESP restarts. On an unconfirmed image
 * the bootloader then rolls back; on a confirmed one the attempts guard
 * counts it, and after three such runs repairs stop for this image. */
static void deadline_expired(void *argument)
{
    (void)argument;
    /* 2.7.6: inside a block's window (its magic cleared, not yet programmed
     * back) the restart waits once, at most 2 s: on the same image the next
     * boot would redo the block, but a window is never cut on purpose. */
    if (atomic_load(&s_window_open) && !atomic_exchange(&s_deadline_deferred, true) &&
        esp_timer_start_once(s_deadline_timer, SR_WINDOW_GRACE_US) == ESP_OK)
        return;
    boot_report_repair(s_phase, s_pass, true);
    esp_restart();
}

/* free_entries counts the page NVS keeps for its own GC; available_entries
 * leaves it out (what a write can really use). */
static void nvs_entries(uint32_t *free_entries, uint32_t *available)
{
    nvs_stats_t stats;
    bool known = nvs_get_stats(NULL, &stats) == ESP_OK;
    *free_entries = known ? (uint32_t)stats.free_entries : 0;
    *available = known ? (uint32_t)stats.available_entries : 0;
}

static bool damage_flag_take(void)
{
    uint8_t value = 0;
    nvs_handle_t handle;
    if (nvs_open(SRR_NVS_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return false;
    bool seen = nvs_get_u8(handle, "damage", &value) == ESP_OK && value;
    if (seen && nvs_erase_key(handle, "damage") == ESP_OK) (void)nvs_commit(handle);
    nvs_close(handle);
    return seen;
}

void storage_repair_note_damage(int error)
{
    if (!error || atomic_exchange(&s_damage_noted, true)) return;
    nvs_handle_t handle;
    if (nvs_open(SRR_NVS_NAMESPACE, NVS_READWRITE, &handle) != ESP_OK) return;
    if (nvs_set_u8(handle, "damage", 1) == ESP_OK) (void)nvs_commit(handle);
    nvs_close(handle);
}

/* The app a reset of the unconfirmed running image returns to. The
 * bootloader marks the running entry ABORTED and boots the other otadata
 * entry unless it is missing, INVALID or ABORTED
 * (bootloader_common_ota_select_valid); then the factory app. */
static const esp_partition_t *rollback_partition(const esp_partition_t *running)
{
    const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t state;
    if (other && running && other->address != running->address &&
        esp_ota_get_state_partition(other, &state) == ESP_OK && state != ESP_OTA_IMG_INVALID &&
        state != ESP_OTA_IMG_ABORTED && state != ESP_OTA_IMG_PENDING_VERIFY)
        return other;
    return esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_FACTORY, NULL);
}

static srr_config_t boot_config(void)
{
    srr_config_t config = {0};
    esp_ota_img_states_t state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    config.pending_verify = running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY;
    esp_reset_reason_t reason = boot_report_reset_reason();
    s_previous_known = boot_report_previous_repair(&s_previous_phase, &s_previous_pass, &s_previous_deadline,
                                                   &s_previous_returned);
    /* A failure is the repair's only when the previous boot's breadcrumb
     * shows it had not returned when the crash or watchdog came, or that its
     * deadline fired: an unrelated crash after a stopped run is not. */
    /* 2.7.6 (review D5): a brownout that ends a repair counts too: an erase
     * that pulls the supply down every boot must not loop forever. */
    bool crash = reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT || reason == ESP_RST_TASK_WDT ||
        reason == ESP_RST_WDT || reason == ESP_RST_CPU_LOCKUP || reason == ESP_RST_BROWNOUT;
    config.previous_failed = s_previous_known && (s_previous_deadline || (crash && !s_previous_returned));
    const esp_app_desc_t *app = esp_app_get_description();
    if (app) memcpy(&config.image_tag, app->app_elf_sha256, sizeof(config.image_tag));
    /* An unconfirmed image's reset returns to the image the bootloader picks
     * next: the other OTA slot when an otadata entry still selects it,
     * otherwise the factory app (ESP-IDF 5.5.3 bootloader_utility.c; the
     * first OTA after a factory flash has no other entry). A manifest records
     * it, so that image (when it runs this repair too) resumes the manifest
     * as its own rather than as a stale one. */
    const esp_partition_t *previous = config.pending_verify ? rollback_partition(running) : NULL;
    esp_app_desc_t description;
    if (previous && running && previous->address != running->address &&
        esp_ota_get_partition_description(previous, &description) == ESP_OK)
        memcpy(&config.rollback_tag, description.app_elf_sha256, sizeof(config.rollback_tag));
    config.damage_seen = damage_flag_take();
    return config;
}

/* ---- 2.7.6: the redo area ------------------------------------------------ */
/* Proven unused (the chip is 16 MiB, no partition of the table reaches past
 * 0xFA0000), then registered for exactly that range, which also proves no
 * overlap. Deregistered before storage_repair_boot returns. */
static srx_area_state_t redo_register(void)
{
    uint32_t physical = 0;
    if (!esp_flash_default_chip || esp_flash_get_physical_size(esp_flash_default_chip, &physical) != ESP_OK ||
        physical < SR_CHIP_BYTES || esp_flash_default_chip->size < SR_CHIP_BYTES)
        return SRX_AREA_ABSENT;
    bool clear = true;
    esp_partition_iterator_t it = esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL);
    for (; it; it = esp_partition_next(it)) {
        const esp_partition_t *partition = esp_partition_get(it);
        if (partition->address + partition->size > SR_REDO_OFFSET) clear = false;
    }
    esp_partition_iterator_release(it);
    if (!clear) return SRX_AREA_ABSENT;
    /* The store's blocks are the records' (low 5). */
    if (!s_partition || s_partition->erase_size != SRX_BLOCK || s_partition->size % SRX_BLOCK) return SRX_AREA_ABSENT;
    if (esp_partition_register_external(NULL, SR_REDO_OFFSET, SR_REDO_BYTES, "srp_redo", ESP_PARTITION_TYPE_DATA,
                                        (esp_partition_subtype_t)SR_REDO_SUBTYPE, &s_redo) != ESP_OK) {
        s_redo = NULL;
        return SRX_AREA_OVERLAP;
    }
    return SRX_AREA_READY;
}

/* Every boot, in every mode, before anything mounts the store (reviews D1,
 * C2, M1): the area read, every record a reset left open completed on raw
 * flash (or made a byte source), the reclaim journal updated. A round that
 * may have open records nobody could inspect makes them sources for later
 * boots; when even that cannot be journaled the store stays unmounted.
 * Without the deadline (`timed` false) nothing is written to the store or
 * the area: open records go to the journal as sources, as for an area that
 * cannot be read (round 1 review, low 2). The breadcrumb names the reformat
 * only while recovery acts on an open record. */
static void redo_recover(srr_config_t *config, bool timed)
{
    memset(&s_area, 0, sizeof(s_area));
    srp_reclaim_t journal;
    bool have = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) == 1 &&
        srp_reclaim_valid(&journal);
    if (!have) memset(&journal, 0, sizeof(journal));
    srx_area_state_t state = redo_register();
    uint8_t *work = state == SRX_AREA_READY ? heap_caps_malloc(3U * SRX_BLOCK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
                                            : NULL;
    bool inspected = false;
    uint32_t open = 0;
    if (state != SRX_AREA_READY) {
        s_area.state = state;
    } else if (!work || !s_bounce) {
        s_area.state = SRX_AREA_NO_MEMORY;
    } else {
        srx_discover(&REDO_IO, have && (journal.flags & SRP_RECLAIM_CLAIMED), work, &s_area);
        inspected = timed && (s_area.state == SRX_AREA_READY || s_area.state == SRX_AREA_FOREIGN);
        uint32_t records_open = 0;
        for (uint32_t s = 0; s < s_area.slots; ++s) records_open += s_area.slot[s] == SRX_SLOT_OPEN;
        if (inspected && records_open) {
            boot_report_repair(SRP_PHASE_REFORMAT, 0, false);
            open = srx_recover(&REDO_IO, s_partition->size / SRX_BLOCK, config->image_tag,
                               have && (journal.flags & SRP_RECLAIM_SOURCES), work, &s_area);
        }
    }
    heap_caps_free(work);
    /* 2.7.7, the OTA floor (critique 1): anything this device ever wrote to
     * the area or journaled about it. A foreign area counts too. */
    s_floor_seen = have && (journal.flags || journal.rounds || journal.blocks);
    for (uint32_t slot = 0; slot < s_area.slots && !s_floor_seen; ++slot) s_floor_seen = s_area.slot[slot] != SRX_SLOT_ERASED;
    /* Also without a journal (NVS lost it): records left open, or sources,
     * get one, so no later boot redoes an open record after this boot's
     * mount (round 2 review, R2-2). */
    if (srx_journal_after_recovery(&journal, inspected, open, srx_sources_left(&s_area)) &&
        !port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) &&
        (journal.flags & SRP_RECLAIM_SOURCES)) {
        s_mount_blocked = true;
        queue_line("ERROR", "STORAGE_REPAIR_RECLAIM_SOURCES",
                   "an interrupted reformat could not be journaled; the store stays unmounted this boot");
    }
    /* Byte sources this boot cannot read: the queues' unreadable bytes wait
     * for a boot that can (round 1 review, low). */
    config->sources_unread = have && srx_sources_unread(&journal, inspected);
    config->area = &s_area;
}

bool storage_repair_storage_allowed(void) { return !s_mount_blocked; }

/* The area as discovery and recovery left it, for a boot no run reported. */
static void note_area(void)
{
    if (s_report.runs) return;
    s_report.reclaim.area = (uint8_t)s_area.state;
    s_report.reclaim.area_foreign = s_area.foreign;
    s_report.reclaim.area_first_foreign = s_area.first_foreign;
    s_report.reclaim.area_foreign_crc = s_area.foreign_crc;
    s_report.reclaim.redone = s_area.redone + s_area.reasserted;
    s_report.reclaim.sources = s_area.to_sources;
    s_report.reclaim.failed = s_area.failed;
}

/* ---- 2.7.7: the exception's record and inputs ---------------------------- */
static bool exception_load(sx_record_t *out)
{
    memset(out, 0, sizeof(*out));
    return port_nvs_load(NULL, SRR_NVS_NAMESPACE, SX_RECORD_KEY, out, sizeof(*out)) == 1 && sx_record_valid(out);
}

bool storage_repair_exception_store(sx_record_t *record)
{
    if (!record) return false;
    sx_record_seal(record);
    sx_record_t back;
    bool ok = port_nvs_store(NULL, SRR_NVS_NAMESPACE, SX_RECORD_KEY, record, sizeof(*record)) && exception_load(&back) &&
        !memcmp(&back, record, sizeof(back));
    if (ok) {
        s_exception = *record;
        s_exception_present = true;
    }
    return ok;
}

bool storage_repair_exception_record(sx_record_t *out)
{
    if (!out || !s_exception_present) return false;
    *out = s_exception;
    return true;
}

bool storage_repair_exception_active(void) { return s_exception_active; }
void storage_repair_exception_mark_confirmed(void)
{
    s_exception_active = true;
    s_ota_floor = true;
}
uint32_t storage_repair_image_tag(void) { return s_image_tag; }
bool storage_repair_running_pending(void) { return s_running_pending; }
uint32_t storage_repair_store_writes(void) { return atomic_load(&s_store_writes); }
bool storage_repair_hold_probe(void) { return s_probe_hold; }
bool storage_repair_ota_floor(void) { return s_ota_floor; }

bool storage_repair_survey(bool *journal_names)
{
    if (journal_names) *journal_names = s_report.journal_names;
    return s_ran && !s_mount_failed && s_report.names_surveyed;
}

bool storage_repair_exception_repair(sx_repair_t *out)
{
    if (!out) return false;
    sx_from_srr(s_ran ? &s_report : NULL, out);
    out->ran = s_ran && out->ran;
    out->mount_failed = s_mount_failed;
    out->storage_allowed = !s_mount_blocked;
    out->pending_verify = s_running_pending;
    out->previous_failed = s_previous_failed;
    snprintf(out->area_test, sizeof(out->area_test), "%s", srx_test_name(s_area_test));
    out->records_pending = atomic_load(&s_records_pending);
    return out->ran;
}

bool storage_repair_exception_inventory(sx_inventory_t *out)
{
    if (!out) return false;
    memset(out, 0, sizeof(*out));
    si_report_t *scan = heap_caps_malloc(sizeof(*scan), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    bool done = scan && storage_inventory_snapshot(scan);
    if (done) {
        out->done = true;
        out->complete = scan->complete;
        out->error = scan->error;
        out->files = scan->files;
        out->unreadable_files = scan->unreadable_files;
        out->count = scan->count < SX_INVENTORY_FILES ? (uint32_t)scan->count : SX_INVENTORY_FILES;
        for (uint32_t i = 0; i < out->count; ++i) {
            const si_file_t *f = &scan->file[i];
            sx_inventory_file_t *t = &out->file[i];
            snprintf(t->name, sizeof(t->name), "%s", f->name);
            t->bytes = f->bytes;
            t->unreadable_bytes = f->unreadable_bytes;
            t->first_error_offset = f->first_error_offset;
            t->first_error = f->first_error;
            t->unreadable_spans = f->unreadable_spans;
            t->last_error_offset = f->last_error_offset;
            t->span_digest = f->span_digest;
            t->span_errors_other = f->span_errors_other;
            t->other_unreadable = f->other_unreadable;
        }
    }
    heap_caps_free(scan);
    return done;
}

/* Before the run: the record, and whether this boot is an exception image's
 * round boot (critique 11: custody blocks only with every custody file held). */
static void exception_before(srr_config_t *config)
{
    s_image_tag = config->image_tag;
    s_running_pending = config->pending_verify;
    s_previous_failed = config->previous_failed;
    s_exception_present = exception_load(&s_exception);
    s_exception_active = s_exception_present && sx_exception_active(&s_exception, s_image_tag, s_running_pending);
    config->custody_needs_hold = s_exception_active;
}

/* After the run, while the redo area is still registered. On an exception
 * image: spans that wait for byte sources keep the VFS off (2.7.6 decision 5
 * (c), critique 2). On an unconfirmed boot that could use the exception: the
 * area's program/erase test, CLAIMED journaled first so a torn test is this
 * device's own write (critique 3), and the persistence probe held. */
static void exception_after_run(void)
{
    if (s_exception_active && s_report.reclaim.waiting && !s_mount_blocked) {
        s_mount_blocked = true;
        queue_line("ERROR", "STORAGE_REPAIR_EXCEPTION_WAITING",
                   "spans wait for byte sources after the exception's round; the store stays unmounted this boot");
    }
    if (!s_running_pending || s_exception_active) return;
    sx_repair_t *probe = heap_caps_calloc(1, sizeof(*probe), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!probe) return;
    (void)storage_repair_exception_repair(probe);
    snprintf(probe->area_test, sizeof(probe->area_test), "PASSED");
    sx_reason_t verdict = sx_repair_verdict(probe);
    heap_caps_free(probe);
    if (verdict != SX_OK && verdict != SX_RECORDS_UNACKED) return;
    srp_reclaim_t journal;
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal)) != 1 || !srp_reclaim_valid(&journal))
        memset(&journal, 0, sizeof(journal));
    if (!(journal.flags & SRP_RECLAIM_CLAIMED)) {
        journal.flags |= SRP_RECLAIM_CLAIMED;
        srp_reclaim_seal(&journal);
        if (!port_nvs_store(NULL, SRR_NVS_NAMESPACE, "reclaim", &journal, sizeof(journal))) return;
        s_floor_seen = true;
    }
    uint8_t *scratch = heap_caps_malloc(SRX_BLOCK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_area_test = scratch ? srx_area_test(&REDO_IO, &s_area, scratch, NULL) : SRX_TEST_NOT_RUN;
    heap_caps_free(scratch);
    s_probe_hold = s_area_test == SRX_TEST_PASSED;
    char message[120];
    snprintf(message, sizeof(message), "the redo area's program/erase test: %s; the storage exception may apply",
             srx_test_name(s_area_test));
    queue_line(s_area_test == SRX_TEST_PASSED ? "WARN" : "ERROR", "STORAGE_REPAIR_EXCEPTION_AREA_TEST", message);
}

/* After everything: an exception image's round boot records its outcome and
 * a crash of the boot before (M7); the OTA floor. */
static void exception_after_boot(void)
{
    s_ota_floor = s_floor_seen || s_exception_active || s_mount_blocked;
    if (!s_exception_active) return;
    sx_repair_t *after = heap_caps_calloc(1, sizeof(*after), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!after) return;
    (void)storage_repair_exception_repair(after);
    sx_outcome_t outcome = sx_round_outcome(after);
    heap_caps_free(after);
    esp_reset_reason_t reason = boot_report_reset_reason();
    bool crashed = reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT || reason == ESP_RST_TASK_WDT ||
        reason == ESP_RST_WDT || reason == ESP_RST_CPU_LOCKUP || reason == ESP_RST_BROWNOUT;
    sx_record_t record = s_exception;
    uint8_t state = record.state;
    bool changed = sx_record_after_boot(&record, s_image_tag, s_running_pending, crashed, outcome);
    if (changed && !storage_repair_exception_store(&record))
        queue_line("ERROR", "STORAGE_REPAIR_EXCEPTION_RECORD", "the exception's record could not be updated");
    if (state == SX_STATE_ROUND_DONE && !crashed) return;
    char message[160];
    snprintf(message, sizeof(message), "round boot %u: %s; %s, %u restart(s), %u crash(es)", (unsigned)record.round_boots,
             sx_outcome_name(outcome), sx_state_name((sx_state_t)record.state), (unsigned)record.restarts,
             (unsigned)record.crashes);
    queue_line(outcome == SX_ROUND_OK ? "WARN" : "ERROR", "STORAGE_REPAIR_EXCEPTION_ROUND", message);
}

/* ---- the run --------------------------------------------------------- */
void storage_repair_boot(void)
{
    if (s_ran) return;
    s_ran = true;
    s_internal_before = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    nvs_entries(&s_nvs_free_before, &s_nvs_available_before);
    s_lines = heap_caps_calloc(SR_LOG_LINES, sizeof(sr_line_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, NULL);
    if (!s_partition) {
        s_mount_failed = true;
        s_mount_error = SPIFFS_ERR_NOT_A_FS;
        count_records();
        return;
    }
    srr_config_t config = boot_config();
    exception_before(&config);
    /* Internal: the bounce buffer every transfer of the store and of the redo
     * area goes through. */
    s_bounce = heap_caps_malloc(SR_BOUNCE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    int64_t limit = config.pending_verify ? SR_DEADLINE_PENDING_US : SR_DEADLINE_CONFIRMED_US;
    const esp_timer_create_args_t timer = {.callback = deadline_expired, .name = "repair_deadline"};
    bool timed = esp_timer_create(&timer, &s_deadline_timer) == ESP_OK &&
        esp_timer_start_once(s_deadline_timer, (uint64_t)limit) == ESP_OK;
    s_last_yield_us = esp_timer_get_time();
    s_soft_deadline_us = s_last_yield_us + limit - SR_SOFT_MARGIN_US;
    /* 2.7.6: a block a reset left half reformatted is completed first, on raw
     * flash, whatever else this boot can or cannot do (with the deadline). */
    redo_recover(&config, timed);
    if (!timed) {
        /* No deadline, no run: a hang here could never roll back. Nothing was
         * written to the store or the area. */
        ESP_LOGE(TAG, "deadline timer unavailable; storage repair skipped");
        s_report.result = SRR_RESULT_NOT_RUN;
        snprintf(s_report.error_stage, sizeof(s_report.error_stage), "deadline");
        if (s_deadline_timer) (void)esp_timer_delete(s_deadline_timer);
        s_deadline_timer = NULL;
        heap_caps_free(s_bounce);
        s_bounce = NULL;
        if (s_redo) (void)esp_partition_deregister_external(s_redo);
        s_redo = NULL;
        note_area();
        /* Records an earlier run left still go to ADD. */
        count_records();
        exception_after_boot();
        /* This boot's repair is over: a crash later in it is not the
         * repair's (round 1 review, low 2). */
        boot_report_repair_returned();
        return;
    }
    boot_report_repair(SRP_PHASE_SURVEY, 0, false);

    /* Internal: SPIFFS's own buffers and the lock (flash operations cannot
     * run from PSRAM stacks or need internal buffers). */
    uint32_t page = CONFIG_SPIFFS_PAGE_SIZE, block = SRX_BLOCK;
    memset(&s_efs, 0, sizeof(s_efs));
    memset(&s_fs, 0, sizeof(s_fs));
    s_fds_bytes = SR_FD_COUNT * sizeof(spiffs_fd);
    s_cache_bytes = sizeof(spiffs_cache) + SR_FD_COUNT * (sizeof(spiffs_cache_page) + page);
    s_work = heap_caps_calloc(1, page * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_fds = heap_caps_calloc(1, s_fds_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_cache = heap_caps_calloc(1, s_cache_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    s_efs.lock = xSemaphoreCreateMutex();
    s_efs.partition = s_partition;
    s_efs.fs = &s_fs;
    /* The metadata mirror first: it turns the check's 2.1 M header reads
     * into PSRAM copies, and is counted against the budget before holds. */
    size_t lookup_bytes = srm_lookup_bytes(s_partition->size, block, page);
    size_t header_bytes = srm_header_bytes(s_partition->size, page);
    uint8_t *lookup = heap_caps_malloc(lookup_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uint8_t *headers = heap_caps_malloc(header_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    bool ready = s_bounce && s_work && s_fds && s_cache && s_efs.lock && !s_mount_blocked;
    if (ready && lookup && headers) {
        const srm_flash_t raw = {NULL, raw_read, raw_write, raw_erase};
        uint8_t *scratch = heap_caps_malloc(block, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        s_mirror = scratch && srm_init(&s_mirror_state, &raw, s_partition->size, block, page, lookup, headers, scratch,
                                       block);
        heap_caps_free(scratch);
    }
    if (!s_mirror) {
        heap_caps_free(lookup);
        heap_caps_free(headers);
        lookup = headers = NULL;
    }
    size_t free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    uint64_t room = free_psram > SR_PSRAM_RESERVE + SR_ENGINE_BYTES ? free_psram - SR_PSRAM_RESERVE - SR_ENGINE_BYTES : 0;
    s_budget = room < SR_HOLD_CAP ? room : SR_HOLD_CAP;
    config.hold_budget = s_budget;
    /* The journal's NVS peak is checked before anything is written. */
    nvs_stats_t nvs_stats;
    config.nvs_known = nvs_get_stats(NULL, &nvs_stats) == ESP_OK;
    config.nvs_available_entries = config.nvs_known ? (uint32_t)nvs_stats.available_entries : 0;
    /* Until a pass is measured: the mirror serves the check's header reads
     * from PSRAM; without it every one is a flash read. */
    config.check_ms_per_mib = s_mirror ? SR_CHECK_MS_PER_MIB_MIRROR : SR_CHECK_MS_PER_MIB_FLASH;
    if (ready) {
        s_efs.cfg.hal_read_f = hal_read;
        s_efs.cfg.hal_write_f = hal_write;
        s_efs.cfg.hal_erase_f = hal_erase;
        s_efs.cfg.log_block_size = block;
        s_efs.cfg.log_page_size = page;
        s_efs.cfg.phys_addr = 0;
        s_efs.cfg.phys_erase_block = block;
        s_efs.cfg.phys_size = s_partition->size;
        s_fs.user_data = &s_efs;
        s32_t mounted = SPIFFS_mount(&s_fs, &s_efs.cfg, s_work, s_fds, s_fds_bytes, s_cache, s_cache_bytes,
                                     srr_check_callback);
        if (mounted == SPIFFS_OK) {
            srr_run(&s_fs, &config, &PORTS, &s_report);
            SPIFFS_unmount(&s_fs);
            /* A failed block's record nothing durable marks a source (round 2
             * review, R2-4): the store is not written this boot. */
            if (s_report.reclaim.store_blocked) s_mount_blocked = true;
        } else {
            s_mount_failed = true;
            s_mount_error = SPIFFS_errno(&s_fs);
            ESP_LOGE(TAG, "private mount failed %" PRId32 "; nothing repaired", s_mount_error);
            queue_line("ERROR", "STORAGE_REPAIR_MOUNT_FAILED", "the storage partition would not mount; nothing repaired");
        }
    } else if (s_mount_blocked) {
        snprintf(s_report.error_stage, sizeof(s_report.error_stage), "unmounted");
        s_report.result = SRR_RESULT_NOT_RUN;
    } else {
        snprintf(s_report.error_stage, sizeof(s_report.error_stage), "internal_memory");
        s_report.result = SRR_RESULT_FAILED;
    }
    note_area();
    exception_after_run();
    (void)esp_timer_stop(s_deadline_timer);
    (void)esp_timer_delete(s_deadline_timer);
    s_deadline_timer = NULL;
    led_status_set(LED_STATUS_BOOTING);
    heap_caps_free(lookup);
    heap_caps_free(headers);
    s_mirror_state.lookup = s_mirror_state.headers = NULL;
    heap_caps_free(s_cache);
    heap_caps_free(s_fds);
    heap_caps_free(s_work);
    s_cache = s_fds = s_work = NULL;
    heap_caps_free(s_bounce);
    s_bounce = NULL;
    if (s_efs.lock) vSemaphoreDelete(s_efs.lock);
    if (s_redo) (void)esp_partition_deregister_external(s_redo);
    s_redo = NULL;
    s_stack_free = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
    s_internal_after = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    nvs_entries(&s_nvs_free_after, &s_nvs_available_after);
    count_records();
    exception_after_boot();
    boot_report_repair_returned();
}

/* After the VFS mount: the command inbox, when the run filled a span of it
 * or found a torn command line, is merged by add_connector under its own
 * lock. The journal flag stays until that succeeds. */
void storage_repair_after_mount(void)
{
    srp_state_t state;
    bool loaded = port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) == 1 &&
        srp_state_valid(&state);
    bool flagged = loaded && (state.flags & SRP_STATE_COMMANDS_DAMAGED);
    /* A merge an earlier boot left unfinished runs again even unflagged. */
    if (!flagged && !add_connector_command_inbox_merge_pending()) return;
    if (!add_connector_repair_command_inbox()) {
        queue_line("ERROR", "STORAGE_REPAIR_COMMANDS_FAILED", "the command inbox could not be merged; it stays flagged");
        return;
    }
    if (flagged) {
        state.flags &= ~SRP_STATE_COMMANDS_DAMAGED;
        srp_state_seal(&state);
        (void)port_nvs_store(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state));
    }
    queue_line("WARN", "STORAGE_REPAIR_COMMANDS_MERGED", "the command inbox was rebuilt from its readable lines");
}

/* ---- reporting -------------------------------------------------------- */
static bool queue_generation(char *out, size_t size)
{
    char instance[33];
    if (!qs_generation(instance)) return false;
    int n = snprintf(out, size, "storage-repair-v1-%s", instance);
    return n > 0 && (size_t)n < size;
}

/* One PSRAM block per send: the records and the rendered evidence stay off
 * the ADD outbox task's stack. */
typedef struct {
    srp_gaps_t gaps;
    srp_resets_t resets;
    char generation[80], record[120], raw[512];
} sr_send_t;

static void count_records(void)
{
    sr_send_t *work = heap_caps_malloc(sizeof(*work), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!work) return;
    uint32_t pending = 0, acked = 0;
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "gaps", &work->gaps, sizeof(work->gaps)) == 1 &&
        work->gaps.count <= SRP_MAX_GAPS) {
        pending += srp_gaps_unacked(&work->gaps);
        acked += work->gaps.count - srp_gaps_unacked(&work->gaps);
    }
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "resets", &work->resets, sizeof(work->resets)) == 1 &&
        work->resets.count <= SRP_MAX_RESETS) {
        pending += srp_resets_unacked(&work->resets);
        acked += work->resets.count - srp_resets_unacked(&work->resets);
    }
    heap_caps_free(work);
    atomic_store(&s_records_pending, pending);
    atomic_store(&s_records_acked, acked);
}

static void record_acked(void)
{
    atomic_fetch_add(&s_records_sent, 1);
    atomic_fetch_add(&s_records_acked, 1);
    unsigned pending = atomic_load(&s_records_pending);
    if (pending) atomic_store(&s_records_pending, pending - 1);
}

static bool send_gap_evidence(sr_send_t *work)
{
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "gaps", &work->gaps, sizeof(work->gaps)) != 1 ||
        work->gaps.count > SRP_MAX_GAPS)
        return false;
    for (uint32_t i = 0; i < work->gaps.count; ++i) {
        srp_gap_t *gap = &work->gaps.gap[i];
        if (gap->flags & SRP_RECORD_ACKED) continue;
        size_t length = srp_gap_evidence(gap, work->raw, sizeof(work->raw));
        if (!length || !srp_gap_record_id(gap, work->record, sizeof(work->record)) ||
            !queue_generation(work->generation, sizeof(work->generation)) ||
            !add_connector_transfer_queue_evidence("storage_repair", work->generation, work->record, work->raw, length,
                                                   NULL, "MALFORMED"))
            return false;
        gap->flags |= SRP_RECORD_ACKED;
        (void)port_nvs_store(NULL, SRR_NVS_NAMESPACE, "gaps", &work->gaps, sizeof(work->gaps));
        record_acked();
        return true;
    }
    return false;
}

static bool send_reset_evidence(sr_send_t *work)
{
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "resets", &work->resets, sizeof(work->resets)) != 1 ||
        work->resets.count > SRP_MAX_RESETS)
        return false;
    for (uint32_t i = 0; i < work->resets.count; ++i) {
        srp_reset_t *reset = &work->resets.reset[i];
        if (reset->flags & SRP_RECORD_ACKED) continue;
        size_t length = srp_reset_evidence(reset, work->raw, sizeof(work->raw));
        if (!length || !srp_reset_record_id(reset, work->record, sizeof(work->record)) ||
            !queue_generation(work->generation, sizeof(work->generation)) ||
            !add_connector_transfer_queue_evidence("storage_repair", work->generation, work->record, work->raw, length,
                                                   NULL, "LEGACY_RECOVERY"))
            return false;
        reset->flags |= SRP_RECORD_ACKED;
        (void)port_nvs_store(NULL, SRR_NVS_NAMESPACE, "resets", &work->resets, sizeof(work->resets));
        record_acked();
        return true;
    }
    return false;
}

void storage_repair_report_step(void)
{
    /* A refused or unanswered transfer waits a minute: each attempt can hold
     * the outbox task for an acknowledgement timeout. */
    static int64_t retry_at_us;
    if (!add_connector_is_connected()) return;
    for (unsigned i = 0; s_lines && i < 4; ++i) {
        unsigned next = atomic_load(&s_lines_sent);
        if (next >= atomic_load(&s_lines_count)) break;
        const sr_line_t *line = &s_lines[next];
        if (!add_connector_log(line->level, "storage", line->code, line->message)) return;
        atomic_store(&s_lines_sent, next + 1);
    }
    if (!atomic_load(&s_records_pending)) return;
    int64_t now = esp_timer_get_time();
    if (now < retry_at_us) return;
    sr_send_t *work = heap_caps_malloc(sizeof(*work), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    /* Gaps first: each is ADD's only record of bytes nobody can read. */
    bool sent = work && (send_gap_evidence(work) || send_reset_evidence(work));
    heap_caps_free(work);
    if (!sent) {
        retry_at_us = now + 60LL * 1000000LL;
        /* Nothing left to send (or NVS changed under us): recount. */
        count_records();
    }
}

/* ---- heartbeat --------------------------------------------------------- */
static bool add_actions(cJSON *row, uint16_t actions)
{
    static const struct {
        uint16_t bit;
        const char *name;
    } names[] = {{SRR_FILE_NORMALIZED, "NORMALIZED"}, {SRR_FILE_GAPS, "GAPS"}, {SRR_FILE_RESTORED, "RESTORED"},
                 {SRR_FILE_REBUILT, "REBUILT"}, {SRR_FILE_TAIL, "TAIL_TERMINATED"},
                 {SRR_FILE_CURSOR_RESET, "CURSOR_RESET"}, {SRR_FILE_REMOVED, "REMOVED"}, {SRR_FILE_UNHELD, "UNHELD"},
                 {SRR_FILE_SIZE_RECOVERED, "SIZE_RECOVERED"}, {SRR_FILE_SUPERSEDED, "SUPERSEDED"},
                 {SRR_FILE_REDO_SOURCE, "REDO_SOURCE"}};
    cJSON *list = cJSON_AddArrayToObject(row, "actions");
    if (!list) return false;
    unsigned listed = 0;
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]) && listed < 8; ++i) {
        if (!(actions & names[i].bit)) continue;
        ++listed;
        cJSON *item = cJSON_CreateString(names[i].name);
        if (!item || !cJSON_AddItemToArray(list, item)) {
            cJSON_Delete(item);
            return false;
        }
    }
    return true;
}

static bool printable(const char *text)
{
    for (; *text; ++text)
        if ((unsigned char)*text < 0x20 || (unsigned char)*text > 0x7e) return false;
    return true;
}

bool storage_repair_append(cJSON *storage)
{
    if (!s_ran) return true;
    const srr_report_t *r = &s_report;
    uint32_t acked = atomic_load(&s_records_acked), pending = atomic_load(&s_records_pending);
    cJSON *repair = cJSON_AddObjectToObject(storage, "repair");
    if (!repair) return false;
    const char *result = s_mount_failed ? "MOUNT_FAILED" : srr_result_name(r->result);
    bool ok = cJSON_AddStringToObject(repair, "result", result) &&
        cJSON_AddStringToObject(repair, "mode", srp_mode_name(r->mode)) &&
        cJSON_AddStringToObject(repair, "phase", srp_phase_name(r->phase)) &&
        cJSON_AddStringToObject(repair, "previous_phase", srp_phase_name(r->previous_phase)) &&
        cJSON_AddNumberToObject(repair, "generation", r->generation) &&
        cJSON_AddNumberToObject(repair, "incomplete", r->incomplete) &&
        cJSON_AddNumberToObject(repair, "failures", r->failures) &&
        cJSON_AddNumberToObject(repair, "level", r->level) &&
        cJSON_AddBoolToObject(repair, "check_skipped", r->check_skipped) &&
        cJSON_AddBoolToObject(repair, "mirror", s_mirror) &&
        cJSON_AddNumberToObject(repair, "duration_ms", r->duration_ms) &&
        cJSON_AddNumberToObject(repair, "check_ms", r->check_ms) &&
        cJSON_AddNumberToObject(repair, "budget_bytes", (double)s_budget) &&
        cJSON_AddNumberToObject(repair, "files", r->files) &&
        cJSON_AddNumberToObject(repair, "files_held", r->files_held) &&
        cJSON_AddNumberToObject(repair, "bytes_held", (double)r->bytes_held) &&
        cJSON_AddNumberToObject(repair, "files_unheld_custody", r->files_unheld_custody) &&
        cJSON_AddNumberToObject(repair, "gaps", r->gaps) &&
        cJSON_AddNumberToObject(repair, "unreadable_bytes", r->unreadable_bytes) &&
        cJSON_AddNumberToObject(repair, "records_reported", acked) &&
        cJSON_AddNumberToObject(repair, "records_pending", pending) &&
        cJSON_AddNumberToObject(repair, "records_dropped", r->records_dropped) &&
        cJSON_AddNumberToObject(repair, "spans_rewritten", r->spans_rewritten) &&
        cJSON_AddNumberToObject(repair, "index_rewritten", r->index_rewritten) &&
        cJSON_AddNumberToObject(repair, "duplicates_removed", r->duplicates_removed) &&
        cJSON_AddNumberToObject(repair, "orphan_index_removed", r->orphan_index_removed) &&
        cJSON_AddNumberToObject(repair, "blocks_compacted", r->blocks_compacted) &&
        cJSON_AddNumberToObject(repair, "reserve_pages", r->reserve_pages) &&
        (!r->room_needed || (cJSON_AddNumberToObject(repair, "room_needed_pages", r->room_needed) &&
                             cJSON_AddNumberToObject(repair, "room_erased_pages", r->room_erased))) &&
        cJSON_AddNumberToObject(repair, "sizes_recovered", r->sizes_recovered) &&
        cJSON_AddNumberToObject(repair, "strays_removed", r->strays_removed) &&
        cJSON_AddNumberToObject(repair, "files_superseded", r->files_superseded) &&
        cJSON_AddNumberToObject(repair, "lookup_fixed", r->lookup_fixed) &&
        cJSON_AddNumberToObject(repair, "tails_terminated", r->tails_terminated) &&
        cJSON_AddNumberToObject(repair, "files_restored", r->files_restored) &&
        cJSON_AddNumberToObject(repair, "files_rebuilt", r->files_rebuilt) &&
        cJSON_AddNumberToObject(repair, "cursors_reset", r->cursors_reset) &&
        cJSON_AddNumberToObject(repair, "expendables_removed", r->expendables_removed) &&
        cJSON_AddNumberToObject(repair, "settled_removed", r->settled_removed) &&
        cJSON_AddNumberToObject(repair, "interrupted_losses", r->interrupted_losses) &&
        cJSON_AddNumberToObject(repair, "used_before", r->used_before) &&
        cJSON_AddNumberToObject(repair, "used_after", r->used_after) &&
        cJSON_AddNumberToObject(repair, "total_bytes", r->total_bytes) &&
        cJSON_AddNumberToObject(repair, "nvs_free_entries_before", s_nvs_free_before) &&
        cJSON_AddNumberToObject(repair, "nvs_free_entries_after", s_nvs_free_after) &&
        cJSON_AddNumberToObject(repair, "nvs_available_entries_before", s_nvs_available_before) &&
        cJSON_AddNumberToObject(repair, "nvs_available_entries_after", s_nvs_available_after) &&
        cJSON_AddNumberToObject(repair, "internal_largest_before", s_internal_before) &&
        cJSON_AddNumberToObject(repair, "internal_largest_after", s_internal_after) &&
        cJSON_AddNumberToObject(repair, "stack_free_bytes", s_stack_free) &&
        cJSON_AddBoolToObject(repair, "commands_damaged", r->commands_damaged) &&
        cJSON_AddNumberToObject(repair, "log_lines_dropped", atomic_load(&s_lines_dropped));
    if (ok && s_previous_known)
        ok = cJSON_AddStringToObject(repair, "previous_boot_phase", srp_phase_name((srp_phase_t)s_previous_phase)) &&
            cJSON_AddBoolToObject(repair, "previous_boot_deadline", s_previous_deadline) &&
            cJSON_AddBoolToObject(repair, "previous_boot_returned", s_previous_returned);
    if (ok && (r->error_stage[0] || s_mount_failed))
        ok = cJSON_AddStringToObject(repair, "error_stage", s_mount_failed ? "mount" : r->error_stage) &&
            cJSON_AddNumberToObject(repair, "error_code", s_mount_failed ? s_mount_error : r->error_code);
    cJSON *check = ok ? cJSON_AddObjectToObject(repair, "check") : NULL;
    ok = check && cJSON_AddNumberToObject(check, "passes", r->check_passes) &&
        cJSON_AddNumberToObject(check, "fix_index", r->check_fix_index) &&
        cJSON_AddNumberToObject(check, "fix_lookup", r->check_fix_lookup) &&
        cJSON_AddNumberToObject(check, "delete_orphaned_index", r->check_delete_orphan_index) &&
        cJSON_AddNumberToObject(check, "delete_page", r->check_delete_page) &&
        cJSON_AddNumberToObject(check, "delete_bad_file", r->check_delete_bad_file) &&
        cJSON_AddNumberToObject(check, "deleted_live_files", r->check_deleted_live) &&
        cJSON_AddNumberToObject(check, "errors", r->check_errors) &&
        cJSON_AddNumberToObject(check, "last_error", r->check_last_error);
    /* 2.7.6: the last resort, every boot (the area's state at least). */
    const srr_reclaim_report_t *x = &r->reclaim;
    cJSON *reclaim = ok ? cJSON_AddObjectToObject(repair, "reclaim") : NULL;
    ok = reclaim && cJSON_AddNumberToObject(repair, "runs", r->runs) &&
        cJSON_AddStringToObject(reclaim, "area", srx_area_name((srx_area_state_t)x->area)) &&
        cJSON_AddNumberToObject(reclaim, "rounds", x->rounds) && cJSON_AddNumberToObject(reclaim, "blocks", x->blocks) &&
        cJSON_AddNumberToObject(reclaim, "pages_freed", x->pages_freed) &&
        cJSON_AddNumberToObject(reclaim, "pages_kept", x->pages_kept) &&
        cJSON_AddNumberToObject(reclaim, "erased_after", x->erased_after) &&
        cJSON_AddNumberToObject(reclaim, "redone", x->redone) && cJSON_AddNumberToObject(reclaim, "sources", x->sources) &&
        cJSON_AddNumberToObject(reclaim, "settled", x->settled) &&
        cJSON_AddNumberToObject(reclaim, "reported", x->reported) &&
        cJSON_AddNumberToObject(reclaim, "records_open", x->records_open) &&
        cJSON_AddNumberToObject(reclaim, "failed", x->failed) &&
        cJSON_AddNumberToObject(reclaim, "held_mismatch", x->held_mismatch) &&
        cJSON_AddNumberToObject(reclaim, "ms", x->ms);
    if (ok && x->wanted)
        ok = cJSON_AddStringToObject(reclaim, "trigger", x->trigger) &&
            (!x->short_reason[0] || cJSON_AddStringToObject(reclaim, "short", x->short_reason)) &&
            cJSON_AddNumberToObject(reclaim, "candidates", x->candidates) &&
            cJSON_AddNumberToObject(reclaim, "unconfirmed_blocks", x->unconfirmed_blocks) &&
            cJSON_AddNumberToObject(reclaim, "custody_blocks", x->custody_blocks) &&
            cJSON_AddNumberToObject(reclaim, "index_blocks", x->index_blocks) &&
            cJSON_AddNumberToObject(reclaim, "excluded_blocks", x->excluded_blocks) &&
            cJSON_AddNumberToObject(reclaim, "low_magic_blocks", x->low_magic_blocks);
    /* 2.7.7: the planner's room, the excluded blocks by reason and the store
     * as the first survey found it (diagnostics); the area test and the OTA
     * floor of the confirm-first exception. */
    if (ok && x->wanted)
        ok = cJSON_AddNumberToObject(reclaim, "want", x->want) && cJSON_AddNumberToObject(reclaim, "target", x->target) &&
            cJSON_AddNumberToObject(reclaim, "custody_gain", x->custody_gain) &&
            cJSON_AddNumberToObject(reclaim, "unconfirmed_gain", x->unconfirmed_gain) &&
            cJSON_AddNumberToObject(reclaim, "held_out", x->held_out) &&
            cJSON_AddNumberToObject(reclaim, "excluded_hidden", x->excluded_hidden) &&
            cJSON_AddNumberToObject(reclaim, "excluded_unwindowed", x->excluded_unwindowed) &&
            cJSON_AddNumberToObject(reclaim, "excluded_last_page", x->excluded_last_page) &&
            cJSON_AddNumberToObject(reclaim, "excluded_index_flag", x->excluded_index_flag) &&
            cJSON_AddNumberToObject(reclaim, "excluded_busy", x->excluded_busy) &&
            cJSON_AddNumberToObject(reclaim, "excluded_mixed", x->excluded_mixed) &&
            (!x->excluded_file[0] || !printable(x->excluded_file) || strlen(x->excluded_file) > 32 ||
             (cJSON_AddStringToObject(reclaim, "excluded_file", x->excluded_file) &&
              cJSON_AddNumberToObject(reclaim, "excluded_file_blocks", x->excluded_file_blocks)));
    if (ok && x->mount_measured)
        ok = cJSON_AddNumberToObject(reclaim, "erased_at_mount", x->erased_at_mount) &&
            cJSON_AddNumberToObject(reclaim, "free_blocks_at_mount", x->free_blocks_at_mount);
    if (ok && x->waiting) ok = cJSON_AddNumberToObject(reclaim, "waiting", x->waiting) != NULL;
    if (ok && s_area_test != SRX_TEST_NOT_RUN)
        ok = cJSON_AddStringToObject(reclaim, "area_test", srx_test_name(s_area_test)) != NULL;
    if (ok) ok = cJSON_AddBoolToObject(reclaim, "ota_floor", s_ota_floor) != NULL;
    if (ok) ok = cJSON_AddBoolToObject(repair, "manifest_active", r->manifest_active) != NULL;
    if (ok && r->names_surveyed) ok = cJSON_AddBoolToObject(repair, "journal_names", r->journal_names) != NULL;
    if (ok && x->area == SRX_AREA_FOREIGN)
        ok = cJSON_AddNumberToObject(reclaim, "area_foreign_sectors", x->area_foreign) &&
            cJSON_AddNumberToObject(reclaim, "area_first_foreign", x->area_first_foreign) &&
            cJSON_AddNumberToObject(reclaim, "area_foreign_crc", x->area_foreign_crc);
    cJSON *files = ok ? cJSON_AddArrayToObject(repair, "entries") : NULL;
    if (!files) return false;
    for (uint32_t i = 0; i < r->file_count && i < SRR_REPORT_FILES; ++i) {
        const srr_report_file_t *f = &r->file[i];
        if (!printable(f->name)) continue;
        cJSON *row = cJSON_CreateObject();
        bool row_ok = row && cJSON_AddStringToObject(row, "name", f->name) &&
            cJSON_AddStringToObject(row, "class", srp_class_name((srp_class_t)f->cls)) &&
            cJSON_AddNumberToObject(row, "size", f->size) && cJSON_AddNumberToObject(row, "gaps", f->gaps) &&
            cJSON_AddNumberToObject(row, "unreadable_bytes", f->unreadable_bytes) &&
            cJSON_AddNumberToObject(row, "spans_rewritten", f->spans_rewritten) &&
            (!f->blocked_spans || (cJSON_AddNumberToObject(row, "blocked_spans", f->blocked_spans) &&
                                   cJSON_AddNumberToObject(row, "blocked_offset", f->blocked_offset) &&
                                   cJSON_AddNumberToObject(row, "blocked_last", r->blocked_last[i]) &&
                                   cJSON_AddNumberToObject(row, "blocked_digest", r->blocked_digest[i]))) &&
            add_actions(row, f->actions);
        if (!row_ok || !cJSON_AddItemToArray(files, row)) {
            cJSON_Delete(row);
            return false;
        }
    }
    return true;
}
