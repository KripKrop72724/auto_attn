"""Execute the historical firmware parser; current names cannot authorize ORDS."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_historical_parser_retains_identifiers_without_current_person(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    parser = source[source.index("static bool parse_attendance_record("):
                    source.index("static void sha256_bytes_hex(")]
    end = source.index("} attendance_event_t;") + len("} attendance_event_t;")
    event_type = source[source.rfind("typedef struct {", 0, end):end]
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "zkt_record.h"
''' + event_type + r'''
typedef struct {int unused;} user_table_t;
static unsigned builder_calls;
static bool build_attendance_event(attendance_event_t *out,const user_table_t *t,const char *id,
 uint16_t uid,uint32_t timestamp,uint8_t status,uint8_t punch,bool snapshot){
 (void)t;(void)status;(void)punch;
 ++builder_calls;assert(uid==0 && snapshot && id[0]);
 memset(out,0,sizeof(*out));if(!timestamp)return false;
 snprintf(out->user_id,sizeof(out->user_id),"%s",id);strcpy(out->uid,"7");
 strcpy(out->cnic,"1234567890123");strcpy(out->employee_name,"Current owner");
 strcpy(out->event_uid,"unchanged-event-uid");strcpy(out->terminal_identity_fingerprint,"current-fingerprint");
 out->raw_punch=true;return true;
}
''' + parser + r'''
int main(void){
 user_table_t table={0};attendance_event_t event;uint32_t timestamp;
 const unsigned sizes[]={16,40};
 for(unsigned i=0;i<2;i++){
  uint8_t row[40]={7};
  if(sizes[i]==16)row[4]=1;
  else {row[0]=40;row[2]='7';row[27]=1;}
  assert(parse_attendance_record(row,sizes[i],&table,&event,&timestamp));
  assert(!event.cnic[0] && !event.employee_name[0] && !event.raw_punch);
  assert(!strcmp(event.event_uid,"unchanged-event-uid"));
  assert(!strcmp(event.user_id,"7") && !strcmp(event.uid,"7"));
  assert(!strcmp(event.terminal_identity_fingerprint,"current-fingerprint"));
  if(sizes[i]==40)assert(!strcmp(event.attendance_record_uid,"40"));
 }
 assert(builder_calls==2);
 /* A current enrollment with the same numeric UID must never be consulted. */
 uint8_t missing[40]={40};missing[3]=1;
 assert(!parse_attendance_record(missing,8,&table,&event,&timestamp));
 assert(zkt_record_identity_missing(missing,8));
 missing[3]=0;missing[27]=1;
 assert(!parse_attendance_record(missing,40,&table,&event,&timestamp));
 assert(zkt_record_identity_missing(missing,40));
 memset(missing+2,' ',24);
 assert(!parse_attendance_record(missing,40,&table,&event,&timestamp));
 assert(zkt_record_identity_missing(missing,40));
 assert(builder_calls==2);
 uint8_t row[13]={0};assert(!parse_attendance_record(row,13,&table,&event,&timestamp));
 return 0;
}
'''
    unit = tmp_path / "historical.c"
    unit.write_text(program)
    executable = tmp_path / "historical"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(ROOT / "firmware/zone_lite/main"),
                    str(ROOT / "firmware/zone_lite/main/zkt_record.c"), str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
