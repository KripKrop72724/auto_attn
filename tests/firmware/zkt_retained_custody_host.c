#include "queue_store.h"
#include "legacy_queue.h"
#include "delivery_scheduler.h"
#include "evidence_receipt.h"
#include "reliability.h"
#include "cJSON.h"
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#define ADD_OUTBOX_LINE_BYTES 8192U
#define ADD_OUTBOX_RETRY_MS 1000U
#define ADD_PRIORITY_HOLD_MS 1000U
#define ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS 1000U
#define ADD_OUTBOX_ACK_TIMEOUT_MS 1000U
#define ADD_WORKER_READING 1
#define ADD_WORKER_IDLE 2
#define ADD_WORKER_NETWORK 3
#define ADD_WORKER_COMMITTING 4
#define LED_STATUS_LOCAL_FAILURE 1
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
#define ESP_LOGW(...) ((void)0)
typedef unsigned SemaphoreHandle_t;
static bool required=true;
#if ZONE_LITE_QUEUE_OWNER
static bool zj_runtime_checkpoint_required(void) { return required; }
#endif
#include "retained_types_actual.inc"
static add_outbox_t s_live_outbox={.lock=1,.depth=1,.depth_known=true};
static add_outbox_t s_bulk_outbox={.lock=2,.depth=1,.depth_known=true};
static uint32_t s_outbox_tick_ms,s_outbox_progress_ms;
static unsigned s_add_worker_operation,held,selected,sends,custody_sends,retirements;
static bool s_outbox_buffer_ready,ack_ok,batch_ok,custody_ok,generation_ok,commit_ok,successful;
static char raw[ADD_OUTBOX_LINE_BYTES],buffer[ADD_OUTBOX_LINE_BYTES],custody_key[256];
static jmp_buf finished;
static size_t allocations,fail_at;
static void *allocate(size_t length) { return ++allocations==fail_at ? NULL : malloc(length); }
static int64_t monotonic_ms(void) { return 1234; }
static uint32_t esp_random(void) { return 17; }
static char *allocate_outbox_line_buffer(void) { return buffer; }
static void vTaskDelay(unsigned wait) { assert(wait);longjmp(finished,1); }
static bool add_connector_is_connected(void) { return true; }
static bool priority_delivery_hold_active(void) { return false; }
static int xSemaphoreTake(SemaphoreHandle_t lock,unsigned wait)
{ assert(lock && wait && !held);held=lock;return pdTRUE; }
static void xSemaphoreGive(SemaphoreHandle_t lock) { assert(held==lock);held=0; }
static void led_status_fault(int fault) { assert(fault==LED_STATUS_LOCAL_FAILURE && !held); }
bool qs_snapshot(qs_lane_t lane,uint32_t *depth) { assert(lane<QS_COUNT && !held);*depth=1;return true; }
bool qs_generation(char output[33]) { assert(!held);memset(output,'a',32);output[32]=0;return generation_ok; }
int ds_pick(const delivery_scheduler_t *s,unsigned ready,uint64_t now,bool background)
{ (void)s;(void)now;assert(!background && ready==63 && !held);return (int)selected; }
void ds_attempted(delivery_scheduler_t *s,unsigned lane) { (void)s;assert(lane==selected); }
void ds_complete(delivery_scheduler_t *s,unsigned lane,uint64_t now,bool ok,uint32_t jitter)
{ (void)s;(void)now;(void)jitter;assert(lane==selected && !held);successful=ok;longjmp(finished,1); }
static bool read_legacy_delivery(add_outbox_t *outbox,char *line,size_t *length,lq_token_t *token)
{
    assert((selected==0 ? outbox==&s_live_outbox : outbox==&s_bulk_outbox) && !held);
    *length=strlen(raw);memcpy(line,raw,*length+1);
    *token=(lq_token_t){.generation=9,.offset=7,.end=(uint32_t)(7+*length),.crc=99};return true;
}
dq_result_t qs_peek(qs_lane_t lane,void *line,size_t capacity,size_t *length,dq_token_t *token)
{
    assert(lane<QS_COUNT && !held && capacity==ADD_OUTBOX_LINE_BYTES-1);*length=strlen(raw);
    memcpy(line,raw,*length);*token=(dq_token_t){.segment=3,.offset=7,.end=(uint32_t)(7+*length),.sequence=19,.crc=99};return DQ_OK;
}
static bool attendance_payload_is_valid(const cJSON *payload) { return cJSON_IsObject(payload); }
static bool oracle_receipt_payload_is_valid(const cJSON *payload) { return cJSON_IsObject(payload); }
static bool evidence_identity(const cJSON *payload,evidence_receipt_t *out,bool receipt)
{ (void)out;assert(!receipt);return cJSON_IsObject(payload); }
static bool send_payload_and_wait_for_ack(const char *type,const char *payload,unsigned lock,unsigned wait,
    void *reconcile,add_attendance_settlement_ack_t *ack,void *custody)
{
    assert(!held && type[0] && payload[0] && lock && wait && !reconcile && !custody && !custody_sends && !retirements);
    ++sends;if(ack)ack->valid=true;return ack_ok;
}
static bool attendance_settlement_matches_payload(const char *payload,const add_attendance_settlement_ack_t *ack)
{ assert(payload[0] && ack->valid && sends==1);return batch_ok; }
static bool add_connector_transfer_queue_evidence(const char *queue,const char *generation,const char *id,
    const void *bytes,size_t length,const char *serial,const char *reason)
{
    assert(!held && !serial && !retirements && length==strlen(raw) && !memcmp(bytes,raw,length));
    if(!strcmp(reason,"LEGACY_RECOVERY"))assert(sends==1 && ack_ok && batch_ok && add_legacy_owner_required());
    else assert(!strcmp(reason,"MALFORMED") && !sends);
    ++custody_sends;snprintf(custody_key,sizeof(custody_key),"%s/%s/%s",queue,generation,id);return custody_ok;
}
static bool retire(bool malformed)
{
    assert(!held);
    if(add_legacy_owner_required() || malformed)assert(custody_sends==1 && custody_ok);
    else assert(sends==1 && ack_ok && batch_ok && !custody_sends);
    ++retirements;return commit_ok;
}
static bool settle_legacy_delivery(add_outbox_t *outbox,const lq_token_t *token,bool custody)
{ assert(outbox && token->generation==9 && token->offset==7 && token->crc==99);return retire(custody); }
dq_result_t qs_settle(qs_lane_t lane,const dq_token_t *token)
{ assert(lane<QS_COUNT && token->segment==3 && token->offset==7 && token->sequence==19);return retire(raw[0]!='{') ? DQ_OK : DQ_IO; }
#include "retained_worker_actual.inc"
static void reset(void)
{
    sends=custody_sends=retirements=held=0;allocations=fail_at=0;successful=false;
    ack_ok=batch_ok=custody_ok=generation_ok=commit_ok=true;custody_key[0]=0;
}
static void run(void) { if(!setjmp(finished))outbox_task(NULL);assert(!held); }
int main(void)
{
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    for(selected=0;selected<6;++selected){
        reset();strcpy(raw,"{\"type\":\"attendance_batch\",\"payload\":{\"event_uid\":\"original-uid\"}}\n");run();
        assert(successful && sends==1 && retirements==1 && custody_sends==(unsigned)add_legacy_owner_required());
        size_t total=allocations;
        for(size_t fault=1;fault<=total;++fault){reset();fail_at=fault;run();assert(!successful || retirements==1);}
        reset();ack_ok=false;run();assert(!successful && sends==1 && !retirements && !custody_sends);
        reset();batch_ok=false;run();assert(!successful && sends==1 && !retirements && !custody_sends);
        if(add_legacy_owner_required()){
            reset();generation_ok=false;run();assert(!successful && !retirements && !custody_sends);
            reset();custody_ok=false;run();assert(!successful && !retirements && custody_sends==1);
            char lost_ack_key[256];memcpy(lost_ack_key,custody_key,sizeof(lost_ack_key));
            reset();commit_ok=false;run();assert(!successful && retirements==1 && !strcmp(lost_ack_key,custody_key));
            reset();run();assert(successful && !strcmp(lost_ack_key,custody_key));
        }
        reset();strcpy(raw,"malformed original bytes\n");custody_ok=false;run();assert(!successful && !sends && !retirements && custody_sends==1);
        reset();run();assert(successful && !sends && retirements==1 && custody_sends==1);
    }
    required=false;selected=0;reset();strcpy(raw,"{\"type\":\"attendance_batch\",\"payload\":{}}\n");
    run();assert(successful && sends==1 && !custody_sends && retirements==1);
    puts("actual ADD worker: original custody, lost replies, checkpoint retries, allocation faults and family isolation passed");
}
