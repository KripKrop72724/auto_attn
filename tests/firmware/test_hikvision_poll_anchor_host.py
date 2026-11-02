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
#include <string.h>
typedef enum {HIK_OK,HIK_CONFIGURATION,HIK_NETWORK,HIK_AUTH,HIK_HTTP_STATUS,HIK_OVERSIZED,HIK_BINDING,HIK_PARSE,HIK_CUSTODY} hik_result_t;
/* A terminal holding 1000 events at serials 5000, 5003, ... 7997. */
static unsigned count_calls;
static hik_result_t hik_history_count(uint32_t begin,uint32_t end,uint32_t *total){
 ++count_calls;*total=0;for(uint32_t s=5000;s<8000;s+=3)if(s>=begin&&s<=end)++*total;return HIK_OK;
}
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
 /* A backlog is cut into a window of at most 400 events that is never empty;
  * a small one is read whole. */
 uint32_t end=0,inside=0;
 assert(poll_window_end(5000,&end)==HIK_OK&&end<3000000000U&&count_calls<40);
 hik_history_count(5000,end,&inside);assert(inside==400);
 hik_history_count(5000,end+1,&inside);assert(inside==401);
 assert(poll_window_end(7000,&end)==HIK_OK&&end==3000000000U);
 /* A failed durable append keeps the checkpoint where it was. */
 r=(range_context_t){.anchor_serial=10};custody_ok=false;
 assert(!range_row(&r,c,strlen(c),12)&&!r.max_serial);custody_ok=true;
 return 0;
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
 return 0;
}
"""
    unit = tmp_path / "poll-schedule.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "poll-schedule"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
