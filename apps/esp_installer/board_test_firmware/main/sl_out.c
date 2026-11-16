/*
 * Protocol output, END-line CRC, heartbeat and per-stage error counting.
 *
 * END crc algorithm (the installer recomputes it):
 *   Take every SLTEST line printed before END, in the order printed, each
 *   line's exact text from "SLTEST" up to but not including the line end
 *   (the console turns "\n" into "\r\n"; neither is part of the text).
 *   Join them with a single "\n" (no "\n" after the last line) and take the
 *   standard CRC-32 (IEEE 802.3, reflected polynomial 0xEDB88320, initial
 *   value 0xFFFFFFFF, final XOR 0xFFFFFFFF; the same as zlib.crc32).
 *   It is computed here with esp_rom_crc32_le(), which inverts the value on
 *   entry and exit, so esp_rom_crc32_le(0, buf, len) is that standard CRC and
 *   chaining esp_rom_crc32_le(previous, next, n) continues it:
 *     crc = esp_rom_crc32_le(0, line1, len1);
 *     crc = esp_rom_crc32_le(crc, "\n", 1); crc = esp_rom_crc32_le(crc, line2, len2); ...
 *   Python: "%08x" % (zlib.crc32("\n".join(lines).encode("ascii")) & 0xffffffff)
 *   It is printed as 8 lowercase hex digits.
 */
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "board_test.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define LINE_MAX_LEN 240
#define ALIVE_EVERY_US (400 * 1000)
#define YIELD_EVERY_US (50 * 1000)

static SemaphoreHandle_t s_out_lock;
static uint32_t s_crc;
static uint32_t s_lines;
static bool s_crc_closed;

int64_t sl_now_us(void)
{
    return esp_timer_get_time();
}

void sl_out_init(void)
{
    s_out_lock = xSemaphoreCreateMutex();
    configASSERT(s_out_lock);
}

static void emit(const char *text, size_t len, bool in_crc)
{
    xSemaphoreTake(s_out_lock, portMAX_DELAY);
    if (in_crc && !s_crc_closed) {
        if (s_lines > 0) {
            s_crc = esp_rom_crc32_le(s_crc, (const uint8_t *)"\n", 1);
        }
        s_crc = esp_rom_crc32_le(s_crc, (const uint8_t *)text, len);
        s_lines++;
    }
    fwrite(text, 1, len, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    xSemaphoreGive(s_out_lock);
}

static size_t format_line(char *buf, const char *fmt, va_list ap)
{
    int head = snprintf(buf, LINE_MAX_LEN, "SLTEST ");
    int body = vsnprintf(buf + head, LINE_MAX_LEN - head, fmt, ap);
    size_t len = (size_t)head + (body > 0 ? (size_t)body : 0);
    if (len >= LINE_MAX_LEN) {
        len = LINE_MAX_LEN - 1; /* truncated text is what is printed and CRC'd */
    }
    return len;
}

void sl_line(const char *fmt, ...)
{
    char buf[LINE_MAX_LEN];
    va_list ap;
    va_start(ap, fmt);
    size_t len = format_line(buf, fmt, ap);
    va_end(ap);
    emit(buf, len, true);
}

void sl_end(bool ok, const char *failed)
{
    char buf[LINE_MAX_LEN];
    xSemaphoreTake(s_out_lock, portMAX_DELAY);
    uint32_t crc = s_lines > 0 ? s_crc : 0;
    s_crc_closed = true;
    int len = snprintf(buf, sizeof(buf), "SLTEST END result=%s failed=%s crc=%08lx",
                       ok ? "ok" : "fail", failed, (unsigned long)crc);
    xSemaphoreGive(s_out_lock);
    if (len >= (int)sizeof(buf)) {
        len = sizeof(buf) - 1;
    }
    emit(buf, (size_t)len, false);
}

/* ------------------------------------------------------------ heartbeat */

static portMUX_TYPE s_hb_mux = portMUX_INITIALIZER_UNLOCKED;
static struct {
    const char *stage;
    int step;
    int of;
    volatile uint32_t at;
    volatile uint32_t ticks;
    int64_t busy_until;
    bool on;
} s_hb;
static TaskHandle_t s_hb_task;
static volatile bool s_hb_running;

static void print_alive(const char *stage, int step, int of, uint32_t at)
{
    if (at) {
        sl_line("ALIVE stage=%s step=%d of=%d at=0x%08lx", stage, step, of, (unsigned long)at);
    } else {
        sl_line("ALIVE stage=%s step=%d of=%d at=0", stage, step, of);
    }
}

static void heartbeat_task(void *arg)
{
    (void)arg;
    uint32_t last_ticks = 0;
    int64_t last_print = sl_now_us();
    while (s_hb_running) {
        vTaskDelay(pdMS_TO_TICKS(100));
        portENTER_CRITICAL(&s_hb_mux);
        bool on = s_hb.on;
        const char *stage = s_hb.stage;
        int step = s_hb.step, of = s_hb.of;
        uint32_t at = s_hb.at, ticks = s_hb.ticks;
        int64_t busy_until = s_hb.busy_until;
        portEXIT_CRITICAL(&s_hb_mux);
        int64_t now = sl_now_us();
        if (!on || now - last_print < ALIVE_EVERY_US) {
            continue;
        }
        /* No progress and no announced slow call: stay silent (= hang). */
        if (ticks == last_ticks && now >= busy_until) {
            continue;
        }
        print_alive(stage, step, of, at);
        last_ticks = ticks;
        last_print = sl_now_us();
    }
    s_hb_task = NULL;
    vTaskDelete(NULL);
}

void sl_heartbeat_start(void)
{
    s_hb_running = true;
    BaseType_t ok = xTaskCreate(heartbeat_task, "sl_alive", 4096, NULL, 10, &s_hb_task);
    configASSERT(ok == pdPASS);
}

void sl_heartbeat_stop(void)
{
    portENTER_CRITICAL(&s_hb_mux);
    s_hb.on = false;
    portEXIT_CRITICAL(&s_hb_mux);
    s_hb_running = false;
    /* Wait for the task to leave so no ALIVE line can follow END. */
    for (int i = 0; i < 50 && s_hb_task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void sl_stage(const char *stage, int of)
{
    portENTER_CRITICAL(&s_hb_mux);
    s_hb.stage = stage;
    s_hb.step = 0;
    s_hb.of = of;
    s_hb.at = 0;
    s_hb.ticks++;
    s_hb.busy_until = 0;
    s_hb.on = true;
    portEXIT_CRITICAL(&s_hb_mux);
    print_alive(stage, 0, of, 0);
}

void sl_step(int step)
{
    portENTER_CRITICAL(&s_hb_mux);
    s_hb.step = step;
    s_hb.ticks++;
    portEXIT_CRITICAL(&s_hb_mux);
}

void sl_tick(uint32_t at)
{
    s_hb.at = at;
    s_hb.ticks++;
}

void sl_busy(uint32_t ms)
{
    int64_t until = sl_now_us() + (int64_t)ms * 1000;
    portENTER_CRITICAL(&s_hb_mux);
    s_hb.busy_until = until;
    s_hb.ticks++;
    portEXIT_CRITICAL(&s_hb_mux);
}

void sl_yield(void)
{
    static int64_t last[portNUM_PROCESSORS];
    int core = xPortGetCoreID();
    int64_t now = sl_now_us();
    if (now - last[core] >= YIELD_EVERY_US) {
        vTaskDelay(1); /* lets this core's idle task feed the task watchdog */
        last[core] = sl_now_us();
    }
}

/* ------------------------------------------------------- error counting */

static volatile uint32_t s_errors;
static volatile uint32_t s_err_lines;
static const char *volatile s_first_test;

void sl_errors_reset(void)
{
    s_errors = 0;
    s_err_lines = 0;
    s_first_test = NULL;
}

void sl_err(const char *test, uint32_t addr, uint32_t want, uint32_t got)
{
    __atomic_fetch_add(&s_errors, 1, __ATOMIC_RELAXED);
    const char *none = NULL;
    __atomic_compare_exchange_n(&s_first_test, &none, test, false, __ATOMIC_RELAXED,
                                __ATOMIC_RELAXED);
    if (__atomic_load_n(&s_err_lines, __ATOMIC_RELAXED) >= SL_MAX_ERR_LINES) {
        return;
    }
    if (__atomic_fetch_add(&s_err_lines, 1, __ATOMIC_RELAXED) < SL_MAX_ERR_LINES) {
        const char *stage;
        portENTER_CRITICAL(&s_hb_mux);
        stage = s_hb.stage;
        portEXIT_CRITICAL(&s_hb_mux);
        sl_line("ERR stage=%s test=%s addr=0x%08lx want=0x%08lx got=0x%08lx", stage, test,
                (unsigned long)addr, (unsigned long)want, (unsigned long)got);
    }
}

uint32_t sl_errors(void)
{
    return __atomic_load_n(&s_errors, __ATOMIC_RELAXED);
}

const char *sl_first_failed_test(void)
{
    const char *test = s_first_test;
    return test ? test : "none";
}
