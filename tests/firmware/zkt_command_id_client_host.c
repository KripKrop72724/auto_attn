#include "zkt_storage_owner_platform.h"
#include "zkt_command_ids.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

static zi_store_t store;
static uint64_t now = 1, ticket, submissions;
static bool required = true, refuse, stall, lost_poll, nested, started_stall;
static zj_request_t accepted;
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned milliseconds) { now += (uint64_t)milliseconds * 1000; }
bool zj_runtime_checkpoint_required(void) { return required; }
static bool admit(void *context, size_t bytes) { (void)context; return bytes > 512; }
bool zj_owner_submit(const zj_request_t *request, uint64_t *id)
{
    *id = 0; assert(request->operation == ZJ_COMMAND_IDS && !ticket);
    if (refuse) return false;
    assert(zi_request_valid(&request->input.command_ids));
    accepted = *request; *id = ticket = ++submissions; return true;
}
bool zj_owner_poll(uint64_t id, zj_reply_t *reply, bool *complete)
{
    assert(id && id == ticket); *complete = false;
    if (nested) { nested = false; assert(zi_cache_contains(ZI_PROCESSED, "nested") == REL_ID_ERROR); }
    if (lost_poll) return false;
    if (stall) return true;
    memset(reply, 0, sizeof(*reply));
    bool pending = zi_store_step(&store, id, now, &accepted.input.command_ids, &reply->command_ids, &reply->result);
    if (pending && started_stall) { started_stall = false; stall = true; }
    if (!pending) { *complete = true; ticket = 0; }
    return true;
}
static off_t size(const char *path) { struct stat st; return stat(path, &st) == 0 ? st.st_size : -1; }
int main(void)
{
    assert(zi_store_init(&store, "processed", "cancelled", admit, NULL));
    assert(zi_cache_contains(ZI_PROCESSED, "A") == REL_ID_ABSENT);
    nested = true; assert(zi_cache_remember(ZI_PROCESSED, "A"));
    assert(zi_cache_contains(ZI_PROCESSED, "A") == REL_ID_PRESENT && size("processed") == 2);
    assert(zi_cache_remember(ZI_PROCESSED, "A") && size("processed") == 2);
    assert(zi_cache_contains(ZI_CANCELLED, "A") == REL_ID_ABSENT);
    refuse = true; assert(!zi_cache_remember(ZI_CANCELLED, "B") && size("cancelled") == -1); refuse = false;
    stall = true; assert(!zi_cache_remember(ZI_CANCELLED, "B") && ticket);
    uint64_t before = submissions;
    lost_poll = true; assert(zi_cache_contains(ZI_CANCELLED, "C") == REL_ID_ERROR && submissions == before);
    lost_poll = stall = false;
    /* The expired unstarted B request is collected; it cannot answer C. */
    assert(zi_cache_contains(ZI_CANCELLED, "C") == REL_ID_ABSENT && !ticket && size("cancelled") == -1);
    assert(zi_cache_remember(ZI_CANCELLED, "B") && size("cancelled") == 2);
    assert(zi_cache_contains(ZI_CANCELLED, "B") == REL_ID_PRESENT);
    FILE *file = fopen("processed", "wb"); assert(file);
    for (unsigned i = 0; i < 2000; ++i) assert(fputs("old-command\n", file) >= 0);
    assert(!fclose(file)); off_t prior = size("processed");
    started_stall = true; assert(!zi_cache_remember(ZI_PROCESSED, "late") && ticket && store.work_ticket);
    stall = false;
    assert(zi_cache_contains(ZI_PROCESSED, "another") == REL_ID_ABSENT && !ticket);
    assert(zi_cache_contains(ZI_PROCESSED, "late") == REL_ID_PRESENT && size("processed") == prior + 5);
    assert(zi_cache_remember(ZI_PROCESSED, "late") && size("processed") == prior + 5);
    required = false; before = submissions;
    assert(zi_cache_contains(ZI_PROCESSED, "A") == REL_ID_ERROR && !zi_cache_remember(ZI_PROCESSED, "A"));
    required = true;
    assert(zi_cache_contains((zi_kind_t)-1, "A") == REL_ID_ERROR && !zi_cache_remember(ZI_CANCELLED, "bad\nID"));
    assert(!zi_cache_remember(ZI_CANCELLED, NULL) && !zi_cache_remember(ZI_CANCELLED, ""));
    now = UINT64_MAX - 1; assert(zi_cache_contains(ZI_PROCESSED, "A") == REL_ID_ERROR && submissions == before);
    puts("retained command receipt deadlines, concurrent calls and exact replay passed");
}
