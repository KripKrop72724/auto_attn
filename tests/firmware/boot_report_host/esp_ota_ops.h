#pragma once
typedef struct {
    char label[17];
} esp_partition_t;
const esp_partition_t *esp_ota_get_last_invalid_partition(void);
