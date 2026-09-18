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
#include <string.h>
static unsigned writes;
static bool custody_ok = true;
/* Deterministic digest stand-in: test ordering/custody, not SHA's implementation. */
static void mbedtls_sha256(const unsigned char *body,size_t n,unsigned char *out,int mode){
 (void)mode; memset(out,0,32); for(size_t i=0;i<n;i++)out[i%32]^=body[i];
}
static bool preserve(const char *channel,const char *body,size_t n){
 assert(!strcmp(channel,"POLL"));assert(body && n);++writes;return custody_ok;
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
 return 0;
}
'''
    unit = tmp_path / "poll-anchor.c"
    unit.write_text(harness.replace("/* PRODUCTION */", body))
    exe = tmp_path / "poll-anchor"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
