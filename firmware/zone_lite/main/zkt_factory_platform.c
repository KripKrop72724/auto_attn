#include "zkt_factory_platform.h"
#if defined(ZONE_LITE_FACTORY_TRIAL_IMAGE)
#include "zone_config.h"
#include "zkt_journal_state.h"
#include "zkt_journal_store.h"
#include "zkt_storage_owner.h"
#include "durable_queue.h"
#include "esp_app_desc.h"
#include "esp_cpu.h"
#include "esp_image_format.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_secure_boot.h"
#include "bootloader_common.h"
#include "nvs.h"
#include "mbedtls/sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <dirent.h>
#include <errno.h>
#include <string.h>

#if !CONFIG_SECURE_BOOT_V2_ENABLED || !CONFIG_SECURE_SIGNED_APPS_RSA_SCHEME || !CONFIG_SECURE_SIGNED_ON_UPDATE || !CONFIG_NVS_ENCRYPTION || !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE || CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK
#error Factory trial requires actual RSA Secure Boot verification and factory-compatible rollback
#endif

static zf_proof_t proof;
static bool prepared, storage_checked, selection_uncertain;
static const char *error="FACTORY_TRIAL_NOT_OBSERVED";
static bool refuse(const char *reason) { error=reason;return false; }
static void hex(const uint8_t *in,char out[65])
{ static const char a[]="0123456789abcdef";for(unsigned i=0;i<32;++i){out[2*i]=a[in[i]>>4];out[2*i+1]=a[in[i]&15];}out[64]=0; }
static int read_proof(void *unused,uint8_t out[ZF_PROOF_BYTES])
{
    (void)unused;nvs_handle_t h;esp_err_t e=nvs_open("zkt_factory",NVS_READONLY,&h);
    if(e==ESP_ERR_NVS_NOT_FOUND)return 0;
    if(e!=ESP_OK)return -1;
    size_t n=ZF_PROOF_BYTES;e=nvs_get_blob(h,"proof_v1",out,&n);nvs_close(h);
    if(e==ESP_ERR_NVS_NOT_FOUND)return 0;
    return e==ESP_OK && n==ZF_PROOF_BYTES ? 1:-1;
}
static bool write_proof(void *unused,const uint8_t bytes[ZF_PROOF_BYTES])
{
    (void)unused;nvs_handle_t h;if(nvs_open("zkt_factory",NVS_READWRITE,&h)!=ESP_OK)return false;
    esp_err_t e=nvs_set_blob(h,"proof_v1",bytes,ZF_PROOF_BYTES);if(e==ESP_OK)e=nvs_commit(h);nvs_close(h);return e==ESP_OK;
}
static zf_proof_port_t port(void) { return (zf_proof_port_t){read_proof,write_proof,NULL}; }
static bool secure(void)
{ return esp_secure_boot_enabled() && !esp_cpu_dbgr_is_attached(); }
static bool pending(void)
{
    const esp_partition_t *p=esp_ota_get_running_partition();esp_ota_img_states_t state;
    return p && p->type==ESP_PARTITION_TYPE_APP &&
        (p->subtype==ESP_PARTITION_SUBTYPE_APP_OTA_0 || p->subtype==ESP_PARTITION_SUBTYPE_APP_OTA_1) &&
        esp_ota_get_state_partition(p,&state)==ESP_OK && state==ESP_OTA_IMG_PENDING_VERIFY;
}
static bool valid_running(void)
{
    const esp_partition_t *p=esp_ota_get_running_partition();esp_ota_img_states_t state;
    return p && esp_ota_get_state_partition(p,&state)==ESP_OK && state==ESP_OTA_IMG_VALID;
}
static const esp_partition_t *factory(void)
{
    const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_APP,ESP_PARTITION_SUBTYPE_APP_FACTORY,NULL);
    return p && p->address==ZF_FACTORY_ADDRESS && p->size==ZF_FACTORY_SIZE && !strcmp(p->label,"factory") ? p:NULL;
}
static bool fallback_metadata(void)
{
    const esp_partition_t *data=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_DATA_OTA,NULL);
    const esp_partition_t *running=esp_ota_get_running_partition();
    esp_ota_select_entry_t entries[2];uint32_t seq[2],states[2];bool crc[2];
    if(!data || data->address!=0x17000 || data->size!=0x2000 || !running || !pending())return false;
    for(unsigned i=0;i<2;++i){
        if(esp_partition_read(data,i*4096,&entries[i],sizeof(entries[i]))!=ESP_OK)return false;
        seq[i]=entries[i].ota_seq;states[i]=entries[i].ota_state;
        crc[i]=bootloader_common_ota_select_crc(&entries[i])==entries[i].crc;
    }
    return zf_fallback_metadata(esp_ota_get_app_partition_count(),running->subtype-ESP_PARTITION_SUBTYPE_APP_OTA_MIN,seq,states,crc) &&
        esp_ota_check_rollback_is_possible();
}
static void put32(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;++i)p[i]=(uint8_t)(v>>(8*i));}
static bool layout(uint8_t out[32])
{
    /* Match all seven partitions, not just factory and the destination. */
    static const struct { const char *name;uint8_t type,subtype;uint32_t address,size; } expected[]={
        {"nvs",1,2,0x11000,0x6000},{"otadata",1,0,0x17000,0x2000},{"phy_init",1,1,0x19000,0x1000},
        {"factory",0,0,0x20000,0x280000},{"ota_0",0,0x10,0x2a0000,0x280000},
        {"ota_1",0,0x11,0x520000,0x280000},{"storage",1,0x82,0x7a0000,0x800000}};
    uint8_t bytes[32+7*32]={0};bool seen[7]={0};unsigned count=0;
    memcpy(bytes,"ZKT-FACTORY-LAYOUT-V1",20);put32(bytes+24,7);
    esp_partition_iterator_t it=esp_partition_find(ESP_PARTITION_TYPE_ANY,ESP_PARTITION_SUBTYPE_ANY,NULL);
    bool ok=it!=NULL;
    while(it){
        const esp_partition_t *p=esp_partition_get(it);unsigned match=7;
        if(p)for(unsigned i=0;i<7;++i)if(!strcmp(p->label,expected[i].name))match=i;
        if(!p || match==7 || seen[match] || p->type!=expected[match].type || p->subtype!=expected[match].subtype ||
           p->address!=expected[match].address || p->size!=expected[match].size || p->encrypted){ok=false;break;}
        seen[match]=true;++count;uint8_t *row=bytes+32+match*32;
        row[0]=(uint8_t)p->type;row[1]=(uint8_t)p->subtype;put32(row+4,p->address);put32(row+8,p->size);memcpy(row+12,p->label,strlen(p->label));
        it=esp_partition_next(it);
    }
    esp_partition_iterator_release(it);
    return ok && count==7 && mbedtls_sha256(bytes,sizeof(bytes),out,0)==0;
}
static bool full_image(zf_proof_t *out)
{
    const esp_partition_t *p=factory();esp_app_desc_t app;esp_image_metadata_t metadata={0};uint8_t expected[32];
    if(!p || !secure() || esp_ota_get_partition_description(p,&app)!=ESP_OK ||
        memcmp(app.project_name,"zone_lite\0",10) || memcmp(app.version,"2.5.2\0",6) ||
        !zf_digest_parse(zf_target(out->target)->application_sha256,expected) ||
        esp_partition_get_sha256(p,out->factory_digest)!=ESP_OK || memcmp(expected,out->factory_digest,32))return false;
    esp_partition_pos_t pos={.offset=p->address,.size=p->size};
    /* Required CONFIG_SECURE_SIGNED_ON_UPDATE makes VERIFY validate the full
     * padded RSA signature against actual unrevoked eFuse digests. */
    if(esp_image_verify(ESP_IMAGE_VERIFY_SILENT,&pos,&metadata)!=ESP_OK ||
        metadata.image_len<8192 || metadata.image_len>p->size || metadata.image_len%4096)return false;
    out->signed_image_bytes=metadata.image_len;
    mbedtls_sha256_context sha;mbedtls_sha256_init(&sha);int e=mbedtls_sha256_starts(&sha,0);
    uint8_t bytes[512];
    for(uint32_t offset=0;e==0 && offset<metadata.image_len;offset+=sizeof(bytes)){
        size_t n=metadata.image_len-offset;if(n>sizeof(bytes))n=sizeof(bytes);
        if(esp_partition_read(p,offset,bytes,n)!=ESP_OK){e=-1;break;}
        e=mbedtls_sha256_update(&sha,bytes,n);
        /* Strictly positive tick count at both100Hz and1000Hz. */
        if(!(offset%4096))vTaskDelay(1);
    }
    if(e==0)e=mbedtls_sha256_finish(&sha,out->signed_digest);
    mbedtls_sha256_free(&sha);
    memset(bytes,0,sizeof(bytes));return e==0;
}
static bool namespace_absent(void)
{
    nvs_handle_t h;esp_err_t e=nvs_open("zkt_journal",NVS_READONLY,&h);
    if(e==ESP_OK)nvs_close(h);
    return e==ESP_ERR_NVS_NOT_FOUND;
}
void zf_platform_prepare(const char *deployment,const char *reader_hex)
{
    prepared=storage_checked=false;selection_uncertain=false;memset(&proof,0,sizeof(proof));
    const zone_config_t *config=zone_config_get();uint8_t mac[6],reader[32],existing[ZF_PROOF_BYTES];
    const esp_app_desc_t *app=esp_app_get_description();
    if(!config || !app || strcmp(app->version,"2.6.22") || strcmp(app->project_name,"zone_lite") ||
        !config->provisioned || strcmp(config->firmware_family,"zkt") || !secure() ||
        esp_read_mac(mac,ESP_MAC_WIFI_STA)!=ESP_OK || !zf_digest_parse(reader_hex,reader)){
        refuse("FACTORY_TRIAL_SECURITY_OR_IDENTITY");return;}
    int target=zf_target_match(config->connector_id,mac,config->zkt_expected_serial);
    if(target<0){refuse("FACTORY_TRIAL_EXACT_TARGET_REQUIRED");return;}
    int read=read_proof(NULL,existing);
    if(read<0 || (read==1 && !zf_proof_decode(existing,&proof))){refuse("FACTORY_TRIAL_CHECKPOINT_INVALID");return;}
    if(read==1){
        if(proof.target!=(uint32_t)target || memcmp(proof.reader_digest,reader,32)){
            refuse("FACTORY_TRIAL_CHECKPOINT_BINDING");return;}
        if(proof.state==ZF_REVOKED && valid_running()){
            prepared=storage_checked=true;error="";return;}
        if(proof.state==ZF_VERIFIED && valid_running()){
            /* Power loss after VALID but before revocation cannot resurrect
             * pending-boot factory selection. Complete that one-way step. */
            if(!zf_proof_transition(port(),&proof,ZF_REVOKED,&proof)){refuse("FACTORY_TRIAL_REVOCATION_UNCERTAIN");return;}
            prepared=storage_checked=true;error="";return;
        }
        refuse("FACTORY_TRIAL_RETAINED_ATTEMPT_REQUIRES_REVIEW");return;
    }
    if(!deployment || !deployment[0] || strlen(deployment)>=sizeof(proof.deployment_id) ||
        !pending() || !namespace_absent()){refuse("FACTORY_TRIAL_PREDECESSOR_OR_AUTHORITY");return;}
    proof.state=ZF_VERIFIED;proof.target=(uint32_t)target;strcpy(proof.deployment_id,deployment);memcpy(proof.reader_digest,reader,32);
    if(!layout(proof.layout_digest) || !full_image(&proof) || !fallback_metadata()){
        refuse("FACTORY_TRIAL_FACTORY_NOT_VERIFIED");return;}
    if(!zf_proof_create(port(),&proof)){refuse("FACTORY_TRIAL_PROOF_COMMIT_UNCERTAIN");return;}
    prepared=true;error="FACTORY_TRIAL_STORAGE_NOT_CHECKED";
}
bool zf_platform_storage_check(void)
{
    if(!prepared)return false;
    if(storage_checked)return true;
    DIR *directory=opendir(ZJ_DEVICE_DIRECTORY);if(!directory)return refuse("FACTORY_TRIAL_STORAGE_UNAVAILABLE");
    unsigned count=0;bool ok=true;
    for(;;){errno=0;struct dirent *entry=readdir(directory);if(!entry){if(errno)ok=false;break;}
        if(!strncmp(entry->d_name,ZJ_DEVICE_BASENAME,sizeof(ZJ_DEVICE_BASENAME)-1) || ++count>1024){ok=false;break;}}
    if(closedir(directory))ok=false;
    if(!ok)return refuse("FACTORY_TRIAL_JOURNAL_EVIDENCE_PRESENT");
    storage_checked=true;error="";return true;
}
bool zf_platform_startup_allowed(void){return prepared && storage_checked && !selection_uncertain && proof.state!=ZF_FALLBACK_INTENT;}
const char *zf_platform_error(void){return error;}
bool zf_platform_revoke(void)
{
    if(!prepared || !storage_checked || selection_uncertain || !valid_running())return refuse("FACTORY_TRIAL_REVOCATION_HELD");
    if(proof.state==ZF_REVOKED)return true;
    if(!zf_proof_transition(port(),&proof,ZF_REVOKED,&proof))return refuse("FACTORY_TRIAL_REVOCATION_UNCERTAIN");
    error="";return true;
}
bool zf_platform_pending_fallback(void)
{return prepared && storage_checked && proof.state==ZF_VERIFIED && !selection_uncertain && pending() && secure();}
bool zf_platform_select_factory(void)
{
    if(!zf_platform_pending_fallback())return refuse("FACTORY_TRIAL_FALLBACK_NOT_AUTHORIZED");
    zf_proof_t measured=proof;
    if(!full_image(&measured) || memcmp(measured.signed_digest,proof.signed_digest,32) ||
        measured.signed_image_bytes!=proof.signed_image_bytes || !fallback_metadata())return refuse("FACTORY_TRIAL_FALLBACK_CHANGED");
    if(!zf_proof_transition(port(),&proof,ZF_FALLBACK_INTENT,&proof))return refuse("FACTORY_TRIAL_INTENT_UNCERTAIN");
    /* Once attempted, no caller may reopen admission or reinterpret an error
     * as proof that the boot metadata stayed unchanged. */
    selection_uncertain=true;
    if(esp_ota_mark_app_invalid_rollback()!=ESP_OK)return refuse("FACTORY_TRIAL_SELECTION_UNCERTAIN");
    const esp_partition_t *selected=esp_ota_get_boot_partition(),*expected=factory();
    if(!selected || !expected || selected->address!=expected->address || selected->size!=expected->size ||
        selected->subtype!=ESP_PARTITION_SUBTYPE_APP_FACTORY)return refuse("FACTORY_TRIAL_SELECTION_UNCERTAIN");
    error="FACTORY_TRIAL_FACTORY_SELECTED";return true;
}
bool zf_platform_proof(zf_proof_t *out)
{if(!out || !prepared || !storage_checked || selection_uncertain || proof.state==ZF_FALLBACK_INTENT)return false;*out=proof;return true;}
bool zf_platform_add_proof(cJSON *root)
{
    zf_proof_t p;uint8_t bytes[ZF_PROOF_BYTES],digest[32];char encoded[65];
    if(!root || !zf_platform_proof(&p) || !zf_proof_encode(&p,bytes) || mbedtls_sha256(bytes,sizeof(bytes),digest,0))return false;
    bool ok=cJSON_AddNumberToObject(root,"schema_version",1) &&
        cJSON_AddStringToObject(root,"proof_state",p.state==ZF_REVOKED?"FACTORY_FALLBACK_REVOKED":"FACTORY_VERIFIED") &&
        cJSON_AddStringToObject(root,"deployment_id",p.deployment_id) &&
        cJSON_AddNumberToObject(root,"factory_signed_image_bytes",p.signed_image_bytes) &&
        cJSON_AddNumberToObject(root,"factory_address",ZF_FACTORY_ADDRESS) && cJSON_AddNumberToObject(root,"factory_size",ZF_FACTORY_SIZE);
    const char *names[]={"reader_application_sha256","factory_application_sha256","factory_signed_image_sha256","layout_sha256","checkpoint_sha256"};
    const uint8_t *values[]={p.reader_digest,p.factory_digest,p.signed_digest,p.layout_digest,digest};
    for(unsigned i=0;ok && i<5;++i){hex(values[i],encoded);ok=cJSON_AddStringToObject(root,names[i],encoded)!=NULL;}
    const char *flags[]={"checkpoint_verified","secure_boot_verified","signature_verified","encrypted_nvs","rollback_enabled","anti_rollback_disabled","factory_fallback_verified","legacy_only"};
    for(unsigned i=0;ok && i<sizeof(flags)/sizeof(flags[0]);++i)ok=cJSON_AddBoolToObject(root,flags[i],true)!=NULL;
    return ok;
}
#else
void zf_platform_prepare(const char *a,const char *b){(void)a;(void)b;}
bool zf_platform_storage_check(void){return true;}
bool zf_platform_startup_allowed(void){return true;}
const char *zf_platform_error(void){return "";}
bool zf_platform_revoke(void){return true;}
bool zf_platform_pending_fallback(void){return false;}
bool zf_platform_select_factory(void){return false;}
bool zf_platform_proof(zf_proof_t *out){(void)out;return false;}
bool zf_platform_add_proof(cJSON *root){(void)root;return false;}
#endif
