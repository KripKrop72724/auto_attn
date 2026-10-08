#include "zkt_storage_owner.h"
#include "zkt_journal_crypto.h"
#include "zkt_journal_state.h"
#include "zkt_custody_wire.h"
#include "zkt_journal_transport.h"
#include "zkt_reader_platform.h"
#include "zkt_runtime_checkpoint.h"
#include "zkt_rollback.h"
#include "zkt_add_legacy_owner.h"
#include "zkt_quarantine_owner.h"
#include "zkt_legacy_attendance.h"
#include "zkt_legacy_inventory.h"
#include "queue_store.h"
#include <dirent.h>
#include <errno.h>
#include <string.h>
#include <time.h>
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_app_desc.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/platform_util.h"
#include "nvs.h"

typedef struct {
    zj_mailbox_t mailbox;
    zj_store_t store;
    zj_state_t state;
    zj_crypto_key_t key;
    zj_metadata_t metadata;
    zc_store_t catalog;
    zc_store_t commands;
    zi_store_t command_ids;
    zq_store_t segmented;
    zq_inventory_t legacy_inventory;
    zj_owner_health_t health;
    char prefix[112];
    int nvs_error;
    bool opening_store;
    bool writer_allowed, compatibility_checked;
    zj_compat_result_t compatibility;
    uint64_t retry_at_us;
    uint64_t io_revision;
    char wire_scratch[ZJ_CUSTODY_PAYLOAD_MAX];
    /* One reserved shutdown control, independent of retained DONE replies.
     * Its immutable target and uncertain result survive caller timeouts. */
    uint64_t reader_ticket;
    bool reader_bound, reader_completed;
    ota_checkpoint_t reader_expected, reader_committed;
    zj_result_t reader_result;
    zj_compat_result_t reader_compatibility;
} owner_t;
static owner_t *owner;
static SemaphoreHandle_t mailbox_lock;
static TaskHandle_t owner_task;
/* Both journal workers must have their full internal stacks reserved before
 * Wi-Fi/TLS allocation; fixing transport must not shift fragmentation failure
 * to the storage task which starts first. Large mailbox state stays in PSRAM. */
#define ZJ_OWNER_STACK_BYTES 12288U
static DRAM_ATTR StackType_t owner_stack[ZJ_OWNER_STACK_BYTES / sizeof(StackType_t)]
    __attribute__((aligned(16)));
static DRAM_ATTR StaticTask_t owner_control;
_Static_assert(sizeof(owner_stack) == ZJ_OWNER_STACK_BYTES, "journal owner stack size");

bool zj_owner_started(void) { return owner_task != NULL; }
bool zj_owner_is_current_task(void)
{ return owner_task && xTaskGetCurrentTaskHandle() == owner_task; }

static bool enter(void)
{
    return mailbox_lock && xSemaphoreTake(mailbox_lock, pdMS_TO_TICKS(100)) == pdTRUE;
}
static int state_read(void *context, const char *name, uint8_t *out, size_t length)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zkt_journal", NVS_READONLY, &handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK) { o->nvs_error = status; return -1; }
    size_t actual = length;
    status = nvs_get_blob(handle, name, out, &actual);
    nvs_close(handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK || actual != length) {
        o->nvs_error = status == ESP_OK ? ESP_ERR_INVALID_SIZE : status;
        return -1;
    }
    return 1;
}
static int file_checkpoint_load(void *context, const char *key, ft_checkpoint_t *checkpoint)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("file_tx", NVS_READONLY, &handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK) { o->nvs_error = status; return -1; }
    size_t length = sizeof(*checkpoint);
    status = nvs_get_blob(handle, key, checkpoint, &length);
    nvs_close(handle);
    if (status == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (status != ESP_OK || length != sizeof(*checkpoint)) {
        o->nvs_error = status == ESP_OK ? ESP_ERR_INVALID_SIZE : status;
        return -1;
    }
    return 1;
}
static bool file_checkpoint_commit(void *context, const char *key, const ft_checkpoint_t *checkpoint)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("file_tx", NVS_READWRITE, &handle);
    if (status == ESP_OK) {
        status = nvs_set_blob(handle, key, checkpoint, sizeof(*checkpoint));
        if (status == ESP_OK) status = nvs_commit(handle);
        nvs_close(handle);
    }
    if (status != ESP_OK) o->nvs_error = status;
    return status == ESP_OK;
}
static int catalog_load(void *context, ft_checkpoint_t *checkpoint)
{ return file_checkpoint_load(context, "catalog", checkpoint); }
static bool catalog_commit(void *context, const ft_checkpoint_t *checkpoint)
{ return file_checkpoint_commit(context, "catalog", checkpoint); }
static int commands_load(void *context, ft_checkpoint_t *checkpoint)
{ return file_checkpoint_load(context, "commands", checkpoint); }
static bool commands_commit(void *context, const ft_checkpoint_t *checkpoint)
{ return file_checkpoint_commit(context, "commands", checkpoint); }
static bool command_id_admit(void *context, size_t bytes)
{ (void)context; return qs_local_admit_locked(QS_ADMIT_RECOVERY, bytes); }
static bool execute_command_ids(owner_t *o, uint64_t ticket, const zj_request_t *request, zj_reply_t *reply)
{
    memset(reply, 0, sizeof(*reply)); o->nvs_error = 0;
    o->store.last_errno = 0; o->store.last_operation = NULL;
    if (!qs_local_read_begin()) {
        reply->result = ZJ_IO;
        reply->command_ids.error = errno; reply->command_ids.operation = "command_id_lock";
        o->store.last_errno = reply->command_ids.error; o->store.last_operation = reply->command_ids.operation;
        return o->command_ids.work_ticket == ticket;
    }
    bool pending = zi_store_step(&o->command_ids, ticket, (uint64_t)esp_timer_get_time(),
        &request->input.command_ids, &reply->command_ids, &reply->result);
    /* Command receipts have their own hold/error, not an attendance verdict. */
    qs_local_end(true, 0);
    o->store.last_errno = reply->command_ids.error; o->store.last_operation = reply->command_ids.operation;
    return pending;
}
static bool execute_catalog(owner_t *o, uint64_t ticket, const zj_request_t *request, zj_reply_t *reply)
{
    const zc_request_t *catalog = &request->input.catalog;
    bool commands = request->operation == ZJ_COMMANDS;
    zc_store_t *files = commands ? &o->commands : &o->catalog;
    memset(reply, 0, sizeof(*reply));
    o->nvs_error = 0;
    o->store.last_errno = 0;
    o->store.last_operation = NULL;
    /* Do not inherit the legacy optional producer's ten-second lock wait on
     * the attendance owner. Take its short local lock, then check capacity. */
    bool locked = qs_local_read_begin();
    size_t bytes = locked ? zc_store_admission_bytes(files, catalog) : 0;
    bool admitted = locked && (!bytes || qs_local_admit_locked(
        commands ? QS_ADMIT_RECOVERY : QS_ADMIT_OPTIONAL_HISTORICAL, bytes));
    if (!admitted) {
        reply->catalog.error = errno;
        reply->catalog.operation = commands ?
            (errno == EBUSY ? "commands_lock" : "commands_admission") :
            (errno == EBUSY ? "catalog_lock" : "catalog_admission");
        reply->result = errno == ENOSPC ? ZJ_FULL : ZJ_IO;
        if (locked) qs_local_end(true, 0);
        o->store.last_errno = reply->catalog.error;
        o->store.last_operation = reply->catalog.operation;
        /* A yielded activation must retain its transaction and ticket if
         * a later lock acquisition fails. No admitted intent is discarded. */
        return files->work_ticket == ticket;
    }
    bool pending = zc_store_step(files, ticket, (uint64_t)esp_timer_get_time(), catalog,
        &reply->catalog, &reply->result);
    /* Optional catalog failure cannot become an attendance write failure.
     * Its captured filesystem/NVS error remains in this request's reply. */
    qs_local_end(true, 0);
    o->store.last_errno = reply->catalog.error;
    o->store.last_operation = reply->catalog.operation;
    return pending;
}
static bool state_write(void *context, const char *name, const uint8_t *bytes, size_t length)
{
    owner_t *o = context;
    nvs_handle_t handle;
    esp_err_t status = nvs_open("zkt_journal", NVS_READWRITE, &handle);
    if (status == ESP_OK) {
        status = nvs_set_blob(handle, name, bytes, length);
        if (status == ESP_OK) status = nvs_commit(handle);
        nvs_close(handle);
    }
    if (status != ESP_OK) o->nvs_error = status;
    return status == ESP_OK;
}
static bool state_random(void *context, uint8_t *out, size_t length)
{
    (void)context;
    esp_fill_random(out, length);
    return true;
}
static bool journal_absent(void *context)
{
    owner_t *o = context;
    char directory[112];
    const char *base = strrchr(o->prefix, '/');
    if (!base || !base[1]) return false;
    size_t length = (size_t)(base - o->prefix);
    memcpy(directory, o->prefix, length);
    directory[length] = 0;
    if (!length) strcpy(directory, "/");
    ++base;
    DIR *dir = opendir(directory);
    if (!dir) return false;
    bool absent = true;
    struct dirent *entry;
    for (;;) {
        errno = 0;
        entry = readdir(dir);
        if (!entry) { if (errno) absent = false; break; }
        if (!strncmp(entry->d_name, base, strlen(base))) absent = false;
    }
    if (closedir(dir) != 0) absent = false;
    return absent;
}
static bool admitted(void *context, size_t bytes)
{
    zj_state_t *state = context;
    owner_t *o = state->port.context;
    if (o->opening_store) return qs_local_admit_locked(QS_ADMIT_RECOVERY, bytes);
    /* APPEND already holds the shared budget lock and an admission covering
     * both possible segment metadata and this record. No competing producer
     * can consume that budget before the write finishes. */
    return bytes <= ZJ_META_BYTES + ZJ_RECORD_MAX;
}
static zj_result_t recover(owner_t *o)
{
    o->writer_allowed = o->compatibility_checked = false;
    o->compatibility = ZJ_COMPAT_NOT_READY;
    o->nvs_error = 0;
    o->store.last_errno = 0;
    o->store.last_operation = "journal_recovery";
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now < o->retry_at_us) return ZJ_IO;
    if (!qs_local_read_begin()) {
        o->store.last_errno = errno;
        o->store.last_operation = "storage_recovery_lock";
        return ZJ_IO;
    }
    zj_state_port_t state_port = {state_read, state_write, state_random, journal_absent, o};
    zj_result_t result = zj_state_open(&o->state, state_port, o->metadata.terminal_serial);
    if (result == ZJ_OK && !zj_state_identity(&o->state, o->key.master, o->metadata.capture_epoch))
        result = ZJ_CORRUPT;
    if (result == ZJ_OK) {
        zj_store_port_t port = {zj_state_checkpoint_load, zj_state_checkpoint_commit,
            zj_state_reserve, admitted, &o->state, zj_crypto_port(&o->key)};
        o->opening_store = true;
        result = zj_store_open(&o->store, o->prefix, &o->metadata, o->state.limit, port);
        o->opening_store = false;
    }
    qs_local_end(true, 0);
    o->retry_at_us = result == ZJ_OK ? 0 : now + 5000000;
    if (result != ZJ_OK) {
        o->store.ready = false;
        mbedtls_platform_zeroize(&o->key, sizeof(o->key));
    }
    return result;
}
static void execute(owner_t *o, const zj_request_t *request, zj_reply_t *reply)
{
    memset(reply, 0, sizeof(*reply));
    reply->compatibility = ZJ_COMPAT_NOT_READY;
    o->nvs_error = 0;
    o->store.last_errno = 0;
    o->store.last_operation = NULL;
    if (request->operation == ZJ_RUNTIME_CHECKPOINT || request->operation == ZJ_LEASE) {
        o->store.last_operation = request->operation == ZJ_LEASE ? "lease_checkpoint" : "runtime_checkpoint";
        if (!qs_local_read_begin()) {
            o->store.last_errno = errno;
            reply->result = ZJ_IO;
            return;
        }
        /* NVS lease/cursor preservation must still work if journal recovery
         * is held. This operation shares the owner and local lock, but does
         * not require filesystem capacity or an enabled journal writer. */
        if (request->operation == ZJ_LEASE)
            reply->result = zl_lease_commit(&request->input.lease.state,
                request->input.lease.deadline_us, &reply->lease, &o->nvs_error);
        else
            reply->result = zj_runtime_checkpoint_commit(&request->input.runtime_checkpoint.state,
                request->input.runtime_checkpoint.deadline_us, &reply->runtime_checkpoint, &o->nvs_error);
        qs_local_end(true, 0); /* Report NVS separately from attendance file loss. */
        return;
    }
    if (!o->store.ready || !o->state.ready) {
        reply->result = recover(o);
        if (reply->result != ZJ_OK) return;
    }
    bool writing = request->operation == ZJ_APPEND;
    if (writing && !o->writer_allowed) {
        o->store.last_errno = EPERM;
        o->store.last_operation = "journal_writer_gate";
        reply->result = ZJ_INVALID;
        reply->compatibility = o->compatibility;
        return;
    }
    bool locked = writing ? qs_local_begin(QS_ADMIT_LIVE, ZJ_META_BYTES + ZJ_RECORD_MAX) : qs_local_read_begin();
    if (!locked) {
        o->store.last_errno = errno;
        o->store.last_operation = errno == EBUSY ? "storage_lock" : "storage_admission";
        reply->result = errno == ENOSPC ? ZJ_FULL : ZJ_IO;
        return;
    }
    switch (request->operation) {
        case ZJ_SOURCE_BOUNDARY: {
            uint64_t now = (uint64_t)esp_timer_get_time();
            uint64_t deadline = request->input.source_boundary.deadline_us;
            o->store.last_operation = "source_boundary";
            if (deadline <= now || deadline - now > 5000000ULL) { reply->result = ZJ_STALE; break; }
            if (!o->writer_allowed || zj_state_authority(&o->state) != ZJ_AUTHORITY_ADD ||
                o->store.checkpoint_recovery_pending) { reply->result = ZJ_INVALID; break; }
            qs_health_t health = qs_local_health_locked();
            if (!health.observed || !health.available || !health.recovery_complete ||
                !health.persistence_verified || health.last_error || health.persistence_probe_error) {
                reply->result = ZJ_IO; break;
            }
            zj_reader_identity_t identity;
            reply->compatibility = zj_reader_platform_writer_identity(o->metadata.terminal_serial,
                o->metadata.capture_epoch, &identity);
            if (reply->compatibility != ZJ_COMPAT_OK) { reply->result = ZJ_INVALID; break; }
            if ((uint64_t)esp_timer_get_time() >= deadline) { reply->result = ZJ_STALE; break; }
            reply->result = zsb_open(o->state.port, &identity,
                request->input.source_boundary.create ? &request->input.source_boundary.facts : NULL,
                &reply->source_boundary);
            break;
        }
        case ZJ_APPEND:
            reply->result = zj_store_append(&o->store, &request->input.observation, &reply->capture_sequence);
            break;
        case ZJ_SETTLE: {
            zj_custody_expected_t actual;
            reply->result = zj_store_peek(&o->store, &reply->item);
            if (reply->result != ZJ_OK) break;
            if (!zj_custody_encode(&reply->item, o->store.port.crypto, o->wire_scratch,
                                   sizeof(o->wire_scratch), &actual)) reply->result = ZJ_INVALID;
            else if (memcmp(actual.observation_id, request->input.settlement.observation_id, sizeof(actual.observation_id)) ||
                     memcmp(actual.payload_digest, request->input.settlement.payload_digest, sizeof(actual.payload_digest)))
                reply->result = ZJ_STALE;
            else reply->result = zj_store_settle(&o->store, &request->input.settlement.token,
                                                 request->input.settlement.receipt_digest);
            mbedtls_platform_zeroize(o->wire_scratch, sizeof(o->wire_scratch));
            break;
        }
        case ZJ_PEEK: reply->result = zj_store_peek(&o->store, &reply->item); break;
        case ZJ_RECLAIM: reply->result = zj_store_reclaim_step(&o->store); break;
        case ZJ_READER_CHECK:
        case ZJ_OTA_CHECK:
        case ZJ_SELECT_READER: {
            qs_health_t health = qs_local_health_locked();
            zj_transport_health_t transport;
            bool delivery_ready = zj_transport_health(&transport) && transport.started &&
                (uint32_t)((uint32_t)(esp_timer_get_time() / 1000) - transport.sampled_ms) < 45000U;
            bool persistence = health.observed && health.available && health.recovery_complete && health.persistence_verified &&
                !health.last_error && !health.persistence_probe_error;
            if (request->operation == ZJ_SELECT_READER) {
                reply->compatibility = (request->input.reader_selection.failed_boot
                    ? zj_reader_platform_failed_boot : zj_reader_platform_select)(o->metadata.terminal_serial,
                    o->metadata.capture_epoch, o->store.ready && o->state.ready, delivery_ready,
                    persistence, o->store.checkpoint_recovery_pending,
                    request->input.reader_selection.image_digest,
                    request->input.reader_selection.deadline_us);
                if (reply->compatibility == ZJ_COMPAT_OK ||
                    reply->compatibility == ZJ_COMPAT_SELECTION_UNCERTAIN) {
                    /* Boot selection may make the bridge NEW. A previous
                     * cached writer permission cannot outlive that change. */
                    o->writer_allowed = false;
                    o->compatibility_checked = false;
                }
            } else if (request->operation == ZJ_OTA_CHECK) {
                /* A rejected install cannot revoke a valid reader/writer or
                 * alter proof. This operation holds local storage only. */
                reply->compatibility = zj_reader_platform_update(o->metadata.terminal_serial,
                    o->metadata.capture_epoch, o->store.ready && o->state.ready, delivery_ready,
                    persistence, o->store.checkpoint_recovery_pending,
                    request->input.ota.address, request->input.ota.size, request->input.ota.version);
            } else {
                bool writer_image = false;
                o->compatibility = zj_reader_platform_check(o->metadata.terminal_serial,
                    o->metadata.capture_epoch, o->store.ready && o->state.ready, delivery_ready,
                    persistence, o->store.checkpoint_recovery_pending, &writer_image);
                o->compatibility_checked = true;
                o->writer_allowed = false;
                if (o->compatibility == ZJ_COMPAT_OK && writer_image &&
                    zj_state_enable_add(&o->state) != ZJ_OK) {
                    /* Commit may have reached NVS. Recover the root before
                     * trusting authority or retrying; never fall back. */
                    o->compatibility = ZJ_COMPAT_UNCERTAIN;
                    o->compatibility_checked = false;
                    o->store.last_operation = "delivery_authority_commit";
                }
                /* OK without writer_image is the exact validated bridge.
                 * It may keep capturing only after an earlier writer's
                 * persisted, irreversible transfer to ADD. */
                o->writer_allowed = o->compatibility == ZJ_COMPAT_OK &&
                    zj_state_authority(&o->state) == ZJ_AUTHORITY_ADD;
                reply->compatibility = o->compatibility;
            }
            reply->result = reply->compatibility == ZJ_COMPAT_OK ? ZJ_OK : ZJ_INVALID;
            break;
        }
        default: reply->result = ZJ_INVALID; break;
    }
    bool write_failed = writing && (reply->result == ZJ_IO || reply->result == ZJ_UNCERTAIN);
    qs_local_end(!write_failed, o->store.last_errno);
}
static void execute_reader_shutdown(owner_t *o, zj_request_t *request, zj_reply_t *reply)
{
    memset(reply, 0, sizeof(*reply));
    reply->compatibility = ZJ_COMPAT_NOT_READY;
    o->store.last_operation = "rollback_intent";
    o->store.last_errno = o->nvs_error = 0;
    const esp_app_desc_t *app = esp_app_get_description();
    if (!app || strcmp(app->project_name, "zone_lite") || strcmp(app->version, ZJ_WRITER_VERSION) ||
        !o->store.ready || !o->state.ready || o->store.checkpoint_recovery_pending) {
        reply->result = ZJ_INVALID;
        return;
    }
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - 5000000U) { reply->result = ZJ_STALE; return; }
    request->input.reader_selection.deadline_us = now + 5000000U;
    if (!qs_local_read_begin()) {
        o->store.last_errno = errno;
        reply->result = ZJ_IO;
        return;
    }
    reply->result = zj_rollback_commit_intent(&o->reader_expected,
        request->input.reader_selection.deadline_us, &o->reader_committed, &o->nvs_error);
    qs_local_end(true, 0);
    if (reply->result != ZJ_OK) return;
    request->input.reader_selection.failed_boot = zj_rollback_failed_boot(&o->reader_committed);
    const char *hex = o->reader_committed.journal.image_sha256;
    for (unsigned i = 0; i < 32; ++i) {
        unsigned a = hex[i * 2] <= '9' ? (unsigned)(hex[i * 2] - '0') : (unsigned)(hex[i * 2] - 'a' + 10);
        unsigned b = hex[i * 2 + 1] <= '9' ? (unsigned)(hex[i * 2 + 1] - '0') : (unsigned)(hex[i * 2 + 1] - 'a' + 10);
        request->input.reader_selection.image_digest[i] = (uint8_t)((a << 4) | b);
    }
    /* All regular work was drained before this control was admitted. No
     * producer can race between the committed intent and checked selection. */
    execute(o, request, reply);
    o->store.last_operation = "rollback_selection";
}

static void task(void *context)
{
    owner_t *o = context;
    zj_request_t request;
    zj_reply_t reply;
    /* Static creation can schedule this higher-priority task before returning
     * its handle. Routing must recognize the owner before it touches storage. */
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    for (;;) {
        uint64_t ticket = 0;
        while (!enter()) vTaskDelay(pdMS_TO_TICKS(1));
        o->health.sampled_uptime_us = (uint64_t)esp_timer_get_time();
        bool work = zj_mailbox_begin(&o->mailbox, &request, &ticket);
        bool reader_control = !work && o->reader_ticket && !o->reader_completed;
        if (reader_control) {
            memset(&request, 0, sizeof(request));
            request.operation = ZJ_SELECT_READER;
            ticket = o->reader_ticket;
            work = true;
        }
        bool repair = !o->health.quiescing && !work && (!o->store.ready || !o->state.ready) &&
            o->health.sampled_uptime_us >= o->retry_at_us;
        if (o->health.quiescing && !work) {
            /* This task is the sole executor. Reaching this boundary means
             * every admitted request has returned and released its resources.
             * DONE replies can remain without representing unfinished I/O. */
            o->health.quiesced = true;
            o->writer_allowed = o->health.writer_allowed = false;
            o->compatibility_checked = o->health.compatibility_checked = false;
        }
        if (work || repair) {
            o->health.operation_running = true;
            o->health.recovering = repair || !o->store.ready || !o->state.ready;
            if (work) o->health.operation = request.operation;
            o->health.operation_started_us = o->health.sampled_uptime_us;
            o->health.inventory_known = false;
            if (repair || (work && request.operation == ZJ_APPEND)) o->health.verified_empty = false;
            o->health.legacy_verified_empty = false;
            if (o->health.recovering) zq_inventory_invalidate(&o->legacy_inventory);
            else if (request.operation == ZJ_SEGMENTED_QUEUE)
                zq_inventory_begin(&o->legacy_inventory, &request.input.segmented);
        }
        xSemaphoreGive(mailbox_lock);
        if (!work && !repair) { ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(200)); continue; }
        bool pending = false;
        if (reader_control) execute_reader_shutdown(o, &request, &reply);
        else if (work && (request.operation == ZJ_CATALOG || request.operation == ZJ_COMMANDS))
            pending = execute_catalog(o, ticket, &request, &reply);
        else if (work && request.operation == ZJ_COMMAND_IDS) pending = execute_command_ids(o, ticket, &request, &reply);
        else if (work && request.operation == ZJ_SEGMENTED_QUEUE) {
            memset(&reply, 0, sizeof(reply));
            /* Queue operations take their existing short local locks. No
             * owner/mailbox lock surrounds them, and public routing detects
             * this task instead of recursively queuing another request. */
            zq_store_execute(&o->segmented, (uint64_t)esp_timer_get_time(),
                &request.input.segmented, &reply.segmented);
            reply.result = ZJ_OK; /* Copied response; queue result is separate. */
        }
        else if (work) execute(o, &request, &reply);
        else {
            memset(&reply, 0, sizeof(reply));
            reply.result = recover(o);
        }
        uint64_t finished = (uint64_t)esp_timer_get_time();
        while (!enter()) vTaskDelay(pdMS_TO_TICKS(1));
        if (work) {
            if (reader_control) {
                o->reader_result = reply.result;
                o->reader_compatibility = reply.compatibility;
                o->reader_completed = true;
                o->health.quiesced = true;
                ++o->mailbox.completed;
            } else if (pending) (void)zj_mailbox_yield(&o->mailbox, ticket);
            else (void)zj_mailbox_finish(&o->mailbox, ticket, &reply);
        }
        o->health.progress_uptime_us = finished;
        o->health.sampled_uptime_us = finished;
        uint64_t elapsed = finished - o->health.operation_started_us;
        if (elapsed > o->health.max_operation_us) o->health.max_operation_us = elapsed;
        o->health.operation_running = false;
        o->health.recovering = false;
        o->health.ready = o->store.ready && o->state.ready;
        o->health.checkpoint_recovery_pending = o->store.checkpoint_recovery_pending;
        o->health.compatibility_checked = o->compatibility_checked;
        o->health.writer_allowed = o->writer_allowed;
        o->health.delivery_authority = zj_state_authority(&o->state);
        o->health.compatibility = o->compatibility;
        o->health.last_result = reply.result;
        if (o->io_revision < UINT64_MAX) ++o->io_revision;
        const char *failed = o->store.last_operation;
        bool transient_lock = o->store.last_errno == EBUSY && !o->nvs_error && failed &&
            (!strcmp(failed, "storage_lock") || !strcmp(failed, "storage_recovery_lock") ||
             !strcmp(failed, "catalog_lock") || !strcmp(failed, "command_id_lock"));
        bool persistence_fault = reply.result == ZJ_CORRUPT || reply.result == ZJ_UNCERTAIN ||
            (reply.result == ZJ_IO && !transient_lock);
        bool preservation_refused = work && request.operation == ZJ_APPEND && reply.result == ZJ_FULL;
        bool legacy_fault = work && request.operation == ZJ_SEGMENTED_QUEUE &&
            (reply.segmented.result == DQ_IO || reply.segmented.result == DQ_CORRUPT ||
             (request.input.segmented.operation <= ZQ_APPEND_COMMIT && reply.segmented.result == DQ_FULL));
        /* Successful reads/checkpoints and optional catalog capacity refusals
         * cannot erase a preservation incident from this test's boot. Normal
         * recovery/capture still proceed; only experimental reboot is vetoed. */
        if (persistence_fault || preservation_refused || legacy_fault)
            o->health.hil_reboot_persistence_incident = true;
        if (work && request.operation == ZJ_SEGMENTED_QUEUE)
            zq_inventory_complete(&o->legacy_inventory, &request.input.segmented, &reply.segmented);
        if (!o->health.ready || o->health.checkpoint_recovery_pending)
            zq_inventory_invalidate(&o->legacy_inventory);
        o->health.legacy_append_pending = o->segmented.append_transfer != 0;
        o->health.legacy_read_pending = o->segmented.read_transfer != 0;
        o->health.legacy_empty_mask = o->legacy_inventory.empty_mask;
        o->health.legacy_required_mask = ZQ_INVENTORY_REQUIRED;
        o->health.legacy_inventory_generation = o->legacy_inventory.generation;
        o->health.legacy_verified_empty = o->health.ready && !o->health.checkpoint_recovery_pending &&
            !o->health.legacy_append_pending && zq_inventory_empty(&o->legacy_inventory);
        o->health.inventory_known = o->health.ready;
        o->health.journal_segments = o->store.count;
        o->health.journal_bytes = 0;
        for (unsigned i = 0; i < o->store.count; ++i) o->health.journal_bytes += o->store.segments[i].size;
        if (!o->health.ready || o->health.checkpoint_recovery_pending) o->health.verified_empty = false;
        else if (work && request.operation == ZJ_PEEK) o->health.verified_empty = reply.result == ZJ_EMPTY;
        if (work && request.operation == ZJ_APPEND) {
            o->health.append_observed = true;
            o->health.last_append_result = reply.result;
            o->health.last_append_uptime_us = finished;
        }
        if (work && request.operation == ZJ_SOURCE_BOUNDARY) {
            o->health.source_boundary_observed = true;
            o->health.source_boundary_result = reply.result;
            o->health.source_boundary = reply.source_boundary;
        }
        if (!pending && reply.result != ZJ_OK && reply.result != ZJ_EMPTY && reply.result != ZJ_STALE) {
            ++o->health.failures;
            o->health.filesystem_error = o->store.last_errno;
            o->health.nvs_error = o->nvs_error;
            o->health.failed_operation = o->store.last_operation;
        }
        xSemaphoreGive(mailbox_lock);
        mbedtls_platform_zeroize(&request, sizeof(request));
        mbedtls_platform_zeroize(&reply, sizeof(reply));
        if (pending) vTaskDelay(pdMS_TO_TICKS(1));
    }
}

bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata)
{
    if (owner || !prefix || !metadata || !strrchr(prefix, '/') || strlen(prefix) >= sizeof(owner->prefix)) return false;
    uint8_t validated[ZJ_META_BYTES];
    if (!zj_metadata_encode(metadata, validated)) return false;
    mailbox_lock = xSemaphoreCreateMutex();
    if (!mailbox_lock) return false;
    owner = heap_caps_calloc(1, sizeof(*owner), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!owner) {
        vSemaphoreDelete(mailbox_lock);
        mailbox_lock = NULL;
        return false;
    }
    strcpy(owner->prefix, prefix);
    owner->metadata = *metadata;
    owner->segmented.legacy = (zq_legacy_port_t){
        add_legacy_owner_append, add_legacy_owner_peek, add_legacy_owner_settle};
    owner->segmented.quarantine = (zq_legacy_port_t){
        NULL, zkt_quarantine_owner_peek, zkt_quarantine_owner_settle};
    owner->segmented.attendance = (zq_legacy_port_t){
        zol_owner_append, zol_owner_peek, zol_owner_settle};
    ft_port_t catalog_port = {catalog_load, catalog_commit, owner};
    ft_port_t command_port = {commands_load, commands_commit, owner};
    if (!zc_store_init(&owner->catalog, ZC_ACTIVE_PATH, ZC_COMMIT_PATH, ZC_BACKUP_PATH,
                      ZC_TEMP_PATH, ZC_STAGE_PATH, catalog_port) ||
        !zc_store_init(&owner->commands, ZC_COMMAND_ACTIVE_PATH, ZC_COMMAND_COMMIT_PATH,
                      ZC_COMMAND_BACKUP_PATH, ZC_COMMAND_TEMP_PATH, ZC_COMMAND_STAGE_PATH, command_port) ||
        !zi_store_init(&owner->command_ids, ZI_PROCESSED_PATH, ZI_CANCELLED_PATH, command_id_admit, owner)) {
        heap_caps_free(owner);
        owner = NULL;
        vSemaphoreDelete(mailbox_lock);
        mailbox_lock = NULL;
        return false;
    }
    owner->commands.limit = ZC_COMMAND_LIMIT_BYTES;
    /* Legacy command .tmp files can contain an incomplete filtered inbox.
     * Recovery must never promote one without its committed replacement intent. */
    owner->commands.allow_first_recovery = false;
    zj_mailbox_init(&owner->mailbox);
    owner->health.started = true;
    owner->compatibility = owner->health.compatibility = ZJ_COMPAT_NOT_READY;
    owner_task = xTaskCreateStatic(task, "zkt_storage", ZJ_OWNER_STACK_BYTES,
        owner, 5, owner_stack, &owner_control);
    if (!owner_task) {
        heap_caps_free(owner);
        owner = NULL;
        vSemaphoreDelete(mailbox_lock);
        mailbox_lock = NULL;
        return false;
    }
    xTaskNotifyGive(owner_task);
    return true;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    if (ticket) *ticket = 0;
    /* Boot selection is a shutdown control, never ordinary competing work. */
    if (!request || request->operation == ZJ_SELECT_READER) return false;
    if (!owner || !enter()) return false;
    bool ok = !owner->health.quiescing && zj_mailbox_submit(&owner->mailbox, request, ticket);
    if (ok && request->operation == ZJ_APPEND) owner->health.verified_empty = false;
    if (ok && request->operation == ZJ_SEGMENTED_QUEUE) {
        zq_inventory_admitted(&owner->legacy_inventory, &request->input.segmented);
        owner->health.legacy_verified_empty = false;
    }
    if (owner->health.quiescing) ++owner->mailbox.refused;
    xSemaphoreGive(mailbox_lock);
    if (ok) xTaskNotifyGive(owner_task);
    return ok;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    if (complete) *complete = false;
    if (!owner || !ticket || !reply || !complete || !enter()) return false;
    if (ticket == owner->reader_ticket) {
        if (owner->reader_completed) {
            memset(reply, 0, sizeof(*reply));
            reply->result = owner->reader_result;
            reply->compatibility = owner->reader_compatibility;
            reply->rollback_intent = owner->reader_committed;
            *complete = true;
            owner->reader_ticket = 0;
        }
        xSemaphoreGive(mailbox_lock);
        return true;
    }
    bool ok = zj_mailbox_poll(&owner->mailbox, ticket, reply, complete);
    xSemaphoreGive(mailbox_lock);
    return ok;
}
bool zj_owner_abandon(uint64_t ticket)
{
    if (!owner || !enter()) return false;
    bool ok = ticket != owner->reader_ticket && zj_mailbox_abandon(&owner->mailbox, ticket);
    xSemaphoreGive(mailbox_lock);
    return ok;
}
bool zj_owner_quiesce(void)
{
    if (!owner || !enter()) return false;
    owner->health.quiescing = true;
    bool complete = owner->health.quiesced;
    xSemaphoreGive(mailbox_lock);
    xTaskNotifyGive(owner_task);
    return complete;
}
bool zj_owner_try_quiesce_before(uint64_t deadline_us, int64_t expires_epoch,
                                 int64_t *accepted_epoch, uint64_t *accepted_us)
{
    if (!owner || !deadline_us || !accepted_epoch || !accepted_us || !enter()) return false;
    uint64_t revision = owner->io_revision;
    xSemaphoreGive(mailbox_lock);
    /* Never invert mailbox -> filesystem-lock ordering. The revision check
     * rejects a storage operation that completes across this health snapshot. */
    qs_health_t persistence = qs_health();
    if (!persistence.observed || !persistence.available || !persistence.recovery_complete ||
        !persistence.persistence_verified || persistence.persistence_recheck_required ||
        persistence.last_error || persistence.persistence_probe_error || persistence.legacy.error || !enter()) return false;
    bool idle = revision != UINT64_MAX && revision == owner->io_revision &&
        !owner->health.hil_reboot_persistence_incident &&
        !owner->health.quiescing && !owner->health.operation_running &&
        !owner->health.recovering && owner->store.ready && owner->state.ready &&
        !owner->store.checkpoint_recovery_pending && owner->writer_allowed &&
        owner->compatibility_checked && zj_state_authority(&owner->state) == ZJ_AUTHORITY_ADD &&
        !owner->mailbox.running_ticket && !owner->mailbox.resume_ticket && !owner->reader_ticket &&
        !owner->catalog.work_ticket && !owner->commands.work_ticket && !owner->command_ids.work_ticket &&
        !owner->segmented.append_transfer && !owner->segmented.read_transfer;
    for (unsigned i = 0; idle && i < ZJ_REQUEST_SLOTS; ++i)
        if (owner->mailbox.slots[i].state == ZJ_SLOT_QUEUED ||
            owner->mailbox.slots[i].state == ZJ_SLOT_RUNNING) idle = false;
    /* Check after acquiring the lock and inspecting every accepted operation.
     * Submission and the owner recovery loop use this same lock. */
    int64_t epoch = (int64_t)time(NULL);
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (idle && epoch >= 1700000000 && epoch < expires_epoch && now < deadline_us) {
        owner->health.quiescing = owner->health.quiesced = true;
        owner->writer_allowed = owner->health.writer_allowed = false;
        owner->compatibility_checked = owner->health.compatibility_checked = false;
        *accepted_epoch = epoch;
        *accepted_us = now;
    } else idle = false;
    xSemaphoreGive(mailbox_lock);
    return idle;
}
bool zj_owner_select_quiesced_reader(const ota_checkpoint_t *expected, uint64_t *ticket)
{
    if (ticket) *ticket = 0;
    if (!owner || !ticket || !zj_rollback_request_valid(expected) || !enter()) return false;
    bool allowed = owner->health.quiesced && !owner->health.operation_running &&
        !owner->mailbox.running_ticket && !owner->reader_ticket && owner->mailbox.next_ticket &&
        (!owner->reader_bound || zj_rollback_same_target(&owner->reader_expected, expected));
    if (allowed) {
        owner->reader_expected = *expected;
        memset(&owner->reader_committed, 0, sizeof(owner->reader_committed));
        owner->reader_bound = true;
        owner->reader_completed = false;
        owner->reader_ticket = owner->mailbox.next_ticket++;
        owner->health.quiesced = false;
        *ticket = owner->reader_ticket;
    }
    xSemaphoreGive(mailbox_lock);
    if (allowed) xTaskNotifyGive(owner_task);
    return allowed;
}
bool zj_owner_health(zj_owner_health_t *health)
{
    if (!owner || !health || !enter()) return false;
    *health = owner->health;
    health->completed = owner->mailbox.completed;
    health->refused = owner->mailbox.refused;
    health->occupied = owner->mailbox.occupied;
    health->high_watermark = owner->mailbox.high_watermark;
    health->pending_appends = 0;
    health->legacy_empty_mask = owner->legacy_inventory.empty_mask;
    health->legacy_required_mask = ZQ_INVENTORY_REQUIRED;
    health->legacy_inventory_generation = owner->legacy_inventory.generation;
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        const zj_request_slot_t *slot = &owner->mailbox.slots[i];
        if ((slot->state == ZJ_SLOT_QUEUED || slot->state == ZJ_SLOT_RUNNING) &&
            slot->request.operation == ZJ_APPEND) ++health->pending_appends;
        if ((slot->state == ZJ_SLOT_QUEUED || slot->state == ZJ_SLOT_RUNNING) &&
            slot->request.operation == ZJ_SEGMENTED_QUEUE &&
            slot->request.input.segmented.operation <= ZQ_APPEND_COMMIT)
            health->legacy_append_pending = true;
    }
    /* A PEEK can finish after another producer admits an append. Check the
     * mailbox under the same lock; that snapshot must never advertise zero. */
    if (health->pending_appends) health->verified_empty = false;
    if (health->legacy_append_pending || health->operation_running || health->recovering ||
        !zq_inventory_empty(&owner->legacy_inventory)) health->legacy_verified_empty = false;
    xSemaphoreGive(mailbox_lock);
    return true;
}
