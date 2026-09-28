"""Exercise the production live enqueue paths with ACK and lock failures."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for position in range(brace, len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"Unclosed function: {signature}")


def test_live_outbox_recovery_distinguishes_contention_from_storage_failure(tmp_path: Path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    helper = _function(source, "static bool recover_live_event_after_storage_error(")
    enqueue = _function(source, "static enqueue_result_t enqueue_event(")
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
#define LED_STATUS_BLOCKED_IDENTITY 1
#define LED_STATUS_LOCAL_FAILURE 2
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
typedef struct { char event_uid[40]; char cnic[20]; } attendance_event_t;
typedef struct { bool add_enabled; } zone_config_t;
typedef enum { ENQUEUE_DUPLICATE, ENQUEUE_PENDING, ENQUEUE_BLOCKED,
    ENQUEUE_ACKNOWLEDGED, ENQUEUE_STORAGE_ERROR, ENQUEUE_RESOURCE_ERROR } enqueue_result_t;
static int storage_mutex, *g_storage_lock = &storage_mutex;
static bool lock_available, ack_available, send_available;
static enqueue_result_t local_result;
static int local_faults, blocked_faults, ack_attempts, sends;
static char last_log_code[80];
static zone_config_t config = {.add_enabled = true};
static const zone_config_t *zone_config_get(void) { return &config; }
static int xSemaphoreTake(int *lock, unsigned timeout)
{ (void)lock; (void)timeout; return lock_available ? pdTRUE : 0; }
static void xSemaphoreGive(int *lock) { (void)lock; }
static void led_status_fault(int fault)
{ if (fault == LED_STATUS_LOCAL_FAILURE) ++local_faults; else ++blocked_faults; }
static bool add_send_attendance_event_acknowledged(const attendance_event_t *event, const char *capture)
{ (void)event; (void)capture; ++ack_attempts; return ack_available; }
static bool seen_add(const char *uid) { (void)uid; return true; }
static bool add_connector_log(const char *level, const char *area, const char *code, const char *message)
{ (void)level; (void)area; (void)message; snprintf(last_log_code, sizeof(last_log_code), "%s", code); return true; }
static enqueue_result_t enqueue_event_to_files(const attendance_event_t *event, const char *capture)
{ (void)event; (void)capture; return local_result; }
static bool add_send_attendance_event(const attendance_event_t *event, const char *capture)
{ (void)event; (void)capture; ++sends; return send_available; }
/* INSERT_PRODUCTION_FUNCTIONS */
static void reset(void)
{
    g_storage_lock = &storage_mutex; lock_available = false;
    ack_available = false; send_available = false;
    local_result = ENQUEUE_PENDING; local_faults = blocked_faults = ack_attempts = sends = 0;
    last_log_code[0] = 0;
}
int main(void)
{
    attendance_event_t event = {.event_uid = "test-event", .cnic = "test-identity"};
    reset(); ack_available = true;
    assert(enqueue_event(&event, "LIVE") == ENQUEUE_ACKNOWLEDGED);
    assert(local_faults == 0 && ack_attempts == 1);
    assert(!strcmp(last_log_code, "LIVE_OUTBOX_LOCK_RECOVERED"));
    reset();
    assert(enqueue_event(&event, "LIVE") == ENQUEUE_STORAGE_ERROR);
    assert(local_faults == 1 && ack_attempts == 1);
    reset(); g_storage_lock = NULL; ack_available = true;
    assert(enqueue_event(&event, "LIVE") == ENQUEUE_ACKNOWLEDGED);
    assert(local_faults == 1);
    reset(); lock_available = true; local_result = ENQUEUE_STORAGE_ERROR; ack_available = true;
    assert(enqueue_event(&event, "LIVE") == ENQUEUE_ACKNOWLEDGED);
    assert(local_faults == 1 && !strcmp(last_log_code, "LIVE_LOCAL_STORAGE_RECOVERED"));
    reset(); lock_available = true; local_result = ENQUEUE_PENDING;
    assert(enqueue_event(&event, "LIVE") == ENQUEUE_PENDING);
    assert(local_faults == 0 && sends == 1);
    reset(); ack_available = true;
    assert(enqueue_event(&event, "FULL_HISTORY") == ENQUEUE_STORAGE_ERROR);
    assert(local_faults == 1 && ack_attempts == 0);
    puts("live outbox recovery regressions passed");
}
'''
    unit = tmp_path / "live_outbox.c"
    unit.write_text(harness.replace("/* INSERT_PRODUCTION_FUNCTIONS */", helper + "\n" + enqueue))
    compiler = shutil.which("cc")
    assert compiler
    executable = tmp_path / "live_outbox"
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", str(unit), "-o", str(executable)],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
