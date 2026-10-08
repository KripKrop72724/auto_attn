"""Real IDF tick rounding, owner fairness and telemetry without storage work."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def _function(source, name):
    begin = source.index(name)
    opening = source.index("{", begin)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[begin:end]


@pytest.mark.parametrize("tick_rate", [100, 1000])
def test_actual_owner_client_blocks_for_progress_and_keeps_deadline_receipts(tmp_path, tick_rate):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    projdefs = idf / "components/freertos/FreeRTOS-Kernel/include/freertos/projdefs.h"
    # Use the actual pinned SDK whenever available. Plain host CI has no SDK;
    # keep its regression active with that exact floor conversion instead of
    # the rounding-up stub that previously concealed the zero-delay defect.
    tick_definition = (
        f'#include "{projdefs}"' if projdefs.is_file() else
        "#define pdMS_TO_TICKS(xTimeInMs) "
        "((TickType_t)(((TickType_t)(xTimeInMs) * (TickType_t)configTICK_RATE_HZ) / (TickType_t)1000U))"
    )
    header = f'''#pragma once
#include <stdint.h>
typedef unsigned TickType_t;
typedef int BaseType_t;
#define configTICK_RATE_HZ {tick_rate}
{tick_definition}
int64_t esp_timer_get_time(void);
void vTaskDelay(TickType_t ticks);
'''
    for name in ("esp_timer.h", "freertos/FreeRTOS.h", "freertos/task.h"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    source = (MAIN / "zkt_storage_owner.c").read_text()
    pause = _function(source, "static void pause_after_work(")
    task = source[source.index("static void task(void *context)"):source.index("bool zj_owner_start(")]
    # A yield must follow release of ownership locks and copied secret buffers.
    assert task.rindex("xSemaphoreGive(mailbox_lock)") < task.index("pause_after_work(pending,")
    assert task.rindex("mbedtls_platform_zeroize(&reply") < task.index("pause_after_work(pending,")
    unit = tmp_path / "progress.c"
    unit.write_text(r'''
#include "zkt_storage_owner.h"
#include "freertos/FreeRTOS.h"
#include <assert.h>
#include <string.h>
static uint64_t now=1,ticket,next_ticket,request_deadline;
static unsigned submissions,polls,blocked_ticks,idle_runs,owner_runs;
static bool ready,hold_reply,reject,locks_held,owner_may_run=true;
int64_t esp_timer_get_time(void){return (int64_t)now;}
void vTaskDelay(TickType_t ticks){
 /* A zero delay never removes the priority caller from READY; fail rather
  * than advancing a fake millisecond clock that masks the actual bug. */
 assert(ticks>0 && !locks_held);blocked_ticks+=ticks;
 now+=(uint64_t)ticks*1000000U/configTICK_RATE_HZ;++idle_runs;
 if(ticket && owner_may_run){++owner_runs;ready=true;}
}
bool zj_owner_submit(const zj_request_t *in,uint64_t *out){
 *out=0;if(reject)return false;
 assert(!ticket && in->operation==ZJ_SEGMENTED_QUEUE && in->input.segmented.operation==ZQ_SNAPSHOT);
 assert(in->input.segmented.deadline_us>now);request_deadline=in->input.segmented.deadline_us;
 *out=ticket=++next_ticket;ready=false;++submissions;return true;
}
bool zj_owner_poll(uint64_t id,zj_reply_t *reply,bool *complete){
 assert(id==ticket);++polls;*complete=ready && !hold_reply;
 if(*complete){memset(reply,0,sizeof(*reply));reply->result=ZJ_OK;reply->segmented.result=DQ_OK;
  reply->segmented.verified=true;reply->segmented.depth=7;ticket=0;}
 return true;
}
/* PAUSE */
int main(void){
 assert(pdMS_TO_TICKS(2)==(configTICK_RATE_HZ==100?0:2));
 uint32_t depth=999;
 assert(zq_snapshot(QS_LIVE,&depth) && depth==7 && idle_runs && owner_runs);
 assert(polls==2 && submissions==1 && blocked_ticks==(configTICK_RATE_HZ==100?1U:2U));
 /* Missing completion stays bounded; accepted request/reply cannot be
  * replaced or silently cancelled when a caller reaches its deadline. */
 hold_reply=true;uint64_t start=now;unsigned before=submissions;
 assert(!zq_snapshot(QS_LIVE,&depth) && ticket && submissions==before+1);
 uint64_t tick_us=1000000U/configTICK_RATE_HZ;
 assert(now>=start+ZQ_DEADLINE_US && now<start+ZQ_DEADLINE_US+tick_us+2000U);
 assert(request_deadline==start+ZQ_DEADLINE_US);
 before=submissions;start=now;
 assert(!zq_snapshot(QS_LIVE,&depth) && ticket && submissions==before);
 assert(now>=start+ZQ_DEADLINE_US && now<start+ZQ_DEADLINE_US+tick_us+2000U);
 hold_reply=false;assert(zq_snapshot(QS_LIVE,&depth) && !ticket && submissions==before+1);
 /* Owner admission cancellation refuses new work without a polling spin. */
 reject=true;before=polls;start=now;assert(!zq_snapshot(QS_LIVE,&depth));assert(!ticket && polls==before && now==start);reject=false;
 /* A completely stalled owner still allows IDLE to run until the deadline. */
 owner_may_run=false;before=idle_runs;assert(!zq_snapshot(QS_LIVE,&depth) && ticket);assert(idle_runs>before);
 owner_may_run=true;assert(zq_snapshot(QS_LIVE,&depth) && !ticket);
 /* Continuous completed operations and yielded slices both get positive
  * idle time; fast copied chunks need not sleep after every operation. */
 uint64_t last_pause=now;before=idle_runs;
 for(unsigned i=0;i<1000;i++){now+=100;pause_after_work(false,&last_pause);}
 assert(idle_runs-before==50);before=idle_runs;pause_after_work(true,&last_pause);assert(idle_runs==before+1);
 return 0;
}
'''.replace("/* PAUSE */", pause))
    binary = tmp_path / "progress"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(tmp_path), "-I", str(MAIN),
                    str(unit), str(MAIN / "zkt_segmented_client.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)


def test_telemetry_snapshot_is_nonblocking_unknown_until_current_audit_and_read_only(tmp_path):
    source = (MAIN / "queue_store.c").read_text()
    snapshot = _function(source, "bool qs_snapshot_ram(")
    diagnostics = (MAIN / "add_connector.c").read_text()
    begin = diagnostics.index("static void append_firmware_diagnostics(")
    diagnostics = diagnostics[begin:diagnostics.index("static void heartbeat_task(", begin)]
    assert "qs_snapshot_ram((qs_lane_t)i, &depth)" in diagnostics
    assert "qs_snapshot(" not in diagnostics
    unit = tmp_path / "snapshot.c"
    unit.write_text(r'''
#include "queue_store.h"
#include <assert.h>
#include <string.h>
#define pdTRUE 1
typedef struct {durable_queue_t queue;unsigned *mutex;} lane_t;
static lane_t lanes[QS_COUNT];static dq_audit_t recovery_audits[QS_COUNT];
static unsigned mutex,tries,releases;
static int xSemaphoreTake(unsigned *lock,unsigned ticks){assert(ticks==0);++tries;if(*lock)return 0;*lock=1;return 1;}
static void xSemaphoreGive(unsigned *lock){assert(*lock);*lock=0;++releases;}
/* SNAPSHOT */
int main(void){
 uint32_t depth=999;assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999 && !tries);
 lanes[QS_LIVE].mutex=&mutex;
 assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999);
 lanes[QS_LIVE].queue.ready=true;lanes[QS_LIVE].queue.checkpoint.generation=3;
 assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999);
 recovery_audits[QS_LIVE].complete=true;recovery_audits[QS_LIVE].generation=2;
 assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999);
 recovery_audits[QS_LIVE].generation=3;assert(qs_snapshot_ram(QS_LIVE,&depth) && depth==0);
 lanes[QS_LIVE].queue.checkpoint.generation=4;lanes[QS_LIVE].queue.checkpoint.depth=5;depth=999;
 assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999);
 recovery_audits[QS_LIVE].generation=4;
 mutex=1;unsigned before=releases;assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999 && releases==before);mutex=0;
 durable_queue_t original=lanes[QS_LIVE].queue;dq_audit_t audit=recovery_audits[QS_LIVE];
 assert(qs_snapshot_ram(QS_LIVE,&depth) && depth==5);
 assert(!memcmp(&original,&lanes[QS_LIVE].queue,sizeof(original)) && !memcmp(&audit,&recovery_audits[QS_LIVE],sizeof(audit)));
 lanes[QS_LIVE].queue.ready=false;depth=999;assert(!qs_snapshot_ram(QS_LIVE,&depth) && depth==999);
 assert(!qs_snapshot_ram((qs_lane_t)QS_COUNT,&depth) && !qs_snapshot_ram(QS_LIVE,NULL));
 return 0;
}
'''.replace("/* SNAPSHOT */", snapshot))
    binary = tmp_path / "snapshot"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(MAIN), str(unit), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
