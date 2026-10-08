#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define ESP_OK 0
#define ESP_ERR_NVS_NOT_FOUND 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define ESP_PARTITION_TYPE_ANY 255
#define ESP_PARTITION_TYPE_APP 0
#define ESP_PARTITION_TYPE_DATA 1
#define ESP_PARTITION_SUBTYPE_ANY 255
#define ESP_PARTITION_SUBTYPE_APP_FACTORY 0
#define ESP_PARTITION_SUBTYPE_APP_OTA_MIN 16
#define ESP_PARTITION_SUBTYPE_APP_OTA_0 16
#define ESP_PARTITION_SUBTYPE_APP_OTA_1 17
#define ESP_PARTITION_SUBTYPE_DATA_OTA 0
#define ESP_OTA_IMG_VALID 2
#define ESP_OTA_IMG_PENDING_VERIFY 1
#define ESP_MAC_WIFI_STA 0
#define ESP_IMAGE_VERIFY_SILENT 1
typedef int esp_err_t;
typedef int nvs_handle_t;
typedef int esp_ota_img_states_t;
typedef struct { char project_name[32], version[32]; } esp_app_desc_t;
typedef struct { uint32_t address,size;int type,subtype;bool encrypted;char label[17]; } esp_partition_t;
typedef struct { uint32_t offset,size; } esp_partition_pos_t;
typedef struct { uint32_t image_len; } esp_image_metadata_t;
typedef struct { uint32_t ota_seq,ota_state,crc; } esp_ota_select_entry_t;
typedef struct iterator *esp_partition_iterator_t;
typedef struct { uint32_t value; } mbedtls_sha256_context;
typedef struct { unsigned fields; } cJSON;
cJSON *cJSON_AddNumberToObject(cJSON *,const char *,double);
cJSON *cJSON_AddStringToObject(cJSON *,const char *,const char *);
cJSON *cJSON_AddBoolToObject(cJSON *,const char *,bool);
const esp_app_desc_t *esp_app_get_description(void);
bool esp_secure_boot_enabled(void);
bool esp_cpu_dbgr_is_attached(void);
int esp_read_mac(uint8_t *,int);
const esp_partition_t *esp_ota_get_running_partition(void);
const esp_partition_t *esp_ota_get_boot_partition(void);
int esp_ota_get_state_partition(const esp_partition_t *,esp_ota_img_states_t *);
int esp_ota_get_partition_description(const esp_partition_t *,esp_app_desc_t *);
int esp_partition_get_sha256(const esp_partition_t *,uint8_t *);
const esp_partition_t *esp_partition_find_first(int,int,const char *);
esp_partition_iterator_t esp_partition_find(int,int,const char *);
const esp_partition_t *esp_partition_get(esp_partition_iterator_t);
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t);
void esp_partition_iterator_release(esp_partition_iterator_t);
int esp_partition_read(const esp_partition_t *,size_t,void *,size_t);
unsigned esp_ota_get_app_partition_count(void);
uint32_t bootloader_common_ota_select_crc(const esp_ota_select_entry_t *);
bool esp_ota_check_rollback_is_possible(void);
int esp_ota_mark_app_invalid_rollback(void);
int esp_image_verify(int,const esp_partition_pos_t *,esp_image_metadata_t *);
int nvs_open(const char *,int,nvs_handle_t *);
int nvs_get_blob(nvs_handle_t,const char *,void *,size_t *);
int nvs_set_blob(nvs_handle_t,const char *,const void *,size_t);
int nvs_commit(nvs_handle_t);
void nvs_close(nvs_handle_t);
void mbedtls_sha256_init(mbedtls_sha256_context *);
int mbedtls_sha256_starts(mbedtls_sha256_context *,int);
int mbedtls_sha256_update(mbedtls_sha256_context *,const unsigned char *,size_t);
int mbedtls_sha256_finish(mbedtls_sha256_context *,unsigned char *);
void mbedtls_sha256_free(mbedtls_sha256_context *);
int mbedtls_sha256(const unsigned char *,size_t,unsigned char *,int);
void vTaskDelay(unsigned);
