"""Exercise the real command parser/admission and acknowledged witness envelope."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "add_connector.c").read_text()


def function(declaration):
    start = source.index(declaration)
    return source[start:source.index("\n}\n", start) + 3]


inbound = function("static void parse_inbound(")
admission = "static void offer(cJSON *root)\n{\n" + inbound[inbound.rindex("    add_command_t command;"):]
actual = function("static bool parse_command_object(") + admission
actual += function("bool add_connector_command_update_acknowledged(")
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "add_connector.h"
#define ESP_LOGW(...) do {} while (0)
#define strlcpy copy_text
static size_t copy_text(char *out,const char *in,size_t size){size_t n=strlen(in);if(size){size_t copy=n<size-1?n:size-1;memcpy(out,in,copy);out[copy]=0;}return n;}
static bool durable=true;
static unsigned offered,journal_calls,acknowledged,retries,sends;
static size_t allocations,fail_at;
static void *allocate(size_t n){if(++allocations==fail_at)return NULL;return malloc(n);}
static bool command_journal_append(cJSON *root,const char *id){assert(root&&id[0]);++journal_calls;return durable;}
static bool queue_command_if_idle(const add_command_t *command){assert(command->command_id[0]);++offered;return true;}
bool add_connector_command_update(const char *id,const char *status,const char *code,const char *message,const char *json)
{assert(id[0]&&json);(void)code;(void)message;if(!strcmp(status,"ACKNOWLEDGED"))++acknowledged;else{assert(!strcmp(status,"RETRYING"));++retries;}return true;}
bool add_connector_send_payload_acknowledged(const char *type,const char *payload,uint32_t timeout)
{assert(!strcmp(type,"command_update")&&timeout==1000);assert(strstr(payload,"RUNNING")&&!strstr(payload,"SUCCEEDED"));++sends;return true;}
/* PRODUCTION */
static cJSON *request(void){return cJSON_Parse("{\"type\":\"command\",\"command_type\":\"ESP_REBOOT\","
 "\"command_id\":\"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\",\"expires_epoch\":1800000060,"
 "\"expected_state\":{\"serial\":\"TERMINAL-1\"},\"payload\":{"
 "\"run_id\":\"11111111-2222-4333-8444-555555555555\",\"boot_id\":\"a4cb8fd46664-12345678\","
 "\"application_sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
 "\"terminal_serial\":\"TERMINAL-1\",\"expires_at\":1800000060}}");}
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 add_command_t command;cJSON *root=request();assert(root);
#ifdef ZONE_LITE_HIKVISION
 assert(!parse_command_object(root,&command));cJSON_Delete(root);offer(request());assert(!offered&&!journal_calls);
#else
 assert(parse_command_object(root,&command));assert(!strcmp(command.reboot.run_id,"11111111-2222-4333-8444-555555555555"));cJSON_Delete(root);
 const char *fields[]={"run_id","boot_id","application_sha256","terminal_serial","expires_at"};
 for(unsigned i=0;i<5;i++){root=request();cJSON_DeleteItemFromObject(cJSON_GetObjectItemCaseSensitive(root,"payload"),fields[i]);assert(!parse_command_object(root,&command));cJSON_Delete(root);}
 root=request();cJSON_ReplaceItemInObject(root,"expires_epoch",cJSON_CreateNumber(1800000061));assert(!parse_command_object(root,&command));cJSON_Delete(root);
 root=request();cJSON_ReplaceItemInObject(cJSON_GetObjectItemCaseSensitive(root,"payload"),"expires_at",cJSON_CreateNumber(1800000060.5));assert(!parse_command_object(root,&command));cJSON_Delete(root);
 root=request();cJSON_ReplaceItemInObject(cJSON_GetObjectItemCaseSensitive(root,"expected_state"),"serial",cJSON_CreateString("WRONG"));assert(!parse_command_object(root,&command));cJSON_Delete(root);
 root=request();cJSON_ReplaceItemInObject(cJSON_GetObjectItemCaseSensitive(root,"payload"),"boot_id",cJSON_CreateString("bad\"boot"));assert(!parse_command_object(root,&command));cJSON_Delete(root);
 durable=false;offer(request());assert(!offered&&!acknowledged&&journal_calls==1&&retries==1);
 durable=true;offer(request());assert(offered==1&&acknowledged==1&&journal_calls==2);
#endif
 allocations=0;assert(add_connector_command_update_acknowledged("id","{\"safe_checkpoint\":true}"));size_t count=allocations;
 for(size_t i=1;i<=count;i++){allocations=0;fail_at=i;unsigned before=sends;assert(!add_connector_command_update_acknowledged("id","{\"safe_checkpoint\":true}"));assert(sends==before);fail_at=0;}
 assert(!add_connector_command_update_acknowledged("id","[]"));
 puts("Controlled reboot parser, durable inbox admission and acknowledged evidence passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    (temporary / "esp_attr.h").write_text("#define RTC_NOINIT_ATTR\n")
    unit = temporary / "hil.c"
    unit.write_text(program.replace("/* PRODUCTION */", actual))
    for family in (0, 1):
        executable = temporary / f"hil-{family}"
        subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(temporary), "-I", str(main), "-I", str(cjson),
            *(["-DZONE_LITE_HIKVISION=1"] if family else []), str(unit), str(cjson / "cJSON.c"),
            str(main / "zkt_hil_reboot.c"), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True, timeout=30)
