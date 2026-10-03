"""Exercise production source-row classification and atomic cJSON allocation.

The crypto/base64 and enrollment boundaries are spies, not alternate protocol
implementations. Actual record decoding and row construction run unchanged.
"""
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
source = (MAIN / "zone_lite.c").read_text()
end = source.index("} attendance_event_t;") + len("} attendance_event_t;")
event_type = source[source.rfind("typedef struct {", 0, end):end]
parser = source[source.index("static bool parse_attendance_record("):
                source.index("static void sha256_bytes_hex(")]
append = source[source.index("static bool append_terminal_source_record("):
                source.index("static void release_reconciliation_credit(")]
clock_fields = source[source.index("static bool attendance_record_timestamp("):
                      source.index("static bool find_attendance_month_bounds(")]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "zkt_record.h"
static unsigned calls, fail_at, builds;
static uint8_t encoded_input[40]; static size_t encoded_length;
static void *allocate(size_t size) { if (++calls == fail_at) return NULL; return malloc(size); }
typedef struct {int unused;} user_table_t;
static const char *g_device_serial="synthetic-terminal";
static uint16_t read_le16(const uint8_t *p) {return (uint16_t)p[0] | (uint16_t)p[1]<<8;}
static uint32_t read_le32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24;
}
static bool zk_attendance_timestamp_is_plausible(uint32_t t) {return t==859972462U;}
static void copy_zk_string(char *out,size_t size,const uint8_t *raw,size_t length) {
    size_t n=0; while(n<length && raw[n] && n+1<size) {out[n]=(char)raw[n]; ++n;}
    while(n && out[n-1]==' ') --n;
    out[n]=0;
}
static void json_add_utf8_string(cJSON *row,const char *name,const char *value) {
    cJSON_AddStringToObject(row,name,value);
}
static void sha256_bytes_hex(const uint8_t *raw,size_t length,char out[65]) {
    assert(raw && length<=40); memset(out,'a',64); out[64]=0;
}
static void sha256_hex(const char *in,char out[65]) {assert(in[0]);memset(out,'b',64);out[64]=0;}
static bool encode_record_base64(const uint8_t *raw,size_t length,char *out,size_t size) {
    assert(length<=sizeof(encoded_input));memcpy(encoded_input,raw,length);encoded_length=length;
    return snprintf(out,size,"preserved-raw-evidence")<(int)size;
}
''' + event_type + r'''
static bool build_attendance_event(attendance_event_t *out,const user_table_t *users,
    const char *id,uint16_t uid,uint32_t timestamp,uint8_t status,uint8_t punch,bool snapshot) {
    (void)users;(void)status;(void)punch;++builds;
    assert(uid==0 && id[0] && snapshot);
    if(!zk_attendance_timestamp_is_plausible(timestamp))return false;
    memset(out,0,sizeof(*out));snprintf(out->user_id,sizeof(out->user_id),"%s",id);
    strcpy(out->event_uid,"stable-event");return true;
}
static cJSON *add_attendance_json_row(const attendance_event_t *event,const char *capture) {
    assert(event->user_id[0] && capture[0]);cJSON *row=cJSON_CreateObject();
    if(row && !cJSON_AddStringToObject(row,"event_uid",event->event_uid)) {cJSON_Delete(row);return NULL;}
    return row;
}
''' + clock_fields + parser + append + r'''
static void check(const uint8_t *raw,unsigned size,const char *expected,bool has_event) {
    user_table_t table={0}; cJSON *rows=cJSON_CreateArray(),*canonical=cJSON_CreateArray();
    assert(rows && canonical);calls=fail_at=builds=0;
    const char *disposition=NULL;
    assert(append_terminal_source_record(rows,canonical,raw,size,&table,3,"FULL_HISTORY",&disposition));
    unsigned allocations=calls;
    assert(!strcmp(disposition,expected));
    assert(encoded_length==size && !memcmp(encoded_input,raw,size));
    cJSON *row=cJSON_GetArrayItem(rows,0),*digest_row=cJSON_GetArrayItem(canonical,0);
    assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(row,"disposition")),expected));
    assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(digest_row,"disposition")),expected));
    assert(cJSON_HasObjectItem(row,"event")==has_event);
    assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(row,"raw_record_b64")),"preserved-raw-evidence"));
    if(!strcmp(expected,"IDENTITY_UNRESOLVED")) {
        assert(!builds && !cJSON_HasObjectItem(row,"observed_user_id"));
        assert(cJSON_IsNull(cJSON_GetObjectItem(digest_row,"event_uid")));
        assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(row,"observed_uid")),"40"));
        assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(row,"error_code")),"HISTORICAL_IDENTITY_EVIDENCE_REQUIRED"));
    }
    cJSON_Delete(rows);cJSON_Delete(canonical);
    for(unsigned fault=1;fault<=allocations;++fault) {
        fail_at=0;rows=cJSON_CreateArray();canonical=cJSON_CreateArray();
        calls=0;fail_at=fault;disposition="unchanged";
        assert(!append_terminal_source_record(rows,canonical,raw,size,&table,3,"FULL_HISTORY",&disposition));
        assert(cJSON_GetArraySize(rows)==0 && cJSON_GetArraySize(canonical)==0);
        assert(!strcmp(disposition,"unchanged"));
        cJSON_Delete(rows);cJSON_Delete(canonical);
    }
    fail_at=0;
}
int main(void) {
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    uint8_t raw[40]={40};uint32_t time=859972462U;
    for(unsigned i=0;i<4;++i) raw[27+i]=(uint8_t)(time>>(i*8));
    check(raw,40,"IDENTITY_UNRESOLVED",false);
    memset(raw+2,' ',24);check(raw,40,"IDENTITY_UNRESOLVED",false);
    memset(raw+2,0,24);raw[2]='7';check(raw,40,"EVENT",true);
    raw[2]=1;check(raw,40,"MALFORMED",false);
    raw[2]=0;memset(raw+27,0,4);check(raw,40,"INVALID_TIME",false);
    memset(raw,0,sizeof(raw));raw[0]=40;
    for(unsigned i=0;i<4;++i) raw[3+i]=(uint8_t)(time>>(i*8));
    check(raw,8,"IDENTITY_UNRESOLVED",false);
    memset(raw,0,sizeof(raw));raw[0]=7;
    for(unsigned i=0;i<4;++i) raw[4+i]=(uint8_t)(time>>(i*8));
    check(raw,16,"EVENT",true);
    puts("Source identity holds, raw evidence and atomic allocation faults passed");
    return 0;
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    unit, executable = Path(directory) / "source.c", Path(directory) / "source"
    unit.write_text(program)
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(cjson),
        "-I", str(MAIN), str(unit), str(MAIN / "zkt_record.c"),
        str(cjson / "cJSON.c"), "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)
