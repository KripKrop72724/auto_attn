#include "zkt_add_legacy_owner.h"
#include "zkt_storage_owner.h"
#include "reliability.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define ZONE_LITE_QUEUE_OWNER 1
#define ADD_OUTBOX_LINE_BYTES 8192U
#define LED_STATUS_LOCAL_FAILURE 1
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
typedef int SemaphoreHandle_t;
typedef int esp_err_t;
typedef unsigned nvs_handle_t;
static bool required = true, owner_task, admission_full, reject;
static lf_state_t legacy_health;
static qs_health_t health;
#define failed_writes health.write_failures
#include "legacy_health_actual.inc"
static unsigned held, budget, peeks, stats, faults;
static int io_fault, nvs_fault;
enum { SHORT_WRITE = 1, FLUSH, SYNC, CLOSE, RENAME, STAT, RESTORE };
static lq_checkpoint_t committed[2], pending_checkpoint;
static unsigned pending_lane;
static bool zj_runtime_checkpoint_required(void) { return required; }
bool zj_owner_is_current_task(void) { return owner_task; }
static int xSemaphoreTake(SemaphoreHandle_t lock, unsigned wait)
{ assert(lock && wait == 100 && !held); held = (unsigned)lock; return pdTRUE; }
static void xSemaphoreGive(SemaphoreHandle_t lock) { assert(held == (unsigned)lock && !budget); held = 0; }
bool qs_local_read_begin(void) { assert(held && !budget); budget = 1; return true; }
bool qs_local_admit_locked(qs_admission_t policy, size_t bytes)
{ assert(budget && (unsigned)policy <= QS_ADMIT_RECOVERY && bytes); if (admission_full) errno = ENOSPC; return !admission_full; }
void qs_local_end(bool ok, int error) { assert(budget); if (!ok) { assert(error); ++failed_writes; } budget = 0; }
static void led_status_fault(int code) { assert(code == LED_STATUS_LOCAL_FAILURE); ++faults; }
static esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *handle)
{ assert(owner_task && budget && !strcmp(name, "add_legacy")); *handle = (unsigned)mode + 1; return nvs_fault == 1 ? 9 : ESP_OK; }
static void nvs_close(nvs_handle_t handle) { assert(handle); }
static esp_err_t nvs_get_blob(nvs_handle_t handle, const char *key, void *out, size_t *length)
{
    assert(handle == 1 && *length == sizeof(lq_checkpoint_t));
    unsigned lane = !strcmp(key, "live") ? 0 : 1;
    if (!committed[lane].version) return ESP_ERR_NVS_NOT_FOUND;
    memcpy(out, &committed[lane], *length); return ESP_OK;
}
static esp_err_t nvs_set_blob(nvs_handle_t handle, const char *key, const void *in, size_t length)
{
    assert(handle == 2 && length == sizeof(lq_checkpoint_t));
    pending_lane = !strcmp(key, "live") ? 0 : 1;
    memcpy(&pending_checkpoint, in, length); return ESP_OK;
}
static esp_err_t nvs_commit(nvs_handle_t handle)
{ assert(handle == 2); if (nvs_fault == 2) { nvs_fault = 0; return 9; } committed[pending_lane] = pending_checkpoint; return ESP_OK; }
#include "add_legacy_types_actual.inc"
static add_outbox_t s_live_outbox = {.path="live.jsonl", .tmp_path="live.tmp", .backup_path="live.bak",
    .cursor_path="live.pos", .cursor_tmp_path="live.pos.tmp", .max_bytes=65536, .label="live", .lock=1};
static add_outbox_t s_bulk_outbox = {.path="bulk.jsonl", .tmp_path="bulk.tmp", .backup_path="bulk.bak",
    .cursor_path="bulk.pos", .cursor_tmp_path="bulk.pos.tmp", .max_bytes=65536, .label="reconcile", .lock=2};
/* Include libc first, then interpose the actual adapter. This also works with
 * Linux fortified stdio, without renaming libc's inline implementations. */
static size_t fault_fwrite(const void *bytes, size_t size, size_t length, FILE *file)
{ if (io_fault == SHORT_WRITE) { io_fault=0; errno=ENOSPC; return fwrite(bytes,size,length/2,file); } return fwrite(bytes,size,length,file); }
static int fault_fflush(FILE *file)
{ if (io_fault == FLUSH) { io_fault=0; errno=EIO; return EOF; } return fflush(file); }
static int fault_fsync(int fd)
{ if (io_fault == SYNC) { io_fault=0; errno=EIO; return -1; } return fsync(fd); }
static int fault_fclose(FILE *file)
{ int result=fclose(file); if (io_fault == CLOSE) { io_fault=0; errno=EIO; return EOF; } return result; }
static int fault_rename(const char *from, const char *to)
{ if (io_fault == RENAME || (io_fault == RESTORE && strstr(from,".bak"))) {
    io_fault=0; errno=EIO; return -1; } return rename(from,to); }
static int fault_stat(const char *path, struct stat *out)
{ ++stats; if (io_fault == STAT) { io_fault=0; errno=EIO; return -1; } return stat(path,out); }
static dq_result_t traced_peek(legacy_queue_t *q, char *bytes, size_t capacity, lq_token_t *token)
{ ++peeks; return lq_peek(q,bytes,capacity,token); }
#define fwrite fault_fwrite
#define fflush fault_fflush
#define fsync fault_fsync
#define fclose fault_fclose
#define rename fault_rename
#define stat(...) fault_stat(__VA_ARGS__)
#define lq_peek traced_peek
#include "add_legacy_owner_actual.inc"
#undef fwrite
#undef fflush
#undef fsync
#undef fclose
#undef rename
#undef stat
#undef lq_peek

/* Segmented calls must never be selected by a legacy request. */
dq_result_t qs_append_with_policy(qs_lane_t lane,const void *bytes,size_t length,qs_admission_t policy)
{ (void)lane;(void)bytes;(void)length;(void)policy;assert(false);return DQ_IO; }
dq_result_t qs_peek(qs_lane_t lane,void *bytes,size_t capacity,size_t *length,dq_token_t *token)
{ (void)lane;(void)bytes;(void)capacity;(void)length;(void)token;assert(false);return DQ_IO; }
dq_result_t qs_settle(qs_lane_t lane,const dq_token_t *token)
{ (void)lane;(void)token;assert(false);return DQ_IO; }
bool qs_snapshot(qs_lane_t lane,uint32_t *depth) { (void)lane;(void)depth;assert(false);return false; }
bool qs_generation(char out[33]) { (void)out;assert(false);return false; }
bool qs_recover_step(void) { assert(false);return false; }
bool qs_verify_persistence(void) { assert(false);return false; }
static zq_store_t store = {.legacy={add_legacy_owner_append,add_legacy_owner_peek,add_legacy_owner_settle}};
static uint64_t now=1, next_ticket, ticket;
static zj_request_t request;
static zj_reply_t reply;
static bool executed, hold;
static int lose_operation=-1;
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned ms) { now+=(uint64_t)ms*1000; }
bool zj_owner_submit(const zj_request_t *input,uint64_t *out)
{
    *out=0;
    assert(!ticket && input->operation==ZJ_SEGMENTED_QUEUE && zq_request_valid(&input->input.segmented));
    if(reject)return false;
    request=*input;executed=false;*out=ticket=++next_ticket;return true;
}
bool zj_owner_poll(uint64_t input,zj_reply_t *out,bool *complete)
{
    assert(ticket && ticket==input);*complete=false;
    if(!executed) {
        owner_task=true;memset(&reply,0,sizeof(reply));
        zq_store_execute(&store,now,&request.input.segmented,&reply.segmented);
        reply.result=ZJ_OK;owner_task=false;executed=true;
        assert(!held && !budget);
        if(lose_operation==request.input.segmented.operation){hold=true;lose_operation=-1;}
    }
    if(hold)return true;
    *out=reply;*complete=true;ticket=0;return true;
}
static void write_bytes(const char *path,const void *bytes,size_t length)
{ FILE *f=fopen(path,"wb");assert(f && fwrite(bytes,1,length,f)==length && fclose(f)==0); }
static char copied[8192];
static size_t copied_length;
static lq_token_t token;
static dq_result_t peek(unsigned lane)
{ return zq_legacy_peek(lane,copied,sizeof(copied),&copied_length,&token); }
static void expect(unsigned lane,const char *text)
{ assert(peek(lane)==DQ_OK && copied_length==strlen(text) && !memcmp(copied,text,copied_length)); }
static void settle(unsigned lane,bool custody) { assert(zq_legacy_settle(lane,&token,custody)==DQ_OK); }
int main(void)
{
    /* Actual adapters reject a direct caller, even if the owner exists. */
    assert(add_legacy_owner_append(0,"x",1,QS_ADMIT_LIVE)==DQ_PENDING);
    required=false;owner_task=true;assert(add_legacy_owner_append(0,"x",1,QS_ADMIT_LIVE)==DQ_PENDING);
    required=true;owner_task=false;
    write_bytes("live.jsonl","same\nsame\n",10);write_bytes("live.bak","backup\n",7);
    write_bytes("live.tmp","staged\n",7);write_bytes("live.pos","999999\n",7);
    expect(0,"same\n");assert(!token.offset && !s_live_outbox.depth_known);
    lq_token_t first=token, wrong=token;wrong.crc^=1;
    assert(zq_legacy_settle(0,&wrong,false)==DQ_STALE);
    nvs_fault=2;assert(zq_legacy_settle(0,&token,false)==DQ_IO);
    assert(health.legacy.retire_faults==1 && !health.legacy.read_faults && health.write_failures==1);
    expect(0,"same\n");settle(0,false);
    assert(zq_legacy_settle(0,&first,false)==DQ_STALE);
    expect(0,"same\n");assert(token.offset==5);settle(0,false);
    expect(0,"backup\n");settle(0,false);expect(0,"staged\n");settle(0,false);
    assert(peek(0)==DQ_EMPTY && s_live_outbox.depth_known && !s_live_outbox.depth);
    unsigned before_peek=peeks,before_stat=stats;
    for(unsigned i=0;i<100;i++)assert(peek(0)==DQ_EMPTY);
    assert(peeks==before_peek && stats==before_stat);
    admission_full=true;unsigned failures=failed_writes;
    assert(zq_legacy_append(0,"full",4,QS_ADMIT_LIVE)==DQ_FULL && failed_writes==failures);
    admission_full=false;
    assert(zq_legacy_append(0,"new",3,QS_ADMIT_LIVE)==DQ_OK && !s_live_outbox.legacy.empty_cached);
    expect(0,"new\n");settle(0,false);
    /* Each write fault refuses a durable claim; every written byte remains
     * readable as an ordinary complete row or explicit fragment evidence. */
    for(int fault=SHORT_WRITE;fault<=CLOSE;fault++) {
        assert(peek(0)==DQ_EMPTY);io_fault=fault;failures=failed_writes;
        assert(zq_legacy_append(0,"abcdef",6,QS_ADMIT_LIVE)==DQ_IO && !io_fault);
        assert(failed_writes==failures+1 && !s_live_outbox.depth_known);
        assert(peek(0)==DQ_OK);
        if(fault==SHORT_WRITE) {
            assert(copied_length==3 && token.evidence_required);
            assert(zq_legacy_settle(0,&token,false)==DQ_STALE);
            assert(zq_legacy_append(0,"after",5,QS_ADMIT_LIVE)==DQ_IO); /* Incomplete tail is preserved. */
        }
        settle(0,token.evidence_required);
    }
    /* Cursor rename failure cannot erase the just-settled file. A later
     * empty read retries safe reclamation before accepting a new generation. */
    assert(zq_legacy_append(0,"rename",6,QS_ADMIT_LIVE)==DQ_OK);expect(0,"rename\n");
    io_fault=RENAME;assert(zq_legacy_settle(0,&token,false)==DQ_IO && !io_fault);
    struct stat st;assert(stat("live.jsonl",&st)==0);assert(peek(0)==DQ_EMPTY);
    write_bytes("live.bak","retained\n",9);
    assert(zq_legacy_append(0,"active",6,QS_ADMIT_LIVE)==DQ_OK);expect(0,"active\n");
    io_fault=RESTORE;assert(zq_legacy_settle(0,&token,false)==DQ_IO && !io_fault);
    assert(!s_live_outbox.owner_initialized && !s_live_outbox.depth_known);
    expect(0,"retained\n");settle(0,false);assert(peek(0)==DQ_EMPTY);
    /* A failed stat must not turn an inaccessible existing file into empty
     * storage or authorize append/create. */
    io_fault=STAT;assert(zq_legacy_append(0,"stat",4,QS_ADMIT_LIVE)==DQ_IO && !io_fault);
    assert(stat("live.jsonl",&st)!=0 && errno==ENOENT);
    assert(health.legacy.read_faults==1 && legacy_health.errors[LF_ADD_LIVE][LF_READ]==EIO);
    char maximum[8190];memset(maximum,'m',sizeof(maximum));
    assert(zq_legacy_append(0,maximum,sizeof(maximum),QS_ADMIT_LIVE)==DQ_OK);
    assert(health.legacy.read_faults==1); /* A producer is not a complete read proof. */
    assert(peek(0)==DQ_OK && copied_length==8191 && copied[8190]=='\n' && !memcmp(copied,maximum,sizeof(maximum)));
    assert(!health.legacy.read_faults && health.legacy.read_recoveries==1 && health.legacy.append_faults==1 && health.legacy.retire_faults==1);
    settle(0,false);
    /* Lost append and retirement replies never authorize a different item. */
    lose_operation=ZQ_APPEND_COMMIT;
    assert(zq_legacy_append(0,"first",5,QS_ADMIT_LIVE)==DQ_PENDING && ticket);
    hold=false;assert(zq_legacy_append(0,"second",6,QS_ADMIT_LIVE)==DQ_OK);
    expect(0,"first\n");first=token;lose_operation=ZQ_SETTLE;
    assert(zq_legacy_settle(0,&first,false)==DQ_PENDING && ticket);
    hold=false;assert(zq_legacy_settle(0,&first,false)==DQ_STALE);
    expect(0,"second\n");settle(0,false);
    reject=true;assert(zq_legacy_append(0,"quiesced",8,QS_ADMIT_LIVE)==DQ_PENDING);
    reject=false;assert(peek(0)==DQ_EMPTY);
    /* A consumed 24-KiB prefix is verified across separate owner requests.
     * Another lane stays usable while that recovery proceeds. */
    char history[24580];memset(history,'h',sizeof(history));history[24575]='\n';
    memcpy(history+24576,"end\n",4);write_bytes("bulk.jsonl",history,sizeof(history));
    committed[1]=(lq_checkpoint_t){.version=1,.generation=4,.offset=24576,.prefix_crc=dq_crc32(history,24576)};
    committed[1].crc=dq_crc32(&committed[1],offsetof(lq_checkpoint_t,crc));
    assert(peek(1)==DQ_PENDING && s_bulk_outbox.legacy.recovery_offset==8192);
    assert(zq_legacy_append(0,"during",6,QS_ADMIT_LIVE)==DQ_OK);
    assert(peek(1)==DQ_PENDING && s_bulk_outbox.legacy.recovery_offset==16384);
    expect(1,"end\n");assert(token.offset==24576);settle(1,false);
    expect(0,"during\n");settle(0,false);
    /* A reboot reloads the checkpoint; no old text cursor can skip bytes. */
    s_bulk_outbox.owner_initialized=false;memset(&s_bulk_outbox.legacy,0,sizeof(s_bulk_outbox.legacy));
    nvs_fault=1;assert(peek(1)==DQ_IO);
    assert(health.legacy.read_faults==1 && legacy_health.errors[LF_ADD_BULK][LF_READ]==EIO);
    nvs_fault=0;assert(peek(1)==DQ_EMPTY);
    assert(!health.legacy.read_faults && health.legacy.read_recoveries==2 && health.legacy.retire_faults==1);
    /* A corrupt cursor is returned as opaque evidence, never as a row. A
     * receipt resets only that cursor; the first original row remains. */
    write_bytes("bulk.jsonl","kept\n",5);
    committed[1]=(lq_checkpoint_t){.version=1,.generation=19,.offset=2,.crc=1};
    lq_checkpoint_t original_checkpoint=committed[1];
    s_bulk_outbox.owner_initialized=false;memset(&s_bulk_outbox.legacy,0,sizeof(s_bulk_outbox.legacy));
    assert(peek(1)==DQ_OK && token.checkpoint_evidence && token.evidence_required);
    assert(copied_length==sizeof(original_checkpoint) && !memcmp(copied,&original_checkpoint,copied_length));
    assert(health.legacy.read_faults && !s_bulk_outbox.depth_known);
    assert(zq_legacy_settle(1,&token,false)==DQ_STALE && !memcmp(&committed[1],&original_checkpoint,sizeof(original_checkpoint)));
    settle(1,true);
    assert(!committed[1].offset && committed[1].generation==20 && health.legacy.read_faults);
    expect(1,"kept\n");assert(!token.checkpoint_evidence && !health.legacy.read_faults);settle(1,false);
    puts("actual ADD legacy owner: generations, faults, copied replies, prefix recovery and custody passed");
}
