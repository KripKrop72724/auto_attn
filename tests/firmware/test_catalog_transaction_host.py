"""Execute catalog activation across uncertain NVS and filesystem boundaries."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_replacement_keeps_committed_generation(tmp_path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "add_connector.c").read_text()
    recovery = source[source.index("static bool recover_catalog_transaction_locked("):
                      source.index("static const char *s_catalog_writer_failure_reason")]
    activation = source[source.index("static bool activate_identity_catalog("):
                        source.index("static bool persist_identity_catalog_locked(")]
    program = r'''
#include "file_transaction.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#define ADD_IDENTITY_CATALOG_PATH "active"
#define ADD_IDENTITY_CATALOG_COMMIT_PATH "commit"
#define ADD_IDENTITY_CATALOG_BACKUP_PATH "backup"
#define ADD_IDENTITY_CATALOG_MAX_BYTES 1024
#define LED_STATUS_LOCAL_FAILURE 1
static ft_checkpoint_t saved;
static unsigned nvs_calls,nvs_fail,fs_calls,fs_fail;
static bool uncertain;
int ct_rename(const char *a,const char *b){if(++fs_calls==fs_fail){errno=EIO;return -1;}return renameat(AT_FDCWD,a,AT_FDCWD,b);}
int ct_remove(const char *a){if(++fs_calls==fs_fail){errno=EIO;return -1;}return unlink(a);}
static int load(void *c,ft_checkpoint_t *cp){(void)c;*cp=saved;return saved.version?1:0;}
static bool commit(void *c,const ft_checkpoint_t *cp){(void)c;if(++nvs_calls==nvs_fail){if(uncertain)saved=*cp;return false;}saved=*cp;return true;}
static const ft_port_t catalog_transaction_port={load,commit,NULL};
static void led_status_fault(int state){assert(state==1);}
''' + recovery + activation + r'''
static void seed(const char *path,const char *data){FILE *f=fopen(path,"w");assert(f && fputs(data,f)>=0);assert(!fflush(f) && !fsync(fileno(f)) && !fclose(f));}
static bool content(const char *path,const char *wanted){FILE *f=fopen(path,"r");if(!f)return false;char buf[32]={0};size_t n=fread(buf,1,31,f);assert(!fclose(f));return n==strlen(wanted) && !strcmp(buf,wanted);}
static void reset(void){fs_fail=nvs_fail=0;unlink("active");unlink("commit");unlink("backup");unlink("stage");saved=(ft_checkpoint_t){0};fs_calls=nvs_calls=0;}
int main(void){
 for(unsigned initial=0;initial<2;initial++)for(unsigned boundary=0;boundary<=3;boundary++)for(unsigned u=0;u<2;u++){
  reset();if(!initial)seed("active","old");seed("stage","new");nvs_fail=boundary;uncertain=u;
  (void)activate_identity_catalog("stage");
  assert(content("active","old") || content("active","new") || content("backup","old") || content("commit","new") || content("stage","new"));
  nvs_fail=0;assert(recover_catalog_transaction_locked());
  assert(content("active","old") || content("active","new"));
 }
 for(unsigned boundary=1;boundary<=6;boundary++){
  reset();seed("active","old");seed("stage","new");fs_fail=boundary;
  (void)activate_identity_catalog("stage");
  assert(content("active","old") || content("active","new") || content("backup","old"));
  fs_fail=0;assert(recover_catalog_transaction_locked());
  assert(content("active","old") || content("active","new"));
 }
 reset();seed("active","old");seed("backup","ambiguous");seed("stage","new");
 assert(!activate_identity_catalog("stage"));assert(content("active","old") && content("backup","ambiguous") && content("stage","new"));
 return 0;
}
'''
    unit = tmp_path / "catalog.c"
    unit.write_text(program)
    executable = tmp_path / "catalog"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Drename=ct_rename", "-Dremove=ct_remove",
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(firmware), str(unit), str(firmware / "file_transaction.c"), str(firmware / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
