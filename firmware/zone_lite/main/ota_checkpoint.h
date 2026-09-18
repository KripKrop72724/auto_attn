#pragma once
#include "durable_queue.h"
#include <string.h>

#define OTA_CHECKPOINT_VERSION 1U
#define OTA_APPLICATION_MAX_BYTES 0x280000U
typedef struct {
    char deployment_id[48];
    char release_id[64];
    char target_version[32];
    char image_sha256[65];
    char download_url[512];
    uint32_t image_size;
    uint32_t bytes_written;
    char state[32];
} ota_journal_t;
typedef struct {
    uint32_t version, generation;
    ota_journal_t journal;
    uint32_t crc;
} ota_checkpoint_t;

static inline bool ota_journal_valid(const ota_journal_t *j)
{
    if (!j || !memchr(j->deployment_id, 0, sizeof(j->deployment_id)) ||
        !memchr(j->release_id, 0, sizeof(j->release_id)) ||
        !memchr(j->target_version, 0, sizeof(j->target_version)) ||
        !memchr(j->image_sha256, 0, sizeof(j->image_sha256)) ||
        !memchr(j->download_url, 0, sizeof(j->download_url)) ||
        !memchr(j->state, 0, sizeof(j->state))) return false;
    if (!strcmp(j->state, "IDLE")) return !j->deployment_id[0] && !j->image_size && !j->bytes_written;
    if (strcmp(j->state, "DOWNLOADING") && strcmp(j->state, "READY_TO_BOOT") &&
        strcmp(j->state, "RECONCILING")) return false;
    return j->deployment_id[0] && j->release_id[0] && j->target_version[0] &&
        strlen(j->image_sha256) == 64 && strspn(j->image_sha256, "0123456789abcdef") == 64 &&
        !strncmp(j->download_url, "https://", 8) && j->download_url[8] &&
        j->image_size && j->image_size <= OTA_APPLICATION_MAX_BYTES &&
        j->bytes_written <= j->image_size &&
        (!strcmp(j->state, "DOWNLOADING") || j->bytes_written == j->image_size);
}
static inline bool ota_checkpoint_valid(const ota_checkpoint_t *c)
{
    return c && c->version == OTA_CHECKPOINT_VERSION && c->generation &&
        ota_journal_valid(&c->journal) && c->crc == dq_crc32(c, offsetof(ota_checkpoint_t, crc));
}
