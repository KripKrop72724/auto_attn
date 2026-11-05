"""The source anchor must be checked before any candidate enters durable custody."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_inclusive_poll_anchor_gates_durable_records(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/hikvision_runtime.c").read_text()
    start = source.index("typedef struct {\n    unsigned char digest[32];")
    body = source[start:source.index("static void poll_task(", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef enum {HIK_OK,HIK_CONFIGURATION,HIK_NETWORK,HIK_AUTH,HIK_HTTP_STATUS,HIK_OVERSIZED,HIK_BINDING,HIK_PARSE,HIK_CUSTODY} hik_result_t;
/* terminal 0: 1000 events at sparse serials 5000, 5003, ... 7997.
 * terminal 1/2: Mirpur-02 shape, dense serials 1..newest with a committed
 * anchor at 23088 and 1000 waiting; 1 counts beginSerialNo inclusively,
 * 2 exclusively. fail_at makes that count call fail like a slow terminal.
 * terminal 3: dense serials 1..23100, then 2000 more starting gap serials
 * later (inclusive begin). terminal 4: 1000 events at 1e5, 2e5, ... 1e8. */
static unsigned count_calls, terminal, fail_at;
static uint32_t newest = 24088, gap;
static uint32_t span(uint32_t lo,uint32_t hi,uint32_t a,uint32_t b){
 if(lo<a)lo=a;
 if(hi>b)hi=b;
 return hi>=lo?hi-lo+1:0;
}
static hik_result_t hik_history_count(uint32_t begin,uint32_t end,uint32_t *total){
 ++count_calls;*total=0;
 if(fail_at && count_calls==fail_at)return HIK_NETWORK;
 if(!terminal){for(uint32_t s=5000;s<8000;s+=3)if(s>=begin&&s<=end)++*total;return HIK_OK;}
 if(terminal==3){*total=span(begin,end,1,23100)+span(begin,end,23100+gap,23100+gap+1999);return HIK_OK;}
 if(terminal==4){uint32_t a=begin<100000?1:(begin+99999)/100000,b=end/100000;if(b>1000)b=1000;*total=b>=a?b-a+1:0;return HIK_OK;}
 uint32_t lo=terminal==1?begin:begin+1, hi=end<newest?end:newest;
 *total=hi>=lo?hi-lo+1:0;return HIK_OK;
}
static unsigned hik_history_last_request_ms(void){return 10000+count_calls;}
static unsigned writes;
static bool custody_ok = true;
/* Deterministic digest stand-in: test ordering/custody, not SHA's implementation. */
static void mbedtls_sha256(const unsigned char *body,size_t n,unsigned char *out,int mode){
 (void)mode; memset(out,0,32); for(size_t i=0;i<n;i++)out[i%32]^=body[i];
}
static bool preserve(const char *channel,const char *body,size_t n){
 assert(!strcmp(channel,"POLL"));assert(body && n);++writes;return custody_ok;
}
static bool immutable(const char *body,size_t n,unsigned char out[32]){
 (void)n;const char *facts=strncmp(body,"committed source record",23)?"different facts":"immutable facts";
 mbedtls_sha256((const unsigned char*)facts,strlen(facts),out,0);return true;
}
/* PRODUCTION */
static unsigned searches_for(uint32_t from,uint32_t *end);
int main(void){
 const char *anchor="committed source record", *fresh="new source record";
 unsigned char expected[32], next[32];
 mbedtls_sha256((const unsigned char *)anchor,strlen(anchor),expected,0);
 mbedtls_sha256((const unsigned char *)fresh,strlen(fresh),next,0);
 page_context_t p={.expected_anchor=expected};
 assert(poll_record(&p,anchor,strlen(anchor)));
 assert(p.anchor_seen && !p.anchor_conflict && !p.count && !writes);
 assert(poll_record(&p,fresh,strlen(fresh)));
 assert(p.count==1 && writes==1 && !memcmp(p.digest,next,32));
 /* A reused identifier/retention loss cannot admit the first mismatching row. */
 p=(page_context_t){.expected_anchor=expected};writes=0;
 assert(!poll_record(&p,fresh,strlen(fresh)));
 assert(p.anchor_conflict && !p.anchor_seen && !writes && !p.count);
 /* A fresh installation has no predecessor and admits its bounded tail. */
 p=(page_context_t){0};assert(poll_record(&p,fresh,strlen(fresh)));
 assert(writes==1 && p.count==1);
 /* A failed durable append cannot update the candidate checkpoint digest. */
 p=(page_context_t){.expected_anchor=expected};
 assert(poll_record(&p,anchor,strlen(anchor)));custody_ok=false;
 assert(!poll_record(&p,fresh,strlen(fresh)));assert(!p.count);
 custody_ok=true;writes=0;
 unsigned char fixed[32];immutable(anchor,strlen(anchor),fixed);
 const char *presentation="committed source record changed picture URL";
 p=(page_context_t){.expected_anchor=expected,.legacy_anchor=true,.digest_record=immutable};
 assert(!poll_record(&p,presentation,strlen(presentation))&&!writes); /* no unproved upgrade */
 p=(page_context_t){.expected_anchor=expected,.approved_anchor=fixed,.legacy_anchor=true,.digest_record=immutable};
 assert(poll_record(&p,presentation,strlen(presentation))&&p.legacy_upgraded&&writes==1&&!p.count);
 assert(!memcmp(p.digest,fixed,32)); /* new checkpoint can commit without a new punch */
 p=(page_context_t){.expected_anchor=expected,.approved_anchor=fixed,.legacy_anchor=true,.digest_record=immutable};writes=0;
 assert(!poll_record(&p,fresh,strlen(fresh))&&!writes); /* changed facts remain blocked */
 p=(page_context_t){.expected_anchor=fixed,.digest_record=immutable};
 assert(poll_record(&p,presentation,strlen(presentation))&&!p.legacy_upgraded&&!writes);
 /* 3.1.2 unordered ranges: the anchor is found by serial, not position, and
  * the checkpoint follows the highest admitted serial, not the last row. */
 writes=0;
 range_context_t r={.page={.expected_anchor=expected},.anchor_serial=3199};
 assert(anchor_row(&r,fresh,strlen(fresh),3198)&&!r.page.anchor_seen&&!writes); /* neighbour */
 assert(anchor_row(&r,anchor,strlen(anchor),3199)&&r.page.anchor_seen&&!writes);
 assert(anchor_row(&r,fresh,strlen(fresh),3199)&&!writes); /* already proved */
 r.page.expected_anchor=NULL;
 const char *a="boot record 3852",*b="boot record 3290",*c="punch 3900";
 unsigned char top[32];mbedtls_sha256((const unsigned char *)c,strlen(c),top,0);
 assert(range_row(&r,anchor,strlen(anchor),3199)&&!writes); /* the anchor again */
 assert(range_row(&r,a,strlen(a),3852)&&range_row(&r,b,strlen(b),3290));
 assert(range_row(&r,c,strlen(c),3900)&&range_row(&r,"punch 3300",10,3300));
 assert(writes==4&&r.page.count==4&&r.max_serial==3900&&!memcmp(r.max_digest,top,32));
 /* A wrong anchor admits nothing, wherever it sorts. */
 r=(range_context_t){.page={.expected_anchor=expected},.anchor_serial=3199};writes=0;
 assert(!anchor_row(&r,fresh,strlen(fresh),3199)&&r.page.anchor_conflict&&!writes);
 /* The anchor row met again in the main read must carry the proved facts. */
 r=(range_context_t){.anchor_serial=3199,.recheck_anchor=true,.page={.digest_record=immutable}};
 immutable(anchor,strlen(anchor),r.anchor_stable);writes=0;
 assert(range_row(&r,anchor,strlen(anchor),3199)&&!writes&&!r.page.anchor_conflict);
 assert(!range_row(&r,fresh,strlen(fresh),3199)&&r.page.anchor_conflict&&!writes);
 /* A backlog is cut into a window of exactly 400 events; a smaller multi-page
  * tail ends at its newest serial so new punches stay outside the window; a
  * single page is read whole. Sparse serials stay within the search cap. */
 uint32_t end=0,inside=0;
 assert(searches_for(5000,&end)<=40&&end<3000000000U);
 hik_history_count(5000,end,&inside);assert(inside==400);
 hik_history_count(5000,end-1,&inside);assert(inside==399);
 assert(searches_for(7000,&end)<=40&&end==7997);
 hik_history_count(7000,end,&inside);assert(inside==333);
 assert(searches_for(7950,&end)==1&&end==3000000000U);
 /* 3.1.6: a dense backlog of 1000 is sized in at most 3 searches (3.1.5 made
  * ~33), and the window is still the smallest one holding 400 counted rows. */
 for(terminal=1;terminal<=2;terminal++){
  unsigned used=searches_for(23088,&end);
  assert(used==(terminal==1?2u:3u));
  hik_history_count(23088,end,&inside);assert(inside==400);
  hik_history_count(23088,end-1,&inside);assert(inside==399);
  assert(end==(terminal==1?23487u:23488u));
  /* A small multi-page tail ends exactly at the newest serial, so a punch
   * arriving mid-read (serial newest+1) cannot change the window total. */
  newest=23118;
  assert(searches_for(23088,&end)<=3&&end==newest);
  newest=23100;assert(searches_for(23088,&end)==1&&end==3000000000U);
  newest=24088;
 }
 /* Sparse serials always converge to the smallest end (no search cap): a
  * serial gap of up to 2e9 above the anchor, or 1 serial in 1e5, still gives
  * a window of exactly 400, so it never turns permanently OVERSIZED. */
 terminal=3;
 static const uint32_t gaps[]={1,1000,100000,100000000,1000000000,2000000000};
 for(unsigned i=0;i<sizeof(gaps)/sizeof(gaps[0]);i++){
  gap=gaps[i];
  assert(searches_for(23088,&end)<=44);
  hik_history_count(23088,end,&inside);assert(inside==400);
  hik_history_count(23088,end-1,&inside);assert(inside==399);
 }
 terminal=4;
 assert(searches_for(100000,&end)<=44&&end==40000000);
 hik_history_count(100000,end-1,&inside);assert(inside==399);
 /* A failing sub-request is named with its elapsed time. */
 terminal=2;
 const char *names[]={"count0","probe","probe#2"};
 for(unsigned i=0;i<3;i++){
  poll_request_t q={0};fail_at=count_calls+1+i;
  assert(poll_window_end(23088,&end,&q)==HIK_NETWORK);
  assert(!strcmp(q.name,names[i])&&q.ms==10000+count_calls&&q.searches==i+1);
  char text[64];poll_request_text(text,sizeof(text),&q);
  char want[64];snprintf(want,sizeof(want),"; request=%s; request_ms=%u",names[i],10000+count_calls);
  assert(!strcmp(text,want));
 }
 fail_at=0;terminal=0;
 {poll_request_t q={0};char text[64];poll_request_note(&q,"page",7);
  poll_request_text(text,sizeof(text),&q);assert(!strncmp(text,"; request=page#7; request_ms=",29));
  q=(poll_request_t){0};poll_request_text(text,sizeof(text),&q);
  assert(!strncmp(text,"; request=none; request_ms=0",28));}
 /* A first install starts at the newest 20 serials, whatever the time order. */
 uint32_t tail=0;
 assert(poll_tail_begin(&tail)==HIK_OK);
 hik_history_count(tail,3000000000U,&inside);assert(inside==20);
 hik_history_count(tail-1,3000000000U,&inside);assert(inside==21);
 /* A failed durable append keeps the checkpoint where it was. */
 r=(range_context_t){.anchor_serial=10};custody_ok=false;
 assert(!range_row(&r,c,strlen(c),12)&&!r.max_serial);custody_ok=true;
 return 0;
}
static unsigned searches_for(uint32_t from,uint32_t *end){
 poll_request_t q={0};unsigned before=count_calls;
 assert(poll_window_end(from,end,&q)==HIK_OK);assert(q.searches==count_calls-before);
 return q.searches;
}
'''
    unit = tmp_path / "poll-anchor.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "poll-anchor"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_slow_poll_yields_one_bounded_background_slot(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/hikvision_runtime.c").read_text()
    start = source.index("static bool background_slot;")
    body = source[start:source.index("/* A single encrypted", start)]
    harness = """
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
typedef enum {HIK_OK,HIK_CONFIGURATION,HIK_NETWORK,HIK_AUTH,HIK_HTTP_STATUS,HIK_OVERSIZED,HIK_BINDING,HIK_PARSE,HIK_CUSTODY} hik_result_t;
/* PRODUCTION */
int main(void){
 assert(!take_background_slot());
 background_slot=true;
 assert(take_background_slot());assert(!take_background_slot());
 assert(poll_delay_ms(1000000)==1000);
 assert(poll_delay_ms(0)==2000);
 assert(poll_delay_ms(4800000)==250);
 assert(poll_delay_ms(7000000)==250);
 background_slot=true;assert(take_background_slot());
 /* 3.1.6 back-off: network/auth failures (the slow-terminal symptoms) at
  * any stage wait 2, 5, 15, 30, then 60 s; success resets. */
 unsigned failing=0;
 assert(poll_next_delay_ms(1000000,HIK_OK,&failing)==1000&&!failing);
 static const uint32_t expected[]={2000,5000,15000,30000,60000,60000,60000};
 for(unsigned i=0;i<7;i++){
  hik_result_t why=i%2?HIK_AUTH:HIK_NETWORK;
  assert(poll_next_delay_ms(10000000,why,&failing)==expected[i]&&failing==i+1);
 }
 assert(poll_next_delay_ms(0,HIK_OK,&failing)==2000&&!failing);
 assert(poll_next_delay_ms(7000000,HIK_OK,&failing)==250&&!failing);
 assert(poll_next_delay_ms(100000,HIK_NETWORK,&failing)==2000&&failing==1);
 assert(poll_next_delay_ms(100000,HIK_AUTH,&failing)==5000&&failing==2);
 assert(poll_next_delay_ms(100000,HIK_OK,&failing)==1900&&!failing);
 /* Deterministic rejects (events-stage PARSE/OVERSIZED, anchor BINDING,
  * CUSTODY, HTTP status) are not load symptoms: they keep the 2 s / 250 ms
  * cadence, so history, light audit and profile workers keep one slot about
  * every 2 s, and they neither escalate nor reset a running back-off. */
 static const hik_result_t steady[]={HIK_PARSE,HIK_OVERSIZED,HIK_BINDING,HIK_CUSTODY,HIK_HTTP_STATUS,HIK_CONFIGURATION};
 for(unsigned r=0;r<6;r++){
  failing=0;
  for(unsigned i=0;i<10;i++){
   assert(poll_next_delay_ms(1000000,steady[r],&failing)==1000&&!failing);
   assert(poll_next_delay_ms(7000000,steady[r],&failing)==250&&!failing);
  }
  assert(!poll_backs_off(steady[r]));
 }
 failing=0;
 assert(poll_next_delay_ms(100000,HIK_NETWORK,&failing)==2000&&failing==1);
 assert(poll_next_delay_ms(100000,HIK_PARSE,&failing)==1900&&failing==1);
 assert(poll_next_delay_ms(100000,HIK_AUTH,&failing)==5000&&failing==2);
 assert(poll_backoff_ms(0)==0&&poll_backoff_ms(1)==2000&&poll_backoff_ms(999)==60000);
 assert(poll_backs_off(HIK_NETWORK)&&poll_backs_off(HIK_AUTH)&&!poll_backs_off(HIK_OK));
 return 0;
}
"""
    unit = tmp_path / "poll-schedule.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "poll-schedule"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
