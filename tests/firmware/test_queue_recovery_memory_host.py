"""Exercise the actual owner recovery path with unavailable PSRAM scratch."""

from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def _function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_psram_refusal_never_verifies_or_mutates_queue_and_retry_is_bounded(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "queue_store.c").read_text()
    assert "static uint8_t *recovery_buffer;" in source
    assert "static uint8_t recovery_buffer[" not in source
    actual = _function(source, "bool qs_recover_step(")
    harness = r'''
#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define MALLOC_CAP_SPIRAM 1024U
#define MALLOC_CAP_8BIT 4U
typedef struct { durable_queue_t queue; int *mutex; } lane_t;
static lane_t lanes[QS_COUNT];
static int lane_lock,budget_mutex;
static int *budget_lock=&budget_mutex;
static qs_health_t health;
static dq_audit_t recovery_audits[QS_COUNT];
static uint8_t *recovery_buffer;
static bool refuse=true;
static unsigned allocations,audits,reopens,failures;
static uint8_t scratch[DQ_MAX_RECORD_BYTES];
static bool lock(qs_lane_t lane){assert((unsigned)lane<QS_COUNT && !lane_lock);lane_lock=1;return true;}
static int xSemaphoreTake(int *mutex,int wait){(void)wait;assert(!*mutex);*mutex=1;return 1;}
static void xSemaphoreGive(int *mutex){assert(*mutex);*mutex=0;}
static void *heap_caps_malloc(size_t bytes,unsigned caps){
 assert(lane_lock && budget_mutex);assert(bytes==8192 && caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
 ++allocations;return refuse?NULL:scratch;
}
static dq_result_t reopen(lane_t *lane){
 assert(lane_lock && budget_mutex);++reopens;lane->queue.ready=true;return DQ_OK;
}
dq_result_t dq_audit_step(const durable_queue_t *queue,dq_audit_t *audit,void *buffer,size_t capacity){
 assert(lane_lock && budget_mutex && buffer==scratch && capacity==8192);++audits;
 audit->complete=true;audit->generation=queue->checkpoint.generation;return DQ_OK;
}
static void record_queue_result(dq_result_t result,const char *operation,bool writing){
 (void)operation;(void)writing;if(result!=DQ_OK)++failures;
}
/* ACTUAL */
int main(void){
 for(unsigned i=0;i<QS_COUNT;i++){lanes[i].mutex=&lane_lock;lanes[i].queue.checkpoint.generation=1;}
 health.recovery_complete=true;
 assert(!qs_recover_step());assert(!health.recovery_complete && !recovery_buffer);
 assert(allocations==1 && !audits && !reopens && !failures && !lane_lock && !budget_mutex);
 assert(!qs_recover_step());assert(allocations==2 && !audits && !reopens && !failures);
 refuse=false;assert(qs_recover_step());assert(allocations==3 && audits==QS_COUNT && health.recovery_complete);
 assert(!lane_lock && !budget_mutex);
 assert(qs_recover_step());assert(allocations==3 && audits==QS_COUNT);
 /* A new generation must be audited with the full record capacity. */
 ++lanes[QS_LIVE].queue.checkpoint.generation;assert(qs_recover_step());assert(audits==QS_COUNT+1);
 /* Successful scratch allocation never resolves an unrelated storage incident. */
 health.last_error=EIO;assert(!qs_recover_step() && !health.recovery_complete && health.last_error==EIO);
 assert(allocations==3 && !failures && !lane_lock && !budget_mutex);
 return 0;
}
'''.replace("/* ACTUAL */", actual)
    unit = tmp_path / "recovery.c"
    unit.write_text(harness)
    binary = tmp_path / "recovery"
    subprocess.run(
        [shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-I", str(main), str(unit), "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=10)
