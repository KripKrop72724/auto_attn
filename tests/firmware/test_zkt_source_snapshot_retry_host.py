"""Real source precheck: transport uncertainty is not immutable divergence."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def snapshot_binary(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("source-snapshot")
    main = ROOT / "firmware/zone_lite/main"
    source = (main / "zone_lite.c").read_text()
    start = source.index("typedef enum {\n    SOURCE_SNAPSHOT_READY,")
    end = source.index("static bool process_add_reconciliation_assignment(", start)
    actual = source[start:end]
    dispatch_start = source.index("    uint32_t record_size = 0;", end)
    dispatch_end = source.index("    if (!assignment->has_cutoff)", dispatch_start)
    dispatch = source[dispatch_start:dispatch_end].replace("    bool ok = true;", "    (void)record_size;")
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "zkt_record.h"
#define SHUT_RDWR 2
typedef struct {unsigned session_id;} zk_context_t;
typedef struct {bool prepared;uint32_t size;} zk_bounded_buffer_t;
typedef struct {char first_anchor_digest[65],committed_predecessor_digest[65],preceding_chain_digest[65],source_epoch[37];uint32_t committed_next_ordinal,cutoff_count,generation;bool has_cutoff;} add_reconcile_assignment_t;
static unsigned reads,failed_read,closes,counts,shutdowns,hashes,sends;
static uint32_t payload_bytes;
static int32_t fresh_count;
static bool release_ok,count_ok,first_mismatch,boundary_mismatch;
static char last_code[64];
static int shutdown(int sock,int how){assert(sock==7 && how==SHUT_RDWR);++shutdowns;return 0;}
static bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *message){
 assert(!strcmp(subsystem,"reconcile") && message[0]);assert(!strcmp(level,"WARN") || !strcmp(level,"ERROR"));
 assert(strlen(code)<sizeof(last_code));strcpy(last_code,code);return true;
}
static uint32_t read_le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static uint32_t choose_zk_record_size(uint32_t bytes,uint32_t count,const uint32_t *sizes,size_t n){return zkt_record_size(bytes,count,sizes,n);}
static bool zk_read_bounded_range(int sock,zk_context_t *ctx,const zk_bounded_buffer_t *buffer,uint32_t offset,uint8_t *out,uint32_t bytes){
 assert(sock==7 && ctx->session_id==3 && buffer->prepared && !closes);++reads;
 if(reads==failed_read)return false;
 if(!offset){assert(bytes==4);for(unsigned i=0;i<4;i++)out[i]=(uint8_t)(payload_bytes>>(8*i));}
 else {assert(bytes==40 || bytes==16 || bytes==8);memset(out,offset==4?(first_mismatch?'x':'a'):(boundary_mismatch?'x':'b'),bytes);}
 return true;
}
static bool zk_close_bounded_buffer(int sock,zk_context_t *ctx,zk_bounded_buffer_t *buffer){
 assert(sock==7 && ctx->session_id==3 && buffer->prepared && !closes);++closes;buffer->prepared=false;buffer->size=0;return release_ok;
}
static bool zk_get_counts(int sock,zk_context_t *ctx,int32_t *users,int32_t *count){
 assert(sock==7 && ctx->session_id==3 && closes==1 && !shutdowns);++counts;assert(counts==1);*users=736;*count=fresh_count;return count_ok;
}
static void sha256_bytes_hex(const uint8_t *bytes,size_t length,char out[65]){++hashes;memset(out,length?bytes[0]:'e',64);out[64]=0;}
/* ACTUAL_HELPERS */
static bool actual_dispatch(int sock,zk_context_t *ctx,zk_bounded_buffer_t source,int32_t latest_records,uint32_t cutoff,const add_reconcile_assignment_t *assignment){
/* ACTUAL_DISPATCH */
 ++sends;return true; /* Records/anchors can only be reached after READY. */
}
static void reset(void){reads=closes=counts=shutdowns=hashes=sends=failed_read=0;payload_bytes=68788U*40;fresh_count=68788;release_ok=count_ok=true;first_mismatch=boundary_mismatch=false;last_code[0]=0;}
int main(int argc,char **argv){
 assert(argc==2);unsigned scenario=(unsigned)atoi(argv[1]);reset();
 zk_context_t ctx={3};zk_bounded_buffer_t source={true,4+payload_bytes};
 add_reconcile_assignment_t assignment={.committed_next_ordinal=57300,.cutoff_count=68788,.generation=2,.has_cutoff=true};
 memset(assignment.first_anchor_digest,'a',64);memset(assignment.committed_predecessor_digest,'b',64);memset(assignment.preceding_chain_digest,'c',64);strcpy(assignment.source_epoch,"11111111-2222-4333-8444-555555555555");
 int32_t before=68788;source_snapshot_result_t expected=SOURCE_SNAPSHOT_RETRY;
 const char *code="SOURCE_RANGE_READ_RETRY";
 if(scenario==0){expected=SOURCE_SNAPSHOT_READY;code="";}
 if(scenario>=1 && scenario<=3)failed_read=scenario;
 if(scenario==4 || scenario==5 || scenario==18){payload_bytes=68789U*40;source.size=4+payload_bytes;fresh_count=scenario==4?68789:68790;code="SOURCE_RANGE_SNAPSHOT_RETRY";}
 if(scenario==6){payload_bytes=68789U*16;source.size=4+payload_bytes;fresh_count=68790;code="SOURCE_RANGE_SNAPSHOT_RETRY";}
 if(scenario==7){payload_bytes=68789U*8;source.size=4+payload_bytes;fresh_count=68790;code="SOURCE_RANGE_SNAPSHOT_RETRY";}
 if(scenario==8){payload_bytes=160;source.size=164;before=1;fresh_count=20;assignment.cutoff_count=1;assignment.committed_next_ordinal=0;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";}
 if(scenario==9){++payload_bytes;++source.size;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";}
 if(scenario==10){source.size-=1;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";}
 if(scenario==11 || scenario==12){payload_bytes+=40;source.size+=40;fresh_count=scenario==11?68787:57299;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_COUNT_REGRESSION";}
 if(scenario==13){payload_bytes+=40;source.size+=40;count_ok=false;}
 if(scenario==14){payload_bytes+=40;source.size+=40;release_ok=false;}
 if(scenario==15){failed_read=2;release_ok=false;}
 if(scenario==16){first_mismatch=true;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_FIRST_ANCHOR_DIVERGED";}
 if(scenario==17){boundary_mismatch=true;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_COMMITTED_BOUNDARY_DIVERGED";}
 if(scenario==19){payload_bytes=UINT32_MAX;source.size=128U*1024*1024;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";}
 if(scenario==20){assignment.committed_predecessor_digest[0]=0;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_COMMITTED_BOUNDARY_DIVERGED";}
 if(scenario==21){before=0;payload_bytes=0;source.size=4;assignment.cutoff_count=assignment.committed_next_ordinal=0;assignment.first_anchor_digest[0]=0;expected=SOURCE_SNAPSHOT_READY;code="";}
 if(scenario==22){payload_bytes=68789U*40;source.size=4+payload_bytes;fresh_count=-1;}
 if(scenario>=23 && scenario<=26){before=0;payload_bytes=scenario==26?24:8;source.size=4+payload_bytes;assignment.cutoff_count=assignment.committed_next_ordinal=0;fresh_count=scenario==26?3:1;code="SOURCE_RANGE_SNAPSHOT_RETRY";}
 if(scenario==24){count_ok=false;code="SOURCE_RANGE_READ_RETRY";}
 if(scenario==25){fresh_count=0;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";}
 add_reconcile_assignment_t saved=assignment;
 unsigned repeat=scenario==18?20:1;
 for(unsigned iteration=0;iteration<repeat;iteration++){
  uint32_t original_size=source.size;uint32_t width=0;char digest[65];
  source_snapshot_result_t result=zk_validate_source_snapshot(7,&ctx,&source,before,assignment.cutoff_count,&assignment,&width,digest);
  assert(result==expected && !strcmp(last_code,code));assert(!memcmp(&assignment,&saved,sizeof(saved)) && !sends);
  if(expected==SOURCE_SNAPSHOT_READY){assert(width==40 && !closes && source.prepared && !counts);assert(zk_release_source_snapshot(7,&ctx,&source));}
  else{assert(!width && closes==1 && !source.prepared);}
  if(scenario==14 || scenario==15)assert(shutdowns==1 && !counts);
  if(scenario==4 || scenario==5 || scenario==6 || scenario==7 || scenario==18)assert(counts==1 && reads==1 && !hashes);
  /* Real dispatch cannot reach sends on any failed/retried snapshot. */
  if(expected!=SOURCE_SNAPSHOT_READY){
   reads=closes=counts=shutdowns=hashes=0;source=(zk_bounded_buffer_t){true,original_size};
   assert(actual_dispatch(7,&ctx,source,before,assignment.cutoff_count,&assignment)==(expected==SOURCE_SNAPSHOT_HOLD));assert(!sends);assert(!memcmp(&assignment,&saved,sizeof(saved)));
  }
  reads=closes=counts=shutdowns=hashes=0;source=(zk_bounded_buffer_t){true,original_size};
 }
 return 0;
}
'''
    unit = tmp_path / "snapshot.c"
    unit.write_text(program.replace("/* ACTUAL_HELPERS */", actual).replace("/* ACTUAL_DISPATCH */", dispatch))
    binary = tmp_path / "snapshot"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(unit), str(main / "zkt_record.c"), "-o", str(binary)], check=True)
    return binary


@pytest.mark.parametrize("scenario", range(27))
def test_actual_snapshot_preserves_checkpoint_and_fault_evidence(snapshot_binary, scenario):
    subprocess.run([str(snapshot_binary), str(scenario)], check=True, timeout=10)
