"""Run production blocked-file recovery against actual persistent queue code."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_blocked_generations_drain_without_copying_and_survive_restart(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    start = source.index("static bool restore_blocked_backup_if_needed(")
    end = source.index("static bool recover_blocked_events_from_snapshot(", start)
    harness = r'''
#include "legacy_queue.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#define BLOCKED_PATH "blocked.jsonl"
#define BLOCKED_RECOVERY_BACKUP_PATH "blocked.bak"
#define BLOCKED_RECOVERY_TMP_PATH "blocked.tmp"
static lq_checkpoint_t saved;
static bool exists, fail_commit;
/* PRODUCTION */
static int legacy_pending_load(void *context,lq_checkpoint_t *cp)
{ assert(!strcmp(context,"blocked"));*cp=saved;return exists; }
static bool legacy_pending_commit(void *context,const lq_checkpoint_t *cp)
{ assert(!strcmp(context,"blocked"));if(fail_commit)return false;saved=*cp;exists=true;return true; }
static void append(const char *path,const char *row)
{ FILE *f=fopen(path,"a");assert(f);assert(fprintf(f,"%s\n",row)>0);assert(!fclose(f)); }
int main(void)
{
    for(unsigned i=0;i<10000;++i)append(BLOCKED_PATH,"preserved-row");
    append(BLOCKED_RECOVERY_BACKUP_PATH,"backup");
    append(BLOCKED_RECOVERY_TMP_PATH,"temporary");
    struct stat st;assert(!stat(BLOCKED_PATH,&st) && st.st_size>65536);
    const long original=st.st_size;
    char row[128];lq_token_t token;
    assert(read_blocked_locked(row,sizeof(row),&token)==DQ_OK);
    // A failed delivery does not settle anything; restart replays the same row.
    lq_token_t first=token;g_legacy_blocked.ready=false;
    assert(read_blocked_locked(row,sizeof(row),&token)==DQ_OK);
    assert(first.offset==token.offset && first.crc==token.crc);
    fail_commit=true;assert(!settle_blocked_locked(&token, true));
    fail_commit=false;g_legacy_blocked.ready=false;
    assert(read_blocked_locked(row,sizeof(row),&token)==DQ_OK && token.offset==0);
    assert(settle_blocked_locked(&token, true));
    assert(!stat(BLOCKED_PATH,&st) && st.st_size==original); // no rewritten copy
    assert(!stat(BLOCKED_RECOVERY_BACKUP_PATH,&st));
    assert(!stat(BLOCKED_RECOVERY_TMP_PATH,&st));
    // Append between read and commit; new live data must remain pending.
    assert(read_blocked_locked(row,sizeof(row),&token)==DQ_OK);
    append(BLOCKED_PATH,"concurrent");assert(settle_blocked_locked(&token, true));
    unsigned rows=2;
    while(read_blocked_locked(row,sizeof(row),&token)==DQ_OK){
        assert(settle_blocked_locked(&token, true));++rows;
        if(rows%997==0)g_legacy_blocked.ready=false;
        assert(rows<=10003);
    }
    assert(rows==10003);
    assert(stat(BLOCKED_PATH,&st)!=0 && errno==ENOENT);
    assert(stat(BLOCKED_RECOVERY_BACKUP_PATH,&st)!=0 && errno==ENOENT);
    assert(stat(BLOCKED_RECOVERY_TMP_PATH,&st)!=0 && errno==ENOENT);
    // A truncated tail is preserved, never mistaken for empty or retired.
    FILE *f=fopen(BLOCKED_PATH,"w");assert(f);assert(fputs("partial",f)>=0);assert(!fclose(f));
    assert(read_blocked_locked(row,sizeof(row),&token)==DQ_OK && token.evidence_required);
    assert(!settle_blocked_locked(&token, false));
    assert(!stat(BLOCKED_PATH,&st) && st.st_size==7);
    assert(settle_blocked_locked(&token, true));
    assert(stat(BLOCKED_PATH,&st)!=0 && errno==ENOENT);
    return 0;
}
'''
    unit = tmp_path / "blocked.c"
    unit.write_text(harness.replace("/* PRODUCTION */", source[start:end]))
    executable = tmp_path / "blocked"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "legacy_queue.c"), str(firmware / "durable_queue.c"),
        "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
