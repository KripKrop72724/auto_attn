"""Execute HIK's restart gate against busy terminal and custody workers."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_restart_claim_quiesces_hik_workers_without_zkt_state(tmp_path):
    firmware = ROOT / 'firmware/zone_lite/main'
    runtime = (firmware / 'hikvision_runtime.c').read_text()
    start = runtime.index('bool hikvision_claim_ota_restart(void)')
    claim = runtime[start:runtime.index('/* OTA proves', start)]
    connector = (firmware / 'add_connector.c').read_text()
    start = connector.index('bool add_connector_claim_ota_restart(void)')
    gate = connector[start:connector.index('bool add_connector_begin_pending_command_activity', start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <string.h>
#define ZONE_LITE_HIKVISION 1
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef int *SemaphoreHandle_t;
static int request, source, connector;
static SemaphoreHandle_t request_lock=&request,source_upload_lock=&source,s_lock=&connector;
static atomic_bool checkpoint_ready=true,runtime_workers_started=true;
static bool s_ota_restart_claimed;
static char s_activity[32]="HIKVISION_POLL_2S";
typedef struct {bool observed,available,recovery_complete,persistence_verified;int last_error;} qs_health_t;
static qs_health_t health={true,true,true,true,0};
static qs_health_t qs_health(void){return health;}
static int xSemaphoreTake(int *lock,int timeout){(void)timeout;if(*lock)return 0;*lock=1;return 1;}
static void xSemaphoreGive(int *lock){assert(*lock);*lock=0;}
#define strlcpy test_strlcpy
static void test_strlcpy(char *dst,const char *src,size_t size){assert(strlen(src)<size);strcpy(dst,src);}
/* PRODUCTION */
int main(void){
 checkpoint_ready=false;assert(!add_connector_claim_ota_restart());checkpoint_ready=true;
 runtime_workers_started=false;assert(!add_connector_claim_ota_restart());runtime_workers_started=true;
 request=1;assert(!add_connector_claim_ota_restart()&&!source&&!connector);request=0;
 source=1;assert(!add_connector_claim_ota_restart()&&!request&&!connector);source=0;
 health.persistence_verified=false;assert(!add_connector_claim_ota_restart()&&!request&&!source);health.persistence_verified=true;
 health.last_error=1;assert(!add_connector_claim_ota_restart()&&!request&&!source);health.last_error=0;
 assert(add_connector_claim_ota_restart());
 assert(request&&source&&!connector&&s_ota_restart_claimed&&!strcmp(s_activity,"OTA_RESTART"));
 assert(!add_connector_claim_ota_restart()&&request&&source&&!connector);
 return 0;
}
'''
    unit = tmp_path / 'restart.c'
    unit.write_text(harness.replace('/* PRODUCTION */', claim + gate))
    executable = tmp_path / 'restart'
    subprocess.run([shutil.which('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', str(unit), '-o', str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
    uploader = runtime[runtime.index('static void source_uploader'):runtime.index('void hikvision_gateway_task')]
    assert uploader.index('xSemaphoreTake(source_upload_lock') < uploader.index('qs_peek(')
    assert uploader.index('qs_settle(') < uploader.index('xSemaphoreGive(source_upload_lock)')
