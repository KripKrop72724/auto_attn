#include "zkt_quarantine_owner.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define STORAGE_BASE "."
#define CORRUPT_ORDS_PATH "./corrupt_ords.jsonl"
#define LED_STATUS_LOCAL_FAILURE 1
#define ADD_WORKER_NETWORK 2
#define ADD_WORKER_COMMITTING 3
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
typedef int SemaphoreHandle_t;
typedef int esp_err_t;
typedef unsigned nvs_handle_t;
static SemaphoreHandle_t g_storage_lock=1;
static bool required=true, owner_task, reject, grant_custody;
static lf_state_t legacy_health;
static qs_health_t health;
#define failed_writes health.write_failures
#include "legacy_health_actual.inc"
static unsigned held,budget,peeks,reclaims,faults,receipts,checkpoint_writes;
static unsigned nvs_fault;
static bool peek_fault;
static lq_checkpoint_t committed[3],pending_checkpoint;
static unsigned pending_lane;
static const char *paths[]={CORRUPT_ORDS_PATH,"./add_corrupt.jsonl","./add_corrupt.bak"};
static char buffer[DQ_MAX_RECORD_BYTES+1],sent[DQ_MAX_RECORD_BYTES+1];
static char sent_generation[80],sent_id[80];
static size_t sent_length;
static char *g_blocked_drain_buffer=buffer;
enum { IO_READ=1,IO_CLOSE,IO_STAT,IO_REMOVE };
static unsigned io_fault;
static size_t recovery_bytes;
/* Interpose after libc declarations, so fortified Linux stdio cannot bypass
 * the injected call through an inline declaration under a renamed symbol. */
static size_t fault_fread(void *bytes,size_t size,size_t count,FILE *file)
{
    assert(held);recovery_bytes+=size*count;
    if(io_fault==IO_READ){io_fault=0;errno=EIO;return fread(bytes,size,count/2,file);}
    return fread(bytes,size,count,file);
}
static int fault_fclose(FILE *file)
{
    int result=fclose(file);
    if(io_fault==IO_CLOSE){io_fault=0;errno=EIO;return EOF;}
    return result;
}
static int fault_stat(const char *path,struct stat *st)
{ if(io_fault==IO_STAT){io_fault=0;errno=EIO;return -1;}return stat(path,st); }
static int fault_remove(const char *path)
{ if(io_fault==IO_REMOVE){io_fault=0;errno=EIO;return -1;}return remove(path); }
#define fread fault_fread
#define fclose fault_fclose
#define stat(...) fault_stat(__VA_ARGS__)
#define remove fault_remove
#include "legacy_queue.c"
#undef fread
#undef fclose
#undef stat
#undef remove
#if ZONE_LITE_QUEUE_OWNER
static bool zj_runtime_checkpoint_required(void) { return required; }
#endif
bool zj_owner_is_current_task(void) { return owner_task; }
static int xSemaphoreTake(SemaphoreHandle_t lock,unsigned wait)
{ assert(lock && (wait==100 || wait==200) && !held);held=(unsigned)lock;return pdTRUE; }
static void xSemaphoreGive(SemaphoreHandle_t lock) { assert(held==(unsigned)lock && !budget);held=0; }
bool qs_local_read_begin(void) { assert(owner_task && held && !budget);budget=1;return true; }
void qs_local_end(bool ok,int error) { assert(owner_task && budget);if(!ok){assert(error);++failed_writes;}budget=0; }
static void led_status_fault(int code) { assert(code==LED_STATUS_LOCAL_FAILURE);++faults; }
static unsigned lane_for_key(const char *key)
{ if(!strcmp(key,"old_qo"))return 0;if(!strcmp(key,"old_qa"))return 1;assert(!strcmp(key,"old_qb"));return 2; }
static esp_err_t nvs_open(const char *name,int mode,nvs_handle_t *handle)
{ assert(held && !strcmp(name,"legacy_queues"));assert(!required || !ZONE_LITE_QUEUE_OWNER || (owner_task && budget));
  *handle=(unsigned)mode+1;if(nvs_fault==1){nvs_fault=0;return 9;}return ESP_OK; }
static void nvs_close(nvs_handle_t handle) { assert(handle); }
static esp_err_t nvs_get_blob(nvs_handle_t handle,const char *key,void *out,size_t *size)
{ assert(handle==1 && *size==sizeof(lq_checkpoint_t));unsigned lane=lane_for_key(key);
  if(!committed[lane].version)return ESP_ERR_NVS_NOT_FOUND;
  memcpy(out,&committed[lane],*size);return ESP_OK; }
static esp_err_t nvs_set_blob(nvs_handle_t handle,const char *key,const void *in,size_t size)
{ assert(handle==2 && size==sizeof(lq_checkpoint_t));pending_lane=lane_for_key(key);memcpy(&pending_checkpoint,in,size);return ESP_OK; }
static esp_err_t nvs_commit(nvs_handle_t handle)
{
    assert(handle==2);++checkpoint_writes;
    if(nvs_fault==2){nvs_fault=0;return 9;}
    committed[pending_lane]=pending_checkpoint;
    if(nvs_fault==3){nvs_fault=0;return 9;}
    return ESP_OK;
}
static dq_result_t traced_peek(legacy_queue_t *queue,char *bytes,size_t capacity,lq_token_t *token)
{ ++peeks;if(peek_fault){peek_fault=false;return DQ_IO;}return lq_peek(queue,bytes,capacity,token); }
static dq_result_t traced_reclaim(legacy_queue_t *queue) { ++reclaims;return lq_reclaim(queue); }
static bool add_connector_is_connected(void) { return true; }
static void add_connector_report_ords_worker(int state) { assert(!held && !budget && (state==2 || state==3)); }
bool qs_generation(char output[33]) { assert(!held && !budget);memset(output,'a',32);output[32]=0;return true; }
static bool add_connector_transfer_queue_evidence(const char *name,const char *generation,const char *id,
    const void *bytes,size_t length,const char *serial,const char *reason)
{
    assert(!held && !budget && !owner_task && !serial && !strcmp(reason,"LEGACY_RECOVERY"));
    assert(strstr(name,"legacy_") && strstr(name,"quarantine") && length && length<=DQ_MAX_RECORD_BYTES);
    memcpy(sent,bytes,length);sent_length=length;
    snprintf(sent_generation,sizeof(sent_generation),"%s",generation);snprintf(sent_id,sizeof(sent_id),"%s",id);
    ++receipts;return grant_custody;
}
#define lq_peek traced_peek
#define lq_reclaim traced_reclaim
#include "quarantine_actual.inc"
#undef lq_peek
#undef lq_reclaim

/* An evidence-domain request must never call an attendance operation. */
dq_result_t qs_append_with_policy(qs_lane_t lane,const void *bytes,size_t length,qs_admission_t policy)
{ (void)lane;(void)bytes;(void)length;(void)policy;assert(false);return DQ_IO; }
dq_result_t qs_peek(qs_lane_t lane,void *bytes,size_t capacity,size_t *length,dq_token_t *token)
{ (void)lane;(void)bytes;(void)capacity;(void)length;(void)token;assert(false);return DQ_IO; }
dq_result_t qs_settle(qs_lane_t lane,const dq_token_t *token) { (void)lane;(void)token;assert(false);return DQ_IO; }
bool qs_snapshot(qs_lane_t lane,uint32_t *depth) { (void)lane;(void)depth;assert(false);return false; }
bool qs_recover_step(void) { assert(false);return false; }
bool qs_verify_persistence(void) { assert(false);return false; }
static zq_store_t store={
#if ZONE_LITE_QUEUE_OWNER
    .quarantine={NULL,zkt_quarantine_owner_peek,zkt_quarantine_owner_settle}
#endif
};
static uint64_t now=1,next_ticket,ticket;
static zj_request_t pending;
static zj_reply_t reply;
static bool executed,hold_reply;
static int lose_operation=-1;
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned ms) { now+=(uint64_t)ms*1000; }
bool zj_owner_submit(const zj_request_t *input,uint64_t *out)
{
    assert(!held && !budget && !ticket && ZONE_LITE_QUEUE_OWNER);
    *out=0;if(reject)return false;
    assert(input->operation==ZJ_SEGMENTED_QUEUE && zq_request_valid(&input->input.segmented));
    pending=*input;executed=false;*out=ticket=++next_ticket;return true;
}
bool zj_owner_poll(uint64_t input,zj_reply_t *out,bool *complete)
{
    assert(ticket && input==ticket && !held && !budget);*complete=false;
    if(!executed){
        memset(&reply,0,sizeof(reply));owner_task=true;
        zq_store_execute(&store,now,&pending.input.segmented,&reply.segmented);
        owner_task=false;reply.result=ZJ_OK;executed=true;
        if(lose_operation==pending.input.segmented.operation){hold_reply=true;lose_operation=-1;}
    }
    if(hold_reply)return true;
    *out=reply;*complete=true;ticket=0;return true;
}
static void seed(unsigned lane,const void *bytes,size_t length)
{
    assert(!held && !budget && lane<3);
    FILE *file=fopen(paths[lane],"wb");assert(file && fwrite(bytes,1,length,file)==length && fclose(file)==0);
    memset(&g_legacy_quarantine[lane],0,sizeof(g_legacy_quarantine[lane]));
    memset(&committed[lane],0,sizeof(committed[lane]));
}
static bool exists(unsigned lane) { struct stat st;return stat(paths[lane],&st)==0; }
static void slice(unsigned lane) { g_quarantine_lane=lane;legacy_quarantine_slice();assert(!held && !budget); }
int main(void)
{
    const unsigned char raw[]={'A',0,'B','\n','t','a','i','l'};
    char copy[DQ_MAX_RECORD_BYTES+1];size_t length=99;lq_token_t token;
    seed(0,raw,sizeof(raw));seed(1,"held\n",5);seed(2,"spare\n",6);
    if(ZONE_LITE_QUEUE_OWNER){
        reject=true;unsigned before=peeks;slice(0);
        assert(peeks==before && !receipts && exists(0));reject=false;
#if ZONE_LITE_QUEUE_OWNER
        assert(zkt_quarantine_owner_peek(0,copy,sizeof(copy),&length,&token)==DQ_PENDING && !length && !token.end);
        assert(zkt_quarantine_owner_settle(0,&(lq_token_t){.end=1},false)==DQ_IO);
#endif
        zq_request_t invalid={.operation=ZQ_APPEND_BEGIN,.domain=ZQ_QUARANTINE,.total=1,.deadline_us=now+100};
        assert(!zq_request_valid(&invalid));
        invalid.operation=ZQ_SETTLE;invalid.legacy_token.end=1;
        assert(!zq_request_valid(&invalid));invalid.custody=true;assert(zq_request_valid(&invalid));
        invalid.lane=3;assert(!zq_request_valid(&invalid));invalid.lane=0;invalid.domain=99;assert(!zq_request_valid(&invalid));
    }
    grant_custody=false;slice(0);
    assert(receipts==1 && sent_length==4 && !memcmp(sent,raw,4) && !committed[0].offset && exists(0));
    char first_generation[80],first_id[80];strcpy(first_generation,sent_generation);strcpy(first_id,sent_id);
    slice(0);assert(receipts==2 && !strcmp(first_generation,sent_generation) && !strcmp(first_id,sent_id));
    grant_custody=true;nvs_fault=2;slice(0);
    assert(!nvs_fault && !committed[0].offset && exists(0));
    slice(2);assert(!exists(2) && exists(0) && exists(1)); /* One held lane never stalls another. */
    slice(0);assert(committed[0].offset==4 && exists(0));
    slice(0);assert(sent_length==4 && !memcmp(sent,"tail",4) && !exists(0));
    unsigned before=peeks,before_reclaim=reclaims,before_receipts=receipts;
    for(unsigned i=0;i<100;++i)slice(0);
    assert(peeks==before && reclaims==before_reclaim && receipts==before_receipts);
    peek_fault=true;slice(1);assert(!peek_fault && exists(1));
    if(ZONE_LITE_QUEUE_OWNER)assert(health.legacy.read_faults==1 && health.legacy.retire_faults==1);
    slice(1);assert(!exists(1));
    if(ZONE_LITE_QUEUE_OWNER)assert(!health.legacy.read_faults && health.legacy.read_recoveries==1 && health.legacy.retire_faults==1);
    seed(1,"repeat\nrepeat\n",14);nvs_fault=3;slice(1);
    assert(!nvs_fault && committed[1].offset==7 && exists(1));
    slice(1);assert(!exists(1)); /* Uncertain checkpoint is recovered, never a fabricated receipt. */
    if(ZONE_LITE_QUEUE_OWNER){
        seed(1,"one\ntwo\n",8);
        assert(read_quarantine_delivery(1,copy,sizeof(copy),&length,&token)==DQ_OK && length==4);
        lq_token_t wrong=token;wrong.crc^=1;
        assert(settle_quarantine_delivery(1,&wrong)==DQ_STALE && !committed[1].offset);
        lose_operation=ZQ_SETTLE;
        assert(settle_quarantine_delivery(1,&token)==DQ_PENDING && ticket && committed[1].offset==4);
        hold_reply=false;
        assert(settle_quarantine_delivery(1,&token)==DQ_STALE && committed[1].offset==4);
        assert(read_quarantine_delivery(1,copy,sizeof(copy),&length,&token)==DQ_OK && !memcmp(copy,"two\n",4));
        assert(settle_quarantine_delivery(1,&token)==DQ_OK && !exists(1));
        /* Copy failure loses neither the file nor its exact head. */
        seed(2,raw,sizeof(raw));lose_operation=ZQ_PEEK_CHUNK;
        assert(read_quarantine_delivery(2,copy,sizeof(copy),&length,&token)==DQ_PENDING && !length && !token.end);
        assert(exists(2) && !committed[2].offset);hold_reply=false;
        assert(read_quarantine_delivery(2,copy,sizeof(copy),&length,&token)==DQ_OK && length==4 && !memcmp(copy,raw,4));
        slice(2);slice(2);assert(!exists(2));
        assert(zq_evidence_peek(3,copy,sizeof(copy),&length,&token)==DQ_IO && !length && !token.end);
        assert(failed_writes==2);
    }
    /* Real file close and reclamation failures retain the source. Failed
     * removal may replay an already receipted record, never skip its bytes. */
    seed(0,"retained\n",9);before_receipts=receipts;io_fault=IO_CLOSE;slice(0);
    assert(!io_fault && exists(0) && receipts==before_receipts && !committed[0].offset);
    io_fault=IO_STAT;slice(0);assert(!io_fault && exists(0) && committed[0].offset==9);
    io_fault=IO_REMOVE;slice(0);assert(!io_fault && exists(0) && !committed[0].offset);
    slice(0);assert(!exists(0));
    /* Recovery yields after exactly one 8 KiB consumed-prefix slice. The
     * other lane can settle while this checkpoint is still being verified. */
    char large[LQ_RECOVERY_SLICE_BYTES*3+5];memset(large,'x',sizeof(large));
    large[LQ_RECOVERY_SLICE_BYTES*3-1]='\n';memcpy(large+LQ_RECOVERY_SLICE_BYTES*3,"tail\n",5);
    seed(0,large,sizeof(large));
    committed[0]=(lq_checkpoint_t){.version=1,.generation=12,.offset=LQ_RECOVERY_SLICE_BYTES*3,
        .prefix_crc=dq_crc32(large,LQ_RECOVERY_SLICE_BYTES*3)};
    committed[0].crc=dq_crc32(&committed[0],offsetof(lq_checkpoint_t,crc));
    size_t before_bytes=recovery_bytes;io_fault=IO_READ;
    assert(read_quarantine_delivery(0,copy,sizeof(copy),&length,&token)==DQ_IO && !io_fault && !length);
    assert(recovery_bytes-before_bytes<=LQ_RECOVERY_SLICE_BYTES && !g_legacy_quarantine[0].recovery_offset);
    for(unsigned i=0;i<2;++i){
        before_bytes=recovery_bytes;
        assert(read_quarantine_delivery(0,copy,sizeof(copy),&length,&token)==DQ_PENDING && !length && !token.end);
        assert(recovery_bytes-before_bytes==LQ_RECOVERY_SLICE_BYTES);
        seed(1,"live-independent\n",17);slice(1);assert(!exists(1));
    }
    before_bytes=recovery_bytes;
    assert(read_quarantine_delivery(0,copy,sizeof(copy),&length,&token)==DQ_OK && length==5 && !memcmp(copy,"tail\n",5));
    assert(recovery_bytes-before_bytes==LQ_RECOVERY_SLICE_BYTES);
    assert(settle_quarantine_delivery(0,&token)==DQ_OK && !exists(0));
    /* A raw record larger than a transfer remains exact evidence, including
     * embedded zero bytes and the incomplete final fragment. */
    char oversized[DQ_MAX_RECORD_BYTES+37];
    for(size_t i=0;i<sizeof(oversized);++i)oversized[i]=(char)(i%9);
    seed(2,oversized,sizeof(oversized));size_t copied=0;
    while(copied<sizeof(oversized)){
        slice(2);assert(sent_length && sent_length<=sizeof(oversized)-copied);
        assert(!memcmp(sent,oversized+copied,sent_length));copied+=sent_length;
    }
    assert(!exists(2));
    /* Legacy/Hikvision compilation and older ZKT retain their direct path. */
    required=false;seed(0,"old\n",4);slice(0);assert(!exists(0));
    if(!ZONE_LITE_QUEUE_OWNER)assert(!health.legacy.observed && !health.read_failures && !health.write_failures);
    assert(checkpoint_writes && faults && !held && !budget && !ticket);
    puts("actual quarantine custody, owner routing, faults, empty cache and family paths passed");
}
