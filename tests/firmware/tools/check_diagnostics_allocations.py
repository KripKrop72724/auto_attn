"""Fault-test actual diagnostics serialization; partial health is never published."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
firmware = ROOT / "firmware/zone_lite/main"
source = (firmware / "add_connector.c").read_text()
functions = source[source.index("static bool append_worker_diagnostic("):
                   source.index("static void heartbeat_task(", source.index("static bool append_worker_diagnostic("))]
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
#define ESP_OK 0
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct {bool add_source_coverage_certified,committed_source_known; int64_t last_light_check_uptime_ms,last_tail_audit_uptime_ms;uint32_t committed_source_generation,committed_source_cursor;} add_zkt_telemetry_t;
static add_zkt_telemetry_t zkt={.add_source_coverage_certified=true,.committed_source_known=true,.last_light_check_uptime_ms=2000,.last_tail_audit_uptime_ms=4000,.committed_source_generation=9,.committed_source_cursor=100};
typedef int esp_err_t;
typedef enum {ADD_WORKER_IDLE,ADD_WORKER_READING,ADD_WORKER_NETWORK,ADD_WORKER_COMMITTING,ADD_WORKER_RESOURCE} add_worker_operation_t;
typedef struct {int *lock;bool depth_known;unsigned depth;const char *path;} add_outbox_t;
static int held;
static add_outbox_t s_live_outbox={&held,true,3,"absent-live"},s_bulk_outbox={&held,false,0,"absent-bulk"};
static size_t calls,fail_at;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
static int xSemaphoreTake(int *lock,unsigned timeout){(void)timeout;assert(!*lock);*lock=1;return 1;}
static void xSemaphoreGive(int *lock){assert(*lock);*lock=0;}
static int64_t monotonic_ms(void){return 5000;}
static const char *led_status_current_name(void){return "HEALTHY";}
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
qs_health_t qs_health(void){return (qs_health_t){.observed=true,.available=true,.write_failures=2,.read_failures=3,.admission_reserve_bytes=1048576,.last_error=EIO,.last_operation="local_write_commit"};}
bool qs_snapshot(qs_lane_t lane,uint32_t *depth){*depth=lane+1;return lane!=QS_BLOCKED;}
''' + functions + r'''
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 cJSON *payload=cJSON_CreateObject();assert(payload);calls=0;
 append_firmware_diagnostics(payload, &zkt, "LIVE_CAPTURE");size_t total=calls;
 cJSON *diagnostics=cJSON_GetObjectItemCaseSensitive(payload,"diagnostics");assert(diagnostics);
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(diagnostics,"reconciliation_mode")->valuestring,"APPEND_TAIL_ASSURANCE"));
 assert(cJSON_GetObjectItemCaseSensitive(diagnostics,"committed_source_cursor")->valueint==100);
 cJSON *storage=cJSON_GetObjectItemCaseSensitive(diagnostics,"storage");
 assert(!strcmp(cJSON_GetObjectItemCaseSensitive(storage,"durability")->valuestring,"DEGRADED"));
 assert(cJSON_GetObjectItemCaseSensitive(storage,"write_failures")->valueint==2);
 assert(cJSON_GetObjectItemCaseSensitive(storage,"read_failures")->valueint==3);
 cJSON *queues=cJSON_GetObjectItemCaseSensitive(diagnostics,"queues");assert(cJSON_GetArraySize(queues)==8);
 cJSON *unknown=cJSON_GetArrayItem(queues,2+QS_BLOCKED);assert(cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(unknown,"count_known")));
 assert(!cJSON_HasObjectItem(unknown,"records"));cJSON_Delete(payload);
 for(size_t i=1;i<=total;i++){
  fail_at=0;payload=cJSON_CreateObject();assert(payload);calls=0;fail_at=i;
  append_firmware_diagnostics(payload, &zkt, "LIVE_CAPTURE");assert(!cJSON_HasObjectItem(payload,"diagnostics") && !held);cJSON_Delete(payload);
 }
 fail_at=0;memset(&zkt,0,sizeof(zkt));
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
                    str(unit), str(cjson / "cJSON.c"), "-lm", "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True)
