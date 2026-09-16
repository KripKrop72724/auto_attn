/* The Python harness inserts the actual production drain function below. */
#include "legacy_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define MAX_EVENT_JSON 1024
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define PENDING_PATH "pending.jsonl"
#define BLOCKED_PATH "blocked.jsonl"
#define CORRUPT_ORDS_PATH "corrupt.jsonl"
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
static int storage_lock, gate_lock;
static int *g_storage_lock=&storage_lock, *g_ords_outbox_gate=&gate_lock;
static int64_t g_ords_drain_retry_not_before_ms;
static legacy_queue_t g_legacy_pending;
static char (*g_legacy_drain_buffer)[MAX_EVENT_JSON];
static bool g_legacy_probe_head;
static bool fail_allocate, fail_receipt, fail_commit, append_during_send, fail_bulk;
static lq_checkpoint_t durable;
static bool exists;
static unsigned requests, accepted, faults;
typedef enum {ORACLE_DELIVERY_RETRYABLE, ORACLE_DELIVERY_ACKED,
    ORACLE_DELIVERY_PERMANENT_REJECTION, ORACLE_DELIVERY_CORRUPT_LOCAL_ROW} oracle_delivery_result_t;
static void *heap_caps_malloc(size_t n,int flags)
{ (void)flags; return fail_allocate?NULL:malloc(n); }
static int64_t uptime_ms(void) { return 1000; }
static bool truth_ords_gate_priority_active(int64_t now) { (void)now;return false; }
static bool ords_send_allowed(void) { return true; }
static int xSemaphoreTake(int *lock,unsigned timeout)
{ (void)timeout;assert(!*lock);*lock=1;return pdTRUE; }
static void xSemaphoreGive(int *lock) { assert(*lock);*lock=0; }
static void led_status_set_backlog(bool pending) { (void)pending; }
static void ords_drain_preserved_deferred(const char *stage,int error)
{ assert(!storage_lock);(void)stage;(void)error;faults++; }
static int legacy_pending_load(void *context,lq_checkpoint_t *cp)
{ (void)context;*cp=durable;return exists?1:0; }
static bool legacy_pending_commit(void *context,const lq_checkpoint_t *cp)
{ (void)context;assert(storage_lock);if(fail_commit)return false;durable=*cp;exists=true;return true; }
static bool append_line(const char *path,const char *line)
{ FILE *f=fopen(path,"a");assert(f);assert(fprintf(f,"%s\n",line)>0);return fclose(f)==0; }
static void request(size_t count)
{
    assert(!storage_lock && gate_lock);requests++;accepted+=(unsigned)count;
    if(append_during_send) { append_during_send=false;assert(append_line(PENDING_PATH,"arrived-live")); }
}
static oracle_delivery_result_t oracle_send_live(const char *event)
{ assert(event[0]);request(1);return ORACLE_DELIVERY_ACKED; }
static bool oracle_send_bulk(char **events,size_t count)
{ assert(events[0][0] && count<=100);request(count);return !fail_bulk; }
static bool add_enqueue_json_receipts(char *const *events,size_t count,const char *path)
{ assert(!storage_lock && events[0][0] && count<=100 && path[0]);return !fail_receipt; }
static char *oracle_mark_permanent_rejection(const char *line) { (void)line;return NULL; }

/* INSERT_PRODUCTION_DRAIN */

int main(void)
{
    fail_allocate=true;oracle_drain_pending(true);assert(requests==0 && faults==1);
    fail_allocate=false;
    for(unsigned i=0;i<101;i++) assert(append_line(PENDING_PATH,"record"));
    fail_receipt=true;
    oracle_drain_pending(true);
    assert(requests==1 && accepted==100 && g_legacy_pending.checkpoint.offset==0);
    fail_receipt=false;append_during_send=true;
    oracle_drain_pending(true);
    assert(requests==2 && accepted==200 && g_legacy_pending.checkpoint.offset==700);
    // Restart with two records still pending, including the concurrent append.
    g_legacy_pending.ready=false;
    fail_commit=true;oracle_drain_pending(true);
    assert(requests==3 && durable.offset==700 && !g_legacy_pending.ready);
    fail_commit=false;oracle_drain_pending(true);
    assert(requests==4 && durable.offset==0);
    FILE *f=fopen(PENDING_PATH,"r");assert(!f);
    assert(append_line(PENDING_PATH,"new-1"));assert(append_line(PENDING_PATH,"new-2"));
    fail_bulk=true;oracle_drain_pending(true);
    assert(g_legacy_probe_head && durable.offset==0);
    unsigned prior=requests;oracle_drain_pending(true);
    assert(requests==prior+1 && durable.offset==6 && !g_legacy_probe_head);
    fail_bulk=false;oracle_drain_pending(true);
    assert(durable.offset==0 && !storage_lock && !gate_lock);
    free(g_legacy_drain_buffer);
    puts("legacy drain integration regressions passed");
}
