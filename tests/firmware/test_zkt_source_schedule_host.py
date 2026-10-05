"""Exercise source scheduling and the actual terminal-count integration."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def run(tmp_path, source, *defines):
    binary = tmp_path / "schedule"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
        *defines, str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)


def test_source_schedule_spacing_outage_backoff_and_clock_limits(tmp_path):
    run(tmp_path, ROOT / "tests/firmware/zkt_source_schedule_host.c")


@pytest.mark.parametrize("defines,enabled", [
    (["-DZONE_LITE_JOURNAL_WRITES=1"], True), ([], False),
    (["-DZONE_LITE_JOURNAL_WRITES=1", "-DZONE_LITE_HIKVISION=1"], False),
])
def test_actual_gateway_readiness_and_unknown_count_are_preserved(tmp_path, defines, enabled):
    text = (MAIN / "zone_lite.c").read_text()
    start = text.index("static bool process_add_source_tail_step(")
    adapter = text[start:text.index("static int64_t gateway_run_session(", start)]
    unit = tmp_path / "source.c"
    unit.write_text(r'''
#include "zkt_source_schedule.h"
#include <assert.h>
typedef int zk_context_t;
typedef int user_table_t;
#define ZONE_LITE_RECONCILE_INTERVAL_MS 900000
#define ZONE_LITE_RECOVERY_STABILITY_MS 120000
static bool g_add_source_coverage_certified, legacy, ready, connected, counts_ok, tail_ok, more_records;
static int64_t g_session_stable_since_ms;
static struct { int32_t attendance_count; } g_add_zkt;
static int32_t observed_records;
static unsigned calls;
static bool zkt_legacy_capture_allowed(void) { return legacy; }
static __attribute__((unused)) bool zj_runtime_writer_ready(void) { return ready; }
static __attribute__((unused)) bool add_connector_is_connected(void) { return connected; }
static bool zk_get_counts(int sock, zk_context_t *ctx, int32_t *users, int32_t *records)
{ (void)sock; (void)ctx; *users=2048; *records=observed_records; return counts_ok; }
static bool process_add_incremental_tail(int sock, zk_context_t *ctx, const user_table_t *users,
    int32_t records, bool *more)
{ (void)sock; (void)ctx; (void)users; assert(records==observed_records); ++calls; *more=more_records; return tail_ok; }
''' + adapter + r'''
int main(void)
{
    zts_schedule_t schedule={0};
    legacy=true; g_add_source_coverage_certified=true;
    assert(!zkt_source_tail_due(&schedule,899999,0));
    assert(zkt_source_tail_due(&schedule,900000,0));
    g_session_stable_since_ms=900000;
    assert(!zkt_source_tail_due(&schedule,900000,0));
    legacy=false; ready=true; connected=true;
    assert(zkt_source_tail_due(&schedule,900000,900000)==ENABLED);
    ready=false; assert(!zkt_source_tail_due(&schedule,900000,0)); ready=true;
    connected=false; assert(!zkt_source_tail_due(&schedule,900000,0)); connected=true;
    g_add_source_coverage_certified=false; assert(!zkt_source_tail_due(&schedule,900000,0));
    assert(!zkt_source_tail_due(&schedule,-1,0));
    bool more=true; g_add_zkt.attendance_count=200000;
    assert(!process_add_source_tail_step(1,0,0,&more));
    assert(!more && !calls && g_add_zkt.attendance_count==200000);
    counts_ok=true; observed_records=-1;
    assert(!process_add_source_tail_step(1,0,0,&more));
    assert(!calls && g_add_zkt.attendance_count==200000);
    observed_records=200003; more_records=true;
    assert(!process_add_source_tail_step(1,0,0,&more));
    assert(calls==1 && more && g_add_zkt.attendance_count==200003);
    tail_ok=true;
    assert(process_add_source_tail_step(1,0,0,&more)); assert(calls==2);
    observed_records=0; more_records=false;
    assert(process_add_source_tail_step(1,0,0,&more));
    assert(!more && calls==3 && g_add_zkt.attendance_count==0);
}
'''.replace("ENABLED", "true" if enabled else "false"))
    run(tmp_path, unit, *defines)
