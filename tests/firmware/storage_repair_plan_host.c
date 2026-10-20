/* ZKT 2.7.5 storage repair: the decisions that need no filesystem, and the
 * metadata mirror, with plain cc; 2.7.6: the redo records, the block plan,
 * the per-block writer and the boot recovery of the last-resort reformat.
 * The SPIFFS engine itself runs in spiffs_repair_host.c against ESP-IDF's
 * own SPIFFS. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "durable_queue.h"
#include "legacy_queue.h"
#include "reliability.h"
#include "storage_repair_mirror.h"
#include "storage_repair_plan.h"
#include "storage_repair_redo.h"

static void classes(void)
{
    srp_kind_t k = srp_classify("/pending.jsonl");
    assert(k.cls == SRP_CLASS_ATTENDANCE && k.fill == SRP_FILL_JSON_ROWS && (k.flags & SRP_APPEND_TARGET));
    assert(!strcmp(k.lq_namespace, "legacy_queues") && !strcmp(k.lq_key, "ords_pending"));
    k = srp_classify("/blocked_identity.jsonl");
    assert(k.cls == SRP_CLASS_ATTENDANCE && !strcmp(k.lq_key, "blocked"));
    k = srp_classify("/add_live.jsonl");
    assert(k.cls == SRP_CLASS_ATTENDANCE && !strcmp(k.lq_namespace, "add_legacy") && !strcmp(k.lq_key, "live"));
    assert(!strcmp(srp_classify("/add_pending.jsonl").lq_key, "reconcile"));
    assert(srp_classify("/pending.bak").cls == SRP_CLASS_ATTENDANCE && !srp_classify("/pending.bak").lq_key);
    assert(srp_classify("/blocked_recovery.tmp").cls == SRP_CLASS_ATTENDANCE);
    k = srp_classify("/corrupt_ords.jsonl");
    assert(k.cls == SRP_CLASS_EVIDENCE && !strcmp(k.lq_key, "old_qo"));
    assert(!strcmp(srp_classify("/add_corrupt.bak").lq_key, "old_qb"));
    k = srp_classify("/add_commands.jsonl");
    assert(k.cls == SRP_CLASS_OPERATIONAL && (k.flags & SRP_COMMAND_FAMILY) && k.fill == SRP_FILL_BINARY);
    assert(srp_classify("/add_commands.spare").flags & SRP_COMMAND_FAMILY);
    k = srp_classify("/processed_commands.txt");
    assert(k.cls == SRP_CLASS_OPERATIONAL && k.fill == SRP_FILL_ID_LINES && (k.flags & SRP_APPEND_TARGET));
    assert(srp_classify("/add_live.pos").fill == SRP_FILL_CURSOR);
    k = srp_classify("/acked_uids.txt");
    assert(k.cls == SRP_CLASS_DEDUPE && k.fill == SRP_FILL_ID_LINES);
    k = srp_classify("/add_identities.commit");
    assert(k.cls == SRP_CLASS_CATALOG && (k.flags & SRP_CATALOG_FAMILY));
    assert(srp_classify("/persistence-probe").cls == SRP_CLASS_EXPENDABLE);
    assert(srp_classify("/persistence-probe-2").cls == SRP_CLASS_EXPENDABLE);
    assert(srp_classify("/add_pending.pos.tmp").cls == SRP_CLASS_EXPENDABLE);
    /* Segments by pattern: queue_store.c and zkt_journal_store.c names. */
    k = srp_classify("/qo0000002a.q");
    assert(k.cls == SRP_CLASS_ATTENDANCE && (k.flags & SRP_SEGMENT) && k.fill == SRP_FILL_BINARY);
    assert(srp_classify("/qh00000000.q").cls == SRP_CLASS_ATTENDANCE);
    assert(srp_classify("/qz0000002a.q").cls == SRP_CLASS_UNKNOWN);
    assert(srp_classify("/qo0000002A.q").cls == SRP_CLASS_UNKNOWN);
    k = srp_classify("/zktj0123456789abcdef.j");
    assert(k.cls == SRP_CLASS_EVIDENCE && (k.flags & SRP_SEGMENT));
    assert(srp_classify("/zktj0123456789abcde.j").cls == SRP_CLASS_UNKNOWN);
    assert(srp_classify("/something.else").cls == SRP_CLASS_UNKNOWN && srp_classify(NULL).cls == SRP_CLASS_UNKNOWN);
    /* The repair's own rebuild under a temporary name. */
    assert(srp_classify("/pending.jsonl" SRP_RESTORE_SUFFIX).cls == SRP_CLASS_EXPENDABLE);
    assert(srp_classify("/qo0000002a.q" SRP_RESTORE_SUFFIX).cls == SRP_CLASS_EXPENDABLE);
    assert(srp_classify(SRP_RESTORE_SUFFIX).cls == SRP_CLASS_UNKNOWN);
    /* Every custody name leaves room for the suffix in SPIFFS's 31 bytes. */
    assert(strlen("/blocked_identity.jsonl" SRP_RESTORE_SUFFIX) < 32 &&
           strlen("/zktj0123456789abcdef.j" SRP_RESTORE_SUFFIX) < 32);
    /* Attendance before evidence before operational before dedupe. */
    assert(srp_class_priority(SRP_CLASS_ATTENDANCE) < srp_class_priority(SRP_CLASS_EVIDENCE));
    assert(srp_class_priority(SRP_CLASS_EVIDENCE) < srp_class_priority(SRP_CLASS_OPERATIONAL));
    assert(srp_class_priority(SRP_CLASS_OPERATIONAL) < srp_class_priority(SRP_CLASS_DEDUPE));
    assert(srp_class_priority(SRP_CLASS_DEDUPE) < srp_class_priority(SRP_CLASS_CATALOG));
}

static void fills(void)
{
    uint8_t out[16];
    static const char row[] = "{\"event_uid\":\"a\",\"seq\":1}";
    static const char next[] = "{\"seq\":2}\n";
    /* A row whose newline was lost keeps its own line; so does the next row. */
    srp_fill(SRP_FILL_JSON_ROWS, (const uint8_t *)row, strlen(row), (const uint8_t *)next, strlen(next), true, out, 8);
    assert(out[0] == '\n' && out[7] == '\n');
    for (int i = 1; i < 7; ++i) assert(out[i] == 0);
    /* A fragment on either side: one damaged row of NUL. */
    srp_fill(SRP_FILL_JSON_ROWS, (const uint8_t *)row, 10, (const uint8_t *)"seq\":2}\n", 8, true, out, 8);
    for (int i = 0; i < 8; ++i) assert(out[i] == 0);
    /* The next row is complete only when its newline was read. */
    srp_fill(SRP_FILL_JSON_ROWS, NULL, 0, (const uint8_t *)next, strlen(next) - 1, false, out, 8);
    for (int i = 0; i < 8; ++i) assert(out[i] == 0);
    srp_fill(SRP_FILL_JSON_ROWS, NULL, 0, (const uint8_t *)next, strlen(next), true, out, 8);
    assert(out[0] == 0 && out[7] == '\n');
    /* One byte between two complete rows. */
    srp_fill(SRP_FILL_JSON_ROWS, (const uint8_t *)row, strlen(row), (const uint8_t *)next, strlen(next), true, out, 1);
    assert(out[0] == '\n');
    /* A NUL inside the readable prefix is never a complete row. */
    uint8_t with_nul[sizeof(row)];
    memcpy(with_nul, row, sizeof(row));
    with_nul[3] = 0;
    srp_fill(SRP_FILL_JSON_ROWS, with_nul, strlen(row), NULL, 0, false, out, 4);
    assert(out[0] == 0);
    srp_fill(SRP_FILL_ID_LINES, NULL, 0, NULL, 0, false, out, 5);
    assert(!memcmp(out, "\n\n\n\n\n", 5));
    srp_fill(SRP_FILL_CURSOR, NULL, 0, NULL, 0, false, out, 4);
    assert(!memcmp(out, "000\n", 4));
    memset(out, 0xAA, sizeof(out));
    srp_fill(SRP_FILL_BINARY, NULL, 0, NULL, 0, false, out, 6);
    for (int i = 0; i < 6; ++i) assert(out[i] == 0);
    assert(out[6] == 0xAA);
}

typedef struct {
    const uint8_t *data;
    uint32_t size, fail_at;
} bytes_t;
static bool read_bytes(void *context, uint32_t offset, uint8_t *buffer, size_t length)
{
    bytes_t *b = context;
    if (offset + length > b->size) return false;
    if (b->fail_at != UINT32_MAX && b->fail_at >= offset && b->fail_at < offset + length) return false;
    memcpy(buffer, b->data + offset, length);
    return true;
}

static void cursors(void)
{
    static const char file[] = "{\"a\":1}\n{\"b\":2}\n{\"c\":3}\n";
    uint32_t size = (uint32_t)strlen(file);
    bytes_t source = {(const uint8_t *)file, size, UINT32_MAX};
    srp_bytes_t bytes = {&source, read_bytes};
    /* The CRC is the queue's own: a chained CRC equals dq_crc32. */
    assert(srp_crc32(srp_crc32(0, file, 5), file + 5, 11) == dq_crc32(file, 16));
    lq_checkpoint_t cp = {.version = 1, .generation = 3, .offset = 8, .prefix_crc = srp_crc32(0, file, 8)};
    lq_checkpoint_seal(&cp);
    assert(srp_cursor_verdict(0, &cp, true, size, false, &bytes) == SRP_CURSOR_KEEP);
    assert(srp_cursor_verdict(1, &cp, true, size, false, &bytes) == SRP_CURSOR_KEEP);
    /* An older image rewrote the file: the settled prefix differs. */
    lq_checkpoint_t stale = cp;
    stale.prefix_crc ^= 1;
    lq_checkpoint_seal(&stale);
    assert(srp_cursor_verdict(1, &stale, true, size, false, &bytes) == SRP_CURSOR_PREFIX_MISMATCH);
    lq_checkpoint_t middle = cp;
    middle.offset = 5;
    middle.prefix_crc = srp_crc32(0, file, 5);
    lq_checkpoint_seal(&middle);
    assert(srp_cursor_verdict(1, &middle, true, size, false, &bytes) == SRP_CURSOR_NOT_ROW_BOUNDARY);
    middle.version = 2; /* inside an evidence row: no boundary rule */
    lq_checkpoint_seal(&middle);
    assert(srp_cursor_verdict(1, &middle, true, size, false, &bytes) == SRP_CURSOR_KEEP);
    lq_checkpoint_t beyond = cp;
    beyond.offset = size + 1;
    lq_checkpoint_seal(&beyond);
    assert(srp_cursor_verdict(1, &beyond, true, size, false, &bytes) == SRP_CURSOR_BEYOND_FILE);
    assert(srp_cursor_verdict(1, &cp, false, 0, false, NULL) == SRP_CURSOR_BEYOND_FILE);
    lq_checkpoint_t zero = {.version = 1, .generation = 9};
    lq_checkpoint_seal(&zero);
    assert(srp_cursor_verdict(1, &zero, false, 0, false, NULL) == SRP_CURSOR_KEEP);
    lq_checkpoint_t corrupt = cp;
    corrupt.crc ^= 1;
    assert(srp_cursor_verdict(1, &corrupt, true, size, false, &bytes) == SRP_CURSOR_CORRUPT);
    assert(srp_cursor_verdict(1, &cp, true, size, true, &bytes) == SRP_CURSOR_PREFIX_GAP);
    source.fail_at = 2;
    assert(srp_cursor_verdict(1, &cp, true, size, false, &bytes) == SRP_CURSOR_PREFIX_GAP);
    source.fail_at = 20; /* past the cursor: the prefix still verifies */
    assert(srp_cursor_verdict(1, &cp, true, size, false, &bytes) == SRP_CURSOR_KEEP);
}

static srp_gap_t gap_named(const char *name, uint32_t offset, uint32_t length, uint32_t generation)
{
    srp_gap_t gap;
    memset(&gap, 0, sizeof(gap));
    snprintf(gap.name, sizeof(gap.name), "%s", name);
    gap.offset = offset;
    gap.length = length;
    gap.generation = generation;
    gap.cause = SRP_GAP_SALVAGE;
    gap.cls = SRP_CLASS_ATTENDANCE;
    gap.spiffs_error = -10004;
    gap.file_size = 3826949;
    return gap;
}

static void records(void)
{
    srp_gap_t gap = gap_named("/blocked_identity.jsonl", 50200, 251, 7);
    char id[128], raw[512];
    assert(srp_gap_record_id(&gap, id, sizeof(id)));
    assert(!strcmp(id, "gap:blocked_identity_jsonl:50200:251:7"));
    for (const char *p = id; *p; ++p)
        assert((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || strchr("_:-", *p));
    size_t n = srp_gap_evidence(&gap, raw, sizeof(raw));
    static const char expect[] =
        "{\"type\":\"storage_repair_unreadable\",\"schema\":1,\"file\":\"/blocked_identity.jsonl\",\"offset\":50200,"
        "\"length\":251,\"file_size\":3826949,\"spiffs_error\":-10004,\"cause\":\"SALVAGE\",\"class\":\"attendance\","
        "\"merged\":false,\"run\":7}";
    assert(n == strlen(expect) && !strcmp(raw, expect));
    assert(rel_json_syntax_valid(raw, n));
    /* Byte-identical on every resend: rendered only from the frozen record. */
    char again[512];
    assert(srp_gap_evidence(&gap, again, sizeof(again)) == n && !memcmp(raw, again, n));
    assert(srp_gap_evidence(&gap, raw, 40) == 0 && !srp_gap_record_id(&gap, id, 10));
    srp_gap_t bad = gap;
    bad.name[3] = '"';
    assert(srp_gap_evidence(&bad, raw, sizeof(raw)) == 0);

    srp_reset_t reset;
    memset(&reset, 0, sizeof(reset));
    snprintf(reset.name, sizeof(reset.name), "/pending.jsonl");
    reset.verdict = SRP_CURSOR_PREFIX_MISMATCH;
    reset.generation = 7;
    reset.previous = (lq_checkpoint_t){.version = 1, .generation = 12, .offset = 50055, .prefix_crc = 5, .crc = 6};
    assert(srp_reset_record_id(&reset, id, sizeof(id)) && !strcmp(id, "cursor:pending_jsonl:12:7"));
    n = srp_reset_evidence(&reset, raw, sizeof(raw));
    assert(n && rel_json_syntax_valid(raw, n));
    assert(strstr(raw, "\"verdict\":\"PREFIX_MISMATCH\"") && strstr(raw, "\"offset\":50055"));

    /* Bounded: an unacknowledged record is never dropped. */
    srp_gaps_t gaps;
    memset(&gaps, 0, sizeof(gaps));
    for (uint32_t i = 0; i < SRP_MAX_GAPS; ++i) {
        srp_gap_t g = gap_named(i % 2 ? "/pending.jsonl" : "/blocked_identity.jsonl", i * 1000, 251, 7);
        assert(srp_gaps_add(&gaps, &g));
    }
    assert(gaps.count == SRP_MAX_GAPS && srp_gaps_unacked(&gaps) == SRP_MAX_GAPS);
    /* Full: a new range widens that file's last record of the same run. */
    srp_gap_t extra = gap_named("/pending.jsonl", 90000, 251, 7);
    assert(srp_gaps_add(&gaps, &extra));
    const srp_gap_t *widened = &gaps.gap[SRP_MAX_GAPS - 1];
    assert(!strcmp(widened->name, "/pending.jsonl") && (widened->flags & SRP_GAP_MERGED));
    assert(widened->offset == (SRP_MAX_GAPS - 1) * 1000 && widened->offset + widened->length == 90251);
    /* Another run and no acknowledged slot: refused, nothing overwritten. */
    srp_gap_t later = gap_named("/pending.jsonl", 1, 1, 8);
    assert(!srp_gaps_add(&gaps, &later));
    gaps.gap[4].flags |= SRP_RECORD_ACKED;
    assert(srp_gaps_add(&gaps, &later) && gaps.gap[4].generation == 8);
    srp_resets_t resets;
    memset(&resets, 0, sizeof(resets));
    for (uint32_t i = 0; i < SRP_MAX_RESETS; ++i) assert(srp_resets_add(&resets, &reset));
    assert(!srp_resets_add(&resets, &reset) && srp_resets_unacked(&resets) == SRP_MAX_RESETS);
    resets.reset[2].flags |= SRP_RECORD_ACKED;
    assert(srp_resets_add(&resets, &reset) && srp_resets_unacked(&resets) == SRP_MAX_RESETS);
}

static void journal(void)
{
    srp_state_t state;
    memset(&state, 0, sizeof(state));
    state.generation = 4;
    state.phase = SRP_PHASE_CHECK;
    assert(!srp_state_valid(&state));
    srp_state_seal(&state);
    assert(srp_state_valid(&state));
    state.incomplete++;
    assert(!srp_state_valid(&state));
    srp_state_seal(&state);
    state.phase = 99;
    srp_state_seal(&state);
    assert(!srp_state_valid(&state));
    srp_manifest_t manifest;
    memset(&manifest, 0, sizeof(manifest));
    manifest.count = 1;
    snprintf(manifest.file[0].name, sizeof(manifest.file[0].name), "/pending.jsonl");
    srp_manifest_seal(&manifest);
    assert(srp_manifest_valid(&manifest));
    manifest.file[0].size = 1;
    assert(!srp_manifest_valid(&manifest));
    manifest.count = SRP_MANIFEST_FILES + 1;
    srp_manifest_seal(&manifest);
    assert(!srp_manifest_valid(&manifest));
    /* The image and boot it belongs to, and per-file settlement, are sealed. */
    manifest.count = 1;
    manifest.flags = SRP_MANIFEST_PENDING_IMAGE;
    manifest.file[0].flags = SRP_MANIFEST_CURSOR | SRP_MANIFEST_SETTLED;
    srp_manifest_seal(&manifest);
    assert(srp_manifest_valid(&manifest));
    manifest.image_tag ^= 1;
    assert(!srp_manifest_valid(&manifest));
    /* The journal's NVS peak beside NVS's own page (available_entries leaves
     * it out): the state, the manifest, the resets, the gap list, a rewrite
     * of the largest of them, and the runtime's reserve. */
    uint32_t first = srp_nvs_entries_needed(false, false, false, false);
    uint32_t later = srp_nvs_entries_needed(true, true, true, true);
    uint32_t largest = (uint32_t)((sizeof(srp_gaps_t) > sizeof(srp_manifest_t) ? sizeof(srp_gaps_t)
                                                                                : sizeof(srp_manifest_t)) / 32U);
    assert(first > SRP_NVS_RUNTIME_RESERVE + largest + (sizeof(srp_gaps_t) + sizeof(srp_manifest_t)) / 32U);
    assert(first < 300U);
    assert(later > SRP_NVS_RUNTIME_RESERVE + largest && later < first);
    assert(srp_nvs_entries_needed(true, true, true, false) > later);
    assert(!strcmp(srp_phase_name(SRP_PHASE_NORMALIZE), "NORMALIZE"));
}

static void policy(void)
{
    srp_policy_input_t in = {0};
    srp_policy_t out = srp_policy(&in);
    assert(out.mode == SRP_MODE_FULL && !out.disable && !out.clear_disable);
    /* Power cuts do not count against the guard; crashes and the deadline do. */
    in.incomplete = 5;
    assert(srp_policy(&in).mode == SRP_MODE_FULL);
    in.failures = SRP_GUARD_FAILURES - 1;
    assert(srp_policy(&in).mode == SRP_MODE_FULL);
    in.failures = SRP_GUARD_FAILURES;
    out = srp_policy(&in);
    assert(out.mode == SRP_MODE_REPORT_ONLY && out.disable);
    /* Off until another image runs, which tries again. */
    in.disabled = true;
    in.same_image = true;
    out = srp_policy(&in);
    assert(out.mode == SRP_MODE_REPORT_ONLY && !out.disable && !out.clear_disable);
    in.same_image = false;
    out = srp_policy(&in);
    assert(out.mode == SRP_MODE_FULL && out.clear_disable);
    /* An unconfirmed image after an incomplete run normalizes only. */
    in = (srp_policy_input_t){.incomplete = 1, .pending_verify = true};
    assert(srp_policy(&in).mode == SRP_MODE_NORMALIZE_ONLY);
    in.incomplete = 0;
    assert(srp_policy(&in).mode == SRP_MODE_FULL);
    assert(srp_policy(NULL).mode == SRP_MODE_REPORT_ONLY);
}

static void budget(void)
{
    srp_budget_file_t files[] = {
        {.size = 3826949, .cls = SRP_CLASS_ATTENDANCE},
        {.size = 733720, .cls = SRP_CLASS_DEDUPE},
        {.size = 148, .cls = SRP_CLASS_OPERATIONAL},
        {.size = 100000, .cls = SRP_CLASS_ATTENDANCE},
        {.size = 5000, .cls = SRP_CLASS_UNKNOWN},
    };
    uint64_t held = srp_budget(files, 5, 4000000);
    /* Custody first, whole files only: the acked log does not fit. */
    assert(files[0].hold && files[3].hold && files[2].hold && !files[1].hold && files[4].hold);
    assert(held == 3826949 + 100000 + 148 + 5000);
    held = srp_budget(files, 5, 200000);
    assert(!files[0].hold && files[3].hold && files[2].hold);
    held = srp_budget(files, 5, 0);
    assert(!held && !files[2].hold);
}

/* ---- the mirror ----------------------------------------------------- */
static uint8_t flash[64 * 1024];
static int fail_next_write, fail_next_erase;
static bool f_read(void *c, uint32_t a, void *b, uint32_t n) { (void)c; memcpy(b, flash + a, n); return true; }
static bool f_write(void *c, uint32_t a, const void *b, uint32_t n)
{
    (void)c;
    const uint8_t *s = b;
    if (fail_next_write) {
        /* A failed program that still cleared some bits. */
        fail_next_write = 0;
        flash[a] &= s[0];
        return false;
    }
    for (uint32_t i = 0; i < n; ++i) flash[a + i] &= s[i];
    return true;
}
static bool f_erase(void *c, uint32_t a, uint32_t n)
{
    (void)c;
    if (fail_next_erase) {
        fail_next_erase = 0;
        memset(flash + a, 0xFF, n / 2);
        return false;
    }
    memset(flash + a, 0xFF, n);
    return true;
}

static void mirror(void)
{
    for (size_t i = 0; i < sizeof(flash); ++i) flash[i] = (uint8_t)(i * 7 + 3);
    const srm_flash_t port = {NULL, f_read, f_write, f_erase};
    srm_mirror_t m;
    static uint8_t lookup[16 * 256], headers[256 * 5], scratch[4096];
    assert(srm_lookup_bytes(sizeof(flash), 4096, 256) == sizeof(lookup));
    assert(srm_header_bytes(sizeof(flash), 256) == sizeof(headers));
    assert(!srm_init(&m, &port, sizeof(flash), 4096, 256, lookup, headers, scratch, 100));
    assert(srm_init(&m, &port, sizeof(flash), 4096, 256, lookup, headers, scratch, sizeof(scratch)));
    uint8_t got[300], expect[300];
    /* Lookup pages and headers come from RAM; anything else from flash. */
    uint32_t hits = m.hits;
    assert(srm_read(&m, 4096 + 10, got, 20) && !memcmp(got, flash + 4096 + 10, 20) && m.hits == hits + 1);
    assert(srm_read(&m, 4096 + 256 * 3, got, 5) && !memcmp(got, flash + 4096 + 768, 5) && m.hits == hits + 2);
    assert(srm_read(&m, 4096 + 256 * 3 + 2, got, 3) && m.hits == hits + 3);
    assert(srm_read(&m, 4096 + 256 * 3, got, 6) && !memcmp(got, flash + 4096 + 768, 6) && m.hits == hits + 3);
    assert(srm_read(&m, 4096 + 250, got, 12) && !memcmp(got, flash + 4096 + 250, 12) && m.hits == hits + 3);
    /* A program ANDs into the copy, as into NOR. */
    uint8_t value[8] = {0x0F, 0xF0, 0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80};
    assert(srm_write(&m, 8192 + 256 * 2, value, 8));
    assert(srm_read(&m, 8192 + 256 * 2, got, 5));
    memcpy(expect, flash + 8192 + 512, 5);
    assert(!memcmp(got, expect, 5));
    assert(srm_write(&m, 8192 + 4, value, 8));
    assert(srm_read(&m, 8192, got, 256) && !memcmp(got, flash + 8192, 256));
    /* A failed program may still have cleared bits: the copy is read back. */
    fail_next_write = 1;
    uint8_t zero = 0;
    assert(!srm_write(&m, 12288 + 256 * 5, &zero, 1));
    assert(srm_read(&m, 12288 + 256 * 5, got, 5) && !memcmp(got, flash + 12288 + 1280, 5));
    /* An erase sets the copy to 0xFF; a failed erase is read back. */
    assert(srm_erase(&m, 16384, 4096));
    assert(srm_read(&m, 16384, got, 256));
    for (int i = 0; i < 256; ++i) assert(got[i] == 0xFF);
    for (size_t i = 20480; i < 24576; ++i) flash[i] = 0;
    fail_next_erase = 1;
    assert(!srm_erase(&m, 20480, 4096));
    assert(srm_read(&m, 20480, got, 256) && !memcmp(got, flash + 20480, 256));
    assert(srm_read(&m, 20480 + 256 * 10, got, 5) && !memcmp(got, flash + 20480 + 2560, 5));
    assert(m.refreshes > 0);
    /* 2.7.6: a program that reported success but did not stick is read again
     * from flash once the caller verified the block on raw flash. */
    assert(srm_write(&m, 24576 + 252, &zero, 1));
    flash[24576 + 252] = 0x5A;
    assert(srm_read(&m, 24576 + 252, got, 1) && got[0] == 0);
    assert(srm_refresh(&m, 24576, 4096));
    assert(srm_read(&m, 24576 + 252, got, 1) && got[0] == 0x5A);
}


/* ---- 2.7.6: the last-resort reformat ----------------------------------- */
#define RX_BLOCKS 64U
static uint8_t rx_store[RX_BLOCKS * 4096], rx_area[8 * 4096];
static int rx_fail_store_write, rx_corrupt_after; /* fail the n-th store write; corrupt a byte n writes on */
static unsigned rx_store_writes, rx_windows_open;
static bool rx_read(void *c, uint32_t o, void *b, uint32_t n, uint8_t *mem, size_t size)
{
    (void)c;
    if ((uint64_t)o + n > size) return false;
    memcpy(b, mem + o, n);
    return true;
}
static bool rx_area_read(void *c, uint32_t o, void *b, uint32_t n) { return rx_read(c, o, b, n, rx_area, sizeof(rx_area)); }
static bool rx_store_read(void *c, uint32_t o, void *b, uint32_t n) { return rx_read(c, o, b, n, rx_store, sizeof(rx_store)); }
static bool rx_program(uint8_t *mem, size_t size, uint32_t o, const void *b, uint32_t n)
{
    if ((uint64_t)o + n > size) return false;
    const uint8_t *src = b;
    for (uint32_t i = 0; i < n; ++i) mem[o + i] &= src[i];
    return true;
}
static bool rx_area_write(void *c, uint32_t o, const void *b, uint32_t n)
{
    (void)c;
    return rx_program(rx_area, sizeof(rx_area), o, b, n);
}
static bool rx_store_write(void *c, uint32_t o, const void *b, uint32_t n)
{
    (void)c;
    if (rx_fail_store_write && ++rx_store_writes == (unsigned)rx_fail_store_write) return false;
    bool ok = rx_program(rx_store, sizeof(rx_store), o, b, n);
    /* A sector that does not hold what it is programmed with. */
    if (ok && rx_corrupt_after && n == 256) rx_store[o + 17] &= 0x0F;
    return ok;
}
static bool rx_erase(uint8_t *mem, size_t size, uint32_t o, uint32_t n)
{
    if ((uint64_t)o + n > size) return false;
    memset(mem + o, 0xFF, n);
    return true;
}
static bool rx_area_erase(void *c, uint32_t o, uint32_t n)
{
    (void)c;
    return rx_erase(rx_area, sizeof(rx_area), o, n);
}
static bool rx_store_erase(void *c, uint32_t o, uint32_t n)
{
    (void)c;
    return rx_erase(rx_store, sizeof(rx_store), o, n);
}
static void rx_window(void *c, bool open)
{
    (void)c;
    if (open) rx_windows_open++;
}
static const srx_io_t RX_IO = {
    .area_size = sizeof(rx_area), .area_read = rx_area_read, .area_write = rx_area_write, .area_erase = rx_area_erase,
    .store_read = rx_store_read, .store_write = rx_store_write, .store_erase = rx_store_erase, .window = rx_window,
};

static void put16_at(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

/* A block of the test store: entries per `kinds` ('d' deleted unread, 'u'
 * used by object 5, 'f' erased, 'r' deleted but referenced), magic valid. */
static void rx_block(uint32_t block, const char *kinds)
{
    uint8_t *b = rx_store + block * 4096;
    memset(b, 0xFF, 4096);
    for (uint32_t e = 0; e < 15; ++e) {
        uint8_t *page = b + (1 + e) * 256;
        switch (kinds[e]) {
        case 'u':
            put16_at(b + e * 2, 5);
            put16_at(page, 5);
            put16_at(page + 2, (uint16_t)e);
            page[4] = 0xFC; /* finalized data page */
            memset(page + 5, 'a' + (int)e, 251);
            break;
        case 'd': case 'r':
            put16_at(b + e * 2, 0);
            put16_at(page, 5);
            put16_at(page + 2, (uint16_t)(100 + e));
            page[4] = 0x7C; /* deleted */
            memset(page + 5, 'z', 251);
            break;
        default: break;
        }
    }
    put16_at(b + 254, 7);
    put16_at(b + 252, srx_spiffs_magic(RX_BLOCKS, block));
}

static void rx_entries(uint32_t block, const char *kinds, srx_entry_t *entry)
{
    const uint8_t *b = rx_store + block * 4096;
    for (uint32_t e = 0; e < 15; ++e) {
        const uint8_t *page = b + (1 + e) * 256;
        entry[e] = (srx_entry_t){.lu = srx_get16(b + e * 2), .obj = srx_get16(page), .flags = page[4],
                                 .erased = srx_erased(page, 256), .referenced = kinds[e] == 'r'};
    }
}

static void redo_plan(void)
{
    /* The low magics D2 found: block 983 of 2,048 is 0x0000. */
    assert(srx_spiffs_magic(2048, 983) == 0 && !srx_magic_strong(2048, 983));
    /* Round 1 review, D3: block 810's magic is 0x00FF. The window clears it
     * to 0x0000 and an erase cut short leaves each byte erased or not (the
     * harness's NOR model), so it can read valid over a half-erased block. */
    assert(srx_spiffs_magic(2048, 810) == 0x00FF && !srx_magic_strong(2048, 810));
    unsigned weak = 0;
    for (uint32_t b = 0; b < 2048; ++b) weak += !srx_magic_strong(2048, b);
    assert(weak == 68);
    /* Every store size the firmware or the harness uses: no strong magic can
     * read valid after a byte-wise torn erase of its cleared value. */
    for (uint32_t blocks = 64; blocks <= 4096; blocks *= 2)
        for (uint32_t b = 0; b < blocks; ++b) {
            uint16_t magic = srx_spiffs_magic(blocks, b);
            for (unsigned tear = 0; tear < 4; ++tear) {
                uint16_t reads = (uint16_t)(((tear & 1U) ? 0x00FFU : 0U) | ((tear & 2U) ? 0xFF00U : 0U));
                assert(reads != magic || !srx_magic_strong(blocks, b));
            }
        }
    srx_entry_t e[15];
    /* Interior and tail drops; the referenced deleted page stays. */
    rx_block(3, "uudurddfffffff" "f");
    rx_entries(3, "uudurddfffffff" "f", e);
    srx_plan_t p = srx_plan_block(e, true, false);
    assert(p.reason == SRX_PLAN_OK && p.dropped == ((1U << 2) | (1U << 5) | (1U << 6)) && p.gain == 3 && p.tail == 2);
    assert(p.kept == ((1U << 0) | (1U << 1) | (1U << 3) | (1U << 4)));
    /* Entry 0 stays while a later entry stays (SPIFFS counts free blocks by it). */
    rx_block(4, "dduddfffffffff" "f");
    rx_entries(4, "dduddfffffffff" "f", e);
    p = srx_plan_block(e, true, false);
    assert(p.reason == SRX_PLAN_OK && (p.kept & 1U) && !(p.dropped & 1U) && p.dropped == ((1U << 1) | (1U << 3) | (1U << 4)));
    /* All deleted: the whole block. */
    rx_block(5, "ddddddddddddddd");
    rx_entries(5, "ddddddddddddddd", e);
    p = srx_plan_block(e, true, true);
    assert(p.reason == SRX_PLAN_OK && p.dropped == 0x7FFF && !p.kept && p.tail == 15);
    /* Nothing to drop, a weak magic, an index page, custody on an unconfirmed image. */
    rx_block(6, "uuuufffffffffff");
    rx_entries(6, "uuuufffffffffff", e);
    assert(srx_plan_block(e, true, false).reason == SRX_PLAN_NO_GAIN);
    rx_entries(3, "uudurddfffffff" "f", e);
    assert(srx_plan_block(e, false, false).reason == SRX_PLAN_MAGIC);
    e[1].index = true;
    assert(srx_plan_block(e, true, false).reason == SRX_PLAN_INDEX);
    e[1].index = false;
    e[0].keep = SRX_KEEP_EXCLUDED;
    assert(srx_plan_block(e, true, false).reason == SRX_PLAN_EXCLUDED);
    e[0].keep = SRX_KEEP_CUSTODY;
    p = srx_plan_block(e, true, false);
    assert(p.reason == SRX_PLAN_OK && p.custody);
    assert(srx_plan_block(e, true, true).reason == SRX_PLAN_CUSTODY);
    /* A free lookup entry over a page that is not erased, or one naming
     * another object: never a candidate. */
    rx_entries(3, "uudurddfffffff" "f", e);
    e[9].erased = false;
    assert(srx_plan_block(e, true, false).reason == SRX_PLAN_MISLABELED);
    rx_entries(3, "uudurddfffffff" "f", e);
    e[0].lu = 6;
    assert(srx_plan_block(e, true, false).reason == SRX_PLAN_MISLABELED);
}

static void rx_record(uint32_t slot, uint32_t block, uint16_t drop, uint32_t sequence, uint8_t flags, uint32_t tag,
                      uint8_t *target)
{
    static uint8_t sector[4096];
    srx_record_t h;
    memset(&h, 0, sizeof(h));
    h.reclaim_id = 9;
    h.sequence = sequence;
    h.image_tag = tag;
    h.flags = flags;
    h.block = (uint16_t)block;
    h.drop_mask = drop;
    h.block_count = RX_BLOCKS;
    h.erase_count = 12;
    for (uint32_t e = 0; e < 15; ++e) {
        uint16_t lu = srx_get16(rx_store + block * 4096 + e * 2);
        if (!(drop & (1U << e)) && !(lu == 0xFFFF && srx_erased(rx_store + block * 4096 + (1 + e) * 256, 256)))
            h.kept_mask |= (uint16_t)(1U << e);
    }
    h.old_crc = srp_crc32(0, rx_store + block * 4096, 4096);
    h.new_crc = srx_target(rx_store + block * 4096, drop, 12, RX_BLOCKS, block, target);
    h.objects = 1;
    h.object[0] = (srx_object_t){.obj = 5, .cls = SRP_CLASS_ATTENDANCE, .flags = SRX_OBJECT_CURSOR, .cursor_offset = 777};
    snprintf(h.object[0].name, sizeof(h.object[0].name), "/pending.jsonl");
    srx_encode(&h, target, sector);
    static uint8_t scratch[4096];
    assert(srx_write_record(&RX_IO, slot, sector, scratch));
}

static void redo_records(void)
{
    static uint8_t target[4096], expanded[4096], sector[4096], work[3 * 4096];
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(10, "uudurddfffffff" "f");
    uint16_t drop = (1U << 2) | (1U << 5) | (1U << 6);
    rx_record(0, 10, drop, 1, 0, 0x2760, target);
    srx_record_t r;
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, expanded) && !memcmp(expanded, target, 4096));
    assert(r.block == 10 && r.kept_mask == ((1U << 0) | (1U << 1) | (1U << 3) | (1U << 4)) && r.objects == 1);
    assert(!strcmp(r.object[0].name, "/pending.jsonl") && r.object[0].cursor_offset == 777);
    assert(srx_kept_page(sector, &r, 3) == sector + 512 + 2 * 256 && !srx_kept_page(sector, &r, 2));
    assert(srx_get16(target + 252) == srx_spiffs_magic(RX_BLOCKS, 10) && srx_get16(target + 254) == 12);
    assert(srx_get16(target + 2 * 2) == 0xFFFF && srx_erased(target + 3 * 256, 256));
    /* Torn header, torn page: invalid. */
    sector[9] ^= 1;
    assert(!srx_decode(sector, &r, NULL));
    sector[9] ^= 1;
    sector[600] ^= 1;
    assert(!srx_decode(sector, &r, NULL));
    /* Progress words. */
    assert(srx_attempts_used(0xFFFFFFFFU) == 0 && srx_attempts_used(0xFFFFFFF8U) == 3);
    assert(srx_clear_bits(&RX_IO, 0, SRX_OFF_ATTEMPTS, 1U));
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL) && srx_attempts_used(r.attempts) == 1 && !srx_is_source(&r));
    /* Discovery: claimed or not. */
    memset(rx_area + 3 * 4096, 0, 100);   /* a torn write in slot 3 */
    srx_area_t area;
    srx_discover(&RX_IO, true, sector, &area);
    assert(area.state == SRX_AREA_READY && area.slots == 8 && area.slot[0] == SRX_SLOT_OPEN && area.slot[3] == SRX_SLOT_TORN);
    assert(srx_slot_free(&area, 3) && !srx_slot_free(&area, 0) && srx_block_busy(&area, 10) && !srx_block_busy(&area, 11));
    srx_discover(&RX_IO, false, sector, &area);
    assert(area.state == SRX_AREA_FOREIGN && area.foreign == 1 && area.first_foreign == 3 && area.slot[0] == SRX_SLOT_OPEN);
    memset(rx_area + 3 * 4096, 0xFF, 4096);

    /* The decision table (C2, D4, D5). */
    static uint8_t raw[4096];
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL));
    memcpy(raw, rx_store + 10 * 4096, 4096);
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_REDO);          /* the old image */
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2750, false) == SRX_ACT_SOURCE);        /* another image */
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, true) == SRX_ACT_SOURCE);         /* the journal */
    memcpy(raw, target, 4096);
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_REASSERT);      /* complete */
    memset(raw, 0xFF, 4096);
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_REDO);          /* no magic */
    put16_at(raw + 252, srx_spiffs_magic(RX_BLOCKS, 10));
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_CHANGED);       /* mounted since */
    assert(srx_decide(&r, raw, NULL, RX_BLOCKS, 0x2760, false) == SRX_ACT_CHANGED);
    /* Round 1 review, D3: the window's erase cut short with the magic
     * reading valid (each byte of the block with its magic cleared erased or
     * kept, bits only set): redone. The same with a page programmed since in
     * an erased entry, or a kept byte cleared, or no kept byte left (a
     * mount's erase): changed. */
    for (unsigned trial = 0; trial < 64; ++trial) {
        memcpy(raw, rx_store + 10 * 4096, 4096);
        put16_at(raw + 252, 0);
        uint32_t state = 0x9E3779B9U * (trial + 1);
        for (uint32_t i = 0; i < 4096; ++i) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            if (trial % 2 ? state & 1U : (state & 7U) == 0) raw[i] = 0xFF;
            else if (trial % 4 == 3) raw[i] |= (uint8_t)state;    /* partly erased bits */
        }
        put16_at(raw + 252, srx_spiffs_magic(RX_BLOCKS, 10));
        assert(srx_torn_erase(&r, raw, target) && srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_REDO);
        assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2750, false) == SRX_ACT_SOURCE);
        assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, true) == SRX_ACT_SOURCE);
    }
    memcpy(raw, rx_store + 10 * 4096, 4096);
    memset(raw + 2048, 0xFF, 2048);
    put16_at(raw + 252, srx_spiffs_magic(RX_BLOCKS, 10));
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_REDO);
    raw[(1 + 9) * 256 + 7] = 0x12;                                                      /* an erased entry written */
    assert(!srx_torn_erase(&r, raw, target) && srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_CHANGED);
    memcpy(raw, rx_store + 10 * 4096, 4096);
    put16_at(raw + 252, srx_spiffs_magic(RX_BLOCKS, 10));
    raw[(1 + 0) * 256 + 40] &= 0x0F;                                                     /* a kept byte cleared */
    assert(srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_CHANGED);
    memset(raw, 0xFF, 4096);
    put16_at(raw + 254, 13);
    put16_at(raw + 252, srx_spiffs_magic(RX_BLOCKS, 10));
    memset(raw + (1 + 2) * 256, 0x33, 256);                                             /* a dropped entry written */
    put16_at(raw + 2 * 2, 9);
    assert(!srx_torn_erase(&r, raw, target) && srx_decide(&r, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_CHANGED);
    srx_record_t unconfirmed = r;
    unconfirmed.flags |= SRX_FLAG_UNCONFIRMED;
    assert(srx_decide(&unconfirmed, target, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_SOURCE);
    srx_record_t tired = r;
    tired.attempts = 0xFFFFFFF0U;
    memset(raw, 0xFF, 4096);
    assert(srx_decide(&tired, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_EXHAUSTED);
    srx_record_t done = r;
    done.done = 0;
    assert(srx_decide(&done, raw, target, RX_BLOCKS, 0x2760, false) == SRX_ACT_NONE);

    /* Recovery: a cut left the block without magic, half erased. */
    memset(rx_store + 10 * 4096, 0xFF, 2048);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0);
    assert(area.redone == 1 && area.slot[0] == SRX_SLOT_DONE && !memcmp(rx_store + 10 * 4096, target, 4096));
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL) && r.done != 0xFFFFFFFFU && srx_attempts_used(r.attempts) == 2);
    /* Re-assert: the block is the target, done not yet programmed. */
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(11, "duddddudffffff" "f");
    rx_record(1, 11, (1U << 2) | (1U << 3) | (1U << 4) | (1U << 5), 4, 0, 0x2760, target);
    memcpy(rx_store + 11 * 4096, target, 4096);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.reasserted == 1 &&
           area.slot[1] == SRX_SLOT_DONE);
    /* Another writer: a source, never redone, and sticky. */
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(12, "uuddufffffffff" "f");
    rx_record(2, 12, (1U << 2) | (1U << 3), 5, 0, 0x2760, target);
    rx_block(12, "uuuuuuufffffff" "f");                    /* mounted (erased) and written since */
    static uint8_t now[4096];
    memcpy(now, rx_store + 12 * 4096, 4096);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.to_sources == 1 &&
           area.slot[2] == SRX_SLOT_SOURCE && !memcmp(now, rx_store + 12 * 4096, 4096));
    memset(rx_store + 12 * 4096 + 252, 0, 2);                /* a later GC erase torn: no magic now */
    memcpy(now, rx_store + 12 * 4096, 4096);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.redone == 0 &&
           !memcmp(now, rx_store + 12 * 4096, 4096));
    /* An unconfirmed image's record: a source whatever the block says. */
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(13, "ddudddffffffff" "f");
    rx_record(0, 13, (1U << 0) | (1U << 1) | (1U << 3) | (1U << 4) | (1U << 5), 6, SRX_FLAG_UNCONFIRMED, 0x2760,
              target);
    memset(rx_store + 13 * 4096 + 252, 0, 2);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.slot[0] == SRX_SLOT_SOURCE &&
           area.redone == 0);
    /* The attempts tally: four, then a source. */
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(14, "uuddufffffffff" "f");
    rx_record(0, 14, (1U << 2) | (1U << 3), 7, 0, 0x2760, target);
    assert(srx_clear_bits(&RX_IO, 0, SRX_OFF_ATTEMPTS, 0xFU));
    memset(rx_store + 14 * 4096, 0xFF, 4096);
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.exhausted == 1 &&
           area.slot[0] == SRX_SLOT_SOURCE);
    /* A store write error leaves the record open for the caller. */
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_block(15, "uuddufffffffff" "f");
    rx_record(0, 15, (1U << 2) | (1U << 3), 8, 0, 0x2760, target);
    memset(rx_store + 15 * 4096 + 252, 0, 2);
    rx_fail_store_write = 3;
    rx_store_writes = 0;
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 1 && area.slot[0] == SRX_SLOT_OPEN);
    rx_fail_store_write = 0;
    /* A block that never verifies: retried twice, left without magic, a source. */
    rx_corrupt_after = 1;
    rx_windows_open = 0;
    srx_discover(&RX_IO, true, sector, &area);
    assert(srx_recover(&RX_IO, RX_BLOCKS, 0x2760, false, work, &area) == 0 && area.slot[0] == SRX_SLOT_SOURCE &&
           area.exhausted == 1 && rx_windows_open == 3);
    assert(!srx_has_magic(rx_store + 15 * 4096, RX_BLOCKS, 15));
    rx_corrupt_after = 0;
    /* Settling: every kept entry settled retires it; a reported one keeps it. */
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL) && srx_is_source(&r) && !srx_retired(&r) && !srx_retained(&r));
    assert(srx_clear_bits(&RX_IO, 0, SRX_OFF_SETTLE, (1U << 0) | (1U << 1) | (1U << 4)));
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL) && srx_retired(&r));
    srx_discover(&RX_IO, true, sector, &area);
    assert(area.slot[0] == SRX_SLOT_RETIRED && srx_slot_free(&area, 0));
    memset(rx_area, 0xFF, sizeof(rx_area));
    rx_record(0, 15, (1U << 2) | (1U << 3), 9, 0, 0x2760, target);
    assert(srx_clear_bits(&RX_IO, 0, SRX_OFF_SETTLE, SRX_SETTLE_SOURCE | (1U << 0) | (1U << 1) | (1U << 20)));
    memcpy(sector, rx_area, 4096);
    assert(srx_decode(sector, &r, NULL) && !srx_retired(&r) && srx_retained(&r));
    /* The reclaim journal and its NVS entries. */
    srp_reclaim_t j = {.flags = SRP_RECLAIM_CLAIMED | SRP_RECLAIM_ACTIVE, .reclaim_id = 3};
    srp_reclaim_seal(&j);
    assert(srp_reclaim_valid(&j));
    j.reclaim_id++;
    assert(!srp_reclaim_valid(&j));
    assert(sizeof(srp_reclaim_t) == 28 && srp_reclaim_entries_needed(false) == 4U * 3U && srp_reclaim_entries_needed(true) == 3U * 3U);
    assert(!strcmp(srx_area_name(SRX_AREA_FOREIGN), "FOREIGN"));
    /* The journal after recovery (C2). */
    srp_reclaim_t k = {.flags = SRP_RECLAIM_CLAIMED | SRP_RECLAIM_ACTIVE};
    assert(srx_journal_after_recovery(&k, false, 0, 0) && (k.flags & SRP_RECLAIM_SOURCES) && srp_reclaim_valid(&k));
    assert(!srx_journal_after_recovery(&k, false, 0, 0) && srx_sources_unread(&k, false) && !srx_sources_unread(&k, true));
    assert(srx_journal_after_recovery(&k, true, 0, 0) && k.flags == SRP_RECLAIM_CLAIMED);
    assert(srx_journal_after_recovery(&k, true, 2, 0) && (k.flags & SRP_RECLAIM_SOURCES) && (k.flags & SRP_RECLAIM_ACTIVE));
    srp_reclaim_t idle = {.flags = SRP_RECLAIM_CLAIMED};
    assert(!srx_journal_after_recovery(&idle, false, 0, 0) && !srx_journal_after_recovery(&idle, true, 0, 0));
    assert(!srx_sources_unread(&idle, false));
    /* Round 1 review, low: HELD follows the inspected area's source records,
     * so a later boot that cannot read the area knows bytes may be there. */
    assert(srx_journal_after_recovery(&idle, true, 0, 3) && idle.flags == (SRP_RECLAIM_CLAIMED | SRP_RECLAIM_HELD));
    assert(!srx_journal_after_recovery(&idle, false, 0, 0) && idle.flags == (SRP_RECLAIM_CLAIMED | SRP_RECLAIM_HELD));
    assert(srx_sources_unread(&idle, false) && !srx_sources_unread(&idle, true));
    assert(srx_journal_after_recovery(&idle, true, 0, 0) && idle.flags == SRP_RECLAIM_CLAIMED && srp_reclaim_valid(&idle));
    srx_area_t held_area;
    memset(&held_area, 0, sizeof(held_area));
    held_area.slots = 4;
    held_area.slot[1] = SRX_SLOT_SOURCE;
    held_area.slot[2] = SRX_SLOT_RETIRED;
    held_area.slot[3] = SRX_SLOT_SOURCE;
    assert(srx_sources_left(&held_area) == 2);
}

int main(void)
{
    classes();
    fills();
    cursors();
    records();
    journal();
    policy();
    budget();
    mirror();
    redo_plan();
    redo_records();
    puts("storage repair plan host tests passed");
    return 0;
}
