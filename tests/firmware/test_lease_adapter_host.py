"""Execute production lease persistence and command adapter with faulted ports."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_lease_command_adapter_preserves_revocation_obligation(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    clear = source[source.index("static bool temp_admin_clear(void)"):
                   source.index("typedef struct {", source.index("static bool temp_admin_clear(void)"))]
    persist = source[source.index("static bool temp_admin_persist("):
                     source.index("static bool temp_admin_write(")]
    execute = source[source.index("static bool execute_temp_admin_grant("):
                     source.index("static bool temp_admin_revoke_if_due(")]
    program = r'''
#include "lease_guard.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static bool g_temp_admin_active;
static uint16_t g_temp_admin_uid;
static int64_t g_temp_admin_expires_epoch,clock_now=1800000000;
static unsigned writes,fail_at,mutations;
static bool nvs_save_runtime_state(void){return ++writes!=fail_at;}
typedef struct {int unused;} zk_context_t;
typedef struct {int unused;} user_table_t;
typedef struct {int duration_seconds;char uid[16];int64_t lease_expires_epoch;} add_command_t;
typedef struct {int sock;zk_context_t *ctx;user_table_t *users;const add_command_t *command;} temp_admin_port_t;
typedef struct {char terminal_identity_fingerprint[65],terminal_state_fingerprint[65];} zkt_user_t;
static zkt_user_t row={"identity","state"};
static bool ensure_system_time_synced(void){return true;}
static const zkt_user_t *find_user_by_uid(const user_table_t *t,uint16_t uid){(void)t;assert(uid==7);return &row;}
static int64_t temp_admin_now(void *arg){(void)arg;return clock_now;}
static bool temp_admin_elevate(void *arg,uint16_t uid){(void)arg;assert(uid==7 && g_temp_admin_active && writes);mutations++;return true;}
static bool temp_admin_revoke(void *arg,uint16_t uid){(void)arg;assert(uid==7);return true;}
static bool temp_admin_verify(void *arg,uint16_t uid,int privilege){(void)arg;assert(uid==7);if(privilege==14)clock_now+=2;return true;}
''' + clear + persist + execute + r'''
static void reset(void){g_temp_admin_active=false;g_temp_admin_uid=0;g_temp_admin_expires_epoch=0;clock_now=1800000000;writes=fail_at=mutations=0;}
int main(void){
 add_command_t command={.duration_seconds=600,.uid="7"};zk_context_t ctx={0};user_table_t users={0};const char *code,*message;char result[512];
 reset();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(g_temp_admin_active && g_temp_admin_expires_epoch==1800000602 && writes==2 && mutations==1);
 reset();fail_at=1;assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)) && !mutations);
 assert(!strcmp(code,"LEASE_CHECKPOINT_FAILED"));
 reset();fail_at=2;assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(!g_temp_admin_active && writes==3);
 reset();g_temp_admin_active=true;g_temp_admin_uid=9;g_temp_admin_expires_epoch=1800000100;
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)) && !writes && !mutations);
 assert(g_temp_admin_uid==9 && g_temp_admin_expires_epoch==1800000100);
 fail_at=1;assert(!temp_admin_clear());assert(g_temp_admin_active && g_temp_admin_uid==9 && g_temp_admin_expires_epoch==1800000100);
 reset();strcpy(command.uid,"65543");assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)) && !writes);
 return 0;
}
'''
    unit = tmp_path / "lease_adapter.c"
    unit.write_text(program)
    executable = tmp_path / "lease_adapter"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(firmware),
                    str(unit), str(firmware / "lease_guard.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
