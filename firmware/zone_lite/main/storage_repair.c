#include "storage_repair.h"

#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "add_connector.h"
#include "boot_report.h"
#include "esp_app_desc.h"
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
#include "storage_repair_core.h"
#include "storage_repair_mirror.h"

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

typedef struct {
    char level[6], code[40], message[200];
} sr_line_t;

static srr_report_t s_report;
static bool s_ran, s_mount_failed, s_mirror, s_previous_deadline, s_previous_known, s_previous_returned;
static unsigned s_previous_phase, s_previous_pass;
static int32_t s_mount_error;
static uint64_t s_budget;
static uint32_t s_nvs_free_before, s_nvs_free_after, s_internal_before, s_internal_after, s_stack_free;
static sr_line_t *s_lines;
static atomic_uint s_lines_count, s_lines_sent, s_lines_dropped;
static atomic_uint s_records_sent;
/* Gap and cursor-reset records awaiting, and holding, ADD's acknowledgement:
 * counted from NVS once after the run and kept here, so the outbox task and
 * the heartbeat never read NVS when nothing is pending. */
static atomic_uint s_records_pending, s_records_acked;
static atomic_bool s_damage_noted;

static const esp_partition_t *s_partition;
static uint8_t *s_bounce;
static srm_mirror_t s_mirror_state;
static int64_t s_last_yield_us, s_soft_deadline_us;
static esp_timer_handle_t s_deadline_timer;
static volatile unsigned s_phase, s_pass;

static void count_records(void);

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
};

/* ---- the deadline ---------------------------------------------------- */
/* Never depends on the repair code: if a run outlives it, whatever it is
 * doing, the breadcrumb is kept and the ESP restarts. On an unconfirmed image
 * the bootloader then rolls back; on a confirmed one the attempts guard
 * counts it, and after three such runs repairs stop for this image. */
static void deadline_expired(void *argument)
{
    (void)argument;
    boot_report_repair(s_phase, s_pass, true);
    esp_restart();
}

static uint32_t nvs_free_entries(void)
{
    nvs_stats_t stats;
    return nvs_get_stats(NULL, &stats) == ESP_OK ? (uint32_t)stats.free_entries : 0;
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
    bool crash = reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT || reason == ESP_RST_TASK_WDT ||
        reason == ESP_RST_WDT || reason == ESP_RST_CPU_LOCKUP;
    config.previous_failed = s_previous_known && (s_previous_deadline || (crash && !s_previous_returned));
    const esp_app_desc_t *app = esp_app_get_description();
    if (app) memcpy(&config.image_tag, app->app_elf_sha256, sizeof(config.image_tag));
    config.damage_seen = damage_flag_take();
    return config;
}

/* ---- the run --------------------------------------------------------- */
void storage_repair_boot(void)
{
    if (s_ran) return;
    s_ran = true;
    s_internal_before = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    s_nvs_free_before = nvs_free_entries();
    s_lines = heap_caps_calloc(SR_LOG_LINES, sizeof(sr_line_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, NULL);
    if (!s_partition) {
        s_mount_failed = true;
        s_mount_error = SPIFFS_ERR_NOT_A_FS;
        return;
    }
    srr_config_t config = boot_config();
    int64_t limit = config.pending_verify ? SR_DEADLINE_PENDING_US : SR_DEADLINE_CONFIRMED_US;
    const esp_timer_create_args_t timer = {.callback = deadline_expired, .name = "repair_deadline"};
    if (esp_timer_create(&timer, &s_deadline_timer) != ESP_OK ||
        esp_timer_start_once(s_deadline_timer, (uint64_t)limit) != ESP_OK) {
        /* No deadline, no run: a hang here could never roll back. */
        ESP_LOGE(TAG, "deadline timer unavailable; storage repair skipped");
        s_report.result = SRR_RESULT_NOT_RUN;
        snprintf(s_report.error_stage, sizeof(s_report.error_stage), "deadline");
        return;
    }
    s_last_yield_us = esp_timer_get_time();
    s_soft_deadline_us = s_last_yield_us + limit - SR_SOFT_MARGIN_US;
    boot_report_repair(SRP_PHASE_SURVEY, 0, false);

    /* Internal: the bounce buffer, SPIFFS's own buffers and the lock (flash
     * operations cannot run from PSRAM stacks or need internal buffers). */
    uint32_t page = CONFIG_SPIFFS_PAGE_SIZE, block = 4096U;
    static esp_spiffs_t efs;
    static spiffs fs;
    memset(&efs, 0, sizeof(efs));
    memset(&fs, 0, sizeof(fs));
    size_t fds_bytes = SR_FD_COUNT * sizeof(spiffs_fd);
    size_t cache_bytes = sizeof(spiffs_cache) + SR_FD_COUNT * (sizeof(spiffs_cache_page) + page);
    s_bounce = heap_caps_malloc(SR_BOUNCE_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *work = heap_caps_calloc(1, page * 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *fds = heap_caps_calloc(1, fds_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint8_t *cache = heap_caps_calloc(1, cache_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    efs.lock = xSemaphoreCreateMutex();
    efs.partition = s_partition;
    efs.fs = &fs;
    /* The metadata mirror first: it turns the check's 2.1 M header reads
     * into PSRAM copies, and is counted against the budget before holds. */
    size_t lookup_bytes = srm_lookup_bytes(s_partition->size, block, page);
    size_t header_bytes = srm_header_bytes(s_partition->size, page);
    uint8_t *lookup = heap_caps_malloc(lookup_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uint8_t *headers = heap_caps_malloc(header_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    bool ready = s_bounce && work && fds && cache && efs.lock;
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
    config.nvs_free_entries = config.nvs_known ? (uint32_t)nvs_stats.free_entries : 0;
    /* Until a pass is measured: the mirror serves the check's header reads
     * from PSRAM; without it every one is a flash read. */
    config.check_ms_per_mib = s_mirror ? SR_CHECK_MS_PER_MIB_MIRROR : SR_CHECK_MS_PER_MIB_FLASH;
    if (ready) {
        efs.cfg.hal_read_f = hal_read;
        efs.cfg.hal_write_f = hal_write;
        efs.cfg.hal_erase_f = hal_erase;
        efs.cfg.log_block_size = block;
        efs.cfg.log_page_size = page;
        efs.cfg.phys_addr = 0;
        efs.cfg.phys_erase_block = block;
        efs.cfg.phys_size = s_partition->size;
        fs.user_data = &efs;
        s32_t mounted = SPIFFS_mount(&fs, &efs.cfg, work, fds, fds_bytes, cache, cache_bytes, srr_check_callback);
        if (mounted == SPIFFS_OK) {
            srr_run(&fs, &config, &PORTS, &s_report);
            SPIFFS_unmount(&fs);
        } else {
            s_mount_failed = true;
            s_mount_error = SPIFFS_errno(&fs);
            ESP_LOGE(TAG, "private mount failed %" PRId32 "; nothing repaired", s_mount_error);
            queue_line("ERROR", "STORAGE_REPAIR_MOUNT_FAILED", "the storage partition would not mount; nothing repaired");
        }
    } else {
        snprintf(s_report.error_stage, sizeof(s_report.error_stage), "internal_memory");
        s_report.result = SRR_RESULT_FAILED;
    }
    (void)esp_timer_stop(s_deadline_timer);
    (void)esp_timer_delete(s_deadline_timer);
    s_deadline_timer = NULL;
    led_status_set(LED_STATUS_BOOTING);
    heap_caps_free(lookup);
    heap_caps_free(headers);
    s_mirror_state.lookup = s_mirror_state.headers = NULL;
    heap_caps_free(cache);
    heap_caps_free(fds);
    heap_caps_free(work);
    heap_caps_free(s_bounce);
    s_bounce = NULL;
    if (efs.lock) vSemaphoreDelete(efs.lock);
    s_stack_free = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
    s_internal_after = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    s_nvs_free_after = nvs_free_entries();
    count_records();
    boot_report_repair_returned();
}

/* After the VFS mount: the command inbox, when the run filled a span of it
 * or found a torn command line, is merged by add_connector under its own
 * lock. The journal flag stays until that succeeds. */
void storage_repair_after_mount(void)
{
    srp_state_t state;
    if (port_nvs_load(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state)) != 1 || !srp_state_valid(&state) ||
        !(state.flags & SRP_STATE_COMMANDS_DAMAGED))
        return;
    if (!add_connector_repair_command_inbox()) {
        queue_line("ERROR", "STORAGE_REPAIR_COMMANDS_FAILED", "the command inbox could not be merged; it stays flagged");
        return;
    }
    state.flags &= ~SRP_STATE_COMMANDS_DAMAGED;
    srp_state_seal(&state);
    (void)port_nvs_store(NULL, SRR_NVS_NAMESPACE, "state", &state, sizeof(state));
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
                 {SRR_FILE_SIZE_RECOVERED, "SIZE_RECOVERED"}, {SRR_FILE_SUPERSEDED, "SUPERSEDED"}};
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
            cJSON_AddNumberToObject(row, "spans_rewritten", f->spans_rewritten) && add_actions(row, f->actions);
        if (!row_ok || !cJSON_AddItemToArray(files, row)) {
            cJSON_Delete(row);
            return false;
        }
    }
    return true;
}
