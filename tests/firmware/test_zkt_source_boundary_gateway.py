"""Execute the gateway's real bounded boundary step with transport faults."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_boundary_uses_consistent_released_terminal_snapshot(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "zone_lite.c").read_text()
    start = source.index("static void zkt_preserve_first_source_boundary(int sock, zk_context_t *ctx)")
    function = source[start:source.index("\n#else", start)]
    program = r'''
#include "zkt_source_boundary_client.h"
#include "zkt_source_schedule.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#define CMD_ATTLOG_RRQ 13
typedef struct {int session_id;} zk_context_t;
typedef struct {bool prepared; uint32_t size;} zk_bounded_buffer_t;
static unsigned scenario, reads, creates, prepared, released, counts, activity;
static uint64_t now=100000;
static bool stored;
static zsb_record_t record;
static int64_t uptime_ms(void){return (int64_t)now;}
static uint32_t esp_random(void){return 0;}
static bool zsb_runtime_required_stub(void){return scenario!=1;}
#define zsb_runtime_required zsb_runtime_required_stub
static bool zj_runtime_writer_ready(void){return scenario!=2;}
static bool add_connector_begin_exclusive_activity(const char *name){
 assert(!strcmp(name,"PRESERVING_SOURCE_BOUNDARY")); ++activity; return scenario!=3;
}
static void add_connector_set_activity(const char *name){assert(!strcmp(name,"LIVE_CAPTURE"));}
zj_result_t zsb_runtime_read(zsb_record_t *out){
 ++reads; assert(prepared==released);
 if(scenario==4)return ZJ_CORRUPT;
 if(stored){*out=record;return ZJ_OK;} return ZJ_EMPTY;
}
zj_result_t zsb_runtime_create(const zsb_facts_t *facts,zsb_record_t *out){
 assert(prepared==released && zsb_facts_valid(facts)); ++creates;
 record.facts=*facts;stored=true;*out=record;
 return scenario==10?ZJ_UNCERTAIN:ZJ_OK;
}
static bool zk_get_counts(int sock,zk_context_t *ctx,int32_t *users,int32_t *records){
 assert(sock==9 && ctx->session_id==7);++counts;*users=2048;
 *records=scenario==11?0:200000; return scenario!=5;
}
static bool zk_prepare_bounded_buffer(int sock,zk_context_t *ctx,unsigned command,unsigned fct,zk_bounded_buffer_t *buffer){
 (void)sock;(void)ctx;assert(command==13 && !fct);++prepared;
 buffer->prepared=true;buffer->size=scenario==11?4:8000004;
 return true;
}
static uint32_t read_le32(const uint8_t *bytes){return (uint32_t)zsb_get(bytes,4);}
static bool zk_read_bounded_range(int sock,zk_context_t *ctx,const zk_bounded_buffer_t *buffer,uint32_t offset,uint8_t *out,uint32_t size){
 (void)sock;(void)ctx;assert(buffer->prepared && prepared>released);
 if(!offset){assert(size==4);zsb_put(out,scenario==6?8000040:buffer->size-4,4);return true;}
 assert(offset==7999964 && size==40);memset(out,8,size);return scenario!=7;
}
static bool zk_close_bounded_buffer(int sock,zk_context_t *ctx,zk_bounded_buffer_t *buffer){
 (void)sock;(void)ctx;if(buffer->prepared){++released;buffer->prepared=false;}return scenario!=8;
}
static int mbedtls_sha256(const uint8_t *raw,size_t size,uint8_t *digest,int sha224){
 assert(size==40 && raw[0]==8 && !sha224);memset(digest,9,32);return scenario==9?-1:0;
}
''' + function + r'''
int main(int argc,char **argv){
 assert(argc==2);scenario=(unsigned)atoi(argv[1]);zk_context_t ctx={.session_id=7};
 if(scenario==12){stored=true;record.facts.next_ordinal=100;}
 zkt_preserve_first_source_boundary(9,&ctx);
 if(scenario==1 || scenario==2){assert(!activity&&!reads&&!counts);return 0;}
 if(scenario==3){assert(activity==1&&!reads&&!counts);return 0;}
 if(scenario==12){assert(reads==1&&!counts&&!creates);return 0;}
 if(scenario>=4 && scenario<=9){
  assert(!creates&&!stored&&prepared==released);
  unsigned before=reads;zkt_preserve_first_source_boundary(9,&ctx);assert(reads==before);return 0;
 }
 assert(creates==1&&stored&&prepared==released);
 assert(record.facts.next_ordinal==(scenario==11?0:200000));
 now+=60000;zkt_preserve_first_source_boundary(9,&ctx);
 assert(creates==1&&counts==1); /* Successful recovery never resamples growth. */
 return 0;
}
'''
    unit = tmp_path / "boundary-gateway.c"
    unit.write_text(program)
    binary = tmp_path / "boundary-gateway"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(unit), str(main / "durable_queue.c"), "-o", str(binary)], check=True)
    for scenario in range(13):
        subprocess.run([str(binary), str(scenario)], check=True, timeout=10)
