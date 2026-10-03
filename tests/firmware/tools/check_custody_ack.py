"""Exercise production websocket ACK dispatch, including stale and lost replies."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
CJSON = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
source = (MAIN / "add_connector.c").read_text()
start = source.index("static void parse_inbound(")
end = source.index('    if (cJSON_IsString(type) && strcmp(type->valuestring, "error") == 0)', start)
production = source[start:end] + "    cJSON_Delete(root);\n}\n"
type_end = source.index("} add_attendance_settlement_ack_t;") + len("} add_attendance_settlement_ack_t;")
attendance_type = source[source.rfind("typedef struct {", 0, type_end):type_end]
harness = r'''
#include "add_connector.h"
#include "zkt_custody_wire.h"
#include "evidence_receipt.h"
#include "cJSON.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define ADD_MAX_INBOUND_BYTES 65536
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
/* ATTENDANCE_TYPE */
static unsigned acknowledgements;
static int s_lock, s_ack_sem;
static bool s_ack_matched, s_waiting_evidence, s_waiting_hikvision, s_waiting_zkt_custody;
static char s_waiting_ack[80], s_hikvision_expected[65], s_reconcile_last_job_id[40];
static uint32_t s_reconcile_last_generation, s_reconcile_last_committed_ordinal;
static evidence_receipt_t s_evidence_expected;
static add_reconcile_chunk_ack_t s_reconcile_chunk_ack;
static add_source_tail_ack_t s_source_tail_ack;
static add_attendance_settlement_ack_t s_attendance_settlement_ack;
static zj_custody_expected_t s_zkt_custody_expected;
static uint8_t s_zkt_custody_receipt[32];
static unsigned allocations, fail_at;
static void *allocate(size_t bytes) { return ++allocations == fail_at ? NULL : malloc(bytes); }
static int xSemaphoreTake(int lock, unsigned timeout) { (void)lock; (void)timeout; return pdTRUE; }
static void xSemaphoreGive(int lock) { if (lock == s_ack_sem) ++acknowledgements; }
static size_t strlcpy(char *out, const char *text, size_t capacity)
{ size_t n = strlen(text); if (capacity) { size_t copy = n < capacity - 1 ? n : capacity - 1; memcpy(out, text, copy); out[copy] = 0; } return n; }
static bool evidence_identity(const cJSON *payload, evidence_receipt_t *out, bool receipt)
{ (void)payload; (void)out; (void)receipt; return false; }
static bool attendance_event_uid_is_valid(const char *value) { return value && strlen(value) == 64; }
static bool custody_digest(void *context, const uint8_t *bytes, size_t length, uint8_t out[32])
{ (void)context; assert(bytes && length); memset(out, 0x12, 32); return true; }
/* PRODUCTION */
static void arm(void)
{
    s_lock = 1; s_ack_sem = 2;
    strcpy(s_waiting_ack, "request-2");
    memset(s_zkt_custody_expected.observation_id, 'a', 64);
    memset(s_zkt_custody_expected.payload_digest, 'b', 64);
    memset(s_zkt_custody_receipt, 0, 32);
    s_waiting_zkt_custody = true;
    s_ack_matched = false;
    acknowledgements = 0;
}
static void response(char out[1024], const char *message_id, const char *payload_digest)
{
    snprintf(out, 1024, "{\"type\":\"zkt_observation_ack\",\"message_id\":\"%s\","
        "\"schema_version\":1,\"committed\":true,\"delivery_authority\":\"ADD\","
        "\"oracle_completion\":\"NOT_ASSERTED\",\"items\":[{\"index\":0,"
        "\"observation_id\":\"%s\",\"payload_digest\":\"%s\","
        "\"receipt_id\":\"11111111-2222-4333-8444-555555555555\","
        "\"custody\":\"PRESERVED_EXCEPTION\",\"replay\":true}]}",
        message_id, s_zkt_custody_expected.observation_id, payload_digest);
}
int main(void)
{
    arm();
    char ack[1024];
    response(ack, "request-1", s_zkt_custody_expected.payload_digest);
    parse_inbound(ack, strlen(ack));
    assert(!s_ack_matched && !acknowledgements && !strcmp(s_waiting_ack, "request-2"));
    const char *transport = "{\"type\":\"ack\",\"message_id\":\"request-2\"}";
    parse_inbound(transport, strlen(transport));
    assert(!s_ack_matched && acknowledgements == 1);
    for (unsigned i = 0; i < 32; ++i) assert(!s_zkt_custody_receipt[i]);
    arm();
    response(ack, "request-2", "different-payload");
    parse_inbound(ack, strlen(ack));
    assert(!s_ack_matched && acknowledgements == 1);
    arm();
    response(ack, "request-2", s_zkt_custody_expected.payload_digest);
    parse_inbound(ack, strlen(ack));
    assert(s_ack_matched && acknowledgements == 1 && !s_waiting_ack[0]);
    for (unsigned i = 0; i < 32; ++i) assert(s_zkt_custody_receipt[i] == 0x12);
    s_ack_matched = false; acknowledgements = 0;
    parse_inbound(ack, strlen(ack));
    assert(!s_ack_matched && !acknowledgements);

    cJSON_Hooks hooks = {allocate, free};
    cJSON_InitHooks(&hooks);
    for (unsigned point = 1; point < 100; ++point) {
        arm(); allocations = 0; fail_at = point;
        parse_inbound(ack, strlen(ack));
        assert(!s_ack_matched || allocations < point);
        if (!s_ack_matched) {
            assert(!acknowledgements);
            for (unsigned i = 0; i < 32; ++i) assert(!s_zkt_custody_receipt[i]);
        }
    }
    cJSON_InitHooks(NULL);
    puts("Production custody ACK dispatch, delayed/duplicate replies and allocation faults passed");
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="zkt-custody-ack-") as temporary:
    unit = Path(temporary) / "ack.c"
    unit.write_text(harness.replace("/* PRODUCTION */", production).replace("/* ATTENDANCE_TYPE */", attendance_type))
    binary = Path(temporary) / "ack"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    "-I", str(CJSON), str(unit), str(MAIN / "zkt_custody_receipt.c"),
                    str(MAIN / "evidence_receipt.c"), str(CJSON / "cJSON.c"), "-lm", "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
