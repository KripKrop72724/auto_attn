#include "add_connector.h"
#include "zkt_command_ids.h"
#include <assert.h>
#include <stdio.h>

static rel_id_result_t status;
static bool managed, saved;
static unsigned owner_calls, legacy_calls;
#if !ZONE_LITE_HIKVISION
static bool zj_runtime_checkpoint_required(void) { return managed; }
static bool catalog_owner_required(void) { return managed; }
rel_id_result_t zi_cache_contains(zi_kind_t kind, const char *id)
{ assert(kind <= ZI_CANCELLED && !strcmp(id, "A")); ++owner_calls; return status; }
bool zi_cache_remember(zi_kind_t kind, const char *id)
{ assert(kind <= ZI_CANCELLED && !strcmp(id, "A")); ++owner_calls; return saved; }
#endif
rel_id_result_t rel_id_file_contains(const char *path, const char *id, size_t maximum)
{
    assert((!strcmp(path, ZI_PROCESSED_PATH) || !strcmp(path, ZI_CANCELLED_PATH)) && !strcmp(id, "A"));
    assert(maximum == ZI_ID_BYTES || maximum == sizeof(((add_command_t *)0)->command_id));
    ++legacy_calls; return status;
}
bool rel_append_bounded_id(const char *path, const char *id, size_t capacity, size_t maximum)
{
    assert((!strcmp(path, ZI_PROCESSED_PATH) || !strcmp(path, ZI_CANCELLED_PATH)) && !strcmp(id, "A"));
    assert(capacity == ZI_LIMIT_BYTES && (maximum == ZI_ID_BYTES || maximum == sizeof(((add_command_t *)0)->command_id)));
    ++legacy_calls; return saved;
}
#include "command_id_actual.inc"
int main(void)
{
    assert(!strcmp(PROCESSED_COMMANDS_PATH, ZI_PROCESSED_PATH));
    assert(!strcmp(CANCELLED_COMMANDS_PATH, ZI_CANCELLED_PATH) && !strcmp(ADD_CANCELLED_COMMANDS_PATH, ZI_CANCELLED_PATH));
    assert(COMMAND_ID_MAX_BYTES == ZI_ID_BYTES && COMMAND_RECEIPT_CACHE_BYTES == ZI_LIMIT_BYTES &&
        ADD_COMMAND_RECEIPT_CACHE_BYTES == ZI_LIMIT_BYTES);
    for (unsigned mode = 0; mode < 2; ++mode) {
        managed = mode; owner_calls = legacy_calls = 0;
        status = REL_ID_ERROR; saved = false;
        assert(command_was_processed("A") == REL_ID_ERROR && command_was_cancelled("A") == REL_ID_ERROR);
        assert(!mark_command_processed("A") && !append_cancelled_command("A"));
        status = REL_ID_ABSENT;
        assert(command_was_processed("A") == REL_ID_ABSENT && command_was_cancelled("A") == REL_ID_ABSENT);
        assert(!mark_command_processed("A") && !append_cancelled_command("A"));
        saved = true;
        assert(mark_command_processed("A") && append_cancelled_command("A"));
        status = REL_ID_PRESENT;
        assert(command_was_processed("A") == REL_ID_PRESENT && command_was_cancelled("A") == REL_ID_PRESENT);
        assert(mark_command_processed("A") && append_cancelled_command("A"));
        if (managed && !ZONE_LITE_HIKVISION) assert(owner_calls && !legacy_calls);
        else assert(legacy_calls && !owner_calls);
    }
#if !ZONE_LITE_HIKVISION
    char oversized[49]; memset(oversized, 'x', sizeof(oversized) - 1); oversized[48] = 0;
    unsigned before = owner_calls;
    assert(!append_cancelled_command(NULL) && !append_cancelled_command(oversized) && owner_calls == before);
#endif
    puts("production command receipt family routing and failure holds passed");
}
