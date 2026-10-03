#include "zkt_storage_owner_platform.h"
#include "zkt_storage_owner.h"
#include "zkt_journal_crypto.h"
#include "zkt_journal_state.h"
#include "zkt_custody_wire.h"
#include "zkt_journal_transport.h"
#include "zkt_reader_platform.h"
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
static pthread_t thread;
static pthread_mutex_t budget = PTHREAD_MUTEX_INITIALIZER;
static void (*task_function)(void *);
static void *task_argument;
static uint8_t root[ZJ_ROOT_BYTES], checkpoint[ZJ_CHECKPOINT_BYTES];
static size_t root_length, checkpoint_length;

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
    assert(!strcmp(name, "zkt_journal"));
    *handle = 1;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { assert(handle == 1); }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *name, void *out, size_t *length)
{
    assert(handle == 1);
    if (!strcmp(name, "reader_v1")) return ESP_ERR_NVS_NOT_FOUND;
    bool is_root = !strcmp(name, "root");
    size_t size = is_root ? root_length : checkpoint_length;
    if (!size) return ESP_ERR_NVS_NOT_FOUND;
    if (size > *length) return ESP_ERR_INVALID_SIZE;
    *length = size;
    memcpy(out, is_root ? root : checkpoint, size);
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *name, const void *bytes, size_t length)
{
    assert(handle == 1);
    if (!strcmp(name, "root")) {
        assert(length == sizeof(root));
        memcpy(root, bytes, length);
        root_length = length;
    } else {
        assert(!strcmp(name, "retirement") && length == sizeof(checkpoint));
        memcpy(checkpoint, bytes, length);
        checkpoint_length = length;
    }
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle) { assert(handle == 1); return ESP_OK; }
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
    *writer_allowed = delivery && !recovering && !atomic_load(&refuse_compatibility);
    return *writer_allowed ? ZJ_COMPAT_OK : ZJ_COMPAT_NOT_READY;
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
    bool recovering_checkpoint = argc == 2;
    uint8_t damaged[ZJ_CHECKPOINT_BYTES];
    if (recovering_checkpoint) {
        /* A retained encrypted-NVS root is intact; only its retirement blob
         * is damaged. Use the real owner and recovery admission callbacks. */
        memcpy(root, "ZJROOT01", 8);
        root[8] = 1; root[9] = 1; /* Exclusive reserved sequence limit 257. */
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
    assert(zj_owner_start("./owner-journal-", &metadata));
    zj_owner_health_t health;
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
    assert(zj_owner_health(&health) && health.writer_allowed);
    uint64_t gating_operations = health.completed, gating_failures = health.failures;
    assert(gating_operations == 5 && gating_failures == 4);
    atomic_store(&pause_write, true);
    assert(zj_owner_submit(&request, &ticket));
    request.input.observation.raw[0] = 'B';
    for (unsigned i = 0; i < 2000 && !atomic_load(&write_waiting); ++i) vTaskDelay(1);
    assert(atomic_load(&write_waiting));
    assert(zj_owner_health(&health) && health.operation_running && health.occupied == 1);
    assert(zj_owner_abandon(ticket));
    atomic_store(&pause_write, false);
    request.operation = ZJ_PEEK;
    assert(zj_owner_submit(&request, &ticket));
    zj_reply_t reply = wait_reply(ticket);
    assert(reply.result == ZJ_OK && reply.item.kind == ZJ_OBSERVATION && reply.item.observation.raw[0] == 'A');

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
    atomic_store(&stop, true);
    assert(!pthread_join(thread, NULL));
    return 0;
}
