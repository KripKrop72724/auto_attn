"""Compile the actual OTA installer with faulting transport, flash and NVS ports."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_installer_persists_before_slot_selection_and_restores_bad_images(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "ota_manager.c").read_text()
    start = source.index("static bool perform_update(void)\n{")
    production = source[start:source.index("static void wait_for_zkt_safepoint(void)\n{", start)]
    harness = r'''
#include "ota_checkpoint.h"
#include <assert.h>
#include <stdio.h>
#define ESP_OK 0
#define ESP_ERR_HTTPS_OTA_IN_PROGRESS 1
#define OTA_RESUME_CHECKPOINT_BYTES (64*1024)
#define pdMS_TO_TICKS(x) (x)
typedef int esp_err_t;
typedef struct { unsigned size; } esp_partition_t;
typedef struct { char project_name[32],version[32]; } esp_app_desc_t;
typedef void *esp_https_ota_handle_t;
typedef struct {const char *url; void *crt_bundle_attach;int timeout_ms;bool keep_alive_enable;} esp_http_client_config_t;
typedef struct {esp_http_client_config_t *http_config;bool partial_http_download;unsigned max_http_request_size;bool ota_resumption;unsigned ota_image_bytes_written;} esp_https_ota_config_t;
static void *esp_crt_bundle_attach;
static ota_journal_t s_journal;
static char s_last_error[64];
static esp_partition_t target={OTA_APPLICATION_MAX_BYTES}, running={OTA_APPLICATION_MAX_BYTES};
static unsigned stage,saves,fail_save,performs,aborts,finishes,reboots,restores;
static bool handle_live,selected,boot_checkpoint;
static size_t strlcpy(char *out,const char *in,size_t n) {size_t len=strlen(in);if(n){size_t k=len<n-1?len:n-1;memcpy(out,in,k);out[k]=0;}return len;}
static const esp_partition_t *esp_ota_get_next_update_partition(const void *arg){(void)arg;return &target;}
static const esp_partition_t *esp_ota_get_running_partition(void){return &running;}
static int esp_ota_set_boot_partition(const esp_partition_t *p){assert(p==&running);++restores;if(stage==8)return -1;selected=false;return 0;}
static int esp_https_ota_begin(const esp_https_ota_config_t *c,esp_https_ota_handle_t *h)
{assert(c->http_config->url);if(stage==1)return -1;handle_live=true;*h=&target;return 0;}
static int esp_https_ota_get_img_desc(esp_https_ota_handle_t h,esp_app_desc_t *d)
{assert(h && handle_live);strcpy(d->project_name,"zone_lite");strcpy(d->version,"2.6.0");return stage==2?-1:0;}
static int esp_https_ota_perform(esp_https_ota_handle_t h)
{assert(h && handle_live);if(stage==3)return -1;return ++performs==1?ESP_ERR_HTTPS_OTA_IN_PROGRESS:ESP_OK;}
static int esp_https_ota_get_image_len_read(esp_https_ota_handle_t h){assert(h && handle_live);return 65536;}
static bool esp_https_ota_is_complete_data_received(esp_https_ota_handle_t h){assert(h && handle_live);return stage!=4;}
static int esp_https_ota_abort(esp_https_ota_handle_t h){assert(h && handle_live);++aborts;handle_live=false;return 0;}
static int esp_https_ota_finish(esp_https_ota_handle_t h)
{assert(h && handle_live && boot_checkpoint);++finishes;handle_live=false;if(stage==5)return -1;selected=true;return 0;}
static int esp_partition_get_sha256(const esp_partition_t *p,unsigned char digest[32])
{assert(p==&target);memset(digest,stage==7?0x22:0x11,32);return stage==6 || stage==8?-1:0;}
static void hex_bytes(const unsigned char *bytes,size_t n,char *out){memset(out,bytes[0]==0x11?'a':'b',2*n);out[2*n]=0;}
static bool save_journal(void)
{if(++saves==fail_save)return false;if(!strcmp(s_journal.state,"READY_TO_BOOT")){assert(s_journal.bytes_written==s_journal.image_size);boot_checkpoint=true;}return true;}
static bool report_state(const char *state,const char *error){(void)error;assert(state[0]);return true;}
static void vTaskDelay(unsigned ms){(void)ms;}
static void wait_for_zkt_safepoint(void){assert(selected && boot_checkpoint);}
static void esp_restart(void){assert(selected && boot_checkpoint);++reboots;}
/* PRODUCTION */
static void reset(void)
{
    saves=performs=aborts=finishes=reboots=restores=0;
    handle_live=selected=boot_checkpoint=false;
    memset(&s_journal,0,sizeof(s_journal));strcpy(s_journal.target_version,"2.6.0");
    strcpy(s_journal.state,"DOWNLOADING");strcpy(s_journal.download_url,"https://test.invalid/image");
    memset(s_journal.image_sha256,'a',64);s_journal.image_size=131072;
}
int main(void)
{
    reset();assert(perform_update() && reboots==1 && finishes==1 && !handle_live);
    for(stage=1;stage<=8;++stage){
        reset();assert(!perform_update() && !reboots && !handle_live);
        if(stage==6 || stage==7)assert(restores==1 && !selected);
        if(stage==8)assert(!strcmp(s_last_error,"BOOT_SELECTION_RECOVERY_FAILED"));
    }
    stage=0;
    for(fail_save=1;fail_save<=2;++fail_save){reset();assert(!perform_update());assert(!selected && !reboots && !finishes && aborts==1 && !handle_live);}
    puts("OTA installer ordering regression tests passed");
}
'''
    unit = tmp_path / "install.c"
    unit.write_text(harness.replace("/* PRODUCTION */", production))
    executable = tmp_path / "install"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(firmware),
        str(unit), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
