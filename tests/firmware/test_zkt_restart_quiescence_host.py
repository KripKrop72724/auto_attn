"""Execute session handoff and OTA wait paths, including a request during capture."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def run(tmp_path, program, family=0):
    unit = tmp_path / "restart.c"
    unit.write_text(program)
    binary = tmp_path / "restart"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        *(["-DZONE_LITE_HIKVISION=1"] if family else []), str(unit), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)


def test_restart_cannot_overtake_terminal_owner_or_start_another_session(tmp_path):
    connector = (MAIN / "add_connector.c").read_text()
    start = connector.index("bool add_connector_terminal_session_begin(void)")
    production = connector[start:connector.index("bool add_connector_begin_pending_command_activity", start)]
    runtime = (MAIN / "zone_lite.c").read_text()
    declarations = [
        "int64_t gateway_run(uint32_t host_order_ip)",
        "bool maybe_reboot_zkt_for_recovery(uint32_t discovery_failures, int64_t *last_reboot_ms)",
        "bool daily_zkt_reboot_try_target(uint32_t target_ip, int local_day_key)",
        "bool discover_zkt(uint32_t *selected_ip, uint32_t skip_ip)",
        "bool process_pending_comm_key_command(void)",
    ]
    for declaration in declarations:
        start = runtime.index("static " + declaration + "\n{")
        production += runtime[start:runtime.index("\n}\n", start) + 3]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
static int mutex,*s_lock=&mutex;
static bool s_ota_restart_claimed,s_terminal_session_active,body_finished;
static unsigned fail_locks,delays,bodies;
static char s_activity[32]="LIVE_CAPTURE";
static struct {bool online;char connection_state[32];} s_zkt={true,"ONLINE"};
static int xSemaphoreTake(int *lock,int timeout)
{(void)timeout;assert(lock==&mutex && !mutex);if(fail_locks){--fail_locks;return 0;}mutex=1;return pdTRUE;}
static void xSemaphoreGive(int *lock){assert(lock==&mutex&&mutex);mutex=0;}
#define strlcpy copy_string
static void copy_string(char *out,const char *in,size_t capacity){assert(strlen(in)<capacity);strcpy(out,in);}
static void vTaskDelay(unsigned ms){assert(ms==10 && body_finished && s_terminal_session_active && !mutex);++delays;}
static int64_t gateway_run_session(uint32_t ip);
static bool maybe_reboot_zkt_for_recovery_session(uint32_t failures,int64_t *last);
static bool daily_zkt_reboot_try_target_session(uint32_t ip,int day);
static bool discover_zkt_session(uint32_t *ip,uint32_t skip);
static bool process_pending_comm_key_command_session(void);
/* PRODUCTION */
static void session(void)
{
    ++bodies;assert(s_terminal_session_active && !body_finished);
    assert(!add_connector_terminal_session_begin());
    /* This is the point at which a frame/save/command is still in progress. */
    assert(!add_connector_claim_ota_restart() && s_ota_restart_claimed);
    assert(add_connector_terminal_restart_pending());
    assert(!add_connector_terminal_session_begin());
    assert(!add_connector_claim_ota_restart());
    body_finished=true;fail_locks=3; /* cleanup finished; release must retry */
}
static int64_t gateway_run_session(uint32_t ip){assert(ip==7);session();return 17;}
static bool maybe_reboot_zkt_for_recovery_session(uint32_t failures,int64_t *last)
{assert(failures==2&&*last==3);session();return true;}
static bool daily_zkt_reboot_try_target_session(uint32_t ip,int day)
{assert(ip==7&&day==4);session();return true;}
static bool discover_zkt_session(uint32_t *ip,uint32_t skip)
{assert(*ip==7&&skip==5);session();return true;}
static bool process_pending_comm_key_command_session(void){session();return true;}
static void reset(void)
{assert(!mutex);s_ota_restart_claimed=s_terminal_session_active=body_finished=false;delays=bodies=fail_locks=0;strcpy(s_activity,"LIVE_CAPTURE");}
static void finished(void)
{
    assert(body_finished && bodies==1 && delays==3 && !s_terminal_session_active);
    assert(add_connector_claim_ota_restart() && !mutex);
    assert(!add_connector_terminal_session_begin());
}
int main(void)
{
    fail_locks=1;assert(!add_connector_terminal_session_begin()&&!s_terminal_session_active);
    fail_locks=1;assert(add_connector_terminal_restart_pending());
    assert(!add_connector_terminal_restart_pending());
    s_zkt.online=false;assert(!add_connector_claim_ota_restart()&&!s_ota_restart_claimed);s_zkt.online=true;
    strcpy(s_activity,"RECONCILING");assert(!add_connector_claim_ota_restart()&&!s_ota_restart_claimed);
    reset();assert(gateway_run(7)==17);finished();assert(gateway_run(7)==0&&bodies==1);
    int64_t last=3;reset();assert(maybe_reboot_zkt_for_recovery(2,&last));finished();
    reset();assert(daily_zkt_reboot_try_target(7,4));finished();
    uint32_t ip=7;reset();assert(discover_zkt(&ip,5));finished();
    reset();assert(process_pending_comm_key_command());finished();
    puts("Terminal session ownership, in-flight restart and cleanup handoff passed");
}
'''
    run(tmp_path, harness.replace("/* PRODUCTION */", production))


@pytest.mark.parametrize("family", [0, 1])
def test_ota_waits_for_owner_drain_only_after_terminal_handoff(tmp_path, family):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("static void wait_for_capture_safepoint(void)\n{")
    production = source[start:source.index("static bool uses_local_boot_confirmation(void)\n{", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define OTA_SAFEPOINT_REPORT_SECONDS 30
#define pdMS_TO_TICKS(x) (x)
static unsigned claims,waits,reports,owner_calls;
static uint64_t elapsed;
static bool handed_off;
static int64_t esp_timer_get_time(void){return elapsed;}
static void vTaskDelay(unsigned ms){++waits;elapsed+=(uint64_t)ms*1000;}
static bool add_connector_claim_ota_restart(void){if(++claims>=3)handed_off=true;return handed_off;}
static bool report_state(const char *state,const char *error)
{assert(!handed_off&&!owner_calls&&!strcmp(state,"READY_TO_BOOT")&&error[0]);++reports;return false;}
#ifndef ZONE_LITE_HIKVISION
static bool local=true;
static char s_last_error[64];
static bool uses_local_boot_confirmation(void){return local;}
static bool zj_owner_quiesce(void){assert(handed_off);return ++owner_calls>=4;}
#define strlcpy copy_string
static void copy_string(char *out,const char *in,size_t capacity){assert(strlen(in)<capacity);strcpy(out,in);}
#endif
/* PRODUCTION */
int main(void)
{
    wait_for_capture_safepoint();assert(handed_off&&reports>0);
#ifndef ZONE_LITE_HIKVISION
    assert(owner_calls==4 && waits==5 && !s_last_error[0]);
    local=false;owner_calls=claims=waits=reports=0;handed_off=false;
    wait_for_capture_safepoint();assert(handed_off && !owner_calls && waits==2);
#else
    assert(!owner_calls && waits==2);
#endif
}
'''
    run(tmp_path, harness.replace("/* PRODUCTION */", production), family=family)
