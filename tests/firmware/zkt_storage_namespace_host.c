#include "zone_storage_paths.h"
#include "zkt_storage_owner.h"
#include "zkt_legacy_attendance.h"
#include "uid_cache.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#define SEEN_UID_CAPACITY 16U
#define MALLOC_CAP_SPIRAM 1U
#define MALLOC_CAP_8BIT 2U
#define LED_STATUS_LOCAL_FAILURE 1
#define ESP_OK 0
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
typedef int esp_err_t;
typedef struct {
    const char *base_path, *partition_label;
    unsigned max_files;
    bool format_if_mount_failed;
} esp_vfs_spiffs_conf_t;
static int g_seen_lock;
static uid_cache_t g_seen_cache;
static char mounted[64], journal_prefix[128];
static unsigned legacy_reads;
static bool pending_restored = true, blocked_restored = true, backlog, owner_required;
static bool legacy_attendance_owner_required(void) { return owner_required; }
static unsigned storage_faults;
static int xSemaphoreCreateMutex(void) { return 1; }
static void *heap_caps_calloc(size_t count, size_t bytes, unsigned flags)
{ assert(flags == (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)); return calloc(count, bytes); }
static void heap_caps_free(void *pointer) { free(pointer); }
static esp_err_t esp_vfs_spiffs_register(const esp_vfs_spiffs_conf_t *config)
{
    assert(config && !config->partition_label && !config->format_if_mount_failed && config->max_files == 16);
    assert(strlen(config->base_path) < sizeof(mounted));
    strcpy(mounted, config->base_path); return ESP_OK;
}
static bool in_mount(const char *path)
{ return mounted[0] && !strncmp(path, mounted, strlen(mounted)) && path[strlen(mounted)] == '/'; }
static bool storage_upgrade_init(void) { return true; }
bool qs_init(void) { return true; }
static bool restore_pending_backup_if_needed(void) { assert(mounted[0]); return pending_restored; }
static bool restore_blocked_backup_if_needed(void) { assert(mounted[0]); return blocked_restored; }
static void load_seen_from_file(const char *path) { assert(in_mount(path)); ++legacy_reads; }
static bool file_has_nonempty_line(const char *path) { assert(in_mount(path)); return false; }
static void led_status_set_backlog(bool pending) { backlog = pending; }
static void led_status_fault(int code) { assert(code == LED_STATUS_LOCAL_FAILURE); ++storage_faults; }
bool zj_owner_start(const char *prefix, const zj_metadata_t *metadata)
{
    assert(in_mount(prefix) && metadata && !strcmp(metadata->terminal_serial, "SYNTHETIC-TERMINAL"));
    assert(strlen(prefix) < sizeof(journal_prefix)); strcpy(journal_prefix, prefix); return true;
}
#include "storage_namespace_actual.inc"
int main(void)
{
    storage_init(); /* Actual boot function records the actual VFS mount. */
    assert(g_queue_store_ready && g_seen_lock && legacy_reads == 3 && !backlog && !storage_faults);
    assert(start_owner(NULL, "SYNTHETIC-TERMINAL")); /* Actual runtime adapter. */
    assert(!strcmp(mounted, ZJ_DEVICE_DIRECTORY)); /* OTA evidence directory. */
    assert(!strcmp(journal_prefix, ZJ_DEVICE_PREFIX));
    const char *paths[] = {PENDING_PATH, BLOCKED_PATH, ACKED_PATH, ZC_ACTIVE_PATH,
        ZC_COMMAND_ACTIVE_PATH, ZI_PROCESSED_PATH, ZI_CANCELLED_PATH};
    for (unsigned i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) assert(in_mount(paths[i]));
    assert(!strcmp(PENDING_PATH,ZOL_PENDING_PATH) && !strcmp(BLOCKED_PATH,ZOL_BLOCKED_PATH));
    assert(!strcmp(PENDING_BACKUP_PATH,ZOL_PENDING_BACKUP_PATH) && !strcmp(PENDING_TMP_PATH,ZOL_PENDING_TEMP_PATH));
    assert(!strcmp(BLOCKED_RECOVERY_BACKUP_PATH,ZOL_BLOCKED_BACKUP_PATH) && !strcmp(BLOCKED_RECOVERY_TMP_PATH,ZOL_BLOCKED_TEMP_PATH));
    pending_restored = false;
    storage_init();
    assert(backlog && storage_faults == 1); /* Later file probing cannot erase the failed restore. */
    pending_restored = true; blocked_restored = false;
    storage_init();
    assert(backlog && storage_faults == 2);
    owner_required=true;
    unsigned before=legacy_reads;
    storage_init();
    assert(backlog && storage_faults==2 && legacy_reads==before);
    free(g_seen_cache.keys);
    free(g_seen_cache.occupied);
}
