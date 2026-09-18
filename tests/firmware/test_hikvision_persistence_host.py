from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_idle_boot_proves_both_stores_without_clearing_existing_fault(tmp_path):
    main = ROOT / 'firmware/zone_lite/main'
    source = (main / 'queue_store.c').read_text()
    start = source.index('bool qs_verify_persistence(void)')
    body = source[start:source.index('dq_result_t qs_append(', start)]
    body = body.replace('"/storage/persistence-probe"', '"' + str(tmp_path / 'probe') + '"')
    harness = r'''
#include "queue_store.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define ESP_OK 0
#define NVS_READWRITE 1
#define NVS_READONLY 0
#define SB_RECOVERY 2
typedef int nvs_handle_t;
static qs_health_t health;
static int budget, budget_lock=1, fail_fs, fail_nvs, corrupt_nvs, writes;
static unsigned char stored[32];
static bool storage_upgrade_ready(void){return true;}
static int xSemaphoreTake(int a,int b){(void)a;(void)b;return 1;}
static void xSemaphoreGive(int a){(void)a;}
static bool measure(void){health.available=true;return true;}
static bool storage_budget_admit(int *b,size_t t,size_t u,size_t n,int c){(void)b;(void)t;(void)u;(void)n;(void)c;return true;}
static void esp_fill_random(void *p,size_t n){memset(p,123,n);}
static int probe_fsync(int fd){return fail_fs?-1:fsync(fd);}
#define fsync probe_fsync
static int nvs_open(const char *n,int mode,nvs_handle_t *h){(void)n;(void)mode;*h=1;return 0;}
static int nvs_set_blob(int h,const char *key,const void *p,size_t n){(void)h;assert(!strcmp(key,"write_proof"));assert(n==32);writes++;memcpy(stored,p,n);return 0;}
static int nvs_commit(int h){(void)h;return fail_nvs?-1:0;}
static void nvs_close(int h){(void)h;}
static int nvs_get_blob(int h,const char *key,void *p,size_t *n){(void)h;(void)key;assert(*n==32);memcpy(p,stored,32);if(corrupt_nvs)((char*)p)[0]^=1;return 0;}
static void record_queue_result(dq_result_t r,const char *op,bool writing){assert(r==DQ_IO && writing);health.last_error=5;health.last_operation=op;}
/* PRODUCTION */
int main(void){
 assert(!qs_verify_persistence());assert(writes==0);
 health.recovery_complete=true;assert(qs_verify_persistence());assert(health.persistence_verified && writes==1);
 assert(qs_verify_persistence() && writes==1); /* no recurring flash wear */
 health=(qs_health_t){.recovery_complete=true};fail_fs=1;
 assert(!qs_verify_persistence() && !health.persistence_verified && health.last_error);assert(writes==1);
 fail_fs=0;assert(!qs_verify_persistence());assert(writes==1); /* latched fault not hidden */
 health=(qs_health_t){.recovery_complete=true};fail_nvs=1;
 assert(!qs_verify_persistence() && !health.persistence_verified && health.last_error);
 health=(qs_health_t){.recovery_complete=true};fail_nvs=0;corrupt_nvs=1;
 assert(!qs_verify_persistence() && !health.persistence_verified && health.last_error);
 return 0;
}
'''
    unit = tmp_path / 'proof.c'
    unit.write_text(harness.replace('/* PRODUCTION */', body))
    exe = tmp_path / 'proof'
    subprocess.run([shutil.which('cc'), '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-I', str(main), str(unit), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
