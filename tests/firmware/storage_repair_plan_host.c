/* ZKT 2.7.5 storage repair: the decisions that need no filesystem, and the
 * metadata mirror, with plain cc. The SPIFFS engine itself runs in
 * spiffs_repair_host.c against ESP-IDF's own SPIFFS. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "durable_queue.h"
#include "legacy_queue.h"
#include "reliability.h"
#include "storage_repair_mirror.h"
#include "storage_repair_plan.h"

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
    puts("storage repair plan host tests passed");
    return 0;
}
