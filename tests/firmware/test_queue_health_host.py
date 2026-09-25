"""Execute queue adapter read/settle locking and fault reporting with injected ports."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_queue_read_and_settlement_failures_are_visible_and_retryable(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "queue_store.c").read_text()
    report = source[
        source.index("static void record_queue_result(") : source.index("bool qs_init(")
    ]
    operations = source[source.index("dq_result_t qs_peek(") : source.index("bool qs_snapshot(")]
    harness = r"""
#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#include <string.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct { durable_queue_t queue; int *mutex; } lane_t;
static int lane_lock, budget_mutex;
static int *budget_lock=&budget_mutex;
static lane_t lanes[QS_COUNT];
static qs_health_t health;
static bool generation_ok=true, budget_busy, lane_busy;
static dq_result_t injected=DQ_OK;
static unsigned accesses;
static int xSemaphoreTake(int *mutex,int timeout)
{ (void)timeout;assert(!*mutex);if(budget_busy)return 0;*mutex=1;return 1; }
static void xSemaphoreGive(int *mutex) { assert(*mutex);*mutex=0; }
static bool lock(qs_lane_t lane)
{ if((unsigned)lane>=QS_COUNT)return false;if(lane_busy)return false;assert(!lane_lock);lane_lock=1;return true; }
static bool ensure_storage_generation(void) { assert(lane_lock && budget_mutex);return generation_ok; }
static dq_result_t reopen(lane_t *lane)
{ (void)lane;assert(lane_lock && budget_mutex);return DQ_OK; }
dq_result_t dq_peek(durable_queue_t *queue,void *data,size_t capacity,size_t *length,dq_token_t *token)
{ (void)queue;(void)data;(void)capacity;(void)length;(void)token;assert(lane_lock && budget_mutex);++accesses;return injected; }
dq_result_t dq_settle(durable_queue_t *queue,const dq_token_t *token)
{ (void)queue;(void)token;assert(lane_lock && budget_mutex);++accesses;return injected; }
/* PRODUCTION */
int main(void)
{
    for(unsigned i=0;i<QS_COUNT;++i)lanes[i].mutex=&lane_lock;
    char data[16];size_t size=0;dq_token_t token={0};
    errno=ENOSPC;injected=DQ_IO;
    assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==DQ_IO);
    assert(health.last_error==EIO && health.read_failures==1 && !health.write_failures);
    assert(!strcmp(health.last_operation,"segment_read") && !lane_lock && !budget_mutex);
    injected=DQ_CORRUPT;
    assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==DQ_CORRUPT);
    assert(health.last_error==EBADMSG && health.read_failures==2);
    unsigned failures=health.failures;
    dq_result_t retryable[]={DQ_BUFFER_SMALL,DQ_EMPTY,DQ_STALE,DQ_OK};
    for(unsigned i=0;i<4;++i){
        injected=retryable[i];assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==injected);
        assert(health.failures==failures && health.last_error==EBADMSG);
        assert(!lane_lock && !budget_mutex);
    }
    injected=DQ_IO;assert(qs_settle(QS_LIVE,&token)==DQ_IO);
    assert(health.write_failures==1 && !strcmp(health.last_operation,"segment_settle"));
    generation_ok=false;unsigned before=accesses;
    assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==DQ_IO);
    assert(qs_settle(QS_LIVE,&token)==DQ_IO && accesses==before);
    generation_ok=true;budget_busy=true;
    failures=health.failures;
    assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==DQ_PENDING);
    assert(health.failures==failures);
    budget_busy=false;lane_busy=true;
    assert(qs_peek(QS_LIVE,data,sizeof(data),&size,&token)==DQ_PENDING);
    assert(health.failures==failures && !lane_lock && !budget_mutex);
    lane_busy=false;budget_busy=true;
    assert(qs_settle(QS_LIVE,&token)==DQ_IO && accesses==before);
    assert(!lane_lock && !budget_mutex);
    return 0;
}
"""
    unit = tmp_path / "health.c"
    unit.write_text(harness.replace("/* PRODUCTION */", report + operations))
    executable = tmp_path / "health"
    subprocess.run(
        [
            shutil.which("cc"),
            "-std=c11",
            "-g",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-fsanitize=address,undefined",
            "-fno-omit-frame-pointer",
            "-I",
            str(firmware),
            str(unit),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
