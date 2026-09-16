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

connector = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
start = connector.index("bool add_connector_transfer_queue_evidence(")
end = connector.index("static char *outbox_record_line(", start)
evidence_program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
static size_t calls, fail_at, sends;
static void *allocate(size_t size) { if (++calls == fail_at) return NULL; return malloc(size); }
#define malloc allocate
#define pdMS_TO_TICKS(x) (x)
#define ADD_PRIORITY_ACK_LOCK_TIMEOUT_MS 1
#define ADD_OUTBOX_ACK_TIMEOUT_MS 1
static struct { const char *connector_id; } config = {"connector"};
static const void *unused_pointer;
#define zone_config_get() (&config)
static int mbedtls_sha256(const void *data,size_t length,unsigned char digest[32],int mode)
{ (void)mode;assert(length==3 && !memcmp(data,"abc",3));memset(digest,0x11,32);return 0; }
static int mbedtls_base64_encode(unsigned char *out,size_t capacity,size_t *written,const void *raw,size_t length)
{ assert(capacity>=5 && length==3 && !memcmp(raw,"abc",3));memcpy(out,"YWJj",5);*written=4;return 0; }
static bool send_payload_and_wait_for_ack(const char *type,const char *payload,int a,int b,const void *c,const void *d)
{
    (void)a;(void)b;unused_pointer=c;unused_pointer=d;
    assert(!strcmp(type,"queue_evidence"));
    const char *fields[]={"connector_id","queue_generation","record_id","payload_digest","raw_b64","provenance","terminal_serial","reason","encoding"};
    for(unsigned i=0;i<sizeof(fields)/sizeof(*fields);++i) assert(strstr(payload,fields[i]));
    ++sends;return true;
}
''' + connector[start:end] + r'''
int main(void) {
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    assert(add_connector_transfer_queue_evidence("blocked","epoch","record","abc",3,"OLD","MALFORMED"));
    size_t total=calls;
    for(size_t i=1;i<=total;++i) {
        calls=0;fail_at=i;sends=0;
        assert(!add_connector_transfer_queue_evidence("blocked","epoch","record","abc",3,"OLD","MALFORMED"));
        assert(sends==0);
    }
    puts("Evidence allocation regressions passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    c_file = temporary / "evidence.c"
    c_file.write_text(evidence_program)
    executable = temporary / "evidence"
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(cjson), str(c_file), str(cjson / "cJSON.c"),
        "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)
