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
oracle_serializers = source[source.index("static int oracle_local_identity_eligible("):
                            source.index("static void oracle_log_rejection_details(")]
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
static size_t strlcpy(char *out, const char *in, size_t size) {
    size_t length = strlen(in); if (size) snprintf(out, size, "%s", in); return length;
}
static void iso_system_now(char *out) { strcpy(out, "2026-09-16T10:00:00"); }
''' + event_type + helpers + serializer + row + oracle_serializers + r'''
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
    const char *live = "{\"device_serial\":\"test-terminal\",\"capturetype\":\"LIVE\"}";
    const char *historical = "{\"device_serial\":\"test-terminal\",\"capturetype\":\"DUMP_STARTUP\"}";
    const char *wrong = "{\"device_serial\":\"old-terminal\",\"capturetype\":\"LIVE\"}";
    calls=0; fail_at=0;
    assert(oracle_local_identity_eligible(live) == 1);
    total=calls;
    for (size_t i=1; i<=total; i++) {
        calls=0; fail_at=i; assert(oracle_local_identity_eligible(live) == -1);
    }
    calls=0; fail_at=0;
    assert(oracle_local_identity_eligible(historical) == 0);
    assert(oracle_local_identity_eligible(wrong) == 0);
    assert(oracle_local_identity_eligible("{}") == 0);
    char *(*functions[])(const char *) = {oracle_normalize_event_json, oracle_mark_permanent_rejection};
    for (size_t f=0; f<2; f++) {
        calls=0; fail_at=0; json=functions[f](live); assert(json); free(json); total=calls;
        for (size_t i=1; i<=total; i++) {
            calls=0; fail_at=i; json=functions[f](live); assert(json == NULL);
        }
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

# Compile the production OTA evidence builders, including serialization, against
# actual cJSON; transport records only complete payloads.
ota = (ROOT / "firmware/zone_lite/main/ota_manager.c").read_text()
start = ota.index("static bool add_running_image_evidence(")
end = ota.index("static bool fetch_assignment(", start)
ota_program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
static size_t calls, fail_at, sends;
static bool fail_hash, missing_partition, missing_description;
static void *allocate(size_t n) { if(++calls==fail_at)return NULL;return malloc(n); }
#define ESP_OK 0
#define ZONE_LITE_OTA_PARTITION_LAYOUT "zone-lite-ota-v1"
typedef struct { const char *version; } esp_app_desc_t;
typedef struct { const char *label; } esp_partition_t;
static const esp_app_desc_t description={"2.6.0"};
static const esp_partition_t partition={"ota_1"};
static struct { char deployment_id[48];size_t bytes_written,image_size;char state[40],target_version[80]; } s_journal={.deployment_id="test",.bytes_written=1024,.image_size=1024,.state="SUCCEEDED",.target_version="2.6.0"};
static bool s_busy;
static const char s_last_error[]="test_error";
static const esp_app_desc_t *esp_app_get_description(void) { return missing_description?NULL:&description; }
static const esp_partition_t *esp_ota_get_running_partition(void) { return missing_partition?NULL:&partition; }
static bool esp_secure_boot_enabled(void) { return true; }
static int esp_partition_get_sha256(const esp_partition_t *p,unsigned char digest[32])
{ assert(p==&partition);memset(digest,0x11,32);return fail_hash?-1:0; }
static void hex_bytes(const unsigned char *input,size_t length,char *out)
{ (void)input;memset(out,'1',length*2);out[length*2]=0; }
static bool post_json(const char *path,cJSON *root)
{
    assert(path[0]);char *body=cJSON_PrintUnformatted(root);if(!body)return false;
    assert(strstr(body,"running_version") && strstr(body,"running_partition") && strstr(body,"image_sha256"));
    if(strstr(path,"progress"))assert(strstr(body,"bytes_written") && strstr(body,"error_code"));
    ++sends;free(body);return true;
}
''' + ota[start:end] + ota[ota.index("void ota_manager_append_telemetry("):] + r'''
int main(void)
{
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    for(unsigned mode=0;mode<2;++mode){
        calls=fail_at=sends=0;
        assert(mode?report_capability():report_state("SUCCEEDED","test"));
        size_t total=calls;assert(sends==1);
        for(size_t i=1;i<=total;++i){
            calls=sends=0;fail_at=i;
            assert(!(mode?report_capability():report_state("SUCCEEDED","test")));
            assert(!sends);
        }
        fail_at=0;sends=0;
        fail_hash=true;assert(!(mode?report_capability():report_state("SUCCEEDED","test")));fail_hash=false;
        missing_partition=true;assert(!(mode?report_capability():report_state("SUCCEEDED","test")));missing_partition=false;
        missing_description=true;assert(!(mode?report_capability():report_state("SUCCEEDED","test")));missing_description=false;
        assert(!sends);
    }
    fail_at=0;cJSON *heartbeat=cJSON_CreateObject();calls=0;
    ota_manager_append_telemetry(heartbeat);size_t total=calls;
    cJSON *evidence=cJSON_GetObjectItemCaseSensitive(heartbeat,"ota");assert(evidence);
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(evidence,"running_version")->valuestring,"2.6.0"));
    assert(strlen(cJSON_GetObjectItemCaseSensitive(evidence,"image_sha256")->valuestring)==64);
    cJSON_Delete(heartbeat);
    for(size_t i=1;i<=total;i++){
        fail_at=0;heartbeat=cJSON_CreateObject();calls=0;fail_at=i;
        ota_manager_append_telemetry(heartbeat);assert(!cJSON_HasObjectItem(heartbeat,"ota"));cJSON_Delete(heartbeat);
    }
    fail_at=0;heartbeat=cJSON_CreateObject();fail_hash=true;
    ota_manager_append_telemetry(heartbeat);assert(!cJSON_HasObjectItem(heartbeat,"ota"));cJSON_Delete(heartbeat);
    puts("OTA evidence allocation regressions passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    c_file = temporary / "ota.c"
    c_file.write_text(ota_program)
    executable = temporary / "ota"
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(cjson), str(c_file), str(cjson / "cJSON.c"),
        "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)

start = connector.index("static char *outbox_record_line(")
end = connector.index("static char *attendance_outbox_record_line(", start)
outbox_program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#define ADD_OUTBOX_LINE_BYTES 8192
#define ESP_LOGE(...) ((void)0)
static size_t calls, fail_at;
static void *allocate(size_t size) { if(++calls==fail_at)return NULL;return malloc(size); }
static bool attendance_payload_is_valid(const cJSON *p) { return cJSON_IsObject(p); }
static bool attendance_payload_is_live(const cJSON *p) { (void)p;return true; }
static bool oracle_receipt_payload_is_valid(const cJSON *p) { return cJSON_IsObject(p); }
''' + connector[start:end] + r'''
int main(void)
{
    cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
    const char *types[]={"attendance_batch","oracle_receipt_batch"};
    for(unsigned kind=0;kind<2;++kind){
        calls=fail_at=0;bool live=false;
        char *line=outbox_record_line(types[kind],"{\"confirmation_path\":\"FIRMWARE_LIVE\"}",&live);
        assert(line && live && strstr(line,"payload") && strstr(line,types[kind]));free(line);
        size_t total=calls;
        for(size_t i=1;i<=total;++i){
            calls=0;fail_at=i;
            line=outbox_record_line(types[kind],"{\"confirmation_path\":\"FIRMWARE_LIVE\"}",&live);
            assert(!line);
        }
    }
    puts("Outbox envelope allocation regressions passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    c_file = temporary / "outbox.c"
    c_file.write_text(outbox_program)
    executable = temporary / "outbox"
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(cjson), str(c_file), str(cjson / "cJSON.c"),
        "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)

# Every allocation in the actual transport envelope must fail closed.
envelope = connector[connector.index("static cJSON *message_envelope("):
                     connector.index("static bool send_payload(\n")]
envelope_program = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "cJSON.h"
static size_t calls, fail_at;
static void *allocate(size_t size) { if (++calls == fail_at) return NULL; return malloc(size); }
''' + envelope + r'''
int main(void) {
    cJSON_Hooks hooks = {allocate, free}; cJSON_InitHooks(&hooks);
    cJSON *payload=cJSON_CreateObject(); calls=0;
    cJSON *root=message_envelope(payload,"attendance_batch","message","connector","boot",1,"time");
    assert(root); size_t total=calls; cJSON_Delete(root);
    for(size_t i=1;i<=total;i++) {
        fail_at=0;payload=cJSON_CreateObject();assert(payload);
        calls=0;fail_at=i;
        assert(!message_envelope(payload,"attendance_batch","message","connector","boot",1,"time"));
    }
    puts("Transport envelope allocation regressions passed");
}
'''
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "envelope.c"
    unit.write_text(envelope_program)
    executable = temporary / "envelope"
    subprocess.run([
        "cc", "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
        "-I", str(cjson), str(unit), str(cjson / "cJSON.c"),
        "-lm", "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], check=True)
