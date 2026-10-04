"""Run rollback intent persistence and the real OTA coordinator at failure boundaries."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def run(tmp_path, program, sources=(), family=0):
    unit = tmp_path / "rollback.c"
    unit.write_text(program)
    binary = tmp_path / "rollback"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        f"-DZONE_LITE_HIKVISION={family}", "-I", str(tmp_path), "-I", str(MAIN), str(unit),
        *(str(MAIN / source) for source in sources), str(MAIN / "durable_queue.c"),
        "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)


def test_intent_is_committed_and_read_back_before_selection(tmp_path):
    (tmp_path / "nvs.h").write_text('''#pragma once
#include <stddef.h>
typedef int nvs_handle_t;
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READWRITE 1
int nvs_open(const char *,int,int *);
int nvs_get_blob(int,const char *,void *,size_t *);
int nvs_set_blob(int,const char *,const void *,size_t);
int nvs_commit(int);
void nvs_close(int);
''')
    (tmp_path / "esp_timer.h").write_text('#include <stdint.h>\nint64_t esp_timer_get_time(void);\n')
    program = r'''
#include "zkt_rollback.h"
#include <assert.h>
#include <stdio.h>
static ota_checkpoint_t stored,pending;
static unsigned fail,reads,writes,commits,closes;
static uint64_t now=1;
static bool present=true;
int64_t esp_timer_get_time(void){return now;}
int nvs_open(const char *name,int mode,int *handle)
{assert(!strcmp(name,"zone_ota") && mode==1);*handle=1;return fail==1?-7:0;}
int nvs_get_blob(int h,const char *key,void *out,size_t *length)
{
    assert(h==1 && !strcmp(key,"journal_v1") && *length==sizeof(stored));++reads;
    if(fail==2 || (fail==7 && reads==2))return -7;
    if(!present)return 1;
    memcpy(out,&stored,sizeof(stored));
    if(fail==8 && reads==2)((ota_checkpoint_t *)out)->crc^=1;
    if(fail==9 && reads==2)--*length;
    if(fail==10)now=1000;
    return 0;
}
int nvs_set_blob(int h,const char *key,const void *in,size_t length)
{assert(h==1 && !strcmp(key,"journal_v1") && length==sizeof(stored));++writes;pending=*(const ota_checkpoint_t *)in;return fail==3?-7:0;}
int nvs_commit(int h)
{assert(h==1);++commits;if(fail==4)return -7;stored=pending;return fail==5?-7:0;}
void nvs_close(int h){assert(h==1);++closes;}
static ota_checkpoint_t request(void)
{
    ota_checkpoint_t c={.version=1,.generation=7};
    strcpy(c.journal.deployment_id,"synthetic-operation");strcpy(c.journal.release_id,"synthetic-reader");
    strcpy(c.journal.target_version,"2.6.16");memset(c.journal.image_sha256,'1',64);
    strcpy(c.journal.download_url,"https://example.invalid/unused");strcpy(c.journal.state,"DOWNLOADING");
    c.journal.image_size=131072;c.crc=dq_crc32(&c,offsetof(ota_checkpoint_t,crc));return c;
}
static void reset(void){stored=request();pending=(ota_checkpoint_t){0};reads=writes=commits=closes=fail=0;now=1;present=true;}
int main(void)
{
    reset();ota_checkpoint_t expected=stored,out;int error;
    assert(zj_rollback_request_valid(&expected));
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_OK);
    assert(!error && writes==1 && commits==1 && closes==1 && out.generation==8);
    assert(!strcmp(out.journal.state,"READER_INTENT") && !out.journal.bytes_written && ota_checkpoint_valid(&out));
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_OK && writes==1);
    for(unsigned fault=1;fault<=10;++fault){
        if(fault==6)continue;
        reset();expected=stored;fail=fault;
        zj_result_t result=zj_rollback_commit_intent(&expected,1000,&out,&error);
        assert(result!=ZJ_OK && !out.version);
        if(fault==1 || fault==2)assert(!writes && result==ZJ_IO);
        else if(fault==10)assert(!writes && result==ZJ_STALE);
        else assert(result==ZJ_UNCERTAIN);
        fail=0;now=1;reads=0;
        unsigned prior_writes=writes;
        assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_OK);
        assert(out.generation==8);
        if(fault==5 || (fault>=7 && fault<=9))assert(writes==prior_writes);
    }
    reset();expected=stored;present=false;
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_CORRUPT && !writes);
    reset();expected=stored;stored.crc^=1;
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_CORRUPT && !writes);
    reset();expected=stored;strcpy(stored.journal.deployment_id,"new-operation");stored.crc=dq_crc32(&stored,offsetof(ota_checkpoint_t,crc));
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_STALE && !writes);
    reset();expected=stored;++stored.generation;stored.crc=dq_crc32(&stored,offsetof(ota_checkpoint_t,crc));
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_STALE && !writes);
    reset();stored.generation=UINT32_MAX;stored.crc=dq_crc32(&stored,offsetof(ota_checkpoint_t,crc));expected=stored;
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_FULL && !writes);
    reset();expected=stored;now=1000;
    assert(zj_rollback_commit_intent(&expected,1000,&out,&error)==ZJ_STALE && !writes && !reads);
    reset();expected=stored;expected.journal.bytes_written=16;expected.crc=dq_crc32(&expected,offsetof(ota_checkpoint_t,crc));
    assert(!zj_rollback_request_valid(&expected));
    expected=request();strcpy(expected.journal.state,"READER_INTENT");expected.crc=dq_crc32(&expected,offsetof(ota_checkpoint_t,crc));
    assert(zj_rollback_request_valid(&expected));strcpy(expected.journal.target_version,"2.7.0");
    assert(!ota_journal_valid(&expected.journal));
    puts("Rollback intent replay, corruption, stale operation and interrupted commit checks passed");
}
'''
    run(tmp_path, program, ["zkt_rollback.c"])


@pytest.mark.parametrize("family", [0, 1])
def test_ota_coordinator_waits_and_verifies_exact_bridge_after_reboot(tmp_path, family):
    source = (MAIN / "ota_manager.c").read_text()
    start = source.index("static bool advance_reader_rollback(void)\n{")
    production = source[start:source.index("static bool perform_update(void)\n{", start)]
    program = r'''
#include "zkt_storage_owner.h"
#include "zkt_rollback.h"
#include <assert.h>
#include <stdio.h>
static ota_journal_t s_journal,s_committed_journal;
static uint32_t s_journal_generation=7;
static bool s_busy;
#if !ZONE_LITE_HIKVISION
typedef struct {char project_name[32],version[32];} esp_app_desc_t;
static esp_app_desc_t app={"zone_lite","2.7.0"};
static char s_running_image_digest[65],s_last_error[64];
static uint64_t s_reader_ticket;
static unsigned claims,drains,submits,polls,restarts,saves;
static bool claimed,drained,admitted=true,complete,polled=true,save_ok=true,cache_ok=true,reply_valid=true;
static zj_result_t result=ZJ_OK;
static zj_compat_result_t compatibility=ZJ_COMPAT_OK;
static ota_checkpoint_t accepted;
static const esp_app_desc_t *esp_app_get_description(void){return &app;}
static size_t strlcpy(char *out,const char *in,size_t n){size_t l=strlen(in);assert(l<n);memcpy(out,in,l+1);return l;}
static bool cache_running_image_digest(void){return cache_ok;}
static bool save_journal(void){++saves;if(!save_ok){s_journal=s_committed_journal;return false;}s_committed_journal=s_journal;++s_journal_generation;return true;}
static bool add_connector_claim_ota_restart(void){++claims;return claimed;}
bool zj_owner_quiesce(void){assert(claimed);++drains;return drained;}
bool zj_owner_select_quiesced_reader(const ota_checkpoint_t *expected,uint64_t *ticket)
{assert(claimed&&drained&&s_busy);++submits;if(!admitted)return false;accepted=*expected;*ticket=11;return true;}
bool zj_owner_poll(uint64_t ticket,zj_reply_t *reply,bool *done)
{
    assert(ticket==11 && claimed && drained);++polls;*done=false;if(!polled)return false;if(!complete)return true;
    memset(reply,0,sizeof(*reply));reply->result=result;reply->compatibility=compatibility;
    reply->rollback_intent=accepted;++reply->rollback_intent.generation;
    strcpy(reply->rollback_intent.journal.state,"READER_INTENT");
    if(!reply_valid)strcpy(reply->rollback_intent.journal.deployment_id,"wrong-operation");
    reply->rollback_intent.crc=dq_crc32(&reply->rollback_intent,offsetof(ota_checkpoint_t,crc));
    *done=true;return true;
}
const char *zj_compat_error(zj_compat_result_t code){assert(code!=ZJ_COMPAT_OK);return "selection-error";}
bool zj_rollback_request_valid(const ota_checkpoint_t *c)
{return ota_checkpoint_valid(c) && !strcmp(c->journal.target_version,"2.6.16") && !c->journal.bytes_written;}
bool zj_rollback_same_target(const ota_checkpoint_t *a,const ota_checkpoint_t *b)
{return zj_rollback_request_valid(a)&&zj_rollback_request_valid(b)&&!strcmp(a->journal.deployment_id,b->journal.deployment_id)&&!strcmp(a->journal.image_sha256,b->journal.image_sha256);}
static void esp_restart(void){assert(claimed&&drained&&complete&&s_busy);++restarts;}
#endif
/* PRODUCTION */
static void reset(void)
{
    memset(&s_journal,0,sizeof(s_journal));strcpy(s_journal.deployment_id,"synthetic-operation");
    strcpy(s_journal.release_id,"synthetic-reader");strcpy(s_journal.target_version,"2.6.16");
    strcpy(s_journal.download_url,"https://example.invalid/unused");memset(s_journal.image_sha256,'1',64);
    strcpy(s_journal.state,"DOWNLOADING");s_journal.image_size=131072;s_committed_journal=s_journal;
    s_journal_generation=7;s_busy=false;
#if !ZONE_LITE_HIKVISION
    strcpy(app.version,"2.7.0");strcpy(app.project_name,"zone_lite");
    memset(s_running_image_digest,'1',64);s_reader_ticket=claims=drains=submits=polls=restarts=saves=0;
    claimed=drained=complete=false;admitted=polled=save_ok=cache_ok=reply_valid=true;result=ZJ_OK;compatibility=ZJ_COMPAT_OK;
#endif
}
int main(void)
{
    reset();
#if ZONE_LITE_HIKVISION
    assert(advance_reader_rollback() && !s_busy);
#else
    assert(!advance_reader_rollback() && s_busy && claims==1 && !drains && !submits);
    claimed=true;assert(!advance_reader_rollback() && drains==1 && !submits);
    drained=true;assert(!advance_reader_rollback() && submits==1 && s_reader_ticket==11);
    polled=false;for(unsigned i=0;i<100;++i)assert(!advance_reader_rollback());
    assert(submits==1 && s_reader_ticket==11 && !restarts);
    polled=true;assert(!advance_reader_rollback() && submits==1 && !restarts);
    complete=true;assert(!advance_reader_rollback() && restarts==1 && !s_reader_ticket && !saves);
    assert(!strcmp(s_committed_journal.state,"READER_INTENT"));
    strcpy(app.version,"2.6.16");assert(advance_reader_rollback() && !s_busy && saves==1);
    assert(!strcmp(s_committed_journal.state,"READY_TO_BOOT") && s_journal.bytes_written==s_journal.image_size);
    reset();claimed=drained=true;assert(!advance_reader_rollback());complete=true;reply_valid=false;
    assert(!advance_reader_rollback() && !restarts && !strcmp(s_last_error,"JOURNAL_READER_INTENT_REPLY_INVALID"));
    reset();claimed=drained=true;assert(!advance_reader_rollback());complete=true;result=ZJ_UNCERTAIN;
    assert(!advance_reader_rollback() && !restarts && !s_reader_ticket && s_busy);
    result=ZJ_OK;complete=false;assert(!advance_reader_rollback() && submits==2);
    complete=true;compatibility=ZJ_COMPAT_SELECTION_UNCERTAIN;result=ZJ_INVALID;
    assert(!advance_reader_rollback() && !restarts && !strcmp(s_last_error,"selection-error"));
    reset();strcpy(s_journal.state,"READER_INTENT");s_committed_journal=s_journal;
    strcpy(app.version,"2.6.16");s_running_image_digest[0]='2';
    assert(!advance_reader_rollback() && !saves && !claims && s_busy);
    s_running_image_digest[0]='1';cache_ok=false;assert(!advance_reader_rollback()&&!saves);
    cache_ok=true;save_ok=false;assert(!advance_reader_rollback() && !strcmp(s_journal.state,"READER_INTENT"));
    save_ok=true;assert(advance_reader_rollback() && !s_busy);
    reset();strcpy(s_journal.state,"READER_INTENT");s_committed_journal=s_journal;strcpy(app.version,"2.6.15");
    assert(!advance_reader_rollback() && !claims && !restarts && !saves);
    reset();claimed=drained=true;admitted=false;assert(!advance_reader_rollback() && !s_reader_ticket && !restarts);
    reset();s_journal_generation=0;assert(!advance_reader_rollback()&&!claims);
    reset();strcpy(app.version,"2.6.15");assert(advance_reader_rollback()&&!s_busy&&!claims);
    reset();strcpy(s_journal.target_version,"2.7.0");assert(advance_reader_rollback()&&!s_busy&&!claims);
#endif
    puts("Rollback coordinator handoff, retained replies and exact post-boot evidence passed");
}
'''
    run(tmp_path, program.replace("/* PRODUCTION */", production), family=family)
