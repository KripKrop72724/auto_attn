/* Host harness for queue_store.c fault recovery, built as one translation
 * unit with the real storage budget and legacy fault tracker. Queues and NVS
 * live in memory; the persistence probe writes real files under QS_ROOT. */
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static char root[256];
static unsigned probe_opens;
static int failing_opens; /* the next N probe file opens fail with EIO */

static const char *host_path(const char *path)
{
    static char out[512];
    snprintf(out, sizeof(out), "%s%s", root, path);
    return out;
}
static FILE *host_fopen(const char *path, const char *mode)
{
    ++probe_opens;
    if (failing_opens) {
        --failing_opens;
        errno = EIO;
        return NULL;
    }
    return fopen(host_path(path), mode);
}
static int host_unlink(const char *path) { return unlink(host_path(path)); }
#define fopen(path, mode) host_fopen((path), (mode))
#define unlink(path) host_unlink(path)

#include "queue_store.c"

#undef fopen
#undef unlink

/* ---- the platform ---- */
static int64_t now_us;
int64_t esp_timer_get_time(void) { return now_us; }
void esp_fill_random(void *buffer, size_t length)
{
    static unsigned char next = 1;
    for (size_t index = 0; index < length; ++index) ((unsigned char *)buffer)[index] = next++;
}
void *heap_caps_malloc(size_t size, uint32_t caps)
{
    (void)caps;
    return malloc(size);
}

SemaphoreHandle_t xSemaphoreCreateMutex(void) { return calloc(1, sizeof(int)); }
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t timeout)
{
    (void)timeout;
    assert(mutex && !*mutex); /* never re-entered: catches lock-order slips */
    *mutex = 1;
    return pdTRUE;
}
BaseType_t xSemaphoreGive(SemaphoreHandle_t mutex)
{
    assert(mutex && *mutex);
    *mutex = 0;
    return pdTRUE;
}

static size_t fs_total = 8000000, fs_used = 1000000;
esp_err_t esp_spiffs_info(const char *label, size_t *total, size_t *used)
{
    (void)label;
    *total = fs_total;
    *used = fs_used;
    return ESP_OK;
}

static unsigned char nvs_proof[32];
static size_t nvs_proof_size;
esp_err_t nvs_open(const char *name, nvs_open_mode_t mode, nvs_handle_t *handle)
{
    (void)name, (void)mode;
    *handle = 1;
    return ESP_OK;
}
esp_err_t nvs_get_str(nvs_handle_t handle, const char *key, char *out, size_t *length)
{
    (void)handle;
    assert(!strcmp(key, "instance") && *length >= 33);
    snprintf(out, *length, "%s", "00112233445566778899aabbccddeeff");
    return ESP_OK;
}
esp_err_t nvs_set_str(nvs_handle_t handle, const char *key, const char *value)
{
    (void)handle, (void)key, (void)value;
    return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *key, void *out, size_t *length)
{
    (void)handle;
    if (strcmp(key, "write_proof") || !nvs_proof_size) return ESP_ERR_NVS_NOT_FOUND;
    assert(*length >= nvs_proof_size);
    memcpy(out, nvs_proof, nvs_proof_size);
    *length = nvs_proof_size;
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *key, const void *value, size_t length)
{
    (void)handle;
    if (!strcmp(key, "write_proof")) {
        assert(length <= sizeof(nvs_proof));
        memcpy(nvs_proof, value, length);
        nvs_proof_size = length;
    }
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle)
{
    (void)handle;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { (void)handle; }

bool storage_upgrade_ready(void) { return true; }
bool storage_upgrade_segmented_writes(void) { return true; }

/* ---- in-memory queues; the audit reads one record per call, as dq does ---- */
static dq_result_t append_result = DQ_OK;
static int append_errno;
static dq_result_t audit_fault[QS_COUNT];

static unsigned lane_of(const durable_queue_t *queue)
{
    return (unsigned)(((const char *)queue - (const char *)&lanes[0].queue) / (ptrdiff_t)sizeof(lane_t));
}
dq_result_t dq_open(durable_queue_t *queue, const char *prefix, dq_port_t port)
{
    snprintf(queue->prefix, sizeof(queue->prefix), "%s", prefix);
    queue->port = port;
    queue->checkpoint.generation = 1;
    queue->ready = true;
    return DQ_OK;
}
dq_result_t dq_append(durable_queue_t *queue, const void *data, size_t length)
{
    (void)data, (void)length;
    if (append_result != DQ_OK) {
        errno = append_errno;
        return append_result;
    }
    ++queue->checkpoint.generation;
    ++queue->checkpoint.depth;
    return DQ_OK;
}
dq_result_t dq_peek(durable_queue_t *queue, void *data, size_t capacity, size_t *length, dq_token_t *token)
{
    (void)data, (void)capacity, (void)token;
    *length = 0;
    return queue->checkpoint.depth ? DQ_OK : DQ_EMPTY;
}
dq_result_t dq_settle(durable_queue_t *queue, const dq_token_t *token)
{
    (void)token;
    if (queue->checkpoint.depth) --queue->checkpoint.depth;
    ++queue->checkpoint.generation;
    return DQ_OK;
}
dq_result_t dq_audit_step(const durable_queue_t *queue, dq_audit_t *audit, void *buffer, size_t capacity)
{
    (void)buffer, (void)capacity;
    unsigned lane = lane_of(queue);
    assert(lane < QS_COUNT);
    if (audit_fault[lane] != DQ_OK) return audit_fault[lane];
    if (!audit->started || audit->generation != queue->checkpoint.generation) {
        *audit = (dq_audit_t){.generation = queue->checkpoint.generation,
                              .remaining = queue->checkpoint.depth, .started = true};
    }
    if (!audit->remaining) {
        audit->complete = true;
        return DQ_OK;
    }
    --audit->remaining;
    return DQ_PENDING;
}

/* ---- helpers ---- */
/* Whether a complete pass of every lane covers the latest restart. */
static bool audited(void) { return audited_request == health.verification_requests; }

/* Run supervisor passes until every lane has been audited since the last
 * restart; returns how many passes that took. */
static unsigned audit_all(void)
{
    for (unsigned pass = 1; pass <= 64; ++pass) {
        (void)qs_recover_step();
        if (audited()) return pass;
    }
    assert(!"audit never completed");
    return 0;
}

static qs_health_t current(void) { return qs_health(); }

/* A verification attempt after the probe interval, so only the store's own
 * rules decide whether a probe runs. */
static bool verify(void)
{
    now_us += QS_PROBE_INTERVAL_US + 1000000;
    return qs_verify_persistence();
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    snprintf(root, sizeof(root), "%s", argv[1]);
    assert(mkdir(host_path("/storage"), 0700) == 0);

    /* A fresh boot is not verified until every lane is audited and the probe
     * passes. */
    assert(qs_init());
    qs_health_t seen = current();
    assert(!seen.recovery_complete && !seen.persistence_verified && !seen.verified_through);
    assert(!verify());
    audit_all();
    assert(verify());
    seen = current();
    assert(seen.recovery_complete && seen.persistence_verified && !seen.last_error);
    assert(seen.verified_through == seen.verification_requests);

    /* A failed append is recovered, not forgotten: only after every lane has
     * been audited again and a fresh probe passes. */
    append_result = DQ_IO;
    append_errno = EIO;
    assert(qs_append(QS_LIVE, "x", 1) == DQ_IO);
    append_result = DQ_OK;
    seen = current();
    uint32_t request = seen.verification_requests;
    assert(seen.last_error == EIO && !seen.recovery_complete && seen.verified_through < request);
    assert(!verify()); /* not audited since the fault */
    unsigned opens = probe_opens;
    assert(!qs_recover_step());       /* the fault keeps recovery incomplete */
    audit_all();
    assert(probe_opens == opens);
    assert(verify());
    seen = current();
    assert(!seen.last_error && seen.recovery_complete && seen.persistence_verified);
    assert(seen.recoveries == 1 && seen.recovered_error == EIO);
    assert(!strcmp(seen.recovered_operation, "segment_append"));
    assert(seen.verified_through == request && probe_opens == opens + 2);

    /* A queue with pending records takes one pass per record to audit, and
     * a fault reported while a pass runs is not covered by that pass. */
    for (unsigned record = 0; record < 3; ++record) assert(qs_append(QS_BULK, "y", 1) == DQ_OK);
    assert(qs_request_verification());
    assert(audit_all() == 4);
    assert(qs_local_begin(QS_ADMIT_LIVE, 100));
    qs_local_end(false, EIO);
    assert(!audited() && !verify());
    audit_all();
    assert(verify() && current().recoveries == 2);

    /* Refused live attendance: no probe writes flash while the store is
     * full, and the fault recovers once live attendance has room. */
    fs_used = 6500000;
    assert(!qs_local_begin(QS_ADMIT_LIVE, 100) && errno == ENOSPC);
    assert(current().last_error == ENOSPC);
    audit_all();
    opens = probe_opens;
    for (unsigned pass = 0; pass < 5; ++pass) assert(!verify());
    assert(probe_opens == opens && current().last_error == ENOSPC);
    fs_used = 2000000;
    assert(verify());
    seen = current();
    assert(seen.recoveries == 3 && seen.recovered_error == ENOSPC && !seen.last_error);
    assert(!strcmp(seen.recovered_operation, "capacity_admission"));

    /* Corruption found by the audit restarts verification on every pass,
     * so it never recovers until the lane audits clean. */
    audit_fault[QS_ORDS] = DQ_CORRUPT;
    assert(qs_request_verification());
    for (unsigned pass = 0; pass < 10; ++pass) {
        assert(!qs_recover_step());
        assert(!verify());
    }
    assert(current().last_error == EBADMSG && current().recoveries == 3);
    audit_fault[QS_ORDS] = DQ_OK;
    audit_all();
    assert(verify());
    seen = current();
    assert(seen.recoveries == 4 && seen.recovered_error == EBADMSG && !strcmp(seen.recovered_operation, "segment_verify"));

    /* A legacy append fault holds verification until that same lane appends
     * again; a retirement there or an append elsewhere is not evidence. */
    assert(qs_local_read_begin());
    qs_local_end_legacy(LF_ORDS_PENDING, LF_APPEND, DQ_IO, EIO);
    seen = current();
    assert(seen.legacy.append_faults == 1 && seen.last_error == EIO && !seen.recovery_complete);
    audit_all();
    assert(!verify());
    assert(qs_local_read_begin());
    qs_local_end_legacy(LF_ORDS_PENDING, LF_RETIRE, DQ_OK, 0);
    assert(qs_local_read_begin());
    qs_local_end_legacy(LF_IDENTITY_BLOCKED, LF_APPEND, DQ_OK, 0);
    assert(current().legacy.append_faults == 1 && !verify());
    assert(qs_local_read_begin());
    qs_local_end_legacy(LF_ORDS_PENDING, LF_APPEND, DQ_OK, 0);
    seen = current();
    assert(!seen.legacy.append_faults && seen.legacy.write_recoveries == 1 && !seen.last_error);
    assert(!verify()); /* still needs the fresh audit */
    audit_all();
    assert(qs_recover_step() && verify());
    seen = current();
    assert(seen.recovery_complete && seen.persistence_verified && seen.recoveries == 4);
    assert(seen.verified_through == seen.verification_requests);

    /* A probe that fails during recovery backs off; the fault stays until a
     * later probe passes. */
    append_result = DQ_IO;
    append_errno = EIO;
    assert(qs_append(QS_RECEIPTS, "z", 1) == DQ_IO);
    append_result = DQ_OK;
    audit_all();
    failing_opens = 1;
    assert(!verify());
    seen = current();
    assert(seen.last_error == EIO && seen.persistence_probe_error == EIO);
    opens = probe_opens;
    assert(!qs_verify_persistence() && probe_opens == opens); /* no immediate retry writes flash */
    assert(verify());
    seen = current();
    assert(seen.recoveries == 5 && !seen.persistence_probe_error && !seen.last_error);
    assert(seen.recovered_uptime_ms == now_us / 1000);

    /* However often faults or requests restart verification, probes write
     * flash at most once per interval. */
    assert(qs_request_verification());
    audit_all();
    opens = probe_opens;
    assert(!qs_verify_persistence() && probe_opens == opens);
    now_us += QS_PROBE_INTERVAL_US - 1;
    assert(!qs_verify_persistence() && probe_opens == opens);
    now_us += 1;
    assert(qs_verify_persistence() && probe_opens == opens + 2);

    /* An outside request is covered only by a pass that started after it,
     * and request numbers skip 0 when they wrap. */
    request = qs_request_verification();
    assert(request && current().verified_through < request && !verify());
    audit_all();
    assert(verify() && current().verified_through == request);
    health.verification_requests = UINT32_MAX;
    assert(qs_request_verification() == 1);
    puts("queue recovery host tests passed");
    return 0;
}
