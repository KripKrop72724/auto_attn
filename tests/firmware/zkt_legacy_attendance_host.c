#define ZOL_PENDING_PATH "pending.jsonl"
#define ZOL_PENDING_BACKUP_PATH "pending.bak"
#define ZOL_PENDING_TEMP_PATH "pending.tmp"
#define ZOL_BLOCKED_PATH "blocked_identity.jsonl"
#define ZOL_BLOCKED_BACKUP_PATH "blocked_recovery.bak"
#define ZOL_BLOCKED_TEMP_PATH "blocked_recovery.tmp"
#include "zkt_legacy_attendance.h"
#include "zkt_storage_owner.h"
#include "zkt_storage_owner_platform.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static bool owner_task,required=true,full,reject;
static unsigned budget,failed_writes,nvs_fault,io_fault,peeks,stats;
static size_t read_bytes;
enum { SHORT_WRITE=1,FLUSH,SYNC,CLOSE,RENAME,STAT,REMOVE,READ };
static lq_checkpoint_t saved[2],pending_cp;
static unsigned pending_lane;
bool zj_owner_is_current_task(void) { return owner_task; }
bool zj_runtime_checkpoint_required(void) { return required; }
bool qs_local_read_begin(void) { assert(owner_task && !budget);budget=1;return true; }
bool qs_local_admit_locked(qs_admission_t policy,size_t size)
{ assert(owner_task && budget && (unsigned)policy<=QS_ADMIT_RECOVERY && size);if(full)errno=ENOSPC;return !full; }
void qs_local_end(bool ok,int error) { assert(budget);if(!ok){assert(error);++failed_writes;}budget=0; }
esp_err_t nvs_open(const char *name,int mode,nvs_handle_t *handle)
{ assert(owner_task && budget && !strcmp(name,"legacy_queues"));*handle=(unsigned)mode+1;
  if(nvs_fault==1){nvs_fault=0;return 9;}return ESP_OK; }
void nvs_close(nvs_handle_t handle) { assert(handle); }
static unsigned key_lane(const char *key) { if(!strcmp(key,"ords_pending"))return 0;assert(!strcmp(key,"blocked"));return 1; }
esp_err_t nvs_get_blob(nvs_handle_t handle,const char *key,void *out,size_t *size)
{ assert(handle==1 && *size==sizeof(lq_checkpoint_t));unsigned lane=key_lane(key);
  if(!saved[lane].version)return ESP_ERR_NVS_NOT_FOUND;
  memcpy(out,&saved[lane],*size);return ESP_OK; }
esp_err_t nvs_set_blob(nvs_handle_t handle,const char *key,const void *in,size_t size)
{ assert(handle==2 && size==sizeof(lq_checkpoint_t));pending_lane=key_lane(key);memcpy(&pending_cp,in,size);return ESP_OK; }
esp_err_t nvs_commit(nvs_handle_t handle)
{ assert(handle==2);if(nvs_fault==2){nvs_fault=0;return 9;}saved[pending_lane]=pending_cp;
  if(nvs_fault==3){nvs_fault=0;return 9;}return ESP_OK; }
/* Interpose after libc declarations, including its fortified inline stdio. */
static size_t fault_write(const void *bytes,size_t size,size_t count,FILE *file)
{ if(io_fault==SHORT_WRITE){io_fault=0;errno=ENOSPC;return fwrite(bytes,size,count/2,file);}return fwrite(bytes,size,count,file); }
static size_t fault_read(void *bytes,size_t size,size_t count,FILE *file)
{ read_bytes+=size*count;if(io_fault==READ){io_fault=0;errno=EIO;return fread(bytes,size,count/2,file);}return fread(bytes,size,count,file); }
static int fault_flush(FILE *file) { if(io_fault==FLUSH){io_fault=0;errno=EIO;return EOF;}return fflush(file); }
static int fault_sync(int fd) { if(io_fault==SYNC){io_fault=0;errno=EIO;return -1;}return fsync(fd); }
static int fault_close(FILE *file) { int r=fclose(file);if(io_fault==CLOSE){io_fault=0;errno=EIO;return EOF;}return r; }
static int fault_rename(const char *from,const char *to) { if(io_fault==RENAME){io_fault=0;errno=EIO;return -1;}return rename(from,to); }
static int fault_stat(const char *path,struct stat *st) { ++stats;if(io_fault==STAT){io_fault=0;errno=EIO;return -1;}return stat(path,st); }
static int fault_remove(const char *path) { if(io_fault==REMOVE){io_fault=0;errno=EIO;return -1;}return remove(path); }
#define fwrite fault_write
#define fread fault_read
#define fflush fault_flush
#define fsync fault_sync
#define fclose fault_close
#define rename fault_rename
#define stat(...) fault_stat(__VA_ARGS__)
#define remove fault_remove
#include "legacy_queue.c"
static dq_result_t traced_peek(legacy_queue_t *q,char *bytes,size_t capacity,lq_token_t *token)
{ ++peeks;return lq_peek(q,bytes,capacity,token); }
#define lq_peek traced_peek
#include "zkt_legacy_attendance.c"
#undef lq_peek
#undef fwrite
#undef fread
#undef fflush
#undef fsync
#undef fclose
#undef rename
#undef stat
#undef remove

static zq_store_t store={.attendance={zol_owner_append,zol_owner_peek,zol_owner_settle}};
static uint64_t now=1,next_ticket,ticket;
static zj_request_t request;
static zj_reply_t reply;
static bool executed,hold;
static int lose_operation=-1;
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned ms) { now+=(uint64_t)ms*1000; }
bool zj_owner_submit(const zj_request_t *input,uint64_t *out)
{ assert(!budget && !ticket);*out=0;if(reject)return false;
  assert(input->operation==ZJ_SEGMENTED_QUEUE && zq_request_valid(&input->input.segmented));
  request=*input;executed=false;*out=ticket=++next_ticket;return true; }
bool zj_owner_poll(uint64_t input,zj_reply_t *out,bool *complete)
{
    assert(ticket && input==ticket && !budget);*complete=false;
    if(!executed){
        memset(&reply,0,sizeof(reply));owner_task=true;
        zq_store_execute(&store,now,&request.input.segmented,&reply.segmented);
        owner_task=false;reply.result=ZJ_OK;executed=true;
        if(lose_operation==request.input.segmented.operation){hold=true;lose_operation=-1;}
    }
    if(hold)return true;
    *out=reply;*complete=true;ticket=0;return true;
}
dq_result_t qs_append_with_policy(qs_lane_t lane,const void *bytes,size_t length,qs_admission_t policy)
{ (void)lane;(void)bytes;(void)length;(void)policy;assert(false);return DQ_IO; }
dq_result_t qs_peek(qs_lane_t lane,void *bytes,size_t capacity,size_t *length,dq_token_t *token)
{ (void)lane;(void)bytes;(void)capacity;(void)length;(void)token;assert(false);return DQ_IO; }
dq_result_t qs_settle(qs_lane_t lane,const dq_token_t *token) { (void)lane;(void)token;assert(false);return DQ_IO; }
bool qs_snapshot(qs_lane_t lane,uint32_t *depth) { assert(!budget && lane==QS_ORDS);*depth=0;return true; }
bool qs_generation(char output[33]) { assert(!budget);memset(output,'a',32);output[32]=0;return true; }
bool qs_recover_step(void) { assert(false);return false; }
bool qs_verify_persistence(void) { assert(false);return false; }

#define ZONE_LITE_QUEUE_OWNER 1
#define PENDING_PATH ZOL_PENDING_PATH
#define BLOCKED_PATH ZOL_BLOCKED_PATH
#define MAX_EVENT_JSON 1024
#define ESP_LOGE(...) ((void)0)
#define ADD_WORKER_NETWORK 1
#define ADD_WORKER_COMMITTING 2
static legacy_queue_t g_legacy_pending,g_legacy_blocked;
static char delivery_buffer[16][MAX_EVENT_JSON];
static char (*g_legacy_drain_buffer)[MAX_EVENT_JSON]=delivery_buffer;
static int64_t g_ords_drain_retry_not_before_ms;
static bool g_legacy_owner_progress;
static bool backlog,receipt,evidence;
static unsigned sends,evidence_sends,delivery_faults;
static char delivered[DQ_MAX_RECORD_BYTES+1];
static size_t evidence_length;
typedef enum { ORACLE_DELIVERY_ACKED,ORACLE_DELIVERY_RETRYABLE,ORACLE_DELIVERY_CORRUPT_LOCAL_ROW,
    ORACLE_DELIVERY_IDENTITY_UNRESOLVED,ORACLE_DELIVERY_PERMANENT_REJECTION } oracle_delivery_result_t;
static oracle_delivery_result_t delivery_result;
static bool legacy_attendance_owner_required(void) { return required; }
static bool storage_upgrade_segmented_writes(void) { return false; }
bool qs_local_begin(qs_admission_t policy,size_t length) { (void)policy;(void)length;assert(false);return false; }
static bool oracle_drain_segmented_slice(void) { assert(!budget && !owner_task);return false; }
static void led_status_set_backlog(bool value) { backlog=value; }
static void ords_drain_preserved_deferred(const char *stage,int error)
{ assert(!budget && !owner_task && strstr(stage,"legacy-owner-") && error);++delivery_faults; }
static void add_connector_report_ords_worker(int state) { assert(!budget && !owner_task && (state==1 || state==2)); }
static oracle_delivery_result_t oracle_send_live(const char *line)
{ assert(!budget && !owner_task);size_t n=strlen(line);assert(n<sizeof(delivered));memcpy(delivered,line,n+1);++sends;return delivery_result; }
static bool add_enqueue_json_receipts(char **events,size_t count,const char *path)
{ assert(!budget && !owner_task && count==1 && !strcmp(events[0],delivered) && !strcmp(path,"FIRMWARE_LIVE"));return receipt; }
static bool add_connector_transfer_queue_evidence(const char *queue,const char *generation,const char *id,
    const void *bytes,size_t length,const char *serial,const char *reason)
{
    assert(!budget && !owner_task && !strcmp(queue,"ords_legacy") && strstr(generation,"-legacy-") &&
        strchr(id,':') && !serial && (!strcmp(reason,"MALFORMED") || !strcmp(reason,"IDENTITY_UNRESOLVED")));
    ++evidence_sends;evidence_length=length;memcpy(delivered,bytes,length);return evidence;
}
static char *oracle_mark_permanent_rejection(const char *line)
{ assert(!budget && !owner_task && line[0]);return strdup("{\"event_uid\":\"retained-uid\",\"blocked_reason\":\"REJECTED\"}"); }
#include "legacy_delivery_actual.inc"
static void seed(const char *path,const void *bytes,size_t length)
{ FILE *file=fopen(path,"wb");assert(file && fwrite(bytes,1,length,file)==length && fclose(file)==0); }
static bool exists(const char *path) { struct stat st;return stat(path,&st)==0; }
static void reset_lane(unsigned lane)
{
    assert(!ticket && !budget);
    remove(lanes[lane].path);remove(lanes[lane].backup);remove(lanes[lane].temporary);
    memset(&lanes[lane].queue,0,sizeof(lanes[lane].queue));lanes[lane].initialized=false;
    memset(&saved[lane],0,sizeof(saved[lane]));
    if(!lane)atomic_store(&pending_empty,false);
}
static dq_result_t peek_lane(unsigned lane,char *bytes,size_t *length,lq_token_t *token)
{ return zq_attendance_legacy_peek(lane,bytes,DQ_MAX_RECORD_BYTES,length,token); }
int main(void)
{
    char bytes[DQ_MAX_RECORD_BYTES];size_t length=99;lq_token_t token;
    assert(!zol_pending_verified_empty());
    assert(zol_owner_peek(0,bytes,sizeof(bytes),&length,&token)==DQ_PENDING && !length && !token.end);
    reject=true;assert(zol_append(0,"first",5,QS_ADMIT_LIVE)==DQ_PENDING && !exists(ZOL_PENDING_PATH));reject=false;
    assert(peek_lane(0,bytes,&length,&token)==DQ_EMPTY && zol_pending_verified_empty());
    unsigned before=peeks,before_stats=stats;
    for(unsigned i=0;i<100;++i)assert(peek_lane(0,bytes,&length,&token)==DQ_EMPTY);
    assert(peeks==before && stats==before_stats);
    full=true;assert(zol_append(0,"first",5,QS_ADMIT_LIVE)==DQ_FULL && !failed_writes && !zol_pending_verified_empty());full=false;
    assert(zol_append(0,"same",4,QS_ADMIT_LIVE)==DQ_OK);
    assert(zol_append(0,"same",4,QS_ADMIT_LIVE)==DQ_OK);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && length==5 && !memcmp(bytes,"same\n",5));
    nvs_fault=2;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_IO && !nvs_fault && !saved[0].offset);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK);
    lq_token_t first=token;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_OK && saved[0].offset==5);
    assert(zq_attendance_legacy_settle(0,&first,false)==DQ_STALE);
    /* The unchanged legacy reader recovers a checkpoint written by the owner. */
    legacy_queue_t old;owner_task=true;budget=1;
    assert(lq_open(&old,ZOL_PENDING_PATH,(lq_port_t){load,commit,&lanes[0]})==DQ_OK && old.checkpoint.offset==5);
    budget=0;owner_task=false;
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && token.offset==5 && token.crc==first.crc);
    nvs_fault=3;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_IO && !nvs_fault && saved[0].offset==10);
    assert(peek_lane(0,bytes,&length,&token)==DQ_PENDING);
    assert(peek_lane(0,bytes,&length,&token)==DQ_EMPTY && !exists(ZOL_PENDING_PATH));
    reset_lane(0);seed(ZOL_PENDING_PATH,"active\n",7);seed(ZOL_PENDING_BACKUP_PATH,"backup\n",7);seed(ZOL_PENDING_TEMP_PATH,"temp\n",5);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && length==7);
    io_fault=RENAME;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_IO && !io_fault);
    assert(!exists(ZOL_PENDING_PATH) && exists(ZOL_PENDING_BACKUP_PATH) && !zol_pending_verified_empty());
    io_fault=RENAME;assert(peek_lane(0,bytes,&length,&token)==DQ_IO && !io_fault && !length);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && !memcmp(bytes,"backup\n",7));
    assert(zq_attendance_legacy_settle(0,&token,false)==DQ_OK);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && !memcmp(bytes,"temp\n",5));
    assert(zq_attendance_legacy_settle(0,&token,false)==DQ_OK);
    assert(peek_lane(0,bytes,&length,&token)==DQ_EMPTY && zol_pending_verified_empty());
    for(unsigned fault=SHORT_WRITE;fault<=CLOSE;++fault){
        reset_lane(1);assert(peek_lane(1,bytes,&length,&token)==DQ_EMPTY);
        unsigned failed=failed_writes;io_fault=fault;
        assert(zol_append(1,"partial-write",13,QS_ADMIT_LIVE)==DQ_IO && !io_fault && failed_writes==failed+1);
        assert(peek_lane(1,bytes,&length,&token)==DQ_OK && length && exists(ZOL_BLOCKED_PATH));
        if(token.evidence_required)assert(zq_attendance_legacy_settle(1,&token,false)==DQ_STALE);
        assert(zq_attendance_legacy_settle(1,&token,true)==DQ_OK);
    }
    reset_lane(1);seed(ZOL_BLOCKED_BACKUP_PATH,"raw\0tail",8);
    io_fault=STAT;assert(peek_lane(1,bytes,&length,&token)==DQ_IO && !io_fault && !length);
    io_fault=CLOSE;assert(peek_lane(1,bytes,&length,&token)==DQ_IO && !io_fault && !length);
    assert(peek_lane(1,bytes,&length,&token)==DQ_OK && length==8 && !memcmp(bytes,"raw\0tail",8));
    io_fault=REMOVE;assert(zq_attendance_legacy_settle(1,&token,true)==DQ_IO && !io_fault && exists(ZOL_BLOCKED_PATH));
    assert(peek_lane(1,bytes,&length,&token)==DQ_OK && length==8);
    assert(zq_attendance_legacy_settle(1,&token,true)==DQ_OK);
    /* A lost append response retains its committed bytes and blocks replacement
     * until the exact previous result is collected. */
    reset_lane(0);lose_operation=ZQ_APPEND_COMMIT;
    assert(zol_append(0,"first",5,QS_ADMIT_LIVE)==DQ_PENDING && ticket && exists(ZOL_PENDING_PATH));
    assert(zol_append(0,"next",4,QS_ADMIT_LIVE)==DQ_PENDING);hold=false;
    assert(zol_append(0,"next",4,QS_ADMIT_LIVE)==DQ_OK);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && length==6 && !memcmp(bytes,"first\n",6));
    lose_operation=ZQ_SETTLE;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_PENDING && saved[0].offset==6);
    hold=false;assert(zq_attendance_legacy_settle(0,&token,false)==DQ_STALE);
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && length==5 && !memcmp(bytes,"next\n",5));
    assert(zq_attendance_legacy_settle(0,&token,false)==DQ_OK);
    /* Exact consumed-prefix verification yields while another lane progresses. */
    reset_lane(0);char large[LQ_RECOVERY_SLICE_BYTES*3+5];memset(large,'x',sizeof(large));
    large[LQ_RECOVERY_SLICE_BYTES*3-1]='\n';memcpy(large+LQ_RECOVERY_SLICE_BYTES*3,"tail\n",5);seed(ZOL_PENDING_PATH,large,sizeof(large));
    saved[0]=(lq_checkpoint_t){.version=1,.generation=4,.offset=LQ_RECOVERY_SLICE_BYTES*3,.prefix_crc=dq_crc32(large,LQ_RECOVERY_SLICE_BYTES*3)};
    saved[0].crc=dq_crc32(&saved[0],offsetof(lq_checkpoint_t,crc));
    io_fault=READ;assert(peek_lane(0,bytes,&length,&token)==DQ_IO && !io_fault && !length);
    for(unsigned i=0;i<2;++i){
        size_t read_before=read_bytes;
        assert(peek_lane(0,bytes,&length,&token)==DQ_PENDING && !length && !token.end);
        assert(read_bytes-read_before==LQ_RECOVERY_SLICE_BYTES);
        assert(zol_append(1,"independent",11,QS_ADMIT_LIVE)==DQ_OK);
    }
    assert(peek_lane(0,bytes,&length,&token)==DQ_OK && length==5 && !memcmp(bytes,"tail\n",5));
    assert(zq_attendance_legacy_settle(0,&token,false)==DQ_OK);
    /* Execute the actual gateway adapter and Oracle delivery slice. A good
     * Oracle response alone cannot retire before its durable ADD receipt. */
    reset_lane(0);reset_lane(1);receipt=false;delivery_result=ORACLE_DELIVERY_ACKED;
    assert(append_line_policy(PENDING_PATH,"{\"event_uid\":\"retained-uid\"}",QS_ADMIT_LIVE));
    oracle_drain_owned_pending();assert(sends==1 && !saved[0].offset && exists(ZOL_PENDING_PATH) && backlog);
    assert(!strcmp(delivered,"{\"event_uid\":\"retained-uid\"}\n"));
    receipt=true;oracle_drain_owned_pending();assert(sends==2 && !exists(ZOL_PENDING_PATH));
    oracle_drain_owned_pending();assert(!backlog && zol_pending_verified_empty() && !file_has_nonempty_line(PENDING_PATH));
    reject=true;assert(!append_line_policy(PENDING_PATH,"rejected-admission",QS_ADMIT_LIVE));
    assert(!zol_pending_verified_empty() && file_has_nonempty_line(PENDING_PATH));reject=false;
    assert(append_line(PENDING_PATH,"retry"));delivery_result=ORACLE_DELIVERY_RETRYABLE;
    oracle_drain_owned_pending();assert(exists(ZOL_PENDING_PATH) && !saved[0].offset);
    delivery_result=ORACLE_DELIVERY_PERMANENT_REJECTION;full=true;
    oracle_drain_owned_pending();assert(exists(ZOL_PENDING_PATH) && !exists(ZOL_BLOCKED_PATH));full=false;
    oracle_drain_owned_pending();assert(!exists(ZOL_PENDING_PATH) && exists(ZOL_BLOCKED_PATH));
    assert(peek_lane(1,bytes,&length,&token)==DQ_OK);bytes[length]=0;assert(strstr(bytes,"retained-uid"));
    reset_lane(0);seed(ZOL_PENDING_PATH,"raw\0tail",8);before=sends;evidence=false;
    oracle_drain_owned_pending();assert(sends==before && evidence_sends==1 && exists(ZOL_PENDING_PATH));
    evidence=true;oracle_drain_owned_pending();assert(sends==before && evidence_length==8 && !memcmp(delivered,"raw\0tail",8));
    assert(!exists(ZOL_PENDING_PATH));
    reset_lane(0);assert(append_line(PENDING_PATH,"unresolved"));delivery_result=ORACLE_DELIVERY_IDENTITY_UNRESOLVED;evidence=false;
    oracle_drain_owned_pending();assert(exists(ZOL_PENDING_PATH) && !saved[0].offset);
    evidence=true;oracle_drain_owned_pending();assert(!exists(ZOL_PENDING_PATH));
    assert(!delivery_faults); /* Refusals and unresolved identity are not failed persistence. */
    required=false;assert(zol_append(0,"wrong-image",11,QS_ADMIT_LIVE)==DQ_PENDING && !exists(ZOL_PENDING_PATH));
    assert(!budget && !ticket && failed_writes);
    puts("legacy attendance owner: generations, actual I/O faults, retained replies, ABI and bounded recovery passed");
}
