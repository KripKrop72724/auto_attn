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
#define ESP_PARTITION_SUBTYPE_ANY 255
#define ESP_PARTITION_SUBTYPE_APP_OTA_0 16
#define ESP_PARTITION_SUBTYPE_APP_OTA_1 17
#define ESP_OTA_IMG_VALID 2
#define ESP_OTA_IMG_NEW 0
#define ESP_OTA_IMG_PENDING_VERIFY 1
typedef int esp_err_t;
typedef int nvs_handle_t;
typedef int esp_ota_img_states_t;
typedef struct { char project_name[32], version[32]; } esp_app_desc_t;
typedef struct { uint32_t address, size; int type, subtype; bool encrypted; char label[17]; } esp_partition_t;
typedef struct iterator *esp_partition_iterator_t;
const esp_app_desc_t *esp_app_get_description(void);
const esp_partition_t *esp_ota_get_running_partition(void);
const esp_partition_t *esp_ota_get_next_update_partition(const void *);
const esp_partition_t *esp_ota_get_boot_partition(void);
int esp_ota_set_boot_partition(const esp_partition_t *);
bool esp_ota_check_rollback_is_possible(void);
int esp_ota_mark_app_invalid_rollback(void);
unsigned esp_ota_get_app_partition_count(void);
int64_t esp_timer_get_time(void);
int esp_ota_get_partition_description(const esp_partition_t *, esp_app_desc_t *);
int esp_partition_get_sha256(const esp_partition_t *, uint8_t *);
int esp_ota_get_state_partition(const esp_partition_t *, esp_ota_img_states_t *);
esp_partition_iterator_t esp_partition_find(int, int, const char *);
const esp_partition_t *esp_partition_get(esp_partition_iterator_t);
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t);
void esp_partition_iterator_release(esp_partition_iterator_t);
bool esp_secure_boot_enabled(void);
int mbedtls_sha256(const unsigned char *, size_t, unsigned char *, int);
int nvs_open(const char *, int, int *);
int nvs_get_blob(int, const char *, void *, size_t *);
int nvs_set_blob(int, const char *, const void *, size_t);
int nvs_commit(int);
void nvs_close(int);
