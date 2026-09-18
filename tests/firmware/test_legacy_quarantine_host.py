"""Run the production legacy evidence drain, including binary and partial rows."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_quarantine_requires_exact_durable_custody(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    drain = source[source.index("static legacy_queue_t g_legacy_quarantine[3];"):
                   source.index("static void ords_uploader_task(void *arg)")]
    program = r'''
#include "legacy_queue.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define STORAGE_BASE "."
#define CORRUPT_ORDS_PATH "./corrupt_ords.jsonl"
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
#define LED_STATUS_LOCAL_FAILURE 1
#define ADD_WORKER_NETWORK 1
#define ADD_WORKER_COMMITTING 2
static int lock;
static int *g_storage_lock=&lock;
static char buffer[DQ_MAX_RECORD_BYTES+1];
static char *g_blocked_drain_buffer=buffer;
static lq_checkpoint_t saved[3];
static bool fail_receipt, fail_commit;
static unsigned receipts, faults;
static size_t bytes;
static unsigned key_index(const char *key){return !strcmp(key,"old_qo")?0:!strcmp(key,"old_qa")?1:2;}
static int legacy_pending_load(void *context,lq_checkpoint_t *cp){*cp=saved[key_index(context)];return cp->version?1:0;}
static bool legacy_pending_commit(void *context,const lq_checkpoint_t *cp){assert(lock);if(fail_commit)return false;saved[key_index(context)]=*cp;return true;}
static int xSemaphoreTake(int *m,int timeout){(void)timeout;assert(!*m);*m=1;return 1;}
static void xSemaphoreGive(int *m){assert(*m);*m=0;}
static void led_status_fault(int status){assert(status==1);faults++;}
static bool add_connector_is_connected(void){return true;}
static void add_connector_report_ords_worker(int state){assert(state==1 || state==2);}
static bool qs_generation(char out[33]){memset(out,'a',32);out[32]=0;return true;}
static bool add_connector_transfer_queue_evidence(const char *name,const char *generation,const char *id,
 const void *data,size_t length,const char *serial,const char *reason){
 assert(!lock && name[0] && generation[0] && id[0] && !serial && !strcmp(reason,"LEGACY_RECOVERY"));
 assert(length && length<=DQ_MAX_RECORD_BYTES && data==buffer);receipts++;if(!fail_receipt)bytes+=length;return !fail_receipt;
}
''' + drain + r'''
static void create(const char *name,const void *data,size_t length){FILE *f=fopen(name,"wb");assert(f);assert(fwrite(data,1,length,f)==length);assert(!fclose(f));}
int main(void){
 create("./corrupt_ords.jsonl","bad\0raw\n",8);
 create("./add_corrupt.jsonl","partial",7);
 char large[9000];memset(large,'x',sizeof(large));create("./add_corrupt.bak",large,sizeof(large));
 fail_receipt=true;legacy_quarantine_slice();assert(receipts==1 && !saved[0].offset && access("./corrupt_ords.jsonl",F_OK)==0);
 fail_receipt=false;fail_commit=true;g_quarantine_lane=0;legacy_quarantine_slice();assert(receipts==2 && !saved[0].offset);
 fail_commit=false;g_legacy_quarantine[0].ready=false;g_quarantine_lane=0;legacy_quarantine_slice();assert(receipts==3 && access("./corrupt_ords.jsonl",F_OK)!=0);
 for(unsigned i=0;i<9;i++){legacy_quarantine_slice();memset(g_legacy_quarantine,0,sizeof(g_legacy_quarantine));}
 assert(access("./add_corrupt.jsonl",F_OK)!=0 && access("./add_corrupt.bak",F_OK)!=0);
 assert(bytes==8+8+7+9000 && faults==1 && !lock);
 return 0;
}
'''
    unit = tmp_path / "quarantine.c"
    unit.write_text(program)
    executable = tmp_path / "quarantine"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
                    str(firmware / "legacy_queue.c"), str(firmware / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
