"""Actual OTA control flow: local validity precedes network reporting."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def compile_and_run(tmp_path, program, *, family=0, bridge_version="2.6.16"):
    unit = tmp_path / "boot.c"
    unit.write_text(program)
    binary = tmp_path / "boot"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", f"-DZONE_LITE_HIKVISION={family}", f'-DZJ_BRIDGE_VERSION="{bridge_version}"',
        "-I", str(MAIN), str(unit), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)


@pytest.mark.parametrize("family", [0, 1])
@pytest.mark.parametrize("bridge_version", ["2.6.16", "2.6.17", "2.6.18", "2.6.19"])
def test_boot_confirmation_network_loss_and_checkpoint_boundaries(tmp_path, family, bridge_version):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("static bool uses_local_boot_confirmation(void)\n{")
    production = source[start:source.index("static bool acknowledge_pending_success(void)\n{", start)]
    harness = r'''
#include "ota_checkpoint.h"
#include <assert.h>
#include <stdio.h>
#define ESP_OK 0
#define OTA_BOOT_CONFIRM_SECONDS 30
#define OTA_BOOT_HEALTH_REPORT_SECONDS 5
#define pdMS_TO_TICKS(x) (x)
typedef struct {char project_name[32],version[32];} esp_app_desc_t;
static esp_app_desc_t app={"zone_lite","2.7.0"};
static ota_journal_t s_journal,durable;
static char s_last_error[64];
static unsigned s_boot_health_checks,reports,saves,marked,rollbacks,remote_state,fail_save,fail_report;
static bool s_boot_health_last_ready,local_ready=true,network,legacy_ready=true,lost_response,actual_valid,fail_mark;
static bool s_failed_boot_pending;
static uint64_t now_us;
static const esp_app_desc_t *esp_app_get_description(void){return &app;}
static int64_t esp_timer_get_time(void){return now_us;}
static void vTaskDelay(unsigned ms){now_us+=(uint64_t)ms*1000;}
static size_t strlcpy(char *out,const char *in,size_t n){size_t len=strlen(in);if(n){size_t k=len<n-1?len:n-1;memcpy(out,in,k);out[k]=0;}return len;}
static const char *add_connector_local_boot_health_error(void){return local_ready?NULL:"BOOT_LOCAL_TEST";}
static bool add_connector_boot_health_ready(void){return legacy_ready;}
static int esp_ota_mark_app_valid_cancel_rollback(void){assert(local_ready || legacy_ready);++marked;if(fail_mark)return -1;actual_valid=true;return ESP_OK;}
static int esp_ota_mark_app_invalid_rollback_and_reboot(void){++rollbacks;return ESP_OK;}
static bool save_journal(void){if(++saves==fail_save){s_journal=durable;return false;}durable=s_journal;return true;}
static bool clear_journal(void){memset(&s_journal,0,sizeof(s_journal));return save_journal();}
static bool acknowledge_pending_success(void){return false;}
static bool report_state(const char *state,const char *error)
{
    (void)error;++reports;if(!network)return false;
    bool fail=reports==fail_report;if(fail && !lost_response)return false;
    if(!strcmp(state,"BOOTED_PENDING")){assert(remote_state<=1);remote_state=1;}
    else if(!strcmp(state,"RECONCILING")){assert(remote_state==1 || remote_state==2);remote_state=2;}
    else assert(!strcmp(state,"FAILED") || !strcmp(state,"ROLLED_BACK"));
    return !fail;
}
/* PRODUCTION */
static void reset(void)
{
    memset(&s_journal,0,sizeof(s_journal));strcpy(s_journal.deployment_id,"synthetic");
    strcpy(s_journal.target_version,"2.7.0");strcpy(s_journal.state,"READY_TO_BOOT");durable=s_journal;
    reports=saves=marked=rollbacks=remote_state=fail_save=fail_report=s_boot_health_checks=0;
    local_ready=legacy_ready=true;network=false;lost_response=actual_valid=fail_mark=false;now_us=0;
    s_failed_boot_pending=false;
    strcpy(app.project_name,"zone_lite");strcpy(app.version,"2.7.0");
}
int main(void)
{
    reset();
#if !ZONE_LITE_HIKVISION
    assert(uses_local_boot_confirmation());
    fail_mark=true;assert(!confirm_or_report_rollback());
    assert(!actual_valid && !reports && !saves && !strcmp(s_last_error,"BOOT_LOCAL_MARK_VALID_FAILED"));
    reset();assert(!confirm_or_report_rollback());
    assert(marked==1 && actual_valid && !rollbacks && !strcmp(durable.state,"LOCAL_VALIDATED"));
    now_us=700000000000ULL;local_ready=false;legacy_ready=false;
    assert(!confirm_or_report_rollback() && marked==1 && !rollbacks);
    network=true;assert(confirm_or_report_rollback());
    assert(remote_state==2 && !strcmp(durable.state,"RECONCILING"));
    assert(confirm_or_report_rollback() && marked==1);
    for(unsigned failure=1;failure<=3;++failure){
        reset();network=true;fail_save=failure;
        assert(!confirm_or_report_rollback() && marked==1 && actual_valid && !rollbacks);
        if(failure==1)assert(!reports && remote_state==0);
        fail_save=0;s_journal=durable; /* reset after interrupted checkpoint */
        assert(confirm_or_report_rollback());assert(remote_state==2 && !strcmp(durable.state,"RECONCILING"));
    }
    for(unsigned failure=1;failure<=2;++failure)for(unsigned lost=0;lost<2;++lost){
        reset();network=true;fail_report=failure;lost_response=lost;
        assert(!confirm_or_report_rollback() && marked==1 && !rollbacks);
        fail_report=0;s_journal=durable;now_us+=4000000000ULL;
        assert(confirm_or_report_rollback() && marked==1 && remote_state==2);
    }
    reset();local_ready=false;legacy_ready=false;
    assert(!confirm_or_report_rollback() && !marked && !rollbacks && s_failed_boot_pending);
    assert(now_us>=OTA_BOOT_CONFIRM_SECONDS*1000000ULL);
    reset();strcpy(app.version,ZJ_BRIDGE_VERSION);strcpy(s_journal.target_version,ZJ_BRIDGE_VERSION);durable=s_journal;
    assert(uses_local_boot_confirmation() && !confirm_or_report_rollback());
    assert(marked==1 && !strcmp(durable.state,"LOCAL_VALIDATED"));
#else
    assert(!uses_local_boot_confirmation());network=true;
    assert(confirm_or_report_rollback() && marked==1 && remote_state==2);
#endif
    reset();strcpy(app.version,"2.6.15");strcpy(s_journal.target_version,"2.6.15");durable=s_journal;
    assert(!uses_local_boot_confirmation());network=true;
    assert(confirm_or_report_rollback() && marked==1 && remote_state==2);
    puts("Local boot proof, legacy behavior, lost replies and checkpoint boundaries passed");
}
'''
    compile_and_run(tmp_path, harness.replace("/* PRODUCTION */", production), family=family, bridge_version=bridge_version)


def test_local_boot_uses_current_storage_and_required_workers(tmp_path):
    source = (MAIN / "add_connector.c").read_text()
    start = source.index("const char *add_connector_local_boot_health_error(void)")
    production = source[start:source.index("bool add_connector_boot_health_ready(void)", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#define ZJ_BRIDGE_VERSION "2.6.16"
#define ZJ_WRITER_VERSION "2.7.0"
#define ZONE_LITE_RECOVERY_STABILITY_MS 30000
#define ADD_WORKER_RESOURCE 9
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct {char project_name[32],version[32];} esp_app_desc_t;
static esp_app_desc_t app={"zone_lite","2.7.0"};
static const esp_app_desc_t *esp_app_get_description(void){return &app;}
typedef struct {bool observed,available,recovery_complete,persistence_verified;int last_error,persistence_probe_error;} qs_health_t;
static qs_health_t storage={true,true,true,true,0,0};
static qs_health_t qs_health(void){return storage;}
static bool upgrade=true,runtime=true,locked;
static bool storage_upgrade_ready(void){return upgrade;}
static bool zj_runtime_boot_ready(void){return runtime;}
static int s_lock=1,s_outbox_task_handle,s_heartbeat_task_handle;
static bool s_outbox_buffer_ready,s_worker_start_failed,s_ords_worker_started;
static unsigned s_ords_worker_operation,s_ords_worker_tick_ms,s_outbox_tick_ms;
static unsigned now=2000;
static uint32_t monotonic_ms(void){return now;}
static struct {bool online;char connection_state[24];time_t stability_since_epoch;int user_count,attendance_count;} s_zkt={true,"ONLINE",0,0,0};
static int xSemaphoreTake(int lock,unsigned ms){(void)ms;assert(lock && !locked);locked=true;return pdTRUE;}
static void xSemaphoreGive(int lock){assert(lock && locked);locked=false;}
/* PRODUCTION */
int main(void)
{
    /* Writer proves the journal worker set, including an empty enrollment.
     * There is intentionally no ADD-connection or LED state in this proof. */
    assert(add_connector_local_boot_health_ready());
    runtime=false;assert(!strcmp(add_connector_local_boot_health_error(),"BOOT_LOCAL_JOURNAL_RECOVERY"));runtime=true;
    storage.persistence_probe_error=1;assert(!add_connector_local_boot_health_ready());storage.persistence_probe_error=0;
    storage.available=false;assert(!add_connector_local_boot_health_ready());storage.available=true;
    s_zkt.online=false;assert(!add_connector_local_boot_health_ready());s_zkt.online=true;
    s_zkt.attendance_count=-1;assert(!add_connector_local_boot_health_ready());s_zkt.attendance_count=0;
    strcpy(s_zkt.connection_state,"RECOVERING");assert(!add_connector_local_boot_health_ready());
    s_zkt.stability_since_epoch=time(NULL)-31;assert(add_connector_local_boot_health_ready());
    strcpy(app.version,"2.6.16");assert(!add_connector_local_boot_health_ready());
    s_outbox_task_handle=s_heartbeat_task_handle=1;s_outbox_buffer_ready=s_ords_worker_started=true;
    assert(add_connector_local_boot_health_ready());
    s_ords_worker_operation=ADD_WORKER_RESOURCE;assert(!add_connector_local_boot_health_ready());s_ords_worker_operation=0;
    now=90000;assert(!add_connector_local_boot_health_ready());now=2000;
    strcpy(app.project_name,"zone_lite_hikvision");assert(!add_connector_local_boot_health_ready());
    strcpy(app.project_name,"zone_lite");strcpy(app.version,"2.6.15");assert(!add_connector_local_boot_health_ready());
    assert(!locked);
}
'''
    compile_and_run(tmp_path, harness.replace("/* PRODUCTION */", production))
