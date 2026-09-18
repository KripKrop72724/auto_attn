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
