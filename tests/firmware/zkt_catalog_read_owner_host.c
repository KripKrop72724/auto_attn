#include "add_connector.h"
#include "zkt_catalog_client.h"
#include "cJSON.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>
#define ADD_IDENTITY_CATALOG_PATH "active"
#define ADD_IDENTITY_CATALOG_MAX_BYTES (2U * 1024U * 1024U)
#define ADD_IDENTITY_CATALOG_MAX_ROWS 4096
#define ADD_COMMAND_LINE_BYTES 12288
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
static int catalog_lock, identity_lock;
static int *s_catalog_lock = &catalog_lock, *s_lock = &identity_lock;
static bool s_identity_catalog_active_memory_valid;
typedef struct { char user_id[32], uid[32], display_name[32], cnic[32]; bool shift_worker; } add_identity_alias_t;
static add_identity_alias_t s_identity_catalog_active_aliases[1];
static size_t s_identity_catalog_active_alias_rows, s_identity_catalog_rows;
static uint32_t s_identity_catalog_generation;
static const char *s_catalog_writer_failure_reason;
static unsigned persists, reads, fail_read, change_revision, stall_read;
static bool refuse;
static uint64_t now = 1, ticket;
static zj_request_t accepted;
static zc_store_t store;
static zc_client_t s_catalog_client;
static ft_checkpoint_t checkpoint;
static size_t allocations, fail_at;
static void *allocate(size_t bytes) { if (++allocations == fail_at) return NULL; return malloc(bytes); }
#define malloc allocate
static char *decrypt_storage_line(const char *line)
{ char *out = malloc(strlen(line) + 1); if (out) strcpy(out, line); return out; }
static int xSemaphoreTake(int *lock, unsigned timeout)
{ (void)timeout; assert(!*lock); *lock = 1; return 1; }
static void xSemaphoreGive(int *lock) { assert(*lock); *lock = 0; }
static size_t copy_text(char *out, const char *in, size_t capacity)
{ size_t n = strlen(in); if (capacity) { size_t k = n < capacity - 1 ? n : capacity - 1; memcpy(out, in, k); out[k] = 0; } return n; }
#define strlcpy copy_text
static bool catalog_owner_required(void) { return true; }
static int64_t esp_timer_get_time(void) { return (int64_t)now; }
static uint64_t time_port(void *context) { (void)context; return now; }
static void wait_port(void *context) { (void)context; now += 100000; }
static bool submit(void *context, const zj_request_t *request, uint64_t *id)
{
    (void)context; assert(catalog_lock && request->operation == ZJ_CATALOG);
    if (refuse) return false;
    assert(!ticket); accepted = *request; *id = ++ticket;
    if (request->input.catalog.operation == ZC_READ) ++reads;
    return true;
}
static bool poll(void *context, uint64_t id, zj_reply_t *reply, bool *complete)
{
    (void)context; assert(catalog_lock && ticket == id); *complete = false;
    if (accepted.input.catalog.operation == ZC_READ && stall_read == reads) return true;
    memset(reply, 0, sizeof(*reply));
    if (accepted.input.catalog.operation == ZC_READ && change_revision == reads) { ++store.revision; change_revision = 0; }
    if (!zc_store_step(&store, ticket, now, &accepted.input.catalog, &reply->catalog, &reply->result)) {
        if (accepted.input.catalog.operation == ZC_READ && fail_read == reads) reply->result = ZJ_IO;
        *complete = true; ticket = 0;
    }
    return true;
}
static const zc_client_port_t catalog_client_port = {time_port, wait_port, submit, poll, NULL};
static void catalog_owner_invalidate_memory(void)
{ if (s_catalog_client.active_may_have_changed) { s_identity_catalog_active_memory_valid = false; s_catalog_client.active_may_have_changed = false; } }
static bool catalog_owner_idle(void)
{ bool idle = zc_client_drain(&s_catalog_client, catalog_client_port); catalog_owner_invalidate_memory(); return idle; }
static bool recover_catalog_transaction_locked(void)
{
    assert(catalog_lock); zc_reply_t reply;
    zc_request_t request = {.operation = ZC_RECOVER, .deadline_us = now + 30000000};
    zj_result_t result = zc_client_call(&s_catalog_client, catalog_client_port, &request, &reply);
    catalog_owner_invalidate_memory(); return result == ZJ_OK;
}
static bool persist_identity_catalog_locked(cJSON *root, size_t *count)
{ (void)count; assert(catalog_lock && cJSON_IsArray(cJSON_GetObjectItemCaseSensitive(root, "rows"))); ++persists; return true; }
static FILE *forbidden_fopen(const char *path, const char *mode)
{ (void)path; (void)mode; assert(false && "catalog file opened outside owner"); return NULL; }
#define fopen forbidden_fopen
#include "catalog_read_actual.inc"
#undef fopen
static int load(void *context, ft_checkpoint_t *out) { (void)context; *out = checkpoint; return checkpoint.version ? 1 : 0; }
static bool commit(void *context, const ft_checkpoint_t *value) { (void)context; checkpoint = *value; return true; }
static void seed(const char *text)
{
    assert(!catalog_lock && !identity_lock);
    unlink("active"); unlink("commit"); unlink("backup"); unlink("producer"); unlink("spare");
    if (text) { FILE *file = fopen("active", "wb"); assert(file && fputs(text, file) >= 0 && !fclose(file)); }
    checkpoint = (ft_checkpoint_t){0}; s_catalog_client = (zc_client_t){0};
    assert(zc_store_init(&store, "active", "commit", "backup", "producer", "spare", (ft_port_t){load, commit, NULL}));
    now = 1; ticket = 0; reads = fail_read = change_revision = stall_read = persists = 0;
    refuse = s_identity_catalog_active_memory_valid = false;
    s_identity_catalog_rows = 999; s_identity_catalog_generation = 0;
}
static char catalog[4096];
static void make_catalog(void)
{
    strcpy(catalog, "{\"type\":\"identity_catalog\",\"rows_count\":10}\n");
    for (unsigned i = 0; i < 10; ++i) {
        size_t n = strlen(catalog);
        int used = snprintf(catalog + n, sizeof(catalog) - n,
            "{\"user_id\":\"user%u\",\"uid\":\"%u\",\"display_name\":\"Synthetic User %u\",\"cnic\":\"synthetic%u\"}\n", i, i, i, i);
        assert(used > 0 && (size_t)used < sizeof(catalog) - n);
    }
    assert(strlen(catalog) > ZC_CHUNK_BYTES);
}
static bool restore(void)
{ catalog_lock = 1; bool ok = restore_valid_identity_catalog_locked(); catalog_lock = 0; return ok; }
static bool lookup(void)
{
    char name[64] = "stale", identity[32] = "stale"; bool shift = true;
    bool ok = add_connector_lookup_identity("user0", "0", name, sizeof(name), identity, sizeof(identity), &shift);
    if (ok) assert(!strcmp(name, "Synthetic User 0") && !strcmp(identity, "synthetic0") && !shift);
    else assert(!name[0] && !identity[0] && !shift);
    return ok;
}
int main(void)
{
    cJSON_Hooks hooks = {allocate, free}; cJSON_InitHooks(&hooks); make_catalog();
    seed(catalog); assert(restore() && s_identity_catalog_rows == 10 && s_identity_catalog_generation == 1);
    assert(lookup() && reads > 1);
    seed(catalog); allocations = 0; assert(lookup()); size_t total = allocations;
    for (size_t fault = 1; fault <= total; ++fault) { seed(catalog); allocations = 0; fail_at = fault; assert(!lookup()); fail_at = 0; }
    seed(catalog); fail_read = 2; assert(!lookup() && reads == 2);
    fail_read = 0; assert(lookup());
    seed(catalog); change_revision = 2; assert(!lookup()); assert(lookup());
    seed(catalog); stall_read = 2; assert(!lookup() && s_catalog_client.pending_ticket);
    stall_read = 0; assert(lookup() && !s_catalog_client.pending_ticket);
    seed(catalog); refuse = true; assert(!lookup() && !restore()); refuse = false; assert(restore());
    seed(catalog); allocations = 0; assert(restore()); total = allocations;
    for (size_t fault = 1; fault <= total; ++fault) {
        seed(catalog); allocations = 0; fail_at = fault; assert(!restore()); fail_at = 0;
        assert(!s_identity_catalog_generation && s_identity_catalog_rows == 999);
    }
    add_command_t command = {.has_tombstone = true, .user_id = "missing", .uid = "new",
        .tombstone_display_name = "Synthetic", .tombstone_cnic = "synthetic"};
    seed(catalog); allocations = 0; assert(add_connector_persist_command_tombstone(&command)); total = allocations;
    for (size_t fault = 1; fault <= total; ++fault) {
        seed(catalog); allocations = 0; fail_at = fault; assert(!add_connector_persist_command_tombstone(&command) && !persists); fail_at = 0;
    }
    seed(NULL); assert(add_connector_persist_command_tombstone(&command) && persists == 1);
    seed(NULL); refuse = true; assert(!add_connector_persist_command_tombstone(&command) && !persists);
    for (const char **bad = (const char *[]) {"", "{}\n", "{\"rows_count\":2}\n{}\n", "{\"rows_count\":0}\n{}\n",
            "{\"rows_count\":1}\n{}", "{\"type\":\"identity_catalog\",\"rows\":[]}\nextra", NULL}; *bad; ++bad) {
        seed(*bad); assert(!restore() && !lookup() && !add_connector_persist_command_tombstone(&command) && !persists);
    }
    puts("owned catalog restore, complete lookup, tombstone, allocation and stale-stream checks passed");
}
