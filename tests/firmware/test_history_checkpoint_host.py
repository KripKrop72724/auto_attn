"""Exercise history start/finish against failed checkpoint persistence."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_history_never_reports_uncommitted_completion(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    start = source.index("static bool history_start_new_sweep(void)")
    functions = source[start:source.index("static char *json_escape_alloc", start)]
    program = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static bool g_history_backfill_pending,g_history_backfill_had_failures,fail;
static int32_t g_history_cursor_year,g_history_cursor_month,g_history_oldest_year,g_history_oldest_month;
static int64_t g_history_last_sweep_epoch;
static uint32_t g_history_failed_windows;
static unsigned telemetry,logs,writes;
static void history_dump_cache_clear(void) {}
static int64_t epoch_now(void) {return 1800000000;}
static bool nvs_save_runtime_state(void) {writes++;if(fail){g_history_backfill_pending=true;g_history_backfill_had_failures=true;return false;}return true;}
static void update_history_telemetry(void) {telemetry++;}
static bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *message) {
 assert(level && subsystem && message && !g_history_backfill_pending);
 assert(!strcmp(code,g_history_backfill_had_failures?"HISTORY_BACKFILL_BLOCKED":"HISTORY_BACKFILL_COMPLETE"));logs++;return true;
}
""" + functions + r"""
int main(void) {
 fail=true;assert(!history_start_new_sweep());assert(!telemetry && !logs && writes==1);
 assert(!history_finish_sweep(1800000000));assert(!telemetry && !logs && writes==2 && g_history_backfill_pending);
 fail=false;assert(history_start_new_sweep());assert(telemetry==1 && !logs);
 assert(history_finish_sweep(1800000001));assert(telemetry==2 && logs==1 && !g_history_backfill_pending);
 assert(g_history_last_sweep_epoch==1800000001);
 g_history_backfill_had_failures=true;assert(history_finish_sweep(1800000002));assert(logs==2);
 return 0;
}
"""
    unit = tmp_path / "history.c"
    unit.write_text(program)
    executable = tmp_path / "history"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
