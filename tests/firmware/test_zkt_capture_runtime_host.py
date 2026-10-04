"""Exercise entry/idle snapshots and lock refusal in the actual capture adapter."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_capture_adapter_publishes_entry_before_work_and_never_invents_idle_progress(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    header = tmp_path / "platform.h"
    header.write_text('''#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
typedef struct {bool locked;} *SemaphoreHandle_t;
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t,unsigned);
void xSemaphoreGive(SemaphoreHandle_t);
void vSemaphoreDelete(SemaphoreHandle_t);
void *heap_caps_calloc(size_t,size_t,unsigned);
void heap_caps_free(void *);
int64_t esp_timer_get_time(void);
void esp_fill_random(void *,size_t);
void vTaskDelay(unsigned);
int mbedtls_sha256(const unsigned char *,size_t,unsigned char *,int);
''')
    for name in ("esp_heap_caps.h", "esp_random.h", "esp_timer.h", "freertos/FreeRTOS.h",
                 "freertos/semphr.h", "freertos/task.h", "mbedtls/sha256.h"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "platform.h"\n')
    (tmp_path / "cJSON.h").write_text('typedef struct cJSON cJSON;\n')
    unit = tmp_path / "capture.c"
    unit.write_text(r'''
#include "platform.h"
#include "zkt_capture_runtime.h"
#include "zkt_storage_owner.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
static uint32_t clock_ms=1000;
static bool writer=true, fail_allocation, refused_lock;
static unsigned calls;
SemaphoreHandle_t xSemaphoreCreateMutex(void){return calloc(1,sizeof(struct {bool locked;}));}
int xSemaphoreTake(SemaphoreHandle_t lock,unsigned wait){(void)wait;assert(lock&&!lock->locked);if(refused_lock)return 0;lock->locked=true;return 1;}
void xSemaphoreGive(SemaphoreHandle_t lock){assert(lock->locked);lock->locked=false;}
void vSemaphoreDelete(SemaphoreHandle_t lock){assert(!lock->locked);free(lock);}
void *heap_caps_calloc(size_t count,size_t size,unsigned caps){assert(caps==3);return fail_allocation?NULL:calloc(count,size);}
void heap_caps_free(void *value){free(value);}
int64_t esp_timer_get_time(void){return (int64_t)clock_ms*1000;}
void esp_fill_random(void *out,size_t size){memset(out,1,size);}
void vTaskDelay(unsigned duration){clock_ms+=duration;}
int mbedtls_sha256(const unsigned char *p,size_t n,unsigned char *out,int variant){(void)p;(void)n;(void)variant;memset(out,1,32);return 0;}
bool zj_runtime_writer_ready(void){return writer;}
bool zj_owner_health(zj_owner_health_t *out){*out=(zj_owner_health_t){.ready=true,.compatibility_checked=true,.writer_allowed=true};return true;}
bool zj_owner_submit(const zj_request_t *r,uint64_t *t){(void)r;(void)t;return false;}
bool zj_owner_poll(uint64_t t,zj_reply_t *r,bool *c){(void)t;(void)r;(void)c;return false;}
bool zj_owner_abandon(uint64_t t){(void)t;return true;}
bool zj_capture_init(zj_capture_t *c,zj_capture_port_t port){c->port=port;c->initialized=true;return true;}
bool zj_capture_packet(zj_capture_t *c,const uint8_t *p,size_t n,const zj_capture_facts_t *f){
 (void)p;(void)n;(void)f;++calls;
 zj_capture_health_t snapshot;assert(zj_capture_runtime_health(&snapshot));
 assert(snapshot.running&&snapshot.started_ms==clock_ms&&snapshot.sampled_ms==clock_ms);
 c->health.running=true;c->health.started_ms=clock_ms;c->port.wait_ms(c->port.context,10);
 assert(zj_capture_runtime_health(&snapshot)&&snapshot.running);
 c->health.running=false;c->health.last_result=ZJ_UNCERTAIN;++c->health.failures;return false;
}
#include "zkt_capture_runtime.c"
int main(void){
 zj_capture_health_t health;
 assert(!zj_capture_runtime_health(&health));
 fail_allocation=true;assert(!zj_capture_runtime_start());assert(!zj_capture_runtime_health(&health));
 fail_allocation=false;assert(zj_capture_runtime_start());assert(!zj_capture_runtime_start());
 assert(zj_capture_runtime_health(&health)&&!health.running&&health.sampled_ms==1000);
 clock_ms=200000;
 assert(zj_capture_runtime_health(&health)&&health.sampled_ms==1000&&!health.packets&&!health.progress_ms);
 writer=false;assert(!zj_capture_runtime_packet(NULL,0,NULL)&&!calls);writer=true;
 refused_lock=true;assert(!zj_capture_runtime_packet(NULL,0,NULL)&&!calls);refused_lock=false;
 assert(!zj_capture_runtime_packet(NULL,0,NULL)&&calls==1);
 assert(zj_capture_runtime_health(&health)&&!health.running&&health.last_result==ZJ_UNCERTAIN&&health.failures==1);
 assert(health.sampled_ms==200010);
 heap_caps_free(capture);vSemaphoreDelete(capture_lock);vSemaphoreDelete(health_lock);
}
''')
    executable = tmp_path / "capture"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(tmp_path), "-I", str(main),
                    str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)
