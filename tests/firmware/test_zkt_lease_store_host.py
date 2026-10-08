"""Execute the lease NVS port and production gateway adapters under faults."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def compile_host(tmp_path, unit):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    for header in ["esp_timer.h", "nvs.h", "freertos/FreeRTOS.h", "freertos/task.h"]:
        path = tmp_path / header
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#include "zkt_storage_owner_platform.h"\n')
    binary = tmp_path / "lease-owner"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(tmp_path), "-I", str(fixture), "-I", str(main), str(unit),
                    str(main / "zkt_lease_store.c"), str(main / "lease_guard.c"),
                    str(main / "durable_queue.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=30)


def test_lease_identity_generation_corruption_and_retained_owner_reply(tmp_path):
    compile_host(tmp_path, ROOT / "tests/firmware/zkt_lease_store_host.c")


def test_gateway_lease_restores_identity_without_trusting_runtime_or_reused_uid(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    end = source.index("\n}\n", source.index("static bool temp_admin_command_held(")) + 3
    adapters = source[source.index("static bool temp_admin_evidence_ready(void)"):end]
    program = r'''
#define ZL_GATEWAY_TEST
#include "zkt_lease_store_host.c"
#include "lease_guard.h"
#include <stdlib.h>
static bool g_temp_admin_active, g_committed_runtime_valid, g_lease_evidence_known, g_lease_alert_sent;
static uint16_t g_temp_admin_uid;
static int64_t g_temp_admin_expires_epoch, wall=1800000000, monotonic=1000;
static lg_watch_t g_temp_admin_watch;
static zl_lease_record_t g_committed_lease, g_lease_binding;
static runtime_checkpoint_t g_committed_runtime;
static char g_device_serial[81]="TEST-LEASE";
static bool writer_ready=true, connected=true, user_exists=true, apply_write=true;
static unsigned mutations, legacy_writes, logs;
typedef struct {int unused;} zk_context_t;
typedef struct {int unused;} user_table_t;
typedef struct {char uid[16],user_id[32],terminal_identity_fingerprint[65],terminal_state_fingerprint[65];int privilege;} zkt_user_t;
typedef struct {char uid[16],user_id[32],expected_terminal_identity_fingerprint[65],command_type[32];int duration_seconds;int64_t lease_expires_epoch;bool has_expected_terminal_identity_fingerprint;} add_command_t;
static zkt_user_t row;
static struct {int user_count,attendance_count;} g_add_zkt;
static size_t test_strlcpy(char *out,const char *in,size_t capacity){size_t length=strlen(in);assert(length<capacity);memcpy(out,in,length+1);return length;}
#define strlcpy test_strlcpy
static int64_t epoch_now(void){return wall;}
static int64_t uptime_ms(void){return monotonic;}
static bool ensure_system_time_synced(void){return true;}
static bool zj_runtime_writer_ready(void){return writer_ready;}
static bool nvs_save_runtime_state(void){++legacy_writes;return false;}
static bool zk_get_counts(int sock,zk_context_t *ctx,int32_t *users,int32_t *records)
{(void)sock;(void)ctx;*users=1;*records=10;return connected;}
static bool zk_refresh_users_preserving_current(int sock,zk_context_t *ctx,user_table_t *users,int32_t count)
{(void)sock;(void)ctx;(void)users;assert(count==1);return connected;}
static const zkt_user_t *find_user_by_uid(const user_table_t *users,uint16_t uid)
{(void)users;return user_exists&&strtoul(row.uid,NULL,10)==uid?&row:NULL;}
static zkt_user_t *find_mutable_user_by_uid(user_table_t *users,const char *uid)
{(void)users;return user_exists&&!strcmp(row.uid,uid)?&row:NULL;}
static bool zk_write_user(int sock,zk_context_t *ctx,zkt_user_t *user,const char *name,int privilege)
{(void)sock;(void)ctx;assert(user==&row&&!name);++mutations;if(apply_write)row.privilege=privilege;return true;}
static void add_connector_set_zkt(const void *value){assert(value==&g_add_zkt);}
static void add_connector_log(const char *level,const char *component,const char *code,const char *message)
{(void)level;(void)component;(void)code;(void)message;++logs;}
#define LED_STATUS_ZKT_FAILURE 1
static void led_status_fault(int value){assert(value==1);}
''' + adapters + r'''
static void reset_gateway(void){
 reset_ports();g_temp_admin_active=g_lease_evidence_known=g_lease_alert_sent=false;
 g_temp_admin_uid=0;g_temp_admin_expires_epoch=0;g_temp_admin_watch=(lg_watch_t){0};
 g_committed_lease=g_lease_binding=(zl_lease_record_t){0};
 g_committed_runtime=runtime_blob;g_committed_runtime_valid=true;
 row=(zkt_user_t){.uid="7",.user_id="synthetic-7"};memset(row.terminal_identity_fingerprint,'a',64);memset(row.terminal_state_fingerprint,'b',64);
 strcpy(g_device_serial,"TEST-LEASE");writer_ready=connected=user_exists=apply_write=true;
 mutations=legacy_writes=logs=0;wall=1800000000;monotonic=1000;
 temp_admin_load_lease_evidence();
}
static void reboot_gateway(void){
 g_temp_admin_active=false;g_temp_admin_uid=0;g_temp_admin_expires_epoch=0;g_temp_admin_watch=(lg_watch_t){0};
 g_committed_lease=g_lease_binding=(zl_lease_record_t){0};g_lease_evidence_known=g_lease_alert_sent=false;
 temp_admin_load_lease_evidence();
}
int main(void){
 zk_context_t ctx={0};user_table_t users={0};const char *code,*message;char result[512];
 add_command_t command={.uid="7",.user_id="synthetic-7",.duration_seconds=600,.has_expected_terminal_identity_fingerprint=true};
 memset(command.expected_terminal_identity_fingerprint,'a',64);
 reset_gateway();assert(temp_admin_evidence_ready());writer_ready=false;
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result))&&!mutations&&!writes);
 assert(!strcmp(code,"ADMIN_LEASE_IDENTITY_REQUIRED"));writer_ready=true;row.privilege=14;
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result))&&!mutations&&!writes);
 row.privilege=0;assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(durable.active&&durable.uid==7&&g_temp_admin_active&&row.privilege==14&&!legacy_writes);
 strcpy(command.command_type,"DELETE_USER");assert(temp_admin_command_held(&command));
 strcpy(command.command_type,"REFRESH_USERS");assert(!temp_admin_command_held(&command));
 strcpy(command.command_type,"GRANT_TEMP_ADMIN");assert(!temp_admin_command_held(&command));
 int64_t bound=g_temp_admin_watch.deadline_ms;
 ++wall;monotonic+=1000;
 assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 assert(g_temp_admin_watch.deadline_ms==bound);
 zkt_user_t other=row;strcpy(other.uid,"8");assert(!temp_admin_clear_owned(&other)&&g_temp_admin_active);
 runtime_blob.crc^=1;g_committed_runtime_valid=false;
 reboot_gateway();assert(temp_admin_evidence_ready()&&g_temp_admin_active&&!g_temp_admin_watch.armed);
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!durable.active&&!row.privilege&&!g_temp_admin_active&&!legacy_writes);
 assert(!runtime_checkpoint_valid(&runtime_blob));

 reset_gateway();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 reboot_gateway();memset(row.terminal_identity_fingerprint,'c',64);
 unsigned prior=mutations,prior_writes=writes;
 assert(temp_admin_revoke_if_due(1,&ctx,&users)); /* capture can continue */
 assert(!temp_admin_evidence_ready()&&g_temp_admin_active&&row.privilege==14&&mutations==prior&&writes==prior_writes);
 assert(!temp_admin_clear_owned(&row));
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result))&&mutations==prior);
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&logs);

 reset_gateway();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 reboot_gateway();strcpy(g_device_serial,"DIFFERENT-TERMINAL");prior=mutations;
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!temp_admin_evidence_ready()&&mutations==prior);

 reset_gateway();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 reboot_gateway();user_exists=false;prior=mutations;prior_writes=writes;
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!temp_admin_evidence_ready());
 assert(g_temp_admin_active&&mutations==prior&&writes==prior_writes&&!temp_admin_clear_owned(NULL));

 reset_gateway();assert(execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result)));
 durable.crc^=1;prior=mutations;prior_writes=writes;
 reboot_gateway();assert(!temp_admin_evidence_ready());
 assert(temp_admin_command_held(&command));strcpy(command.command_type,"REFRESH_USERS");
 assert(!temp_admin_command_held(&command));strcpy(command.command_type,"GRANT_TEMP_ADMIN");
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&mutations==prior&&writes==prior_writes);
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result))&&mutations==prior);

 reset_gateway();g_committed_runtime.lease_active=1;g_temp_admin_active=true;g_temp_admin_uid=7;
 temp_admin_load_lease_evidence();assert(!temp_admin_evidence_ready());
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!mutations&&!writes);

 reset_gateway();fault=COMMIT_AFTER;
 assert(!execute_temp_admin_grant(1,&ctx,&users,&command,&code,&message,result,sizeof(result))&&!mutations&&durable.active);
 fault=NONE;monotonic+=600000;
 assert(!temp_admin_revoke_if_due(1,&ctx,&users)); /* resolve exact uncertain active commit first */
 assert(g_committed_lease.active&&g_temp_admin_active&&!mutations);
 assert(temp_admin_revoke_if_due(1,&ctx,&users)&&!durable.active&&!g_temp_admin_active&&!mutations);
 return 0;
}
'''
    unit = tmp_path / "gateway-lease.c"
    unit.write_text(program)
    compile_host(tmp_path, unit)
