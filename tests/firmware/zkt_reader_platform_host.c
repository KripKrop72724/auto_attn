#include "zkt_reader_platform_host.h"
#include "zkt_reader_platform.h"
#include "durable_queue.h"
#include <assert.h>
#include <string.h>

static esp_app_desc_t app = {.project_name = "zone_lite", .version = ZJ_BRIDGE_VERSION};
static esp_app_desc_t prior = {.project_name = "zone_lite", .version = ZJ_BRIDGE_VERSION};
static esp_partition_t partitions[] = {
    {.address = 0x11000, .size = 0x6000, .type = 1, .subtype = 2, .label = "nvs"},
    {.address = 0x17000, .size = 0x2000, .type = 1, .label = "otadata"},
    {.address = 0x2a0000, .size = 0x280000, .subtype = 16, .label = "ota_0"},
    {.address = 0x520000, .size = 0x280000, .subtype = 17, .label = "ota_1"},
    {.address = 0x7a0000, .size = 0x800000, .type = 1, .subtype = 130, .label = "storage"},
};
static unsigned running = 3, other = 2, fault, reads, writes, opens, closes, commits, released;
static bool secure = true, exists, current_valid = true, previous_valid = true, reverse;
static bool writer_allowed;
static unsigned boot_slot = 2, selections, check_reads;
static unsigned invalidations;
static unsigned ota_count = 2;
static bool rollback_possible = true;
static int state_override = -1;
static int64_t clock_us = 1000000;
static uint8_t durable[ZJ_READER_PROOF_BYTES], pending[ZJ_READER_PROOF_BYTES], epoch[16] = {77};
struct iterator { unsigned index; };
static struct iterator iterator;
const esp_app_desc_t *esp_app_get_description(void) { return fault == 1 ? NULL : &app; }
const esp_partition_t *esp_ota_get_running_partition(void) { return fault == 2 ? NULL : &partitions[running]; }
const esp_partition_t *esp_ota_get_next_update_partition(const void *p)
{ assert(!p); return fault == 3 ? NULL : &partitions[other]; }
int esp_ota_get_partition_description(const esp_partition_t *p, esp_app_desc_t *out)
{ assert(p == &partitions[other]); *out = prior; return fault == 4 ? -1 : 0; }
int esp_partition_get_sha256(const esp_partition_t *p, uint8_t *out)
{ memset(out, p->subtype, 32); if (fault == 5) out[0] ^= 1;
  if (fault == 25) clock_us += 5000001;
  return fault == 6 ? -1 : 0; }
int esp_ota_get_state_partition(const esp_partition_t *p, esp_ota_img_states_t *state)
{ *state = p == &partitions[other] && state_override >= 0 ? state_override :
    (p == &partitions[running] ? current_valid : previous_valid) ? ESP_OTA_IMG_VALID : 1;
  return fault == 7 ? -1 : 0; }
int64_t esp_timer_get_time(void) { return clock_us; }
const esp_partition_t *esp_ota_get_boot_partition(void)
{
    if (fault == 26) clock_us += 5000001;
    if (fault == 18 || (fault == 23 && (selections || invalidations))) return NULL;
    if (fault == 24 && (selections || invalidations)) return &partitions[running];
    return &partitions[boot_slot];
}
int esp_ota_set_boot_partition(const esp_partition_t *p)
{
    assert(p == &partitions[other]);
    ++selections;
    if (fault == 21) return -1;
    boot_slot = other;
    state_override = ESP_OTA_IMG_NEW;
    return fault == 22 ? -1 : 0;
}
bool esp_ota_check_rollback_is_possible(void) { return rollback_possible; }
unsigned esp_ota_get_app_partition_count(void) { return ota_count; }
int esp_ota_mark_app_invalid_rollback(void)
{
    assert(previous_valid && (state_override == -1 || state_override == ESP_OTA_IMG_VALID));
    ++invalidations;
    if (fault != 31) { boot_slot = other; current_valid = false; }
    if (fault == 33) state_override = ESP_OTA_IMG_NEW;
    if (fault == 34) clock_us += 5000001;
    return fault == 31 || fault == 32 ? -1 : ESP_OK;
}
esp_partition_iterator_t esp_partition_find(int type, int subtype, const char *label)
{ assert(type == 255 && subtype == 255 && !label); iterator.index = 0; return fault == 8 ? NULL : &iterator; }
const esp_partition_t *esp_partition_get(esp_partition_iterator_t it)
{ assert(it == &iterator); return fault == 9 ? NULL : &partitions[reverse ? 4 - it->index : it->index]; }
esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t it)
{ assert(it == &iterator); return ++it->index == 5 ? NULL : it; }
void esp_partition_iterator_release(esp_partition_iterator_t it) { assert(!it || it == &iterator); ++released; }
bool esp_secure_boot_enabled(void) { return secure; }
int mbedtls_sha256(const unsigned char *in, size_t length, unsigned char *out, int is224)
{
    /* Platform-boundary spy; real SHA/HKDF/GCM vectors are tested separately. */
    assert(!is224);
    uint32_t crc = dq_crc32(in, length);
    for (unsigned i = 0; i < 32; ++i) out[i] = (uint8_t)(crc >> (8 * (i % 4)));
    return fault == 10 ? -1 : 0;
}
int nvs_open(const char *name, int mode, int *handle)
{ assert(!strcmp(name, "zkt_journal") && (mode == 0 || mode == 1)); if (fault == 11) return -1; ++opens; *handle = 1; return 0; }
void nvs_close(int handle) { assert(handle == 1); ++closes; }
int nvs_get_blob(int handle, const char *name, void *out, size_t *length)
{
    assert(handle == 1 && !strcmp(name, "reader_v1") && *length == sizeof(durable));
    ++reads; ++check_reads;
    if (fault == 12) return -1;
    if (!exists) return ESP_ERR_NVS_NOT_FOUND;
    memcpy(out, durable, sizeof(durable));
    if (fault == 35 && check_reads == 2) ((uint8_t *)out)[0] ^= 1;
    if (fault == 13) --*length;
    if (fault == 17 && writes) ((uint8_t *)out)[0] ^= 1;
    return 0;
}
int nvs_set_blob(int handle, const char *name, const void *in, size_t length)
{
    assert(handle == 1 && !strcmp(name, "reader_v1") && length == sizeof(pending));
    ++writes;
    memcpy(pending, in, length);
    return fault == 14 ? -1 : 0;
}
int nvs_commit(int handle)
{
    assert(handle == 1);
    ++commits;
    if (fault == 15) return -1;
    memcpy(durable, pending, sizeof(durable));
    exists = true;
    return fault == 16 ? -1 : 0;
}
static zj_compat_result_t check(void)
{
    zj_reader_selection_t selected;
    memset(&selected, 0xa5, sizeof(selected));
    check_reads = 0;
    zj_compat_result_t result = zj_reader_platform_check_evidence("TEST-TERMINAL", epoch, true, true, true, false,
        &writer_allowed, &selected);
    assert(opens == closes);
    assert(writer_allowed == (result == ZJ_COMPAT_OK && !strcmp(app.version, ZJ_WRITER_VERSION)));
    assert(selected.verified == writer_allowed);
    if (writer_allowed) {
        assert(!strcmp(selected.version, prior.version));
        assert(selected.slot_address == partitions[other].address && selected.slot_size == partitions[other].size);
        assert(selected.proof_generation > 0);
        for (unsigned i = 0; i < 32; ++i) assert(selected.image_digest[i] == partitions[other].subtype);
    } else {
        for (unsigned i = 0; i < sizeof(selected); ++i) assert(!((uint8_t *)&selected)[i]);
    }
    return result;
}
static zj_compat_result_t update(void)
{
    unsigned initial = writes;
    zj_compat_result_t result = zj_reader_platform_update("TEST-TERMINAL", epoch, true, true, true, false,
        partitions[other].address, partitions[other].size, ZJ_WRITER_VERSION);
    assert(writes == initial && opens == closes);
    return result;
}
static zj_compat_result_t select_reader(void)
{
    uint8_t expected[32]; memset(expected, 17, sizeof(expected));
    unsigned initial = writes;
    zj_compat_result_t result = zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000000U);
    assert(writes == initial && opens == closes && !selections);
    return result;
}
static zj_compat_result_t failed_boot(void)
{
    uint8_t expected[32]; memset(expected, 16, sizeof(expected));
    unsigned initial = writes;
    zj_compat_result_t result = zj_reader_platform_failed_boot("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000000U);
    assert(writes == initial && opens == closes);
    return result;
}
int main(void)
{
    zj_reader_identity_t source_identity;
    assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, NULL) == ZJ_COMPAT_INVALID);
#if !CONFIG_NVS_ENCRYPTION
    assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, &source_identity) == ZJ_COMPAT_SECURITY);
    assert(check() == ZJ_COMPAT_SECURITY && !writes);
    assert(update() == ZJ_COMPAT_SECURITY && !writes);
    assert(select_reader() == ZJ_COMPAT_SECURITY && !selections);
    assert(failed_boot() == ZJ_COMPAT_SECURITY && !invalidations);
    return 0;
#elif !ZONE_LITE_JOURNAL_WRITES
    assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, &source_identity) == ZJ_COMPAT_CAPTURE_DISABLED);
    assert(check() == ZJ_COMPAT_CAPTURE_DISABLED && !writes);
    assert(update() == ZJ_COMPAT_CAPTURE_DISABLED && !writes);
    assert(select_reader() == ZJ_COMPAT_CAPTURE_DISABLED && !selections);
    assert(failed_boot() == ZJ_COMPAT_CAPTURE_DISABLED && !invalidations);
    return 0;
#else
    assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, &source_identity) == ZJ_COMPAT_VERSION);
    current_valid = false;
    assert(check() == ZJ_COMPAT_SECURITY && !writes); /* Unconfirmed bridge. */
    assert(update() == ZJ_COMPAT_SECURITY && !writes);
    current_valid = true;
    assert(update() == ZJ_COMPAT_MISSING && !writes);
    assert(check() == ZJ_COMPAT_OK && writes == 1 && commits == 1 && released);
    assert(update() == ZJ_COMPAT_OK);
    for (unsigned f = 1; f <= 13; ++f) {
        if (f == 4) continue; /* Target contents are about to be replaced. */
        fault = f;
        assert(update() != ZJ_COMPAT_OK);
    }
    fault = 0;
    assert(zj_reader_platform_update("TEST-TERMINAL", epoch, true, true, true, false,
        partitions[other].address + 0x10000, partitions[other].size, ZJ_WRITER_VERSION) == ZJ_COMPAT_PROTECTED_SLOT);
    assert(zj_reader_platform_update("TEST-TERMINAL", epoch, true, true, true, false,
        partitions[other].address, partitions[other].size - 0x10000, ZJ_WRITER_VERSION) == ZJ_COMPAT_PROTECTED_SLOT);
    epoch[0] ^= 1; assert(update() == ZJ_COMPAT_BINDING); epoch[0] ^= 1;
    unsigned initial_writes = writes;
    reverse = true;
    assert(check() == ZJ_COMPAT_OK && writes == initial_writes);
    reverse = false;
    strcpy(app.version, ZJ_WRITER_VERSION); running = 2; other = 3; current_valid = false;
    assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, &source_identity) == ZJ_COMPAT_OK);
    assert(source_identity.image_digest[0] == 16 && !memcmp(source_identity.capture_epoch, epoch, 16));
    for (unsigned f=1; f<=10; ++f) {
        if (f==3 || f==4 || f==5 || f==7) continue; /* Other slot, a different valid digest, or boot VALID is not identity. */
        fault=f;
        assert(zj_reader_platform_writer_identity("TEST-TERMINAL", epoch, &source_identity) != ZJ_COMPAT_OK);
        for (unsigned i=0; i<sizeof(source_identity); ++i) assert(!((uint8_t *)&source_identity)[i]);
    }
    fault=0;
    assert(update() == ZJ_COMPAT_PROTECTED_SLOT);
    assert(check() == ZJ_COMPAT_OK && writes == initial_writes);
    fault = 35;
    assert(check() == ZJ_COMPAT_IO && !writer_allowed && writes == initial_writes);
    fault = 0;
#if CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK
    assert(select_reader() == ZJ_COMPAT_ANTI_ROLLBACK && !selections);
    assert(failed_boot() == ZJ_COMPAT_ANTI_ROLLBACK && !invalidations);
#else
    for (unsigned f = 1; f <= 13; ++f) {
        fault = f;
        assert(select_reader() != ZJ_COMPAT_OK && !selections);
    }
    fault = 0;
    uint8_t expected[32]; memset(expected, 17, sizeof(expected));
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        NULL, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_INVALID);
    expected[0] ^= 1;
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_ROLLBACK && !selections);
    expected[0] ^= 1;
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us) == ZJ_COMPAT_SELECTION_EXPIRED && !selections);
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000001U) == ZJ_COMPAT_SELECTION_EXPIRED && !selections);
    fault = 25;
    assert(select_reader() == ZJ_COMPAT_SELECTION_EXPIRED && !selections);
    fault = 26;
    assert(select_reader() == ZJ_COMPAT_SELECTION_EXPIRED && !selections);
    fault = 18;
    assert(select_reader() == ZJ_COMPAT_SELECTION_UNCERTAIN && !selections);
    fault = 0;
    boot_slot = 0;
    assert(select_reader() == ZJ_COMPAT_ROLLBACK && !selections);
    boot_slot = running;
    for (int invalid = 0; invalid < 5; ++invalid) {
        if (invalid == ESP_OTA_IMG_VALID) continue;
        state_override = invalid;
        assert(select_reader() == ZJ_COMPAT_SECURITY && !selections);
    }
    state_override = -1;
    uint8_t saved_proof[ZJ_READER_PROOF_BYTES]; memcpy(saved_proof, durable, sizeof(durable));
    durable[80] ^= 1;
    assert(select_reader() == ZJ_COMPAT_CORRUPT && !selections);
    memcpy(durable, saved_proof, sizeof(durable));
    assert(zj_reader_platform_select("CHANGED", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_BINDING && !selections);
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, false, false,
        expected, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_NOT_READY && !selections);
    memset(prior.version, 'x', sizeof(prior.version));
    assert(select_reader() == ZJ_COMPAT_IO && !selections);
    strcpy(prior.version, ZJ_BRIDGE_VERSION);
    memset(prior.project_name, 'x', sizeof(prior.project_name));
    assert(select_reader() == ZJ_COMPAT_IO && !selections);
    strcpy(prior.project_name, "zone_lite");
    rollback_possible = false;
    assert(select_reader() == ZJ_COMPAT_ROLLBACK && !invalidations);
    rollback_possible = true;
    ota_count = 3; assert(select_reader() == ZJ_COMPAT_SECURITY && !invalidations); ota_count = 2;
    current_valid = true; /* Explicit rollback of an already accepted writer. */
    assert(select_reader() == ZJ_COMPAT_OK && invalidations == 1 && boot_slot == other);
    esp_ota_img_states_t selected_state;
    assert(!current_valid && esp_ota_get_state_partition(&partitions[other], &selected_state) == ESP_OK);
    assert(selected_state == ESP_OTA_IMG_VALID);
    /* Lost success is repeatable without invalidating the selected bridge.
     * Its VALID state permits retained ADD capture before terminal stability. */
    assert(select_reader() == ZJ_COMPAT_OK && invalidations == 1);
    durable[80] ^= 1;
    assert(select_reader() == ZJ_COMPAT_CORRUPT && invalidations == 1);
    memcpy(durable, saved_proof, sizeof(durable));
    expected[0] ^= 1;
    assert(zj_reader_platform_select("TEST-TERMINAL", epoch, true, true, true, false,
        expected, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_ROLLBACK && invalidations == 1);
    expected[0] ^= 1;
    for (int invalid = 0; invalid < 5; ++invalid) {
        if (invalid == ESP_OTA_IMG_VALID) continue;
        state_override = invalid;
        assert(select_reader() == ZJ_COMPAT_SECURITY && invalidations == 1);
    }
    const unsigned rollback_faults[] = {31, 32, 23, 24, 33, 34};
    for (unsigned i = 0; i < sizeof(rollback_faults) / sizeof(rollback_faults[0]); ++i) {
        unsigned f = rollback_faults[i];
        invalidations = 0; boot_slot = running; state_override = -1; fault = f;
        current_valid = true;
        assert(select_reader() == ZJ_COMPAT_SELECTION_UNCERTAIN);
        assert(invalidations == 1 && !selections);
        fault = 0;
        if (f == 33) {
            assert(select_reader() == ZJ_COMPAT_SECURITY && invalidations == 1);
            continue; /* A NEW reader never becomes safe by retry or age. */
        }
        assert(select_reader() == ZJ_COMPAT_OK);
        assert(invalidations == (f == 31 ? 2U : 1U));
        assert(esp_ota_get_state_partition(&partitions[other], &selected_state) == ESP_OK);
        assert(selected_state == ESP_OTA_IMG_VALID);
    }
    selections = invalidations = 0; boot_slot = running; state_override = -1;
    for (unsigned f = 1; f <= 13; ++f) {
        fault = f; assert(failed_boot() != ZJ_COMPAT_OK && !invalidations);
    }
    fault = 0; rollback_possible = false;
    assert(failed_boot() == ZJ_COMPAT_ROLLBACK && !invalidations);
    rollback_possible = true;
    ota_count = 3; assert(failed_boot() == ZJ_COMPAT_SECURITY && !invalidations); ota_count = 2;
    state_override = ESP_OTA_IMG_NEW;
    assert(failed_boot() == ZJ_COMPAT_SECURITY && !invalidations); state_override = -1;
    uint8_t writer_digest[32]; memset(writer_digest, 16, sizeof(writer_digest));
    writer_digest[0] ^= 1;
    assert(zj_reader_platform_failed_boot("TEST-TERMINAL", epoch, true, true, true, false,
        writer_digest, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_ROLLBACK && !invalidations);
    writer_digest[0] ^= 1;
    assert(zj_reader_platform_failed_boot("TEST-TERMINAL", epoch, true, true, false, false,
        writer_digest, (uint64_t)clock_us + 5000000U) == ZJ_COMPAT_NOT_READY && !invalidations);
    assert(zj_reader_platform_failed_boot("TEST-TERMINAL", epoch, true, true, true, false,
        writer_digest, (uint64_t)clock_us) == ZJ_COMPAT_SELECTION_EXPIRED && !invalidations);
    fault = 25; assert(failed_boot() == ZJ_COMPAT_SELECTION_EXPIRED && !invalidations);
    fault = 0; epoch[0] ^= 1;
    assert(failed_boot() == ZJ_COMPAT_BINDING && !invalidations); epoch[0] ^= 1;
    fault = 31;
    assert(failed_boot() == ZJ_COMPAT_SELECTION_UNCERTAIN && invalidations == 1 && boot_slot == running);
    fault = 32;
    assert(failed_boot() == ZJ_COMPAT_SELECTION_UNCERTAIN && invalidations == 2 && boot_slot == other);
    fault = 0;
    assert(failed_boot() == ZJ_COMPAT_OK && invalidations == 2);
    durable[80] ^= 1; assert(failed_boot() == ZJ_COMPAT_CORRUPT && invalidations == 2);
    memcpy(durable, saved_proof, sizeof(durable));
    boot_slot = running;
    assert(failed_boot() == ZJ_COMPAT_OK && invalidations == 3 && boot_slot == other);
    boot_slot = running; fault = 34;
    assert(failed_boot() == ZJ_COMPAT_SELECTION_UNCERTAIN && invalidations == 4 && boot_slot == other);
    fault = 0;
    assert(failed_boot() == ZJ_COMPAT_OK && invalidations == 4);
    boot_slot = running;
#endif
    for (unsigned f = 1; f <= 13; ++f) {
        fault = f;
        assert(check() != ZJ_COMPAT_OK && writes == initial_writes);
    }
    fault = 0;
    previous_valid = false;
    assert(check() == ZJ_COMPAT_SECURITY);
    previous_valid = true;
    secure = false;
    assert(check() == ZJ_COMPAT_SECURITY);
    secure = true;
    assert(zj_reader_platform_check("TEST-TERMINAL", epoch, false, true, true, false, &writer_allowed) == ZJ_COMPAT_NOT_READY);
    assert(zj_reader_platform_check("TEST-TERMINAL", epoch, true, false, true, false, &writer_allowed) == ZJ_COMPAT_NOT_READY);
    assert(zj_reader_platform_check("TEST-TERMINAL", epoch, true, true, false, false, &writer_allowed) == ZJ_COMPAT_NOT_READY);
    assert(zj_reader_platform_check("TEST-TERMINAL", epoch, true, true, true, true, &writer_allowed) == ZJ_COMPAT_NOT_READY);
    assert(zj_reader_platform_check("CHANGED", epoch, true, true, true, false, &writer_allowed) == ZJ_COMPAT_BINDING);
    epoch[0] ^= 1; assert(check() == ZJ_COMPAT_BINDING); epoch[0] ^= 1;
    partitions[4].size -= 0x10000; assert(check() == ZJ_COMPAT_BINDING); partitions[4].size += 0x10000;
    partitions[0].encrypted = true; assert(check() == ZJ_COMPAT_BINDING); partitions[0].encrypted = false;
    strcpy(partitions[4].label, "different"); assert(check() == ZJ_COMPAT_BINDING); strcpy(partitions[4].label, "storage");
    strcpy(prior.version, "2.6.15"); assert(check() == ZJ_COMPAT_VERSION); strcpy(prior.version, ZJ_BRIDGE_VERSION);
    strcpy(prior.project_name, "zone_lite_hikvision"); assert(check() == ZJ_COMPAT_VERSION); strcpy(prior.project_name, "zone_lite");
    partitions[other].subtype = 0; assert(check() == ZJ_COMPAT_SECURITY); partitions[other].subtype = 17;
    partitions[running].subtype = 0; assert(check() == ZJ_COMPAT_SECURITY); partitions[running].subtype = 16;
    assert(check() == ZJ_COMPAT_OK && writes == initial_writes);
    strcpy(app.version, ZJ_BRIDGE_VERSION); running = 3; other = 2; current_valid = true;
    assert(check() == ZJ_COMPAT_OK && writes == initial_writes); /* Rollback reader. */
    for (unsigned f = 11; f <= 17; ++f) {
        exists = f == 12 || f == 13;
        memcpy(durable, pending, sizeof(durable));
        writes = commits = 0;
        fault = f;
        assert(check() != ZJ_COMPAT_OK);
        fault = 0;
        assert(check() == ZJ_COMPAT_OK); /* Restart rereads committed state. */
    }
    assert(zj_reader_platform_check(NULL, epoch, true, true, true, false, &writer_allowed) == ZJ_COMPAT_INVALID);
    assert(zj_reader_platform_check("", epoch, true, true, true, false, &writer_allowed) == ZJ_COMPAT_INVALID);
    assert(zj_reader_platform_check("TEST", NULL, true, true, true, false, &writer_allowed) == ZJ_COMPAT_INVALID);
    return 0;
#endif
}
