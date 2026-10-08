"""Run the production source wire parsers with pinned IDF cJSON and sanitizers."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
CJSON = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
program = r'''
#include "add_source_wire.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
static const char *epoch="11111111-2222-4333-8444-555555555555";
static unsigned allocations,fail_at;
static void *allocate(size_t n) {return ++allocations==fail_at ? NULL : malloc(n);}
static cJSON *base(const char *type)
{
 cJSON *r=cJSON_CreateObject();assert(r);
 cJSON_AddStringToObject(r,"type",type);
 cJSON_AddStringToObject(r,"job_id","aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
 cJSON_AddStringToObject(r,"assignment_id","aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
 cJSON_AddStringToObject(r,"source_epoch",epoch);
 cJSON_AddStringToObject(r,"expected_terminal_serial","synthetic-terminal");
 cJSON_AddStringToObject(r,"terminal_serial","synthetic-terminal");
 cJSON_AddStringToObject(r,"resulting_chain_digest","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
 cJSON_AddStringToObject(r,"source_committed_chain_digest","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
 cJSON_AddNumberToObject(r,"generation",1);
 cJSON_AddNumberToObject(r,"terminal_generation",1);
 cJSON_AddNumberToObject(r,"committed_next_ordinal",10);
 cJSON_AddNumberToObject(r,"source_committed_cursor",10);
 cJSON_AddNumberToObject(r,"chunk_records",100);
 cJSON_AddBoolToObject(r,"active",true);
 return r;
}
static void replace_number(cJSON *r,const char *name,double number)
{cJSON_DeleteItemFromObjectCaseSensitive(r,name);assert(cJSON_AddNumberToObject(r,name,number));}
int main(void)
{
 assert(add_source_epoch_required("2.7.0",true));
 assert(add_source_epoch_required("zone-lite-2.6.16",true));
 assert(add_source_epoch_required("zone-lite-2.6.17",true));
 assert(add_source_epoch_required("zone-lite-2.6.18",true));
 assert(add_source_epoch_required("zone-lite-2.6.19",true));
 assert(!add_source_epoch_required("2.7.0",false));
 assert(!add_source_epoch_required("2.6.15",true));
 add_reconcile_assignment_t a={0},before;
 cJSON *r=base("reconcile_assignment");
 assert(add_source_parse_assignment(r,&a,true) && !strcmp(a.source_epoch,epoch));
 cJSON_DeleteItemFromObjectCaseSensitive(r,"source_epoch");
 assert(add_source_parse_assignment(r,&a,false) && !a.source_epoch[0]);
 before=a;assert(!add_source_parse_assignment(r,&a,true) && !memcmp(&a,&before,sizeof(a)));
 cJSON_AddStringToObject(r,"source_epoch",epoch);
 const char *fields[]={"generation","committed_next_ordinal","chunk_records","credit_end_ordinal","max_chunks","cutoff_count","lease_expires_epoch"};
 double invalid[]={-1,0.5,1.0e100};
 for(size_t f=0;f<sizeof(fields)/sizeof(fields[0]);++f){
  for(size_t n=0;n<sizeof(invalid)/sizeof(invalid[0]);++n){
   cJSON *bad=cJSON_Duplicate(r,true);replace_number(bad,fields[f],invalid[n]);
   assert(!add_source_parse_assignment(bad,&a,true));cJSON_Delete(bad);
  }
 }
 cJSON *probe=base("source_probe_assignment");
 cJSON_DeleteItemFromObjectCaseSensitive(probe,"committed_next_ordinal");
 cJSON_AddNumberToObject(probe,"ordinal",7);
 cJSON_AddNumberToObject(probe,"credit_end_ordinal",100); /* Previously dereferenced missing committed. */
 assert(add_source_parse_assignment(probe,&a,true) && a.source_probe && a.probe_ordinal==7);
 replace_number(probe,"ordinal",4294967296.0);assert(!add_source_parse_assignment(probe,&a,true));
 cJSON_Delete(probe);
 for(size_t i=0;i<36;++i){
  char bad[37];memcpy(bad,epoch,37);bad[i]='X';
  cJSON_ReplaceItemInObjectCaseSensitive(r,"source_epoch",cJSON_CreateString(bad));
  assert(!add_source_parse_assignment(r,&a,true));
 }
 cJSON_ReplaceItemInObjectCaseSensitive(r,"source_epoch",cJSON_CreateString(epoch));
 char serial[81];memset(serial,'x',80);serial[80]=0;
 cJSON_ReplaceItemInObjectCaseSensitive(r,"expected_terminal_serial",cJSON_CreateString(serial));
 assert(!add_source_parse_assignment(r,&a,true)); /* Never truncate a binding. */
 cJSON_Delete(r);
 r=base("reconcile_chunk_ack");
 add_reconcile_chunk_ack_t chunk={0};add_source_tail_ack_t tail={0};add_source_coverage_t coverage={0};
 assert(add_source_parse_chunk_ack(r,&chunk,true) && chunk.valid && !strcmp(chunk.source_epoch,epoch));
 assert(add_source_parse_tail_ack(r,&tail,true) && tail.valid && !strcmp(tail.source_epoch,epoch));
 assert(add_source_parse_coverage(r,&coverage,true) && coverage.active);
 assert(add_source_ack_matches(r,add_source_ack_type("reconcile_chunk"),epoch));
 assert(!add_source_ack_matches(r,add_source_ack_type("reconcile_source_manifest"),epoch));
 assert(!add_source_ack_matches(r,add_source_ack_type("reconcile_chunk"),"22222222-2222-4333-8444-555555555555"));
 assert(!strcmp(add_source_ack_type("reconcile_anchor"),"reconcile_anchor_ack"));
 cJSON_DeleteItemFromObjectCaseSensitive(r,"source_epoch");
 assert(!add_source_parse_chunk_ack(r,&chunk,true));
 assert(!add_source_parse_tail_ack(r,&tail,true));
 assert(!add_source_parse_coverage(r,&coverage,true));
 cJSON_ReplaceItemInObjectCaseSensitive(r,"active",cJSON_CreateBool(false));
 assert(add_source_parse_coverage(r,&coverage,true) && !coverage.active && !coverage.source_epoch[0]);
 assert(add_source_parse_chunk_ack(r,&chunk,false) && !chunk.source_epoch[0]);
 cJSON_AddStringToObject(r,"source_epoch",epoch);
 replace_number(r,"committed_next_ordinal",4294967296.0);
 assert(!add_source_parse_chunk_ack(r,&chunk,true) && !add_source_parse_tail_ack(r,&tail,true));
 replace_number(r,"source_committed_cursor",0.25);assert(!add_source_parse_coverage(r,&coverage,true));
 cJSON_Delete(r);
 r=base("reconcile_assignment");char *wire=cJSON_PrintUnformatted(r);cJSON_Delete(r);assert(wire);
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 for(unsigned point=1;point<200;++point){
  allocations=0;fail_at=point;cJSON *parsed=cJSON_Parse(wire);
  bool ok=add_source_parse_assignment(parsed,&a,true);
  assert(!ok || allocations<point);cJSON_Delete(parsed);
 }
 free(wire);
 for(unsigned point=1;point<8;++point){
  fail_at=0;r=cJSON_CreateObject();allocations=0;fail_at=point;
  bool ok=add_source_epoch_write(r,epoch);assert(!ok || allocations<point);
  if(ok){char check[37];assert(add_source_epoch_read(r,check,true) && !strcmp(check,epoch));}
  cJSON_Delete(r);
 }
 cJSON_InitHooks(NULL);
 puts("Source epoch, bounded numeric parsing, missing probe cursor and allocation faults passed");
 return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="zkt-source-wire-") as temporary:
    unit = Path(temporary) / "wire.c"
    unit.write_text(program)
    binary = Path(temporary) / "wire"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    "-I", str(CJSON), str(unit), str(MAIN / "add_source_wire.c"),
                    str(CJSON / "cJSON.c"), "-lm", "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
