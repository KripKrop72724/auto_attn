"""Fault-test actual firmware serializers against ESP-IDF's actual cJSON.

Run inside the pinned ESP-IDF image; no test implementation of cJSON is used.
"""
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
end = source.index("} attendance_event_t;") + len("} attendance_event_t;")
event_type = source[source.rfind("typedef struct {", 0, end):end]
helpers = source[source.index("static size_t valid_utf8_sequence_length("):
                 source.index("static bool user_table_state_hash(")]
serializer = source[source.index("static const char *oracle_capture_type("):
                    source.index("typedef enum {", source.index("static char *event_to_json("))]
row = source[source.index("static cJSON *add_attendance_json_row("):
             source.index("static char *add_serialize_attendance_events(")]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
static size_t calls, fail_at;
static void *allocate(size_t size) {
    if (++calls == fail_at) return NULL;
    return malloc(size);
}
#define malloc allocate
#define ZONE_LITE_ZONE_ID "test-zone"
#define ZONE_LITE_ZONE_DEVICE_ID "test-device"
static char g_device_serial[80] = "test-terminal";
static void iso_system_now(char *out) { strcpy(out, "2026-09-16T10:00:00"); }
''' + event_type + helpers + serializer + row + r'''
int main(void) {
    cJSON_Hooks hooks = {allocate, free};
    cJSON_InitHooks(&hooks);
    attendance_event_t event = {
        .uid="1", .attendance_record_uid="2", .user_id="test-user",
        .employee_name="Test", .cnic="test-id", .timestamp="2026-09-16T10:00:00",
        .event_uid="test-event", .terminal_identity_fingerprint="test-fingerprint",
        .status=1, .punch=1, .raw_punch=true,
    };
    calls=0; fail_at=0;
    char *json = event_to_json(&event, "LIVE");
    assert(json); free(json);
    size_t total = calls;
    for (size_t i=1; i<=total; i++) {
        calls=0; fail_at=i;
        json = event_to_json(&event, "LIVE");
        assert(json == NULL);
    }
    calls=0; fail_at=0;
    cJSON *row = add_attendance_json_row(&event, "LIVE");
    assert(row); cJSON_Delete(row);
    total = calls;
    for (size_t i=1; i<=total; i++) {
        calls=0; fail_at=i;
        row = add_attendance_json_row(&event, "LIVE");
        assert(row == NULL);
    }
    puts("Attendance serializer allocation regressions passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    c_file = temporary / "serialization.c"
    c_file.write_text(program)
    executable = temporary / "serialization"
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(cjson), str(c_file), str(cjson / "cJSON.c"),
        "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)
