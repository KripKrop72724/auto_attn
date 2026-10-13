/* Host harness for boot_report.c, built with it as one translation unit so a
 * simulated reset can clear what RAM loses and keep what RTC memory keeps. */
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "boot_report.c"

/* ---- the platform ---- */
int host_critical_depth, host_critical_entries;
bool g_panic_abort;
char *g_panic_abort_details;
static esp_reset_reason_t reset_reason;
static int64_t now_us;
static TaskHandle_t current_task;
static bool cache_enabled = true;
static char unreadable[1]; /* stands for a pointer the panic must not follow */
static const esp_partition_t *invalid_partition;
static esp_timer_cb_t timer_callback;
static uint64_t timer_period_us;
static shutdown_handler_t shutdown_handler;
static jmp_buf crashed;
static unsigned real_panics;
void *g_exc_frames[2];
static char *core_tasks[2];
static const char *cache_error_text;
static uint32_t cache_error_vaddr;
/* The stack a backtrace walks: the base save area below each frame's SP
 * names its caller's PC and SP. */
static struct {
    uint32_t sp, next_pc, next_sp;
} stack[16];
static unsigned stack_frames;

esp_reset_reason_t esp_reset_reason(void) { return reset_reason; }
int64_t esp_timer_get_time(void) { return now_us; }
TaskHandle_t xTaskGetCurrentTaskHandle(void) { return current_task; }
TaskHandle_t xTaskGetCurrentTaskHandleForCore(BaseType_t core) { return core_tasks[core]; }
char *pcTaskGetName(TaskHandle_t task) { return task; }
bool spi_flash_cache_enabled(void) { return cache_enabled; }
bool esp_ptr_byte_accessible(const void *p) { return p && p != unreadable; }
const esp_partition_t *esp_ota_get_last_invalid_partition(void) { return invalid_partition; }

void esp_cache_err_get_panic_info(esp_cache_err_info_t *err_info)
{
    err_info->err_str = cache_error_text;
    err_info->vaddr = cache_error_vaddr;
}

bool esp_backtrace_get_next_frame(esp_backtrace_frame_t *frame)
{
    uint32_t sp = frame->sp;
    frame->pc = frame->next_pc;
    frame->next_pc = 0;
    frame->sp = 0;
    for (unsigned index = 0; index < stack_frames; ++index) {
        if (stack[index].sp == sp) {
            frame->next_pc = stack[index].next_pc;
            frame->sp = stack[index].next_sp;
        }
    }
    return esp_stack_ptr_is_sane(frame->sp) &&
           esp_ptr_executable((void *)(uintptr_t)esp_cpu_process_stack_pc(frame->pc));
}

static void push_frame(uint32_t sp, uint32_t next_pc, uint32_t next_sp)
{
    assert(stack_frames < sizeof(stack) / sizeof(stack[0]));
    stack[stack_frames].sp = sp;
    stack[stack_frames].next_pc = next_pc;
    stack[stack_frames++].next_sp = next_sp;
}

esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out)
{
    assert(args->skip_unhandled_events && !strcmp(args->name, "boot_report"));
    timer_callback = args->callback;
    *out = (esp_timer_handle_t)&timer_callback;
    return ESP_OK;
}
esp_err_t esp_timer_start_periodic(esp_timer_handle_t timer, uint64_t period_us)
{
    (void)timer;
    timer_period_us = period_us;
    return ESP_OK;
}
esp_err_t esp_timer_delete(esp_timer_handle_t timer)
{
    (void)timer;
    return ESP_OK;
}
esp_err_t esp_register_shutdown_handler(shutdown_handler_t handler)
{
    shutdown_handler = handler;
    return ESP_OK;
}

/* ESP-IDF's handlers never return; the simulated reset follows instead. */
void __real_esp_panic_handler(panic_info_t *info)
{
    (void)info;
    ++real_panics;
    longjmp(crashed, 1);
}

static char abort_message[] = "abort() was called at PC 0x4037d0a1 on core 0";

void __real__esp_error_check_failed(esp_err_t rc, const char *file, int line, const char *function,
                                    const char *expression)
{
    (void)rc, (void)file, (void)line, (void)function, (void)expression;
    /* ESP-IDF prints, then abort() panics from inside the checker. */
    g_panic_abort = true;
    g_panic_abort_details = abort_message;
    __wrap_esp_panic_handler(&(panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_DEBUG,
                                             .addr = (const void *)0x4037d0a4});
    abort();
}

/* ---- simulated boots ---- */
/* A reset clears RAM and restarts the clock; RTC memory keeps s_rtc. */
static void start_boot(esp_reset_reason_t reason)
{
    s_started = false;
    memset(&s_report, 0, sizeof(s_report));
    reset_reason = reason;
    now_us = 0;
    g_panic_abort = false;
    g_panic_abort_details = NULL;
    timer_callback = NULL;
    shutdown_handler = NULL;
    g_exc_frames[0] = g_exc_frames[1] = NULL;
    core_tasks[0] = core_tasks[1] = NULL;
    cache_error_text = NULL;
    cache_error_vaddr = 0;
    stack_frames = 0;
}

static void boot(esp_reset_reason_t reason)
{
    start_boot(reason);
    boot_report_init();
    assert(timer_callback && timer_period_us == 10000000U && shutdown_handler && !host_critical_depth);
}

static void at_seconds(double seconds) { now_us = (int64_t)(seconds * 1e6); }

/* Kept outside the frames that call setjmp, so longjmp cannot clobber them. */
static panic_info_t pending_panic;
static struct {
    esp_err_t rc;
    int line;
    const char *function, *expression;
} pending_check;
static int entries_before;
static unsigned panics_before;

static void crash(panic_info_t info)
{
    pending_panic = info;
    entries_before = host_critical_entries;
    panics_before = real_panics;
    if (!setjmp(crashed)) {
        __wrap_esp_panic_handler(&pending_panic);
        abort();
    }
    /* The panic path never takes a lock: the other core may hold it. */
    assert(real_panics == panics_before + 1 && host_critical_entries == entries_before);
}

static void failed_check(esp_err_t rc, int line, const char *function, const char *expression)
{
    pending_check.rc = rc;
    pending_check.line = line;
    pending_check.function = function;
    pending_check.expression = expression;
    panics_before = real_panics;
    if (!setjmp(crashed)) {
        __wrap__esp_error_check_failed(pending_check.rc, "main/zone_lite.c", pending_check.line,
                                       pending_check.function, pending_check.expression);
    }
    assert(real_panics == panics_before + 1 && !host_critical_depth);
}

static char *report(void)
{
    cJSON *root = cJSON_CreateObject();
    assert(root && boot_report_append(root));
    char *text = cJSON_PrintUnformatted(root->child);
    cJSON_Delete(root);
    printf("%s\n", text);
    fflush(stdout);
    return text;
}

static void expect(const char *expected)
{
    char *text = report();
    if (strcmp(text, expected)) {
        fprintf(stderr, "expected %s\n", expected);
        abort();
    }
    free(text);
}

#define COUNTS(boots, panics, task_wdts, int_wdts, other_wdts, brownouts)                             \
    "\"boot_count\":" #boots ",\"panic_count\":" #panics ",\"task_wdt_count\":" #task_wdts         \
    ",\"int_wdt_count\":" #int_wdts ",\"other_wdt_count\":" #other_wdts ",\"brownout_count\":" #brownouts

static unsigned allocations, fail_at;
static void *failing_malloc(size_t size) { return ++allocations == fail_at ? NULL : malloc(size); }

/* Every failed allocation fails the whole object, so the heartbeat never
 * carries a partial report. */
static void every_allocation_failure_fails_the_report(unsigned at_least)
{
    cJSON_InitHooks(&(cJSON_Hooks){failing_malloc, free});
    for (unsigned at = 1;; ++at) {
        fail_at = 0;
        cJSON *root = cJSON_CreateObject();
        assert(root);
        allocations = 0;
        fail_at = at;
        bool appended = boot_report_append(root);
        cJSON_Delete(root);
        if (allocations < at) {
            assert(appended && at > at_least);
            break;
        }
        assert(!appended);
    }
    cJSON_InitHooks(&(cJSON_Hooks){malloc, free});
}

int main(void)
{
    /* Power-on: RTC memory holds noise, and nothing from before is reported. */
    memset(&s_rtc, 0xa5, sizeof(s_rtc));
    boot(ESP_RST_POWERON);
    expect("{\"reset_reason\":\"POWERON\"," COUNTS(1, 0, 0, 0, 0, 0) "}");

    /* Stages only advance. The uptime refreshes every 10 s and is exact at
     * an orderly restart. */
    boot_report_stage(BOOT_STAGE_NVS_READY);
    boot_report_stage(BOOT_STAGE_STORAGE_INIT_RETURNED);
    boot_report_stage(BOOT_STAGE_NVS_READY);
    boot_report_stage(BOOT_STAGE_COUNT);
    boot_report_stage(BOOT_STAGE_ADD_CONNECTED);
    at_seconds(125.9);
    timer_callback(NULL);
    assert(s_rtc.uptime_seconds == 125);
    at_seconds(130.2);
    shutdown_handler();
    static const esp_partition_t rolled_back = {.label = "ota_1"};
    invalid_partition = &rolled_back;
    boot(ESP_RST_SW);
    expect("{\"reset_reason\":\"SOFTWARE\"," COUNTS(2, 0, 0, 0, 0, 0) ",\"last_invalid_partition\":\"ota_1\","
           "\"previous_uptime_seconds\":130,\"previous_stage\":\"ADD_CONNECTED\"}");
    invalid_partition = NULL;

    /* A task watchdog: ESP-IDF raises it as an abort without a message from
     * the stuck task's frame. The panic records its own uptime, after the
     * last refresh. */
    boot_report_stage(BOOT_STAGE_NETWORK_READY);
    at_seconds(3590);
    timer_callback(NULL);
    at_seconds(3600.4);
    current_task = "add_ws";
    g_panic_abort = true;
    crash((panic_info_t){.core = 1, .exception = PANIC_EXCEPTION_FAULT, .addr = (const void *)0x42001234});
    boot(ESP_RST_TASK_WDT);
    expect("{\"reset_reason\":\"TASK_WATCHDOG\"," COUNTS(3, 0, 1, 0, 0, 0) ",\"previous_uptime_seconds\":3600,"
           "\"previous_stage\":\"NETWORK_READY\",\"panic\":{\"exception\":\"ABORT\",\"address\":\"0x42001234\","
           "\"core\":1,\"task\":\"add_ws\"}}");

    /* A failed ESP_ERROR_CHECK names its call site; the abort it raises
     * only knows a PC inside the checker. */
    boot_report_stage(BOOT_STAGE_NVS_READY);
    at_seconds(2);
    current_task = "main";
    failed_check(0x105, 10608, "app_main", "zone_config_init()");
    boot(ESP_RST_PANIC);
    char *text = report();
    const char *prefix = "{\"reset_reason\":\"PANIC\"," COUNTS(4, 1, 1, 0, 0, 0) ",\"previous_uptime_seconds\":2,"
                         "\"previous_stage\":\"NVS_READY\",\"panic\":{\"exception\":\"ABORT\",\"address\":\"0x4037d0a4\","
                         "\"core\":0,\"task\":\"main\",\"detail\":\"ESP_ERROR_CHECK 0x105 at 0x";
    const char *suffix = " app_main:10608 zone_config_init()\"}}";
    /* The call site is an 8-digit address, as in the panic's own field. */
    assert(!strncmp(text, prefix, strlen(prefix)));
    assert(strlen(text) == strlen(prefix) + 8 + strlen(suffix));
    assert(!strcmp(text + strlen(prefix) + 8, suffix));
    free(text);

    /* With the flash cache disabled the call-site strings are unreadable, so
     * only the abort's own message survives. */
    cache_enabled = false;
    failed_check(0x101, 77, "timer_isr", "esp_timer_start_once(timer, 1)");
    cache_enabled = true;
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(5, 2, 1, 0, 0, 0) ",\"previous_uptime_seconds\":0,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"ABORT\",\"address\":\"0x4037d0a4\",\"core\":0,"
           "\"task\":\"main\",\"detail\":\"abort() was called at PC 0x4037d0a1 on core 0\"}}");

    /* Startup code crashes before boot_report_init: the hook starts this
     * boot's record itself, so the crash is not blamed on the boot before.
     * A long message is cut, never overrun. */
    start_boot(ESP_RST_PANIC);
    at_seconds(0.3);
    current_task = NULL; /* the scheduler has not started */
    static char assertion[] = "assert failed: spi_flash_init_chip_state spi_flash_os_func_app.c:242 "
                              "(chip->os_func != NULL) while bringing up the main flash chip";
    assert(strlen(assertion) > 95);
    g_panic_abort = true;
    g_panic_abort_details = assertion;
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_DEBUG, .addr = (const void *)0x40375a10});
    boot(ESP_RST_PANIC);
    char expected[512];
    snprintf(expected, sizeof(expected),
             "{\"reset_reason\":\"PANIC\"," COUNTS(7, 4, 1, 0, 0, 0) ",\"previous_uptime_seconds\":0,"
             "\"previous_stage\":\"NONE\",\"panic\":{\"exception\":\"ABORT\",\"address\":\"0x40375a10\",\"core\":0,"
             "\"detail\":\"%.95s\"}}",
             assertion);
    expect(expected);

    /* A panic never follows a pointer it cannot read. */
    at_seconds(42);
    current_task = unreadable;
    g_panic_abort_details = unreadable;
    crash((panic_info_t){.core = 1, .exception = PANIC_EXCEPTION_FAULT, .addr = (const void *)0x420abcde});
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(8, 5, 1, 0, 0, 0) ",\"previous_uptime_seconds\":42,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x420abcde\",\"core\":1}}");

    /* Bytes a heartbeat could not carry as JSON text are replaced. */
    current_task = "bad\001name\377";
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_IWDT, .addr = (const void *)0x40380000});
    boot(ESP_RST_INT_WDT);
    expect("{\"reset_reason\":\"INTERRUPT_WATCHDOG\"," COUNTS(9, 5, 1, 1, 0, 0) ",\"previous_uptime_seconds\":0,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"INTERRUPT_WATCHDOG\",\"address\":\"0x40380000\","
           "\"core\":0,\"task\":\"bad?name?\"}}");

    /* 3FL, 12 October 2026: a cache error the chip could not pin on a core.
     * ESP-IDF fills no address for it and both cores enter the panic
     * handler, so each core's task, PC and backtrace are kept. A backtrace
     * stops at the first frame that is not sane. */
    boot_report_stage(BOOT_STAGE_ADD_CONNECTED);
    at_seconds(9290.5);
    current_task = core_tasks[0] = "add_heartbeat";
    core_tasks[1] = "zone_gateway";
    static XtExcFrame heartbeat = {.pc = 0x42012345, .a0 = 0x82034567, .a1 = 0x3fc9a000,
                                   .exccause = PANIC_RSN_CACHEERR};
    static XtExcFrame gateway = {.pc = 0x4037abcd, .a0 = 0x80389aa0, .a1 = 0x3fcb2000,
                                 .exccause = PANIC_RSN_CACHEERR};
    push_frame(0x3fc9a000, 0x8203aaaa, 0x3fc9a040);
    push_frame(0x3fc9a040, 0, 0x3fc9a080);
    push_frame(0x3fcb2000, 0x82050000, 0x3fcb2034);
    g_exc_frames[0] = &heartbeat;
    g_exc_frames[1] = &gateway;
    cache_error_text = "MMU entry fault error";
    crash((panic_info_t){.core = -1, .exception = PANIC_EXCEPTION_FAULT, .reason = "Cache error",
                         .frame = &heartbeat, .pseudo_excause = true});
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(10, 6, 1, 1, 0, 0) ",\"previous_uptime_seconds\":9290,"
           "\"previous_stage\":\"ADD_CONNECTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x00000000\","
           "\"core\":4294967295,\"task\":\"add_heartbeat\",\"reason\":\"Cache error\","
           "\"cache_error\":\"MMU entry fault error\",\"cores\":[{\"core\":0,\"task\":\"add_heartbeat\","
           "\"pc\":\"0x42012345\",\"backtrace\":[\"0x42012342\",\"0x42034564\",\"0x4203aaa7\"]},"
           "{\"core\":1,\"task\":\"zone_gateway\",\"pc\":\"0x4037abcd\",\"backtrace\":[\"0x4037abca\","
           "\"0x40389a9d\"],\"corrupted\":true}]}}");
    every_allocation_failure_fails_the_report(40);

    /* A CPU exception names the address it failed on. A core that was only
     * stalled left no frame, just its task. */
    at_seconds(61);
    current_task = core_tasks[1] = "zone_gateway";
    core_tasks[0] = "IDLE0";
    static XtExcFrame load = {.pc = 0x42056789, .a1 = 0x3fcb4000, .exccause = EXCCAUSE_LOAD_PROHIBITED,
                              .excvaddr = 0x3c9f0000};
    g_exc_frames[1] = &load;
    crash((panic_info_t){.core = 1, .exception = PANIC_EXCEPTION_FAULT, .reason = "LoadProhibited",
                         .addr = (const void *)0x42056789, .frame = &load});
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(11, 7, 1, 1, 0, 0) ",\"previous_uptime_seconds\":61,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x42056789\","
           "\"core\":1,\"task\":\"zone_gateway\",\"reason\":\"LoadProhibited\",\"fault_address\":\"0x3c9f0000\","
           "\"cores\":[{\"core\":0,\"task\":\"IDLE0\"},{\"core\":1,\"task\":\"zone_gateway\","
           "\"pc\":\"0x42056789\",\"backtrace\":[\"0x42056786\"]}]}}");

    /* A cache error message keeps its first line, a cache error with an
     * address reports it, and a backtrace keeps eight entries. */
    at_seconds(7);
    current_task = core_tasks[0] = "ords_uploader";
    static XtExcFrame deep = {.pc = 0x42000010, .a0 = 0x82000100, .a1 = 0x3fc90000, .exccause = PANIC_RSN_CACHEERR};
    for (uint32_t index = 0; index < 10; ++index) {
        push_frame(0x3fc90000 + index * 0x40, 0x82000110 + index * 0x10, 0x3fc90040 + index * 0x40);
    }
    g_exc_frames[0] = &deep;
    cache_error_text = "Write back error occurred while dcache tries to write back to flash\r\n"
                       "The following backtrace may not indicate the code that caused Cache invalid access";
    cache_error_vaddr = 0x3c123450;
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_FAULT, .reason = "Cache error",
                         .frame = &deep, .pseudo_excause = true});
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(12, 8, 1, 1, 0, 0) ",\"previous_uptime_seconds\":7,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x00000000\","
           "\"core\":0,\"task\":\"ords_uploader\",\"reason\":\"Cache error\",\"cache_error\":\"Write back error "
           "occurred while dcache tries to write back to flash\",\"fault_address\":\"0x3c123450\","
           "\"cores\":[{\"core\":0,\"task\":\"ords_uploader\",\"pc\":\"0x42000010\",\"backtrace\":["
           "\"0x4200000d\",\"0x420000fd\",\"0x4200010d\",\"0x4200011d\",\"0x4200012d\",\"0x4200013d\","
           "\"0x4200014d\",\"0x4200015d\"]}]}}");

    /* abort() reaches the handler as a store to address 0: no reason or
     * fault address of its own, but its backtrace names the caller. */
    at_seconds(5);
    current_task = core_tasks[0] = "add_inbound";
    static XtExcFrame aborted = {.pc = 0x4037d0a4, .a0 = 0x82045678, .a1 = 0x3fca0000,
                                 .exccause = 29, .excvaddr = 0};
    push_frame(0x3fca0000, 0, 0x3fca0040);
    g_exc_frames[0] = &aborted;
    g_panic_abort = true;
    g_panic_abort_details = abort_message;
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_FAULT, .reason = "StoreProhibited",
                         .addr = (const void *)0x4037d0a4, .frame = &aborted});
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(13, 9, 1, 1, 0, 0) ",\"previous_uptime_seconds\":5,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"ABORT\",\"address\":\"0x4037d0a4\","
           "\"core\":0,\"task\":\"add_inbound\",\"detail\":\"abort() was called at PC 0x4037d0a1 on core 0\","
           "\"cores\":[{\"core\":0,\"task\":\"add_inbound\",\"pc\":\"0x4037d0a4\",\"backtrace\":["
           "\"0x4037d0a1\",\"0x42045675\"]}]}}");

    /* A trace is reported only for the boot it describes: a torn one, one
     * from another boot, or one an orderly restart left behind is not. */
    at_seconds(3);
    current_task = core_tasks[0] = "main";
    g_exc_frames[0] = &load;
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_FAULT, .reason = "LoadProhibited",
                         .addr = (const void *)0x42056789, .frame = &load});
    s_trace.cache_error[5] ^= 0x20;
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(14, 10, 1, 1, 0, 0) ",\"previous_uptime_seconds\":3,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x42056789\","
           "\"core\":0,\"task\":\"main\"}}");
    at_seconds(4);
    current_task = core_tasks[0] = "main";
    g_exc_frames[0] = &load;
    crash((panic_info_t){.core = 0, .exception = PANIC_EXCEPTION_FAULT, .reason = "LoadProhibited",
                         .addr = (const void *)0x42056789, .frame = &load});
    ++s_trace.boot_count;
    s_trace.checksum = trace_checksum(&s_trace);
    boot(ESP_RST_PANIC);
    expect("{\"reset_reason\":\"PANIC\"," COUNTS(15, 11, 1, 1, 0, 0) ",\"previous_uptime_seconds\":4,"
           "\"previous_stage\":\"STARTED\",\"panic\":{\"exception\":\"FAULT\",\"address\":\"0x42056789\","
           "\"core\":0,\"task\":\"main\"}}");
    assert(s_trace.magic != BOOT_TRACE_MAGIC);
    s_trace.magic = BOOT_TRACE_MAGIC;
    s_trace.boot_count = s_rtc.boot_count;
    s_trace.checksum = trace_checksum(&s_trace);
    at_seconds(9);
    shutdown_handler();
    boot(ESP_RST_SW);
    assert(!s_report.trace_valid);
    expect("{\"reset_reason\":\"SOFTWARE\"," COUNTS(16, 11, 1, 1, 0, 0) ",\"previous_uptime_seconds\":9,"
           "\"previous_stage\":\"STARTED\"}");

    /* An ESP_ERROR_CHECK's report survives every allocation failure too. */
    current_task = "main";
    failed_check(0x103, 12, "wifi_init_sta", "esp_wifi_init(&config)");
    boot(ESP_RST_PANIC);
    every_allocation_failure_fails_the_report(20);

    /* A torn record, for example from a brownout below RTC retention, and a
     * record written with another layout both start the counters over. */
    s_rtc.panic_detail[3] ^= 0x40;
    boot(ESP_RST_BROWNOUT);
    expect("{\"reset_reason\":\"BROWNOUT\"," COUNTS(1, 0, 0, 0, 0, 1) "}");
    s_rtc.magic = UINT32_C(0x5A4C4252);
    seal();
    boot(ESP_RST_WDT);
    expect("{\"reset_reason\":\"OTHER_WATCHDOG\"," COUNTS(1, 0, 0, 0, 1, 0) "}");

    /* A power cut ends every count, whatever RTC memory still holds. */
    boot(ESP_RST_SW);
    assert(s_report.rtc_valid && s_rtc.boot_count == 2);
    boot(ESP_RST_POWERON);
    expect("{\"reset_reason\":\"POWERON\"," COUNTS(1, 0, 0, 0, 0, 0) "}");
    assert(!host_critical_depth);
    puts("boot report host tests passed");
    return 0;
}
