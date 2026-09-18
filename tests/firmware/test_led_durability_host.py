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
    fault = source[source.index("void led_status_fault("):source.index("void led_status_event(")]
    program = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
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
