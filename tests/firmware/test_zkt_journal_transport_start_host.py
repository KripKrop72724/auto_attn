"""The real journal task adapter must survive the 3FL internal-heap shape."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_transport_retains_full_stack_when_dynamic_internal_heap_is_fragmented(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    headers = {
        "esp_attr.h": "#define DRAM_ATTR\n",
        "esp_heap_caps.h": "#include <stddef.h>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid *heap_caps_calloc(size_t,size_t,unsigned);\nvoid heap_caps_free(void *);\n",
        "esp_random.h": "#include <stdint.h>\nuint32_t esp_random(void);\n",
        "esp_timer.h": "#include <stdint.h>\nint64_t esp_timer_get_time(void);\n",
        "esp_log.h": "#define ESP_LOGE(...) ((void)0)\n",
        "mbedtls/sha256.h": "#include <stddef.h>\nint mbedtls_sha256(const unsigned char *,size_t,unsigned char *,int);\n",
        "freertos/FreeRTOS.h": """#pragma once
#include <stdint.h>
typedef unsigned TickType_t;
typedef uint8_t StackType_t;
typedef struct { unsigned used; } StaticTask_t;
typedef struct { unsigned used; } StaticSemaphore_t;
typedef void *SemaphoreHandle_t;
typedef void *TaskHandle_t;
#define pdPASS 1
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
""",
        "freertos/semphr.h": """#include "FreeRTOS.h"
SemaphoreHandle_t xSemaphoreCreateMutex(void);
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *);
int xSemaphoreTake(SemaphoreHandle_t,TickType_t);
void xSemaphoreGive(SemaphoreHandle_t);
void vSemaphoreDelete(SemaphoreHandle_t);
""",
        "freertos/task.h": """#include "FreeRTOS.h"
int xTaskCreate(void (*)(void *),const char *,unsigned,void *,unsigned,TaskHandle_t *);
TaskHandle_t xTaskCreateStatic(void (*)(void *),const char *,unsigned,void *,unsigned,StackType_t *,StaticTask_t *);
void vTaskDelay(TickType_t);
""",
    }
    for name, content in headers.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    harness = tmp_path / "journal-task.c"
    harness.write_text(r'''
#include <assert.h>
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>
#include "zkt_journal_transport.c"
static bool owner_available, allocation_fails;
static unsigned dynamic_tasks, static_tasks, allocations;
static uint32_t ticks=100;
static jmp_buf yielded;
static void (*worker)(void *);
static void *worker_context;
static StaticSemaphore_t fallback_mutex;
void *heap_caps_calloc(size_t n,size_t bytes,unsigned caps) {
    assert(caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(allocation_fails)return NULL;
    ++allocations; return calloc(n,bytes);
}
void heap_caps_free(void *p) { if(p){--allocations;free(p);} }
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &fallback_mutex; }
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *s) { assert(s);return s; }
int xSemaphoreTake(SemaphoreHandle_t s,TickType_t wait) { assert(s&&wait<=100);return pdTRUE; }
void xSemaphoreGive(SemaphoreHandle_t s) { assert(s); }
void vSemaphoreDelete(SemaphoreHandle_t s) { assert(s); }
int xTaskCreate(void (*fn)(void *),const char *name,unsigned size,void *arg,unsigned priority,TaskHandle_t *out) {
    (void)fn;(void)name;(void)arg;(void)priority;(void)out;
    ++dynamic_tasks;assert(size==12288);
    /* Field evidence: 35,479 bytes free, largest internal block 11,776. */
    return size<=11776 ? pdPASS : 0;
}
TaskHandle_t xTaskCreateStatic(void (*fn)(void *),const char *name,unsigned size,void *arg,unsigned priority,StackType_t *stack,StaticTask_t *control) {
    assert(fn&&arg&&stack&&control&&size==12288&&priority==3);
    assert(!strcmp(name,"zkt_journal_tx"));
    memset(stack,0xa5,size); /* ASan verifies the entire promised stack exists. */
    ++static_tasks;worker=fn;worker_context=arg;return control;
}
void vTaskDelay(TickType_t delay) { assert(delay>0);longjmp(yielded,1); }
uint32_t esp_random(void) { return 17; }
int64_t esp_timer_get_time(void) { return (int64_t)ticks*1000; }
int mbedtls_sha256(const unsigned char *p,size_t n,unsigned char *out,int unused) {
    (void)p;(void)n;(void)unused;memset(out,0x12,32);return 0;
}
bool add_connector_is_connected(void) { return true; }
bool add_connector_send_zkt_custody_acknowledged(const char *p,uint32_t timeout,uint8_t out[32]) {
    assert(p&&timeout);memset(out,0x34,32);return true;
}
bool zj_owner_health(zj_owner_health_t *h) { *h=(zj_owner_health_t){.started=owner_available};return owner_available; }
bool zj_owner_submit(const zj_request_t *r,uint64_t *t) { assert(r&&t);return false; }
bool zj_owner_poll(uint64_t t,zj_reply_t *r,bool *done) { (void)t;(void)r;*done=false;return true; }
bool zj_owner_abandon(uint64_t t) { return t!=0; }
bool zj_delivery_init(zj_delivery_t *d,zj_delivery_port_t p) {
    assert(p.now_ms&&p.random&&p.connected&&p.submit&&p.poll&&p.abandon&&p.send&&p.crypto.digest);
    d->port=p;d->initialized=true;return true;
}
uint32_t zj_delivery_pump(zj_delivery_t *d,void (*observe)(const zj_delivery_t *,void *),void *ctx) {
    assert(d->initialized);d->health.receipts=2;observe(d,ctx);return 10;
}
int main(void) {
    assert(!zj_transport_start()&&!allocations&&!static_tasks&&!dynamic_tasks);
    owner_available=true;allocation_fails=true;
    assert(!zj_transport_start()&&!allocations&&!static_tasks&&!dynamic_tasks);
    allocation_fails=false;
    assert(zj_transport_start());
    assert(static_tasks==1&&!dynamic_tasks&&allocations==1);
    assert(!zj_transport_start()&&static_tasks==1&&allocations==1);
    if(!setjmp(yielded))worker(worker_context);
    zj_transport_health_t health={0};
    assert(zj_transport_health(&health)&&health.started&&health.sampled_ms==ticks&&health.delivery.receipts==2);
    heap_caps_free(delivery);delivery=NULL;
    assert(!allocations);
    return 0;
}
''')
    executable = tmp_path / "journal-task"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(main), str(harness), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)
