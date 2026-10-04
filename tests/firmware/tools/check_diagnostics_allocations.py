"""Fault-test actual diagnostics serialization; partial health is never published."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
firmware = ROOT / "firmware/zone_lite/main"
source = (firmware / "add_connector.c").read_text()
outbox_type = source[source.index("typedef struct {\n    const char *path;"):
                     source.index("} add_outbox_t;") + len("} add_outbox_t;")]
functions = source[source.index("static bool append_worker_diagnostic("):
                   source.index("static void heartbeat_task(", source.index("static bool append_worker_diagnostic("))]
runtime_source = (firmware / "zkt_journal_runtime.c").read_text()
runtime_function = runtime_source[runtime_source.index("bool zj_runtime_append_diagnostics("):]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <errno.h>
#include "cJSON.h"
#include "queue_store.h"
#include "zkt_journal_boot.h"
#include "zkt_journal_diagnostics.h"
#define ESP_OK 0
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct {bool add_source_coverage_certified,committed_source_known; int64_t last_light_check_uptime_ms,last_tail_audit_uptime_ms;uint32_t committed_source_generation,committed_source_cursor;} add_zkt_telemetry_t;
static add_zkt_telemetry_t zkt={.add_source_coverage_certified=true,.committed_source_known=true,.last_light_check_uptime_ms=2000,.last_tail_audit_uptime_ms=4000,.committed_source_generation=9,.committed_source_cursor=100};
typedef int esp_err_t;
typedef enum {ADD_WORKER_IDLE,ADD_WORKER_READING,ADD_WORKER_NETWORK,ADD_WORKER_COMMITTING,ADD_WORKER_RESOURCE} add_worker_operation_t;
typedef int *SemaphoreHandle_t;
''' + outbox_type + r'''
static bool owned_legacy=true;
static bool add_legacy_owner_required(void){return owned_legacy;}
static int held;
static add_outbox_t s_live_outbox={.lock=&held,.depth_known=true,.depth=3,.path="absent-live",.owner_bytes_known=true,.owner_bytes=123};
static add_outbox_t s_bulk_outbox={.lock=&held,.depth_known=false,.path="absent-bulk"};
static size_t calls,fail_at;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
static int xSemaphoreTake(int *lock,unsigned timeout){(void)timeout;assert(!*lock);*lock=1;return 1;}
static void xSemaphoreGive(int *lock){assert(*lock);*lock=0;}
static uint64_t sample_clock=5000;
static int64_t monotonic_ms(void){return (int64_t)sample_clock;}
static uint32_t now_ms(void){return (uint32_t)sample_clock;}
static int64_t esp_timer_get_time(void){return (int64_t)sample_clock*1000;}
static zj_boot_mode_t configured_mode=ZJ_BOOT_BRIDGE;
static bool observed=true;
static zj_boot_t health={.mode=ZJ_BOOT_BRIDGE,.phase=ZJ_BOOT_READY,.delivery_authority=ZJ_AUTHORITY_LEGACY,.reader_ready=true,.sampled_ms=4000,.owner_starts=1,.transport_starts=1};
static zj_boot_mode_t mode(void){return configured_mode;}
static bool zj_runtime_health(zj_boot_t *out){*out=health;return observed;}
static bool owner_observed=true, transport_observed=true, capture_observed=true;
static zj_owner_health_t owner={.started=true,.ready=true,.sampled_uptime_us=4000000,
 .inventory_known=true,.verified_empty=true};
static zj_transport_health_t transport={.started=true,.sampled_ms=4000};
static zj_capture_health_t capture={.sampled_ms=4000};
bool zj_owner_health(zj_owner_health_t *out){*out=owner;return owner_observed;}
bool zj_transport_health(zj_transport_health_t *out){*out=transport;return transport_observed;}
static bool zj_capture_runtime_health(zj_capture_health_t *out){*out=capture;return capture_observed;}
static const char s_boot_id[]="allocation-test-boot";
#define MALLOC_CAP_INTERNAL 1
#define MALLOC_CAP_8BIT 2
static size_t heap_caps_get_free_size(unsigned caps){assert(caps==3);return 100000;}
static size_t heap_caps_get_largest_free_block(unsigned caps){assert(caps==3);return 64000;}
static void led_status_local_failure_source(char *out,size_t size){assert(size>0);out[0]=0;}
static bool storage_upgrade_ready(void){return true;}
static const char *storage_upgrade_error(void){return "";}
static const char *storage_upgrade_contract(void){return "contract";}
static int esp_spiffs_info(const void *label,size_t *total,size_t *used){(void)label;*total=8000000;*used=2000000;return 0;}
static struct {unsigned total;} s_outbox_retry={2};
static unsigned s_ords_start_attempts=2;
static int *s_outbox_task_handle=&held;
static uint32_t s_outbox_tick_ms=4000,s_ords_worker_tick_ms=4000;
static bool s_outbox_buffer_ready=true,s_ords_worker_started=true;
static add_worker_operation_t s_add_worker_operation=ADD_WORKER_IDLE,s_ords_worker_operation=ADD_WORKER_NETWORK;
static bool legacy_healthy;
qs_health_t qs_health(void){return (qs_health_t){.observed=true,.available=true,.write_failures=2,.read_failures=3,
 .recovery_complete=legacy_healthy,.persistence_verified=legacy_healthy,
 .admission_reserve_bytes=1048576,.last_error=legacy_healthy?0:EIO,.last_operation="local_write_commit",
 .legacy={.observed=true,.read_faults=legacy_healthy?0:1,.append_faults=legacy_healthy?0:2,
 .retire_faults=legacy_healthy?0:3,.read_recoveries=7,.error=legacy_healthy?0:EIO,
 .queue="ords_pending",.operation="legacy_read"}};}
bool qs_snapshot(qs_lane_t lane,uint32_t *depth){*depth=lane+1;return lane!=QS_BLOCKED;}
''' + runtime_function + functions + r'''
static cJSON *named(cJSON *array,const char *name){
 cJSON *item; cJSON_ArrayForEach(item,array){
  cJSON *key=cJSON_GetObjectItemCaseSensitive(item,"name");
  if(cJSON_IsString(key)&&!strcmp(key->valuestring,name))return item;
 }return NULL;
}
static const char *string(cJSON *object,const char *key){
 cJSON *value=cJSON_GetObjectItemCaseSensitive(object,key);assert(cJSON_IsString(value));return value->valuestring;
}
static void check_ownership(const char *profile,const char *authority,bool writer){
 fail_at=0;cJSON *payload=cJSON_CreateObject();assert(payload);calls=0;
 append_firmware_diagnostics(payload,&zkt,"LIVE_CAPTURE");size_t total=calls;
 cJSON *diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"runtime_profile")->valuestring,profile));
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"delivery_authority")->valuestring,authority));
 cJSON *runtime=cJSON_GetObjectItemCaseSensitive(diagnostics,"journal_runtime");assert(runtime);
 assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(runtime,"writer_ready"))==writer);
 cJSON *workers=cJSON_GetObjectItemCaseSensitive(diagnostics,"workers");
 assert(named(workers,"add_delivery"));
 if(!strcmp(profile,"ZKT_JOURNAL_V1")){
  assert(cJSON_GetArraySize(workers)==5 && named(workers,"capture") && named(workers,"storage_owner"));
  assert(named(workers,"legacy_add_delivery") && named(workers,"legacy_ords_delivery") && !named(workers,"ords_delivery"));
 }
 cJSON_Delete(payload);
 for(size_t i=1;i<=total;i++){
  fail_at=0;payload=cJSON_CreateObject();assert(payload);calls=0;fail_at=i;
  append_firmware_diagnostics(payload,&zkt,"LIVE_CAPTURE");
  assert(!cJSON_HasObjectItem(payload,"diagnostics")&&!held);cJSON_Delete(payload);
 }
 fail_at=0;
}
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 cJSON *payload=cJSON_CreateObject();assert(payload);calls=0;
 append_firmware_diagnostics(payload, &zkt, "LIVE_CAPTURE");size_t total=calls;
 cJSON *diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"reconciliation_mode")->valuestring,"APPEND_TAIL_ASSURANCE"));
 assert(cJSON_GetObjectItemCaseSensitive(diagnostics,"committed_source_cursor")->valueint==100);
 cJSON *runtime=cJSON_GetObjectItemCaseSensitive(diagnostics,"journal_runtime");assert(runtime);
 assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(runtime,"reader_ready")));
 assert(cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(runtime,"writer_ready")));
 assert(cJSON_GetObjectItemCaseSensitive(runtime,"storage_starts")->valueint==1);
 cJSON *storage=cJSON_GetObjectItemCaseSensitive(diagnostics,"storage");
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(storage,"durability")->valuestring,"DEGRADED"));
 assert(cJSON_GetObjectItemCaseSensitive(storage,"write_failures")->valueint==2);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"read_failures")->valueint==3);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"legacy_read_faults")->valueint==1);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"legacy_append_faults")->valueint==2);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"legacy_retire_faults")->valueint==3);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"legacy_read_recoveries")->valueint==7);
 assert(!strcmp(string(storage,"legacy_error_queue"),"ords_pending"));
 cJSON *queues=cJSON_GetObjectItemCaseSensitive(diagnostics,"queues");assert(cJSON_GetArraySize(queues)==11);
 assert(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(queues,0),"bytes")->valueint==123);
 assert(!cJSON_HasObjectItem(cJSON_GetArrayItem(queues,1),"bytes"));
 cJSON *unknown=cJSON_GetArrayItem(queues,2+QS_BLOCKED);assert(cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(unknown,"count_known")));
 assert(!cJSON_HasObjectItem(unknown,"records"));cJSON_Delete(payload);
 for(size_t i=1;i<=total;i++){
  fail_at=0;payload=cJSON_CreateObject();assert(payload);calls=0;fail_at=i;
  append_firmware_diagnostics(payload, &zkt, "LIVE_CAPTURE");assert(!cJSON_HasObjectItem(payload,"diagnostics") && !held);cJSON_Delete(payload);
 }
 fail_at=0;owned_legacy=false;
 payload=cJSON_CreateObject();append_firmware_diagnostics(payload,&zkt,"LIVE_CAPTURE");
 diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
 queues=cJSON_GetObjectItemCaseSensitive(diagnostics,"queues");
 assert(cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(queues,0),"bytes")->valueint==0);
 cJSON_Delete(payload);owned_legacy=true;memset(&zkt,0,sizeof(zkt));
 payload=cJSON_CreateObject();append_firmware_diagnostics(payload,&zkt,"LIVE_CAPTURE");
 diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"reconciliation_mode")->valuestring,"IDLE"));
 assert(!cJSON_HasObjectItem(diagnostics,"committed_source_cursor"));
 assert(!cJSON_HasObjectItem(diagnostics,"source_generation"));
 assert(!cJSON_HasObjectItem(diagnostics,"last_light_check_uptime_ms"));
 assert(!cJSON_HasObjectItem(diagnostics,"last_tail_audit_uptime_ms"));cJSON_Delete(payload);
 payload=cJSON_CreateObject();append_firmware_diagnostics(payload,&zkt,"FULL_RECONCILE");
 diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"reconciliation_mode")->valuestring,"FULL_RECONCILE"));
 cJSON_Delete(payload);
 check_ownership("ZKT_LEGACY","LEGACY_DUAL",false);
 health.delivery_authority=ZJ_AUTHORITY_ADD;health.writer_ready=true;
 check_ownership("ZKT_JOURNAL_V1","ADD",true); /* Compatible rollback bridge. */
 configured_mode=health.mode=ZJ_BOOT_WRITER;
 check_ownership("ZKT_JOURNAL_V1","ADD",true);
 health.sampled_ms=5000U-45000U;
 check_ownership("ZKT_JOURNAL_V1","UNKNOWN",false);
 health.sampled_ms=4000;observed=false;
 check_ownership("ZKT_JOURNAL_V1","UNKNOWN",false);
 observed=true;health.delivery_authority=ZJ_AUTHORITY_UNKNOWN;health.writer_ready=false;
 check_ownership("ZKT_JOURNAL_V1","UNKNOWN",false);
 configured_mode=ZJ_BOOT_BRIDGE; /* A mismatched old snapshot cannot authorize capture. */
 check_ownership("ZKT_JOURNAL_V1","UNKNOWN",false);
 configured_mode=health.mode=ZJ_BOOT_DISABLED;
 check_ownership("ZKT_LEGACY","LEGACY_DUAL",false);
 /* Actual serialization of idle, failed, stale and wrapping snapshots. */
 configured_mode=health.mode=ZJ_BOOT_WRITER;health.delivery_authority=ZJ_AUTHORITY_ADD;
 health.writer_ready=health.capture_started=true;health.phase=ZJ_BOOT_READY;health.sampled_ms=4000;
 legacy_healthy=true;
 for(unsigned scenario=0;scenario<14;scenario++){
  sample_clock=5000;
  owner=(zj_owner_health_t){.started=true,.ready=true,.sampled_uptime_us=4000000,
   .inventory_known=true,.verified_empty=true};
  capture=(zj_capture_health_t){.sampled_ms=1}; /* Quiet site is not a stalled capture task. */
  for(unsigned i=0;i<100;i++){
   zj_capture_latency_record(&capture.packet_commit_latency,i<99?251:900);
   zj_capture_latency_record(&capture.fragment_commit_latency,10);
  }
  capture.packets=capture.fragments=100;capture.failures=3;capture.timeouts=1;
  transport=(zj_transport_health_t){.started=true,.sampled_ms=4000};
  owner_observed=true;
  if(scenario==1){owner.append_observed=true;owner.last_append_result=ZJ_FULL;}
  if(scenario==2){owner_observed=false;}
  if(scenario==3){owner.operation_running=true;owner.operation_started_us=1;owner.sampled_uptime_us=6000000;}
  if(scenario==4){owner.pending_appends=1;}
  if(scenario==5){capture.running=true;capture.started_ms=5000U-15000U;}
  if(scenario==6){transport.delivery.phase=ZJ_DELIVERY_SEND;transport.delivery.phase_started_ms=5000U-20000U;}
  if(scenario==7){owner.failures=7;owner.filesystem_error=EIO;owner.failed_operation="old_sync";}
  if(scenario==8){transport.delivery.phase=ZJ_DELIVERY_WAIT;transport.delivery.phase_started_ms=4000;}
  if(scenario==9){transport.delivery.consecutive_failures=2;transport.delivery.last_failure="add_custody";}
  if(scenario==10){sample_clock=25000;owner.sampled_uptime_us=24000000;owner.operation_running=true;owner.operation_started_us=10000000;}
  if(scenario==11){transport.sampled_ms=5000U-45000U;}
  if(scenario==12){sample_clock=(uint64_t)UINT32_MAX+5000;owner.sampled_uptime_us=(sample_clock-1000)*1000;capture.sampled_ms=UINT32_MAX-999;}
  if(scenario==13)capture.sampled_ms=5000U-45000U;
  payload=cJSON_CreateObject();append_firmware_diagnostics(payload,&zkt,"LIVE_CAPTURE");
  diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
  storage=cJSON_GetObjectItemCaseSensitive(diagnostics,"storage");
  cJSON *workers=cJSON_GetObjectItemCaseSensitive(diagnostics,"workers");
  cJSON *timing=cJSON_GetObjectItemCaseSensitive(named(workers,"capture"),"packet_commit_latency_ms");
  if(scenario==13)assert(!timing);
  else{
   assert(timing && cJSON_GetObjectItemCaseSensitive(timing,"schema_version")->valueint==1);
   assert(cJSON_GetObjectItemCaseSensitive(timing,"samples")->valueint==100);
   assert(cJSON_GetObjectItemCaseSensitive(timing,"max_ms")->valueint==900);
   cJSON *bins=cJSON_GetObjectItemCaseSensitive(timing,"buckets");
   assert(cJSON_GetArraySize(bins)==13 && cJSON_GetArrayItem(bins,8)->valueint==99 && cJSON_GetArrayItem(bins,9)->valueint==1);
   assert(cJSON_GetObjectItemCaseSensitive(named(workers,"capture"),"failures")->valueint==3);
   assert(cJSON_GetObjectItemCaseSensitive(named(workers,"capture"),"timeouts")->valueint==1);
  }
  queues=cJSON_GetObjectItemCaseSensitive(diagnostics,"queues");
  cJSON *journal=named(queues,"journal"),*migration=named(queues,"legacy_migration");assert(journal&&migration);
  assert(!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(migration,"count_known")) && !cJSON_HasObjectItem(migration,"records"));
  assert(!strcmp(string(storage,"durability"),scenario==1?"FULL":scenario==2||scenario==3?"UNKNOWN":scenario==10?"DEGRADED":"HEALTHY"));
  if(scenario==4)assert(!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(journal,"count_known"))&&!cJSON_HasObjectItem(journal,"records"));
  if(scenario==5)assert(!strcmp(string(named(workers,"capture"),"state"),"FAULT"));
  if(scenario==6)assert(!strcmp(string(named(workers,"add_delivery"),"state"),"FAULT"));
  if(scenario==8)assert(!strcmp(string(named(workers,"add_delivery"),"state"),"RUNNING"));
  if(scenario==9)assert(!strcmp(string(named(workers,"add_delivery"),"state"),"WAITING_NETWORK"));
  if(scenario==10)assert(!strcmp(string(named(workers,"storage_owner"),"state"),"FAULT"));
  if(scenario==11)assert(!strcmp(string(named(workers,"add_delivery"),"state"),"FAULT"));
  if(scenario==12)assert(cJSON_GetObjectItemCaseSensitive(named(workers,"capture"),"last_activity_uptime_ms")->valuedouble==(double)(UINT32_MAX-999));
  if(!scenario){
   assert(!strcmp(string(named(workers,"capture"),"state"),"RUNNING"));
   assert(!strcmp(string(named(workers,"capture"),"execution_model"),"ON_DEMAND"));
   assert(cJSON_GetObjectItemCaseSensitive(named(workers,"capture"),"last_activity_uptime_ms")->valueint==1);
   assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(journal,"count_known")));
   assert(cJSON_GetObjectItemCaseSensitive(journal,"records")->valueint==0);
  }
  cJSON_Delete(payload);
  check_ownership("ZKT_JOURNAL_V1","ADD",true);
 }
 puts("diagnostics allocation regressions passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "diagnostics.c"
    unit.write_text(program)
    executable = temporary / "diagnostics"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(cjson), "-I", str(firmware),
                    str(unit), str(cjson / "cJSON.c"), str(firmware / "zkt_journal_boot.c"),
                    str(firmware / "zkt_journal_compat.c"), str(firmware / "durable_queue.c"),
                    str(firmware / "zkt_journal_diagnostics.c"),
                    "-lm", "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True)
