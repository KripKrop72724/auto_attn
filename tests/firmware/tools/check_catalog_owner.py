"""Fault the actual ADD-to-owner catalog adapter with pinned cJSON and real files."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "add_connector.c").read_text()
helper_start = source.index("static zc_client_t s_catalog_client;")
helpers = source[helper_start:source.index("#endif\nstatic void remove_catalog_stage(", helper_start)]
helpers += source[source.index("static void remove_catalog_stage(const char *path)\n{"):
                  source.index("/* Catalog stream adapter:")]
program = r'''
#include "zkt_catalog_client.h"
#include "cJSON.h"
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define ADD_IDENTITY_CATALOG_TMP_PATH "temporary"
#define ADD_IDENTITY_CATALOG_STAGE_PATH "stage"
#define ADD_IDENTITY_CATALOG_MAX_ROWS 4096
#define pdMS_TO_TICKS(value) (value)
typedef struct { const char *project_name, *version; } esp_app_desc_t;
static const esp_app_desc_t *esp_app_get_description(void)
{ static esp_app_desc_t app={"zone_lite",ZJ_WRITER_VERSION};return &app; }
static uint64_t now=1,ticket;
static int64_t esp_timer_get_time(void){return (int64_t)now;}
static void vTaskDelay(unsigned milliseconds){now+=(uint64_t)milliseconds*1000;}
static bool s_identity_catalog_active_memory_valid=true;
static const char *s_catalog_writer_failure_reason="none";
static void remove_catalog_stage(const char *path);
static zc_store_t store;
static ft_checkpoint_t checkpoint;
static zj_request_t accepted;
static bool refuse,stalled;
static size_t allocations,fail_at;
static void *allocate(size_t bytes){if(++allocations==fail_at)return NULL;return malloc(bytes);}
#define malloc allocate
static char *encrypt_storage_json(const char *plain)
{if(!plain)return NULL;char *out=malloc(strlen(plain)+1);if(out)strcpy(out,plain);return out;}
static bool zj_owner_submit(const zj_request_t *request,uint64_t *id)
{if(refuse)return false;assert(!ticket);accepted=*request;*id=++ticket;return true;}
static bool zj_owner_poll(uint64_t id,zj_reply_t *reply,bool *complete)
{
 assert(ticket==id);*complete=false;if(stalled)return true;
 memset(reply,0,sizeof(*reply));
 if(!zc_store_step(&store,ticket,now,&accepted.input.catalog,&reply->catalog,&reply->result)){
  *complete=true;ticket=0;
 }
 return true;
}
''' + helpers + r'''
static int load(void *context,ft_checkpoint_t *out)
{(void)context;*out=checkpoint;return checkpoint.version?1:0;}
static bool commit(void *context,const ft_checkpoint_t *value)
{(void)context;checkpoint=*value;return true;}
static void seed(void)
{
 unlink("active");unlink("commit");unlink("backup");unlink("temporary");unlink("stage");
 FILE *file=fopen("active","w");assert(file && fputs("old",file)>=0 && !fclose(file));
 checkpoint=(ft_checkpoint_t){0};s_catalog_client=(zc_client_t){0};now=1;ticket=0;
 assert(zc_store_init(&store,"active","commit","backup","temporary","stage",(ft_port_t){load,commit,NULL}));
 s_identity_catalog_active_memory_valid=true;
}
static bool old_active(void)
{
 char bytes[5]={0};FILE *file=fopen("active","r");assert(file);
 size_t length=fread(bytes,1,4,file);assert(!fclose(file));return length==3 && !strcmp(bytes,"old");
}
int main(void)
{
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 assert(catalog_owner_required());atomic_store(&s_catalog_restore_pending,false);
 cJSON *root=cJSON_Parse("{\"rows\":[{\"user_id\":\"synthetic-user\"}]}");assert(root);
 seed();allocations=0;size_t count=0;
 assert(catalog_owner_persist(root,&count) && count==1 && !s_identity_catalog_active_memory_valid);
 size_t total=allocations;
 for(size_t fault=1;fault<=total;++fault){
  seed();allocations=0;fail_at=fault;count=99;
  assert(!catalog_owner_persist(root,&count));
  assert(count==99 && old_active() && s_identity_catalog_active_memory_valid);
 }
 fail_at=0;seed();refuse=true;
 assert(!catalog_owner_persist(root,NULL) && old_active());refuse=false;
 assert(catalog_owner_persist(root,NULL));
 seed();assert(catalog_owner_reset(ADD_IDENTITY_CATALOG_STAGE_PATH));
 assert(catalog_owner_json(ADD_IDENTITY_CATALOG_STAGE_PATH,root));
 stalled=true;assert(!catalog_owner_activate(ADD_IDENTITY_CATALOG_STAGE_PATH));
 assert(s_catalog_client.pending_ticket && old_active());
 assert(!catalog_owner_reset(ADD_IDENTITY_CATALOG_STAGE_PATH));
 stalled=false;
 for(unsigned i=0;i<100 && !catalog_owner_idle();++i){}
 assert(!s_catalog_client.pending_ticket && !s_identity_catalog_active_memory_valid);
 /* This command expired before its first owner step. Its admission was not
  * completion; a new recovery still verifies the old active generation. */
 assert(old_active() && catalog_owner_recover());
 cJSON_Delete(root);
 puts("catalog producer allocation, admission and timeout checks passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "catalog.c"
    unit.write_text(program)
    executable = temporary / "catalog-owner"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main), "-I", str(cjson),
                    str(unit), str(cjson / "cJSON.c"), *(str(main / name) for name in
                        ["zkt_catalog_client.c", "zkt_catalog_store.c", "file_transaction.c", "durable_queue.c"]),
                    "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True, timeout=60)
