"""Storage alarms survive elapsed time and unrelated network status changes."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_actual_led_storage_fault_remains_latched(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "led_status.c").read_text()
    state = source[source.index("typedef struct {"):source.index("static led_strip_handle_t")]
    priority = source[source.index("static int priority_for_status("):source.index("static void set_rgb(")]
    fault = source[source.index("typedef enum { LED_CAUSE_OTHER"):source.index("void led_status_event(")]
    program = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "led_status.h"
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
#define ZONE_LITE_LED_FAULT_LATCH_MS 120000
static bool s_started=true;
static void *s_led_lock=(void *)1;
static int xSemaphoreTake(void *m,int t){(void)m;(void)t;return 1;}
static void xSemaphoreGive(void *m){(void)m;}
static int64_t now_ms(void){return 1;}
''' + state + "\nstatic led_state_t s_state;\n" + priority + fault + r'''
int main(void) {
    s_state.base=LED_STATUS_HEALTHY;
    bool flash;
    led_status_fault(LED_STATUS_LOCAL_FAILURE);
    expire_recoverable_fault(&s_state,900000);
    assert(select_status(&s_state,900000,&flash)==LED_STATUS_LOCAL_FAILURE);
    led_status_fault(LED_STATUS_ORDS_FAILURE);
    led_status_clear_fault(LED_STATUS_ORDS_FAILURE);
    assert(select_status(&s_state,900000,&flash)==LED_STATUS_LOCAL_FAILURE);
    led_status_clear_fault(LED_STATUS_LOCAL_FAILURE);
    assert(select_status(&s_state,900000,&flash)==LED_STATUS_HEALTHY);
    /* Incidents are counted with their first and latest sources. The
     * supervisor's clear refuses to drop an incident raised after the
     * verification it relied on. */
    bool active;
    uint32_t seen=led_status_local_failures(&active);
    assert(!active && seen==1);
    led_status_fault(LED_STATUS_LOCAL_FAILURE);
    led_status_fault(LED_STATUS_LOCAL_FAILURE);
    seen=led_status_local_failures(&active);
    assert(active && seen==3);
    led_local_failure_t report;
    led_status_local_failure_report(&report);
    assert(report.active && report.incidents==3 && !report.recoveries);
    assert(strstr(report.first_source,"led.c:") && strcmp(report.first_source,report.last_source));
    expire_recoverable_fault(&s_state,9000000);
    assert(select_status(&s_state,9000000,&flash)==LED_STATUS_LOCAL_FAILURE);
    assert(!led_status_clear_local_failure(seen-1));
    assert(led_status_clear_local_failure(seen));
    led_status_local_failure_report(&report);
    assert(!report.active && report.recoveries==1 && !report.first_source[0] && report.last_source[0]);
    assert(select_status(&s_state,9000000,&flash)==LED_STATUS_HEALTHY);
    assert(!led_status_clear_local_failure(seen));
    /* ZKT 2.7.7: incidents by the errno their site captured. Only ENOSPC is a
     * room refusal; only EIO on a reclaim queue a held read, with the largest
     * start offset per queue. */
    led_local_causes_t causes;
    led_status_local_causes(&causes);
    assert(causes.known && !causes.fatal && causes.incidents==3 && !causes.room && !causes.held_reads);
    led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, ENOSPC);
    led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, ENOENT);
    led_status_fault_errno(LED_STATUS_LOCAL_FAILURE, 0);
    led_status_fault_errno(LED_STATUS_ORDS_FAILURE, ENOSPC);
    led_status_fault_read(LED_STATUS_LOCAL_FAILURE, EIO, LED_QUEUE_PENDING, 493968);
    led_status_fault_read(LED_STATUS_LOCAL_FAILURE, EIO, LED_QUEUE_PENDING, 1000);
    led_status_fault_read(LED_STATUS_LOCAL_FAILURE, ENOENT, LED_QUEUE_BLOCKED, 7);
    led_status_fault_read(LED_STATUS_LOCAL_FAILURE, EIO, LED_QUEUE_COUNT, 7);
    led_status_local_causes(&causes);
    assert(causes.incidents==10 && causes.room==1 && causes.held_reads==2);
    assert(causes.held_queues==(1U<<LED_QUEUE_PENDING) && causes.held_last[LED_QUEUE_PENDING]==493968);
    assert(!causes.held_last[LED_QUEUE_BLOCKED]);
    led_status_fault(LED_STATUS_FATAL);
    led_status_local_causes(&causes);
    assert(causes.fatal && causes.incidents==10);
    return 0;
}
'''
    unit = tmp_path / "led.c"
    unit.write_text(program)
    executable = tmp_path / "led"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(firmware), str(unit),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
