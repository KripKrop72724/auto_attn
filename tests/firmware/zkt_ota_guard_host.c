#include "zkt_reader_platform_host.h"
#include "zkt_ota_guard.h"
#include "zkt_storage_owner.h"
#include "queue_store.h"
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <string.h>

static esp_app_desc_t app = {.project_name = "zone_lite", .version = "2.6.15"};
static bool no_app, locked, directory_open, refuse_lock, fail_open, fail_read, fail_close;
static bool orphan, refuse_submit, refuse_poll, refuse_abandon, timeout;
static int nvs_result = ESP_ERR_NVS_NOT_FOUND;
static unsigned nvs_opens, nvs_closes, entries, read_at, submits, polls, abandons, delay_polls;
static uint64_t now, live_ticket, next_ticket = 1;
static zj_compat_result_t compatibility = ZJ_COMPAT_OK;
static zj_result_t owner_result = ZJ_OK;
static struct dirent entry;
static int directory_token;

const esp_app_desc_t *esp_app_get_description(void) { return no_app ? NULL : &app; }
int64_t esp_timer_get_time(void) { return (int64_t)now; }
void vTaskDelay(unsigned ms) { assert(!locked && !directory_open); now += ms * 1000ULL; }
int nvs_open(const char *name, int mode, int *handle)
{ assert(!strcmp(name, "zkt_journal") && mode == NVS_READONLY); ++nvs_opens; *handle = 1; return nvs_result; }
void nvs_close(int handle) { assert(handle == 1); ++nvs_closes; }
bool qs_local_read_begin(void) { assert(!locked); if (refuse_lock) return false; locked = true; return true; }
void qs_local_end(bool persisted, int error)
{ assert(locked && !directory_open && persisted && !error); locked = false; }
DIR *opendir(const char *path)
{
    assert(locked && !directory_open && !strcmp(path, ZJ_DEVICE_DIRECTORY));
    if (fail_open) return NULL;
    directory_open = true; read_at = 0;
    return (DIR *)&directory_token;
}
struct dirent *readdir(DIR *directory)
{
    assert(locked && directory_open && directory == (DIR *)&directory_token);
    if (fail_read) { errno = EIO; return NULL; }
    if (read_at++ >= entries) return NULL;
    strcpy(entry.d_name, orphan ? ZJ_DEVICE_BASENAME "-orphan" : "legacy.outbox");
    return &entry;
}
int closedir(DIR *directory)
{
    assert(locked && directory_open && directory == (DIR *)&directory_token);
    directory_open = false;
    return fail_close ? -1 : 0;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    assert(!locked && !directory_open && !live_ticket);
    assert(request->operation == ZJ_OTA_CHECK && request->input.ota.address == 0x2a0000 &&
        request->input.ota.size == 0x280000 && !strcmp(request->input.ota.version, ZJ_WRITER_VERSION));
    ++submits; polls = 0;
    if (refuse_submit) { *ticket = 0; return false; }
    *ticket = live_ticket = next_ticket++;
    return true;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *complete)
{
    assert(!locked && !directory_open && live_ticket && ticket == live_ticket);
    *complete = false; ++polls;
    if (refuse_poll) return false;
    if (timeout || polls <= delay_polls) return true;
    *reply = (zj_reply_t){.result = owner_result, .compatibility = compatibility};
    *complete = true; live_ticket = 0;
    return true;
}
bool zj_owner_abandon(uint64_t ticket)
{
    assert(!locked && !directory_open && live_ticket && ticket == live_ticket);
    ++abandons;
    if (refuse_abandon) return false;
    live_ticket = 0;
    return true;
}
static const char *check(const char *version)
{
    const char *error = zj_ota_before_download(0x2a0000, 0x280000, version);
    assert(!locked && !directory_open);
    if (error) assert(strlen(error) < 64);
    return error;
}
static void expect(const char *error) { assert(!strcmp(check(ZJ_WRITER_VERSION), error)); }
int main(void)
{
    entries = 2;
    assert(!check(ZJ_BRIDGE_VERSION) && nvs_opens == 1 && !nvs_closes && !submits);
    expect("JOURNAL_OTA_BRIDGE_REQUIRED");
    nvs_result = ESP_OK;
    assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_LEGACY_EVIDENCE_PRESENT") && nvs_closes == 1);
    nvs_result = -1;
    assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_READER_PROOF_UNAVAILABLE"));
    nvs_result = ESP_ERR_NVS_NOT_FOUND;
    orphan = true;
    assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_LEGACY_EVIDENCE_PRESENT")); orphan = false;
    for (unsigned scenario = 0; scenario < 4; ++scenario) {
        refuse_lock = scenario == 0; fail_open = scenario == 1; fail_read = scenario == 2; fail_close = scenario == 3;
        assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_OTA_STORAGE_UNAVAILABLE"));
    }
    refuse_lock = fail_open = fail_read = fail_close = false;
    entries = 1024; assert(!check(ZJ_BRIDGE_VERSION));
    entries = 1025; assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_OTA_DIRECTORY_LIMIT"));
    no_app = true; expect("JOURNAL_OTA_IMAGE_UNKNOWN"); no_app = false;
    strcpy(app.project_name, "zone_lite_hikvision"); expect("JOURNAL_OTA_IMAGE_UNKNOWN");
    strcpy(app.project_name, "zone_lite");
    assert(!strcmp(check(NULL), "JOURNAL_OTA_TARGET_UNSUPPORTED"));
    assert(!strcmp(check(""), "JOURNAL_OTA_TARGET_UNSUPPORTED"));
    assert(!strcmp(check("01234567890123456789012345678901"), "JOURNAL_OTA_TARGET_UNSUPPORTED"));

    strcpy(app.version, ZJ_WRITER_VERSION);
    unsigned original_reads = nvs_opens;
    expect("JOURNAL_ROLLBACK_SLOT_PROTECTED");
    assert(!strcmp(check(ZJ_BRIDGE_VERSION), "JOURNAL_ROLLBACK_SLOT_PROTECTED"));
    assert(!strcmp(check("2.7.1"), "JOURNAL_ROLLBACK_SLOT_PROTECTED"));
    assert(!submits && nvs_opens == original_reads);

    strcpy(app.version, ZJ_BRIDGE_VERSION);
    assert(!strcmp(check("2.6.15"), "JOURNAL_OTA_TARGET_UNSUPPORTED"));
    assert(!strcmp(check("2.7.1"), "JOURNAL_OTA_TARGET_UNSUPPORTED"));
    assert(!check(ZJ_WRITER_VERSION) && submits == 1 && !live_ticket && !abandons);
    delay_polls = 40;
    assert(!check(ZJ_WRITER_VERSION) && polls == 41 && !live_ticket); delay_polls = 0;
    refuse_submit = true; expect("JOURNAL_OTA_CHECK_UNAVAILABLE"); refuse_submit = false;
    compatibility = ZJ_COMPAT_CORRUPT; owner_result = ZJ_INVALID;
    expect("JOURNAL_READER_PROOF_CORRUPT");
    compatibility = ZJ_COMPAT_OK; owner_result = ZJ_IO; expect("JOURNAL_OTA_CHECK_FAILED"); owner_result = ZJ_OK;
    timeout = true; expect("JOURNAL_OTA_CHECK_TIMEOUT"); assert(!live_ticket && abandons == 1); timeout = false;
    refuse_poll = true; expect("JOURNAL_OTA_CHECK_TIMEOUT"); assert(!live_ticket && abandons == 2); refuse_poll = false;

    /* Repeated failed abandon does not consume more mailbox slots. A later
     * result, even successful, never authorizes another download assignment. */
    timeout = refuse_abandon = true;
    expect("JOURNAL_OTA_CHECK_TIMEOUT"); assert(live_ticket);
    unsigned before = submits;
    for (unsigned i = 0; i < 10; ++i) expect("JOURNAL_OTA_CHECK_PENDING");
    assert(submits == before && live_ticket);
    refuse_abandon = timeout = false;
    compatibility = ZJ_COMPAT_MISSING;
    expect("JOURNAL_READER_PROOF_MISSING");
    assert(submits == before + 1 && !live_ticket);
    compatibility = ZJ_COMPAT_OK; assert(!check(ZJ_WRITER_VERSION));
    assert(nvs_opens == original_reads);
    return 0;
}
