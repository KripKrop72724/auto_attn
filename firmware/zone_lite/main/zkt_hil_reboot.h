#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Reuses configuration-command payload capacity. The 64-bit deadline can add
 * alignment padding, but never a second full payload per queued command. */
typedef struct {
    char run_id[37], boot_id[48], application_sha256[65];
    int64_t expires_at;
} zhr_binding_t;
typedef struct {
    zhr_binding_t binding;
    char command_id[48], terminal_serial[80];
    uint64_t deadline_us;
} zhr_attempt_t;
typedef struct {
    int64_t epoch;
    uint64_t uptime_ms;
} zhr_checkpoint_t;

bool zhr_valid(const zhr_attempt_t *attempt);
bool zhr_deadline(zhr_attempt_t *attempt, const char *boot, const char *image,
                  const char *serial, int64_t epoch, uint64_t uptime_us);
bool zhr_before_deadline(const zhr_attempt_t *attempt, int64_t epoch, uint64_t uptime_us);
/* RAM only, called AFTER the idle owner acknowledges shutdown. No I/O, locks,
 * allocation or await may occur between this witness and esp_restart(). */
void zhr_witness_arm(const zhr_attempt_t *attempt, int64_t epoch, uint64_t uptime_us);
bool zhr_witness_recovered(const zhr_attempt_t *attempt, const char *boot_after,
                           const char *image, bool software_reset, zhr_checkpoint_t *out);
void zhr_witness_clear(void);
void zhr_witness_cancel(const char *command_id);
