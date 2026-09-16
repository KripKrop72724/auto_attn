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
''' + event_type + r'''
typedef struct {char user_id[32];} zkt_user_t;
typedef struct {int unused;} user_table_t;
static zkt_user_t current={"7"};
static const zkt_user_t *find_user_by_uid(const user_table_t *t,uint16_t uid){(void)t;return uid==7?&current:NULL;}
static const zkt_user_t *find_user_by_user_id(const user_table_t *t,const char *id){(void)t;return !strcmp(id,"7")?&current:NULL;}
static uint16_t read_le16(const uint8_t *p){return p[0]|((uint16_t)p[1]<<8);}
static uint32_t read_le32(const uint8_t *p){return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void copy_zk_string(char *out,size_t capacity,const uint8_t *in,size_t length){
 size_t n=0;while(n<length && n+1<capacity && in[n]){out[n]=(char)in[n];n++;}out[n]=0;
}
static bool build_attendance_event(attendance_event_t *out,const user_table_t *t,const char *id,
 uint16_t uid,uint32_t timestamp,uint8_t status,uint8_t punch,bool snapshot){
 (void)t;(void)uid;(void)status;(void)punch;(void)snapshot;
 memset(out,0,sizeof(*out));if(!timestamp)return false;
 snprintf(out->user_id,sizeof(out->user_id),"%s",id);strcpy(out->uid,"7");
 strcpy(out->cnic,"1234567890123");strcpy(out->employee_name,"Current owner");
 strcpy(out->event_uid,"unchanged-event-uid");strcpy(out->terminal_identity_fingerprint,"current-fingerprint");
 out->raw_punch=true;return true;
}
''' + parser + r'''
int main(void){
 user_table_t table={0};attendance_event_t event;uint32_t timestamp;
 const unsigned sizes[]={8,16,40};
 for(unsigned i=0;i<3;i++){
  uint8_t row[40]={7};
  if(sizes[i]==8)row[3]=1;
  else if(sizes[i]==16)row[4]=1;
  else {row[2]='7';row[27]=1;}
  assert(parse_attendance_record(row,sizes[i],&table,&event,&timestamp));
  assert(!event.cnic[0] && !event.employee_name[0] && !event.raw_punch);
  assert(!strcmp(event.event_uid,"unchanged-event-uid"));
  assert(!strcmp(event.user_id,"7") && !strcmp(event.uid,"7"));
  assert(!strcmp(event.terminal_identity_fingerprint,"current-fingerprint"));
  if(sizes[i]!=16)assert(!strcmp(event.attendance_record_uid,"7"));
 }
 uint8_t row[13]={0};assert(!parse_attendance_record(row,13,&table,&event,&timestamp));
 return 0;
}
'''
    unit = tmp_path / "historical.c"
    unit.write_text(program)
    executable = tmp_path / "historical"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
