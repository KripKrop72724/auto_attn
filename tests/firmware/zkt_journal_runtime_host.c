#include "zkt_reader_platform_host.h"
#include "zkt_journal_runtime.h"
#include "zkt_journal_diagnostics.h"
#include "queue_store.h"
#include "zone_config.h"
#include "freertos/semphr.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static esp_app_desc_t app={.project_name="zone_lite",.version="2.6.15"};
static esp_partition_t running={.type=ESP_PARTITION_TYPE_APP,.subtype=ESP_PARTITION_SUBTYPE_APP_OTA_0};
static zone_config_t config={.provisioned=true,.firmware_family="zkt",.zkt_expected_serial="TEST-SERIAL"};
static bool secure=true, mutex_fail, lock_fail, cutover, pending_bridge;
static uint32_t clock_ms;
static unsigned owner_starts,transport_starts,capture_starts,proofs;
static zj_owner_health_t owner;
static zj_transport_health_t transport;
const esp_app_desc_t *esp_app_get_description(void){return &app;}
const esp_partition_t *esp_ota_get_running_partition(void){return &running;}
bool esp_secure_boot_enabled(void){return secure;}
int esp_ota_get_state_partition(const esp_partition_t *p, esp_ota_img_states_t *out){
 assert(p==&running);*out=pending_bridge?ESP_OTA_IMG_PENDING_VERIFY:ESP_OTA_IMG_VALID;return ESP_OK;
}
const zone_config_t *zone_config_get(void){return &config;}
int64_t esp_timer_get_time(void){return (int64_t)clock_ms*1000;}
SemaphoreHandle_t xSemaphoreCreateMutex(void){return mutex_fail?NULL:(void*)1;}
int xSemaphoreTake(SemaphoreHandle_t h,unsigned timeout){assert(h);(void)timeout;return !lock_fail;}
void xSemaphoreGive(SemaphoreHandle_t h){assert(h);}
qs_health_t qs_health(void){return (qs_health_t){.observed=true,.available=true,.recovery_complete=true,.persistence_verified=true};}
bool zj_owner_start(const char *prefix,const zj_metadata_t *metadata){
 assert(!strcmp(prefix,"/storage/zktj"));assert(!strcmp(metadata->terminal_serial,"TEST-SERIAL"));
 assert(!strcmp(metadata->decoder_profile,"zkt-unqualified"));assert(!strcmp(metadata->decoder_version,"zkt-raw-1"));
 ++owner_starts;owner=(zj_owner_health_t){.started=true,.ready=true,.compatibility=ZJ_COMPAT_NOT_READY,.delivery_authority=cutover?ZJ_AUTHORITY_ADD:ZJ_AUTHORITY_LEGACY};return true;
}
bool zj_owner_health(zj_owner_health_t *out){owner.sampled_uptime_us=(uint64_t)clock_ms*1000;*out=owner;return true;}
bool zj_transport_start(void){++transport_starts;transport.started=true;return true;}
bool zj_transport_health(zj_transport_health_t *out){transport.sampled_ms=clock_ms;*out=transport;return true;}
bool zj_capture_runtime_start(void){assert(owner.writer_allowed);++capture_starts;return true;}
bool zj_capture_runtime_health(zj_capture_health_t *out){*out=(zj_capture_health_t){0};return capture_starts>0;}
bool zj_diagnostics_append(cJSON *j,const zj_boot_t *b,bool r,bool l,const zj_diagnostics_snapshot_t *s,uint64_t n){
 (void)j;(void)b;(void)r;(void)l;(void)s;(void)n;return true;
}
bool zj_owner_submit(const zj_request_t *request,uint64_t *ticket){assert(request->operation==ZJ_READER_CHECK);*ticket=++proofs;return true;}
bool zj_owner_poll(uint64_t ticket,zj_reply_t *reply,bool *complete){
 assert(ticket==proofs);*complete=true;*reply=(zj_reply_t){.result=ZJ_OK,.compatibility=ZJ_COMPAT_OK};
 if(!strcmp(app.version,"2.7.0"))cutover=true;
 owner.compatibility_checked=true;owner.compatibility=ZJ_COMPAT_OK;owner.writer_allowed=cutover;
 owner.delivery_authority=cutover?ZJ_AUTHORITY_ADD:ZJ_AUTHORITY_LEGACY;return true;
}
bool zj_owner_abandon(uint64_t ticket){(void)ticket;return true;}
/* Serialization is separately allocation-faulted against pinned cJSON. */
static cJSON json;
cJSON *cJSON_CreateObject(void){return &json;}
cJSON *cJSON_AddBoolToObject(cJSON *j,const char *k,int v){(void)j;(void)k;(void)v;return &json;}
cJSON *cJSON_AddStringToObject(cJSON *j,const char *k,const char *v){(void)j;(void)k;(void)v;return &json;}
cJSON *cJSON_AddNumberToObject(cJSON *j,const char *k,double v){(void)j;(void)k;(void)v;return &json;}
int cJSON_AddItemToObject(cJSON *j,const char *k,cJSON *v){(void)j;(void)k;(void)v;return 1;}
void cJSON_Delete(cJSON *j){(void)j;}
const char *zj_compat_error(zj_compat_result_t result){(void)result;return "TEST";}
/* Include the actual adapter so each simulated boot resets its file statics. */
#include "zkt_journal_runtime.c"
static void reset(void){
 state=(zj_boot_t){0};snapshot=(zj_boot_t){0};health_lock=NULL;published=false;
 owner=(zj_owner_health_t){0};transport=(zj_transport_health_t){0};
 owner_starts=transport_starts=capture_starts=proofs=clock_ms=0;mutex_fail=lock_fail=false;
}
static void tick(void){clock_ms+=1000;zj_runtime_step();}
int main(void){
 zj_boot_t health;
 assert(zj_runtime_boot_ready() && zj_runtime_legacy_capture_allowed());
 assert(!zj_runtime_writer_ready() && !zj_runtime_raw_source_required());
 tick();assert(zj_runtime_health(&health)&&health.phase==ZJ_BOOT_OFF&&!owner_starts);
 strcpy(app.version,"2.7.1");tick();assert(!owner_starts&&zj_runtime_legacy_capture_allowed());
 strcpy(app.version,ZJ_BRIDGE_VERSION);reset();
 assert(!zj_runtime_boot_ready() && zj_runtime_raw_source_required() && !zj_runtime_legacy_capture_allowed());
 mutex_fail=true;tick();assert(!zj_runtime_health(&health)&&!owner_starts);mutex_fail=false;
 secure=false;tick();assert(zj_runtime_health(&health)&&health.phase==ZJ_BOOT_SECURITY_HOLD);
 secure=true;running.subtype=0;tick();assert(!owner_starts);running.subtype=ESP_PARTITION_SUBTYPE_APP_OTA_0;
 config.provisioned=false;tick();assert(!owner_starts);config.provisioned=true;
#if CONFIG_NVS_ENCRYPTION
 for(unsigned i=0;i<5;i++)tick();
 assert(owner_starts==1&&transport_starts==1&&!capture_starts);
#if ZONE_LITE_JOURNAL_WRITES
 assert(proofs==1&&zj_runtime_boot_ready()&&!zj_runtime_writer_ready());
 assert(zj_runtime_legacy_capture_allowed()&&!zj_runtime_raw_source_required());
 assert(zj_runtime_health(&health)&&health.reader_ready&&health.phase==ZJ_BOOT_READY);
 lock_fail=true;assert(!zj_runtime_health(&health)&&!zj_runtime_boot_ready());
 assert(!zj_runtime_legacy_capture_allowed()&&zj_runtime_raw_source_required());lock_fail=false;
 clock_ms+=45000;assert(!zj_runtime_legacy_capture_allowed());tick();assert(zj_runtime_legacy_capture_allowed());
#else
 assert(!proofs&&!zj_runtime_boot_ready()&&zj_runtime_raw_source_required());
#endif
 reset();strcpy(app.version,"2.7.0");assert(zj_runtime_raw_source_required()&&!zj_runtime_writer_ready());
 for(unsigned i=0;i<5;i++)tick();
#if ZONE_LITE_JOURNAL_WRITES
 assert(capture_starts==1&&cutover&&zj_runtime_writer_ready()&&zj_runtime_boot_ready());
 clock_ms+=45000;assert(!zj_runtime_writer_ready()&&!zj_runtime_boot_ready()&&zj_runtime_raw_source_required());
 tick();assert(zj_runtime_writer_ready()&&capture_starts==1&&!zj_runtime_legacy_capture_allowed());
 strcpy(config.zkt_expected_serial,"REBOUND");tick();assert(!zj_runtime_writer_ready()&&!zj_runtime_boot_ready());
 strcpy(config.zkt_expected_serial,"TEST-SERIAL");
 /* Roll back and reboot. The persisted ADD authority starts bridge capture. */
 strcpy(app.version,ZJ_BRIDGE_VERSION);reset();pending_bridge=true;
 for(unsigned i=0;i<5;i++)tick();
 assert(zj_runtime_boot_ready()&&!zj_runtime_writer_ready()&&!zj_runtime_legacy_capture_allowed());
 assert(zj_runtime_raw_source_required()&&!proofs&&!capture_starts);
 pending_bridge=false;for(unsigned i=0;i<3;i++)tick();
 assert(zj_runtime_writer_ready()&&zj_runtime_raw_source_required()&&capture_starts==1&&proofs==1);
 reset();for(unsigned i=0;i<5;i++)tick();
 assert(zj_runtime_writer_ready()&&!zj_runtime_legacy_capture_allowed()&&capture_starts==1);
 owner.delivery_authority=ZJ_AUTHORITY_LEGACY;tick();
 assert(!zj_runtime_writer_ready()&&!zj_runtime_legacy_capture_allowed()&&zj_runtime_raw_source_required());
#else
 assert(!capture_starts&&!cutover&&!proofs&&!zj_runtime_writer_ready()&&!zj_runtime_boot_ready());
 assert(zj_runtime_raw_source_required());
 assert(zj_runtime_health(&health)&&health.phase==ZJ_BOOT_WRITER_DISABLED);
#endif
#else
 for(unsigned i=0;i<5;i++)tick();
 assert(!owner_starts&&!zj_runtime_boot_ready()&&zj_runtime_raw_source_required());
#endif
 puts("actual journal runtime authority, rollback, version and build gates passed");
}
