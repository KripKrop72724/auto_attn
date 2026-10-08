"""Volatile dedup allocation failure must only cause safe stable-ID replay."""

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.mark.parametrize("failed_allocation", [0, 1, 2, 3])
def test_bounded_psram_cache_lifetime_and_replay_on_partial_failure(tmp_path, failed_allocation):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "zone_lite.c").read_text()
    actual = "\n".join(_function(source, signature) for signature in (
        "static void seen_cache_init(", "static bool seen_contains(", "static bool seen_add(",
        "static enqueue_result_t enqueue_event_to_files(",
    ))
    harness = r'''
#include "uid_cache.h"
#include "queue_store.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#define SEEN_UID_CAPACITY 65536U
#define MALLOC_CAP_SPIRAM 1024U
#define MALLOC_CAP_8BIT 4U
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define PENDING_PATH "/synthetic/pending"
#define BLOCKED_PATH "/synthetic/blocked"
#define LED_STATUS_LOCAL_FAILURE 1
#define LED_STATUS_BLOCKED_IDENTITY 2
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
static uid_cache_t g_seen_cache;
static int g_seen_lock,held;
static unsigned calls,allocations,frees,mutex_creations,takes;
static bool busy;
typedef struct {char event_uid[65],cnic[14];} attendance_event_t;
typedef enum {ENQUEUE_DUPLICATE,ENQUEUE_RESOURCE_ERROR,ENQUEUE_PENDING,ENQUEUE_BLOCKED,ENQUEUE_STORAGE_ERROR} enqueue_result_t;
static bool g_force_truth_reconcile,refuse_append;
static unsigned preserved;
static char preserved_uids[2][65];
static char *event_to_json(const attendance_event_t *event,const char *capture){
 assert(!strcmp(capture,"LIVE"));char *json=malloc(65);assert(json);memcpy(json,event->event_uid,65);return json;
}
static bool append_line_policy(const char *path,const char *json,qs_admission_t policy){
 assert(!held && !strcmp(path,PENDING_PATH) && policy==QS_ADMIT_LIVE);
 if(refuse_append){return false;}
 assert(preserved<2);memcpy(preserved_uids[preserved++],json,65);return true;
}
static void led_status_fault(int code){assert(code==LED_STATUS_LOCAL_FAILURE || code==LED_STATUS_BLOCKED_IDENTITY);}
static void led_status_set_backlog(bool value){assert(value);}
static int xSemaphoreCreateMutex(void){++mutex_creations;++calls;return FAIL==calls?0:1;}
static int xSemaphoreTake(int mutex,int timeout){assert(mutex==1 && timeout==200 && !held);++takes;if(busy)return 0;held=1;return 1;}
static void xSemaphoreGive(int mutex){assert(mutex==1 && held);held=0;}
static void *heap_caps_calloc(size_t count,size_t bytes,unsigned caps){
 assert(!held && caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));++calls;
 assert((count==8192 && bytes==1) || (count==65536 && bytes==32));
 if(FAIL==calls){return NULL;}
 ++allocations;return calloc(count,bytes);
}
static void heap_caps_free(void *pointer){if(pointer){++frees;free(pointer);}}
bool real_contains(const uid_cache_t *cache,const char *uid);
bool real_add(uid_cache_t *cache,const char *uid);
static bool checked_contains(const uid_cache_t *cache,const char *uid){assert(held);return real_contains(cache,uid);}
static bool checked_add(uid_cache_t *cache,const char *uid){assert(held);return real_add(cache,uid);}
#define uid_cache_contains checked_contains
#define uid_cache_add checked_add
/* ACTUAL */
#undef uid_cache_contains
#undef uid_cache_add
int main(void){
 seen_cache_init();assert(mutex_creations==1 && !held);
 unsigned before=calls;
 for(unsigned i=0;i<10;i++)seen_cache_init();
 assert(calls==before && mutex_creations==1); /* One boot allocation, never leaked/replaced. */
 const char *stable_uid="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
 assert(!seen_contains(stable_uid));
 attendance_event_t event={0};memcpy(event.event_uid,stable_uid,65);strcpy(event.cnic,"1234567890123");
 assert(enqueue_event_to_files(&event,"LIVE")==ENQUEUE_PENDING && preserved==1);
 if(FAIL){
  assert(!g_seen_cache.keys && !g_seen_cache.occupied && allocations==frees);
  assert(!seen_add(stable_uid) && !seen_contains(stable_uid));
  /* Actual capture retries append the same immutable identity, with no
   * duplicate claim, acknowledgement or retirement from the missing cache. */
  assert(enqueue_event_to_files(&event,"LIVE")==ENQUEUE_PENDING && preserved==2);
  assert(!strcmp(preserved_uids[0],stable_uid) && !strcmp(preserved_uids[1],stable_uid));
 }else{
  assert(allocations==2 && !frees && g_seen_cache.keys && g_seen_cache.occupied);
  assert(seen_add(stable_uid) && seen_contains(stable_uid) && g_seen_cache.count==1);
  assert(seen_add(stable_uid) && g_seen_cache.count==1);
  assert(enqueue_event_to_files(&event,"LIVE")==ENQUEUE_DUPLICATE && preserved==1);
  busy=true;assert(!seen_contains(stable_uid) && !seen_add(stable_uid) && !held);busy=false;
  assert(seen_contains(stable_uid) && g_seen_cache.count==1);
 }
 /* A failed durable append cannot become acceptance merely because the
  * cache is present/absent, and must not seed a future duplicate shortcut. */
 event.event_uid[0]='f';refuse_append=true;
 assert(enqueue_event_to_files(&event,"LIVE")==ENQUEUE_STORAGE_ERROR && !seen_contains(event.event_uid));
 assert(!g_force_truth_reconcile && !strcmp(event.event_uid+1,stable_uid+1));
 heap_caps_free(g_seen_cache.keys);heap_caps_free(g_seen_cache.occupied);
 assert(!held && allocations==frees);return 0;
}
'''.replace("/* ACTUAL */", actual)
    unit = tmp_path / "cache.c"
    unit.write_text(harness)
    # Rename only the actual cache implementation entry points, so wrappers
    # prove real gateway contains/add calls hold their mutex.
    implementation = tmp_path / "uid_cache.c"
    implementation.write_text((main / "uid_cache.c").read_text().replace(
        "uid_cache_contains", "real_contains").replace("uid_cache_add", "real_add"))
    binary = tmp_path / "cache"
    subprocess.run(
        [shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", f"-DFAIL={failed_allocation}", "-I", str(main),
         str(unit), str(implementation), "-o", str(binary)], check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=10)


def test_hikvision_retains_static_occupancy_and_existing_allocation_policy():
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    assert "#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION\nstatic uint8_t g_seen_occupied[SEEN_UID_CAPACITY / 8];\n#endif" in source
    initialization = _function(source, "static void seen_cache_init(")
    hikvision = initialization.split("#if defined(ZONE_LITE_HIKVISION) && ZONE_LITE_HIKVISION", 1)[1].split("#else", 1)[0]
    assert "g_seen_cache.occupied = g_seen_occupied;" in hikvision
    assert "g_seen_cache.keys = calloc(SEEN_UID_CAPACITY, 32);" in hikvision
