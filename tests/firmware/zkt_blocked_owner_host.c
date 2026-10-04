#include "zkt_legacy_attendance.h"
#include "reliability.h"
#include "cJSON.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_USERS 4
#define MAX_EVENT_JSON 1024
#define PENDING_PATH "pending.jsonl"
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
#define ESP_LOGI(...) ((void)0)
#define LED_STATUS_LOCAL_FAILURE 1
#define ADD_WORKER_RESOURCE 2
#define ADD_WORKER_NETWORK 3
#define ADD_WORKER_COMMITTING 4
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
static int g_storage_lock=1,held;
static bool required=ZONE_LITE_QUEUE_OWNER,append_ok=true,settle_ok=true,custody_ok,with_fingerprint=true;
static bool session_active,generation_ok=true;
static dq_result_t read_result=DQ_OK;
static unsigned appends,settles,owner_reads,direct_reads,evidence_calls;
static bool last_custody;
static char source_line[MAX_EVENT_JSON],preserved[MAX_EVENT_JSON];
static char evidence_generation[80],evidence_id[80];
static char g_device_serial[80]="TEST-TERMINAL";
static char saved_fingerprint[65],enrollment_uid[16]="17",capture[32]="LIVE",terminal[80]="TEST-TERMINAL";
static const char *reason="";
static size_t allocations,fail_at;
static void *allocate(size_t size) { if(++allocations==fail_at)return NULL;return malloc(size); }
static bool legacy_attendance_owner_required(void) { return required; }
static int xSemaphoreTake(int lock,unsigned delay) { assert(lock && delay==200 && !held);held=lock;return pdTRUE; }
static void xSemaphoreGive(int lock) { assert(held==lock);held=0; }
static void led_status_fault(int code) { assert(code==LED_STATUS_LOCAL_FAILURE); }
static bool append_line(const char *path,const char *line)
{
    assert(!strcmp(path,PENDING_PATH) && (required ? !held : held));
    ++appends;snprintf(preserved,sizeof(preserved),"%s",line);return append_ok;
}
static dq_result_t read_source(char *bytes,size_t capacity,size_t *length,lq_token_t *token)
{
    *length=0;memset(token,0,sizeof(*token));
    if(read_result!=DQ_OK)return read_result;
    size_t n=strlen(source_line);assert(n<capacity);memcpy(bytes,source_line,n+1);*length=n;
    *token=(lq_token_t){.generation=7,.offset=11,.end=(uint32_t)n+11,.crc=dq_crc32(source_line,n)};
    return DQ_OK;
}
static bool commit_source(const lq_token_t *token,bool custody)
{
    assert(token->generation==7 && token->offset==11 && token->end==11+strlen(source_line) &&
        token->crc==dq_crc32(source_line,strlen(source_line)));
    ++settles;last_custody=custody;return settle_ok;
}
static dq_result_t read_blocked_locked(char *bytes,size_t capacity,lq_token_t *token)
{ assert(held);++direct_reads;size_t length;return read_source(bytes,capacity,&length,token); }
static bool settle_blocked_locked(const lq_token_t *token,bool custody) { assert(held);return commit_source(token,custody); }
dq_result_t zq_attendance_legacy_peek(unsigned lane,void *bytes,size_t capacity,size_t *length,lq_token_t *token)
{ assert(required && !held && lane==ZOL_BLOCKED);++owner_reads;return read_source(bytes,capacity,length,token); }
dq_result_t zq_attendance_legacy_settle(unsigned lane,const lq_token_t *token,bool custody)
{ assert(required && !held && lane==ZOL_BLOCKED);return commit_source(token,custody)?DQ_OK:DQ_IO; }
dq_result_t qs_peek(qs_lane_t lane,void *bytes,size_t capacity,size_t *length,dq_token_t *token)
{ (void)bytes;(void)capacity;(void)token;assert(lane==QS_BLOCKED && !held);*length=0;return DQ_EMPTY; }
dq_result_t qs_settle(qs_lane_t lane,const dq_token_t *token) { (void)lane;(void)token;assert(false);return DQ_IO; }
bool qs_generation(char output[33]) { assert(!held && !session_active);memset(output,'a',32);output[32]=0;return generation_ok; }
static bool add_connector_is_connected(void) { return true; }
static void *heap_caps_malloc(size_t size,unsigned flags) { assert(flags==3);return malloc(size); }
static void add_connector_report_ords_worker(int state) { assert(!held && state>=2 && state<=4); }
static bool add_connector_transfer_queue_evidence(const char *queue,const char *generation,const char *id,
    const void *bytes,size_t length,const char *serial,const char *classification)
{
    (void)serial;assert(!held && !session_active && !strcmp(queue,"blocked_legacy") && strstr(generation,"-legacy-7") &&
        strchr(id,':') && (!strcmp(classification,"LEGACY_RECOVERY") || !strcmp(classification,"MALFORMED")));
    assert(length==strlen(source_line) && !memcmp(bytes,source_line,length));
    if(evidence_calls) assert(!strcmp(evidence_generation,generation) && !strcmp(evidence_id,id));
    snprintf(evidence_generation,sizeof(evidence_generation),"%s",generation);
    snprintf(evidence_id,sizeof(evidence_id),"%s",id);
    ++evidence_calls;return custody_ok;
}
#include "blocked_consumers_actual.inc"
static void reset(void)
{
    allocations=fail_at=0;appends=settles=owner_reads=direct_reads=evidence_calls=0;
    append_ok=settle_ok=generation_ok=true;session_active=custody_ok=last_custody=false;read_result=DQ_OK;
    snprintf(source_line,sizeof(source_line),"{\"event_uid\":\"retained-uid\",\"user_id\":\"TEST-USER\","
        "\"_terminal_uid\":\"%s\",\"device_serial\":\"%s\",\"capturetype\":\"%s\"%s%s%s%s}\n",
        enrollment_uid,terminal,capture,with_fingerprint?",\"_terminal_identity_fingerprint\":\"":"",
        with_fingerprint?saved_fingerprint:"",with_fingerprint?"\"":"",reason);
}
static void expect_hold(user_table_t *users)
{ reset();size_t recovered=999;assert(recover_blocked_events_from_snapshot(users,&recovered));assert(!recovered && !appends && !settles && !held); }
int main(void)
{
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    user_table_t users={.count=1};
    zkt_user_t *user=&users.rows[0];
    strcpy(user->uid,"17");strcpy(user->user_id,"TEST-USER");strcpy(user->cnic,"0000000000000");
    strcpy(user->employee_name,"SYNTHETIC TEST");user->raw_punch=true;
    memset(saved_fingerprint,'b',64);saved_fingerprint[64]=0;strcpy(user->terminal_identity_fingerprint,saved_fingerprint);
    reset();size_t recovered=999;
    if(required) {
        /* Even an exactly matching roster cannot rewrite/retire original bytes
         * in the terminal session. This path must not wait on ADD or storage. */
        for(unsigned unavailable=0;unavailable<2;++unavailable) {
            reset();session_active=true;fail_at=unavailable?1:0;
            assert(recover_blocked_events_from_snapshot(&users,&recovered));
            assert(!recovered && !allocations && !appends && !settles &&
                !owner_reads && !direct_reads && !evidence_calls && !held);
        }
        session_active=false;
    }
    /* Existing firmware/Hikvision recovery still requires provenance and a
     * durable local destination. Its allocation and persistence checks remain. */
    required=false;reset();
    assert(recover_blocked_events_from_snapshot(&users,&recovered) && recovered==1 && appends==1 && settles==1 && !last_custody && !held);
    assert(direct_reads==1 && !owner_reads);
    size_t total=allocations;
    cJSON *json=cJSON_Parse(preserved);assert(json);
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(json,"event_uid")->valuestring,"retained-uid"));
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(json,"cnic")->valuestring,"0000000000000"));cJSON_Delete(json);
    for(size_t i=1;i<=total;++i){
        reset();fail_at=i;recovered=999;
        bool ok=recover_blocked_events_from_snapshot(&users,&recovered);
        assert(!held && (!settles || (appends==1 && append_ok)));
        assert(ok ? recovered==1 && settles==1 : !recovered && !settles);
    }
    reset();append_ok=false;assert(!recover_blocked_events_from_snapshot(&users,&recovered) && !recovered && appends==1 && !settles);
    reset();settle_ok=false;assert(!recover_blocked_events_from_snapshot(&users,&recovered) && !recovered && appends==1 && settles==1);
    strcpy(capture,"DUMP_STARTUP");expect_hold(&users);strcpy(capture,"LIVE");
    strcpy(terminal,"REPLACEMENT");expect_hold(&users);strcpy(terminal,"TEST-TERMINAL");
    strcpy(enrollment_uid,"18");expect_hold(&users);strcpy(enrollment_uid,"17");
    saved_fingerprint[0]='c';expect_hold(&users);saved_fingerprint[0]='b';
    with_fingerprint=false;expect_hold(&users);with_fingerprint=true;
    reason=",\"blocked_reason\":\"ORACLE_REJECTED\"";expect_hold(&users);reason="";
    user->cnic[0]='x';expect_hold(&users);user->cnic[0]='0';
    reset();read_result=DQ_PENDING;assert(!recover_blocked_events_from_snapshot(&users,&recovered) && !appends && !settles && !held);
    /* Raw custody retirement is independent of identity repair. No receipt,
     * including a lost response, means no retirement; it holds no file lock. */
    static char raw_buffer[DQ_MAX_RECORD_BYTES+1];g_blocked_drain_buffer=raw_buffer;
    required=ZONE_LITE_QUEUE_OWNER;
    reset();custody_ok=false;blocked_evidence_slice();assert(evidence_calls==1 && !settles && !held);
    custody_ok=true;settle_ok=false;blocked_evidence_slice();assert(evidence_calls==2 && settles==1 && last_custody && !held);
    settle_ok=true;blocked_evidence_slice();assert(evidence_calls==3 && settles==2 && last_custody && !held && !appends);
    assert(required ? owner_reads==3 && !direct_reads : direct_reads==3 && !owner_reads);
    reset();generation_ok=false;blocked_evidence_slice();assert(!evidence_calls && !settles && !appends && !held);
    required=false;reset();assert(recover_blocked_events_from_snapshot(&users,&recovered) && recovered==1 && !held);
    puts("actual blocked identity, exact custody, owner/family routing and allocation faults passed");
}
