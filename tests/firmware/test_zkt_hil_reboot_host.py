"""Run the actual bounded reboot controller, RTC witness and inbox admission."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def run(tmp_path, source, name="reboot", extra=()):
    (tmp_path / "esp_attr.h").write_text("#define RTC_NOINIT_ATTR\n")
    path = tmp_path / f"{name}.c"
    path.write_text(source)
    binary = tmp_path / name
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(tmp_path), "-I", str(MAIN),
        str(path), *map(str, extra), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)


def test_rtc_witness_never_promotes_an_intent_or_an_unrelated_reset(tmp_path):
    run(tmp_path, r'''
#include <assert.h>
#include <string.h>
#include "zkt_hil_reboot.c"
int main(void) {
    zhr_attempt_t a = {.binding={.run_id="11111111-2222-4333-8444-555555555555",
        .boot_id="a4cb8fd46664-12345678",
        .application_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        .expires_at=1800000060}, .command_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", .terminal_serial="TERMINAL-1"};
    zhr_checkpoint_t cp;
    assert(zhr_valid(&a));
    assert(zhr_deadline(&a,a.binding.boot_id,a.binding.application_sha256,a.terminal_serial,1800000000,1000));
    assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    /* A durable command/processed-ID intent alone does not arm RTC evidence. */
    assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    zhr_witness_arm(&a,1800000001,1001000);
    assert(zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    assert(cp.epoch==1800000001 && cp.uptime_ms==1001);
    assert(!zhr_witness_recovered(&a,a.binding.boot_id,a.binding.application_sha256,true,&cp));
    assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,false,&cp));
    assert(!zhr_witness_recovered(&a,"boot-new","wrong-image",true,&cp));
    a.command_id[0]='b';assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));a.command_id[0]='a';
    a.binding.run_id[0]='2';assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));a.binding.run_id[0]='1';
    a.terminal_serial[0]='X';assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));a.terminal_serial[0]='T';
    witness.crc^=1;assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));witness.crc^=1;
    zhr_witness_cancel("another-command");assert(zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    zhr_witness_cancel(a.command_id);assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    zhr_witness_arm(&a,1800000001,1001000);
    zhr_witness_clear();assert(!zhr_witness_recovered(&a,"boot-new",a.binding.application_sha256,true,&cp));
    zhr_witness_arm(&a,1800000060,1001000);assert(!witness.magic);
    zhr_witness_arm(&a,1800000001,a.deadline_us);assert(!witness.magic);
    assert(!zhr_deadline(&a,a.binding.boot_id,a.binding.application_sha256,a.terminal_serial,1799999999,1000));
    assert(!zhr_deadline(&a,"another-boot",a.binding.application_sha256,a.terminal_serial,1800000000,1000));
    assert(!zhr_deadline(&a,a.binding.boot_id,a.binding.application_sha256,a.terminal_serial,1800000000,UINT64_MAX-1));
    assert(!zhr_before_deadline(&a,1800000060,1000));
    a.binding.application_sha256[0]='A';assert(!zhr_valid(&a));
    memset(a.binding.boot_id,'X',sizeof(a.binding.boot_id));assert(!zhr_valid(&a));
}
''')


def test_actual_gateway_reboot_is_durable_bounded_and_at_most_once(tmp_path):
    runtime = (MAIN / "zone_lite.c").read_text()
    start = runtime.index("/* Only the terminal gateway owns this pending action.")
    helpers = runtime[start:runtime.index("\n#endif\n\nstatic bool process_add_commands", start)]
    start = runtime.index("static int64_t gateway_run(uint32_t host_order_ip)\n{")
    wrapper = runtime[start:runtime.index("\nstatic int64_t gateway_run_session(uint32_t host_order_ip)\n{", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "add_connector.h"
#define pdMS_TO_TICKS(x) (x)
#define ESP_RST_SW 3
typedef enum {REL_ID_ERROR=-1,REL_ID_ABSENT,REL_ID_PRESENT} rel_id_result_t;
static rel_id_result_t processed,cancelled;
static bool capable=true,lease_known=true,g_temp_admin_active,writer_ready=true,reserved,session_active,closed;
static bool persist_ok=true,ack=true,late_commit,busy_gate,portal;
static unsigned resets,gates,reports,releases,completions,retries,terminal_restarts;
static uint64_t uptime=1000000;
static int64_t epoch=1800000000;
static int reason=1;
static char boot[48]="a4cb8fd46664-12345678", image[65]="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static char g_device_serial[80]="TERMINAL-1", report[1024];
static add_command_t command={.command_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",.command_type="ESP_REBOOT",
    .expected_serial="TERMINAL-1",.reboot={.run_id="11111111-2222-4333-8444-555555555555",
    .boot_id="a4cb8fd46664-12345678",.application_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",.expires_at=1800000060}};
#define strlcpy copy_text
static size_t copy_text(char *out,const char *in,size_t size){assert(strlen(in)<size);strcpy(out,in);return strlen(in);}
static int64_t epoch_now(void){return epoch;}
static int64_t esp_timer_get_time(void){return (int64_t)uptime;}
static int esp_reset_reason(void){return reason;}
const char *add_connector_boot_id(void){return boot;}
static bool ota_manager_running_image(char out[65]){memcpy(out,image,65);return true;}
static bool ota_manager_hil_reboot_capable(void){return capable;}
static bool ota_manager_hil_reboot_reserve(void){assert(!reserved);reserved=true;return true;}
static void ota_manager_hil_reboot_release(void){assert(reserved);reserved=false;++releases;}
static bool temp_admin_evidence_ready(void){return lease_known;}
static bool zj_runtime_writer_ready(void){return writer_ready;}
static bool setup_portal_active(void){return portal;}
static rel_id_result_t command_was_processed(const char *id){assert(!strcmp(id,command.command_id));return processed;}
static rel_id_result_t command_was_cancelled(const char *id){assert(!strcmp(id,command.command_id));return cancelled;}
static bool mark_command_processed(const char *id){assert(!strcmp(id,command.command_id)&&session_active);if(persist_ok)processed=REL_ID_PRESENT;if(late_commit){epoch+=61;uptime+=61000000;}return persist_ok;}
bool add_connector_command_update_acknowledged(const char *id,const char *json){assert(!strcmp(id,command.command_id));++reports;strcpy(report,json);return ack;}
bool add_connector_command_update(const char *id,const char *status,const char *code,const char *message,const char *json)
{assert(!strcmp(id,command.command_id)&&strcmp(status,"SUCCEEDED"));(void)code;(void)message;(void)json;return true;}
bool add_connector_command_complete(const char *id){assert(!strcmp(id,command.command_id));++completions;return true;}
void add_connector_command_retry(const char *id){assert(!strcmp(id,command.command_id));++retries;}
void add_connector_set_activity(const char *activity){assert(!strcmp(activity,"ONLINE"));}
static bool zj_owner_try_quiesce_before(uint64_t deadline,int64_t expires,int64_t *e,uint64_t *u)
{assert(!session_active&&closed&&reserved&&processed==REL_ID_PRESENT&&ack);++gates;if(busy_gate)return false;assert(uptime<deadline&&epoch<expires);*e=epoch;*u=uptime;return true;}
static void esp_restart(void){assert(!session_active&&closed&&reserved&&gates);++resets;}
static void vTaskDelay(unsigned ms){assert(ms==20);uptime+=(uint64_t)ms*1000;if(uptime>=61000000)epoch=1800000060;}
bool add_connector_terminal_session_begin(void){assert(!session_active);session_active=true;closed=false;return true;}
bool add_connector_terminal_session_end(void){assert(session_active&&closed);session_active=false;return true;}
static int64_t gateway_run_session(uint32_t ip);
/* PRODUCTION */
static int64_t gateway_run_session(uint32_t ip){assert(ip==7&&session_active);process_hil_reboot(&command);closed=true;return 1;}
static void reset(void){g_hil_reboot_pending=false;reserved=false;session_active=false;closed=false;processed=REL_ID_ABSENT;cancelled=REL_ID_ABSENT;epoch=1800000000;uptime=1000000;resets=gates=reports=releases=completions=retries=0;late_commit=busy_gate=false;persist_ok=ack=capable=lease_known=writer_ready=true;g_temp_admin_active=portal=false;reason=1;strcpy(boot,command.reboot.boot_id);zhr_witness_clear();report[0]=0;}
int main(void){
    reset();capable=false;gateway_run(7);assert(!resets&&!gates&&completions==1);
    reset();g_temp_admin_active=true;gateway_run(7);assert(!resets&&!gates&&retries==1);
    reset();lease_known=false;gateway_run(7);assert(!resets&&!gates);
    reset();persist_ok=false;gateway_run(7);assert(!resets&&!gates&&releases==1);
    reset();late_commit=true;gateway_run(7);assert(processed==REL_ID_PRESENT&&!resets&&!gates&&releases==1);
    reset();ack=false;gateway_run(7);assert(processed==REL_ID_PRESENT&&!resets&&!gates&&releases==1);
    reset();busy_gate=true;gateway_run(7);assert(processed==REL_ID_PRESENT&&!resets&&gates&&releases==1&&!g_hil_reboot_pending);
    reset();gateway_run(7);assert(resets==1&&gates==1&&!terminal_restarts&&strstr(report,"INTENT_PERSISTED"));
    /* Original-boot duplicate consumes the attempt without claiming success. */
    g_hil_reboot_pending=false;reserved=false;gateway_run(7);assert(resets==1&&strstr(report,"NOT_OBSERVED"));
    /* New boot without an RTC witness is an unrelated reset, never recovery. */
    strcpy(boot,"new-boot");reason=3;gateway_run(7);assert(resets==1&&strstr(report,"NOT_OBSERVED"));
    reset();gateway_run(7);g_hil_reboot_pending=false;reserved=false;strcpy(boot,"new-boot");reason=3;
    ack=false;gateway_run(7);assert(strstr(report,"\"safe_checkpoint\":true")&&strstr(report,"RECOVERED"));
    ack=true;gateway_run(7);assert(strstr(report,"RECOVERED")&&resets==1);
    gateway_run(7);assert(strstr(report,"NOT_OBSERVED")&&resets==1);
    reset();processed=REL_ID_PRESENT;strcpy(boot,"new-boot");reason=3;gateway_run(7);assert(!resets&&strstr(report,"NOT_OBSERVED"));
}
'''
    run(tmp_path, harness.replace("/* PRODUCTION */", helpers + wrapper),
        extra=[MAIN / "zkt_hil_reboot.c"])


def test_actual_ota_reservation_is_nonblocking_and_signed_writer_only(tmp_path):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("bool ota_manager_busy(void)\n{")
    actual = source[start:source.index("void ota_manager_append_telemetry(", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <string.h>
#define ZONE_LITE_JOURNAL_WRITER_IMAGE 1
#define CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE 1
typedef struct {char project_name[32],version[32];} esp_app_desc_t;
static esp_app_desc_t app={"zone_lite","2.7.0"};
static bool secure=true,portal,s_busy;
static atomic_flag s_control_owner=ATOMIC_FLAG_INIT;
static atomic_bool s_hil_reboot_reserved;
static char s_running_image_digest[65]="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const esp_app_desc_t *esp_app_get_description(void){return &app;}
static bool esp_secure_boot_enabled(void){return secure;}
static bool setup_portal_active(void){return portal;}
/* PRODUCTION */
int main(void){
 assert(ota_manager_hil_reboot_capable());
 strcpy(app.version,"2.6.19");assert(!ota_manager_hil_reboot_capable()&&!ota_manager_hil_reboot_reserve());
 strcpy(app.version,"2.6.20");assert(!ota_manager_hil_reboot_capable()&&!ota_manager_hil_reboot_reserve());
 strcpy(app.version,"2.6.21");assert(!ota_manager_hil_reboot_capable()&&!ota_manager_hil_reboot_reserve());
 strcpy(app.version,"2.6.23");assert(!ota_manager_hil_reboot_capable()&&!ota_manager_hil_reboot_reserve());
 strcpy(app.version,"2.7.0");secure=false;assert(!ota_manager_hil_reboot_capable());secure=true;
 strcpy(app.project_name,"other");assert(!ota_manager_hil_reboot_capable());strcpy(app.project_name,"zone_lite");
 assert(!ota_manager_busy()&&ota_manager_hil_reboot_reserve()&&ota_manager_busy());
 assert(!ota_manager_hil_reboot_reserve());
 assert(atomic_flag_test_and_set(&s_control_owner));
 ota_manager_hil_reboot_release();assert(!ota_manager_busy());
 assert(!atomic_flag_test_and_set(&s_control_owner));assert(!ota_manager_hil_reboot_reserve());
 assert(atomic_flag_test_and_set(&s_control_owner));atomic_flag_clear(&s_control_owner);
 s_busy=true;assert(!ota_manager_hil_reboot_reserve());s_busy=false;
 portal=true;assert(!ota_manager_hil_reboot_reserve());portal=false;
 assert(ota_manager_hil_reboot_reserve());ota_manager_hil_reboot_release();
 char image[65];assert(ota_manager_running_image(image)&&!strcmp(image,s_running_image_digest));
}
'''
    run(tmp_path, harness.replace("/* PRODUCTION */", actual), "reservation")
