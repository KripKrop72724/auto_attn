#include "zkt_storage_owner_platform.h"
#include "zkt_storage_owner.h"
#include "zkt_journal_crypto.h"
#include "zkt_journal_state.h"
#include "zkt_custody_wire.h"
#include "zkt_journal_transport.h"
#include "zkt_reader_platform.h"
#include "zkt_runtime_checkpoint.h"
#include "zkt_legacy_inventory.h"
#include "queue_store.h"
#include "durable_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static atomic_bool stop, pause_write, write_waiting, full;
static atomic_bool refuse_compatibility, stale_transport;
static atomic_bool bridge_image, cutover_readback_failure;
static atomic_int cutover_failure;
static atomic_int persistence_failure;
static atomic_bool fail_next_sync;
#undef fsync
int zkt_owner_test_fsync(int descriptor)
{
    if (atomic_exchange(&fail_next_sync, false)) { errno = EIO; return -1; }
    return fsync(descriptor);
}
static pthread_t thread;
static atomic_bool start_waiting, owner_announced;
static pthread_mutex_t budget = PTHREAD_MUTEX_INITIALIZER;
static void (*task_function)(void *);
static void *task_argument;
static uint8_t root[ZJ_ROOT_BYTES], checkpoint[ZJ_CHECKPOINT_BYTES];
static size_t root_length, checkpoint_length;
static uint8_t source_boundary[ZSB_BYTES];
static bool source_boundary_present;
static unsigned source_boundary_writes;
static runtime_checkpoint_t runtime_blob;
static bool runtime_present;
static unsigned runtime_writes;
static zl_lease_record_t lease_blob;
static unsigned lease_writes;
static uint64_t lease_root;
static ft_checkpoint_t catalog_checkpoint, command_checkpoint;
static ota_checkpoint_t ota_blob, ota_pending;
static atomic_int ota_failure;
static atomic_bool ota_pause, ota_waiting;
static unsigned ota_writes, selection_calls;
const esp_app_desc_t *esp_app_get_description(void)
{
    static const esp_app_desc_t app = {.project_name = "zone_lite", .version = "2.7.0"};
    return &app;
}

int64_t esp_timer_get_time(void)
{
    struct timespec now;
    assert(!clock_gettime(CLOCK_MONOTONIC, &now));
    return (int64_t)now.tv_sec * 1000000 + now.tv_nsec / 1000;
}
void vTaskDelay(unsigned milliseconds)
{
    struct timespec duration = {milliseconds / 1000, (long)(milliseconds % 1000) * 1000000};
    nanosleep(&duration, NULL);
}
void esp_fill_random(void *bytes, size_t length) { memset(bytes, 0x71, length); }
void *heap_caps_calloc(size_t count, size_t size, unsigned caps) { (void)caps; return calloc(count, size); }
void heap_caps_free(void *memory) { free(memory); }
void mbedtls_platform_zeroize(void *memory, size_t length)
{
    volatile uint8_t *bytes = memory;
    while (length--) *bytes++ = 0;
}
SemaphoreHandle_t xSemaphoreCreateMutex(void)
{
    pthread_mutex_t *mutex = malloc(sizeof(*mutex));
    assert(mutex && !pthread_mutex_init(mutex, NULL));
    return mutex;
}
int xSemaphoreTake(SemaphoreHandle_t mutex, unsigned wait_ms)
{
    int64_t deadline = esp_timer_get_time() + (int64_t)wait_ms * 1000;
    do {
        if (!pthread_mutex_trylock(mutex)) return pdTRUE;
        vTaskDelay(1);
    } while (esp_timer_get_time() < deadline);
    return 0;
}
void xSemaphoreGive(SemaphoreHandle_t mutex) { assert(!pthread_mutex_unlock(mutex)); }
void vSemaphoreDelete(SemaphoreHandle_t mutex) { assert(!pthread_mutex_destroy(mutex)); free(mutex); }
static void *worker(void *argument) { (void)argument; task_function(task_argument); return NULL; }
int xTaskCreate(void (*function)(void *), const char *name, unsigned stack,
                void *argument, unsigned priority, TaskHandle_t *handle)
{
    (void)name; (void)stack; (void)priority;
    /* Replay the observed internal heap shape for the other required worker. */
    if (stack > 11776U) return 0;
    task_function = function;
    task_argument = argument;
    *handle = &thread;
    assert(!pthread_create(&thread, NULL, worker, NULL));
    return pdPASS;
}
TaskHandle_t xTaskCreateStatic(void (*function)(void *), const char *name, unsigned stack,
                              void *argument, unsigned priority, StackType_t *buffer, StaticTask_t *control)
{
    assert(!strcmp(name, "zkt_storage") && stack == 12288U && priority == 5);
    assert(buffer && control && !control->used);
    memset(buffer, 0xa5, stack); /* ASan verifies the complete caller-owned allocation. */
    control->used++;
    task_function = function;
    task_argument = argument;
    assert(!pthread_create(&thread, NULL, worker, NULL));
    /* A higher-priority FreeRTOS task can run before creation returns. */
    for (unsigned i = 0; i < 2000 && !atomic_load(&start_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&start_waiting));
    return &thread;
}
unsigned ulTaskNotifyTake(int clear, unsigned wait_ms)
{
    (void)clear; (void)wait_ms;
    if (wait_ms == portMAX_DELAY) {
        atomic_store(&start_waiting, true);
        while (!atomic_load(&owner_announced)) vTaskDelay(1);
        return 1;
    }
    if (atomic_load(&stop)) pthread_exit(NULL);
    vTaskDelay(1);
    return 1;
}
void xTaskNotifyGive(TaskHandle_t handle) { assert(handle == &thread); atomic_store(&owner_announced, true); }
TaskHandle_t xTaskGetCurrentTaskHandle(void)
{ return pthread_equal(pthread_self(), thread) ? &thread : NULL; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle)
{
    assert(atomic_load(&owner_announced) && zj_owner_is_current_task());
    (void)mode;
    assert(!strcmp(name, "zkt_journal") || !strcmp(name, "zone_lite") || !strcmp(name, "file_tx") || !strcmp(name, "zone_ota"));
    if (!strcmp(name, "zone_ota")) {
        assert(pthread_equal(pthread_self(), thread));
        assert(pthread_mutex_trylock(&budget) == EBUSY);
        *handle = 4; return ESP_OK;
    }
    if (!strcmp(name, "file_tx")) {
        assert(pthread_equal(pthread_self(), thread));
        assert(pthread_mutex_trylock(&budget) == EBUSY);
        *handle = 3; return ESP_OK;
    }
    if (!strcmp(name, "zone_lite")) {
        assert(pthread_equal(pthread_self(), thread));
        assert(pthread_mutex_trylock(&budget) == EBUSY);
        *handle = 2;
    } else *handle = 1;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(handle >= 1 && handle <= 4); }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *name, void *out, size_t *length)
{
    if (handle == 4) {
        assert(!strcmp(name, "journal_v1") && *length == sizeof(ota_blob));
        if (!ota_blob.version) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, &ota_blob, sizeof(ota_blob)); return ESP_OK;
    }
    if (handle == 3) {
        assert((!strcmp(name, "catalog") || !strcmp(name, "commands")) && *length == sizeof(catalog_checkpoint));
        ft_checkpoint_t *checkpoint = !strcmp(name, "catalog") ? &catalog_checkpoint : &command_checkpoint;
        if (!checkpoint->version) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, checkpoint, sizeof(*checkpoint)); return ESP_OK;
    }
    if (handle == 2) {
        if (!strcmp(name, "lease_v2_root")) {
            assert(*length == sizeof(lease_root));
            if (!lease_root) return ESP_ERR_NVS_NOT_FOUND;
            memcpy(out, &lease_root, sizeof(lease_root)); return ESP_OK;
        }
        if (!strcmp(name, "lease_v2")) {
            assert(*length == sizeof(lease_blob));
            if (!lease_blob.version) return ESP_ERR_NVS_NOT_FOUND;
            memcpy(out, &lease_blob, sizeof(lease_blob));
            return ESP_OK;
        }
        assert(!strcmp(name, "runtime_v1") && *length == sizeof(runtime_blob));
        if (!runtime_present) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, &runtime_blob, sizeof(runtime_blob));
        return ESP_OK;
    }
    assert(handle == 1);
    if (!strcmp(name, "source_v1")) {
        assert(*length == ZSB_BYTES && pthread_equal(pthread_self(), thread));
        if (!source_boundary_present) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, source_boundary, ZSB_BYTES); return ESP_OK;
    }
    if (!strcmp(name, "reader_v1")) return ESP_ERR_NVS_NOT_FOUND;
    bool is_root = !strcmp(name, "root");
    if (is_root && atomic_exchange(&cutover_readback_failure, false)) return -7;
    size_t size = is_root ? root_length : checkpoint_length;
    if (!size) return ESP_ERR_NVS_NOT_FOUND;
    if (size > *length) return ESP_ERR_INVALID_SIZE;
    *length = size;
    memcpy(out, is_root ? root : checkpoint, size);
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *name, const void *bytes, size_t length)
{
    if (handle == 4) {
        assert(!strcmp(name, "journal_v1") && length == sizeof(ota_blob));
        ++ota_writes;
        ota_pending = *(const ota_checkpoint_t *)bytes;
        return ESP_OK;
    }
    if (handle == 3) {
        assert((!strcmp(name, "catalog") || !strcmp(name, "commands")) && length == sizeof(catalog_checkpoint));
        memcpy(!strcmp(name, "catalog") ? &catalog_checkpoint : &command_checkpoint, bytes, length); return ESP_OK;
    }
    if (handle == 2) {
        if (!strcmp(name, "lease_v2_root")) {
            assert(length == sizeof(lease_root));
            lease_root = *(const uint64_t *)bytes; return ESP_OK;
        }
        if (!strcmp(name, "lease_v2")) {
            assert(length == sizeof(lease_blob));
            lease_blob = *(const zl_lease_record_t *)bytes;
            ++lease_writes;
            return ESP_OK;
        }
        assert(!strcmp(name, "runtime_v1") && length == sizeof(runtime_blob));
        runtime_blob = *(const runtime_checkpoint_t *)bytes;
        runtime_present = true;
        ++runtime_writes;
        return ESP_OK;
    }
    assert(handle == 1);
    if (!strcmp(name, "source_v1")) {
        assert(length == ZSB_BYTES && pthread_equal(pthread_self(), thread));
        assert(pthread_mutex_trylock(&budget) == EBUSY);
        memcpy(source_boundary, bytes, length); source_boundary_present=true;
        ++source_boundary_writes; return ESP_OK;
    }
    if (!strcmp(name, "root")) {
        assert(length == sizeof(root));
        int failure = ((const uint8_t *)bytes)[145] == 1 && root[145] == 0
            ? atomic_exchange(&cutover_failure, 0) : 0;
        if (failure == 1) return -7;
        memcpy(root, bytes, length);
        root_length = length;
        if (failure == 2) return -7;
        if (failure == 3) atomic_store(&cutover_readback_failure, true);
    } else {
        assert(!strcmp(name, "retirement") && length == sizeof(checkpoint));
        memcpy(checkpoint, bytes, length);
        checkpoint_length = length;
    }
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle)
{
    assert(handle >= 1 && handle <= 4);
    if (handle == 4) {
        while (atomic_load(&ota_pause)) { atomic_store(&ota_waiting, true); vTaskDelay(1); }
        int failure = atomic_exchange(&ota_failure, 0);
        if (failure != 1) ota_blob = ota_pending;
        return failure ? -7 : ESP_OK;
    }
    return ESP_OK;
}
bool qs_local_begin(qs_admission_t policy, size_t bytes)
{
    assert((policy == QS_ADMIT_LIVE && bytes >= ZJ_RECORD_MAX) ||
        (policy == QS_ADMIT_OPTIONAL_HISTORICAL && bytes <= ZC_CHUNK_BYTES + 512U));
    if (atomic_load(&full)) { errno = ENOSPC; return false; }
    assert(!pthread_mutex_lock(&budget));
    while (atomic_load(&pause_write)) {
        atomic_store(&write_waiting, true);
        vTaskDelay(1);
    }
    return true;
}
bool qs_local_read_begin(void) { assert(!pthread_mutex_lock(&budget)); return true; }
bool qs_local_admit_locked(qs_admission_t policy, size_t bytes)
{
    assert((policy == QS_ADMIT_RECOVERY && (bytes == 8U + ZJ_CHECKPOINT_BYTES || bytes <= ZC_CHUNK_BYTES + 512U)) ||
        (policy == QS_ADMIT_OPTIONAL_HISTORICAL && bytes <= ZC_CHUNK_BYTES + 512U));
    if (atomic_load(&full)) { errno = ENOSPC; return false; }
    return true;
}
void qs_local_end(bool persisted, int error) { (void)persisted; (void)error; assert(!pthread_mutex_unlock(&budget)); }
qs_health_t qs_local_health_locked(void)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    int failure = atomic_load(&persistence_failure);
    return (qs_health_t){.observed = failure != 1, .available = failure != 2,
        .recovery_complete = failure != 3, .persistence_verified = failure != 4,
        .last_error = failure == 5 ? EIO : 0, .persistence_probe_error = failure == 6 ? EIO : 0};
}
qs_health_t qs_health(void)
{
    if (pthread_mutex_trylock(&budget)) return (qs_health_t){0};
    qs_health_t snapshot = qs_local_health_locked();
    assert(!pthread_mutex_unlock(&budget));
    return snapshot;
}
/* Existing queue format with real files/checkpoints. Every operation invoked
 * by the copied client must execute on the storage thread. */
static durable_queue_t retained[QS_COUNT];
static dq_checkpoint_t retained_checkpoints[QS_COUNT];
static int retained_load(void *context, dq_checkpoint_t *out)
{
    unsigned lane = (unsigned)(uintptr_t)context;
    if (!retained_checkpoints[lane].version) return 0;
    *out = retained_checkpoints[lane]; return 1;
}
static bool retained_commit(void *context, const dq_checkpoint_t *in)
{ retained_checkpoints[(unsigned)(uintptr_t)context] = *in; return true; }
static bool retained_admit(void *context, size_t bytes)
{ (void)context; (void)bytes; return !atomic_load(&full); }
static durable_queue_t *retained_queue(qs_lane_t lane)
{
    assert(zj_owner_is_current_task() && (unsigned)lane < QS_COUNT);
    assert(!pthread_mutex_trylock(&budget));
    assert(!pthread_mutex_unlock(&budget));
    if (!retained[lane].ready) {
        char prefix[32]; snprintf(prefix, sizeof(prefix), "retained-%u", (unsigned)lane);
        assert(dq_open(&retained[lane], prefix,
            (dq_port_t){retained_load, retained_commit, retained_admit, (void *)(uintptr_t)lane}) == DQ_OK);
    }
    return &retained[lane];
}
dq_result_t qs_append_with_policy(qs_lane_t lane, const void *data, size_t length, qs_admission_t policy)
{ assert((unsigned)policy <= QS_ADMIT_RECOVERY); return dq_append(retained_queue(lane), data, length); }
dq_result_t qs_peek(qs_lane_t lane, void *data, size_t capacity, size_t *length, dq_token_t *token)
{ return dq_peek(retained_queue(lane), data, capacity, length, token); }
dq_result_t qs_settle(qs_lane_t lane, const dq_token_t *token)
{ return dq_settle(retained_queue(lane), token); }
bool qs_snapshot(qs_lane_t lane, uint32_t *depth)
{ *depth = retained_queue(lane)->checkpoint.depth; return true; }
bool qs_generation(char output[33])
{ assert(zj_owner_is_current_task()); memset(output, 'a', 32); output[32] = 0; return true; }
bool qs_recover_step(void) { assert(zj_owner_is_current_task()); return true; }
bool qs_verify_persistence(void) { assert(zj_owner_is_current_task()); return true; }
static legacy_queue_t flat[7];
static lq_checkpoint_t flat_checkpoints[7];
static int flat_load(void *context, lq_checkpoint_t *out)
{ unsigned lane=(unsigned)(uintptr_t)context; *out=flat_checkpoints[lane]; return out->version ? 1 : 0; }
static bool flat_commit(void *context, const lq_checkpoint_t *in)
{ flat_checkpoints[(unsigned)(uintptr_t)context]=*in; return true; }
static legacy_queue_t *flat_queue(unsigned lane)
{
    assert(zj_owner_is_current_task() && lane < 7);
    if (!flat[lane].ready) {
        static const char *paths[]={"flat-live.jsonl","flat-bulk.jsonl","quarantine-ords","quarantine-add","quarantine-backup",
            "flat-ords","flat-blocked"};
        const char *path=paths[lane];
        assert(lq_open(&flat[lane],path,(lq_port_t){flat_load,flat_commit,(void *)(uintptr_t)lane})==DQ_OK);
    }
    return &flat[lane];
}
dq_result_t add_legacy_owner_append(unsigned lane,const void *bytes,size_t length,qs_admission_t policy)
{
    assert((unsigned)policy<=QS_ADMIT_RECOVERY);
    legacy_queue_t *q=flat_queue(lane);
    lq_invalidate_empty(q); /* Match the actual producer adapter's invalidation. */
    if(atomic_load(&full))return DQ_FULL;
    FILE *f=fopen(q->path,"ab");
    assert(f && fwrite(bytes,1,length,f)==length && fputc('\n',f)!=EOF && fclose(f)==0);
    return DQ_OK;
}
dq_result_t add_legacy_owner_peek(unsigned lane,void *bytes,size_t capacity,size_t *length,lq_token_t *token)
{
    dq_result_t result=lq_peek(flat_queue(lane),bytes,capacity,token);
    if(result==DQ_OK)*length=token->end-token->offset;
    return result;
}
dq_result_t add_legacy_owner_settle(unsigned lane,const lq_token_t *token,bool custody)
{ return custody ? lq_settle_evidence(flat_queue(lane),token) : lq_settle(flat_queue(lane),token); }
dq_result_t zkt_quarantine_owner_peek(unsigned lane,void *bytes,size_t capacity,size_t *length,lq_token_t *token)
{ assert(lane<3);return add_legacy_owner_peek(lane+2,bytes,capacity,length,token); }
dq_result_t zkt_quarantine_owner_settle(unsigned lane,const lq_token_t *token,bool custody)
{ assert(lane<3 && custody);return add_legacy_owner_settle(lane+2,token,true); }
dq_result_t zol_owner_append(unsigned lane,const void *bytes,size_t length,qs_admission_t policy)
{ assert(lane<2);return add_legacy_owner_append(lane+5,bytes,length,policy); }
dq_result_t zol_owner_peek(unsigned lane,void *bytes,size_t capacity,size_t *length,lq_token_t *token)
{ assert(lane<2);return add_legacy_owner_peek(lane+5,bytes,capacity,length,token); }
dq_result_t zol_owner_settle(unsigned lane,const lq_token_t *token,bool custody)
{ assert(lane<2);return add_legacy_owner_settle(lane+5,token,custody); }
bool zj_transport_health(zj_transport_health_t *health)
{
    *health = (zj_transport_health_t){.started = true,
        .sampled_ms = (uint32_t)(esp_timer_get_time() / 1000) - (atomic_load(&stale_transport) ? 50000U : 0)};
    return true;
}
zj_compat_result_t zj_reader_platform_writer_identity(const char *serial, const uint8_t epoch[16],
    zj_reader_identity_t *out)
{
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0]);
    assert(pthread_equal(pthread_self(), thread) && pthread_mutex_trylock(&budget) == EBUSY);
    if (atomic_load(&bridge_image)) return ZJ_COMPAT_VERSION;
    *out=(zj_reader_identity_t){.terminal_digest={2}, .image_digest={3}};
    memcpy(out->capture_epoch, epoch, 16); return ZJ_COMPAT_OK;
}
zj_compat_result_t zj_reader_platform_check_evidence(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, bool *writer_allowed,
    zj_reader_selection_t *selection)
{
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0]);
    assert(ready && persistence);
    /* The actual ESP/NVS/identity checks have their own platform harness.
     * This spy verifies owner locking, gating and recovery transitions. */
    bool compatible = delivery && !recovering && !atomic_load(&refuse_compatibility);
    *writer_allowed = compatible && !atomic_load(&bridge_image);
    if (selection) memset(selection, 0, sizeof(*selection));
    return compatible ? ZJ_COMPAT_OK : ZJ_COMPAT_NOT_READY;
}
zj_compat_result_t zj_reader_platform_check(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, bool *writer_allowed)
{
    return zj_reader_platform_check_evidence(serial, epoch, ready, delivery, persistence, recovering, writer_allowed, NULL);
}
zj_compat_result_t zj_reader_platform_update(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, uint32_t address, uint32_t size, const char *version)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0] && ready && persistence);
    assert(address == 0x2a0000 && size == 0x280000 && !strcmp(version, ZJ_WRITER_VERSION));
    return delivery && !recovering ? ZJ_COMPAT_PROTECTED_SLOT : ZJ_COMPAT_NOT_READY;
}
static bool uncertain_selection, failed_rollback_test;
zj_compat_result_t zj_reader_platform_select(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, const uint8_t expected[32], uint64_t deadline)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0] && ready && persistence && expected[0] == 17);
    assert(ota_checkpoint_valid(&ota_blob) && !strcmp(ota_blob.journal.state,
        failed_rollback_test ? "FAILED_BOOT_INTENT" : "READER_INTENT"));
    zj_owner_health_t health;
    assert(zj_owner_health(&health) && health.quiescing && !health.pending_appends);
    ++selection_calls;
    if (deadline <= (uint64_t)esp_timer_get_time()) return ZJ_COMPAT_SELECTION_EXPIRED;
    if (!delivery || recovering) return ZJ_COMPAT_NOT_READY;
    return uncertain_selection ? ZJ_COMPAT_SELECTION_UNCERTAIN : ZJ_COMPAT_OK;
}
zj_compat_result_t zj_reader_platform_failed_boot(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, const uint8_t expected[32], uint64_t deadline)
{
    assert(failed_rollback_test);
    return zj_reader_platform_select(serial, epoch, ready, delivery, persistence, recovering, expected, deadline);
}
/* The genuine mbedTLS adapter is independently tested. This port exercises
 * owner/thread/file/NVS interactions, not cryptographic authentication. */
static bool seal(void *context, const uint8_t *metadata, const uint8_t *nonce,
                 const uint8_t *aad, size_t aad_length, const uint8_t *plain,
                 size_t length, uint8_t *cipher, uint8_t *tag)
{
    (void)context; (void)metadata; (void)nonce; (void)aad; (void)aad_length;
    memcpy(cipher, plain, length);
    memset(tag, 1, ZJ_TAG_BYTES);
    return true;
}
static bool decode(void *context, const uint8_t *metadata, const uint8_t *nonce,
                   const uint8_t *aad, size_t aad_length, const uint8_t *cipher,
                   size_t length, const uint8_t *tag, uint8_t *plain)
{
    (void)tag;
    uint8_t unused[ZJ_TAG_BYTES];
    return seal(context, metadata, nonce, aad, aad_length, cipher, length, plain, unused);
}
static bool digest(void *context, const uint8_t *bytes, size_t length, uint8_t out[32])
{
    (void)context;
    uint32_t crc = dq_crc32(bytes, length);
    for (unsigned i = 0; i < 32; ++i) out[i] = (uint8_t)(crc >> ((i % 4) * 8));
    return true;
}
zj_crypto_port_t zj_crypto_port(zj_crypto_key_t *key)
{
    return (zj_crypto_port_t){seal, decode, digest, key};
}
static zj_reply_t wait_reply(uint64_t ticket)
{
    zj_reply_t reply;
    for (unsigned i = 0; i < 2000; ++i) {
        bool complete;
        assert(zj_owner_poll(ticket, &reply, &complete));
        if (complete) return reply;
        vTaskDelay(1);
    }
    assert(!"Storage owner failed to finish bounded host operation");
    return (zj_reply_t){0};
}
static void rollback_with_full_mailbox(void)
{
    ota_checkpoint_t expected = {.version = 1, .generation = 7};
    strcpy(expected.journal.deployment_id, "synthetic-operation");
    strcpy(expected.journal.release_id, "synthetic-reader");
    strcpy(expected.journal.target_version, ZJ_BRIDGE_VERSION);
    memset(expected.journal.image_sha256, '1', 64);
    strcpy(expected.journal.download_url, "https://example.invalid/unused");
    strcpy(expected.journal.state, "DOWNLOADING"); expected.journal.image_size = 131072;
    if (failed_rollback_test) {
        strcpy(expected.journal.target_version, ZJ_WRITER_VERSION);
        strcpy(expected.journal.state, "READY_TO_BOOT");
        expected.journal.bytes_written = expected.journal.image_size;
    }
    expected.crc = dq_crc32(&expected, offsetof(ota_checkpoint_t, crc));
    ota_blob = expected;
    uint64_t ticket, retained[ZJ_REQUEST_SLOTS];
    assert(!zj_owner_select_quiesced_reader(&expected, &ticket) && !ticket);
    zj_request_t proof = {.operation = ZJ_READER_CHECK};
    assert(zj_owner_submit(&proof, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_OK);
    zj_request_t capture = {.operation = ZJ_APPEND, .input.observation = {
        .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
        .source_ordinal = UINT32_MAX, .raw_length = 40, .raw = {'R'}}};
    atomic_store(&pause_write, true);
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) assert(zj_owner_submit(&capture, &retained[i]));
    for (unsigned i = 0; i < 2000 && !atomic_load(&write_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&write_waiting) && !zj_owner_quiesce());
    assert(!zj_owner_select_quiesced_reader(&expected, &ticket) && !ticket);
    atomic_store(&pause_write, false);
    for (unsigned i = 0; i < 2000 && !zj_owner_quiesce(); ++i) vTaskDelay(1);
    assert(zj_owner_quiesce());
    zj_owner_health_t health;
    assert(zj_owner_health(&health) && health.occupied == ZJ_REQUEST_SLOTS &&
        !health.pending_appends && health.completed == ZJ_REQUEST_SLOTS + 1 && !selection_calls);
    atomic_store(&ota_failure, 1); atomic_store(&ota_pause, true);
    assert(zj_owner_select_quiesced_reader(&expected, &ticket) && ticket);
    for (unsigned i = 0; i < 2000 && !atomic_load(&ota_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&ota_waiting) && !zj_owner_abandon(ticket));
    uint64_t refused;
    assert(!zj_owner_select_quiesced_reader(&expected, &refused) && !refused);
    assert(!zj_owner_submit(&capture, &refused));
    assert(!zj_owner_quiesce() && !selection_calls);
    atomic_store(&ota_pause, false);
    zj_reply_t reply = wait_reply(ticket);
    assert(reply.result == ZJ_UNCERTAIN && !selection_calls && !reply.rollback_intent.version);
    atomic_store(&ota_failure, 2); /* intent persisted but its reply failed */
    assert(zj_owner_select_quiesced_reader(&expected, &ticket));
    reply = wait_reply(ticket);
    assert(reply.result == ZJ_UNCERTAIN && !selection_calls && ota_blob.generation == 8);
    assert(!strcmp(ota_blob.journal.state, failed_rollback_test ? "FAILED_BOOT_INTENT" : "READER_INTENT"));
    uncertain_selection = true;
    assert(zj_owner_select_quiesced_reader(&expected, &ticket));
    reply = wait_reply(ticket);
    assert(reply.compatibility == ZJ_COMPAT_SELECTION_UNCERTAIN && selection_calls == 1 && ota_writes == 2);
    assert(ota_checkpoint_valid(&reply.rollback_intent) && reply.rollback_intent.generation == 8);
    /* Includes expiry after the platform's otadata mutation: it is uncertain,
     * never a retry-safe pre-action expiry or permission to resume capture. */
    assert(zj_owner_health(&health) && health.quiescing && health.quiesced &&
        !health.writer_allowed && !health.compatibility_checked);
    assert(!zj_owner_submit(&capture, &refused));
    assert(!strcmp(ota_blob.journal.state, failed_rollback_test ? "FAILED_BOOT_INTENT" : "READER_INTENT"));
    uncertain_selection = false;
    assert(zj_owner_select_quiesced_reader(&expected, &ticket));
    reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.compatibility == ZJ_COMPAT_OK && selection_calls == 2 && ota_writes == 2);
    strcpy(expected.journal.deployment_id, "changed-operation");
    expected.crc = dq_crc32(&expected, offsetof(ota_checkpoint_t, crc));
    assert(!zj_owner_select_quiesced_reader(&expected, &ticket));
    for (unsigned i = 0; i < ZJ_REQUEST_SLOTS; ++i) {
        reply = wait_reply(retained[i]);
        assert(reply.result == ZJ_OK && reply.capture_sequence == i + 2);
    }
    assert(zj_owner_health(&health) && health.quiesced && !health.occupied && !health.writer_allowed);
    atomic_store(&stop, true);
    assert(!pthread_join(thread, NULL));
}

static void prove_legacy_empty(void)
{
    uint8_t bytes[DQ_MAX_RECORD_BYTES]; size_t length;
    lq_token_t token; uint32_t depth;
    for(unsigned lane=0;lane<QS_HIK_SOURCE;lane++)assert(zq_snapshot((qs_lane_t)lane,&depth)&&!depth);
    for(unsigned lane=0;lane<2;lane++)assert(zq_legacy_peek(lane,bytes,sizeof(bytes),&length,&token)==DQ_EMPTY);
    for(unsigned lane=0;lane<2;lane++)assert(zq_attendance_legacy_peek(lane,bytes,sizeof(bytes),&length,&token)==DQ_EMPTY);
    for(unsigned lane=0;lane<3;lane++)assert(zq_evidence_peek(lane,bytes,sizeof(bytes),&length,&token)==DQ_EMPTY);
    zj_owner_health_t health;
    assert(zj_owner_health(&health) && health.legacy_verified_empty && !health.legacy_append_pending);
    assert(health.legacy_empty_mask==ZQ_INVENTORY_REQUIRED && health.legacy_required_mask==ZQ_INVENTORY_REQUIRED);
}

int main(int argc, char **argv)
{
    assert(!zj_owner_started() && !zj_owner_is_current_task());
    assert(zq_append(QS_LIVE, "before-owner", 12, QS_ADMIT_LIVE) == DQ_PENDING);
    assert(zq_legacy_append(0, "before-owner", 12, QS_ADMIT_LIVE) == DQ_PENDING && !flat[0].ready);
    assert(!retained[QS_LIVE].ready); /* No direct storage fallback. */
    zj_metadata_t metadata = {.segment_id = 1, .capture_epoch = {1},
        .terminal_serial = "TEST-TERMINAL", .decoder_profile = "G3-v1", .decoder_version = "1"};
    bool corrupt_journal = argc == 2 && !strcmp(argv[1], "--runtime-corrupt-journal");
    bool authority_test = argc == 2 && !strncmp(argv[1], "--authority-", 12);
    failed_rollback_test = argc == 2 && !strcmp(argv[1], "--failed-boot-full");
    bool rollback_test = failed_rollback_test || (argc == 2 && !strcmp(argv[1], "--rollback-full"));
    bool boundary_test = argc == 2 && !strcmp(argv[1], "--source-boundary");
    bool hil_idle_test = argc == 2 && !strncmp(argv[1], "--hil-", 6);
    bool recovering_checkpoint = argc == 2 && !corrupt_journal && !authority_test && !rollback_test && !boundary_test && !hil_idle_test;
    uint8_t damaged[ZJ_CHECKPOINT_BYTES];
    if (recovering_checkpoint) {
        /* A retained encrypted-NVS root is intact; only its retirement blob
         * is damaged. Use the real owner and recovery admission callbacks. */
        memcpy(root, "ZJROOT01", 8);
        root[8] = 1; root[9] = 1; /* Exclusive reserved sequence limit 257. */
        root[145] = 1; /* ADD cutover committed before sequence reservation. */
        for (unsigned i = 16; i < 64; ++i) root[i] = (uint8_t)i;
        memcpy(root + 64, "TEST-TERMINAL", 13);
        uint32_t crc = dq_crc32(root, ZJ_ROOT_BYTES - 4);
        for (unsigned i = 0; i < 4; ++i) root[ZJ_ROOT_BYTES - 4 + i] = (uint8_t)(crc >> (8 * i));
        root_length = sizeof(root);
        memset(checkpoint, 0xff, sizeof(checkpoint));
        checkpoint_length = sizeof(checkpoint);
        memcpy(damaged, checkpoint, sizeof(damaged));
        atomic_store(&full, !strcmp(argv[1], "--recovery-full"));
    }
    if (corrupt_journal) { memset(root, 0xff, sizeof(root)); root_length = sizeof(root); }
    assert(zj_owner_start("./owner-journal-", &metadata));
    zj_owner_health_t health;
    if (corrupt_journal) {
        for (unsigned i = 0; i < 2000; ++i) {
            assert(zj_owner_health(&health));
            if (!health.operation_running && health.failures) break;
            vTaskDelay(1);
        }
        assert(!health.ready && health.failures);
        runtime_checkpoint_t state = {.version = 1, .generation = 1, .history_schema = 2,
            .lease_active = 1, .lease_uid = 42, .lease_expiry = 1900000100};
        memset(state.source_chain, '0', 64);
        state.crc = dq_crc32(&state, offsetof(runtime_checkpoint_t, crc));
        runtime_checkpoint_t confirmed;
        assert(zj_runtime_checkpoint_save(&state, &confirmed));
        assert(confirmed.lease_active && confirmed.lease_uid == 42);
        assert(zj_owner_health(&health) && !health.ready);
        for (unsigned i = 0; i < sizeof(root); ++i) assert(root[i] == 0xff);
        atomic_store(&stop, true);
        assert(!pthread_join(thread, NULL));
        return 0;
    }
    if (recovering_checkpoint && atomic_load(&full)) {
        for (unsigned i = 0; i < 2000; ++i) {
            assert(zj_owner_health(&health));
            if (!health.operation_running && health.last_result == ZJ_FULL) break;
            vTaskDelay(1);
        }
        assert(!health.ready && health.last_result == ZJ_FULL);
        assert(!memcmp(damaged, checkpoint, sizeof(damaged)));
        atomic_store(&full, false);
    }
    for (unsigned i = 0; i < 8000; ++i) {
        assert(zj_owner_health(&health));
        if (health.ready) break;
        vTaskDelay(1);
    }
    assert(health.ready && root_length && !health.operation_running);
    if (boundary_test) {
        uint64_t ticket;
        zj_request_t request = {.operation=ZJ_SOURCE_BOUNDARY, .input.source_boundary={
            .create=true, .deadline_us=(uint64_t)esp_timer_get_time()+5000000,
            .facts={.next_ordinal=200000, .record_size=40, .anchor_digest={9}}}};
        assert(zj_owner_submit(&request, &ticket));
        assert(wait_reply(ticket).result==ZJ_INVALID && !source_boundary_writes);
        zj_request_t proof={.operation=ZJ_READER_CHECK};
        assert(zj_owner_submit(&proof, &ticket)); assert(wait_reply(ticket).result==ZJ_OK);
        for (int failure = 1; failure <= 6; ++failure) {
            atomic_store(&persistence_failure, failure);
            assert(zj_owner_submit(&request, &ticket));
            assert(wait_reply(ticket).result==ZJ_IO && !source_boundary_writes);
        }
        atomic_store(&persistence_failure, 0);
        atomic_store(&full, true); /* NVS boundary does not consume live SPIFFS reserve. */
        assert(zj_owner_submit(&request, &ticket));
        zj_reply_t reply=wait_reply(ticket);
        assert(reply.result==ZJ_OK && reply.source_boundary.facts.next_ordinal==200000 && source_boundary_writes==1);
        request.input.source_boundary.facts.next_ordinal=210000;
        assert(zj_owner_submit(&request, &ticket));
        reply=wait_reply(ticket);
        assert(reply.result==ZJ_OK && reply.source_boundary.facts.next_ordinal==200000 && source_boundary_writes==1);
        request.input.source_boundary.deadline_us=1;
        assert(zj_owner_submit(&request, &ticket)); assert(wait_reply(ticket).result==ZJ_STALE);
        request.input.source_boundary.deadline_us=(uint64_t)esp_timer_get_time()+5000000;
        atomic_store(&bridge_image, true);
        assert(zj_owner_submit(&request, &ticket)); assert(wait_reply(ticket).result==ZJ_INVALID);
        assert(source_boundary_writes==1);
        atomic_store(&stop, true); assert(!pthread_join(thread, NULL)); return 0;
    }
    if (rollback_test) { rollback_with_full_mailbox(); return 0; }
    if (authority_test) {
        assert(health.delivery_authority == ZJ_AUTHORITY_LEGACY && root[145] == 0);
        zj_request_t proof = {.operation = ZJ_READER_CHECK};
        zj_request_t append = {.operation = ZJ_APPEND, .input.observation = {
            .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
            .source_ordinal = UINT32_MAX, .raw_length = 40, .raw = {'A'}}};
        uint64_t attempt;
        /* An initial bridge may read/attest, but cannot initiate cutover. */
        atomic_store(&bridge_image, true);
        assert(zj_owner_submit(&proof, &attempt));
        assert(wait_reply(attempt).compatibility == ZJ_COMPAT_OK);
        assert(zj_owner_health(&health) && !health.writer_allowed && health.delivery_authority == ZJ_AUTHORITY_LEGACY);
        assert(zj_owner_submit(&append, &attempt));
        assert(wait_reply(attempt).result == ZJ_INVALID && root[145] == 0 && root[8] == 1 && root[9] == 0);
        atomic_store(&bridge_image, false);
        int fault = !strcmp(argv[1], "--authority-before") ? 1 :
            !strcmp(argv[1], "--authority-after") ? 2 :
            !strcmp(argv[1], "--authority-readback") ? 3 : 0;
        atomic_store(&cutover_failure, fault);
        assert(zj_owner_submit(&proof, &attempt));
        zj_reply_t result = wait_reply(attempt);
        if (fault) {
            assert(result.compatibility == ZJ_COMPAT_UNCERTAIN);
            assert(zj_owner_health(&health) && !health.writer_allowed);
            assert(root[145] == (fault == 1 ? 0 : 1));
            assert(zj_owner_submit(&append, &attempt));
            assert(wait_reply(attempt).result == ZJ_INVALID && root[8] == 1 && root[9] == 0);
            assert(zj_owner_submit(&proof, &attempt));
            result = wait_reply(attempt);
        }
        assert(result.compatibility == ZJ_COMPAT_OK && root[145] == 1);
        assert(zj_owner_health(&health) && health.writer_allowed && health.delivery_authority == ZJ_AUTHORITY_ADD);
        /* The verified bridge must now preserve new records through ADD. */
        atomic_store(&bridge_image, true);
        assert(zj_owner_submit(&proof, &attempt));
        assert(wait_reply(attempt).compatibility == ZJ_COMPAT_OK);
        assert(zj_owner_health(&health) && health.writer_allowed && health.delivery_authority == ZJ_AUTHORITY_ADD);
        assert(zj_owner_submit(&append, &attempt));
        zj_reply_t captured = wait_reply(attempt);
        /* Segment identity consumes sequence one; the first observation is two. */
        assert(captured.result == ZJ_OK && captured.capture_sequence == 2 && root[145] == 1);
        atomic_store(&stop, true);
        assert(!pthread_join(thread, NULL));
        return 0;
    }
    if (recovering_checkpoint) {
        assert(health.checkpoint_recovery_pending);
        zj_request_t evidence = {.operation = ZJ_PEEK};
        uint64_t evidence_ticket;
        assert(zj_owner_submit(&evidence, &evidence_ticket));
        zj_reply_t preserved = wait_reply(evidence_ticket);
        assert(preserved.result == ZJ_OK && preserved.item.exception == ZJ_EXCEPTION_CHECKPOINT);
        assert(preserved.item.exception_length == 8 + sizeof(damaged));
        assert(!memcmp(preserved.item.exception_bytes + 8, damaged, sizeof(damaged)));
        char wire[ZJ_CUSTODY_PAYLOAD_MAX];
        zj_custody_expected_t expected;
        zj_crypto_port_t crypto = {.digest = digest};
        assert(zj_custody_encode(&preserved.item, crypto, wire, sizeof(wire), &expected));
        evidence.operation = ZJ_SETTLE;
        evidence.input.settlement.token = preserved.item.token;
        memset(evidence.input.settlement.receipt_digest, 1, 32);
        memcpy(evidence.input.settlement.observation_id, expected.observation_id, sizeof(expected.observation_id));
        memcpy(evidence.input.settlement.payload_digest, expected.payload_digest, sizeof(expected.payload_digest));
        assert(zj_owner_submit(&evidence, &evidence_ticket));
        assert(wait_reply(evidence_ticket).result == ZJ_OK);
        assert(zj_owner_health(&health) && !health.checkpoint_recovery_pending);
        return 0;
    }
    zj_request_t request = {.operation = ZJ_APPEND, .input.observation = {
        .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
        .source_ordinal = UINT32_MAX, .raw_length = 40, .raw = {'A'}}};
    uint64_t ticket;
    assert(!health.writer_allowed && !health.compatibility_checked);
    assert(health.delivery_authority == ZJ_AUTHORITY_LEGACY);
    assert(zj_owner_submit(&request, &ticket));
    zj_reply_t refused = wait_reply(ticket);
    assert(refused.result == ZJ_INVALID && !refused.capture_sequence);
    zj_request_t compatibility = {.operation = ZJ_READER_CHECK};
    atomic_store(&refuse_compatibility, true);
    assert(zj_owner_submit(&compatibility, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_NOT_READY);
    assert(zj_owner_health(&health) && health.compatibility_checked && !health.writer_allowed);
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_INVALID);
    atomic_store(&refuse_compatibility, false);
    atomic_store(&stale_transport, true);
    assert(zj_owner_submit(&compatibility, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_NOT_READY);
    atomic_store(&stale_transport, false);
    assert(zj_owner_submit(&compatibility, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_OK);
    assert(zj_owner_health(&health) && health.writer_allowed && health.delivery_authority == ZJ_AUTHORITY_ADD);
    if (hil_idle_test) {
        int64_t accepted_epoch = 0;
        uint64_t accepted_us = 0, second;
        int64_t epoch = (int64_t)time(NULL);
        uint64_t deadline = (uint64_t)esp_timer_get_time() + 5000000;
        if (strstr(argv[1], "incident")) {
            /* A write failure finishes just before an otherwise idle gate.
             * Later success/readiness cannot disguise this boot's incident. */
            bool io_failure = !strcmp(argv[1], "--hil-io-incident");
            atomic_store(&full, !io_failure);
            atomic_store(&fail_next_sync, io_failure);
            assert(zj_owner_submit(&request, &ticket));
            assert(wait_reply(ticket).result == (io_failure ? ZJ_UNCERTAIN : ZJ_FULL));
            atomic_store(&full, false);
            assert(zj_owner_submit(&request, &ticket)); assert(wait_reply(ticket).result == ZJ_OK);
            assert(zj_owner_health(&health) && health.ready && health.writer_allowed &&
                health.last_append_result == ZJ_OK && health.hil_reboot_persistence_incident);
            assert(!zj_owner_try_quiesce_before(deadline, epoch + 60, &accepted_epoch, &accepted_us));
            assert(zj_owner_health(&health) && !health.quiescing && health.writer_allowed);
            atomic_store(&stop, true); assert(!pthread_join(thread, NULL)); return 0;
        }
        for (int failure = 1; failure <= 6; ++failure) {
            atomic_store(&persistence_failure, failure);
            assert(!zj_owner_try_quiesce_before(deadline, epoch + 60, &accepted_epoch, &accepted_us));
            assert(zj_owner_health(&health) && !health.quiescing && health.writer_allowed);
        }
        atomic_store(&persistence_failure, 0);
        assert(!zj_owner_try_quiesce_before(1, epoch + 60, &accepted_epoch, &accepted_us));
        assert(!zj_owner_try_quiesce_before(deadline, epoch, &accepted_epoch, &accepted_us));
        assert(zj_owner_health(&health) && !health.quiescing && health.writer_allowed);
        atomic_store(&pause_write, true);
        assert(zj_owner_submit(&request, &ticket));
        for (unsigned i = 0; i < 2000 && !atomic_load(&write_waiting); ++i) vTaskDelay(1);
        assert(atomic_load(&write_waiting));
        assert(!zj_owner_try_quiesce_before(deadline, epoch + 60, &accepted_epoch, &accepted_us));
        assert(zj_owner_submit(&request, &second));
        assert(!zj_owner_try_quiesce_before(deadline, epoch + 60, &accepted_epoch, &accepted_us));
        assert(zj_owner_health(&health) && !health.quiescing && health.writer_allowed);
        atomic_store(&pause_write, false);
        assert(wait_reply(ticket).result == ZJ_OK);
        /* The second completed reply may remain; it cannot represent I/O. */
        bool closed = false;
        for (unsigned i = 0; i < 2000 && !closed; ++i) {
            closed = zj_owner_try_quiesce_before(deadline, epoch + 60, &accepted_epoch, &accepted_us);
            if (!closed) vTaskDelay(1);
        }
        assert(closed && accepted_epoch >= epoch && accepted_us < deadline);
        assert(wait_reply(second).result == ZJ_OK);
        assert(!zj_owner_submit(&request, &ticket));
        assert(zj_owner_health(&health) && health.quiesced && !health.operation_running && !health.writer_allowed);
        atomic_store(&stop, true); assert(!pthread_join(thread, NULL)); return 0;
    }
    zj_request_t update = {.operation = ZJ_OTA_CHECK, .input.ota = {
        .address = 0x2a0000, .size = 0x280000, .version = ZJ_WRITER_VERSION}};
    assert(zj_owner_submit(&update, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_PROTECTED_SLOT);
    assert(zj_owner_health(&health) && health.writer_allowed && health.compatibility == ZJ_COMPAT_OK);
    atomic_store(&stale_transport, true);
    assert(zj_owner_submit(&update, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_NOT_READY);
    atomic_store(&stale_transport, false);
    assert(zj_owner_health(&health) && health.writer_allowed && health.compatibility == ZJ_COMPAT_OK);
    uint64_t gating_operations = health.completed, gating_failures = health.failures;
    assert(gating_operations == 7 && gating_failures == 6);
    /* Selection has no ordinary admission path: it requires the drained
     * owner's reserved control plus a durable approved OTA assignment. */
    zj_request_t selection = {.operation = ZJ_SELECT_READER,
        .input.reader_selection = {.image_digest = {17}, .deadline_us = 1}};
    assert(!zj_owner_submit(&selection, &ticket) && !ticket);
    assert(zj_owner_health(&health) && health.writer_allowed && health.compatibility_checked);
    assert(health.completed == gating_operations && health.failures == gating_failures);
    atomic_store(&pause_write, true);
    assert(zj_owner_submit(&request, &ticket));
    request.input.observation.raw[0] = 'B';
    for (unsigned i = 0; i < 2000 && !atomic_load(&write_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&write_waiting));
    assert(zj_owner_health(&health) && health.operation_running && health.occupied == 1);
    assert(!health.inventory_known && !health.verified_empty && health.pending_appends == 1);
    assert(zj_owner_abandon(ticket));
    atomic_store(&pause_write, false);
    request.operation = ZJ_PEEK;
    assert(zj_owner_submit(&request, &ticket));
    zj_reply_t reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.item.kind == ZJ_OBSERVATION && reply.item.observation.raw[0] == 'A');
    assert(zj_owner_health(&health) && health.inventory_known && !health.verified_empty &&
        health.journal_bytes > ZJ_META_BYTES && health.journal_segments == 1 && !health.pending_appends);
    assert(health.append_observed && health.last_append_result == ZJ_OK);

    atomic_store(&full, true);
    zj_token_t token = reply.item.token;
    char wire[ZJ_CUSTODY_PAYLOAD_MAX];
    zj_custody_expected_t expected;
    zj_crypto_port_t crypto = {.digest = digest};
    assert(zj_custody_encode(&reply.item, crypto, wire, sizeof(wire), &expected));
    request.operation = ZJ_APPEND;
    assert(zj_owner_submit(&request, &ticket));
    reply = wait_reply(ticket);
    assert(reply.result == ZJ_FULL && !reply.capture_sequence);
    assert(zj_owner_health(&health) && health.last_append_result == ZJ_FULL);
    request.operation = ZJ_SETTLE;
    request.input.settlement.token = token;
    memset(request.input.settlement.receipt_digest, 1, 32);
    memcpy(request.input.settlement.observation_id, expected.observation_id, sizeof(expected.observation_id));
    memcpy(request.input.settlement.payload_digest, expected.payload_digest, sizeof(expected.payload_digest));
    request.input.settlement.payload_digest[0] ^= 1;
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_STALE && !checkpoint_length);
    request.input.settlement.payload_digest[0] ^= 1;
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_OK && checkpoint_length);
    request.operation = ZJ_RECLAIM;
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_OK);
    request.operation = ZJ_PEEK;
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_EMPTY);
    assert(zj_owner_health(&health) && !health.occupied && !health.operation_running);
    assert(health.completed == gating_operations + 7 && health.failures == gating_failures + 1 &&
        health.max_operation_us && health.ready);
    assert(health.inventory_known && health.verified_empty && !health.journal_bytes && !health.journal_segments);
    assert(health.last_append_result == ZJ_FULL); /* Reads/reclamation cannot clear an append fault. */
    /* Runtime writes work without journal writer permission or free SPIFFS
     * space, and execute only on the storage owner under its shared lock. */
    runtime_checkpoint_t runtime = {.version = 1, .generation = 1, .history_schema = 2,
        .source_cursor = 99, .lease_active = 1, .lease_uid = 42, .lease_expiry = 1900000100};
    memset(runtime.source_chain, '0', 64);
    runtime.crc = dq_crc32(&runtime, offsetof(runtime_checkpoint_t, crc));
    runtime_checkpoint_t confirmed;
    assert(zj_runtime_checkpoint_save(&runtime, &confirmed));
    assert(runtime_writes == 1 && confirmed.source_cursor == 99 && confirmed.generation == 1);
    assert(zj_owner_health(&health));
    zl_lease_record_t lease = {.version = ZL_LEASE_VERSION, .generation = 1,
        .uid = 42, .active = 1, .expires_epoch = 1900000100, .terminal_serial = "TEST-LEASE"};
    memset(lease.identity_fingerprint, 'a', 64);
    zl_lease_checksum(&lease);
    zj_request_t lease_request = {.operation = ZJ_LEASE,
        .input.lease = {.state = lease, .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U}};
    assert(zj_owner_submit(&lease_request, &ticket));
    assert(wait_reply(ticket).result == ZJ_CORRUPT && !lease_writes); /* legacy active has no fingerprint */
    runtime_blob.lease_active = 0;
    runtime_blob.crc = dq_crc32(&runtime_blob, offsetof(runtime_checkpoint_t, crc));
    assert(zj_owner_submit(&lease_request, &ticket));
    assert(wait_reply(ticket).result == ZJ_OK && lease_writes == 1);
    runtime_blob.crc ^= 1;
    lease_request.input.lease.state.generation = 2;
    lease_request.input.lease.state.active = 0;
    lease_request.input.lease.state.expires_epoch = 0;
    zl_lease_checksum(&lease_request.input.lease.state);
    assert(zj_owner_submit(&lease_request, &ticket));
    reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && !reply.lease.active && lease_writes == 2);
    assert(!runtime_checkpoint_valid(&runtime_blob)); /* lease clear did not overwrite damaged source evidence */
    runtime_blob = confirmed;
    lease_blob.crc ^= 1;
    assert(zj_owner_submit(&lease_request, &ticket));
    assert(wait_reply(ticket).result == ZJ_CORRUPT && lease_writes == 2);
    assert(zj_owner_health(&health) && health.ready && !strcmp(health.failed_operation, "lease_checkpoint"));
    lease_blob.crc ^= 1;
    /* Real owner integration: optional refusal leaves attendance storage
     * usable; abandoned activation yields to live capture, then finishes
     * before any later catalog mutation or read can use the same paths. */
    zj_request_t catalog = {.operation = ZJ_CATALOG, .input.catalog = {
        .operation = ZC_RESET, .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U}};
    assert(zj_owner_submit(&catalog, &ticket));
    assert(wait_reply(ticket).result == ZJ_FULL);
    atomic_store(&full, false);
    assert(zj_owner_submit(&catalog, &ticket));
    reply = wait_reply(ticket); assert(reply.result == ZJ_OK);
    assert(zj_owner_health(&health) && health.append_observed && health.last_append_result == ZJ_FULL && health.ready);
    catalog.input.catalog.id = reply.catalog.id;
    catalog.input.catalog.operation = ZC_APPEND;
    catalog.input.catalog.length = ZC_CHUNK_BYTES;
    memset(catalog.input.catalog.bytes, 'C', ZC_CHUNK_BYTES);
    for (unsigned i = 0; i < 40; ++i) {
        assert(zj_owner_submit(&catalog, &ticket)); reply = wait_reply(ticket);
        assert(reply.result == ZJ_OK); catalog.input.catalog.offset = reply.catalog.offset;
    }
    catalog.input.catalog.operation = ZC_ACTIVATE;
    uint64_t catalog_ticket;
    assert(zj_owner_submit(&catalog, &catalog_ticket));
    assert(zj_owner_abandon(catalog_ticket));
    zj_request_t concurrent_capture = {.operation = ZJ_APPEND, .input.observation = {
        .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
        .source_ordinal = UINT32_MAX, .raw_length = 40, .raw = {'C'}}};
    assert(zj_owner_submit(&concurrent_capture, &ticket));
    assert(wait_reply(ticket).result == ZJ_OK);
    catalog.input.catalog = (zc_request_t){.operation = ZC_READ,
        .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U};
    assert(zj_owner_submit(&catalog, &ticket)); reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.catalog.length == ZC_CHUNK_BYTES &&
        reply.catalog.total == 40 * ZC_CHUNK_BYTES && reply.catalog.bytes[0] == 'C');
    /* Command replacement uses a separate bounded namespace and checkpoint.
     * Its yielded activation also lets a live observation commit first. */
    zj_request_t commands = {.operation = ZJ_COMMANDS, .input.catalog = {
        .operation = ZC_RECOVER, .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U}};
    assert(zj_owner_submit(&commands, &ticket)); assert(wait_reply(ticket).result == ZJ_OK);
    commands.input.catalog.operation = ZC_RESET;
    assert(zj_owner_submit(&commands, &ticket)); reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK);
    commands.input.catalog.id = reply.catalog.id;
    commands.input.catalog.operation = ZC_APPEND;
    commands.input.catalog.length = ZC_CHUNK_BYTES;
    memset(commands.input.catalog.bytes, 'D', ZC_CHUNK_BYTES);
    for (unsigned i = 0; i < 40; ++i) {
        assert(zj_owner_submit(&commands, &ticket)); reply = wait_reply(ticket);
        assert(reply.result == ZJ_OK); commands.input.catalog.offset = reply.catalog.offset;
    }
    commands.input.catalog.operation = ZC_ACTIVATE;
    uint64_t command_ticket;
    assert(zj_owner_submit(&commands, &command_ticket)); assert(zj_owner_abandon(command_ticket));
    assert(zj_owner_submit(&concurrent_capture, &ticket)); assert(wait_reply(ticket).result == ZJ_OK);
    commands.input.catalog = (zc_request_t){.operation = ZC_READ,
        .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U};
    assert(zj_owner_submit(&commands, &ticket)); reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.catalog.total == 40 * ZC_CHUNK_BYTES && reply.catalog.bytes[0] == 'D');
    assert(command_checkpoint.version && catalog_checkpoint.version);
    assert(zj_owner_submit(&catalog, &ticket)); reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.catalog.bytes[0] == 'C');
    /* Both command callers use the actual owner. Distinct files remain
     * independent; a timeout never supplies a false absence or durable ACK. */
    assert(zi_cache_contains(ZI_PROCESSED, "owner-receipt") == REL_ID_ABSENT);
    assert(zi_cache_remember(ZI_PROCESSED, "owner-receipt"));
    assert(zi_cache_remember(ZI_PROCESSED, "owner-receipt"));
    assert(zi_cache_contains(ZI_PROCESSED, "owner-receipt") == REL_ID_PRESENT);
    assert(zi_cache_contains(ZI_CANCELLED, "owner-receipt") == REL_ID_ABSENT);
    atomic_store(&full, true);
    assert(!zi_cache_remember(ZI_CANCELLED, "owner-receipt"));
    assert(zi_cache_contains(ZI_CANCELLED, "owner-receipt") == REL_ID_ABSENT);
    assert(zi_cache_remember(ZI_PROCESSED, "owner-receipt")); /* replay needs no new capacity */
    atomic_store(&full, false);
    assert(zi_cache_remember(ZI_CANCELLED, "owner-receipt"));
    /* An 8 KiB legacy item never enlarges a mailbox frame or the task stack.
     * Read a copied snapshot, append another item, then retire only its exact
     * original token; queue and journal custody remain distinct. */
    uint8_t retained_input[DQ_MAX_RECORD_BYTES], retained_output[DQ_MAX_RECORD_BYTES];
    for (unsigned i = 0; i < sizeof(retained_input); ++i) retained_input[i] = (uint8_t)i;
    uint32_t retained_depth = 999; size_t retained_length = 0; dq_token_t retained_token;
    assert(zq_append(QS_ORDS, retained_input, sizeof(retained_input), QS_ADMIT_LIVE) == DQ_OK);
    assert(zq_snapshot(QS_ORDS, &retained_depth) && retained_depth == 1);
    assert(zq_peek(QS_ORDS, retained_output, sizeof(retained_output), &retained_length, &retained_token) == DQ_OK);
    assert(retained_length == sizeof(retained_input) && !memcmp(retained_input, retained_output, retained_length));
    assert(zq_append(QS_ORDS, "later", 5, QS_ADMIT_RECOVERY) == DQ_OK);
    assert(zq_settle(QS_ORDS, &retained_token) == DQ_OK);
    assert(zq_settle(QS_ORDS, &retained_token) == DQ_STALE);
    assert(zq_snapshot(QS_ORDS, &retained_depth) && retained_depth == 1);
    char retained_instance[33];
    assert(zq_generation(retained_instance) && strlen(retained_instance) == 32);
    assert(zq_recover() && zq_probe());
    atomic_store(&full, true);
    assert(zq_append(QS_LIVE, "full", 4, QS_ADMIT_LIVE) == DQ_FULL);
    assert(zq_peek(QS_ORDS, retained_output, sizeof(retained_output), &retained_length, &retained_token) == DQ_OK);
    assert(retained_length == 5 && !memcmp(retained_output, "later", 5));
    assert(zq_settle(QS_ORDS, &retained_token) == DQ_OK);
    assert(zq_peek(QS_ORDS, retained_output, sizeof(retained_output), &retained_length, &retained_token) == DQ_EMPTY);
    atomic_store(&full, false);
    assert(zj_owner_health(&health) && !health.occupied);
    lq_token_t flat_token;
    assert(zq_legacy_append(0, "owner-flat", 10, QS_ADMIT_LIVE) == DQ_OK);
    assert(zq_legacy_peek(0, retained_output, sizeof(retained_output), &retained_length, &flat_token) == DQ_OK);
    assert(retained_length == 11 && !memcmp(retained_output, "owner-flat\n", 11));
    assert(zq_legacy_settle(0, &flat_token, false) == DQ_OK);
    assert(zq_legacy_settle(0, &flat_token, false) == DQ_STALE);
    assert(zq_legacy_peek(0, retained_output, sizeof(retained_output), &retained_length, &flat_token) == DQ_EMPTY);
    FILE *quarantine_file=fopen("quarantine-add","wb");
    assert(quarantine_file && fwrite("raw\0tail",1,8,quarantine_file)==8 && fclose(quarantine_file)==0);
    assert(zq_evidence_peek(1,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_OK);
    assert(retained_length==8 && !memcmp(retained_output,"raw\0tail",8) && flat_token.evidence_required);
    assert(zq_evidence_settle(1,&flat_token)==DQ_OK);
    assert(zq_evidence_settle(1,&flat_token)==DQ_STALE);
    assert(zq_evidence_peek(1,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_EMPTY);
    assert(zq_attendance_legacy_append(0,"retained-ords",13,QS_ADMIT_RECOVERY)==DQ_OK);
    assert(zq_attendance_legacy_peek(0,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_OK);
    assert(retained_length==14 && !memcmp(retained_output,"retained-ords\n",14));
    assert(zq_attendance_legacy_settle(0,&flat_token,false)==DQ_OK);
    assert(zq_attendance_legacy_peek(0,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_EMPTY);
    assert(zj_owner_health(&health) && !health.occupied);
    assert(zq_probe()); /* Starts a new absence proof across all thirteen domains. */
    assert(zj_owner_health(&health) && !health.legacy_verified_empty);
    prove_legacy_empty();
    assert(zj_owner_health(&health));
    uint64_t inventory_generation=health.legacy_inventory_generation;
    prove_legacy_empty();
    assert(zj_owner_health(&health) && health.legacy_inventory_generation==inventory_generation);
    /* A queued legacy append invalidates absence before it executes. Keep the
     * real owner blocked in another write while admitting the copied request. */
    zj_request_t pause_capture={.operation=ZJ_APPEND,.input.observation={
        .raw_format=ZJ_LIVE_FRAME,.time_quality=ZJ_TIME_UNKNOWN,.source_ordinal=UINT32_MAX,
        .raw_length=40,.raw={'I'}}};
    atomic_store(&pause_write,true);atomic_store(&write_waiting,false);
    assert(zj_owner_submit(&pause_capture,&ticket));
    for(unsigned i=0;i<2000&&!atomic_load(&write_waiting);i++)vTaskDelay(1);
    assert(atomic_load(&write_waiting));
    zj_request_t pending_legacy={.operation=ZJ_SEGMENTED_QUEUE,.input.segmented={
        .domain=ZQ_ADD_LEGACY,.lane=0,.operation=ZQ_APPEND_BEGIN,.policy=QS_ADMIT_LIVE,
        .total=1,.deadline_us=(uint64_t)esp_timer_get_time()+5000000U}};
    uint64_t legacy_ticket;
    assert(zj_owner_submit(&pending_legacy,&legacy_ticket));
    assert(zj_owner_health(&health) && !health.legacy_verified_empty && health.legacy_append_pending);
    atomic_store(&pause_write,false);assert(wait_reply(ticket).result==ZJ_OK);
    zj_reply_t legacy_reply=wait_reply(legacy_ticket);assert(legacy_reply.segmented.result==DQ_OK);
    assert(zj_owner_health(&health) && health.legacy_append_pending && !health.legacy_verified_empty);
    pending_legacy.input.segmented.transfer=legacy_reply.segmented.transfer;
    pending_legacy.input.segmented.operation=ZQ_APPEND_CHUNK;
    pending_legacy.input.segmented.length=1;pending_legacy.input.segmented.bytes[0]='Z';
    assert(zj_owner_submit(&pending_legacy,&legacy_ticket));assert(wait_reply(legacy_ticket).segmented.result==DQ_OK);
    pending_legacy.input.segmented.operation=ZQ_APPEND_COMMIT;pending_legacy.input.segmented.length=0;
    assert(zj_owner_submit(&pending_legacy,&legacy_ticket));assert(wait_reply(legacy_ticket).segmented.result==DQ_OK);
    assert(zj_owner_health(&health) && !health.legacy_verified_empty && !health.legacy_append_pending);
    assert(zq_legacy_peek(0,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_OK);
    assert(retained_length==2 && !memcmp(retained_output,"Z\n",2));
    assert(zq_legacy_settle(0,&flat_token,false)==DQ_OK);
    prove_legacy_empty();
    atomic_store(&full,true);
    assert(zq_legacy_append(1,"refused",7,QS_ADMIT_LIVE)==DQ_FULL);
    assert(zj_owner_health(&health) && !health.legacy_verified_empty);
    atomic_store(&full,false);prove_legacy_empty();
    assert(zj_owner_health(&health) && health.legacy_inventory_generation>inventory_generation);
    /* A capture caller can time out while its accepted append is still inside
     * storage. Quiescence must finish that write and all queued work before
     * acknowledging; a new producer cannot race the completed barrier. */
    uint64_t before_quiesce = health.completed, queued_ticket;
    zj_request_t final_capture = {.operation = ZJ_APPEND, .input.observation = {
        .raw_format = ZJ_LIVE_FRAME, .time_quality = ZJ_TIME_UNKNOWN,
        .source_ordinal = UINT32_MAX, .raw_length = 40, .raw = {'Q'}}};
    atomic_store(&full, false);
    atomic_store(&write_waiting, false);
    atomic_store(&pause_write, true);
    assert(zj_owner_submit(&final_capture, &ticket));
    for (unsigned i = 0; i < 2000 && !atomic_load(&write_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&write_waiting));
    assert(zj_owner_submit(&final_capture, &queued_ticket));
    assert(zj_owner_abandon(queued_ticket));
    zj_request_t final_checkpoint = {.operation = ZJ_RUNTIME_CHECKPOINT,
        .input.runtime_checkpoint = {.state = runtime,
            .deadline_us = (uint64_t)esp_timer_get_time() + 5000000U}};
    assert(zj_owner_submit(&final_checkpoint, &queued_ticket));
    assert(zj_owner_abandon(queued_ticket));
    assert(!zj_owner_quiesce());
    assert(zj_owner_health(&health) && health.quiescing && !health.quiesced && health.operation_running);
    assert(health.pending_appends == 2 && !health.verified_empty && !health.inventory_known);
    uint64_t refused_ticket = 99;
    assert(!zj_owner_submit(&final_capture, &refused_ticket) && !refused_ticket);
    atomic_store(&pause_write, false);
    assert(wait_reply(ticket).result == ZJ_OK);
    for (unsigned i = 0; i < 2000 && !zj_owner_quiesce(); ++i) vTaskDelay(1);
    assert(zj_owner_quiesce());
    assert(zj_owner_health(&health) && health.quiesced && !health.operation_running &&
        !health.writer_allowed && !health.compatibility_checked && health.completed == before_quiesce + 3);
    assert(runtime_writes == 2 && runtime_blob.generation == 2);
    assert(health.last_append_result == ZJ_OK && !health.verified_empty && !health.pending_appends);
    assert(!zj_owner_submit(&final_checkpoint, &refused_ticket));
    assert(!zj_owner_submit(&compatibility, &refused_ticket));
    assert(zq_append(QS_LIVE, "after-quiescence", 16, QS_ADMIT_LIVE) == DQ_PENDING);
    assert(zq_legacy_append(0, "after-quiescence", 16, QS_ADMIT_LIVE) == DQ_PENDING);
    assert(zq_evidence_peek(2,retained_output,sizeof(retained_output),&retained_length,&flat_token)==DQ_PENDING);
    assert(zq_attendance_legacy_append(1,"after-quiescence",16,QS_ADMIT_RECOVERY)==DQ_PENDING);
    assert(zq_peek(QS_ORDS, retained_output, sizeof(retained_output), &retained_length, &retained_token) == DQ_PENDING);
    assert(!retained_length && !retained_token.end && !zq_probe());
    vTaskDelay(10);
    assert(zj_owner_health(&health) && health.completed == before_quiesce + 3);
    assert(!pthread_mutex_trylock(&budget));
    assert(!pthread_mutex_unlock(&budget));
    atomic_store(&stop, true);
    assert(!pthread_join(thread, NULL));
    return 0;
}
