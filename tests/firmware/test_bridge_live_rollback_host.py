"""Retained ADD authority must preserve punches while rollback reestablishes health."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FIXTURES = ROOT / "tests/firmware"


@pytest.mark.parametrize("bridge_version", ["2.6.20", "2.6.21"])
def test_valid_reader_captures_during_terminal_stability_window(tmp_path, bridge_version):
    for header in ("esp_app_desc.h", "esp_ota_ops.h", "esp_secure_boot.h", "esp_err.h", "sdkconfig.h"):
        (tmp_path / header).write_text('#include "zkt_reader_platform_host.h"\n')
    (tmp_path / "esp_timer.h").write_text('#include <stdint.h>\nint64_t esp_timer_get_time(void);\n')
    (tmp_path / "freertos").mkdir()
    (tmp_path / "freertos/FreeRTOS.h").write_text('''#pragma once
typedef void *SemaphoreHandle_t;
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
''')
    (tmp_path / "freertos/semphr.h").write_text('''#pragma once
#include "FreeRTOS.h"
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
void xSemaphoreGive(SemaphoreHandle_t);
''')
    (tmp_path / "cJSON.h").write_text('''#pragma once
typedef struct { int unused; } cJSON;
cJSON *cJSON_CreateObject(void);
cJSON *cJSON_AddBoolToObject(cJSON *,const char *,int);
cJSON *cJSON_AddStringToObject(cJSON *,const char *,const char *);
cJSON *cJSON_AddNumberToObject(cJSON *,const char *,double);
int cJSON_AddItemToObject(cJSON *,const char *,cJSON *);
void cJSON_Delete(cJSON *);
''')
    runtime = (FIXTURES / "zkt_journal_runtime_host.c").read_text()
    runtime = runtime[:runtime.index("int main(void)")]
    zone = (MAIN / "zone_lite.c").read_text()
    preservation = zone[zone.index("typedef enum { ZK_LIVE_HELD"):
                        zone.index("static bool zk_recv_data_stream(")]
    start = zone.index("                zk_live_path_t captured_path = zk_preserve_live_packet(packet, top.length);")
    live_acceptance = zone[start:zone.index("                // The session starts", start)]
    add = (MAIN / "add_connector.c").read_text()
    start = add.index("const char *add_connector_local_boot_health_error(void)")
    local_health = add[start:add.index("bool add_connector_boot_health_ready(void)", start)]
    support = r'''
#include <time.h>
#include <unistd.h>
#define ZONE_LITE_RECOVERY_STABILITY_MS 120000
#define ADD_WORKER_RESOURCE 9
static bool g_force_truth_reconcile,persist_ok=true;
static unsigned accepted,acks;
static FILE *durable;
static int64_t epoch_now(void){return 1800000000+(int64_t)clock_ms/1000;}
static int64_t uptime_ms(void){return clock_ms;}
static int64_t monotonic_ms(void){return clock_ms;}
static time_t synthetic_time(time_t *out){time_t value=epoch_now();if(out)*out=value;return value;}
#define time synthetic_time
static bool storage_upgrade_ready(void){return true;}
static SemaphoreHandle_t s_lock=(void*)1;
static bool s_outbox_task_handle=true,s_heartbeat_task_handle=true,s_outbox_buffer_ready=true;
static bool s_worker_start_failed,s_ords_worker_started=true;
static unsigned s_ords_worker_operation,s_ords_worker_tick_ms,s_outbox_tick_ms;
static struct {bool online;char connection_state[24];time_t stability_since_epoch;int user_count,attendance_count;}
    s_zkt={true,"RECOVERING",0,12,100};
/* The capture storage port supplies a durable receipt only after the exact
 * synthetic packet reaches disk. Production capture fault tests separately
 * exercise owner/journal persistence and interrupted writes. */
bool zj_capture_runtime_packet(const uint8_t *packet,size_t length,const zj_capture_facts_t *facts)
{
    assert(facts->wall_seconds==epoch_now());
    if(!capture_starts || !zj_runtime_writer_ready() || !persist_ok)return false;
    assert(fwrite(packet,1,length,durable)==length && !fflush(durable) && !fsync(fileno(durable)));
    ++accepted;return true;
}
static bool zk_send_ack_only(int sock,uint16_t session,int64_t deadline)
{assert(sock==1 && session==12 && deadline>0 && accepted==acks+1);++acks;return true;}
'''
    simulate = r'''
static bool live_packet(unsigned ordinal)
{
    uint8_t *packet=malloc(16);assert(packet);memset(packet,(int)ordinal,16);
    struct {size_t length;} top={16};struct {uint16_t session_id;} ctx={12};
    int sock=1;int64_t deadline=1000000;
    for(;;){
/* ACTUAL_LIVE_ACCEPTANCE */
        assert(captured_path==ZK_LIVE_JOURNAL);free(packet);return true;
    }
    return false;
}
static void boot(bool pending)
{
    reset();cutover=true;pending_bridge=pending;persist_ok=true;accepted=acks=0;
    g_force_truth_reconcile=false;strcpy(app.version,ZJ_BRIDGE_VERSION);
    for(unsigned i=0;i<5;++i)tick();
    s_zkt.online=true;strcpy(s_zkt.connection_state,"RECOVERING");
    s_zkt.stability_since_epoch=epoch_now();
    s_ords_worker_tick_ms=s_outbox_tick_ms=clock_ms;
}
int main(void)
{
    durable=tmpfile();assert(durable);
    /* Control for normal legacy→bridge first install: pending validation
     * already permits the legacy path after durable owner recovery. */
    reset();cutover=false;pending_bridge=true;strcpy(app.version,ZJ_BRIDGE_VERSION);
    for(unsigned i=0;i<5;++i)tick();
    assert(zj_runtime_legacy_capture_allowed() && zj_runtime_boot_ready());
    /* Old explicit-selection behavior made the retained bridge NEW. After
     * boot it is pending: valid local reader proof is not capture permission. */
    boot(true);
    assert(zj_runtime_boot_ready()&&!zj_runtime_writer_ready()&&!zj_runtime_legacy_capture_allowed());
    for(unsigned second=1;second<=180;++second){
        tick();s_ords_worker_tick_ms=s_outbox_tick_ms=clock_ms;
        if(second%10==0){assert(!live_packet(second));s_zkt.stability_since_epoch=epoch_now();}
        assert(!add_connector_local_boot_health_ready());
    }
    assert(!accepted&&!acks&&g_force_truth_reconcile&&ftell(durable)==0);
    /* Keeping the exact proved bridge VALID starts its ADD capture before
     * stability. Every accepted packet is durable; health is still withheld
     * throughout the unchanged two-minute terminal stability requirement. */
    boot(false);
    assert(zj_runtime_writer_ready()&&capture_starts==1&&!zj_runtime_legacy_capture_allowed());
    for(unsigned second=1;second<=180;++second){
        tick();s_ords_worker_tick_ms=s_outbox_tick_ms=clock_ms;
        if(second%10==0)assert(live_packet(second));
        assert(add_connector_local_boot_health_ready()==(second>=120));
    }
    assert(accepted==18&&acks==18&&ftell(durable)==18*16);
    rewind(durable);for(unsigned event=1;event<=18;++event)for(unsigned byte=0;byte<16;++byte)
        assert(fgetc(durable)==(int)(event*10));
    /* A healthy boot never authorizes an ACK for refused persistence. */
    persist_ok=false;assert(!live_packet(200)&&accepted==18&&acks==18&&g_force_truth_reconcile);
    owner.writer_allowed=false;tick();assert(!zj_runtime_writer_ready()&&!add_connector_local_boot_health_ready());
    fclose(durable);
    puts("retained ADD rollback, repeated punches, durable ACKs and unchanged terminal-health gates passed");
}
'''
    unit = tmp_path / "rollback-live.c"
    unit.write_text(runtime + support + preservation + local_health +
                    simulate.replace("/* ACTUAL_LIVE_ACCEPTANCE */", live_acceptance))
    binary = tmp_path / "rollback-live"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    f'-DZJ_BRIDGE_VERSION="{bridge_version}"', "-DCONFIG_NVS_ENCRYPTION=1", "-DZONE_LITE_JOURNAL_WRITES=1",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer", "-I", str(tmp_path), "-I", str(FIXTURES), "-I", str(MAIN),
                    str(unit), str(MAIN / "zkt_journal_boot.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)
