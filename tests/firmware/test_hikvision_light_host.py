"""Execute the actual light-audit state machine with power/storage/network faults."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_light_audit_durable_resume_backpressure_and_source_change(tmp_path):
    source = (ROOT / 'firmware/zone_lite/main/hikvision_runtime.c').read_text()
    pages = source[source.index('typedef struct {\n    unsigned char digest[32];'):source.index('static void poll_task(')]
    light = source[source.index('/* Low-rate independent audit.'):source.index('static void history_task(')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
typedef enum {HIK_OK,HIK_CONFIGURATION,HIK_NETWORK,HIK_AUTH,HIK_HTTP_STATUS,HIK_OVERSIZED,HIK_BINDING,HIK_PARSE,HIK_CUSTODY} hik_result_t;
typedef struct {uint32_t serial;unsigned char binding[32];} poll_checkpoint_t;
static atomic_bool reachable=true;
static atomic_uint poll_cursor=45;
static int64_t tick=60000000;
static bool connected=true,fail_write,fail_queue,corrupt_anchor;
static uint32_t depth, writes, checkpoints, last;
static unsigned char saved[1024];static size_t saved_size;
static int64_t esp_timer_get_time(void){return tick;}
static time_t fake_time(time_t *out){(void)out;return 1790000000+tick/1000000;}
#define time fake_time
#define NVS_READWRITE 1
#define NVS_READONLY 0
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 2
#define QS_HIK_SOURCE 7
#define ESP_LOGI(...) ((void)0)
typedef int nvs_handle_t;typedef int esp_err_t;
static int nvs_open(const char *n,int mode,int *h){(void)n;(void)mode;*h=1;return 0;}
static int nvs_set_blob(int h,const char*k,const void*p,size_t n){(void)h;(void)k;if(fail_write)return -1;memcpy(saved,p,n);saved_size=n;++checkpoints;return 0;}
static int nvs_commit(int h){(void)h;return 0;}
static void nvs_close(int h){(void)h;}
static int nvs_get_blob(int h,const char*k,void*p,size_t*n){(void)h;(void)k;if(!saved_size)return 2;assert(*n>=saved_size);memcpy(p,saved,saved_size);*n=saved_size;return 0;}
static bool checkpoint_load(poll_checkpoint_t*p,bool*present){memset(p,0,sizeof(*p));memset(p->binding,5,32);*present=true;return true;}
static bool add_connector_is_connected(void){return connected;}
static bool qs_snapshot(int lane,uint32_t*n){assert(lane==7);*n=depth;return true;}
static const char*hik_reason(hik_result_t r){(void)r;return "reason";}
static bool add_connector_log(const char*a,const char*b,const char*c,const char*d){(void)a;(void)b;(void)c;assert(d);return true;}
static void mbedtls_sha256(const unsigned char*p,size_t n,unsigned char*out,int mode){(void)mode;memset(out,0,32);for(size_t i=0;i<n;i++)out[i%32]^=p[i];}
static bool preserve(const char*channel,const char*body,size_t n){assert(!strcmp(channel,"HISTORY"));assert(body&&n);if(fail_queue)return false;++writes;return true;}
typedef struct {uint32_t first_serial,last_serial,previous_serial;bool complete;} hik_search_t;
static void hik_search_init(hik_search_t*s,uint32_t first,uint32_t end){*s=(hik_search_t){.first_serial=first,.last_serial=end};}
static hik_result_t hik_history_bounds(uint32_t*f,uint32_t*l,uint32_t*c){*f=1;*l=45;*c=45;return HIK_OK;}
static hik_result_t hik_history_page(hik_search_t*s,bool(*fn)(void*,const char*,size_t),void*ctx){
 uint32_t end=s->first_serial+19;if(end>s->last_serial)end=s->last_serial;
 for(uint32_t i=s->first_serial;i<=end;i++){char b[32];snprintf(b,sizeof(b),"row-%u%s",i,corrupt_anchor?"changed":"");if(!fn(ctx,b,strlen(b)))return HIK_CUSTODY;s->previous_serial=i;}
 s->complete=end==s->last_serial;last=end;return HIK_OK;
}
/* PRODUCTION */
static void step(void){tick+=31000000;light_step();}
int main(void){
 /* Pin the upper bound before scanning; one bounded page per invocation. */
 step();assert(light_checkpoint.first==1&&light_checkpoint.last==45&&!writes);
 step();assert(writes==20&&light_checkpoint.cursor==20&&light_checkpoint.scanned==20);
 /* Backpressure and unavailable terminal cannot advance durable coverage. */
 depth=40;step();assert(writes==20&&light_state==LIGHT_DELIVERY);depth=0;
 connected=false;step();assert(writes==20);connected=true;
 reachable=false;step();assert(light_state==LIGHT_TERMINAL&&writes==20);reachable=true;
 /* Reboot restores the cursor, then verifies the inclusive saved anchor. */
 light_loaded=false;memset(&light_checkpoint,0,sizeof(light_checkpoint));step();
 assert(light_checkpoint.cursor==39&&writes==39&&last==39);
 /* Failed queue custody cannot commit a checkpoint. */
 unsigned before=checkpoints;fail_queue=true;step();assert(checkpoints==before&&light_checkpoint.cursor==39);fail_queue=false;
 /* A source mutation cannot be silently accepted as recovered history. */
 corrupt_anchor=true;step();assert(light_state==LIGHT_BLOCKED&&checkpoints==before);corrupt_anchor=false;
 /* An NVS failure replays durable observations; it never advances in RAM. */
 fail_write=true;step();assert(light_checkpoint.cursor==39&&checkpoints==before);fail_write=false;
 step();assert(light_state==LIGHT_COMPLETE&&light_checkpoint.completed_epoch&&light_checkpoint.last==0);
 assert(light_checkpoint.scanned==45&&poll_cursor==45);
 /* Reboot within the six-hour policy must not start another full sweep. */
 before=checkpoints;light_loaded=false;step();assert(checkpoints==before&&light_state==LIGHT_COMPLETE);
 tick+=21601000000LL;step();assert(light_checkpoint.last==45&&light_checkpoint.cursor==0);
 return 0;
}
'''
    unit=tmp_path/'light.c'
    unit.write_text(harness.replace('/* PRODUCTION */',pages+'\n'+light))
    exe=tmp_path/'light'
    subprocess.run([shutil.which('cc'),'-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',str(unit),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
