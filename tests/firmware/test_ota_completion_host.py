"""OTA completion runs only after certified coverage, not after an elapsed timer."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_ota_completion_waits_for_source_and_durable_acknowledgements(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    connector = (firmware / "add_connector.c").read_text()
    source = (firmware / "ota_manager.c").read_text()
    gate = connector[connector.index("bool add_connector_ota_reconcile_ready("):connector.index("bool add_connector_consume_connected_edge(")]
    start = source.index("static bool acknowledge_pending_success(void)\n{")
    ack = source[start:source.index("static void ota_task(", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
static bool boot_ready, locked, lock_failed, fail_clear;
static int lock;
static int *s_lock=&lock;
static unsigned reports, clears, fail_report;
static struct { bool add_source_coverage_certified;int32_t attendance_count;uint32_t add_source_coverage_cursor; } s_zkt;
static struct { char deployment_id[48],state[32],target_version[32]; } s_journal;
typedef struct { const char *version; } esp_app_desc_t;
static esp_app_desc_t app={"2.6.0"};
static const esp_app_desc_t *esp_app_get_description(void) { return &app; }
static bool add_connector_boot_health_ready(void) { return boot_ready; }
static int xSemaphoreTake(int *mutex,int timeout) { (void)mutex;(void)timeout;assert(!locked);if(lock_failed)return 0;locked=true;return 1; }
static void xSemaphoreGive(int *mutex) { (void)mutex;assert(locked);locked=false; }
static bool report_state(const char *state,const char *error)
{ (void)error;assert(!locked && state[0]);return ++reports!=fail_report; }
static bool clear_journal(void)
{ ++clears;if(fail_clear)return false;memset(&s_journal,0,sizeof(s_journal));return true; }
/* PRODUCTION */
int main(void)
{
    strcpy(s_journal.deployment_id,"deployment");strcpy(s_journal.state,"RECONCILING");strcpy(s_journal.target_version,"2.6.0");
    assert(!acknowledge_pending_success() && !reports);
    boot_ready=true;s_zkt.attendance_count=100;
    assert(!acknowledge_pending_success() && !reports);
    s_zkt.add_source_coverage_certified=true;s_zkt.add_source_coverage_cursor=99;
    assert(!acknowledge_pending_success() && !reports);
    s_zkt.attendance_count=-1;assert(!acknowledge_pending_success() && !reports);
    s_zkt.attendance_count=100;s_zkt.add_source_coverage_cursor=100;
    lock_failed=true;assert(!acknowledge_pending_success() && !reports);lock_failed=false;
    fail_report=1;assert(!acknowledge_pending_success() && reports==1 && !clears);
    reports=0;fail_report=2;assert(!acknowledge_pending_success() && reports==2 && !clears);
    reports=fail_report=0;fail_clear=true;
    assert(!acknowledge_pending_success() && reports==2 && clears==1 && s_journal.deployment_id[0]);
    fail_clear=false;reports=0;
    assert(acknowledge_pending_success() && reports==2 && clears==2 && !s_journal.deployment_id[0]);
    assert(!locked);
    puts("OTA source completion regression tests passed");
}
'''
    unit = tmp_path / "completion.c"
    unit.write_text(harness.replace("/* PRODUCTION */", gate + ack))
    executable = tmp_path / "completion"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(unit), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)


def test_hikvision_ota_requires_recovered_storage_workers_and_empty_custody(tmp_path):
    firmware = ROOT / 'firmware/zone_lite/main'
    source = (firmware / 'hikvision_runtime.c').read_text()
    health = source[source.index('bool hikvision_boot_health_ready(void)'):]
    source = (firmware / 'add_connector.c').read_text()
    gates = source[source.index('bool add_connector_boot_health_ready(void)'):source.index('bool add_connector_consume_connected_edge(void)')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#define ZONE_LITE_HIKVISION 1
#define QS_HIK_SOURCE 7
static atomic_uint poll_tick=1000,history_tick=1000,uploader_tick=1000;
static atomic_bool runtime_workers_started=true,checkpoint_ready=true,uploader_started=true,uploader_buffer_ready=true;
typedef struct {bool observed,available,recovery_complete,persistence_verified;int last_error;} qs_health_t;
static qs_health_t storage={true,true,true,true,0};
static uint32_t now=2000,pending;
static bool snapshot_ok=true,connected=true,delivery=true,upgrade=true;
static int s_heartbeat_task_handle=1;
static uint64_t esp_timer_get_time(void){return now*1000ULL;}
static qs_health_t qs_health(void){return storage;}
static bool storage_upgrade_ready(void){return upgrade;}
static bool add_connector_is_connected(void){return connected;}
static bool add_connector_delivery_healthy(void){return delivery;}
static bool qs_snapshot(int lane,uint32_t *depth){assert(lane==7);*depth=pending;return snapshot_ok;}
/* PRODUCTION */
int main(void){
 assert(add_connector_boot_health_ready()&&add_connector_ota_reconcile_ready());
 pending=1;assert(add_connector_boot_health_ready()&&!add_connector_ota_reconcile_ready());pending=0;
 snapshot_ok=false;assert(!add_connector_ota_reconcile_ready());snapshot_ok=true;
 storage.persistence_verified=false;assert(!add_connector_boot_health_ready());storage.persistence_verified=true;
 storage.last_error=1;assert(!add_connector_boot_health_ready());storage.last_error=0;
 storage.recovery_complete=false;assert(!add_connector_boot_health_ready());storage.recovery_complete=true;
 checkpoint_ready=false;assert(!add_connector_boot_health_ready());checkpoint_ready=true;
 runtime_workers_started=false;assert(!add_connector_boot_health_ready());runtime_workers_started=true;
 uploader_buffer_ready=false;assert(!add_connector_boot_health_ready());uploader_buffer_ready=true;
 connected=false;assert(!add_connector_boot_health_ready());connected=true;
 delivery=false;assert(!add_connector_boot_health_ready());delivery=true;
 now=92000;assert(!add_connector_boot_health_ready());now=2000;
 assert(add_connector_ota_reconcile_ready());
 return 0;
}
'''
    unit=tmp_path/'hik-ota.c'
    unit.write_text(harness.replace('/* PRODUCTION */',health+gates))
    exe=tmp_path/'hik-ota'
    subprocess.run([shutil.which('cc'),'-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(unit),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
