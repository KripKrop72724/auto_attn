"""Exercise the actual command replacement with ESP-IDF cJSON and faulting NVS."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
firmware = ROOT / "firmware/zone_lite/main"
source = (firmware / "add_connector.c").read_text()
helpers = source[source.index("static int command_transaction_load("):source.index("static bool command_is_scheduled_locked(")]
complete = source[source.index("bool add_connector_command_complete("):source.index("bool add_connector_lookup_identity(")]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>
#include "cJSON.h"
#include "file_transaction.h"
#include "reliability.h"
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
#define LED_STATUS_LOCAL_FAILURE 1
#define QS_ADMIT_RECOVERY 2
#define ADD_COMMAND_LINE_BYTES 512
#define ADD_COMMAND_INBOX_MAX_BYTES 65536
#define ADD_COMMAND_INBOX_PATH "active"
#define ADD_COMMAND_INBOX_TMP_PATH "stage"
#define ADD_COMMAND_INBOX_BACKUP_PATH "backup"
typedef int nvs_handle_t;
typedef int esp_err_t;
static ft_checkpoint_t saved, pending;
static bool deny_budget, held, admitted, fail_commit;
static char s_running_command_id[80];
static void *s_command_lock=(void *)1;
static size_t calls, fail_at;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
#define malloc allocate
static int xSemaphoreTake(void *m,int timeout){(void)m;(void)timeout;assert(!held);held=true;return 1;}
static void xSemaphoreGive(void *m){(void)m;assert(held);held=false;}
static void led_status_fault(int state){assert(state==1);}
static bool qs_local_begin(int policy,size_t bytes){assert(held && policy==2 && bytes<=65536);admitted=!deny_budget;return admitted;}
static void qs_local_end(bool ok,int error){(void)ok;(void)error;assert(admitted);admitted=false;}
static void command_unmark_queued_locked(const char *id){assert(held && id[0]);}
static char *copy(const char *s){if(!s)return NULL;char *r=malloc(strlen(s)+1);if(r)strcpy(r,s);return r;}
static char *decrypt_storage_line(const char *s){return copy(s);}
static char *encrypt_storage_json(const char *s){return copy(s);}
static int nvs_open(const char *s,int mode,int *handle){assert(!strcmp(s,"file_tx"));(void)mode;*handle=1;return 0;}
static void nvs_close(int h){assert(h==1);}
static int nvs_get_blob(int h,const char *k,void *out,size_t *n)
{assert(h==1 && !strcmp(k,"commands") && *n==sizeof(saved));memcpy(out,&saved,sizeof(saved));return saved.version?0:1;}
static int nvs_set_blob(int h,const char *k,const void *in,size_t n)
{assert(h==1 && !strcmp(k,"commands") && n==sizeof(pending));pending=*(const ft_checkpoint_t *)in;return 0;}
static int nvs_commit(int h){assert(h==1);if(fail_commit)return -1;saved=pending;return 0;}
''' + helpers + complete + r'''
static const char *rows="{\"command_id\":\"A\"}\n{\"command_id\":\"B\"}\n";
static void seed(void){saved=(ft_checkpoint_t){0};(void)remove("backup");(void)remove("stage");FILE *f=fopen("active","wb");assert(f);assert(fputs(rows,f)>=0);assert(!fclose(f));strcpy(s_running_command_id,"A");}
static bool contents(const char *expected){FILE *f=fopen("active","rb");assert(f);char buf[512]={0};size_t n=fread(buf,1,511,f);assert(!ferror(f));assert(!fclose(f));return n==strlen(expected) && !strcmp(buf,expected);}
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 seed();calls=0;assert(add_connector_command_complete("A"));size_t total=calls;
 assert(contents("{\"command_id\":\"B\"}\n") && !held && !admitted);
 for(size_t i=1;i<=total;i++){
  seed();calls=0;fail_at=i;assert(!add_connector_command_complete("A"));
  assert(contents(rows) && !held && !admitted);fail_at=0;
  assert(add_connector_command_complete("A"));assert(contents("{\"command_id\":\"B\"}\n"));
 }
 seed();deny_budget=true;assert(!add_connector_command_complete("A"));assert(contents(rows));deny_budget=false;
 seed();fail_commit=true;assert(!add_connector_command_complete("A"));assert(contents(rows));fail_commit=false;
 assert(add_connector_command_complete("A"));assert(contents("{\"command_id\":\"B\"}\n"));
 seed();calls=0;fail_at=1;assert(command_journal_contains_locked("A")==-1);fail_at=0;
 cJSON *new_command=cJSON_Parse("{\"command_id\":\"C\"}");assert(new_command);
 assert(command_journal_append(new_command,"C"));cJSON_Delete(new_command);
 assert(command_journal_contains_locked("C")==1);
 puts("command journal allocation and persistence regressions passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "commands.c"
    unit.write_text(program)
    executable = temporary / "commands"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(cjson), "-I", str(firmware),
                    str(unit), str(cjson / "cJSON.c"), str(firmware / "file_transaction.c"),
                    str(firmware / "durable_queue.c"), str(firmware / "reliability.c"), "-lm", "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True)
