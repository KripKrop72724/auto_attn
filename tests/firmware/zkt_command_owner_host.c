#include "zkt_catalog_client.h"
#include "cJSON.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define pdMS_TO_TICKS(value) (value)
#define pdTRUE 1
#define LED_STATUS_LOCAL_FAILURE 1
#define QS_ADMIT_RECOVERY 2
#define ESP_LOGW(...) ((void)0)
#define ADD_COMMAND_LINE_BYTES 12288
#define ADD_COMMAND_INBOX_MAX_BYTES 65536
#define ADD_COMMAND_INBOX_PATH "active"
#define ADD_COMMAND_INBOX_TMP_PATH "commit"
#define ADD_COMMAND_INBOX_BACKUP_PATH "backup"
#define ADD_TRACKED_COMMAND_CAPACITY 16
typedef int nvs_handle_t;
typedef int esp_err_t;
typedef void *QueueHandle_t;
typedef struct { char command_id[80], command_type[40]; } add_command_t;
static void *s_command_lock = (void *)1;
static QueueHandle_t s_commands = (void *)2, s_config_commands = (void *)3;
static char s_running_command_id[80], s_queued_command_ids[16][80];
static size_t s_queued_command_count, queue_deliveries, config_deliveries;
static bool s_command_inbox_restored, held, queue_full;
static uint64_t now = 1, ticket;
static zc_store_t store;
static ft_checkpoint_t checkpoint;
static zj_request_t accepted;
static bool refuse, stall_activation, activation_started, fail_commit;
static size_t allocations, fail_at;
static void *allocate(size_t bytes)
{ if (++allocations == fail_at) return NULL; return malloc(bytes); }
#define malloc allocate
static char *copy(const char *plain)
{
    if (!plain) return NULL;
    char *out = malloc(strlen(plain) + 1);
    if (out) strcpy(out, plain);
    return out;
}
/* Crypto is independently qualified. These stubs keep allocation and
 * decrypted-line ownership identical while making fixture bytes inspectable. */
static char *encrypt_storage_json(const char *plain) { return copy(plain); }
static char *decrypt_storage_line(const char *line) { return copy(line); }
static int64_t esp_timer_get_time(void) { return (int64_t)now; }
static bool catalog_owner_required(void) { return true; }
static int xSemaphoreTake(void *lock, int timeout)
{ (void)timeout; assert(lock == s_command_lock && !held); held = true; return pdTRUE; }
static void xSemaphoreGive(void *lock) { assert(lock == s_command_lock && held); held = false; }
static int xQueueSend(QueueHandle_t queue, const add_command_t *command, int timeout)
{
    assert(held && command->command_id[0] && !timeout);
    if (queue_full) return 0;
    if (queue == s_config_commands) ++config_deliveries;
    else { assert(queue == s_commands); ++queue_deliveries; }
    return pdTRUE;
}
static size_t bounded_copy(char *out, const char *text, size_t capacity)
{
    size_t length = strlen(text);
    if (capacity) { size_t n = length < capacity ? length : capacity - 1; memcpy(out, text, n); out[n] = 0; }
    return length;
}
#define strlcpy bounded_copy
static bool parse_command_object(cJSON *root, add_command_t *command)
{
    cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
    cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "command_type");
    if (!cJSON_IsString(id) || !cJSON_IsString(type)) return false;
    bounded_copy(command->command_id, id->valuestring, sizeof(command->command_id));
    bounded_copy(command->command_type, type->valuestring, sizeof(command->command_type));
    return true;
}
/* Any use of the former caller-owned NVS/budget path is a regression. */
static void led_status_fault(int state) { (void)state; assert(false); }
static bool qs_local_begin(int policy, size_t bytes)
{ (void)policy; (void)bytes; assert(false); return false; }
static void qs_local_end(bool ok, int error) { (void)ok; (void)error; assert(false); }
FILE *rel_open_append(const char *path) { (void)path; assert(false); return NULL; }
static int nvs_open(const char *name, int mode, int *handle)
{ (void)name; (void)mode; (void)handle; assert(false); return -1; }
static void nvs_close(int handle) { (void)handle; assert(false); }
static int nvs_get_blob(int h, const char *name, void *out, size_t *size)
{ (void)h; (void)name; (void)out; (void)size; assert(false); return -1; }
static int nvs_set_blob(int h, const char *name, const void *in, size_t size)
{ (void)h; (void)name; (void)in; (void)size; assert(false); return -1; }
static int nvs_commit(int handle) { (void)handle; assert(false); return -1; }
static uint64_t time_port(void *context) { (void)context; return now; }
static void wait_port(void *context) { (void)context; now += 100000; }
static bool submit(void *context, const zj_request_t *request, uint64_t *id)
{
    (void)context;
    assert(held && request->operation == ZJ_COMMANDS);
    if (refuse) return false;
    assert(!ticket); accepted = *request; *id = ++ticket; activation_started = false;
    return true;
}
static bool poll(void *context, uint64_t id, zj_reply_t *reply, bool *complete)
{
    (void)context;
    assert(held && id == ticket);
    *complete = false;
    if (stall_activation && activation_started) return true;
    memset(reply, 0, sizeof(*reply));
    bool pending = zc_store_step(&store, ticket, now, &accepted.input.catalog, &reply->catalog, &reply->result);
    if (accepted.input.catalog.operation == ZC_ACTIVATE) activation_started = pending;
    if (!pending) { *complete = true; ticket = 0; }
    return true;
}
static const zc_client_port_t catalog_client_port = {
    .now_us = time_port, .wait = wait_port, .submit = submit, .poll = poll};
#include "command_actual.inc"

static int load(void *context, ft_checkpoint_t *out)
{ (void)context; *out = checkpoint; return checkpoint.version ? 1 : 0; }
static bool commit(void *context, const ft_checkpoint_t *value)
{ (void)context; if (fail_commit) return false; checkpoint = *value; return true; }
static const char *rows = "{\"command_id\":\"A\",\"command_type\":\"READ\"}\n"
    "{\"command_id\":\"B\",\"command_type\":\"APPLY_CONFIG\"}\n";
static const char *only_b = "{\"command_id\":\"B\",\"command_type\":\"APPLY_CONFIG\"}\n";
static void seed(const char *bytes)
{
    assert(!held);
    for (unsigned i = 0; i < 5; ++i) unlink((const char *[]) {"active", "commit", "backup", "producer", "spare"}[i]);
    if (bytes) { FILE *f = fopen("active", "wb"); assert(f && fputs(bytes, f) >= 0 && !fclose(f)); }
    checkpoint = (ft_checkpoint_t){0}; s_command_storage_client = (zc_client_t){.commands = true};
    now = 1; ticket = 0; refuse = stall_activation = activation_started = fail_commit = false;
    queue_full = s_command_inbox_restored = false; s_running_command_id[0] = 0;
    s_queued_command_count = queue_deliveries = config_deliveries = 0;
    memset(s_queued_command_ids, 0, sizeof(s_queued_command_ids));
    assert(zc_store_init(&store, "active", "commit", "backup", "producer", "spare", (ft_port_t){load, commit, NULL}));
    store.limit = ZC_COMMAND_LIMIT_BYTES; store.allow_first_recovery = false;
}
static bool contents(const char *expected)
{
    FILE *f = fopen("active", "rb"); assert(f);
    size_t index = 0; int byte;
    while ((byte = fgetc(f)) != EOF) { if (!expected[index] || byte != expected[index++]) { fclose(f); return false; } }
    assert(!ferror(f) && !fclose(f)); return !expected[index];
}
static int contains(const char *id)
{ held = true; int found = command_journal_contains_locked(id); held = false; return found; }
int main(void)
{
    cJSON_Hooks hooks = {allocate, free}; cJSON_InitHooks(&hooks);
    seed(rows); strcpy(s_running_command_id, "A"); allocations = 0;
    assert(add_connector_command_complete("A") && contents(only_b) && !s_running_command_id[0]);
    size_t total = allocations;
    for (size_t fault = 1; fault <= total; ++fault) {
        seed(rows); strcpy(s_running_command_id, "A"); allocations = 0; fail_at = fault;
        assert(!add_connector_command_complete("A") && contents(rows) && !strcmp(s_running_command_id, "A"));
        assert(!held); fail_at = 0;
        assert(add_connector_command_complete("A") && contents(only_b));
    }
    cJSON *new_command = cJSON_Parse("{\"command_id\":\"C\",\"command_type\":\"READ\"}"); assert(new_command);
    seed(rows); allocations = 0; assert(command_journal_append(new_command, "C")); total = allocations;
    assert(contains("C") == 1);
    for (size_t fault = 1; fault <= total; ++fault) {
        seed(rows); allocations = 0; fail_at = fault;
        assert(!command_journal_append(new_command, "C") && contents(rows) && !held); fail_at = 0;
    }
    seed(rows); refuse = true;
    assert(!command_journal_append(new_command, "C") && !add_connector_command_complete("A") && contents(rows));
    refuse = false; fail_commit = true;
    assert(!add_connector_command_complete("A") && contents(rows)); fail_commit = false;
    assert(add_connector_command_complete("A") && contents(only_b));
    seed(rows); assert(command_journal_append(new_command, "B") && contents(rows)); /* stable replay */
    assert(add_connector_command_complete("absent") && contents(rows));

    seed(rows); strcpy(s_running_command_id, "A"); stall_activation = true;
    assert(!add_connector_command_complete("A") && contents(rows) && s_command_storage_client.pending_ticket);
    assert(!strcmp(s_running_command_id, "A"));
    s_command_inbox_restored = true;
    restore_command_inbox(); assert(s_command_storage_client.pending_ticket && s_command_inbox_restored);
    stall_activation = false;
    for (unsigned i = 0; i < 100 && s_command_storage_client.pending_ticket; ++i) restore_command_inbox();
    assert(!s_command_storage_client.pending_ticket && contents(only_b) && s_command_inbox_restored);
    assert(config_deliveries == 1 && !queue_deliveries);
    assert(add_connector_command_complete("A") && !s_running_command_id[0]);

    seed(rows); queue_full = true; restore_command_inbox(); assert(!s_command_inbox_restored && !s_queued_command_count);
    queue_full = false; restore_command_inbox();
    assert(s_command_inbox_restored && s_queued_command_count == 2 && queue_deliveries == 1 && config_deliveries == 1);
    restore_command_inbox(); assert(queue_deliveries == 1 && config_deliveries == 1);
    for (const char **bad = (const char *[]) {"{\"command_id\":\"A\"}", "not-json\n", "{}\n", NULL}; *bad; ++bad) {
        seed(*bad); assert(!command_journal_append(new_command, "C") && !add_connector_command_complete("A") && contents(*bad));
        restore_command_inbox(); assert(!s_command_inbox_restored);
    }
    seed(NULL); assert(command_journal_append(new_command, "C") && contains("C") == 1);
    char payload[1201]; memset(payload, 'x', 1200); payload[1200] = 0;
    assert(cJSON_AddStringToObject(new_command, "payload", payload));
    seed(rows); assert(command_journal_append(new_command, "C") && contains("C") == 1);
    assert(add_connector_command_complete("A") && contains("C") == 1 && contains("A") == 0);
    restore_command_inbox(); assert(s_command_inbox_restored && config_deliveries == 1 && queue_deliveries == 1);

    /* A complete maximum-size inbox survives a refused replacement. */
    seed(NULL); FILE *f = fopen("active", "wb"); assert(f);
    char row[256]; memset(row, ' ', sizeof(row)); memcpy(row, only_b, strlen(only_b) - 1); row[255] = '\n';
    for (unsigned i = 0; i < 256; ++i) assert(fwrite(row, 1, sizeof(row), f) == sizeof(row));
    assert(!fclose(f)); assert(!command_journal_append(new_command, "C"));
    struct stat st; assert(!stat("active", &st) && st.st_size == ADD_COMMAND_INBOX_MAX_BYTES && contains("C") == 0);
    cJSON_Delete(new_command);
    puts("command owner allocation, replay, recovery, bounded streaming and late activation checks passed");
}
