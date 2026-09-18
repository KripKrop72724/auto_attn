"""Exercise the actual runtime NVS writer and its failure recovery."""
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_checkpoint_failures_restore_committed_source(tmp_path: Path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    start = source.index("static uint32_t g_runtime_checkpoint_generation;")
    end = source.index("static void nvs_load_runtime_state(", start)
    production = source[start:end]
    globals_ = []
    for name in sorted(set(re.findall(r"\bg_[a-z_]+\b", production))):
        if name in {"g_runtime_checkpoint_generation", "g_committed_runtime", "g_committed_runtime_valid"}:
            continue
        match = re.search(r"^static [^;\n]*\b" + name + r"\b[^;]*;", source[:start], re.MULTILINE)
        assert match, name
        globals_.append(match.group())
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "runtime_checkpoint.h"
typedef int esp_err_t;
typedef unsigned nvs_handle_t;
typedef struct { char version[32]; } esp_app_desc_t;
typedef struct { bool add_source_coverage_certified; uint32_t add_source_coverage_cursor; bool committed_source_known; uint32_t committed_source_generation, committed_source_cursor; } add_zkt_telemetry_t;
#define ESP_OK 0
#define NVS_READWRITE 1
#define LED_STATUS_LOCAL_FAILURE 1
#define ZONE_LITE_HISTORY_SCHEMA_VERSION 2
static unsigned failure, fault_count, writes;
static runtime_checkpoint_t pending, durable;
static size_t strlcpy(char *dst,const char *src,size_t size) {
    size_t n=strlen(src);if(size) {size_t copy=n<size-1?n:size-1;memcpy(dst,src,copy);dst[copy]=0;}return n;
}
static void update_history_telemetry(void) {}
static void led_status_fault(int state) {assert(state==1);++fault_count;}
static bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *message)
{ assert(level && subsystem && code && message);return true; }
static const esp_app_desc_t *esp_app_get_description(void)
{ static const esp_app_desc_t description={"2.5.4"};return &description; }
static esp_err_t nvs_open(const char *space,int mode,nvs_handle_t *handle)
{ assert(!strcmp(space,"zone_lite") && mode==1);*handle=1;return failure==1?-1:0; }
static esp_err_t nvs_set_blob(nvs_handle_t handle,const char *key,const void *data,size_t length)
{ assert(handle==1 && !strcmp(key,"runtime_v1") && length==sizeof(pending));++writes;pending=*(const runtime_checkpoint_t *)data;return failure==2?-1:0; }
static esp_err_t nvs_commit(nvs_handle_t handle)
{ assert(handle==1);if(failure==3)return -1;durable=pending;return failure==4?-1:0; }
static void nvs_close(nvs_handle_t handle) {assert(handle==1);}
''' + '\n'.join(globals_) + production + r'''
int main(void) {
    g_add_source_coverage_cursor=100;g_add_source_coverage_generation=9;
    g_last_synced_attendance_count=100;g_add_source_coverage_certified=true;
    assert(nvs_save_runtime_state());
    assert(runtime_checkpoint_valid(&durable));
    assert(g_committed_runtime.source_cursor==100);
    for(failure=1;failure<=4;++failure) {
        g_force_truth_reconcile=false;g_add_source_coverage_certified=true;
        g_add_zkt.add_source_coverage_certified=true;
        g_add_source_coverage_cursor=200;g_add_source_coverage_generation=10;
        g_last_synced_attendance_count=200;
        assert(!nvs_save_runtime_state());
        assert(g_force_truth_reconcile && !g_add_source_coverage_certified);
        assert(!g_add_zkt.add_source_coverage_certified);
        assert(g_add_source_coverage_cursor==100 && g_add_zkt.add_source_coverage_cursor==100);
        assert(g_add_source_coverage_generation==9 && g_last_synced_attendance_count==100);
        assert(g_runtime_checkpoint_generation==1);
        assert(g_add_zkt.committed_source_known && g_add_zkt.committed_source_cursor==100 && g_add_zkt.committed_source_generation==9);
        assert(g_history_backfill_pending && g_history_backfill_had_failures);
    }
    assert(fault_count==4);
    failure=0;g_force_truth_reconcile=false;g_add_source_coverage_cursor=300;
    assert(nvs_save_runtime_state());assert(durable.source_cursor==300);
    size_t live=3;
    for(failure=1;failure<=4;++failure) {
        assert(!commit_reconcile_count(103,&live));
        assert(live==3 && g_last_synced_attendance_count==100);
    }
    failure=0;assert(commit_reconcile_count(103,&live));
    assert(live==0 && durable.attendance_count==103);
    unsigned prior=writes;g_add_source_coverage_chain[0]='X';
    assert(!nvs_save_runtime_state());assert(writes==prior);
    assert(g_add_source_coverage_chain[0]=='0');
    g_runtime_checkpoint_generation=UINT32_MAX;
    assert(!nvs_save_runtime_state());assert(writes==prior);
    puts("runtime checkpoint NVS regression tests passed");
}
'''
    unit = tmp_path / "runtime.c"
    unit.write_text(program)
    compiler = shutil.which("cc")
    assert compiler
    executable = tmp_path / "runtime"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "durable_queue.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
