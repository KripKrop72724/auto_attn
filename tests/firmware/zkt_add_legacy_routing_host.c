#include "zkt_segmented_owner.h"
#include "reliability.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define ADD_OUTBOX_LINE_BYTES 8192U
#define ADD_BULK_CAPACITY_WAIT_MS 600000
#define ADD_BULK_CAPACITY_POLL_MS 250
#define ADD_OUTBOX_RECORD_OVERHEAD_BYTES 128
#define LED_STATUS_LOCAL_FAILURE 1
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
typedef int SemaphoreHandle_t;
typedef unsigned TickType_t;
static bool required=true, segmented;
static unsigned held, writes, reads, settlements, direct_reads, direct_settlements;
static unsigned fail_on_write;
static bool last_custody;
static bool generation_ok=true,evidence_ok=true;
static unsigned evidence_sends;
static char evidence_key[256];
static int64_t now;
static dq_result_t reply=DQ_OK;
#if ZONE_LITE_QUEUE_OWNER
static bool zj_runtime_checkpoint_required(void) { return required; }
#endif
#include "add_legacy_types_actual.inc"
static add_outbox_t s_live_outbox={.path="live.jsonl",.max_bytes=65536,.lock=1};
static add_outbox_t s_bulk_outbox={.path="bulk.jsonl",.max_bytes=65536,.lock=2};
static int xSemaphoreTake(SemaphoreHandle_t lock,unsigned wait)
{ assert(lock && wait && !held);held=(unsigned)lock;return pdTRUE; }
static void xSemaphoreGive(SemaphoreHandle_t lock) { assert(held==(unsigned)lock);held=0; }
static int64_t monotonic_ms(void) { return now; }
static void vTaskDelay(unsigned ms) { now+=ms; }
static bool storage_upgrade_segmented_writes(void) { return segmented; }
typedef struct { bool add_enabled; } config_t;
static config_t config = {true};
static const config_t *zone_config_get(void) { return &config; }
static char *attendance_outbox_record_line(const char *payload, bool *live)
{ *live=false;return payload ? strdup(payload) : NULL; }
static bool add_connector_is_connected(void) { return false; }
static bool deliver_attendance_payloads_acknowledged(const char *const *payloads,size_t count)
{ (void)payloads;(void)count;assert(!held);return false; }
static void led_status_fault(int code) { assert(code==LED_STATUS_LOCAL_FAILURE); }
static void refresh_capacity_deadline_on_progress(const add_outbox_t *outbox,uint32_t *depth,int64_t *until)
{ (void)outbox;(void)depth;(void)until;assert(held); }
static bool compact_outbox_locked(add_outbox_t *outbox,bool force) { (void)outbox;(void)force;assert(held);return true; }
bool qs_local_begin(qs_admission_t policy,size_t bytes) { assert(held && (unsigned)policy<=QS_ADMIT_RECOVERY && bytes);return true; }
void qs_local_end(bool ok,int error) { assert(held && ok && !error); }
static bool read_outbox_row_locked(add_outbox_t *outbox,char *line,off_t *end)
{
    assert(held);++direct_reads;memcpy(line,"old\n",5);
    outbox->pending_token=(lq_token_t){.generation=1,.offset=0,.end=4,.crc=17};*end=4;return true;
}
static bool advance_outbox_locked(add_outbox_t *outbox,off_t end,bool custody)
{ assert(held && end==(off_t)outbox->pending_token.end);++direct_settlements;last_custody=custody;return true; }
dq_result_t qs_append_with_policy(qs_lane_t lane,const void *bytes,size_t length,qs_admission_t policy)
{ assert(segmented && !held && (lane==QS_LIVE || lane==QS_BULK) && bytes && length==3 &&
    (policy==QS_ADMIT_LIVE || policy==QS_ADMIT_HISTORICAL));++writes;return reply; }
dq_result_t zq_legacy_append(unsigned lane,const void *bytes,size_t length,qs_admission_t policy)
{ assert(required && !held && lane<2 && bytes && length==3 && (unsigned)policy<=QS_ADMIT_RECOVERY);
  ++writes;return fail_on_write==writes ? DQ_IO : reply; }
dq_result_t zq_legacy_peek(unsigned lane,void *bytes,size_t capacity,size_t *length,lq_token_t *token)
{
    assert(required && !held && lane<2 && capacity==8191);++reads;*length=0;memset(token,0,sizeof(*token));
    if(reply==DQ_OK) { memcpy(bytes,"new\n",4);*length=4;*token=(lq_token_t){.generation=9,.offset=7,.end=11,.crc=99}; }
    return reply;
}
dq_result_t zq_legacy_settle(unsigned lane,const lq_token_t *token,bool custody)
{ assert(required && !held && lane<2 && token->generation==9 && token->offset==7 && token->end==11 && token->crc==99);
  ++settlements;last_custody=custody;return reply; }
bool qs_generation(char output[33])
{ assert(!held);memset(output,'a',32);output[32]=0;return generation_ok; }
static bool add_connector_transfer_queue_evidence(const char *queue,const char *generation,const char *id,
    const void *bytes,size_t length,const char *serial,const char *reason)
{
    assert(!held && required && !serial && !strcmp(reason,"LEGACY_RECOVERY") && length==4 && !memcmp(bytes,"new\n",4));
    ++evidence_sends;snprintf(evidence_key,sizeof(evidence_key),"%s/%s/%s",queue,generation,id);return evidence_ok;
}
#include "add_legacy_routing_actual.inc"
int main(void)
{
    assert(add_legacy_owner_required() == (ZONE_LITE_QUEUE_OWNER && required));
    char bytes[8192];size_t length;lq_token_t token;
    if(ZONE_LITE_QUEUE_OWNER) {
        assert(add_connector_enqueue_validated_line_with_policy("new",true,QS_ADMIT_LIVE) && writes==1);
        struct stat st;assert(stat("live.jsonl",&st)!=0 && errno==ENOENT);
        const char *batches[]={"one","two"};
        unsigned before=writes;fail_on_write=before+2;
        assert(!add_connector_enqueue_attendance_bulk(batches,2) && writes==before+2);
        fail_on_write=0;assert(add_connector_enqueue_attendance_bulk(batches,2) && writes==before+4);
        assert(stat("bulk.jsonl",&st)!=0 && errno==ENOENT);
        assert(read_legacy_delivery(&s_live_outbox,bytes,&length,&token) && length==4 && !strcmp(bytes,"new\n"));
        assert(settle_legacy_delivery(&s_live_outbox,&token,true) && last_custody);
        assert(settle_legacy_delivery(&s_live_outbox,&token,false) && !last_custody);
        reply=DQ_PENDING;
        assert(!add_connector_enqueue_validated_line_with_policy("new",true,QS_ADMIT_LIVE));
        assert(!settle_legacy_delivery(&s_live_outbox,&token,false));
        assert(!read_legacy_delivery(&s_live_outbox,bytes,&length,&token) && !length && !token.end);
        int64_t started=now;
        assert(!add_connector_enqueue_validated_line_with_policy("new",false,QS_ADMIT_HISTORICAL));
        assert(now-started==ADD_BULK_CAPACITY_WAIT_MS && !direct_reads && !direct_settlements && !held);
        assert(stat("live.jsonl",&st)!=0 && errno==ENOENT && stat("bulk.jsonl",&st)!=0 && errno==ENOENT);
        reply=DQ_OK;
        dq_token_t segmented_token={.segment=3,.offset=7,.end=11,.sequence=19,.crc=99};
        token=(lq_token_t){.generation=9,.offset=7,.end=11,.crc=99};
        const char *names[]={"add_live_legacy/","add_live/","add_bulk_legacy/","add_bulk/","receipts/","evidence/"};
        for(unsigned lane=0;lane<6;++lane){
            assert(preserve_retained_outbox(lane,"new\n",4,&segmented_token,&token));
            assert(strstr(evidence_key,names[lane])==evidence_key);
            assert(strstr(evidence_key,lane==0 || lane==2 ? "-legacy-9/7:99" : "-segmented-v2/3:7:19"));
        }
        unsigned custody_before=evidence_sends;generation_ok=false;
        assert(!preserve_retained_outbox(0,"new\n",4,&segmented_token,&token) && evidence_sends==custody_before);
        generation_ok=true;evidence_ok=false;
        assert(!preserve_retained_outbox(0,"new\n",4,&segmented_token,&token) && evidence_sends==custody_before+1);
        evidence_ok=true;
    }
    required=false; /* Older ZKT and Hikvision use their existing direct path. */
    unsigned custody_before=evidence_sends;
    assert(preserve_retained_outbox(0,NULL,0,NULL,NULL) && evidence_sends==custody_before);
    assert(add_connector_enqueue_validated_line_with_policy("old",true,QS_ADMIT_LIVE));
    FILE *file=fopen("live.jsonl","rb");assert(file && fread(bytes,1,4,file)==4 && !memcmp(bytes,"old\n",4) && fclose(file)==0);
    assert(read_legacy_delivery(&s_live_outbox,bytes,&length,&token) && length==4 && direct_reads==1);
    assert(settle_legacy_delivery(&s_live_outbox,&token,false) && direct_settlements==1 && !last_custody);
    segmented=true;unsigned before=writes;
    assert(add_connector_enqueue_validated_line_with_policy("new",true,QS_ADMIT_LIVE) && writes==before+1);
    assert(!held);
    puts("actual ADD producer/delivery routing and family isolation passed");
}
