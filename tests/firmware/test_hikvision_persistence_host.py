from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('hikvision', [0, 1])
def test_idle_boot_proves_both_stores_without_clearing_existing_fault(tmp_path, hikvision):
    main = ROOT / 'firmware/zone_lite/main'
    source = (main / 'queue_store.c').read_text()
    start = source.index('bool qs_verify_persistence(void)')
    body = source[start:source.index('dq_result_t qs_append(', start)]
    body = body.replace('"/storage/persistence-probe"', '"' + str(tmp_path / 'probe') + '"')
    harness = r'''
#include "queue_store.h"
#include <assert.h>
#include <errno.h>
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
#if ZONE_LITE_HIKVISION
static void record_queue_result(dq_result_t r,const char *op,bool writing){assert(r==DQ_IO && writing);health.last_error=5;health.last_operation=op;}
#else
static int64_t now_us;
static int64_t esp_timer_get_time(void){return now_us;}
#endif
/* PRODUCTION */
static bool attempt_probe(void){
#if !ZONE_LITE_HIKVISION
 now_us+=61000000;
#endif
 return qs_verify_persistence();
}
int main(void){
 assert(!attempt_probe());assert(writes==0);
 health.recovery_complete=true;assert(attempt_probe());assert(health.persistence_verified && writes==1);
 assert(attempt_probe() && writes==1); /* no recurring flash wear */
 health.legacy.error=EIO;
 assert(!attempt_probe() && writes==1); /* No global probe clears a scoped legacy incident. */
 health.legacy.error=0;
 health.persistence_recheck_required=true;
 assert(attempt_probe() && writes==2 && !health.persistence_recheck_required);
 assert(attempt_probe() && writes==2); /* One fresh full proof, then no recurring writes. */
 writes=1; /* Retain the independent fault-matrix counter baseline below. */
 health=(qs_health_t){.recovery_complete=true};fail_fs=1;
 assert(!attempt_probe() && !health.persistence_verified && !health.last_error);
 assert(health.persistence_probe_failures==1 && writes==1);
 fail_fs=0;assert(attempt_probe() && health.persistence_verified && !health.last_error);
 assert(health.persistence_probe_failures==0 && writes==2); /* transient retry proves both stores */
 health=(qs_health_t){.recovery_complete=true};fail_fs=1;
 for(int i=0;i<3;i++)assert(!attempt_probe());
#if ZONE_LITE_HIKVISION
 assert(!health.persistence_verified && health.last_error);
 assert(!strcmp(health.last_operation,"persistence_sync"));
 fail_fs=0;assert(!attempt_probe()); /* Hikvision's existing policy is unchanged. */
#else
 assert(!health.persistence_verified && !health.last_error && health.persistence_probe_error==EIO);
 assert(!strcmp(health.persistence_probe_operation,"persistence_sync"));
 assert(health.persistence_probe_total_failures==3);
 fail_fs=0;
 assert(!qs_verify_persistence()); /* Retry deadline avoids recurring flash writes. */
 assert(writes==2);
 assert(attempt_probe() && health.persistence_verified && !health.persistence_probe_error);
 assert(health.persistence_probe_total_failures==3 && !health.persistence_probe_failures);
#endif
 health=(qs_health_t){.recovery_complete=true};fail_nvs=1;
 for(int i=0;i<3;i++)assert(!attempt_probe());
#if ZONE_LITE_HIKVISION
 assert(!health.persistence_verified && health.last_error);
 assert(!strcmp(health.last_operation,"persistence_nvs_commit"));
#else
 assert(!health.persistence_verified && health.persistence_probe_error==-1 && !health.last_error);
 assert(!strcmp(health.persistence_probe_operation,"persistence_nvs_commit"));
 health.last_error=EBADMSG;health.last_operation="segment_verify";
 int before=writes;fail_nvs=0;
 assert(!attempt_probe() && writes==before); /* An unrelated incident is not cleared. */
 assert(health.last_error==EBADMSG && health.persistence_probe_error==-1);
#endif
 health=(qs_health_t){.recovery_complete=true};fail_nvs=0;corrupt_nvs=1;
 for(int i=0;i<3;i++)assert(!attempt_probe());
#if ZONE_LITE_HIKVISION
 assert(!health.persistence_verified && health.last_error);
 assert(!strcmp(health.last_operation,"persistence_nvs_read"));
#else
 assert(!health.persistence_verified && health.persistence_probe_error==EIO && !health.last_error);
 assert(!strcmp(health.persistence_probe_operation,"persistence_nvs_read"));
 corrupt_nvs=0;health.persistence_verified=true; /* Unrelated append cannot substitute for a probe. */
 int previous=writes;assert(attempt_probe() && writes==previous+1);
 assert(!health.persistence_probe_error && !health.persistence_probe_operation);
#endif
 return 0;
}
'''
    unit = tmp_path / 'proof.c'
    unit.write_text(harness.replace('/* PRODUCTION */', body))
    exe = tmp_path / 'proof'
    subprocess.run([shutil.which('cc'), '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', f'-DZONE_LITE_HIKVISION={hikvision}',
                    '-I', str(main), str(unit), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
