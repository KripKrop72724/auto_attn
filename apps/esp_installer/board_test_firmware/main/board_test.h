/*
 * State Life board hardware test program: shared declarations.
 *
 * See README.md in the project folder for the line protocol the installer
 * reads. Every protocol line starts with "SLTEST " and goes through sl_line()
 * so its exact text is part of the END line's CRC.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SL_FW_VERSION "1.0.0"
#define SL_PROTO 1
#define SL_MAX_ERR_LINES 8

/* ---- output (sl_out.c) ---- */

/* Print one protocol line: "SLTEST " + the formatted text. Thread safe. */
void sl_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
/* Print the END line with the CRC of every earlier SLTEST line; later lines
 * (CLEAN) are not part of the CRC. */
void sl_end(bool ok, const char *failed);
void sl_out_init(void);

/* Heartbeat: a task prints "SLTEST ALIVE ..." every 400 ms while the running
 * stage makes progress (sl_tick) or is inside a known slow call (sl_busy).
 * When the stage stops making progress the heartbeat goes silent, so the
 * installer sees a real hang as silence. */
void sl_heartbeat_start(void);
void sl_heartbeat_stop(void);
void sl_stage(const char *stage, int of);     /* new stage, prints ALIVE step=0 */
void sl_step(int step);                        /* 1-based step within the stage */
void sl_tick(uint32_t at);                     /* progress, current address */
void sl_busy(uint32_t ms);                     /* a slow call without ticks follows */
void sl_yield(void);                           /* lets the idle task run (task WDT) */

/* Per-stage error accounting. sl_err() counts every error and prints the
 * first SL_MAX_ERR_LINES ERR lines of the stage. Safe from both cores. */
void sl_errors_reset(void);
void sl_err(const char *test, uint32_t addr, uint32_t want, uint32_t got);
uint32_t sl_errors(void);
const char *sl_first_failed_test(void);

int64_t sl_now_us(void);

/* ---- two-core workers (board_test_main.c) ---- */
typedef void (*sl_job_fn)(void *arg);
/* Run job0 on core 0 and job1 on core 1 at the same time; returns when both
 * finished. Either may be NULL. */
void sl_run_on_both(sl_job_fn job0, void *arg0, sl_job_fn job1, void *arg1);

/* ---- stages: each prints exactly one RESULT line, returns false on fail ---- */
bool sl_test_psram(void);
bool sl_test_iram(void);
bool sl_test_flash(uint32_t begin_flash_id);
bool sl_test_cpu(void);
bool sl_test_wifi(void);

/* After END: erase what the flash test wrote and check it reads 0xFF. */
bool sl_flash_clean(void);
