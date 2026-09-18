"""Run the production OTA journal with failing NVS and interrupted commits."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_ota_journal_commits_fail_closed_and_legacy_upgrade(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "ota_manager.c").read_text()
    production = source[source.index("static bool journal_failure("):source.index("static bool signed_request(")]
    program = r'''
#include "ota_checkpoint.h"
#include <assert.h>
#include <stdio.h>
#define OTA_NAMESPACE "zone_ota"
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READWRITE 1
#define NVS_READONLY 0
typedef int nvs_handle_t;
typedef int esp_err_t;
static ota_journal_t s_journal,s_committed_journal,legacy;
static ota_checkpoint_t pending,durable;
static uint32_t s_journal_generation;
static bool s_journal_ready,exists,legacy_exists;
static char s_last_error[64];
static unsigned failure,writes;
static size_t strlcpy(char *out,const char *in,size_t n) {size_t len=strlen(in);if(n){size_t k=len<n-1?len:n-1;memcpy(out,in,k);out[k]=0;}return len;}
static int nvs_open(const char *name,int mode,int *handle)
{assert(!strcmp(name,OTA_NAMESPACE));(void)mode;*handle=1;return failure==1?-1:0;}
static int nvs_set_blob(int handle,const char *key,const void *data,size_t size)
{assert(handle==1 && !strcmp(key,"journal_v1") && size==sizeof(pending));++writes;pending=*(const ota_checkpoint_t *)data;return failure==2?-1:0;}
static int nvs_commit(int handle)
{assert(handle==1);if(failure==3)return -1;durable=pending;exists=true;return failure==4?-1:0;}
static int nvs_get_blob(int handle,const char *key,void *data,size_t *size)
{
    assert(handle==1);if(failure==5)return -1;
    if(!strcmp(key,"journal_v1")){if(!exists)return ESP_ERR_NVS_NOT_FOUND;assert(*size>=sizeof(durable));memcpy(data,&durable,sizeof(durable));*size=sizeof(durable);}
    else{assert(!strcmp(key,"journal"));if(!legacy_exists)return ESP_ERR_NVS_NOT_FOUND;assert(*size>=sizeof(legacy));memcpy(data,&legacy,sizeof(legacy));*size=sizeof(legacy);}
    return 0;
}
static void nvs_close(int handle){assert(handle==1);}
''' + production + r'''
static ota_journal_t download(void)
{
    ota_journal_t j={0};strcpy(j.deployment_id,"deployment");strcpy(j.release_id,"release");
    strcpy(j.target_version,"2.5.4");memset(j.image_sha256,'a',64);
    strcpy(j.download_url,"https://example.invalid/firmware");strcpy(j.state,"DOWNLOADING");
    j.image_size=1024;j.bytes_written=256;return j;
}
int main(void)
{
    assert(load_journal() && !strcmp(s_journal.state,"IDLE"));
    legacy=download();legacy_exists=true;
    assert(load_journal() && s_journal.bytes_written==256);
    assert(save_journal() && ota_checkpoint_valid(&durable));
    assert(s_journal_generation==1);
    for(failure=1;failure<=4;++failure){
        s_journal.bytes_written=512;
        assert(!save_journal() && !s_journal_ready && s_journal.bytes_written==256);
        unsigned prior=failure;failure=0;
        assert(load_journal());
        assert(s_journal.bytes_written==(prior==4?512:256));
        s_journal=download();assert(save_journal());failure=prior;
    }
    failure=0;
    assert(clear_journal() && !strcmp(s_journal.state,"IDLE"));
    failure=3;s_journal=download();assert(!save_journal());
    assert(!strcmp(s_journal.state,"IDLE"));
    failure=0;assert(load_journal());s_journal=download();assert(save_journal());
    failure=3;assert(!clear_journal() && !strcmp(s_journal.state,"DOWNLOADING"));
    failure=0;assert(load_journal());
    // Corrupt v1 cannot resurrect even a valid legacy checkpoint.
    durable.crc^=1;assert(!load_journal() && !s_journal_ready);
    durable.crc^=1;failure=5;assert(!load_journal());failure=0;assert(load_journal());
    unsigned prior=writes;s_journal_generation=UINT32_MAX;assert(!save_journal() && writes==prior);
    assert(load_journal());s_journal.image_size=0;assert(!save_journal() && writes==prior);
    puts("OTA persistence regression tests passed");
}
'''
    unit = tmp_path / "ota.c"
    unit.write_text(program)
    executable = tmp_path / "ota"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "durable_queue.c"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
