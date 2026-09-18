"""The production startup loop preserves running capture during delivery failures."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("hikvision", [0, 1])
def test_startup_retries_do_not_reboot_or_recreate_healthy_capture(tmp_path, hikvision):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    start = source.index("    // Retain handles and retry startup")
    body = source[start:]
    harness = r'''
#include "worker_retry.h"
#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#define pdPASS 1
#define ESP_LOGE(...) ((void)0)
#define pdMS_TO_TICKS(x) (x)
#define LED_STATUS_LOCAL_FAILURE 1
typedef void *TaskHandle_t;
static jmp_buf done;
static uint32_t now,stop_at;
static unsigned gateway_attempts,ords_attempts,capture_ticks,faults,reported_attempts;
static bool gateway_created,ords_created,ords_always_fails,first_fails;
static int64_t uptime_ms(void){return now;}
static void gateway_task(void *arg){(void)arg;}
void ords_uploader_task(void *arg){(void)arg;}
static int xTaskCreate(void (*task)(void *),const char *name,unsigned stack,void *arg,unsigned priority,TaskHandle_t *handle)
{
    (void)name;(void)stack;(void)arg;(void)priority;
    if(task==gateway_task){++gateway_attempts;assert(!gateway_created);if(first_fails && gateway_attempts==1)return 0;gateway_created=true;}
    else{assert(task==ords_uploader_task && !ords_created);++ords_attempts;if(ords_always_fails || (first_fails && ords_attempts==1))return 0;ords_created=true;}
    *handle=(void *)1;return pdPASS;
}
void add_connector_report_ords_start(bool started,uint32_t attempts){assert(started==ords_created);reported_attempts=attempts;}
static bool g_queue_store_ready=true;
static bool qs_init(void){return true;}
static void led_status_fault(int state){assert(state==1);++faults;}
static void vTaskDelay(unsigned ms){if(gateway_created)++capture_ticks;now+=ms;if(now>=stop_at)longjmp(done,1);}
static void launch(void)
{
/* PRODUCTION */
int main(void)
{
    ords_always_fails=true;stop_at=600000;
    if(!setjmp(done))launch();
#if ZONE_LITE_HIKVISION
    assert(gateway_attempts==1 && ords_attempts==0 && capture_ticks==600 && faults==0);
#else
    assert(gateway_attempts==1 && ords_attempts==3 && capture_ticks==600 && reported_attempts==3 && faults==600);
#endif
    now=gateway_attempts=ords_attempts=capture_ticks=faults=reported_attempts=0;
    gateway_created=ords_created=ords_always_fails=false;first_fails=true;stop_at=700000;
    if(!setjmp(done))launch();
#if ZONE_LITE_HIKVISION
    assert(gateway_attempts==2 && ords_attempts==0 && capture_ticks==699 && faults==1);
#else
    assert(gateway_attempts==2 && ords_attempts==2 && capture_ticks==699 && reported_attempts==2 && faults==1);
#endif
    return 0;
}
'''
    unit = tmp_path / "startup.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    executable = tmp_path / "startup"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        f"-DZONE_LITE_HIKVISION={hikvision}", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(firmware),
        str(unit), str(firmware / "worker_retry.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
