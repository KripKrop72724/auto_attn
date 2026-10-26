/* Host tests for storage_exception.c, the ZKT 2.7.7 confirm-first storage
 * exception's decision: the gate on Peshawar-02's 21 October report, each
 * term alone, the stable window, the record, the round outcomes and the
 * restart policy, and the OTA floor. Plain cc, ASan and UBSan. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "span_digest.h"
#include "storage_exception.h"

static unsigned checks;
#define EXPECT(cond)                                                                                                   \
    do {                                                                                                               \
        ++checks;                                                                                                      \
        if (!(cond)) {                                                                                                 \
            fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #cond);                                                 \
            return 1;                                                                                                  \
        }                                                                                                              \
    } while (0)
#define EXPECT_REASON(got, want)                                                                                       \
    do {                                                                                                               \
        ++checks;                                                                                                      \
        sx_reason_t got_ = (got);                                                                                      \
        if (got_ != (want)) {                                                                                          \
            fprintf(stderr, "%s:%d: %s, expected %s\n", __FILE__, __LINE__, sx_reason_name(got_),                      \
                    sx_reason_name(want));                                                                             \
            return 1;                                                                                                  \
        }                                                                                                              \
    } while (0)

/* P02's first 2.7.6 boot (21 October 2026, 13:29 UTC), the fields 2.7.7 adds
 * filled as that boot would have reported them: 51 spans of /pending.jsonl
 * held from offset 493,968 (span 1,968), 34 pages short, 67 custody blocks. */
static uint32_t p02_digest(void)
{
    uint32_t digest = 0;
    for (uint32_t span = 1968; span < 1968 + 51; ++span) digest = span_digest_add(digest, span);
    return digest;
}

static void p02_repair(sx_repair_t *r)
{
    memset(r, 0, sizeof(*r));
    r->ran = r->storage_allowed = r->pending_verify = true;
    strcpy(r->result, "DEFERRED");
    strcpy(r->mode, "FULL");
    strcpy(r->error_stage, "full");
    r->error_code = -10001;
    strcpy(r->area, "READY");
    strcpy(r->area_test, "PASSED");
    strcpy(r->trigger, "full");
    strcpy(r->short_reason, "candidate");
    r->mount_measured = true;
    r->erased_at_mount = 0;
    r->want = 34;
    r->target = 34;
    r->custody_blocks = 67;
    r->custody_gain = 67 * 4;
    r->file_count = 1;
    sx_repair_file_t *f = &r->file[0];
    strcpy(f->name, "/pending.jsonl");
    f->custody = true;
    f->size = 2526978;
    f->blocked_spans = 51;
    f->blocked_offset = 1968 * 251;
    f->blocked_last = (1968 + 50) * 251;
    f->blocked_digest = p02_digest();
}

/* 2.7.4's inventory at 13:31:08: six files, pending.jsonl 12,801 of
 * 2,526,978 bytes unreadable from 493,968, errno 5. */
static void p02_inventory(sx_inventory_t *v)
{
    memset(v, 0, sizeof(*v));
    v->done = v->complete = true;
    v->files = 6;
    v->unreadable_files = 1;
    v->count = 6;
    static const char *const names[6] = {"pending.jsonl", "acked_uids.txt", "add_live.jsonl", "add_pending.jsonl",
                                         "processed_commands.txt", "add_identities.enc"};
    for (int i = 0; i < 6; ++i) {
        strcpy(v->file[i].name, names[i]);
        v->file[i].bytes = i ? 200000U : 2526978U;
    }
    sx_inventory_file_t *f = &v->file[0];
    f->unreadable_bytes = 12801;
    f->first_error = EIO;
    f->first_error_offset = 493968;
    f->unreadable_spans = 51;
    f->last_error_offset = (1968 + 50) * 251;
    f->span_digest = p02_digest();
}

/* The running image as P02's 2.7.6 boot ran (the probe refused a fresh name
 * for room; the live punch at 13:41:16 failed to append for room). */
static void p02_runtime(sx_runtime_t *rt)
{
    memset(rt, 0, sizeof(*rt));
    rt->qs_known = rt->qs_observed = rt->qs_available = true;
    rt->probe_error = ENOENT;
    rt->probe_operation = "persistence_open_write";
    rt->last_error = ENOSPC;
    rt->last_operation = "local_write_commit";
    rt->led_known = true;
    rt->local_failures = rt->room_failures = 2;
    rt->spiffs_mounted = true;
    rt->spiffs_used = 4962772;
    rt->spiffs_total = 7703441;
    rt->health_known = true;
    rt->health_blockers = SX_H_QS_RECOVERY | SX_H_QS_PERSISTENCE | SX_H_QS_LAST_ERROR | SX_H_QS_PROBE_ERROR |
        SX_H_LED_LOCAL_FAILURE;
    rt->add_connected = rt->heartbeat_acked = true;
    rt->capability_accepted = rt->ota_precheck_ok = rt->passive_slot_ok = true;
    rt->internal_largest = 63488;
}

static sx_repair_t repair;
static sx_inventory_t inventory;
static sx_runtime_t runtime;
static sx_input_t input;

static void p02(void)
{
    p02_repair(&repair);
    p02_inventory(&inventory);
    p02_runtime(&runtime);
    input = (sx_input_t){.production_role = true, .running_pending = true, .ota_deployment = true,
                         .repair = &repair, .inventory = &inventory, .runtime = &runtime};
}

static int gate_terms(void)
{
    p02();
    EXPECT_REASON(sx_evaluate(&input), SX_OK);
    /* Which boots. */
    p02(); input.production_role = false; EXPECT_REASON(sx_evaluate(&input), SX_ROLE);
    p02(); input.running_pending = false; EXPECT_REASON(sx_evaluate(&input), SX_NOT_PENDING);
    p02(); input.ota_deployment = false; EXPECT_REASON(sx_evaluate(&input), SX_NOT_OTA_IMAGE);
    p02(); input.record_terminal = true; EXPECT_REASON(sx_evaluate(&input), SX_ALREADY_USED);
    /* The boot repair. */
    p02(); repair.ran = false; EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_MISSING);
    p02(); repair.pending_verify = false; EXPECT_REASON(sx_evaluate(&input), SX_NOT_PENDING);
    p02(); repair.previous_failed = true; EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_GUARD);
    p02(); strcpy(repair.mode, "REPORT_ONLY"); EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_GUARD);
    p02(); repair.mount_failed = true; EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_RESULT);
    p02(); strcpy(repair.result, "FAILED"); EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_RESULT);
    p02(); strcpy(repair.result, "REPAIRED"); EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_RESULT);
    p02(); repair.storage_allowed = false; EXPECT_REASON(sx_evaluate(&input), SX_STORE_NOT_MOUNTED);
    p02(); strcpy(repair.error_stage, "time"); EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_STAGE);
    p02(); strcpy(repair.trigger, "gc_unsafe"); EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_STAGE);
    p02(); repair.error_code = -10010; EXPECT_REASON(sx_evaluate(&input), SX_REPAIR_STAGE);
    p02(); strcpy(repair.error_stage, "gc_unsafe"); strcpy(repair.trigger, "gc_unsafe");
    EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); strcpy(repair.area, "FOREIGN"); EXPECT_REASON(sx_evaluate(&input), SX_AREA_NOT_READY);
    p02(); strcpy(repair.area_test, "FAILED"); EXPECT_REASON(sx_evaluate(&input), SX_AREA_TEST);
    p02(); repair.area_test[0] = '\0'; EXPECT_REASON(sx_evaluate(&input), SX_AREA_TEST);
    p02(); strcpy(repair.short_reason, "slots"); EXPECT_REASON(sx_evaluate(&input), SX_SHORT_REASON);
    p02(); repair.rounds = 1; EXPECT_REASON(sx_evaluate(&input), SX_ROUND_RAN);
    p02(); repair.blocks = 1; EXPECT_REASON(sx_evaluate(&input), SX_ROUND_RAN);
    p02(); repair.failed = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.held_mismatch = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.records_open = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.reported = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.sources = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.store_blocked = true; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.waiting = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECLAIM_FAULT);
    p02(); repair.manifest_active = true; EXPECT_REASON(sx_evaluate(&input), SX_MANIFEST);
    p02(); repair.mount_measured = false; EXPECT_REASON(sx_evaluate(&input), SX_ARRIVED_WITH_ROOM);
    /* A store that arrived with the room the work needs was filled by this
     * image's own run: a broken image must not confirm (critique 10). */
    p02(); repair.erased_at_mount = 34; EXPECT_REASON(sx_evaluate(&input), SX_ARRIVED_WITH_ROOM);
    p02(); repair.erased_at_mount = 33; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); repair.check_deleted_live = 1; EXPECT_REASON(sx_evaluate(&input), SX_CHECK_DELETED);
    p02(); repair.check_delete_bad_file = 1; EXPECT_REASON(sx_evaluate(&input), SX_CHECK_DELETED);
    /* Custody. */
    p02(); repair.files_unheld_custody = 1; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_UNHELD);
    p02(); repair.file[0].unheld = true; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_UNHELD);
    p02(); repair.gaps = 1; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_LOST);
    p02(); repair.unreadable_bytes = 1; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_LOST);
    p02(); repair.records_dropped = 1; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_LOST);
    p02(); repair.interrupted_losses = 1; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_LOST);
    p02(); repair.commands_damaged = true; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_LOST);
    p02(); repair.records_pending = 1; EXPECT_REASON(sx_evaluate(&input), SX_RECORDS_UNACKED);
    /* Room. Every counted custody block drops a page: 67 blocks, 34 short. */
    p02(); repair.want = 0; EXPECT_REASON(sx_evaluate(&input), SX_ROOM_UNKNOWN);
    p02(); repair.custody_gain = 33; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_SHORT);
    p02(); repair.custody_gain = 34; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); repair.custody_blocks = 0; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_BLOCKS);
    p02(); repair.custody_blocks = 257; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_BLOCKS);
    /* The inventory against the held spans (C4, critique 8). */
    p02(); inventory.done = false; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_PENDING);
    EXPECT(sx_reason_unknown(SX_INVENTORY_PENDING));
    p02(); inventory.complete = false; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.error = EIO; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.unreadable_files = 17; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.unreadable_files = 2; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].first_error = ENOENT; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].other_unreadable = true; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].span_errors_other = 1; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    /* One unheld unreadable span among the held ones: the count, the first,
     * the last or the digest differs. */
    p02(); inventory.file[0].unreadable_spans = 52; inventory.file[0].unreadable_bytes += 251;
    EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].first_error_offset -= 251; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].last_error_offset += 251; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    /* The same count, first and last, one span moved inside: the digest. */
    p02(); inventory.file[0].span_digest = span_digest_add(p02_digest() - span_digest_add(0, 1990), 2100);
    EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); inventory.file[0].unreadable_bytes = 50 * 251; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    /* Another file unreadable, which the repair does not hold. */
    p02(); inventory.unreadable_files = 2; inventory.file[1].first_error = EIO; inventory.file[1].unreadable_spans = 1;
    inventory.file[1].unreadable_bytes = 251; EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    /* A held file that now reads whole: the sets differ. */
    p02(); memset(&inventory.file[0].unreadable_bytes, 0, sizeof(inventory.file[0]) - offsetof(sx_inventory_file_t, unreadable_bytes));
    inventory.file[0].bytes = 2526978; inventory.unreadable_files = 0;
    EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    /* A custody file the repair reported is gone or shorter (critique 7). */
    p02(); strcpy(inventory.file[0].name, "pending.old"); inventory.unreadable_files = 1;
    EXPECT_REASON(sx_evaluate(&input), SX_INVENTORY_UNEXPLAINED);
    p02(); repair.file_count = 2; strcpy(repair.file[1].name, "/acked_uids.txt"); repair.file[1].custody = true;
    repair.file[1].size = 200001; EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_MISSING);
    repair.file[1].size = 200000; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    repair.file[1].removed = true; repair.file[1].size = 900000; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); repair.file_count = 2; strcpy(repair.file[1].name, "/add_cancelled.txt"); repair.file[1].custody = true;
    EXPECT_REASON(sx_evaluate(&input), SX_CUSTODY_MISSING);
    /* Runtime storage evidence: only "no room" (S2, H2). */
    p02(); runtime.spiffs_mounted = false; EXPECT_REASON(sx_evaluate(&input), SX_STORE_NOT_MOUNTED);
    p02(); runtime.qs_known = false; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_UNKNOWN);
    EXPECT(sx_reason_unknown(SX_STORAGE_UNKNOWN));
    p02(); runtime.qs_available = false; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_ERROR_CLASS);
    p02(); runtime.probe_operation = "persistence_open_read"; EXPECT_REASON(sx_evaluate(&input), SX_PROBE_ERROR_CLASS);
    p02(); runtime.probe_error = EIO; EXPECT_REASON(sx_evaluate(&input), SX_PROBE_ERROR_CLASS);
    p02(); runtime.probe_error = ENOSPC; runtime.probe_operation = "persistence_capacity";
    EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); runtime.probe_error = ENOENT; runtime.probe_operation = "persistence_capacity";
    EXPECT_REASON(sx_evaluate(&input), SX_PROBE_ERROR_CLASS);
    p02(); runtime.probe_operation = "persistence_nvs_commit"; runtime.probe_error = ENOSPC;
    EXPECT_REASON(sx_evaluate(&input), SX_PROBE_ERROR_CLASS);
    p02(); runtime.last_error = ENOENT; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_ERROR_CLASS);
    p02(); runtime.last_error = EIO; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_ERROR_CLASS);
    p02(); runtime.last_error = EBADMSG; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_ERROR_CLASS);
    p02(); runtime.last_operation = "segment_read"; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_ERROR_CLASS);
    p02(); runtime.last_operation = "capacity_admission"; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    p02(); runtime.last_error = 0; runtime.probe_error = 0; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    /* The LED (S3, H1): every incident classified, by errno at the site. */
    p02(); runtime.led_known = false; EXPECT_REASON(sx_evaluate(&input), SX_STORAGE_UNKNOWN);
    p02(); runtime.led_fatal = true; EXPECT_REASON(sx_evaluate(&input), SX_LED_FATAL);
    p02(); runtime.local_failures = 3; EXPECT_REASON(sx_evaluate(&input), SX_LED_CAUSE);
    p02(); runtime.local_failures = 3; runtime.held_read_failures = 1; runtime.held_read_queues = 1U << 0;
    EXPECT_REASON(sx_evaluate(&input), SX_OK);
    /* A read failure of a queue the repair does not report blocked. */
    p02(); runtime.local_failures = 3; runtime.held_read_failures = 1; runtime.held_read_queues = 1U << 1;
    EXPECT_REASON(sx_evaluate(&input), SX_LED_CAUSE);
    p02(); runtime.local_failures = 3; runtime.held_read_failures = 1; runtime.held_read_queues = 1U << 7;
    EXPECT_REASON(sx_evaluate(&input), SX_LED_CAUSE);
    /* A held read must start at or before the last held span (H1): the
     * last span's first byte to its last byte, not past it. */
    p02(); runtime.local_failures = 3; runtime.held_read_failures = 1; runtime.held_read_queues = 1U << 0;
    runtime.held_read_last[0] = (1968 + 50) * 251 + 250; EXPECT_REASON(sx_evaluate(&input), SX_OK);
    runtime.held_read_last[0]++; EXPECT_REASON(sx_evaluate(&input), SX_LED_CAUSE);
    /* Every other boot-health term (3.7). */
    p02(); runtime.health_known = false; EXPECT_REASON(sx_evaluate(&input), SX_HEALTH_UNKNOWN);
    for (uint32_t bit = 1; bit <= SX_H_TERMINAL_COUNTS; bit <<= 1) {
        p02();
        runtime.health_blockers |= bit;
        EXPECT_REASON(sx_evaluate(&input), (bit & SX_HEALTH_EXCEPTED) ? SX_OK : SX_HEALTH);
        EXPECT(strcmp(sx_health_name(bit), "UNKNOWN"));
    }
    /* ADD and the OTA path. */
    p02(); runtime.add_connected = false; EXPECT_REASON(sx_evaluate(&input), SX_ADD_DISCONNECTED);
    /* D2: a device whose heartbeats ADD drops (and so whose floor, confirm
     * reason and exception diagnostics the operator cannot see) refuses. */
    p02(); runtime.heartbeat_acked = false; EXPECT_REASON(sx_evaluate(&input), SX_HEARTBEAT_UNACKED);
    p02(); runtime.capability_accepted = false; EXPECT_REASON(sx_evaluate(&input), SX_CAPABILITY);
    p02(); runtime.ota_precheck_ok = false; EXPECT_REASON(sx_evaluate(&input), SX_OTA_PRECHECK);
    p02(); runtime.passive_slot_ok = false; EXPECT_REASON(sx_evaluate(&input), SX_OTA_SLOT);
    p02(); runtime.internal_largest = SX_INTERNAL_FLOOR_BYTES - 1; EXPECT_REASON(sx_evaluate(&input), SX_HEAP);
    for (int r = 0; r < SX_REASON_COUNT; ++r) EXPECT(strcmp(sx_reason_name((sx_reason_t)r), "UNKNOWN"));
    for (int s = 0; s < SX_STATE_COUNT; ++s) EXPECT(strcmp(sx_state_name((sx_state_t)s), "UNKNOWN"));
    return 0;
}

static int window(void)
{
    sx_window_t w = {0};
    sx_store_t a = {.used = 5, .total = 9, .writes = 40}, z = {0};
    /* 119 s held: stabilizing; 120 s: eligible. */
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1000, &a), SX_STABILIZING);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1119, &a), SX_STABILIZING);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1120, &a), SX_OK);
    EXPECT(sx_window_same(&w, &a) && !sx_window_same(&w, &z) && !sx_window_same(NULL, &a));
    /* A lock that was busy neither refuses nor restarts the clock (M6). */
    EXPECT_REASON(sx_window_step(&w, SX_STORAGE_UNKNOWN, 1121, &z), SX_STORAGE_UNKNOWN);
    EXPECT_REASON(sx_window_step(&w, SX_HEALTH_UNKNOWN, 1121, &z), SX_HEALTH_UNKNOWN);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1122, &a), SX_OK);
    /* A refusal closes it; the clock starts again. */
    EXPECT_REASON(sx_window_step(&w, SX_LED_CAUSE, 1130, &a), SX_LED_CAUSE);
    EXPECT(!sx_window_same(&w, &a));
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1131, &a), SX_STABILIZING);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1250, &a), SX_STABILIZING);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1251, &a), SX_OK);
    /* The store changed under the gate (critique 9): its used bytes, or a
     * single program or erase of the VFS's SPIFFS (a rewrite of the same
     * size, a deletion, a garbage collection): the clock restarts. */
    sx_store_t b = a;
    b.used = 6;
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1260, &b), SX_STORE_CHANGED);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1379, &b), SX_STABILIZING);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1380, &b), SX_OK);
    sx_store_t c = b;
    c.writes++;
    EXPECT(!sx_window_same(&w, &c));
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1390, &c), SX_STORE_CHANGED);
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1510, &c), SX_OK);
    c.total--;
    EXPECT_REASON(sx_window_step(&w, SX_OK, 1511, &c), SX_STORE_CHANGED);
    /* An unknown before the window opened does not open it. */
    sx_window_t fresh = {0};
    EXPECT_REASON(sx_window_step(&fresh, SX_INVENTORY_PENDING, 10, &a), SX_INVENTORY_PENDING);
    EXPECT(!fresh.open);
    EXPECT_REASON(sx_window_step(&fresh, SX_OK, 10, NULL), SX_STABILIZING);
    EXPECT(!fresh.open);
    return 0;
}

static int record(void)
{
    sx_record_t r = {0};
    r.state = SX_STATE_CONFIRMING;
    r.deployment_crc = sx_deployment_crc("8f1c2a3b-0000-4000-8000-000000000001");
    r.image_tag = 0x27700001U;
    sx_record_seal(&r);
    EXPECT(sx_record_valid(&r) && r.version == SX_RECORD_VERSION);
    EXPECT(sx_record_confirmed(&r, 0x27700001U) && !sx_record_confirmed(&r, 0x27600001U));
    EXPECT(!sx_record_ended(&r, r.deployment_crc));
    /* A torn write never reads as another valid record: every single byte
     * changed fails the CRC. */
    for (size_t i = 0; i < sizeof(r); ++i) {
        sx_record_t torn = r;
        ((uint8_t *)&torn)[i] ^= 0x5A;
        EXPECT(!sx_record_valid(&torn));
        EXPECT(!sx_record_confirmed(&torn, 0x27700001U));
    }
    sx_record_t erased;
    memset(&erased, 0xFF, sizeof(erased));
    EXPECT(!sx_record_valid(&erased));
    /* One exception per deployment: an ended record of this deployment, not
     * of another. */
    r.state = SX_STATE_ABANDONED;
    sx_record_seal(&r);
    EXPECT(sx_record_ended(&r, r.deployment_crc) && !sx_record_ended(&r, sx_deployment_crc("other")));
    EXPECT(!sx_record_confirmed(&r, 0x27700001U));
    r.state = SX_STATE_UNREPAIRED;
    sx_record_seal(&r);
    EXPECT(sx_record_ended(&r, r.deployment_crc) && sx_record_confirmed(&r, 0x27700001U));
    EXPECT(sx_deployment_crc("") == 0 && sx_deployment_crc(NULL) == 0);
    EXPECT(sizeof(sx_record_t) == 36);
    return 0;
}

static void repaired(sx_repair_t *a, const char *result)
{
    memset(a, 0, sizeof(*a));
    a->ran = a->storage_allowed = true;
    strcpy(a->result, result);
    strcpy(a->mode, "FULL");
    strcpy(a->area, "READY");
}

static int outcomes(void)
{
    sx_repair_t a;
    repaired(&a, "REPAIRED"); EXPECT(sx_round_outcome(&a) == SX_ROUND_OK);
    repaired(&a, "CLEAN"); EXPECT(sx_round_outcome(&a) == SX_ROUND_OK);
    repaired(&a, "REPAIRED_WITH_GAPS"); a.gaps = 1; EXPECT(sx_round_outcome(&a) == SX_ROUND_OK);
    /* Repaired, but spans wait for byte sources: the VFS stays off (2.7.6
     * decision 5 (c) on exception devices), and a later boot may do better. */
    repaired(&a, "REPAIRED"); a.waiting = 2; EXPECT(sx_round_outcome(&a) == SX_ROUND_WAITING);
    repaired(&a, "REPAIRED"); a.storage_allowed = false; a.store_blocked = true;
    EXPECT(sx_round_outcome(&a) == SX_ROUND_STORE_BLOCKED);
    repaired(&a, "NOT_RUN"); strcpy(a.error_stage, "unmounted"); EXPECT(sx_round_outcome(&a) == SX_ROUND_STORE_BLOCKED);
    repaired(&a, "DEFERRED"); a.held_mismatch = 1; EXPECT(sx_round_outcome(&a) == SX_ROUND_MISMATCH);
    repaired(&a, "DEFERRED"); strcpy(a.area, "FOREIGN"); EXPECT(sx_round_outcome(&a) == SX_ROUND_AREA);
    repaired(&a, "DEFERRED"); strcpy(a.area, "ABSENT"); EXPECT(sx_round_outcome(&a) == SX_ROUND_AREA);
    repaired(&a, "DEFERRED"); strcpy(a.area, "UNREADABLE"); EXPECT(sx_round_outcome(&a) == SX_ROUND_AREA_UNREADABLE);
    repaired(&a, "REPORT_ONLY"); EXPECT(sx_round_outcome(&a) == SX_ROUND_GUARD);
    repaired(&a, "FAILED"); strcpy(a.error_stage, "remount"); EXPECT(sx_round_outcome(&a) == SX_ROUND_REMOUNT);
    repaired(&a, "DEFERRED"); a.gaps = 1; EXPECT(sx_round_outcome(&a) == SX_ROUND_GAPS);
    repaired(&a, "DEFERRED"); strcpy(a.short_reason, "hold"); EXPECT(sx_round_outcome(&a) == SX_ROUND_HOLD);
    repaired(&a, "DEFERRED"); strcpy(a.short_reason, "time"); EXPECT(sx_round_outcome(&a) == SX_ROUND_FAILED);
    a.pages_freed = 3; EXPECT(sx_round_outcome(&a) == SX_ROUND_TIME);
    repaired(&a, "DEFERRED"); strcpy(a.short_reason, "cap"); EXPECT(sx_round_outcome(&a) == SX_ROUND_FAILED);
    a.pages_freed = 3; EXPECT(sx_round_outcome(&a) == SX_ROUND_CAP);
    repaired(&a, "STOPPED"); a.pages_freed = 1; EXPECT(sx_round_outcome(&a) == SX_ROUND_TIME);
    repaired(&a, "DEFERRED"); strcpy(a.short_reason, "candidate"); a.pages_freed = 9;
    EXPECT(sx_round_outcome(&a) == SX_ROUND_NO_CANDIDATE);
    repaired(&a, "DEFERRED"); EXPECT(sx_round_outcome(&a) == SX_ROUND_FAILED);
    a.pages_freed = 1; EXPECT(sx_round_outcome(&a) == SX_ROUND_DEFERRED);
    repaired(&a, "FAILED"); EXPECT(sx_round_outcome(&a) == SX_ROUND_FAILED);
    EXPECT(sx_round_outcome(NULL) == SX_ROUND_DEFERRED);
    for (int o = SX_ROUND_TIME; o <= SX_ROUND_DEFERRED; ++o) EXPECT(sx_outcome_retryable((sx_outcome_t)o));
    for (int o = SX_ROUND_MISMATCH; o < SX_ROUND_COUNT; ++o) EXPECT(!sx_outcome_retryable((sx_outcome_t)o));
    EXPECT(!sx_outcome_retryable(SX_ROUND_OK));
    for (int o = 0; o < SX_ROUND_COUNT; ++o) EXPECT(strcmp(sx_outcome_name((sx_outcome_t)o), "UNKNOWN"));
    return 0;
}

static sx_record_t at(sx_state_t state, uint8_t reason, uint8_t restarts, uint8_t round_boots, uint32_t epoch)
{
    sx_record_t r = {0};
    r.state = (uint8_t)state;
    r.reason = reason;
    r.restarts = restarts;
    r.round_boots = round_boots;
    r.restart_epoch = epoch;
    r.image_tag = 7;
    sx_record_seal(&r);
    return r;
}

static int restarts(void)
{
    uint32_t wait = 0;
    const uint32_t t0 = 1800000000U;
    /* The confirming boot: its one restart; counted before it, so a second
     * look at the same boot never restarts again. */
    sx_record_t r = at(SX_STATE_CONFIRMED, 0, 0, 0, 0);
    EXPECT(sx_next_step(&r, true, t0, 0, &wait) == SX_NEXT_RESTART);
    EXPECT(sx_next_step(&r, false, 0, 0, &wait) == SX_NEXT_RESTART);
    r = at(SX_STATE_CONFIRMED, 0, 1, 0, t0);
    EXPECT(sx_next_step(&r, true, t0 + 10, 10, &wait) == SX_NEXT_UNREPAIRED);
    /* The CONFIRMED record failed to write: CONFIRMING on the valid image. */
    r = at(SX_STATE_CONFIRMING, 0, 0, 0, 0);
    EXPECT(sx_next_step(&r, true, t0, 0, &wait) == SX_NEXT_RESTART);
    /* A round boot that could not record its outcome. */
    r = at(SX_STATE_CONFIRMED, 0, 1, 1, t0);
    EXPECT(sx_next_step(&r, true, t0 + 10, 10, &wait) == SX_NEXT_UNREPAIRED);
    /* Done: nothing more. */
    r = at(SX_STATE_ROUND_DONE, 0, 1, 1, t0);
    EXPECT(sx_next_step(&r, true, t0 + 10, 10, &wait) == SX_NEXT_NONE);
    /* Retryable: the second after 1 h, the third after 6 h more, no fourth. */
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 1, 1, t0);
    EXPECT(sx_next_step(&r, true, t0 + 3599, 0, &wait) == SX_NEXT_WAIT && wait == 1);
    EXPECT(sx_next_step(&r, true, t0 + 3600, 0, &wait) == SX_NEXT_RESTART);
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_STORE_BLOCKED, 2, 2, t0);
    EXPECT(sx_next_step(&r, true, t0 + 6 * 3600 - 1, 0, &wait) == SX_NEXT_WAIT && wait == 1);
    EXPECT(sx_next_step(&r, true, t0 + 6 * 3600, 0, &wait) == SX_NEXT_RESTART);
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_STORE_BLOCKED, 3, 3, t0);
    EXPECT(sx_next_step(&r, true, t0 + 99 * 3600, 0, &wait) == SX_NEXT_UNREPAIRED);
    /* An unknown wall clock backs off on this boot's own clock instead of
     * waiting forever (design 14.2 item 4): the second restart after
     * 1 h of uptime, the third after 6 h, and the cap still ends it. */
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 1, 1, t0);
    EXPECT(sx_next_step(&r, false, 0, 0, &wait) == SX_NEXT_WAIT && wait == 3600);
    EXPECT(sx_next_step(&r, false, 0, 3599, &wait) == SX_NEXT_WAIT && wait == 1);
    EXPECT(sx_next_step(&r, false, 0, 3600, &wait) == SX_NEXT_RESTART);
    /* The restart was counted with the clock unknown (restart_epoch 0): the
     * uptime backoff also applies when time is known now. */
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 1, 1, 0);
    EXPECT(sx_next_step(&r, true, t0, 0, &wait) == SX_NEXT_WAIT && wait == 3600);
    EXPECT(sx_next_step(&r, true, t0, 3600, &wait) == SX_NEXT_RESTART);
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 2, 2, 0);
    EXPECT(sx_next_step(&r, false, 0, 6 * 3600 - 1, &wait) == SX_NEXT_WAIT && wait == 1);
    EXPECT(sx_next_step(&r, false, 0, 6 * 3600, &wait) == SX_NEXT_RESTART);
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 3, 3, 0);
    EXPECT(sx_next_step(&r, false, 0, 99 * 3600, &wait) == SX_NEXT_UNREPAIRED);
    /* Never after an outcome no boot can change, or after a crash (M7). */
    for (int o = SX_ROUND_MISMATCH; o < SX_ROUND_COUNT; ++o) {
        r = at(SX_STATE_ROUND_SHORT, (uint8_t)o, 1, 1, t0);
        EXPECT(sx_next_step(&r, true, t0 + 99 * 3600, 0, &wait) == SX_NEXT_UNREPAIRED);
    }
    r = at(SX_STATE_ROUND_SHORT, SX_ROUND_TIME, 1, 1, t0);
    r.crashes = 1;
    sx_record_seal(&r);
    EXPECT(sx_next_step(&r, true, t0 + 99 * 3600, 0, &wait) == SX_NEXT_UNREPAIRED);
    /* Ended or other states: nothing (the caller resumes an UNREPAIRED report). */
    r = at(SX_STATE_UNREPAIRED, 0, 1, 1, t0);
    EXPECT(sx_next_step(&r, true, t0, 0, &wait) == SX_NEXT_NONE);
    r = at(SX_STATE_ABANDONED, 0, 0, 0, 0);
    EXPECT(sx_next_step(&r, true, t0, 0, &wait) == SX_NEXT_NONE);
    sx_record_t invalid = r;
    invalid.crc ^= 1;
    EXPECT(sx_next_step(&invalid, true, t0, 0, &wait) == SX_NEXT_NONE);
    return 0;
}

/* Power cuts at every write of the confirmation and after it, as the next
 * boots' decisions read them (critique 14, M3, M4, M7): the durable state is
 * the otadata entry (pending or valid), the OTA journal and the record, and
 * every boot decides from them alone through the module the firmware runs.
 * The confirming boot writes, in order: the record CONFIRMING, otadata
 * valid, the record CONFIRMED, the journal RECONCILING, the record's restart
 * count (before the safepoint is claimed). Then the round boot records its
 * outcome. A cut while the image is still pending rolls back (the
 * bootloader) and the record is stale on any later pending boot; after
 * otadata says valid nothing ever invalidates it, the next boot is a round
 * boot, and the boots that follow are bounded: no boot loop. */
typedef struct {
    bool pending, old_image;
    char journal[16];
    sx_record_t record;
} durable_t;

static int boots_after(durable_t *d, uint32_t tag, uint32_t crc, sx_outcome_t round, bool *unrepaired)
{
    uint32_t now = 1800000000U;
    *unrepaired = false;
    for (unsigned boot = 0; boot < 32; ++boot) {
        if (d->pending) {
            /* Any reset of an unconfirmed image: the bootloader takes the old
             * one, which ignores the record. */
            d->old_image = true;
            return 0;
        }
        sx_boot_t action = sx_boot_action(d->journal, d->pending, &d->record, tag, crc);
        if (action == SX_BOOT_CONFIRM) return -1; /* a valid image never re-enters the loop */
        if (action == SX_BOOT_RECONCILE) strcpy(d->journal, "RECONCILING");
        /* The repair ran; its outcome is recorded on an exception image. */
        (void)sx_record_after_boot(&d->record, tag, d->pending, false, round);
        if (action == SX_BOOT_REPORT_ENDED || (sx_record_ended(&d->record, crc) && !d->record.reported)) {
            d->record.reported = 1;
            sx_record_seal(&d->record);
            d->journal[0] = '\0';
        }
        uint32_t wait = 0;
        sx_next_t next = sx_next_step(&d->record, true, now, 0, &wait);
        if (next == SX_NEXT_NONE) return (int)boot + 1;
        if (next == SX_NEXT_UNREPAIRED) {
            d->record.state = SX_STATE_UNREPAIRED;
            sx_record_seal(&d->record);
            *unrepaired = true;
            continue; /* the next pass reports it and clears the journal */
        }
        if (next == SX_NEXT_WAIT) now += wait ? wait : 3600;
        /* A controlled restart: counted before the claim. */
        d->record.restarts++;
        d->record.restart_epoch = now;
        sx_record_seal(&d->record);
    }
    return -2; /* a boot loop */
}

static int cuts(void)
{
    const uint32_t tag = 0x27700001U, crc = sx_deployment_crc("8f1c2a3b-0000-4000-8000-000000000001");
    const sx_outcome_t outcomes[] = {SX_ROUND_OK, SX_ROUND_TIME, SX_ROUND_WAITING, SX_ROUND_MISMATCH};
    for (size_t o = 0; o < sizeof(outcomes) / sizeof(outcomes[0]); ++o) {
        for (int cut = 0; cut <= 5; ++cut) {
            durable_t d = {.pending = true};
            strcpy(d.journal, "READY_TO_BOOT");
            if (cut >= 1) {
                d.record.state = SX_STATE_CONFIRMING;
                d.record.deployment_crc = crc;
                d.record.image_tag = tag;
                sx_record_seal(&d.record);
            }
            if (cut >= 2) d.pending = false;
            if (cut >= 3) {
                d.record.state = SX_STATE_CONFIRMED;
                sx_record_seal(&d.record);
            }
            if (cut >= 4) strcpy(d.journal, "RECONCILING");
            if (cut >= 5) {
                d.record.restarts = 1;
                sx_record_seal(&d.record);
            }
            bool unrepaired = false;
            int boots = boots_after(&d, tag, crc, outcomes[o], &unrepaired);
            EXPECT(boots >= 0);
            if (cut < 2) {
                /* Rolled back as 2.7.6; the record means nothing to a later
                 * unconfirmed boot of the same image (another deployment). */
                EXPECT(d.old_image);
                EXPECT(!sx_exception_active(&d.record, tag, true));
                EXPECT(!sx_record_ended(&d.record, sx_deployment_crc("next deployment")));
                sx_record_t stale = d.record;
                EXPECT(!sx_record_after_boot(&stale, tag, true, false, SX_ROUND_OK));
                continue;
            }
            EXPECT(!d.old_image && !d.pending);
            EXPECT(!strcmp(d.journal, outcomes[o] == SX_ROUND_OK ? "RECONCILING" : ""));
            EXPECT(d.record.restarts <= SX_MAX_RESTARTS);
            EXPECT(d.record.round_boots >= 1);
            if (outcomes[o] == SX_ROUND_OK) {
                EXPECT(d.record.state == SX_STATE_ROUND_DONE && !unrepaired && boots == 1);
            } else {
                EXPECT(d.record.state == SX_STATE_UNREPAIRED && unrepaired && d.record.reported);
                if (outcomes[o] == SX_ROUND_MISMATCH) EXPECT(d.record.round_boots == 1);
            }
        }
    }
    /* The VALID-image rule on its own (M4): READY_TO_BOOT on a valid image
     * reconciles whatever the record says, even with none. */
    sx_record_t none = {0};
    EXPECT(sx_boot_action("READY_TO_BOOT", false, &none, tag, crc) == SX_BOOT_RECONCILE);
    EXPECT(sx_boot_action("READY_TO_BOOT", true, &none, tag, crc) == SX_BOOT_CONFIRM);
    EXPECT(sx_boot_action("RECONCILING", false, &none, tag, crc) == SX_BOOT_ORDINARY);
    EXPECT(sx_boot_action("", false, &none, tag, crc) == SX_BOOT_ORDINARY);
    EXPECT(sx_boot_action(NULL, true, &none, tag, crc) == SX_BOOT_ORDINARY);
    EXPECT(!sx_may_invalidate(false) && sx_may_invalidate(true));
    /* An ended record resumes its FAILED report until receipted, for this
     * image and deployment only. */
    sx_record_t ended = {.state = SX_STATE_UNHEALTHY, .image_tag = tag, .deployment_crc = crc};
    sx_record_seal(&ended);
    EXPECT(sx_boot_action("RECONCILING", false, &ended, tag, crc) == SX_BOOT_REPORT_ENDED);
    EXPECT(sx_boot_action("RECONCILING", false, &ended, tag + 1, crc) == SX_BOOT_ORDINARY);
    EXPECT(sx_boot_action("RECONCILING", false, &ended, tag, crc + 1) == SX_BOOT_ORDINARY);
    ended.reported = 1;
    sx_record_seal(&ended);
    EXPECT(sx_boot_action("RECONCILING", false, &ended, tag, crc) == SX_BOOT_ORDINARY);
    /* Crashes after confirmation are counted and stop retries (M7). */
    sx_record_t r = {.state = SX_STATE_CONFIRMED, .image_tag = tag, .deployment_crc = crc, .restarts = 1};
    sx_record_seal(&r);
    EXPECT(sx_record_after_boot(&r, tag, false, true, SX_ROUND_TIME));
    EXPECT(r.crashes == 1 && r.state == SX_STATE_ROUND_SHORT && r.round_boots == 1 && sx_record_valid(&r));
    uint32_t wait = 0;
    EXPECT(sx_next_step(&r, true, 1900000000U, 0, &wait) == SX_NEXT_UNREPAIRED);
    r.state = SX_STATE_ROUND_DONE;
    sx_record_seal(&r);
    EXPECT(sx_record_after_boot(&r, tag, false, true, SX_ROUND_FAILED));
    EXPECT(r.state == SX_STATE_ROUND_DONE && r.crashes == 2 && r.round_boots == 1);
    EXPECT(!sx_record_after_boot(&r, tag, false, false, SX_ROUND_FAILED));
    EXPECT(!sx_record_after_boot(&r, tag + 1, false, true, SX_ROUND_OK));
    return 0;
}

/* Review of 26 October, D1: deployment A of 2.7.7 wrote CONFIRMING, then a
 * panic before mark_app_valid rolled it back (no ABANDONED). Deployment B of
 * the same image must not inherit A's record, whether B confirms ordinarily
 * or the image later runs valid some other way. */
static int stale_record(void)
{
    const uint32_t tag = 0x2770U, a = sx_deployment_crc("deployment-A"), b = sx_deployment_crc("deployment-B");
    sx_record_t stale = {.state = SX_STATE_CONFIRMING, .image_tag = tag, .deployment_crc = a};
    sx_record_seal(&stale);
    /* The reviewer's reproduction: nothing ends A's record for B, and a
     * valid boot of the image reads it as an exception boot. */
    EXPECT(!sx_record_ended(&stale, b));
    EXPECT(!sx_exception_active(&stale, tag, true) && sx_exception_active(&stale, tag, false));
    /* B's unconfirmed boot, before its gate: A's record is ABANDONED (still
     * ended for A), so no later valid boot of the image is an exception boot. */
    sx_record_t r = stale;
    EXPECT(sx_record_retire_other(&r, tag, b));
    EXPECT(sx_record_valid(&r) && r.state == SX_STATE_ABANDONED && r.deployment_crc == a);
    EXPECT(sx_record_ended(&r, a) && !sx_record_ended(&r, b));
    EXPECT(!sx_exception_active(&r, tag, false) && !sx_record_confirmed(&r, tag));
    EXPECT(!sx_record_retire_other(&r, tag, b));
    /* Every confirmed state of another deployment is retired; B's own record,
     * another image's, an unconfirmed state or a torn record is not. */
    for (int state = SX_STATE_NONE; state < SX_STATE_COUNT; ++state) {
        r = stale;
        r.state = (uint8_t)state;
        sx_record_seal(&r);
        bool confirmed = sx_record_confirmed(&r, tag);
        EXPECT(sx_record_retire_other(&r, tag, b) == confirmed);
        EXPECT(!sx_exception_active(&r, tag, false));
    }
    r = stale;
    EXPECT(!sx_record_retire_other(&r, tag, a) && !sx_record_retire_other(&r, tag + 1, b));
    EXPECT(r.state == SX_STATE_CONFIRMING);
    r.crc ^= 1U;
    EXPECT(!sx_record_retire_other(&r, tag, b));
    /* An ordinary confirmation clears any record of this image, not only the
     * CONFIRMING its own boot wrote: after it the image is not an exception
     * image, the round's restarts never start, and B cannot end FAILED
     * STORAGE_REPAIR_EXCEPTION_UNREPAIRED. */
    EXPECT(sx_record_clear_on_ordinary(&stale, tag) && !sx_record_clear_on_ordinary(&stale, tag + 1));
    sx_record_t cleared = {.state = SX_STATE_NONE, .image_tag = tag, .deployment_crc = b};
    sx_record_seal(&cleared);
    EXPECT(!sx_record_clear_on_ordinary(&cleared, tag));
    EXPECT(!sx_exception_active(&cleared, tag, false));
    EXPECT(!sx_record_after_boot(&cleared, tag, false, false, SX_ROUND_DEFERRED));
    uint32_t wait = 0;
    EXPECT(sx_next_step(&cleared, true, 1900000000U, 7200, &wait) == SX_NEXT_NONE);
    for (int state = SX_STATE_EVALUATING; state < SX_STATE_COUNT; ++state) {
        r = stale;
        r.state = (uint8_t)state;
        sx_record_seal(&r);
        EXPECT(sx_record_clear_on_ordinary(&r, tag));
    }
    sx_record_t erased;
    memset(&erased, 0xFF, sizeof(erased));
    EXPECT(!sx_record_clear_on_ordinary(&erased, tag) && !sx_record_clear_on_ordinary(NULL, tag));
    return 0;
}

static int floor_versions(void)
{
    EXPECT(sx_version_redo_aware("2.7.6") && sx_version_redo_aware("2.7.7") && sx_version_redo_aware("2.7.10"));
    EXPECT(sx_version_redo_aware("zone-lite-2.7.7") && sx_version_redo_aware("2.8.0") && sx_version_redo_aware("3.0.0"));
    EXPECT(!sx_version_redo_aware("2.7.5") && !sx_version_redo_aware("2.5.2") && !sx_version_redo_aware("2.6.27"));
    EXPECT(!sx_version_redo_aware("2.7.0") && !sx_version_redo_aware("2.4.12") && !sx_version_redo_aware("1.9.9"));
    EXPECT(!sx_version_redo_aware("") && !sx_version_redo_aware(NULL) && !sx_version_redo_aware("2.7"));
    EXPECT(!sx_version_redo_aware("2.7.7-rc1") && !sx_version_redo_aware("v2.7.7") && !sx_version_redo_aware("2.7.x"));
    /* Every room-only errno pair, and nothing else (H2). */
    EXPECT(sx_probe_error_room("persistence_open_write", ENOENT) && sx_probe_error_room("persistence_open_write", ENOSPC));
    EXPECT(sx_probe_error_room("persistence_write", ENOSPC) && !sx_probe_error_room("persistence_write", ENOENT));
    EXPECT(!sx_probe_error_room("persistence_open_read", ENOENT) && !sx_probe_error_room("persistence_read", EIO));
    EXPECT(!sx_probe_error_room(NULL, ENOSPC) && !sx_probe_error_room("persistence_unlink", ENOSPC));
    EXPECT(sx_last_error_room("local_write_commit", ENOSPC) && !sx_last_error_room("local_write_commit", EIO));
    EXPECT(!sx_last_error_room("segment_read", ENOSPC) && !sx_last_error_room("legacy_read", ENOSPC));
    return 0;
}

int main(void)
{
    if (gate_terms() || window() || record() || outcomes() || restarts() || cuts() || stale_record() || floor_versions())
        return 1;
    printf("storage exception host tests passed (%u checks)\n", checks);
    return 0;
}
