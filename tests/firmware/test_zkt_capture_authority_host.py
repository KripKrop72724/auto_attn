"""Production live capture chooses its persisted authority before interpretation."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


@pytest.mark.parametrize("capture,hikvision", [(1, 0), (0, 0), (1, 1), (0, 1)])
def test_live_capture_has_no_dual_delivery_or_disabled_writer_fallback(tmp_path, capture, hikvision):
    source = (MAIN / "zone_lite.c").read_text()
    preservation = source[source.index("typedef enum { ZK_LIVE_HELD"):
                          source.index("static bool zk_recv_data_stream(")]
    processing = source[source.index("static size_t process_live_packet("):
                        source.index("static bool zk_register_attlog_events(")]
    start = source.index("                /* Keep this packet's route")
    dispatch = source[start:source.index('                add_connector_set_activity("LIVE_CAPTURE");', start)]
    end = source.index("} attendance_event_t;") + len("} attendance_event_t;")
    event_type = source[source.rfind("typedef struct {", 0, end):end]
    end = source.index("} enqueue_result_t;") + len("} enqueue_result_t;")
    result_type = source[source.rfind("typedef enum {", 0, end):end]
    program = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "reliability.h"
#include "zkt_journal_capture.h"
static bool legacy_permission, preserve_ok, g_force_truth_reconcile;
static unsigned captures, interpretations, deliveries, notices;
static unsigned live_events_since_sync;
static const uint8_t packet[20] = {1};
typedef struct {uint64_t header;} zk_header_t;
typedef struct {int unused;} user_table_t;
#define LED_EVENT_LIVE_PUNCH 1
#ifndef ZONE_LITE_HIKVISION
static bool zj_runtime_legacy_capture_allowed(void) { return legacy_permission; }
#endif
#if ZONE_LITE_JOURNAL_WRITES && !defined(ZONE_LITE_HIKVISION)
static int64_t epoch_now(void) {return 1800000000;}
static int64_t uptime_ms(void) {return 5000;}
static bool zj_capture_runtime_packet(const uint8_t *raw, size_t size, const zj_capture_facts_t *facts) {
    assert(raw==packet && size==sizeof(packet));
    assert(facts->time_quality==ZJ_TIME_UNKNOWN && facts->wall_seconds==1800000000 && facts->uptime_ms==5000);
    ++captures; return preserve_ok;
}
#endif
bool rel_live_frame_size(const uint8_t *raw,size_t length,size_t hint,size_t *size) {
    (void)hint;assert(raw==packet+sizeof(zk_header_t) && length==12);++interpretations;*size=12;return true;
}
bool rel_parse_live_record(const uint8_t *raw,size_t length,rel_live_record_t *out) {
    assert(raw==packet+sizeof(zk_header_t) && length==12);++interpretations;memset(out,0,sizeof(*out));strcpy(out->user_id,"SYNTHETIC");return true;
}
static void led_status_event(int event) {assert(event==LED_EVENT_LIVE_PUNCH);++notices;}
static bool add_connector_log(const char *level,const char *part,const char *code,const char *detail) {
    (void)level;(void)part;(void)code;(void)detail;return true;
}
''' + event_type + result_type + r'''
static bool build_attendance_event(attendance_event_t *out,const user_table_t *users,const char *user,
    uint16_t uid,uint32_t timestamp,uint8_t status,uint8_t punch,bool snapshot) {
    (void)users;(void)timestamp;(void)status;(void)punch;assert(!uid && snapshot && !strcmp(user,"SYNTHETIC"));
    ++interpretations;memset(out,0,sizeof(*out));return true;
}
static enqueue_result_t enqueue_event(const attendance_event_t *event,const char *capture_type) {
    (void)event;assert(!strcmp(capture_type,"LIVE"));++deliveries;return ENQUEUE_PENDING;
}
''' + preservation + processing + r'''
static void dispatch_packet(zk_live_path_t captured_path) {
    user_table_t table={0};user_table_t *users=&table;
    struct {uint8_t live_record_size;} ctx={0};
    struct {size_t length;} top={.length=sizeof(packet)};
''' + dispatch + r'''
}
int main(void) {
    legacy_permission=true;preserve_ok=false;
    zk_live_path_t pinned=zk_preserve_live_packet(packet,sizeof(packet));
    assert(pinned==ZK_LIVE_LEGACY && !captures);
    /* Permission snapshots can fail between preservation and dispatch. The
     * route of the already accepted legacy packet must not be reinterpreted. */
    legacy_permission=false;
    dispatch_packet(pinned);
    assert(deliveries==1 && notices==1 && live_events_since_sync==1);
    captures=interpretations=deliveries=notices=0;legacy_permission=false;
#ifdef ZONE_LITE_HIKVISION
    assert(zk_preserve_live_packet(packet,sizeof(packet))==ZK_LIVE_LEGACY && !captures);
    dispatch_packet(ZK_LIVE_LEGACY);
    assert(deliveries==1 && live_events_since_sync==2);
#else
    assert(zk_preserve_live_packet(packet,sizeof(packet))==ZK_LIVE_HELD && g_force_truth_reconcile);
    assert(!interpretations && !deliveries && !notices);
    preserve_ok=true;g_force_truth_reconcile=false;
#if ZONE_LITE_JOURNAL_WRITES
    assert(zk_preserve_live_packet(packet,sizeof(packet))==ZK_LIVE_JOURNAL && captures==2 && !g_force_truth_reconcile);
    dispatch_packet(ZK_LIVE_JOURNAL);
    assert(!interpretations && !deliveries && notices==1 && live_events_since_sync==2);
#else
    assert(zk_preserve_live_packet(packet,sizeof(packet))==ZK_LIVE_HELD && !captures && g_force_truth_reconcile);
#endif
    assert(!interpretations && !deliveries);
#endif
    return 0;
}
'''
    test = tmp_path / "capture-authority.c"
    test.write_text(program)
    binary = tmp_path / "capture-authority"
    flags = [f"-DZONE_LITE_JOURNAL_WRITES={capture}"]
    if hikvision:
        flags.append("-DZONE_LITE_HIKVISION=1")
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", *flags,
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN),
                    str(test), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=30)
