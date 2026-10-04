"""Execute production lease persistence and command adapter with faulted ports."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_lease_command_adapter_preserves_revocation_obligation(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    clear = source[source.index("static bool temp_admin_clear(void)"):
                   source.index("static bool temp_admin_clear_owned(")]
    persist = source[source.index("static bool temp_admin_persist("):
                     source.index("static bool temp_admin_write(")]
    execute = source[source.index("static bool execute_temp_admin_grant("):
                     source.index("static bool temp_admin_revoke_if_due(")]
    program = r'''
#define ZONE_LITE_HIKVISION 1
#include "lease_guard.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static bool g_temp_admin_active;
static uint16_t g_temp_admin_uid;
static lg_watch_t g_temp_admin_watch;
static int64_t g_temp_admin_expires_epoch,clock_now=1800000000,mono_now=1000;
static int64_t epoch_now(void){return clock_now;}
static int64_t uptime_ms(void){return mono_now;}
static unsigned writes,fail_at,mutations;
static bool temp_admin_checkpoint(void){return ++writes!=fail_at;}
typedef struct {int unused;} zk_context_t;
typedef struct {int unused;} user_table_t;
typedef struct {int duration_seconds;char uid[16];int64_t lease_expires_epoch;} add_command_t;
typedef struct {int sock;zk_context_t *ctx;user_table_t *users;const add_command_t *command;} temp_admin_port_t;
typedef struct {char terminal_identity_fingerprint[65],terminal_state_fingerprint[65];} zkt_user_t;
static zkt_user_t row={"identity","state"};
static bool temp_admin_prepare_binding(const zkt_user_t *user,uint16_t uid){assert(user==&row&&uid==7);return true;}
static bool ensure_system_time_synced(void){return true;}
static const zkt_user_t *find_user_by_uid(const user_table_t *t,uint16_t uid){(void)t;assert(uid==7);return &row;}
static int64_t temp_admin_now(void *arg){(void)arg;return clock_now;}
static bool temp_admin_elevate(void *arg,uint16_t uid){(void)arg;assert(uid==7 && g_temp_admin_active && writes);mutations++;return true;}
static bool temp_admin_revoke(void *arg,uint16_t uid){(void)arg;assert(uid==7);return true;}
static bool temp_admin_verify(void *arg,uint16_t uid,int privilege){(void)arg;assert(uid==7);if(privilege==14){clock_now+=2;mono_now+=2000;}return true;}
''' + clear + persist + execute + r'''
static void reset(void){g_temp_admin_active=false;g_temp_admin_uid=0;g_temp_admin_expires_epoch=0;g_temp_admin_watch=(lg_watch_t){0};clock_now=1800000000;mono_now=1000;writes=fail_at=mutations=0;}
int main(void){
 add_command_t command={.duration_seconds=600,.uid="7"};zk_context_t ctx={0};user_table_t users={0};const char *code,*message;char result[512];
 reset();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(g_temp_admin_active && g_temp_admin_expires_epoch==1800000602 && writes==2 && mutations==1);
 int64_t bound=g_temp_admin_watch.deadline_ms;
 clock_now+=10;mono_now+=10000;
 assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(g_temp_admin_expires_epoch==1800000602 && g_temp_admin_watch.deadline_ms==bound);
 mono_now=bound;unsigned previous_writes=writes,previous_mutations=mutations;
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(!strcmp(code,"ADMIN_LEASE_REVOCATION_REQUIRED") && writes==previous_writes && mutations==previous_mutations);
 reset();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 g_temp_admin_watch=(lg_watch_t){0}; /* restored durable lease after ESP reboot */
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(!strcmp(code,"ADMIN_LEASE_REVOCATION_REQUIRED") && writes==2 && mutations==1);
 reset();fail_at=1;assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)) && !mutations);
 assert(!strcmp(code,"LEASE_CHECKPOINT_FAILED"));
 reset();fail_at=2;assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(!g_temp_admin_active && writes==3);
 reset();g_temp_admin_active=true;g_temp_admin_uid=9;g_temp_admin_expires_epoch=1800000100;
 assert(lg_watch_arm(&g_temp_admin_watch,9,g_temp_admin_expires_epoch,clock_now,mono_now));
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


def test_local_revocation_uses_monotonic_expiry_and_retains_failed_obligations(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    clear = source[source.index("static bool temp_admin_clear(void)"):
                   source.index("static bool temp_admin_clear_owned(")]
    revoke = source[source.index("static bool temp_admin_revoke_if_due("):
                    source.index("static bool temp_admin_command_held(")]
    program = r'''
#define ZONE_LITE_HIKVISION 1
#include "lease_guard.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static bool g_temp_admin_active;
static uint16_t g_temp_admin_uid;
static int64_t g_temp_admin_expires_epoch,wall=1800000000,monotonic=1000;
static lg_watch_t g_temp_admin_watch;
static bool connected=true,exists=true,commit=true,write_ok=true,apply=true;
static unsigned reads,writes,saves;
static int64_t epoch_now(void){return wall;}
static int64_t uptime_ms(void){return monotonic;}
static bool temp_admin_checkpoint(void){++saves;return commit;}
typedef struct {int unused;} zk_context_t;
typedef struct {int unused;} user_table_t;
typedef struct {int privilege;} zkt_user_t;
static zkt_user_t row={14};
static bool temp_admin_evidence_ready(void){return true;}
static bool temp_admin_identity_matches(const zkt_user_t *user){return user==&row;}
static struct {int user_count,attendance_count;} g_add_zkt;
static bool zk_get_counts(int sock,zk_context_t *ctx,int32_t *users,int32_t *records)
{(void)sock;(void)ctx;++reads;*users=11;*records=22;return connected;}
static bool zk_refresh_users_preserving_current(int sock,zk_context_t *ctx,user_table_t *users,int32_t count)
{(void)sock;(void)ctx;(void)users;assert(count==11);return connected;}
static zkt_user_t *find_mutable_user_by_uid(user_table_t *users,const char *uid)
{(void)users;assert(!strcmp(uid,"7"));return exists?&row:NULL;}
static bool zk_write_user(int sock,zk_context_t *ctx,zkt_user_t *user,const char *name,int privilege)
{(void)sock;(void)ctx;assert(user==&row&&!name&&!privilege);++writes;if(write_ok&&apply)row.privilege=0;return write_ok;}
static void add_connector_set_zkt(const void *value){assert(value==&g_add_zkt);}
static void add_connector_log(const char *level,const char *component,const char *code,const char *message)
{(void)level;(void)component;(void)code;(void)message;}
#define LED_STATUS_ZKT_FAILURE 1
static void led_status_fault(int value){assert(value==1);}
''' + clear + revoke + r'''
static void active(void){g_temp_admin_active=true;g_temp_admin_uid=7;g_temp_admin_expires_epoch=wall+600;row.privilege=14;}
int main(void){
 zk_context_t ctx={0};user_table_t users={0};
 active();assert(lg_watch_arm(&g_temp_admin_watch,7,g_temp_admin_expires_epoch,wall,monotonic));
 monotonic+=599999;assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!reads&&!writes&&!saves);
 ++monotonic; /* Wall time is frozen but the duration cannot grow. */
 assert(temp_admin_revoke_if_due(1,&ctx,&users));
 assert(!g_temp_admin_active&&!g_temp_admin_watch.armed&&!row.privilege&&writes==1&&saves==1);
 active(); /* no boot-local anchor survives reboot */
 connected=false;assert(!temp_admin_revoke_if_due(1,&ctx,&users));
 assert(g_temp_admin_active&&g_temp_admin_watch.due&&row.privilege==14);
 connected=true;exists=false;assert(!temp_admin_revoke_if_due(1,&ctx,&users));
 assert(g_temp_admin_active&&writes==1&&saves==1);
 exists=true;apply=false;assert(!temp_admin_revoke_if_due(1,&ctx,&users));
 assert(g_temp_admin_active&&row.privilege==14&&saves==1);
 apply=true;commit=false;assert(!temp_admin_revoke_if_due(1,&ctx,&users));
 assert(g_temp_admin_active&&g_temp_admin_uid==7&&g_temp_admin_watch.due&&!row.privilege);
 unsigned previous=writes;commit=true;assert(temp_admin_revoke_if_due(1,&ctx,&users));
 assert(!g_temp_admin_active&&!g_temp_admin_watch.armed&&writes==previous);
 active();assert(lg_watch_arm(&g_temp_admin_watch,7,g_temp_admin_expires_epoch,wall,monotonic));
 --wall;assert(temp_admin_revoke_if_due(1,&ctx,&users));
 assert(!g_temp_admin_active&&!row.privilege);
 return 0;
}
'''
    unit = tmp_path / "lease_watchdog.c"
    unit.write_text(program)
    executable = tmp_path / "lease_watchdog"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(firmware),
                    str(unit), str(firmware / "lease_guard.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
