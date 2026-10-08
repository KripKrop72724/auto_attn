"""Actual22 HTTP proof exchange must consume only a committed exact receipt."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def block(text, marker):
    start = text.index(marker)
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def test_actual_factory_http_receipt_and_replayed_observation(tmp_path):
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    cjson = idf / "components/json/cJSON"
    if not (cjson / "cJSON.c").is_file():
        pytest.skip("Pinned ESP-IDF cJSON source is required")
    ota = (MAIN / "ota_manager.c").read_text()
    platform = (MAIN / "zkt_factory_platform.c").read_text()
    production = "\n".join(block(ota, signature) for signature in (
        "static const char *json_string(", "static bool same_json(", "static bool report_factory_trial(void)\n{"))
    payload = block(platform, "bool zf_platform_add_proof(cJSON *root)\n{")
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "cJSON.h"
#include "zkt_factory_trial.h"
#define OTA_HTTP_RESPONSE_BYTES 8192
typedef struct {char *data;size_t length,capacity;} ota_response_t;
static struct {char target_version[32],deployment_id[48];} s_journal;
static bool factory_reported;
static zf_proof_t retained;
static int mode;
static unsigned get_calls,posts;
static char previous_body[4096];
static time_t fixed_time(time_t *out){time_t value=1800000000;if(out)*out=value;return value;}
#define time fixed_time
static const char *add_connector_boot_id(void){return "e072a1d6f328-12345678";}
static bool zf_platform_proof(zf_proof_t *out){*out=retained;return true;}
static int mbedtls_sha256(const uint8_t *in,size_t size,uint8_t out[32],int mode){assert(in && size==ZF_PROOF_BYTES && mode==0);memset(out,0x45,32);return 0;}
static void hex(const uint8_t *in,char out[65]){const char *a="0123456789abcdef";for(unsigned i=0;i<32;++i){out[2*i]=a[in[i]>>4];out[2*i+1]=a[in[i]&15];}out[64]=0;}
/* PAYLOAD */
static bool signed_request(const char *method,const char *path,const char *body,ota_response_t *out,int *status){
 assert(strstr(path,retained.deployment_id));*status=200;
 cJSON *reply=cJSON_CreateObject();assert(reply);
 if(!strcmp(method,"GET")){
  ++get_calls;assert(!body);
  cJSON_AddStringToObject(reply,"trial_id",mode==11?"bad":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
  cJSON_AddStringToObject(reply,"challenge",mode==10?"bad":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");
  cJSON_AddStringToObject(reply,"deployment_id",retained.deployment_id);
  cJSON_AddNumberToObject(reply,"onboarding_generation",mode==7?5:4);
  cJSON_AddNumberToObject(reply,"expires_epoch",mode==8?1799999999:mode==9?1800003601:1800003500);
 }else{
  assert(!strcmp(method,"POST"));++posts;cJSON *request=cJSON_Parse(body);assert(request);
  assert(cJSON_GetObjectItemCaseSensitive(request,"onboarding_generation")->valuedouble==4);
  assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(request,"legacy_only")));
  assert(!strcmp(cJSON_GetObjectItemCaseSensitive(request,"proof_state")->valuestring,"FACTORY_FALLBACK_REVOKED"));
  if(previous_body[0])assert(!strcmp(body,previous_body));else{assert(strlen(body)<sizeof(previous_body));strcpy(previous_body,body);}
  const char *keys[]={"trial_id","challenge","deployment_id","boot_id","checkpoint_sha256","proof_state"};
  cJSON_AddBoolToObject(reply,"accepted",mode!=2);
  for(unsigned i=0;i<6;++i){const char *value=cJSON_GetObjectItemCaseSensitive(request,keys[i])->valuestring;
   if((mode==3 && i==0)||(mode==4 && i==3)||(mode==5 && i==4)||(mode==6 && i==5))value="wrong";
   cJSON_AddStringToObject(reply,keys[i],value);
  }
  if(mode==1)*status=202;
  cJSON_Delete(request);
 }
 char *text=cJSON_PrintUnformatted(reply);assert(text && strlen(text)<out->capacity);strcpy(out->data,text);out->length=strlen(text);free(text);cJSON_Delete(reply);
 return !(mode==14 && !strcmp(method,"POST"));
}
/* PRODUCTION */
static void reset(void){
 factory_reported=false;get_calls=posts=0;previous_body[0]=0;memset(&retained,0,sizeof(retained));
 retained.state=ZF_REVOKED;retained.target=0;retained.signed_image_bytes=8192;
 strcpy(retained.deployment_id,"11111111-2222-4333-8444-555555555555");
 memset(retained.reader_digest,0x12,32);memset(retained.signed_digest,0x23,32);memset(retained.layout_digest,0x34,32);
 assert(zf_digest_parse(zf_target(0)->application_sha256,retained.factory_digest));
 strcpy(s_journal.deployment_id,retained.deployment_id);strcpy(s_journal.target_version,"2.6.22");
}
int main(void){
 for(mode=1;mode<=11;++mode){reset();assert(!report_factory_trial());assert(!factory_reported);assert(get_calls==1);assert(posts==(mode>=7?0:1));}
 mode=0;reset();retained.state=ZF_VERIFIED;assert(!report_factory_trial() && !get_calls && !posts);
 reset();strcpy(s_journal.target_version,"2.7.0");assert(!report_factory_trial() && !get_calls && !posts);
 reset();s_journal.deployment_id[0]='2';assert(!report_factory_trial() && !get_calls && !posts);
 reset();mode=14;assert(!report_factory_trial() && posts==1 && !factory_reported);
 mode=0;assert(report_factory_trial() && posts==2 && get_calls==2 && factory_reported);
 assert(report_factory_trial() && posts==2 && get_calls==2);return 0;
}
'''.replace("/* PAYLOAD */", payload).replace("/* PRODUCTION */", production)
    source = tmp_path / "transport.c"
    source.write_text(program)
    binary = tmp_path / "transport"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(MAIN), "-I", str(cjson), str(source), str(cjson / "cJSON.c"),
                    str(MAIN / "zkt_factory_trial.c"), str(MAIN / "durable_queue.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)
