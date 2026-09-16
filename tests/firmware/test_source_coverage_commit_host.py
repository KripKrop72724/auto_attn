"""Source coverage is reported applied only after its durable runtime commit."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_source_coverage_application_requires_commit(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    begin = source.index("static bool apply_add_source_coverage(")
    adapter = source[begin : source.index("static bool process_add_incremental_tail(", begin)]
    program = (
        r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef struct { char terminal_serial[80]; bool active; uint32_t committed_next_ordinal,terminal_generation;char committed_chain_digest[65]; } add_source_coverage_t;
static char g_device_serial[]="PGB1261200077",g_add_source_coverage_chain[65];
static bool g_add_source_coverage_certified,failed;
static uint32_t g_add_source_coverage_cursor,g_add_source_coverage_generation,durable_cursor;
static struct {bool add_source_coverage_certified;uint32_t add_source_coverage_cursor;} g_add_zkt;
static unsigned applied,commits,mismatches;
static void copy_string(char *target,const char *source,size_t size)
{size_t length=strlen(source);if(length>=size)length=size-1;memcpy(target,source,length);target[length]=0;}
#define strlcpy copy_string
static bool nvs_save_runtime_state(void)
{
 ++commits;
 if(failed){g_add_source_coverage_cursor=durable_cursor;g_add_source_coverage_certified=false;g_add_zkt.add_source_coverage_certified=false;return false;}
 durable_cursor=g_add_source_coverage_cursor;return true;
}
static void add_connector_log(const char *level,const char *category,const char *event,const char *message)
{
 (void)level;(void)category;(void)message;
 if(!strcmp(event,"ADD_SOURCE_COVERAGE_APPLIED")){assert(!failed && durable_cursor==g_add_source_coverage_cursor);++applied;}
 if(!strcmp(event,"ADD_SOURCE_COVERAGE_TERMINAL_MISMATCH"))++mismatches;
}
"""
        + adapter
        + r"""
int main(void)
{
 add_source_coverage_t coverage={.active=true,.committed_next_ordinal=123,.terminal_generation=4};
 strcpy(coverage.terminal_serial,g_device_serial);memset(coverage.committed_chain_digest,'a',64);
 failed=true;assert(!apply_add_source_coverage(&coverage));
 assert(!applied && !durable_cursor && !g_add_source_coverage_certified);
 failed=false;assert(apply_add_source_coverage(&coverage));
 assert(applied==1 && durable_cursor==123 && g_add_source_coverage_generation==4);
 strcpy(coverage.terminal_serial,"replacement");assert(apply_add_source_coverage(&coverage));
 assert(applied==1 && mismatches==1 && !g_add_source_coverage_certified && !g_add_zkt.add_source_coverage_certified);
 strcpy(coverage.terminal_serial,g_device_serial);coverage.active=false;
 assert(apply_add_source_coverage(&coverage));assert(applied==1 && !g_add_source_coverage_certified);
 assert(!apply_add_source_coverage(NULL) && commits==4);
 return 0;
}
"""
    )
    unit = tmp_path / "source.c"
    unit.write_text(program)
    executable = tmp_path / "source"
    subprocess.run(
        [
            shutil.which("cc"),
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            str(unit),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
