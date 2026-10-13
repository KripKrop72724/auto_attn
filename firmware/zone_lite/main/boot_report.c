#include "boot_report.h"

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_cpu.h"
#include "esp_cpu_utils.h"
#include "esp_debug_helpers.h"
#include "esp_err.h"
#include "esp_memory_utils.h"
#include "esp_ota_ops.h"
#include "esp_private/cache_utils.h"
#include "esp_private/panic_internal.h"
#include "esp_private/panic_reason.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "xtensa/corebits.h"
#include "xtensa_context.h"

#define BOOT_TOUCH_PERIOD_US (10LL * 1000LL * 1000LL)
#define BOOT_TRACE_CORES 2U
#define BOOT_TRACE_DEPTH 8U

/* ESP-IDF 5.5 declares this in esp_system's private esp_private/cache_err_int.h,
 * which other components cannot include. The layout is ESP-IDF's. */
typedef struct {
    const char *err_str;
    uint32_t vaddr;
    uint32_t size;
} esp_cache_err_info_t;
void esp_cache_err_get_panic_info(esp_cache_err_info_t *err_info);

/* The panic hook is placed like ESP-IDF's own esp_panic_handler: in IRAM
 * only when CONFIG_ESP_PANIC_HANDLER_IRAM asks for it. Either way ESP-IDF
 * re-enables the flash cache before the handler runs. */
#if defined(CONFIG_ESP_PANIC_HANDLER_IRAM) && CONFIG_ESP_PANIC_HANDLER_IRAM
#define PANIC_ATTR IRAM_ATTR
#else
#define PANIC_ATTR
#endif

/* Survives every reset except a power cut. The checksum rejects the random
 * contents RTC memory holds after power-on. */
typedef struct {
    uint32_t magic;
    uint32_t boot_count, panic_count, task_wdt_count, int_wdt_count, other_wdt_count, brownout_count;
    /* The rest describes one boot and is cleared when a boot starts. */
    uint32_t uptime_seconds, stage;
    uint32_t panic_recorded, panic_exception, panic_address, panic_core;
    char panic_task[16];
    char panic_detail[96];
    uint32_t checksum;
} boot_rtc_t;

/* A record written with another layout is never misread as this one. */
#define BOOT_RTC_MAGIC (UINT32_C(0x5A4C4252) ^ (uint32_t)sizeof(boot_rtc_t))

/* What ESP-IDF prints on the console at a panic, kept for the next boot: the
 * panic reason, the cache error and fault address, and each core's task and
 * backtrace. 3FL's 2.7.2 cache error (12 October 2026) left only "FAULT at
 * 0x00000000": ESP-IDF fills no address for a cache error and could not tell
 * which core caused it. It follows boot_rtc_t, whose 2.7.1 layout an older
 * image still reads after a rollback. */
typedef struct {
    uint32_t magic;
    uint32_t boot_count; /* the boot this describes */
    uint32_t fault_address;
    uint8_t fault_address_valid;
    uint8_t corrupted; /* bit per core: the backtrace stopped at an insane frame */
    uint8_t depth[BOOT_TRACE_CORES]; /* backtrace entries; 0 when the core left no frame */
    char reason[40];
    char cache_error[72];
    char task[BOOT_TRACE_CORES][16];
    uint32_t pc[BOOT_TRACE_CORES];
    uint32_t backtrace[BOOT_TRACE_CORES][BOOT_TRACE_DEPTH];
    uint32_t checksum;
} boot_rtc_trace_t;

#define BOOT_TRACE_MAGIC (UINT32_C(0x5A4C4254) ^ (uint32_t)sizeof(boot_rtc_trace_t))

typedef struct {
    esp_reset_reason_t reason;
    bool rtc_valid; /* the previous boot's record survived */
    bool trace_valid; /* and so did its panic trace */
    boot_rtc_t previous;
    boot_rtc_trace_t trace;
    char last_invalid_partition[17];
} boot_report_t;

/* One RTC object, the 2.7.1 record first: an image that only knows that
 * record finds it at the same address after a rollback, whatever order the
 * compiler emits variables in. */
typedef struct {
    boot_rtc_t rtc;
    boot_rtc_trace_t trace;
} boot_retained_t;

static RTC_NOINIT_ATTR boot_retained_t s_retained;
#define s_rtc (s_retained.rtc)
#define s_trace (s_retained.trace)
static boot_report_t s_report;
/* Zeroed at every boot: whether this boot has started its part of s_rtc. */
static bool s_started;
/* Updates from different tasks must not leave a torn checksum. The panic hook
 * never takes this lock. */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

static uint32_t PANIC_ATTR fnv1a(const void *data, size_t length)
{
    const unsigned char *bytes = data;
    uint32_t sum = UINT32_C(2166136261);
    for (size_t index = 0; index < length; ++index) {
        sum = (sum ^ bytes[index]) * UINT32_C(16777619);
    }
    return sum;
}

static uint32_t PANIC_ATTR checksum(const boot_rtc_t *rtc)
{
    return fnv1a(rtc, offsetof(boot_rtc_t, checksum));
}

static uint32_t PANIC_ATTR trace_checksum(const boot_rtc_trace_t *trace)
{
    return fnv1a(trace, offsetof(boot_rtc_trace_t, checksum));
}

static void PANIC_ATTR seal(void)
{
    s_rtc.checksum = checksum(&s_rtc);
}

static bool PANIC_ATTR record_valid(void)
{
    return s_rtc.magic == BOOT_RTC_MAGIC && s_rtc.checksum == checksum(&s_rtc);
}

static void PANIC_ATTR reset_record(void)
{
    s_rtc.magic = BOOT_RTC_MAGIC;
    s_rtc.boot_count = s_rtc.panic_count = s_rtc.task_wdt_count = 0;
    s_rtc.int_wdt_count = s_rtc.other_wdt_count = s_rtc.brownout_count = 0;
}

/* An if chain, not a switch: a jump table would live in flash. */
static void PANIC_ATTR count_reset(esp_reset_reason_t reason)
{
    if (reason == ESP_RST_PANIC) ++s_rtc.panic_count;
    else if (reason == ESP_RST_TASK_WDT) ++s_rtc.task_wdt_count;
    else if (reason == ESP_RST_INT_WDT) ++s_rtc.int_wdt_count;
    else if (reason == ESP_RST_WDT) ++s_rtc.other_wdt_count;
    else if (reason == ESP_RST_BROWNOUT) ++s_rtc.brownout_count;
}

static void PANIC_ATTR clear_boot_fields(void)
{
    s_rtc.uptime_seconds = 0;
    s_rtc.stage = BOOT_STAGE_NONE;
    s_rtc.panic_recorded = s_rtc.panic_exception = s_rtc.panic_address = s_rtc.panic_core = 0;
    s_rtc.panic_task[0] = '\0';
    s_rtc.panic_detail[0] = '\0';
}

/* When startup code fails before boot_report_init runs, s_rtc still
 * describes an earlier boot. Start this boot's record here so the next boot
 * does not blame the crash on the boot before. */
static void PANIC_ATTR claim_record(void)
{
    if (s_started) return;
    if (!record_valid()) reset_record();
    ++s_rtc.boot_count;
    count_reset(esp_reset_reason());
    clear_boot_fields();
    s_started = true;
}

static void PANIC_ATTR copy_text(char *out, size_t size, const char *text)
{
    size_t length = 0;
    if (text && (esp_ptr_byte_accessible(text) || esp_ptr_in_drom(text))) {
        while (length + 1 < size && text[length]) {
            out[length] = text[length];
            ++length;
        }
    }
    out[length] = '\0';
}

/* One line of ESP-IDF's panic text. */
static void PANIC_ATTR copy_line(char *out, size_t size, const char *text)
{
    copy_text(out, size, text);
    for (; *out; ++out) {
        if (*out == '\r' || *out == '\n') {
            *out = '\0';
            break;
        }
    }
}

static uint32_t PANIC_ATTR uptime_seconds(void)
{
    int64_t now = esp_timer_get_time();
    return now > 0 ? (uint32_t)(now / 1000000) : 0;
}

/* The backtrace ESP-IDF prints for a core's exception frame, cut at
 * BOOT_TRACE_DEPTH entries. Only a core that entered the panic handler
 * itself left a frame. */
static void PANIC_ATTR trace_core(unsigned core, const XtExcFrame *frame)
{
    s_trace.depth[core] = 0;
    s_trace.pc[core] = 0;
    if (!frame) return;
    s_trace.pc[core] = (uint32_t)frame->pc;
    esp_backtrace_frame_t walk = {.pc = (uint32_t)frame->pc, .sp = (uint32_t)frame->a1,
                                  .next_pc = (uint32_t)frame->a0, .exc_frame = frame};
    s_trace.backtrace[core][s_trace.depth[core]++] = esp_cpu_process_stack_pc(walk.pc);
    /* As in ESP-IDF's output, a fetch from a bad PC still has a caller to
     * walk to. */
    bool sane = esp_stack_ptr_is_sane(walk.sp) &&
                (esp_ptr_executable((void *)(uintptr_t)esp_cpu_process_stack_pc(walk.pc)) ||
                 (uint32_t)frame->exccause == EXCCAUSE_INSTR_PROHIBITED);
    while (sane && walk.next_pc && s_trace.depth[core] < BOOT_TRACE_DEPTH) {
        sane = esp_backtrace_get_next_frame(&walk);
        s_trace.backtrace[core][s_trace.depth[core]++] = esp_cpu_process_stack_pc(walk.pc);
    }
    if (!sane) s_trace.corrupted |= (uint8_t)(1U << core);
}

static void PANIC_ATTR record_trace(const panic_info_t *info)
{
    const XtExcFrame *frame = info->frame;
    s_trace.boot_count = s_rtc.boot_count;
    s_trace.fault_address = 0;
    s_trace.fault_address_valid = 0;
    s_trace.corrupted = 0;
    s_trace.cache_error[0] = '\0';
    /* abort() reaches the handler as a store to address 0, so its reason and
     * fault address describe abort() itself. */
    copy_line(s_trace.reason, sizeof(s_trace.reason), g_panic_abort ? NULL : info->reason);
    if (frame && info->pseudo_excause && (uint32_t)frame->exccause == PANIC_RSN_CACHEERR) {
        esp_cache_err_info_t error = {0};
        esp_cache_err_get_panic_info(&error);
        copy_line(s_trace.cache_error, sizeof(s_trace.cache_error), error.err_str);
        s_trace.fault_address = error.vaddr;
        s_trace.fault_address_valid = error.vaddr != 0;
    } else if (frame && !info->pseudo_excause && !g_panic_abort) {
        /* EXCVADDR: the address a failed load, store or fetch named. */
        s_trace.fault_address = (uint32_t)frame->excvaddr;
        s_trace.fault_address_valid = 1;
    }
    for (unsigned core = 0; core < BOOT_TRACE_CORES; ++core) {
        TaskHandle_t task = xTaskGetCurrentTaskHandleForCore((BaseType_t)core);
        copy_text(s_trace.task[core], sizeof(s_trace.task[core]), task ? pcTaskGetName(task) : NULL);
        trace_core(core, g_exc_frames[core]);
    }
    s_trace.magic = BOOT_TRACE_MAGIC;
    s_trace.checksum = trace_checksum(&s_trace);
}

/* Linked with -Wl,--wrap=esp_panic_handler. Only plain stores to RTC memory:
 * nothing here may allocate, log, lock or wait. */
void __real_esp_panic_handler(panic_info_t *info);
void PANIC_ATTR __wrap_esp_panic_handler(panic_info_t *info)
{
    claim_record();
    if (info && !s_rtc.panic_recorded) {
        s_rtc.panic_recorded = 1;
        /* esp_panic_handler reclassifies aborts, task watchdog timeouts
         * included, only after this hook returns. */
        s_rtc.panic_exception = (uint32_t)(g_panic_abort ? PANIC_EXCEPTION_ABORT : info->exception);
        s_rtc.panic_address = (uint32_t)(uintptr_t)info->addr;
        s_rtc.panic_core = (uint32_t)info->core;
        TaskHandle_t task = xTaskGetCurrentTaskHandle();
        copy_text(s_rtc.panic_task, sizeof(s_rtc.panic_task), task ? pcTaskGetName(task) : NULL);
        /* A failed ESP_ERROR_CHECK has already said more than its abort can. */
        if (!s_rtc.panic_detail[0]) {
            copy_text(s_rtc.panic_detail, sizeof(s_rtc.panic_detail), g_panic_abort_details);
        }
        s_rtc.uptime_seconds = uptime_seconds();
        seal();
        record_trace(info);
    }
    __real_esp_panic_handler(info);
}

typedef struct {
    char *out;
    size_t size, length;
} text_t;

static void put_char(text_t *text, char value)
{
    if (text->length + 1 < text->size) text->out[text->length++] = value;
    text->out[text->length] = '\0';
}

static void put_text(text_t *text, const char *value)
{
    while (value && *value) put_char(text, *value++);
}

static void put_number(text_t *text, uint32_t value, unsigned base, size_t min_digits)
{
    char digits[10];
    size_t count = 0;
    do {
        digits[count++] = "0123456789abcdef"[value % base];
        value /= base;
    } while (value || count < min_digits);
    while (count) put_char(text, digits[--count]);
}

static NOINLINE_ATTR void record_check_failure(esp_err_t rc, intptr_t caller, int line, const char *function,
                                               const char *expression)
{
    portENTER_CRITICAL_SAFE(&s_lock);
    claim_record();
    if (!s_rtc.panic_recorded) {
        text_t text = {s_rtc.panic_detail, sizeof(s_rtc.panic_detail), 0};
        put_text(&text, "ESP_ERROR_CHECK 0x");
        put_number(&text, (uint32_t)rc, 16, 1);
        put_text(&text, " at 0x");
        put_number(&text, (uint32_t)caller, 16, 8);
        put_char(&text, ' ');
        put_text(&text, function);
        put_char(&text, ':');
        put_number(&text, line > 0 ? (uint32_t)line : 0, 10, 1);
        put_char(&text, ' ');
        put_text(&text, expression);
        seal();
    }
    portEXIT_CRITICAL_SAFE(&s_lock);
}

/* Linked with -Wl,--wrap=_esp_error_check_failed. The abort that follows
 * only knows a PC inside the checker, so name the failed call first. ESP-IDF
 * keeps its checker in IRAM because a check can fail with the flash cache
 * disabled; the call-site strings are then unreadable and nothing is
 * recorded. ESP-IDF's console message now names this wrapper as the caller;
 * the record keeps the real call site. */
void __real__esp_error_check_failed(esp_err_t rc, const char *file, int line, const char *function,
                                    const char *expression) __attribute__((noreturn));
void __wrap__esp_error_check_failed(esp_err_t rc, const char *file, int line, const char *function,
                                    const char *expression) __attribute__((noreturn));
void IRAM_ATTR __wrap__esp_error_check_failed(esp_err_t rc, const char *file, int line, const char *function,
                                              const char *expression)
{
    if (spi_flash_cache_enabled()) {
        record_check_failure(rc, esp_cpu_get_call_addr((intptr_t)__builtin_return_address(0)), line, function,
                             expression);
    }
    __real__esp_error_check_failed(rc, file, line, function, expression);
}

static void touch(void *unused)
{
    (void)unused;
    uint32_t now = uptime_seconds();
    portENTER_CRITICAL(&s_lock);
    s_rtc.uptime_seconds = now;
    seal();
    portEXIT_CRITICAL(&s_lock);
}

static void on_restart(void)
{
    touch(NULL);
}

/* Heartbeats are JSON text: never forward a byte ADD could not decode. */
static void printable(char *text, size_t size)
{
    text[size - 1] = '\0';
    for (; *text; ++text) {
        unsigned char value = (unsigned char)*text;
        if (value < 0x20 || value > 0x7e) *text = '?';
    }
}

/* A trace describes the previous boot only if that boot panicked and the
 * trace carries its boot count. */
static bool trace_describes(const boot_rtc_t *previous)
{
    if (!previous->panic_recorded || s_trace.magic != BOOT_TRACE_MAGIC ||
        s_trace.checksum != trace_checksum(&s_trace) || s_trace.boot_count != previous->boot_count) return false;
    for (unsigned core = 0; core < BOOT_TRACE_CORES; ++core) {
        if (s_trace.depth[core] > BOOT_TRACE_DEPTH) return false;
    }
    return true;
}

void boot_report_init(void)
{
    memset(&s_report, 0, sizeof(s_report));
    s_report.reason = esp_reset_reason();
    portENTER_CRITICAL(&s_lock);
    s_report.rtc_valid = s_report.reason != ESP_RST_POWERON && record_valid();
    if (s_report.rtc_valid) {
        s_report.previous = s_rtc;
        s_report.trace_valid = trace_describes(&s_rtc);
        if (s_report.trace_valid) s_report.trace = s_trace;
    } else {
        reset_record();
    }
    s_trace.magic = 0;
    ++s_rtc.boot_count;
    count_reset(s_report.reason);
    clear_boot_fields();
    s_rtc.stage = BOOT_STAGE_STARTED;
    seal();
    s_started = true;
    portEXIT_CRITICAL(&s_lock);
    printable(s_report.previous.panic_task, sizeof(s_report.previous.panic_task));
    printable(s_report.previous.panic_detail, sizeof(s_report.previous.panic_detail));
    printable(s_report.trace.reason, sizeof(s_report.trace.reason));
    printable(s_report.trace.cache_error, sizeof(s_report.trace.cache_error));
    for (unsigned core = 0; core < BOOT_TRACE_CORES; ++core) {
        printable(s_report.trace.task[core], sizeof(s_report.trace.task[core]));
    }
    const esp_partition_t *invalid = esp_ota_get_last_invalid_partition();
    if (invalid) {
        snprintf(s_report.last_invalid_partition, sizeof(s_report.last_invalid_partition), "%s", invalid->label);
        printable(s_report.last_invalid_partition, sizeof(s_report.last_invalid_partition));
    }
    const esp_timer_create_args_t timer_args = {
        .callback = touch, .name = "boot_report", .skip_unhandled_events = true};
    esp_timer_handle_t timer = NULL;
    if (esp_timer_create(&timer_args, &timer) == ESP_OK &&
        esp_timer_start_periodic(timer, BOOT_TOUCH_PERIOD_US) != ESP_OK) {
        (void)esp_timer_delete(timer);
    }
    /* Exact uptime for orderly restarts, OTA and rollback included. */
    (void)esp_register_shutdown_handler(on_restart);
}

void boot_report_stage(boot_stage_t stage)
{
    portENTER_CRITICAL(&s_lock);
    if (stage > s_rtc.stage && stage < BOOT_STAGE_COUNT) {
        s_rtc.stage = stage;
        seal();
    }
    portEXIT_CRITICAL(&s_lock);
}

static const char *reason_name(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXTERNAL";
    case ESP_RST_SW: return "SOFTWARE";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INTERRUPT_WATCHDOG";
    case ESP_RST_TASK_WDT: return "TASK_WATCHDOG";
    case ESP_RST_WDT: return "OTHER_WATCHDOG";
    case ESP_RST_DEEPSLEEP: return "DEEP_SLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SDIO: return "SDIO";
    case ESP_RST_USB: return "USB";
    case ESP_RST_JTAG: return "JTAG";
    case ESP_RST_EFUSE: return "EFUSE";
    case ESP_RST_PWR_GLITCH: return "POWER_GLITCH";
    case ESP_RST_CPU_LOCKUP: return "CPU_LOCKUP";
    default: return "UNKNOWN";
    }
}

static const char *stage_name(uint32_t stage)
{
    static const char *const names[BOOT_STAGE_COUNT] = {
        "NONE", "STARTED", "NVS_READY", "STORAGE_INIT_RETURNED", "NETWORK_READY", "ADD_CONNECTED"};
    return stage < BOOT_STAGE_COUNT ? names[stage] : "UNKNOWN";
}

static const char *exception_name(uint32_t exception)
{
    switch (exception) {
    case PANIC_EXCEPTION_DEBUG: return "DEBUG";
    case PANIC_EXCEPTION_IWDT: return "INTERRUPT_WATCHDOG";
    case PANIC_EXCEPTION_TWDT: return "TASK_WATCHDOG";
    case PANIC_EXCEPTION_ABORT: return "ABORT";
    case PANIC_EXCEPTION_FAULT: return "FAULT";
    default: return "UNKNOWN";
    }
}

static void hex_word(char out[11], uint32_t value)
{
    snprintf(out, 11, "0x%08lx", (unsigned long)value);
}

static bool append_core(cJSON *cores, const boot_rtc_trace_t *trace, unsigned core)
{
    cJSON *row = cJSON_CreateObject();
    if (!row) return false;
    char text[11];
    bool ok = cJSON_AddNumberToObject(row, "core", core) &&
              (!trace->task[core][0] || cJSON_AddStringToObject(row, "task", trace->task[core]));
    if (ok && trace->depth[core]) {
        hex_word(text, trace->pc[core]);
        cJSON *backtrace = cJSON_AddStringToObject(row, "pc", text) ? cJSON_AddArrayToObject(row, "backtrace") : NULL;
        ok = backtrace != NULL;
        for (unsigned index = 0; ok && index < trace->depth[core]; ++index) {
            hex_word(text, trace->backtrace[core][index]);
            cJSON *entry = cJSON_CreateString(text);
            ok = entry && cJSON_AddItemToArray(backtrace, entry);
            if (!ok) cJSON_Delete(entry);
        }
        ok = ok && (!(trace->corrupted & (1U << core)) || cJSON_AddBoolToObject(row, "corrupted", true));
    }
    if (ok && cJSON_AddItemToArray(cores, row)) return true;
    cJSON_Delete(row);
    return false;
}

static bool append_trace(cJSON *panic, const boot_rtc_trace_t *trace)
{
    char address[11];
    hex_word(address, trace->fault_address);
    if ((trace->reason[0] && !cJSON_AddStringToObject(panic, "reason", trace->reason)) ||
        (trace->cache_error[0] && !cJSON_AddStringToObject(panic, "cache_error", trace->cache_error)) ||
        (trace->fault_address_valid && !cJSON_AddStringToObject(panic, "fault_address", address))) return false;
    cJSON *cores = NULL;
    for (unsigned core = 0; core < BOOT_TRACE_CORES; ++core) {
        if (!trace->task[core][0] && !trace->depth[core]) continue;
        if (!cores && !(cores = cJSON_AddArrayToObject(panic, "cores"))) return false;
        if (!append_core(cores, trace, core)) return false;
    }
    return true;
}

static bool append_panic(cJSON *report, const boot_rtc_t *previous)
{
    cJSON *panic = cJSON_AddObjectToObject(report, "panic");
    if (!panic) return false;
    /* A detail without a recorded panic means the abort never reached the
     * panic handler, for example because a watchdog reset it first. */
    if (previous->panic_recorded) {
        char address[11];
        hex_word(address, previous->panic_address);
        if (!cJSON_AddStringToObject(panic, "exception", exception_name(previous->panic_exception)) ||
            !cJSON_AddStringToObject(panic, "address", address) ||
            !cJSON_AddNumberToObject(panic, "core", previous->panic_core)) return false;
        if (previous->panic_task[0] && !cJSON_AddStringToObject(panic, "task", previous->panic_task)) return false;
    }
    if (previous->panic_detail[0] && !cJSON_AddStringToObject(panic, "detail", previous->panic_detail)) return false;
    return !s_report.trace_valid || append_trace(panic, &s_report.trace);
}

bool boot_report_append(cJSON *diagnostics)
{
    cJSON *report = cJSON_AddObjectToObject(diagnostics, "boot_report");
    if (!report || !cJSON_AddStringToObject(report, "reset_reason", reason_name(s_report.reason)) ||
        !cJSON_AddNumberToObject(report, "boot_count", s_rtc.boot_count) ||
        !cJSON_AddNumberToObject(report, "panic_count", s_rtc.panic_count) ||
        !cJSON_AddNumberToObject(report, "task_wdt_count", s_rtc.task_wdt_count) ||
        !cJSON_AddNumberToObject(report, "int_wdt_count", s_rtc.int_wdt_count) ||
        !cJSON_AddNumberToObject(report, "other_wdt_count", s_rtc.other_wdt_count) ||
        !cJSON_AddNumberToObject(report, "brownout_count", s_rtc.brownout_count)) return false;
    if (s_report.last_invalid_partition[0] &&
        !cJSON_AddStringToObject(report, "last_invalid_partition", s_report.last_invalid_partition)) return false;
    if (!s_report.rtc_valid) return true; /* nothing trustworthy survived from the previous boot */
    const boot_rtc_t *previous = &s_report.previous;
    if (!cJSON_AddNumberToObject(report, "previous_uptime_seconds", previous->uptime_seconds) ||
        !cJSON_AddStringToObject(report, "previous_stage", stage_name(previous->stage))) return false;
    return !(previous->panic_recorded || previous->panic_detail[0]) || append_panic(report, previous);
}
