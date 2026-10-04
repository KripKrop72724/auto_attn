#include "zkt_storage_owner_platform.h"
#include "zkt_storage_owner.h"
#include "zkt_journal_crypto.h"
#include "zkt_journal_state.h"
#include "zkt_custody_wire.h"
#include "zkt_journal_transport.h"
#include "zkt_reader_platform.h"
#include "zkt_runtime_checkpoint.h"
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
static pthread_t thread;
static pthread_mutex_t budget = PTHREAD_MUTEX_INITIALIZER;
static void (*task_function)(void *);
static void *task_argument;
static uint8_t root[ZJ_ROOT_BYTES], checkpoint[ZJ_CHECKPOINT_BYTES];
static size_t root_length, checkpoint_length;
static runtime_checkpoint_t runtime_blob;
static bool runtime_present;
static unsigned runtime_writes;
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
    task_function = function;
    task_argument = argument;
    *handle = &thread;
    assert(!pthread_create(&thread, NULL, worker, NULL));
    return pdPASS;
}
unsigned ulTaskNotifyTake(int clear, unsigned wait_ms)
{
    (void)clear; (void)wait_ms;
    if (atomic_load(&stop)) pthread_exit(NULL);
    vTaskDelay(1);
    return 1;
}
void xTaskNotifyGive(TaskHandle_t handle) { (void)handle; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle)
{
    (void)mode;
    assert(!strcmp(name, "zkt_journal") || !strcmp(name, "zone_lite"));
    if (!strcmp(name, "zone_lite")) {
        assert(pthread_equal(pthread_self(), thread));
        assert(pthread_mutex_trylock(&budget) == EBUSY);
        *handle = 2;
    } else *handle = 1;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(handle == 1 || handle == 2); }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *name, void *out, size_t *length)
{
    if (handle == 2) {
        assert(!strcmp(name, "runtime_v1") && *length == sizeof(runtime_blob));
        if (!runtime_present) return ESP_ERR_NVS_NOT_FOUND;
        memcpy(out, &runtime_blob, sizeof(runtime_blob));
        return ESP_OK;
    }
    assert(handle == 1);
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
    if (handle == 2) {
        assert(!strcmp(name, "runtime_v1") && length == sizeof(runtime_blob));
        runtime_blob = *(const runtime_checkpoint_t *)bytes;
        runtime_present = true;
        ++runtime_writes;
        return ESP_OK;
    }
    assert(handle == 1);
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
esp_err_t nvs_commit(nvs_handle_t handle) { assert(handle == 1 || handle == 2); return ESP_OK; }
bool qs_local_begin(qs_admission_t policy, size_t bytes)
{
    assert(policy == QS_ADMIT_LIVE && bytes >= ZJ_RECORD_MAX);
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
    assert(policy == QS_ADMIT_RECOVERY && bytes == 8U + ZJ_CHECKPOINT_BYTES);
    if (atomic_load(&full)) { errno = ENOSPC; return false; }
    return true;
}
void qs_local_end(bool persisted, int error) { (void)persisted; (void)error; assert(!pthread_mutex_unlock(&budget)); }
qs_health_t qs_local_health_locked(void)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    return (qs_health_t){.observed = true, .available = true,
        .recovery_complete = true, .persistence_verified = true};
}
bool zj_transport_health(zj_transport_health_t *health)
{
    *health = (zj_transport_health_t){.started = true,
        .sampled_ms = (uint32_t)(esp_timer_get_time() / 1000) - (atomic_load(&stale_transport) ? 50000U : 0)};
    return true;
}
zj_compat_result_t zj_reader_platform_check(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, bool *writer_allowed)
{
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0]);
    assert(ready && persistence);
    /* The actual ESP/NVS/identity checks have their own platform harness.
     * This spy verifies owner locking, gating and recovery transitions. */
    bool compatible = delivery && !recovering && !atomic_load(&refuse_compatibility);
    *writer_allowed = compatible && !atomic_load(&bridge_image);
    return compatible ? ZJ_COMPAT_OK : ZJ_COMPAT_NOT_READY;
}
zj_compat_result_t zj_reader_platform_update(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, uint32_t address, uint32_t size, const char *version)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0] && ready && persistence);
    assert(address == 0x2a0000 && size == 0x280000 && !strcmp(version, ZJ_WRITER_VERSION));
    return delivery && !recovering ? ZJ_COMPAT_PROTECTED_SLOT : ZJ_COMPAT_NOT_READY;
}
static bool uncertain_selection;
zj_compat_result_t zj_reader_platform_select(const char *serial, const uint8_t epoch[16],
    bool ready, bool delivery, bool persistence, bool recovering, const uint8_t expected[32], uint64_t deadline)
{
    assert(pthread_mutex_trylock(&budget) == EBUSY);
    assert(!strcmp(serial, "TEST-TERMINAL") && epoch[0] && ready && persistence && expected[0] == 17);
    if (deadline <= (uint64_t)esp_timer_get_time()) return ZJ_COMPAT_SELECTION_EXPIRED;
    if (!delivery || recovering) return ZJ_COMPAT_NOT_READY;
    return uncertain_selection ? ZJ_COMPAT_SELECTION_UNCERTAIN : ZJ_COMPAT_OK;
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
int main(int argc, char **argv)
{
    zj_metadata_t metadata = {.segment_id = 1, .capture_epoch = {1},
        .terminal_serial = "TEST-TERMINAL", .decoder_profile = "G3-v1", .decoder_version = "1"};
    bool corrupt_journal = argc == 2 && !strcmp(argv[1], "--runtime-corrupt-journal");
    bool authority_test = argc == 2 && !strncmp(argv[1], "--authority-", 12);
    bool recovering_checkpoint = argc == 2 && !corrupt_journal && !authority_test;
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
    zj_request_t selection = {.operation = ZJ_SELECT_READER,
        .input.reader_selection = {.image_digest = {17}, .deadline_us = 1}};
    assert(zj_owner_submit(&selection, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_SELECTION_EXPIRED);
    assert(zj_owner_health(&health) && health.writer_allowed && health.compatibility_checked);
    selection.input.reader_selection.deadline_us = (uint64_t)esp_timer_get_time() + 5000000U;
    atomic_store(&stale_transport, true);
    assert(zj_owner_submit(&selection, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_NOT_READY);
    assert(zj_owner_health(&health) && health.writer_allowed && health.compatibility_checked);
    atomic_store(&stale_transport, false);
    assert(zj_owner_submit(&selection, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_OK);
    assert(zj_owner_health(&health) && !health.writer_allowed && !health.compatibility_checked);
    assert(zj_owner_submit(&request, &ticket));
    assert(wait_reply(ticket).result == ZJ_INVALID);
    assert(zj_owner_submit(&compatibility, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_OK);
    uncertain_selection = true;
    assert(zj_owner_submit(&selection, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_SELECTION_UNCERTAIN);
    assert(zj_owner_health(&health) && !health.writer_allowed && !health.compatibility_checked);
    uncertain_selection = false;
    assert(zj_owner_submit(&compatibility, &ticket));
    assert(wait_reply(ticket).compatibility == ZJ_COMPAT_OK);
    assert(zj_owner_health(&health) && health.writer_allowed);
    assert(health.completed == gating_operations + 7 && health.failures == gating_failures + 4);
    gating_operations = health.completed;
    gating_failures = health.failures;
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
    vTaskDelay(10);
    assert(zj_owner_health(&health) && health.completed == before_quiesce + 3);
    assert(!pthread_mutex_trylock(&budget));
    assert(!pthread_mutex_unlock(&budget));
    atomic_store(&stop, true);
    assert(!pthread_join(thread, NULL));
    return 0;
}
