#include "zkt_hil_reboot.h"
#include "esp_attr.h"
#include <stddef.h>
#include <string.h>

#define ZHR_MAGIC UINT32_C(0x5a485231)
#define ZHR_MIN_EPOCH INT64_C(1700000000)
typedef struct {
    uint32_t magic;
    zhr_attempt_t attempt;
    zhr_checkpoint_t checkpoint;
    uint32_t crc;
} zhr_witness_t;
/* Software resets retain this witness, power-on contents are untrusted. It is
 * deliberately separate from the bridge-compatible attendance storage ABI. */
static RTC_NOINIT_ATTR volatile zhr_witness_t witness;

static bool bounded(const char *text, size_t capacity)
{
    if (!text || !*text || !memchr(text, 0, capacity)) return false;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p)
        if (*p < 0x21 || *p > 0x7e || *p == '"' || *p == '\\') return false;
    return true;
}
static bool hexadecimal(const char *text, size_t length)
{
    if (strlen(text) != length) return false;
    for (size_t i = 0; i < length; ++i)
        if (!((text[i] >= '0' && text[i] <= '9') || (text[i] >= 'a' && text[i] <= 'f'))) return false;
    return true;
}
static bool uuid(const char *text)
{
    if (strlen(text) != 36) return false;
    for (unsigned i = 0; i < 36; ++i) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (text[i] != '-') return false; }
        else if (!((text[i] >= '0' && text[i] <= '9') || (text[i] >= 'a' && text[i] <= 'f'))) return false;
    }
    return true;
}
bool zhr_valid(const zhr_attempt_t *a)
{
    return a && bounded(a->command_id, sizeof(a->command_id)) && uuid(a->command_id) &&
        bounded(a->binding.run_id, sizeof(a->binding.run_id)) && uuid(a->binding.run_id) &&
        bounded(a->binding.boot_id, sizeof(a->binding.boot_id)) &&
        bounded(a->binding.application_sha256, sizeof(a->binding.application_sha256)) &&
        hexadecimal(a->binding.application_sha256, 64) &&
        bounded(a->terminal_serial, sizeof(a->terminal_serial)) && a->binding.expires_at > ZHR_MIN_EPOCH;
}
bool zhr_deadline(zhr_attempt_t *a, const char *boot, const char *image,
                  const char *serial, int64_t epoch, uint64_t uptime_us)
{
    if (!zhr_valid(a) || !boot || !image || !serial || strcmp(boot, a->binding.boot_id) ||
        strcmp(image, a->binding.application_sha256) || strcmp(serial, a->terminal_serial) ||
        epoch < ZHR_MIN_EPOCH || a->binding.expires_at <= epoch || a->binding.expires_at - epoch > 60)
        return false;
    uint64_t remaining = (uint64_t)(a->binding.expires_at - epoch) * 1000000U;
    if (uptime_us > UINT64_MAX - remaining) return false;
    a->deadline_us = uptime_us + remaining;
    return true;
}
bool zhr_before_deadline(const zhr_attempt_t *a, int64_t epoch, uint64_t uptime_us)
{
    return a && a->deadline_us && epoch >= ZHR_MIN_EPOCH && epoch < a->binding.expires_at &&
        uptime_us < a->deadline_us;
}
static uint32_t checksum(const zhr_witness_t *value)
{
    uint32_t crc = UINT32_MAX;
    const uint8_t *bytes = (const uint8_t *)value;
    for (size_t i = 0; i < offsetof(zhr_witness_t, crc); ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (0U - (crc & 1U)));
    }
    return ~crc;
}
void zhr_witness_clear(void)
{
    volatile uint8_t *bytes = (volatile uint8_t *)&witness;
    for (size_t i = 0; i < sizeof(witness); ++i) bytes[i] = 0;
}
void zhr_witness_cancel(const char *command_id)
{
    if (!command_id || !*command_id || strlen(command_id) >= sizeof(witness.attempt.command_id)) return;
    char saved[sizeof(witness.attempt.command_id)];
    for (size_t i = 0; i < sizeof(saved); ++i) saved[i] = witness.attempt.command_id[i];
    if (memchr(saved, 0, sizeof(saved)) && !strcmp(saved, command_id)) zhr_witness_clear();
}
void zhr_witness_arm(const zhr_attempt_t *a, int64_t epoch, uint64_t uptime_us)
{
    zhr_witness_clear();
    if (!zhr_valid(a) || !zhr_before_deadline(a, epoch, uptime_us)) return;
    zhr_witness_t prepared = {.magic = ZHR_MAGIC, .attempt = *a,
        .checkpoint = {.epoch = epoch, .uptime_ms = uptime_us / 1000U}};
    prepared.crc = checksum(&prepared);
    /* Volatile stores are observable even immediately before a noreturn
     * restart. Publish magic last; an interrupted copy cannot be evidence. */
    const uint8_t *input = (const uint8_t *)&prepared;
    volatile uint8_t *output = (volatile uint8_t *)&witness;
    for (size_t i = sizeof(prepared.magic); i < sizeof(prepared); ++i) output[i] = input[i];
    witness.magic = ZHR_MAGIC;
}
bool zhr_witness_recovered(const zhr_attempt_t *a, const char *boot_after,
                           const char *image, bool software_reset, zhr_checkpoint_t *out)
{
    if (out) memset(out, 0, sizeof(*out));
    zhr_witness_t saved;
    const volatile uint8_t *input = (const volatile uint8_t *)&witness;
    uint8_t *output = (uint8_t *)&saved;
    for (size_t i = 0; i < sizeof(saved); ++i) output[i] = input[i];
    if (!out || !zhr_valid(a) || !software_reset || !boot_after || !*boot_after || !image ||
        saved.magic != ZHR_MAGIC || saved.crc != checksum(&saved) ||
        !zhr_valid(&saved.attempt) || !strcmp(a->binding.boot_id, boot_after) ||
        strcmp(image, a->binding.application_sha256) ||
        strcmp(a->command_id, saved.attempt.command_id) ||
        strcmp(a->binding.run_id, saved.attempt.binding.run_id) ||
        strcmp(a->binding.boot_id, saved.attempt.binding.boot_id) ||
        strcmp(a->binding.application_sha256, saved.attempt.binding.application_sha256) ||
        strcmp(a->terminal_serial, saved.attempt.terminal_serial) ||
        a->binding.expires_at != saved.attempt.binding.expires_at ||
        saved.checkpoint.epoch < ZHR_MIN_EPOCH || saved.checkpoint.epoch >= a->binding.expires_at)
        return false;
    *out = saved.checkpoint;
    return true;
}
