"""Fault injection at the actual legacy admission and append boundary."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_append_shares_reserves_and_releases_budget_on_every_failure(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    adapter = (firmware / "queue_store.c").read_text()
    runtime = (firmware / "zone_lite.c").read_text()
    measure = adapter[adapter.index("static bool measure("):adapter.index("static bool admit(")]
    admission = adapter[adapter.index("bool qs_local_begin("):adapter.index("static bool lock(")]
    append = runtime[runtime.index("static bool append_line_policy("):runtime.index("static bool extract_event_uid(")]
    harness = r'''
#include "queue_store.h"
#include "storage_budget.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define ESP_OK 0
#define PENDING_PATH "pending"
#define BLOCKED_PATH "blocked"
static bool storage_upgrade_segmented_writes(void) { return false; }
dq_result_t qs_append_with_policy(qs_lane_t lane,const void *data,size_t length,qs_admission_t policy)
{ (void)lane;(void)data;(void)length;(void)policy;assert(false);return DQ_IO; }
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define ESP_LOGE(...) ((void)0)
static int locked;
static int *budget_lock=&locked;
static qs_health_t health;
static storage_budget_t budget;
static size_t used, total=8U*1024U*1024U;
static bool measure_failed;
static unsigned fail_operation, operation, opens;
static int xSemaphoreTake(int *mutex,int timeout)
{ (void)timeout;assert(!*mutex);*mutex=1;return 1; }
static void xSemaphoreGive(int *mutex) { assert(*mutex);*mutex=0; }
static int esp_spiffs_info(const void *label,size_t *t,size_t *u)
{ (void)label;assert(locked);*t=total;*u=used;return measure_failed?-1:0; }
static bool fail(void) { assert(locked);if(++operation==fail_operation){errno=EIO;return true;}return false; }
static FILE *rel_open_append(const char *path)
{ ++opens;if(fail())return NULL;return fopen(path,"a"); }
static int checked_puts(const char *text,FILE *file) { return fail()?EOF:fputs(text,file); }
static int checked_putc(int ch,FILE *file) { return fail()?EOF:fputc(ch,file); }
static int checked_flush(FILE *file) { return fail()?EOF:fflush(file); }
static int checked_sync(int fd) { return fail()?-1:fsync(fd); }
static int checked_close(FILE *file) { bool failed=fail();int result=fclose(file);if(failed){errno=EIO;return EOF;}return result; }
#define fputs checked_puts
#define fputc checked_putc
#define fflush checked_flush
#define fsync checked_sync
#define fclose checked_close
/* PRODUCTION */
int main(void)
{
    assert(append_line_policy("rows","live",QS_ADMIT_LIVE));assert(!locked);
    unsigned operations=operation;assert(operations==6);
    for(unsigned i=1;i<=operations;++i){
        operation=0;fail_operation=i;
        assert(!append_line_policy("rows","retry",QS_ADMIT_LIVE));
        assert(!locked && health.last_error==EIO);
    }
    assert(health.write_failures==operations);
    fail_operation=0;operation=0;
    used=(total*60+99)/100;
    unsigned before=opens;
    assert(!append_line_policy("rows","history",QS_ADMIT_HISTORICAL));
    assert(!locked && opens==before && health.bulk_paused);
    assert(health.admission_rejections==1 && health.write_failures==operations);
    assert(!strcmp(health.last_operation,"capacity_admission"));
    assert(append_line_policy("rows","live",QS_ADMIT_LIVE));
    used=total*56/100;
    assert(!append_line_policy("rows","history",QS_ADMIT_HISTORICAL));
    used=total*54/100;
    assert(append_line_policy("rows","history",QS_ADMIT_HISTORICAL));
    used=total*75/100-4096;
    assert(!append_line_policy("rows","live",QS_ADMIT_LIVE));
    assert(append_line("rows","receipt")); // Reserved recovery capacity remains usable.
    used=total*75/100;
    assert(!append_line("rows","receipt"));assert(!locked);
    measure_failed=true;before=opens;
    assert(!append_line("rows","unknown capacity"));
    assert(!locked && opens==before && health.last_error==EIO);
    return 0;
}
'''
    unit = tmp_path / "admission.c"
    unit.write_text(harness.replace("/* PRODUCTION */", measure + admission + append))
    executable = tmp_path / "admission"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "storage_budget.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
