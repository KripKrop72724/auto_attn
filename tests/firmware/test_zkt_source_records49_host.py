"""Real source-range code over a synthetic 49-byte MB40-VL/ID attendance log.

LHRCENTRAL's MB40-VL/ID terminals (platform ZAM180_TFT) return the 40-byte
record plus a 9-byte tail (0xff or 0x00, ASCII "255", zeros). The records here
are synthetic: user ID "1234", sequential UIDs and the observed tail bytes.
The firmware functions are extracted unchanged from zone_lite.c.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def _between(source: str, start: str, end: str) -> str:
    first = source.index(start)
    return source[first:source.index(end, first)]


@pytest.fixture(scope="module")
def records49_binary(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("records49")
    source = (MAIN / "zone_lite.c").read_text()
    chooser = _between(source, "static uint32_t choose_zk_record_size(", "static bool zk_load_users(")
    snapshot = _between(source, "typedef enum {\n    SOURCE_SNAPSHOT_READY,",
                        "static bool process_add_reconciliation_assignment(")
    decode_time = _between(source, "static void decode_zk_time(", "static bool zk_attendance_timestamp_is_plausible(")
    months = _between(source, "static int days_in_month(int year, int month)\n{", "static void history_dump_cache_clear(")
    timestamps = _between(source, "static bool attendance_record_timestamp(", "static bool parse_attendance_record(")
    parser = _between(source, "static bool parse_attendance_record(", "static void sha256_bytes_hex(")
    end = source.index("} attendance_event_t;") + len("} attendance_event_t;")
    event_type = source[source.rfind("typedef struct {", 0, end):end]
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "zkt_record.h"
#define SHUT_RDWR 2
#define ZONE_LITE_MIN_ATTENDANCE_YEAR 2010
typedef enum { ZK_FRAME_SESSION, ZK_FRAME_OVERSIZE, ZK_FRAME_RECORD_SIZE, ZK_FRAME_KINDS } zk_frame_deviation_t;
typedef struct {unsigned session_id;} zk_context_t;
typedef struct {bool prepared;uint32_t size;} zk_bounded_buffer_t;
typedef struct {char first_anchor_digest[65],committed_predecessor_digest[65];uint32_t committed_next_ordinal;} add_reconcile_assignment_t;
typedef struct {int unused;} user_table_t;
static bool legacy_framing;
static unsigned reads,closes,counts,builds;
static uint32_t read_offsets[8],read_lengths[8];
static uint8_t *log_bytes;
static int32_t fresh_count;
static char last_code[64],last_message[256];
static int shutdown(int sock,int how){(void)sock;(void)how;assert(!"release is confirmed in these scenarios");return 0;}
static bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *message){
 assert(!strcmp(subsystem,"reconcile") && (!strcmp(level,"WARN") || !strcmp(level,"ERROR")));
 snprintf(last_code,sizeof(last_code),"%s",code);snprintf(last_message,sizeof(last_message),"%s",message);return true;
}
static bool zk_legacy_framing(void){return legacy_framing;}
static void zk_note_framing(zk_frame_deviation_t kind,uint16_t value,uint32_t expected,uint32_t observed){
 assert(kind==ZK_FRAME_RECORD_SIZE && legacy_framing);(void)value;(void)expected;(void)observed;
}
static uint32_t read_le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static bool zk_read_bounded_range(int sock,zk_context_t *ctx,const zk_bounded_buffer_t *buffer,uint32_t offset,uint8_t *out,uint32_t bytes){
 assert(sock==7 && ctx->session_id==3 && buffer->prepared && !closes && reads<8);
 assert(offset<=buffer->size && bytes<=buffer->size-offset);
 read_offsets[reads]=offset;read_lengths[reads++]=bytes;memcpy(out,log_bytes+offset,bytes);return true;
}
static bool zk_close_bounded_buffer(int sock,zk_context_t *ctx,zk_bounded_buffer_t *buffer){
 assert(sock==7 && ctx->session_id==3 && buffer->prepared && !closes);++closes;buffer->prepared=false;buffer->size=0;return true;
}
static bool zk_get_counts(int sock,zk_context_t *ctx,int32_t *users,int32_t *count){
 assert(sock==7 && ctx->session_id==3 && closes==1);++counts;*users=736;*count=fresh_count;return true;
}
/* FNV-1a over every byte: equality evidence only, sensitive to each tail byte. */
static void sha256_bytes_hex(const uint8_t *bytes,size_t length,char out[65]){
 uint64_t h=1469598103934665603ULL;for(size_t i=0;i<length;++i){h^=bytes[i];h*=1099511628211ULL;}
 for(unsigned i=0;i<4;++i)snprintf(out+16*i,17,"%016llx",(unsigned long long)(h+i));
}
''' + chooser + decode_time + months + timestamps + event_type + r'''
static bool build_attendance_event(attendance_event_t *out,const user_table_t *users,const char *id,
 uint16_t uid,uint32_t timestamp,uint8_t status,uint8_t punch,bool snapshot){
 (void)users;(void)status;(void)punch;++builds;assert(uid==0 && snapshot && !strcmp(id,"1234"));
 memset(out,0,sizeof(*out));snprintf(out->user_id,sizeof(out->user_id),"%s",id);
 strcpy(out->event_uid,"synthetic-event");return timestamp!=0;
}
''' + parser + snapshot + r'''
static uint32_t zk_time(int year,int month,int day,int hour,int minute,int second){
 return (uint32_t)((((((year-2000)*12+(month-1))*31+(day-1))*24+hour)*60+minute)*60+second);
}
/* Bytes 0-39: uid, user "1234", status, packed time, punch 0, eight zeros.
 * Bytes 40-48: the observed tail; a few records carry 00 instead of ff. */
static void synthetic_record(uint8_t *raw,uint32_t width,uint32_t index){
 memset(raw,0,width);raw[0]=(uint8_t)(index+1);raw[1]=(uint8_t)((index+1)>>8);memcpy(raw+2,"1234",4);
 raw[26]=index%3?15:1;
 uint32_t t=zk_time(2024+(int)(index/4000)%3,1+(int)(index/300)%12,1+(int)(index%28),8+(int)(index%9),(int)(index%60),(int)(index*7%60));
 for(unsigned i=0;i<4;++i)raw[27+i]=(uint8_t)(t>>(8*i));
 if(width==49){raw[40]=index%2000==7?0x00:0xff;memcpy(raw+41,"255",3);}
}
static uint32_t build_log(uint32_t width,uint32_t records,uint32_t extra){
 uint32_t payload=width*records+extra;free(log_bytes);log_bytes=calloc(1,4+(size_t)payload);assert(log_bytes);
 for(unsigned i=0;i<4;++i)log_bytes[i]=(uint8_t)(payload>>(8*i));
 for(uint32_t i=0;i<records;++i)synthetic_record(log_bytes+4+(size_t)i*width,width,i);
 return payload;
}
static void digest_of(uint32_t width,uint32_t ordinal,char out[65]){sha256_bytes_hex(log_bytes+4+(size_t)ordinal*width,width,out);}
static void check_snapshot(unsigned scenario){
 zk_context_t ctx={3};add_reconcile_assignment_t a={.committed_next_ordinal=6000};
 uint32_t width=49,records=13293,extra=0,cutoff=13293,expected_width=49;int32_t before=13293;
 source_snapshot_result_t expected=SOURCE_SNAPSHOT_READY;const char *code="",*message="";
 fresh_count=before;
 switch(scenario){
 case 0:break; /* LHRCENTRAL-01, 4 Nov: prepared=651361 payload=651357 count=13293. */
 case 1:case 2:expected=SOURCE_SNAPSHOT_HOLD;
  code=scenario==1?"SOURCE_FIRST_ANCHOR_DIVERGED":"SOURCE_COMMITTED_BOUNDARY_DIVERGED";break;
 case 3:records=13294;fresh_count=13294;expected=SOURCE_SNAPSHOT_RETRY;code="SOURCE_RANGE_SNAPSHOT_RETRY";
  message="Append snapshot changed count 13293 to 13294 (prepared=651410 payload=651406 width=49); no source records committed.";break;
 case 4: /* 1225 40-byte records counted as 1000 before preparing match 1000 x 49. */
  width=40;records=1225;before=1000;cutoff=1000;a.committed_next_ordinal=900;
  expected=SOURCE_SNAPSHOT_RETRY;code="SOURCE_RANGE_SNAPSHOT_RETRY";
  message="A 49-byte width also fits a grown 8/16/40-byte log and the first record lacks the 49-byte tail; no source records committed.";break;
 case 5: /* Counted as 999: both 40 and 49 explain the growth. Retried as before, naming no width. */
  width=40;records=1225;before=999;cutoff=999;fresh_count=1225;a.committed_next_ordinal=900;
  expected=SOURCE_SNAPSHOT_RETRY;code="SOURCE_RANGE_SNAPSHOT_RETRY";
  message="Append snapshot changed count 999 to 1225 (prepared=49004 payload=49000 width=0); no source records committed.";break;
 case 6:extra=1;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_LAYOUT_INVALID";
  message="Source layout rejected (read=complete prepared=651362 payload=651358 count_before=13293 count_after=13293).";break;
 case 7:width=40;expected_width=40;break; /* The same count of 40-byte records is unchanged. */
 case 8:records=13294;fresh_count=13292;expected=SOURCE_SNAPSHOT_HOLD;code="SOURCE_RANGE_COUNT_REGRESSION";break;
 case 9: /* Growth to a count divisible by 8: the 2.6.15 fallback picks 16, the recount names 49. */
  records=13296;fresh_count=13296;expected=SOURCE_SNAPSHOT_RETRY;code="SOURCE_RANGE_SNAPSHOT_RETRY";
  message="Append snapshot changed count 13293 to 13296 (prepared=651508 payload=651504 width=49); no source records committed.";break;
 case 10:case 11: /* 13296 x 49 also divides by 8: the first record's tail proves 49. */
  records=13296;before=13296;cutoff=13296;fresh_count=13296;
  if(scenario==11){expected=SOURCE_SNAPSHOT_RETRY;code="SOURCE_RANGE_SNAPSHOT_RETRY";}break;
 case 12:records=13296;before=13296;cutoff=13296;break; /* A 00 lead tail byte also proves it. */
 default:assert(!"scenario");
 }
 uint32_t payload=build_log(width,records,extra);
 zk_bounded_buffer_t source={true,4+payload};
 if(scenario==12)log_bytes[4+40]=0;
 digest_of(width,0,a.first_anchor_digest);
 if(a.committed_next_ordinal)digest_of(width,a.committed_next_ordinal-1,a.committed_predecessor_digest);
 if(scenario==1)log_bytes[4+44]^=1; /* Only the first record's tail changed. */
 if(scenario==2)log_bytes[4+(size_t)(a.committed_next_ordinal-1)*49+48]^=1;
 if(scenario==11)log_bytes[4+41]='3';
 reads=closes=counts=0;last_code[0]=last_message[0]=0;
 uint32_t selected=0;char first[65]={0};
 source_snapshot_result_t result=zk_validate_source_snapshot(7,&ctx,&source,before,cutoff,&a,&selected,first);
 if(result!=expected||strcmp(last_code,code))fprintf(stderr,"scenario %u: result=%d code=%s message=%s\n",scenario,result,last_code,last_message);
 assert(result==expected && !strcmp(last_code,code));
 if(message[0])assert(!strcmp(last_message,message));
 if(expected==SOURCE_SNAPSHOT_READY){
  /* Anchor and committed boundary digests cover every byte, tail included. */
  assert(selected==expected_width && !closes && source.prepared && reads==3);
  assert(read_offsets[0]==0 && read_lengths[0]==4);
  assert(read_offsets[1]==4 && read_lengths[1]==expected_width);
  assert(read_offsets[2]==4+(a.committed_next_ordinal-1)*expected_width && read_lengths[2]==expected_width);
  char digest[65];digest_of(expected_width,0,digest);assert(!strcmp(first,digest));
  assert(zk_release_source_snapshot(7,&ctx,&source) && closes==1);
 } else {
  assert(!selected && closes==1 && !source.prepared);
  if(scenario==4 || scenario==11)assert(!counts && reads==2 && read_lengths[1]==49);
  else if(expected==SOURCE_SNAPSHOT_RETRY)assert(counts==1 && reads==1);
 }
}
static void check_fields(void){
 const uint32_t n=12000;build_log(49,n,0);
 const uint8_t *data=log_bytes;size_t len=4+(size_t)n*49;
 uint8_t prefix[40];memcpy(prefix,data+4,40);
 uint32_t t49=0,t40=0;
 assert(attendance_record_timestamp(data+4,49,&t49) && attendance_record_timestamp(prefix,40,&t40) && t49==t40);
 for(uint32_t size=41;size<=64;++size)if(size!=49)assert(!attendance_record_timestamp(data+4,size,&t49));
 /* Month bounds walk the whole log at the 49-byte stride. */
 int year=0,month=0;
 assert(find_attendance_month_bounds(data,len,49,2026,12,&year,&month) && year==2024 && month==1);
 assert(find_next_attendance_month(data,len,49,2024,2,2026,12,&year,&month) && year==2024 && month==2);
 assert(find_next_attendance_month(data,len,49,2026,11,2026,12,&year,&month) && year==2026 && month==11);
 assert(!find_next_attendance_month(data,len,49,2027,1,2027,12,&year,&month));
 user_table_t users={0};attendance_event_t event;uint32_t timestamp=0;
 for(uint32_t i=0;i<n;i+=997){
  const uint8_t *row=data+4+(size_t)i*49;builds=0;
  assert(parse_attendance_record(row,49,&users,&event,&timestamp) && builds==1);
  char expected_uid[16];snprintf(expected_uid,sizeof(expected_uid),"%u",(unsigned)((i+1)&0xffff));
  assert(!strcmp(event.user_id,"1234") && !strcmp(event.attendance_record_uid,expected_uid));
  assert(timestamp==read_le32(row+27) && !event.cnic[0] && !event.raw_punch);
 }
 uint8_t missing[49];memcpy(missing,data+4,49);memset(missing+2,' ',24);builds=0;
 assert(!parse_attendance_record(missing,49,&users,&event,&timestamp) && !builds);
 assert(zkt_record_identity_missing(missing,49));
 assert(!parse_attendance_record(data+4,48,&users,&event,&timestamp) && !builds);
}
int main(int argc,char **argv){
 assert(argc==3);legacy_framing=atoi(argv[2])!=0;
 int scenario=atoi(argv[1]);
 if(scenario<0)check_fields();else check_snapshot((unsigned)scenario);
 free(log_bytes);return 0;
}
'''
    unit = tmp_path / "records49.c"
    unit.write_text(program)
    binary = tmp_path / "records49"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    str(unit), str(MAIN / "zkt_record.c"), "-o", str(binary)], check=True)
    return binary


@pytest.mark.parametrize("legacy_framing", [0, 1])
@pytest.mark.parametrize("scenario", [-1, *range(13)])
def test_49_byte_source_range_uses_full_record_evidence_and_prefix_fields(
        records49_binary, scenario, legacy_framing):
    subprocess.run([str(records49_binary), str(scenario), str(legacy_framing)], check=True, timeout=30)
