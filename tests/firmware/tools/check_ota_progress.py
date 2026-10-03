"""Actual HTTP success path must verify the OTA progress receipt before retiring state."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
CJSON = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
source = (MAIN / "ota_manager.c").read_text()
start = source.index("static bool post_json(")
production = source[start:source.index("static bool cache_running_image_digest(", start)]
program = r'''
#include "ota_progress_receipt.h"
#include "ota_checkpoint.h"
#include <assert.h>
#include <stdlib.h>
#include <stdio.h>
#define OTA_HTTP_RESPONSE_BYTES 8192
typedef struct {char *data;size_t length,capacity;} ota_response_t;
static ota_journal_t s_journal;
static char s_running_image_digest[65],response_body[1024];
static bool strict=true,transport_ok=true;
static unsigned status_code=200,allocations,fail_at;
static void *allocate(size_t n){return ++allocations==fail_at?NULL:malloc(n);}
static void *response_allocate(size_t n,size_t size){return ++allocations==fail_at?NULL:calloc(n,size);}
static bool uses_local_boot_confirmation(void){return strict;}
static bool signed_request(const char *method,const char *path,const char *body,ota_response_t *out,int *status)
{assert(!strcmp(method,"POST") && path[0] && body[0]);assert(strlen(response_body)<out->capacity);strcpy(out->data,response_body);out->length=strlen(response_body);*status=status_code;return transport_ok;}
#define calloc response_allocate
/* PRODUCTION */
#undef calloc
static void response(const char *state,const char *deployment,const char *digest)
{
 snprintf(response_body,sizeof(response_body),"{\"schema_version\":1,\"deployment_id\":\"%s\",\"state\":\"%s\",\"target_version\":\"2.7.0\",\"application_sha256\":\"%s\"}",deployment,state,digest);
}
int main(void)
{
 strcpy(s_journal.deployment_id,"synthetic-deployment");strcpy(s_journal.target_version,"2.7.0");
 memset(s_running_image_digest,'c',64);cJSON *request=cJSON_CreateObject();assert(request);int status;
 response("BOOTED_PENDING",s_journal.deployment_id,s_running_image_digest);
 assert(post_json("/progress",request,&status,"BOOTED_PENDING") && status==200);
 response("FAILED",s_journal.deployment_id,s_running_image_digest);
 assert(!post_json("/progress",request,&status,"BOOTED_PENDING") && status==200);
 response("SUCCEEDED",s_journal.deployment_id,s_running_image_digest);
 assert(!post_json("/progress",request,&status,"BOOTED_PENDING"));
 assert(post_json("/progress",request,&status,"RECONCILING")); /* lost success ACK/clear */
 assert(post_json("/progress",request,&status,"SUCCEEDED"));
 response("RECONCILING","other-deployment",s_running_image_digest);
 assert(!post_json("/progress",request,&status,"RECONCILING"));
 response("RECONCILING",s_journal.deployment_id,"wrong-running-image");
 assert(!post_json("/progress",request,&status,"RECONCILING"));
 const char *rejected[]={"FAILED","CANCELLED","RELEASE_REVOKED","ROLLED_BACK","SUPERSEDED"};
 for(unsigned i=0;i<sizeof(rejected)/sizeof(rejected[0]);++i){
  response(rejected[i],s_journal.deployment_id,s_running_image_digest);
  assert(!post_json("/progress",request,&status,"RECONCILING"));
 }
 strcpy(response_body,"{}");assert(!post_json("/progress",request,&status,"BOOTED_PENDING"));
 assert(post_json("/capability",request,&status,NULL));
 strict=false;assert(post_json("/legacy",request,&status,"BOOTED_PENDING"));strict=true;
 response("BOOTED_PENDING",s_journal.deployment_id,s_running_image_digest);
 status_code=409;assert(!post_json("/progress",request,&status,"BOOTED_PENDING") && status==409);status_code=200;
 transport_ok=false;assert(!post_json("/progress",request,&status,"BOOTED_PENDING"));transport_ok=true;
 cJSON *reply=cJSON_Parse(response_body);assert(reply);
 cJSON_ReplaceItemInObjectCaseSensitive(reply,"schema_version",cJSON_CreateBool(true));
 assert(!ota_progress_receipt_matches(reply,s_journal.deployment_id,"BOOTED_PENDING","2.7.0",s_running_image_digest));
 cJSON_ReplaceItemInObjectCaseSensitive(reply,"schema_version",cJSON_CreateNumber(1));
 assert(!ota_progress_receipt_matches(reply,s_journal.deployment_id,"BOOTED_PENDING","2.6.16",s_running_image_digest));
 cJSON_Delete(reply);
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 for(unsigned point=1;point<120;++point){
  allocations=0;fail_at=point;bool ok=post_json("/progress",request,&status,"BOOTED_PENDING");
  assert(!ok || allocations<point);
 }
 cJSON_InitHooks(NULL);cJSON_Delete(request);
 puts("OTA HTTP acceptance, exact deployment/image binding and allocation faults passed");
}
'''
with tempfile.TemporaryDirectory(prefix="ota-progress-") as temporary:
    unit = Path(temporary) / "progress.c"
    unit.write_text(program.replace("/* PRODUCTION */", production))
    binary = Path(temporary) / "progress"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    "-I", str(CJSON), str(unit), str(MAIN / "ota_progress_receipt.c"),
                    str(CJSON / "cJSON.c"), "-lm", "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
