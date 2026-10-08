/* Executes production platform control with fault-injected SDK boundaries.
 * The real ESP cryptography is covered by the pinned SDK/archive tests; this
 * harness proves refusal, write ordering and API selection, not eFuse state. */
#include <assert.h>
#include <string.h>
#include "zkt_factory_platform_host.h"
#include "zone_config.h"
#include "zkt_factory_trial.h"
#include "zkt_factory_platform.c"
static zone_config_t config;
static esp_app_desc_t app;
static esp_partition_t parts[7];
static struct iterator { unsigned index; } iterator;
static int run_state;
static bool secure_boot,debug,signature_ok,hash_ok,read_ok,rollback_ok,other_ota,
    namespace_present,proof_present,commit_ok,readback_ok,orphan,selected_factory,select_ok;
static uint8_t nvs_bytes[ZF_PROOF_BYTES];
static unsigned verify_calls,writes,select_calls,delays,dir_reads;
static char image[65];
static const char *deployment="11111111-2222-4333-8444-555555555555";
const zone_config_t *zone_config_get(void){return &config;}
const esp_app_desc_t *esp_app_get_description(void){return &app;}
bool esp_secure_boot_enabled(void){return secure_boot;}
bool esp_cpu_dbgr_is_attached(void){return debug;}
int esp_read_mac(uint8_t *out,int type){assert(type==0);memcpy(out,zf_target(0)->mac,6);return 0;}
const esp_partition_t *esp_ota_get_running_partition(void){return &parts[4];}
const esp_partition_t *esp_ota_get_boot_partition(void){return &parts[selected_factory?3:5];}
int esp_ota_get_state_partition(const esp_partition_t *p,esp_ota_img_states_t *state){assert(p==&parts[4]);*state=run_state;return 0;}
int esp_ota_get_partition_description(const esp_partition_t *p,esp_app_desc_t *out){assert(p==&parts[3]);memset(out,0,sizeof(*out));strcpy(out->project_name,"zone_lite");strcpy(out->version,"2.5.2");return 0;}
int esp_partition_get_sha256(const esp_partition_t *p,uint8_t *out){assert(p==&parts[3]);assert(zf_digest_parse(zf_target(0)->application_sha256,out));if(!hash_ok)out[0]^=1;return 0;}
const esp_partition_t *esp_partition_find_first(int type,int sub,const char *label){(void)label;for(unsigned i=0;i<7;++i)if(parts[i].type==type && parts[i].subtype==sub)return &parts[i];return 0;}
esp_partition_iterator_t esp_partition_find(int type,int sub,const char *label){(void)type;(void)sub;(void)label;iterator.index=0;return &iterator;}
const esp_partition_t *esp_partition_get(esp_partition_iterator_t it){return &parts[it->index];}
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t it){return ++it->index<7?it:0;}
void esp_partition_iterator_release(esp_partition_iterator_t it){(void)it;}
int esp_partition_read(const esp_partition_t *p,size_t offset,void *out,size_t size){
    if(!read_ok)return -1;
    if(p==&parts[1]){assert(size==sizeof(esp_ota_select_entry_t));esp_ota_select_entry_t e;
        if(!offset)e=(esp_ota_select_entry_t){1,1,123};
        else e=other_ota?(esp_ota_select_entry_t){2,2,123}:(esp_ota_select_entry_t){UINT32_MAX,UINT32_MAX,0};
        memcpy(out,&e,size);return 0;}
    assert(p==&parts[3] && offset+size<=8192);memset(out,0x57,size);return 0;
}
unsigned esp_ota_get_app_partition_count(void){return 2;}
uint32_t bootloader_common_ota_select_crc(const esp_ota_select_entry_t *entry){(void)entry;return 123;}
bool esp_ota_check_rollback_is_possible(void){return rollback_ok;}
int esp_ota_mark_app_invalid_rollback(void){zf_proof_t retained;assert(zf_proof_decode(nvs_bytes,&retained));assert(retained.state==ZF_FALLBACK_INTENT);++select_calls;run_state=3;return select_ok?0:-1;}
int esp_image_verify(int mode,const esp_partition_pos_t *pos,esp_image_metadata_t *data){
    assert(mode==ESP_IMAGE_VERIFY_SILENT && pos->offset==ZF_FACTORY_ADDRESS && pos->size==ZF_FACTORY_SIZE);
    ++verify_calls;data->image_len=8192;return signature_ok?0:-1;
}
int nvs_open(const char *name,int mode,nvs_handle_t *handle){
    if(!strcmp(name,"zkt_journal"))return namespace_present?(*handle=2,0):ESP_ERR_NVS_NOT_FOUND;
    assert(!strcmp(name,"zkt_factory"));if(mode==NVS_READONLY && !proof_present)return ESP_ERR_NVS_NOT_FOUND;*handle=1;return 0;}
int nvs_get_blob(nvs_handle_t h,const char *name,void *out,size_t *n){assert(h==1 && !strcmp(name,"proof_v1") && *n==ZF_PROOF_BYTES);if(!proof_present)return ESP_ERR_NVS_NOT_FOUND;memcpy(out,nvs_bytes,*n);if(!readback_ok)((uint8_t *)out)[80]^=1;return 0;}
int nvs_set_blob(nvs_handle_t h,const char *name,const void *in,size_t n){assert(h==1 && !strcmp(name,"proof_v1") && n==ZF_PROOF_BYTES);++writes;proof_present=true;memcpy(nvs_bytes,in,n);return 0;}
int nvs_commit(nvs_handle_t h){assert(h==1);return commit_ok?0:-1;}
void nvs_close(nvs_handle_t h){(void)h;}
void mbedtls_sha256_init(mbedtls_sha256_context *c){c->value=1;}
int mbedtls_sha256_starts(mbedtls_sha256_context *c,int mode){assert(mode==0);c->value=1;return 0;}
int mbedtls_sha256_update(mbedtls_sha256_context *c,const unsigned char *p,size_t n){while(n--)c->value=c->value*33+*p++;return 0;}
int mbedtls_sha256_finish(mbedtls_sha256_context *c,unsigned char *out){for(unsigned i=0;i<32;++i)out[i]=(uint8_t)((c->value>>(i%4)*8)^0x12);return 0;}
void mbedtls_sha256_free(mbedtls_sha256_context *c){c->value=0;}
int mbedtls_sha256(const unsigned char *p,size_t n,unsigned char *out,int mode){mbedtls_sha256_context c;mbedtls_sha256_starts(&c,mode);mbedtls_sha256_update(&c,p,n);return mbedtls_sha256_finish(&c,out);}
void vTaskDelay(unsigned ticks){assert(ticks>0);++delays;}
cJSON *cJSON_AddNumberToObject(cJSON *o,const char *k,double v){assert(k && v>=0);++o->fields;return o;}
cJSON *cJSON_AddStringToObject(cJSON *o,const char *k,const char *v){assert(k && v);++o->fields;return o;}
cJSON *cJSON_AddBoolToObject(cJSON *o,const char *k,bool v){assert(k && v);++o->fields;return o;}
DIR *factory_opendir(const char *path){assert(!strcmp(path,ZJ_DEVICE_DIRECTORY));dir_reads=0;return (DIR *)&iterator;}
struct dirent *factory_readdir(DIR *dir){(void)dir;static struct dirent e;if(orphan && !dir_reads++){memset(&e,0,sizeof(e));strcpy(e.d_name,"zktj-orphan");return &e;}return 0;}
int factory_closedir(DIR *dir){(void)dir;return 0;}
static void reset(void){
    memset(&config,0,sizeof(config));config.provisioned=true;strcpy(config.firmware_family,"zkt");
    strcpy(config.connector_id,zf_target(0)->connector_id);strcpy(config.zkt_expected_serial,zf_target(0)->terminal_serial);
    memset(&app,0,sizeof(app));strcpy(app.project_name,"zone_lite");strcpy(app.version,"2.6.22");
    parts[0]=(esp_partition_t){0x11000,0x6000,1,2,false,"nvs"};parts[1]=(esp_partition_t){0x17000,0x2000,1,0,false,"otadata"};
    parts[2]=(esp_partition_t){0x19000,0x1000,1,1,false,"phy_init"};parts[3]=(esp_partition_t){0x20000,0x280000,0,0,false,"factory"};
    parts[4]=(esp_partition_t){0x2a0000,0x280000,0,16,false,"ota_0"};parts[5]=(esp_partition_t){0x520000,0x280000,0,17,false,"ota_1"};
    parts[6]=(esp_partition_t){0x7a0000,0x800000,1,0x82,false,"storage"};
    run_state=1;secure_boot=signature_ok=hash_ok=read_ok=rollback_ok=commit_ok=readback_ok=selected_factory=select_ok=true;
    debug=other_ota=namespace_present=proof_present=orphan=false;verify_calls=writes=select_calls=delays=0;
    memset(nvs_bytes,0,sizeof(nvs_bytes));memset(image,'a',64);image[64]=0;
}
int main(void){
    for(unsigned fault=0;fault<10;++fault){reset();
        if(fault==0)secure_boot=false;if(fault==1)debug=true;if(fault==2)signature_ok=false;if(fault==3)hash_ok=false;
        if(fault==4)read_ok=false;if(fault==5)other_ota=true;if(fault==6)rollback_ok=false;
        if(fault==7)namespace_present=true;if(fault==8)parts[6].size-=4096;if(fault==9)config.connector_id[0]='b';
        zf_platform_prepare(deployment,image);assert(!zf_platform_startup_allowed());assert(!zf_platform_pending_fallback());assert(writes==0 && select_calls==0);
    }
    reset();zf_platform_prepare(deployment,image);assert(writes==1 && verify_calls==1 && delays>0);assert(!zf_platform_startup_allowed());
    assert(zf_platform_storage_check());assert(zf_platform_startup_allowed());assert(zf_platform_pending_fallback());
    zf_proof_t out;assert(zf_platform_proof(&out) && out.state==ZF_VERIFIED);
    assert(!zf_platform_revoke());assert(select_calls==0);run_state=2;assert(zf_platform_revoke());assert(zf_platform_proof(&out) && out.state==ZF_REVOKED);
    assert(!zf_platform_pending_fallback());assert(!zf_platform_select_factory());assert(select_calls==0);
    namespace_present=true;zf_platform_prepare("",image);assert(zf_platform_startup_allowed());assert(!zf_platform_pending_fallback());
    reset();zf_platform_prepare(deployment,image);orphan=true;assert(!zf_platform_storage_check());assert(!zf_platform_startup_allowed());assert(!zf_platform_pending_fallback());
    reset();commit_ok=false;zf_platform_prepare(deployment,image);assert(!zf_platform_startup_allowed());assert(!zf_platform_pending_fallback());assert(!select_calls);
    reset();readback_ok=false;zf_platform_prepare(deployment,image);assert(!zf_platform_startup_allowed());assert(!select_calls);
    reset();zf_platform_prepare(deployment,image);assert(zf_platform_storage_check());assert(zf_platform_select_factory());assert(select_calls==1);assert(!zf_platform_startup_allowed());assert(!zf_platform_select_factory());assert(select_calls==1);
    for(unsigned fault=0;fault<4;++fault){reset();zf_platform_prepare(deployment,image);assert(zf_platform_storage_check());
        if(fault==0)signature_ok=false;if(fault==1)commit_ok=false;if(fault==2)select_ok=false;if(fault==3)selected_factory=false;
        assert(!zf_platform_select_factory());assert(select_calls==(fault>=2?1:0));
        if(fault>=2){assert(!zf_platform_startup_allowed());assert(!zf_platform_select_factory());assert(select_calls==1);}
    }
    return 0;
}
