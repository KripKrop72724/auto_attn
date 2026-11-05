"""3.1.7: slow-but-answering terminal requests keep worker ticks fresh; a stuck holder does not."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_request_progress_refreshes_only_lock_holder_ticks(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/hikvision_runtime.c").read_text()
    start = source.index("/* 3.1.7 request progress.")
    body = source[start:source.index("/* Protected by request_lock", start)]
    assert "hik_http_set_progress_hook(request_progress);" in source
    assert "poll_take_request_lock();" in source
    assert "xSemaphoreTake(request_lock, portMAX_DELAY)" not in source
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stddef.h>
typedef void *TaskHandle_t;
typedef int *SemaphoreHandle_t;
typedef int BaseType_t;
#define pdTRUE 1
#define pdFALSE 0
#define pdMS_TO_TICKS(ms) (ms)
static int lock_storage;
static SemaphoreHandle_t request_lock = &lock_storage;
static atomic_uint poll_tick, history_tick;
static int poll_task_id, history_task_id, profile_task_id, stream_task_id;
static TaskHandle_t current, holder;
static int64_t now_us;
static unsigned busy_waits;
static int64_t esp_timer_get_time(void){return now_us;}
static TaskHandle_t xTaskGetCurrentTaskHandle(void){return current;}
static TaskHandle_t xSemaphoreGetMutexHolder(SemaphoreHandle_t s){assert(s==request_lock);return holder;}
/* The holder (another task) finishes one 25 s search per failed 1 s wait of
 * the poll task, for busy_waits waits; then the lock is free. */
static TaskHandle_t progressing_holder;
static void request_progress(void);
static BaseType_t xSemaphoreTake(SemaphoreHandle_t s,int ticks){
 assert(s==request_lock&&ticks==1000);
 if(!busy_waits){holder=current;return pdTRUE;}
 busy_waits--;now_us+=1000000;
 if(progressing_holder){TaskHandle_t me=current;current=progressing_holder;holder=progressing_holder;request_progress();current=me;}
 return pdFALSE;
}
/* PRODUCTION */
int main(void){
 poll_worker=&poll_task_id;history_worker=&history_task_id;
 /* Poll task holding the lock: each finished request refreshes poll_tick only. */
 now_us=5000000;current=&poll_task_id;holder=&poll_task_id;request_progress();
 assert(poll_tick==5000&&history_tick==0&&request_progress_ms==5000);
 /* History/audit holder: history_tick only. */
 now_us=30000000;current=&history_task_id;holder=&history_task_id;request_progress();
 assert(history_tick==30000&&poll_tick==5000&&request_progress_ms==30000);
 /* Stream task identity check runs without the lock: ignored. */
 now_us=40000000;current=&stream_task_id;holder=NULL;request_progress();
 assert(request_progress_ms==30000&&poll_tick==5000&&history_tick==30000);
 /* Profile holder: shared stamp only. */
 current=&profile_task_id;holder=&profile_task_id;request_progress();
 assert(request_progress_ms==40000&&poll_tick==5000&&history_tick==30000);
 /* Poll waits 120 s behind a holder that keeps finishing requests: fresh. */
 current=&poll_task_id;holder=&history_task_id;progressing_holder=&history_task_id;busy_waits=120;
 poll_take_request_lock();
 assert(holder==&poll_task_id&&poll_tick==(uint32_t)(now_us/1000)&&now_us==160000000);
 /* Poll waits 120 s behind a holder that finishes nothing: stays stale. */
 uint32_t before=poll_tick;
 current=&poll_task_id;holder=&profile_task_id;progressing_holder=NULL;busy_waits=120;
 poll_take_request_lock();
 assert(poll_tick==before&&now_us-(int64_t)before*1000==120000000);
 /* Before the first poll (tick 0) progress never fakes a live poll task. */
 atomic_store(&poll_tick,0);atomic_store(&request_progress_ms,1);
 current=&poll_task_id;holder=&history_task_id;progressing_holder=&history_task_id;busy_waits=3;
 poll_take_request_lock();assert(poll_tick==0);
 return 0;
}
'''
    unit = tmp_path / "progress.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "progress"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
